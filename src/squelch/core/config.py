"""Editorial policy, the source catalogue and the delivery targets.

Split across files on purpose, one per thing you would sit down to change:
``feed.yaml`` is what the feed *is*, ``sources.yaml`` is where it looks,
``delivery.yaml`` is where it goes, and ``models.yaml`` plus ``prompts/`` are
how it decides. Editing the source list should never mean scrolling past the
policy, and vice versa.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

CONFIG_DIR = Path("config")
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
    # rejected issues are closed already.
    consumes: Literal["ready", "rejected"] = "ready"
    # Routing, by the labels on the issue. ``only`` delivers just the articles
    # carrying at least one of the listed labels; ``skip`` delivers everything
    # except those. This is sectioning, not filtering: every article must still
    # match some enabled channel, and the closing pass only counts the channels
    # an article was actually routed to.
    only: list[str] = Field(default_factory=list)
    skip: list[str] = Field(default_factory=list)
    emphasis: Emphasis = Field(default_factory=Emphasis)

    @model_validator(mode="after")
    def _one_selector(self) -> Channel:
        if self.only and self.skip:
            raise ValueError(
                f"channel {self.id} sets both `only` and `skip`; pick one — "
                "their combined meaning would be ambiguous"
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
    def required_channels(self) -> list[str]:
        """The channels an article must reach before it counts as published."""
        return [c.id for c in self.ready_channels]

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
