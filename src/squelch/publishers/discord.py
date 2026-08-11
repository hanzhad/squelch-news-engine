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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..core.config import Config, Emphasis
from ..core.log import get_logger
from ..core.models import Digest, DigestEntry, Status
from ..core.settings import Settings
from ..core.throttle import paced
from ..github.issues import IssueRecord, IssueStore

log = get_logger(__name__)

# Discord's documented limits. Every one of them is a 400 when exceeded, which
# is why nothing reaches the API without passing through _trim first.
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FOOTER_LIMIT = 2048
AUTHOR_LIMIT = 256
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
MAX_FIELDS = 25
MAX_EMBEDS = 10
MESSAGE_LIMIT = 6000
# The name of a thread a webhook creates in a forum channel. Much shorter than
# a title, and titles routinely run past it.
THREAD_NAME_LIMIT = 100
# Tags Discord will accept on one forum post. An article can easily carry more
# topic labels than this, so the overflow is dropped rather than sent.
MAX_APPLIED_TAGS = 5

# Well under DESCRIPTION_LIMIT on purpose: a summary that fills a chat window
# stops being a summary, and the article is one click away.
SUMMARY_TARGET = 900

MAX_ATTEMPTS = 5
# Retries wait at most this long; anything worse is better left to the next run.
MAX_BACKOFF = 120.0
ACCENT_COLOR = 0x8A4B2A
HIGHLIGHTS_TITLE = "Highlights"


class Weight(StrEnum):
    """How much of the channel one article is given."""

    LEAD = "lead"
    STANDARD = "standard"
    BRIEF = "brief"


# Height is what a reader actually notices while a channel scrolls past — a lead
# is several times the size of a brief, and the colour only confirms what the
# size already said. Kept inside one family so it still reads as one feed;
# red and green would read as alarm and success, which is not what is meant.
WEIGHT_COLORS = {
    Weight.LEAD: 0xD85A30,
    Weight.STANDARD: ACCENT_COLOR,
    Weight.BRIEF: 0x4A4A47,
}

# Must match the channel ids in config/delivery.yaml — that is what ties this
# module to the sent:* labels and to the count that closes an issue. The
# config decides which channels exist and what each one wants; which webhook
# each posts through is a credential, so it stays in the environment, one env
# var per channel.
CHANNEL = "discord"
# The rubric channel articles are routed to by label (see `only`/`skip` in
# delivery.yaml) — Claude-skills repositories, kept out of the main feed.
SKILLS_CHANNEL = "discord-skills"
# The window onto what the classifier threw away. A channel of its own in the
# same server, so the feed stays clean; consumes status:rejected and never
# takes part in closing an issue.
REJECTED_CHANNEL = "discord-rejected"
# The brief's grey: rejected posts are the quietest thing squelch says.
REJECTED_COLOR = 0x4A4A47
# Last resort for the name of the digest's forum post; see post_digest.
DIGEST_THREAD_NAME = "Weekly digest"


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


def _weight(issue: IssueRecord, emphasis: Emphasis) -> Weight:
    """Which tier this article belongs in.

    An article with no score at all — opened by hand, never classified — lands
    in the middle. Neither promoting nor hiding something we have not judged is
    the only defensible thing to do with it.
    """
    score = issue.meta.get("score")
    if not isinstance(score, int | float):
        return Weight.STANDARD
    if score >= emphasis.lead:
        return Weight.LEAD
    if score >= emphasis.standard:
        return Weight.STANDARD
    return Weight.BRIEF


def _issue_embed(issue: IssueRecord, emphasis: Emphasis | None = None) -> dict[str, Any]:
    """Render one article as a single embed, sized by what it is worth.

    Read top to bottom the way a person scans a feed: who published it, what
    they said, what it is about, and only then the bookkeeping. The source is
    the author line rather than a word buried in the footer, because "is this
    from a lab or from a blog" is the first thing anyone wants to know.

    A brief keeps its headline, its link and its topics and gives up the summary
    and the picture. It is still a whole article in the channel, reachable in
    the same two clicks — it just stops competing with the ones that matter.
    """
    weight = _weight(issue, emphasis or Emphasis())
    # Issues opened by hand never went through the LLM, so they have no summary;
    # the head of the article itself is a better placeholder than nothing.
    body = issue.summary or issue.text
    tags = [str(tag) for tag in issue.meta.get("tags") or [] if tag]
    score = issue.meta.get("score")

    embed: dict[str, Any] = {
        "title": _trim(issue.title, TITLE_LIMIT),
        "color": WEIGHT_COLORS[weight],
    }
    if issue.url:
        embed["url"] = issue.url
    if issue.source:
        embed["author"] = {"name": _trim(issue.source, AUTHOR_LIMIT)}

    # The picture the publisher chose for link previews. Discord fetches it
    # itself, so a dead link costs a blank space, not a failed message.
    if issue.image and weight is Weight.LEAD:
        embed["image"] = {"url": issue.image}
    elif issue.image and weight is Weight.STANDARD:
        embed["thumbnail"] = {"url": issue.image}

    footer = ["squelch"]
    if isinstance(score, int | float):
        footer.append(f"score {int(score)}/10")
    embed["footer"] = {"text": _trim(" · ".join(footer), FOOTER_LIMIT)}

    # One line rather than two side-by-side fields: topics and the link to the
    # discussion are both navigation, and splitting them into columns made the
    # embed taller without making it clearer.
    meta_line = " · ".join(
        part
        for part in (
            " ".join(f"`{tag}`" for tag in tags),
            f"[discuss #{issue.number}]({issue.html_url})" if issue.html_url else "",
        )
        if part
    )
    if weight is Weight.BRIEF:
        # The one line becomes the description rather than a field: a field
        # carries a blank name row above it, which is most of what a brief was
        # meant to save.
        if meta_line:
            embed["description"] = _trim(meta_line, DESCRIPTION_LIMIT)
    else:
        embed["description"] = _trim(body, min(SUMMARY_TARGET, DESCRIPTION_LIMIT))
        if meta_line:
            embed["fields"] = [{"name": "​", "value": _trim(meta_line, FIELD_VALUE_LIMIT)}]

    stamp = _timestamp(issue.meta.get("published_at")) or _timestamp(issue.created_at)
    if stamp:
        embed["timestamp"] = stamp
    return embed


def _rejected_embed(issue: IssueRecord) -> dict[str, Any]:
    """One rejected article: the verdict, and the way to appeal it.

    Deliberately the size of a brief — this channel exists so the classifier's
    mistakes are visible, not to compete with the feed. The reason is the
    content here, and the issue link is the call to action: enough 👍 on the
    issue and the rescue pass sends the article back for its write-up.
    """
    embed: dict[str, Any] = {
        "title": _trim(issue.title, TITLE_LIMIT),
        "color": REJECTED_COLOR,
    }
    if issue.url:
        embed["url"] = issue.url
    if issue.source:
        embed["author"] = {"name": _trim(issue.source, AUTHOR_LIMIT)}

    lines = []
    reason = str(issue.meta.get("verdict_reason") or "").strip()
    if reason:
        lines.append(f"**Rejected:** {reason}")
    if issue.html_url:
        lines.append(
            f"Disagree? React 👍 on [#{issue.number}]({issue.html_url}) "
            "and it goes back into the pipeline."
        )
    if lines:
        embed["description"] = _trim("\n".join(lines), DESCRIPTION_LIMIT)

    embed["footer"] = {"text": "squelch · rejected"}
    stamp = _timestamp(issue.meta.get("published_at")) or _timestamp(issue.created_at)
    if stamp:
        embed["timestamp"] = stamp
    return embed


def _payload(
    embeds: list[dict[str, Any]],
    thread_name: str = "",
    applied_tags: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "embeds": _fit_embeds(embeds),
        # Headlines routinely contain @handles and words like "everyone";
        # without this Discord would turn them into real pings.
        "allowed_mentions": {"parse": []},
    }
    if thread_name:
        # Forum channels only, where it is not optional: the post is a thread,
        # and a thread cannot exist unnamed. Sent to a text channel the same
        # field is a 400, which is why nothing here guesses.
        payload["thread_name"] = _trim(thread_name, THREAD_NAME_LIMIT)
    if applied_tags:
        payload["applied_tags"] = applied_tags[:MAX_APPLIED_TAGS]
    return payload


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


def _deliver(
    webhook: _Webhook,
    store: IssueStore,
    issues: list[IssueRecord],
    channel: str,
    make_embed: Callable[[IssueRecord], dict[str, Any]],
    delay: float,
    forum: bool = False,
    tags: Callable[[IssueRecord], list[str]] | None = None,
) -> tuple[int, int]:
    """Post each issue and mark it delivered; returns (posted, relabelled).

    One bad article never stops the batch: whatever fails keeps its place in
    the queue and the next run tries it again.
    """
    posted = 0
    relabelled = 0
    for issue in paced(issues, delay):
        known_id = str(issue.delivery(channel).get("message_id") or "")
        if known_id:
            # A previous run posted this and died before its label landed.
            # Reposting would double it up, so only the label moves.
            try:
                store.record_delivery(issue, channel, {"message_id": known_id})
                relabelled += 1
            except Exception as exc:  # noqa: BLE001 - retried on the next run
                log.error("#%d is already on Discord but will not relabel: %s", issue.number, exc)
            continue

        try:
            # In a forum the article's own headline names the post, so the
            # channel reads as a list of stories rather than a wall of cards,
            # and its topic labels become the tags readers filter by.
            message_id = webhook.send(
                _payload(
                    [make_embed(issue)],
                    issue.title if forum else "",
                    tags(issue) if forum and tags else None,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one article must not stop the batch
            log.error("could not publish #%d: %s", issue.number, exc)
            continue
        posted += 1

        try:
            store.record_delivery(issue, channel, {"message_id": message_id})
        except Exception as exc:  # noqa: BLE001 - the message is out either way
            # Logged loudly: the id exists nowhere else yet, and without it
            # on the issue the next run has no way to know not to repost.
            log.error(
                "#%d posted as message %s but was not recorded: %s",
                issue.number,
                message_id or "?",
                exc,
            )
    return posted, relabelled


def _ready_webhooks(settings: Settings) -> dict[str, tuple[str, str]]:
    """Channel id -> (webhook url, env var that should hold it)."""
    return {
        CHANNEL: (settings.discord_webhook_url, "DISCORD_WEBHOOK_URL"),
        SKILLS_CHANNEL: (settings.discord_skills_webhook_url, "DISCORD_SKILLS_WEBHOOK_URL"),
    }


def publish_ready(settings: Settings, config: Config, store: IssueStore) -> int:
    """Post ready articles each Discord channel wants and mark them delivered.

    Returns the number of messages actually sent. Whether an article is now
    finished with — that is, out on every channel it was routed to — is not
    this stage's question. An enabled channel with no webhook fails the run
    rather than silently borrowing another channel's: routing exists precisely
    so posts do not end up in the wrong place.
    """
    webhooks = _ready_webhooks(settings)
    total = 0
    for channel in config.ready_channels:
        if channel.id not in webhooks:
            # The site and the feed deliver through pages.yml, not through us.
            continue
        url, env_name = webhooks[channel.id]
        if not url:
            raise DiscordError(f"{env_name} is not set")

        pending = [
            issue
            for issue in store.list_pending(channel.id)
            if channel.wants(set(issue.labels))
        ][: settings.publish_batch_size]
        if not pending:
            log.info("nothing to publish for %s", channel.id)
            continue

        emphasis = channel.emphasis
        with _Webhook(settings, url) as webhook:
            posted, relabelled = _deliver(
                webhook,
                store,
                pending,
                channel.id,
                lambda issue, e=emphasis: _issue_embed(issue, e),
                settings.publish_delay_seconds,
                channel.forum,
                lambda issue, c=channel: c.tag_ids(set(issue.labels), MAX_APPLIED_TAGS),
            )
        log.info(
            "%s: published %d issue(s), relabelled %d already posted",
            channel.id,
            posted,
            relabelled,
        )
        total += posted
    return total


def publish_rejected(settings: Settings, config: Config, store: IssueStore) -> int:
    """Post recently rejected articles to their own channel and mark them.

    The rejected channel consumes ``status:rejected`` instead of ready, so it
    never takes part in closing an issue — rejections are closed already. The
    credential is checked before anything else on purpose: the shared webhook
    fallback in ``_Webhook`` would otherwise quietly post rejects into the
    main feed, which is the one thing this channel must never do.
    """
    rejected = config.channel(REJECTED_CHANNEL)
    if not rejected.enabled:
        log.info("%s is disabled in delivery.yaml, nothing to do", REJECTED_CHANNEL)
        return 0
    if not settings.discord_rejected_webhook_url:
        raise DiscordError("DISCORD_REJECTED_WEBHOOK_URL is not set")

    # A window, not the whole history: switching the channel on should show
    # what was thrown away lately, not replay every rejection ever made.
    since = datetime.now(UTC) - timedelta(days=settings.rejected_window_days)
    issues = store.list_pending(
        REJECTED_CHANNEL,
        limit=settings.publish_batch_size,
        status=Status.REJECTED,
        since=since,
    )
    if not issues:
        log.info("nothing rejected to post")
        return 0

    with _Webhook(settings, settings.discord_rejected_webhook_url) as webhook:
        posted, relabelled = _deliver(
            webhook,
            store,
            issues,
            REJECTED_CHANNEL,
            _rejected_embed,
            settings.publish_delay_seconds,
            rejected.forum,
            lambda issue: rejected.tag_ids(set(issue.labels), MAX_APPLIED_TAGS),
        )

    log.info("posted %d rejected issue(s), relabelled %d already posted", posted, relabelled)
    return posted


def post_digest(settings: Settings, digest: Digest) -> None:
    """Post the weekly roundup as one message, to its own channel if it has one.

    A week of news read back in one sitting is a different thing from the
    article that just landed, and it gets buried if it arrives in the same
    stream. Falls back to the feed's webhook when no digest channel is set.

    In a forum that channel gets one post per week, titled with the week's
    headline, so a roundup can be discussed without the next one burying the
    argument. The digest has no entry in delivery.yaml — it is not a queue any
    article passes through — so the flag lives beside its webhook instead.
    """
    thread_name = ""
    if settings.digest_forum:
        # A forum post cannot be nameless, and a model can return a blank
        # headline; a dull title beats a 400 that loses the whole week.
        thread_name = digest.headline.strip() or DIGEST_THREAD_NAME

    with _Webhook(settings, settings.digest_webhook_url) as webhook:
        message_id = webhook.send(_payload(_digest_embeds(digest), thread_name))
    log.info("posted digest as message %s", message_id or "?")
