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

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime

from inbox.gmail import GmailClient
from inbox.parser import InvalidAction, ParsedAction, parse_message
from pipeline.suppressions import PulseEntry
from pulse.links import ACTION_EDITED, ACTION_SENT, build_sent_bcc_address

# How far back the BCC auto-log pass scans for un-actioned pulses. Each
# candidate costs one Gmail search, so the window bounds API traffic;
# a rep who hasn't used the compose button within two weeks of the pulse
# isn't going to.
BCC_LOOKBACK_DAYS = 14


@dataclass(frozen=True)
class InboxPollResult:
    fetched: int  # how many unprocessed messages Gmail returned
    applied: int  # successful action -> history updates
    rejected: dict[str, int]  # reason -> count (bad_subject, bad_token, no_match, ...)
    auto_logged: int = 0  # actions inferred from compose-button BCC copies


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


def _normalize_for_compare(text: str) -> str:
    """Collapse all whitespace runs so line-wrapping / \\r\\n differences
    between the pulse draft and a mail client's sent copy don't register
    as edits."""
    return re.sub(r"\s+", " ", text).strip()


def _bcc_search_query(addr: str) -> str:
    """Gmail query that finds the compose-button BCC copy in the control
    mailbox, covering BOTH delivery shapes:

    * Rep account != control account (e.g. Zack sending): the BCC traverses
      real delivery into the control mailbox and the received copy carries a
      ``Delivered-To`` header — ``deliveredto:`` matches.
    * Rep account == control account (the owner sending from the same Gmail
      that IS the control mailbox): Gmail dedupes the self-addressed BCC, so
      the ONLY copy in the mailbox is the SENT one — which has no
      ``Delivered-To`` header and is invisible to ``deliveredto:``. That
      missed Henry's Jul 9 2026 Matt Dunham send. ``bcc:`` matches sent
      copies, closing the gap.

    Braces are Gmail's OR group. ``to:``/``cc:`` are belt-and-braces for
    clients that put the log address in a visible recipient field. The token
    in the address local part keeps all of these unforgeable without the
    HMAC secret; ``-label`` keeps handled copies from re-applying forever.
    """
    return f"{{deliveredto:{addr} bcc:{addr} to:{addr} cc:{addr}}} -label:OutgrowProcessed"


def _classify_bcc_body(body: str, draft_text: str | None) -> tuple[str, str | None]:
    """Infer (action, edited_text) from a compose-button BCC copy.

    ``startswith`` rather than equality: mail clients auto-append
    signatures below the draft, and "draft + signature" is still a
    verbatim send. Anything else — changed greeting, rewritten text —
    is an edit, recorded with the full body so the drafter-fit metric
    sees what actually went out. Entries predating the ``draft_text``
    field can't be compared; recorded as sent.
    """
    if draft_text is None:
        return ACTION_SENT, None
    norm_body = _normalize_for_compare(body)
    norm_draft = _normalize_for_compare(draft_text)
    if norm_body.startswith(norm_draft):
        return ACTION_SENT, None
    return ACTION_EDITED, body


def run_inbox_poll(
    *,
    history: Sequence[PulseEntry],
    client: GmailClient,
    secret: bytes,
    today: date,
    label_name: str = "Outgrow",
    control_address: str | None = None,
) -> tuple[list[PulseEntry], InboxPollResult]:
    """Fetch new action replies, apply them to ``history``, mark processed.

    Returns the updated history (callers persist via
    ``pipeline.suppressions.save_history``) and a tally of what happened.
    Each Gmail message is *always* marked processed after handling —
    even bad-subject / bad-token / no-match ones — so a poisoned message
    doesn't loop forever. Logging is the audit trail.

    Two passes:

    1. **Explicit action replies** (subject-token verified) — unchanged.
    2. **BCC auto-log** (needs ``control_address``): for each recent entry
       still without an action, search for the compose button's per-pulse
       BCC address. A hit means the rep sent the draft via "Send to
       customer" without tapping an action button — record it for them.
       Explicit replies win: they are applied every poll regardless of
       existing state, while this pass only fills gaps.
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

    auto_logged = 0
    if control_address:
        cutoff = today - timedelta(days=BCC_LOOKBACK_DAYS)
        for idx, entry in enumerate(new_history):
            if entry.action is not None or entry.date < cutoff:
                continue
            addr = build_sent_bcc_address(
                control_address=control_address, pulse_id=entry.pulse_id, secret=secret
            )
            hits = client.search_message_ids(_bcc_search_query(addr))
            if not hits:
                continue
            msg = client.get_message(hits[0])
            action, edited_text = _classify_bcc_body(msg.body_text, entry.draft_text)
            new_history[idx] = entry.with_action(
                action=action,
                action_at=_action_date(msg.received_at, today),
                edited_text=edited_text,
            )
            auto_logged += 1
            for hit in hits:
                client.mark_processed(hit)

    return new_history, InboxPollResult(
        fetched=len(msg_ids), applied=applied, rejected=rejected, auto_logged=auto_logged
    )


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
    return (
        f"fetched={result.fetched} applied={result.applied} "
        f"auto_logged={result.auto_logged} rejected={rejected}"
    )
