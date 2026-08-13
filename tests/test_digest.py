"""The digests — and the difference between a quiet window and a broken one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from squelch.core.config import Config, DigestPolicy
from squelch.core.models import Digest, DigestEntry, Period
from squelch.core.settings import Settings
from squelch.github.issues import IssueRecord
from squelch.llm import digest as digest_module
from squelch.llm.digest import (
    HARD_MAX_ARTICLES,
    DigestError,
    _score,
    _selected,
    _verified_highlights,
    build_digest,
)


class FakeStore:
    def __init__(self, issues: list[IssueRecord]) -> None:
        self._issues = issues
        self.asked_since: datetime | None = None

    def list_published_since(self, since: datetime) -> list[IssueRecord]:
        self.asked_since = since
        return self._issues


def make_issue(number: int, url: str, score: int = 5) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=f"Article {number}",
        html_url=f"https://github.com/acme/feed/issues/{number}",
        meta={"url": url, "score": score},
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, gemini_api_key="test-key")  # type: ignore[call-arg]


def stub_client(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    class FakeGemini:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def structured(self, *args: object, **kwargs: object) -> Any:
            return result

    monkeypatch.setattr(digest_module, "GeminiClient", FakeGemini)


@pytest.mark.parametrize("period", list(Period))
def test_a_quiet_window_is_not_an_error(
    settings: Settings, config: Config, period: Period
) -> None:
    # Normal for a week and routine for a day: with a daily roundup this is the
    # common path, not an edge case, and it must leave the run green.
    assert build_digest(settings, config, FakeStore([]), period) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(("period", "days"), [(Period.DAILY, 1), (Period.WEEKLY, 7)])
def test_each_period_looks_back_over_its_own_window(
    settings: Settings, config: Config, period: Period, days: int
) -> None:
    store = FakeStore([])

    build_digest(settings, config, store, period)  # type: ignore[arg-type]

    assert store.asked_since is not None
    assert abs((datetime.now(UTC) - store.asked_since) - timedelta(days=days)) < timedelta(
        minutes=1
    )


def test_an_explicit_window_overrides_the_period_s_own(
    settings: Settings, config: Config
) -> None:
    # The catch-up path: still the daily roundup, over three days of backlog.
    store = FakeStore([])

    build_digest(settings, config, store, Period.DAILY, days=3)  # type: ignore[arg-type]

    assert store.asked_since is not None
    assert abs((datetime.now(UTC) - store.asked_since) - timedelta(days=3)) < timedelta(minutes=1)


def test_each_period_is_written_from_its_own_prompt_file(
    settings: Settings, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daily reports and the weekly synthesises. If both were written from
    one file they would drift into the same message, and a reader who gets both
    on a Monday would be reading it twice."""
    seen: list[str] = []

    class RecordingGemini:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def structured(self, prompt: str, *args: object, **kwargs: object) -> Any:
            seen.append(prompt)
            return Digest(brief="The point.", summary="Prose.", trends=[], highlights=[])

    monkeypatch.setattr(digest_module, "GeminiClient", RecordingGemini)
    store = FakeStore([make_issue(1, "https://example.com/a")])

    build_digest(settings, config, store, Period.DAILY)  # type: ignore[arg-type]
    build_digest(settings, config, store, Period.WEEKLY)  # type: ignore[arg-type]

    # Structural markers rather than counts: the wording of these files is
    # rewritten often, but only the weekly ever asks for trends.
    assert "THE BRIEF" in seen[0] and "THE DETAIL" in seen[0]
    assert "THE TRENDS" not in seen[0]
    assert "THE TRENDS" in seen[1]


def test_the_roundup_asks_for_its_own_timeout(
    settings: Settings, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage that reads sixty write-ups gets its own ceiling. On the shared
    thirty seconds the call stalled, and the day's roundup was lost with it."""
    asked: list[float | None] = []

    class RecordingGemini:
        def __init__(self, *args: object, timeout: float | None = None, **kwargs: object) -> None:
            asked.append(timeout)

        def structured(self, *args: object, **kwargs: object) -> Any:
            return Digest(brief="The point.", summary="Prose.", trends=[], highlights=[])

    monkeypatch.setattr(digest_module, "GeminiClient", RecordingGemini)

    build_digest(  # type: ignore[arg-type]
        settings, config, FakeStore([make_issue(1, "https://example.com/a")]), Period.DAILY
    )

    assert asked == [settings.digest_request_timeout]


def test_a_failed_generation_raises_instead_of_looking_like_a_quiet_week(
    settings: Settings, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bug this guards: both used to return None, so a rate-limited run
    # logged "nothing published" over a week that published plenty — in green.
    stub_client(monkeypatch, None)
    store = FakeStore([make_issue(1, "https://example.com/a")])

    with pytest.raises(DigestError):
        build_digest(settings, config, store, Period.WEEKLY)  # type: ignore[arg-type]


def test_a_successful_digest_comes_back_whole(
    settings: Settings, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_client(
        monkeypatch,
        Digest(
            brief="A week of releases.",
            trends=["Everyone shipped an agent"],
            summary="What the week added up to.",
            highlights=[DigestEntry(title="A", url="https://example.com/a")],
        ),
    )
    store = FakeStore([make_issue(1, "https://example.com/a")])

    result = build_digest(settings, config, store, Period.WEEKLY)  # type: ignore[arg-type]

    assert result is not None
    assert result.digest.brief == "A week of releases."
    assert [h.url for h in result.digest.highlights] == ["https://example.com/a"]
    # The counts travel with it: they are stored on the issue and shown in its
    # body, which is how you tell a thin roundup from a thin week.
    assert (result.articles, result.days) == (1, 7)


# -- what reaches the prompt --------------------------------------------------


def scored(*scores: int) -> list[IssueRecord]:
    return [make_issue(n, f"https://example.com/{n}", score) for n, score in enumerate(scores)]


def test_the_shipped_policy_rations_nothing() -> None:
    """`min_score: 0` is what ships, and it has to mean "everything gets in".
    At fifteen articles a day there is nothing to ration yet, and a threshold
    nobody chose deliberately is the silent filter this pipeline is against."""
    issues = scored(*([0] * 150))

    assert len(_selected(issues, DigestPolicy())) == 150


def test_past_the_budget_a_good_score_still_gets_in() -> None:
    # The cap is the size the roundup aims for, not a hard edge: nothing is
    # dropped for being article number four alone.
    policy = DigestPolicy(max_articles=3, min_score=5)

    kept = _selected(scored(1, 1, 1, 9, 2), policy)

    # Ranked 9,2,1,1,1 — the first three are in on position, and nothing after
    # them clears 5.
    assert [_score(i) for i in kept] == [9, 2, 1]


def test_the_budget_alone_drops_nothing() -> None:
    # max_articles without a threshold is inert, which is what lets the knob
    # ship set while it is not yet meant to bite.
    policy = DigestPolicy(max_articles=2, min_score=0)

    assert len(_selected(scored(1, 2, 3, 4, 5), policy)) == 5


def test_a_window_that_has_to_lose_something_loses_its_filler() -> None:
    policy = DigestPolicy(max_articles=1, min_score=7)

    kept = _selected(scored(8, 3, 9, 2), policy)

    assert [_score(i) for i in kept] == [9, 8]


def test_an_unscored_article_is_the_first_to_go() -> None:
    # Opened by hand, never classified. Ranking it as zero is the only
    # defensible place for something nobody judged.
    policy = DigestPolicy(max_articles=1, min_score=1)
    unscored = IssueRecord(number=99, title="By hand", meta={})

    kept = _selected([*scored(4), unscored], policy)

    assert [i.number for i in kept] == [0]


def test_a_catch_up_run_cannot_blow_up_one_request() -> None:
    # The last-resort bound, for the --days override reaching over a backlog.
    kept = _selected(scored(*([5] * (HARD_MAX_ARTICLES + 50))), DigestPolicy())

    assert len(kept) == HARD_MAX_ARTICLES


def test_highlights_the_model_invented_are_dropped() -> None:
    # The links come back from a language model; only the ones we fed it are real.
    digest = Digest(
        brief="The point.",
        summary="Prose.",
        trends=[],
        highlights=[
            DigestEntry(title="real", url="https://example.com/a"),
            DigestEntry(title="invented", url="https://example.com/never-supplied"),
        ],
    )

    kept = _verified_highlights(digest, [make_issue(1, "https://example.com/a")])

    assert [h.title for h in kept] == ["real"]
