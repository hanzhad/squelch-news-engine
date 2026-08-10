"""Assembling prompts from the text in ``config/prompts.yaml``.

What this feed publishes is tuned by rewriting sentences, not logic, so the
wording lives in a config file that can be read and edited without opening the
code. This module only fills in the placeholders.

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

from ..core.config import Config
from ..github.issues import IssueRecord

PROMPTS_PATH = Path("config/prompts.yaml")

# Enough of each article for the model to see the theme, short enough that forty
# of them still make a small request.
DIGEST_SUMMARY_CHARS = 400


class PromptPair(BaseModel):
    system: str
    template: str


class Prompts(BaseModel):
    filter: PromptPair
    digest: PromptPair


@lru_cache
def load_prompts(path: Path | None = None) -> Prompts:
    target = path or PROMPTS_PATH
    return Prompts.model_validate(yaml.safe_load(target.read_text(encoding="utf-8")))


def _fill(template: str, **values: str) -> str:
    # safe_substitute so an unknown or mistyped placeholder survives as text
    # instead of taking the whole run down.
    return Template(template).safe_substitute(**values).strip()


def filter_system() -> str:
    return load_prompts().filter.system.strip()


def digest_system() -> str:
    return load_prompts().digest.system.strip()


def filter_prompt(config: Config, issue: IssueRecord) -> str:
    """The judgement call on a single article."""
    return _fill(
        load_prompts().filter.template,
        focus=config.focus.strip(),
        topics=", ".join(config.topics) or "(none)",
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
        load_prompts().digest.template,
        focus=config.focus.strip(),
        count=str(len(issues)),
        days=str(days),
        articles="\n".join(entries),
    )
