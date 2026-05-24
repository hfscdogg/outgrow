"""Inbox poll CLI — Phase 2 entry point.

Hourly cron-job.org POST triggers the ``inbox_poll`` workflow which
invokes this module. Reads action replies from the rep's Gmail
``+outgrow`` label, verifies HMAC, applies the action to
``state/pulse_history.json``, and re-commits the updated history back
to main via the workflow's final step.

Mirrors ``scripts/daily_pulse.py``'s structure — env-validation guard
chain, sockets blocked at conftest in tests, dependencies injected.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from inbox.gmail import GmailCreds, LiveGmailClient
from inbox.processor import format_result, run_inbox_poll
from pipeline.suppressions import load_history, save_history

logger = logging.getLogger("outgrow.inbox_poll")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    """CLI entry: load creds, poll Gmail, apply actions, save history."""
    parser = argparse.ArgumentParser(description="Outgrow inbox poller")
    parser.add_argument(
        "--label",
        default=None,
        help="Override Gmail label to poll (default: env OUTGROW_INBOX_LABEL or 'Outgrow').",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if os.environ.get("OUTGROW_PAUSED", "").lower() == "true":
        logger.info("OUTGROW_PAUSED=true — exiting before any work.")
        return 0

    secret_str = os.environ.get("OUTGROW_HMAC_SECRET")
    if not secret_str:
        logger.error("OUTGROW_HMAC_SECRET not set")
        return 1

    creds = GmailCreds.from_env()
    client = LiveGmailClient(creds=creds)
    label = args.label or os.environ.get("OUTGROW_INBOX_LABEL") or "Outgrow"

    history = load_history()
    today = date.today()

    new_history, result = run_inbox_poll(
        history=history,
        client=client,
        secret=secret_str.encode(),
        today=today,
        label_name=label,
    )
    logger.info("inbox_poll %s", format_result(result))

    if result.applied > 0:
        save_history(new_history)
        logger.info("persisted %d updated entries to pulse_history.json", result.applied)
    else:
        logger.info("no action updates; pulse_history.json unchanged")

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
