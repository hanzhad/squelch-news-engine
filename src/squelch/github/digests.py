"""A roundup as an issue, so that writing it and posting it are separate acts.

The digest used to go Gemini → Discord inside one process, which made it the
one stage with nothing behind it: a webhook that failed after the model had
answered cost the whole roundup and one of the day's scarce strong-model
requests, and a re-run posted a second copy of whatever did get through.

So a digest lands in the database like everything else. The build stage writes
an issue; the delivery stage posts it and records the message id on it, which
is the same proof-of-delivery an article carries. What that buys, beyond not
losing work: a roundup can be read — and edited — before it goes out, which is
the thing the issue list was always good for and the digest never got.

A digest issue is deliberately inert to the article pipeline. It carries no
``status:`` label, so every article query — classify, summarize, the site
build, and the window the *next* digest reads — steps straight over it. The
precedent is ``ledger.py``: a non-article issue, in the same database, with a
label of its own.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import yaml

from ..core.config import Channel
from ..core.log import get_logger
from ..core.models import Digest, Period
from ..core.text import trim
from .client import GitHubClient
from .issues import BODY_LIMIT, SENT_PREFIX, IssueRecord, parse_issue

log = get_logger(__name__)

# The kind label: `digest:daily`, `digest:weekly`. A separate axis from
# `status:`, which is what keeps these issues out of every article query.
DIGEST_PREFIX = "digest:"
# GitHub rejects issue titles over 256 characters, and a headline is a sentence.
TITLE_LIMIT = 250


def digest_label(period: Period) -> str:
    return f"{DIGEST_PREFIX}{period.value}"


def digest_labels() -> list[str]:
    return [digest_label(period) for period in Period]


def render_digest_body(meta: dict[str, Any]) -> str:
    """The stored roundup, machine-readable first and legible underneath.

    The YAML block is the source of truth — it is what the publisher reads and
    what a person edits to change what gets posted. The markdown below it is a
    preview of the same thing, and says so: two copies of the text in one body
    would otherwise be an invitation to fix the wrong one.
    """
    front = yaml.safe_dump(meta, sort_keys=True, allow_unicode=True).strip()
    parts = [f"<!-- squelch\n{front}\n-->", ""]

    headline = str(meta.get("headline") or "").strip()
    if headline:
        parts += [f"## {headline}", ""]

    trends = [str(t).strip() for t in meta.get("trends") or [] if str(t).strip()]
    if trends:
        parts += [*(f"- {trend}" for trend in trends), ""]

    highlights = [h for h in meta.get("highlights") or [] if isinstance(h, dict)]
    if highlights:
        parts += ["### Highlights", ""]
        for entry in highlights:
            title = str(entry.get("title") or "").strip() or "untitled"
            url = str(entry.get("url") or "").strip()
            takeaway = str(entry.get("takeaway") or "").strip()
            parts.append(f"- [{title}]({url}) — {takeaway}" if url else f"- {title} — {takeaway}")
        parts.append("")

    parts += [
        "---",
        "",
        f"**Period:** `{meta.get('period', '?')}` · "
        f"**Window:** {meta.get('days', '?')} day(s) · "
        f"**Articles read:** {meta.get('articles', '?')}",
        "",
        "_Edit the YAML block at the top to change what gets posted; the text "
        "above it is only a preview. Nothing is sent until the publish-digest "
        "stage runs._",
    ]

    body = "\n".join(parts)
    if len(body) <= BODY_LIMIT:
        return body
    # Only the preview is expendable. A blunt cut would sever the block's
    # closing `-->`, and a metadata block that no longer parses reads back as
    # no roundup at all — which is exactly the state that must never be
    # reachable by writing.
    head = f"<!-- squelch\n{front}\n-->"
    return head[:BODY_LIMIT] if len(head) > BODY_LIMIT else f"{head}\n\n_[preview omitted]_"


def digest_from_meta(meta: dict[str, Any]) -> Digest | None:
    """Read a stored roundup back out of an issue.

    Returns None rather than raising when the block has been edited into
    something unusable: one broken issue must not take down a delivery run that
    has other roundups waiting behind it.

    An empty roundup counts as unusable, and that check is doing more work than
    it looks. A YAML block whose indentation was broken by hand parses as
    nothing at all, and every field below would then validate happily as its
    empty default — so without this the channel gets a blank card and, worse,
    the delivery that follows rewrites the issue body from the empty metadata
    and takes the roundup with it.
    """
    headline = str(meta.get("headline") or "").strip()
    if not any((headline, meta.get("trends"), meta.get("highlights"))):
        log.error("stored digest has no headline, trends or highlights — refusing to post it")
        return None
    try:
        return Digest.model_validate(
            {
                "headline": meta.get("headline") or "",
                "trends": meta.get("trends") or [],
                "highlights": meta.get("highlights") or [],
            }
        )
    except Exception as exc:  # noqa: BLE001 - a hand-edited block can fail any number of ways
        log.error("stored digest cannot be read back: %s", exc)
        return None


def period_of(issue: IssueRecord) -> Period:
    """Which roundup this issue is, from its label rather than its metadata.

    The label is the source of truth here for the same reason it is everywhere
    else: it survives an edit to the body, and it is what the query found.
    """
    for period in Period:
        if digest_label(period) in issue.labels:
            return period
    # Only reachable if the label was removed by hand after the query matched.
    stored = str(issue.meta.get("period") or "")
    return Period(stored) if stored in tuple(Period) else Period.WEEKLY


class DigestStore:
    """CRUD over the issues that hold roundups."""

    def __init__(self, client: GitHubClient) -> None:
        self.client = client
        self.repo = client.repo

    # -- reads --------------------------------------------------------------

    def _list(self, label: str, state: str = "all") -> list[IssueRecord]:
        payloads = self.client.paginate(
            f"/repos/{self.repo}/issues",
            {"labels": label, "state": state, "sort": "created", "direction": "desc"},
        )
        return [parse_issue(p) for p in payloads if "pull_request" not in p]

    def built_on(self, period: Period, day: date) -> IssueRecord | None:
        """A roundup of this period already built for ``day``, if any.

        Guards the double build: a workflow dispatched by hand on the morning
        the cron already ran must not queue a second copy of the same day. Keyed
        on the day rather than on "is anything pending", so a delivery outage
        stalls the roundups it has, and never stops tomorrow's from being
        written.

        Open *and* closed, which is the whole guarantee. A roundup is posted
        within the hour and closed by the next closing tick, so an open-only
        search would stop recognising the morning's roundup about fifteen
        minutes after it went out — and a dispatch at noon would write a second
        one for the same day.
        """
        for issue in self._list(digest_label(period), state="all"):
            if str(issue.meta.get("built_on") or "") == day.isoformat():
                return issue
        return None

    def list_pending(self, channel: str, limit: int | None = None) -> list[IssueRecord]:
        """Roundups this channel has not delivered yet, oldest first.

        Oldest first so a backlog goes out in the order it was written: a
        Tuesday roundup arriving after Wednesday's would read as a correction.
        """
        pending = [
            issue
            for label in digest_labels()
            for issue in self._list(label, state="open")
            if channel not in issue.delivered_to
        ]
        pending.sort(key=lambda issue: issue.number)
        if limit is not None:
            pending = pending[:limit]
        log.info("%d roundup(s) pending for %s", len(pending), channel)
        return pending

    # -- writes -------------------------------------------------------------

    def create(
        self,
        digest: Digest,
        period: Period,
        days: int,
        articles: int,
        built_on: date | None = None,
    ) -> IssueRecord:
        meta: dict[str, Any] = {
            "period": period.value,
            "days": days,
            "articles": articles,
            "built_on": (built_on or datetime.now(UTC).date()).isoformat(),
            "built_at": datetime.now(UTC).isoformat(),
            "headline": digest.headline,
            "trends": list(digest.trends),
            "highlights": [entry.model_dump() for entry in digest.highlights],
        }
        # The headline names the issue, so the tracker reads as a list of
        # roundups. A model can return a blank one, and GitHub refuses an
        # untitled issue.
        title = trim(digest.headline.strip(), TITLE_LIMIT) or period.label.capitalize()
        payload = self.client.request(
            "POST",
            f"/repos/{self.repo}/issues",
            json={
                "title": title,
                "body": render_digest_body(meta),
                "labels": [digest_label(period)],
            },
        ).json()
        record = parse_issue(payload)
        log.info("stored %s as #%d %s", period.label, record.number, title[:70])
        return record

    def record_delivery(
        self,
        issue: IssueRecord,
        channel: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Note that ``channel`` has posted this roundup.

        Body before label, exactly as ``IssueStore.record_delivery`` does it and
        for the same reason: a run that dies between the two leaves the message
        id behind while the issue still looks undelivered, so the next run sees
        the id and relabels instead of posting the roundup twice.

        Nothing here closes the issue — ``close_delivered`` owns that.

        Refuses to write a body when the issue carries no metadata at all. That
        is not a roundup with nothing in it; it is a block that failed to parse,
        and rewriting from it would replace the stored roundup with the delivery
        note alone. The label still goes on, so the roundup leaves the queue
        rather than being posted again on the next tick.
        """
        if not issue.meta:
            log.error(
                "#%d has no readable metadata; recording the delivery label only", issue.number
            )
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
        self._patch(issue.number, body=render_digest_body(meta))
        issue.meta = meta

        labels = sorted({*issue.labels, f"{SENT_PREFIX}{channel}"})
        self._patch(issue.number, labels=labels)
        issue.labels = labels
        log.info("#%d delivered to %s", issue.number, channel)

    def close_delivered(self, channels: list[Channel]) -> list[int]:
        """Close every roundup that all its digest channels have posted.

        Same division of labour as the article pipeline: the publisher never
        closes, so a channel dying between its own label and the close cannot
        strand a roundup nobody will look at again.
        """
        if not channels:
            log.warning("no digest channel is enabled, leaving roundups open")
            return []

        required = {channel.id for channel in channels}
        closed: list[int] = []
        for label in digest_labels():
            for issue in self._list(label, state="open"):
                missing = required - issue.delivered_to
                if missing:
                    log.debug("#%d still owes %s", issue.number, ", ".join(sorted(missing)))
                    continue
                self._patch(issue.number, state="closed", state_reason="completed")
                closed.append(issue.number)
                log.info("#%d -> posted and closed", issue.number)
        return closed

    def _patch(self, number: int, **fields: Any) -> None:
        self.client.request("PATCH", f"/repos/{self.repo}/issues/{number}", json=fields)
