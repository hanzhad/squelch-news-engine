"""Posting into a Discord forum, where every message has to name its thread."""

from __future__ import annotations

from typing import Any

import pytest
from test_routing import FEED, SKILLS, FakeClient, FakeWebhook, make_issue

from squelch.core.config import Channel, Config
from squelch.core.models import Digest, Status
from squelch.core.settings import Settings
from squelch.github.issues import IssueStore
from squelch.publishers import discord
from squelch.publishers.discord import THREAD_NAME_LIMIT, post_digest, publish_ready


def forum_config() -> Config:
    return Config(
        focus="f",
        channels=[
            Channel(id="discord", skip=["topic:claude-skills"]),
            Channel(id="discord-skills", only=["topic:claude-skills"], forum=True),
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


# -- the weekly digest -------------------------------------------------------


def digest(headline: str = "A week of small models") -> Digest:
    return Digest(headline=headline, trends=["Trend one."], highlights=[])


def test_the_digest_opens_a_post_titled_with_the_week_s_headline() -> None:
    post_digest(make_settings(digest_forum=True), digest())

    assert sent_to(FEED)[0]["thread_name"] == "A week of small models"


def test_the_digest_carries_no_thread_name_in_a_text_channel() -> None:
    post_digest(make_settings(), digest())

    assert "thread_name" not in sent_to(FEED)[0]


def test_a_blank_headline_still_gets_the_post_out() -> None:
    # A forum post cannot be nameless, and the headline comes from a model. A
    # dull title beats a 400 that costs the whole week's roundup.
    post_digest(make_settings(digest_forum=True), digest(headline="   "))

    assert sent_to(FEED)[0]["thread_name"] == discord.DIGEST_THREAD_NAME
