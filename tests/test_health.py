"""The source check: what counts as a source that stopped working."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from test_rss import FakeClient  # sibling test module; tests/ is not a package

from squelch.core.config import Config, Source
from squelch.core.models import RawArticle
from squelch.core.settings import Settings
from squelch.scrapers import health
from squelch.scrapers.health import PROBE_ITEMS, SourceHealth, check_sources, probe


def article(**overrides: object) -> RawArticle:
    values: dict[str, object] = {
        "title": "A release",
        "url": "https://example.com/post",
        "source": "acme",
    }
    values.update(overrides)
    return RawArticle(**values)  # type: ignore[arg-type]


@pytest.fixture
def source(make_source: Callable[..., Source]) -> Source:
    return make_source("acme", max_items=10)


# -- what one probe reports --------------------------------------------------


def test_a_source_that_still_produces_articles_is_healthy(
    source: Source, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        health, "scraper_for", lambda _: lambda *_a: [article(image="https://cdn/x.png")]
    )

    result = probe(source, config, FakeClient())

    assert result.ok
    assert (result.articles, result.imaged, result.dated) == (1, 1, 0)


def test_a_source_that_produces_nothing_is_broken(
    source: Source, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is the whole point of the check: during a scrape an empty source is
    # ordinary, here it is the failure we are looking for.
    monkeypatch.setattr(health, "scraper_for", lambda _: lambda *_a: [])

    assert not probe(source, config, FakeClient()).ok


def test_a_crashing_source_is_reported_rather_than_ending_the_check(
    source: Source, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object) -> list[RawArticle]:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(health, "scraper_for", lambda _: explode)

    result = probe(source, config, FakeClient())

    assert not result.ok
    assert "ConnectError" in result.error and "no route to host" in result.error


def test_a_source_with_no_scraper_for_its_type_is_broken(
    source: Source, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "scraper_for", lambda _: None)

    assert "unknown type" in probe(source, config, FakeClient()).error


def test_the_probe_asks_for_a_couple_of_items_not_the_whole_listing(
    source: Source, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Checking twenty sources should cost a handful of requests each, and the
    # cap must not leak back into the catalogue the real scrape reads.
    asked: list[int] = []
    monkeypatch.setattr(
        health, "scraper_for", lambda _: lambda s, *_a: asked.append(s.max_items) or []
    )

    probe(source, config, FakeClient())

    assert asked == [PROBE_ITEMS]
    assert source.max_items == 10


# -- the sweep ---------------------------------------------------------------


def test_disabled_sources_are_not_checked(
    config: Config, make_source: Callable[..., Source], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A source switched off on purpose must not be able to redden the run.
    monkeypatch.setattr(health, "scraper_for", lambda _: lambda *_a: [])
    catalogue = config.model_copy(
        update={"sources": [make_source("live"), make_source("parked", enabled=False)]}
    )

    results = check_sources(catalogue, Settings(_env_file=None))

    assert [result.source for result in results] == ["live"]


def test_the_type_filter_narrows_the_sweep(
    config: Config, make_source: Callable[..., Source], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "scraper_for", lambda _: lambda *_a: [article()])
    catalogue = config.model_copy(
        update={
            "sources": [
                make_source("feed"),
                make_source("listing", type="html", link_selector="a"),
            ]
        }
    )

    results = check_sources(catalogue, Settings(_env_file=None), only_type="html")

    assert [result.source for result in results] == ["listing"]


def test_a_single_source_can_be_checked_on_its_own(
    config: Config, make_source: Callable[..., Source], monkeypatch: pytest.MonkeyPatch
) -> None:
    # How you iterate on a selector without hitting eighteen other sites.
    monkeypatch.setattr(health, "scraper_for", lambda _: lambda *_a: [article()])
    catalogue = config.model_copy(
        update={"sources": [make_source("one"), make_source("two"), make_source("three")]}
    )

    results = check_sources(catalogue, Settings(_env_file=None), only_source="two")

    assert [result.source for result in results] == ["two"]


def test_the_report_names_every_broken_source(caplog: pytest.LogCaptureFixture) -> None:
    results = [
        SourceHealth("good", "rss", articles=2, dated=2, imaged=2),
        SourceHealth("empty", "html"),
        SourceHealth("dead", "rss", error="ConnectError: refused"),
    ]

    with caplog.at_level("INFO"):
        health.report(results)

    logged = caplog.text
    assert "BROKEN" in logged
    assert "ConnectError: refused" in logged
    # Undated and imageless articles are counted, never judged — a page that
    # states no date is normal, and failing on it would make the check noise.
    assert "good" in logged and "ok" in logged
