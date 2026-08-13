"""The look back over what the feed actually published — daily, and weekly.

One call, one digest: the value is in seeing the window as a whole, which is
exactly what per-article calls cannot produce. What reaches the prompt is
decided by ``digest`` in feed.yaml, not here — see ``_selected`` for how the
budget and the score threshold combine. Links come back from a language model,
so anything the model did not read here is dropped before the digest reaches a
publisher.

The two periods are one code path and two prompt files. What a daily roundup
should say and what a weekly one should say is a question about writing, and it
is answered in ``prompts/digest-*.md`` rather than by a branch in here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from ..core.config import Config, DigestPolicy
from ..core.log import get_logger
from ..core.models import Digest, DigestEntry, Period
from ..core.settings import Settings
from ..core.urls import canonicalize
from ..github.digests import DigestStore
from ..github.issues import IssueRecord, IssueStore
from . import prompts
from .gemini import GeminiClient

log = get_logger(__name__)

# Not policy — policy is `digest` in feed.yaml. This is the last-resort bound on
# one HTTP request, for the catch-up run that reaches back over a fortnight of
# backlog. It is far above anything the shipped settings produce, and it says so
# in the log when it bites, because a cap that trims in silence reads as
# "covered everything" when it did not.
HARD_MAX_ARTICLES = 200


class DigestError(RuntimeError):
    """The digest could not be written — as opposed to there being nothing to write."""


class BuiltDigest(NamedTuple):
    """A roundup and the shape of the window it came out of.

    The counts travel with it because they are stored on the issue and shown in
    its body: "read 105 articles over 7 days" is how you tell a thin roundup
    from a thin week.
    """

    digest: Digest
    articles: int
    days: int
    # Highlight URL -> the feed channel's post about that article, where one
    # exists. Resolved here rather than asked of the model, which must never be
    # in a position to write a Discord link.
    links: dict[str, str] = {}


def build_digest(
    settings: Settings,
    config: Config,
    store: IssueStore,
    period: Period = Period.WEEKLY,
    days: int | None = None,
) -> BuiltDigest | None:
    """Summarise the published articles this ``period`` looks back over.

    ``days`` overrides the period's own window without changing which roundup
    is being written — that is for catching up by hand after an outage, and it
    is why neither prompt file states its window in words.

    Returns None only when there was genuinely nothing to summarise — a quiet
    day is a normal outcome, and so is a quiet week. A digest that could not be
    *written* raises instead: those two look identical from the outside and must
    not, or a rate-limited run reports "nothing published" on a week that
    published plenty, and reports it in green.
    """
    window = days or period.days
    since = datetime.now(UTC) - timedelta(days=window)
    issues = store.list_published_since(since)
    if not issues:
        log.info("nothing published in the last %d days", window)
        return None

    ranked = _selected(issues, config.digest)
    if len(ranked) < 1:
        log.info("nothing left after the digest policy, no %s to write", period.label)
        return None

    model = settings.gemini_model or prompts.load_models().digest
    client = GeminiClient(settings, model, timeout=settings.digest_request_timeout)
    digest = client.structured(
        prompts.digest_prompt(config, period, window, ranked),
        Digest,
        prompts.system(period.stage),
    )
    if digest is None:
        # GeminiClient has already retried and logged why.
        raise DigestError(f"{model} did not return a {period.label} for the last {window} days")

    digest = digest.model_copy(update={"highlights": _verified_highlights(digest, ranked)})
    log.info(
        "%s ready: %d trends, %d highlights over %d articles",
        period.label,
        len(digest.trends),
        len(digest.highlights),
        len(ranked),
    )
    return BuiltDigest(
        digest, len(ranked), window, _feed_links(digest, ranked, config.digest.link_channel)
    )


def _feed_links(digest: Digest, issues: list[IssueRecord], channel: str) -> dict[str, str]:
    """Where each highlight should point: the feed's post about that article.

    A roundup is the way into the article-by-article channel, not a substitute
    for it, so a highlight lands on the message there — with whatever
    discussion has already gathered under it — rather than straight on the
    publisher's page.

    Resolved from what the delivery pass recorded, never asked of the model. An
    article the feed has not carried yet, or one delivered before message links
    were being stored, simply keeps its own URL.
    """
    if not channel:
        return {}
    by_article = {
        canonicalize(url): link
        for url, link in (
            (
                issue.url or issue.html_url,
                str(issue.delivery(channel).get("message_url") or ""),
            )
            for issue in issues
        )
        if url and link
    }
    resolved = {
        entry.url: by_article[canonicalize(entry.url)]
        for entry in digest.highlights
        if canonicalize(entry.url) in by_article
    }
    if len(resolved) < len(digest.highlights):
        log.info(
            "%d of %d highlights link to the feed; the rest point at the article",
            len(resolved),
            len(digest.highlights),
        )
    return resolved


def run_digest(
    settings: Settings,
    config: Config,
    store: IssueStore,
    digests: DigestStore,
    period: Period = Period.WEEKLY,
    days: int | None = None,
) -> IssueRecord | None:
    """Write the roundup and store it as an issue for the delivery stage.

    Storing rather than posting is the whole point of the split: the model's
    answer survives a webhook that is down, a roundup can be read and edited
    before it goes out, and the message id recorded against it later is what
    stops a re-run posting a second copy.

    Returns None when there was nothing to write, or when today's roundup of
    this period already exists — a workflow dispatched by hand on a morning the
    cron already ran must not queue a duplicate.

    An explicit ``days`` lifts that guard, because it is the one case where a
    second roundup on the same day is the whole intention: catching up over a
    backlog after an outage. Without this the catch-up input would report
    success and do nothing, which is the failure the guard was meant to prevent
    in the first place, pointed the other way.
    """
    today = datetime.now(UTC).date()
    already = None if days else digests.built_on(period, today)
    if already is not None:
        log.info(
            "#%d is already today's %s, not writing another — pass --days to override",
            already.number,
            period.label,
        )
        return None

    built = build_digest(settings, config, store, period, days=days)
    if built is None:
        return None
    return digests.create(
        built.digest,
        period,
        built.days,
        built.articles,
        built_on=today,
        links=built.links,
    )


def _selected(issues: list[IssueRecord], policy: DigestPolicy) -> list[IssueRecord]:
    """The articles this roundup gets to read, best-scored first.

    Two rules, and the second is the point. Everything down to
    ``policy.max_articles`` is in. Past that an article is still in if it scored
    ``policy.min_score`` or better — so the cap sets the size the roundup aims
    for rather than a hard edge, and no article is dropped for its position
    alone. With the shipped ``min_score: 0`` nothing is dropped at all.

    Ranking by score and cutting the tail is deliberate the other way round
    too: when a window really does have to lose something, it loses its filler
    rather than its headline. An article with no score — opened by hand, never
    classified — ranks as zero and is therefore the first to go, which is the
    only defensible place for something nobody judged.
    """
    ranked = sorted(issues, key=_score, reverse=True)
    kept = [
        issue
        for position, issue in enumerate(ranked)
        if position < policy.max_articles or _score(issue) >= policy.min_score
    ]
    if len(kept) < len(ranked):
        log.info(
            "digesting %d of %d articles: %d past the first %d scored under %d",
            len(kept),
            len(ranked),
            len(ranked) - len(kept),
            policy.max_articles,
            policy.min_score,
        )
    if len(kept) > HARD_MAX_ARTICLES:
        log.warning(
            "%d articles is past what one request should carry, digesting the top %d",
            len(kept),
            HARD_MAX_ARTICLES,
        )
        kept = kept[:HARD_MAX_ARTICLES]
    return kept


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
