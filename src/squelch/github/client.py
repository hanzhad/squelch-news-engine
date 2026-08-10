"""Thin GitHub REST client with rate-limit-aware retries.

GitHub enforces two separate budgets. The primary one (5000 requests/hour for
the Actions token) reports itself through ``x-ratelimit-*`` headers. The
secondary one throttles bursts of content creation and shows up as a 403 with a
``retry-after`` header and no warning beforehand. Both are handled here so that
callers never have to think about them.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..core.log import get_logger
from ..core.settings import Settings

log = get_logger(__name__)

API_ROOT = "https://api.github.com"
MAX_ATTEMPTS = 5


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        if not settings.github_token:
            raise GitHubError("GITHUB_TOKEN is not set")
        if "/" not in settings.github_repository:
            raise GitHubError("GITHUB_REPOSITORY must look like 'owner/repo'")

        self.settings = settings
        self.repo = settings.github_repository
        self._client = httpx.Client(
            base_url=API_ROOT,
            timeout=settings.request_timeout,
            headers={
                "Authorization": f"Bearer {settings.github_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "squelch",
            },
        )

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- transport ----------------------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_error: str = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = self._client.request(method, path, **kwargs)

            if response.is_success:
                return response

            wait = self._retry_delay(response, attempt)
            if wait is None:
                raise GitHubError(
                    f"{method} {path} failed: {response.status_code} {response.text[:400]}"
                )

            last_error = f"{response.status_code} {response.text[:200]}"
            log.warning(
                "%s %s -> %s, retrying in %.0fs (attempt %d/%d)",
                method,
                path,
                response.status_code,
                wait,
                attempt,
                MAX_ATTEMPTS,
            )
            time.sleep(wait)

        raise GitHubError(f"{method} {path} gave up after {MAX_ATTEMPTS} attempts: {last_error}")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait before retrying, or None if the error is terminal."""
        status = response.status_code

        if status >= 500:
            return min(2**attempt, 60)

        if status in (403, 429):
            # Secondary rate limit: GitHub tells us exactly how long to wait.
            retry_after = response.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), 120)

            # Primary rate limit: wait until the window resets.
            if response.headers.get("x-ratelimit-remaining") == "0":
                reset = response.headers.get("x-ratelimit-reset")
                if reset and reset.isdigit():
                    return max(0.0, min(float(reset) - time.time() + 1, 300))

            # A 403 without either signal is a permissions problem, not a
            # throttle — retrying it would just burn the budget.
            if status == 403:
                return None
            return min(2**attempt, 60)

        return None

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).json()

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Walk a paginated collection endpoint to the end."""
        results: list[dict[str, Any]] = []
        query = dict(params or {})
        query.setdefault("per_page", 100)
        page = 1
        while True:
            query["page"] = page
            batch = self.get_json(path, params=query)
            if not isinstance(batch, list) or not batch:
                break
            results.extend(batch)
            if len(batch) < query["per_page"]:
                break
            page += 1
        return results
