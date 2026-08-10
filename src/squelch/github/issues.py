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

from ..core.log import get_logger
from ..core.models import ALL_STATUSES, RawArticle, Status, Verdict
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
    r"^## Summary\n\n(.*?)(?=\n\n\*\*Tags:|\n\n---|\n\n\*\*Source|\Z)",
    re.DOTALL | re.MULTILINE,
)

# GitHub's hard cap on an issue body is 65536 characters.
BODY_LIMIT = 60000
TRUNCATION_NOTE = "\n\n[truncated]"


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


def _neutralize(text: str) -> str:
    """Stop article text from closing our own markers."""
    return text.replace(ORIGINAL_CLOSE, ORIGINAL_CLOSE.replace("<!--", "<!- -"))


def render_body(
    meta: dict[str, Any],
    original: str,
    summary: str = "",
    verdict: Verdict | None = None,
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
    verdict: Verdict | None = None,
) -> str:
    front = yaml.safe_dump(meta, sort_keys=True, allow_unicode=True).strip()
    parts = [f"<!-- squelch\n{front}\n-->", ""]

    if summary:
        parts += ["## Summary", "", summary.strip(), ""]
        if verdict:
            tags = ", ".join(verdict.tags) if verdict.tags else "—"
            parts += [f"**Tags:** {tags} · **Score:** {verdict.score}/10", ""]

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
    )


class IssueStore:
    """CRUD over the issues that back the pipeline."""

    def __init__(self, client: GitHubClient) -> None:
        self.client = client
        self.repo = client.repo

    # -- reads --------------------------------------------------------------

    def list_by_status(self, status: Status, limit: int | None = None) -> list[IssueRecord]:
        payloads = self.client.paginate(
            f"/repos/{self.repo}/issues",
            {"labels": status.value, "state": "all", "sort": "created", "direction": "asc"},
        )
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

    def list_published_since(self, since: datetime) -> list[IssueRecord]:
        payloads = self.client.paginate(
            f"/repos/{self.repo}/issues",
            {
                "labels": Status.PUBLISHED.value,
                "state": "all",
                "since": since.isoformat(),
                "sort": "created",
                "direction": "desc",
            },
        )
        return [parse_issue(p) for p in payloads if "pull_request" not in p]

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

    def apply_verdict(self, issue: IssueRecord, verdict: Verdict) -> None:
        """Move a raw issue to its next state based on the LLM's judgement."""
        meta = dict(issue.meta)
        meta["verdict_reason"] = verdict.reason
        meta["score"] = verdict.score
        meta["tags"] = verdict.tags

        if verdict.relevant:
            body = render_body(meta, issue.text, verdict.summary, verdict)
            labels = self._swap_status(issue.labels, Status.READY)
            labels += [f"topic:{tag}" for tag in _label_safe_tags(verdict.tags)]
            self._patch(issue.number, body=body, labels=sorted(set(labels)))
            log.info("#%d -> ready (score %d)", issue.number, verdict.score)
        else:
            body = render_body(meta, issue.text)
            labels = self._swap_status(issue.labels, Status.REJECTED)
            self._patch(
                issue.number,
                body=body,
                labels=labels,
                state="closed",
                state_reason="not_planned",
            )
            log.info("#%d -> rejected (%s)", issue.number, verdict.reason[:60])

    def mark_published(self, issue: IssueRecord, discord_message_id: str | None) -> None:
        meta = dict(issue.meta)
        meta["published_at_discord"] = datetime.now().astimezone().isoformat()
        if discord_message_id:
            meta["discord_message_id"] = discord_message_id
        self._patch(
            issue.number,
            body=render_body(meta, issue.text, issue.summary),
            labels=self._swap_status(issue.labels, Status.PUBLISHED),
        )
        log.info("#%d -> published", issue.number)

    def close(self, number: int, reason: str = "completed") -> None:
        self._patch(number, state="closed", state_reason=reason)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _swap_status(labels: list[str], new: Status) -> list[str]:
        kept = [label for label in labels if not label.startswith("status:")]
        return [*kept, new.value]

    def _patch(self, number: int, **fields: Any) -> None:
        self.client.request("PATCH", f"/repos/{self.repo}/issues/{number}", json=fields)
