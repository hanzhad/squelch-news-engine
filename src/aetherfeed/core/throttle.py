"""Pacing helper for loops that talk to rate-limited APIs."""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator


def paced[T](items: Iterable[T], delay: float) -> Iterator[T]:
    """Yield ``items``, sleeping ``delay`` seconds *between* them.

    No sleep before the first item or after the last one, so a run of one item
    costs nothing.
    """
    first = True
    for item in items:
        if not first and delay > 0:
            time.sleep(delay)
        first = False
        yield item
