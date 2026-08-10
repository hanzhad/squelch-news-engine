"""Stage two: write up the articles that survived classification.

Separate from stage one so the expensive model only ever reads articles that
are going to be published, and so a bad minute at the API costs a summary
rather than a verdict. An issue that fails here stays at ``status:2-relevant``
and is retried on the next tick; the judgement it already carries is not
thrown away.
"""

from __future__ import annotations

from ..core.config import Config
from ..core.log import get_logger
from ..core.models import Status, Summary
from ..core.settings import Settings
from ..core.throttle import paced
from ..github.client import GitHubError
from ..github.issues import IssueStore
from . import prompts
from .gemini import GeminiClient

log = get_logger(__name__)


def run_summarize(settings: Settings, config: Config, store: IssueStore) -> int:
    """Write up one batch of accepted issues. Returns how many are now ready."""
    issues = store.list_by_status(Status.RELEVANT, limit=settings.llm_batch_size)
    if not issues:
        log.info("nothing to summarize")
        return 0

    model = settings.gemini_model or prompts.load_models().summarize
    client = GeminiClient(settings, model)
    log.info("summarizing %d issues with %s", len(issues), model)

    written = 0
    for issue in paced(issues, settings.llm_delay_seconds):
        result = client.structured(
            prompts.summarize_prompt(config, issue),
            Summary,
            prompts.system("summarize"),
        )
        if result is None or not result.summary.strip():
            log.warning("#%d got no summary, staying relevant", issue.number)
            continue

        try:
            store.apply_summary(issue, result)
        except GitHubError as exc:
            log.error("#%d could not be updated: %s", issue.number, exc)
            continue
        written += 1

    log.info("summarized %d of %d issues", written, len(issues))
    return written
