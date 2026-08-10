"""Scrape orchestration: sources in, issues out."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..core.config import Config
from ..core.log import get_logger
from ..core.models import RawArticle, Status
from ..core.settings import Settings
from ..core.throttle import paced
from ..github.client import GitHubError
from ..github.issues import IssueStore
from ..github.ledger import IssueLedger, NullLedger
from . import github_repos, html, rss, sites, web
from .extract import new_http_client

log = get_logger(__name__)

SCRAPERS = {
    "rss": rss.scrape,
    "html": html.scrape,
    "web": web.scrape,
    "github": github_repos.scrape,
}


def collect(config: Config, settings: Settings, only_type: str | None = None) -> list[RawArticle]:
    """Run every enabled source and return what they produced."""
    collected: list[RawArticle] = []
    with new_http_client(settings.request_timeout) as client:
        for source in config.enabled_sources:
            if only_type and source.type != only_type:
                continue
            # A hand-written scraper for this id wins over the generic one for
            # its type; see scrapers/sites/ for when that is worth doing.
            scraper = sites.get(source.id) or SCRAPERS.get(source.type)
            if scraper is None:
                log.error("source %s has unknown type %r", source.id, source.type)
                continue
            try:
                collected.extend(scraper(source, config, client))
            except Exception as exc:  # noqa: BLE001 - one bad source must not stop the rest
                log.exception("source %s crashed: %s", source.id, exc)
    return collected


def is_current(article: RawArticle, max_age_days: int) -> bool:
    """Whether this article is recent enough to still count as news.

    An article with no date passes. Undated is not the same as old, and the
    pages that state no date are exactly the ones we would otherwise silence
    entirely.
    """
    if article.published_at is None:
        return True
    return article.published_at >= datetime.now(UTC) - timedelta(days=max_age_days)


def _in_flight_uids(store: IssueStore | None) -> set[str]:
    """Uids of articles already queued but not yet through the pipeline.

    The ledger only knows what *this* scraper created. Articles suggested by
    the community through the issue form never pass through it, and an item can
    outlive the ledger's rolling window while still sitting in the queue. Both
    cases would otherwise come back as a duplicate issue. Only the two unpublished
    states are read — they stay small, unlike the published archive.
    """
    if store is None:
        return set()
    uids: set[str] = set()
    try:
        for status in (Status.RAW, Status.READY):
            uids.update(issue.uid for issue in store.list_by_status(status) if issue.uid)
    except GitHubError as exc:
        # Worst case we create a duplicate; that beats skipping the whole run.
        log.warning("could not read in-flight issues: %s", exc)
    return uids


def run_scrape(
    settings: Settings,
    config: Config,
    store: IssueStore | None,
    only_type: str | None = None,
    dry_run: bool = False,
) -> int:
    """Create issues for articles we have not seen before.

    Articles over the per-run cap are left unrecorded on purpose, so the next
    cron tick picks them up instead of dropping them.
    """
    ledger = (
        IssueLedger(store.client, max_entries=settings.seen_max_entries).load()
        if store is not None
        else NullLedger()
    )
    in_flight = _in_flight_uids(store)
    articles = collect(config, settings, only_type)

    fresh: list[RawArticle] = []
    batch_uids: set[str] = set()
    stale = 0
    for article in articles:
        uid = article.uid
        # The same story regularly appears in several feeds within one run.
        if uid in ledger or uid in batch_uids or uid in in_flight:
            continue
        if not is_current(article, config.max_age_days):
            stale += 1
            continue
        batch_uids.add(uid)
        fresh.append(article)

    log.info(
        "%d scraped, %d new, %d older than %d days",
        len(articles),
        len(fresh),
        stale,
        config.max_age_days,
    )

    capped = fresh[: settings.scrape_max_new_issues]
    if len(fresh) > len(capped):
        log.info(
            "creating %d of %d now; the rest waits for the next run (cap %d)",
            len(capped),
            len(fresh),
            settings.scrape_max_new_issues,
        )

    if dry_run:
        for article in capped:
            log.info("would create: [%s] %s", article.source, article.title[:80])
        return 0

    if store is None:
        raise ValueError("store is required unless dry_run is set")

    created = 0
    try:
        for article in paced(capped, settings.scrape_delay_seconds):
            try:
                store.create_article(article)
            except GitHubError as exc:
                # Leave it out of the ledger so the next run retries it.
                log.error("could not create issue for %s: %s", article.url, exc)
                continue
            ledger.add(article.uid)
            created += 1
    finally:
        # Persist whatever we managed to record, even if the run was cut short.
        ledger.save()

    log.info("created %d issues", created)
    return created
