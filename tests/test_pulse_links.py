"""Signed mailto-action-link tests."""

from __future__ import annotations

import urllib.parse

import pytest

from pulse.links import (
    ACTION_TOKEN_HEX_LEN,
    build_mailto,
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
