"""Assembling prompts from the files in ``config/``.

What this feed publishes is tuned by rewriting sentences, not logic, so the
wording lives in ``config/prompts/<stage>.yaml`` — one file per stage — and the
model ids live in ``config/models.yaml``, because Google retires those on its
own schedule. This module only fills in the placeholders.

``string.Template`` rather than ``str.format``: prompt text is prose that may
well contain literal braces, and a stray one should not blow up a run.
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from string import Template

import yaml
from pydantic import BaseModel

from ..core.config import CONFIG_DIR, Config
from ..github.issues import IssueRecord

PROMPTS_DIR = CONFIG_DIR / "prompts"
MODELS_PATH = CONFIG_DIR / "models.yaml"

# Enough of each article for the model to see the theme, short enough that forty
# of them still make a small request.
DIGEST_SUMMARY_CHARS = 400


class Prompt(BaseModel):
    system: str
    template: str


class Models(BaseModel):
    classify: str
    summarize: str
    digest: str


@lru_cache
def load_prompt(stage: str, directory: Path | None = None) -> Prompt:
    path = (directory or PROMPTS_DIR) / f"{stage}.yaml"
    return Prompt.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache
def load_models(path: Path | None = None) -> Models:
    target = path or MODELS_PATH
    return Models.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))


def _fill(template: str, **values: str) -> str:
    # safe_substitute so an unknown or mistyped placeholder survives as text
    # instead of taking the whole run down.
    return Template(template).safe_substitute(**values).strip()


def system(stage: str) -> str:
    return load_prompt(stage).system.strip()


def classify_prompt(config: Config, issue: IssueRecord, body_chars: int) -> str:
    """Stage one: is this worth a slot in the feed?"""
    return _fill(
        load_prompt("classify").template,
        focus=config.focus.strip(),
        topics=", ".join(config.topics) or "(none)",
        title=issue.title,
        source=issue.source or "unknown",
        url=issue.url or "unknown",
        body=issue.text.strip()[:body_chars] or "(no article text was captured)",
    )


def summarize_prompt(config: Config, issue: IssueRecord) -> str:
    """Stage two: the write-up, for an article that already got through."""
    return _fill(
        load_prompt("summarize").template,
        focus=config.focus.strip(),
        title=issue.title,
        source=issue.source or "unknown",
        url=issue.url or "unknown",
        body=issue.text.strip()[: config.max_body_chars] or "(no article text was captured)",
    )


def digest_prompt(config: Config, days: int, issues: Sequence[IssueRecord]) -> str:
    """One roundup over everything the feed published in the window."""
    entries: list[str] = []
    for issue in issues:
        gist = " ".join((issue.summary or issue.text).split())[:DIGEST_SUMMARY_CHARS]
        entries += [
            f"- Title: {issue.title}",
            f"  URL: {issue.url or issue.html_url}",
            f"  Summary: {gist or '(none)'}",
        ]

    return _fill(
        load_prompt("digest").template,
        focus=config.focus.strip(),
        count=str(len(issues)),
        days=str(days),
        articles="\n".join(entries),
    )
