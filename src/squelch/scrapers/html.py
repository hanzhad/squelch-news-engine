"""Listing pages that are server-rendered and therefore need no browser.

Plenty of vendor newsrooms — Anthropic's among them — ship their index as plain
HTML with every article link already in the markup. Driving Playwright at those
costs a chromium download and a minute of CI time per run to learn nothing a
single GET would not have told us, so this scraper handles them instead and
``web.py`` is reserved for pages that genuinely need JavaScript.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urldefrag, urljoin

import httpx
import lxml.html
import trafilatura
from htmldate import find_date

from ..core.config import Config, Source
from ..core.log import get_logger
from ..core.models import RawArticle
from .extract import extract_from_html, social_image

log = get_logger(__name__)


def published_date(page_html: str, url: str) -> datetime | None:
    """The date the page states, or None when it states none.

    ``extensive_search`` is off on purpose. With it on, a page carrying no date
    at all still gets one — htmldate falls back to guessing from whatever
    year-like string it can find in the markup, and ai.meta.com posts published
    this summer came out dated 2022. No date is an honest answer: the article
    then shows the day we found it, which is at least true.
    """
    raw = find_date(page_html, url=url, extensive_search=False, outputformat="%Y-%m-%d")
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _collect_links(page_html: str, base_url: str, selector: str, limit: int) -> list[str]:
    document = lxml.html.fromstring(page_html)
    found: list[str] = []
    seen: set[str] = set()
    for element in document.cssselect(selector):
        href = element.get("href")
        if not href:
            continue
        # Listings routinely link the same article twice, once with an anchor
        # (#comments, #community). Dropping the fragment here saves fetching
        # and extracting the same page a second time.
        absolute = urldefrag(urljoin(base_url, href)).url
        if absolute in seen:
            continue
        seen.add(absolute)
        found.append(absolute)
        if len(found) >= limit:
            break
    return found


def scrape(source: Source, config: Config, client: httpx.Client) -> list[RawArticle]:
    if not source.link_selector:
        log.error("source %s is type 'html' but has no link_selector", source.id)
        return []

    try:
        response = client.get(source.url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.error("source %s unreachable: %s", source.id, exc)
        return []

    links = _collect_links(response.text, source.url, source.link_selector, source.max_items)
    if not links:
        log.warning("source %s matched no links for %r", source.id, source.link_selector)
        return []

    articles: list[RawArticle] = []
    for url in links:
        try:
            page = client.get(url)
            page.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("could not fetch %s: %s", url, exc)
            continue

        body = extract_from_html(page.text, url)
        metadata = trafilatura.extract_metadata(page.text, default_url=url)
        title = (metadata.title if metadata else "") or ""
        if not body or not title.strip():
            log.debug("nothing extractable at %s", url)
            continue

        try:
            articles.append(
                RawArticle(
                    title=title,
                    url=url,
                    source=source.id,
                    published_at=published_date(page.text, url),
                    body=body[: config.max_body_chars],
                    image=social_image(page.text, url),
                )
            )
        except ValueError as exc:
            log.warning("skipping %s: %s", url, exc)

    log.info("source %s yielded %d entries", source.id, len(articles))
    return articles
