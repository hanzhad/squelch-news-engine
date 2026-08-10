"""Community submissions arrive without a metadata block and must survive it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aetherfeed.core.models import RawArticle, Status
from aetherfeed.github.issues import meta_from_form, parse_issue

FORM_BODY = """### Link

https://example.com/story/?utm_source=newsletter

### Why it matters

First public benchmark numbers, and they contradict the vendor's claims.

### Source

Ars Technica"""


def form_payload(body: str) -> dict[str, object]:
    return {
        "number": 42,
        "title": "[suggestion] benchmark numbers",
        "labels": [{"name": Status.RAW.value}],
        "state": "open",
        "body": body,
    }


def test_form_link_is_recovered_and_canonicalized() -> None:
    meta = meta_from_form(FORM_BODY)

    assert meta["url"] == "https://example.com/story"
    assert meta["source"] == "Ars Technica"
    assert meta["uid"]


def test_absent_source_falls_back_to_community() -> None:
    body = FORM_BODY.replace("Ars Technica", "_No response_")

    assert meta_from_form(body)["source"] == "community"


def test_a_suggestion_reaches_the_pipeline_with_a_usable_link() -> None:
    record = parse_issue(form_payload(FORM_BODY))

    # Without the fallback these are all empty and the article would be
    # published as a headline pointing nowhere.
    assert record.url == "https://example.com/story"
    assert record.uid
    assert record.status is Status.RAW
    # The whole form is the text the filter judges, including the rationale.
    assert "contradict the vendor" in record.text


def test_a_scraped_issue_ignores_the_form_fallback() -> None:
    body = "<!-- aetherfeed\nurl: https://real.example/a\nsource: krebs\n-->\n\n### Link\n\nhttps://decoy.example/b"

    record = parse_issue(form_payload(body))

    assert record.url == "https://real.example/a"
    assert record.source == "krebs"


@pytest.mark.parametrize("body", ["No headings at all.", "### Link\n\nnot a url\n", ""])
def test_bodies_without_a_link_yield_no_metadata(body: str) -> None:
    assert meta_from_form(body) == {}


# -- URL sanity at the model boundary ---------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # feedparser substitutes an entry's <id> when it carries no <link>.
        "urn:uuid:entry-two",
        "tag:example.com,2025:post-1",
        "mailto:editor@example.com",
        "javascript:alert(1)",
        "/relative/path",
    ],
)
def test_unfetchable_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        RawArticle(title="Some title", url=url, source="test")


def test_ordinary_article_urls_are_accepted() -> None:
    article = RawArticle(title="Some title", url="http://example.com/a", source="test")

    assert article.url == "http://example.com/a"
