"""Per-site scrapers, for sources a CSS selector cannot express.

Most sources need no code at all: `config/sources.yaml` declares a feed URL or
a listing page plus a selector, and `rss.py` / `html.py` / `web.py` handle them
generically. That is the path to prefer — one line of config beats one file of
Python, and the whole source list stays readable on one screen.

A site earns a module here when it needs real logic: an undocumented JSON API
behind the page, pagination, a login, a date format only it uses, or an
anti-bot dance. Write the module, register it against the source id, and the
runner will call it instead of the generic scraper for that source. Everything
else about the source — id, cadence, item cap — still comes from the config.

    from ..extract import fetch_full_text
    from . import register

    @register("xai")
    def scrape(source, config, client):
        ...
        return [RawArticle(...), ...]
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ...core.config import Config, Source
from ...core.log import get_logger
from ...core.models import RawArticle

log = get_logger(__name__)

Scraper = Callable[[Source, Config, Any], list[RawArticle]]

_REGISTRY: dict[str, Scraper] = {}


def register(source_id: str) -> Callable[[Scraper], Scraper]:
    """Claim a source id for a hand-written scraper."""

    def decorator(scraper: Scraper) -> Scraper:
        if source_id in _REGISTRY:
            raise ValueError(f"two scrapers registered for source {source_id!r}")
        _REGISTRY[source_id] = scraper
        log.debug("registered custom scraper for %s", source_id)
        return scraper

    return decorator


def get(source_id: str) -> Scraper | None:
    """The hand-written scraper for this source, if there is one."""
    return _REGISTRY.get(source_id)


def registered() -> list[str]:
    return sorted(_REGISTRY)
