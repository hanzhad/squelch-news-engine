"""The bot connection to Discord — the only place the pipeline *reads* a channel.

Everything else here posts through a webhook, which is write-only by design: a
webhook URL cannot list a channel, and no amount of configuration makes it. So
the community forum needs the other kind of credential, a bot token, and this
module is the whole of what uses it.

It reads and it replies, in one place, because they are one connection with one
credential and one set of rate-limit buckets. The alternative — a webhook for
the reply, as every other channel has — would mean a second secret for the same
channel and an answer that comes from a nameless webhook identity rather than
from the bot people can see in the member list, mute, and argue with.

What the token is allowed to do is deliberately small: view channels, read
message history, send messages in threads, embed links. It cannot open a post,
delete one, or touch a message that is not its own, which is the point — the
forum belongs to the people posting in it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..core.config import Channel
from ..core.log import get_logger
from ..core.models import CasePost
from ..core.settings import Settings

log = get_logger(__name__)

API_ROOT = "https://discord.com/api/v10"
MAX_ATTEMPTS = 5
MAX_BACKOFF = 120.0
# One page of archived posts. Active threads are the normal case; this is the
# safety net for a forum that went quiet — or an ingest that was down — long
# enough for Discord to archive a post nobody answered yet.
ARCHIVED_PAGE = 50


class ForumError(RuntimeError):
    pass


class BotClient:
    """One authenticated connection to Discord, with its bucket bookkeeping.

    The retry rules are the webhook's, for the same reasons: 429 is answered by
    waiting exactly as long as Discord asked, a 5xx is worth another try, and
    anything else is a request that will stay wrong however many times it is
    sent.
    """

    def __init__(self, settings: Settings) -> None:
        token = settings.discord_bot_token.strip()
        if not token:
            raise ForumError("DISCORD_BOT_TOKEN is not set")
        self._client = httpx.Client(
            base_url=API_ROOT,
            timeout=settings.request_timeout,
            headers={
                "Authorization": f"Bot {token}",
                # Discord asks bots to identify themselves, and answers a
                # missing User-Agent with a 403 that says nothing useful.
                "User-Agent": "squelch (https://github.com/hanzhad/squelch-news-engine)",
            },
        )
        self._cooldown = 0.0
        # Forum tag ids are meaningless to a reader and to a model; the names
        # are fetched once per run and remembered.
        self._tag_names: dict[str, dict[str, str]] = {}

    def __enter__(self) -> BotClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- transport ----------------------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """One call to the API, retried where retrying can help."""
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_out_bucket()
            response = self._client.request(method, path, **kwargs)
            self._note_bucket(response)

            if response.is_success:
                return self._json(response)

            delay = self._retry_delay(response, attempt)
            if delay is None:
                raise ForumError(
                    f"discord refused {method} {path}: "
                    f"{response.status_code} {response.text[:300]}"
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

        raise ForumError(f"discord gave up after {MAX_ATTEMPTS} attempts: {last_error}")

    def _wait_out_bucket(self) -> None:
        if self._cooldown > 0:
            log.debug("bucket empty, holding %.2fs", self._cooldown)
            time.sleep(self._cooldown)
            self._cooldown = 0.0

    def _note_bucket(self, response: httpx.Response) -> None:
        self._cooldown = 0.0
        if response.headers.get("x-ratelimit-remaining") != "0":
            return
        try:
            self._cooldown = min(
                max(float(response.headers.get("x-ratelimit-reset-after") or 0), 0.0),
                MAX_BACKOFF,
            )
        except ValueError:
            self._cooldown = 0.0

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float | None:
        if response.status_code == 429:
            body = response.json() if response.content else {}
            retry_after = body.get("retry_after") if isinstance(body, dict) else None
            if not isinstance(retry_after, int | float):
                try:
                    retry_after = float(response.headers.get("retry-after", "1"))
                except ValueError:
                    retry_after = 1.0
            return min(max(float(retry_after), 0.0) + 0.25, MAX_BACKOFF)
        if response.status_code >= 500:
            return float(min(2**attempt, 30))
        return None

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {}

    # -- reads --------------------------------------------------------------

    def threads(self, guild_id: str, forum_id: str) -> list[dict[str, Any]]:
        """Every post in this forum worth looking at, active and recently archived.

        Both, because a forum post archives on its own after a few quiet days.
        The active list is the normal case; without the archived page a run that
        was down over a weekend would step straight past the posts it missed and
        never answer them.
        """
        active = self.request("GET", f"/guilds/{guild_id}/threads/active")
        threads = [
            thread
            for thread in (active.get("threads") or [])
            if str(thread.get("parent_id") or "") == forum_id
        ]
        found = {str(thread.get("id")) for thread in threads}

        archived = self.request(
            "GET",
            f"/channels/{forum_id}/threads/archived/public",
            params={"limit": ARCHIVED_PAGE},
        )
        threads += [
            thread
            for thread in (archived.get("threads") or [])
            if str(thread.get("id")) not in found
        ]
        log.info("forum %s: %d post(s) visible", forum_id, len(threads))
        return threads

    def starter_message(self, thread_id: str) -> dict[str, Any]:
        """The post itself.

        A forum post's opening message carries the same id as the thread, which
        is what makes this one request rather than a listing. Everything after
        it is the discussion the reply is meant to start, and is deliberately
        not read.
        """
        message = self.request("GET", f"/channels/{thread_id}/messages/{thread_id}")
        return message if isinstance(message, dict) else {}

    def tag_names(self, forum_id: str) -> dict[str, str]:
        """``tag id -> tag name`` for one forum, asked once.

        Never fatal: tags are context for the reading, and a forum that will not
        describe itself costs the tag names and nothing else.
        """
        if forum_id not in self._tag_names:
            names: dict[str, str] = {}
            try:
                channel = self.request("GET", f"/channels/{forum_id}")
                for tag in channel.get("available_tags") or []:
                    names[str(tag.get("id"))] = str(tag.get("name") or "")
            except ForumError as exc:
                log.warning("could not read the forum's tags: %s", exc)
            self._tag_names[forum_id] = names
        return self._tag_names[forum_id]

    # -- writes -------------------------------------------------------------

    def reply(self, thread_id: str, payload: dict[str, Any]) -> str:
        """Post one message inside a thread and return its id.

        The id is what proves the reply is out. It is written onto the issue
        before the label that says so, so a run dying in between leaves evidence
        the next one can read instead of answering the same post twice.
        """
        message = self.request("POST", f"/channels/{thread_id}/messages", json=payload)
        message_id = str(message.get("id") or "") if isinstance(message, dict) else ""
        if not message_id:
            log.warning("discord accepted the reply but returned no id")
        return message_id


def _posted_at(thread: dict[str, Any], message: dict[str, Any]) -> datetime | None:
    """When the post was made, preferring the thread's own record of it."""
    metadata = thread.get("thread_metadata")
    stamps = [
        (metadata or {}).get("create_timestamp") if isinstance(metadata, dict) else None,
        message.get("timestamp"),
    ]
    for stamp in stamps:
        if not isinstance(stamp, str) or not stamp.strip():
            continue
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _author(message: dict[str, Any]) -> str:
    """Who posted it, as they are named in the server.

    Display name first, because that is who a reader sees in the channel. Only
    the name — no id, no discriminator, no avatar: this ends up in a public
    issue, and the least of it that has to travel there, the better.
    """
    author = message.get("author")
    if not isinstance(author, dict):
        return ""
    return str(author.get("global_name") or author.get("username") or "")


def _body(message: dict[str, Any], limit: int) -> str:
    """The text of the post, with a note about anything the bot cannot read.

    Attachments matter even unread. A post whose whole evidence is a screenshot
    would otherwise reach the model as an empty claim, and be answered as one —
    so their names travel instead, and the prompt is told they were not opened.
    """
    text = str(message.get("content") or "").strip()
    names = [
        str(item.get("filename") or "file")
        for item in message.get("attachments") or []
        if isinstance(item, dict)
    ]
    if names:
        listed = ", ".join(names[:10])
        text = f"{text}\n\n[attachments, not read: {listed}]".strip()
    return text[:limit]


def fetch_cases(
    settings: Settings,
    channel: Channel,
    max_age_days: int,
    body_chars: int,
    client: BotClient | None = None,
) -> list[CasePost]:
    """Read the forum and return the posts, newest last.

    Bounded by age rather than by count on purpose: enabling this on a forum
    with a year of history must not open a year of issues on the first run, and
    "everything since yesterday" is the only window that stays honest as the
    forum grows. One unreadable post is skipped rather than fatal — a post the
    bot cannot parse must not stop the ones behind it from being answered.
    """
    guild_id, forum_id = channel.forum_ids
    if not (guild_id and forum_id):
        raise ForumError(f"channel {channel.id} has no usable forum_url")

    owned = client is None
    bot = client or BotClient(settings)
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    posts: list[CasePost] = []
    try:
        tag_names = bot.tag_names(forum_id)
        for thread in bot.threads(guild_id, forum_id):
            thread_id = str(thread.get("id") or "")
            if not thread_id.isdigit():
                continue
            try:
                message = bot.starter_message(thread_id)
            except Exception as exc:  # noqa: BLE001 - one bad post must not stop the rest
                # Deliberately wider than ForumError: a dropped connection on
                # one post would otherwise take down a run that had every other
                # post in the forum still to read.
                log.warning("could not read post %s: %s", thread_id, exc)
                continue

            posted_at = _posted_at(thread, message)
            if posted_at is not None and posted_at < cutoff:
                continue
            try:
                posts.append(
                    CasePost(
                        thread_id=thread_id,
                        title=str(thread.get("name") or ""),
                        body=_body(message, body_chars),
                        author=_author(message),
                        url=f"https://discord.com/channels/{guild_id}/{thread_id}",
                        posted_at=posted_at,
                        tags=[
                            name
                            for tag in thread.get("applied_tags") or []
                            if (name := tag_names.get(str(tag), ""))
                        ],
                    )
                )
            except ValueError as exc:
                log.warning("post %s is not usable: %s", thread_id, exc)
    finally:
        if owned:
            bot.close()

    posts.sort(key=lambda post: (post.posted_at or datetime.min.replace(tzinfo=UTC)))
    log.info("%d case(s) inside the last %d days", len(posts), max_age_days)
    return posts
