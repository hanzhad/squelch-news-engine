"""Delivery bookkeeping: one label per channel, and who gets to close an issue.

Status is a single line and delivery is not — an article is out on the site and
not yet on Discord all the time — so the two are tracked separately. These tests
pin down that separation and the rule that closes an issue only once every
enabled channel has had it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from squelch.core.config import Channel, Config
from squelch.core.models import Status
from squelch.github.issues import SENT_PREFIX, IssueRecord, IssueStore, parse_issue, render_body
from squelch.github.labels import label_specs
from squelch.site.build import FEED_CHANNEL, FEED_LIMIT, Article, _feed_selection


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


def make_issue(number: int, *, labels: list[str], meta: dict[str, Any] | None = None) -> dict:
    return {
        "number": number,
        "title": f"Article {number}",
        "labels": [{"name": name} for name in labels],
        "state": "open",
        "body": render_body(meta or {"uid": f"uid{number}"}, "Original text.", "A summary."),
    }


@pytest.fixture
def store() -> IssueStore:
    return IssueStore(
        FakeClient(
            [
                make_issue(1, labels=[Status.READY.value]),
                make_issue(2, labels=[Status.READY.value, f"{SENT_PREFIX}discord"]),
            ]
        )
    )


# -- the pending queue -------------------------------------------------------


def test_a_channel_only_sees_articles_it_has_not_delivered(store: IssueStore) -> None:
    assert [issue.number for issue in store.list_pending("discord")] == [1]
    assert [issue.number for issue in store.list_pending("site")] == [1, 2]


def test_published_articles_are_never_offered_to_a_channel_again() -> None:
    # A channel enabled later must start with today's news, not replay the archive.
    store = IssueStore(FakeClient([make_issue(9, labels=[Status.PUBLISHED.value])]))

    assert store.list_pending("telegram") == []


def test_the_pending_queue_respects_the_batch_limit(store: IssueStore) -> None:
    assert len(store.list_pending("site", limit=1)) == 1


# -- recording a delivery ----------------------------------------------------


def test_recording_a_delivery_adds_the_channel_label(store: IssueStore) -> None:
    issue = store.list_pending("site")[0]

    store.record_delivery(issue, "site")

    assert f"{SENT_PREFIX}site" in store.list_pending("discord")[0].labels


def test_the_delivery_record_survives_a_round_trip(store: IssueStore) -> None:
    issue = store.list_pending("discord")[0]

    store.record_delivery(issue, "discord", {"message_id": "42"})

    reread = parse_issue(store.client.issues[1])
    assert reread.delivery("discord")["message_id"] == "42"
    assert reread.delivery("discord")["at"]


def test_the_body_is_written_before_the_label(store: IssueStore) -> None:
    # A run that dies between the two must leave proof the article is already
    # out, so the next one relabels instead of posting it twice.
    issue = store.list_pending("discord")[0]

    store.record_delivery(issue, "discord", {"message_id": "42"})

    fields = [call[1] for call in store.client.calls]
    assert "body" in fields[0] and "labels" not in fields[0]
    assert "labels" in fields[1] and "body" not in fields[1]


def test_a_recorded_delivery_is_visible_on_the_record_it_was_given(store: IssueStore) -> None:
    # Delivering to several channels in a row must not read stale labels back.
    issue = store.list_pending("site")[0]

    store.record_delivery(issue, "site")
    store.record_delivery(issue, "rss")

    assert issue.delivered_to == {"site", "rss"}


def test_recording_keeps_the_summary_and_the_original_text(store: IssueStore) -> None:
    issue = store.list_pending("site")[0]

    store.record_delivery(issue, "site")

    reread = parse_issue(store.client.issues[1])
    assert reread.summary == "A summary."
    assert reread.original == "Original text."


# -- closing -----------------------------------------------------------------


def test_an_article_closes_once_every_required_channel_has_it(store: IssueStore) -> None:
    issue = store.list_pending("site")[0]
    for channel in ("site", "rss", "discord"):
        store.record_delivery(issue, channel)

    assert store.close_delivered(["site", "rss", "discord"]) == [1]
    closed = store.client.issues[1]
    assert closed["state"] == "closed"
    assert closed["state_reason"] == "completed"
    assert Status.PUBLISHED.value in [label["name"] for label in closed["labels"]]


def test_an_article_stays_open_while_a_channel_still_owes_it(store: IssueStore) -> None:
    assert store.close_delivered(["site", "rss", "discord"]) == []
    assert store.client.issues[2]["state"] == "open"


def test_disabling_a_channel_releases_what_was_waiting_on_it(store: IssueStore) -> None:
    # Issue 2 has only Discord. Nobody re-runs the channels; the closing pass
    # re-derives the answer from the labels, so the config change is enough.
    assert store.close_delivered(["discord"]) == [2]


def test_no_enabled_channels_closes_nothing(store: IssueStore) -> None:
    # Otherwise "everyone delivered" is vacuously true and the queue empties
    # into a published state without anything having been delivered.
    assert store.close_delivered([]) == []
    assert store.client.issues[1]["state"] == "open"


def test_closing_keeps_the_source_and_topic_labels() -> None:
    store = IssueStore(
        FakeClient(
            [
                make_issue(
                    5,
                    labels=[
                        Status.READY.value,
                        "source:anthropic",
                        "topic:models",
                        f"{SENT_PREFIX}site",
                    ],
                )
            ]
        )
    )

    store.close_delivered(["site"])

    names = {label["name"] for label in store.client.issues[5]["labels"]}
    assert {"source:anthropic", "topic:models", f"{SENT_PREFIX}site"} <= names
    assert Status.READY.value not in names


# -- the feed as a delivery channel ------------------------------------------


def make_article(number: int) -> Article:
    return Article(
        number=number,
        title=f"Article {number}",
        url=f"https://example.com/{number}",
        # Descending, so the list is already in newest-first order.
        published=datetime(2026, 1, 1, tzinfo=UTC) - timedelta(days=number),
    )


def make_ready(number: int, *, announced: bool) -> IssueRecord:
    labels = [Status.READY.value] + ([f"{SENT_PREFIX}{FEED_CHANNEL}"] if announced else [])
    return IssueRecord(number=number, title=f"Article {number}", labels=labels)


def test_the_feed_holds_the_window_when_nothing_is_backed_up() -> None:
    articles = [make_article(n) for n in range(FEED_LIMIT + 5)]
    issues = [make_ready(n, announced=True) for n in range(FEED_LIMIT + 5)]

    assert len(_feed_selection(articles, issues)) == FEED_LIMIT


def test_an_article_that_fell_past_the_window_unannounced_is_carried_anyway() -> None:
    # Otherwise it waits on the feed forever, and the issue never closes.
    articles = [make_article(n) for n in range(FEED_LIMIT + 3)]
    issues = [make_ready(n, announced=n < FEED_LIMIT) for n in range(FEED_LIMIT + 3)]

    selected = [article.number for article in _feed_selection(articles, issues)]

    assert selected[FEED_LIMIT:] == [FEED_LIMIT, FEED_LIMIT + 1, FEED_LIMIT + 2]
    assert selected[:FEED_LIMIT] == list(range(FEED_LIMIT))


def test_old_published_articles_are_not_dragged_back_into_the_feed() -> None:
    articles = [make_article(n) for n in range(FEED_LIMIT + 3)]
    issues = [make_ready(n, announced=True) for n in range(FEED_LIMIT)]
    issues += [
        IssueRecord(number=n, title="old", labels=[Status.PUBLISHED.value])
        for n in range(FEED_LIMIT, FEED_LIMIT + 3)
    ]

    assert len(_feed_selection(articles, issues)) == FEED_LIMIT


# -- configuration -----------------------------------------------------------


def test_only_enabled_channels_are_required(config: Config) -> None:
    config = config.model_copy(
        update={
            "channels": [
                Channel(id="site"),
                Channel(id="discord", enabled=False),
            ]
        }
    )

    assert config.required_channels == ["site"]


def test_every_channel_gets_a_label_even_when_disabled(config: Config) -> None:
    # A disabled channel's past deliveries must stay readable in the issue list.
    config = config.model_copy(
        update={"channels": [Channel(id="site"), Channel(id="discord", enabled=False)]}
    )

    names = {spec.name for spec in label_specs(config)}

    assert {f"{SENT_PREFIX}site", f"{SENT_PREFIX}discord"} <= names
