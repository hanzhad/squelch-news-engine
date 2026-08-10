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
from .extract import fetch_article, strip_html

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


def _entry_image(entry: object) -> str:
    """A picture the feed attached to this entry, if it bothered to.

    Feeds advertise images in three different places depending on which
    generator wrote them, and none of them is reliably present.
    """
    for item in getattr(entry, "media_content", None) or []:
        url = item.get("url") if isinstance(item, dict) else None
        if url:
            return str(url)
    for item in getattr(entry, "media_thumbnail", None) or []:
        url = item.get("url") if isinstance(item, dict) else None
        if url:
            return str(url)
    for link in getattr(entry, "links", None) or []:
        if not isinstance(link, dict) or link.get("rel") != "enclosure":
            continue
        if str(link.get("type", "")).startswith("image/") and link.get("href"):
            return str(link["href"])
    return ""


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
        image = _entry_image(entry)
        if source.fetch_full_text:
            page = fetch_article(client, link)
            # Feeds often carry a one-line teaser; prefer whichever is richer.
            if len(page.text) > len(body):
                body = page.text
            # The feed's own media wins: a publisher who bothered to attach one
            # picked it for this entry, while og:image is often a site-wide banner.
            image = image or page.image

        try:
            articles.append(
                RawArticle(
                    title=title,
                    url=link,
                    source=source.id,
                    published_at=_to_datetime(getattr(entry, "published_parsed", None))
                    or _to_datetime(getattr(entry, "updated_parsed", None)),
                    body=body[: config.max_body_chars],
                    image=image,
                )
            )
        except ValueError as exc:
            log.warning("skipping malformed entry from %s: %s", source.id, exc)

    log.info("source %s yielded %d entries", source.id, len(articles))
    return articles
