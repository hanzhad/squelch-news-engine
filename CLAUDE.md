# Squelch — notes for Claude Code

Serverless news pipeline. GitHub Issues are the database, labels are the state machine,
GitHub Actions is the scheduler. Python 3.12+, src-layout, package `squelch`.

## Layout

```
src/squelch/
  core/        models, settings, config loader, URL canonicalization, dedup ledger, logging
  github/      REST client, issue CRUD + body render/parse, label bootstrap,
               digests (a roundup stored as an issue of its own kind)
  scrapers/    rss, web (Playwright), github repo search, text extraction, orchestration,
               source health check
  llm/         Gemini calls: classify, summarize, review, digest (daily and weekly)
  publishers/  Discord webhook
  site/        static archive rendering (templates live in top-level site/templates/)
  cli/         typer app; console script is `squelch`
config/               feed.yaml (policy), sources.yaml, delivery.yaml, models.yaml
                      the dedup ledger is an issue (label meta:ledger), not a file
prompts/              <stage>.md — one markdown file per LLM stage, `## System` +
                      `## Template` taken verbatim; see prompts/README.md
tests/                offline only — fixtures + fake HTTP clients, never the real network
```

## Label state machine

`status:1-raw` → `status:2-relevant` → `status:3-ready` → `status:4-published`, plus
`status:rejected`. Rejected closes as `not_planned`, published closes as `completed`, so open
issues are exactly the work still in flight. Extra labels: `source:<id>`, `topic:<tag>`. The label on the issue is the
source of truth — never introduce a parallel state store. Use `IssueStore._swap_status` to
change status so that `source:` and `topic:` labels survive.

Delivery is a separate axis and never goes into `status:`. Each channel in `config/delivery.yaml`
owns a `sent:<id>` label, consumes `status:3-ready` via `IssueStore.list_pending(channel)`, and
records itself with `record_delivery`. Publishers must not close issues: `close_delivered` is the
single owner of the move to `status:4-published`, so a channel dying after its own label can
never strand an article. `record_delivery` writes the body before the label on purpose — that
order is what stops a half-finished Discord run from double-posting.

A channel may declare `consumes: rejected` (the `discord-rejected` window onto what the
classifier threw away): it reads `status:rejected` through the same `list_pending` bookkeeping
but must never appear in `required_channels` — ordinary articles never carry its `sent:` label,
so counting it would strand every ready article open. The way back is the rescue pass
(`squelch rescue`): enough 👍 reactions on a rejected issue reopen it as `status:2-relevant` —
relevant, not raw, because re-classifying the same text would reject it again; a vote is a
human override, exactly like flipping the label by hand.

A channel may also declare `review: true` (forum-only). Articles routed to it get a second LLM
call inside stage two — `llm/review.py`, prompt in `prompts/review.md` — that walks the
`SKILL.md` files a repository actually contains and judges each one, and the result is posted as
the first reply inside the article's thread. `Config.wants_review` asks the routing rather than
a label list in Python, so switching the channel off stops the call too. The verdict never
gates anything: rejecting an article is the classifier's job against `focus`, and a second
silent filter is exactly what this pipeline is against.

Channels can also route by label (`only:` / `skip:` in `delivery.yaml`, matched via
`Channel.wants`) — that is how the skills rubric gets its own Discord channel. Routing is
sectioning, never filtering: every article must match some enabled channel, `close_delivered`
counts per article exactly the channels that want it, and an article routed nowhere is left
open with a warning rather than closed unseen. Each Discord channel has its own webhook env
var and deliberately no fallback to another channel's.

The digests are the public face, not the per-article stream. Two roundups — daily and weekly —
share one Discord channel through `DISCORD_DIGEST_WEBHOOK_URL`; `core.models.Period` carries the
window, the prompt file (`prompts/digest-<period>.md`), the label and how the window is named, and
everything else is one code path. A roundup is a title naming the stretch it covers, a body of
connected prose over the whole selection, and the articles as bare links — `DigestEntry` carries
no per-article commentary on purpose, because a caption per item is what turned the roundup into
the list the body exists to replace. Only the weekly asks for `trends`, and that is the one thing
keeping the two from reading alike.

A roundup is an issue, in `github/digests.py` — same database, same body format, its own module
the way `ledger.py` is. It carries `digest:<period>` and **never** a `status:` label: that is the
whole reason it stays invisible to classify, summarize, the site build and `list_published_since`,
which would otherwise feed a digest back into the next one. Writing and posting are separate
stages (`squelch digest` → `squelch publish-digest`) so the model's answer survives a dead webhook
and can be edited before it goes out; the YAML block is what the publisher reads, the markdown
below it is a preview. Delivery reuses the channel bookkeeping through `consumes: digest`, which
keeps it out of `ready_channels` and therefore out of anything that closes an article. Body before
label in `record_delivery`, and `close_delivered` still owns the close — both for the same reasons
as on the article path. What a roundup gets to read is `digest` in `feed.yaml` — `max_articles` is the size it
aims for and `min_score` is what an article needs to get in past that, so the cap is a budget and
not a verdict; shipped at `min_score: 0`, it rations nothing. Articles reach the model as the
write-up the feed published, never as raw source text: a roundup that read the article behind the
summary could say what the channel never said. The `discord` channel still receives every article and still gates closing, but it is
the project's own working view now, which is why `_Webhook` no longer falls back to its webhook
and `DISCORD_DIGEST_WEBHOOK_URL` is required: a roundup posted into the feed channel would be
published to nobody, and it would look like a successful run.

The classifier's `score` decides how much room an article gets in Discord, through `emphasis`
thresholds in `delivery.yaml` — `_weight` in `publishers/discord.py` maps it to a lead, a
standard or a brief. Size is the whole signal; the colour only confirms it. Every article is
still posted and still links to the same places, so this must never become a filter — a score
threshold that silently drops articles belongs nowhere in this pipeline.

Issue bodies carry a YAML metadata block inside an HTML comment; `render_body` / `parse_body`
in `github/issues.py` are the only place that format is defined. An issue opened by hand has
no metadata block and no original marker — `IssueRecord.text` falls back to `raw_body` for
exactly that case, so keep that path working.

## Editorial policy lives in config, not in code

`config/` drives what the pipeline does. `focus` goes verbatim into the classifier
prompt and decides what survives; `topics` bounds the set of `topic:*` labels the LLM may
apply. Tuning behaviour means editing that file — do not hard-code source lists, keyword
filters or score thresholds in Python. Adding a topic or source means re-running
`squelch bootstrap-labels`.

The wording the models actually see is the other half of that, and lives one level up in
`prompts/<stage>.md` — top-level because it is prose and gets rewritten more than anything
else here. `llm/prompts.py` reads the `## System` and `## Template` sections verbatim and
only substitutes `$name` placeholders; everything above the first heading is notes for the
editor. Never move a rule out of a prompt file and into Python.

## Conventions

- Every module starts with `from __future__ import annotations`; type hints everywhere; ruff
  with line-length 100 (`select = ["E", "F", "I", "UP", "B", "SIM"]`).
- Code, comments and docstrings in English. Comment the non-obvious *why*, not the *what*.
- Commit messages: Conventional Commits, English, imperative subject, no task IDs
  (`feat: add atom fallback for missing published dates`, `fix: keep topic labels on swap`).
- Tests must run offline. No network, no GitHub API, no real Gemini calls.
- Secrets are never written into files — not into code, config, docs or commits. Reference
  them by env var name only (`GEMINI_API_KEY`, `DISCORD_WEBHOOK_URL`, `GITHUB_TOKEN`), read
  through `core/settings.py`, supplied by repository secrets in Actions or a local `.env`.
- Rate limits are a design constraint, not an afterthought: batch caps and inter-call delays
  live in `core/settings.py`, and leftover work is deliberately deferred to the next cron tick
  rather than pushed through.
