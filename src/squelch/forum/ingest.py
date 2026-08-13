"""Forum in, issues out — the case pipeline's first stage.

The shape is the scraper's: read everything visible, drop what is already
known, cap what one tick will take on, and leave the rest for the next one.
What differs is what "already known" means. The scraper has a ledger of urls it
created; a case is identified by its thread id, which Discord never reuses, so
the issues themselves are the ledger and no second store is needed.

Nothing here judges a post. Every case in the window that does not already have
an issue gets one, including the thin ones — the forum is where people put their
own work, and a pipeline that quietly skipped some of it would be a filter
nobody voted for.
"""

from __future__ import annotations

from ..core.config import Channel, Config
from ..core.log import get_logger
from ..core.settings import Settings
from ..core.throttle import paced
from ..github.cases import CaseStore
from ..github.client import GitHubError
from .bot import BotClient, fetch_cases

log = get_logger(__name__)


def run_ingest(
    settings: Settings,
    config: Config,
    store: CaseStore | None,
    channel: Channel,
    dry_run: bool = False,
    client: BotClient | None = None,
) -> int:
    """Open an issue for every case the pipeline has not seen. Returns how many."""
    # The post is bounded by the same knob an article's text is: a forum post
    # that runs to a novel is still one prompt, and the limit belongs in config
    # rather than in two places in Python.
    posts = fetch_cases(
        settings, channel, settings.cases_window_days, config.max_body_chars, client
    )
    if not posts:
        log.info("no cases in the window")
        return 0

    known = store.known_threads(settings.cases_window_days) if store is not None else set()
    fresh = [post for post in posts if post.uid not in known]
    log.info("%d case(s) visible, %d new", len(posts), len(fresh))

    capped = fresh[: settings.cases_max_new_issues]
    if len(fresh) > len(capped):
        log.info(
            "opening %d of %d now; the rest waits for the next run (cap %d)",
            len(capped),
            len(fresh),
            settings.cases_max_new_issues,
        )

    if dry_run:
        for post in capped:
            log.info("would open: %s — %s", post.author or "unknown", post.title[:70])
        return 0

    if store is None:
        raise ValueError("store is required unless dry_run is set")

    created = 0
    for post in paced(capped, settings.scrape_delay_seconds):
        try:
            store.create(post)
        except GitHubError as exc:
            # Left unrecorded so the next run tries again: the thread is still
            # in the forum, and the dedup set is derived from the issues.
            log.error("could not open an issue for %s: %s", post.url, exc)
            continue
        created += 1

    log.info("opened %d case issue(s)", created)
    return created
