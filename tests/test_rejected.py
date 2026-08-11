"""The rejected channel and the community rescue that answers it.

Rejections are already closed, so the channel that shows them consumes
``status:rejected`` and must never take part in closing an article. The way
back is a 👍 vote on the issue: the rescue pass reopens it as relevant — a
human override, not a second opinion from the classifier that already said no.
"""

from __future__ import annotations

from typing import Any

import pytest

from squelch.core.config import Channel, Config
from squelch.core.models import Classification, Status
from squelch.core.settings import Settings
from squelch.github.issues import SENT_PREFIX, IssueStore, parse_issue, render_body
from squelch.github.rescue import run_rescue
from squelch.publishers.discord import (
    REJECTED_CHANNEL,
    DiscordError,
    _rejected_embed,
    publish_rejected,
)


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    """Just enough GitHub to run IssueStore against, with a call log."""

    repo = "acme/feed"

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = {issue["number"]: issue for issue in issues}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        wanted = (params or {}).get("labels")
        return [
            issue
            for issue in self.issues.values()
            if wanted is None or wanted in [label["name"] for label in issue.get("labels", [])]
        ]

    def request(self, method: str, path: str, json: dict[str, Any] | None = None) -> FakeResponse:
        number = int(path.rsplit("/", 1)[-1])
        fields = json or {}
        self.calls.append((f"{method} #{number}", fields))
        issue = self.issues[number]
        if "labels" in fields:
            issue["labels"] = [{"name": name} for name in fields["labels"]]
        for key in ("body", "state", "state_reason"):
            if key in fields:
                issue[key] = fields[key]
        return FakeResponse(issue)


REASON = "Repackaged press release with no new facts"


def make_rejected(
    number: int,
    *,
    labels: list[str] | None = None,
    reactions: dict[str, int] | None = None,
) -> dict[str, Any]:
    meta = {
        "uid": f"uid{number}",
        "url": f"https://example.com/{number}",
        "source": "lwn",
        "verdict_reason": REASON,
        "score": 2,
        "tags": ["models"],
    }
    verdict = Classification(relevant=False, reason=REASON, tags=["models"], score=2)
    return {
        "number": number,
        "title": f"Article {number}",
        "labels": [{"name": name} for name in (labels or [Status.REJECTED.value])],
        "state": "closed",
        "html_url": f"https://github.com/acme/feed/issues/{number}",
        "body": render_body(meta, "Original text.", verdict=verdict),
        "reactions": reactions or {},
    }


def make_settings(**overrides: object) -> Settings:
    # _env_file=None so a developer's real .env cannot reach the suite.
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def rejected_config(*, enabled: bool = True) -> Config:
    return Config(
        focus="f",
        channels=[
            Channel(id="site"),
            Channel(id="discord"),
            Channel(id=REJECTED_CHANNEL, enabled=enabled, consumes="rejected"),
        ],
    )


# -- the channel is a window, never a gate ------------------------------------


def test_a_rejected_channel_is_never_required_for_closing() -> None:
    # Ordinary articles never get its sent label, so counting it would leave
    # every ready article open forever.
    assert rejected_config().required_channels == ["site", "discord"]


def test_the_rejected_queue_sees_only_undelivered_rejections() -> None:
    store = IssueStore(
        FakeClient(
            [
                make_rejected(1),
                make_rejected(
                    2, labels=[Status.REJECTED.value, f"{SENT_PREFIX}{REJECTED_CHANNEL}"]
                ),
            ]
        )
    )

    pending = store.list_pending(REJECTED_CHANNEL, status=Status.REJECTED)

    assert [issue.number for issue in pending] == [1]


def test_recording_a_delivery_keeps_the_rejected_heading() -> None:
    # record_delivery re-renders the whole body; the verdict must not flip to
    # "Kept" just because the bookkeeping was written by a delivery pass.
    store = IssueStore(FakeClient([make_rejected(1)]))
    issue = store.list_pending(REJECTED_CHANNEL, status=Status.REJECTED)[0]

    store.record_delivery(issue, REJECTED_CHANNEL, {"message_id": "42"})

    body = store.client.issues[1]["body"]
    assert "**Rejected:**" in body
    assert "**Kept:**" not in body


def test_publish_rejected_refuses_the_feed_webhook_fallback() -> None:
    # _Webhook falls back to DISCORD_WEBHOOK_URL when given an empty url, and
    # rejects landing in the main feed is the one thing this must never do.
    settings = make_settings(discord_webhook_url="https://discord.com/api/webhooks/1/feed")

    with pytest.raises(DiscordError, match="DISCORD_REJECTED_WEBHOOK_URL"):
        publish_rejected(settings, rejected_config(), IssueStore(FakeClient([])))


def test_a_disabled_rejected_channel_is_a_quiet_no_op() -> None:
    # Switching the section off in config must not require deleting a secret
    # or a workflow, and must not paint the cron red.
    store = IssueStore(FakeClient([]))

    assert publish_rejected(make_settings(), rejected_config(enabled=False), store) == 0


# -- what a rejected post looks like ------------------------------------------


def test_the_embed_carries_the_reason_and_the_appeal() -> None:
    issue = parse_issue(make_rejected(1))

    embed = _rejected_embed(issue)

    assert REASON in embed["description"]
    assert "https://github.com/acme/feed/issues/1" in embed["description"]
    assert embed["url"] == "https://example.com/1"
    assert embed["author"]["name"] == "lwn"


def test_the_embed_stays_the_size_of_a_brief() -> None:
    payload = make_rejected(1)
    payload["body"] = render_body(
        {**parse_issue(payload).meta, "image": "https://example.com/pic.png"},
        "Original text.",
        verdict=Classification(relevant=False, reason=REASON),
    )
    embed = _rejected_embed(parse_issue(payload))

    assert "image" not in embed
    assert "thumbnail" not in embed


# -- the way back -------------------------------------------------------------


def test_enough_reactions_send_an_article_back() -> None:
    store = IssueStore(FakeClient([make_rejected(1, reactions={"+1": 2})]))

    assert run_rescue(make_settings(rescue_min_reactions=2), store) == 1

    issue = store.client.issues[1]
    names = {label["name"] for label in issue["labels"]}
    assert Status.RELEVANT.value in names
    assert Status.REJECTED.value not in names
    # The classifier's tags become labels now — the rejection path never
    # applied them.
    assert "topic:models" in names
    assert issue["state"] == "open"
    assert "Rescued by community vote" in issue["body"]


def test_the_original_reason_survives_the_rescue() -> None:
    store = IssueStore(FakeClient([make_rejected(1, reactions={"+1": 1})]))

    run_rescue(make_settings(), store)

    reread = parse_issue(store.client.issues[1])
    assert reread.meta["rejected_reason"] == REASON


def test_too_few_reactions_leave_it_closed() -> None:
    store = IssueStore(FakeClient([make_rejected(1, reactions={"+1": 1})]))

    assert run_rescue(make_settings(rescue_min_reactions=3), store) == 0
    assert store.client.issues[1]["state"] == "closed"
    assert store.client.calls == []


def test_the_vote_is_read_from_the_listing_rollup() -> None:
    # One GET for the whole queue, nothing per issue — that is the budget this
    # feature fits in.
    assert parse_issue(make_rejected(1, reactions={"+1": 3, "eyes": 9})).approvals == 3
    assert parse_issue(make_rejected(1)).approvals == 0
