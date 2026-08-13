"""The only place that talks to Gemini.

The pipeline runs unattended on a cron trigger, so a bad minute at the API must
never take a run down with it: every call here returns either a validated model
or ``None``, and the caller decides what an empty answer means. Retries cover
only the failures that clear on their own — quota, server faults, dropped
connections — because retrying a rejected key or a malformed request just burns
the quota faster.

The response schema travels with the request instead of being described in the
prompt, so the reply is validated by construction rather than parsed by hand.
"""

from __future__ import annotations

import time
from typing import TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from ..core.log import get_logger
from ..core.settings import Settings

log = get_logger(__name__)

MAX_ATTEMPTS = 4
MAX_BACKOFF_SECONDS = 30.0

# The SDK raises out of two error trees: the classic one under
# ``google.genai.errors``, and the next-gen client's own, which lives behind a
# private path. A client-side read timeout arrives from the second — so it
# matched neither the ladder below nor ``httpx.TransportError``, fell to the
# handler meant for bugs in our own code, and a roundup that only needed a
# longer call was thrown away without one retry.
#
# Imported defensively, because the path is private and may move. A test
# asserts it still resolves, so an SDK upgrade that moves it fails offline
# rather than quietly at six in the morning.
try:  # pragma: no cover - the failure path is the test's subject, not a branch
    from google.genai._gaos.lib import compat_errors as _compat
except ImportError:  # pragma: no cover
    _compat = None  # type: ignore[assignment]


def _classes(*names: str) -> tuple[type[BaseException], ...]:
    found = (getattr(_compat, name, None) for name in names)
    return tuple(cls for cls in found if isinstance(cls, type))


# Anything here is worth another attempt, or at least worth classifying by its
# status code. Everything else is a bug on our side and keeps its traceback.
RETRIABLE: tuple[type[BaseException], ...] = (
    errors.APIError,
    httpx.TransportError,
    ValidationError,
    *_classes("APIError"),
)

# A stalled or dropped call: nothing about the request is wrong, so it is never
# permanent however it is spelled.
CONNECTION_FAULTS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    *_classes("APIConnectionError"),
)

T = TypeVar("T", bound=BaseModel)


class GeminiError(RuntimeError):
    pass


class GeminiClient:
    """One long-lived client; one way to ask it anything."""

    def __init__(self, settings: Settings, model: str, timeout: float | None = None) -> None:
        if not settings.gemini_api_key:
            raise GeminiError("GEMINI_API_KEY is not set")

        # Passed in rather than read from settings: each stage runs its own
        # model, and which one is a config decision, not a client one. The
        # timeout is per stage for the same reason — one article and sixty
        # write-ups are not the same call, and the default is sized for the
        # first.
        self.model = model
        seconds = settings.request_timeout if timeout is None else timeout
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            # HttpOptions counts the timeout in milliseconds.
            http_options=types.HttpOptions(timeout=int(seconds * 1000)),
        )

    def structured(self, prompt: str, schema: type[T], system_instruction: str = "") -> T | None:
        """Ask for one instance of ``schema``, or return None if it cannot be had."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                interaction = self._client.interactions.create(
                    model=self.model,
                    input=prompt,
                    system_instruction=system_instruction or None,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": schema.model_json_schema(),
                    },
                    # Nothing here is worth keeping server-side between runs.
                    store=False,
                )
                return schema.model_validate_json(interaction.output_text or "")
            except RETRIABLE as exc:
                wait = _retry_delay(exc, attempt)
                if wait is None:
                    log.error("gemini call failed: %s: %s", type(exc).__name__, exc)
                    return None
                if attempt == MAX_ATTEMPTS:
                    break
                log.warning(
                    "gemini call failed (%s), retrying in %.0fs (attempt %d/%d)",
                    type(exc).__name__,
                    wait,
                    attempt,
                    MAX_ATTEMPTS,
                )
                time.sleep(wait)
            except Exception as exc:  # noqa: BLE001 - one bad call must not end the run
                log.exception("gemini call raised: %s", exc)
                return None

        log.error("gemini gave up after %d attempts", MAX_ATTEMPTS)
        return None


def _status_code(exc: Exception) -> int | None:
    """The HTTP status behind a failure, whichever tree it came out of."""
    for attribute in ("code", "status_code"):
        try:
            return int(getattr(exc, attribute))
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def _retry_delay(exc: Exception, attempt: int) -> float | None:
    """Seconds to wait before trying again, or None when trying again is pointless."""
    backoff = min(float(2**attempt), MAX_BACKOFF_SECONDS)

    # Before any status code, because a connection fault carries one on the
    # next-gen tree — a timeout arrives with a status attribute that says
    # nothing about the request, and reading it as a 4xx would retire exactly
    # the failure this ladder is for.
    if isinstance(exc, CONNECTION_FAULTS):
        return backoff

    code = _status_code(exc)
    if code is not None:
        if code == 429 or code >= 500:
            return backoff
        # Any other 4xx means the request itself is wrong; it will stay wrong.
        return None

    # An unparseable sample usually clears on a fresh call.
    return backoff
