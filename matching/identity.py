"""Production 3-tier Zoho ↔ QBO identity matcher.

Reads the cached sync output written by ``sync/zoho_crm.py`` and
``sync/qbo.py``, normalizes each side into a common ``ContactRecord``,
and runs a 3-tier match per ``docs/plan.md``::

  Tier 1 — email exact (lowercased)               -> auto-accept
  Tier 2 — E.164 phone exact                      -> auto-accept
  Tier 3 — fuzzy name+address (rapidfuzz)         -> auto-accept ≥ 0.85
                                                     review queue otherwise

Reuses the scoring core from ``intake/match_audit.py`` (Phase 0
dispatch tool) — same ``ContactRecord`` shape, same ``score_pair``
math — so the production join uses the audit tool's verdicts as
ground truth.

Pure functions only — no I/O beyond the optional cache loaders, no
sockets. The orchestrator's cache→Customer wiring is the next-PR
boundary; this module just produces the ``zoho_id ↔ qbo_id`` join
table with confidence scores.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from intake.match_audit import (
    AUTO_ACCEPT_THRESHOLD,
    ContactRecord,
    MatchScore,
    score_pair,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ZOHO_CACHE = REPO_ROOT / ".cache" / "zoho" / "contacts.json"
DEFAULT_QBO_CACHE = REPO_ROOT / ".cache" / "qbo" / "customers.json"

US_TEN_DIGIT = 10
US_ELEVEN_DIGIT = 11

TIER_EMAIL = 1
TIER_PHONE = 2
TIER_FUZZY = 3

_DIGITS_RE = re.compile(r"\D")
_QboIndex = tuple[dict[str, ContactRecord], dict[str, ContactRecord]]


def _normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


def _normalize_e164_us(raw: str | None) -> str | None:
    """Best-effort US-default E.164 normalizer.

    Returns ``None`` for inputs that can't be confidently normalized —
    those won't tier-2 match (which is correct: a malformed phone
    shouldn't merge two records).
    """
    if not raw:
        return None
    digits = _DIGITS_RE.sub("", raw)
    if len(digits) == US_TEN_DIGIT:
        return f"+1{digits}"
    if len(digits) == US_ELEVEN_DIGIT and digits.startswith("1"):
        return f"+{digits}"
    if raw.lstrip().startswith("+") and len(digits) >= US_TEN_DIGIT:
        return f"+{digits}"
    return None


def zoho_contact_to_record(payload: Mapping[str, Any]) -> ContactRecord:
    """Project a ``.cache/zoho/contacts.json`` row into ``ContactRecord``."""
    full_name = payload.get("Full_Name") or ""
    if not full_name:
        first = payload.get("First_Name") or ""
        last = payload.get("Last_Name") or ""
        full_name = f"{first} {last}".strip()
    address = ", ".join(
        part
        for part in (
            payload.get("Mailing_Street"),
            payload.get("Mailing_City"),
            payload.get("Mailing_State"),
            payload.get("Mailing_Zip"),
        )
        if part
    )
    return ContactRecord(
        record_id=str(payload["id"]),
        name=full_name.strip(),
        address=address,
        email=_normalize_email(payload.get("Email")),
        phone_e164=_normalize_e164_us(payload.get("Phone") or payload.get("Mobile")),
    )


def qbo_customer_to_record(payload: Mapping[str, Any]) -> ContactRecord:
    """Project a ``.cache/qbo/customers.json`` row into ``ContactRecord``."""
    addr = payload.get("BillAddr") or {}
    address = ", ".join(
        part
        for part in (
            addr.get("Line1"),
            addr.get("City"),
            addr.get("CountrySubDivisionCode"),
            addr.get("PostalCode"),
        )
        if part
    )
    primary_email = (payload.get("PrimaryEmailAddr") or {}).get("Address")
    primary_phone = (payload.get("PrimaryPhone") or {}).get("FreeFormNumber")
    return ContactRecord(
        record_id=str(payload["Id"]),
        name=str(payload.get("DisplayName") or "").strip(),
        address=address,
        email=_normalize_email(primary_email),
        phone_e164=_normalize_e164_us(primary_phone),
    )


@dataclass(frozen=True)
class MatchResult:
    """Output of a full match run.

    * ``auto_matched`` — pairs that auto-flow to the Growable queue
      (Tier 1, Tier 2, or Tier 3 with score ≥ 0.85).
    * ``review_queue`` — Tier 3 pairs below 0.85; owner triages these
      via ``intake/match_audit.py`` style worksheet.
    * ``unmatched_zoho`` / ``unmatched_qbo`` — record IDs with no
      candidate at all (data-quality bucket; refresh contact info,
      then rerun).
    """

    auto_matched: tuple[MatchScore, ...]
    review_queue: tuple[MatchScore, ...]
    unmatched_zoho: tuple[str, ...]
    unmatched_qbo: tuple[str, ...]


def _build_index(records: Iterable[ContactRecord]) -> _QboIndex:
    by_email: dict[str, ContactRecord] = {}
    by_phone: dict[str, ContactRecord] = {}
    for r in records:
        if r.email:
            by_email.setdefault(r.email, r)
        if r.phone_e164:
            by_phone.setdefault(r.phone_e164, r)
    return by_email, by_phone


def _exact_pass(
    zoho_list: list[ContactRecord],
    index: dict[str, ContactRecord],
    key: str,
    matched_zoho: set[str],
    matched_qbo: set[str],
) -> list[MatchScore]:
    """Tier 1 / Tier 2 helper: exact-key lookup against a QBO index."""
    hits: list[MatchScore] = []
    for z in zoho_list:
        if z.record_id in matched_zoho:
            continue
        z_key = getattr(z, key)
        if not z_key:
            continue
        q = index.get(z_key)
        if q is None or q.record_id in matched_qbo:
            continue
        hits.append(score_pair(z, q))
        matched_zoho.add(z.record_id)
        matched_qbo.add(q.record_id)
    return hits


def _fuzzy_pass(
    zoho_list: list[ContactRecord],
    qbo_list: list[ContactRecord],
    matched_zoho: set[str],
    matched_qbo: set[str],
) -> tuple[list[MatchScore], list[MatchScore]]:
    """Tier 3 helper: pick best fuzzy candidate per remaining Zoho row."""
    auto: list[MatchScore] = []
    review: list[MatchScore] = []
    remaining_qbo = [q for q in qbo_list if q.record_id not in matched_qbo]
    for z in zoho_list:
        if z.record_id in matched_zoho:
            continue
        best: MatchScore | None = None
        for q in remaining_qbo:
            if q.record_id in matched_qbo:
                continue
            scored = score_pair(z, q)
            if best is None or scored.score > best.score:
                best = scored
        if best is None:
            continue
        if best.score >= AUTO_ACCEPT_THRESHOLD:
            auto.append(best)
        else:
            review.append(best)
        matched_zoho.add(z.record_id)
        matched_qbo.add(best.qbo_id)
    return auto, review


def match(
    zoho: Iterable[ContactRecord],
    qbo: Iterable[ContactRecord],
) -> MatchResult:
    """Run the 3-tier match.

    Tier 1+2 are O(n) via dict lookup against the QBO index. Tier 3
    fuzzy fallback is O(n × m_remaining); fine for pilot-scale (a few
    hundred customers per rep).
    """
    zoho_list = list(zoho)
    qbo_list = list(qbo)
    qbo_by_email, qbo_by_phone = _build_index(qbo_list)

    matched_zoho: set[str] = set()
    matched_qbo: set[str] = set()

    auto = _exact_pass(zoho_list, qbo_by_email, "email", matched_zoho, matched_qbo)
    auto += _exact_pass(zoho_list, qbo_by_phone, "phone_e164", matched_zoho, matched_qbo)
    fuzzy_auto, review = _fuzzy_pass(zoho_list, qbo_list, matched_zoho, matched_qbo)
    auto += fuzzy_auto

    return MatchResult(
        auto_matched=tuple(auto),
        review_queue=tuple(review),
        unmatched_zoho=tuple(z.record_id for z in zoho_list if z.record_id not in matched_zoho),
        unmatched_qbo=tuple(q.record_id for q in qbo_list if q.record_id not in matched_qbo),
    )


def load_zoho_contacts(path: Path = DEFAULT_ZOHO_CACHE) -> list[ContactRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [zoho_contact_to_record(p) for p in payload]


def load_qbo_customers(path: Path = DEFAULT_QBO_CACHE) -> list[ContactRecord]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [qbo_customer_to_record(p) for p in payload]


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    """CLI: read both caches, run match, print summary counts."""
    parser = argparse.ArgumentParser(description="Production 3-tier Zoho↔QBO matcher")
    parser.add_argument("--zoho-cache", type=Path, default=DEFAULT_ZOHO_CACHE)
    parser.add_argument("--qbo-cache", type=Path, default=DEFAULT_QBO_CACHE)
    args = parser.parse_args(argv)

    zoho = load_zoho_contacts(args.zoho_cache)
    qbo = load_qbo_customers(args.qbo_cache)
    result = match(zoho, qbo)
    print(
        json.dumps(
            {
                "auto_matched": len(result.auto_matched),
                "review_queue": len(result.review_queue),
                "unmatched_zoho": len(result.unmatched_zoho),
                "unmatched_qbo": len(result.unmatched_qbo),
                "tier_breakdown": {
                    "tier_1_email": sum(1 for s in result.auto_matched if s.tier == TIER_EMAIL),
                    "tier_2_phone": sum(1 for s in result.auto_matched if s.tier == TIER_PHONE),
                    "tier_3_fuzzy_auto": sum(
                        1 for s in result.auto_matched if s.tier == TIER_FUZZY
                    ),
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
