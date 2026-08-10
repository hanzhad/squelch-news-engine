"""How much room an article gets in the chat channel, and who decides."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from squelch.core.config import Channel, Config, Emphasis
from squelch.github.issues import IssueRecord
from squelch.publishers.discord import Weight, _issue_embed, _weight

DEFAULTS = Emphasis()


def record(**meta: object) -> IssueRecord:
    base: dict[str, object] = {"url": "https://example.com/post", "source": "deepmind"}
    base.update(meta)
    return IssueRecord(
        number=7,
        title="A release",
        html_url="https://github.com/acme/feed/issues/7",
        meta=base,
        summary="What happened, in two sentences.",
    )


# -- picking the tier --------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (10, Weight.LEAD),
        (7, Weight.LEAD),
        (6, Weight.STANDARD),
        (4, Weight.STANDARD),
        (3, Weight.BRIEF),
        (0, Weight.BRIEF),
    ],
)
def test_the_score_decides_the_tier(score: int, expected: Weight) -> None:
    assert _weight(record(score=score), DEFAULTS) == expected


def test_an_article_nobody_scored_lands_in_the_middle() -> None:
    # Opened by hand, never classified. Promoting it would be a guess and
    # hiding it would be a punishment; neither is deserved.
    assert _weight(record(), DEFAULTS) == Weight.STANDARD
    assert _weight(record(score="not a number"), DEFAULTS) == Weight.STANDARD


def test_thresholds_are_config_not_code() -> None:
    everything_shouts = Emphasis(lead=0, standard=0)
    nothing_does = Emphasis(lead=10, standard=10)

    assert _weight(record(score=1), everything_shouts) == Weight.LEAD
    assert _weight(record(score=9), nothing_does) == Weight.BRIEF


def test_a_lead_below_standard_is_refused() -> None:
    # It would make the lead tier unreachable, silently.
    with pytest.raises(ValidationError, match="unreachable"):
        Emphasis(lead=3, standard=7)


def test_a_channel_that_says_nothing_about_emphasis_gets_the_defaults() -> None:
    config = Config(focus="f", channels=[Channel(id="discord")])

    assert config.channel("discord").emphasis == DEFAULTS
    assert config.channel("nobody-configured-this").emphasis == DEFAULTS


# -- what each tier renders --------------------------------------------------


def test_a_brief_gives_up_its_summary_but_never_its_links() -> None:
    embed = _issue_embed(record(score=1, tags=["models"]), DEFAULTS)

    assert "What happened" not in embed["description"]
    assert embed["url"] == "https://example.com/post"
    assert "[discuss #7](https://github.com/acme/feed/issues/7)" in embed["description"]
    assert "`models`" in embed["description"]
    # The topic line moves into the description on purpose: a field would add a
    # blank name row above it, which is most of the height a brief saves.
    assert "fields" not in embed


def test_a_brief_still_says_where_it_came_from_and_what_it_scored() -> None:
    embed = _issue_embed(record(score=2), DEFAULTS)

    assert embed["author"]["name"] == "deepmind"
    assert "score 2/10" in embed["footer"]["text"]


def test_the_bigger_tiers_keep_the_summary_and_the_topic_row() -> None:
    for score in (5, 9):
        embed = _issue_embed(record(score=score, tags=["models"]), DEFAULTS)

        assert embed["description"] == "What happened, in two sentences."
        assert "`models`" in embed["fields"][0]["value"]


def test_each_tier_is_a_different_colour() -> None:
    colours = {_issue_embed(record(score=score), DEFAULTS)["color"] for score in (9, 5, 1)}

    assert len(colours) == 3
