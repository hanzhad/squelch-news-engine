"""What counts as news, and what a date has to look like to be believed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from squelch.core.models import RawArticle
from squelch.scrapers.runner import is_current


def article(**overrides: object) -> RawArticle:
    values: dict[str, object] = {
        "title": "A release",
        "url": "https://example.com/post",
        "source": "acme",
    }
    values.update(overrides)
    return RawArticle(**values)  # type: ignore[arg-type]


# -- dates we refuse to believe ---------------------------------------------


def test_a_date_from_the_future_is_dropped_but_the_article_is_kept() -> None:
    # A feed in the wrong timezone should cost us a date, never a story.
    far_ahead = datetime.now(UTC) + timedelta(days=400)

    assert article(published_at=far_ahead).published_at is None
    assert article(published_at=far_ahead).url == "https://example.com/post"


def test_a_few_hours_ahead_is_clock_skew_and_survives() -> None:
    soon = datetime.now(UTC) + timedelta(hours=6)

    assert article(published_at=soon).published_at == soon


def test_a_naive_date_is_read_as_utc_rather_than_blowing_up_the_comparison() -> None:
    naive = datetime(2026, 7, 23, 10, 0)

    assert article(published_at=naive).published_at == datetime(2026, 7, 23, 10, 0, tzinfo=UTC)


# -- how far back news reaches ----------------------------------------------


@pytest.mark.parametrize("age_days", [0, 1, 20])
def test_a_recent_article_is_news(age_days: int) -> None:
    recent = article(published_at=datetime.now(UTC) - timedelta(days=age_days))

    assert is_current(recent, max_age_days=21)


def test_an_article_from_the_back_catalogue_is_not() -> None:
    # Listing pages keep months of posts. Without this, enabling a source would
    # publish its whole archive on day one.
    old = article(published_at=datetime.now(UTC) - timedelta(days=90))

    assert not is_current(old, max_age_days=21)


def test_an_undated_article_is_let_through() -> None:
    # Unknown is not old. The pages that state no date would otherwise be
    # silenced entirely, which is the opposite of what the filter is for.
    assert is_current(article(), max_age_days=21)
