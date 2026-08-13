"""A community case as an issue, so the reading survives the reply.

Same database, same body format, its own module — the precedent is
``digests.py``, and the reasoning is the same one twice over. Reading a case and
answering it are separate stages because the model's answer must outlive a dead
connection: a reply that failed to post is worth re-sending, not re-writing, and
a run that dies between the two must not answer the same post twice.

What makes a case issue a case is its label axis, ``case:``, and the ``status:``
label it deliberately does not have. Classify, summarize, the site build and the
window a roundup reads all query on ``status:``, so every one of them steps over
these — which is the only thing standing between the forum and the front page.
Somebody's half-finished experiment is not news, and was never offered as any.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import yaml

from ..core.config import Channel
from ..core.log import get_logger
from ..core.models import ALL_CASE_STATUSES, CasePost, CaseReading, CaseStatus
from ..core.text import trim
from .client import GitHubClient
from .issues import BODY_LIMIT, SENT_PREFIX, IssueRecord, parse_issue

log = get_logger(__name__)

TITLE_LIMIT = 250
# GitHub's `since` is a filter on last update, and a case is touched twice after
# it is opened. A little slack over the ingest window keeps a post that was
# answered days ago from being read as new when its issue drops out of view.
DEDUP_SLACK_DAYS = 4


def case_labels() -> list[str]:
    return [status.value for status in ALL_CASE_STATUSES]


def render_case_body(meta: dict[str, Any], post: str, reading: CaseReading | None = None) -> str:
    """The stored case: metadata, the reading, then the post as it was written.

    The YAML block is the source of truth — it is what the publisher reads, and
    editing it is how a person changes what gets posted back. The markdown under
    it is a preview, and the post itself is kept verbatim at the bottom: an issue
    that has lost the text it is about is an issue nobody can check the answer
    against.
    """
    front = yaml.safe_dump(meta, sort_keys=True, allow_unicode=True).strip()
    parts = [f"<!-- squelch\n{front}\n-->", ""]

    if reading is not None:
        parts += [f"**{reading.claim.strip()}**", ""]
        for heading, items in (
            ("Checkable", reading.checkable),
            ("Taken on faith", reading.assumed),
            ("Worth measuring", reading.measure),
        ):
            lines = [str(item).strip() for item in items if str(item).strip()]
            if lines:
                parts += [f"### {heading}", "", *(f"- {line}" for line in lines), ""]

    parts += [
        "---",
        "",
        f"**Posted by:** {meta.get('author') or 'unknown'}",
        f"**Thread:** {meta.get('url', '')}",
        f"**Posted:** {meta.get('posted_at') or 'unknown'}",
        "",
        "<details>",
        "<summary>The post</summary>",
        "",
        "<!-- squelch:original -->",
        # Not neutralised the way an article's text is: what closes the marker
        # there is our own comment syntax, and this text is quoted, never
        # re-rendered into anything that would execute it.
        post.strip(),
        "<!-- /squelch:original -->",
        "",
        "</details>",
    ]

    body = "\n".join(parts)
    if len(body) <= BODY_LIMIT:
        return body
    head = f"<!-- squelch\n{front}\n-->"
    return head[:BODY_LIMIT] if len(head) > BODY_LIMIT else f"{head}\n\n_[body omitted]_"


def post_text(issue: IssueRecord) -> str:
    """The post this issue is about, and never anything else.

    Deliberately not ``IssueRecord.text``. That falls back to the whole raw body
    for issues opened by hand, which is right for an article — a person pasting
    one into the tracker is the article's only text. Here it would be a trap: a
    post with no words at all (a screenshot, a sticker, a bare link) renders an
    empty original, and the fallback would hand the model the issue's own YAML
    block and headings as though the person had written them, then store that
    back as the post on the next write.

    An empty answer is the honest one. The prompt says so in as many words.
    """
    return issue.original


def reading_from_meta(meta: dict[str, Any]) -> CaseReading | None:
    """Read a stored reading back out of an issue, or None if it is unusable.

    Returns None rather than raising for the same reason the digest does: one
    hand-edited block must not take down a run that has other cases waiting
    behind it. An empty claim counts as unusable — a block whose indentation was
    broken parses as nothing, every field below validates as its empty default,
    and the forum would get a reply with nothing in it.
    """
    record = meta.get("reading")
    if not isinstance(record, dict) or not str(record.get("claim") or "").strip():
        return None
    try:
        return CaseReading.model_validate(record)
    except Exception as exc:  # noqa: BLE001 - a hand-edited block can fail any number of ways
        log.error("stored reading cannot be read back: %s", exc)
        return None


class CaseStore:
    """CRUD over the issues that hold community cases."""

    def __init__(self, client: GitHubClient) -> None:
        self.client = client
        self.repo = client.repo

    # -- reads --------------------------------------------------------------

    def _list(
        self, label: str, state: str = "all", since: datetime | None = None
    ) -> list[IssueRecord]:
        params: dict[str, Any] = {
            "labels": label,
            "state": state,
            "sort": "created",
            "direction": "asc",
        }
        if since is not None:
            params["since"] = since.isoformat()
        payloads = self.client.paginate(f"/repos/{self.repo}/issues", params)
        return [parse_issue(p) for p in payloads if "pull_request" not in p]

    def list_by_status(self, status: CaseStatus, limit: int | None = None) -> list[IssueRecord]:
        records = self._list(status.value, state="open")
        if limit is not None:
            records = records[:limit]
        log.info("found %d case(s) with %s", len(records), status.value)
        return records

    def known_threads(self, window_days: int) -> set[str]:
        """Thread ids the pipeline already has an issue for.

        Bounded by the same window the ingest reads, plus slack: an answered
        case is closed and will never be looked at again, so carrying every one
        of them forever would make this query cost more every week for an answer
        that only matters about yesterday.
        """
        since = datetime.now(UTC) - timedelta(days=window_days + DEDUP_SLACK_DAYS)
        known = {
            thread
            for label in case_labels()
            for issue in self._list(label, state="all", since=since)
            if (thread := str(issue.meta.get("thread_id") or ""))
        }
        log.debug("%d case(s) already known in the window", len(known))
        return known

    def list_pending(self, channel: str, limit: int | None = None) -> list[IssueRecord]:
        """Cases whose reading is written and whose reply this channel has not posted."""
        pending = [
            issue
            for issue in self.list_by_status(CaseStatus.READ)
            if channel not in issue.delivered_to
        ]
        pending.sort(key=lambda issue: issue.number)
        if limit is not None:
            pending = pending[:limit]
        log.info("%d case(s) pending for %s", len(pending), channel)
        return pending

    # -- writes -------------------------------------------------------------

    def create(self, post: CasePost) -> IssueRecord:
        meta: dict[str, Any] = {
            "thread_id": post.thread_id,
            "url": post.url,
            "author": post.author,
            "posted_at": post.posted_at.isoformat() if post.posted_at else None,
            "tags": list(post.tags),
            "read_from": "discord",
        }
        payload = self.client.request(
            "POST",
            f"/repos/{self.repo}/issues",
            json={
                "title": trim(post.title, TITLE_LIMIT),
                "body": render_case_body(meta, post.body),
                "labels": [CaseStatus.NEW.value],
            },
        ).json()
        record = parse_issue(payload)
        log.info("opened case #%d %s", record.number, record.title[:70])
        return record

    def apply_reading(self, issue: IssueRecord, reading: CaseReading) -> None:
        """Store what the model made of this case and queue it for its reply."""
        meta = dict(issue.meta)
        meta["reading"] = reading.model_dump()
        meta["read_at"] = datetime.now(UTC).isoformat()
        self.client.request(
            "PATCH",
            f"/repos/{self.repo}/issues/{issue.number}",
            json={
                "body": render_case_body(meta, post_text(issue), reading),
                "labels": self._swap_status(issue.labels, CaseStatus.READ),
            },
        )
        issue.meta = meta
        log.info("#%d -> read", issue.number)

    def record_delivery(
        self,
        issue: IssueRecord,
        channel: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Note that the reply is in the thread.

        Body before label, as everywhere else in this repository and for the
        same reason: a run that dies between the two leaves the message id on
        the issue while it still looks undelivered, so the next run sees the id
        and relabels instead of posting a second reply under somebody's post.

        Nothing here closes anything — ``close_delivered`` owns that.
        """
        if not issue.meta:
            log.error("#%d has no readable metadata; recording the label only", issue.number)
            labels = sorted({*issue.labels, f"{SENT_PREFIX}{channel}"})
            self._patch(issue.number, labels=labels)
            issue.labels = labels
            return

        meta = dict(issue.meta)
        delivery = dict(meta.get("delivery") or {})
        delivery[channel] = {
            "at": datetime.now().astimezone().isoformat(),
            **(details or {}),
        }
        meta["delivery"] = delivery
        self._patch(
            issue.number,
            body=render_case_body(meta, post_text(issue), reading_from_meta(meta)),
        )
        issue.meta = meta

        labels = sorted({*issue.labels, f"{SENT_PREFIX}{channel}"})
        self._patch(issue.number, labels=labels)
        issue.labels = labels
        log.info("#%d answered in %s", issue.number, channel)

    def close_delivered(self, channels: list[Channel]) -> list[int]:
        """Close every case whose reply has gone out on all its channels."""
        if not channels:
            log.warning("no case channel is enabled, leaving cases open")
            return []

        required = {channel.id for channel in channels}
        closed: list[int] = []
        for issue in self.list_by_status(CaseStatus.READ):
            missing = required - issue.delivered_to
            if missing:
                log.debug("#%d still owes %s", issue.number, ", ".join(sorted(missing)))
                continue
            self._patch(
                issue.number,
                labels=self._swap_status(issue.labels, CaseStatus.ANSWERED),
                state="closed",
                state_reason="completed",
            )
            closed.append(issue.number)
            log.info("#%d -> answered and closed", issue.number)
        return closed

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _swap_status(labels: list[str], new: CaseStatus) -> list[str]:
        """Move along the case axis, leaving every other label alone.

        Its own helper rather than ``IssueStore._swap_status``: that one strips
        ``status:``, and a case has none — passing one through it would leave
        both case labels in place and the issue in two states at once.
        """
        kept = [label for label in labels if not label.startswith("case:")]
        return sorted({*kept, new.value})

    def _patch(self, number: int, **fields: Any) -> None:
        self.client.request("PATCH", f"/repos/{self.repo}/issues/{number}", json=fields)
