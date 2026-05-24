"""Tests for ``inbox/gmail.py`` — focused on parsing/decoding bits that
would silently corrupt action data if broken.

The LiveGmailClient's HTTP edges are tested via monkeypatched
``urllib.request.urlopen`` (sockets are blocked at conftest, so a real
network call would raise ``NetworkBlockedError``). We don't try to
exhaustively exercise every Gmail API path — focus is on the shape
contract with the parser/processor downstream.
"""

from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.request

import pytest

from inbox.gmail import (
    GmailCreds,
    LiveGmailClient,
    _decode_body,  # type: ignore[attr-defined]  -- testing internal helper
    _header,  # type: ignore[attr-defined]
    refresh_access_token,
)


def _b64url(text: str) -> str:
    """Encode `text` the way Gmail returns body data."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _fake_response(body_bytes: bytes, status: int = 200) -> object:
    class _Resp:
        def __init__(self) -> None:
            self.status = status
            self._b = io.BytesIO(body_bytes)

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self) -> bytes:
            return self._b.read()

    return _Resp()


# ---- GmailCreds.from_env ---------------------------------------------------


def test_creds_from_env_requires_all_four_vars() -> None:
    with pytest.raises(RuntimeError, match="Missing Gmail OAuth credentials"):
        GmailCreds.from_env({"OUTGROW_GMAIL_CLIENT_ID": "x"})


def test_creds_from_env_returns_creds_when_all_set() -> None:
    creds = GmailCreds.from_env(
        {
            "OUTGROW_GMAIL_CLIENT_ID": "cid",
            "OUTGROW_GMAIL_CLIENT_SECRET": "csec",
            "OUTGROW_GMAIL_REFRESH_TOKEN": "rt",
            "OUTGROW_GMAIL_USER": "henry@getlivewire.com",
        }
    )
    assert creds.client_id == "cid"
    assert creds.client_secret == "csec"
    assert creds.refresh_token == "rt"
    assert creds.user == "henry@getlivewire.com"


# ---- refresh_access_token --------------------------------------------------


def test_refresh_access_token_returns_token_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(*_args: object, **_kwargs: object) -> object:
        return _fake_response(b'{"access_token": "ya29.test", "expires_in": 3599}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    creds = GmailCreds(client_id="c", client_secret="s", refresh_token="r", user="u@x")
    assert refresh_access_token(creds) == "ya29.test"


def test_refresh_access_token_raises_on_missing_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(*_args: object, **_kwargs: object) -> object:
        return _fake_response(b'{"error": "invalid_grant"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    creds = GmailCreds(client_id="c", client_secret="s", refresh_token="r", user="u@x")
    with pytest.raises(RuntimeError, match="no access_token"):
        refresh_access_token(creds)


def test_refresh_access_token_surfaces_http_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    err = urllib.error.HTTPError(
        url="https://oauth2.googleapis.com/token",
        code=400,
        msg="Bad Request",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error":"invalid_grant","error_description":"Token revoked"}'),
    )

    def fake_urlopen(*_args: object, **_kwargs: object) -> object:
        raise err

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    creds = GmailCreds(client_id="c", client_secret="s", refresh_token="r", user="u@x")
    with pytest.raises(RuntimeError, match="HTTP 400"):
        refresh_access_token(creds)


# ---- _decode_body ----------------------------------------------------------


def test_decode_body_extracts_text_plain_from_single_part() -> None:
    payload = {
        "mimeType": "text/plain",
        "body": {"data": _b64url("hello world")},
    }
    assert _decode_body(payload) == "hello world"


def test_decode_body_traverses_multipart_alternative() -> None:
    """multipart/alternative with text/plain + text/html — must prefer plain."""
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url("plain version")}},
            {"mimeType": "text/html", "body": {"data": _b64url("<p>html version</p>")}},
        ],
    }
    assert _decode_body(payload) == "plain version"


def test_decode_body_returns_empty_when_no_text_plain() -> None:
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b64url("<p>only html</p>")},
    }
    assert _decode_body(payload) == ""


def test_decode_body_handles_nested_parts() -> None:
    """multipart/mixed wrapping multipart/alternative — Gmail does this when
    a reply has an attachment alongside the text body."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64url("the message")}},
                ],
            },
            {"mimeType": "application/pdf", "body": {"attachmentId": "att1"}},
        ],
    }
    assert _decode_body(payload) == "the message"


# ---- _header ---------------------------------------------------------------


def test_header_is_case_insensitive() -> None:
    payload = {"headers": [{"name": "Subject", "value": "SENT pulse_x t"}]}
    assert _header(payload, "subject") == "SENT pulse_x t"
    assert _header(payload, "SUBJECT") == "SENT pulse_x t"


def test_header_returns_empty_string_when_missing() -> None:
    assert _header({"headers": []}, "Subject") == ""


# ---- LiveGmailClient ------------------------------------------------------


def test_live_client_get_message_projects_payload_into_gmail_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end shape contract — what get_message returns is what the
    parser/processor consume, so we'd catch a key-name drift here.
    """
    # urlopen will be called for: (1) token refresh, (2) the messages.get.
    responses = iter(
        [
            _fake_response(b'{"access_token": "tok"}'),
            _fake_response(
                json.dumps(
                    {
                        "id": "abc",
                        "threadId": "tid",
                        "payload": {
                            "headers": [
                                {"name": "Subject", "value": "SENT pulse_2026-05-22-henry tok"},
                                {"name": "From", "value": "Henry <henry@getlivewire.com>"},
                                {"name": "To", "value": "henry+outgrow@getlivewire.com"},
                                {"name": "Date", "value": "Fri, 22 May 2026 10:55:00 -0400"},
                            ],
                            "mimeType": "text/plain",
                            "body": {"data": _b64url("body content")},
                        },
                    }
                ).encode()
            ),
        ]
    )

    def fake_urlopen(*_args: object, **_kwargs: object) -> object:
        return next(responses)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = LiveGmailClient(
        creds=GmailCreds(client_id="c", client_secret="s", refresh_token="r", user="henry@x")
    )
    msg = client.get_message("abc")
    assert msg.message_id == "abc"
    assert msg.subject == "SENT pulse_2026-05-22-henry tok"
    assert msg.sender == "Henry <henry@getlivewire.com>"
    assert msg.to == "henry+outgrow@getlivewire.com"
    assert msg.body_text == "body content"
    assert msg.received_at == "Fri, 22 May 2026 10:55:00 -0400"
