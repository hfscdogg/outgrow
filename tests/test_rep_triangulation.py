"""Rep-match triangulation tests. Synthetic rep names only."""

from __future__ import annotations

from pathlib import Path

import pytest

from intake.rep_triangulation import (
    TriangulationRow,
    fetch_dtools_pm,
    fetch_qbo_rep,
    fetch_zoho_owner,
    filter_disagreements,
    has_disagreement,
    write_disagreements_csv,
)


def _row(zoho: str | None, qbo: str | None, dtools: str | None) -> TriangulationRow:
    return TriangulationRow(
        customer_id="c1",
        customer_name="Synthetic Customer",
        zoho_owner=zoho,
        qbo_rep=qbo,
        dtools_pm=dtools,
    )


def test_full_agreement_is_not_disagreement() -> None:
    assert not has_disagreement(_row("zack", "zack", "zack"))


def test_two_agree_one_differs_is_disagreement() -> None:
    assert has_disagreement(_row("zack", "zack", "owner"))


def test_three_way_disagreement_is_disagreement() -> None:
    assert has_disagreement(_row("zack", "owner", "alex"))


def test_only_one_source_present_is_not_disagreement() -> None:
    assert not has_disagreement(_row("zack", None, None))


def test_two_sources_match_third_missing_is_not_disagreement() -> None:
    assert not has_disagreement(_row("zack", None, "zack"))


def test_disagreement_check_is_case_insensitive() -> None:
    assert not has_disagreement(_row("Zack", "ZACK", "zack"))


def test_filter_returns_only_disagreements() -> None:
    agree = _row("zack", "zack", "zack")
    disagree = _row("zack", "owner", None)
    out = filter_disagreements([agree, disagree])
    assert out == [disagree]


def test_write_disagreements_csv_emits_only_disagreements(tmp_path: Path) -> None:
    agree = _row("zack", "zack", "zack")
    disagree = _row("zack", "owner", None)
    out = tmp_path / "disagreements.csv"
    count = write_disagreements_csv([agree, disagree], out)
    assert count == 1
    content = out.read_text(encoding="utf-8")
    assert "owner" in content
    assert content.count("\n") == 2  # header + 1 row


def test_zoho_owner_connector_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        fetch_zoho_owner("c1")


def test_qbo_rep_connector_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        fetch_qbo_rep("c1")


def test_dtools_pm_connector_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        fetch_dtools_pm("c1")
