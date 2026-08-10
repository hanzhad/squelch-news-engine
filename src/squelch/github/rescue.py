"""Community rescue: enough 👍 reactions send a rejected article back.

The classifier is cheap and sometimes wrong, and the issue list is already the
admin panel — anyone with write access can flip a label by hand. This pass
extends that courtesy to readers who only have a GitHub account: react 👍 on a
rejected issue and the next rescue tick reopens it as relevant, so the
summariser picks it up. The reaction rollup rides along on the issue listing,
so counting votes costs no extra API requests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..core.log import get_logger
from ..core.models import Status
from ..core.settings import Settings
from .issues import IssueStore

log = get_logger(__name__)


def run_rescue(settings: Settings, store: IssueStore) -> int:
    """Reopen rejected issues the community voted back. Returns how many.

    The window bounds the listing: GitHub filters on last update, and the
    rejection itself is an update, so every rejected issue stays voteable for
    at least this long. A 👍 arriving later is simply too late — the archive
    is not paged through forever for it.
    """
    since = datetime.now(UTC) - timedelta(days=settings.rescue_window_days)
    rescued = 0
    for issue in store.list_by_status(Status.REJECTED, since=since):
        if issue.approvals < settings.rescue_min_reactions:
            continue
        store.rescue(issue, issue.approvals)
        rescued += 1
    log.info("rescued %d issue(s)", rescued)
    return rescued
