"""Publishing fans out over routed Discord channels, each with its own webhook."""

from __future__ import annotations

from typing import Any

import pytest

from squelch.core.config import Channel, Config
from squelch.core.models import Status
from squelch.core.settings import Settings
from squelch.github.issues import IssueStore, render_body
from squelch.publishers import discord
from squelch.publishers.discord import DiscordError, Sent, publish_ready

FEED = "https://discord.com/api/webhooks/1/feed"
SKILLS = "https://discord.com/api/webhooks/2/skills"


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    repo = "acme/feed"

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = {issue["number"]: issue for issue in issues}

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        wanted = (params or {}).get("labels")
        return [
            issue
            for issue in self.issues.values()
            if wanted is None or wanted in [label["name"] for label in issue.get("labels", [])]
        ]

    def request(self, method: str, path: str, json: dict[str, Any] | None = None) -> FakeResponse:
        number = int(path.rsplit("/", 1)[-1])
        issue = self.issues[number]
        fields = json or {}
        if "labels" in fields:
            issue["labels"] = [{"name": name} for name in fields["labels"]]
        for key in ("body", "state", "state_reason"):
            if key in fields:
                issue[key] = fields[key]
        return FakeResponse(issue)


class FakeWebhook:
    """Records what publish_ready would post, and to which webhook."""

    sent: list[tuple[str, dict[str, Any]]] = []
    # Threads a message was posted into, in the same order as `sent`, so a test
    # can tell an opening post from a reply under one.
    threads: list[str] = []

    def __init__(self, settings: Settings, url: str) -> None:
        # No fallback to the feed webhook, exactly like the real one: a fake
        # that quietly filled in a missing URL would hide the bug where a
        # channel posts into somebody else's.
        self.url = url

    def __enter__(self) -> FakeWebhook:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def message_url(self, channel_id: str, message_id: str) -> str:
        # The real one asks the webhook for its guild once; here the shape is
        # all that matters, and "" stands in for a guild we could not read.
        if not (channel_id and message_id):
            return ""
        return f"https://discord.com/channels/1/{channel_id}/{message_id}"

    def send(self, payload: dict[str, Any], thread_id: str = "") -> Sent:
        FakeWebhook.sent.append((self.url, payload))
        FakeWebhook.threads.append(thread_id)
        n = len(FakeWebhook.sent)
        return Sent(f"msg-{n}", thread_id or f"thread-{n}")


def make_issue(number: int, *, labels: list[str]) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"Article {number}",
        "labels": [{"name": name} for name in labels],
        "state": "open",
        "body": render_body({"uid": f"uid{number}", "score": 5}, "Text.", "A summary."),
    }


def routed_config() -> Config:
    return Config(
        focus="f",
        channels=[
            Channel(id="site"),
            Channel(id="rss"),
            Channel(id="discord", skip=["topic:claude-skills"]),
            Channel(id="discord-skills", only=["topic:claude-skills"]),
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


def test_each_article_lands_only_in_the_channel_that_wants_it() -> None:
    store = IssueStore(
        FakeClient(
            [
                make_issue(1, labels=[Status.READY.value]),
                make_issue(2, labels=[Status.READY.value, "topic:claude-skills"]),
            ]
        )
    )

    assert publish_ready(make_settings(), routed_config(), store) == 2

    by_webhook = {url: payload["embeds"][0]["title"] for url, payload in FakeWebhook.sent}
    assert by_webhook == {FEED: "Article 1", SKILLS: "Article 2"}
    labels_1 = {label["name"] for label in store.client.issues[1]["labels"]}
    labels_2 = {label["name"] for label in store.client.issues[2]["labels"]}
    assert "sent:discord" in labels_1 and "sent:discord-skills" not in labels_1
    assert "sent:discord-skills" in labels_2 and "sent:discord" not in labels_2


def test_an_enabled_channel_without_its_webhook_fails_loudly() -> None:
    # Borrowing another channel's webhook is the one thing routing must never
    # do, and a silent skip would look like a quiet day.
    store = IssueStore(FakeClient([]))

    with pytest.raises(DiscordError, match="DISCORD_SKILLS_WEBHOOK_URL"):
        publish_ready(make_settings(discord_skills_webhook_url=""), routed_config(), store)


def test_a_disabled_rubric_channel_takes_its_articles_nowhere() -> None:
    # With the rubric off, its articles are simply not posted by us — the
    # closing pass, not the publisher, decides what that means for the issue.
    config = Config(
        focus="f",
        channels=[
            Channel(id="discord", skip=["topic:claude-skills"]),
            Channel(id="discord-skills", enabled=False, only=["topic:claude-skills"]),
        ],
    )
    store = IssueStore(
        FakeClient([make_issue(2, labels=[Status.READY.value, "topic:claude-skills"])])
    )

    assert publish_ready(make_settings(discord_skills_webhook_url=""), config, store) == 0
    assert FakeWebhook.sent == []
