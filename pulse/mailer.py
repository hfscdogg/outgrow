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

import html
import smtplib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import yaml

from pulse.links import (
    ACTION_EDITED,
    ACTION_REASSIGN,
    ACTION_SENT,
    ACTION_SKIPPED,
    build_customer_compose_mailto,
    build_customer_sms_uri,
    build_mailto,
    build_sent_bcc_address,
    sign_action,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWLIST_PATH = REPO_ROOT / "config" / "recipient_allowlist.yaml"

SKIPPED_BODY_TEMPLATE = "Reason (optional):\n\n"
REASSIGN_BODY_TEMPLATE = "Reassign to: {suggested_rep}\n\n"

# Subject pre-filled on the compose-to-customer email. Static and casual —
# reads like a personal note from the rep, never goes stale.
COMPOSE_SUBJECT = "Checking in"


# Instruction header of the EDITED reply body. Exported because reps
# sometimes send it back verbatim above their rewrite, so downstream
# consumers of edited_text (pipeline.suppressions.recent_edit_examples)
# need to strip it before treating the text as the rep's own words.
EDITED_COMPOSE_BOILERPLATE = (
    "Edit below to match what you actually sent the customer, then send. "
    "(This goes to the control mailbox to record your action — NOT to the customer.)"
)


def _build_edited_body(draft_text: str) -> str:
    """EDITED reply body — pre-fills the draft so the rep can edit in place
    and send to record the actual text that went to the customer (vs. an
    empty "paste here" prompt that forced a copy/paste round trip).
    """
    return (
        "Edit below to match what you actually sent the customer, then send.\n"
        "(This goes to the control mailbox to record your action — NOT to the customer.)\n\n"
        f"{draft_text}\n"
    )


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
    # Humanized "X ago" string for the email subhead — e.g. "4y 3mo ago".
    # Computed upstream from ``last_purchase_at``; absent when the customer
    # has no recorded purchase. Renderers fall back to a name-only headline.
    dormancy_label: str | None = None
    # Customer's email + first name, when Zoho has them. Powers the one-click
    # "Send draft to customer" mailto: button. When email is absent the
    # button is omitted from the rendered pulse.
    email: str | None = None
    first_name: str | None = None
    # Customer's mobile in E.164 (normalized from Zoho's Mobile field
    # upstream). Powers the "Text customer" sms: button; omitted when Zoho
    # has no mobile or the number can't be confidently normalized.
    mobile_e164: str | None = None


@dataclass(frozen=True)
class ActionLinks:
    sent_as_is: str
    sent_with_edits: str
    skip_today: str
    reassign: str | None = None
    # mailto: opening a fresh email TO the customer, pre-filled with the AI
    # draft. ``None`` when the customer has no email on file (Zoho gap).
    compose_to_customer: str | None = None
    # sms: opening the rep's messaging app with the draft pre-filled.
    # ``None`` when the customer has no normalizable mobile number.
    text_to_customer: str | None = None


@dataclass(frozen=True)
class PulseEmail:
    pulse_id: str
    rep_email: str
    cc: tuple[str, ...]
    subject: str
    body: str
    html_body: str | None = None
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
    draft_text: str,
    suggested_rep: str | None = None,
    customer_email: str | None = None,
    customer_mobile: str | None = None,
) -> ActionLinks:
    """Generate the signed ``mailto:`` action links plus the optional
    one-click compose/text-to-customer links.

    ``draft_text`` is embedded into the EDITED body so the rep can edit
    in place rather than copy/paste from the pulse. ``customer_email``
    powers the "Send to customer" button — pre-filled subject, draft body,
    and a token-bearing BCC back to the control mailbox so the inbox
    poller auto-logs the send (no second tap). ``customer_mobile`` (E.164)
    powers the "Text customer" button; SMS has no BCC, so texts still need
    a manual action tap to record.
    """

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
    compose_to_customer = (
        build_customer_compose_mailto(
            to_address=customer_email,
            body=draft_text,
            subject=COMPOSE_SUBJECT,
            bcc=build_sent_bcc_address(
                control_address=control_address, pulse_id=pulse_id, secret=secret
            ),
        )
        if customer_email
        else None
    )
    text_to_customer = (
        build_customer_sms_uri(to_number=customer_mobile, body=draft_text)
        if customer_mobile
        else None
    )
    return ActionLinks(
        sent_as_is=link(ACTION_SENT),
        sent_with_edits=link(ACTION_EDITED, body=_build_edited_body(draft_text)),
        skip_today=link(ACTION_SKIPPED, body=SKIPPED_BODY_TEMPLATE),
        reassign=reassign,
        compose_to_customer=compose_to_customer,
        text_to_customer=text_to_customer,
    )


def render_pulse_body(
    *,
    rep_first_name: str,
    briefing: CustomerBriefing,
    draft_text: str,
    links: ActionLinks,
) -> str:
    """Plain-text fallback for mail clients that don't render HTML."""
    rows: list[str] = [f"Morning {rep_first_name},", "", f"Today's pulse: {briefing.name}"]
    if briefing.dormancy_label:
        rows.append(f"Last touch: {briefing.dormancy_label}")
    rows.append("")
    for key, value in briefing.rows:
        rows.append(f"  {key}: {value}")
    rows.extend(["", "Suggested text (in your voice):", "", draft_text, ""])
    if links.compose_to_customer or links.text_to_customer:
        rows.append("One-click compose (pre-fills a message TO the customer):")
        if links.compose_to_customer:
            rows.append(f"  Send to customer:  {links.compose_to_customer}")
        if links.text_to_customer:
            rows.append(f"  Text customer:     {links.text_to_customer}")
        rows.append("")
    rows.append("Then log what you did — these go to the control mailbox:")
    rows.append(f"  Sent as-is:        {links.sent_as_is}")
    rows.append(f"  Sent with edits:   {links.sent_with_edits}")
    rows.append(f"  Skip today:        {links.skip_today}")
    if links.reassign:
        rows.append(f"  Not my customer:   {links.reassign}")
    return "\n".join(rows)


# Inline-only CSS (Gmail strips <style> blocks). Color palette + serif
# headline follow docs/design/pulse_email_mockup.png; tested in Gmail web,
# Apple Mail, and Outlook 365 web. Fonts cascade: pretty serif first,
# robust system serif as fallback (Outlook always lands on the latter).
_SERIF_STACK = "'Cormorant Garamond', 'Playfair Display', Georgia, 'Times New Roman', serif"
_SANS_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
_COLOR_BG = "#000000"
_COLOR_CARD = "#f7f3ec"
_COLOR_INK = "#1a1a1a"
_COLOR_MUTED = "#6b6b6b"
_COLOR_ACCENT = "#d97a3a"
_COLOR_WHITE = "#ffffff"


def _format_header_date(today: date) -> str:
    """Render `today` as the email header date, e.g. ``Tuesday``."""
    return today.strftime("%A")


def _action_button(
    href: str,
    label: str,
    *,
    primary: bool,
) -> str:
    """Inline-styled <a> rendered as a button. ``primary`` flips fill+border."""
    if primary:
        bg, color, border = _COLOR_ACCENT, _COLOR_WHITE, _COLOR_ACCENT
    else:
        bg, color, border = _COLOR_WHITE, _COLOR_INK, "#d4cfc4"
    style = "; ".join(
        [
            "display:inline-block",
            f"background:{bg}",
            f"color:{color}",
            f"border:1px solid {border}",
            "padding:14px 20px",
            "text-decoration:none",
            f"font-family:{_SANS_STACK}",
            "font-size:13px",
            "font-weight:600",
            "letter-spacing:0.12em",
            "text-transform:uppercase",
            "border-radius:2px",
            "min-width:140px",
            "text-align:center",
        ]
    )
    return f'<a href="{html.escape(href, quote=True)}" style="{style}">{html.escape(label)}</a>'


def render_pulse_html(
    *,
    rep_first_name: str,
    briefing: CustomerBriefing,
    draft_text: str,
    links: ActionLinks,
    today: date,
) -> str:
    """HTML pulse body. Plain-text alternative is ``render_pulse_body``.

    All styling is inline so Gmail (which strips <style> blocks in some
    contexts) renders consistently. The layout uses tables for cross-
    client compatibility — Outlook still doesn't fully support modern
    block layout.
    """
    rep_name_safe = html.escape(rep_first_name)
    customer_safe = html.escape(briefing.name)
    date_label = html.escape(_format_header_date(today))
    subhead = (
        f"Last touch: {html.escape(briefing.dormancy_label)}" if briefing.dormancy_label else ""
    )

    row_html = "".join(
        f"<tr>"
        f'<td style="padding:14px 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:12px;letter-spacing:0.14em;text-transform:uppercase;border-bottom:1px dashed #d4cfc4;">{html.escape(key)}</td>'
        f'<td style="padding:14px 0;color:{_COLOR_INK};font-family:{_SANS_STACK};font-size:15px;text-align:right;border-bottom:1px dashed #d4cfc4;">{html.escape(value)}</td>'
        f"</tr>"
        for key, value in briefing.rows
    )

    # "Send to customer" / "Text customer" are the primary CTAs when we have
    # the customer's email / mobile: one click, pre-filled outbound draft,
    # edit, send. The control-mailbox action buttons sit below for logging.
    compose_buttons: list[str] = []
    if links.compose_to_customer:
        compose_buttons.append(
            _action_button(links.compose_to_customer, "Send to customer", primary=True)
        )
    if links.text_to_customer:
        compose_buttons.append(
            _action_button(links.text_to_customer, "Text customer", primary=True)
        )
    compose_button_html = (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 16px 0;">'
        "<tr>"
        + "".join(f'<td style="padding:0 6px 0 0;">{btn}</td>' for btn in compose_buttons)
        + "</tr></table>"
        if compose_buttons
        else ""
    )

    # When either compose button is present it's the primary CTA; downgrade
    # "Sent as-is" to secondary so the visual hierarchy reads top-to-bottom
    # (one-click outbound first, then logging buttons).
    sent_as_is_primary = not compose_buttons
    log_buttons = [
        _action_button(links.sent_as_is, "Sent as-is", primary=sent_as_is_primary),
        _action_button(links.sent_with_edits, "Sent w/ edits", primary=False),
        _action_button(links.skip_today, "Skip today", primary=False),
    ]
    if links.reassign:
        log_buttons.append(_action_button(links.reassign, "Not my customer", primary=False))
    buttons_html = "".join(f'<td style="padding:6px;">{btn}</td>' for btn in log_buttons)

    draft_safe = html.escape(draft_text).replace("\n", "<br>")

    return f"""\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:{_COLOR_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_COLOR_BG};">
  <tr><td align="center" style="padding:24px;">
    <table role="presentation" cellpadding="0" cellspacing="0" width="600" style="background:{_COLOR_CARD};border-radius:6px;overflow:hidden;max-width:600px;">
      <tr><td style="background:{_COLOR_BG};padding:24px 32px;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr>
            <td style="color:{_COLOR_WHITE};font-family:{_SERIF_STACK};font-size:20px;">
              Your Outgrow pulse &middot; {date_label}
            </td>
            <td align="right" style="color:{_COLOR_ACCENT};font-family:{_SANS_STACK};font-size:12px;letter-spacing:0.18em;">
              07:00 EDT
            </td>
          </tr>
        </table>
      </td></tr>
      <tr><td style="padding:32px;">
        <p style="margin:0 0 8px 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:13px;">Morning {rep_name_safe},</p>
        <h1 style="margin:0;color:{_COLOR_INK};font-family:{_SERIF_STACK};font-weight:400;font-size:36px;line-height:1.15;">
          Reach out to <em style="color:{_COLOR_ACCENT};font-style:italic;">{customer_safe}</em> today.
        </h1>
        <p style="margin:12px 0 24px 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:14px;">
          {subhead}
        </p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_COLOR_CARD};border-left:3px solid {_COLOR_ACCENT};">
          <tr><td style="padding:8px 20px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
              {row_html}
            </table>
          </td></tr>
        </table>
        <p style="margin:32px 0 12px 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:11px;letter-spacing:0.18em;text-transform:uppercase;">
          Suggested text &middot; In your voice
        </p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_COLOR_WHITE};border-radius:4px;">
          <tr><td style="padding:24px;color:{_COLOR_INK};font-family:{_SERIF_STACK};font-size:18px;line-height:1.5;">
            {draft_safe}
          </td></tr>
        </table>
        {compose_button_html}
        <p style="margin:24px 0 8px 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:12px;">
          Then log what you did &mdash; these reply to the control mailbox:
        </p>
        <table role="presentation" cellpadding="0" cellspacing="0">
          <tr>{buttons_html}</tr>
        </table>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


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
    today: date | None = None,
) -> PulseEmail:
    """Assemble the rep's pulse and validate every recipient against the allowlist.

    ``today`` drives the HTML header date and is optional so tests + the
    plain-text fallback still work without it; when omitted, the pulse
    has no ``html_body`` and the email goes out as text/plain only.
    """
    cc_owner = should_cc_owner(rep) and rep.email.lower() != policy.owner_email.lower()
    cc = (policy.owner_email,) if cc_owner else ()
    assert_recipients_allowed([rep.email, *cc], policy)
    body = render_pulse_body(
        rep_first_name=rep.first_name,
        briefing=briefing,
        draft_text=draft_text,
        links=links,
    )
    html_body = (
        render_pulse_html(
            rep_first_name=rep.first_name,
            briefing=briefing,
            draft_text=draft_text,
            links=links,
            today=today,
        )
        if today is not None
        else None
    )
    subject = f"Outgrow pulse — {briefing.name}"
    return PulseEmail(
        pulse_id=pulse_id,
        rep_email=rep.email,
        cc=cc,
        subject=subject,
        body=body,
        html_body=html_body,
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
    if pulse.html_body is not None:
        msg.add_alternative(pulse.html_body, subtype="html")
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
