"""Reading one community case: what it claims, what would settle it, what to run.

The middle stage of the forum pipeline. Ingest opens an issue for a post, this
writes the reading onto it, and the answer stage posts that reading back into
the thread. Three stages rather than one for the reason the digest is split the
same way: a reply that failed to reach Discord is worth re-sending, not
re-writing, and the model's answer should survive a bad minute at either API.

A case that gets no reading stays at ``case:1-new`` and comes back on the next
tick. Silence is the right failure here — a forum where the bot sometimes says
nothing for twenty minutes is fine; one that posts half an answer is not.
"""

from __future__ import annotations

from ..core.config import Config
from ..core.log import get_logger
from ..core.models import CaseReading, CaseStatus
from ..core.settings import Settings
from ..core.throttle import paced
from ..github.cases import CaseStore
from ..github.client import GitHubError
from . import prompts
from .gemini import GeminiClient

log = get_logger(__name__)


def run_read_cases(settings: Settings, config: Config, store: CaseStore) -> int:
    """Read one batch of new cases. Returns how many now have a reading."""
    issues = store.list_by_status(CaseStatus.NEW, limit=settings.cases_batch_size)
    if not issues:
        log.info("no new cases to read")
        return 0

    model = settings.gemini_model or prompts.load_models().case
    client = GeminiClient(settings, model)
    log.info("reading %d case(s) with %s", len(issues), model)

    read = 0
    for issue in paced(issues, settings.llm_delay_seconds):
        reading = client.structured(
            prompts.case_prompt(config, issue),
            CaseReading,
            prompts.system("case"),
        )
        if reading is None or not reading.claim.strip():
            # An empty claim is not a short answer, it is no answer: the reply
            # would open with a blank line where the point should be.
            log.warning("#%d got no reading, staying new", issue.number)
            continue

        try:
            store.apply_reading(issue, reading)
        except GitHubError as exc:
            log.error("#%d could not be updated: %s", issue.number, exc)
            continue
        read += 1

    log.info("read %d of %d case(s)", read, len(issues))
    return read
