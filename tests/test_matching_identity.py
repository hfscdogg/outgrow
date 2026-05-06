"""Production 3-tier identity matcher tests.

Pure unit tests — no I/O beyond a tmp_path JSON roundtrip for the
loaders. Sockets are blocked at conftest, so any accidental live call
would raise NetworkBlockedError.
"""

from __future__ import annotations

import json
from pathlib import Path

from intake.match_audit import ContactRecord
from matching.identity import (
    MatchResult,
    load_qbo_customers,
    load_zoho_contacts,
    match,
    qbo_customer_to_record,
    zoho_contact_to_record,
)

# ---- normalizers ----------------------------------------------------------


def test_zoho_record_uses_full_name_when_present() -> None:
    rec = zoho_contact_to_record(
        {
            "id": "z1",
            "Full_Name": "Jane Smith",
            "First_Name": "Jane",
            "Last_Name": "Smith",
            "Email": "Jane@Example.com",
            "Phone": "(555) 123-4567",
            "Mailing_Street": "1 Main St",
            "Mailing_City": "Boston",
            "Mailing_State": "MA",
            "Mailing_Zip": "02108",
        }
    )
    assert rec.record_id == "z1"
    assert rec.name == "Jane Smith"
    assert rec.email == "jane@example.com"  # lowercased
    assert rec.phone_e164 == "+15551234567"  # E.164 normalized
    assert rec.address == "1 Main St, Boston, MA, 02108"


def test_zoho_record_falls_back_to_first_last_when_full_name_missing() -> None:
    rec = zoho_contact_to_record({"id": "z2", "First_Name": "Jane", "Last_Name": "Smith"})
    assert rec.name == "Jane Smith"


def test_zoho_record_handles_missing_optional_fields() -> None:
    rec = zoho_contact_to_record({"id": "z3", "Full_Name": "Jane Smith"})
    assert rec.email is None
    assert rec.phone_e164 is None
    assert rec.address == ""


def test_zoho_record_uses_mobile_when_phone_missing() -> None:
    rec = zoho_contact_to_record({"id": "z4", "Full_Name": "Jane", "Mobile": "555-123-4567"})
    assert rec.phone_e164 == "+15551234567"


def test_qbo_record_extracts_nested_email_phone_address() -> None:
    rec = qbo_customer_to_record(
        {
            "Id": "1",
            "DisplayName": "Jane Smith",
            "PrimaryEmailAddr": {"Address": "JANE@example.com"},
            "PrimaryPhone": {"FreeFormNumber": "555.123.4567"},
            "BillAddr": {
                "Line1": "1 Main St",
                "City": "Boston",
                "CountrySubDivisionCode": "MA",
                "PostalCode": "02108",
            },
        }
    )
    assert rec.record_id == "1"
    assert rec.name == "Jane Smith"
    assert rec.email == "jane@example.com"
    assert rec.phone_e164 == "+15551234567"
    assert rec.address == "1 Main St, Boston, MA, 02108"


def test_qbo_record_handles_missing_email_phone() -> None:
    rec = qbo_customer_to_record({"Id": "2", "DisplayName": "Solo"})
    assert rec.email is None
    assert rec.phone_e164 is None


def test_e164_normalizer_accepts_already_e164_format() -> None:
    rec = qbo_customer_to_record(
        {
            "Id": "3",
            "DisplayName": "Intl",
            "PrimaryPhone": {"FreeFormNumber": "+44 20 7946 0958"},
        }
    )
    # Already-prefixed number with ≥10 digits keeps the +
    assert rec.phone_e164 == "+442079460958"


def test_e164_normalizer_rejects_garbage() -> None:
    rec = qbo_customer_to_record(
        {"Id": "4", "DisplayName": "x", "PrimaryPhone": {"FreeFormNumber": "ext 1234"}}
    )
    assert rec.phone_e164 is None


# ---- 3-tier match logic ----------------------------------------------------


def _zoho(
    rid: str,
    *,
    email: str | None = None,
    phone: str | None = None,
    name: str = "",
    address: str = "",
) -> ContactRecord:
    return ContactRecord(record_id=rid, name=name, address=address, email=email, phone_e164=phone)


def _qbo(
    rid: str,
    *,
    email: str | None = None,
    phone: str | None = None,
    name: str = "",
    address: str = "",
) -> ContactRecord:
    return ContactRecord(record_id=rid, name=name, address=address, email=email, phone_e164=phone)


def test_tier1_email_exact_auto_matches() -> None:
    z = [_zoho("z1", email="jane@example.com")]
    q = [_qbo("q1", email="jane@example.com")]
    result = match(z, q)
    assert len(result.auto_matched) == 1
    assert result.auto_matched[0].tier == 1
    assert result.auto_matched[0].zoho_id == "z1"
    assert result.auto_matched[0].qbo_id == "q1"
    assert result.review_queue == ()


def test_tier2_phone_exact_when_emails_differ() -> None:
    z = [_zoho("z1", email="alt-zoho@example.com", phone="+15551234567")]
    q = [_qbo("q1", email="alt-qbo@example.com", phone="+15551234567")]
    result = match(z, q)
    assert len(result.auto_matched) == 1
    assert result.auto_matched[0].tier == 2


def test_tier3_fuzzy_auto_accepts_above_threshold() -> None:
    z = [_zoho("z1", name="Jane Smith", address="1 Main St, Boston, MA")]
    q = [_qbo("q1", name="Jane Smith", address="1 Main St Boston MA")]
    result = match(z, q)
    assert len(result.auto_matched) == 1
    assert result.auto_matched[0].tier == 3


def test_tier3_below_threshold_lands_in_review_queue() -> None:
    z = [_zoho("z1", name="Jane Smith", address="1 Main St, Boston, MA")]
    q = [_qbo("q1", name="John Doe", address="999 Other Ave, Seattle, WA")]
    result = match(z, q)
    assert result.auto_matched == ()
    assert len(result.review_queue) == 1
    assert result.review_queue[0].tier == 3


def test_unmatched_when_no_qbo_candidates() -> None:
    z = [_zoho("z1", email="jane@example.com")]
    q: list[ContactRecord] = []
    result = match(z, q)
    assert result.auto_matched == ()
    assert result.review_queue == ()
    assert result.unmatched_zoho == ("z1",)


def test_unmatched_qbo_collected_when_no_zoho_match() -> None:
    z: list[ContactRecord] = []
    q = [_qbo("q1", email="orphan@example.com")]
    result = match(z, q)
    assert result.unmatched_qbo == ("q1",)


def test_email_match_is_case_insensitive() -> None:
    z = [_zoho("z1", email="jane@example.com")]
    # email already normalized via zoho_contact_to_record; here we feed
    # ContactRecord directly so test the index path
    q_raw = qbo_customer_to_record(
        {"Id": "q1", "DisplayName": "j", "PrimaryEmailAddr": {"Address": "JANE@EXAMPLE.COM"}}
    )
    result = match(z, [q_raw])
    assert len(result.auto_matched) == 1
    assert result.auto_matched[0].tier == 1


def test_qbo_row_not_double_assigned_across_tiers() -> None:
    """Two Zoho contacts both want the same QBO row — first claim wins,
    other Zoho falls through to fuzzy / unmatched."""
    z = [
        _zoho("z1", email="jane@example.com"),  # tier 1 winner
        _zoho("z2", phone="+15551234567"),  # would tier 2 to same q1
    ]
    q = [_qbo("q1", email="jane@example.com", phone="+15551234567")]
    result = match(z, q)
    matched_qbo_ids = {s.qbo_id for s in result.auto_matched}
    assert matched_qbo_ids == {"q1"}
    assert "z1" not in result.unmatched_zoho
    assert "z2" in result.unmatched_zoho


def test_tier_priority_email_beats_phone() -> None:
    """When the same Zoho row could match two different QBOs (email vs phone),
    Tier 1 (email) wins."""
    z = [_zoho("z1", email="jane@example.com", phone="+15551234567")]
    q = [
        _qbo("q1", email="jane@example.com"),  # tier 1 candidate
        _qbo("q2", phone="+15551234567"),  # tier 2 candidate
    ]
    result = match(z, q)
    assert len(result.auto_matched) == 1
    assert result.auto_matched[0].tier == 1
    assert result.auto_matched[0].qbo_id == "q1"
    assert result.unmatched_qbo == ("q2",)


def test_full_3tier_run_aggregate() -> None:
    z = [
        _zoho("z1", email="jane@example.com"),
        _zoho("z2", phone="+15552223333"),
        _zoho("z3", name="Sarah Jones", address="500 Park Ave, NY, NY"),
        _zoho("z4", email="orphan@example.com"),
    ]
    q = [
        _qbo("q1", email="jane@example.com"),
        _qbo("q2", phone="+15552223333"),
        _qbo("q3", name="Sarah Jones", address="500 Park Ave NY NY"),
        _qbo("q4", name="Stranger", address="far away"),
    ]
    result = match(z, q)
    tiers = sorted(s.tier for s in result.auto_matched)
    assert tiers == [1, 2, 3]
    # z4 had a candidate (q4 via fuzzy), so check that one ended up in
    # the review queue rather than unmatched_zoho.
    assert len(result.review_queue) == 1
    assert result.review_queue[0].zoho_id == "z4"


# ---- loaders ---------------------------------------------------------------


def test_load_zoho_contacts_from_json(tmp_path: Path) -> None:
    payload = [
        {"id": "z1", "Full_Name": "Jane Smith", "Email": "jane@example.com"},
        {"id": "z2", "First_Name": "Bob", "Last_Name": "Jones"},
    ]
    p = tmp_path / "contacts.json"
    p.write_text(json.dumps(payload))
    records = load_zoho_contacts(p)
    assert [r.record_id for r in records] == ["z1", "z2"]
    assert records[0].email == "jane@example.com"
    assert records[1].name == "Bob Jones"


def test_load_qbo_customers_from_json(tmp_path: Path) -> None:
    payload = [
        {"Id": "1", "DisplayName": "Jane", "PrimaryEmailAddr": {"Address": "jane@example.com"}},
    ]
    p = tmp_path / "customers.json"
    p.write_text(json.dumps(payload))
    records = load_qbo_customers(p)
    assert records[0].email == "jane@example.com"


def test_match_result_is_immutable() -> None:
    """Defense against accidental in-place mutation by callers."""
    result = match([], [])
    assert isinstance(result, MatchResult)
    assert isinstance(result.auto_matched, tuple)
    assert isinstance(result.review_queue, tuple)
    assert isinstance(result.unmatched_zoho, tuple)
    assert isinstance(result.unmatched_qbo, tuple)
