"""Weekly digest CLI — Phase 2 reporting.

Runs Monday morning (~07:30 ET) via cron-job.org's ``repository_dispatch``
trigger. Reads the persisted ``state/pulse_history.json`` + the Zoho
contacts cache (populated by the workflow's preceding Zoho sync step),
computes one ``WeeklyDigest`` per active rep, and ships each rep their
own recap email.

Mirrors ``scripts/daily_pulse.py``'s structure: env-validation guard
chain, ``--dry-run`` by default, ``--write`` for real SMTP, sockets
blocked at conftest in tests.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from pipeline.reps import load_reps
from pipeline.suppressions import load_history
from pulse.mailer import SmtpSender, make_smtp_sender
from reports.render import render_html, render_plain, render_subject
from reports.weekly_digest import WeeklyDigest, build_digest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ZOHO_CACHE_DIR = REPO_ROOT / ".cache" / "zoho"

logger = logging.getLogger("outgrow.weekly_digest")


def _read_cache_json(path: Path) -> list[dict]:  # pragma: no cover
    return json.loads(path.read_text(encoding="utf-8"))


def _build_email(
    *,
    digest: WeeklyDigest,
    rep_email: str,
    rep_first_name: str,
    sender_email: str,
) -> EmailMessage:
    """Assemble the multipart/alternative email for one rep's digest."""
    msg = EmailMessage()
    msg["From"] = sender_email
    msg["To"] = rep_email
    msg["Subject"] = render_subject(digest)
    msg.set_content(render_plain(digest, rep_first_name))
    msg.add_alternative(render_html(digest, rep_first_name), subtype="html")
    return msg


def _logging_smtp_sender(msg: EmailMessage) -> None:  # pragma: no cover
    logger.info("[dry-run] would send to=%s subject=%s", msg["To"], msg["Subject"])


def _smtp_sender_from_env() -> SmtpSender:  # pragma: no cover
    """Same env-var contract as ``scripts/daily_pulse._smtp_sender_from_env``."""
    host = os.environ.get("OUTGROW_SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("OUTGROW_SMTP_PORT") or "587")
    user = os.environ.get("OUTGROW_SMTP_USER")
    password = os.environ.get("OUTGROW_SMTP_PASS")
    if not user or not password:
        raise RuntimeError(
            "OUTGROW_SMTP_USER and OUTGROW_SMTP_PASS must be set to run with --write"
        )
    return make_smtp_sender(host=host, port=port, user=user, password=password)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Outgrow weekly digest")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Default. Build the digests but don't send. Set --write to actually email.",
    )
    parser.add_argument(
        "--write",
        dest="dry_run",
        action="store_false",
        help="Actually send. Requires SMTP env vars (same as the daily pulse).",
    )
    parser.add_argument("--zoho-cache-dir", type=Path, default=DEFAULT_ZOHO_CACHE_DIR)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if os.environ.get("OUTGROW_PAUSED", "").lower() == "true":
        logger.info("OUTGROW_PAUSED=true — exiting before any work.")
        return 0

    sender_email = (
        os.environ.get("OUTGROW_SENDER_EMAIL")
        or os.environ.get("OUTGROW_CONTROL_ADDRESS")
        or "outgrow-control@getlivewire.com"
    )

    reps = load_reps()
    if not reps:
        logger.warning("no rep YAMLs; nothing to do this Monday.")
        return 0

    zoho_contacts = _read_cache_json(args.zoho_cache_dir / "contacts.json")
    history = load_history()
    today = date.today()

    smtp_sender: SmtpSender
    if args.dry_run:
        smtp_sender = _logging_smtp_sender
        logger.info("dry-run: digests will be rendered but no email will be sent")
    else:
        smtp_sender = _smtp_sender_from_env()
        logger.info("write mode: digests will be emailed via SMTP")

    sent = 0
    for rep in reps:
        digest = build_digest(
            entries=history,
            zoho_contacts=zoho_contacts,
            rep_id=rep.profile.rep_id,
            today=today,
        )
        logger.info(
            "rep=%s pulses=%d acted=%s",
            rep.profile.rep_id,
            digest.pulses_sent,
            digest.actions,
        )
        if digest.pulses_sent == 0:
            logger.info("rep=%s skipping email (no pulses last week)", rep.profile.rep_id)
            continue
        msg = _build_email(
            digest=digest,
            rep_email=rep.profile.email,
            rep_first_name=rep.profile.first_name,
            sender_email=sender_email,
        )
        smtp_sender(msg)
        sent += 1

    logger.info("weekly_digest sent=%d total reps=%d", sent, len(reps))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
