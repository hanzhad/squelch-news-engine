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
from ..core.settings import Settings, get_settings
from ..github.client import GitHubClient, GitHubError
from ..github.issues import IssueStore
from ..github.labels import ensure_labels, label_specs
from ..github.ledger import rebuild
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
    _require(settings, "DISCORD_WEBHOOK_URL")

    from ..publishers.discord import publish_ready

    with _store(settings) as store:
        sent = publish_ready(settings, store)
    log.info("publish done: %d articles sent", sent)


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

    with _store(settings) as store:
        closed = store.close_delivered(config.required_channels)
    log.info("close done: %d article(s) published", len(closed))


@app.command()
def digest(
    days: Annotated[
        int,
        typer.Option(min=1, help="Summarise articles published in this many past days."),
    ] = 7,
) -> None:
    """Summarise the week's published articles and post the roundup to Discord."""
    settings = _boot()
    config = _config()
    _require(settings, "GEMINI_API_KEY")
    if not settings.digest_webhook_url:
        # Either webhook will do: the digest channel is optional and falls back
        # to the feed's.
        log.error("DISCORD_WEBHOOK_URL is not set")
        raise typer.Exit(1)

    from ..llm.digest import DigestError, build_digest
    from ..publishers.discord import post_digest

    try:
        with _store(settings) as store:
            result = build_digest(settings, config, store, days=days)
    except DigestError as exc:
        # Distinct from a quiet week on purpose: this one has to go red, or a
        # digest that silently never arrives looks exactly like a quiet week.
        log.error("could not build the digest: %s", exc)
        raise typer.Exit(1) from exc

    if result is None:
        # A quiet week is a normal outcome, not a failed run.
        log.info("nothing published in the last %d days, no digest to send", days)
        return

    post_digest(settings, result)
    log.info("digest done: %s", result.headline)


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
