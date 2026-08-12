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
     │                          ▲                             │                        │
     └─classify─▶ status:rejected                     sent:site  sent:rss  sent:discord └── closed
                  (closed; enough 👍 on the issue          (delivery, one label each)
                   and rescue votes it back in)
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
| Digest (daily) | `digest-daily.yml` | Writes the morning roundup over the day just gone into an issue | `0 6 * * *` |
| Digest (weekly) | `digest.yml` | Writes the look back over the week, with trends, into an issue | `0 9 * * 1` |
| Publish digest | `publish-digest.yml` | Posts the roundups waiting in the queue and marks `sent:discord-digest` | `8 * * * *` |
| Publish rejected | `publish-rejected.yml` | Posts recent rejections, with their reasons, to the rejected channel and marks `sent:discord-rejected` | `18 * * * *` |
| Rescue | `rescue.yml` | Reopens rejected issues with enough 👍 reactions as `status:2-relevant` | `48 * * * *` |
| Labels | `labels.yml` | Reconciles the label set with the config | on push to `config/**` |
| Sources | `sources.yml` | Asks every enabled source for a couple of articles and goes red on any that has none | `17 6 * * 1`, and on PRs touching `sources.yaml` |

Every stage on the hot path owns its own five-minute slot, so no two ever fire on the same
minute and queue behind each other on the runner. The two community stages run hourly and sit
off that grid — all twelve slots are taken, and neither is in a race with anything. The offsets also stagger them in order, so each finds what the
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

That quota is counted **per model**, and it counts requests rather than tokens — we peak at
about 5% of the token allowance and can still run out of day. The ceilings differ by an order of
magnitude: 500 requests a day on the lite models against 20 on `gemini-3.6-flash`. So which
model a stage sits on is a capacity decision before it is a quality one, and both high-volume
stages belong on a lite model. See the comments in `config/models.yaml` for why each stage sits
where it does.

Per-model counting also matters the day you drain one: both digest workflows take a `model`
input on manual dispatch, so a roundup can still be written on a model that has budget left
while the usual one is spent. Blank — which is what the scheduled runs pass — leaves
`config/models.yaml` in charge. `digest-daily.yml` additionally takes a `days` input, for
catching up over a backlog after an outage.

### The digests, and what the feed channel is for now

Two roundups share one Discord channel: a daily every morning over the day just gone, and on
Mondays a weekly look back once the week is actually over. They are one code path and two
prompt files — the daily reports, the weekly synthesises — because a reader who gets both on a
Monday must not feel they got the same message twice. Which one a message is, is in its footer.

**A roundup is an issue too.** The build stage writes it into one labelled `digest:daily` or
`digest:weekly` and stops there; `publish-digest.yml` posts it, records the message id on it and
marks `sent:discord-digest`; `close-delivered` closes it. That split is what the rest of the
pipeline already had and the digest did not:

- A webhook that is down costs a delay, not the roundup — and not one of the day's twenty
  strong-model requests.
- The same message-id trick that stops articles double-posting now covers roundups, so a run
  dying between "posted" and "labelled" relabels next time instead of sending a second copy.
- **You can read a roundup before it goes out, and edit it.** The YAML block at the top of the
  issue is what the publisher reads; the markdown under it is a preview. This is what the issue
  list was always good for, and the digest never got it.
- Running a build twice in a day writes nothing the second time: it finds today's roundup
  already waiting. Keyed on the day, so a stalled queue never stops tomorrow's from being
  written.

A digest issue carries no `status:` label, which is what keeps it inert: classify, summarize, the
site build and the window the *next* digest reads all step straight over it. The precedent is the
dedup ledger — a non-article issue, same database, label of its own.

That channel is the one to point people at. The article-by-article `discord` channel is still
there and still gets every survivor as it lands, but it is the project's own view of the
pipeline working rather than something to read start to finish. Nothing in the pipeline changed
for it: an article still has to reach it before it counts as published.

The consequence in code is that **nothing falls back to the feed's webhook any more**.
`DISCORD_DIGEST_WEBHOOK_URL` is required rather than optional, and a digest run with it unset
goes red instead of quietly posting the roundup where nobody reads it.

What a roundup gets to read is `digest` in `config/feed.yaml`, and it is two knobs that only
mean anything together:

```yaml
digest:
  max_articles: 60     # the size the roundup aims for
  min_score: 0         # what it takes to get in past that size — 0 keeps everything
  summary_chars: 1000  # each article's published write-up, cut to this
```

Articles are ranked by the classifier's score, and everything down to `max_articles` is in. Past
that an article is still in if it cleared `min_score` — so the cap is a budget rather than a
verdict, and nothing is dropped for being article number sixty-one alone. With the shipped
`min_score: 0` the cap is inert and the whole window reaches the model: at about fifteen
articles a day, a week is a hundred-odd articles and roughly 12k tokens, which is nothing
against this model's context. Raise the threshold the day that stops being true — at this
volume `min_score: 4` would carry about 75 of a week's 105 — and the run logs what it cost,
because a cap that trims in silence reads as "covered everything" when it did not.

Each article reaches the model as **the write-up the feed published**, not the article behind
it. A roundup built from raw source text could highlight something that never appeared in the
channel or the archive — the same guarantee the link check already gives, one level down. Raw
text is the fallback for issues opened by hand, which have no write-up, and that is what
`summary_chars` really bounds.

## Quick start

1. **Create the repository** — fork this one or start a new one from its contents. Keep it
   **public**: that way GitHub Actions minutes are not billed and GitHub Pages is available on
   the free plan.

2. **Add the secrets** under *Settings → Secrets and variables → Actions → New repository
   secret*:
   - `GEMINI_API_KEY` — a key from Google AI Studio;
   - `DISCORD_WEBHOOK_URL` — the webhook of the article-by-article feed channel (*Channel
     settings → Integrations → Webhooks*). This is the channel you watch the pipeline in,
     not the one you invite people to;
   - `DISCORD_DIGEST_WEBHOOK_URL` — the webhook of the channel both roundups go to, daily
     and weekly. Required by `publish-digest`: there is deliberately no fallback to the feed
     channel, because a roundup posted there would be published to nobody. Unset, roundups
     are still written and simply queue up as open issues until it is set.
   - `DISCORD_REJECTED_WEBHOOK_URL` — optional. A webhook for the channel that shows what
     the classifier rejected (see below). Deliberately no fallback: rejects never land in
     the feed channel. Unset, disable the `discord-rejected` channel in
     `config/delivery.yaml` or the `publish-rejected` runs go red.
   - `DISCORD_SKILLS_WEBHOOK_URL` — optional. A webhook for the skills rubric — articles
     routed by label to their own channel (see below). No fallback either; unset, disable
     the `discord-skills` channel in `config/delivery.yaml` or `publish` runs go red.

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
| `config/feed.yaml` | The name of the feed, `focus`, the allowed `topics`, how far back news reaches, text limits, how much of the window a roundup carries |
| `config/sources.yaml` | The source catalogue and nothing else |
| `config/delivery.yaml` | Which channels an article must reach before it counts as published, and how much room it gets there |
| `config/models.yaml` | Which Gemini model runs each stage |

The prompts are the exception: they sit at the top level in `prompts/`, one
markdown file per stage, because they are the text you reread and rewrite most
often and a diff on them should read like a diff on prose.

| File | What it holds |
| --- | --- |
| `prompts/classify.md` | What the judge is told |
| `prompts/summarize.md` | What the writer is told |
| `prompts/review.md` | What the skills rubric is told |
| `prompts/digest-daily.md` | What the morning roundup is told |
| `prompts/digest-weekly.md` | What the weekly look back is told |

Each file is a `## System` section and a `## Template` section, taken verbatim,
with notes above them that never reach the model; placeholders are `$name`.
[`prompts/README.md`](prompts/README.md) has the details.

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

### `emphasis` — how loud an article is in Discord

The classifier scores every article 0-10 on one question, asked in
`prompts/classify.md`: how much this changes a working engineer's day. That score decides
how much of the channel the article gets.

```yaml
  - id: discord
    enabled: true
    emphasis:
      lead: 7        # full-width picture, summary, topic line
      standard: 4    # small picture on the right, same text
                     # below that: headline and one line, no picture
```

Height is the signal that actually works — a chat channel is skimmed while it scrolls past, and
a lead is several times the size of a brief. The colour only confirms what the size already
said, which is why all three stay in one family: this is one feed, not three bots.

Nothing is dropped or delayed. A brief keeps its headline, its link, its topics and its
discussion link, and is reachable in the same two clicks — it just stops competing. On the
archive so far the split lands at roughly 17% lead, 49% standard, 35% brief.

Only Discord reads this. The site and the feed are read on purpose rather than skimmed, so they
give every article the same room.

Raise `lead` if too much of the channel is shouting. Set `standard: 0` to switch the compact
tier off and give everything equal weight. If the classifier ever drifts and starts handing out
sevens to everything, the tiers stop meaning anything — the distribution above is worth a
glance now and then.

### Routing a rubric to its own channel

By default every enabled channel gets every article. A channel can instead declare, by label,
what it wants:

```yaml
  - id: discord
    skip: ["topic:claude-skills", "source:claude-skills"]   # everything except these
  - id: discord-skills
    only: ["topic:claude-skills", "source:claude-skills"]   # nothing but these
```

This is sectioning, not filtering — every article still goes out, just to the channel whose
readers asked for it. The closing pass counts, per article, exactly the channels it was routed
to, so a channel that skips an article never holds it open waiting for a delivery that will
never come. Both the topic and the source label are listed on purpose: the source is set
deterministically at scrape time, the topic is the classifier's tag — either is enough to
reroute, so an article the LLM forgot to tag still lands in the right place. Each Discord
channel posts through its own webhook (`DISCORD_SKILLS_WEBHOOK_URL` for the one above), and an
enabled channel with no webhook fails the run rather than borrowing the feed's — routing exists
precisely so posts do not end up in the wrong place. The site and the RSS feed stay unrouted:
they are the archive, and the archive holds everything.

### Forum channels

A Discord forum has no message stream: every post is a thread with a name. A channel that is
one says so, and each article then arrives as its own post titled with its headline, discussion
attached, instead of scrolling away behind the next card.

```yaml
  - id: discord-skills
    forum: true
```

For the digests — which have no entry here, being one-offs rather than a queue articles pass
through — the same switch is the `DIGEST_FORUM` environment variable, next to their webhook. It
covers both roundups, since they share a channel, and each post is titled with its own headline.

The flag has to match what the channel actually is: Discord refuses a forum message that names
no thread, and refuses a text message that names one. Get it wrong and the run goes red with
Discord's own complaint in it, which is the intended failure — the alternative would be
guessing.

A forum can also tag its posts, driven by the labels already on the issue:

```yaml
  - id: discord-skills
    forum: true
    tags:
      "topic:tooling": "1536677352002560000"
      "topic:models":  "1536677793474027581"
```

The ids are Discord's. They are **not** secrets — a tag id names nothing and opens nothing — so
they belong here in the open next to the channel, not in the environment with the webhook. They
also cannot be discovered: no webhook endpoint lists a forum's tags, so the mapping is written
down once by hand. To read an id, create the tag in Discord and take it from the forum's tag
filter in the page (`forum-tag-<id>`); a pasted tag *name* is refused when the config loads,
because Discord's complaint about it would otherwise surface days later in a workflow log.

Discord accepts at most five tags on a post, and an article can carry more topics than that, so
config order decides the overflow — write the most telling label first. A label with no entry is
simply not tagged, which is what keeps adding a topic to `feed.yaml` from breaking the channel.

### Hunting skill repositories: the `github` source

Skill collections for coding agents are announced nowhere and hyped everywhere — TikTok
included — and the only reliable signal is the repository itself appearing and gathering stars.
The `github` source type turns a GitHub search into articles:

```yaml
  - id: claude-skills
    type: github
    url: https://github.com/search?q=claude+skills+in%3Aname%2Cdescription&type=repositories
    max_items: 5
```

The URL is the human-clickable search page; the scraper runs its `q=` against the API, bounded
to repositories created within `max_age_days` and sorted by stars — so what surfaces each run
is "the new repositories people actually flocked to". The article body is what the repository
says about itself, hard facts first: stars, forks, dates, file counts, how many `SKILL.md`
files and shell scripts it contains. Then an inventory of the skills that actually exist —
every `SKILL.md` in the tree, listed by the name and one-line description it declares in its
own frontmatter — and only after that the README and a few skills quoted at length. The
inventory comes before the README on purpose: everything downstream reads a prefix of this
text, so what survives the cut should be the contents rather than the banner at the top of a
sales pitch. Nothing from the repository is ever executed: this pipeline reads, it does not
audit. Tune the search by editing the URL; the empty-shell repos are for `focus` to filter,
as always.

### The rubric's review

A summary says what a repository is. In the skills channel that is not enough — the whole
question about a collection with 800 stars is whether there is anything behind it — so a
channel can ask for a review:

```yaml
  - id: discord-skills
    forum: true
    review: true
```

Articles routed to that channel get a second LLM call in stage two (`prompts/review.md`,
its own model in `models.yaml`), which reads the inventory and the files and answers three
things: what each skill that actually exists does, whether the collection matches what it claims
about itself, and who would get real value out of it today. The result is posted as the first
reply inside the article's own thread — the card stays a card, and the argument starts under the
analysis.

Each skill is marked `thin` or `unclear` where the files do not back the claim; a real one is
left unmarked, so the list reads as an inventory rather than scored homework. The reply's footer
says every time that this is a reading of files and that nothing was run, because that is the
truth and a verdict that implied otherwise would be a lie in the one place it matters.

The verdict is read beside the numbers it is weighed against — `**Hype** · 72 269 ★ · 864
skills` — and those come from the scraper, never from the model: they are counted at scrape time
and carried to the channel in the issue's `facts` block, so a figure a reader is about to act on
never passes through anything that could round it. A repository whose tree could not be read
reports its stars and stays silent about skills, because "0 skills" is a finding and a failed
request is not. Between them, the star count and the skill count are also what tell a reader
whether the thing is worth cloning to check for themselves.

The verdict is informational and always published, including when it is damning. It is
deliberately *not* a second gate: whether an article runs at all was decided by the classifier
against `focus`, where it is visible and arguable, and a silent second filter is the thing this
feed exists to avoid. `review` requires `forum` — a reply needs a thread to land in — and
turning the channel off stops the extra call along with the reply.

### The rejected channel, and voting an article back

The classifier is cheap and sometimes wrong, and its mistakes are only correctable if someone
sees them. The `discord-rejected` channel in `delivery.yaml` posts every fresh rejection —
headline, source, the classifier's reason — to a channel of its own in the same server, sized
like a brief and coloured grey: a window onto the cutting-room floor, not a second feed.

```yaml
  - id: discord-rejected
    enabled: true
    consumes: rejected   # reads status:rejected, so it never gates closing
```

`consumes: rejected` is what keeps this channel out of the count that closes an article —
rejected issues are closed already, and ordinary articles never carry its `sent:` label. The
channel shows a rolling window (`REJECTED_WINDOW_DAYS`, three days by default), so switching it
on shows what was thrown away lately rather than replaying every rejection ever made. It needs
its own webhook in `DISCORD_REJECTED_WEBHOOK_URL`, and refuses to fall back to the feed's.

Each post ends with the appeal: react 👍 on the linked issue. The `rescue` pass counts those
reactions — they ride along on the issue listing, so this costs no extra API requests — and
reopens any rejected issue with enough of them (`RESCUE_MIN_REACTIONS`, one by default) as
`status:2-relevant`. Relevant rather than raw on purpose: re-running the same classifier on the
same text would just reject it again, and a community vote is the same human override as
flipping the label by hand — it goes straight to the summariser, keeps the topic tags the
classifier chose, and the original rejection reason stays in the metadata as `rejected_reason`.
Votes count for `RESCUE_WINDOW_DAYS` (fourteen by default); after that the article would need a
label flipped by hand, which anyone with write access can still do.

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

### Keeping the catalogue honest

Sources rot quietly. A vendor restyles its newsroom and `a[href^="/news/"]` matches nothing; a
WordPress blog stops being an archive and its `/feed/` starts serving an empty comments feed
instead. Both parse fine and yield nothing, and a scrape that finds nothing is green — as it
must be, since most half-hours genuinely have no news. So the source that died looks exactly
like the source that had a quiet week.

`squelch check-sources` is the one command with the opposite rule: it asks every enabled source
for two articles and fails if any of them has nothing at all.

```bash
squelch check-sources                    # the whole catalogue
squelch check-sources --source anthropic # one source, while iterating on its selector
```

```
source        type  items  dated    img  status
msdevblogs    rss       0      0      0  BROKEN: yielded nothing
anthropic     html      2      2      2  ok
deepmind      rss       2      2      2  ok
```

It runs the real scrapers rather than asserting anything about the markup, so there is no second
description of a source to drift out of step with the first. Dates and pictures are counted but
never judged — plenty of pages state no date and carry no picture, and failing on that would
make the check cry wolf until it is ignored.

The `sources` workflow runs it weekly, and on any pull request that touches `sources.yaml`, so a
selector edit is checked against the live page before it merges.

### The rest

`max_age_days` is how far back an article may be dated and still count as news. Listing pages
keep months of posts on them, so without it every source opens its whole back catalogue on the
day it is enabled. Articles no page gives a date for are let through — unknown is not old.
Raise it when adding a source whose archive is worth importing on purpose.

Where that date comes from is deliberately conservative. The article page is asked first, and
only for a date it states outright; a page that states none is left undated rather than guessed
at. Failing that, the date printed next to the link on the listing page is used, read from the
smallest block that belongs to that article alone — search any wider and every article inherits
its neighbour's date. Failing both, the article shows the day it was found.

`max_body_chars` is how far the article text is truncated before it goes to the LLM. More
characters mean slower and more expensive calls; 8000 is usually enough.

`title` is the public name of the feed — masthead, RSS channel and digest headers. It is kept
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
| `squelch publish-rejected` | post recent rejections, with reasons, to the rejected channel |
| `squelch rescue` | reopen rejected articles the community voted back with 👍 |
| `squelch close-delivered` | close what every enabled channel has delivered |
| `squelch digest --period daily\|weekly` | write one roundup and store it as an issue |
| `squelch publish-digest` | post the roundups waiting in the queue |
| `squelch build-site` | render the static archive and the feed |
| `squelch check-sources` | ask every source for two articles, fail on the ones that have none |
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
DISCORD_DIGEST_WEBHOOK_URL=...   # required by `squelch digest`; no fallback to the one above
```

The same file overrides the thresholds in `src/squelch/core/settings.py` — for example
`SCRAPE_MAX_NEW_ISSUES`, `LLM_DELAY_SECONDS`, `SEEN_MAX_ENTRIES`, `GEMINI_MODEL`,
`CLASSIFY_BODY_CHARS`, `FEED_WINDOW_DAYS`, `DIGEST_FORUM`. Secret values are never committed: only their names live in the
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
