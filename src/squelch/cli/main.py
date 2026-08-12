"""Command line entry point — one subcommand per pipeline stage.

Each stage runs as its own scheduled workflow, in its own process, so this is
where credentials are checked and where a failure becomes a non-zero exit that
paints the Actions run red. "Nothing to do" is not a failure: an empty scrape or
a quiet week must leave the run green, or the red badge stops meaning anything.

Modules that need an optional credential (Gemini, Discord) or that pull in a
heavy dependency tree (the site builder) are imported *inside* the command that
uses them. A repository with no DISCORD_WEBHOOK_URL must still be able to
scrape, and an import error in the digest code must not break `bootstrap-labels`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from ..core.config import FEED_PATH, Config, load_config
from ..core.log import get_logger, setup_logging
from ..core.models import Period
from ..core.settings import Settings, get_settings
from ..github.client import GitHubClient, GitHubError
from ..github.issues import IssueStore
from ..github.labels import ensure_labels, label_specs
from ..github.ledger import rebuild
from ..scrapers.health import check_sources, report
from ..scrapers.runner import run_scrape

log = get_logger(__name__)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Serverless news pipeline. GitHub Issues are the database, labels are the state.",
)


class SourceType(StrEnum):
    """The scraper families, exposed as choices for --type."""

    RSS = "rss"
    HTML = "html"
    WEB = "web"
    GITHUB = "github"


# -- shared prologue ---------------------------------------------------------


def _boot() -> Settings:
    """Every command starts here: logging first, so failures below are visible."""
    setup_logging()
    return get_settings()


def _config() -> Config:
    """Load the source catalogue, or fail the run — a broken catalogue is fatal."""
    try:
        return load_config()
    except Exception as exc:  # noqa: BLE001 - yaml, IO and validation all mean the same thing here
        log.error("could not load config from %s and friends: %s", FEED_PATH.parent, exc)
        raise typer.Exit(1) from exc


def _require(settings: Settings, name: str) -> None:
    """Fail fast on a missing credential instead of dying deep inside a stage."""
    if not getattr(settings, name.lower(), ""):
        log.error("%s is not set", name)
        raise typer.Exit(1)


@contextmanager
def _client(settings: Settings) -> Iterator[GitHubClient]:
    """A GitHub client whose errors end the process.

    This wraps the whole command body on purpose: a GitHubError raised halfway
    through a stage means GitHub is unreachable or the token is wrong, and both
    deserve a red run rather than a quietly truncated one.
    """
    try:
        with GitHubClient(settings) as client:
            yield client
    except GitHubError as exc:
        log.error("github: %s", exc)
        raise typer.Exit(1) from exc


@contextmanager
def _store(settings: Settings) -> Iterator[IssueStore]:
    with _client(settings) as client:
        yield IssueStore(client)


# -- commands ----------------------------------------------------------------


@app.command()
def scrape(
    type_: Annotated[
        SourceType | None,
        typer.Option("--type", help="Only run sources of this type."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Collect and log what would be created, write nothing."),
    ] = False,
) -> None:
    """Collect articles from the configured sources and open an issue for each new one."""
    settings = _boot()
    config = _config()
    only_type = type_.value if type_ else None

    if dry_run:
        # Nothing is written, so no token is needed — this stays usable locally.
        run_scrape(settings, config, None, only_type=only_type, dry_run=True)
        return

    with _store(settings) as store:
        created = run_scrape(settings, config, store, only_type=only_type)
    log.info("scrape done: %d issues created", created)


@app.command()
def classify() -> None:
    """Decide which raw issues belong in the feed. Cheap model, no prose."""
    settings = _boot()
    config = _config()
    _require(settings, "GEMINI_API_KEY")

    from ..llm.classify import run_classify

    with _store(settings) as store:
        kept, rejected = run_classify(settings, config, store)
    log.info("classify done: %d kept, %d rejected", kept, rejected)


@app.command()
def summarize() -> None:
    """Write up the issues that survived classification."""
    settings = _boot()
    config = _config()
    _require(settings, "GEMINI_API_KEY")

    from ..llm.summarize import run_summarize

    with _store(settings) as store:
        written = run_summarize(settings, config, store)
    log.info("summarize done: %d articles ready", written)


@app.command()
def publish() -> None:
    """Send ready articles Discord has not seen yet and mark them delivered."""
    settings = _boot()
    config = _config()
    _require(settings, "DISCORD_WEBHOOK_URL")

    from ..publishers.discord import DiscordError, publish_ready

    try:
        with _store(settings) as store:
            sent = publish_ready(settings, config, store)
    except DiscordError as exc:
        # A misconfigured webhook is a setup problem, not a crash to read a
        # traceback for.
        log.error("discord: %s", exc)
        raise typer.Exit(1) from exc
    log.info("publish done: %d articles sent", sent)


@app.command("publish-rejected")
def publish_rejected_cmd() -> None:
    """Post recently rejected articles, with their reasons, to their own Discord channel."""
    settings = _boot()
    config = _config()

    from ..publishers.discord import REJECTED_CHANNEL, DiscordError, publish_rejected

    if config.channel(REJECTED_CHANNEL).enabled:
        # Checked here so a forgotten secret fails the run visibly instead of
        # the channel silently never appearing.
        _require(settings, "DISCORD_REJECTED_WEBHOOK_URL")

    try:
        with _store(settings) as store:
            sent = publish_rejected(settings, config, store)
    except DiscordError as exc:
        log.error("discord: %s", exc)
        raise typer.Exit(1) from exc
    log.info("publish-rejected done: %d article(s) posted", sent)


@app.command()
def rescue() -> None:
    """Reopen rejected articles the community voted back with 👍 reactions.

    A vote is the same human override as flipping the label by hand: the
    article returns as status:2-relevant, so the summariser — not the
    classifier that already rejected it once — picks it up next.
    """
    settings = _boot()

    from ..github.rescue import run_rescue

    with _store(settings) as store:
        rescued = run_rescue(settings, store)
    log.info("rescue done: %d article(s) back in the pipeline", rescued)


@app.command("close-delivered")
def close_delivered() -> None:
    """Close ready articles that every enabled channel has delivered.

    Its own stage on purpose. If the channel that happened to deliver last also
    had to close the issue, a crash in that window would strand the article
    open forever — every channel has had it, so no queue would contain it
    again. This pass re-derives the answer from the labels each time instead.
    """
    settings = _boot()
    config = _config()

    from ..github.digests import DigestStore

    with _store(settings) as store:
        closed = store.close_delivered(config.ready_channels)
        # Roundups close the same way and for the same reason, on their own
        # queue: the publisher never closes, so a channel dying between its
        # label and the close cannot strand one.
        posted = DigestStore(store.client).close_delivered(config.digest_channels)
    log.info(
        "close done: %d article(s) published, %d roundup(s) closed", len(closed), len(posted)
    )


@app.command()
def digest(
    period: Annotated[
        Period,
        typer.Option("--period", help="Which roundup to write: the day, or the week."),
    ] = Period.WEEKLY,
    days: Annotated[
        int | None,
        typer.Option(
            min=1,
            help="Look back this many days instead of the period's own window, for catching up.",
        ),
    ] = None,
) -> None:
    """Write the roundup over what the feed published and store it as an issue.

    Writing and posting are separate stages on purpose. This one touches no
    webhook: it puts the model's answer in the database, where it survives a
    Discord outage and can be read — or edited — before `publish-digest` sends
    it out.
    """
    settings = _boot()
    config = _config()
    _require(settings, "GEMINI_API_KEY")

    from ..github.digests import DigestStore
    from ..llm.digest import DigestError, run_digest

    window = days or period.days
    try:
        with _store(settings) as store:
            issue = run_digest(
                settings, config, store, DigestStore(store.client), period, days=days
            )
    except DigestError as exc:
        # Distinct from a quiet window on purpose: this one has to go red, or a
        # digest that silently never arrives looks exactly like a quiet week.
        log.error("could not build the %s: %s", period.label, exc)
        raise typer.Exit(1) from exc

    if issue is None:
        # A quiet day is a normal outcome, not a failed run — and with a daily
        # roundup it is a common one.
        log.info("no %s written for the last %d days", period.label, window)
        return
    log.info("%s stored as #%d: %s", period.label, issue.number, issue.title)


@app.command("publish-digest")
def publish_digest_cmd() -> None:
    """Post the roundups waiting in the queue to the digest channel."""
    settings = _boot()
    config = _config()

    from ..github.digests import DigestStore
    from ..publishers.discord import DiscordError, publish_digests

    if config.digest_channels:
        # Checked here so a forgotten secret fails the run visibly instead of
        # roundups piling up unposted with nothing saying why.
        _require(settings, "DISCORD_DIGEST_WEBHOOK_URL")

    try:
        with _store(settings) as store:
            sent = publish_digests(settings, config, DigestStore(store.client))
    except DiscordError as exc:
        log.error("discord: %s", exc)
        raise typer.Exit(1) from exc
    log.info("publish-digest done: %d roundup(s) posted", sent)


@app.command("build-site")
def build_site_cmd(
    out: Annotated[
        Path,
        typer.Option("--out", help="Directory to write the static site into."),
    ] = Path("site/out"),
    record: Annotated[
        bool,
        typer.Option(
            "--record/--no-record",
            help="Mark the rendered articles as delivered to the site and the feed.",
        ),
    ] = True,
) -> None:
    """Render the published archive as a static site."""
    settings = _boot()
    config = _config()

    from ..site.build import build_site

    with _store(settings) as store:
        build_site(settings, config, store, out, record=record)
    log.info("site written to %s", out)


@app.command("check-sources")
def check_sources_cmd(
    type_: Annotated[
        SourceType | None,
        typer.Option("--type", help="Only check sources of this type."),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Only check this source id, for iterating on a selector."),
    ] = None,
) -> None:
    """Ask every enabled source for a couple of articles and fail if one has nothing.

    The one command here that treats an empty result as an error. A scrape must
    stay green when a source has nothing new; this exists precisely so that a
    source with nothing *at all* — a moved feed, a selector that stopped
    matching — stops being invisible. Touches no credentials and writes nothing.
    """
    settings = _boot()
    config = _config()

    results = check_sources(config, settings, type_.value if type_ else None, source)
    if not results:
        log.error("no enabled source matched")
        raise typer.Exit(1)
    report(results)

    broken = [result for result in results if not result.ok]
    if broken:
        log.error(
            "%d of %d sources are broken: %s",
            len(broken),
            len(results),
            ", ".join(result.source for result in broken),
        )
        raise typer.Exit(1)
    log.info("all %d sources still work", len(results))


@app.command("bootstrap-labels")
def bootstrap_labels() -> None:
    """Create or repair the labels that make up the state machine. Safe to re-run."""
    settings = _boot()
    config = _config()
    specs = label_specs(config)

    with _client(settings) as client:
        ensure_labels(client, specs)
    log.info("bootstrap done: %d labels reconciled", len(specs))


@app.command("rebuild-ledger")
def rebuild_ledger() -> None:
    """Rebuild the dedup ledger from the uids stored in the issues themselves.

    The ledger is a cache; the issues are the record. Use this if the ledger
    issue is lost or edited into nonsense. Pages through the whole repository,
    so it is a manual repair, not something to schedule.
    """
    settings = _boot()

    with _store(settings) as store:
        count = rebuild(store.client, store, max_entries=settings.seen_max_entries)
    log.info("ledger rebuilt with %d uids", count)


if __name__ == "__main__":
    app()
