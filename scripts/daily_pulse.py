"""Daily-pulse orchestrator: sync -> match -> rank -> draft -> mail.

Three layers:

* ``run_one_pulse(...)`` — single-rep orchestration. Pure: accepts
  pre-prepared candidates and injected deps, returns a
  ``PulseRunResult``.

* ``run_pipeline(...)`` — multi-rep orchestration. Runs match,
  builds Customer records from caches, picks the play, and calls
  ``run_one_pulse`` for every active rep. Pure: accepts injected
  Anthropic client + SMTP transport so the whole pipeline is
  exercisable in tests.

* ``main(argv)`` — the CLI the cron calls. Reads env, loads files,
  instantiates a real Anthropic client, and delegates to
  ``run_pipeline``. The ``OUTGROW_PAUSED`` gate keeps the cron from
  doing anything live until the owner manually flips the variable.

Sockets are blocked at conftest, so any accidental live call from
``run_one_pulse`` / ``run_pipeline`` would raise
``NetworkBlockedError``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from enum import StrEnum
from pathlib import Path
from typing import Any

from drafting.generator import (
    CustomerBriefing as DraftBriefing,
)
from drafting.generator import (
    DrafterConfig,
    GenerationResult,
    generate_and_judge,
    load_drafter_config,
)
from drafting.sample_judge import JudgeProfile, JudgeResult
from matching.identity import (
    match,
    qbo_customer_to_record,
    zoho_contact_to_record,
)
from pipeline.customers import build_briefing, build_customers
from pipeline.reps import LoadedRep, is_active_today, load_reps, zoho_user_to_rep_id_map
from pulse.mailer import (
    CustomerBriefing as MailBriefing,
)
from pulse.mailer import (
    RecipientPolicy,
    RepProfile,
    SmtpSender,
    build_action_links,
    build_pulse_email,
    load_recipient_policy,
    send_pulse,
)
from ranking.engine import (
    Customer,
    Play,
    RankingConfig,
    load_plays,
    load_ranking_config,
    rank_for_rep,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ZOHO_CACHE_DIR = REPO_ROOT / ".cache" / "zoho"
DEFAULT_QBO_CACHE_DIR = REPO_ROOT / ".cache" / "qbo"

logger = logging.getLogger("outgrow.daily_pulse")

# Phase 2 will move these to ``config/prompts/<play>.md`` per docs/plan.md.
# Phase 1 inlines a sensible default per play.key so the drafter has a
# directive without owner authoring extra files.
PLAY_BRIEFS: dict[str, str] = {
    "daily_proactive": (
        "Reach out to a dormant customer with no specific ask. "
        "Keep it casual — re-establish presence, not push a sale."
    ),
    "reverse_dyk": (
        "Open with a recent industry insight or a 'thought of you' — "
        "show you're paying attention, not just selling."
    ),
    "referral": (
        "Ask the customer if they know anyone who'd appreciate similar service. "
        "Anchor the ask in specific recent work."
    ),
    "testimonial": (
        "Ask the customer if they'd share a quick line about their experience. "
        "Anchor the ask in their last completed install."
    ),
    "dyk": (
        "Tell the customer about a related accessory or upgrade that pairs "
        "with what they already own. Lead with the user benefit, not the SKU."
    ),
}

DEFAULT_JUDGE_PROFILE = JudgeProfile(
    allowed_greetings=("hey", "hi", "morning"),
    max_emoji=1,
    min_chars=20,
    max_chars=300,
)


class PulseStatus(StrEnum):
    SENT = "sent"
    DRAFTED_DRY_RUN = "drafted_dry_run"
    NO_ELIGIBLE_CUSTOMER = "no_eligible_customer"
    JUDGE_REJECTED = "judge_rejected"
    REP_INACTIVE = "rep_inactive"


@dataclass(frozen=True)
class PulseRunResult:
    rep_id: str
    status: PulseStatus
    customer_id: str | None = None
    pulse_id: str | None = None
    model_used: str | None = None
    judge: JudgeResult | None = None
    generation: GenerationResult | None = None


BriefingFn = Callable[[Customer], tuple[DraftBriefing, MailBriefing]]
PulseIdFactory = Callable[[str], str]


def run_one_pulse(
    *,
    rep: RepProfile,
    candidates: Iterable[Customer],
    play: Play,
    ranking_cfg: RankingConfig,
    drafter_cfg: DrafterConfig,
    voice: dict[str, Any],
    play_brief: str,
    today: date,
    briefing_for: BriefingFn,
    pulse_id: str,
    secret: bytes,
    control_address: str,
    sender_email: str,
    policy: RecipientPolicy,
    anthropic_client: Any,
    smtp_send: SmtpSender,
    judge_profile: JudgeProfile,
    suggested_rep: str | None = None,
    dry_run: bool = True,
) -> PulseRunResult:
    """Rank → draft → judge → mail one rep's pulse.

    A failed judge result aborts the send (the engine is a habit
    prompter — better to skip a day than poison the rep's trust with a
    bad draft). The caller decides whether to retry / regenerate /
    escalate based on the returned ``judge`` field.
    """
    candidates = list(candidates)
    ranked = rank_for_rep(candidates, play, ranking_cfg, rep.rep_id, today, top_n=1)
    if not ranked:
        return PulseRunResult(rep_id=rep.rep_id, status=PulseStatus.NO_ELIGIBLE_CUSTOMER)
    top = ranked[0]
    customer = next(c for c in candidates if c.id == top.customer_id)
    draft_brief, mail_brief = briefing_for(customer)

    gen, verdict = generate_and_judge(
        anthropic_client,
        voice=voice,
        briefing=draft_brief,
        play_brief=play_brief,
        cfg=drafter_cfg,
        judge_profile=judge_profile,
    )
    if not verdict.passed:
        return PulseRunResult(
            rep_id=rep.rep_id,
            status=PulseStatus.JUDGE_REJECTED,
            customer_id=customer.id,
            pulse_id=pulse_id,
            model_used=gen.model_used,
            judge=verdict,
            generation=gen,
        )

    links = build_action_links(
        secret=secret,
        control_address=control_address,
        pulse_id=pulse_id,
        suggested_rep=suggested_rep,
    )
    pulse_email = build_pulse_email(
        pulse_id=pulse_id,
        rep=rep,
        briefing=mail_brief,
        draft_text=gen.draft_text,
        links=links,
        policy=policy,
    )

    if dry_run:
        return PulseRunResult(
            rep_id=rep.rep_id,
            status=PulseStatus.DRAFTED_DRY_RUN,
            customer_id=customer.id,
            pulse_id=pulse_id,
            model_used=gen.model_used,
            judge=verdict,
            generation=gen,
        )

    send_pulse(pulse_email, sender_email=sender_email, smtp_send=smtp_send)
    return PulseRunResult(
        rep_id=rep.rep_id,
        status=PulseStatus.SENT,
        customer_id=customer.id,
        pulse_id=pulse_id,
        model_used=gen.model_used,
        judge=verdict,
        generation=gen,
    )


def _pick_first_enabled_play(plays: Sequence[Play]) -> Play | None:
    for p in plays:
        if p.enabled:
            return p
    return None


def _make_briefing_factory(
    zoho_contacts: Sequence[dict],
    qbo_horizon: date,
) -> BriefingFn:
    by_id = {str(z["id"]): z for z in zoho_contacts}

    def factory(customer: Customer) -> tuple[DraftBriefing, MailBriefing]:
        return build_briefing(
            customer,
            zoho_contact=by_id[customer.id],
            qbo_horizon=qbo_horizon,
        )

    return factory


def run_pipeline(
    *,
    today: date,
    reps: Sequence[LoadedRep],
    plays: Sequence[Play],
    ranking_cfg: RankingConfig,
    drafter_cfg: DrafterConfig,
    policy: RecipientPolicy,
    judge_profile: JudgeProfile,
    zoho_contacts: Sequence[dict],
    qbo_customers: Sequence[dict],
    qbo_invoices: Sequence[dict],
    secret: bytes,
    control_address: str,
    sender_email: str,
    anthropic_client: Any,
    smtp_send: SmtpSender,
    pulse_id_factory: PulseIdFactory,
    dry_run: bool = True,
) -> list[PulseRunResult]:
    """End-to-end pipeline for every rep on ``today``.

    Skipped reps (OOO / paused) get a ``REP_INACTIVE`` result so the
    workflow log shows the full roster.
    """
    play = _pick_first_enabled_play(plays)
    if play is None:
        logger.warning("no enabled play in plays.yaml; nothing to do")
        return []

    play_brief = PLAY_BRIEFS.get(play.key, play.key.replace("_", " "))
    user_to_rep = zoho_user_to_rep_id_map(list(reps))

    zoho_records = [zoho_contact_to_record(z) for z in zoho_contacts]
    qbo_records = [qbo_customer_to_record(q) for q in qbo_customers]
    match_result = match(zoho_records, qbo_records)

    customers = build_customers(
        zoho_contacts=zoho_contacts,
        qbo_customers=qbo_customers,
        qbo_invoices=qbo_invoices,
        match_result=match_result,
        zoho_user_to_rep_id=user_to_rep,
    )
    briefing_for = _make_briefing_factory(zoho_contacts, ranking_cfg.qbo_horizon)

    results: list[PulseRunResult] = []
    for rep in reps:
        if not is_active_today(rep, today):
            results.append(
                PulseRunResult(rep_id=rep.profile.rep_id, status=PulseStatus.REP_INACTIVE)
            )
            continue
        result = run_one_pulse(
            rep=rep.profile,
            candidates=customers,
            play=play,
            ranking_cfg=ranking_cfg,
            drafter_cfg=drafter_cfg,
            voice=rep.voice,
            play_brief=play_brief,
            today=today,
            briefing_for=briefing_for,
            pulse_id=pulse_id_factory(rep.profile.rep_id),
            secret=secret,
            control_address=control_address,
            sender_email=sender_email,
            policy=policy,
            anthropic_client=anthropic_client,
            smtp_send=smtp_send,
            judge_profile=judge_profile,
            dry_run=dry_run,
        )
        results.append(result)
    return results


def _read_cache_json(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _logging_smtp_sender(msg: EmailMessage) -> None:  # pragma: no cover
    logger.info(
        "[dry-run] would send to=%s cc=%s subject=%s",
        msg["To"],
        msg["Cc"] or "",
        msg["Subject"],
    )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    """CLI entry: load configs + caches, run pipeline, print summary."""
    parser = argparse.ArgumentParser(description="Daily Outgrow pulse orchestrator")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Default. Generate drafts but do not send. Set --write to actually send.",
    )
    parser.add_argument(
        "--write",
        dest="dry_run",
        action="store_false",
        help="Actually send pulse emails. Requires SMTP env vars (Phase 2).",
    )
    parser.add_argument("--zoho-cache-dir", type=Path, default=DEFAULT_ZOHO_CACHE_DIR)
    parser.add_argument("--qbo-cache-dir", type=Path, default=DEFAULT_QBO_CACHE_DIR)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if os.environ.get("OUTGROW_PAUSED", "").lower() == "true":
        logger.info("OUTGROW_PAUSED=true — exiting before any work.")
        return 0

    if not args.dry_run:
        raise NotImplementedError(
            "--write requires SMTP creds in GH Actions secrets (Phase 2). "
            "Run with --dry-run for now."
        )

    owner_email = os.environ.get("OUTGROW_OWNER_EMAIL")
    if not owner_email:
        logger.error("OUTGROW_OWNER_EMAIL not set")
        return 1
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set")
        return 1
    secret_str = os.environ.get("OUTGROW_HMAC_SECRET")
    if not secret_str:
        logger.error("OUTGROW_HMAC_SECRET not set")
        return 1

    plays = load_plays()
    ranking_cfg = load_ranking_config()
    drafter_cfg = load_drafter_config()
    policy = load_recipient_policy(owner_email=owner_email)
    reps = load_reps()
    if not reps:
        logger.warning(
            "no rep YAMLs in config/reps/; run voice/wizard.py and add "
            "rep_id/email/first_name/zoho_user_id fields. Nothing to do today."
        )
        return 0

    zoho_contacts = _read_cache_json(args.zoho_cache_dir / "contacts.json")
    qbo_customers = _read_cache_json(args.qbo_cache_dir / "customers.json")
    qbo_invoices = _read_cache_json(args.qbo_cache_dir / "invoices.json")

    import anthropic  # noqa: PLC0415  (heavy import, only needed in main)

    client = anthropic.Anthropic(api_key=api_key)
    control_address = os.environ.get("OUTGROW_CONTROL_ADDRESS", "outgrow-control@getlivewire.com")
    sender_email = os.environ.get("OUTGROW_SENDER_EMAIL", control_address)
    today = date.today()

    def pulse_id_factory(rep_id: str) -> str:
        return f"{today.isoformat()}-{rep_id}"

    results = run_pipeline(
        today=today,
        reps=reps,
        plays=plays,
        ranking_cfg=ranking_cfg,
        drafter_cfg=drafter_cfg,
        policy=policy,
        judge_profile=DEFAULT_JUDGE_PROFILE,
        zoho_contacts=zoho_contacts,
        qbo_customers=qbo_customers,
        qbo_invoices=qbo_invoices,
        secret=secret_str.encode(),
        control_address=control_address,
        sender_email=sender_email,
        anthropic_client=client,
        smtp_send=_logging_smtp_sender,
        pulse_id_factory=pulse_id_factory,
        dry_run=args.dry_run,
    )

    print(
        json.dumps(
            [
                {
                    "rep_id": r.rep_id,
                    "status": str(r.status),
                    "customer_id": r.customer_id,
                    "pulse_id": r.pulse_id,
                    "model_used": r.model_used,
                }
                for r in results
            ],
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
