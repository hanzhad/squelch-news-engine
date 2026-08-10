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

T = TypeVar("T", bound=BaseModel)


class GeminiError(RuntimeError):
    pass


class GeminiClient:
    """One long-lived client; one way to ask it anything."""

    def __init__(self, settings: Settings) -> None:
        if not settings.gemini_api_key:
            raise GeminiError("GEMINI_API_KEY is not set")

        self.model = settings.gemini_model
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            # HttpOptions counts the timeout in milliseconds.
            http_options=types.HttpOptions(timeout=int(settings.request_timeout * 1000)),
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
            except (errors.APIError, httpx.TransportError, ValidationError) as exc:
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


def _status_code(exc: errors.APIError) -> int | None:
    try:
        return int(exc.code)  # type: ignore[arg-type]
    except (AttributeError, TypeError, ValueError):
        return None


def _retry_delay(exc: Exception, attempt: int) -> float | None:
    """Seconds to wait before trying again, or None when trying again is pointless."""
    backoff = min(float(2**attempt), MAX_BACKOFF_SECONDS)

    if isinstance(exc, errors.APIError):
        code = _status_code(exc)
        if code == 429 or (code is not None and code >= 500):
            return backoff
        # Any other 4xx means the request itself is wrong; it will stay wrong.
        return None

    # A transport fault or an unparseable sample usually clears on a fresh call.
    return backoff
