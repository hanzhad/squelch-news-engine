"""Domain models shared by every stage of the pipeline."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
    # Whatever this source could count rather than read — stars, files, how
    # many skills a repository actually ships. Kept apart from the body so a
    # number a reader is going to act on never travels through the LLM, and
    # left empty by every source that has nothing to count. Measured when the
    # article was scraped, which for a repository gathering stars by the hour
    # is the only honest thing it could be.
    facts: dict[str, int] = Field(default_factory=dict)

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
        description=(
            "How much of this repository is its own work with something behind it. "
            "substance: original skills carrying real instructions, code or reference. "
            "mixed: some of that among filler, or someone else's work with a real "
            "addition on top. hype: nothing of its own — a link list, a mirror, a "
            "translation, install instructions. Judge the contents, not whether the "
            "README was honest about them: a repository that accurately describes "
            "itself as a copy of someone else's work is still a copy."
        )
    )
    promise: str = Field(
        description=(
            "One sentence on whether what is inside matches what the repository claims "
            "about itself. Name the gap when there is one."
        )
    )
    usefulness: str = Field(
        description=(
            "One or two sentences naming who would open this and for what task, "
            "or saying plainly that nobody would. No 'immediate value', no "
            "'production-ready' — the job, not the pitch."
        )
    )
    skills: list[SkillNote] = Field(
        default_factory=list,
        description="Every skill the repository actually contains, in the order they appear",
    )


class CaseStatus(StrEnum):
    """Lifecycle of a community case — somebody's own experiment, posted in the forum.

    A separate axis from ``Status`` on purpose, and that is the whole design.
    A case is not an article: it was written by a person who wanted an answer,
    not scraped, and it must never be classified, summarised, rendered on the
    site or fed to a roundup. Carrying no ``status:`` label at all is what makes
    every one of those queries step over it — the same trick the digests and the
    ledger use.

    There is no rejected state here either. The classifier's job is to keep the
    feed clean of noise nobody chose; a case was posted deliberately by a member
    of the community, and answering only the cases we happen to like is how a
    forum stops being one.
    """

    NEW = "case:1-new"
    # The reading is written and stored, waiting for its reply to be posted.
    # Separate from answered for the same reason a digest is written before it
    # is sent: the model's answer survives a dead webhook and can be read — or
    # edited — before it goes back to the person who asked.
    READ = "case:2-read"
    ANSWERED = "case:3-answered"


ALL_CASE_STATUSES = tuple(CaseStatus)


class CasePost(BaseModel):
    """One forum post, as the bot found it. The thread is the unit, not the message.

    Only the opening message is read. Everything under it is the discussion the
    reply is meant to start, and a bot that answered the whole thread would be
    answering itself a tick later.
    """

    thread_id: str
    title: str
    body: str = ""
    author: str = ""
    url: str = ""
    posted_at: datetime | None = None
    # Whatever the poster tagged it with, by name rather than by Discord id: the
    # names are what the reading is allowed to see, and the ids mean nothing to
    # a model.
    tags: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str) -> str:
        collapsed = " ".join(value.split())
        if not collapsed:
            # Discord will not create an unnamed forum post, so this is only
            # reachable through a malformed payload.
            raise ValueError("a forum post must have a name")
        return collapsed[:250]

    @field_validator("thread_id")
    @classmethod
    def _snowflake(cls, value: str) -> str:
        digits = value.strip()
        if not digits.isdigit():
            raise ValueError(f"not a Discord id: {value!r}")
        return digits

    @property
    def uid(self) -> str:
        """What makes this post the same post on the next run.

        The thread id, which Discord never reuses — not a hash of the text. A
        post edited after it was read must not come back as a second case, and
        an edit is exactly what people do when they remember a number.
        """
        return self.thread_id


class CaseReading(BaseModel):
    """What the bot has to say about one case.

    Deliberately four fields rather than a verdict. A case is somebody's own
    measurement, usually partial, often about numbers the model cannot check
    from here — and a single block of prose asked to respond to that reliably
    turns into either applause or a confident correction invented on the spot.
    Splitting the answer is what stops both: the claim is restated in the
    poster's own terms, what could settle it is named as *work*, and what the
    post is taking on faith is named without being called wrong.

    There is no score, no verdict and no "quality" field, and there must not be
    one. Nothing here decides whether a post stays: the reply is a reply, and a
    forum where a bot rates your work is a forum people stop posting in.
    """

    claim: str = Field(
        description=(
            "What this post reports, restated in one or two plain sentences, in the "
            "language the post was written in. The finding, not the topic."
        )
    )
    checkable: list[str] = Field(
        default_factory=list,
        description=(
            "The parts that could actually be settled, each with the measurement or "
            "source that would settle it. Empty if the post is pure experience report."
        ),
    )
    assumed: list[str] = Field(
        default_factory=list,
        description=(
            "What the post takes for granted or generalises past its own evidence. "
            "Stated as a gap in what was shown, never as an accusation, and empty "
            "when the post stayed inside what it measured."
        ),
    )
    measure: list[str] = Field(
        default_factory=list,
        description=(
            "Concrete next steps somebody could run themselves to take this further — "
            "the experiment, not the advice."
        ),
    )


class Period(StrEnum):
    """How far back one roundup looks.

    Two digests share one channel: the daily is what happened since yesterday,
    read the morning after, and the weekly is the look back that only makes
    sense once the week is over. They are different pieces of writing rather
    than the same one over a longer window — a day rarely carries a trend, a
    week is mostly trends — so each has a prompt file of its own and this is
    what picks between them.
    """

    DAILY = "daily"
    WEEKLY = "weekly"

    @property
    def days(self) -> int:
        return 1 if self is Period.DAILY else 7

    @property
    def stage(self) -> str:
        """The prompt file this roundup is written from."""
        return f"digest-{self.value}"

    @property
    def label(self) -> str:
        """What the roundup calls itself in the channel and in the logs."""
        return f"{self.value} digest"

    def window_label(self, start: date, end: date) -> str:
        """The stretch this roundup covers, as a reader would say it.

        A daily is named after the day the news happened on — its window runs
        from yesterday morning to this one, and nobody calls that "the 11th to
        the 12th". A weekly gets the range, written short where it can be:
        "5–11 August 2026", but "29 July – 4 August 2026" across a month.
        """
        if self is Period.DAILY:
            return f"{start.day} {start:%B %Y}"
        if (start.year, start.month) == (end.year, end.month):
            return f"{start.day}–{end.day} {end:%B %Y}"
        if start.year == end.year:
            return f"{start.day} {start:%B} – {end.day} {end:%B %Y}"
        return f"{start.day} {start:%B %Y} – {end.day} {end:%B %Y}"


class DigestEntry(BaseModel):
    """One line of a digest: a headline and where to read it.

    Deliberately carries no per-article commentary. What each release means is
    the body's job, said once across everything; repeating it item by item
    turned the roundup back into a list of captions, which is the thing the
    body exists to replace.
    """

    title: str
    url: str


class Digest(BaseModel):
    """A roundup produced from published articles. Same shape either period.

    Read in layers, and the layering is the design. One paragraph asked to be
    both skimmable and synthesised produced neither: made plain it turned into
    a sentence per article, made analytical it turned into prose nobody
    finishes. So ``brief`` is what somebody exhausted gets in two sentences,
    ``summary`` is the block underneath for whoever went on, and each is judged
    by its own rules.

    There is no headline field. It named the archived issue and nothing else,
    and once ``brief`` existed the two were the same sentence written twice —
    a field the model fills only because it is there, which is exactly how
    trends started leaking into the daily.

    The descriptions travel with the request as the response schema, so they
    say nothing about a day or a week: the rules that differ between the two
    live in the prompt files, where they can be rewritten without a deploy.
    ``highlights`` used to say "up to 8", which is the weekly's cap — the daily
    asks for six, so the model was handed both numbers in one request and the
    schema quietly outranked the prose. Counts belong in the prompt files with
    everything else that differs; a number here is the bug, not a detail of it.
    """

    brief: str = Field(
        description=(
            "One or two sentences, in the plainest words available: the whole point, "
            "for somebody too tired to read further. Never a list."
        )
    )
    summary: str = Field(
        description=(
            "The detailed block, for a reader who did go on: connected prose grouping "
            "what belongs together and ending with what it all adds up to. Never a "
            "restatement of the article titles one by one."
        )
    )
    trends: list[str] = Field(
        default_factory=list,
        description="Threads visible across more than one article. None, if there are none.",
    )
    highlights: list[DigestEntry] = Field(
        description=(
            "The articles worth opening, most important first, each copied from the "
            "list given. How many belongs to the roundup's own instructions."
        )
    )
