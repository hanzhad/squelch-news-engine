"""Stage one: decide whether an article belongs in the feed at all.

Runs on everything scraped, which is why it uses the cheapest model and reads
only the top of an article — enough to tell news from noise. It writes no prose:
articles that survive are written up later, and the ones that do not are closed
without ever costing a summary.

Work is capped and paced on purpose. An article left raw is picked up by the
next cron tick, so falling behind costs nothing while overrunning the quota
costs the run. For the same reason a failed call leaves the issue exactly as it
was — retry is the next run's job, not this one's.
"""

from __future__ import annotations

from ..core.config import Config
from ..core.log import get_logger
from ..core.models import Classification, Status
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


def run_classify(settings: Settings, config: Config, store: IssueStore) -> tuple[int, int]:
    """Judge one batch of raw issues. Returns (kept, rejected)."""
    issues = store.list_by_status(Status.RAW, limit=settings.llm_batch_size)
    if not issues:
        log.info("nothing to classify")
        return 0, 0

    model = settings.gemini_classify_model or prompts.load_models().classify
    client = GeminiClient(settings, model)
    allowed = _topic_lookup(config)
    log.info("classifying %d issues with %s", len(issues), model)

    kept = 0
    rejected = 0
    for issue in paced(issues, settings.llm_delay_seconds):
        if not _judgeable(issue):
            log.warning("#%d has no text and a thin title, skipping: %s", issue.number, issue.title)
            continue

        verdict = client.structured(
            prompts.classify_prompt(config, issue, settings.classify_body_chars),
            Classification,
            prompts.system("classify"),
        )
        if verdict is None:
            log.warning("#%d got no verdict, staying raw", issue.number)
            continue

        verdict = verdict.model_copy(update={"tags": _clean_tags(verdict.tags, allowed)})

        try:
            store.apply_classification(issue, verdict)
        except GitHubError as exc:
            # Also stays raw, so the next run re-judges and re-applies it.
            log.error("#%d could not be updated: %s", issue.number, exc)
            continue

        if verdict.relevant:
            kept += 1
        else:
            rejected += 1

    log.info("classified %d issues: %d kept, %d rejected", len(issues), kept, rejected)
    return kept, rejected


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
