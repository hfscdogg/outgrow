"""Rep-match triangulation: Zoho contact owner vs QBO sales rep vs D-Tools PM.

Phase 0 dispatch tool. Connector functions raise `NotImplementedError`
until production credentials land. The disagreement detector is pure.
Authoritative `rep_id` for each disagreement row is set by the owner
during the Phase 0 audit and written back into the customer record.
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

MIN_SOURCES_FOR_DISAGREEMENT = 2


@dataclass(frozen=True)
class TriangulationRow:
    customer_id: str
    customer_name: str
    zoho_owner: str | None
    qbo_rep: str | None
    dtools_pm: str | None


def has_disagreement(row: TriangulationRow) -> bool:
    """A row is a disagreement when ≥2 sources are present and not all agree.

    A row with only one populated source is *insufficient signal*, not a
    disagreement; the owner reviews insufficient-signal rows out-of-band.
    """
    sources = [s for s in (row.zoho_owner, row.qbo_rep, row.dtools_pm) if s]
    if len(sources) < MIN_SOURCES_FOR_DISAGREEMENT:
        return False
    return len({s.strip().lower() for s in sources}) > 1


def filter_disagreements(rows: Iterable[TriangulationRow]) -> list[TriangulationRow]:
    return [r for r in rows if has_disagreement(r)]


def write_disagreements_csv(rows: Iterable[TriangulationRow], path: Path) -> int:
    """Write only disagreement rows to CSV. Returns row count written."""
    disagreements = filter_disagreements(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "customer_id",
                "customer_name",
                "zoho_owner",
                "qbo_rep",
                "dtools_pm",
                "authoritative_rep",
            ]
        )
        for r in disagreements:
            writer.writerow(
                [
                    r.customer_id,
                    r.customer_name,
                    r.zoho_owner or "",
                    r.qbo_rep or "",
                    r.dtools_pm or "",
                    "",
                ]
            )
    return len(disagreements)


def fetch_zoho_owner(customer_id: str) -> str | None:  # pragma: no cover
    raise NotImplementedError("Zoho CRM connector lands in Phase 1.")


def fetch_qbo_rep(customer_id: str) -> str | None:  # pragma: no cover
    raise NotImplementedError("QuickBooks connector lands in Phase 1.")


def fetch_dtools_pm(customer_id: str) -> str | None:  # pragma: no cover
    raise NotImplementedError("D-Tools connector lands in Phase 1.")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Rep-match triangulation audit")
    parser.add_argument("--sample", type=int, default=100)
    parser.add_argument("--out", type=Path, default=Path("rep_disagreements.csv"))
    args = parser.parse_args(argv)
    raise NotImplementedError(
        "Triangulation runner needs Zoho/QBO/D-Tools credentials. "
        f"(target sample={args.sample}, out={args.out})"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
