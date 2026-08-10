"""The gate between everything scraped and the handful worth publishing.

Each article waits at ``status:1-raw`` until the model has an opinion about it.
Work is capped and paced on purpose: the free Gemini tier is measured in
requests per minute, and an issue left raw is picked up by the next cron tick,
so falling behind costs nothing while overrunning the quota costs the run. For
the same reason a failed call leaves the issue exactly as it was — retry is the
next run's job, not this one's.
"""

from __future__ import annotations

from ..core.config import Config
from ..core.log import get_logger
from ..core.models import Status, Verdict
from ..core.settings import Settings
from ..core.throttle import paced
from ..github.client import GitHubError
from ..github.issues import IssueRecord, IssueStore
from . import prompts
from .gemini import GeminiClient

log = get_logger(__name__)

# Below this, a title with no body carries too little to judge honestly, and
# asking anyway just produces a confident guess.
MIN_TITLE_CHARS = 30
MAX_TAGS = 3


def run_filter(settings: Settings, config: Config, store: IssueStore) -> tuple[int, int]:
    """Judge one batch of raw issues. Returns (ready, rejected)."""
    issues = store.list_by_status(Status.RAW, limit=settings.llm_batch_size)
    if not issues:
        log.info("nothing to filter")
        return 0, 0

    client = GeminiClient(settings)
    allowed = _topic_lookup(config)
    ready = 0
    rejected = 0

    for issue in paced(issues, settings.llm_delay_seconds):
        if not _judgeable(issue):
            log.warning("#%d has no text and a thin title, skipping: %s", issue.number, issue.title)
            continue

        verdict = client.structured(
            prompts.filter_prompt(config, issue), Verdict, prompts.filter_system()
        )
        if verdict is None:
            log.warning("#%d got no verdict, staying raw", issue.number)
            continue

        verdict = verdict.model_copy(update={"tags": _clean_tags(verdict.tags, allowed)})

        try:
            store.apply_verdict(issue, verdict)
        except GitHubError as exc:
            # Also stays raw, so the next run re-judges and re-applies it.
            log.error("#%d could not be updated: %s", issue.number, exc)
            continue

        if verdict.relevant:
            ready += 1
        else:
            rejected += 1

    log.info("filtered %d issues: %d ready, %d rejected", len(issues), ready, rejected)
    return ready, rejected


def _judgeable(issue: IssueRecord) -> bool:
    return bool(issue.text.strip()) or len(issue.title.strip()) >= MIN_TITLE_CHARS


def _topic_lookup(config: Config) -> dict[str, str]:
    """Case-folded topic -> the canonical spelling that becomes a label."""
    return {topic.strip().lower(): topic for topic in config.topics}


def _clean_tags(tags: list[str], allowed: dict[str, str]) -> list[str]:
    """Keep only known topics, in canonical spelling, without duplicates."""
    kept: list[str] = []
    for tag in tags:
        canonical = allowed.get(tag.strip().lower())
        if canonical is None:
            log.debug("dropping unknown tag %r", tag)
            continue
        if canonical not in kept:
            kept.append(canonical)
    return kept[:MAX_TAGS]
