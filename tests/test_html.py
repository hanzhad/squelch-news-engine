"""Link harvesting for server-rendered listing pages."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from squelch.core.config import Config, Source
from squelch.core.models import RawArticle
from squelch.core.settings import Settings
from squelch.scrapers import runner, sites
from squelch.scrapers.html import _collect_links, published_date

LISTING = """
<html><body>
  <nav><a href="/news/">All news</a></nav>
  <main>
    <a href="/news/first-post">First</a>
    <a href="/news/second-post?utm_source=nav">Second</a>
    <a href="https://www.anthropic.com/news/third-post">Third, absolute</a>
    <a href="/news/first-post">First again, in a teaser card</a>
    <a href="/careers/engineer">Not news</a>
    <a>No href at all</a>
  </main>
</body></html>
"""

BASE = "https://www.anthropic.com/news"


def test_links_are_absolutised_and_deduplicated() -> None:
    links = _collect_links(LISTING, BASE, 'a[href^="/news/"]', limit=10)

    # The repeated teaser link appears once; the absolute one is not matched by
    # a prefix selector, and /careers/ is filtered out by the selector itself.
    assert links == [
        "https://www.anthropic.com/news/",
        "https://www.anthropic.com/news/first-post",
        "https://www.anthropic.com/news/second-post?utm_source=nav",
    ]


def test_limit_stops_the_walk_early() -> None:
    links = _collect_links(LISTING, BASE, 'a[href^="/news/"]', limit=2)

    assert len(links) == 2


def test_a_selector_matching_nothing_yields_nothing() -> None:
    assert _collect_links(LISTING, BASE, "article h2 a", limit=10) == []


def test_anchors_without_href_are_skipped() -> None:
    links = _collect_links(LISTING, BASE, "a", limit=10)

    assert all(link.startswith("https://") for link in links)


ARTICLE_URL = "https://example.com/blog/a-post"

DATED_META = """
<html><head><meta property="article:published_time" content="2026-07-23T10:11:12Z"></head>
<body><p>Body.</p></body></html>
"""

DATED_TIME_TAG = """
<html><body><article><time datetime="2026-07-23">23 July</time><p>Body.</p></article></body></html>
"""

# The shape that caused the bug: no machine-readable date anywhere, but a year
# in the footer for a guesser to latch onto.
UNDATED = """
<html><head><title>A post</title></head>
<body><p>A post with no date at all.</p><footer>Copyright 2022 Example Inc.</footer></body></html>
"""


@pytest.mark.parametrize("page", [DATED_META, DATED_TIME_TAG])
def test_a_date_the_page_actually_states_is_read(page: str) -> None:
    assert published_date(page, ARTICLE_URL) == datetime(2026, 7, 23, tzinfo=UTC)


def test_a_page_that_states_no_date_gets_none_rather_than_a_guess() -> None:
    # Guessing put 2026 articles in 2022. Downstream falls back to the day we
    # found the article, which is at least a fact.
    assert published_date(UNDATED, ARTICLE_URL) is None


def test_an_empty_page_is_not_a_crash() -> None:
    assert published_date("", ARTICLE_URL) is None


def test_a_registered_site_scraper_overrides_the_generic_one() -> None:
    """A source id with hand-written logic must win over its generic type."""
    calls: list[str] = []

    @sites.register("pretend-site")
    def _custom(source: Source, config: Config, client: object) -> list[RawArticle]:
        calls.append(source.id)
        return []

    try:
        config = Config(focus="f", sources=[Source(id="pretend-site", url="https://x.test")])
        runner.collect(config, Settings())

        assert calls == ["pretend-site"]
        assert "pretend-site" in sites.registered()
    finally:
        sites._REGISTRY.pop("pretend-site", None)


def test_registering_the_same_source_twice_is_refused() -> None:
    @sites.register("taken")
    def _first(source: Source, config: Config, client: object) -> list[RawArticle]:
        return []

    try:
        with pytest.raises(ValueError, match="taken"):
            sites.register("taken")(_first)
    finally:
        sites._REGISTRY.pop("taken", None)
