"""The deduplication ledger.

GitHub's search index lags behind writes by anything from seconds to minutes,
so "search the issues before creating one" loses races and posts duplicates.
Instead the scraper keeps its own ledger of article uids in the repository and
commits it back after every run, which is exact and costs no API calls.

The file is a rolling window: only the newest ``max_entries`` uids are kept, so
it stays small forever while still covering every source's realistic backlog.
"""

from __future__ import annotations

import json
from pathlib import Path

from .log import get_logger

log = get_logger(__name__)

DEFAULT_PATH = Path("data/seen.json")


class SeenLedger:
    def __init__(self, path: Path | None = None, max_entries: int = 5000) -> None:
        self.path = path or DEFAULT_PATH
        self.max_entries = max_entries
        self._order: list[str] = []
        self._index: set[str] = set()

    def load(self) -> SeenLedger:
        if not self.path.exists():
            log.info("no ledger at %s, starting empty", self.path)
            return self
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # A half-written or conflicted commit of the ledger must not wedge
            # every future run. Starting empty costs one round of duplicates;
            # refusing to start costs the pipeline.
            log.error("ledger at %s is unreadable (%s), starting empty", self.path, exc)
            return self
        self._order = list(payload.get("uids", []))
        self._index = set(self._order)
        log.info("loaded %d seen uids", len(self._order))
        return self

    def __contains__(self, uid: str) -> bool:
        return uid in self._index

    def add(self, uid: str) -> None:
        if uid in self._index:
            return
        self._index.add(uid)
        self._order.append(uid)

    def save(self) -> None:
        # Trim from the front: oldest uids fall out of the window first.
        if len(self._order) > self.max_entries:
            dropped = self._order[: -self.max_entries]
            self._order = self._order[-self.max_entries :]
            self._index = set(self._order)
            log.info("trimmed %d uids out of the ledger window", len(dropped))

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"version": 1, "uids": self._order}, indent=1) + "\n",
            encoding="utf-8",
        )
        log.info("saved ledger with %d uids", len(self._order))

    def __len__(self) -> int:
        return len(self._order)
