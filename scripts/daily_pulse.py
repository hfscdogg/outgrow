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
from pipeline.suppressions import (
    PulseEntry,
    append_entries,
    load_history,
    recently_pulsed_customer_ids,
    reps_pulsed_today,
    save_history,
)
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
    make_smtp_sender,
    send_pulse,
)
from ranking.engine import (
    Customer,
    EligibilityDiagnostics,
    Play,
    RankingConfig,
    load_plays,
    load_ranking_config,
    rank_for_rep_with_diagnostics,
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
    PIPELINE_ERROR = "pipeline_error"
    ALREADY_PULSED_TODAY = "already_pulsed_today"


@dataclass(frozen=True)
class PulseRunResult:
    rep_id: str
    status: PulseStatus
    customer_id: str | None = None
    pulse_id: str | None = None
    model_used: str | None = None
    judge: JudgeResult | None = None
    generation: GenerationResult | None = None
    eligibility: EligibilityDiagnostics | None = None


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
    ranked, diagnostics = rank_for_rep_with_diagnostics(
        candidates, play, ranking_cfg, rep.rep_id, today, top_n=1
    )
    if not ranked:
        return PulseRunResult(
            rep_id=rep.rep_id,
            status=PulseStatus.NO_ELIGIBLE_CUSTOMER,
            eligibility=diagnostics,
        )
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
            eligibility=diagnostics,
        )

    links = build_action_links(
        secret=secret,
        control_address=control_address,
        pulse_id=pulse_id,
        draft_text=gen.draft_text,
        suggested_rep=suggested_rep,
        customer_email=mail_brief.email,
    )
    pulse_email = build_pulse_email(
        pulse_id=pulse_id,
        rep=rep,
        briefing=mail_brief,
        draft_text=gen.draft_text,
        links=links,
        policy=policy,
        today=today,
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
            eligibility=diagnostics,
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
        eligibility=diagnostics,
    )


def _pick_first_enabled_play(plays: Sequence[Play]) -> Play | None:
    for p in plays:
        if p.enabled:
            return p
    return None


def _format_eligibility(diag: EligibilityDiagnostics | None) -> str:
    """Render diagnostics as ``candidates=N rejected=reason=K reason=K`` for the log."""
    if diag is None:
        return "candidates=- rejected=-"
    if not diag.rejected:
        return f"candidates={diag.candidates} rejected=none"
    parts = sorted(diag.rejected.items(), key=lambda kv: (-kv[1], kv[0]))
    rendered = " ".join(f"{k}={v}" for k, v in parts)
    return f"candidates={diag.candidates} rejected={rendered}"


def _make_briefing_factory(
    zoho_contacts: Sequence[dict],
    qbo_horizon: date,
    today: date,
) -> BriefingFn:
    by_id = {str(z["id"]): z for z in zoho_contacts}

    def factory(customer: Customer) -> tuple[DraftBriefing, MailBriefing]:
        return build_briefing(
            customer,
            zoho_contact=by_id[customer.id],
            qbo_horizon=qbo_horizon,
            today=today,
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
    recently_pulsed_ids: frozenset[str] = frozenset(),
    reps_already_pulsed_today: frozenset[str] = frozenset(),
) -> list[PulseRunResult]:
    """End-to-end pipeline for every rep on ``today``.

    Skipped reps (OOO / paused) get a ``REP_INACTIVE`` result so the
    workflow log shows the full roster.

    ``recently_pulsed_ids`` are customer IDs the engine has emailed in
    the suppression window — they get filtered out before ranking, so
    we don't pester the same customer two weeks in a row.

    ``reps_already_pulsed_today`` are rep_ids with an entry recorded
    for ``today`` already — backup cron triggers see them and no-op.
    """
    import time  # noqa: PLC0415  -- only used for timing logs in this function

    play = _pick_first_enabled_play(plays)
    if play is None:
        logger.warning("no enabled play in plays.yaml; nothing to do")
        return []

    play_brief = PLAY_BRIEFS.get(play.key, play.key.replace("_", " "))
    user_to_rep = zoho_user_to_rep_id_map(list(reps))

    logger.info(
        "loaded %d zoho contacts, %d qbo customers, %d qbo invoices, %d reps",
        len(zoho_contacts),
        len(qbo_customers),
        len(qbo_invoices),
        len(reps),
    )

    t0 = time.monotonic()
    zoho_records = [zoho_contact_to_record(z) for z in zoho_contacts]
    qbo_records = [qbo_customer_to_record(q) for q in qbo_customers]
    match_result = match(zoho_records, qbo_records)
    logger.info(
        "match: auto_matched=%d review_queue=%d unmatched_zoho=%d unmatched_qbo=%d (%.1fs)",
        len(match_result.auto_matched),
        len(match_result.review_queue),
        len(match_result.unmatched_zoho),
        len(match_result.unmatched_qbo),
        time.monotonic() - t0,
    )

    t0 = time.monotonic()
    customers = build_customers(
        zoho_contacts=zoho_contacts,
        qbo_customers=qbo_customers,
        qbo_invoices=qbo_invoices,
        match_result=match_result,
        zoho_user_to_rep_id=user_to_rep,
        recently_pulsed_ids=recently_pulsed_ids,
    )
    logger.info(
        "built %d customer records after rep_id mapping (%.1fs); play=%s",
        len(customers),
        time.monotonic() - t0,
        play.key,
    )
    briefing_for = _make_briefing_factory(zoho_contacts, ranking_cfg.qbo_horizon, today)

    results: list[PulseRunResult] = []
    for rep in reps:
        if rep.profile.rep_id in reps_already_pulsed_today:
            logger.info(
                "rep=%s status=ALREADY_PULSED_TODAY (backup cron, no-op)",
                rep.profile.rep_id,
            )
            results.append(
                PulseRunResult(
                    rep_id=rep.profile.rep_id,
                    status=PulseStatus.ALREADY_PULSED_TODAY,
                )
            )
            continue
        if not is_active_today(rep, today):
            logger.info("rep=%s status=REP_INACTIVE (ooo/paused)", rep.profile.rep_id)
            results.append(
                PulseRunResult(rep_id=rep.profile.rep_id, status=PulseStatus.REP_INACTIVE)
            )
            continue
        t_rep = time.monotonic()
        try:
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
        except Exception:  # noqa: BLE001 -- isolating one rep's failure so the others still send
            # One rep's pulse failing (Anthropic rate limit, SMTP burp, etc.)
            # must NOT cancel the remaining reps. Log the traceback, append a
            # PIPELINE_ERROR result, and keep going. The non-zero exit-code
            # signal that GitHub Actions surfaces still happens at the end if
            # any rep errored — see ``main()`` below.
            logger.exception("rep=%s pipeline raised; skipping send", rep.profile.rep_id)
            results.append(
                PulseRunResult(rep_id=rep.profile.rep_id, status=PulseStatus.PIPELINE_ERROR)
            )
            continue
        logger.info(
            "rep=%s status=%s %s model=%s (%.1fs)",
            rep.profile.rep_id,
            result.status,
            _format_eligibility(result.eligibility),
            result.model_used or "-",
            time.monotonic() - t_rep,
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


def _smtp_sender_from_env() -> SmtpSender:  # pragma: no cover
    """Build a real SMTP sender from env vars. Raises if any are missing.

    Defaults are tuned for Google Workspace (smtp.gmail.com:587 with
    STARTTLS). Override ``OUTGROW_SMTP_HOST`` / ``_PORT`` for other
    providers (Zoho Mail, Microsoft 365, etc.).

    Uses ``... or <default>`` rather than ``dict.get(key, default)``
    because GitHub Actions interpolates an unset repo variable as an
    empty string — present-but-empty, which ``.get(..., default)``
    would not recognize as missing.
    """
    host = os.environ.get("OUTGROW_SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("OUTGROW_SMTP_PORT") or "587")
    user = os.environ.get("OUTGROW_SMTP_USER")
    password = os.environ.get("OUTGROW_SMTP_PASS")
    if not user or not password:
        raise RuntimeError(
            "OUTGROW_SMTP_USER and OUTGROW_SMTP_PASS must be set to run with --write"
        )
    return make_smtp_sender(host=host, port=port, user=user, password=password)


# noqa: PLR0911,PLR0915 -- env-validation guard chain at the top, then the run; refactoring just hides it.
def main(argv: list[str] | None = None) -> int:  # pragma: no cover  # noqa: PLR0911, PLR0915
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
    # GitHub Actions interpolates unset repo variables as empty strings, so
    # ``dict.get(key, default)`` won't fall through — use ``... or default``.
    # Same gotcha pattern as ZOHO_CRM_DC (PR #22) and OUTGROW_SMTP_HOST/PORT
    # (PR #34).
    control_address = os.environ.get("OUTGROW_CONTROL_ADDRESS") or "outgrow-control@getlivewire.com"
    sender_email = os.environ.get("OUTGROW_SENDER_EMAIL") or control_address
    today = date.today()

    def pulse_id_factory(rep_id: str) -> str:
        return f"{today.isoformat()}-{rep_id}"

    smtp_sender: SmtpSender
    if args.dry_run:
        smtp_sender = _logging_smtp_sender
        logger.info("dry-run: drafts will be generated but no email will be sent")
    else:
        smtp_sender = _smtp_sender_from_env()
        logger.info("write mode: pulses will be emailed via SMTP")

    history = load_history()
    recently_pulsed = recently_pulsed_customer_ids(history, today)
    already_pulsed_today = reps_pulsed_today(history, today)
    logger.info(
        "suppressing %d recently-pulsed customer(s); %d rep(s) already pulsed today",
        len(recently_pulsed),
        len(already_pulsed_today),
    )

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
        smtp_send=smtp_sender,
        pulse_id_factory=pulse_id_factory,
        dry_run=args.dry_run,
        recently_pulsed_ids=recently_pulsed,
        reps_already_pulsed_today=already_pulsed_today,
    )

    # Only SENT pulses go into history — dry-run drafts never reached the
    # customer, so the suppression window doesn't apply. Save unconditionally
    # so pruned-only updates still get committed by the workflow.
    new_entries = [
        PulseEntry(
            customer_id=r.customer_id or "",
            rep_id=r.rep_id,
            pulse_id=r.pulse_id or "",
            date=today,
        )
        for r in results
        if r.status == PulseStatus.SENT and r.customer_id and r.pulse_id
    ]
    save_history(append_entries(history, new_entries, today=today))

    _print_dry_run_report(results)
    # Surface per-rep pipeline errors as a non-zero exit so GitHub Actions
    # marks the run as failed (and the email-on-failure notification fires).
    # The other reps' pulses still went out — we just want the operator to
    # notice and investigate the failed one.
    if any(r.status == PulseStatus.PIPELINE_ERROR for r in results):
        logger.error("one or more reps failed; see traceback(s) above")
        return 2
    return 0


def _print_dry_run_report(results: list[PulseRunResult]) -> None:  # pragma: no cover
    """Human-readable per-rep summary for the workflow log.

    Includes the actual draft text + judge result so the owner can read
    each would-be pulse end-to-end without digging through structured
    JSON. Compact JSON dump still happens at the bottom for grep-ability.
    """
    for r in results:
        print(f"\n=== {r.rep_id} ===")
        print(f"status: {r.status}")
        if r.eligibility is not None:
            print(f"eligibility: {_format_eligibility(r.eligibility)}")
        if r.customer_id:
            print(f"customer_id: {r.customer_id}")
        if r.pulse_id:
            print(f"pulse_id: {r.pulse_id}")
        if r.model_used:
            print(f"model_used: {r.model_used}")
        if r.generation:
            print("draft_text:")
            for line in (r.generation.draft_text or "").splitlines() or [""]:
                print(f"  {line}")
        if r.judge:
            verdict = "passed" if r.judge.passed else "FAILED"
            print(f"judge: {verdict}")
            for v in r.judge.violations:
                print(f"  - {v.reason}: {v.detail}")
    print("\n--- summary ---")
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
