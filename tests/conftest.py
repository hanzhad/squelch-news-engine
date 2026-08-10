"""Shared test fixtures.

Nothing in the test suite touches the network or the GitHub API: feeds come
from ``tests/fixtures`` and HTTP clients are hand-rolled fakes.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

# The suite is meant to run straight from a checkout, so make the src-layout
# package importable even when aetherfeed has not been pip-installed yet.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aetherfeed.core.config import Config, Source  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def read_fixture() -> Callable[[str], bytes]:
    def _read(name: str) -> bytes:
        return (FIXTURES_DIR / name).read_bytes()

    return _read


@pytest.fixture
def config() -> Config:
    return Config(
        focus="Test focus.",
        topics=["models", "tooling"],
        max_body_chars=8000,
        sources=[],
    )


@pytest.fixture
def make_source() -> Callable[..., Source]:
    def _make(
        source_id: str = "fixture",
        url: str = "https://example.com/feed.xml",
        **overrides: object,
    ) -> Source:
        # fetch_full_text defaults to True in production; tests opt in instead,
        # so that a scrape does not try to follow every link it finds.
        params: dict[str, object] = {
            "id": source_id,
            "type": "rss",
            "url": url,
            "fetch_full_text": False,
            "max_items": 10,
        }
        params.update(overrides)
        return Source(**params)

    return _make
