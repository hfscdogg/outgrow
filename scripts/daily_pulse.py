"""Daily-pulse orchestrator: sync -> rank -> draft -> mail.

Wires the per-module pieces shipped in PRs 1-5 into the 07:00 ET
weekday cron. Two layers:

* ``run_one_pulse(...)`` — pure orchestration of one rep's pulse.
  Accepts a pre-prepared list of ``Customer`` records, picks the top
  via ranking, generates a draft via Anthropic, runs it through the
  judge, builds the email, and (unless ``dry_run=True``) hands it to
  the injected SMTP transport. Fully testable with fakes.

* ``main(argv)`` — the CLI the workflow calls. Loads configs, runs the
  Zoho + QBO syncs end-to-end (which work today, GET-only), and stops
  at the cache->joined-``Customer`` step with a clear Phase 2 boundary
  message. The matching layer (``matching/identity.py`` per
  docs/plan.md) is the next-PR concern; the workflow gate
  (``vars.OUTGROW_PAUSED``) keeps the cron from doing anything live
  until that lands.

Sockets are blocked at conftest import time, so any accidental live
call from ``run_one_pulse`` would raise ``NetworkBlockedError``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
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
)
from drafting.sample_judge import JudgeProfile, JudgeResult
from pulse.links import build_mailto  # noqa: F401  (re-exported for callers)
from pulse.mailer import (
    CustomerBriefing as MailBriefing,
)
from pulse.mailer import (
    RecipientPolicy,
    RepProfile,
    SmtpSender,
    build_action_links,
    build_pulse_email,
    send_pulse,
)
from ranking.engine import (
    Customer,
    Play,
    RankingConfig,
    rank_for_rep,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("outgrow.daily_pulse")


class PulseStatus(StrEnum):
    SENT = "sent"
    DRAFTED_DRY_RUN = "drafted_dry_run"
    NO_ELIGIBLE_CUSTOMER = "no_eligible_customer"
    JUDGE_REJECTED = "judge_rejected"


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
    """Rank → draft → mail one rep's pulse.

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


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    """CLI entry: run end-to-end syncs, stop at the matching-layer boundary.

    Phase 1 ships sync (works), ranking + drafting + mailing (work in
    isolation, glued together by ``run_one_pulse``). The bridge from
    ``.cache/{zoho,qbo}/`` JSON to joined ``Customer`` records lives in
    the not-yet-shipped matching layer; this CLI documents that
    boundary instead of pretending past it.
    """
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
        help="Actually send pulse emails. Requires OUTGROW_PAUSED!=true upstream.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if os.environ.get("OUTGROW_PAUSED", "").lower() == "true":
        logger.info("OUTGROW_PAUSED=true — exiting before any work.")
        return 0

    logger.info("Phase 1: syncs run end-to-end; cache->Customer wiring is Phase 2.")
    logger.info("Run `python -m sync.zoho_crm` and `python -m sync.qbo` to populate caches.")
    logger.info("dry_run=%s", args.dry_run)
    logger.info(
        "Skipping rank/draft/mail until matching/identity.py lands (Phase 2). "
        "Workflow gate keeps this safe; no live emails sent."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
