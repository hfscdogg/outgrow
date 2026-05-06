"""Daily-pulse orchestrator tests.

The orchestrator is fully testable with injected fakes: an Anthropic
client (matches the shape `drafting.generator` expects), an SMTP
transport (callable), and pre-prepared ``Customer`` records (the
matching layer is Phase 2). Sockets are blocked at conftest, so any
accidental live call would raise ``NetworkBlockedError``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from drafting.generator import CustomerBriefing as DraftBriefing
from drafting.generator import DrafterConfig
from drafting.sample_judge import JudgeProfile
from pulse.mailer import (
    CustomerBriefing as MailBriefing,
)
from pulse.mailer import (
    RecipientPolicy,
    RepProfile,
    load_recipient_policy,
)
from ranking.engine import Customer, Play, RankingConfig
from scripts.daily_pulse import PulseStatus, run_one_pulse

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


def _play() -> Play:
    return Play(
        key="daily_proactive",
        base_weight=1.0,
        enabled=True,
        min_days_since_install=None,
        max_days_since_install=None,
        seasonal_weights={},
    )


def _voice() -> dict[str, Any]:
    return {
        "formality": "casual",
        "avg_sentence_length": 11.5,
        "typical_greetings": ["hey"],
        "typical_signoffs": ["talk soon"],
        "emoji_per_message": 0.3,
        "distinctive_vocab": ["soundbar", "rack"],
    }


def _judge_profile() -> JudgeProfile:
    return JudgeProfile(
        allowed_greetings=("hey", "hi"),
        max_emoji=1,
        min_chars=20,
        max_chars=300,
    )


def _policy() -> RecipientPolicy:
    return load_recipient_policy(owner_email=OWNER_EMAIL, path=ALLOWLIST_PATH)


def _zack(cc_remaining: int = 0) -> RepProfile:
    return RepProfile(
        rep_id="zack",
        email=ZACK_EMAIL,
        first_name="Zack",
        cc_review_remaining=cc_remaining,
    )


def _customer(**overrides: object) -> Customer:
    base: dict[str, Any] = {
        "id": "c1",
        "rep_id": "zack",
        "ltv_cents": 5_000_000,
        "last_purchase_at": date(2025, 5, 6),  # 365 days ago — sweet spot
        "first_known_contact_at": date(2010, 1, 1),
        "rep_match_confidence": 0.9,
        "suppressed": False,
        "suppression_reason": None,
        "last_install_completed_at": None,
    }
    base.update(overrides)
    return Customer(**base)  # type: ignore[arg-type]


def _briefing_for(customer: Customer) -> tuple[DraftBriefing, MailBriefing]:
    draft = DraftBriefing(
        name=f"customer_{customer.id}",
        last_purchase_label="Sep 2024",
        lifetime_spend_label="$52,400 (partial since 2017)",
        last_install_label=None,
        recent_tickets_label=None,
        sanitized_notes="loves the soundbar",
        source_dollar_amounts=frozenset({"$52,400"}),
    )
    mail = MailBriefing(
        name=f"customer_{customer.id}",
        rows=(
            ("lifetime_spend", "$52,400 (partial since 2017)"),
            ("last_purchase", "Sep 2024"),
        ),
    )
    return draft, mail


def _run(*, dry_run: bool, anthropic_text: str = "Hey Jane — quick check-in on the soundbar."):
    captured: list[EmailMessage] = []
    client = _FakeAnthropic([anthropic_text])
    result = run_one_pulse(
        rep=_zack(cc_remaining=5),
        candidates=[_customer()],
        play=_play(),
        ranking_cfg=_ranking_cfg(),
        drafter_cfg=_drafter_cfg(),
        voice=_voice(),
        play_brief="daily proactive",
        today=date(2026, 5, 6),
        briefing_for=_briefing_for,
        pulse_id="p1",
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        sender_email="outgrow-control@getlivewire.com",
        policy=_policy(),
        anthropic_client=client,
        smtp_send=captured.append,
        judge_profile=_judge_profile(),
        dry_run=dry_run,
    )
    return result, client, captured


# ---- tests -----------------------------------------------------------------


def test_happy_path_dry_run_drafts_but_does_not_send() -> None:
    result, client, captured = _run(dry_run=True)
    assert result.status == PulseStatus.DRAFTED_DRY_RUN
    assert result.customer_id == "c1"
    assert result.pulse_id == "p1"
    assert result.model_used == "claude-sonnet-4-6"
    assert len(client.messages.calls) == 1
    assert captured == []  # nothing handed to SMTP


def test_happy_path_write_sends_email() -> None:
    result, _, captured = _run(dry_run=False)
    assert result.status == PulseStatus.SENT
    assert len(captured) == 1
    msg = captured[0]
    assert msg["To"] == ZACK_EMAIL
    assert msg["Cc"] == OWNER_EMAIL  # Zack still in review window (cc_remaining=5)
    assert "Hey Jane — quick check-in" in msg.get_content()


def test_no_eligible_customer_returns_status() -> None:
    captured: list[EmailMessage] = []
    client = _FakeAnthropic([])  # never called
    result = run_one_pulse(
        rep=_zack(),
        candidates=[],  # empty pool
        play=_play(),
        ranking_cfg=_ranking_cfg(),
        drafter_cfg=_drafter_cfg(),
        voice=_voice(),
        play_brief="daily proactive",
        today=date(2026, 5, 6),
        briefing_for=_briefing_for,
        pulse_id="p1",
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        sender_email="outgrow-control@getlivewire.com",
        policy=_policy(),
        anthropic_client=client,
        smtp_send=captured.append,
        judge_profile=_judge_profile(),
        dry_run=False,
    )
    assert result.status == PulseStatus.NO_ELIGIBLE_CUSTOMER
    assert result.customer_id is None
    assert client.messages.calls == []
    assert captured == []


def test_no_eligible_customer_when_all_suppressed() -> None:
    captured: list[EmailMessage] = []
    client = _FakeAnthropic([])
    result = run_one_pulse(
        rep=_zack(),
        candidates=[_customer(suppressed=True, suppression_reason="open_ticket")],
        play=_play(),
        ranking_cfg=_ranking_cfg(),
        drafter_cfg=_drafter_cfg(),
        voice=_voice(),
        play_brief="daily proactive",
        today=date(2026, 5, 6),
        briefing_for=_briefing_for,
        pulse_id="p1",
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        sender_email="outgrow-control@getlivewire.com",
        policy=_policy(),
        anthropic_client=client,
        smtp_send=captured.append,
        judge_profile=_judge_profile(),
        dry_run=False,
    )
    assert result.status == PulseStatus.NO_ELIGIBLE_CUSTOMER
    assert captured == []


def test_judge_rejection_aborts_send() -> None:
    """If the heuristic judge flags the draft, the pulse never leaves the engine.
    Better to skip a day than poison the rep's trust with a bad draft."""
    drifty = "Hey Jane, I hope this finds you well — wanted to reconnect."
    result, _, captured = _run(dry_run=False, anthropic_text=drifty)
    assert result.status == PulseStatus.JUDGE_REJECTED
    assert result.judge is not None
    assert result.judge.passed is False
    assert captured == []  # no email sent
    assert result.generation is not None
    assert result.generation.draft_text == drifty


def test_dry_run_ignores_smtp_even_when_judge_passes() -> None:
    _, _, captured = _run(dry_run=True)
    assert captured == []
