"""Editorial policy and the source catalogue.

Split across files on purpose, one per thing you would sit down to change:
``feed.yaml`` is what the feed *is*, ``sources.yaml`` is where it looks, and
``models.yaml`` plus ``prompts/`` are how it decides. Editing the source list
should never mean scrolling past the policy, and vice versa.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

CONFIG_DIR = Path("config")
FEED_PATH = CONFIG_DIR / "feed.yaml"
SOURCES_PATH = CONFIG_DIR / "sources.yaml"


class Source(BaseModel):
    id: str
    type: Literal["rss", "html", "web"] = "rss"
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
    max_body_chars: int = 8000
    sources: list[Source] = Field(default_factory=list)

    @property
    def enabled_sources(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]


def _read(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def load_config(feed: Path | None = None, sources: Path | None = None) -> Config:
    merged = _read(feed or FEED_PATH)
    merged.update(_read(sources or SOURCES_PATH))
    return Config.model_validate(merged)
