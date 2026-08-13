"""Editorial policy, the source catalogue and the delivery targets.

Split across files on purpose, one per thing you would sit down to change:
``feed.yaml`` is what the feed *is*, ``sources.yaml`` is where it looks,
``delivery.yaml`` is where it goes, and ``models.yaml`` — with the top-level
``prompts/`` — is how it decides. Editing the source list should never mean
scrolling past the policy, and vice versa.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CONFIG_DIR = Path("config")
# A channel link as Discord's own "Copy Link" gives it. Anchored, because the
# failure mode this guards against is a *nearly* right URL — a message link with
# a third id on the end would otherwise read its message id as the channel.
FORUM_URL_RE = re.compile(r"^https://(?:\w+\.)?discord\.com/channels/(\d+)/(\d+)/?$")
FEED_PATH = CONFIG_DIR / "feed.yaml"
SOURCES_PATH = CONFIG_DIR / "sources.yaml"
DELIVERY_PATH = CONFIG_DIR / "delivery.yaml"


class Source(BaseModel):
    id: str
    type: Literal["rss", "html", "web", "github"] = "rss"
    # For type: github this is the human-clickable search page; the scraper
    # runs its `q=` parameter against the API, so the config stays one URL per
    # source and the query is editable without touching code.
    url: str
    enabled: bool = True
    # Follow the link and extract the full article text instead of trusting the
    # feed summary. Costs one HTTP request per item.
    fetch_full_text: bool = True
    # Hard cap on items taken from this source per run.
    max_items: int = 10
    # For type: html and web — CSS selector matching the article links on the
    # listing page. Everything it matches is treated as an article to extract.
    link_selector: str | None = None


class Emphasis(BaseModel):
    """The scores at which an article earns more room in a channel.

    Read as thresholds from the top down: at ``lead`` or above an article is
    given the full treatment, at ``standard`` or above a smaller one, and
    anything below that is reduced to its headline. Nothing is ever dropped —
    this decides size, not whether an article is delivered.
    """

    lead: int = Field(default=7, ge=0, le=10)
    standard: int = Field(default=4, ge=0, le=10)

    @model_validator(mode="after")
    def _ordered(self) -> Emphasis:
        if self.standard > self.lead:
            raise ValueError(
                f"emphasis.standard ({self.standard}) is above emphasis.lead ({self.lead}), "
                "which would make the lead tier unreachable"
            )
        return self


class DigestPolicy(BaseModel):
    """How much of the window one roundup is allowed to carry.

    Two knobs that only mean something together. ``max_articles`` is the size
    the roundup aims for; ``min_score`` is what an article has to be worth to
    get in *past* that size. So the cap is a budget rather than a verdict — a
    late-ranked article that scored well still makes it, and nothing is dropped
    for being article number sixty-one alone.

    Shipped with ``min_score: 0``, which keeps everything and makes the cap
    inert: at the volume this feed runs at there is nothing to ration yet, and
    a threshold nobody chose deliberately is exactly the kind of silent filter
    this pipeline is against. Raise it the day a week genuinely stops fitting.
    """

    max_articles: int = Field(default=60, ge=1)
    min_score: int = Field(default=0, ge=0, le=10)
    # Which channel's post a highlight links to. The roundup is an index into
    # the article-by-article feed rather than a replacement for it, so a link
    # lands on the message about that article and the discussion under it.
    # Falls back to the article's own URL for anything that channel has not
    # carried yet, and empty here links straight to the articles throughout.
    link_channel: str = "discord"
    # What each article contributes to the prompt: its write-up, cut to this.
    # Stage two writes two to four sentences, so this is sized to leave a normal
    # summary whole and to bound the fallback — an issue opened by hand has no
    # summary, and the raw body behind it runs to max_body_chars.
    summary_chars: int = Field(default=1000, ge=100)


class Channel(BaseModel):
    """One place a ready article is delivered to.

    There is no address here on purpose: where a channel actually posts is a
    credential, and credentials come from the environment. This file only
    decides which channels count, and how much room each article gets once it
    is there.
    """

    id: str
    enabled: bool = True
    # What this channel consumes. Almost every channel delivers ready articles
    # and takes part in the count that closes an issue; a ``rejected`` channel
    # instead shows what the classifier threw away, and never gates closing —
    # rejected issues are closed already. A ``digest`` channel consumes roundups,
    # which are issues of their own kind: same delivery bookkeeping, a queue no
    # article is ever in, and so no part in closing one either. A ``cases``
    # channel is the one that also *reads* — see ``forum_url``.
    consumes: Literal["ready", "rejected", "digest", "cases"] = "ready"
    # The forum this channel reads from, as the link you would paste from
    # Discord: https://discord.com/channels/<guild>/<channel>. Only a `cases`
    # channel has one, and it is the single exception to "no address in this
    # file" — it names a public channel rather than opening one, exactly like
    # the tag ids below, and the credential that actually grants access is the
    # bot token in the environment. Having it here is what lets the forum be
    # pointed somewhere else without a deploy.
    forum_url: str = ""
    # Routing, by the labels on the issue. ``only`` delivers just the articles
    # carrying at least one of the listed labels; ``skip`` delivers everything
    # except those. This is sectioning, not filtering: every article must still
    # match some enabled channel, and the closing pass only counts the channels
    # an article was actually routed to.
    only: list[str] = Field(default_factory=list)
    skip: list[str] = Field(default_factory=list)
    # Whether this channel is a Discord forum rather than a text channel. A
    # forum has no message stream: every article becomes a post of its own,
    # which the webhook has to name up front. Discord answers 400 to a forum
    # message with no thread name and to a text message that carries one, so
    # this has to be told rather than guessed, and it must match what the
    # channel actually is.
    forum: bool = False
    # Forum tags, as ``issue label -> Discord tag id``. Ids are not secrets —
    # they name nothing and unlock nothing — so they live here in the open,
    # beside the channel they belong to, rather than in the environment with
    # the webhook. They cannot be looked up either: no webhook endpoint lists a
    # forum's tags, so the mapping is written down once by hand and only
    # changes when someone renames a tag in Discord.
    tags: dict[str, str] = Field(default_factory=dict)
    # Whether articles routed here get a review — the skill-by-skill reading of
    # what a repository actually contains, written by its own LLM stage and
    # posted as a follow-up inside the article's thread. A flag on the channel
    # rather than a list of labels in Python: which articles are skill
    # collections is already spelled out by this channel's `only`, and saying
    # it twice would let the two drift apart. Forum-only, since a follow-up
    # needs a thread to land in.
    review: bool = False
    emphasis: Emphasis = Field(default_factory=Emphasis)

    @field_validator("tags")
    @classmethod
    def _tag_ids_are_snowflakes(cls, value: dict[str, str]) -> dict[str, str]:
        """A mistyped id is a 400 at publish time; catch it at load instead.

        Pasting a tag *name* here is the easy mistake, and Discord's complaint
        about it arrives days later in a workflow log nobody is reading.
        """
        bad = {label: tag for label, tag in value.items() if not str(tag).isdigit()}
        if bad:
            raise ValueError(
                f"forum tag ids must be numeric Discord ids, got {bad} — "
                "copy the id, not the tag's name"
            )
        return value

    @field_validator("forum_url")
    @classmethod
    def _forum_url_is_a_channel_link(cls, value: str) -> str:
        """Catch a mistyped link at load, not on the first cron tick.

        Everything downstream reads two ids out of this string, so a link to a
        message — or to the server — would otherwise send the bot looking for a
        forum that does not exist, once an hour, in a log nobody is reading.
        """
        url = value.strip()
        if url and not FORUM_URL_RE.match(url):
            raise ValueError(
                f"forum_url must be a channel link like "
                f"https://discord.com/channels/<server>/<channel>, got {url[:60]!r}"
            )
        return url

    @property
    def forum_ids(self) -> tuple[str, str]:
        """``(guild id, channel id)`` from ``forum_url``, or empty strings."""
        match = FORUM_URL_RE.match(self.forum_url)
        return (match.group(1), match.group(2)) if match else ("", "")

    def tag_ids(self, labels: set[str], limit: int) -> list[str]:
        """Tag ids for an article carrying ``labels``, in configured order.

        Config order decides which survive the cap, so the mapping is written
        most-telling-first and the overflow is the author's choice rather than
        whatever a set happened to yield.
        """
        matched = [tag for label, tag in self.tags.items() if label in labels]
        # The same id can be reached by two labels; Discord rejects duplicates.
        seen: dict[str, None] = dict.fromkeys(matched)
        return list(seen)[:limit]

    @model_validator(mode="after")
    def _one_selector(self) -> Channel:
        if self.only and self.skip:
            raise ValueError(
                f"channel {self.id} sets both `only` and `skip`; pick one — "
                "their combined meaning would be ambiguous"
            )
        if self.review and not self.forum:
            # The review is a second message inside the article's own thread. A
            # text channel has no thread to put it in, so it would either be
            # dropped or land as a loose message under an unrelated article —
            # and the LLM stage would run and be paid for regardless.
            raise ValueError(
                f"channel {self.id} sets `review` without `forum`; a review is posted "
                "into the article's thread, and a text channel has none"
            )
        if self.consumes == "cases" and not self.forum_url:
            # The one channel that reads. Without the link there is nothing to
            # read, and the stage would run green over an empty forum forever.
            raise ValueError(
                f"channel {self.id} consumes cases but names no `forum_url`; "
                "paste the channel's link from Discord"
            )
        return self

    def wants(self, labels: set[str]) -> bool:
        """Whether an article carrying ``labels`` belongs in this channel."""
        if self.only:
            return bool(set(self.only) & labels)
        if self.skip:
            return not (set(self.skip) & labels)
        return True


class Config(BaseModel):
    # The public name of the feed — masthead, RSS channel, Discord digest. Kept
    # apart from the repository name so the community can be branded without
    # renaming the engine.
    title: str = "Squelch"
    # Plain-language description of what this feed is about. Goes straight into
    # the classifier prompt, so it is the main knob for what survives.
    focus: str
    # The LLM may only tag articles from this list, which keeps the label set
    # on the repository finite.
    topics: list[str] = Field(default_factory=list)
    # Articles dated further back than this are not news any more; see the
    # comment in feed.yaml for why a listing page makes this necessary.
    max_age_days: int = Field(default=21, ge=1)
    max_body_chars: int = 8000
    digest: DigestPolicy = Field(default_factory=DigestPolicy)
    sources: list[Source] = Field(default_factory=list)
    channels: list[Channel] = Field(default_factory=list)

    @property
    def enabled_sources(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]

    @property
    def ready_channels(self) -> list[Channel]:
        """The enabled channels that deliver ready articles and gate closing."""
        return [c for c in self.channels if c.enabled and c.consumes == "ready"]

    @property
    def digest_channels(self) -> list[Channel]:
        """The enabled channels that post roundups.

        Never part of ``ready_channels``, so a roundup channel cannot hold an
        article open waiting for a delivery no article was ever queued for —
        the same rule the rejected channel lives by.
        """
        return [c for c in self.channels if c.enabled and c.consumes == "digest"]

    @property
    def case_channels(self) -> list[Channel]:
        """The enabled channels that read the community forum and answer in it.

        Kept out of ``ready_channels`` for the same reason the roundups are: a
        case is never queued for the article channels, so counting one here
        would leave every ready article open waiting for a delivery that is
        never coming.
        """
        return [c for c in self.channels if c.enabled and c.consumes == "cases"]

    @property
    def required_channels(self) -> list[str]:
        """The channels an article must reach before it counts as published."""
        return [c.id for c in self.ready_channels]

    def wants_review(self, labels: set[str]) -> bool:
        """Whether an article carrying ``labels`` is owed a skill-by-skill review.

        Asked of the routing rather than of the labels directly: an article gets
        a review because some enabled channel publishes reviews and this article
        belongs there. Switch that channel off and the extra LLM call stops with
        it, which is the point of asking the question this way round.
        """
        return any(c.enabled and c.review and c.wants(labels) for c in self.channels)

    def channel(self, channel_id: str) -> Channel:
        """One channel's settings, or the defaults if it is not configured."""
        return next((c for c in self.channels if c.id == channel_id), Channel(id=channel_id))


def _read(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def load_config(
    feed: Path | None = None,
    sources: Path | None = None,
    delivery: Path | None = None,
) -> Config:
    merged = _read(feed or FEED_PATH)
    merged.update(_read(sources or SOURCES_PATH))
    merged.update(_read(delivery or DELIVERY_PATH))
    return Config.model_validate(merged)
