"""Owner alerts for pulses the draft judge rejected.

A judge-rejected pulse is skipped by design (better no pulse than a bad
one), but it used to be skipped *silently*: the run still exits 0, nothing
lands in ``state/pulse_history.json``, and the next day the same customer
gets re-drafted and re-rejected. That hid a stuck rep for two weeks in Oct
2026. This module turns each rejection into one email to the owner.

Backup crons re-run the orchestrator for any rep that wasn't pulsed, so a
rejected rep is rejected again on every firing. ``state/alerts.json``
records which (date, rep) pairs were already alerted so the owner gets at
most one alert per rep per day. The file is committed back to main by the
workflow alongside the pulse history and pruned to a short window.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALERTS_PATH = REPO_ROOT / "state" / "alerts.json"
DEFAULT_ALERT_WINDOW_DAYS = 14
SCHEMA_VERSION = 1
KIND_JUDGE_REJECTED = "judge_rejected"


@dataclass(frozen=True)
class AlertRecord:
    date: date
    rep_id: str
    kind: str = KIND_JUDGE_REJECTED

    def to_json(self) -> dict[str, str]:
        return {"date": self.date.isoformat(), "rep_id": self.rep_id, "kind": self.kind}

    @classmethod
    def from_json(cls, raw: dict[str, str]) -> AlertRecord:
        return cls(
            date=date.fromisoformat(str(raw["date"])),
            rep_id=str(raw["rep_id"]),
            kind=str(raw.get("kind") or KIND_JUDGE_REJECTED),
        )


@dataclass(frozen=True)
class RejectedDraft:
    """What the owner needs to see to act on one rejected pulse."""

    rep_id: str
    customer_id: str
    pulse_id: str
    draft_text: str
    violations: tuple[str, ...]  # rendered "reason: detail" strings


def load_alerts(path: Path = DEFAULT_ALERTS_PATH) -> list[AlertRecord]:
    """Read the alert log. Missing file means nothing was alerted yet."""
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8")) or {}
    return [AlertRecord.from_json(r) for r in payload.get("alerts") or []]


def save_alerts(
    records: Iterable[AlertRecord],
    *,
    today: date,
    path: Path = DEFAULT_ALERTS_PATH,
    window_days: int = DEFAULT_ALERT_WINDOW_DAYS,
) -> None:
    """Write the alert log, pruned to ``window_days`` and deterministically
    sorted so the committed diff stays small."""
    cutoff = today - timedelta(days=window_days)
    kept = sorted(
        {r for r in records if r.date >= cutoff},
        key=lambda r: (r.date, r.rep_id, r.kind),
    )
    payload = {"version": SCHEMA_VERSION, "alerts": [r.to_json() for r in kept]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def unalerted(
    rejected: Iterable[RejectedDraft],
    records: Iterable[AlertRecord],
    today: date,
) -> list[RejectedDraft]:
    """Drop rejections already alerted today (a backup cron re-rejecting)."""
    done = {r.rep_id for r in records if r.date == today and r.kind == KIND_JUDGE_REJECTED}
    return [r for r in rejected if r.rep_id not in done]


def build_rejection_alert(
    rejected: Iterable[RejectedDraft],
    *,
    today: date,
    owner_email: str,
    sender_email: str,
) -> EmailMessage:
    """One plain-text email covering every rep whose pulse was rejected."""
    rejected = list(rejected)
    reps = ", ".join(r.rep_id for r in rejected)
    lines = [
        f"The draft judge rejected today's pulse for: {reps}.",
        "No pulse email went out for these reps. The orchestrator will",
        "re-draft on the next run, but a rejection that repeats day after",
        "day usually means the judge rules and the rep's voice have drifted",
        "apart (see drafting/sample_judge.py).",
        "",
    ]
    for r in rejected:
        lines += [
            f"=== {r.rep_id} ===",
            f"pulse_id: {r.pulse_id}",
            f"customer_id: {r.customer_id}",
            "violations:",
            *(f"  - {v}" for v in r.violations),
            "draft:",
            *(f"  {line}" for line in (r.draft_text.splitlines() or [""])),
            "",
        ]
    msg = EmailMessage()
    msg["From"] = sender_email
    msg["To"] = owner_email
    msg["Subject"] = f"[Outgrow] Pulse draft rejected ({reps}) {today.isoformat()}"
    msg.set_content("\n".join(lines))
    return msg
