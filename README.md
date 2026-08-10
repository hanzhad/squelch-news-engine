# Squelch

A serverless news pipeline that lives entirely inside one GitHub repository: it collects
articles from RSS feeds and websites, sifts them through Gemini, and publishes the survivors to
Discord and to a static archive on GitHub Pages. No database, no server, no Docker — just
GitHub Actions on a schedule.

**Live archive: [hanzhad.github.io/squelch-news-engine](https://hanzhad.github.io/squelch-news-engine/)**
· [RSS](https://hanzhad.github.io/squelch-news-engine/rss.xml)
· [the queue itself](https://github.com/hanzhad/squelch-news-engine/issues)

## The idea: Issues are the database

One article is one issue. Metadata lives in a YAML block inside an HTML comment at the top of
the issue body: GitHub does not render it, and we know how to read and rewrite it. Labels are
the state machine, and they — not some external table — are the source of truth about where an
article stands.

```
status:1-raw  ──filter──▶  status:2-ready  ──publish──▶  status:3-published
      │
      └──filter──▶  status:rejected   (issue closed as not planned)
```

Issues additionally carry `source:<id>` (where it came from) and `topic:<tag>` (what it is
about — the LLM picks tags from the list in the config).

What this buys for free:

- **Reviewing and editing by hand.** The issue list is the admin panel. Disagree with the LLM's
  verdict? Change the label, and the next run picks the article up in its new state.
- **History.** Every issue has an edit timeline, and the repository has a git history.
- **Submissions from people.** An issue opened by hand has neither a metadata block nor an
  original-text marker, so the pipeline takes the whole issue body as the article text.
- **Zero infrastructure.** Storage, authentication and scheduling are GitHub's problem.

Deduplication lives in Issues too, but not through search. Storing a uid is free — every
article issue already carries one — while asking *"has any issue ever carried uid X"* is not:
the search API lags writes and allows 30 requests a minute, and paginating the whole archive
gets slower every week as rejected articles accumulate. So the whole set of seen uids lives in
the body of one closed issue labelled `meta:ledger`. One GET loads it, one PATCH saves it, and
the cost never grows with the archive. A uid is a hash of the canonicalized URL
(`src/squelch/core/urls.py`): utm parameters, `fbclid`, `www.`, a default port, the fragment
and query-parameter order do not affect it, so the same article arriving from three different
feeds yields one uid.

## The four pipelines

Each pipeline is its own workflow in `.github/workflows/`, triggered by cron and manually via
**Actions → Run workflow**.

| Pipeline | Workflow | What it does | Schedule |
| --- | --- | --- | --- |
| Scrape | `scrape.yml` | Walks the enabled sources, drops already-seen uids, opens issues labelled `status:1-raw`, updates the ledger issue | `0,30 * * * *` |
| Filter | `filter.yml` | Sends each raw issue to Gemini together with `focus` from the config and gets a verdict: `status:2-ready` plus a summary and tags, or `status:rejected` and closure | `10,40 * * * *` |
| Publish | `publish.yml` | Sends ready articles to the Discord webhook and moves them to `status:3-published` | `5,20,35,50 * * * *` |
| Digest | `digest.yml` | Builds a weekly roundup with trends out of what was published, then closes issues past the retention window | `0 9 * * 1` |

The offsets are chosen so the filter always finds the issues the scraper created ten minutes
earlier, and so publishing never shares a slot with either.

There is also `pages.yml` — it renders the static archive from published issues
(`site/templates/` → `site/out/`) and deploys it to GitHub Pages.

None of the pipelines tries to do all the work in one run: scraping, filtering and publishing
each cap their batch and leave the remainder for the next tick. This is deliberate — GitHub
throttles bulk content creation, and the free Gemini tier counts requests per minute.

## Quick start

1. **Create the repository** — fork this one or start a new one from its contents. Keep it
   **public**: that way GitHub Actions minutes are not billed and GitHub Pages is available on
   the free plan.

2. **Add the secrets** under *Settings → Secrets and variables → Actions → New repository
   secret*:
   - `GEMINI_API_KEY` — a key from Google AI Studio;
   - `DISCORD_WEBHOOK_URL` — the webhook of the target channel (*Channel settings →
     Integrations → Webhooks*).

   `GITHUB_TOKEN` and `GITHUB_REPOSITORY` are supplied by Actions itself; do not create them.

3. **Let workflows write to the repository.** *Settings → Actions → General → Workflow
   permissions → Read and write permissions*. Every stage edits issues, and a read-only token
   caps what a workflow can ask for no matter what its own `permissions:` block says.

4. **Create the labels once.** Labels are the state machine, and they need meaningful colours
   and descriptions — otherwise GitHub invents them on first use:

   ```bash
   pip install -e .
   export GITHUB_TOKEN=<personal access token with issues access to the repo>
   export GITHUB_REPOSITORY=<owner/repo>
   squelch bootstrap-labels
   ```

   The command is idempotent: run it again after editing `topics` or the source list in the
   config. Alternatively run it from Actions, if the workflow has a manual trigger.

5. **Enable GitHub Pages.** *Settings → Pages → Source → **GitHub Actions*** (not "Deploy from
   a branch"). The archive appears after the first successful `pages.yml` run.

6. **Edit `config/sources.yaml`** for your subject (see below) and run `scrape.yml` by hand to
   see what comes out.

## Configuration: `config/sources.yaml`

All the editorial judgement lives in one file. No code changes needed.

### `focus` — the main knob

The `focus` text goes into the filter prompt verbatim, so it decides what survives at all.
Write it in plain language rather than keywords, and always as two lists — what to let through
and what to cut. A vague `focus` ("interesting tech stuff") produces a feed full of noise; a
specific one behaves predictably.

```yaml
focus: |
  Substantive news about AI, software engineering and the infrastructure around them.

  Let through: model and tool releases with concrete capabilities or pricing, technical
  write-ups with real detail, security incidents, honest negative results.

  Filter out: speculation with no new facts, listicles, course promotion, repackaged
  press releases.
```

If the feed lets junk through, fix `focus` — not a threshold, and not the code.

### `topics` — the bounds of the label set

The LLM may only apply tags from this list, which keeps the set of `topic:*` labels in the
repository finite. After adding a topic, run `squelch bootstrap-labels` so the label appears
with a proper colour.

### Adding an RSS source

```yaml
sources:
  - id: lwn                      # becomes the label source:lwn — ASCII, no spaces
    type: rss
    url: https://lwn.net/headlines/newrss
    max_items: 5                 # hard cap per run
    fetch_full_text: true        # follow the link for the full text (+1 HTTP request)
    enabled: true                # can be switched off without deleting
```

`fetch_full_text: true` earns its keep where the feed carries only a teaser: the full text is
used if it turns out longer than what the feed gave. For sources that publish full text in the
feed, switch it off and save the requests.

### Adding a site with no feed

```yaml
  - id: example-listing
    type: web
    url: https://example.com/news
    link_selector: "article h2 a"   # CSS selector for article links on the listing page
    max_items: 5
```

The `web` type needs the optional Playwright extra:

```bash
pip install -e '.[web]'
playwright install chromium
```

This scraper is deliberately primitive: a listing page plus a link selector. Pagination, logins
and infinite scroll do not fit here — those call for a purpose-built scraper.

### The rest

`max_body_chars` is how far the article text is truncated before it goes to the LLM. More
characters mean slower and more expensive calls; 8000 is usually enough.

`title` is the public name of the feed — masthead, RSS channel and digest header. It is kept
separate from the repository name so the brand can change without renaming the engine.

## Local development

```bash
pip install -e '.[dev]'

pytest                 # tests run offline: feed fixtures and fake HTTP clients
ruff check .           # lint (line-length 100, py312)
ruff check . --fix
```

CLI commands (the console script from `pyproject.toml` is `squelch`):

| Command | Purpose |
| --- | --- |
| `squelch scrape` | walk the sources and open issues |
| `squelch filter` | run raw issues through the LLM |
| `squelch publish` | send ready articles to Discord |
| `squelch digest` | build the weekly digest |
| `squelch build-site` | render the static archive |
| `squelch bootstrap-labels` | create or repair labels from the config |
| `squelch retention` | close old published issues |

Before running for real, see what a command intends to do without writing anything:

```bash
squelch scrape --dry-run
```

For the exact flags of any command, use `squelch <command> --help`.

Environment variables are read through pydantic-settings, so a local `.env` file is convenient
(it is in `.gitignore`):

```
GITHUB_TOKEN=...
GITHUB_REPOSITORY=owner/repo
GEMINI_API_KEY=...
DISCORD_WEBHOOK_URL=...
```

The same file overrides the thresholds in `src/squelch/core/settings.py` — for example
`SCRAPE_MAX_NEW_ISSUES`, `LLM_DELAY_SECONDS`, `SEEN_MAX_ENTRIES`, `GEMINI_MODEL`,
`PUBLISHED_RETENTION_DAYS`. Secret values are never committed: only their names live in the
repository.

## Known limitations

An honest list. These are not bugs about to be fixed — they follow from the architecture.

- **Posting to Discord and moving the label are not one transaction.** The message goes to the
  webhook first, and only then does the issue become `status:3-published`. If the run dies (or
  Actions kills it on timeout) between the two, the article stays `status:2-ready` and gets
  posted to Discord again on the next run. A duplicate in the channel is the price of never
  losing a publication; the opposite order would lose them.
- **The dedup ledger is a rolling window.** The ledger issue keeps only the newest
  `SEEN_MAX_ENTRIES` uids (3500 by default, bounded by GitHub's 65536-character issue body). A
  source silent for a very long time can have its old entries pushed out by other sources — and
  then a stale article comes back as new. In-flight issues are checked separately, so only
  articles already published or rejected can resurface this way.
- **The batch caps only bind while catching up.** A first run against a fresh repository sees
  every source's whole front page at once — a few dozen articles — and drains it over the next
  few ticks. In steady state a source publishes on the order of one article a day, so the caps
  never come near binding and most filter runs find nothing to do. The free Gemini tier is the
  ceiling that would bite first if that changed, at one call per article.
- **GitHub throttles bulk content creation** separately from the 5000-requests-per-hour budget,
  without warning and with a 403. That is why a run creates at most `SCRAPE_MAX_NEW_ISSUES`
  issues and defers the rest to the next tick on purpose.
- **Deduplication works on URLs.** The same story on three different sites is three different
  issues. Collapsing by meaning would need embeddings and is not attempted here.
- **Issue bodies are capped.** GitHub refuses anything over 65536 characters and the pipeline
  cuts at 60000, so very long articles are stored truncated (marked `[truncated]`). The metadata
  block and the summary are never the part that gets cut.
- **Manual edits race the pipeline.** Change a label at the exact moment a run is in flight and
  the pipeline's write wins — it does not check whether the issue changed since it was read.
- **The ledger is read once and written once per run.** Two concurrent scrape runs would
  overwrite each other's uids; the workflow's concurrency group prevents that, so do not work
  around it.
- **The `web` source type is minimal.** Playwright, a listing page, a CSS selector. No
  pagination, no authentication, no anti-bot evasion.
