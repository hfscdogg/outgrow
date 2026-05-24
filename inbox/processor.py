"""Inbox poll orchestrator — ties the Gmail client, parser, and history.

Pure orchestration. Tests inject a ``FakeGmailClient`` and a list of
``PulseEntry`` records; the function returns an updated history list +
diagnostics for the workflow log. No I/O happens here — the Gmail
client handles network, the caller persists the updated history.

The bookkeeping pattern matches ``scripts/daily_pulse.py::run_pipeline``:
emit per-step counts so the workflow log surfaces what happened without
needing to dig into JSON.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from inbox.gmail import GmailClient
from inbox.parser import InvalidAction, ParsedAction, parse_message
from pipeline.suppressions import PulseEntry


@dataclass(frozen=True)
class InboxPollResult:
    fetched: int  # how many unprocessed messages Gmail returned
    applied: int  # successful action -> history updates
    rejected: dict[str, int]  # reason -> count (bad_subject, bad_token, no_match, ...)


def _action_date(received_at: str, today: date) -> date:
    """Best-effort parse of the message's ``Date:`` header into a date.

    Action replies carry the rep's tap time. We want that, not poll time —
    a Friday afternoon tap polled Monday morning should record Friday.
    Falls back to ``today`` on parse failure so a malformed header doesn't
    drop the action.
    """
    if not received_at:
        return today
    try:
        dt: datetime = parsedate_to_datetime(received_at)
    except (TypeError, ValueError):
        return today
    return dt.date()


def run_inbox_poll(
    *,
    history: Sequence[PulseEntry],
    client: GmailClient,
    secret: bytes,
    today: date,
    label_name: str = "Outgrow",
) -> tuple[list[PulseEntry], InboxPollResult]:
    """Fetch new action replies, apply them to ``history``, mark processed.

    Returns the updated history (callers persist via
    ``pipeline.suppressions.save_history``) and a tally of what happened.
    Each Gmail message is *always* marked processed after handling —
    even bad-subject / bad-token / no-match ones — so a poisoned message
    doesn't loop forever. Logging is the audit trail.
    """
    msg_ids = client.list_unprocessed_message_ids(label_name)
    history_by_pulse_id = {e.pulse_id: i for i, e in enumerate(history)}
    new_history: list[PulseEntry] = list(history)
    rejected: dict[str, int] = {}
    applied = 0

    for mid in msg_ids:
        msg = client.get_message(mid)
        parsed = parse_message(
            subject=msg.subject,
            body=msg.body_text,
            sender=msg.sender,
            secret=secret,
        )
        if isinstance(parsed, InvalidAction):
            rejected[parsed.reason] = rejected.get(parsed.reason, 0) + 1
            client.mark_processed(mid)
            continue
        assert isinstance(parsed, ParsedAction)  # noqa: S101 -- exhaustive type guard
        idx = history_by_pulse_id.get(parsed.pulse_id)
        if idx is None:
            # Action reply for a pulse we don't have on record (e.g. the
            # reply is older than the suppression window's prune horizon, or
            # the pulse_history file was reset). Mark processed so we don't
            # keep retrying, but log it.
            rejected["no_match"] = rejected.get("no_match", 0) + 1
            client.mark_processed(mid)
            continue
        new_history[idx] = new_history[idx].with_action(
            action=parsed.action,
            action_at=_action_date(msg.received_at, today),
            edited_text=parsed.edited_text,
            skip_reason=parsed.skip_reason,
        )
        applied += 1
        client.mark_processed(mid)

    return new_history, InboxPollResult(fetched=len(msg_ids), applied=applied, rejected=rejected)


# A small helper for the workflow log — keeps the orchestrator's caller
# (scripts/inbox_poll.py) one-liner clean.
def format_result(result: InboxPollResult) -> str:
    """Render the result as a single grep-able log line."""
    if not result.rejected:
        rejected = "none"
    else:
        rejected = " ".join(
            f"{k}={v}" for k, v in sorted(result.rejected.items(), key=lambda kv: (-kv[1], kv[0]))
        )
    return f"fetched={result.fetched} applied={result.applied} rejected={rejected}"
