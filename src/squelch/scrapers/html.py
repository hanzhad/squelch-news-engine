"""Listing pages that are server-rendered and therefore need no browser.

Plenty of vendor newsrooms — Anthropic's among them — ship their index as plain
HTML with every article link already in the markup. Driving Playwright at those
costs a chromium download and a minute of CI time per run to learn nothing a
single GET would not have told us, so this scraper handles them instead and
``web.py`` is reserved for pages that genuinely need JavaScript.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, NamedTuple
from urllib.parse import urldefrag, urljoin

import httpx
import lxml.etree
import lxml.html
import trafilatura
from htmldate import find_date

from ..core.config import Config, Source
from ..core.log import get_logger
from ..core.models import RawArticle
from .extract import extract_from_html, social_image

log = get_logger(__name__)

# A teaser card is short — the widest across our real sources holds about 600
# characters. The limit is loose because it guards against one case only: a
# listing that links a single article, where the walk below reaches the document
# root and "the card" would quietly become the whole page again.
CARD_TEXT_LIMIT = 2000


class Link(NamedTuple):
    """One article found on a listing page, with the date its card showed."""

    url: str
    published: datetime | None = None


def _to_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def published_date(page_html: str, url: str) -> datetime | None:
    """The date the page states, or None when it states none.

    ``extensive_search`` is off on purpose. With it on, a page carrying no date
    at all still gets one — htmldate falls back to guessing from whatever
    year-like string it can find in the markup, and ai.meta.com posts published
    this summer came out dated 2022. No date is an honest answer: the article
    then shows the day we found it, which is at least true.
    """
    if not page_html:
        return None
    return _to_date(find_date(page_html, url=url, extensive_search=False, outputformat="%Y-%m-%d"))


def _card_of(element: Any, url: str, reach: dict[Any, set[str]]) -> Any:
    """The largest block around ``element`` that still belongs to it alone.

    Climbing stops at the first ancestor that also contains a link to a
    different article, so the block can never reach into the neighbouring
    teaser. That bound is the whole point: search a whole listing page for a
    date and you get somebody else's.
    """
    card = element
    while (parent := card.getparent()) is not None and reach[parent] == {url}:
        card = parent
    return card


def _card_date(card: Any) -> datetime | None:
    """The date printed on a teaser card, if it prints one.

    Extensive search is safe here — and off the article page it is the only
    thing that works, since a card writes "Jul 27, 2026" as plain text and
    marks it up as nothing at all. What made it dangerous was the size of the
    haystack, and the haystack is now one article wide.
    """
    # Copied because htmldate cleans what it is given, and this card is a live
    # branch of the document we are still walking. Scripts and stylesheets go
    # with it: inline CSS is text to itertext() and noise to a date parser.
    fragment = deepcopy(card)
    lxml.etree.strip_elements(fragment, "script", "style", with_tail=False)
    text = " ".join(chunk.strip() for chunk in fragment.itertext() if chunk.strip())
    if not text or len(text) > CARD_TEXT_LIMIT:
        return None

    # Only what the card *prints*. Handed the card's markup instead, htmldate
    # goes on finding dates that are not there — nebius teasers show no date at
    # all and still produced one, out of an image path or a stray attribute.
    # Narrowing the haystack was not enough; the date now has to be readable.
    printed = lxml.html.Element("body")
    lxml.etree.SubElement(printed, "p").text = text
    return _to_date(
        find_date(printed, extensive_search=True, original_date=True, outputformat="%Y-%m-%d")
    )


def _collect_links(page_html: str, base_url: str, selector: str, limit: int) -> list[Link]:
    """Every article the listing links to, each with the date shown next to it."""
    document = lxml.html.fromstring(page_html)

    # Listings routinely link the same article twice — once from its image, once
    # from its headline — so the anchors are grouped by article rather than
    # deduplicated away. Dropping the fragment (#comments, #community) here
    # saves fetching and extracting the same page a second time.
    anchors: dict[str, list[Any]] = defaultdict(list)
    for element in document.cssselect(selector):
        href = element.get("href")
        if href:
            anchors[urldefrag(urljoin(base_url, href)).url].append(element)

    # Which articles are reachable below each element on the page. Built by
    # walking up from the links rather than down from the root, so the cost is
    # the number of links times the depth of the tree, not the size of the page.
    reach: dict[Any, set[str]] = defaultdict(set)
    for url, elements in anchors.items():
        for element in elements:
            node: Any = element
            while node is not None:
                reach[node].add(url)
                node = node.getparent()

    found: list[Link] = []
    for url, elements in anchors.items():
        # Only one of the two links usually sits in the block that prints the
        # date — Meta's lead story is linked from a hero banner that shows none.
        # So every anchor for this article is tried before giving up on it.
        published = next(
            (date for el in elements if (date := _card_date(_card_of(el, url, reach)))), None
        )
        found.append(Link(url, published))
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
    for url, listed_date in links:
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
                    # The article page first — it is the publisher speaking
                    # about itself. The listing card second, for the sites that
                    # print the date beside the link and nowhere else.
                    published_at=published_date(page.text, url) or listed_date,
                    body=body[: config.max_body_chars],
                    image=social_image(page.text, url),
                )
            )
        except ValueError as exc:
            log.warning("skipping %s: %s", url, exc)

    log.info("source %s yielded %d entries", source.id, len(articles))
    return articles
