"""The weekly look back over what the feed actually published.

One call, one digest: the value is in seeing the week as a whole, which is
exactly what per-article calls cannot produce. The article list going into the
prompt is capped and ranked by the score the filter already assigned, so a busy
week loses its filler rather than its headline. Links come back from a language
model, so anything the model did not read here is dropped before the digest
reaches a publisher.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..core.config import Config
from ..core.log import get_logger
from ..core.models import Digest, DigestEntry
from ..core.settings import Settings
from ..core.urls import canonicalize
from ..github.issues import IssueRecord, IssueStore
from . import prompts
from .gemini import GeminiClient

log = get_logger(__name__)

MAX_DIGEST_ARTICLES = 40


class DigestError(RuntimeError):
    """The digest could not be written — as opposed to there being nothing to write."""


def build_digest(
    settings: Settings,
    config: Config,
    store: IssueStore,
    days: int = 7,
) -> Digest | None:
    """Summarise the last ``days`` of published articles.

    Returns None only when there was genuinely nothing to summarise — a quiet
    week is a normal outcome. A digest that could not be *written* raises
    instead: those two look identical from the outside and must not, or a
    rate-limited run reports "nothing published" on a week that published
    plenty, and reports it in green.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    issues = store.list_published_since(since)
    if not issues:
        log.info("nothing published in the last %d days", days)
        return None

    ranked = sorted(issues, key=_score, reverse=True)[:MAX_DIGEST_ARTICLES]
    if len(ranked) < len(issues):
        log.info("digesting the top %d of %d published articles", len(ranked), len(issues))

    model = settings.gemini_model or prompts.load_models().digest
    client = GeminiClient(settings, model)
    digest = client.structured(
        prompts.digest_prompt(config, days, ranked), Digest, prompts.system("digest")
    )
    if digest is None:
        # GeminiClient has already retried and logged why.
        raise DigestError(f"{model} did not return a digest for the last {days} days")

    digest = digest.model_copy(update={"highlights": _verified_highlights(digest, ranked)})
    log.info("digest ready: %d trends, %d highlights", len(digest.trends), len(digest.highlights))
    return digest


def _score(issue: IssueRecord) -> int:
    try:
        return int(issue.meta.get("score", 0))
    except (TypeError, ValueError):
        return 0


def _verified_highlights(digest: Digest, issues: list[IssueRecord]) -> list[DigestEntry]:
    """Drop highlights whose link is not one of the articles we supplied."""
    known = {canonicalize(url) for url in (i.url or i.html_url for i in issues) if url}
    kept: list[DigestEntry] = []
    for entry in digest.highlights:
        if entry.url and canonicalize(entry.url) in known:
            kept.append(entry)
        else:
            log.warning("dropping highlight with unknown link: %s", entry.url)
    return kept
