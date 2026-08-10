"""The published issues, rendered as an archive anyone can read or subscribe to.

Discord is where the feed is read, but a chat channel is not an archive: it
scrolls away, it is behind a login, and nothing outside it can subscribe. This
module turns the same issues into a single static page plus an RSS feed, both of
which GitHub Pages can serve straight out of the repository.

One page, not one page per article. The summaries are a few sentences and the
issue itself is already a permanent, linkable page for each story, so per-article
files would add URLs that have to stay stable forever while carrying nothing the
index does not already show.

A rolling window, not the complete history. Published issues accumulate forever,
and this runs after every write-up, so reading all of them would make each
rebuild slower than the last. The permanent record is the issue tracker; this is
the shop window onto it.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from feedgen.feed import FeedGenerator
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field

from ..core.config import Config
from ..core.log import get_logger
from ..core.models import Status
from ..core.settings import Settings
from ..github.issues import IssueRecord, IssueStore

log = get_logger(__name__)

TEMPLATES_DIR = Path("site/templates")
INDEX_TEMPLATE = "index.html.j2"
STYLESHEET = "style.css"
FEED_FILE = "rss.xml"
# Two delivery channels, not one: the page carries the whole archive, the feed
# carries the newest entries that have a link. An article can reach one and not
# the other, which is exactly why they are counted apart.
PAGE_CHANNEL = "site"
FEED_CHANNEL = "rss"
# How many entries a reader is served at once. A soft limit — see
# _feed_selection, which stretches it rather than let an article go unannounced.
FEED_LIMIT = 50
# Env override for a custom domain, so Settings does not grow a field that only
# the site cares about.
SITE_URL_ENV = "AETHERFEED_SITE_URL"


class Article(BaseModel):
    """One published issue, flattened into what the templates and feed need."""

    number: int
    title: str
    url: str
    issue_url: str = ""
    source: str = ""
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    score: int = 0
    published: datetime


# -- reading ----------------------------------------------------------------


def _to_datetime(value: Any) -> datetime | None:
    """Parse whatever the metadata block happens to hold, as an aware datetime."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _published_at(issue: IssueRecord) -> datetime:
    """Best available publication time, always timezone-aware.

    RSS pubDate needs an offset and sorting needs a total order, so the
    fallbacks run all the way down to when the issue was created.
    """
    candidates = (
        issue.meta.get("published_at"),
        issue.delivery(PAGE_CHANNEL).get("at"),
        issue.created_at,
    )
    for candidate in candidates:
        parsed = _to_datetime(candidate)
        if parsed:
            return parsed
    return datetime.fromtimestamp(0, tz=UTC)


def _as_article(issue: IssueRecord) -> Article:
    score = issue.meta.get("score")
    return Article(
        number=issue.number,
        title=issue.title,
        # Falling back to the issue keeps hand-opened entries linkable.
        url=issue.url or issue.html_url,
        issue_url=issue.html_url,
        source=issue.source,
        summary=issue.summary,
        tags=[str(tag) for tag in issue.meta.get("tags") or [] if tag],
        score=int(score) if isinstance(score, int | float) else 0,
        published=_published_at(issue),
    )


def site_base_url(settings: Settings) -> str:
    """Absolute root the feed links against, with a trailing slash.

    Feed entries outlive the page they were generated from, so nothing in the
    feed may be relative. Settings knows the repository but not where it is
    served, so default to the GitHub Pages address that repository would get.
    """
    override = os.environ.get(SITE_URL_ENV, "").strip()
    if override:
        return override.rstrip("/") + "/"
    if "/" in settings.github_repository:
        return f"https://{settings.repo_owner}.github.io/{settings.repo_name}/"
    return "http://localhost:8000/"


def _human_date(value: datetime) -> str:
    # Explicit day number because %-d is not portable and %d looks like a form.
    return f"{value.day} {value:%b %Y}"


# -- writing ----------------------------------------------------------------


def _write_feed(
    articles: list[Article], title: str, tagline: str, base_url: str, path: Path
) -> set[int]:
    """Write the feed and report which articles actually made it into it."""
    generator = FeedGenerator()
    generator.id(base_url)
    generator.title(title)
    generator.link(href=base_url, rel="alternate")
    generator.link(href=base_url + FEED_FILE, rel="self")
    generator.description(tagline or title)
    generator.language("en")
    generator.lastBuildDate(datetime.now(UTC))

    included: set[int] = set()
    for article in articles:
        if not article.url:
            # Without a URL there is no stable id, and an entry without one gets
            # re-announced on every build.
            log.warning("#%d has no link, left out of the feed", article.number)
            continue
        # Appended rather than prepended: articles already arrive newest-first.
        entry = generator.add_entry(order="append")
        # The article URL is the only id that survives the issue being renumbered
        # or the site moving, so readers never re-announce an old story.
        entry.guid(article.url, permalink=True)
        entry.title(article.title)
        entry.link(href=article.url)
        entry.description(article.summary or article.title)
        entry.pubDate(article.published)
        if article.tags:
            entry.category([{"term": tag} for tag in article.tags])
        included.add(article.number)

    path.write_bytes(generator.rss_str(pretty=True))
    return included


def _feed_selection(articles: list[Article], issues: list[IssueRecord]) -> list[Article]:
    """The newest entries, plus anything ready the feed has never carried.

    The window is what a reader wants. The extras are what correctness needs:
    while the pipeline is catching up there can be more than a window's worth of
    ready articles at once, and one that slipped past the cut before it was ever
    announced would wait on the feed forever — and, because the feed is a
    delivery channel, never finish delivering and never close.
    """
    never_announced = {
        issue.number
        for issue in issues
        if issue.status is Status.READY and FEED_CHANNEL not in issue.delivered_to
    }
    head = articles[:FEED_LIMIT]
    tail = [article for article in articles[FEED_LIMIT:] if article.number in never_announced]
    if tail:
        log.info("feed carries %d extra entry(s) that it never announced", len(tail))
    # Both halves come from the same date-sorted list, so the feed stays
    # newest-first.
    return head + tail


def _record(store: IssueStore, issues: list[IssueRecord], in_feed: set[int]) -> None:
    """Mark the page and the feed as having delivered what they just rendered.

    Recorded at build time rather than after deployment, which is safe because
    both outputs are re-rendered whole on every build: an article stays in the
    archive whether or not its label ever landed, so a failed deploy costs a
    delay, never a lost article.
    """
    for issue in issues:
        if issue.status is not Status.READY:
            continue
        if PAGE_CHANNEL not in issue.delivered_to:
            store.record_delivery(issue, PAGE_CHANNEL)
        if issue.number in in_feed and FEED_CHANNEL not in issue.delivered_to:
            store.record_delivery(issue, FEED_CHANNEL)


def build_site(
    settings: Settings,
    config: Config,
    store: IssueStore,
    out_dir: Path,
    record: bool = True,
) -> None:
    """Render the last ``feed_window_days`` of articles into ``out_dir``.

    With ``record`` off nothing is written back to GitHub, which is what you
    want when rendering locally to look at the page.
    """
    issues = store.list_in_feed(
        since=datetime.now(UTC) - timedelta(days=settings.feed_window_days)
    )
    articles = sorted(
        (_as_article(issue) for issue in issues),
        key=lambda article: article.published,
        reverse=True,
    )
    # `focus` is a whole editorial policy; only its opening paragraph reads as a
    # tagline, and the rest belongs in the prompt, not on the page.
    tagline = " ".join(config.focus.strip().split("\n\n")[0].split())
    base_url = site_base_url(settings)
    out_dir.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["human_date"] = _human_date

    page = env.get_template(INDEX_TEMPLATE).render(
        title=config.title,
        tagline=tagline,
        topics=config.topics,
        articles=articles,
        stylesheet=STYLESHEET,
        feed_url=base_url + FEED_FILE,
        repo_url=(
            f"https://github.com/{settings.github_repository}"
            if "/" in settings.github_repository
            else ""
        ),
        built_at=datetime.now(UTC),
    )
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    in_feed = _write_feed(
        _feed_selection(articles, issues), config.title, tagline, base_url, out_dir / FEED_FILE
    )
    shutil.copyfile(TEMPLATES_DIR / STYLESHEET, out_dir / STYLESHEET)
    # Pages runs the output through Jekyll unless this file exists, which
    # silently drops anything it does not recognise.
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    log.info("wrote %d article(s) to %s", len(articles), out_dir)
    if record:
        _record(store, issues, in_feed)
