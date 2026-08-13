"""The cron grid in one file, and the code that puts it into the workflows.

GitHub reads a schedule from nowhere but the workflow file itself — no
expressions, no `vars.`, no include — so eighteen files each own a literal
`cron:` line and the grid as a whole exists in no single place. Changing when a
stage runs therefore means re-deriving by hand which minutes are still free,
and the answer is spread across eighteen comment blocks that go stale quietly.

This module is the missing half. ``config/schedule.yaml`` is the source of
truth; ``squelch schedule --write`` stamps it into the managed block of each
workflow, comment and all, so the rationale cannot drift from the cron line
that implements it; ``--check`` fails CI when somebody edits a workflow by hand
or when two stages land on the same minute.

The rule the grid encodes is one thing: **whether a stage spends an API
request.** Those are paced by the quota and keep sparse slots. Everything else
costs a runner minute, which is free, so it takes a whole residue class.
``Stage.model`` records which is which, and the printed grid shows it, because
that is the question to ask before making anything more frequent.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from .config import CONFIG_DIR

WORKFLOW_DIR = Path(".github/workflows")
SCHEDULE_PATH = CONFIG_DIR / "schedule.yaml"

# The managed region inside a workflow's `on.schedule:`. Everything between the
# two markers is rewritten wholesale, so a hand edit there is discarded rather
# than merged — which is the point of `--check` running in CI.
BEGIN = "# schedule:begin"
END = "# schedule:end"
_CRON_LINE = re.compile(r'^\s*- cron: ".*"\s*$')

# A raw cron entry, loosely: five whitespace-separated fields. Not a validator
# for cron semantics — GitHub is the authority on those — just enough to catch
# a field dropped while editing.
_RAW_CRON = re.compile(r"^\S+(\s+\S+){4}$")


class Stage(BaseModel):
    """When one workflow runs, and why it is allowed to run that often.

    Three mutually exclusive ways to say it, because the eighteen workflows
    genuinely differ:

    ``every`` + ``offset``
        The hourly grid. ``every: 5, offset: 3`` means :03, :08, :13 … — one
        residue class, wholly owned. This is the form the collision check
        understands, and the form every free stage should be in.
    ``cron``
        Anything that is not hourly: the daily roundup, the weekly one, the
        source sweep. Written out verbatim.
    ``event``
        Not scheduled at all — CI, the label sync, triage, the site build.
        Listed anyway, with the trigger named here, so this file is a complete
        map of what runs and nothing has to be discovered by grepping. Nothing
        is written into these workflows.
    """

    # Minutes between runs, within the hour. Must divide 60, or the last gap of
    # the hour would be shorter than the rest and the cadence would be a lie.
    every: int | None = Field(default=None, ge=1, le=60)
    # The first minute of the class. Kept below `every` so a stage's residue is
    # readable straight off the number rather than needing a modulo in the head.
    offset: int = Field(default=0, ge=0, le=59)
    cron: list[str] = Field(default_factory=list)
    event: str = ""
    # Whether this stage spends an LLM request. The one fact that decides how
    # often it is allowed to run, recorded next to the cadence it justifies.
    model: bool = False
    why: str = ""

    @model_validator(mode="after")
    def _exactly_one_form(self) -> Stage:
        forms = [self.every is not None, bool(self.cron), bool(self.event)]
        if sum(forms) != 1:
            raise ValueError(
                "a stage sets exactly one of `every` (hourly grid), `cron` (raw) "
                "or `event` (not scheduled); got "
                f"every={self.every!r}, cron={self.cron!r}, event={self.event!r}"
            )
        if self.every is not None:
            if 60 % self.every:
                raise ValueError(
                    f"every: {self.every} does not divide 60, so the last gap of the hour would "
                    "be shorter than the rest — pick 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30 or 60"
                )
            if self.offset >= self.every:
                raise ValueError(
                    f"offset: {self.offset} is not below every: {self.every}; "
                    f"write offset: {self.offset % self.every} for the same grid"
                )
        for entry in self.cron:
            if not _RAW_CRON.match(entry.strip()):
                raise ValueError(f"cron entry {entry!r} is not five fields")
        if not self.why:
            raise ValueError("every stage needs a `why` — it is the comment written into the file")
        return self

    @property
    def minutes(self) -> list[int]:
        """The minutes of every hour this stage fires on, or empty."""
        if self.every is None:
            return []
        return sorted((self.offset + step * self.every) % 60 for step in range(60 // self.every))

    @property
    def crons(self) -> list[str]:
        """The literal cron expressions this stage should carry."""
        if self.every is not None:
            return [f"{','.join(str(m) for m in self.minutes)} * * * *"]
        return [entry.strip() for entry in self.cron]

    @property
    def cadence(self) -> str:
        """How often this runs, in words, for the printed grid."""
        if self.event:
            return f"on {self.event}"
        if self.every is not None:
            per_hour = 60 // self.every
            klass = f":x{self.offset % 5}" if self.every % 5 == 0 else ""
            every = f"every {self.every} min" if self.every > 1 else "every minute"
            return f"{every} ({per_hour}/h){f' — class {klass}' if klass else ''}"
        return ", ".join(self.crons)


class Schedule(BaseModel):
    stages: dict[str, Stage]

    @property
    def scheduled(self) -> dict[str, Stage]:
        """The stages that carry a cron, in file order."""
        return {name: s for name, s in self.stages.items() if not s.event}

    @property
    def hourly(self) -> dict[str, Stage]:
        """The stages on the hourly grid — the ones a collision can be found in."""
        return {name: s for name, s in self.stages.items() if s.every is not None}

    def owners(self) -> dict[int, list[str]]:
        """Which stage owns each minute of the hour."""
        owners: dict[int, list[str]] = {}
        for name, stage in self.hourly.items():
            for minute in stage.minutes:
                owners.setdefault(minute, []).append(name)
        return owners


def load_schedule(path: Path | None = None) -> Schedule:
    data = yaml.safe_load((path or SCHEDULE_PATH).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "stages" not in data:
        raise ValueError(f"{path or SCHEDULE_PATH} must contain a `stages:` mapping")
    return Schedule.model_validate(data)


def workflow_path(name: str, root: Path | None = None) -> Path:
    return (root or WORKFLOW_DIR) / f"{name}.yml"


def _wrap(text: str, width: int, indent: str) -> list[str]:
    """`why` as comment lines, hard-wrapped to the project's 100 columns."""
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(indent) + 2 + len(candidate) > width and current:
            lines.append(f"{indent}# {current}")
            current = word
        else:
            current = candidate
    if current:
        lines.append(f"{indent}# {current}")
    return lines


def render_block(stage: Stage, indent: str = "    ") -> list[str]:
    """The full managed region for one stage, markers included.

    The opening marker says where to edit instead, because the person who finds
    this block is by definition looking for the cron line and about to change
    it in the wrong file.
    """
    lines = [f"{indent}{BEGIN} — generated from config/schedule.yaml; edit there, `make schedule`"]
    lines += _wrap(stage.why, 100, indent)
    lines += [f'{indent}- cron: "{entry}"' for entry in stage.crons]
    lines.append(f"{indent}{END}")
    return lines


def apply(text: str, stage: Stage) -> str:
    """Replace the managed region of one workflow's text.

    Everything outside the markers is left byte for byte, because the rest of
    these files is comment that took longer to write than the code did.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip().startswith(BEGIN)]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise ValueError(
            f"expected exactly one {BEGIN} … {END} region under `schedule:`; "
            f"found {len(starts)} begin and {len(ends)} end markers"
        )
    indent = lines[starts[0]][: len(lines[starts[0]]) - len(lines[starts[0]].lstrip())]
    block = render_block(stage, indent)
    rebuilt = lines[: starts[0]] + block + lines[ends[0] + 1 :]
    return "\n".join(rebuilt) + "\n"


def current_crons(text: str) -> list[str]:
    """The cron expressions a workflow's text actually carries."""
    return [
        line.strip().removeprefix('- cron: "').removesuffix('"')
        for line in text.splitlines()
        if _CRON_LINE.match(line)
    ]


def problems(schedule: Schedule, root: Path | None = None) -> list[str]:
    """Everything wrong with the grid as it stands, worst first.

    Three separate failures, all of which have to be loud. A stage in the config
    with no workflow (or the other way round) means the map is lying. Two stages
    on the same minute means they queue behind each other on the runner every
    hour, forever. And a workflow whose cron no longer matches the config means
    somebody edited the generated block and their edit is about to be discarded.
    """
    found: list[str] = []
    directory = root or WORKFLOW_DIR

    on_disk = {path.stem for path in sorted(directory.glob("*.yml"))}
    for missing in sorted(on_disk - set(schedule.stages)):
        found.append(f"{missing}.yml exists but is not in {SCHEDULE_PATH}")
    for extra in sorted(set(schedule.stages) - on_disk):
        found.append(f"{SCHEDULE_PATH} names {extra}, which has no workflow file")

    for minute, names in sorted(schedule.owners().items()):
        if len(names) > 1:
            found.append(f"minute :{minute:02d} is claimed by {' and '.join(sorted(names))}")

    for name, stage in schedule.stages.items():
        path = workflow_path(name, directory)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if stage.event:
            if current_crons(text):
                found.append(f"{name} is marked `event` but its workflow carries a cron")
            continue
        try:
            if apply(text, stage) != text:
                found.append(f"{name} is out of date — run `squelch schedule --write`")
        except ValueError as exc:
            found.append(f"{name}: {exc}")
    return found


def write(schedule: Schedule, root: Path | None = None) -> list[str]:
    """Stamp the config into every scheduled workflow. Returns what changed."""
    changed: list[str] = []
    for name, stage in schedule.scheduled.items():
        path = workflow_path(name, root)
        text = path.read_text(encoding="utf-8")
        rebuilt = apply(text, stage)
        if rebuilt != text:
            path.write_text(rebuilt, encoding="utf-8")
            changed.append(name)
    return changed


def grid(schedule: Schedule) -> list[str]:
    """The grid as lines to print: one row per stage, model stages marked.

    Sorted by how often a stage runs rather than by name, because the question
    this table exists to answer is "what is allowed to be frequent, and why".
    """
    width = max(len(name) for name in schedule.stages)

    def rank(item: tuple[str, Stage]) -> tuple[int, str]:
        stage = item[1]
        return (stage.every if stage.every is not None else 999, item[0])

    lines = []
    for name, stage in sorted(schedule.stages.items(), key=rank):
        mark = "LLM" if stage.model else "   "
        lines.append(f"  {mark}  {name:<{width}}  {stage.cadence}")
    free = sorted(set(range(60)) - set(schedule.owners()))
    lines.append("")
    lines.append(
        f"  {60 - len(free)}/60 minutes of the hour owned"
        + (f", free: {', '.join(f':{m:02d}' for m in free)}" if free else ", none free")
    )
    return lines
