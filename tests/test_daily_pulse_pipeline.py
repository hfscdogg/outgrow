"""End-to-end ``run_pipeline`` tests.

The orchestrator's multi-rep pipeline is exercised with synthetic Zoho/QBO
caches + injected Anthropic + SMTP fakes. Sockets are blocked at conftest,
so any accidental live call would raise ``NetworkBlockedError``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from drafting.generator import DrafterConfig
from drafting.sample_judge import JudgeProfile
from pipeline.reps import LoadedRep
from pulse.mailer import RepProfile, load_recipient_policy
from ranking.engine import Play, RankingConfig
from scripts.daily_pulse import PulseStatus, run_pipeline

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "config" / "recipient_allowlist.yaml"

OWNER_EMAIL = "henry@getlivewire.com"
ZACK_EMAIL = "zack@getlivewire.com"
SECRET = b"\x00" * 32


# ---- fakes -----------------------------------------------------------------


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeContent(text)]


class _FakeMessages:
    def __init__(self, behaviors: Iterable[Any]) -> None:
        self._b = list(behaviors)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        nxt = self._b.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return _FakeResponse(nxt)


class _FakeAnthropic:
    def __init__(self, behaviors: Iterable[Any]) -> None:
        self.messages = _FakeMessages(behaviors)


# ---- shared fixtures -------------------------------------------------------


def _ranking_cfg() -> RankingConfig:
    return RankingConfig(
        qbo_horizon=date(2017, 1, 1),
        legacy_bonus_cents_per_year=5_000_000,
        too_recent_days=30,
        ramp_up_days=180,
        sweet_spot_end_days=720,
        decay_floor_days=1825,
        cold_floor_factor=0.5,
        min_rep_match_confidence=0.5,
        suppression_penalty=0.0,
    )


def _drafter_cfg() -> DrafterConfig:
    return DrafterConfig(
        primary_model="claude-sonnet-4-6",
        fallback_model="claude-haiku-4-5-20251001",
        max_tokens=400,
        temperature=0.7,
    )


def _judge_profile() -> JudgeProfile:
    return JudgeProfile(
        allowed_greetings=("hey", "hi"),
        max_emoji=1,
        min_chars=20,
        max_chars=300,
    )


def _policy():
    return load_recipient_policy(owner_email=OWNER_EMAIL, path=ALLOWLIST_PATH)


def _play() -> Play:
    return Play(
        key="daily_proactive",
        base_weight=1.0,
        enabled=True,
        min_days_since_install=None,
        max_days_since_install=None,
        seasonal_weights={},
    )


def _zack_loaded() -> LoadedRep:
    return LoadedRep(
        profile=RepProfile(
            rep_id="zack",
            email=ZACK_EMAIL,
            first_name="Zack",
            cc_review_remaining=5,
        ),
        voice={"formality": "casual", "typical_greetings": ["hey"]},
        zoho_user_id="u_zack",
        ooo_until=None,
        paused_until=None,
    )


def _zoho_contact(zid: str = "z1", *, owner: str = "u_zack") -> dict:
    return {
        "id": zid,
        "Owner": {"id": owner},
        "Created_Time": "2010-01-15T10:00:00+00:00",
        "Full_Name": "Jane Smith",
        "Email": "jane@example.com",
        "Phone": "+15551234567",
    }


def _qbo_customer(qid: str = "q1") -> dict:
    return {
        "Id": qid,
        "DisplayName": "Jane Smith",
        "PrimaryEmailAddr": {"Address": "jane@example.com"},
        "MetaData": {"CreateTime": "2017-03-20T14:30:00+00:00"},
    }


def _invoice(qid: str = "q1", total: float = 524.00, txn: str = "2025-05-07") -> dict:
    return {
        "CustomerRef": {"value": qid},
        "TotalAmt": total,
        "TxnDate": txn,
    }


def _make_pid(rep_id: str) -> str:
    return f"2026-05-07-{rep_id}"


# ---- tests -----------------------------------------------------------------


def test_run_pipeline_happy_path_dry_run() -> None:
    captured: list[EmailMessage] = []
    client = _FakeAnthropic(["Hey Jane — quick check-in on the soundbar."])

    results = run_pipeline(
        today=date(2026, 5, 7),
        reps=[_zack_loaded()],
        plays=[_play()],
        ranking_cfg=_ranking_cfg(),
        drafter_cfg=_drafter_cfg(),  # type: ignore[arg-type]
        policy=_policy(),
        judge_profile=_judge_profile(),
        zoho_contacts=[_zoho_contact()],
        qbo_customers=[_qbo_customer()],
        qbo_invoices=[_invoice()],
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        sender_email="outgrow-control@getlivewire.com",
        anthropic_client=client,
        smtp_send=captured.append,
        pulse_id_factory=_make_pid,
        dry_run=True,
    )

    assert len(results) == 1
    r = results[0]
    assert r.rep_id == "zack"
    assert r.status == PulseStatus.DRAFTED_DRY_RUN
    assert r.customer_id == "z1"
    assert r.pulse_id == "2026-05-07-zack"
    assert r.model_used == "claude-sonnet-4-6"
    assert captured == []  # dry-run, nothing sent


def test_run_pipeline_skips_inactive_rep() -> None:
    rep = _zack_loaded()
    ooo_rep = LoadedRep(
        profile=rep.profile,
        voice=rep.voice,
        zoho_user_id=rep.zoho_user_id,
        ooo_until=date(2026, 5, 10),
        paused_until=None,
    )
    client = _FakeAnthropic([])  # never called

    results = run_pipeline(
        today=date(2026, 5, 7),
        reps=[ooo_rep],
        plays=[_play()],
        ranking_cfg=_ranking_cfg(),
        drafter_cfg=_drafter_cfg(),  # type: ignore[arg-type]
        policy=_policy(),
        judge_profile=_judge_profile(),
        zoho_contacts=[_zoho_contact()],
        qbo_customers=[_qbo_customer()],
        qbo_invoices=[_invoice()],
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        sender_email="outgrow-control@getlivewire.com",
        anthropic_client=client,
        smtp_send=lambda _: None,
        pulse_id_factory=_make_pid,
        dry_run=True,
    )

    assert len(results) == 1
    assert results[0].status == PulseStatus.REP_INACTIVE
    assert results[0].customer_id is None
    assert client.messages.calls == []


def test_run_pipeline_returns_no_eligible_when_owner_unmapped() -> None:
    """Customer with Zoho owner not in any rep YAML never reaches the rep."""
    other_zoho = _zoho_contact(owner="stranger_uid")
    client = _FakeAnthropic([])

    results = run_pipeline(
        today=date(2026, 5, 7),
        reps=[_zack_loaded()],  # only knows u_zack
        plays=[_play()],
        ranking_cfg=_ranking_cfg(),
        drafter_cfg=_drafter_cfg(),  # type: ignore[arg-type]
        policy=_policy(),
        judge_profile=_judge_profile(),
        zoho_contacts=[other_zoho],
        qbo_customers=[_qbo_customer()],
        qbo_invoices=[_invoice()],
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        sender_email="outgrow-control@getlivewire.com",
        anthropic_client=client,
        smtp_send=lambda _: None,
        pulse_id_factory=_make_pid,
        dry_run=True,
    )

    assert results[0].status == PulseStatus.NO_ELIGIBLE_CUSTOMER
    assert client.messages.calls == []  # never reached the drafter
    # Owner mapping filters upstream of ranking, so this rep has zero
    # candidates rather than a per-reason rejection tally.
    assert results[0].eligibility is not None
    assert results[0].eligibility.candidates == 0
    assert results[0].eligibility.rejected == {}


def test_run_pipeline_diagnostics_attach_to_happy_path_result() -> None:
    """Diagnostics ride along even when a pulse goes through, so the funnel
    is visible day-over-day (helps owner spot when filters start cutting too
    much).
    """
    client = _FakeAnthropic(["Hey Jane — quick check-in on the soundbar."])
    results = run_pipeline(
        today=date(2026, 5, 7),
        reps=[_zack_loaded()],
        plays=[_play()],
        ranking_cfg=_ranking_cfg(),
        drafter_cfg=_drafter_cfg(),  # type: ignore[arg-type]
        policy=_policy(),
        judge_profile=_judge_profile(),
        zoho_contacts=[_zoho_contact()],
        qbo_customers=[_qbo_customer()],
        qbo_invoices=[_invoice()],
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        sender_email="outgrow-control@getlivewire.com",
        anthropic_client=client,
        smtp_send=lambda _: None,
        pulse_id_factory=_make_pid,
        dry_run=True,
    )
    assert results[0].status == PulseStatus.DRAFTED_DRY_RUN
    assert results[0].eligibility is not None
    assert results[0].eligibility.candidates == 1
    assert results[0].eligibility.rejected == {}


def test_run_pipeline_returns_empty_when_no_enabled_play() -> None:
    disabled = Play(
        key="daily_proactive",
        base_weight=1.0,
        enabled=False,
        min_days_since_install=None,
        max_days_since_install=None,
        seasonal_weights={},
    )
    results = run_pipeline(
        today=date(2026, 5, 7),
        reps=[_zack_loaded()],
        plays=[disabled],
        ranking_cfg=_ranking_cfg(),
        drafter_cfg=_drafter_cfg(),  # type: ignore[arg-type]
        policy=_policy(),
        judge_profile=_judge_profile(),
        zoho_contacts=[_zoho_contact()],
        qbo_customers=[_qbo_customer()],
        qbo_invoices=[_invoice()],
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        sender_email="outgrow-control@getlivewire.com",
        anthropic_client=_FakeAnthropic([]),
        smtp_send=lambda _: None,
        pulse_id_factory=_make_pid,
        dry_run=True,
    )
    assert results == []


def test_run_pipeline_write_path_sends_via_smtp() -> None:
    captured: list[EmailMessage] = []
    client = _FakeAnthropic(["Hey Jane — quick check-in on the soundbar."])

    results = run_pipeline(
        today=date(2026, 5, 7),
        reps=[_zack_loaded()],
        plays=[_play()],
        ranking_cfg=_ranking_cfg(),
        drafter_cfg=_drafter_cfg(),  # type: ignore[arg-type]
        policy=_policy(),
        judge_profile=_judge_profile(),
        zoho_contacts=[_zoho_contact()],
        qbo_customers=[_qbo_customer()],
        qbo_invoices=[_invoice()],
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        sender_email="outgrow-control@getlivewire.com",
        anthropic_client=client,
        smtp_send=captured.append,
        pulse_id_factory=_make_pid,
        dry_run=False,
    )

    assert results[0].status == PulseStatus.SENT
    assert len(captured) == 1
    msg = captured[0]
    assert msg["To"] == ZACK_EMAIL
    assert msg["Cc"] == OWNER_EMAIL  # zack still in review window
    assert "Jane Smith" in msg.get_content() or "Jane Smith" in msg["Subject"]


def test_run_pipeline_judge_rejection_aborts_send() -> None:
    captured: list[EmailMessage] = []
    drifty = "Hey Jane, I hope this finds you well — wanted to reconnect."
    client = _FakeAnthropic([drifty])

    results = run_pipeline(
        today=date(2026, 5, 7),
        reps=[_zack_loaded()],
        plays=[_play()],
        ranking_cfg=_ranking_cfg(),
        drafter_cfg=_drafter_cfg(),  # type: ignore[arg-type]
        policy=_policy(),
        judge_profile=_judge_profile(),
        zoho_contacts=[_zoho_contact()],
        qbo_customers=[_qbo_customer()],
        qbo_invoices=[_invoice()],
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        sender_email="outgrow-control@getlivewire.com",
        anthropic_client=client,
        smtp_send=captured.append,
        pulse_id_factory=_make_pid,
        dry_run=False,
    )

    assert results[0].status == PulseStatus.JUDGE_REJECTED
    assert captured == []  # judge blocked the send
