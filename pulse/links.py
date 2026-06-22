"""Signed ``mailto:`` action links for the daily pulse.

Each pulse email contains 3-4 ``mailto:`` URLs the rep taps to log what
they did with the suggested text. The URL pre-fills the rep's mail app
with a reply to the control inbox. Tapping the link opens the mail
app, the rep taps Send, and the inbox poller (Phase 2) parses the
subject, verifies the HMAC token, and writes a Zoho Call Activity.

Tokens are HMAC-SHA256 of ``f"{action}|{pulse_id}"`` truncated to 32
hex chars (16 bytes of entropy). That's tight enough that the URL fits
in an iOS Mail subject line and still well above guess-the-token-by-
brute-force resistance for the 5-minute kill window the engine uses.

Pure functions only — no environment reads, no I/O. The cron-runner
loads the secret from ``OUTGROW_HMAC_SECRET`` and passes it in.
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse

ACTION_TOKEN_HEX_LEN = 32

ACTION_SENT = "sent"
ACTION_EDITED = "edited"
ACTION_SKIPPED = "skipped"
ACTION_REASSIGN = "reassign"

KNOWN_ACTIONS: frozenset[str] = frozenset(
    {ACTION_SENT, ACTION_EDITED, ACTION_SKIPPED, ACTION_REASSIGN}
)


def sign_action(secret: bytes, action: str, pulse_id: str) -> str:
    """Return an HMAC-SHA256 hex token for ``(action, pulse_id)``."""
    if action not in KNOWN_ACTIONS:
        raise ValueError(f"unknown action {action!r}; expected one of {sorted(KNOWN_ACTIONS)}")
    msg = f"{action}|{pulse_id}".encode()
    digest = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return digest[:ACTION_TOKEN_HEX_LEN]


def verify_action(secret: bytes, action: str, pulse_id: str, token: str) -> bool:
    """Constant-time verify that ``token`` was issued for ``(action, pulse_id)``."""
    expected = sign_action(secret, action, pulse_id)
    return hmac.compare_digest(expected, token)


def build_mailto(
    *,
    control_address: str,
    action: str,
    pulse_id: str,
    token: str,
    body: str | None = None,
) -> str:
    """Build a ``mailto:`` URL that pre-fills a control-inbox reply.

    Subject is ``"<ACTION> pulse_<id> <token>"`` so the inbox poller can
    parse the action + verify the token. ``body`` is the optional pre-
    filled message body (e.g., "Paste final text below:" for EDITED).
    """
    subject = f"{action.upper()} pulse_{pulse_id} {token}"
    params = [("subject", subject)]
    if body is not None:
        params.append(("body", body))
    return f"mailto:{control_address}?{urllib.parse.urlencode(params)}"


def build_customer_compose_mailto(
    *,
    to_address: str,
    body: str,
    subject: str | None = None,
) -> str:
    """Build a ``mailto:<customer>`` URL that pre-fills an outbound draft.

    This is the "send draft to customer" link — it opens a fresh email TO
    the customer (NOT the control inbox) with the AI draft already in the
    body. Rep tweaks if needed and hits send. No HMAC token: this email
    never re-enters the inbox poller, it goes straight to the customer.

    ``subject`` is optional; some reps prefer to compose their own.
    """
    params: list[tuple[str, str]] = []
    if subject is not None:
        params.append(("subject", subject))
    params.append(("body", body))
    return f"mailto:{to_address}?{urllib.parse.urlencode(params)}"
