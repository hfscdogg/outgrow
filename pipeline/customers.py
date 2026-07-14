"""Customer record + briefing assembly.

Bridges the matching layer (`matching/identity.py`) and the ranking
engine (`ranking/engine.py`): takes raw cache JSON + match results and
produces typed `Customer` records, with LTV / dormancy / legacy-bonus
signals derived from QBO invoice history. Also produces the per-
customer briefings the drafter and mailer consume.

Pure functions only — no I/O, no sockets. The orchestrator in
`scripts/daily_pulse.py` wires this between the sync output and
`run_one_pulse(...)` (next-PR concern).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

from drafting.generator import CustomerBriefing as DraftBriefing
from intake.match_audit import MatchScore
from matching.identity import MatchResult, _normalize_e164_us, _normalize_email
from pulse.mailer import CustomerBriefing as MailBriefing
from ranking.engine import Customer

CENTS_PER_DOLLAR = 100


# --- date / money helpers ----------------------------------------------------


def _parse_iso_date(raw: str | None) -> date | None:
    """Parse an ISO 8601 datetime/date string; return its date part or None."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None


def _earliest(*candidates: date | None) -> date | None:
    valid = [c for c in candidates if c is not None]
    return min(valid) if valid else None


# --- JSON projectors ---------------------------------------------------------


def _zoho_owner_id(payload: Mapping[str, Any]) -> str | None:
    owner = payload.get("Owner")
    if owner is None:
        return None
    if isinstance(owner, Mapping):
        oid = owner.get("id")
        return str(oid) if oid is not None else None
    return str(owner)


def _zoho_created_date(payload: Mapping[str, Any]) -> date | None:
    return _parse_iso_date(payload.get("Created_Time"))


def _qbo_created_date(payload: Mapping[str, Any]) -> date | None:
    meta = payload.get("MetaData") or {}
    return _parse_iso_date(meta.get("CreateTime"))


def _invoice_customer_id(invoice: Mapping[str, Any]) -> str | None:
    ref = invoice.get("CustomerRef") or {}
    val = ref.get("value")
    return str(val) if val is not None else None


def _invoice_total_cents(invoice: Mapping[str, Any]) -> int:
    raw = invoice.get("TotalAmt")
    if raw is None:
        return 0
    try:
        return int(round(float(raw) * CENTS_PER_DOLLAR))
    except (TypeError, ValueError):
        return 0


def _invoice_txn_date(invoice: Mapping[str, Any]) -> date | None:
    return _parse_iso_date(invoice.get("TxnDate"))


# --- aggregation -------------------------------------------------------------


def _aggregate_invoices_for(
    qbo_id: str,
    invoices: Iterable[Mapping[str, Any]],
) -> tuple[int, date | None, date | None]:
    """Return (ltv_cents, last_txn_date, first_txn_date) for a customer."""
    total_cents = 0
    last_dt: date | None = None
    first_dt: date | None = None
    for inv in invoices:
        if _invoice_customer_id(inv) != qbo_id:
            continue
        total_cents += _invoice_total_cents(inv)
        d = _invoice_txn_date(inv)
        if d is None:
            continue
        if last_dt is None or d > last_dt:
            last_dt = d
        if first_dt is None or d < first_dt:
            first_dt = d
    return total_cents, last_dt, first_dt


# --- public: customer record builder ----------------------------------------


def build_customers(
    *,
    zoho_contacts: Sequence[Mapping[str, Any]],
    qbo_customers: Sequence[Mapping[str, Any]],
    qbo_invoices: Sequence[Mapping[str, Any]],
    match_result: MatchResult,
    zoho_user_to_rep_id: Mapping[str, str],
    recently_pulsed_ids: frozenset[str] = frozenset(),
) -> list[Customer]:
    """Project auto-matched pairs into ranked-engine ``Customer`` records.

    Records are skipped when:
    * the matched Zoho or QBO row isn't in its source list (data drift)
    * the Zoho contact has no Owner field (unassigned in Zoho)
    * the Zoho Owner isn't mapped to an internal rep (unknown rep, owner
      triages by adding a ``zoho_user_id:`` to a rep YAML)

    ``recently_pulsed_ids`` are customer IDs the engine has emailed in the
    last ``DEFAULT_WINDOW_DAYS`` (see ``pipeline/suppressions.py``). They
    surface as ``suppressed=True`` so the ranking layer's eligibility
    filter rejects them, and the suppression fans out across duplicate
    CRM records: the pulsed contact itself (``recently_pulsed``), any
    contact matched to the same QBO account
    (``recently_pulsed_same_account``), and any contact sharing its email
    (``recently_pulsed_same_email``).

    Richer suppression sources (open Desk tickets, active Zoho deals,
    opted-out) arrive with Phase 2's suppressions table and use the same
    ``suppressed``/``suppression_reason`` machinery.
    """
    zoho_by_id = {str(c["id"]): c for c in zoho_contacts}
    qbo_by_id = {str(c["Id"]): c for c in qbo_customers}

    # Cross-contact dedup: a recently-pulsed customer must stay suppressed
    # even when the CRM carries duplicate contact records for the same
    # human. Suppressing by zoho_id alone let "Matthew and Michelle Dunham"
    # get pulsed on Jul 14 2026, five days after its twin record "Matt
    # Dunham" (same email, same matched QBO account). Two extra suppression
    # keys are derived from the recently-pulsed contacts:
    #   * the QBO account their pulse matched to — two contacts matched to
    #     the same QBO customer are the same account;
    #   * their email — two contacts sharing an address reach one inbox.
    recent_qbo_ids = {
        ms.qbo_id for ms in match_result.auto_matched if ms.zoho_id in recently_pulsed_ids
    }
    recent_emails: set[str] = set()
    for zid in recently_pulsed_ids:
        email = _normalize_email((zoho_by_id.get(zid) or {}).get("Email"))
        if email:
            recent_emails.add(email)

    def _suppression_reason(ms: MatchScore, zoho: Mapping[str, Any]) -> str | None:
        if ms.zoho_id in recently_pulsed_ids:
            return "recently_pulsed"
        if ms.qbo_id in recent_qbo_ids:
            return "recently_pulsed_same_account"
        email = _normalize_email(zoho.get("Email"))
        if email is not None and email in recent_emails:
            return "recently_pulsed_same_email"
        return None

    customers: list[Customer] = []
    for ms in match_result.auto_matched:
        zoho = zoho_by_id.get(ms.zoho_id)
        qbo = qbo_by_id.get(ms.qbo_id)
        if zoho is None or qbo is None:
            continue
        owner_id = _zoho_owner_id(zoho)
        if owner_id is None:
            continue
        rep_id = zoho_user_to_rep_id.get(owner_id)
        if rep_id is None:
            continue

        ltv_cents, last_purchase, first_invoice = _aggregate_invoices_for(
            str(qbo["Id"]), qbo_invoices
        )
        first_known = _earliest(
            _zoho_created_date(zoho),
            _qbo_created_date(qbo),
            first_invoice,
        )
        suppression_reason = _suppression_reason(ms, zoho)
        customers.append(
            Customer(
                id=ms.zoho_id,
                rep_id=rep_id,
                ltv_cents=ltv_cents,
                last_purchase_at=last_purchase,
                first_known_contact_at=first_known,
                rep_match_confidence=ms.score,
                first_invoice_at=first_invoice,
                suppressed=suppression_reason is not None,
                suppression_reason=suppression_reason,
            )
        )
    return customers


# --- public: briefing assembly ----------------------------------------------


def _format_dollars(cents: int) -> str:
    """Render cents as ``$N,NNN`` (no decimals; pulse readability beats penny precision)."""
    return f"${cents / CENTS_PER_DOLLAR:,.0f}"


def _format_month(d: date | None) -> str | None:
    return d.strftime("%b %Y") if d else None


def _legacy_disclosure(first_known: date | None, qbo_horizon: date) -> str:
    if first_known is not None and first_known < qbo_horizon:
        return f" (partial since {qbo_horizon.year})"
    return ""


_DAYS_PER_YEAR = 365
_DAYS_PER_MONTH = 30


def _humanize_ago(last: date | None, today: date) -> str | None:
    """Render ``today - last`` as ``Ny Mmo ago`` / ``N months ago`` / ``today``.

    Approximations: 365 days per year, 30 per month. Off by ~5 days/yr at
    worst — fine for a subhead ("4y 3mo ago" feels right whether it's
    really 1,538 or 1,555 days). Returns ``None`` when ``last`` is missing.
    """
    if last is None:
        return None
    days = (today - last).days
    if days <= 0:
        return "today"
    if days < _DAYS_PER_MONTH:
        return "this month"
    if days < _DAYS_PER_YEAR:
        months = days // _DAYS_PER_MONTH
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // _DAYS_PER_YEAR
    remaining_months = (days % _DAYS_PER_YEAR) // _DAYS_PER_MONTH
    if remaining_months == 0:
        return f"{years}y ago"
    return f"{years}y {remaining_months}mo ago"


def _customer_display_name(zoho_contact: Mapping[str, Any]) -> str:
    full = zoho_contact.get("Full_Name")
    if full:
        return str(full).strip()
    first = zoho_contact.get("First_Name") or ""
    last = zoho_contact.get("Last_Name") or ""
    combined = f"{first} {last}".strip()
    return combined or "Customer"


def _stripped(zoho_contact: Mapping[str, Any], key: str) -> str | None:
    """Pull a Zoho string field, stripped; return None if missing or empty."""
    raw = zoho_contact.get(key)
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def build_briefing(
    customer: Customer,
    *,
    zoho_contact: Mapping[str, Any],
    qbo_horizon: date,
    today: date | None = None,
) -> tuple[DraftBriefing, MailBriefing]:
    """Render briefings for the drafter and the mailer.

    ``DraftBriefing.source_dollar_amounts`` carries the LTV string so the
    judge's hallucination check doesn't false-flag the model legitimately
    reciting that figure back. Other rendered amounts (per-invoice totals,
    Phase 2 deal context) get added to that set as they're folded in.
    """
    name = _customer_display_name(zoho_contact)
    disclosure = _legacy_disclosure(customer.first_known_contact_at, qbo_horizon)
    ltv_label = _format_dollars(customer.ltv_cents) + disclosure
    last_purchase_label = _format_month(customer.last_purchase_at)
    first_invoice_label = _format_month(customer.first_invoice_at)
    mobile = _stripped(zoho_contact, "Mobile")
    phone = _stripped(zoho_contact, "Phone")
    email = _stripped(zoho_contact, "Email")

    # Keys are presentation labels (Title Case); the mailer renders them
    # verbatim. Earlier snake_case keys were dropped in favour of the
    # human-readable form per the email-redesign PR.
    rows: list[tuple[str, str]] = [("Lifetime spend", ltv_label)]
    if last_purchase_label:
        rows.append(("Last purchase", last_purchase_label))
    if first_invoice_label:
        rows.append(("First invoice", first_invoice_label))
    if mobile:
        rows.append(("Mobile", mobile))
    if phone:
        rows.append(("Phone", phone))
    if email:
        rows.append(("Email", email))

    draft = DraftBriefing(
        name=name,
        last_purchase_label=last_purchase_label,
        lifetime_spend_label=ltv_label,
        last_install_label=None,
        recent_tickets_label=None,
        sanitized_notes="",
        source_dollar_amounts=frozenset({_format_dollars(customer.ltv_cents)}),
    )
    dormancy_label = _humanize_ago(customer.last_purchase_at, today) if today else None
    first_name = _stripped(zoho_contact, "First_Name")
    # Mobile only, no Phone fallback — Phone is often a landline/office line
    # that can't receive SMS. Un-normalizable numbers -> None -> the mailer
    # omits the "Text customer" button rather than emitting a dead sms: link.
    mobile_e164 = _normalize_e164_us(mobile)
    mail = MailBriefing(
        name=name,
        rows=tuple(rows),
        dormancy_label=dormancy_label,
        email=email,
        first_name=first_name,
        mobile_e164=mobile_e164,
    )
    return draft, mail
