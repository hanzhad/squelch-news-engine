"""The skills rubric's own reading of a repository.

A summary says what something is; this says whether it is worth an evening —
skill by skill, against what the repository claims about itself. It exists
because the one thing a scraper can measure about a skill collection is stars,
and stars are the least informative thing about it: the top result of the week
is routinely four files and a banner.

Part of stage two rather than a stage of its own. The review is written in the
same pass as the write-up and stored on the issue with it, so an article
reaches ``status:3-ready`` complete — the publisher never has to ask whether
the analysis it is about to post has been written yet.

Nothing here is a gate. The verdict is published as written, including when it
is damning; whether an article runs at all was decided by the classifier.
"""

from __future__ import annotations

from ..core.config import Config
from ..core.log import get_logger
from ..core.models import SkillsReview
from ..github.issues import IssueRecord
from . import prompts
from .gemini import GeminiClient

log = get_logger(__name__)


def review_repository(
    client: GeminiClient, config: Config, issue: IssueRecord
) -> SkillsReview | None:
    """Read one repository's files and judge them, or return None if it cannot be done."""
    review = client.structured(
        prompts.review_prompt(config, issue),
        SkillsReview,
        prompts.system("review"),
    )
    if review is None:
        return None
    log.info(
        "#%d reviewed: %s, %d skill(s)", issue.number, review.verdict, len(review.skills)
    )
    return review
