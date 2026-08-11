"""Tests for ``pipeline/suppressions.py``.

Pure unit tests — no sockets (blocked at conftest), no live I/O beyond
``tmp_path``. The roundtrip test exercises load/save against a real
filesystem path so we'd catch schema drift if anyone changed the JSON
shape without bumping ``SCHEMA_VERSION``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from pipeline.suppressions import (
    SCHEMA_VERSION,
    PulseEntry,
    append_entries,
    load_history,
    recent_edit_examples,
    recently_pulsed_customer_ids,
    reps_pulsed_today,
    save_history,
    strip_compose_boilerplate,
)


def _entry(
    customer_id: str = "c1",
    rep_id: str = "henry",
    pulse_id: str = "p1",
    d: date = date(2026, 5, 1),
) -> PulseEntry:
    return PulseEntry(customer_id=customer_id, rep_id=rep_id, pulse_id=pulse_id, date=d)


# ---- recently_pulsed_customer_ids -------------------------------------------


def test_recently_pulsed_returns_only_ids_inside_window() -> None:
    today = date(2026, 5, 14)
    entries = [
        _entry(customer_id="recent", d=date(2026, 5, 1)),  # 13 days ago, in window
        _entry(customer_id="old", d=date(2025, 12, 1)),  # >90 days ago, out
        _entry(customer_id="boundary", d=date(2026, 2, 13)),  # exactly 90 days, in
    ]
    ids = recently_pulsed_customer_ids(entries, today, window_days=90)
    assert ids == frozenset({"recent", "boundary"})


def test_recently_pulsed_empty_when_no_entries() -> None:
    assert recently_pulsed_customer_ids([], date(2026, 5, 14)) == frozenset()


def test_recently_pulsed_dedupes_repeat_customers() -> None:
    today = date(2026, 5, 14)
    entries = [
        _entry(customer_id="c1", pulse_id="p1", d=date(2026, 4, 1)),
        _entry(customer_id="c1", pulse_id="p2", d=date(2026, 5, 1)),
    ]
    assert recently_pulsed_customer_ids(entries, today) == frozenset({"c1"})


# ---- reps_pulsed_today ------------------------------------------------------


def test_reps_pulsed_today_returns_only_todays_rep_ids() -> None:
    today = date(2026, 5, 19)
    entries = [
        _entry(rep_id="henry", d=today),
        _entry(rep_id="zack", d=date(2026, 5, 18)),  # yesterday — not today
        _entry(rep_id="alice", d=today),
    ]
    assert reps_pulsed_today(entries, today) == frozenset({"henry", "alice"})


def test_reps_pulsed_today_empty_when_no_entries_today() -> None:
    entries = [_entry(rep_id="henry", d=date(2026, 5, 18))]
    assert reps_pulsed_today(entries, date(2026, 5, 19)) == frozenset()


def test_reps_pulsed_today_dedupes_same_rep() -> None:
    """A rep with multiple entries today (shouldn't happen, but defensive)
    still appears as a single ID in the set.
    """
    today = date(2026, 5, 19)
    entries = [
        _entry(rep_id="henry", customer_id="c1", d=today),
        _entry(rep_id="henry", customer_id="c2", d=today),
    ]
    assert reps_pulsed_today(entries, today) == frozenset({"henry"})


# ---- append_entries ---------------------------------------------------------


def test_append_entries_prunes_old_entries() -> None:
    today = date(2026, 5, 14)
    existing = [
        _entry(customer_id="recent", d=date(2026, 5, 1)),
        _entry(customer_id="old", d=date(2025, 12, 1)),  # pruned
    ]
    new = [_entry(customer_id="brand_new", d=today)]
    combined = append_entries(existing, new, today=today, window_days=90)
    ids = {e.customer_id for e in combined}
    assert ids == {"recent", "brand_new"}


def test_append_entries_preserves_order_within_window() -> None:
    today = date(2026, 5, 14)
    existing = [_entry(customer_id="a"), _entry(customer_id="b")]
    new = [_entry(customer_id="c"), _entry(customer_id="d")]
    combined = append_entries(existing, new, today=today)
    assert [e.customer_id for e in combined] == ["a", "b", "c", "d"]


# ---- load/save roundtrip ---------------------------------------------------


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "pulse_history.json"
    entries = [
        _entry(customer_id="c1", rep_id="henry", pulse_id="p1", d=date(2026, 5, 1)),
        _entry(customer_id="c2", rep_id="zack", pulse_id="p2", d=date(2026, 5, 2)),
    ]
    save_history(entries, path)
    loaded = load_history(path)
    assert loaded == entries


def test_save_writes_deterministic_order_for_clean_diffs(tmp_path: Path) -> None:
    """Entries sorted by (date, rep_id, customer_id) so daily commits
    produce minimal-noise diffs in PR review.
    """
    path = tmp_path / "pulse_history.json"
    entries = [
        _entry(customer_id="c2", rep_id="zack", d=date(2026, 5, 2)),
        _entry(customer_id="c1", rep_id="henry", d=date(2026, 5, 2)),
        _entry(customer_id="c3", rep_id="henry", d=date(2026, 5, 1)),
    ]
    save_history(entries, path)
    loaded = load_history(path)
    assert [e.customer_id for e in loaded] == ["c3", "c1", "c2"]


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    """First-run setup must not crash; treat absence as empty history."""
    assert load_history(tmp_path / "does-not-exist.json") == []


def test_save_writes_schema_version(tmp_path: Path) -> None:
    """SCHEMA_VERSION lands in the file so a future migration can detect it."""
    path = tmp_path / "pulse_history.json"
    save_history([], path)
    payload = json.loads(path.read_text())
    assert payload["version"] == SCHEMA_VERSION


# ---- action-field roundtrip (Phase 2 inbox poller schema) -----------------


def test_pulse_entry_omits_action_fields_when_none(tmp_path: Path) -> None:
    """Entries without an action keep the JSON small (one entry per day per
    rep × 90-day window stays well under a KB).
    """
    path = tmp_path / "pulse_history.json"
    save_history([_entry()], path)
    payload = json.loads(path.read_text())
    entry = payload["entries"][0]
    assert "action" not in entry
    assert "action_at" not in entry
    assert "edited_text" not in entry
    assert "skip_reason" not in entry


def test_pulse_entry_persists_action_fields_when_set(tmp_path: Path) -> None:
    entry = _entry().with_action(
        action="edited",
        action_at=date(2026, 5, 22),
        edited_text="Hey Drew — long time.",
    )
    path = tmp_path / "pulse_history.json"
    save_history([entry], path)
    loaded = load_history(path)
    assert loaded[0].action == "edited"
    assert loaded[0].action_at == date(2026, 5, 22)
    assert loaded[0].edited_text == "Hey Drew — long time."
    assert loaded[0].skip_reason is None


def test_pulse_entry_with_action_returns_new_frozen_copy() -> None:
    """``with_action`` is a functional update — original stays unchanged
    (PulseEntry is frozen, so accidental in-place mutation would crash)."""
    original = _entry()
    updated = original.with_action(
        action="sent",
        action_at=date(2026, 5, 22),
    )
    assert original.action is None
    assert updated.action == "sent"
    assert updated.action_at == date(2026, 5, 22)
    # All non-action fields preserved.
    assert updated.customer_id == original.customer_id
    assert updated.rep_id == original.rep_id
    assert updated.pulse_id == original.pulse_id
    assert updated.date == original.date


def test_pulse_entry_with_action_skip_reason() -> None:
    entry = _entry().with_action(
        action="skipped",
        action_at=date(2026, 5, 22),
        skip_reason="On vacation",
    )
    assert entry.skip_reason == "On vacation"
    assert entry.edited_text is None


# ---- draft_text provenance (BCC auto-log schema) ---------------------------


def test_pulse_entry_draft_text_roundtrips(tmp_path: Path) -> None:
    entry = PulseEntry(
        customer_id="c1",
        rep_id="henry",
        pulse_id="p1",
        date=date(2026, 6, 29),
        draft_text="Hey Jane! Hope the system is treating you well.",
    )
    path = tmp_path / "pulse_history.json"
    save_history([entry], path)
    loaded = load_history(path)
    assert loaded[0].draft_text == "Hey Jane! Hope the system is treating you well."


def test_pulse_entry_omits_draft_text_when_none(tmp_path: Path) -> None:
    path = tmp_path / "pulse_history.json"
    save_history([_entry()], path)
    payload = json.loads(path.read_text())
    assert "draft_text" not in payload["entries"][0]


def test_with_action_preserves_draft_text() -> None:
    """draft_text is send-time provenance, not an action field — the inbox
    poller applying an action must not erase it."""
    entry = PulseEntry(
        customer_id="c1",
        rep_id="henry",
        pulse_id="p1",
        date=date(2026, 6, 29),
        draft_text="the draft",
    )
    updated = entry.with_action(action="sent", action_at=date(2026, 6, 29))
    assert updated.draft_text == "the draft"


# ---- recent_edit_examples ---------------------------------------------------


def _edited_entry(
    *,
    rep_id: str = "henry",
    d: date = date(2026, 7, 1),
    draft: str | None = "Hey Jane! Just thinking about you.",
    edited: str | None = "Hey Jane — how's the media room holding up?",
    pulse_id: str = "p1",
) -> PulseEntry:
    return PulseEntry(
        customer_id="c1",
        rep_id=rep_id,
        pulse_id=pulse_id,
        date=d,
        action="edited" if edited else None,
        action_at=d,
        edited_text=edited,
        draft_text=draft,
    )


def test_recent_edit_examples_returns_draft_rewrite_pairs() -> None:
    entries = [_edited_entry()]
    examples = recent_edit_examples(entries, "henry")
    assert examples == (
        ("Hey Jane! Just thinking about you.", "Hey Jane — how's the media room holding up?"),
    )


def test_recent_edit_examples_filters_other_reps_and_incomplete_pairs() -> None:
    entries = [
        _edited_entry(rep_id="zack"),  # other rep
        _edited_entry(edited=None),  # never edited
        _edited_entry(draft=None),  # pre-provenance entry, nothing to diff
    ]
    assert recent_edit_examples(entries, "henry") == ()


def test_recent_edit_examples_newest_first_and_limited() -> None:
    entries = [
        _edited_entry(d=date(2026, 6, 1), edited="rewrite june", pulse_id="p1"),
        _edited_entry(d=date(2026, 7, 1), edited="rewrite july", pulse_id="p2"),
        _edited_entry(d=date(2026, 8, 1), edited="rewrite august", pulse_id="p3"),
    ]
    examples = recent_edit_examples(entries, "henry", limit=2)
    assert [rewrite for _, rewrite in examples] == ["rewrite august", "rewrite july"]


def test_recent_edit_examples_strips_compose_boilerplate() -> None:
    # Real shape from state/pulse_history.json: rep sent the compose-flow
    # instruction header back above the rewrite, with client line-wrapping.
    edited = (
        "Edit below to match what you actually sent the customer, then send.\r\n"
        "(This goes to the control mailbox to record your action — NOT to the\r\n"
        "customer.)\r\n\r\n"
        "Hey Teague! It's been a minute since we last connected."
    )
    entries = [_edited_entry(edited=edited)]
    examples = recent_edit_examples(entries, "henry")
    assert examples[0][1] == "Hey Teague! It's been a minute since we last connected."


def test_recent_edit_examples_skips_boilerplate_only_or_verbatim_edits() -> None:
    draft = "Hey Jane! Just thinking about you."
    boilerplate_only = (
        "Edit below to match what you actually sent the customer, then send.\r\n"
        "(This goes to the control mailbox to record your action — NOT to the\r\n"
        "customer.)\r\n\r\n" + draft
    )
    entries = [
        _edited_entry(edited=boilerplate_only),  # header + untouched draft
        _edited_entry(edited="Hey Jane!  Just thinking\r\nabout you."),  # whitespace-only diff
    ]
    assert recent_edit_examples(entries, "henry") == ()


def test_strip_compose_boilerplate_noop_without_header() -> None:
    assert strip_compose_boilerplate("Hey Jane — all good?") == "Hey Jane — all good?"
