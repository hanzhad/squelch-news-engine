"""Sources that have no feed, rendered with Playwright.

Deliberately generic: a listing page plus a CSS selector for the article links.
Anything beyond that (pagination, logins, infinite scroll) belongs in a
purpose-built scraper rather than in this fallback.

Playwright is an optional dependency — install with ``pip install -e '.[web]'``
followed by ``playwright install chromium``.
"""

from __future__ import annotations

from urllib.parse import urljoin

from ..core.config import Config, Source
from ..core.log import get_logger
from ..core.models import RawArticle
from .extract import USER_AGENT, extract_from_html, social_image

log = get_logger(__name__)

NAV_TIMEOUT_MS = 30_000


def _title_from(page_html: str, url: str, fallback: str) -> str:
    try:
        import trafilatura

        metadata = trafilatura.extract_metadata(page_html, default_url=url)
        if metadata and metadata.title:
            return metadata.title
    except Exception:  # noqa: BLE001 - metadata extraction is best-effort
        pass
    return fallback


def scrape(source: Source, config: Config, client: object = None) -> list[RawArticle]:
    if not source.link_selector:
        log.error("source %s is type 'web' but has no link_selector", source.id)
        return []

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error(
            "source %s needs Playwright: pip install -e '.[web]' && playwright install chromium",
            source.id,
        )
        return []

    articles: list[RawArticle] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        try:
            page.goto(source.url, wait_until="domcontentloaded")
            page.wait_for_selector(source.link_selector, timeout=NAV_TIMEOUT_MS)
            hrefs = page.eval_on_selector_all(
                source.link_selector,
                "nodes => nodes.map(n => [n.getAttribute('href'), n.textContent.trim()])",
            )
        except PlaywrightError as exc:
            log.error("source %s listing failed: %s", source.id, exc)
            browser.close()
            return []

        seen: set[str] = set()
        targets: list[tuple[str, str]] = []
        for href, text in hrefs:
            if not href:
                continue
            absolute = urljoin(source.url, href)
            if absolute in seen:
                continue
            seen.add(absolute)
            targets.append((absolute, text or absolute))
            if len(targets) >= source.max_items:
                break

        for url, link_text in targets:
            try:
                page.goto(url, wait_until="domcontentloaded")
                page_html = page.content()
            except PlaywrightError as exc:
                log.warning("could not render %s: %s", url, exc)
                continue

            body = extract_from_html(page_html, url)
            if not body:
                log.debug("no extractable text at %s", url)
                continue

            try:
                articles.append(
                    RawArticle(
                        title=_title_from(page_html, url, link_text),
                        url=url,
                        source=source.id,
                        body=body[: config.max_body_chars],
                        image=social_image(page_html, url),
                    )
                )
            except ValueError as exc:
                log.warning("skipping %s: %s", url, exc)

        browser.close()

    log.info("source %s yielded %d entries", source.id, len(articles))
    return articles
