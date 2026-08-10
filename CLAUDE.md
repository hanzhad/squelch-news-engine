# Squelch — notes for Claude Code

Serverless news pipeline. GitHub Issues are the database, labels are the state machine,
GitHub Actions is the scheduler. Python 3.12+, src-layout, package `squelch`.

## Layout

```
src/squelch/
  core/        models, settings, config loader, URL canonicalization, dedup ledger, logging
  github/      REST client, issue CRUD + body render/parse, label bootstrap
  scrapers/    rss, web (Playwright), text extraction, orchestration
  llm/         Gemini calls: classify, summarize, weekly digest
  publishers/  Discord webhook
  site/        static archive rendering (templates live in top-level site/templates/)
  cli/         typer app; console script is `squelch`
config/               feed.yaml (policy), sources.yaml, models.yaml, prompts/<stage>.yaml
                      the dedup ledger is an issue (label meta:ledger), not a file
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
