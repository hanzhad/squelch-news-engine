"""The dedup ledger: a rolling window of uids, committed back to the repo."""

from __future__ import annotations

import json
from pathlib import Path

from squelch.core.seen import SeenLedger


def test_loading_a_missing_file_starts_an_empty_ledger(tmp_path: Path) -> None:
    ledger = SeenLedger(tmp_path / "never" / "written.json").load()

    assert len(ledger) == 0
    assert "anything" not in ledger


def test_save_then_load_round_trips_the_uids(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    writer = SeenLedger(path)
    for uid in ["aaa", "bbb", "ccc"]:
        writer.add(uid)
    writer.save()

    reader = SeenLedger(path).load()

    assert len(reader) == 3
    assert "bbb" in reader
    assert "zzz" not in reader


def test_save_creates_missing_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "data" / "nested" / "seen.json"
    ledger = SeenLedger(path)
    ledger.add("aaa")

    ledger.save()

    assert path.exists()


def test_the_saved_file_is_versioned_json(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    ledger = SeenLedger(path)
    ledger.add("aaa")
    ledger.save()

    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload == {"version": 1, "uids": ["aaa"]}
    # Trailing newline keeps the committed file diff-friendly.
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_adding_the_same_uid_twice_is_a_no_op(tmp_path: Path) -> None:
    ledger = SeenLedger(tmp_path / "seen.json")

    ledger.add("aaa")
    ledger.add("aaa")

    assert len(ledger) == 1


def test_membership_reflects_additions_before_saving(tmp_path: Path) -> None:
    ledger = SeenLedger(tmp_path / "seen.json")

    assert "aaa" not in ledger
    ledger.add("aaa")
    assert "aaa" in ledger


def test_saving_trims_to_the_newest_entries(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    ledger = SeenLedger(path, max_entries=5)
    for index in range(12):
        ledger.add(f"uid-{index:02d}")

    ledger.save()

    reloaded = SeenLedger(path).load()
    assert len(reloaded) == 5
    # The window keeps the most recently added uids, so a story scraped a
    # moment ago is still recognised as a duplicate.
    assert "uid-11" in reloaded
    assert "uid-07" in reloaded
    assert "uid-06" not in reloaded
    assert "uid-00" not in reloaded


def test_trimming_also_updates_the_in_memory_index(tmp_path: Path) -> None:
    ledger = SeenLedger(tmp_path / "seen.json", max_entries=2)
    for uid in ["old", "mid", "new"]:
        ledger.add(uid)

    ledger.save()

    assert "old" not in ledger
    assert "mid" in ledger
    assert "new" in ledger
    assert len(ledger) == 2


def test_a_ledger_at_exactly_the_window_size_is_not_trimmed(tmp_path: Path) -> None:
    ledger = SeenLedger(tmp_path / "seen.json", max_entries=3)
    for uid in ["a", "b", "c"]:
        ledger.add(uid)

    ledger.save()

    assert len(ledger) == 3
    assert "a" in ledger


def test_saving_twice_keeps_the_ledger_stable(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    ledger = SeenLedger(path, max_entries=3)
    for uid in ["a", "b", "c", "d"]:
        ledger.add(uid)

    ledger.save()
    first = path.read_text(encoding="utf-8")
    ledger.save()

    assert path.read_text(encoding="utf-8") == first


def test_reloading_preserves_insertion_order_for_the_next_trim(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    first = SeenLedger(path, max_entries=3)
    for uid in ["a", "b", "c"]:
        first.add(uid)
    first.save()

    second = SeenLedger(path, max_entries=3).load()
    second.add("d")
    second.save()

    assert json.loads(path.read_text(encoding="utf-8"))["uids"] == ["b", "c", "d"]


def test_load_returns_the_ledger_so_it_can_be_chained(tmp_path: Path) -> None:
    ledger = SeenLedger(tmp_path / "seen.json")

    assert ledger.load() is ledger
