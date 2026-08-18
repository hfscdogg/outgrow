"""Three-way-free merge of two ``state/pulse_history.json`` snapshots.

Two workflows commit this file back to main (daily-dispatch and
inbox-poll). They share a concurrency group, but a push can still be
rejected if main moved under a run — and when it does, ``git rebase``
hits a textual conflict on the JSON array and makes no progress. The
Aug 18 dispatch failure was exactly that: five rebase attempts, five
identical conflicts, exit 1.

Git can't merge this file, but we can: entries are keyed records, not
prose. Union the two sides by ``(pulse_id, customer_id)`` and the merge
is unambiguous — each side either adds entries the other lacks (a new
day's pulse) or fills in action fields on an entry the other still has
in its pre-action shape (a rep tapped SENT/EDITED between the two
writes). Both are additive; nothing is ever dropped.

Deliberately stdlib-only and free of first-party imports so the commit
step can copy this file to ``$RUNNER_TEMP`` and run it with the system
``python3`` after a ``git reset --hard`` has swapped the work tree out
from under it.

Usage:
    python3 merge_pulse_history.py --ours A.json --theirs B.json --out C.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

Entry = dict[str, Any]


def entry_key(entry: Entry) -> tuple[str, str]:
    """Identity of a pulse-history record.

    ``pulse_id`` is ``<date>-<rep_id>`` and so is unique per rep-day;
    ``customer_id`` is carried along so a same-day re-pick (which should
    never happen, but did on Aug 18 when a stale checkout defeated the
    idempotency check) surfaces as two entries rather than one silently
    overwriting the other.
    """
    return (str(entry.get("pulse_id", "")), str(entry.get("customer_id", "")))


def _is_populated(value: Any) -> bool:
    return value is not None and value != ""


def _action_progress(entry: Entry) -> tuple[int, str]:
    """How far along this record's action state is — an acted-on entry
    outranks an untouched one, later action dates outrank earlier."""
    return (
        1 if _is_populated(entry.get("action")) else 0,
        str(entry.get("action_at") or ""),
    )


def merge_entry(ours: Entry, theirs: Entry) -> Entry:
    """Combine two records of the same pulse.

    Whichever side recorded a rep action more recently is authoritative
    for the fields it populates; the other side still contributes any
    field the winner left empty (``draft_text`` written at send time, for
    instance, survives an action update that doesn't carry it). Ties go
    to ``ours`` — the local write is the fresher of two equals.
    """
    if _action_progress(theirs) > _action_progress(ours):
        primary, secondary = theirs, ours
    else:
        primary, secondary = ours, theirs

    merged = dict(secondary)
    for field, value in primary.items():
        if _is_populated(value):
            merged[field] = value
    return merged


def merge_entries(ours: list[Entry], theirs: list[Entry]) -> list[Entry]:
    """Union two entry lists by :func:`entry_key`.

    Sorted on the way out with the same key ``save_history`` uses, so a
    merged file is byte-identical to one the orchestrator would have
    written itself — no spurious reordering diff on the next run.
    """
    merged: dict[tuple[str, str], Entry] = {}
    for entry in theirs:
        merged[entry_key(entry)] = entry
    for entry in ours:
        key = entry_key(entry)
        merged[key] = merge_entry(entry, merged[key]) if key in merged else entry

    def sort_key(entry: Entry) -> tuple[str, str, str]:
        return (
            str(entry.get("date", "")),
            str(entry.get("rep_id", "")),
            str(entry.get("customer_id", "")),
        )

    return sorted(merged.values(), key=sort_key)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")) or {}


def merge_files(ours_path: Path, theirs_path: Path, out_path: Path) -> int:
    """Merge two history files and write the result. Returns entry count."""
    ours, theirs = _load(ours_path), _load(theirs_path)
    entries = merge_entries(ours.get("entries") or [], theirs.get("entries") or [])
    version = max(
        int(ours.get("version") or SCHEMA_VERSION),
        int(theirs.get("version") or SCHEMA_VERSION),
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"version": version, "entries": entries}, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(entries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ours", required=True, type=Path, help="our snapshot (this run's write)")
    parser.add_argument("--theirs", required=True, type=Path, help="the snapshot on origin/main")
    parser.add_argument("--out", required=True, type=Path, help="destination for the merged file")
    args = parser.parse_args(argv)

    count = merge_files(args.ours, args.theirs, args.out)
    print(f"merged pulse history: {count} entries -> {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
