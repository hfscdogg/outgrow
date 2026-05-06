"""Daily-pulse mailer tests.

Sockets are blocked at conftest import time, so the SMTP transport is
injected as a fake. Recipient allowlist is loaded from the shipped
``config/recipient_allowlist.yaml`` for end-to-end realism.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest

from pulse.links import verify_action
from pulse.mailer import (
    CustomerBriefing,
    RecipientPolicy,
    RepProfile,
    assert_recipients_allowed,
    build_action_links,
    build_pulse_email,
    load_recipient_policy,
    render_pulse_body,
    send_pulse,
    should_cc_owner,
    to_email_message,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = REPO_ROOT / "config" / "recipient_allowlist.yaml"

OWNER_EMAIL = "henry@getlivewire.com"
ZACK_EMAIL = "zack@getlivewire.com"
SECRET = b"\x00" * 32


def _briefing() -> CustomerBriefing:
    return CustomerBriefing(
        name="Jane Smith",
        rows=(
            ("lifetime_spend", "$52,400 (partial since 2017)"),
            ("last_purchase", "Sep 2024"),
            ("last_install", "AV rack, Sep 2024"),
        ),
    )


def _policy() -> RecipientPolicy:
    return load_recipient_policy(owner_email=OWNER_EMAIL, path=ALLOWLIST_PATH)


# ---- recipient policy ------------------------------------------------------


def test_load_policy_owner_must_be_on_allowlist() -> None:
    with pytest.raises(RuntimeError, match="not in"):
        load_recipient_policy(owner_email="stranger@example.com", path=ALLOWLIST_PATH)


def test_load_policy_lowercases_addresses() -> None:
    policy = _policy()
    assert OWNER_EMAIL.lower() in policy.allowed
    assert ZACK_EMAIL.lower() in policy.allowed


def test_assert_recipients_passes_for_allowed_address() -> None:
    assert_recipients_allowed([ZACK_EMAIL], _policy())


def test_assert_recipients_raises_for_offlist_address() -> None:
    with pytest.raises(RuntimeError, match="recipient 'attacker@example.com'"):
        assert_recipients_allowed(["attacker@example.com"], _policy())


def test_assert_recipients_is_case_insensitive() -> None:
    assert_recipients_allowed([ZACK_EMAIL.upper()], _policy())


# ---- action links ----------------------------------------------------------


def test_build_action_links_emits_three_for_high_confidence_match() -> None:
    links = build_action_links(
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        pulse_id="42",
    )
    assert links.sent_as_is.startswith("mailto:")
    assert links.sent_with_edits.startswith("mailto:")
    assert links.skip_today.startswith("mailto:")
    assert links.reassign is None


def test_build_action_links_emits_reassign_when_suggested_rep_given() -> None:
    links = build_action_links(
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        pulse_id="42",
        suggested_rep="zack",
    )
    assert links.reassign is not None
    assert "zack" in links.reassign


def test_action_link_tokens_are_verifiable() -> None:
    """Round-trip: a token embedded in the SENT link must verify against
    the same secret + action + pulse_id used to build it."""
    links = build_action_links(
        secret=SECRET,
        control_address="control@example.com",
        pulse_id="abc123",
    )
    # Subject in the URL is "SENT pulse_abc123 <token>"; pull token out.
    sent_url = links.sent_as_is
    # The token is the substring after "pulse_abc123 " (URL-encoded as +)
    needle_plus = "pulse_abc123+"
    needle_pct = "pulse_abc123%20"
    assert needle_plus in sent_url or needle_pct in sent_url
    sep = needle_plus if needle_plus in sent_url else needle_pct
    after = sent_url.split(sep, 1)[1]
    # Token continues until "&" (next param) or end of URL
    token = after.split("&", 1)[0]
    assert verify_action(SECRET, "sent", "abc123", token) is True


# ---- CC review counter -----------------------------------------------------


def test_should_cc_owner_when_review_remaining_positive() -> None:
    rep = RepProfile(rep_id="zack", email=ZACK_EMAIL, first_name="Zack", cc_review_remaining=3)
    assert should_cc_owner(rep) is True


def test_should_not_cc_owner_when_review_exhausted() -> None:
    rep = RepProfile(rep_id="zack", email=ZACK_EMAIL, first_name="Zack", cc_review_remaining=0)
    assert should_cc_owner(rep) is False


# ---- build_pulse_email ----------------------------------------------------


def _links() -> object:
    return build_action_links(
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        pulse_id="p1",
    )


def test_build_pulse_email_cc_zack_review_window() -> None:
    rep = RepProfile(rep_id="zack", email=ZACK_EMAIL, first_name="Zack", cc_review_remaining=5)
    pulse = build_pulse_email(
        pulse_id="p1",
        rep=rep,
        briefing=_briefing(),
        draft_text="Hey Jane — checking in.",
        links=_links(),  # type: ignore[arg-type]
        policy=_policy(),
    )
    assert pulse.cc == (OWNER_EMAIL,)
    assert pulse.cc_was_review is True
    assert pulse.rep_email == ZACK_EMAIL
    assert pulse.subject == "Outgrow pulse — Jane Smith"


def test_build_pulse_email_no_cc_after_review_window() -> None:
    rep = RepProfile(rep_id="zack", email=ZACK_EMAIL, first_name="Zack", cc_review_remaining=0)
    pulse = build_pulse_email(
        pulse_id="p1",
        rep=rep,
        briefing=_briefing(),
        draft_text="Hey Jane — checking in.",
        links=_links(),  # type: ignore[arg-type]
        policy=_policy(),
    )
    assert pulse.cc == ()
    assert pulse.cc_was_review is False


def test_build_pulse_email_owner_pulse_does_not_self_cc() -> None:
    """Owner is also a pilot rep; CC'ing themselves on their own pulse is noise.
    Drift-journal review covers owner-on-owner instead."""
    rep = RepProfile(
        rep_id="henry",
        email=OWNER_EMAIL,
        first_name="Henry",
        cc_review_remaining=99,
    )
    pulse = build_pulse_email(
        pulse_id="p1",
        rep=rep,
        briefing=_briefing(),
        draft_text="Hey Jane — checking in.",
        links=_links(),  # type: ignore[arg-type]
        policy=_policy(),
    )
    assert pulse.cc == ()


def test_build_pulse_email_refuses_offlist_rep() -> None:
    rep = RepProfile(
        rep_id="ghost",
        email="ghost@example.com",
        first_name="Ghost",
        cc_review_remaining=0,
    )
    with pytest.raises(RuntimeError, match="not in the recipient allowlist"):
        build_pulse_email(
            pulse_id="p1",
            rep=rep,
            briefing=_briefing(),
            draft_text="Hey",
            links=_links(),  # type: ignore[arg-type]
            policy=_policy(),
        )


# ---- render_pulse_body -----------------------------------------------------


def test_render_body_contains_briefing_draft_and_links() -> None:
    body = render_pulse_body(
        rep_first_name="Zack",
        briefing=_briefing(),
        draft_text="Hey Jane — wanted to check in on the soundbar setup.",
        links=_links(),  # type: ignore[arg-type]
    )
    assert "Morning Zack" in body
    assert "Jane Smith" in body
    assert "$52,400" in body
    assert "soundbar setup" in body
    assert "Sent as-is:" in body
    assert "Sent with edits:" in body
    assert "Skip today:" in body
    assert "Not my customer" not in body  # no reassign link this run


def test_render_body_includes_reassign_when_link_present() -> None:
    links_with_reassign = build_action_links(
        secret=SECRET,
        control_address="outgrow-control@getlivewire.com",
        pulse_id="p1",
        suggested_rep="zack",
    )
    body = render_pulse_body(
        rep_first_name="Henry",
        briefing=_briefing(),
        draft_text="Hey Jane.",
        links=links_with_reassign,
    )
    assert "Not my customer:" in body


# ---- send_pulse / SMTP transport -------------------------------------------


def test_send_pulse_hands_message_to_transport() -> None:
    rep = RepProfile(rep_id="zack", email=ZACK_EMAIL, first_name="Zack", cc_review_remaining=5)
    pulse = build_pulse_email(
        pulse_id="p1",
        rep=rep,
        briefing=_briefing(),
        draft_text="Hey Jane — checking in.",
        links=_links(),  # type: ignore[arg-type]
        policy=_policy(),
    )
    captured: list[EmailMessage] = []
    send_pulse(
        pulse,
        sender_email="outgrow-control@getlivewire.com",
        smtp_send=captured.append,
    )
    assert len(captured) == 1
    msg = captured[0]
    assert msg["From"] == "outgrow-control@getlivewire.com"
    assert msg["To"] == ZACK_EMAIL
    assert msg["Cc"] == OWNER_EMAIL
    assert msg["Subject"] == "Outgrow pulse — Jane Smith"
    assert "Hey Jane — checking in." in msg.get_content()


def test_to_email_message_omits_cc_header_when_empty() -> None:
    rep = RepProfile(rep_id="zack", email=ZACK_EMAIL, first_name="Zack", cc_review_remaining=0)
    pulse = build_pulse_email(
        pulse_id="p1",
        rep=rep,
        briefing=_briefing(),
        draft_text="Hey",
        links=_links(),  # type: ignore[arg-type]
        policy=_policy(),
    )
    msg = to_email_message(pulse, sender_email="outgrow-control@getlivewire.com")
    assert msg["Cc"] is None
