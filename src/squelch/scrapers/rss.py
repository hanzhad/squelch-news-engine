"""RSS and Atom sources.

feedparser is fed bytes we fetched ourselves rather than a URL, so that
timeouts, redirects and the user agent are under our control.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import struct_time

import feedparser
import httpx

from ..core.config import Config, Source
from ..core.log import get_logger
from ..core.models import RawArticle
from .extract import fetch_full_text, strip_html

log = get_logger(__name__)


def _to_datetime(parsed: struct_time | None) -> datetime | None:
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _entry_summary(entry: object) -> str:
    """Best available text straight from the feed."""
    contents = getattr(entry, "content", None) or []
    for item in contents:
        value = item.get("value") if isinstance(item, dict) else None
        if value:
            return strip_html(value)
    return strip_html(getattr(entry, "summary", "") or "")


def scrape(source: Source, config: Config, client: httpx.Client) -> list[RawArticle]:
    try:
        response = client.get(source.url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("source %s unreachable: %s", source.id, exc)
        return []

    feed = feedparser.parse(response.content)
    if feed.bozo and not feed.entries:
        log.error("source %s returned unparseable feed: %s", source.id, feed.bozo_exception)
        return []

    articles: list[RawArticle] = []
    for entry in feed.entries[: source.max_items]:
        link = getattr(entry, "link", "") or ""
        title = getattr(entry, "title", "") or ""
        if not link or not title.strip():
            continue

        body = _entry_summary(entry)
        if source.fetch_full_text:
            full = fetch_full_text(client, link)
            # Feeds often carry a one-line teaser; prefer whichever is richer.
            if len(full) > len(body):
                body = full

        try:
            articles.append(
                RawArticle(
                    title=title,
                    url=link,
                    source=source.id,
                    published_at=_to_datetime(getattr(entry, "published_parsed", None))
                    or _to_datetime(getattr(entry, "updated_parsed", None)),
                    body=body[: config.max_body_chars],
                )
            )
        except ValueError as exc:
            log.warning("skipping malformed entry from %s: %s", source.id, exc)

    log.info("source %s yielded %d entries", source.id, len(articles))
    return articles
