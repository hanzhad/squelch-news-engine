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


def urls_of(*args: object, **kwargs: object) -> list[str]:
    return [link.url for link in _collect_links(*args, **kwargs)]  # type: ignore[arg-type]


def test_links_are_absolutised_and_deduplicated() -> None:
    # The repeated teaser link appears once; the absolute one is not matched by
    # a prefix selector, and /careers/ is filtered out by the selector itself.
    assert urls_of(LISTING, BASE, 'a[href^="/news/"]', limit=10) == [
        "https://www.anthropic.com/news/",
        "https://www.anthropic.com/news/first-post",
        "https://www.anthropic.com/news/second-post?utm_source=nav",
    ]


def test_limit_stops_the_walk_early() -> None:
    assert len(urls_of(LISTING, BASE, 'a[href^="/news/"]', limit=2)) == 2


def test_a_selector_matching_nothing_yields_nothing() -> None:
    assert _collect_links(LISTING, BASE, "article h2 a", limit=10) == []


def test_anchors_without_href_are_skipped() -> None:
    assert all(url.startswith("https://") for url in urls_of(LISTING, BASE, "a", limit=10))


# -- the date printed beside the link ----------------------------------------

CARDS = """
<html><body><main>
  <article class="card">
    <a href="/news/newer"><img src="/img/newer.png"></a>
    <span class="kicker">Product</span>
    <a href="/news/newer"><h2>The newer one</h2></a>
    <span class="date">Jul 27, 2026</span>
  </article>
  <article class="card">
    <a href="/news/older"><h2>The older one</h2></a>
    <span class="date">Mar 3, 2026</span>
  </article>
</main></body></html>
"""


def test_each_article_gets_the_date_from_its_own_card() -> None:
    # The regression this whole mechanism exists for: search wider than one
    # card and every article inherits its neighbour's date.
    links = _collect_links(CARDS, BASE, 'a[href^="/news/"]', limit=10)

    assert [(link.url.rsplit("/", 1)[-1], link.published) for link in links] == [
        ("newer", datetime(2026, 7, 27, tzinfo=UTC)),
        ("older", datetime(2026, 3, 3, tzinfo=UTC)),
    ]


def test_an_article_linked_twice_still_finds_the_card_that_prints_the_date() -> None:
    # Meta's lead story is linked from a hero banner showing no date and again
    # from a teaser that does; taking the first link only would lose it.
    hero = """
    <html><body><main>
      <div class="hero"><a href="/news/lead">Lead, no date here</a></div>
      <article><a href="/news/lead">Lead</a><span>Jul 9, 2026</span></article>
      <article><a href="/news/other">Other</a><span>Jun 1, 2026</span></article>
    </main></body></html>
    """

    links = {link.url: link.published for link in _collect_links(hero, BASE, "a", limit=10)}

    assert links["https://www.anthropic.com/news/lead"] == datetime(2026, 7, 9, tzinfo=UTC)


def test_a_card_that_prints_no_date_yields_none() -> None:
    bare = '<html><body><main><article><a href="/news/x">X</a></article></main></body></html>'

    assert _collect_links(bare, BASE, "a", limit=10)[0].published is None


def test_a_date_hidden_in_the_markup_does_not_count_as_printed() -> None:
    # Cards routinely carry dates in image paths and tracking attributes that
    # have nothing to do with the article. Only what a reader can see counts.
    sneaky = """
    <html><body><main>
      <article><a href="/news/x" data-ts="2019-04-04">X</a>
      <img src="/img/2019/04/04/hero.png"></article>
      <article><a href="/news/y">Y</a></article>
    </main></body></html>
    """

    assert _collect_links(sneaky, BASE, "a", limit=10)[0].published is None


def test_inline_css_in_a_card_is_not_mistaken_for_its_text() -> None:
    styled = """
    <html><body><main>
      <article><style>.c{background:url(/i/2011-02-03.png)}</style>
      <a href="/news/x">X</a><span>Jul 9, 2026</span></article>
      <article><a href="/news/y">Y</a></article>
    </main></body></html>
    """

    assert _collect_links(styled, BASE, "a", limit=10)[0].published == datetime(
        2026, 7, 9, tzinfo=UTC
    )


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
