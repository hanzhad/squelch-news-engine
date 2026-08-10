"""Turning a page or a feed entry into plain article text."""

from __future__ import annotations

import html
import re
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

from ..core.log import get_logger

log = get_logger(__name__)


class Extracted(NamedTuple):
    """What one page yields: its article text, and the image it advertises."""

    text: str = ""
    image: str = ""

_TAG_RE = re.compile(r"<[^>]+>")

USER_AGENT = (
    "Mozilla/5.0 (compatible; Squelch/0.1; +https://github.com/hanzhad/squelch-news-engine)"
)


def new_http_client(timeout: float) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
    )


def strip_html(fragment: str) -> str:
    """Cheap text extraction for feed summaries, which are usually tiny."""
    text = _TAG_RE.sub(" ", fragment or "")
    return " ".join(html.unescape(text).split())


def extract_from_html(page_html: str, url: str) -> str:
    """Pull the article body out of a full HTML page."""
    if not page_html:
        return ""
    text = trafilatura.extract(
        page_html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    return (text or "").strip()


def social_image(page_html: str, url: str) -> str:
    """The image the page offers for link previews, absolute, or empty.

    This is the picture a publisher chose to represent the article — the same
    one Slack or Twitter would show — so it needs no judgement about which
    image on the page matters. Anything unusable is dropped rather than raised
    on: an article without a picture is still an article.
    """
    if not page_html:
        return ""
    metadata = trafilatura.extract_metadata(page_html, default_url=url)
    candidate = (getattr(metadata, "image", "") or "").strip() if metadata else ""
    if not candidate:
        return ""

    absolute = urljoin(url, candidate)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        log.debug("unusable image %r on %s", candidate, url)
        return ""
    return absolute


def fetch_article(client: httpx.Client, url: str) -> Extracted:
    """Download ``url``, extract its text and its image. Never raises."""
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("could not fetch %s: %s", url, exc)
        return Extracted()

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and content_type:
        log.debug("skipping non-html %s (%s)", url, content_type)
        return Extracted()

    return Extracted(extract_from_html(response.text, url), social_image(response.text, url))
