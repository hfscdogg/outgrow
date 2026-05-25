"""Render a ``WeeklyDigest`` into HTML + plain-text email bodies.

Visual language mirrors the daily pulse email (black header band,
cream cards, orange accent) so the rep recognizes it as part of the
same surface. Inline-only CSS — Gmail strips ``<style>`` blocks in
some contexts.
"""

from __future__ import annotations

import html
from datetime import date

from reports.weekly_digest import (
    WeeklyDigest,
    acted_on_count,
    acted_on_pct,
    drafter_accuracy_pct,
)

# Same palette as ``pulse/mailer.py`` so the surfaces feel like one product.
_SERIF_STACK = "'Cormorant Garamond', 'Playfair Display', Georgia, 'Times New Roman', serif"
_SANS_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
_COLOR_BG = "#000000"
_COLOR_CARD = "#f7f3ec"
_COLOR_INK = "#1a1a1a"
_COLOR_MUTED = "#6b6b6b"
_COLOR_ACCENT = "#d97a3a"
_COLOR_WHITE = "#ffffff"

_ACTION_LABELS: dict[str, str] = {
    "sent": "Sent as-is",
    "edited": "Sent w/ edits",
    "skipped": "Skipped",
    "reassign": "Reassigned",
    "none": "No action recorded",
}


def _date_label(d: date) -> str:
    return d.strftime("%b %-d")


def render_subject(digest: WeeklyDigest) -> str:
    """One short headline so the rep can triage from the inbox list."""
    return (
        f"Outgrow weekly — {digest.pulses_sent} pulse"
        f"{'s' if digest.pulses_sent != 1 else ''}, "
        f"{acted_on_pct(digest.actions)}% acted on"
    )


# ---- plain-text fallback ---------------------------------------------------


def render_plain(digest: WeeklyDigest, rep_first_name: str) -> str:
    """Plain-text body for clients that don't render HTML.

    Same data + ordering as the HTML so a screen-reader-only user gets
    the full report.
    """
    rows: list[str] = [
        f"Morning {rep_first_name},",
        "",
        f"Outgrow recap — week of {_date_label(digest.period_start)}",
        "─" * 40,
        "",
        f"Pulses sent:   {digest.pulses_sent}",
        f"Acted on:      {acted_on_count(digest.actions)} ({acted_on_pct(digest.actions)}%)",
    ]
    # Only the buckets with at least one entry — keeps short weeks readable.
    for key, label in _ACTION_LABELS.items():
        count = digest.actions.get(key, 0)
        if count > 0:
            rows.append(f"  {label:<22} {count}")
    rows.extend(["", "Customers reached:"])
    if not digest.touches:
        rows.append("  (none)")
    for t in digest.touches:
        action_label = _ACTION_LABELS.get(t.action or "none", "—")
        rows.append(f"  • {t.customer_name:<28} {action_label}  ({_date_label(t.date)})")

    rows.extend(
        [
            "",
            f"Drafter accuracy: {drafter_accuracy_pct(digest.actions)}% of acted-on "
            "pulses sent verbatim (signal of voice fit).",
            "",
            f"YTD: {digest.ytd_pulses} pulses, "
            f"{acted_on_count(digest.ytd_actions)} acted on "
            f"({acted_on_pct(digest.ytd_actions)}%).",
        ]
    )
    return "\n".join(rows)


# ---- HTML render ----------------------------------------------------------


def _stat_card(label: str, value: str, accent: bool = False) -> str:
    """Big-number tile. ``accent`` paints the value orange (used for the
    headline pulse count).
    """
    value_color = _COLOR_ACCENT if accent else _COLOR_INK
    return (
        f'<td style="padding:18px 20px;background:{_COLOR_CARD};'
        'border-radius:6px;text-align:center;">'
        f'<div style="color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:11px;'
        f'letter-spacing:0.18em;text-transform:uppercase;">{html.escape(label)}</div>'
        f'<div style="color:{value_color};font-family:{_SERIF_STACK};font-size:36px;'
        f'line-height:1.1;margin-top:6px;">{html.escape(value)}</div>'
        "</td>"
    )


def _action_row(label: str, count: int) -> str:
    return (
        "<tr>"
        f'<td style="padding:10px 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};'
        "font-size:12px;letter-spacing:0.14em;text-transform:uppercase;"
        f'border-bottom:1px dashed #d4cfc4;">{html.escape(label)}</td>'
        f'<td style="padding:10px 0;color:{_COLOR_INK};font-family:{_SANS_STACK};'
        f'font-size:15px;text-align:right;border-bottom:1px dashed #d4cfc4;">{count}</td>'
        "</tr>"
    )


def _touch_row(customer_name: str, action: str | None, d: date) -> str:
    action_label = _ACTION_LABELS.get(action or "none", "—")
    return (
        "<tr>"
        f'<td style="padding:8px 0;color:{_COLOR_INK};font-family:{_SANS_STACK};font-size:14px;">'
        f"{html.escape(customer_name)}"
        "</td>"
        f'<td style="padding:8px 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};'
        'font-size:12px;text-align:right;">'
        f"{html.escape(action_label)}"
        f' <span style="color:#aaa;">· {html.escape(_date_label(d))}</span>'
        "</td>"
        "</tr>"
    )


def render_html(digest: WeeklyDigest, rep_first_name: str) -> str:
    """HTML body. Inline-only CSS for Gmail compatibility; tables-for-layout
    for Outlook. Mirrors ``pulse/mailer.py::render_pulse_html`` visually.
    """
    rep_name_safe = html.escape(rep_first_name)
    period_label = f"Week of {_date_label(digest.period_start)} — {_date_label(digest.period_end)}"

    touches_html = (
        "".join(_touch_row(t.customer_name, t.action, t.date) for t in digest.touches)
        if digest.touches
        else f'<tr><td style="padding:12px 0;color:{_COLOR_MUTED};'
        f'font-family:{_SANS_STACK};font-style:italic;font-size:14px;">'
        "No pulses last week.</td></tr>"
    )

    # Action breakdown — only buckets with at least one entry.
    action_rows_html = "".join(
        _action_row(label, digest.actions.get(key, 0))
        for key, label in _ACTION_LABELS.items()
        if digest.actions.get(key, 0) > 0
    )

    accuracy = drafter_accuracy_pct(digest.actions)
    acted_total = acted_on_count(digest.actions)
    pulses = digest.pulses_sent

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
              Outgrow weekly recap
            </td>
            <td align="right" style="color:{_COLOR_ACCENT};font-family:{_SANS_STACK};font-size:12px;letter-spacing:0.18em;">
              {html.escape(period_label)}
            </td>
          </tr>
        </table>
      </td></tr>
      <tr><td style="padding:32px;background:{_COLOR_WHITE};">
        <p style="margin:0 0 24px 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:13px;">Morning {rep_name_safe},</p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          <tr>
            {_stat_card("Pulses sent", str(pulses), accent=True)}
            <td width="12">&nbsp;</td>
            {_stat_card("Acted on", f"{acted_on_pct(digest.actions)}%")}
            <td width="12">&nbsp;</td>
            {_stat_card("Drafter fit", f"{accuracy}%")}
          </tr>
        </table>
        <p style="margin:24px 0 8px 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:11px;letter-spacing:0.18em;text-transform:uppercase;">
          Action breakdown
        </p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          {action_rows_html}
        </table>
        <p style="margin:32px 0 8px 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:11px;letter-spacing:0.18em;text-transform:uppercase;">
          Customers reached
        </p>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
          {touches_html}
        </table>
        <p style="margin:28px 0 0 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:13px;line-height:1.5;">
          <strong style="color:{_COLOR_INK};">Drafter fit:</strong> {accuracy}% of acted-on
          pulses went out verbatim. The closer that gets to 100%, the better the
          AI is matching your voice.
        </p>
        <p style="margin:8px 0 0 0;color:{_COLOR_MUTED};font-family:{_SANS_STACK};font-size:12px;">
          YTD: {digest.ytd_pulses} pulse{"s" if digest.ytd_pulses != 1 else ""},
          {acted_on_count(digest.ytd_actions)} acted on
          ({acted_on_pct(digest.ytd_actions)}%) · {acted_total} of {pulses} this week.
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
