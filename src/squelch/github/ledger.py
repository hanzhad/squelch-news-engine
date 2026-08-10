"""The dedup ledger, stored in a single issue instead of a committed file.

Storing a uid is free — every article issue already carries one in its
metadata. Asking *"has any issue ever carried uid X?"* is the expensive part:
GitHub indexes issue bodies only through the search API, which lags writes and
allows 30 requests a minute, and the alternative — paginating the whole archive
— gets slower every week as rejected articles pile up.

Keeping the entire set in one issue body sidesteps both. One GET loads it, one
PATCH saves it, the cost never grows with the archive, and the scraper no
longer needs write access to the repository or a rebase dance against its own
bot commits.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..core.log import get_logger
from ..core.seen import SeenLedger
from .client import GitHubClient, GitHubError

log = get_logger(__name__)

LEDGER_LABEL = "meta:ledger"
LEDGER_TITLE = "squelch: seen ledger"
MARKER = "<!-- squelch:ledger -->"
UID_BLOCK_RE = re.compile(r"```text\n(.*?)\n```", re.DOTALL)

# GitHub caps an issue body at 65536 characters. A uid plus its newline is 17,
# and the preamble costs a few hundred, so this window fits with room to spare.
MAX_ENTRIES = 3500

PREAMBLE = (
    "Deduplication ledger for the scraper — **do not edit by hand**, the body is "
    "rewritten on every run.\n\n"
    "Each line is a short hash of a canonicalized article URL. Newest last; the "
    "oldest drop out once the window is full.\n"
)


class IssueLedger:
    """Same interface as :class:`SeenLedger`, backed by an issue body."""

    def __init__(self, client: GitHubClient, max_entries: int = MAX_ENTRIES) -> None:
        self.client = client
        self.max_entries = max_entries
        self.number: int | None = None
        self._order: list[str] = []
        self._index: set[str] = set()

    # -- loading ------------------------------------------------------------

    def load(self, seed_from: Path | None = None) -> IssueLedger:
        issue = self._find()
        if issue is None:
            log.info("no ledger issue yet, creating one")
            self._seed(seed_from)
            self._create()
            return self

        self.number = issue["number"]
        self._order = _parse_uids(issue.get("body") or "")
        self._index = set(self._order)
        log.info("loaded %d seen uids from issue #%d", len(self._order), self.number)
        return self

    def _find(self) -> dict | None:
        found = [
            issue
            for issue in self.client.paginate(
                f"/repos/{self.client.repo}/issues",
                {"labels": LEDGER_LABEL, "state": "all"},
            )
            if "pull_request" not in issue
        ]
        if not found:
            return None
        if len(found) > 1:
            log.warning(
                "%d issues carry %s; using the oldest and ignoring %s",
                len(found),
                LEDGER_LABEL,
                [i["number"] for i in sorted(found, key=lambda i: i["number"])[1:]],
            )
        # Always the oldest, so that if a duplicate ever gets created — the
        # issues list lags a second or two behind a fresh POST — every later
        # run still converges on the same one instead of alternating.
        return min(found, key=lambda issue: issue["number"])

    def _seed(self, seed_from: Path | None) -> None:
        """Carry over a file-based ledger the first time, so nothing reappears."""
        if seed_from is None or not seed_from.exists():
            return
        legacy = SeenLedger(seed_from, max_entries=self.max_entries).load()
        self._order = list(legacy._order)
        self._index = set(self._order)
        log.info("seeded %d uids from %s", len(self._order), seed_from)

    def _create(self) -> None:
        payload = self.client.request(
            "POST",
            f"/repos/{self.client.repo}/issues",
            json={
                "title": LEDGER_TITLE,
                "body": self._render(),
                "labels": [LEDGER_LABEL],
            },
        ).json()
        self.number = payload["number"]
        # Closed on purpose: it is storage, not work, and an open issue here
        # would sit at the top of the list forever.
        self.client.request(
            "PATCH",
            f"/repos/{self.client.repo}/issues/{self.number}",
            json={"state": "closed", "state_reason": "completed"},
        )
        log.info("created ledger issue #%d", self.number)

    # -- the SeenLedger interface ------------------------------------------

    def __contains__(self, uid: str) -> bool:
        return uid in self._index

    def __len__(self) -> int:
        return len(self._order)

    def add(self, uid: str) -> None:
        if uid in self._index:
            return
        self._index.add(uid)
        self._order.append(uid)

    def save(self) -> None:
        if len(self._order) > self.max_entries:
            dropped = len(self._order) - self.max_entries
            self._order = self._order[-self.max_entries :]
            self._index = set(self._order)
            log.info("trimmed %d uids out of the ledger window", dropped)

        if self.number is None:
            raise GitHubError("ledger was never loaded")

        self.client.request(
            "PATCH",
            f"/repos/{self.client.repo}/issues/{self.number}",
            json={"body": self._render()},
        )
        log.info("saved ledger issue #%d with %d uids", self.number, len(self._order))

    def _render(self) -> str:
        uids = "\n".join(self._order)
        return f"{MARKER}\n\n{PREAMBLE}\n```text\n{uids}\n```\n"


class NullLedger:
    """Stand-in for ``--dry-run`` without credentials: nothing is remembered."""

    def __contains__(self, uid: str) -> bool:
        return False

    def __len__(self) -> int:
        return 0

    def add(self, uid: str) -> None:
        pass

    def save(self) -> None:
        pass


def _parse_uids(body: str) -> list[str]:
    match = UID_BLOCK_RE.search(body)
    if not match:
        log.warning("ledger issue has no uid block; treating it as empty")
        return []
    return [line.strip() for line in match.group(1).splitlines() if line.strip()]
