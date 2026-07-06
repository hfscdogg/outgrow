"""Signed mailto-action-link tests."""

from __future__ import annotations

import urllib.parse

import pytest

from pulse.links import (
    ACTION_TOKEN_HEX_LEN,
    BCC_TOKEN_HEX_LEN,
    build_customer_compose_mailto,
    build_customer_sms_uri,
    build_mailto,
    build_sent_bcc_address,
    sign_action,
    verify_action,
)

SECRET = b"\x00" * 32  # deterministic test secret


def test_sign_action_returns_32_hex_chars() -> None:
    token = sign_action(SECRET, "sent", "p1")
    assert len(token) == ACTION_TOKEN_HEX_LEN
    assert all(c in "0123456789abcdef" for c in token)


def test_sign_action_is_deterministic() -> None:
    assert sign_action(SECRET, "sent", "p1") == sign_action(SECRET, "sent", "p1")


def test_sign_action_differs_per_action() -> None:
    assert sign_action(SECRET, "sent", "p1") != sign_action(SECRET, "edited", "p1")


def test_sign_action_differs_per_pulse_id() -> None:
    assert sign_action(SECRET, "sent", "p1") != sign_action(SECRET, "sent", "p2")


def test_sign_action_differs_per_secret() -> None:
    a = sign_action(SECRET, "sent", "p1")
    b = sign_action(b"\x01" * 32, "sent", "p1")
    assert a != b


def test_sign_action_rejects_unknown_action() -> None:
    with pytest.raises(ValueError, match="unknown action"):
        sign_action(SECRET, "PWN", "p1")


def test_verify_action_accepts_correct_token() -> None:
    token = sign_action(SECRET, "sent", "p1")
    assert verify_action(SECRET, "sent", "p1", token) is True


def test_verify_action_rejects_wrong_token() -> None:
    assert verify_action(SECRET, "sent", "p1", "0" * ACTION_TOKEN_HEX_LEN) is False


def test_verify_action_rejects_token_with_wrong_action_label() -> None:
    edited_token = sign_action(SECRET, "edited", "p1")
    assert verify_action(SECRET, "sent", "p1", edited_token) is False


def test_verify_action_rejects_token_with_wrong_pulse_id() -> None:
    other_token = sign_action(SECRET, "sent", "p2")
    assert verify_action(SECRET, "sent", "p1", other_token) is False


def test_verify_action_rejects_token_signed_with_different_secret() -> None:
    foreign = sign_action(b"\x01" * 32, "sent", "p1")
    assert verify_action(SECRET, "sent", "p1", foreign) is False


def test_build_mailto_subject_carries_action_pulse_token() -> None:
    token = "a" * ACTION_TOKEN_HEX_LEN
    url = build_mailto(
        control_address="outgrow-control@getlivewire.com",
        action="sent",
        pulse_id="42",
        token=token,
    )
    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "mailto"
    assert parsed.path == "outgrow-control@getlivewire.com"
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["subject"] == [f"SENT pulse_42 {token}"]
    assert "body" not in qs


def test_build_mailto_body_url_encoded_when_present() -> None:
    url = build_mailto(
        control_address="control@example.com",
        action="edited",
        pulse_id="42",
        token="b" * ACTION_TOKEN_HEX_LEN,
        body="Paste final text below:\n\n",
    )
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["body"] == ["Paste final text below:\n\n"]


def test_build_mailto_url_encodes_spaces_in_subject() -> None:
    url = build_mailto(
        control_address="control@example.com",
        action="skipped",
        pulse_id="42",
        token="c" * ACTION_TOKEN_HEX_LEN,
    )
    # urlencode uses '+' for spaces by default in query strings
    assert "SENT" not in url
    assert "+pulse_42+" in url or "%20pulse_42%20" in url


# ---- build_customer_sms_uri --------------------------------------------------


def test_sms_uri_targets_number_with_sms_scheme() -> None:
    uri = build_customer_sms_uri(to_number="+18043431212", body="Hey Jane")
    assert uri.startswith("sms:+18043431212?body=")


def test_sms_uri_encodes_spaces_as_percent20_not_plus() -> None:
    """iOS Messages renders '+' literally, so the body must use %20."""
    uri = build_customer_sms_uri(to_number="+18043431212", body="Hey Jane, how are you?")
    assert "%20" in uri
    body_part = uri.split("body=", 1)[1]
    assert "+" not in body_part
    assert urllib.parse.unquote(body_part) == "Hey Jane, how are you?"


def test_sms_uri_carries_no_hmac_token() -> None:
    uri = build_customer_sms_uri(to_number="+18043431212", body="Hey")
    assert "pulse_" not in uri
    assert "token" not in uri


# ---- build_sent_bcc_address --------------------------------------------------


def test_bcc_address_shape_and_token_length() -> None:
    addr = build_sent_bcc_address(
        control_address="henry+outgrow@getlivewire.com",
        pulse_id="2026-06-29-zack",
        secret=SECRET,
    )
    local, _, domain = addr.partition("@")
    assert domain == "getlivewire.com"
    assert local.startswith("henry+outgrow-sent-2026-06-29-zack-")
    token = local.rsplit("-", 1)[1]
    assert len(token) == BCC_TOKEN_HEX_LEN
    assert token == sign_action(SECRET, "sent", "2026-06-29-zack")[:BCC_TOKEN_HEX_LEN]


def test_bcc_address_strips_existing_plus_tag() -> None:
    """henry+outgrow@... must derive from 'henry', not double-tag."""
    addr = build_sent_bcc_address(
        control_address="henry+outgrow@getlivewire.com", pulse_id="p1", secret=SECRET
    )
    assert addr.count("+") == 1


def test_bcc_address_works_without_existing_plus_tag() -> None:
    addr = build_sent_bcc_address(
        control_address="outgrow-control@getlivewire.com", pulse_id="p1", secret=SECRET
    )
    assert addr.startswith("outgrow-control+outgrow-sent-p1-")


def test_bcc_address_local_part_stays_under_rfc_limit() -> None:
    """RFC 5321 caps the local part at 64 octets; a real-shaped pulse_id
    must fit with room to spare."""
    addr = build_sent_bcc_address(
        control_address="henry+outgrow@getlivewire.com",
        pulse_id="2026-12-31-henry",
        secret=SECRET,
    )
    local = addr.partition("@")[0]
    assert len(local) <= 64


def test_bcc_address_differs_per_pulse_and_secret() -> None:
    a = build_sent_bcc_address(control_address="h@x.com", pulse_id="2026-06-29-zack", secret=SECRET)
    b = build_sent_bcc_address(control_address="h@x.com", pulse_id="2026-06-30-zack", secret=SECRET)
    c = build_sent_bcc_address(
        control_address="h@x.com", pulse_id="2026-06-29-zack", secret=b"\x01" * 32
    )
    assert len({a, b, c}) == 3


# ---- build_customer_compose_mailto bcc param ---------------------------------


def test_compose_mailto_carries_bcc_with_encoded_plus() -> None:
    url = build_customer_compose_mailto(
        to_address="jane@example.com",
        body="Hey Jane",
        subject="Checking in",
        bcc="henry+outgrow-sent-p1-abcd@getlivewire.com",
    )
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert qs["bcc"] == ["henry+outgrow-sent-p1-abcd@getlivewire.com"]
    # The raw URL must percent-encode the '+' so mail clients don't read a space.
    assert "henry%2Boutgrow-sent" in url


def test_compose_mailto_omits_bcc_when_absent() -> None:
    url = build_customer_compose_mailto(to_address="jane@example.com", body="Hey")
    assert "bcc" not in url
