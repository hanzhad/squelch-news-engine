"""Link harvesting for server-rendered listing pages."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aetherfeed.scrapers.html import _collect_links, _parse_date

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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-23", datetime(2026, 7, 23, tzinfo=UTC)),
        ("2026-07-23T10:11:12", datetime(2026, 7, 23, tzinfo=UTC)),
        (None, None),
        ("", None),
        ("July 2026", None),
        ("2026-13-45", None),
    ],
)
def test_dates_are_parsed_or_given_up_on(raw: str | None, expected: datetime | None) -> None:
    assert _parse_date(raw) == expected
