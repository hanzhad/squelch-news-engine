"""Delivery to Discord — the step that makes the pipeline visible.

Two things make this more than a POST. Discord's embed limits are hard errors,
so every string is trimmed on the way out rather than allowed to fail a whole
batch. And the gap between "the message is in the channel" and "the issue is
labelled" is a real window: a crash inside it would repost the article on the
next run, so the webhook is asked for the message id (``wait=true``), the id is
stored on the issue, and its presence is treated as proof that the article is
already out.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..core.log import get_logger
from ..core.models import Digest, DigestEntry
from ..core.settings import Settings
from ..core.throttle import paced
from ..github.issues import IssueRecord, IssueStore

log = get_logger(__name__)

# Discord's documented limits. Every one of them is a 400 when exceeded, which
# is why nothing reaches the API without passing through _trim first.
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FOOTER_LIMIT = 2048
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
MAX_FIELDS = 25
MAX_EMBEDS = 10
MESSAGE_LIMIT = 6000

# Well under DESCRIPTION_LIMIT on purpose: a summary that fills a chat window
# stops being a summary, and the article is one click away.
SUMMARY_TARGET = 900

MAX_ATTEMPTS = 5
# Retries wait at most this long; anything worse is better left to the next run.
MAX_BACKOFF = 120.0
ACCENT_COLOR = 0x8A4B2A
HIGHLIGHTS_TITLE = "Highlights"

# Must match the channel id in config/delivery.yaml — that is what ties this
# module to the sent:discord label and to the count that closes an issue.
CHANNEL = "discord"


class DiscordError(RuntimeError):
    pass


# -- text fitting -----------------------------------------------------------


def _trim(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, preferring a word boundary."""
    clean = (text or "").strip()
    if len(clean) <= limit:
        return clean
    cut = clean[: max(0, limit - 1)]
    space = cut.rfind(" ")
    # Only honour the word boundary if it does not throw away most of the text.
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _embed_size(embed: dict[str, Any]) -> int:
    """Characters an embed contributes to the 6000-per-message budget."""
    total = len(embed.get("title", "")) + len(embed.get("description", ""))
    total += len(embed.get("footer", {}).get("text", ""))
    total += len(embed.get("author", {}).get("name", ""))
    for field in embed.get("fields", []):
        total += len(field.get("name", "")) + len(field.get("value", ""))
    return total


def _fit_embeds(embeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop trailing embeds until the message fits.

    Losing the last highlight is a worse digest; a 400 is no digest at all.
    """
    kept = embeds[:MAX_EMBEDS]
    while kept and sum(_embed_size(e) for e in kept) > MESSAGE_LIMIT:
        kept.pop()
    if len(kept) < len(embeds):
        log.warning("dropped %d embed(s) to fit Discord limits", len(embeds) - len(kept))
    return kept


# -- transport --------------------------------------------------------------


def _with_wait(url: str) -> str:
    """Force ``wait=true`` so the response body carries the created message.

    Also the only place the webhook URL is checked. A secret pasted without its
    scheme reaches httpx as a bare host and dies in a traceback five frames
    deep, long after the digest has been written — so the shape is rejected
    here, before any work is done, with a message that says what to fix.
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise DiscordError(
            "webhook URL must start with https:// and name a host "
            f"(got {url.strip()[:40]!r}) — re-copy it from the channel's integration settings"
        )
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["wait"] = "true"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class _Webhook:
    """One webhook connection, with Discord's bucket bookkeeping attached."""

    def __init__(self, settings: Settings, url: str = "") -> None:
        target = url or settings.discord_webhook_url
        if not target:
            raise DiscordError("DISCORD_WEBHOOK_URL is not set")
        self._url = _with_wait(target)
        self._client = httpx.Client(
            timeout=settings.request_timeout,
            headers={"User-Agent": "squelch"},
        )
        # Learnt from the previous response: seconds to hold before the next
        # request, so an exhausted bucket costs a pause instead of a 429.
        self._cooldown = 0.0

    def __enter__(self) -> _Webhook:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def send(self, payload: dict[str, Any]) -> str:
        """POST one message and return its id (empty if Discord did not say)."""
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_out_bucket()
            response = self._client.post(self._url, json=payload)
            self._note_bucket(response)

            if response.is_success:
                return self._message_id(response)

            delay = self._retry_delay(response, attempt)
            if delay is None:
                raise DiscordError(
                    f"webhook rejected the message: {response.status_code} {response.text[:300]}"
                )
            last_error = f"{response.status_code} {response.text[:200]}"
            log.warning(
                "discord -> %s, retrying in %.2fs (attempt %d/%d)",
                response.status_code,
                delay,
                attempt,
                MAX_ATTEMPTS,
            )
            time.sleep(delay)

        raise DiscordError(f"webhook gave up after {MAX_ATTEMPTS} attempts: {last_error}")

    # -- rate limiting ------------------------------------------------------

    def _wait_out_bucket(self) -> None:
        if self._cooldown > 0:
            log.debug("bucket empty, holding %.2fs", self._cooldown)
            time.sleep(self._cooldown)
            self._cooldown = 0.0

    def _note_bucket(self, response: httpx.Response) -> None:
        """Remember an emptied bucket so the *next* request waits it out."""
        self._cooldown = 0.0
        if response.headers.get("x-ratelimit-remaining") != "0":
            return
        reset_after = response.headers.get("x-ratelimit-reset-after")
        if not reset_after:
            return
        try:
            self._cooldown = min(max(float(reset_after), 0.0), MAX_BACKOFF)
        except ValueError:
            self._cooldown = 0.0

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait before retrying, or None if the error is terminal."""
        if response.status_code == 429:
            body = self._json(response)
            if body.get("global"):
                log.warning("hit Discord's global rate limit")
            retry_after = body.get("retry_after")
            if not isinstance(retry_after, int | float):
                # The header is the same number rounded up to whole seconds; the
                # JSON body is authoritative when both are present.
                header = response.headers.get("retry-after", "")
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = 1.0
            # A small cushion: the clock Discord measured against is not ours.
            return min(max(float(retry_after), 0.0) + 0.25, MAX_BACKOFF)

        if response.status_code >= 500:
            return float(min(2**attempt, 30))

        return None

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _message_id(self, response: httpx.Response) -> str:
        payload = self._json(response)
        message_id = str(payload.get("id", ""))
        if not message_id:
            log.warning("discord accepted the message but returned no id")
        return message_id


# -- payloads ---------------------------------------------------------------


def _timestamp(value: Any) -> str | None:
    """Discord wants ISO 8601; anything it would reject is dropped instead."""
    if isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _issue_embed(issue: IssueRecord) -> dict[str, Any]:
    """Render one article as a single embed."""
    # Issues opened by hand never went through the LLM, so they have no summary;
    # the head of the article itself is a better placeholder than nothing.
    body = issue.summary or issue.text
    tags = [str(tag) for tag in issue.meta.get("tags") or [] if tag]
    score = issue.meta.get("score")

    embed: dict[str, Any] = {
        "title": _trim(issue.title, TITLE_LIMIT),
        "description": _trim(body, min(SUMMARY_TARGET, DESCRIPTION_LIMIT)),
        "color": ACCENT_COLOR,
    }
    if issue.url:
        embed["url"] = issue.url

    footer = ["squelch"]
    if issue.source:
        footer.append(issue.source)
    if isinstance(score, int | float):
        footer.append(f"score {int(score)}/10")
    embed["footer"] = {"text": _trim(" · ".join(footer), FOOTER_LIMIT)}

    fields: list[dict[str, Any]] = []
    if tags:
        fields.append(
            {"name": "Tags", "value": _trim(", ".join(tags), FIELD_VALUE_LIMIT), "inline": True}
        )
    if issue.html_url:
        fields.append(
            {
                "name": "Discussion",
                "value": _trim(f"[#{issue.number}]({issue.html_url})", FIELD_VALUE_LIMIT),
                "inline": True,
            }
        )
    if fields:
        embed["fields"] = fields[:MAX_FIELDS]

    stamp = _timestamp(issue.meta.get("published_at")) or _timestamp(issue.created_at)
    if stamp:
        embed["timestamp"] = stamp
    return embed


def _payload(embeds: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "embeds": _fit_embeds(embeds),
        # Headlines routinely contain @handles and words like "everyone";
        # without this Discord would turn them into real pings.
        "allowed_mentions": {"parse": []},
    }


def _highlight_field(entry: DigestEntry) -> dict[str, Any]:
    link = f"\n[Read]({entry.url})" if entry.url else ""
    return {
        "name": _trim(entry.title, FIELD_NAME_LIMIT),
        # Trim the prose, not the link, so the link never comes out half-written.
        "value": _trim(entry.takeaway, FIELD_VALUE_LIMIT - len(link)) + link,
        "inline": False,
    }


def _digest_embeds(digest: Digest) -> list[dict[str, Any]]:
    trends = "\n".join(f"• {trend.strip()}" for trend in digest.trends if trend.strip())
    lead: dict[str, Any] = {
        "title": _trim(digest.headline, TITLE_LIMIT),
        "description": _trim(trends, DESCRIPTION_LIMIT),
        "color": ACCENT_COLOR,
        "footer": {"text": "squelch · weekly digest"},
    }
    if not digest.highlights:
        return [lead]

    # Highlights are spent against what the lead embed left over, so the digest
    # loses its tail rather than the whole second embed.
    budget = MESSAGE_LIMIT - _embed_size(lead) - len(HIGHLIGHTS_TITLE)
    fields: list[dict[str, Any]] = []
    for entry in digest.highlights[:MAX_FIELDS]:
        field = _highlight_field(entry)
        cost = len(field["name"]) + len(field["value"])
        if cost > budget:
            log.warning(
                "digest trimmed to %d of %d highlights", len(fields), len(digest.highlights)
            )
            break
        budget -= cost
        fields.append(field)

    if not fields:
        return [lead]
    return [lead, {"title": HIGHLIGHTS_TITLE, "color": ACCENT_COLOR, "fields": fields}]


# -- entry points -----------------------------------------------------------


def publish_ready(settings: Settings, store: IssueStore) -> int:
    """Post ready articles Discord has not seen yet and mark them delivered.

    Returns the number of messages actually sent. One bad article never stops
    the batch: whatever fails keeps its place in the queue and the next run
    tries it again. Whether the article is now finished with — that is, out on
    every channel — is not this stage's question.
    """
    issues = store.list_pending(CHANNEL, limit=settings.publish_batch_size)
    if not issues:
        log.info("nothing to publish")
        return 0

    posted = 0
    relabelled = 0
    with _Webhook(settings) as webhook:
        for issue in paced(issues, settings.publish_delay_seconds):
            known_id = str(issue.delivery(CHANNEL).get("message_id") or "")
            if known_id:
                # A previous run posted this and died before its label landed.
                # Reposting would double it up, so only the label moves.
                try:
                    store.record_delivery(issue, CHANNEL, {"message_id": known_id})
                    relabelled += 1
                except Exception as exc:  # noqa: BLE001 - retried on the next run
                    log.error(
                        "#%d is already on Discord but will not relabel: %s", issue.number, exc
                    )
                continue

            try:
                message_id = webhook.send(_payload([_issue_embed(issue)]))
            except Exception as exc:  # noqa: BLE001 - one article must not stop the batch
                log.error("could not publish #%d: %s", issue.number, exc)
                continue
            posted += 1

            try:
                store.record_delivery(issue, CHANNEL, {"message_id": message_id})
            except Exception as exc:  # noqa: BLE001 - the message is out either way
                # Logged loudly: the id exists nowhere else yet, and without it
                # on the issue the next run has no way to know not to repost.
                log.error(
                    "#%d posted as message %s but was not recorded: %s",
                    issue.number,
                    message_id or "?",
                    exc,
                )

    log.info("published %d issue(s), relabelled %d already posted", posted, relabelled)
    return posted


def post_digest(settings: Settings, digest: Digest) -> None:
    """Post the weekly roundup as one message, to its own channel if it has one.

    A week of news read back in one sitting is a different thing from the
    article that just landed, and it gets buried if it arrives in the same
    stream. Falls back to the feed's webhook when no digest channel is set.
    """
    with _Webhook(settings, settings.digest_webhook_url) as webhook:
        message_id = webhook.send(_payload(_digest_embeds(digest)))
    log.info("posted digest as message %s", message_id or "?")
