"""Which failures are worth another call, and which are the run's own fault."""

from __future__ import annotations

import httpx
import pytest
from google.genai import errors
from pydantic import BaseModel, ValidationError

from squelch.core.settings import Settings
from squelch.llm import gemini
from squelch.llm.gemini import MAX_BACKOFF_SECONDS, _retry_delay


def raised(cls: type[BaseException], **attributes: object) -> BaseException:
    """An instance of an SDK error without going through its constructor.

    The trees differ in what they take and both are the vendor's to change; what
    this module cares about is the type and the status attribute on it.
    """
    exc = cls.__new__(cls)  # type: ignore[call-overload]
    for name, value in attributes.items():
        object.__setattr__(exc, name, value)
    return exc


def test_the_sdk_timeout_class_is_still_where_we_import_it_from() -> None:
    """The next-gen error tree sits behind a private path, so this is the one
    thing between us and the bug returning: an upgrade that moves it fails here,
    offline, rather than at six in the morning with the day's roundup gone."""
    timeout = pytest.importorskip("google.genai._gaos.lib.compat_errors").APITimeoutError

    assert issubclass(timeout, gemini.RETRIABLE)
    assert issubclass(timeout, gemini.CONNECTION_FAULTS)
    # And still not reachable through the classic tree, which is what made a
    # client-side timeout look like a bug in our own code for a whole release.
    assert not issubclass(timeout, errors.APIError)


def test_a_client_side_timeout_is_retried() -> None:
    """The failure that lost 2026-08-13: the call stalled, the SDK raised its
    own timeout, and the ladder never saw it. A stalled call is the plainest
    transient there is — the next one usually lands."""
    compat = pytest.importorskip("google.genai._gaos.lib.compat_errors")

    # status_code is carried by the whole tree and says nothing here.
    exc = raised(compat.APITimeoutError, status_code=None)

    assert _retry_delay(exc, 1) == 2.0


def test_a_connection_fault_is_never_read_as_a_client_error() -> None:
    """Guarding the order in _retry_delay: a connection fault that happens to
    carry a 4xx-shaped attribute must not be classified by it, or the retry
    disappears again in a way nothing would notice."""
    compat = pytest.importorskip("google.genai._gaos.lib.compat_errors")

    exc = raised(compat.APIConnectionError, status_code=400)

    assert _retry_delay(exc, 1) == 2.0


@pytest.mark.parametrize("code", [429, 500, 503])
def test_quota_and_server_faults_are_retried(code: int) -> None:
    assert _retry_delay(raised(errors.APIError, code=code), 2) == 4.0


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_a_rejected_request_is_not_retried(code: int) -> None:
    """It will stay rejected, and retrying a bad key burns the quota faster."""
    assert _retry_delay(raised(errors.APIError, code=code), 1) is None


def test_a_status_code_is_read_from_either_tree() -> None:
    compat = pytest.importorskip("google.genai._gaos.lib.compat_errors")

    assert _retry_delay(raised(compat.APIStatusError, status_code=429), 1) == 2.0
    assert _retry_delay(raised(compat.APIStatusError, status_code=400), 1) is None


def test_a_dropped_connection_and_an_unparseable_sample_both_wait() -> None:
    class Sample(BaseModel):
        value: int

    with pytest.raises(ValidationError) as caught:
        Sample.model_validate_json("{}")

    assert _retry_delay(httpx.ConnectError("dropped"), 1) == 2.0
    assert _retry_delay(caught.value, 1) == 2.0


def test_backoff_is_capped() -> None:
    assert _retry_delay(httpx.ConnectError("dropped"), 20) == MAX_BACKOFF_SECONDS


def test_the_roundup_gets_a_longer_ceiling_than_the_per_article_stages() -> None:
    """One call over up to sixty write-ups against one call over one article.
    The shared ceiling was sized for the second and cost a day of the first."""
    settings = Settings(_env_file=None, gemini_api_key="k")  # type: ignore[call-arg]

    assert settings.digest_request_timeout > settings.request_timeout
