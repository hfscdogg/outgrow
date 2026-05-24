"""Tests for ``inbox/parser.py``.

Pure unit tests — no I/O. HMAC verification is exercised against a
real ``pulse.links.sign_action`` so we'd catch any drift between
the signing path (used in the morning pulse email) and the verifying
path (used here on the reply).
"""

from __future__ import annotations

import pytest

from inbox.parser import (
    EDITED_BODY_PROMPT,
    SKIPPED_BODY_PROMPT,
    InvalidAction,
    ParsedAction,
    parse_message,
    parse_subject,
)
from pulse.links import (
    ACTION_EDITED,
    ACTION_REASSIGN,
    ACTION_SENT,
    ACTION_SKIPPED,
    sign_action,
)

SECRET = b"\x42" * 32
PULSE_ID = "2026-05-22-henry"


def _subject(action: str, pulse_id: str = PULSE_ID, *, token: str | None = None) -> str:
    """Build a subject the way pulse.links.build_mailto would have."""
    real_token = token if token is not None else sign_action(SECRET, action, pulse_id)
    return f"{action.upper()} pulse_{pulse_id} {real_token}"


# ---- parse_subject ---------------------------------------------------------


def test_parse_subject_extracts_action_pulse_id_token() -> None:
    out = parse_subject("SENT pulse_2026-05-22-henry abc123")
    assert out == ("sent", "2026-05-22-henry", "abc123")


def test_parse_subject_strips_reply_prefix() -> None:
    out = parse_subject("Re: SENT pulse_2026-05-22-henry abc123")
    assert out == ("sent", "2026-05-22-henry", "abc123")


def test_parse_subject_strips_nested_reply_prefixes() -> None:
    out = parse_subject("Re: Fwd: RE: SENT pulse_2026-05-22-henry abc123")
    assert out == ("sent", "2026-05-22-henry", "abc123")


def test_parse_subject_rejects_unknown_action() -> None:
    assert parse_subject("WIBBLE pulse_2026-05-22-henry abc123") is None


def test_parse_subject_rejects_malformed_pulse_marker() -> None:
    assert parse_subject("SENT 2026-05-22-henry abc123") is None  # no pulse_ prefix


def test_parse_subject_rejects_too_few_tokens() -> None:
    assert parse_subject("SENT pulse_2026-05-22-henry") is None


def test_parse_subject_handles_pulse_id_with_hyphens() -> None:
    """pulse_id is YYYY-MM-DD-rep_id — must not break on the internal hyphens."""
    out = parse_subject("EDITED pulse_2026-05-22-zack-r tok")
    assert out == ("edited", "2026-05-22-zack-r", "tok")


# ---- parse_message — happy paths ------------------------------------------


def test_parse_message_sent_returns_parsed_action() -> None:
    result = parse_message(
        subject=_subject(ACTION_SENT),
        body="",
        sender="henry@getlivewire.com",
        secret=SECRET,
    )
    assert isinstance(result, ParsedAction)
    assert result.action == "sent"
    assert result.pulse_id == PULSE_ID
    assert result.edited_text is None
    assert result.skip_reason is None


def test_parse_message_edited_extracts_text_after_prompt() -> None:
    body = f"{EDITED_BODY_PROMPT}\n\nHey Drew — long time no talk. Hope all is well.\n\n- Henry"
    result = parse_message(
        subject=_subject(ACTION_EDITED),
        body=body,
        sender="henry@getlivewire.com",
        secret=SECRET,
    )
    assert isinstance(result, ParsedAction)
    assert result.action == "edited"
    assert result.edited_text is not None
    assert result.edited_text.startswith("Hey Drew")
    assert "- Henry" in result.edited_text


def test_parse_message_edited_falls_back_to_full_body_when_prompt_missing() -> None:
    """If the rep deleted the prompt before typing, we still capture text."""
    result = parse_message(
        subject=_subject(ACTION_EDITED),
        body="Hey Drew — quick check-in.",
        sender="henry@getlivewire.com",
        secret=SECRET,
    )
    assert isinstance(result, ParsedAction)
    assert result.edited_text == "Hey Drew — quick check-in."


def test_parse_message_skipped_extracts_reason() -> None:
    body = f"{SKIPPED_BODY_PROMPT}\n\nOn vacation this week"
    result = parse_message(
        subject=_subject(ACTION_SKIPPED),
        body=body,
        sender="henry@getlivewire.com",
        secret=SECRET,
    )
    assert isinstance(result, ParsedAction)
    assert result.action == "skipped"
    assert result.skip_reason == "On vacation this week"


def test_parse_message_reassign_records_action_only() -> None:
    result = parse_message(
        subject=_subject(ACTION_REASSIGN),
        body="Reassign to: zack\n\n",
        sender="henry@getlivewire.com",
        secret=SECRET,
    )
    assert isinstance(result, ParsedAction)
    assert result.action == "reassign"
    assert result.edited_text is None
    assert result.skip_reason is None


# ---- parse_message — rejection paths --------------------------------------


def test_parse_message_rejects_bad_subject() -> None:
    result = parse_message(
        subject="lunch tomorrow?",
        body="hey",
        sender="random@example.com",
        secret=SECRET,
    )
    assert isinstance(result, InvalidAction)
    assert result.reason == "bad_subject"


def test_parse_message_rejects_forged_hmac() -> None:
    """A wrong token must fail HMAC verify — defends against forgery."""
    forged = f"SENT pulse_{PULSE_ID} not-the-real-token"
    result = parse_message(
        subject=forged,
        body="",
        sender="attacker@example.com",
        secret=SECRET,
    )
    assert isinstance(result, InvalidAction)
    assert result.reason == "bad_token"


def test_parse_message_rejects_token_signed_with_wrong_secret() -> None:
    wrong_secret = b"\x00" * 32
    token = sign_action(wrong_secret, ACTION_SENT, PULSE_ID)
    subj = f"SENT pulse_{PULSE_ID} {token}"
    result = parse_message(
        subject=subj,
        body="",
        sender="henry@getlivewire.com",
        secret=SECRET,
    )
    assert isinstance(result, InvalidAction)
    assert result.reason == "bad_token"


def test_parse_message_edited_empty_after_prompt_returns_none() -> None:
    """Rep tapped EDITED but typed nothing past the prompt → no edited_text."""
    result = parse_message(
        subject=_subject(ACTION_EDITED),
        body=f"{EDITED_BODY_PROMPT}\n\n",
        sender="henry@getlivewire.com",
        secret=SECRET,
    )
    assert isinstance(result, ParsedAction)
    assert result.edited_text is None


# ---- regression-style: a real Gmail-shaped subject from the field --------


@pytest.mark.parametrize(
    "subject_prefix",
    ["SENT", "EDITED", "SKIPPED", "REASSIGN"],
)
def test_parse_message_round_trips_every_known_action(subject_prefix: str) -> None:
    action = subject_prefix.lower()
    token = sign_action(SECRET, action, PULSE_ID)
    subj = f"{subject_prefix} pulse_{PULSE_ID} {token}"
    result = parse_message(
        subject=subj,
        body="",
        sender="henry@getlivewire.com",
        secret=SECRET,
    )
    assert isinstance(result, ParsedAction)
    assert result.action == action
