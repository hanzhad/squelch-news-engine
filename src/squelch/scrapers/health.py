"""Are the sources still shaped the way we wired them up?

A selector rots quietly. A vendor restyles its newsroom, ``a[href^="/news/"]``
stops matching, the scrape logs one warning among a hundred lines and the run
stays green — because an empty scrape is not a failure, and must not be. Nobody
notices until somebody wonders why Anthropic has said nothing for a month.

So the check is a separate run with the opposite rule: here, yielding nothing
*is* the failure. It works by running the real scrapers over the first couple of
items rather than by asserting anything about the markup, which means there is
no second description of a source to drift out of step with the first. Whatever
breaks the scrape breaks the check.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import httpx

from ..core.config import Config, Source
from ..core.log import get_logger
from ..core.settings import Settings
from .extract import new_http_client
from .runner import scraper_for

log = get_logger(__name__)

# Enough to tell a working source from a dead one, few enough that checking the
# whole catalogue costs a handful of requests rather than a full scrape.
PROBE_ITEMS = 2


class SourceHealth(NamedTuple):
    """What one source produced when asked for a couple of articles."""

    source: str
    type: str
    articles: int = 0
    dated: int = 0
    imaged: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        """Zero articles counts as broken here, unlike during a scrape.

        A live source can legitimately have nothing new; it cannot legitimately
        have nothing at all, because these are listings and feeds, not queues.
        """
        return not self.error and self.articles > 0


def probe(source: Source, config: Config, client: httpx.Client) -> SourceHealth:
    """Run ``source`` for a couple of items and report what came back."""
    scraper: Any = scraper_for(source)
    if scraper is None:
        return SourceHealth(source.id, source.type, error=f"unknown type {source.type!r}")

    # The scrapers cap themselves by max_items, so a smaller one is all it takes
    # to keep the check cheap. A copy, because config is shared across the run.
    sample = source.model_copy(update={"max_items": min(source.max_items, PROBE_ITEMS)})
    try:
        articles = scraper(sample, config, client)
    except Exception as exc:  # noqa: BLE001 - a crashing source is a finding, not an abort
        return SourceHealth(source.id, source.type, error=f"{type(exc).__name__}: {exc}")

    return SourceHealth(
        source=source.id,
        type=source.type,
        articles=len(articles),
        dated=sum(article.published_at is not None for article in articles),
        imaged=sum(bool(article.image) for article in articles),
    )


def check_sources(
    config: Config,
    settings: Settings,
    only_type: str | None = None,
    only_source: str | None = None,
) -> list[SourceHealth]:
    """Probe every enabled source, in catalogue order."""
    results: list[SourceHealth] = []
    with new_http_client(settings.request_timeout) as client:
        for source in config.enabled_sources:
            if only_type and source.type != only_type:
                continue
            if only_source and source.id != only_source:
                continue
            result = probe(source, config, client)
            log.debug("probed %s: %s", source.id, result)
            results.append(result)
    return results


def report(results: list[SourceHealth]) -> None:
    """Log one line per source, wide enough to read as a table.

    Dates and pictures are counted but never judged: plenty of pages state no
    date and carry no picture, and turning that into a failure would make this
    check cry wolf until it gets ignored. They are here so that a human reading
    a green run can still see a source that used to have both and now has
    neither.
    """
    width = max((len(r.source) for r in results), default=6)
    log.info(
        "%-*s  %-4s  %5s  %5s  %5s  %s", width, "source", "type", "items", "dated", "img", "status"
    )
    for result in sorted(results, key=lambda r: (r.ok, r.source)):
        log.info(
            "%-*s  %-4s  %5d  %5d  %5d  %s",
            width,
            result.source,
            result.type,
            result.articles,
            result.dated,
            result.imaged,
            result.error or ("ok" if result.ok else "BROKEN: yielded nothing"),
        )
