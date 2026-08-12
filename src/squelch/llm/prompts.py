"""Assembling prompts from the files in ``prompts/``.

What this feed publishes is tuned by rewriting sentences, not logic, so the
wording lives in ``prompts/<stage>.md`` — one file per stage, at the top level
where the writing is easy to find and a diff on it reads like a diff on prose —
and the model ids live in ``config/models.yaml``, because Google retires those
on its own schedule. This module only fills in the placeholders.

``string.Template`` rather than ``str.format``: prompt text is prose that may
well contain literal braces, and a stray one should not blow up a run.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from string import Template

import yaml
from pydantic import BaseModel

from ..core.config import CONFIG_DIR, Config
from ..core.models import Period
from ..core.text import trim
from ..github.issues import IssueRecord

PROMPTS_DIR = Path("prompts")
MODELS_PATH = CONFIG_DIR / "models.yaml"

# A level-two heading opens a section; everything before the first one is the
# file's own notes to whoever edits it, and never reaches the model.
_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)


class Prompt(BaseModel):
    system: str
    template: str


class Models(BaseModel):
    classify: str
    summarize: str
    review: str
    digest: str


def _sections(text: str) -> dict[str, str]:
    """The ``## Heading`` sections of a markdown file, keyed by lowercased name.

    Bodies are kept verbatim apart from the blank lines around them: the prompt
    is whitespace-sensitive prose, so nothing here reflows, unindents or
    otherwise interprets it as markdown.
    """
    parts = _HEADING.split(text)
    names, bodies = parts[1::2], parts[2::2]
    return {name.lower(): body.strip("\n") for name, body in zip(names, bodies, strict=True)}


@lru_cache
def load_prompt(stage: str, directory: Path | None = None) -> Prompt:
    path = (directory or PROMPTS_DIR) / f"{stage}.md"
    sections = _sections(path.read_text(encoding="utf-8"))
    missing = [name for name in ("system", "template") if not sections.get(name, "").strip()]
    if missing:
        # Worth naming loudly: a renamed heading leaves a file that still reads
        # fine to a human while silently sending half a prompt.
        raise ValueError(
            f"{path} has no {' and no '.join(f'`## {name.title()}`' for name in missing)} "
            "section — see prompts/README.md for the format"
        )
    return Prompt(system=sections["system"], template=sections["template"])


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


def review_prompt(config: Config, issue: IssueRecord) -> str:
    """The rubric's stage: what is actually in this collection, skill by skill.

    Reads the same body as the write-up rather than the classifier's short
    window — the inventory of skills is the whole subject here, and it sits
    further into the text than 1500 characters.
    """
    return _fill(
        load_prompt("review").template,
        focus=config.focus.strip(),
        title=issue.title,
        source=issue.source or "unknown",
        url=issue.url or "unknown",
        body=issue.text.strip()[: config.max_body_chars] or "(no repository text was captured)",
    )


def digest_prompt(
    config: Config, period: Period, days: int, issues: Sequence[IssueRecord]
) -> str:
    """One roundup over everything the feed published in the window.

    The period chooses the file, not a branch in here: the daily and the weekly
    ask for different writing, and the difference belongs in prose that can be
    rewritten without a deploy.
    """
    entries: list[str] = []
    for issue in issues:
        # The write-up the feed actually published, not the article behind it.
        # A roundup that read the raw text could say things the channel and the
        # archive never said — the same guarantee _verified_highlights gives for
        # links, one level down. Raw text is the fallback for issues opened by
        # hand, which have no write-up, and that is what the cap really bounds.
        gist = trim(" ".join((issue.summary or issue.text).split()), config.digest.summary_chars)
        entries += [
            f"- Title: {issue.title}",
            f"  URL: {issue.url or issue.html_url}",
            f"  Summary: {gist or '(none)'}",
        ]

    return _fill(
        load_prompt(period.stage).template,
        focus=config.focus.strip(),
        count=str(len(issues)),
        days=str(days),
        articles="\n".join(entries),
    )
