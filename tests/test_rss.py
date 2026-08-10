"""Feed parsing, offline: fixtures in, RawArticle out."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from squelch.core.config import Config, Source
from squelch.scrapers import rss
from squelch.scrapers.extract import Extracted, strip_html


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.text = content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    """Stands in for httpx.Client: serves canned bytes, records the calls."""

    def __init__(self, content: bytes = b"", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.requested: list[str] = []

    def get(self, url: str) -> FakeResponse:
        self.requested.append(url)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.content)


@pytest.fixture
def rss_client(read_fixture: Callable[[str], bytes]) -> FakeClient:
    return FakeClient(read_fixture("sample_rss.xml"))


@pytest.fixture
def atom_client(read_fixture: Callable[[str], bytes]) -> FakeClient:
    return FakeClient(read_fixture("sample_atom.xml"))


# -- RSS --------------------------------------------------------------------


def test_rss_entries_map_onto_raw_articles(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    source = make_source("acme")

    articles = rss.scrape(source, config, rss_client)

    assert [a.source for a in articles] == ["acme", "acme", "acme"]
    assert articles[0].url == "https://example.com/articles/first"
    assert articles[1].url == "https://example.com/articles/second"
    assert articles[2].url == "https://example.com/articles/fifth"
    assert rss_client.requested == [source.url]


def test_entry_titles_are_whitespace_collapsed(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    articles = rss.scrape(make_source(), config, rss_client)

    assert articles[0].title == "Whitespace in the headline"


def test_an_over_long_title_is_capped_at_250_characters(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    # GitHub rejects issue titles over 256 chars, so the model trims first.
    articles = rss.scrape(make_source(), config, rss_client)

    assert len(articles[2].title) == 250
    assert articles[2].title.startswith("Very long headline that keeps going")


def test_publication_dates_are_converted_to_utc(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    articles = rss.scrape(make_source(), config, rss_client)

    # The feed says 12:30 +0300.
    assert articles[0].published_at == datetime(2025, 1, 6, 9, 30, tzinfo=UTC)
    assert articles[0].published_at.tzinfo is not None
    assert articles[1].published_at == datetime(2025, 1, 7, 8, 0, tzinfo=UTC)


def test_an_entry_without_a_date_keeps_published_at_none(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    articles = rss.scrape(make_source(), config, rss_client)

    assert articles[2].published_at is None


def test_entries_without_a_link_or_title_are_skipped(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    articles = rss.scrape(make_source(), config, rss_client)

    urls = [a.url for a in articles]
    assert len(articles) == 3
    assert "https://example.com/articles/fourth" not in urls
    assert not any("nothing to open" in a.title.lower() for a in articles)


def test_tracking_params_are_gone_by_the_time_the_article_exists(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    articles = rss.scrape(make_source(), config, rss_client)

    assert "utm_" not in articles[0].url
    assert articles[0].url.startswith("https://example.com/")  # www stripped too
    assert len(articles[0].uid) == 16


def test_the_feed_summary_becomes_plain_text(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    articles = rss.scrape(make_source(), config, rss_client)

    body = articles[0].body
    assert "<b>" not in body
    assert "Teaser with" in body
    assert "markup" in body
    assert "AT&T" in body


def test_a_content_block_wins_over_the_short_description(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    articles = rss.scrape(make_source(), config, rss_client)

    assert "Much richer body text" in articles[1].body
    assert "Short teaser only" not in articles[1].body


def test_max_items_caps_the_entries_taken_from_one_source(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    articles = rss.scrape(make_source(max_items=2), config, rss_client)

    assert len(articles) == 2
    assert articles[1].url == "https://example.com/articles/second"


def test_max_items_is_applied_before_entries_are_filtered(
    rss_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    # Items 3 and 4 of the fixture are unusable, so a cap of 4 yields only 2
    # articles. The cap bounds work, not output.
    articles = rss.scrape(make_source(max_items=4), config, rss_client)

    assert len(articles) == 2


def test_the_body_is_truncated_to_max_body_chars(
    rss_client: FakeClient, make_source: Callable[..., Source]
) -> None:
    tiny = Config(focus="f", topics=[], max_body_chars=10, sources=[])

    articles = rss.scrape(make_source(), tiny, rss_client)

    assert all(len(a.body) <= 10 for a in articles)


# -- Atom -------------------------------------------------------------------


def test_atom_entries_map_onto_raw_articles(
    atom_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    articles = rss.scrape(make_source("atomic"), config, atom_client)

    assert len(articles) == 1
    article = articles[0]
    assert article.title == "Atom entry with an updated timestamp"
    assert article.source == "atomic"
    # Trailing slash trimmed, utm_campaign dropped.
    assert article.url == "https://atom.example.org/posts/one"
    assert "Atom summary body." in article.body


def test_atom_falls_back_from_published_to_updated(
    atom_client: FakeClient, config: Config, make_source: Callable[..., Source]
) -> None:
    articles = rss.scrape(make_source(), config, atom_client)

    assert articles[0].published_at == datetime(2025, 2, 11, 9, 30, tzinfo=UTC)


# -- full-text follow-up ----------------------------------------------------


def test_full_text_replaces_the_feed_summary_when_it_is_richer(
    rss_client: FakeClient,
    config: Config,
    make_source: Callable[..., Source],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        rss, "fetch_article", lambda client, url: Extracted("Full article text. " * 20)
    )

    articles = rss.scrape(make_source(fetch_full_text=True), config, rss_client)

    assert articles[0].body.startswith("Full article text.")


def test_the_feed_summary_is_kept_when_full_text_extraction_comes_back_thin(
    rss_client: FakeClient,
    config: Config,
    make_source: Callable[..., Source],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Paywalls and consent walls routinely yield a few useless words.
    monkeypatch.setattr(rss, "fetch_article", lambda client, url: Extracted("Subscribe"))

    articles = rss.scrape(make_source(fetch_full_text=True), config, rss_client)

    assert "Teaser with" in articles[0].body


# -- failure modes ----------------------------------------------------------


def test_an_unreachable_source_yields_nothing_instead_of_raising(
    config: Config, make_source: Callable[..., Source]
) -> None:
    client = FakeClient(error=httpx.ConnectError("no route to host"))

    assert rss.scrape(make_source(), config, client) == []


def test_an_unparseable_response_yields_nothing(
    config: Config, make_source: Callable[..., Source]
) -> None:
    client = FakeClient(b"\x00\x01 this is not a feed at all")

    assert rss.scrape(make_source(), config, client) == []


# -- strip_html -------------------------------------------------------------


def test_strip_html_removes_tags_and_collapses_whitespace() -> None:
    fragment = "<p>First   line.</p>\n<p>Second <em>line</em>.</p>"

    assert strip_html(fragment) == "First line. Second line ."


def test_strip_html_unescapes_entities() -> None:
    assert strip_html("Tom &amp; Jerry &mdash; &quot;quoted&quot;") == 'Tom & Jerry — "quoted"'


def test_strip_html_handles_empty_input() -> None:
    assert strip_html("") == ""
    assert strip_html(None) == ""  # type: ignore[arg-type]
