"""Match-audit fuzzy scorer tests. All fixtures use synthetic data."""

from __future__ import annotations

from pathlib import Path

import pytest

from intake.match_audit import (
    AUTO_ACCEPT_THRESHOLD,
    ContactRecord,
    fetch_qbo_customers,
    fetch_zoho_contacts,
    filter_ambiguous,
    is_ambiguous,
    score_pair,
    write_ambiguous_csv,
)


def _zoho() -> ContactRecord:
    return ContactRecord(
        record_id="z1",
        name="Jane Doe",
        address="123 Maple St, Anytown, VA",
        email="jane@example.com",
        phone_e164="+15551234567",
    )


def _qbo() -> ContactRecord:
    return ContactRecord(
        record_id="q1",
        name="Jane Doe",
        address="123 Maple St, Anytown, VA",
        email="jane@example.com",
        phone_e164="+15551234567",
    )


def test_email_exact_returns_tier_1() -> None:
    score = score_pair(_zoho(), _qbo())
    assert score.tier == 1
    assert score.score == 1.0
    assert "email" in score.reason


def test_email_match_is_case_insensitive() -> None:
    z = ContactRecord(record_id="z", name="x", address="x", email="JANE@example.com")
    q = ContactRecord(record_id="q", name="x", address="x", email="jane@EXAMPLE.com")
    assert score_pair(z, q).tier == 1


def test_phone_exact_returns_tier_2_when_email_missing() -> None:
    z = ContactRecord(record_id="z", name="A", address="X", phone_e164="+15551234567")
    q = ContactRecord(record_id="q", name="B", address="Y", phone_e164="+15551234567")
    score = score_pair(z, q)
    assert score.tier == 2
    assert score.score == pytest.approx(0.95)


def test_non_e164_phone_does_not_short_circuit_to_tier_2() -> None:
    z = ContactRecord(record_id="z", name="A", address="X", phone_e164="555-123-4567")
    q = ContactRecord(record_id="q", name="A", address="X", phone_e164="555-123-4567")
    score = score_pair(z, q)
    assert score.tier == 3


def test_fuzzy_high_score_auto_accepts() -> None:
    z = ContactRecord(record_id="z", name="Jane Doe", address="123 Maple St")
    q = ContactRecord(record_id="q", name="Jane Doe", address="123 Maple Street")
    score = score_pair(z, q)
    assert score.tier == 3
    assert score.score >= AUTO_ACCEPT_THRESHOLD
    assert not is_ambiguous(score)


def test_fuzzy_low_score_is_ambiguous() -> None:
    z = ContactRecord(record_id="z", name="Aardvark Industries", address="1 First Ave")
    q = ContactRecord(record_id="q", name="Zebra LLC", address="999 Last Way")
    score = score_pair(z, q)
    assert score.tier == 3
    assert is_ambiguous(score)


def test_filter_ambiguous_drops_auto_accepted() -> None:
    z = ContactRecord(record_id="z", name="Jane", address="X", email="jane@example.com")
    q = ContactRecord(record_id="q", name="Jane", address="X", email="jane@example.com")
    auto = score_pair(z, q)
    fuzzy = score_pair(
        ContactRecord(record_id="z2", name="A", address="X"),
        ContactRecord(record_id="q2", name="Z", address="Y"),
    )
    ambiguous = filter_ambiguous([auto, fuzzy])
    assert auto not in ambiguous
    assert fuzzy in ambiguous


def test_write_ambiguous_csv_emits_only_ambiguous(tmp_path: Path) -> None:
    auto = score_pair(_zoho(), _qbo())
    fuzzy = score_pair(
        ContactRecord(record_id="z2", name="A", address="X"),
        ContactRecord(record_id="q2", name="Z", address="Y"),
    )
    out = tmp_path / "audit.csv"
    written = write_ambiguous_csv([auto, fuzzy], out)
    assert written == 1
    content = out.read_text(encoding="utf-8")
    assert "z2" in content
    assert "z1" not in content


def test_zoho_connector_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        fetch_zoho_contacts(10)


def test_qbo_connector_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        fetch_qbo_customers(10)
