# Everything you do to this repository from a terminal.
#
# Two jobs. Editing the cron grid — which lives in config/schedule.yaml and is
# stamped into .github/workflows by `make schedule`. And kicking a stage by hand
# without going through four clicks in the Actions UI, which is `make publish`,
# `make classify`, and so on, one target per workflow.
#
# The run targets shell out to `gh`, so they use your own GitHub login and no
# repository secret is involved. They dispatch the real workflow — the same one
# cron fires — and then follow it to completion; `make publish WATCH=0` returns
# as soon as it is queued instead.

.DEFAULT_GOAL := help
SHELL := /bin/bash

# Follow the run after dispatching it, and take its exit code as ours.
WATCH ?= 1
# Extra `-f key=value` inputs for the workflows that take them, e.g.
#   make daily INPUTS="-f days=3 -f model=gemini-2.5-pro"
INPUTS ?=

define run-workflow
	@command -v gh >/dev/null || { echo "gh is not installed: https://cli.github.com"; exit 1; }
	gh workflow run $(1).yml $(INPUTS)
	@if [ "$(WATCH)" = "1" ]; then \
		sleep 4; \
		gh run watch "$$(gh run list --workflow=$(1).yml --limit 1 --json databaseId --jq '.[0].databaseId')" --exit-status; \
	else \
		echo "queued; \`make runs\` to see it"; \
	fi
endef

# -- the grid ----------------------------------------------------------------

.PHONY: grid schedule schedule-check

grid: ## Print the cron grid: who runs how often, and which stages cost a model call
	squelch schedule

schedule: ## Stamp config/schedule.yaml into the workflow files
	squelch schedule --write

schedule-check: ## Fail if a workflow has drifted from the config, or two stages collide
	squelch schedule --check

# -- running a stage by hand -------------------------------------------------

.PHONY: scrape classify summarize publish close rejected rescue
.PHONY: ingest read answer daily weekly digest site labels sources feed cases

scrape: ## feed / 1 scrape — walk the sources, open an issue per new article
	$(call run-workflow,feed-1-scrape)

classify: ## feed / 2 classify — judge raw issues against `focus`  [costs a model call]
	$(call run-workflow,feed-2-classify)

summarize: ## feed / 3 summarize — write up what survived  [costs a model call]
	$(call run-workflow,feed-3-summarize)

publish: ## feed / 4 publish — send ready articles to Discord
	$(call run-workflow,feed-4-publish)

close: ## feed / 5 close delivered — close what every channel has taken
	$(call run-workflow,feed-5-close)

rejected: ## feed / publish rejected — post recent rejections to their own channel
	$(call run-workflow,feed-publish-rejected)

rescue: ## feed / rescue voted — reopen rejections with enough 👍
	$(call run-workflow,feed-rescue)

ingest: ## cases / 1 ingest — read the community forum
	$(call run-workflow,cases-1-ingest)

read: ## cases / 2 read — write a reading of each new case  [costs a model call]
	$(call run-workflow,cases-2-read)

answer: ## cases / 3 answer — post the readings back into their threads
	$(call run-workflow,cases-3-answer)

daily: ## digest / 1 write daily — the morning roundup  [costs a model call]
	$(call run-workflow,digest-1-write-daily)

weekly: ## digest / 1 write weekly — the week, with trends  [costs a model call]
	$(call run-workflow,digest-1-write-weekly)

digest: ## digest / 2 publish — post the roundups waiting in the queue
	$(call run-workflow,digest-2-publish)

site: ## site / build and deploy — render the archive and deploy to Pages
	$(call run-workflow,site-build)

labels: ## repo / labels — reconcile the label set with the config
	$(call run-workflow,repo-labels)

sources: ## repo / source health — ask every source for a couple of articles
	$(call run-workflow,repo-source-health)

# The whole chain, in order, waiting for each. Useful after changing `focus` or
# a prompt: it walks one batch through end to end instead of over ninety minutes
# of cron. Ignores WATCH — running these in parallel would let publish fire
# before summarize has written anything.
feed: ## Run the article pipeline end to end: scrape → classify → summarize → publish → close
	$(MAKE) WATCH=1 scrape classify summarize publish close

cases: ## Run the forum pipeline end to end: ingest → read → answer
	$(MAKE) WATCH=1 ingest read answer

# -- looking at what happened ------------------------------------------------

.PHONY: runs failures logs workflows

runs: ## The last 20 runs, any workflow
	gh run list --limit 20

failures: ## The last 10 failed runs
	gh run list --status failure --limit 10

logs: ## Failed steps of the most recent failed run
	gh run view "$$(gh run list --status failure --limit 1 --json databaseId --jq '.[0].databaseId')" --log-failed

workflows: ## Every workflow, with its state
	gh workflow list --all

# -- local checks ------------------------------------------------------------

.PHONY: test lint check help

test: ## Run the offline test suite
	pytest -q

lint: ## Ruff
	ruff check .

check: lint test schedule-check ## Everything CI runs

help: ## This list
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) \
		| sed -E 's/:.*## /\t/' \
		| awk -F'\t' '{printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'
