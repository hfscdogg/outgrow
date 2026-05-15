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
    recently_pulsed_customer_ids,
    save_history,
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
