"""Daily pulse mailer.

Builds the morning email each rep gets at 07:00 ET (briefing + draft +
signed mailto action links), enforces the recipient allowlist as a
runtime guard (CI already enforces it on YAML), and CC's the owner on
Zack's first ``cc_review_remaining`` pulses for voice-quality review.

Network transport is **injected** so tests use a fake; sockets are
blocked at conftest import time, so any accidental live SMTP would
raise ``NetworkBlockedError``. The cron-runner instantiates the real
``smtplib.SMTP`` and passes a closure.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import yaml

from pulse.links import (
    ACTION_EDITED,
    ACTION_REASSIGN,
    ACTION_SENT,
    ACTION_SKIPPED,
    build_mailto,
    sign_action,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST_PATH = REPO_ROOT / "config" / "recipient_allowlist.yaml"

EDITED_BODY_TEMPLATE = "Paste the final text you sent below:\n\n"
SKIPPED_BODY_TEMPLATE = "Reason (optional):\n\n"
REASSIGN_BODY_TEMPLATE = "Reassign to: {suggested_rep}\n\n"

SmtpSender = Callable[[EmailMessage], None]


@dataclass(frozen=True)
class RepProfile:
    """Subset of a rep's YAML the mailer needs at send time."""

    rep_id: str
    email: str
    first_name: str
    cc_review_remaining: int = 0


@dataclass(frozen=True)
class RecipientPolicy:
    """Loaded from ``config/recipient_allowlist.yaml``.

    ``owner_email`` is the address that gets CC'd on review-window
    pulses; it must itself be on the allowlist. Validated at load time
    so a misconfigured deploy fails fast, not at 07:00 send time.
    """

    allowed: frozenset[str]
    owner_email: str


@dataclass(frozen=True)
class CustomerBriefing:
    name: str
    rows: tuple[tuple[str, str], ...]  # ordered key/value pairs to render


@dataclass(frozen=True)
class ActionLinks:
    sent_as_is: str
    sent_with_edits: str
    skip_today: str
    reassign: str | None = None


@dataclass(frozen=True)
class PulseEmail:
    pulse_id: str
    rep_email: str
    cc: tuple[str, ...]
    subject: str
    body: str
    cc_was_review: bool = field(default=False)


def load_recipient_policy(
    *,
    owner_email: str,
    path: Path = DEFAULT_ALLOWLIST_PATH,
) -> RecipientPolicy:
    """Read the allowlist YAML and validate that ``owner_email`` is on it."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = payload.get("allowed_recipients") or []
    allowed = frozenset(str(addr).strip().lower() for addr in raw)
    if owner_email.lower() not in allowed:
        raise RuntimeError(
            f"owner_email {owner_email!r} is not in {path}; refusing to load policy."
        )
    return RecipientPolicy(allowed=allowed, owner_email=owner_email)


def assert_recipients_allowed(addresses: Iterable[str], policy: RecipientPolicy) -> None:
    """Raise if any address is not on the allowlist."""
    for addr in addresses:
        if addr.lower() not in policy.allowed:
            raise RuntimeError(
                f"recipient {addr!r} is not in the recipient allowlist; refusing to send."
            )


def build_action_links(
    *,
    secret: bytes,
    control_address: str,
    pulse_id: str,
    suggested_rep: str | None = None,
) -> ActionLinks:
    """Generate the three (or four) signed ``mailto:`` action links."""

    def link(action: str, body: str | None = None) -> str:
        token = sign_action(secret, action, pulse_id)
        return build_mailto(
            control_address=control_address,
            action=action,
            pulse_id=pulse_id,
            token=token,
            body=body,
        )

    reassign = (
        link(ACTION_REASSIGN, body=REASSIGN_BODY_TEMPLATE.format(suggested_rep=suggested_rep))
        if suggested_rep
        else None
    )
    return ActionLinks(
        sent_as_is=link(ACTION_SENT),
        sent_with_edits=link(ACTION_EDITED, body=EDITED_BODY_TEMPLATE),
        skip_today=link(ACTION_SKIPPED, body=SKIPPED_BODY_TEMPLATE),
        reassign=reassign,
    )


def render_pulse_body(
    *,
    rep_first_name: str,
    briefing: CustomerBriefing,
    draft_text: str,
    links: ActionLinks,
) -> str:
    """Plain-text body. Markdown rendering is a Phase 2+ enhancement."""
    rows: list[str] = [f"Morning {rep_first_name},", "", f"Today's pulse: {briefing.name}", ""]
    for key, value in briefing.rows:
        rows.append(f"  {key}: {value}")
    rows.extend(["", "Suggested text (in your voice):", "", draft_text, ""])
    rows.append("Tap one when you're done — your mail app will pre-fill the reply:")
    rows.append(f"  Sent as-is:        {links.sent_as_is}")
    rows.append(f"  Sent with edits:   {links.sent_with_edits}")
    rows.append(f"  Skip today:        {links.skip_today}")
    if links.reassign:
        rows.append(f"  Not my customer:   {links.reassign}")
    return "\n".join(rows)


def should_cc_owner(rep: RepProfile) -> bool:
    """``True`` while the rep is still in their review window."""
    return rep.cc_review_remaining > 0


def build_pulse_email(
    *,
    pulse_id: str,
    rep: RepProfile,
    briefing: CustomerBriefing,
    draft_text: str,
    links: ActionLinks,
    policy: RecipientPolicy,
) -> PulseEmail:
    """Assemble the rep's pulse and validate every recipient against the allowlist."""
    cc_owner = should_cc_owner(rep) and rep.email.lower() != policy.owner_email.lower()
    cc = (policy.owner_email,) if cc_owner else ()
    assert_recipients_allowed([rep.email, *cc], policy)
    body = render_pulse_body(
        rep_first_name=rep.first_name,
        briefing=briefing,
        draft_text=draft_text,
        links=links,
    )
    subject = f"Outgrow pulse — {briefing.name}"
    return PulseEmail(
        pulse_id=pulse_id,
        rep_email=rep.email,
        cc=cc,
        subject=subject,
        body=body,
        cc_was_review=cc_owner,
    )


def to_email_message(pulse: PulseEmail, *, sender_email: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender_email
    msg["To"] = pulse.rep_email
    if pulse.cc:
        msg["Cc"] = ", ".join(pulse.cc)
    msg["Subject"] = pulse.subject
    msg.set_content(pulse.body)
    return msg


def send_pulse(
    pulse: PulseEmail,
    *,
    sender_email: str,
    smtp_send: SmtpSender,
) -> EmailMessage:
    """Hand a fully-built ``EmailMessage`` to the injected SMTP transport."""
    msg = to_email_message(pulse, sender_email=sender_email)
    smtp_send(msg)
    return msg


def make_smtp_sender(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
) -> SmtpSender:
    """Build a real ``SmtpSender`` backed by ``smtplib.SMTP`` + STARTTLS.

    Used by ``scripts/daily_pulse.py`` when ``--write`` is active. Tests
    never reach here — they inject a fake closure directly. STARTTLS is
    required for Google Workspace, Zoho Mail, and Microsoft 365 SMTP
    relays on port 587; if a provider needs implicit TLS (port 465),
    swap to ``smtplib.SMTP_SSL`` — but the defaults are tuned for the
    STARTTLS path because that's what every major hosted relay supports.
    """

    def send(msg: EmailMessage) -> None:
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)

    return send
