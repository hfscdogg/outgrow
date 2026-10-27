"""Deal-based rep attribution tests.

Covers ``pipeline/attribution.py``, the ``deal_rep_by_contact`` hook in
``pipeline.customers.build_customers``, and the preview report in
``scripts/rep_attribution_report.py``. Pure: inputs are built inline.
"""

from __future__ import annotations

from datetime import date

import pytest

from intake.match_audit import MatchScore
from matching.identity import MatchResult
from pipeline.attribution import is_completed_stage, latest_deal_rep_by_contact
from pipeline.customers import build_customers
from ranking.engine import Customer, Play, RankingConfig
from scripts.rep_attribution_report import UNASSIGNED, compare_attribution, render_report

MARSHALL = "u_marshall"
HENRY = "u_henry"
FORMER = "u_former"  # deactivated user, not a rep
USER_TO_REP = {MARSHALL: "marshall", HENRY: "henry"}


def _deal(
    contact: str | None,
    owner: str,
    stage: str = "Installation Complete",
    closing: str | None = "2025-06-01",
    deal_id: str = "d1",
) -> dict:
    return {
        "id": deal_id,
        "Stage": stage,
        "Owner": {"id": owner, "name": "ignored"},
        "Contact_Name": {"id": contact, "name": "ignored"} if contact else None,
        "Closing_Date": closing,
        "Created_Time": "2020-01-01T09:00:00-05:00",
    }


# ---- is_completed_stage -----------------------------------------------------


@pytest.mark.parametrize(
    "stage",
    [
        "Closed Won",
        "Closed Won - Cash and Carry",
        "Installation Complete",
        "Finish-Out Complete",
        "Trim-Out Complete",
        "Service Call Complete",
        "D&E Complete",
    ],
)
def test_completed_stages(stage: str) -> None:
    assert is_completed_stage(stage)


@pytest.mark.parametrize(
    "stage",
    [
        "Closed Lost to Competition",
        "Closed Lost - Unable to Contact",
        "Client decided not to do work",
        "Estimate Delivered to Client",
        "Trim Out Scheduled",
        "",
        None,
    ],
)
def test_incomplete_stages(stage: str | None) -> None:
    assert not is_completed_stage(stage)


# ---- latest_deal_rep_by_contact ----------------------------------------------


def test_latest_completed_deal_wins() -> None:
    deals = [
        _deal("c1", HENRY, closing="2019-03-01", deal_id="d1"),
        _deal("c1", MARSHALL, closing="2024-08-15", deal_id="d2"),
    ]
    assert latest_deal_rep_by_contact(deals, USER_TO_REP) == {"c1": "marshall"}


def test_lost_and_open_deals_ignored() -> None:
    deals = [
        _deal("c1", HENRY, closing="2019-03-01", deal_id="d1"),
        _deal(
            "c1", MARSHALL, stage="Closed Lost to Competition", closing="2025-01-01", deal_id="d2"
        ),
        _deal("c1", MARSHALL, stage="Trim Out Scheduled", closing="2025-02-01", deal_id="d3"),
    ]
    assert latest_deal_rep_by_contact(deals, USER_TO_REP) == {"c1": "henry"}


def test_non_rep_owner_does_not_hide_older_rep_deal() -> None:
    deals = [
        _deal("c1", MARSHALL, closing="2018-05-01", deal_id="d1"),
        _deal("c1", FORMER, closing="2025-05-01", deal_id="d2"),
    ]
    assert latest_deal_rep_by_contact(deals, USER_TO_REP) == {"c1": "marshall"}


def test_deal_without_contact_or_date() -> None:
    deals = [
        _deal(None, MARSHALL, deal_id="d1"),
        _deal("c2", MARSHALL, closing=None, deal_id="d2"),
    ]
    # Created_Time is the fallback date; contactless deals are skipped.
    assert latest_deal_rep_by_contact(deals, USER_TO_REP) == {"c2": "marshall"}


# ---- build_customers integration ----------------------------------------------


def _ms(zoho_id: str, qbo_id: str) -> MatchScore:
    return MatchScore(zoho_id=zoho_id, qbo_id=qbo_id, tier=1, score=1.0, reason="test")


def _match(*pairs: tuple[str, str]) -> MatchResult:
    return MatchResult(
        auto_matched=tuple(_ms(z, q) for z, q in pairs),
        review_queue=(),
        unmatched_zoho=(),
        unmatched_qbo=(),
    )


def _zoho(zid: str, owner: str) -> dict:
    return {"id": zid, "Owner": {"id": owner}, "Created_Time": "2010-01-15T10:00:00+00:00"}


def _build(deal_rep_by_contact: dict[str, str] | None) -> dict[str, str]:
    customers = build_customers(
        zoho_contacts=[_zoho("c1", HENRY), _zoho("c2", HENRY), _zoho("c3", FORMER)],
        qbo_customers=[{"Id": q, "MetaData": {}} for q in ("q1", "q2", "q3")],
        qbo_invoices=[],
        match_result=_match(("c1", "q1"), ("c2", "q2"), ("c3", "q3")),
        zoho_user_to_rep_id=USER_TO_REP,
        deal_rep_by_contact=deal_rep_by_contact,
    )
    return {c.id: c.rep_id for c in customers}


def test_contact_owner_attribution_unchanged_without_deals() -> None:
    assert _build(None) == {"c1": "henry", "c2": "henry"}


def test_deal_rep_overrides_contact_owner_and_rescues_orphans() -> None:
    # c1: Henry-owned contact whose last job was Marshall's -> Marshall.
    # c2: no rep-owned deal -> falls back to contact owner (Henry).
    # c3: owned by a deactivated user (dropped before) -> Marshall via deal.
    assert _build({"c1": "marshall", "c3": "marshall"}) == {
        "c1": "marshall",
        "c2": "henry",
        "c3": "marshall",
    }


# ---- report ---------------------------------------------------------------------


def _cfg() -> RankingConfig:
    return RankingConfig(
        qbo_horizon=date(2017, 1, 1),
        legacy_bonus_cents_per_year=0,
        too_recent_days=30,
        ramp_up_days=180,
        sweet_spot_end_days=720,
        decay_floor_days=1825,
        cold_floor_factor=0.5,
        min_rep_match_confidence=0.5,
        suppression_penalty=0.0,
        min_ltv_cents=200_000,
    )


def _play() -> Play:
    return Play(
        key="daily_proactive",
        base_weight=1.0,
        enabled=True,
        min_days_since_install=None,
        max_days_since_install=None,
        seasonal_weights={},
    )


def _cust(cid: str, rep: str, ltv: int = 500_000) -> Customer:
    return Customer(
        id=cid,
        rep_id=rep,
        ltv_cents=ltv,
        last_purchase_at=date(2024, 1, 1),
        first_known_contact_at=date(2010, 1, 1),
        rep_match_confidence=1.0,
    )


def test_compare_attribution_counts_and_moves() -> None:
    before = [_cust("c1", "henry"), _cust("c2", "henry", ltv=100), _cust("c4", "zack")]
    after = [
        _cust("c1", "marshall"),
        _cust("c2", "henry", ltv=100),
        _cust("c3", "marshall"),
        _cust("c4", "zack"),
    ]
    report = compare_attribution(
        before,
        after,
        rep_ids=["henry", "marshall", "zack"],
        play=_play(),
        ranking_cfg=_cfg(),
        today=date(2026, 10, 27),
    )
    counts = {r.rep_id: r for r in report.reps}
    assert (counts["henry"].pool_contact, counts["henry"].eligible_contact) == (2, 1)
    assert (counts["henry"].pool_deal, counts["henry"].eligible_deal) == (1, 0)
    assert (counts["marshall"].pool_contact, counts["marshall"].pool_deal) == (0, 2)
    assert counts["marshall"].eligible_deal == 2
    assert set(report.moves) == {("henry", "marshall", 1), (UNASSIGNED, "marshall", 1)}

    text = render_report(report, today=date(2026, 10, 27))
    assert "| marshall | 0 | 2 | 0 | 2 |" in text
    assert "| henry | marshall | 1 |" in text
