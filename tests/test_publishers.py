"""Where each kind of message goes, and what happens when a channel is absent."""

from __future__ import annotations

import pytest

from squelch.core.settings import Settings
from squelch.publishers.discord import DiscordError, _Webhook, _with_wait

FEED = "https://discord.com/api/webhooks/1/feed"


def make_settings(**overrides: str) -> Settings:
    values: dict[str, str] = {"discord_webhook_url": FEED}
    values.update(overrides)
    # _env_file=None so a developer's real .env cannot reach the suite. These
    # tests are about which webhook is chosen, and a live one has no business
    # being anywhere near that question.
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_no_webhook_at_all_is_an_error_rather_than_a_silent_no_op() -> None:
    with pytest.raises(DiscordError):
        _Webhook(make_settings(), "")


def test_the_webhook_always_asks_discord_for_the_message_id() -> None:
    # Without wait=true the response carries no id, and the id is the only
    # proof an article is already out.
    assert "wait=true" in _with_wait(FEED)


def test_asking_for_the_id_does_not_lose_the_webhook_s_own_query() -> None:
    assert "thread_id=7" in _with_wait(f"{FEED}?thread_id=7")


@pytest.mark.parametrize(
    "url",
    [
        # A secret pasted without its scheme: httpx would take it as a bare
        # host and die deep in the transport, after the digest was written.
        "discord.com/api/webhooks/1/feed",
        "/api/webhooks/1/feed",
        "https://",
        "   ",
    ],
)
def test_a_malformed_webhook_is_rejected_before_any_work_is_done(url: str) -> None:
    with pytest.raises(DiscordError, match="webhook URL"):
        _with_wait(url)
