"""Tests for ``reports/weekly_digest.py``.

Pure unit tests — no sockets, no I/O. The window-computation tests
exercise every weekday-of-execution case so re-running on a Tuesday
or via manual workflow_dispatch produces the same Mon-Fri window.
"""

from __future__ import annotations

from datetime import date

import pytest

from pipeline.suppressions import PulseEntry
from reports.weekly_digest import (
    acted_on_count,
    acted_on_pct,
    build_digest,
    drafter_accuracy_pct,
    last_completed_workweek,
)

# ---- last_completed_workweek ----------------------------------------------


@pytest.mark.parametrize(
    "today,expected_mon,expected_fri",
    [
        # Monday: the "completed" week ended last Friday.
        (date(2026, 5, 25), date(2026, 5, 18), date(2026, 5, 22)),
        # Tuesday: same — last Mon-Fri is the completed one.
        (date(2026, 5, 26), date(2026, 5, 18), date(2026, 5, 22)),
        # Thursday.
        (date(2026, 5, 28), date(2026, 5, 18), date(2026, 5, 22)),
        # Friday: TODAY is Friday, but week isn't completed until day end.
        # We report the previous Mon-Fri (not today's in-progress week).
        (date(2026, 5, 29), date(2026, 5, 18), date(2026, 5, 22)),
        # Saturday: the week that just ended.
        (date(2026, 5, 30), date(2026, 5, 25), date(2026, 5, 29)),
        # Sunday.
        (date(2026, 5, 31), date(2026, 5, 25), date(2026, 5, 29)),
    ],
)
def test_last_completed_workweek_returns_prior_mon_fri(
    today: date, expected_mon: date, expected_fri: date
) -> None:
    assert last_completed_workweek(today) == (expected_mon, expected_fri)


# ---- helpers ---------------------------------------------------------------


def _zoho(zid: str, name: str = "Jane Smith") -> dict:
    return {"id": zid, "Full_Name": name}


def _entry(
    *,
    rep_id: str = "henry",
    customer_id: str = "c1",
    d: date = date(2026, 5, 19),
    action: str | None = None,
    edited_text: str | None = None,
) -> PulseEntry:
    return PulseEntry(
        customer_id=customer_id,
        rep_id=rep_id,
        pulse_id=f"{d.isoformat()}-{rep_id}",
        date=d,
        action=action,
        action_at=d if action else None,
        edited_text=edited_text,
    )


TODAY_MON = date(2026, 5, 25)  # Monday → window = 2026-05-18 .. 2026-05-22


# ---- build_digest — happy paths -------------------------------------------


def test_build_digest_counts_pulses_in_window() -> None:
    history = [
        _entry(d=date(2026, 5, 18), action="sent"),
        _entry(d=date(2026, 5, 20), action="edited"),
        _entry(d=date(2026, 5, 22)),  # last Friday, no action
        _entry(d=date(2026, 5, 11), action="sent"),  # week BEFORE — out of window
    ]
    digest = build_digest(
        entries=history,
        zoho_contacts=[_zoho("c1", "Jane Smith")],
        rep_id="henry",
        today=TODAY_MON,
    )
    assert digest.pulses_sent == 3  # excludes the prior-week entry
    assert digest.actions == {"sent": 1, "edited": 1, "skipped": 0, "reassign": 0, "none": 1}


def test_build_digest_filters_to_one_rep() -> None:
    history = [
        _entry(rep_id="henry", d=date(2026, 5, 19), action="sent"),
        _entry(rep_id="zack", d=date(2026, 5, 19), action="edited"),
    ]
    henry = build_digest(
        entries=history,
        zoho_contacts=[],
        rep_id="henry",
        today=TODAY_MON,
    )
    zack = build_digest(
        entries=history,
        zoho_contacts=[],
        rep_id="zack",
        today=TODAY_MON,
    )
    assert henry.pulses_sent == 1
    assert henry.actions["sent"] == 1
    assert zack.pulses_sent == 1
    assert zack.actions["edited"] == 1


def test_build_digest_resolves_customer_name_from_zoho() -> None:
    history = [_entry(customer_id="c-real", d=date(2026, 5, 19), action="sent")]
    digest = build_digest(
        entries=history,
        zoho_contacts=[_zoho("c-real", "Drew Spitzer")],
        rep_id="henry",
        today=TODAY_MON,
    )
    assert digest.touches[0].customer_name == "Drew Spitzer"


def test_build_digest_falls_back_to_placeholder_when_name_missing() -> None:
    """A pulse for a customer that's been removed from Zoho since: don't crash."""
    history = [_entry(customer_id="c-gone", d=date(2026, 5, 19), action="sent")]
    digest = build_digest(
        entries=history,
        zoho_contacts=[],  # empty — every lookup misses
        rep_id="henry",
        today=TODAY_MON,
    )
    assert digest.touches[0].customer_name == "(unknown customer)"


def test_build_digest_orders_touches_by_date_then_name() -> None:
    history = [
        _entry(customer_id="c1", d=date(2026, 5, 20), action="sent"),
        _entry(customer_id="c2", d=date(2026, 5, 18), action="edited"),
        _entry(customer_id="c3", d=date(2026, 5, 20), action="skipped"),
    ]
    contacts = [
        _zoho("c1", "Charlie"),
        _zoho("c2", "Alice"),
        _zoho("c3", "Bob"),
    ]
    digest = build_digest(entries=history, zoho_contacts=contacts, rep_id="henry", today=TODAY_MON)
    # Alice (5/18), then Bob and Charlie tied on 5/20 → alpha-sorted
    assert [t.customer_name for t in digest.touches] == ["Alice", "Bob", "Charlie"]


def test_build_digest_carries_edited_text_through() -> None:
    history = [
        _entry(d=date(2026, 5, 19), action="edited", edited_text="Hey Drew - long time."),
    ]
    digest = build_digest(
        entries=history,
        zoho_contacts=[_zoho("c1", "Drew")],
        rep_id="henry",
        today=TODAY_MON,
    )
    assert digest.touches[0].edited_text == "Hey Drew - long time."


# ---- build_digest — YTD ----------------------------------------------------


def test_build_digest_ytd_includes_all_year_entries() -> None:
    history = [
        _entry(d=date(2026, 1, 5), action="sent"),
        _entry(d=date(2026, 3, 12), action="edited"),
        _entry(d=date(2026, 5, 19), action="sent"),
        _entry(d=date(2025, 12, 28), action="sent"),  # PRIOR YEAR — excluded
    ]
    digest = build_digest(
        entries=history,
        zoho_contacts=[_zoho("c1")],
        rep_id="henry",
        today=TODAY_MON,
    )
    assert digest.ytd_pulses == 3
    assert digest.ytd_actions["sent"] == 2
    assert digest.ytd_actions["edited"] == 1


def test_build_digest_respects_year_start_override() -> None:
    """Lets tests simulate a fiscal-year start or boundary scenarios."""
    history = [
        _entry(d=date(2026, 5, 1), action="sent"),
        _entry(d=date(2026, 5, 19), action="sent"),
    ]
    digest = build_digest(
        entries=history,
        zoho_contacts=[],
        rep_id="henry",
        today=TODAY_MON,
        year_start=date(2026, 5, 15),  # only count from May 15
    )
    assert digest.ytd_pulses == 1


# ---- build_digest — empty edge cases --------------------------------------


def test_build_digest_empty_history_returns_zero_pulses() -> None:
    digest = build_digest(
        entries=[],
        zoho_contacts=[],
        rep_id="henry",
        today=TODAY_MON,
    )
    assert digest.pulses_sent == 0
    assert digest.touches == []
    assert digest.actions == {"sent": 0, "edited": 0, "skipped": 0, "reassign": 0, "none": 0}


def test_build_digest_no_window_entries_but_ytd_carries_over() -> None:
    """Rep had pulses this year but none last week — YTD still populated."""
    history = [_entry(d=date(2026, 5, 1), action="sent")]
    digest = build_digest(
        entries=history,
        zoho_contacts=[],
        rep_id="henry",
        today=TODAY_MON,
    )
    assert digest.pulses_sent == 0
    assert digest.ytd_pulses == 1


# ---- helpers: acted_on / drafter_accuracy ---------------------------------


def test_acted_on_count_excludes_none_bucket() -> None:
    actions = {"sent": 1, "edited": 2, "skipped": 1, "reassign": 0, "none": 5}
    assert acted_on_count(actions) == 4


def test_acted_on_pct_handles_zero_total() -> None:
    assert acted_on_pct({"sent": 0, "edited": 0, "skipped": 0, "reassign": 0, "none": 0}) == 0


def test_acted_on_pct_rounds() -> None:
    # 1 acted + 2 none = 33%
    assert acted_on_pct({"sent": 1, "edited": 0, "skipped": 0, "reassign": 0, "none": 2}) == 33


def test_drafter_accuracy_is_sent_over_acted() -> None:
    # 1 sent verbatim, 3 edited = 4 acted; sent fraction = 25%.
    assert (
        drafter_accuracy_pct({"sent": 1, "edited": 3, "skipped": 0, "reassign": 0, "none": 0}) == 25
    )


def test_drafter_accuracy_zero_when_no_actions() -> None:
    assert (
        drafter_accuracy_pct({"sent": 0, "edited": 0, "skipped": 0, "reassign": 0, "none": 5}) == 0
    )
