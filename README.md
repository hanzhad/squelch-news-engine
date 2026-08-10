# Squelch

A serverless news pipeline that lives entirely inside one GitHub repository: it collects
articles from RSS feeds and websites, sifts them through Gemini, and publishes the survivors to
Discord and to a static archive on GitHub Pages. No database, no server, no Docker — just
GitHub Actions on a schedule.

**Live archive: [FEED](https://hanzhad.github.io/squelch-news-engine/)**
· [RSS](https://hanzhad.github.io/squelch-news-engine/rss.xml)
· [the queue itself](https://github.com/hanzhad/squelch-news-engine/issues)

## The idea: Issues are the database

One article is one issue. Metadata lives in a YAML block inside an HTML comment at the top of
the issue body: GitHub does not render it, and we know how to read and rewrite it. Labels are
the state machine, and they — not some external table — are the source of truth about where an
article stands.

```
status:1-raw ─classify─▶ status:2-relevant ─summarize─▶ status:3-ready ─close─▶ status:4-published
     │                                                        │                        │
     └─classify─▶ status:rejected                     sent:site  sent:rss  sent:discord └── closed
                  (closed, not planned)                    (delivery, one label each)
```

Judging and writing are separate stages because they want different things. The classifier runs
on everything scraped, needs only the top of an article, and does well enough on the cheapest
model; the summariser writes prose, so it gets the good model and only ever sees articles that
already survived. Splitting them also means a bad minute at the API costs a summary rather than
a verdict — and lets you read `status:rejected` to see what the classifier threw away, with its
reason rendered in the issue body rather than buried in a comment.

Delivery does not fit on that line, so it gets labels of its own. Status is one axis — an
article is raw, or relevant, or ready — while delivery is several at once: out on the site,
not yet on Discord. Every channel in `config/delivery.yaml` owns a `sent:<id>` label, picks up
ready articles that do not carry it yet, and marks them when it has them. A dead webhook holds
up nothing but Discord.

Nobody closes an issue on their way past. The channel that happens to deliver last could die
between its own label and the close, and the article would sit open forever — every channel has
had it, so no queue would contain it again. Instead `close-delivered` runs on its own schedule,
re-derives the answer from the labels, and moves an article to `status:4-published` once every
*enabled* channel has marked it. Switching a channel off in config therefore releases whatever
was waiting on it, with nothing to re-run. Switching one on only affects articles still at
`status:3-ready` — a new channel starts with today's news instead of replaying the archive.

Open issues are exactly the work still in flight: rejected ones close as *not planned*,
fully delivered ones close as *completed*.

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

## The pipelines

Each pipeline is its own workflow in `.github/workflows/`, triggered by cron and manually via
**Actions → Run workflow**.

| Pipeline | Workflow | What it does | Schedule |
| --- | --- | --- | --- |
| Scrape | `scrape.yml` | Walks the enabled sources, drops already-seen uids, opens issues labelled `status:1-raw`, updates the ledger issue | `0,30 * * * *` |
| Classify | `classify.yml` | Judges each raw issue against `focus` on the cheap model: `status:2-relevant` with tags and a score, or `status:rejected` and closure | `10,40 * * * *` |
| Summarize | `summarize.yml` | Writes up the survivors on the full model and moves them to `status:3-ready` | `20,50 * * * *` |
| Publish | `publish.yml` | Sends ready articles Discord has not seen to the webhook and marks `sent:discord` | `5,25,45 * * * *` |
| Close | `close.yml` | Closes ready articles that every enabled channel has delivered | `15,35,55 * * * *` |
| Digest | `digest.yml` | Builds a weekly roundup with trends out of what reached the feed | `0 9 * * 1` |
| Labels | `labels.yml` | Reconciles the label set with the config | on push to `config/**` |

Every stage owns its own five-minute slot, so no two ever fire on the same minute and queue
behind each other on the runner. The offsets also stagger them in order, so each finds what the
one before it just produced. (GitHub runs cron on a best-effort basis and delays it under load,
so treat the minutes as intent rather than a guarantee — every stage is written to pick up
whatever the last one left behind.)

There is also `pages.yml` — it renders the static archive, deploys it to GitHub Pages, and marks
`sent:site` and `sent:rss` on what it rendered. It runs off `summarize`, not `publish`: **the
archive, the feed and Discord are independent consumers of the same queue.** The page and the
feed are counted apart because they genuinely differ — the feed holds only the newest entries
and skips anything without a link. Marking happens at build time rather than after deployment,
which is safe because both outputs are re-rendered whole every time: a failed deploy costs a
delay, never an article.

The site reads a rolling window rather than the whole history — `FEED_WINDOW_DAYS`, three days
by default. Published issues accumulate forever and the site rebuilds after every write-up, so
an unbounded read would page through more of the archive every week for a page nobody scrolls
that far down. The permanent record is the issue tracker; the site is the shop window onto it.
Raise the setting to show more, and pay the extra requests on every build.

None of the pipelines tries to do all the work in one run: scraping, both LLM stages and
publishing each cap their batch and leave the remainder for the next tick. This is deliberate — GitHub
throttles bulk content creation, and the free Gemini tier meters requests tightly.

That quota is counted **per model**, which matters the day you drain it: `digest.yml` takes a
`model` input on manual dispatch, so a roundup can still be written on a model that has budget
left while the usual one is spent. Blank — which is what the scheduled run passes — leaves
`config/models.yaml` in charge.

## Quick start

1. **Create the repository** — fork this one or start a new one from its contents. Keep it
   **public**: that way GitHub Actions minutes are not billed and GitHub Pages is available on
   the free plan.

2. **Add the secrets** under *Settings → Secrets and variables → Actions → New repository
   secret*:
   - `GEMINI_API_KEY` — a key from Google AI Studio;
   - `DISCORD_WEBHOOK_URL` — the webhook of the feed channel (*Channel settings →
     Integrations → Webhooks*);
   - `DISCORD_DIGEST_WEBHOOK_URL` — optional. A webhook for a channel of the weekly
     roundup's own; unset means it lands in the feed channel with everything else.

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

   After that it takes care of itself: `labels.yml` runs on every push that touches `config/**`,
   which is exactly when a new source, topic or channel needs a label. The command is
   idempotent, so running it by hand or from **Actions → labels → Run workflow** is always safe.

5. **Enable GitHub Pages.** *Settings → Pages → Source → **GitHub Actions*** (not "Deploy from
   a branch"). The archive appears after the first successful `pages.yml` run.

6. **Edit `config/feed.yaml` and `config/sources.yaml`** for your subject (see below) and run
   `scrape.yml` by hand to see what comes out.

## Configuration

Everything you would sit down to change lives in `config/`, one file per thing:

| File | What it holds |
| --- | --- |
| `feed.yaml` | The name of the feed, `focus`, the allowed `topics`, text limits |
| `sources.yaml` | The source catalogue and nothing else |
| `delivery.yaml` | Which channels an article must reach before it counts as published |
| `models.yaml` | Which Gemini model runs each stage |
| `prompts/classify.yaml` | What the judge is told |
| `prompts/summarize.yaml` | What the writer is told |
| `prompts/digest.yaml` | What the weekly roundup is told |

No code changes needed for any of it.

### `focus` — the main knob

The `focus` text goes into the classifier prompt verbatim, so it decides what survives at all.
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
repository finite. Pushing the new topic is enough — `labels.yml` creates the label with a
proper colour and description before the classifier ever reaches for it.

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
| `squelch classify` | judge raw issues on the cheap model |
| `squelch summarize` | write up the ones that survived |
| `squelch publish` | send ready articles to Discord |
| `squelch close-delivered` | close what every enabled channel has delivered |
| `squelch digest` | build the weekly digest |
| `squelch build-site` | render the static archive and the feed |
| `squelch bootstrap-labels` | create or repair labels from the config |
| `squelch rebuild-ledger` | rebuild the dedup ledger from the issues themselves |

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
DISCORD_DIGEST_WEBHOOK_URL=...   # optional, defaults to the one above
```

The same file overrides the thresholds in `src/squelch/core/settings.py` — for example
`SCRAPE_MAX_NEW_ISSUES`, `LLM_DELAY_SECONDS`, `SEEN_MAX_ENTRIES`, `GEMINI_MODEL`,
`CLASSIFY_BODY_CHARS`, `FEED_WINDOW_DAYS`. Secret values are never committed: only their names live in the
repository.

## Known limitations

An honest list. These are not bugs about to be fixed — they follow from the architecture.

- **Posting to Discord and recording it are not one transaction.** The message goes to the
  webhook first, then the message id into the issue body, then the `sent:discord` label. Written
  in that order on purpose: a run that dies mid-way leaves the id behind, and the next run sees
  it and relabels instead of posting the article twice. The gap that remains is the one before
  the id is stored at all — if Actions kills the run between the webhook accepting the message
  and the body being written, the article is posted again next time. A duplicate in the channel
  is the price of never losing a publication; the opposite order would lose them.
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
