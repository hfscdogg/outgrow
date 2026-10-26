"""Tests for owner alerts on judge-rejected pulses.

Covers ``pipeline/alerts.py`` (log persistence, per-day dedupe, email
rendering) and ``scripts.daily_pulse.alert_on_rejections`` (the glue the
orchestrator calls after a ``--write`` run). SMTP is an injected fake.
"""

from __future__ import annotations

import json
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import pytest

from drafting.generator import GenerationResult
from drafting.sample_judge import JudgeResult, Reason, Violation
from pipeline.alerts import (
    AlertRecord,
    RejectedDraft,
    build_rejection_alert,
    load_alerts,
    save_alerts,
    unalerted,
)
from pulse.mailer import RecipientPolicy, load_recipient_policy
from scripts.daily_pulse import PulseRunResult, PulseStatus, alert_on_rejections

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "config" / "recipient_allowlist.yaml"
OWNER_EMAIL = "henry@getlivewire.com"
SENDER = "outgrow-control@getlivewire.com"
TODAY = date(2026, 10, 27)


def _policy() -> RecipientPolicy:
    return load_recipient_policy(owner_email=OWNER_EMAIL, path=ALLOWLIST_PATH)


def _rejected_result(rep_id: str = "henry") -> PulseRunResult:
    return PulseRunResult(
        rep_id=rep_id,
        status=PulseStatus.JUDGE_REJECTED,
        customer_id="cust-1",
        pulse_id=f"{TODAY.isoformat()}-{rep_id}",
        model_used="claude-sonnet-4-6",
        judge=JudgeResult(
            passed=False,
            violations=(Violation(Reason.OFF_PROFILE_GREETING, "greetings"),),
        ),
        generation=GenerationResult(
            draft_text="Greetings!\n\nHope the system is treating you well.",
            model_used="claude-sonnet-4-6",
            retries=0,
        ),
    )


class _Outbox:
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def __call__(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


# ---- pipeline/alerts.py -----------------------------------------------------


def test_load_alerts_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_alerts(tmp_path / "alerts.json") == []


def test_save_alerts_roundtrip_prunes_old_records(tmp_path: Path) -> None:
    path = tmp_path / "alerts.json"
    records = [
        AlertRecord(date=TODAY, rep_id="henry"),
        AlertRecord(date=date(2026, 9, 1), rep_id="zack"),  # outside 14-day window
    ]
    save_alerts(records, today=TODAY, path=path)
    assert load_alerts(path) == [AlertRecord(date=TODAY, rep_id="henry")]
    assert json.loads(path.read_text())["version"] == 1


def test_unalerted_skips_reps_already_alerted_today() -> None:
    henry = RejectedDraft("henry", "c1", "p1", "draft", ())
    zack = RejectedDraft("zack", "c2", "p2", "draft", ())
    records = [
        AlertRecord(date=TODAY, rep_id="henry"),
        AlertRecord(date=date(2026, 10, 26), rep_id="zack"),  # yesterday doesn't count
    ]
    assert unalerted([henry, zack], records, TODAY) == [zack]


def test_build_rejection_alert_includes_draft_and_violations() -> None:
    msg = build_rejection_alert(
        [RejectedDraft("henry", "cust-1", "p1", "Mark:\n\nHope all is well.", ("x: y",))],
        today=TODAY,
        owner_email=OWNER_EMAIL,
        sender_email=SENDER,
    )
    assert msg["To"] == OWNER_EMAIL
    assert msg["From"] == SENDER
    assert "henry" in msg["Subject"]
    body = msg.get_content()
    assert "cust-1" in body
    assert "  - x: y" in body
    assert "  Mark:" in body


# ---- scripts.daily_pulse.alert_on_rejections --------------------------------


def test_alert_sent_once_per_rep_per_day(tmp_path: Path) -> None:
    path = tmp_path / "alerts.json"
    outbox = _Outbox()
    kwargs = {
        "today": TODAY,
        "policy": _policy(),
        "sender_email": SENDER,
        "smtp_send": outbox,
        "alerts_path": path,
    }

    assert alert_on_rejections([_rejected_result()], **kwargs) == ["henry"]  # type: ignore[arg-type]
    # A backup cron re-rejects the same rep later that morning: no second email.
    assert alert_on_rejections([_rejected_result()], **kwargs) == []  # type: ignore[arg-type]

    assert len(outbox.sent) == 1
    body = outbox.sent[0].get_content()
    assert "off_profile_greeting: greetings" in body
    assert "  Greetings!" in body


def test_no_alert_when_nothing_rejected(tmp_path: Path) -> None:
    outbox = _Outbox()
    sent = PulseRunResult(rep_id="zack", status=PulseStatus.SENT, customer_id="c", pulse_id="p")
    alerted = alert_on_rejections(
        [sent],
        today=TODAY,
        policy=_policy(),
        sender_email=SENDER,
        smtp_send=outbox,
        alerts_path=tmp_path / "alerts.json",
    )
    assert alerted == []
    assert outbox.sent == []
    assert not (tmp_path / "alerts.json").exists()


def test_failed_send_is_not_recorded(tmp_path: Path) -> None:
    path = tmp_path / "alerts.json"

    def boom(msg: EmailMessage) -> None:
        raise OSError("smtp down")

    with pytest.raises(OSError):
        alert_on_rejections(
            [_rejected_result()],
            today=TODAY,
            policy=_policy(),
            sender_email=SENDER,
            smtp_send=boom,
            alerts_path=path,
        )
    # Nothing recorded, so the next firing retries the alert.
    assert load_alerts(path) == []
