"""A roundup as an issue: written by one stage, posted by another."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from squelch.core.config import Channel, Config
from squelch.core.models import Digest, DigestEntry, Period, Status
from squelch.core.settings import Settings
from squelch.github.digests import (
    DigestStore,
    digest_from_meta,
    digest_label,
    period_of,
    render_digest_body,
)
from squelch.github.issues import IssueStore, parse_body
from squelch.llm import digest as digest_module
from squelch.llm.digest import run_digest
from squelch.publishers import discord
from squelch.publishers.discord import publish_digests

DIGEST_HOOK = "https://discord.com/api/webhooks/3/digest"


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    """Enough of the issues API to create, relabel and close."""

    repo = "acme/feed"

    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self._next = 1
        self.patches: list[tuple[int, list[str]]] = []

    def paginate(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        wanted = (params or {}).get("labels")
        state = (params or {}).get("state", "open")
        out = []
        for issue in self.issues.values():
            names = [label["name"] for label in issue["labels"]]
            if wanted is not None and wanted not in names:
                continue
            if state != "all" and issue["state"] != state:
                continue
            out.append(issue)
        return out

    def request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> FakeResponse:
        fields = json or {}
        if method == "POST" and path.endswith("/issues"):
            number = self._next
            self._next += 1
            issue = {
                "number": number,
                "title": fields["title"],
                "body": fields["body"],
                "labels": [{"name": name} for name in fields.get("labels", [])],
                "state": "open",
                "html_url": f"https://github.com/acme/feed/issues/{number}",
            }
            self.issues[number] = issue
            return FakeResponse(issue)

        number = int(path.rsplit("/", 1)[-1])
        issue = self.issues[number]
        if "labels" in fields:
            issue["labels"] = [{"name": name} for name in fields["labels"]]
        for key in ("body", "state", "state_reason"):
            if key in fields:
                issue[key] = fields[key]
        # Which of body and labels moved, in order — record_delivery depends on
        # the body landing first, and that is what stops a double post.
        self.patches.append((number, sorted(fields)))
        return FakeResponse(issue)


def make_digest(headline: str = "A day of releases") -> Digest:
    return Digest(
        headline=headline,
        trends=["Everyone shipped an agent."],
        highlights=[DigestEntry(title="A", takeaway="It matters.", url="https://e.com/a")],
    )


def digest_config(enabled: bool = True) -> Config:
    return Config(
        focus="f",
        channels=[
            Channel(id="discord", skip=["topic:claude-skills"]),
            Channel(id="discord-digest", consumes="digest", enabled=enabled),
        ],
    )


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "discord_digest_webhook_url": DIGEST_HOOK,
        "publish_delay_seconds": 0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def fake_webhook(monkeypatch: pytest.MonkeyPatch) -> type:
    from test_routing import FakeWebhook

    FakeWebhook.sent = []
    FakeWebhook.threads = []
    monkeypatch.setattr(discord, "_Webhook", FakeWebhook)
    return FakeWebhook


def sent() -> list[tuple[str, dict[str, Any]]]:
    from test_routing import FakeWebhook

    return FakeWebhook.sent


# -- storing ------------------------------------------------------------------


def test_a_stored_roundup_survives_the_round_trip() -> None:
    store = DigestStore(FakeClient())

    issue = store.create(make_digest(), Period.DAILY, days=1, articles=15)

    read_back = digest_from_meta(issue.meta)
    assert read_back is not None
    assert read_back.headline == "A day of releases"
    assert [h.url for h in read_back.highlights] == ["https://e.com/a"]
    assert issue.meta["articles"] == 15


def test_the_headline_names_the_issue() -> None:
    store = DigestStore(FakeClient())

    issue = store.create(make_digest(), Period.WEEKLY, days=7, articles=40)

    assert issue.title == "A day of releases"


def test_a_blank_headline_still_gets_an_issue_title() -> None:
    # GitHub refuses an untitled issue, and a model can return a blank headline.
    store = DigestStore(FakeClient())

    issue = store.create(make_digest(headline="  "), Period.DAILY, days=1, articles=2)

    assert issue.title == "Daily digest"


def test_a_roundup_carries_no_status_label() -> None:
    """The invariant that keeps a digest inert. A `status:` label would put it
    in front of the classifier, the site build, and the window the *next*
    digest reads — a roundup summarising itself."""
    client = FakeClient()
    DigestStore(client).create(make_digest(), Period.DAILY, days=1, articles=3)

    labels = [label["name"] for label in client.issues[1]["labels"]]
    assert labels == [digest_label(Period.DAILY)]
    assert not any(label.startswith("status:") for label in labels)


def test_the_article_pipeline_never_sees_a_roundup() -> None:
    client = FakeClient()
    DigestStore(client).create(make_digest(), Period.DAILY, days=1, articles=3)
    articles = IssueStore(client)

    assert articles.list_by_status(Status.READY) == []
    assert articles.list_published_since(datetime(2020, 1, 1, tzinfo=UTC)) == []


def test_the_body_keeps_the_payload_a_person_can_edit() -> None:
    # The YAML block is what the publisher reads, so it has to survive being
    # rendered into a body and parsed back out of one.
    meta = {"period": "daily", "headline": "H", "trends": ["T"], "highlights": []}

    parsed, _, _ = parse_body(render_digest_body(meta))

    assert parsed["headline"] == "H"
    assert parsed["trends"] == ["T"]


def test_a_second_build_on_the_same_day_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow dispatched by hand on a morning the cron already ran must not
    queue a second copy of the same day."""
    client = FakeClient()
    store = DigestStore(client)
    today = datetime.now(UTC).date()
    store.create(make_digest(), Period.DAILY, days=1, articles=3, built_on=today)

    assert store.built_on(Period.DAILY, today) is not None
    assert store.built_on(Period.WEEKLY, today) is None
    assert store.built_on(Period.DAILY, date(2000, 1, 1)) is None


def test_the_double_build_guard_survives_the_closing_pass() -> None:
    """The roundup is posted within the hour and closed by the next closing
    tick. An open-only search would stop recognising the morning's roundup
    about fifteen minutes after it went out, and a dispatch at noon would write
    a second one for the same day."""
    client = FakeClient()
    store = DigestStore(client)
    today = datetime.now(UTC).date()
    issue = store.create(make_digest(), Period.DAILY, days=1, articles=3, built_on=today)
    client.issues[issue.number]["state"] = "closed"

    assert store.built_on(Period.DAILY, today) is not None


def test_a_stalled_queue_does_not_stop_tomorrow_s_roundup() -> None:
    # Keyed on the day, not on "is anything pending": a delivery outage holds
    # up the roundups it has and never stops the next one being written.
    client = FakeClient()
    store = DigestStore(client)
    store.create(make_digest(), Period.DAILY, days=1, articles=3, built_on=date(2026, 8, 11))

    assert store.built_on(Period.DAILY, date(2026, 8, 12)) is None


# -- the build stage ----------------------------------------------------------


class FakeIssueStore:
    def __init__(self, issues: list[Any]) -> None:
        self._issues = issues

    def list_published_since(self, since: datetime) -> list[Any]:
        return self._issues


def stub_gemini(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    class FakeGemini:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def structured(self, *args: object, **kwargs: object) -> Any:
            return result

    monkeypatch.setattr(digest_module, "GeminiClient", FakeGemini)


def published(number: int, url: str) -> Any:
    from squelch.github.issues import IssueRecord

    return IssueRecord(
        number=number,
        title=f"Article {number}",
        html_url=f"https://github.com/acme/feed/issues/{number}",
        meta={"url": url, "score": 5},
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_the_build_stage_stores_instead_of_posting(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_gemini(monkeypatch, make_digest())
    client = FakeClient()

    issue = run_digest(
        make_settings(gemini_api_key="k"),
        config,
        FakeIssueStore([published(1, "https://e.com/a")]),  # type: ignore[arg-type]
        DigestStore(client),
        Period.DAILY,
    )

    assert issue is not None
    assert sent() == []  # nothing reached a webhook
    assert client.issues[issue.number]["state"] == "open"


def test_run_digest_refuses_to_write_today_s_roundup_twice(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_gemini(monkeypatch, make_digest())
    client = FakeClient()
    store = DigestStore(client)
    settings = make_settings(gemini_api_key="k")
    issues = FakeIssueStore([published(1, "https://e.com/a")])

    first = run_digest(settings, config, issues, store, Period.DAILY)  # type: ignore[arg-type]
    second = run_digest(settings, config, issues, store, Period.DAILY)  # type: ignore[arg-type]

    assert first is not None
    assert second is None
    assert len(client.issues) == 1


def test_an_explicit_window_overrides_the_double_build_guard(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catch-up path after an outage: a second roundup on the same day is
    the whole intention. Without this the `days` input would report success and
    do nothing, which is the guard's own failure mode pointed the other way."""
    stub_gemini(monkeypatch, make_digest())
    client = FakeClient()
    store = DigestStore(client)
    settings = make_settings(gemini_api_key="k")
    issues = FakeIssueStore([published(1, "https://e.com/a")])

    run_digest(settings, config, issues, store, Period.DAILY)  # type: ignore[arg-type]
    caught_up = run_digest(settings, config, issues, store, Period.DAILY, days=4)  # type: ignore[arg-type]

    assert caught_up is not None
    assert len(client.issues) == 2
    assert client.issues[caught_up.number]["body"].count("days: 4") == 1


def test_a_quiet_day_stores_nothing(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_gemini(monkeypatch, make_digest())
    client = FakeClient()

    issue = run_digest(
        make_settings(gemini_api_key="k"),
        config,
        FakeIssueStore([]),  # type: ignore[arg-type]
        DigestStore(client),
        Period.DAILY,
    )

    assert issue is None
    assert client.issues == {}


# -- the delivery stage -------------------------------------------------------


def stored(client: FakeClient, period: Period = Period.DAILY) -> Any:
    return DigestStore(client).create(make_digest(), period, days=period.days, articles=5)


def test_a_waiting_roundup_is_posted_and_marked() -> None:
    client = FakeClient()
    issue = stored(client)

    posted = publish_digests(make_settings(), digest_config(), DigestStore(client))

    assert posted == 1
    assert sent()[0][0] == DIGEST_HOOK
    labels = [label["name"] for label in client.issues[issue.number]["labels"]]
    assert "sent:discord-digest" in labels


def test_the_body_is_written_before_the_label() -> None:
    """The order that stops a double post: a run dying between the two leaves
    the message id behind while the issue still looks undelivered."""
    client = FakeClient()
    stored(client)

    publish_digests(make_settings(), digest_config(), DigestStore(client))

    writes = [fields for _, fields in client.patches]
    assert writes.index(["body"]) < writes.index(["labels"])


def test_a_roundup_already_posted_is_relabelled_not_reposted() -> None:
    # The crash window: the message went out, the label did not.
    client = FakeClient()
    issue = stored(client)
    DigestStore(client).record_delivery(issue, "discord-digest", {"message_id": "42"})
    client.issues[issue.number]["labels"] = [{"name": digest_label(Period.DAILY)}]
    before = len(sent())

    posted = publish_digests(make_settings(), digest_config(), DigestStore(client))

    assert posted == 0
    assert len(sent()) == before
    labels = [label["name"] for label in client.issues[issue.number]["labels"]]
    assert "sent:discord-digest" in labels


def test_a_delivered_roundup_is_not_posted_again() -> None:
    client = FakeClient()
    stored(client)
    settings, config = make_settings(), digest_config()

    publish_digests(settings, config, DigestStore(client))
    again = publish_digests(settings, config, DigestStore(client))

    assert again == 0
    assert len(sent()) == 1


@pytest.mark.parametrize(
    "body",
    [
        # Edited into the wrong shape.
        "<!-- squelch\nheadline: [1, 2]\n-->",
        # Indentation broken by hand, so the block parses as nothing at all.
        # Every field below then validates as its own empty default, which is
        # how a blank card reaches a public channel.
        "<!-- squelch\n  headline: H\n trends: []\n-->",
        # No block left at all.
        "Someone replaced the whole body with a note.",
    ],
)
def test_an_unreadable_block_stays_in_the_queue(body: str) -> None:
    # Skipping it silently would hide the fact that a roundup is never going
    # out; posting it would publish an empty embed.
    client = FakeClient()
    issue = stored(client)
    client.issues[issue.number]["body"] = body

    posted = publish_digests(make_settings(), digest_config(), DigestStore(client))

    assert posted == 0
    assert sent() == []
    labels = [label["name"] for label in client.issues[issue.number]["labels"]]
    assert "sent:discord-digest" not in labels


def test_an_empty_block_is_not_a_roundup() -> None:
    # Every field of Digest validates as its own empty default, so this has to
    # be caught before the model is built rather than by it.
    assert digest_from_meta({}) is None
    assert digest_from_meta({"headline": "   ", "trends": [], "highlights": []}) is None
    assert digest_from_meta({"headline": "", "trends": ["something happened"]}) is not None


def test_a_body_too_long_to_render_keeps_its_metadata_block() -> None:
    """A blunt cut would sever the closing `-->`, and a block that no longer
    parses reads back as no roundup at all — the one state writing must never
    be able to reach."""
    meta = {"headline": "H", "trends": ["t"], "highlights": [], "filler": "x" * 200}

    body = render_digest_body(meta)
    parsed, _, _ = parse_body(body)

    assert parsed["headline"] == "H"


def test_a_delivery_never_overwrites_a_body_it_could_not_read() -> None:
    # Rewriting from empty metadata would replace the stored roundup with the
    # delivery note alone, destroying it permanently.
    client = FakeClient()
    issue = stored(client)
    client.issues[issue.number]["body"] = "no block here"
    unreadable = DigestStore(client).list_pending("discord-digest")[0]

    DigestStore(client).record_delivery(unreadable, "discord-digest", {"message_id": "1"})

    assert client.issues[issue.number]["body"] == "no block here"
    labels = [label["name"] for label in client.issues[issue.number]["labels"]]
    assert "sent:discord-digest" in labels


def test_a_blank_message_id_still_counts_as_posted() -> None:
    """Discord occasionally answers 2xx with a body we cannot read, which
    leaves the id blank. Treating that as "never posted" would reopen the exact
    window this bookkeeping exists to close."""
    client = FakeClient()
    issue = stored(client)
    DigestStore(client).record_delivery(issue, "discord-digest", {"message_id": ""})
    client.issues[issue.number]["labels"] = [{"name": digest_label(Period.DAILY)}]

    posted = publish_digests(make_settings(), digest_config(), DigestStore(client))

    assert posted == 0
    assert sent() == []


def test_the_footer_says_which_roundup_it_is() -> None:
    client = FakeClient()
    stored(client, Period.WEEKLY)

    publish_digests(make_settings(), digest_config(), DigestStore(client))

    assert sent()[0][1]["embeds"][0]["footer"]["text"] == "squelch · weekly digest"


def test_a_forum_names_the_post_after_the_headline() -> None:
    client = FakeClient()
    stored(client)

    publish_digests(make_settings(digest_forum=True), digest_config(), DigestStore(client))

    assert sent()[0][1]["thread_name"] == "A day of releases"


def test_a_text_channel_gets_no_thread_name() -> None:
    client = FakeClient()
    stored(client)

    publish_digests(make_settings(), digest_config(), DigestStore(client))

    assert "thread_name" not in sent()[0][1]


def test_a_disabled_digest_channel_is_a_quiet_no_op() -> None:
    client = FakeClient()
    stored(client)

    assert publish_digests(make_settings(), digest_config(enabled=False), DigestStore(client)) == 0
    assert sent() == []


def test_a_missing_webhook_fails_loudly() -> None:
    client = FakeClient()
    stored(client)

    with pytest.raises(discord.DiscordError, match="DISCORD_DIGEST_WEBHOOK_URL"):
        publish_digests(
            make_settings(discord_digest_webhook_url=""), digest_config(), DigestStore(client)
        )


def test_the_oldest_waiting_roundup_goes_first() -> None:
    # A Tuesday roundup arriving after Wednesday's would read as a correction.
    client = FakeClient()
    store = DigestStore(client)
    store.create(make_digest("Monday"), Period.DAILY, days=1, articles=1)
    store.create(make_digest("Tuesday"), Period.DAILY, days=1, articles=1)

    publish_digests(make_settings(), digest_config(), store)

    assert [p["embeds"][0]["title"] for _, p in sent()] == ["Monday", "Tuesday"]


# -- closing ------------------------------------------------------------------


def test_a_posted_roundup_is_closed_by_the_closing_pass() -> None:
    client = FakeClient()
    issue = stored(client)
    config = digest_config()
    publish_digests(make_settings(), config, DigestStore(client))

    closed = DigestStore(client).close_delivered(config.digest_channels)

    assert closed == [issue.number]
    assert client.issues[issue.number]["state"] == "closed"


def test_an_unposted_roundup_stays_open() -> None:
    client = FakeClient()
    issue = stored(client)

    closed = DigestStore(client).close_delivered(digest_config().digest_channels)

    assert closed == []
    assert client.issues[issue.number]["state"] == "open"


def test_no_digest_channel_leaves_everything_open() -> None:
    # Otherwise "everyone delivered" is vacuously true and the queue closes
    # without going anywhere.
    client = FakeClient()
    stored(client)

    assert DigestStore(client).close_delivered([]) == []


def test_a_digest_channel_never_gates_an_article() -> None:
    """It consumes a queue no article is ever in, so counting it would strand
    every ready article open — the same rule the rejected channel lives by."""
    config = digest_config()

    assert "discord-digest" not in config.required_channels
    assert [c.id for c in config.digest_channels] == ["discord-digest"]


def test_the_period_is_read_from_the_label() -> None:
    client = FakeClient()
    issue = stored(client, Period.WEEKLY)

    assert period_of(issue) is Period.WEEKLY
