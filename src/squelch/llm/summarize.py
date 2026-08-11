"""Stage two: write up the articles that survived classification.

Separate from stage one so the expensive model only ever reads articles that
are going to be published, and so a bad minute at the API costs a summary
rather than a verdict. An issue that fails here stays at ``status:2-relevant``
and is retried on the next tick; the judgement it already carries is not
thrown away.

Articles routed to a channel that publishes reviews get a second call here, on
a stronger model — see ``review.py``. Written in the same pass on purpose: an
article that reaches ``status:3-ready`` carries everything the publisher is
going to post, so there is no window in which a thread exists with its analysis
still pending.
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
from .review import review_repository

log = get_logger(__name__)


def run_summarize(settings: Settings, config: Config, store: IssueStore) -> int:
    """Write up one batch of accepted issues. Returns how many are now ready."""
    issues = store.list_by_status(Status.RELEVANT, limit=settings.llm_batch_size)
    if not issues:
        log.info("nothing to summarize")
        return 0

    models = prompts.load_models()
    model = settings.gemini_model or models.summarize
    client = GeminiClient(settings, model)
    # Built on first use: most runs review nothing, and a client is a key check
    # we would rather not fail on a day the rubric has no work.
    reviewer: GeminiClient | None = None
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

        review = None
        if config.wants_review(set(issue.labels)):
            reviewer = reviewer or GeminiClient(settings, models.review)
            review = review_repository(reviewer, config, issue)
            if review is None:
                # The rubric's whole content is this analysis, so an article
                # published without one is worse than an article published a
                # tick later. Stays relevant and comes back around, exactly as
                # a failed summary does.
                log.warning("#%d got no review, staying relevant", issue.number)
                continue

        try:
            store.apply_summary(issue, result, review)
        except GitHubError as exc:
            log.error("#%d could not be updated: %s", issue.number, exc)
            continue
        written += 1

    log.info("summarized %d of %d issues", written, len(issues))
    return written
