"""Tests for ``scripts/merge_pulse_history.py``.

The merge only runs when a push race has already happened, so it gets no
exercise in the happy path — these tests are the only thing standing
between a rare conflict and a lost pulse record.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.suppressions import PulseEntry, load_history, save_history
from scripts.merge_pulse_history import merge_entries, merge_files


def _entry(pulse_id: str, customer_id: str = "c1", **extra: Any) -> dict[str, Any]:
    pulse_date, rep_id = pulse_id[:10], pulse_id[11:]
    return {
        "customer_id": customer_id,
        "rep_id": rep_id,
        "pulse_id": pulse_id,
        "date": pulse_date,
        **extra,
    }


# ---- merge_entries ----------------------------------------------------------


def test_union_keeps_entries_unique_to_either_side() -> None:
    ours = [_entry("2026-08-18-henry")]
    theirs = [_entry("2026-08-17-henry", customer_id="c0")]

    merged = merge_entries(ours, theirs)

    assert [e["pulse_id"] for e in merged] == ["2026-08-17-henry", "2026-08-18-henry"]


def test_identical_entries_collapse_to_one() -> None:
    entry = _entry("2026-08-18-henry", draft_text="Hey Whitson!")

    merged = merge_entries([dict(entry)], [dict(entry)])

    assert merged == [entry]


def test_remote_action_update_wins_over_our_pre_action_copy() -> None:
    """inbox-poll recorded SENT on main while our dispatch run was mid-flight."""
    ours = [_entry("2026-08-18-henry", draft_text="Hey Whitson!")]
    theirs = [
        _entry(
            "2026-08-18-henry",
            draft_text="Hey Whitson!",
            action="sent",
            action_at="2026-08-18",
        )
    ]

    (merged,) = merge_entries(ours, theirs)

    assert merged["action"] == "sent"
    assert merged["draft_text"] == "Hey Whitson!"


def test_our_action_update_survives_a_remote_side_without_one() -> None:
    """The mirror case: we're inbox-poll, main only has the send-time entry."""
    ours = [
        _entry(
            "2026-08-18-henry",
            action="edited",
            action_at="2026-08-18",
            edited_text="Hey Whitson, checking in.",
        )
    ]
    theirs = [_entry("2026-08-18-henry", draft_text="Hey Whitson!")]

    (merged,) = merge_entries(ours, theirs)

    assert merged["action"] == "edited"
    assert merged["edited_text"] == "Hey Whitson, checking in."
    # Send-time provenance from the remote side is preserved, not clobbered.
    assert merged["draft_text"] == "Hey Whitson!"


def test_later_action_at_wins_when_both_sides_recorded_one() -> None:
    ours = [_entry("2026-08-18-henry", action="skipped", action_at="2026-08-18")]
    theirs = [_entry("2026-08-18-henry", action="sent", action_at="2026-08-19")]

    (merged,) = merge_entries(ours, theirs)

    assert merged["action"] == "sent"


def test_same_day_repick_of_a_different_customer_keeps_both() -> None:
    """Two customers under one pulse_id means a duplicate send happened —
    the history must show both so the audit trail doesn't hide it."""
    ours = [_entry("2026-08-18-henry", customer_id="c2")]
    theirs = [_entry("2026-08-18-henry", customer_id="c1")]

    merged = merge_entries(ours, theirs)

    assert sorted(e["customer_id"] for e in merged) == ["c1", "c2"]


def test_output_is_sorted_by_date_rep_customer() -> None:
    ours = [_entry("2026-08-18-zack", customer_id="c9"), _entry("2026-08-18-henry")]
    theirs = [_entry("2026-08-17-henry", customer_id="c0")]

    merged = merge_entries(ours, theirs)

    assert [(e["date"], e["rep_id"]) for e in merged] == [
        ("2026-08-17", "henry"),
        ("2026-08-18", "henry"),
        ("2026-08-18", "zack"),
    ]


# ---- merge_files ------------------------------------------------------------


def test_merge_files_roundtrips_through_the_real_schema(tmp_path: Path) -> None:
    """The merged file must load cleanly via ``load_history`` — the merge
    runs inside the commit step, and a shape the loader rejects would
    only surface on the next morning's run."""
    ours_path, theirs_path, out_path = (
        tmp_path / "ours.json",
        tmp_path / "theirs.json",
        tmp_path / "out.json",
    )
    save_history(
        [
            PulseEntry(
                customer_id="c2",
                rep_id="henry",
                pulse_id="2026-08-18-henry",
                date=date(2026, 8, 18),
            )
        ],
        ours_path,
    )
    save_history(
        [
            PulseEntry(
                customer_id="c1",
                rep_id="zack",
                pulse_id="2026-08-17-zack",
                date=date(2026, 8, 17),
            )
        ],
        theirs_path,
    )

    assert merge_files(ours_path, theirs_path, out_path) == 2

    entries = load_history(out_path)
    assert [e.pulse_id for e in entries] == ["2026-08-17-zack", "2026-08-18-henry"]
    assert json.loads(out_path.read_text())["version"] == 1


def test_merge_files_treats_a_missing_side_as_empty(tmp_path: Path) -> None:
    theirs_path, out_path = tmp_path / "theirs.json", tmp_path / "out.json"
    theirs_path.write_text(json.dumps({"version": 1, "entries": [_entry("2026-08-18-henry")]}))

    assert merge_files(tmp_path / "absent.json", theirs_path, out_path) == 1
