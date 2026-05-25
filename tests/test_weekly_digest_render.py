"""Tests for ``reports/render.py``.

Asserts that the right fields surface in both plain-text and HTML,
that HTML escapes work, and that the subject line summarizes correctly.
"""

from __future__ import annotations

from datetime import date

from reports.render import (
    render_html,
    render_plain,
    render_subject,
)
from reports.weekly_digest import CustomerTouch, WeeklyDigest


def _digest(
    *,
    rep_id: str = "henry",
    pulses_sent: int = 3,
    actions: dict[str, int] | None = None,
    touches: list[CustomerTouch] | None = None,
    ytd_pulses: int = 12,
    ytd_actions: dict[str, int] | None = None,
) -> WeeklyDigest:
    return WeeklyDigest(
        rep_id=rep_id,
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 22),
        pulses_sent=pulses_sent,
        actions=actions or {"sent": 1, "edited": 2, "skipped": 0, "reassign": 0, "none": 0},
        touches=touches or [],
        ytd_pulses=ytd_pulses,
        ytd_actions=ytd_actions or {"sent": 4, "edited": 6, "skipped": 1, "reassign": 0, "none": 1},
    )


# ---- subject ---------------------------------------------------------------


def test_subject_pluralizes_pulses() -> None:
    assert "3 pulses" in render_subject(_digest(pulses_sent=3))
    assert "1 pulse," in render_subject(_digest(pulses_sent=1))


def test_subject_includes_acted_on_pct() -> None:
    actions = {"sent": 3, "edited": 0, "skipped": 0, "reassign": 0, "none": 0}
    assert "100% acted on" in render_subject(_digest(pulses_sent=3, actions=actions))


# ---- plain text -----------------------------------------------------------


def test_plain_includes_period_and_counts() -> None:
    out = render_plain(_digest(), "Henry")
    assert "Morning Henry" in out
    assert "week of May 18" in out  # lowercase 'week' in plain-text rendering
    assert "Pulses sent:   3" in out
    assert "Acted on:      3" in out  # 1 sent + 2 edited
    assert "Sent as-is" in out
    assert "Sent w/ edits" in out


def test_plain_omits_zero_action_buckets() -> None:
    out = render_plain(_digest(), "Henry")
    # Skipped/Reassigned/No-action are 0 in the default fixture — should
    # NOT appear (keeps short weeks readable).
    assert "Skipped" not in out
    assert "Reassigned" not in out


def test_plain_shows_no_customers_message_when_empty() -> None:
    out = render_plain(
        _digest(
            pulses_sent=0,
            actions={"sent": 0, "edited": 0, "skipped": 0, "reassign": 0, "none": 0},
            touches=[],
        ),
        "Henry",
    )
    assert "(none)" in out


def test_plain_includes_customer_names_and_actions() -> None:
    touches = [
        CustomerTouch(
            customer_id="c1",
            customer_name="Drew Spitzer",
            date=date(2026, 5, 20),
            action="edited",
            edited_text="Hey Drew",
        ),
    ]
    out = render_plain(_digest(touches=touches), "Henry")
    assert "Drew Spitzer" in out
    assert "Sent w/ edits" in out


def test_plain_includes_drafter_accuracy_line() -> None:
    actions = {"sent": 1, "edited": 3, "skipped": 0, "reassign": 0, "none": 0}
    # 1/4 = 25%
    out = render_plain(_digest(actions=actions, pulses_sent=4), "Henry")
    assert "25%" in out
    assert "Drafter accuracy" in out


def test_plain_includes_ytd_summary() -> None:
    out = render_plain(_digest(ytd_pulses=12), "Henry")
    assert "YTD:" in out
    assert "12 pulses" in out


# ---- HTML -----------------------------------------------------------------


def test_html_includes_rep_name_and_period() -> None:
    out = render_html(_digest(), "Henry")
    assert "Morning Henry" in out
    assert "Week of May 18" in out


def test_html_renders_pulse_count_in_stat_card() -> None:
    out = render_html(_digest(pulses_sent=7), "Henry")
    # Pulse count is the accent (orange) stat card.
    assert ">7<" in out
    assert "Pulses sent" in out


def test_html_action_breakdown_only_includes_nonzero_buckets() -> None:
    out = render_html(_digest(), "Henry")
    # actions={"sent": 1, "edited": 2, ...}  -- skipped/reassign/none=0
    assert "Sent as-is" in out
    assert "Sent w/ edits" in out
    assert "Skipped" not in out
    assert "Reassigned" not in out


def test_html_escapes_customer_name() -> None:
    touches = [
        CustomerTouch(
            customer_id="c1",
            customer_name="<script>alert(1)</script>",
            date=date(2026, 5, 20),
            action="sent",
            edited_text=None,
        ),
    ]
    out = render_html(_digest(touches=touches), "Henry")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_html_handles_empty_week_with_friendly_message() -> None:
    out = render_html(
        _digest(
            pulses_sent=0,
            actions={"sent": 0, "edited": 0, "skipped": 0, "reassign": 0, "none": 0},
            touches=[],
        ),
        "Henry",
    )
    assert "No pulses last week." in out


def test_html_drafter_accuracy_calls_out_voice_fit() -> None:
    out = render_html(_digest(), "Henry")
    # 1 sent / 3 acted = 33%
    assert "33%" in out
    assert "Drafter fit" in out
