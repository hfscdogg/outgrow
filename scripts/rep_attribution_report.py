"""Preview contact-owner vs deal-owner rep attribution before switching.

Builds the customer pool both ways from the sync caches and prints, per
rep, the pool size and the eligible-today count under each mode, plus how
many customers move between reps. Read-only: no drafting, no email, no
state writes. Run by the ``rep-attribution-report`` workflow (which syncs
first), or locally after a sync::

    uv run python -m sync.zoho_crm && uv run python -m sync.qbo
    uv run python -m scripts.rep_attribution_report

Turn deal attribution on for the daily pulse by setting the repo variable
``OUTGROW_REP_ATTRIBUTION=deal``.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from matching.identity import match, qbo_customer_to_record, zoho_contact_to_record
from pipeline.attribution import latest_deal_rep_by_contact
from pipeline.customers import build_customers
from pipeline.reps import load_reps, zoho_user_to_rep_id_map
from pipeline.suppressions import load_history, recently_pulsed_customer_ids
from ranking.engine import (
    Customer,
    Play,
    RankingConfig,
    load_plays,
    load_ranking_config,
    rank_for_rep_with_diagnostics,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ZOHO_CACHE_DIR = REPO_ROOT / ".cache" / "zoho"
DEFAULT_QBO_CACHE_DIR = REPO_ROOT / ".cache" / "qbo"
UNASSIGNED = "(no rep)"


@dataclass(frozen=True)
class RepCounts:
    rep_id: str
    pool_contact: int
    eligible_contact: int
    pool_deal: int
    eligible_deal: int


@dataclass(frozen=True)
class AttributionReport:
    reps: tuple[RepCounts, ...]
    moves: tuple[tuple[str, str, int], ...]  # (from_rep, to_rep, count), largest first


def _eligible(
    customers: Sequence[Customer], play: Play, cfg: RankingConfig, rep_id: str, today: date
) -> tuple[int, int]:
    _, diag = rank_for_rep_with_diagnostics(customers, play, cfg, rep_id, today)
    return diag.candidates, diag.candidates - sum(diag.rejected.values())


def compare_attribution(
    contact_customers: Sequence[Customer],
    deal_customers: Sequence[Customer],
    *,
    rep_ids: Sequence[str],
    play: Play,
    ranking_cfg: RankingConfig,
    today: date,
) -> AttributionReport:
    """Per-rep pool/eligible counts under both modes, plus the move matrix."""
    rows = []
    for rep_id in rep_ids:
        pool_c, elig_c = _eligible(contact_customers, play, ranking_cfg, rep_id, today)
        pool_d, elig_d = _eligible(deal_customers, play, ranking_cfg, rep_id, today)
        rows.append(RepCounts(rep_id, pool_c, elig_c, pool_d, elig_d))

    before = {c.id: c.rep_id for c in contact_customers}
    after = {c.id: c.rep_id for c in deal_customers}
    moves: Counter[tuple[str, str]] = Counter()
    for cid in before.keys() | after.keys():
        old = before.get(cid, UNASSIGNED)
        new = after.get(cid, UNASSIGNED)
        if old != new:
            moves[(old, new)] += 1
    ordered = tuple(
        (a, b, n) for (a, b), n in sorted(moves.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return AttributionReport(reps=tuple(rows), moves=ordered)


def render_report(report: AttributionReport, *, today: date) -> str:
    """Markdown, so it reads the same in the job log and the step summary."""
    lines = [
        f"## Rep attribution preview ({today.isoformat()})",
        "",
        "Pool = matched customers attributed to the rep; eligible = still in the pool after",
        "today's filters (LTV floor, 90-day suppression, too-recent purchase, ...).",
        "",
        "| Rep | Pool (contact owner) | Pool (deal owner) "
        "| Eligible today (contact) | Eligible today (deal) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for r in report.reps:
        cells = (r.rep_id, r.pool_contact, r.pool_deal, r.eligible_contact, r.eligible_deal)
        lines.append("| " + " | ".join(str(c) for c in cells) + " |")
    lines += ["", "### Customers that change rep", ""]
    if not report.moves:
        lines.append("None.")
    else:
        lines += ["| From | To | Customers |", "| --- | --- | ---: |"]
        lines += [f"| {a} | {b} | {n} |" for a, b, n in report.moves]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zoho-cache-dir", type=Path, default=DEFAULT_ZOHO_CACHE_DIR)
    parser.add_argument("--qbo-cache-dir", type=Path, default=DEFAULT_QBO_CACHE_DIR)
    args = parser.parse_args(argv)

    def read(path: Path) -> list[dict]:
        return json.loads(path.read_text(encoding="utf-8"))

    zoho_contacts = read(args.zoho_cache_dir / "contacts.json")
    zoho_deals = read(args.zoho_cache_dir / "deals.json")
    qbo_customers = read(args.qbo_cache_dir / "customers.json")
    qbo_invoices = read(args.qbo_cache_dir / "invoices.json")

    today = date.today()
    reps = load_reps()
    user_to_rep = zoho_user_to_rep_id_map(reps)
    play = next(p for p in load_plays() if p.enabled)
    ranking_cfg = load_ranking_config()
    recently_pulsed = recently_pulsed_customer_ids(load_history(), today)

    match_result = match(
        [zoho_contact_to_record(z) for z in zoho_contacts],
        [qbo_customer_to_record(q) for q in qbo_customers],
    )
    common = {
        "zoho_contacts": zoho_contacts,
        "qbo_customers": qbo_customers,
        "qbo_invoices": qbo_invoices,
        "match_result": match_result,
        "zoho_user_to_rep_id": user_to_rep,
        "recently_pulsed_ids": recently_pulsed,
    }
    report = compare_attribution(
        build_customers(**common),  # type: ignore[arg-type]
        build_customers(
            **common,  # type: ignore[arg-type]
            deal_rep_by_contact=latest_deal_rep_by_contact(zoho_deals, user_to_rep),
        ),
        rep_ids=[r.profile.rep_id for r in reps],
        play=play,
        ranking_cfg=ranking_cfg,
        today=today,
    )
    text = render_report(report, today=today)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
