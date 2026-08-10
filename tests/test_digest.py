"""The weekly digest — and the difference between a quiet week and a broken one."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from squelch.core.config import Config
from squelch.core.models import Digest, DigestEntry
from squelch.core.settings import Settings
from squelch.github.issues import IssueRecord
from squelch.llm import digest as digest_module
from squelch.llm.digest import DigestError, _verified_highlights, build_digest


class FakeStore:
    def __init__(self, issues: list[IssueRecord]) -> None:
        self._issues = issues

    def list_published_since(self, since: datetime) -> list[IssueRecord]:
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


def test_a_quiet_week_is_not_an_error(settings: Settings, config: Config) -> None:
    assert build_digest(settings, config, FakeStore([]), days=7) is None  # type: ignore[arg-type]


def test_a_failed_generation_raises_instead_of_looking_like_a_quiet_week(
    settings: Settings, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bug this guards: both used to return None, so a rate-limited run
    # logged "nothing published" over a week that published plenty — in green.
    stub_client(monkeypatch, None)
    store = FakeStore([make_issue(1, "https://example.com/a")])

    with pytest.raises(DigestError):
        build_digest(settings, config, store, days=7)  # type: ignore[arg-type]


def test_a_successful_digest_comes_back_whole(
    settings: Settings, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_client(
        monkeypatch,
        Digest(
            headline="A week of releases",
            trends=["Everyone shipped an agent"],
            highlights=[DigestEntry(title="A", takeaway="t", url="https://example.com/a")],
        ),
    )
    store = FakeStore([make_issue(1, "https://example.com/a")])

    result = build_digest(settings, config, store, days=7)  # type: ignore[arg-type]

    assert result is not None
    assert result.headline == "A week of releases"
    assert [h.url for h in result.highlights] == ["https://example.com/a"]


def test_highlights_the_model_invented_are_dropped() -> None:
    # The links come back from a language model; only the ones we fed it are real.
    digest = Digest(
        headline="h",
        trends=[],
        highlights=[
            DigestEntry(title="real", takeaway="t", url="https://example.com/a"),
            DigestEntry(title="invented", takeaway="t", url="https://example.com/never-supplied"),
        ],
    )

    kept = _verified_highlights(digest, [make_issue(1, "https://example.com/a")])

    assert [h.title for h in kept] == ["real"]
