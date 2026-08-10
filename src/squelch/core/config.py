"""Source catalogue and editorial policy, loaded from config/sources.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

CONFIG_PATH = Path("config/sources.yaml")


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
    # the filter prompt, so it is the main knob for what survives filtering.
    focus: str
    # The LLM may only tag articles from this list, which keeps the label set
    # on the repository finite.
    topics: list[str] = Field(default_factory=list)
    max_body_chars: int = 8000
    sources: list[Source] = Field(default_factory=list)

    @property
    def enabled_sources(self) -> list[Source]:
        return [s for s in self.sources if s.enabled]


def load_config(path: Path | None = None) -> Config:
    target = path or CONFIG_PATH
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    return Config.model_validate(data)
