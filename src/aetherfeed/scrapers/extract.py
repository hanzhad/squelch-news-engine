"""Turning a page or a feed entry into plain article text."""

from __future__ import annotations

import html
import re

import httpx
import trafilatura

from ..core.log import get_logger

log = get_logger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")

USER_AGENT = (
    "Mozilla/5.0 (compatible; Squelch/0.1; +https://github.com/hanzhad/squelch-news-engine)"
)


def new_http_client(timeout: float) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en,ru;q=0.8"},
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


def fetch_full_text(client: httpx.Client, url: str) -> str:
    """Download ``url`` and extract its article text. Never raises."""
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("could not fetch %s: %s", url, exc)
        return ""

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and content_type:
        log.debug("skipping non-html %s (%s)", url, content_type)
        return ""

    return extract_from_html(response.text, url)
