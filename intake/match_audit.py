"""Zoho CRM ↔ QuickBooks 3-tier match audit.

Phase 0 dispatch tool. Connector functions (`fetch_zoho_contacts`,
`fetch_qbo_customers`) raise `NotImplementedError` until production
credentials land. The scoring core is pure and testable.
"""

from __future__ import annotations

import argparse
import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

AUTO_ACCEPT_THRESHOLD = 0.85
TIER1_SCORE = 1.0
TIER2_SCORE = 0.95
NAME_WEIGHT = 0.6
ADDRESS_WEIGHT = 0.4


@dataclass(frozen=True)
class ContactRecord:
    record_id: str
    name: str
    address: str
    email: str | None = None
    phone_e164: str | None = None


@dataclass(frozen=True)
class MatchScore:
    zoho_id: str
    qbo_id: str
    tier: int
    score: float
    reason: str


def _norm_email(value: str | None) -> str | None:
    return value.strip().lower() if value else None


def _norm_phone(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    return cleaned if cleaned.startswith("+") else None


def score_pair(zoho: ContactRecord, qbo: ContactRecord) -> MatchScore:
    """Score a single Zoho/QBO pair through three tiers."""
    z_email = _norm_email(zoho.email)
    q_email = _norm_email(qbo.email)
    if z_email and q_email and z_email == q_email:
        return MatchScore(zoho.record_id, qbo.record_id, 1, TIER1_SCORE, "email exact")

    z_phone = _norm_phone(zoho.phone_e164)
    q_phone = _norm_phone(qbo.phone_e164)
    if z_phone and q_phone and z_phone == q_phone:
        return MatchScore(zoho.record_id, qbo.record_id, 2, TIER2_SCORE, "phone E.164 exact")

    name_score = fuzz.token_set_ratio(zoho.name, qbo.name) / 100.0
    addr_score = fuzz.token_set_ratio(zoho.address, qbo.address) / 100.0
    combined = NAME_WEIGHT * name_score + ADDRESS_WEIGHT * addr_score
    return MatchScore(zoho.record_id, qbo.record_id, 3, combined, "name+address fuzzy")


def is_ambiguous(score: MatchScore) -> bool:
    return score.score < AUTO_ACCEPT_THRESHOLD


def filter_ambiguous(scores: Iterable[MatchScore]) -> list[MatchScore]:
    return [s for s in scores if is_ambiguous(s)]


def write_ambiguous_csv(scores: Iterable[MatchScore], path: Path) -> int:
    """Write only ambiguous (<0.85) pairs to CSV. Returns row count written."""
    ambiguous = filter_ambiguous(scores)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["zoho_id", "qbo_id", "tier", "score", "reason", "verdict"])
        for s in ambiguous:
            writer.writerow([s.zoho_id, s.qbo_id, s.tier, f"{s.score:.4f}", s.reason, ""])
    return len(ambiguous)


def fetch_zoho_contacts(sample: int) -> list[ContactRecord]:  # pragma: no cover
    raise NotImplementedError(
        "Zoho CRM connector lands in Phase 1 once production read-only tokens are minted."
    )


def fetch_qbo_customers(sample: int) -> list[ContactRecord]:  # pragma: no cover
    raise NotImplementedError(
        "QuickBooks connector lands in Phase 1 once sandbox credentials are wired up."
    )


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Zoho↔QBO 3-tier match audit")
    parser.add_argument("--sample", type=int, default=200)
    parser.add_argument("--out", type=Path, default=Path("match_audit.csv"))
    args = parser.parse_args(argv)

    zoho = fetch_zoho_contacts(args.sample)
    qbo = fetch_qbo_customers(args.sample)
    scores = [score_pair(z, q) for z, q in zip(zoho, qbo, strict=False)]
    written = write_ambiguous_csv(scores, args.out)
    print(f"Wrote {written} ambiguous rows to {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
