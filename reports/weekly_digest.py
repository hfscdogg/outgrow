"""Weekly digest computation.

Pure functions: take a list of ``PulseEntry`` records + zoho contacts +
``today`` and return a typed ``WeeklyDigest`` shape that the renderer
turns into an email. No I/O here — the caller (``scripts/weekly_digest.py``)
loads pulse history and the Zoho cache, and ships the rendered email
via SMTP.

The digest covers the **most recently completed Mon–Fri workweek**:

* Monday morning fire (the cron-job.org cadence) → previous week's Mon–Fri
* Manual mid-week dispatch → still last week's Mon–Fri (deterministic)
* Friday afternoon manual dispatch → the previous week's Mon–Fri, not
  today's in-progress week (avoids partial reports)

That stable window means re-running the workflow at any time produces
the same report — useful for debugging and for re-sending a missed
Monday after a holiday.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from pipeline.suppressions import PulseEntry


@dataclass(frozen=True)
class CustomerTouch:
    """One pulse the rep sent during the digest window, with the rep's action."""

    customer_id: str
    customer_name: str  # falls back to "(unknown customer)" when name lookup fails
    date: date
    action: str | None  # one of pulse.links.KNOWN_ACTIONS, or None if rep didn't tap
    edited_text: str | None


@dataclass(frozen=True)
class WeeklyDigest:
    """Per-rep weekly recap. The renderer turns this into HTML + plain text."""

    rep_id: str
    period_start: date  # inclusive
    period_end: date  # inclusive
    pulses_sent: int
    actions: dict[str, int]  # {"sent": N, "edited": N, "skipped": N, "reassign": N, "none": N}
    touches: list[CustomerTouch]
    ytd_pulses: int
    ytd_actions: dict[str, int]


# ---- window computation ----------------------------------------------------


_FRIDAY = 4


def last_completed_workweek(today: date) -> tuple[date, date]:
    """Return ``(monday, friday)`` of the most recently completed workweek.

    "Completed" means Friday in the past — never today. So a Friday
    dispatch reports the prior week, not today's in-progress one. This
    is deliberate: the rep already knows what they did this morning;
    the value is the look-back.
    """
    days_since_friday = (today.weekday() - _FRIDAY) % 7
    last_friday = today - timedelta(days=days_since_friday)
    if today.weekday() == _FRIDAY:
        # Today IS Friday — the "completed" week ended seven days ago.
        last_friday -= timedelta(days=7)
    last_monday = last_friday - timedelta(days=4)
    return last_monday, last_friday


# ---- name lookup -----------------------------------------------------------


def _customer_name(
    contact: Mapping[str, Any] | None,
) -> str:
    """Project a Zoho contact dict into a display name. Same rules as the
    daily-pulse renderer so the digest names match what the rep saw in
    the morning email.
    """
    if contact is None:
        return "(unknown customer)"
    full = contact.get("Full_Name")
    if full:
        return str(full).strip()
    first = contact.get("First_Name") or ""
    last = contact.get("Last_Name") or ""
    combined = f"{first} {last}".strip()
    return combined or "(unknown customer)"


# ---- main computation ------------------------------------------------------


def _index_zoho_by_id(zoho_contacts: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(c.get("id", "")): c for c in zoho_contacts if c.get("id")}


def _entries_for_rep_in_window(
    entries: Sequence[PulseEntry],
    rep_id: str,
    start: date,
    end: date,
) -> list[PulseEntry]:
    return [e for e in entries if e.rep_id == rep_id and start <= e.date <= end]


def _action_counter(entries: Sequence[PulseEntry]) -> dict[str, int]:
    """Bucket entries by action; ``None`` (rep didn't tap) becomes ``"none"``.

    Always emits the five canonical buckets so the renderer can format a
    consistent table even on light weeks.
    """
    counts: dict[str, int] = {"sent": 0, "edited": 0, "skipped": 0, "reassign": 0, "none": 0}
    for e in entries:
        key = e.action or "none"
        counts[key] = counts.get(key, 0) + 1
    return counts


def build_digest(
    *,
    entries: Sequence[PulseEntry],
    zoho_contacts: Sequence[Mapping[str, Any]],
    rep_id: str,
    today: date,
    year_start: date | None = None,
) -> WeeklyDigest:
    """Compute one rep's weekly digest.

    ``year_start`` defaults to Jan 1 of ``today``'s year. Passed in for
    testability (so tests can simulate a year boundary).
    """
    if year_start is None:
        year_start = date(today.year, 1, 1)

    period_start, period_end = last_completed_workweek(today)
    window_entries = _entries_for_rep_in_window(entries, rep_id, period_start, period_end)
    actions = _action_counter(window_entries)

    by_id = _index_zoho_by_id(zoho_contacts)
    touches = [
        CustomerTouch(
            customer_id=e.customer_id,
            customer_name=_customer_name(by_id.get(e.customer_id)),
            date=e.date,
            action=e.action,
            edited_text=e.edited_text,
        )
        for e in window_entries
    ]
    # Stable order: by date, then customer name. Same week's pulses group
    # naturally and the diff between consecutive Monday digests stays small.
    touches.sort(key=lambda t: (t.date, t.customer_name))

    ytd_entries = [e for e in entries if e.rep_id == rep_id and year_start <= e.date <= today]
    ytd_actions = _action_counter(ytd_entries)

    return WeeklyDigest(
        rep_id=rep_id,
        period_start=period_start,
        period_end=period_end,
        pulses_sent=len(window_entries),
        actions=actions,
        touches=touches,
        ytd_pulses=len(ytd_entries),
        ytd_actions=ytd_actions,
    )


# ---- helpers used by the renderer ------------------------------------------


def acted_on_count(actions: Mapping[str, int]) -> int:
    """Pulses the rep took ANY action on (sent/edited/skipped/reassign)."""
    return sum(v for k, v in actions.items() if k != "none")


def acted_on_pct(actions: Mapping[str, int]) -> int:
    """Response rate as a 0-100 integer; returns 0 when no pulses sent."""
    total = sum(actions.values())
    if total == 0:
        return 0
    return round(100 * acted_on_count(actions) / total)


def drafter_accuracy_pct(actions: Mapping[str, int]) -> int:
    """Share of acted-on pulses sent verbatim. The signal of voice fit."""
    acted = acted_on_count(actions)
    if acted == 0:
        return 0
    return round(100 * actions.get("sent", 0) / acted)
