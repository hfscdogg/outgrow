"""Customer-record + briefing assembly tests.

Pure unit tests — sockets are blocked at conftest, every input is
constructed inline so the math + edge cases are auditable in test
source.
"""

from __future__ import annotations

from datetime import date

from intake.match_audit import MatchScore
from matching.identity import MatchResult
from pipeline.customers import build_briefing, build_customers
from ranking.engine import Customer

# ---- helpers ---------------------------------------------------------------


def _ms(zoho_id: str, qbo_id: str, score: float = 1.0, tier: int = 1) -> MatchScore:
    return MatchScore(
        zoho_id=zoho_id,
        qbo_id=qbo_id,
        tier=tier,
        score=score,
        reason="test",
    )


def _result(*matches: MatchScore) -> MatchResult:
    return MatchResult(
        auto_matched=tuple(matches),
        review_queue=(),
        unmatched_zoho=(),
        unmatched_qbo=(),
    )


def _zoho(
    zid: str = "z1",
    *,
    owner: str = "u1",
    created: str = "2010-01-15T10:00:00+00:00",
    name: str = "Jane Smith",
) -> dict:
    return {
        "id": zid,
        "Owner": {"id": owner, "name": "ignored"},
        "Created_Time": created,
        "Full_Name": name,
    }


def _qbo(qid: str = "q1", *, created: str = "2017-03-20T14:30:00+00:00") -> dict:
    return {
        "Id": qid,
        "DisplayName": "Jane Smith",
        "MetaData": {"CreateTime": created},
    }


def _invoice(qid: str, total: float, txn: str) -> dict:
    return {
        "CustomerRef": {"value": qid},
        "TotalAmt": total,
        "TxnDate": txn,
    }


# ---- build_customers -------------------------------------------------------


def test_build_customers_happy_path() -> None:
    customers = build_customers(
        zoho_contacts=[_zoho()],
        qbo_customers=[_qbo()],
        qbo_invoices=[
            _invoice("q1", 100.00, "2024-09-12"),
            _invoice("q1", 50.50, "2023-05-01"),
        ],
        match_result=_result(_ms("z1", "q1", score=1.0)),
        zoho_user_to_rep_id={"u1": "zack"},
    )
    assert len(customers) == 1
    c = customers[0]
    assert c.id == "z1"
    assert c.rep_id == "zack"
    assert c.ltv_cents == 15050  # $100 + $50.50 = $150.50
    assert c.last_purchase_at == date(2024, 9, 12)
    assert c.first_known_contact_at == date(2010, 1, 15)
    assert c.rep_match_confidence == 1.0
    assert c.suppressed is False


def test_build_customers_zero_invoices_yields_zero_ltv_and_no_purchase() -> None:
    customers = build_customers(
        zoho_contacts=[_zoho()],
        qbo_customers=[_qbo()],
        qbo_invoices=[],
        match_result=_result(_ms("z1", "q1")),
        zoho_user_to_rep_id={"u1": "zack"},
    )
    assert customers[0].ltv_cents == 0
    assert customers[0].last_purchase_at is None


def test_build_customers_skips_unknown_zoho_owner() -> None:
    customers = build_customers(
        zoho_contacts=[_zoho(owner="stranger_uid")],
        qbo_customers=[_qbo()],
        qbo_invoices=[],
        match_result=_result(_ms("z1", "q1")),
        zoho_user_to_rep_id={"u1": "zack"},  # stranger_uid not mapped
    )
    assert customers == []


def test_build_customers_skips_when_zoho_row_missing_from_list() -> None:
    """Match references a Zoho ID that isn't in the synced list (data drift).
    Skip rather than crash."""
    customers = build_customers(
        zoho_contacts=[],
        qbo_customers=[_qbo()],
        qbo_invoices=[],
        match_result=_result(_ms("z_ghost", "q1")),
        zoho_user_to_rep_id={"u1": "zack"},
    )
    assert customers == []


def test_build_customers_uses_invoice_first_date_when_earliest() -> None:
    customers = build_customers(
        zoho_contacts=[_zoho(created="2020-01-01T00:00:00+00:00")],
        qbo_customers=[_qbo(created="2017-03-20T14:30:00+00:00")],
        qbo_invoices=[_invoice("q1", 100.0, "2008-06-01")],  # before both creates
        match_result=_result(_ms("z1", "q1")),
        zoho_user_to_rep_id={"u1": "zack"},
    )
    assert customers[0].first_known_contact_at == date(2008, 6, 1)


def test_build_customers_handles_missing_create_times() -> None:
    z = _zoho(created="")
    q = _qbo(created="")
    customers = build_customers(
        zoho_contacts=[z],
        qbo_customers=[q],
        qbo_invoices=[_invoice("q1", 100.0, "2020-01-01")],
        match_result=_result(_ms("z1", "q1")),
        zoho_user_to_rep_id={"u1": "zack"},
    )
    assert customers[0].first_known_contact_at == date(2020, 1, 1)


def test_build_customers_invoice_filtering_by_customer_id() -> None:
    """An invoice for a different QBO customer must not contribute to LTV."""
    customers = build_customers(
        zoho_contacts=[_zoho()],
        qbo_customers=[_qbo()],
        qbo_invoices=[
            _invoice("q1", 100.0, "2024-09-12"),
            _invoice("q_other", 9999.99, "2024-09-12"),  # ignored
        ],
        match_result=_result(_ms("z1", "q1")),
        zoho_user_to_rep_id={"u1": "zack"},
    )
    assert customers[0].ltv_cents == 10000


def test_build_customers_passes_rep_match_confidence_through() -> None:
    customers = build_customers(
        zoho_contacts=[_zoho()],
        qbo_customers=[_qbo()],
        qbo_invoices=[],
        match_result=_result(_ms("z1", "q1", score=0.87, tier=3)),
        zoho_user_to_rep_id={"u1": "zack"},
    )
    assert customers[0].rep_match_confidence == 0.87


def test_build_customers_handles_owner_as_bare_string() -> None:
    z = _zoho()
    z["Owner"] = "u1"  # not a dict — defensive
    customers = build_customers(
        zoho_contacts=[z],
        qbo_customers=[_qbo()],
        qbo_invoices=[],
        match_result=_result(_ms("z1", "q1")),
        zoho_user_to_rep_id={"u1": "zack"},
    )
    assert len(customers) == 1
    assert customers[0].rep_id == "zack"


def test_build_customers_skips_when_owner_field_absent() -> None:
    z = _zoho()
    del z["Owner"]
    customers = build_customers(
        zoho_contacts=[z],
        qbo_customers=[_qbo()],
        qbo_invoices=[],
        match_result=_result(_ms("z1", "q1")),
        zoho_user_to_rep_id={"u1": "zack"},
    )
    assert customers == []


def test_build_customers_marks_recently_pulsed_as_suppressed() -> None:
    customers = build_customers(
        zoho_contacts=[_zoho()],
        qbo_customers=[_qbo()],
        qbo_invoices=[],
        match_result=_result(_ms("z1", "q1")),
        zoho_user_to_rep_id={"u1": "zack"},
        recently_pulsed_ids=frozenset({"z1"}),
    )
    assert len(customers) == 1
    assert customers[0].suppressed is True
    assert customers[0].suppression_reason == "recently_pulsed"


def test_build_customers_unsuppressed_when_not_in_history() -> None:
    customers = build_customers(
        zoho_contacts=[_zoho()],
        qbo_customers=[_qbo()],
        qbo_invoices=[],
        match_result=_result(_ms("z1", "q1")),
        zoho_user_to_rep_id={"u1": "zack"},
        recently_pulsed_ids=frozenset({"some-other-id"}),
    )
    assert len(customers) == 1
    assert customers[0].suppressed is False
    assert customers[0].suppression_reason is None


# ---- build_briefing --------------------------------------------------------


def _customer(**overrides: object) -> Customer:
    base: dict = {
        "id": "z1",
        "rep_id": "zack",
        "ltv_cents": 5_240_000,  # $52,400
        "last_purchase_at": date(2024, 9, 12),
        "first_known_contact_at": date(2010, 1, 15),
        "rep_match_confidence": 0.9,
    }
    base.update(overrides)
    return Customer(**base)  # type: ignore[arg-type]


def test_briefing_renders_basic_fields() -> None:
    draft, mail = build_briefing(
        _customer(),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    assert draft.name == "Jane Smith"
    assert draft.lifetime_spend_label.startswith("$52,400")
    assert draft.last_purchase_label == "Sep 2024"
    assert mail.name == "Jane Smith"
    assert ("Lifetime spend", draft.lifetime_spend_label) in mail.rows


def test_briefing_legacy_disclosure_when_pre_qbo_horizon() -> None:
    draft, _ = build_briefing(
        _customer(first_known_contact_at=date(2003, 6, 1)),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    assert draft.lifetime_spend_label == "$52,400 (partial since 2017)"


def test_briefing_no_disclosure_when_post_qbo_horizon() -> None:
    draft, _ = build_briefing(
        _customer(first_known_contact_at=date(2020, 6, 1)),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    assert draft.lifetime_spend_label == "$52,400"


def test_briefing_no_disclosure_when_first_known_unknown() -> None:
    draft, _ = build_briefing(
        _customer(first_known_contact_at=None),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    assert "partial since" not in draft.lifetime_spend_label


def test_briefing_source_dollar_amounts_includes_ltv() -> None:
    draft, _ = build_briefing(
        _customer(),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    assert "$52,400" in draft.source_dollar_amounts


def test_briefing_falls_back_to_first_last_when_full_name_absent() -> None:
    z = _zoho(name="")
    z.pop("Full_Name")
    z["First_Name"] = "Jane"
    z["Last_Name"] = "Smith"
    draft, mail = build_briefing(
        _customer(),
        zoho_contact=z,
        qbo_horizon=date(2017, 1, 1),
    )
    assert draft.name == "Jane Smith"
    assert mail.name == "Jane Smith"


def test_briefing_default_name_when_no_name_fields() -> None:
    z = {"id": "z1"}  # no name fields at all
    draft, _ = build_briefing(
        _customer(),
        zoho_contact=z,
        qbo_horizon=date(2017, 1, 1),
    )
    assert draft.name == "Customer"


def test_briefing_omits_last_purchase_row_when_unknown() -> None:
    _, mail = build_briefing(
        _customer(last_purchase_at=None),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    keys = [k for k, _ in mail.rows]
    assert "Last purchase" not in keys


def test_briefing_zero_ltv_renders_as_dollar_zero() -> None:
    draft, _ = build_briefing(
        _customer(ltv_cents=0),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    assert draft.lifetime_spend_label.startswith("$0")
    assert "$0" in draft.source_dollar_amounts


def test_briefing_million_dollar_ltv_formats_with_commas() -> None:
    draft, _ = build_briefing(
        _customer(ltv_cents=123_456_700),  # $1,234,567
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    assert draft.lifetime_spend_label.startswith("$1,234,567")


# ---- HTML-redesign-era fields: first_invoice, contact rows, dormancy_label --


def test_briefing_uses_first_invoice_at_not_first_known_contact() -> None:
    """The display row should come from QBO's first invoice, not the Zoho
    record-creation date — the latter is misleading for migrated customers.
    """
    _, mail = build_briefing(
        _customer(
            first_known_contact_at=date(2010, 1, 15),  # ranking signal
            first_invoice_at=date(2012, 6, 1),  # display signal
        ),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    keys = {k: v for k, v in mail.rows}
    assert keys.get("First invoice") == "Jun 2012"


def test_briefing_omits_first_invoice_row_when_absent() -> None:
    _, mail = build_briefing(
        _customer(first_invoice_at=None),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    assert "First invoice" not in [k for k, _ in mail.rows]


def test_briefing_includes_mobile_phone_email_rows() -> None:
    z = _zoho()
    z["Mobile"] = "+15551234567"
    z["Phone"] = "+15559876543"
    z["Email"] = "jane@example.com"
    _, mail = build_briefing(
        _customer(),
        zoho_contact=z,
        qbo_horizon=date(2017, 1, 1),
    )
    rows = dict(mail.rows)
    assert rows.get("Mobile") == "+15551234567"
    assert rows.get("Phone") == "+15559876543"
    assert rows.get("Email") == "jane@example.com"


def test_briefing_omits_contact_rows_when_blank_or_missing() -> None:
    z = _zoho()
    z["Mobile"] = ""  # blank string — common Zoho export artifact
    z.pop("Phone", None)
    _, mail = build_briefing(
        _customer(),
        zoho_contact=z,
        qbo_horizon=date(2017, 1, 1),
    )
    keys = [k for k, _ in mail.rows]
    assert "Mobile" not in keys
    assert "Phone" not in keys


def test_briefing_dormancy_label_set_when_today_provided() -> None:
    _, mail = build_briefing(
        _customer(last_purchase_at=date(2022, 1, 1)),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
        today=date(2026, 5, 14),
    )
    # 4y 4mo ago by approximate math.
    assert mail.dormancy_label is not None
    assert mail.dormancy_label.startswith("4y")


def test_briefing_dormancy_label_none_when_today_omitted() -> None:
    _, mail = build_briefing(
        _customer(),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
    )
    assert mail.dormancy_label is None


def test_briefing_dormancy_label_handles_recent_purchase() -> None:
    _, mail = build_briefing(
        _customer(last_purchase_at=date(2026, 5, 10)),
        zoho_contact=_zoho(),
        qbo_horizon=date(2017, 1, 1),
        today=date(2026, 5, 14),
    )
    # 4 days ago — under the month threshold.
    assert mail.dormancy_label == "this month"
