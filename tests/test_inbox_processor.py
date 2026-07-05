"""Tests for ``inbox/processor.py``.

Uses a hand-rolled ``FakeGmailClient`` (sockets blocked at conftest, so
the real ``LiveGmailClient`` wouldn't even be able to instantiate a
connection). Exercises:

* applying valid SENT/EDITED/SKIPPED actions to matching history entries
* rejecting messages with bad subjects / bad HMACs
* the "no_match" path when an action arrives for a pulse_id not in
  history (e.g. the entry was pruned past the 90-day window)
* idempotency — every fetched message gets ``mark_processed`` called
  even when rejected, so the poller doesn't loop on bad data
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from inbox.gmail import GmailMessage
from inbox.processor import InboxPollResult, format_result, run_inbox_poll
from pipeline.suppressions import PulseEntry
from pulse.links import (
    ACTION_EDITED,
    ACTION_SENT,
    ACTION_SKIPPED,
    build_sent_bcc_address,
    sign_action,
)

SECRET = b"\x42" * 32
TODAY = date(2026, 5, 22)


@dataclass
class FakeGmailClient:
    """In-memory stand-in for the Gmail API. Records every mark_processed call."""

    messages: dict[str, GmailMessage] = field(default_factory=dict)
    label_to_msg_ids: dict[str, list[str]] = field(default_factory=dict)
    processed: set[str] = field(default_factory=set)
    # query string -> message IDs, for the BCC auto-log pass. Queries the
    # fake receives are recorded so tests can assert what was searched.
    search_results: dict[str, list[str]] = field(default_factory=dict)
    searches: list[str] = field(default_factory=list)

    def list_unprocessed_message_ids(self, label_name: str) -> list[str]:
        all_ids = self.label_to_msg_ids.get(label_name, [])
        return [mid for mid in all_ids if mid not in self.processed]

    def search_message_ids(self, query: str) -> list[str]:
        self.searches.append(query)
        return [m for m in self.search_results.get(query, []) if m not in self.processed]

    def get_message(self, message_id: str) -> GmailMessage:
        return self.messages[message_id]

    def mark_processed(self, message_id: str) -> None:
        self.processed.add(message_id)


def _msg(
    mid: str,
    *,
    action: str,
    pulse_id: str,
    body: str = "",
    token: str | None = None,
    received_at: str = "Fri, 22 May 2026 10:55:00 -0400",
) -> GmailMessage:
    real_token = token if token is not None else sign_action(SECRET, action, pulse_id)
    return GmailMessage(
        message_id=mid,
        thread_id=f"t-{mid}",
        subject=f"{action.upper()} pulse_{pulse_id} {real_token}",
        sender="henry@getlivewire.com",
        to="henry+outgrow@getlivewire.com",
        body_text=body,
        received_at=received_at,
    )


def _entry(pulse_id: str, *, customer_id: str = "c1", rep_id: str = "henry") -> PulseEntry:
    # pulse_id format: YYYY-MM-DD-<rep_id>; take the first three hyphen-
    # delimited fields as the ISO date, regardless of rep_id length.
    y, m, d, *_ = pulse_id.split("-")
    return PulseEntry(
        customer_id=customer_id,
        rep_id=rep_id,
        pulse_id=pulse_id,
        date=date.fromisoformat(f"{y}-{m}-{d}"),
    )


def _client_with(messages: list[GmailMessage], label: str = "Outgrow") -> FakeGmailClient:
    client = FakeGmailClient()
    for m in messages:
        client.messages[m.message_id] = m
    client.label_to_msg_ids[label] = [m.message_id for m in messages]
    return client


# ---- happy paths -----------------------------------------------------------


def test_run_inbox_poll_applies_sent_action_to_matching_entry() -> None:
    pulse_id = "2026-05-22-henry"
    history = [_entry(pulse_id)]
    msg = _msg("m1", action=ACTION_SENT, pulse_id=pulse_id)
    client = _client_with([msg])

    new_history, result = run_inbox_poll(history=history, client=client, secret=SECRET, today=TODAY)

    assert result.applied == 1
    assert result.rejected == {}
    assert new_history[0].action == "sent"
    assert new_history[0].action_at == date(2026, 5, 22)  # from the Date header
    assert "m1" in client.processed


def test_run_inbox_poll_records_edited_text() -> None:
    pulse_id = "2026-05-22-henry"
    history = [_entry(pulse_id)]
    body = "Paste the final text you sent below:\n\nHey Drew — long time no talk."
    msg = _msg("m1", action=ACTION_EDITED, pulse_id=pulse_id, body=body)
    client = _client_with([msg])

    new_history, result = run_inbox_poll(history=history, client=client, secret=SECRET, today=TODAY)

    assert result.applied == 1
    assert new_history[0].action == "edited"
    assert new_history[0].edited_text == "Hey Drew — long time no talk."


def test_run_inbox_poll_records_skip_reason() -> None:
    pulse_id = "2026-05-22-henry"
    history = [_entry(pulse_id)]
    body = "Reason (optional):\n\nCustomer asked us not to contact this month"
    msg = _msg("m1", action=ACTION_SKIPPED, pulse_id=pulse_id, body=body)
    client = _client_with([msg])

    new_history, result = run_inbox_poll(history=history, client=client, secret=SECRET, today=TODAY)

    assert result.applied == 1
    assert new_history[0].action == "skipped"
    assert new_history[0].skip_reason == "Customer asked us not to contact this month"


def test_run_inbox_poll_processes_multiple_messages_in_one_run() -> None:
    history = [
        _entry("2026-05-22-henry"),
        _entry("2026-05-22-zack", rep_id="zack", customer_id="c2"),
    ]
    msgs = [
        _msg("m1", action=ACTION_SENT, pulse_id="2026-05-22-henry"),
        _msg("m2", action=ACTION_SENT, pulse_id="2026-05-22-zack"),
    ]
    client = _client_with(msgs)

    new_history, result = run_inbox_poll(history=history, client=client, secret=SECRET, today=TODAY)

    assert result.applied == 2
    assert {e.action for e in new_history} == {"sent"}


# ---- rejection paths -------------------------------------------------------


def test_run_inbox_poll_rejects_bad_subject_but_still_marks_processed() -> None:
    history = [_entry("2026-05-22-henry")]
    bad = GmailMessage(
        message_id="m1",
        thread_id="t",
        subject="Lunch tomorrow?",
        sender="bob@example.com",
        to="henry+outgrow@getlivewire.com",
        body_text="hey",
        received_at="",
    )
    client = _client_with([bad])

    _, result = run_inbox_poll(history=history, client=client, secret=SECRET, today=TODAY)

    assert result.applied == 0
    assert result.rejected == {"bad_subject": 1}
    # CRITICAL: still marked processed so we don't loop on this forever.
    assert "m1" in client.processed


def test_run_inbox_poll_rejects_forged_hmac() -> None:
    history = [_entry("2026-05-22-henry")]
    forged = _msg(
        "m1",
        action=ACTION_SENT,
        pulse_id="2026-05-22-henry",
        token="0" * 32,
    )
    client = _client_with([forged])

    _, result = run_inbox_poll(history=history, client=client, secret=SECRET, today=TODAY)

    assert result.rejected == {"bad_token": 1}
    assert "m1" in client.processed


def test_run_inbox_poll_handles_no_match_pulse_id() -> None:
    """Reply lands for a pulse we don't have on record — log + mark processed."""
    history = [_entry("2026-05-22-henry")]
    msg = _msg("m1", action=ACTION_SENT, pulse_id="2025-01-01-henry")
    client = _client_with([msg])

    new_history, result = run_inbox_poll(history=history, client=client, secret=SECRET, today=TODAY)

    assert result.applied == 0
    assert result.rejected == {"no_match": 1}
    assert "m1" in client.processed
    assert new_history[0].action is None  # original entry untouched


# ---- received-at date parsing ---------------------------------------------


def test_run_inbox_poll_uses_message_date_header_for_action_at() -> None:
    """Rep tapped Friday afternoon, polled Monday → action_at must be Friday."""
    pulse_id = "2026-05-22-henry"
    history = [_entry(pulse_id)]
    msg = _msg(
        "m1",
        action=ACTION_SENT,
        pulse_id=pulse_id,
        received_at="Fri, 22 May 2026 16:30:00 -0400",
    )
    client = _client_with([msg])

    new_history, _ = run_inbox_poll(
        history=history,
        client=client,
        secret=SECRET,
        today=date(2026, 5, 25),  # Monday — three days after the tap
    )

    assert new_history[0].action_at == date(2026, 5, 22)


def test_run_inbox_poll_falls_back_to_today_on_unparseable_date() -> None:
    pulse_id = "2026-05-22-henry"
    history = [_entry(pulse_id)]
    msg = _msg("m1", action=ACTION_SENT, pulse_id=pulse_id, received_at="not a date")
    client = _client_with([msg])

    new_history, _ = run_inbox_poll(history=history, client=client, secret=SECRET, today=TODAY)

    assert new_history[0].action_at == TODAY


# ---- empty case -----------------------------------------------------------


def test_run_inbox_poll_empty_inbox_returns_unchanged_history() -> None:
    history = [_entry("2026-05-22-henry")]
    client = _client_with([])

    new_history, result = run_inbox_poll(history=history, client=client, secret=SECRET, today=TODAY)

    assert result.fetched == 0
    assert result.applied == 0
    assert result.rejected == {}
    assert new_history == history


# ---- BCC auto-log pass ------------------------------------------------------

CONTROL = "henry+outgrow@getlivewire.com"
DRAFT = "Hey Jane! Hope the system is treating you well. Let me know if you need anything."


def _bcc_entry(pulse_id: str, *, draft: str | None = DRAFT) -> PulseEntry:
    y, m, d, *_ = pulse_id.split("-")
    return PulseEntry(
        customer_id="c1",
        rep_id="henry",
        pulse_id=pulse_id,
        date=date.fromisoformat(f"{y}-{m}-{d}"),
        draft_text=draft,
    )


def _bcc_query(pulse_id: str) -> str:
    addr = build_sent_bcc_address(control_address=CONTROL, pulse_id=pulse_id, secret=SECRET)
    return f"deliveredto:{addr} -label:OutgrowProcessed"


def _outbound_copy(mid: str, body: str) -> GmailMessage:
    """The BCC'd copy of what the rep sent the customer via the compose button."""
    return GmailMessage(
        message_id=mid,
        thread_id=f"t-{mid}",
        subject="Checking in",
        sender="Henry Clifford <henry@getlivewire.com>",
        to="jane@example.com",
        body_text=body,
        received_at="Fri, 22 May 2026 09:12:00 -0400",
    )


def test_bcc_pass_records_sent_when_body_matches_draft() -> None:
    pulse_id = "2026-05-22-henry"
    history = [_bcc_entry(pulse_id)]
    client = _client_with([])
    copy = _outbound_copy("b1", DRAFT)
    client.messages["b1"] = copy
    client.search_results[_bcc_query(pulse_id)] = ["b1"]

    new_history, result = run_inbox_poll(
        history=history, client=client, secret=SECRET, today=TODAY, control_address=CONTROL
    )

    assert result.auto_logged == 1
    assert new_history[0].action == "sent"
    assert new_history[0].edited_text is None
    assert new_history[0].action_at == date(2026, 5, 22)
    assert new_history[0].draft_text == DRAFT  # provenance survives with_action
    assert "b1" in client.processed


def test_bcc_pass_tolerates_appended_signature_as_sent() -> None:
    """Draft + auto-appended signature is still a verbatim send."""
    pulse_id = "2026-05-22-henry"
    history = [_bcc_entry(pulse_id)]
    client = _client_with([])
    body_with_sig = DRAFT + "\r\n\r\n- Henry\r\nHENRY CLIFFORD | CEO\r\nOffice: 804-937-9001"
    client.messages["b1"] = _outbound_copy("b1", body_with_sig)
    client.search_results[_bcc_query(pulse_id)] = ["b1"]

    new_history, result = run_inbox_poll(
        history=history, client=client, secret=SECRET, today=TODAY, control_address=CONTROL
    )

    assert result.auto_logged == 1
    assert new_history[0].action == "sent"


def test_bcc_pass_records_edited_with_body_when_text_changed() -> None:
    pulse_id = "2026-05-22-henry"
    history = [_bcc_entry(pulse_id)]
    client = _client_with([])
    rewritten = "Jane! Totally different opener. Hope you're well."
    client.messages["b1"] = _outbound_copy("b1", rewritten)
    client.search_results[_bcc_query(pulse_id)] = ["b1"]

    new_history, result = run_inbox_poll(
        history=history, client=client, secret=SECRET, today=TODAY, control_address=CONTROL
    )

    assert result.auto_logged == 1
    assert new_history[0].action == "edited"
    assert new_history[0].edited_text == rewritten


def test_bcc_pass_records_sent_when_entry_predates_draft_text_field() -> None:
    """Old history entries have no draft to compare — record as sent."""
    pulse_id = "2026-05-22-henry"
    history = [_bcc_entry(pulse_id, draft=None)]
    client = _client_with([])
    client.messages["b1"] = _outbound_copy("b1", "whatever the rep wrote")
    client.search_results[_bcc_query(pulse_id)] = ["b1"]

    new_history, result = run_inbox_poll(
        history=history, client=client, secret=SECRET, today=TODAY, control_address=CONTROL
    )

    assert result.auto_logged == 1
    assert new_history[0].action == "sent"
    assert new_history[0].edited_text is None


def test_bcc_pass_skips_entries_that_already_have_actions() -> None:
    pulse_id = "2026-05-22-henry"
    entry = _bcc_entry(pulse_id).with_action(action=ACTION_SKIPPED, action_at=TODAY)
    client = _client_with([])

    _, result = run_inbox_poll(
        history=[entry], client=client, secret=SECRET, today=TODAY, control_address=CONTROL
    )

    assert result.auto_logged == 0
    assert client.searches == []  # no Gmail query spent on actioned entries


def test_bcc_pass_skips_entries_older_than_lookback() -> None:
    old = _bcc_entry("2026-04-01-henry")  # 51 days before TODAY
    client = _client_with([])

    _, result = run_inbox_poll(
        history=[old], client=client, secret=SECRET, today=TODAY, control_address=CONTROL
    )

    assert result.auto_logged == 0
    assert client.searches == []


def test_bcc_pass_disabled_without_control_address() -> None:
    history = [_bcc_entry("2026-05-22-henry")]
    client = _client_with([])
    client.search_results[_bcc_query("2026-05-22-henry")] = ["b1"]

    _, result = run_inbox_poll(history=history, client=client, secret=SECRET, today=TODAY)

    assert result.auto_logged == 0
    assert client.searches == []


def test_explicit_reply_beats_bcc_inference_on_a_later_poll() -> None:
    """Poll 1 auto-logs 'sent' from the BCC copy; poll 2 has an explicit
    EDITED reply — the rep's explicit word must overwrite the inference."""
    pulse_id = "2026-05-22-henry"
    client = _client_with([])
    client.messages["b1"] = _outbound_copy("b1", DRAFT)
    client.search_results[_bcc_query(pulse_id)] = ["b1"]

    history1, r1 = run_inbox_poll(
        history=[_bcc_entry(pulse_id)],
        client=client,
        secret=SECRET,
        today=TODAY,
        control_address=CONTROL,
    )
    assert r1.auto_logged == 1
    assert history1[0].action == "sent"

    explicit = _msg(
        "m9",
        action=ACTION_EDITED,
        pulse_id=pulse_id,
        body="Paste the final text you sent below:\n\nActually sent something else.",
    )
    client.messages["m9"] = explicit
    client.label_to_msg_ids["Outgrow"] = ["m9"]

    history2, r2 = run_inbox_poll(
        history=history1, client=client, secret=SECRET, today=TODAY, control_address=CONTROL
    )
    assert r2.applied == 1
    assert history2[0].action == "edited"
    assert history2[0].edited_text == "Actually sent something else."


# ---- format_result --------------------------------------------------------


def test_format_result_renders_grep_able_line() -> None:
    r = InboxPollResult(
        fetched=5, applied=3, rejected={"bad_token": 1, "no_match": 1}, auto_logged=2
    )
    out = format_result(r)
    assert "fetched=5" in out
    assert "applied=3" in out
    assert "auto_logged=2" in out
    assert "bad_token=1" in out
    assert "no_match=1" in out


def test_format_result_says_none_when_no_rejections() -> None:
    assert "rejected=none" in format_result(InboxPollResult(fetched=2, applied=2, rejected={}))
