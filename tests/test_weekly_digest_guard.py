"""Tests for the weekly digest's once-per-week send guard
(``scripts/weekly_digest.py``: load_sent_ledger / already_sent / record_sent)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scripts.weekly_digest import already_sent, load_sent_ledger, record_sent

WEEK = date(2026, 10, 26)


def test_missing_ledger_is_empty(tmp_path: Path) -> None:
    assert load_sent_ledger(tmp_path / "digest_sent.json") == {}


def test_record_then_already_sent_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "state" / "digest_sent.json"
    ledger: dict[str, str] = {}
    record_sent(ledger, "zack", WEEK, path)

    reloaded = load_sent_ledger(path)
    assert reloaded == {"zack": "2026-10-26"}
    assert already_sent(reloaded, "zack", WEEK)
    assert not already_sent(reloaded, "henry", WEEK)
    assert not (path.parent / "digest_sent.json.tmp").exists()


def test_new_week_is_not_already_sent(tmp_path: Path) -> None:
    path = tmp_path / "digest_sent.json"
    ledger: dict[str, str] = {}
    record_sent(ledger, "zack", WEEK, path)
    assert not already_sent(load_sent_ledger(path), "zack", date(2026, 11, 2))


def test_record_keeps_other_reps(tmp_path: Path) -> None:
    path = tmp_path / "digest_sent.json"
    path.write_text(json.dumps({"henry": "2026-10-19"}), encoding="utf-8")
    ledger = load_sent_ledger(path)
    record_sent(ledger, "zack", WEEK, path)
    assert load_sent_ledger(path) == {"henry": "2026-10-19", "zack": "2026-10-26"}


def test_malformed_ledger_raises(tmp_path: Path) -> None:
    path = tmp_path / "digest_sent.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_sent_ledger(path)
