"""Recently-pulsed customer suppression.

Without state across runs the engine would happily pick the same customer
again next week — pestering them and wasting the rep's attention. This
module persists a small JSON history of past sends and exposes a helper
that returns the set of customer_ids pulsed in the last ``window_days``.

The history file (``state/pulse_history.json``) is committed back to the
repo by the workflow at the end of each run. That's the cheapest possible
"durable state" for Phase 1 — no extra infrastructure, just an
append-only audit trail in git. Phase 2's inbox-poller will move
suppression-driving signals into Zoho Activities, at which point this
file can graduate to a deletable cache.

Entries are pruned when older than ``window_days`` on write, so the file
stays a few KB indefinitely.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HISTORY_PATH = REPO_ROOT / "state" / "pulse_history.json"
DEFAULT_WINDOW_DAYS = 90
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PulseEntry:
    customer_id: str
    rep_id: str
    pulse_id: str
    date: date

    def to_json(self) -> dict[str, str]:
        return {
            "customer_id": self.customer_id,
            "rep_id": self.rep_id,
            "pulse_id": self.pulse_id,
            "date": self.date.isoformat(),
        }

    @classmethod
    def from_json(cls, raw: dict[str, str]) -> PulseEntry:
        return cls(
            customer_id=str(raw["customer_id"]),
            rep_id=str(raw["rep_id"]),
            pulse_id=str(raw["pulse_id"]),
            date=date.fromisoformat(str(raw["date"])),
        )


def load_history(path: Path = DEFAULT_HISTORY_PATH) -> list[PulseEntry]:
    """Read the pulse-history file. Returns ``[]`` when the file is missing.

    Treating a missing file as empty makes first-run setup painless and
    keeps tests that don't care about history from having to scaffold a
    fixture file.
    """
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8")) or {}
    return [PulseEntry.from_json(e) for e in payload.get("entries") or []]


def recently_pulsed_customer_ids(
    entries: Iterable[PulseEntry], today: date, window_days: int = DEFAULT_WINDOW_DAYS
) -> frozenset[str]:
    """Return customer_ids pulsed within ``window_days`` of ``today``."""
    cutoff = today - timedelta(days=window_days)
    return frozenset(e.customer_id for e in entries if e.date >= cutoff)


def append_entries(
    existing: Iterable[PulseEntry],
    new: Iterable[PulseEntry],
    *,
    today: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[PulseEntry]:
    """Merge new entries into the existing history and prune entries past
    the suppression window.

    Pruning bounds the file size: at one entry per rep per weekday, a
    90-day window caps the file at ~40 entries per rep — small enough to
    stay readable in a PR diff and trivial to git-pack.
    """
    cutoff = today - timedelta(days=window_days)
    combined = [e for e in existing if e.date >= cutoff]
    combined.extend(new)
    return combined


def save_history(
    entries: Iterable[PulseEntry],
    path: Path = DEFAULT_HISTORY_PATH,
) -> None:
    """Write entries to disk in a deterministic shape (stable diffs)."""
    sorted_entries = sorted(entries, key=lambda e: (e.date, e.rep_id, e.customer_id))
    payload = {
        "version": SCHEMA_VERSION,
        "entries": [e.to_json() for e in sorted_entries],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
