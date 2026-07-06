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
    # Optional action fields — populated by the inbox poller when the rep
    # taps an action button on the morning pulse email. Absent until that
    # round-trip completes (which can be hours or never).
    action: str | None = None  # one of pulse.links.KNOWN_ACTIONS
    action_at: date | None = None
    edited_text: str | None = None
    skip_reason: str | None = None
    # The AI draft as it appeared in the pulse. Written at send time so the
    # poller's BCC auto-log pass can classify sent-verbatim vs edited by
    # comparing the outbound body to the draft. Absent on pre-field entries.
    draft_text: str | None = None

    def to_json(self) -> dict[str, str]:
        out: dict[str, str] = {
            "customer_id": self.customer_id,
            "rep_id": self.rep_id,
            "pulse_id": self.pulse_id,
            "date": self.date.isoformat(),
        }
        # Omit None fields to keep the JSON diff small for entries the rep
        # hasn't acted on yet.
        if self.action is not None:
            out["action"] = self.action
        if self.action_at is not None:
            out["action_at"] = self.action_at.isoformat()
        if self.edited_text is not None:
            out["edited_text"] = self.edited_text
        if self.skip_reason is not None:
            out["skip_reason"] = self.skip_reason
        if self.draft_text is not None:
            out["draft_text"] = self.draft_text
        return out

    @classmethod
    def from_json(cls, raw: dict[str, str]) -> PulseEntry:
        action_at_raw = raw.get("action_at")
        return cls(
            customer_id=str(raw["customer_id"]),
            rep_id=str(raw["rep_id"]),
            pulse_id=str(raw["pulse_id"]),
            date=date.fromisoformat(str(raw["date"])),
            action=str(raw["action"]) if raw.get("action") else None,
            action_at=date.fromisoformat(str(action_at_raw)) if action_at_raw else None,
            edited_text=str(raw["edited_text"]) if raw.get("edited_text") else None,
            skip_reason=str(raw["skip_reason"]) if raw.get("skip_reason") else None,
            draft_text=str(raw["draft_text"]) if raw.get("draft_text") else None,
        )

    def with_action(
        self,
        *,
        action: str,
        action_at: date,
        edited_text: str | None = None,
        skip_reason: str | None = None,
    ) -> PulseEntry:
        """Return a copy with the action fields populated.

        Frozen dataclass + functional update — the inbox poller calls this
        when a verified reply lands for this pulse_id. If ``action`` is
        already set, the new value wins (idempotent retries are safe, and
        a rep changing their mind mid-day is recorded as their final answer).
        ``draft_text`` is send-time provenance, not an action field — it
        survives the update unchanged.
        """
        return PulseEntry(
            customer_id=self.customer_id,
            rep_id=self.rep_id,
            pulse_id=self.pulse_id,
            date=self.date,
            action=action,
            action_at=action_at,
            edited_text=edited_text,
            skip_reason=skip_reason,
            draft_text=self.draft_text,
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


def reps_pulsed_today(entries: Iterable[PulseEntry], today: date) -> frozenset[str]:
    """Return rep_ids that already have a recorded pulse for ``today``.

    Used by the orchestrator for same-day idempotency: GitHub Actions
    scheduled triggers are unreliable enough that we register multiple
    cron entries per morning. If the first trigger succeeded, the
    second/third must no-op cleanly — that's what this set powers.
    """
    return frozenset(e.rep_id for e in entries if e.date == today)


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
