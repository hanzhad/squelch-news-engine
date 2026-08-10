"""Domain models shared by every stage of the pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from .urls import canonicalize, url_uid


class Status(StrEnum):
    """Lifecycle of an article. The label on the issue is the source of truth."""

    RAW = "status:1-raw"
    READY = "status:2-ready"
    PUBLISHED = "status:3-published"
    REJECTED = "status:rejected"


ALL_STATUSES = tuple(Status)


class RawArticle(BaseModel):
    """A scraped article, before the LLM has seen it."""

    title: str
    url: str
    source: str
    published_at: datetime | None = None
    body: str = ""

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


class Verdict(BaseModel):
    """The LLM's judgement on one article. Also used as the response schema."""

    relevant: bool = Field(description="True if this is substantive news worth publishing")
    reason: str = Field(description="One short sentence explaining the decision")
    summary: str = Field(
        default="",
        description="2-4 sentence summary of the article. Empty when relevant is false.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Topic tags chosen from the allowed list. At most 3.",
    )
    score: int = Field(default=0, ge=0, le=10, description="Importance, 0-10")


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
