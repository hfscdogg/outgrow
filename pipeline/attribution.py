"""Deal-based rep attribution.

Zoho's Contact ``Owner`` is a poor signal for whose customer someone is:
contacts default to the owner's account on create/import, while the deal
goes to whoever actually sold the job. Oct 2026 audit: of the customers on
Marshall's 200 most recent completed jobs, only 22 of 104 contacts were
owned by him — 47 sat under the owner and most of the rest under
deactivated users. Contact-owner attribution left a 21-year rep with almost
no pulse pool and put his customers in other reps' pulses.

This module derives ``contact_id -> rep_id`` from the most recent completed
or won deal whose owner is a pulse rep. ``pipeline.customers.build_customers``
prefers it over the contact owner and falls back to the contact owner for
customers with no such deal.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from typing import Any

# A stage counts when the job actually happened: "Closed Won*" variants and
# every "... Complete" milestone (Installation / Finish-Out / Trim-Out /
# Service Call / D&E ...). Lost, abandoned and pre-sale stages never do.
_WON_PREFIX = "closed won"
_COMPLETE_MARKER = "complete"


def is_completed_stage(stage: str | None) -> bool:
    if not stage:
        return False
    s = stage.strip().lower()
    return s.startswith(_WON_PREFIX) or _COMPLETE_MARKER in s


def _lookup_id(value: Any) -> str | None:
    if isinstance(value, Mapping):
        raw = value.get("id")
        return str(raw) if raw is not None else None
    return str(value) if value is not None else None


def _deal_date(deal: Mapping[str, Any]) -> date | None:
    for key in ("Closing_Date", "Created_Time"):
        raw = deal.get(key)
        if not raw:
            continue
        try:
            return datetime.fromisoformat(str(raw)).date()
        except ValueError:
            try:
                return date.fromisoformat(str(raw)[:10])
            except ValueError:
                continue
    return None


def latest_deal_rep_by_contact(
    deals: Iterable[Mapping[str, Any]],
    zoho_user_to_rep_id: Mapping[str, str],
) -> dict[str, str]:
    """Map each contact to the rep on their most recent completed/won deal.

    Only deals owned by a pulse rep (primary ID or alias) count, so a newer
    job sold by a former employee doesn't hide an older one sold by a
    current rep. Undated deals rank below every dated one; ties break on
    deal id so the result is deterministic.
    """
    best: dict[str, tuple[date, str, str]] = {}
    for deal in deals:
        if not is_completed_stage(deal.get("Stage")):
            continue
        owner_id = _lookup_id(deal.get("Owner"))
        rep_id = zoho_user_to_rep_id.get(owner_id) if owner_id else None
        contact_id = _lookup_id(deal.get("Contact_Name"))
        if rep_id is None or contact_id is None:
            continue
        key = (_deal_date(deal) or date.min, str(deal.get("id") or ""), rep_id)
        current = best.get(contact_id)
        if current is None or key[:2] > current[:2]:
            best[contact_id] = key
    return {cid: rep_id for cid, (_, _, rep_id) in best.items()}
