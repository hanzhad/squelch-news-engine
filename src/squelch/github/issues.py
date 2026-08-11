"""Issues as the database.

Every article is one issue. The machine-readable part lives in a YAML block
inside an HTML comment at the top of the body, which GitHub renders as nothing
and which survives editing by hand. Labels carry the state.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..core.config import Channel
from ..core.log import get_logger
from ..core.models import (
    ALL_STATUSES,
    Classification,
    RawArticle,
    SkillsReview,
    Status,
    Summary,
)
from ..core.urls import canonicalize, url_uid
from .client import GitHubClient

log = get_logger(__name__)

# The project was briefly called aetherfeed, and issues written back then are
# still in the queue. Reading accepts either spelling; writing only emits the
# current one, so old issues migrate the first time they are rewritten.
_NAME = r"(?:squelch|aetherfeed)"
META_RE = re.compile(rf"^\s*<!--\s*{_NAME}\s*\n(.*?)\n-->", re.DOTALL)
ORIGINAL_OPEN = "<!-- squelch:original -->"
ORIGINAL_CLOSE = "<!-- /squelch:original -->"
ORIGINAL_RE = re.compile(
    rf"<!-- {_NAME}:original -->\n(.*?)\n<!-- /{_NAME}:original -->", re.DOTALL
)
# The verdict line sits between the summary and the rule, so it has to end the
# match too — otherwise it is read back as part of the summary and re-rendered
# into the body on every subsequent write.
SUMMARY_RE = re.compile(
    r"^## Summary\n\n(.*?)"
    r"(?=\n\n\*\*Kept:|\n\n\*\*Rejected:|\n\n\*\*Tags:|\n\n---|\n\n\*\*Source|\Z)",
    re.DOTALL | re.MULTILINE,
)

# GitHub's hard cap on an issue body is 65536 characters.
BODY_LIMIT = 60000
TRUNCATION_NOTE = "\n\n[truncated]"

# One label per delivery target. Status is a single line — raw, relevant, ready,
# published — and delivery is not: an article is out on some channels and not
# others at the same time. That does not fit in one label, so it gets its own.
SENT_PREFIX = "sent:"


class IssueRecord(BaseModel):
    """One article, as it currently exists on GitHub."""

    number: int
    title: str
    labels: list[str] = Field(default_factory=list)
    state: str = "open"
    html_url: str = ""
    created_at: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    original: str = ""
    # Kept verbatim so that issues opened by hand — which have no metadata
    # block and no original marker — still carry their text into the pipeline.
    raw_body: str = ""
    # The reaction rollup GitHub sends with every issue payload, so counting
    # votes costs the listing request and nothing per issue.
    reactions: dict[str, Any] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        """The article text to reason about, whoever created the issue."""
        return self.original or self.raw_body

    @property
    def uid(self) -> str:
        return str(self.meta.get("uid", ""))

    @property
    def url(self) -> str:
        return str(self.meta.get("url", ""))

    @property
    def source(self) -> str:
        return str(self.meta.get("source", ""))

    @property
    def image(self) -> str:
        return str(self.meta.get("image", ""))

    @property
    def delivered_to(self) -> set[str]:
        """Channels that have already delivered this article."""
        return {
            label[len(SENT_PREFIX) :] for label in self.labels if label.startswith(SENT_PREFIX)
        }

    @property
    def facts(self) -> dict[str, int]:
        """Numbers the scraper counted, for the channels that show them."""
        record = self.meta.get("facts")
        if not isinstance(record, dict):
            return {}
        return {k: v for k, v in record.items() if isinstance(v, int)}

    @property
    def review(self) -> dict[str, Any]:
        """The skill-by-skill reading of this repository, if the rubric wrote one.

        Returned as the raw mapping rather than a model: it comes back out of
        YAML that a human is free to edit in the issue, so every reader of it
        treats its shape as a suggestion.
        """
        record = self.meta.get("review")
        return record if isinstance(record, dict) else {}

    def delivery(self, channel: str) -> dict[str, Any]:
        """What a channel recorded when it delivered — timestamps, message ids."""
        record = (self.meta.get("delivery") or {}).get(channel)
        return record if isinstance(record, dict) else {}

    @property
    def approvals(self) -> int:
        """👍 reactions — the community's vote to rescue a rejected article."""
        count = self.reactions.get("+1", 0)
        return count if isinstance(count, int) else 0

    @property
    def status(self) -> Status | None:
        # Walked in lifecycle order, not label order, so an issue that somehow
        # carries two status labels answers the same way every time.
        present = set(self.labels)
        for status in ALL_STATUSES:
            if status.value in present:
                return status
        return None


MAX_TOPIC_LABELS = 3


def _label_safe_tags(tags: list[str]) -> list[str]:
    """Last line of defence before a tag becomes a permanent label.

    The filter already narrows tags to the configured topics, but this is the
    only place that actually creates labels, and an invented one would live in
    the repository forever with no colour and no description.
    """
    kept: list[str] = []
    for tag in tags:
        slug = re.sub(r"[^a-z0-9._-]+", "-", tag.strip().lower()).strip("-")
        if slug and slug not in kept:
            kept.append(slug)
    return kept[:MAX_TOPIC_LABELS]


def _verdict_from_meta(meta: dict[str, Any], relevant: bool = True) -> Classification | None:
    """Rebuild the stage-one verdict that was stored when the article was judged.

    Later stages rewrite the whole body, so they need the verdict back to
    re-render the line a reader sees. It lives in the metadata block precisely
    so it survives that round trip. Relevance is not stored there — the status
    label is the source of truth for that, so the caller passes it in.
    """
    if "verdict_reason" not in meta:
        return None
    tags = meta.get("tags") or []
    return Classification(
        relevant=relevant,
        reason=str(meta.get("verdict_reason", "")),
        tags=[str(tag) for tag in tags],
        score=int(meta.get("score") or 0),
    )


def _neutralize(text: str) -> str:
    """Stop article text from closing our own markers."""
    return text.replace(ORIGINAL_CLOSE, ORIGINAL_CLOSE.replace("<!--", "<!- -"))


def render_body(
    meta: dict[str, Any],
    original: str,
    summary: str = "",
    verdict: Classification | None = None,
) -> str:
    """Compose an issue body from its parts, trimmed to fit GitHub's limit."""
    body = _render(meta, _neutralize(original.strip()), summary, verdict)
    if len(body) <= BODY_LIMIT:
        return body

    # Take the overflow out of the article text, which is the only part that is
    # both large and expendable. Computed in one pass rather than by re-rendering
    # until it fits: a summary big enough to blow the limit on its own would
    # otherwise never converge.
    text = _neutralize(original.strip())
    room = len(text) - (len(body) - BODY_LIMIT) - len(TRUNCATION_NOTE)
    trimmed = text[:room] + TRUNCATION_NOTE if room > 0 else TRUNCATION_NOTE.strip()

    body = _render(meta, trimmed, summary, verdict)
    # Only reachable when the metadata and summary alone exceed the limit; a
    # blunt cut beats a rejected API call.
    return body if len(body) <= BODY_LIMIT else body[:BODY_LIMIT]


def _render(
    meta: dict[str, Any],
    original: str,
    summary: str = "",
    verdict: Classification | None = None,
) -> str:
    front = yaml.safe_dump(meta, sort_keys=True, allow_unicode=True).strip()
    parts = [f"<!-- squelch\n{front}\n-->", ""]

    if summary:
        parts += ["## Summary", "", summary.strip(), ""]

    # The verdict is rendered whether or not a summary exists yet: an article
    # waiting for its write-up should show why it was kept, and a rejected one
    # must show why it was dropped where a human will actually see it.
    if verdict:
        tags = ", ".join(verdict.tags) if verdict.tags else "—"
        heading = "Kept" if verdict.relevant else "Rejected"
        parts += [
            f"**{heading}:** {verdict.reason.strip()}",
            "",
            f"**Tags:** {tags} · **Score:** {verdict.score}/10",
            "",
        ]

    parts += ["---", ""]
    published = meta.get("published_at") or "unknown"
    parts += [
        f"**Source:** `{meta.get('source', '?')}`",
        f"**Link:** {meta.get('url', '')}",
        f"**Published:** {published}",
        "",
        "<details>",
        "<summary>Original text</summary>",
        "",
        ORIGINAL_OPEN,
        original,
        ORIGINAL_CLOSE,
        "",
        "</details>",
    ]

    return "\n".join(parts)


def parse_body(body: str) -> tuple[dict[str, Any], str, str]:
    """Split a rendered body back into (meta, summary, original)."""
    meta: dict[str, Any] = {}
    match = META_RE.search(body or "")
    if match:
        try:
            parsed = yaml.safe_load(match.group(1))
            if isinstance(parsed, dict):
                meta = parsed
        except yaml.YAMLError:
            log.warning("could not parse metadata block")

    original_match = ORIGINAL_RE.search(body or "")
    original = original_match.group(1).strip() if original_match else ""

    summary_match = SUMMARY_RE.search(body or "")
    summary = summary_match.group(1).strip() if summary_match else ""

    return meta, summary, original


FORM_SECTION_RE = re.compile(
    r"^###[ \t]+(.+?)[ \t]*\n+(.*?)(?=\n###[ \t]|\Z)", re.DOTALL | re.MULTILINE
)
URL_IN_TEXT_RE = re.compile(r"https?://\S+")
NO_RESPONSE = "_No response_"


def meta_from_form(body: str) -> dict[str, Any]:
    """Recover metadata from an issue opened through the suggestion form.

    GitHub issue forms cannot emit our HTML comment block, so a community
    submission arrives as plain ``### Heading`` sections. Without this the
    article would travel through the whole pipeline with no URL and be
    published as a headline pointing nowhere.
    """
    sections = {
        heading.strip().lower(): text.strip()
        for heading, text in FORM_SECTION_RE.findall(body or "")
    }
    if not sections:
        return {}

    link_match = URL_IN_TEXT_RE.search(sections.get("link", ""))
    if not link_match:
        return {}

    url = canonicalize(link_match.group(0).rstrip(").,"))
    source = sections.get("source", "")
    if not source or source == NO_RESPONSE:
        source = "community"

    return {
        "uid": url_uid(url),
        "url": url,
        "source": source,
        "submitted_by": "form",
    }


def parse_issue(payload: dict[str, Any]) -> IssueRecord:
    body = payload.get("body") or ""
    meta, summary, original = parse_body(body)
    if not meta:
        meta = meta_from_form(body)
    return IssueRecord(
        number=payload["number"],
        title=payload["title"],
        labels=[label["name"] for label in payload.get("labels", [])],
        state=payload.get("state", "open"),
        html_url=payload.get("html_url", ""),
        created_at=payload.get("created_at"),
        meta=meta,
        summary=summary,
        original=original,
        raw_body=payload.get("body") or "",
        reactions=payload.get("reactions") or {},
    )


class IssueStore:
    """CRUD over the issues that back the pipeline."""

    def __init__(self, client: GitHubClient) -> None:
        self.client = client
        self.repo = client.repo

    # -- reads --------------------------------------------------------------

    def list_by_status(
        self,
        status: Status,
        limit: int | None = None,
        since: datetime | None = None,
    ) -> list[IssueRecord]:
        params: dict[str, Any] = {
            "labels": status.value,
            "state": "all",
            "sort": "created",
            "direction": "asc",
        }
        if since is not None:
            # GitHub filters on last update, not creation. That is the useful
            # sense here: an article the pipeline still touches stays in view.
            params["since"] = since.isoformat()
        payloads = self.client.paginate(f"/repos/{self.repo}/issues", params)
        records = [
            parse_issue(p)
            for p in payloads
            # The issues endpoint also returns pull requests; they are not ours.
            if "pull_request" not in p
        ]
        if limit is not None:
            records = records[:limit]
        log.info("found %d issues with %s", len(records), status.value)
        return records

    def list_pending(
        self,
        channel: str,
        limit: int | None = None,
        *,
        status: Status = Status.READY,
        since: datetime | None = None,
    ) -> list[IssueRecord]:
        """Articles at ``status`` this channel has not delivered yet.

        The default is READY and never PUBLISHED: an article closes once every
        channel that was enabled at the time had it, and turning on a new
        channel must not replay the whole archive into it. The rejected channel
        consumes REJECTED instead — same bookkeeping, different queue.
        """
        pending = [
            issue
            for issue in self.list_by_status(status, since=since)
            if channel not in issue.delivered_to
        ]
        if limit is not None:
            pending = pending[:limit]
        log.info("%d article(s) pending for %s", len(pending), channel)
        return pending

    def list_in_feed(
        self, limit: int | None = None, since: datetime | None = None
    ) -> list[IssueRecord]:
        """Everything that has cleared the LLM, whether or not Discord has it.

        The site is one delivery channel among several, not a gate on the
        others: a Discord outage must not stop the page, and an unbuilt page
        must not stop Discord. Hence both statuses — ready articles belong on
        the site the moment they are written up.

        ``since`` bounds how far back it reads. Published issues accumulate
        forever, so an unbounded read would page through the entire history on
        every rebuild and get slower every week.
        """
        seen: dict[int, IssueRecord] = {}
        for status in (Status.READY, Status.PUBLISHED):
            for issue in self.list_by_status(status, since=since):
                seen[issue.number] = issue
        records = [seen[number] for number in sorted(seen, reverse=True)]
        return records[:limit] if limit is not None else records

    def list_published_since(self, since: datetime) -> list[IssueRecord]:
        """What reached the feed in the window, for the weekly digest."""
        seen: dict[int, IssueRecord] = {}
        for status in (Status.READY, Status.PUBLISHED):
            payloads = self.client.paginate(
                f"/repos/{self.repo}/issues",
                {
                    "labels": status.value,
                    "state": "all",
                    "since": since.isoformat(),
                    "sort": "created",
                    "direction": "desc",
                },
            )
            for payload in payloads:
                if "pull_request" not in payload:
                    record = parse_issue(payload)
                    seen[record.number] = record
        return [seen[number] for number in sorted(seen, reverse=True)]

    # -- writes -------------------------------------------------------------

    def create_article(self, article: RawArticle) -> IssueRecord:
        meta = {
            "uid": article.uid,
            "url": article.url,
            "source": article.source,
            "published_at": (
                article.published_at.isoformat() if article.published_at else None
            ),
            "scraped_by": "squelch",
        }
        if article.image:
            # Only when there is one: an empty key in every body is noise in a
            # format people read by hand.
            meta["image"] = article.image
        if article.facts:
            # What the source counted rather than read. Same reason it is kept
            # out of the body: these are the numbers a reader weighs a verdict
            # against, so they reach the channel from here, not from a model.
            meta["facts"] = article.facts
        payload = self.client.request(
            "POST",
            f"/repos/{self.repo}/issues",
            json={
                "title": article.title,
                "body": render_body(meta, article.body),
                "labels": [Status.RAW.value, f"source:{article.source}"],
            },
        ).json()
        record = parse_issue(payload)
        log.info("created #%d %s", record.number, record.title[:70])
        return record

    def apply_classification(self, issue: IssueRecord, verdict: Classification) -> None:
        """Stage one result: keep the article for write-up, or close it."""
        meta = dict(issue.meta)
        meta["verdict_reason"] = verdict.reason
        meta["score"] = verdict.score
        meta["tags"] = verdict.tags

        if verdict.relevant:
            labels = self._swap_status(issue.labels, Status.RELEVANT)
            labels += [f"topic:{tag}" for tag in _label_safe_tags(verdict.tags)]
            self._patch(
                issue.number,
                body=render_body(meta, issue.text, verdict=verdict),
                labels=sorted(set(labels)),
            )
            log.info("#%d -> relevant (score %d)", issue.number, verdict.score)
        else:
            self._patch(
                issue.number,
                # The reason goes in the body, not only the metadata comment:
                # skimming what the classifier threw away is how you find out
                # the policy is too tight, and nobody reads raw markdown for that.
                body=render_body(meta, issue.text, verdict=verdict),
                labels=self._swap_status(issue.labels, Status.REJECTED),
                state="closed",
                state_reason="not_planned",
            )
            log.info("#%d -> rejected (%s)", issue.number, verdict.reason[:60])

    def apply_summary(
        self,
        issue: IssueRecord,
        summary: Summary,
        review: SkillsReview | None = None,
    ) -> None:
        """Stage two result: the article is written up and ready to publish.

        The review, where the rubric wrote one, is stored in the metadata block
        rather than rendered into the body: it is the publisher's material, and
        the body is already the place a person reads the article itself.
        Written in the same request as the summary, so an issue never sits ready
        with half of what the channel is going to post.
        """
        meta = dict(issue.meta)
        if review is not None:
            meta["review"] = review.model_dump()
        self._patch(
            issue.number,
            body=render_body(meta, issue.text, summary.summary, _verdict_from_meta(meta)),
            labels=self._swap_status(issue.labels, Status.READY),
        )
        log.info("#%d -> ready", issue.number)

    def record_delivery(
        self,
        issue: IssueRecord,
        channel: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Note that ``channel`` has delivered this article.

        Written in two requests, body before label, and the order matters. If a
        run dies between them the article carries proof it is already out — a
        Discord message id, say — while still looking undelivered, so the next
        run can see it and relabel instead of posting the thing twice. The other
        order would lose the id and produce a duplicate.

        Nothing here closes the issue: that decision belongs to
        ``close_delivered``, which sees all the channels at once.

        The record is updated in place, so a caller delivering to several
        channels in a row reads back its own writes.
        """
        meta = dict(issue.meta)
        delivery = dict(meta.get("delivery") or {})
        delivery[channel] = {
            "at": datetime.now().astimezone().isoformat(),
            **(details or {}),
        }
        meta["delivery"] = delivery
        # The rejected channel delivers rejected issues, and their body must
        # keep saying "Rejected" after the rewrite — hence the label check.
        verdict = _verdict_from_meta(meta, relevant=issue.status is not Status.REJECTED)
        self._patch(
            issue.number,
            body=render_body(meta, issue.text, issue.summary, verdict),
        )
        issue.meta = meta

        labels = sorted({*issue.labels, f"{SENT_PREFIX}{channel}"})
        self._patch(issue.number, labels=labels)
        issue.labels = labels
        log.info("#%d delivered to %s", issue.number, channel)

    def rescue(self, issue: IssueRecord, approvals: int) -> None:
        """Community override of a rejection: back into the queue as relevant.

        Relevant rather than raw on purpose — the classifier already judged
        this text once and would say the same thing again. A vote is the same
        human override as flipping the label by hand, so it lands where a hand
        edit would: waiting for its write-up. The topic labels the classifier
        chose are applied now, since the rejection path never did.
        """
        meta = dict(issue.meta)
        if meta.get("verdict_reason"):
            meta["rejected_reason"] = meta["verdict_reason"]
        meta["verdict_reason"] = f"Rescued by community vote ({approvals} 👍) after rejection"
        tags = [str(tag) for tag in meta.get("tags") or []]
        labels = self._swap_status(issue.labels, Status.RELEVANT)
        labels += [f"topic:{tag}" for tag in _label_safe_tags(tags)]
        self._patch(
            issue.number,
            body=render_body(meta, issue.text, verdict=_verdict_from_meta(meta)),
            labels=sorted(set(labels)),
            state="open",
            state_reason="reopened",
        )
        log.info("#%d -> rescued by %d reaction(s)", issue.number, approvals)

    def close_delivered(self, channels: list[Channel]) -> list[int]:
        """Close every ready article that all its routed channels have delivered.

        Deliberately not done by whichever channel happens to finish last. That
        channel could die between its own label and the close, and then nobody
        would ever look at the issue again — every channel has had it, so no
        queue contains it. A separate pass owns the transition, re-derives the
        answer from the labels every time, and therefore also picks up articles
        that only qualified because a channel was switched off in config.

        Which channels count is decided per article: a channel whose routing
        does not want an article must not hold it open waiting for a delivery
        that will never come.
        """
        if not channels:
            # Otherwise "everyone delivered" is vacuously true and the whole
            # queue closes without going anywhere.
            log.warning("no channels are enabled, leaving ready articles open")
            return []

        closed: list[int] = []
        for issue in self.list_by_status(Status.READY):
            required = [c.id for c in channels if c.wants(set(issue.labels))]
            if not required:
                # Routed nowhere at all — a config hole, not a publication.
                # Closing it would mark as published something no reader saw.
                log.warning("#%d matches no enabled channel, leaving it open", issue.number)
                continue
            missing = set(required) - issue.delivered_to
            if missing:
                log.debug("#%d still owes %s", issue.number, ", ".join(sorted(missing)))
                continue
            self._patch(
                issue.number,
                labels=self._swap_status(issue.labels, Status.PUBLISHED),
                # Closed once it is out everywhere, so the open list is exactly
                # the work still in flight. The archive reads closed issues too,
                # so nothing disappears from the site.
                state="closed",
                state_reason="completed",
            )
            closed.append(issue.number)
            log.info("#%d -> published and closed", issue.number)
        return closed

    def close(self, number: int, reason: str = "completed") -> None:
        self._patch(number, state="closed", state_reason=reason)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _swap_status(labels: list[str], new: Status) -> list[str]:
        kept = [label for label in labels if not label.startswith("status:")]
        return [*kept, new.value]

    def _patch(self, number: int, **fields: Any) -> None:
        self.client.request("PATCH", f"/repos/{self.repo}/issues/{number}", json=fields)
