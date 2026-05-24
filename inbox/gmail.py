"""Gmail API client for the inbox poller.

OAuth 2.0 refresh-token flow against a single user mailbox (the human
who receives the ``+outgrow`` action replies). Same architectural
shape as ``sync/qbo.py`` / ``sync/zoho_crm.py``: refresh-token →
short-lived access-token → API calls. Network edges sit at module
boundaries; the parser/processor consume pure dicts from here.

Three operations the poller needs:

* ``list_unprocessed_message_ids`` — search the inbox label for
  messages that don't yet carry the ``processed`` label
* ``get_message`` — fetch full message (headers + body) by id
* ``mark_processed`` — add the ``processed`` label (idempotency belt)

Tests use a ``FakeGmailClient`` (same pattern as the SmtpSender seam
in pulse/mailer.py); sockets are blocked at conftest so any accidental
live call raises ``NetworkBlockedError``.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from sync._http import urlopen_read_with_retry

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
HTTP_TIMEOUT_S = 30
PROCESSED_LABEL_NAME = "OutgrowProcessed"


@dataclass(frozen=True)
class GmailCreds:
    client_id: str
    client_secret: str
    refresh_token: str
    user: str  # the mailbox email — used for the API path and audit logs

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> GmailCreds:
        source = env if env is not None else dict(os.environ)
        required = (
            "OUTGROW_GMAIL_CLIENT_ID",
            "OUTGROW_GMAIL_CLIENT_SECRET",
            "OUTGROW_GMAIL_REFRESH_TOKEN",
            "OUTGROW_GMAIL_USER",
        )
        missing = [k for k in required if not source.get(k)]
        if missing:
            raise RuntimeError(
                "Missing Gmail OAuth credentials in environment: " + ", ".join(missing)
            )
        return cls(
            client_id=source["OUTGROW_GMAIL_CLIENT_ID"],
            client_secret=source["OUTGROW_GMAIL_CLIENT_SECRET"],
            refresh_token=source["OUTGROW_GMAIL_REFRESH_TOKEN"],
            user=source["OUTGROW_GMAIL_USER"],
        )


@dataclass(frozen=True)
class GmailMessage:
    """Minimal projection of a Gmail message into what the parser needs."""

    message_id: str  # Gmail's internal ID
    thread_id: str
    subject: str
    sender: str  # e.g. "Henry Clifford <henry@getlivewire.com>"
    to: str  # the addressed mailbox (used to verify +outgrow targeting)
    body_text: str  # plain-text body (HTML stripped if multipart/alternative)
    received_at: str  # ISO-8601 from the Date header (best-effort)


class GmailClient(Protocol):
    """Seam for injection. Real impl in ``LiveGmailClient``; tests fake it."""

    def list_unprocessed_message_ids(self, label_name: str) -> list[str]: ...
    def get_message(self, message_id: str) -> GmailMessage: ...
    def mark_processed(self, message_id: str) -> None: ...


# ---- live client -----------------------------------------------------------


def refresh_access_token(creds: GmailCreds) -> str:
    """Exchange the long-lived refresh token for a short-lived access token.

    Google refresh tokens are stable (no rotation on use, unlike Intuit),
    so no persistence callback is needed. Errors surface the response body
    for fast diagnosis — same pattern as ``sync/qbo.py``.
    """
    body = urllib.parse.urlencode(
        {
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        _, payload_bytes = urlopen_read_with_retry(req, timeout=HTTP_TIMEOUT_S)
        payload = json.loads(payload_bytes)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Gmail OAuth refresh failed: HTTP {e.code} {err_body}") from e
    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Gmail token refresh returned no access_token: {str(payload)[:200]}")
    return str(token)


def _decode_body(payload: dict[str, Any]) -> str:
    """Pull plain-text body out of a Gmail message payload.

    Gmail returns the body base64url-encoded inside ``payload.parts`` (for
    multipart) or ``payload.body`` (single-part). Prefers ``text/plain``
    when both ``text/plain`` and ``text/html`` are present — action replies
    are plain-text from the rep's mail app.
    """

    def walk(part: dict[str, Any]) -> str | None:
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            data = (part.get("body") or {}).get("data")
            if data:
                # Gmail uses base64url with no padding; standard b64decode
                # needs padding, urlsafe_b64decode handles both.
                return base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
        for sub in part.get("parts") or []:
            found = walk(sub)
            if found is not None:
                return found
        return None

    return walk(payload) or ""


def _header(payload: dict[str, Any], name: str) -> str:
    """Case-insensitive header lookup; returns '' when missing."""
    target = name.lower()
    for h in payload.get("headers") or []:
        if str(h.get("name", "")).lower() == target:
            return str(h.get("value", ""))
    return ""


@dataclass
class LiveGmailClient:
    """Real Gmail API client. Tests inject a fake conforming to ``GmailClient``."""

    creds: GmailCreds
    _access_token: str | None = None
    _processed_label_id: str | None = None

    def _token(self) -> str:
        # Refreshed once per process. Workflow runs are short-lived (~5 min)
        # so token expiry (1h) never bites. If we ever batch many calls
        # across hours, add expiry-aware caching.
        if self._access_token is None:
            self._access_token = refresh_access_token(self.creds)
        return self._access_token

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{GMAIL_BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            _, body = urlopen_read_with_retry(req, timeout=HTTP_TIMEOUT_S)
            return json.loads(body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Gmail GET {path} failed: HTTP {e.code} {err_body}") from e

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{GMAIL_BASE}{path}"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            _, resp = urlopen_read_with_retry(req, timeout=HTTP_TIMEOUT_S)
            return json.loads(resp) if resp else {}
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Gmail POST {path} failed: HTTP {e.code} {err_body}") from e

    def _resolve_label_id(self, name: str) -> str:
        """Gmail's API takes label IDs, not display names — translate once."""
        payload = self._get(f"/users/{self.creds.user}/labels")
        for lab in payload.get("labels") or []:
            if lab.get("name") == name:
                return str(lab["id"])
        raise RuntimeError(f"Gmail label not found: {name!r} (case-sensitive)")

    def _ensure_processed_label(self) -> str:
        """Create the processed-tracking label if absent; return its ID.

        Done lazily so first-run setup is painless — no manual label
        creation in Gmail required.
        """
        if self._processed_label_id is not None:
            return self._processed_label_id
        payload = self._get(f"/users/{self.creds.user}/labels")
        for lab in payload.get("labels") or []:
            if lab.get("name") == PROCESSED_LABEL_NAME:
                self._processed_label_id = str(lab["id"])
                return self._processed_label_id
        created = self._post(
            f"/users/{self.creds.user}/labels",
            {"name": PROCESSED_LABEL_NAME, "labelListVisibility": "labelShow"},
        )
        self._processed_label_id = str(created["id"])
        return self._processed_label_id

    def list_unprocessed_message_ids(self, label_name: str) -> list[str]:
        """Return message IDs in ``label_name`` that don't yet carry
        ``OutgrowProcessed``. Paginated; returns all matches at once."""
        inbox_label_id = self._resolve_label_id(label_name)
        processed_label_id = self._ensure_processed_label()
        # Gmail search query: in our inbox label, NOT in the processed label.
        # Use label IDs directly to avoid display-name escaping issues.
        ids: list[str] = []
        page_token: str | None = None
        while True:
            params = {
                "labelIds": inbox_label_id,
                "q": f"-label:{PROCESSED_LABEL_NAME}",
                "maxResults": "100",
            }
            if page_token:
                params["pageToken"] = page_token
            payload = self._get(f"/users/{self.creds.user}/messages", params)
            for m in payload.get("messages") or []:
                ids.append(str(m["id"]))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
        # processed_label_id captured by closure — silence the unused warning
        # by referencing it; the variable's real role is "ensure it exists".
        _ = processed_label_id
        return ids

    def get_message(self, message_id: str) -> GmailMessage:
        payload = self._get(
            f"/users/{self.creds.user}/messages/{message_id}",
            {"format": "full"},
        )
        headers = payload.get("payload") or {}
        return GmailMessage(
            message_id=str(payload.get("id", message_id)),
            thread_id=str(payload.get("threadId", "")),
            subject=_header(headers, "Subject"),
            sender=_header(headers, "From"),
            to=_header(headers, "To"),
            body_text=_decode_body(headers),
            received_at=_header(headers, "Date"),
        )

    def mark_processed(self, message_id: str) -> None:
        label_id = self._ensure_processed_label()
        self._post(
            f"/users/{self.creds.user}/messages/{message_id}/modify",
            {"addLabelIds": [label_id]},
        )
