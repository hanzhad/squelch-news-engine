"""Posting into a Discord forum, where every message has to name its thread."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from test_routing import FEED, SKILLS, FakeClient, FakeWebhook, make_issue

from squelch.core.config import Channel, Config
from squelch.core.models import Status
from squelch.core.settings import Settings
from squelch.github.issues import IssueStore
from squelch.publishers import discord
from squelch.publishers.discord import THREAD_NAME_LIMIT, publish_ready

# The digest's own forum behaviour lives in test_digests.py, with the rest of
# the queue it now travels through.
TAGS = {"topic:models": "111", "topic:tooling": "222", "topic:security": "333"}


def forum_config(tags: dict[str, str] | None = None) -> Config:
    return Config(
        focus="f",
        channels=[
            Channel(id="discord", skip=["topic:claude-skills"]),
            Channel(
                id="discord-skills",
                only=["topic:claude-skills"],
                forum=True,
                tags=tags or {},
            ),
        ],
    )


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "discord_webhook_url": FEED,
        "discord_skills_webhook_url": SKILLS,
        "publish_delay_seconds": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def fake_webhook(monkeypatch: pytest.MonkeyPatch) -> type[FakeWebhook]:
    FakeWebhook.sent = []
    FakeWebhook.threads = []
    monkeypatch.setattr(discord, "_Webhook", FakeWebhook)
    return FakeWebhook


def sent_to(url: str) -> list[dict[str, Any]]:
    return [payload for target, payload in FakeWebhook.sent if target == url]


# -- articles ----------------------------------------------------------------


def test_an_article_posted_to_a_forum_names_its_thread_after_the_headline() -> None:
    store = IssueStore(
        FakeClient([make_issue(2, labels=[Status.READY.value, "topic:claude-skills"])])
    )

    publish_ready(make_settings(), forum_config(), store)

    assert sent_to(SKILLS)[0]["thread_name"] == "Article 2"


def test_a_text_channel_is_never_sent_a_thread_name() -> None:
    # Discord rejects the field outright there, so the same payload builder
    # must not hand it over on the strength of the article having a title.
    store = IssueStore(FakeClient([make_issue(1, labels=[Status.READY.value])]))

    publish_ready(make_settings(), forum_config(), store)

    assert "thread_name" not in sent_to(FEED)[0]


def test_a_long_headline_is_cut_to_what_a_thread_name_allows() -> None:
    # Thread names are far shorter than titles, and an over-long one is a 400.
    long_title = "Anthropic " * 30
    issue = make_issue(3, labels=[Status.READY.value, "topic:claude-skills"])
    issue["title"] = long_title
    store = IssueStore(FakeClient([issue]))

    publish_ready(make_settings(), forum_config(), store)

    assert len(sent_to(SKILLS)[0]["thread_name"]) <= THREAD_NAME_LIMIT


# -- tags --------------------------------------------------------------------


def skills_issue(*labels: str) -> dict[str, Any]:
    return make_issue(9, labels=[Status.READY.value, "topic:claude-skills", *labels])


def test_topic_labels_become_the_post_s_tags() -> None:
    store = IssueStore(FakeClient([skills_issue("topic:models", "topic:security")]))

    publish_ready(make_settings(), forum_config(TAGS), store)

    assert sent_to(SKILLS)[0]["applied_tags"] == ["111", "333"]


def test_a_label_with_no_tag_of_its_own_is_simply_not_applied() -> None:
    # The mapping is written by hand and the topic list grows without it; an
    # unmapped label must cost nothing, not a 400.
    store = IssueStore(FakeClient([skills_issue("topic:hardware")]))

    publish_ready(make_settings(), forum_config(TAGS), store)

    assert "applied_tags" not in sent_to(SKILLS)[0]


def test_a_channel_with_no_tags_configured_sends_none() -> None:
    store = IssueStore(FakeClient([skills_issue("topic:models")]))

    publish_ready(make_settings(), forum_config(), store)

    assert "applied_tags" not in sent_to(SKILLS)[0]


def test_more_labels_than_discord_allows_are_cut_in_config_order() -> None:
    # Five is Discord's cap, and config order decides who survives it, so the
    # choice belongs to whoever wrote the mapping rather than to a set.
    many = {f"topic:t{i}": str(i) for i in range(8)}
    store = IssueStore(FakeClient([skills_issue(*many)]))

    publish_ready(make_settings(), forum_config(many), store)

    assert sent_to(SKILLS)[0]["applied_tags"] == ["0", "1", "2", "3", "4"]


def test_two_labels_pointing_at_one_tag_apply_it_once() -> None:
    # Both the topic and the source route an article here, and Discord rejects
    # a repeated tag id outright.
    doubled = {"topic:claude-skills": "777", "source:claude-skills": "777"}
    store = IssueStore(FakeClient([skills_issue("source:claude-skills")]))

    publish_ready(make_settings(), forum_config(doubled), store)

    assert sent_to(SKILLS)[0]["applied_tags"] == ["777"]


def test_a_tag_name_pasted_instead_of_an_id_is_refused_at_load() -> None:
    # The easy mistake, and Discord's complaint about it would arrive days
    # later in a log nobody reads.
    with pytest.raises(ValidationError, match="numeric Discord ids"):
        Channel(id="discord-skills", tags={"topic:models": "models"})


def test_a_text_channel_never_gets_tags_either() -> None:
    store = IssueStore(FakeClient([make_issue(1, labels=[Status.READY.value])]))

    publish_ready(make_settings(), forum_config(TAGS), store)

    assert "applied_tags" not in sent_to(FEED)[0]
