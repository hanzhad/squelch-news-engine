"""Closing published issues once they are old enough.

Nothing in the pipeline ever closes a published issue, so at a few hundred
articles a week the repository would accumulate tens of thousands of open ones.
That is not a storage problem — it is a usability one: the issue list stops
loading, search gets slower, and the open count stops telling anyone anything.

A published article has already done its work; Discord has the message and the
static site has the archive. So after the retention window the issue is closed
but *not* relabelled: `status:3-published` is what the site build and the digest
query on, and closing an issue leaves its labels intact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .core.log import get_logger
from .core.models import Status
from .core.settings import Settings
from .github.issues import IssueStore

log = get_logger(__name__)


def run_retention(settings: Settings, store: IssueStore) -> int:
    """Close published issues older than the retention window. Returns how many."""
    window = settings.published_retention_days
    cutoff = datetime.now(UTC) - timedelta(days=window)
    closed = 0

    # list_by_status returns oldest first, which lets the loop stop at the first
    # issue inside the window instead of walking the whole published archive.
    for issue in store.list_by_status(Status.PUBLISHED):
        created = issue.created_at
        if created is None:
            # Undatable, so unjudgeable — leaving it open is the safe error.
            log.warning("#%d has no created_at, leaving it open", issue.number)
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)

        if created > cutoff:
            break
        if issue.state != "open":
            continue

        store.close(issue.number, "completed")
        closed += 1

    log.info("retention: closed %d issues older than %d days", closed, window)
    return closed
