"""The article's own picture: finding it, refusing bad ones, showing it."""

from __future__ import annotations

from types import SimpleNamespace

from squelch.core.models import RawArticle
from squelch.github.issues import IssueRecord
from squelch.publishers.discord import _issue_embed
from squelch.scrapers.extract import social_image
from squelch.scrapers.rss import _entry_image

PAGE = """
<html><head>
  <meta property="og:title" content="A release">
  <meta property="og:image" content="{image}">
</head><body><p>Body text.</p></body></html>
"""


# -- finding it --------------------------------------------------------------


def test_the_social_image_is_read_off_the_page() -> None:
    found = social_image(
        PAGE.format(image="https://cdn.example.com/hero.png"), "https://example.com/post"
    )

    assert found == "https://cdn.example.com/hero.png"


def test_a_relative_image_is_resolved_against_the_article() -> None:
    # Plenty of blogs write /static/hero.png and leave the rest to the reader.
    found = social_image(PAGE.format(image="/static/hero.png"), "https://example.com/blog/post")

    assert found == "https://example.com/static/hero.png"


def test_a_page_without_a_picture_yields_nothing_rather_than_failing() -> None:
    assert social_image("<html><head></head><body>hi</body></html>", "https://example.com/x") == ""
    assert social_image("", "https://example.com/x") == ""


def test_an_image_with_an_unusable_scheme_is_dropped() -> None:
    # data: and javascript: URLs are not something to hand to Discord.
    found = social_image(PAGE.format(image="data:image/png;base64,AAAA"), "https://example.com/x")

    assert found == ""


# -- feeds that carry their own ---------------------------------------------


def test_media_content_is_preferred_when_the_feed_supplies_it() -> None:
    entry = SimpleNamespace(media_content=[{"url": "https://example.com/a.png"}])

    assert _entry_image(entry) == "https://example.com/a.png"


def test_a_media_thumbnail_counts_too() -> None:
    entry = SimpleNamespace(media_thumbnail=[{"url": "https://example.com/t.png"}])

    assert _entry_image(entry) == "https://example.com/t.png"


def test_an_image_enclosure_counts_but_a_podcast_does_not() -> None:
    entry = SimpleNamespace(
        links=[
            {"rel": "enclosure", "type": "audio/mpeg", "href": "https://example.com/ep.mp3"},
            {"rel": "enclosure", "type": "image/png", "href": "https://example.com/cover.png"},
        ]
    )

    assert _entry_image(entry) == "https://example.com/cover.png"


def test_a_feed_entry_with_no_media_yields_nothing() -> None:
    assert _entry_image(SimpleNamespace()) == ""


# -- carrying it ------------------------------------------------------------


def test_an_unusable_image_never_costs_us_the_article() -> None:
    article = RawArticle(
        title="A release", url="https://example.com/post", source="acme", image="not-a-url"
    )

    assert article.image == ""
    assert article.url == "https://example.com/post"


# -- showing it -------------------------------------------------------------


def make_record(**meta: object) -> IssueRecord:
    base: dict[str, object] = {"url": "https://example.com/post", "source": "deepmind"}
    base.update(meta)
    return IssueRecord(
        number=7,
        title="A release",
        html_url="https://github.com/acme/feed/issues/7",
        meta=base,
        summary="What happened.",
    )


def test_the_embed_shows_the_picture_when_there_is_one() -> None:
    embed = _issue_embed(make_record(image="https://cdn.example.com/hero.png"))

    assert embed["image"] == {"url": "https://cdn.example.com/hero.png"}


def test_the_embed_omits_the_picture_rather_than_sending_an_empty_one() -> None:
    # Discord rejects an embed carrying an empty image url outright.
    assert "image" not in _issue_embed(make_record())


def test_the_source_is_the_author_line_not_a_word_in_the_footer() -> None:
    embed = _issue_embed(make_record())

    assert embed["author"]["name"] == "deepmind"
    assert "deepmind" not in embed["footer"]["text"]


def test_topics_and_the_discussion_link_share_one_line() -> None:
    embed = _issue_embed(make_record(tags=["models", "policy"], score=8))

    value = embed["fields"][0]["value"]
    assert "`models`" in value and "`policy`" in value
    assert "[discuss #7](https://github.com/acme/feed/issues/7)" in value
    assert len(embed["fields"]) == 1


def test_an_article_with_nothing_to_navigate_to_gets_no_empty_field() -> None:
    record = IssueRecord(number=7, title="A release", meta={"url": "https://example.com/post"})

    assert "fields" not in _issue_embed(record)
