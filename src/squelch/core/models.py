"""Domain models shared by every stage of the pipeline."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from .urls import canonicalize, url_uid


class Status(StrEnum):
    """Lifecycle of an article. The label on the issue is the source of truth."""

    RAW = "status:1-raw"
    # Judged worth publishing, but not yet written up. The two steps are
    # separate so the cheap model can do the judging and the expensive one
    # only ever sees articles that survived it.
    RELEVANT = "status:2-relevant"
    READY = "status:3-ready"
    PUBLISHED = "status:4-published"
    REJECTED = "status:rejected"


ALL_STATUSES = tuple(Status)

# Feeds in the wrong timezone, and servers with a drifting clock, both produce
# dates a few hours ahead of ours. Anything further out is not a clock problem.
CLOCK_SKEW = timedelta(days=2)


class RawArticle(BaseModel):
    """A scraped article, before the LLM has seen it."""

    title: str
    url: str
    source: str
    published_at: datetime | None = None
    body: str = ""
    # The picture the publisher put on the article for link previews. Optional
    # everywhere downstream: plenty of pages have none.
    image: str = ""

    @field_validator("published_at")
    @classmethod
    def _plausible_date(cls, value: datetime | None) -> datetime | None:
        # Dropped rather than rejected, like the image: a date we cannot trust
        # is worth less than the article, and "no date" already has a meaning
        # downstream — the reader is shown when we found it instead.
        if value is None:
            return None
        stamped = value if value.tzinfo else value.replace(tzinfo=UTC)
        return None if stamped > datetime.now(UTC) + CLOCK_SKEW else stamped

    @field_validator("image")
    @classmethod
    def _usable_image(cls, value: str) -> str:
        # Dropped rather than rejected — a bad picture must never cost us the
        # article it came with.
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return ""
        return value.strip()

    @field_validator("url")
    @classmethod
    def _canonical(cls, value: str) -> str:
        canonical = canonicalize(value)
        # feedparser falls back to an entry's <id> when it carries no <link>,
        # which yields things like "urn:uuid:...". Those are not fetchable, and
        # publishing one would produce a headline linking nowhere.
        parts = urlsplit(canonical)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"not a usable article URL: {value!r}")
        return canonical

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str) -> str:
        collapsed = " ".join(value.split())
        if not collapsed:
            raise ValueError("title must not be empty")
        # GitHub rejects issue titles over 256 chars.
        return collapsed[:250]

    @property
    def uid(self) -> str:
        return url_uid(self.url)


class Classification(BaseModel):
    """Stage one: does this belong in the feed at all?

    Deliberately carries no summary. Asking one call to both judge and write
    makes the judgement worse — the model has already started composing the
    pitch — and it spends tokens on articles that are about to be closed.
    """

    relevant: bool = Field(description="True if this is substantive news worth publishing")
    reason: str = Field(description="One short sentence explaining the decision")
    tags: list[str] = Field(
        default_factory=list,
        description="Topic tags chosen from the allowed list. At most 3.",
    )
    score: int = Field(default=0, ge=0, le=10, description="Importance, 0-10")


class Summary(BaseModel):
    """Stage two: the write-up, produced only for articles that got through."""

    summary: str = Field(description="2-4 sentence summary of the article, in plain English")


class SkillNote(BaseModel):
    """One skill as it actually exists in the repository, not as it is advertised."""

    name: str = Field(description="The skill's own name, as the repository's files give it")
    does: str = Field(description="What it actually does, in one plain sentence")
    verdict: Literal["real", "thin", "unclear"] = Field(
        description=(
            "real: instructions or code with substance behind them. "
            "thin: a paragraph of prompt dressed up as a tool. "
            "unclear: the files do not say enough to tell."
        )
    )


class SkillsReview(BaseModel):
    """A reading of a skill collection: what is in it, and whether it earns its stars.

    Literal rather than an enum class: pydantic inlines a Literal into the JSON
    schema, while an enum becomes a ``$ref`` into ``$defs`` that the response
    schema travelling with the request cannot be relied on to follow.
    """

    verdict: Literal["substance", "mixed", "hype"] = Field(
        description="Whether the repository is worth a practitioner's time, on the whole"
    )
    promise: str = Field(
        description=(
            "One sentence on whether what is inside matches what the repository claims "
            "about itself. Name the gap when there is one."
        )
    )
    usefulness: str = Field(
        description=(
            "One or two sentences on who would get real value out of this today, "
            "or why nobody would."
        )
    )
    skills: list[SkillNote] = Field(
        default_factory=list,
        description="Every skill the repository actually contains, in the order they appear",
    )


class DigestEntry(BaseModel):
    """One line of the weekly digest."""

    title: str
    url: str
    takeaway: str


class Digest(BaseModel):
    """The weekly roundup produced from published articles."""

    headline: str = Field(description="One sentence naming the theme of the week")
    trends: list[str] = Field(description="2-5 trends observed across the week")
    highlights: list[DigestEntry] = Field(description="Up to 8 most notable articles")
