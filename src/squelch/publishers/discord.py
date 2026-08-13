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
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..core.config import Config, Emphasis
from ..core.log import get_logger
from ..core.models import CaseReading, Digest, DigestEntry, Period, Status
from ..core.settings import Settings
from ..core.text import first_sentence, trim
from ..core.throttle import paced
from ..forum.bot import BotClient
from ..github.cases import CaseStore, reading_from_meta
from ..github.digests import DigestStore, digest_from_meta, period_of
from ..github.issues import IssueRecord, IssueStore

log = get_logger(__name__)

# Discord's documented limits. Every one of them is a 400 when exceeded, which
# is why nothing reaches the API without passing through trim first.
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
FOOTER_LIMIT = 2048
AUTHOR_LIMIT = 256
FIELD_NAME_LIMIT = 256
FIELD_VALUE_LIMIT = 1024
MAX_FIELDS = 25
MAX_EMBEDS = 10
# Links a roundup ends with. Past this the tail stops being a list somebody
# reads and the prompts do not ask for more anyway.
MAX_HIGHLIGHTS = 8
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

# -- the rubric's reply ------------------------------------------------------
#
# Colour carries the verdict here, unlike in the feed, where it only confirms
# what the size of an article already said: a review is a judgement, and its
# whole point is to be readable before it is read. Same family as everything
# else, brightest for the repositories worth an evening, the brief's grey for
# the ones that are a banner and four files.
REVIEW_TITLE = "What is actually in it"
REVIEW_HEADINGS = {
    "substance": "Substance",
    "mixed": "Mixed",
    "hype": "Hype",
}
REVIEW_COLORS = {
    "substance": 0xD85A30,
    "mixed": ACCENT_COLOR,
    "hype": 0x4A4A47,
}
# Prefixed to what a skill does, so the weak ones are visible while skimming
# without a column of icons. A real skill says nothing about itself: it is the
# baseline, and marking it too would make the list read as scored homework.
SKILL_MARKS = {"thin": "*thin* — ", "unclear": "*unclear* — "}
# A reply is one message. Past this the list stops being readable long before
# it reaches Discord's limit, and the repository is one click away.
MAX_REVIEWED_SKILLS = 15
REVIEW_DISCLAIMER = "squelch · read from the repository's files, nothing was executed"


# -- the forum's reply -------------------------------------------------------
#
# The community forum, where the traffic runs the other way: people post their
# own experiments and the bot answers inside the thread. Must match the channel
# id in delivery.yaml; the Discord channel it happens to be called is named
# there too, and renaming it changes nothing here.
CASES_CHANNEL = "discord-cases"
# No verdict colour, unlike the rubric. There is no verdict: a reading is not a
# score, and a palette that went green-to-grey would announce one before anybody
# had read a word.
CASE_HEADINGS = (
    ("checkable", "What would settle it"),
    ("assumed", "Taken on faith"),
    ("measure", "Worth measuring"),
)
# English chrome around a reply that is written in the poster's own language.
# Deliberate: these three phrases are the same three every time, they are what
# makes the shape of a reply recognisable at a glance, and the rest of the
# server — channel names, tags, the posting rules — is English already.
CASE_DISCLAIMER = "squelch · a reading of your post, not a verdict — argue with it"
# A reply is one message. Past this it stops being something anybody reads under
# their own post, and the numbers are in the thread anyway.
MAX_CASE_LINES = 6


class DiscordError(RuntimeError):
    pass


class Sent(NamedTuple):
    """What Discord answered with: the message, and where it ended up.

    A forum post is a thread, and the reply that belongs under it has to name
    that thread. Discord returns it as the message's ``channel_id``; the two
    happen to be equal for a post's opening message, but reading the field it
    actually documents beats relying on that.
    """

    message_id: str
    thread_id: str


# -- text fitting -----------------------------------------------------------


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
    """One webhook connection, with Discord's bucket bookkeeping attached.

    The URL is always passed in. There used to be a fall-back to the feed's
    webhook for callers that had none of their own, and it has to stay gone:
    the feed channel is the project's own working channel now, so a message
    that quietly landed there would be a message nobody outside reads. Every
    caller names its own channel or fails saying which secret is missing.
    """

    def __init__(self, settings: Settings, url: str) -> None:
        if not url.strip():
            raise DiscordError("no webhook URL was given for this channel")
        self._url = _with_wait(url)
        self._client = httpx.Client(
            timeout=settings.request_timeout,
            headers={"User-Agent": "squelch"},
        )
        # Learnt from the previous response: seconds to hold before the next
        # request, so an exhausted bucket costs a pause instead of a 429.
        self._cooldown = 0.0
        # None until asked for the first time; "" once we know we cannot get it.
        self._guild_id: str | None = None

    def __enter__(self) -> _Webhook:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _target(self, thread_id: str) -> str:
        """The URL for this one request: the webhook, plus the thread to post into."""
        if not thread_id:
            return self._url
        parts = urlsplit(self._url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["thread_id"] = thread_id
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def send(self, payload: dict[str, Any], thread_id: str = "") -> Sent:
        """POST one message and return what Discord said about where it landed.

        With ``thread_id`` the message joins an existing thread instead of
        opening one — that is how a reply reaches the post it belongs under.
        """
        last_error = ""
        target = self._target(thread_id)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_out_bucket()
            response = self._client.post(target, json=payload)
            self._note_bucket(response)

            if response.is_success:
                return self._sent(response)

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

    def guild_id(self) -> str:
        """The server this webhook posts into, asked once and remembered.

        Needed only to build a clickable link to a message: Discord's message
        URL is /channels/{guild}/{channel}/{message}, and the execute response
        carries the channel but not the server. Fetching the webhook object
        itself is the way to get it without another secret.

        Never fatal. A missing or unreadable guild costs the links and nothing
        else — the message id is still recorded, and callers fall back to the
        article's own URL.
        """
        if self._guild_id is None:
            self._guild_id = ""
            try:
                response = self._client.get(self._url.split("?")[0])
                if response.is_success:
                    self._guild_id = str(self._json(response).get("guild_id") or "")
            except Exception as exc:  # noqa: BLE001 - links are a nicety, delivery is not
                log.warning("could not read the webhook's guild: %s", exc)
            if not self._guild_id:
                log.warning("webhook did not name its guild; message links unavailable")
        return self._guild_id

    def message_url(self, channel_id: str, message_id: str) -> str:
        """A link a reader can click, or "" when we cannot build one."""
        guild = self.guild_id()
        if not (guild and channel_id and message_id):
            return ""
        return f"https://discord.com/channels/{guild}/{channel_id}/{message_id}"

    def _sent(self, response: httpx.Response) -> Sent:
        payload = self._json(response)
        message_id = str(payload.get("id", ""))
        if not message_id:
            log.warning("discord accepted the message but returned no id")
        # Falls back to the message id: for the opening message of a post the
        # two are the same, so a reply still has somewhere to go if Discord
        # ever stops sending the channel back.
        return Sent(message_id, str(payload.get("channel_id") or message_id))


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
        "title": trim(issue.title, TITLE_LIMIT),
        "color": WEIGHT_COLORS[weight],
    }
    if issue.url:
        embed["url"] = issue.url
    if issue.source:
        embed["author"] = {"name": trim(issue.source, AUTHOR_LIMIT)}

    # The picture the publisher chose for link previews. Discord fetches it
    # itself, so a dead link costs a blank space, not a failed message.
    if issue.image and weight is Weight.LEAD:
        embed["image"] = {"url": issue.image}
    elif issue.image and weight is Weight.STANDARD:
        embed["thumbnail"] = {"url": issue.image}

    footer = ["squelch"]
    if isinstance(score, int | float):
        footer.append(f"score {int(score)}/10")
    embed["footer"] = {"text": trim(" · ".join(footer), FOOTER_LIMIT)}

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
            embed["description"] = trim(meta_line, DESCRIPTION_LIMIT)
    else:
        embed["description"] = trim(body, min(SUMMARY_TARGET, DESCRIPTION_LIMIT))
        if meta_line:
            embed["fields"] = [{"name": "​", "value": trim(meta_line, FIELD_VALUE_LIMIT)}]

    stamp = _timestamp(issue.meta.get("published_at")) or _timestamp(issue.created_at)
    if stamp:
        embed["timestamp"] = stamp
    return embed


def _measured(facts: dict[str, int]) -> str:
    """The counted part of the headline: stars, and how many skills back them.

    Put beside the verdict rather than in a footer because the two are read
    together — "Hype · 822 ★ · 0 skills" is the entire argument of this
    rubric in one line, and it is also what tells a reader whether the
    repository is worth cloning to check for themselves.
    """
    parts = []
    if facts.get("stars"):
        parts.append(f"{facts['stars']:,} ★".replace(",", " "))
    if "skills" in facts:
        count = facts["skills"]
        parts.append("1 skill" if count == 1 else f"{count} skills")
    return " · ".join(parts)


def _review_embed(issue: IssueRecord) -> dict[str, Any] | None:
    """The rubric's reading of a repository, as the reply under its post.

    A list of lines rather than embed fields: this is an argument to be read
    top to bottom, and fields would break it into a form. Each skill carries
    its own verdict inline, so a collection of three real tools and nine
    paragraphs of prompt looks like exactly that at a glance.

    Returns None when there is nothing worth posting — a review the model
    declined to write, or one edited to nothing by hand on the issue.
    """
    review = issue.review
    verdict = str(review.get("verdict") or "").strip().lower()
    lines: list[str] = []

    opening = " ".join(str(review.get("promise") or "").split())
    heading = REVIEW_HEADINGS.get(verdict, "")
    # The numbers ride with the heading, never with the prose: they came from
    # the scraper, and a reader has to be able to tell them apart from what a
    # model concluded.
    measured = _measured(issue.facts)
    head = " · ".join(part for part in (f"**{heading}**" if heading else "", measured) if part)
    if head:
        lines.append(head + (f" — {opening}" if opening else ""))
    elif opening:
        lines.append(f"**{opening}**")

    skills = [s for s in review.get("skills") or [] if isinstance(s, dict)]
    if skills:
        lines.append("")
        lines += [_skill_line(skill) for skill in skills[:MAX_REVIEWED_SKILLS]]
        if len(skills) > MAX_REVIEWED_SKILLS:
            lines.append(f"…and {len(skills) - MAX_REVIEWED_SKILLS} more in the repository.")

    usefulness = " ".join(str(review.get("usefulness") or "").split())
    if usefulness:
        lines += ["", usefulness]

    if not lines:
        return None

    # Said every time, at the bottom, in the smallest voice available: this is a
    # reading of files, not a test run. The scraper never executes anything a
    # repository ships, and a verdict that sounds like it did would be a lie.
    return {
        "title": REVIEW_TITLE,
        "description": trim("\n".join(lines), DESCRIPTION_LIMIT),
        "color": REVIEW_COLORS.get(verdict, ACCENT_COLOR),
        "footer": {"text": REVIEW_DISCLAIMER},
    }


def _skill_line(skill: dict[str, Any]) -> str:
    name = " ".join(str(skill.get("name") or "").split()) or "unnamed"
    does = " ".join(str(skill.get("does") or "").split())
    mark = SKILL_MARKS.get(str(skill.get("verdict") or "").strip().lower(), "")
    return f"**{name}** — {mark}{does}" if does else f"**{name}** — {mark or 'no description'}"


def _rejected_embed(issue: IssueRecord) -> dict[str, Any]:
    """One rejected article: the verdict, and the way to appeal it.

    Deliberately the size of a brief — this channel exists so the classifier's
    mistakes are visible, not to compete with the feed. The reason is the
    content here, and the issue link is the call to action: enough 👍 on the
    issue and the rescue pass sends the article back for its write-up.
    """
    embed: dict[str, Any] = {
        "title": trim(issue.title, TITLE_LIMIT),
        "color": REJECTED_COLOR,
    }
    if issue.url:
        embed["url"] = issue.url
    if issue.source:
        embed["author"] = {"name": trim(issue.source, AUTHOR_LIMIT)}

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
        embed["description"] = trim("\n".join(lines), DESCRIPTION_LIMIT)

    embed["footer"] = {"text": "squelch · rejected"}
    stamp = _timestamp(issue.meta.get("published_at")) or _timestamp(issue.created_at)
    if stamp:
        embed["timestamp"] = stamp
    return embed


def _case_embed(reading: CaseReading) -> dict[str, Any] | None:
    """One reading, as the reply that goes under somebody's post.

    Prose first, then at most three short lists, in the order somebody would
    want them: what they found, what would settle the open part, what the post
    is assuming, what to run next. A list that came back empty is simply absent
    — an empty heading reads as a section the bot failed to fill, and the
    schema allows empty on purpose.

    Returns None when there is nothing to say at all, which the queue treats as
    a case still waiting rather than one answered with silence.
    """
    claim = " ".join(reading.claim.split())
    if not claim:
        return None

    lines = [f"**{claim}**"]
    for field, heading in CASE_HEADINGS:
        items = [
            " ".join(str(item).split())
            for item in getattr(reading, field)
            if str(item).strip()
        ]
        if not items:
            continue
        lines += ["", f"**{heading}**"]
        lines += [f"- {item}" for item in items[:MAX_CASE_LINES]]

    return {
        "description": trim("\n".join(lines), DESCRIPTION_LIMIT),
        "color": ACCENT_COLOR,
        "footer": {"text": CASE_DISCLAIMER},
    }


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
        payload["thread_name"] = trim(thread_name, THREAD_NAME_LIMIT)
    if applied_tags:
        payload["applied_tags"] = applied_tags[:MAX_APPLIED_TAGS]
    return payload


def _highlight_lines(
    entries: list[DigestEntry], budget: int, links: dict[str, str] | None = None
) -> list[str]:
    """The articles as bare links, dropping the tail rather than half a link.

    No commentary per entry: what a release means is said once, in the body.
    Repeating it item by item is what made the roundup a list of captions.

    A link points at the feed channel's own post about that article where the
    delivery pass recorded one — the roundup is the way into that channel, and
    the discussion has already gathered there. Anything the feed has not
    carried keeps the publisher's URL.
    """
    lines: list[str] = []
    for entry in entries[:MAX_HIGHLIGHTS]:
        title = " ".join(entry.title.split())
        target = (links or {}).get(entry.url) or entry.url
        line = f"• [{title}]({target})" if target else f"• {title}"
        if len(line) + 1 > budget:
            log.warning("digest trimmed to %d of %d highlights", len(lines), len(entries))
            break
        budget -= len(line) + 1
        lines.append(line)
    return lines


def _digest_embeds(
    digest: Digest,
    period: Period,
    window: str = "",
    links: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """One roundup as one embed, read in layers.

    The title is the stretch covered — a reader opening the channel already
    knows it is a roundup, and on a Monday two of these arrive together. Then
    the brief in bold, which is the whole point in two sentences for somebody
    who will read nothing else, and the detailed block under it for whoever
    went on. One paragraph asked to do both jobs did neither.
    """
    blocks = []
    brief = " ".join(digest.brief.split())
    if brief:
        blocks.append(f"**{brief}**")
    if digest.summary.strip():
        blocks.append(" ".join(digest.summary.split()))

    trends = "\n".join(f"• {trend.strip()}" for trend in digest.trends if trend.strip())
    if trends:
        blocks.append(trends)

    # The body is what this channel is for, so it is spent first and the links
    # take what is left. Losing the last link costs a click; losing the body
    # costs the roundup.
    spent = sum(len(block) + 2 for block in blocks) + len(HIGHLIGHTS_TITLE) + 4
    lines = _highlight_lines(
        digest.highlights, min(DESCRIPTION_LIMIT, MESSAGE_LIMIT) - spent, links
    )
    if lines:
        blocks.append(f"**{HIGHLIGHTS_TITLE}**\n" + "\n".join(lines))

    # Falls back to the period alone for a roundup stored before windows were
    # recorded; nothing writes one without a window now.
    title = f"{period.label.capitalize()} · {window}" if window else period.label.capitalize()
    return [
        {
            "title": trim(title, TITLE_LIMIT),
            "description": trim("\n\n".join(block for block in blocks if block), DESCRIPTION_LIMIT),
            "color": ACCENT_COLOR,
            # Both roundups share a channel, so the footer still names which is
            # which even when the title is a date.
            "footer": {"text": f"squelch · {period.label}"},
        }
    ]


# -- entry points -----------------------------------------------------------


def _post_review(webhook: _Webhook, issue: IssueRecord, thread_id: str) -> str:
    """Reply to a post with the rubric's reading of it; returns the message id.

    Never fatal. The article is out and correct without its review, so a failure
    here costs the analysis and nothing else — which is why it is attempted
    before the delivery is recorded, and why the recorded id is what tells the
    next run whether to try again.
    """
    embed = _review_embed(issue)
    if embed is None or not thread_id:
        return ""
    try:
        return webhook.send(_payload([embed]), thread_id=thread_id).message_id
    except Exception as exc:  # noqa: BLE001 - the article itself is already out
        log.error("#%d is posted but its review is not: %s", issue.number, exc)
        return ""


def _deliver(
    webhook: _Webhook,
    store: IssueStore,
    issues: list[IssueRecord],
    channel: str,
    make_embed: Callable[[IssueRecord], dict[str, Any]],
    delay: float,
    forum: bool = False,
    tags: Callable[[IssueRecord], list[str]] | None = None,
    review: bool = False,
) -> tuple[int, int]:
    """Post each issue and mark it delivered; returns (posted, relabelled).

    One bad article never stops the batch: whatever fails keeps its place in
    the queue and the next run tries it again.
    """
    posted = 0
    relabelled = 0
    for issue in paced(issues, delay):
        record = issue.delivery(channel)
        known_id = str(record.get("message_id") or "")
        if known_id:
            # A previous run posted this and died before its label landed.
            # Reposting would double it up, so only the label moves — but the
            # reply may be what it died on, so that part is retried here. This
            # is the one chance it gets: after the label, the article leaves
            # the queue for good.
            #
            # Everything that run recorded is carried forward except when it
            # happened — record_delivery stamps that itself, and keeping the
            # old value would freeze the delivery time at the run that failed.
            details = {k: v for k, v in record.items() if k != "at"}
            details["message_id"] = known_id
            if review and not record.get("review_message_id"):
                thread_id = str(record.get("thread_id") or known_id)
                details["review_message_id"] = _post_review(webhook, issue, thread_id)
            try:
                store.record_delivery(issue, channel, details)
                relabelled += 1
            except Exception as exc:  # noqa: BLE001 - retried on the next run
                log.error("#%d is already on Discord but will not relabel: %s", issue.number, exc)
            continue

        try:
            # In a forum the article's own headline names the post, so the
            # channel reads as a list of stories rather than a wall of cards,
            # and its topic labels become the tags readers filter by.
            sent = webhook.send(
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

        # The link back to this very message. Recorded now, on the article's
        # own issue, because it is what a digest later points its highlights at
        # — a roundup is an index into this channel, not a replacement for it.
        details: dict[str, Any] = {"message_id": sent.message_id}
        link = webhook.message_url(sent.thread_id, sent.message_id)
        if link:
            details["message_url"] = link
        if review:
            # The analysis is the reason this channel is a forum: it goes in as
            # a reply under the post, where the argument about it belongs, and
            # the thread id is kept so a failed reply can be retried above.
            details["thread_id"] = sent.thread_id
            details["review_message_id"] = _post_review(webhook, issue, sent.thread_id)

        try:
            store.record_delivery(issue, channel, details)
        except Exception as exc:  # noqa: BLE001 - the message is out either way
            # Logged loudly: the id exists nowhere else yet, and without it
            # on the issue the next run has no way to know not to repost.
            log.error(
                "#%d posted as message %s but was not recorded: %s",
                issue.number,
                sent.message_id or "?",
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
                channel.review,
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


def publish_digests(settings: Settings, config: Config, store: DigestStore) -> int:
    """Post the roundups waiting in the queue and mark each one delivered.

    Both periods go to the same channel on purpose: the daily and the weekly
    are the same promise to the same reader — open this one place and you know
    what happened — and splitting them would only make each half look quiet.
    What tells them apart is the footer, and in a forum the post's own title.

    No fallback to the feed's webhook. The feed channel is where every article
    lands one by one, and it is not the channel people read; a roundup posted
    there by accident would be published to nobody.

    Nothing here closes an issue, and one bad roundup never stops the batch:
    whatever fails keeps its place in the queue for the next run.
    """
    channels = config.digest_channels
    if not channels:
        log.info("no digest channel is enabled in delivery.yaml, nothing to do")
        return 0
    if not settings.discord_digest_webhook_url:
        raise DiscordError("DISCORD_DIGEST_WEBHOOK_URL is not set")

    total = 0
    for channel in channels:
        pending = store.list_pending(channel.id, limit=settings.publish_batch_size)
        if not pending:
            log.info("nothing to post for %s", channel.id)
            continue
        with _Webhook(settings, settings.discord_digest_webhook_url) as webhook:
            total += _deliver_digests(webhook, store, pending, channel.id, settings)
    return total


def publish_cases(settings: Settings, config: Config, store: CaseStore) -> int:
    """Post each waiting reading into the thread it was written about.

    Through the bot rather than a webhook, because this channel is read as well
    as written and one credential is enough — see ``forum/bot.py``. What it
    costs is that a reply is a plain message in an existing thread: no thread
    name, no tags, nothing that opens a post. That is the right shape anyway.
    The bot must never be able to start a conversation in there.

    Nothing here closes an issue, and one failed reply never stops the batch.
    """
    channels = config.case_channels
    if not channels:
        log.info("no case channel is enabled in delivery.yaml, nothing to do")
        return 0

    total = 0
    with BotClient(settings) as bot:
        for channel in channels:
            pending = store.list_pending(channel.id, limit=settings.publish_batch_size)
            if not pending:
                log.info("nothing to answer for %s", channel.id)
                continue
            total += _deliver_cases(bot, store, pending, channel.id, settings)
    return total


def _deliver_cases(
    bot: BotClient,
    store: CaseStore,
    issues: list[IssueRecord],
    channel: str,
    settings: Settings,
) -> int:
    answered = 0
    for issue in paced(issues, settings.publish_delay_seconds):
        record = issue.delivery(channel)
        if record:
            # Already answered by a run that died before its label landed.
            # Posting again would put a second reply under somebody's post,
            # which is the one mistake this channel cannot take back.
            details = {k: v for k, v in record.items() if k != "at"}
            try:
                store.record_delivery(issue, channel, details)
            except Exception as exc:  # noqa: BLE001 - retried on the next run
                log.error("#%d is already answered but will not relabel: %s", issue.number, exc)
            continue

        reading = reading_from_meta(issue.meta)
        embed = _case_embed(reading) if reading else None
        thread_id = str(issue.meta.get("thread_id") or "")
        if embed is None or not thread_id:
            # Left in the queue rather than skipped quietly: an unreadable
            # block or a lost thread id is something to fix on the issue, and
            # the person is still waiting either way.
            log.error("#%d holds no readable reading or no thread, skipping it", issue.number)
            continue

        try:
            message_id = bot.reply(thread_id, _payload([embed]))
        except Exception as exc:  # noqa: BLE001 - one case must not stop the batch
            log.error("could not answer #%d: %s", issue.number, exc)
            continue
        answered += 1

        try:
            store.record_delivery(issue, channel, {"message_id": message_id})
        except Exception as exc:  # noqa: BLE001 - the reply is out either way
            log.error(
                "#%d answered as message %s but was not recorded: %s",
                issue.number,
                message_id or "?",
                exc,
            )
        else:
            log.info("answered case #%d as message %s", issue.number, message_id)
    return answered


def _deliver_digests(
    webhook: _Webhook,
    store: DigestStore,
    issues: list[IssueRecord],
    channel: str,
    settings: Settings,
) -> int:
    posted = 0
    for issue in paced(issues, settings.publish_delay_seconds):
        record = issue.delivery(channel)
        if record:
            # A previous run posted this and died before its label landed.
            # Reposting would double it up, so only the label moves.
            #
            # Any record at all counts, not just one carrying a message id:
            # nothing writes this except a send that already succeeded, and
            # Discord occasionally answers 2xx with a body we cannot read, which
            # leaves the id blank. Treating a blank id as "never posted" would
            # reopen the exact window this bookkeeping exists to close.
            details = {k: v for k, v in record.items() if k != "at"}
            try:
                store.record_delivery(issue, channel, details)
            except Exception as exc:  # noqa: BLE001 - retried on the next run
                log.error("#%d is already posted but will not relabel: %s", issue.number, exc)
            continue

        period = period_of(issue)
        digest = digest_from_meta(issue.meta)
        if digest is None:
            # Edited into something unreadable. Left in the queue on purpose:
            # the fix is to repair the block, and a silent skip would hide that.
            log.error("#%d holds no readable roundup, skipping it", issue.number)
            continue

        thread_name = ""
        if settings.digest_forum:
            # A forum post cannot be nameless, and a model can return a blank
            # headline; a dull title beats a 400 that loses the whole roundup.
            thread_name = first_sentence(digest.brief) or period.label.capitalize()

        try:
            window = str(issue.meta.get("window") or "")
            stored = issue.meta.get("links")
            links = stored if isinstance(stored, dict) else {}
            sent = webhook.send(
                _payload(_digest_embeds(digest, period, window, links), thread_name)
            )
        except Exception as exc:  # noqa: BLE001 - one roundup must not stop the batch
            log.error("could not post #%d: %s", issue.number, exc)
            continue
        posted += 1

        try:
            store.record_delivery(issue, channel, {"message_id": sent.message_id})
        except Exception as exc:  # noqa: BLE001 - the message is out either way
            log.error(
                "#%d posted as message %s but was not recorded: %s",
                issue.number,
                sent.message_id or "?",
                exc,
            )
        else:
            log.info("posted %s #%d as message %s", period.label, issue.number, sent.message_id)
    return posted
