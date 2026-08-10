# Squelch — notes for Claude Code

Serverless news pipeline. GitHub Issues are the database, labels are the state machine,
GitHub Actions is the scheduler. Python 3.12+, src-layout, package `aetherfeed`.

## Layout

```
src/aetherfeed/
  core/        models, settings, config loader, URL canonicalization, dedup ledger, logging
  github/      REST client, issue CRUD + body render/parse, label bootstrap
  scrapers/    rss, web (Playwright), text extraction, orchestration
  llm/         Gemini calls: filter verdicts and the weekly digest
  publishers/  Discord webhook
  site/        static archive rendering (templates live in top-level site/templates/)
  cli/         typer app; console script is `aetherfeed`
config/sources.yaml   editorial policy + source catalogue
data/seen.json        rolling dedup ledger, committed back by the scrape workflow
tests/                offline only — fixtures + fake HTTP clients, never the real network
```

## Label state machine

`status:1-raw` → `status:2-ready` → `status:3-published`, plus `status:rejected` (issue closed
as `not_planned`). Extra labels: `source:<id>`, `topic:<tag>`. The label on the issue is the
source of truth — never introduce a parallel state store. Use `IssueStore._swap_status` to
change status so that `source:` and `topic:` labels survive.

Issue bodies carry a YAML metadata block inside an HTML comment; `render_body` / `parse_body`
in `github/issues.py` are the only place that format is defined. An issue opened by hand has
no metadata block and no original marker — `IssueRecord.text` falls back to `raw_body` for
exactly that case, so keep that path working.

## Editorial policy lives in config, not in code

`config/sources.yaml` drives what the pipeline does. `focus` goes verbatim into the filter
prompt and decides what survives; `topics` bounds the set of `topic:*` labels the LLM may
apply. Tuning behaviour means editing that file — do not hard-code source lists, keyword
filters or score thresholds in Python. Adding a topic or source means re-running
`aetherfeed bootstrap-labels`.

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
