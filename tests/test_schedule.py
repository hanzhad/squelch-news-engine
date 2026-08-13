"""The grid is generated, so the generator has to be the thing that is trusted.

Two halves. The unit tests fix the arithmetic and the file surgery — a stage
that claims a residue class must actually own it, and a rewrite must touch
nothing but the managed block. The last test is the one that matters in
practice: it runs the same check CI runs, against the shipped config and the
real workflows, so a hand-edited cron line fails here before it is merged.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from squelch.core.schedule import (
    BEGIN,
    END,
    Schedule,
    Stage,
    apply,
    current_crons,
    load_schedule,
    problems,
    render_block,
)

WORKFLOW = """\
name: feed / 4 publish

on:
  schedule:
    {begin}
    # an old comment nobody updated
    - cron: "1,2,3 * * * *"
    {end}
  workflow_dispatch:

concurrency:
  # This comment is outside the block and must survive untouched.
  group: feed-publish
"""


def make(**overrides: object) -> Stage:
    fields: dict[str, object] = {"every": 5, "offset": 3, "why": "because."}
    fields.update(overrides)
    return Stage.model_validate(fields)


def test_every_and_offset_expand_to_a_residue_class() -> None:
    assert make(every=5, offset=3).minutes == [3, 8, 13, 18, 23, 28, 33, 38, 43, 48, 53, 58]
    assert make(every=20, offset=17).minutes == [17, 37, 57]
    assert make(every=30, offset=0).minutes == [0, 30]
    assert make(every=60, offset=8).minutes == [8]


def test_cron_is_the_minutes_joined() -> None:
    assert make(every=30, offset=10).crons == ["10,40 * * * *"]


def test_every_must_divide_the_hour() -> None:
    # 7 would leave a 4-minute gap between :56 and :00, so the stated cadence
    # would be a lie once an hour.
    with pytest.raises(ValidationError, match="does not divide 60"):
        make(every=7)


def test_offset_must_name_the_class_it_belongs_to() -> None:
    with pytest.raises(ValidationError, match="offset: 8 is not below every: 5"):
        make(every=5, offset=8)


def test_a_stage_picks_exactly_one_form() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        Stage.model_validate({"every": 5, "cron": ["0 6 * * *"], "why": "both"})
    with pytest.raises(ValidationError, match="exactly one"):
        Stage.model_validate({"why": "neither"})


def test_a_stage_must_say_why() -> None:
    # The `why` is written into the workflow as the comment above the cron line.
    # Without it the generated block would explain nothing, which is most of
    # what the generated block is for.
    with pytest.raises(ValidationError, match="needs a `why`"):
        Stage.model_validate({"every": 5, "offset": 0})


def test_raw_cron_must_have_five_fields() -> None:
    with pytest.raises(ValidationError, match="not five fields"):
        Stage.model_validate({"cron": ["0 6 * *"], "why": "typo"})


def test_collisions_are_reported_per_minute() -> None:
    schedule = Schedule(
        stages={
            "a": make(every=20, offset=5),
            "b": make(every=30, offset=25),
        }
    )
    # :25 belongs to both — a permanent overlap, every hour, forever.
    assert schedule.owners()[25] == ["a", "b"]


def test_a_clean_grid_owns_each_minute_once() -> None:
    schedule = Schedule(stages={"a": make(every=5, offset=1), "b": make(every=5, offset=3)})
    assert all(len(names) == 1 for names in schedule.owners().values())


def test_apply_replaces_only_the_managed_block() -> None:
    text = WORKFLOW.format(begin=BEGIN, end=END)
    rebuilt = apply(text, make(every=30, offset=10, why="Ten minutes behind scrape."))

    assert current_crons(rebuilt) == ["10,40 * * * *"]
    assert "an old comment nobody updated" not in rebuilt
    assert "# Ten minutes behind scrape." in rebuilt
    # Everything outside the markers is untouched, comments included.
    assert "# This comment is outside the block and must survive untouched." in rebuilt
    assert rebuilt.startswith("name: feed / 4 publish")
    assert rebuilt.rstrip().endswith("group: feed-publish")


def test_apply_is_idempotent() -> None:
    text = WORKFLOW.format(begin=BEGIN, end=END)
    once = apply(text, make())
    assert apply(once, make()) == once


def test_apply_refuses_a_file_with_no_markers() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        apply("name: x\non:\n  schedule:\n    - cron: \"0 * * * *\"\n", make())


def test_long_why_wraps_to_the_project_line_length() -> None:
    stage = make(why=" ".join(["word"] * 60))
    assert all(len(line) <= 100 for line in render_block(stage))


def test_the_shipped_schedule_matches_the_workflows_on_disk() -> None:
    """The check CI runs: no drift, no collisions, no workflow left off the map."""
    assert problems(load_schedule()) == []
