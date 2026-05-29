"""Customer ranking engine.

Scores every eligible customer for a (rep, play) pair using the formula
documented in ``docs/plan.md``::

    final_score = play.base_weight
                * play.seasonal_weights[current_month]
                * log10(LTV_cents + legacy_bonus_cents)
                * dormancy_factor(dormancy_days)
                * rep_match_confidence
                - suppression_penalty

Pure functions only — no I/O, no live data fetch, no socket use. The
upstream sync + matching layers prepare ``Customer`` records; the play-
rotation layer (``domain/rotation.py`` in the plan) decides which play
to score against first. This module just does scoring + sorting for one
chosen play and returns ``ScoredCandidate`` rows with a transparent
``components`` breakdown so any pick is auditable
(``cli queue explain``).

Eligibility filters short-circuit before scoring:

* disabled play
* hard-suppressed customer (open ticket, active deal, opted out)
* ``rep_match_confidence`` below the three-way-disagreement floor
* outside the play's ``min/max_days_since_install`` window
* dormancy below ``too_recent_days`` (the rep would feel they're
  pestering an active customer)
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAYS_PATH = REPO_ROOT / "config" / "plays.yaml"
DEFAULT_RANKING_PATH = REPO_ROOT / "config" / "ranking.yaml"

MONTH_KEYS: tuple[str, ...] = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)


@dataclass(frozen=True)
class Customer:
    id: str
    rep_id: str
    ltv_cents: int
    last_purchase_at: date | None
    first_known_contact_at: date | None
    rep_match_confidence: float
    suppressed: bool = False
    suppression_reason: str | None = None
    last_install_completed_at: date | None = None
    # Distinct from first_known_contact_at (the earliest signal across Zoho
    # record-creation, QBO record-creation, and first QBO invoice — used to
    # decide legacy_bonus). The display layer wants the QBO-specific first
    # invoice for the "First invoice" row, since the Zoho-record-creation
    # value is often misleading (record migrated long after the relationship
    # began). None when the matched QBO customer has no invoices.
    first_invoice_at: date | None = None


@dataclass(frozen=True)
class Play:
    key: str
    base_weight: float
    enabled: bool
    min_days_since_install: int | None
    max_days_since_install: int | None
    seasonal_weights: dict[str, float]

    def seasonal_weight_for(self, month_index: int) -> float:
        """Return the weight for ``month_index`` (1=Jan, 12=Dec); 1.0 if missing."""
        return float(self.seasonal_weights.get(MONTH_KEYS[month_index - 1], 1.0))


@dataclass(frozen=True)
class RankingConfig:
    qbo_horizon: date
    legacy_bonus_cents_per_year: int
    too_recent_days: int
    ramp_up_days: int
    sweet_spot_end_days: int
    decay_floor_days: int
    cold_floor_factor: float
    min_rep_match_confidence: float
    suppression_penalty: float
    # Floor on raw QBO-visible LTV (cents). Filters one-off small-job
    # customers out before scoring. Compared against ``customer.ltv_cents``
    # only — does NOT include the legacy_bonus uplift, since this gate is
    # about real visible spend, not ranking math.
    min_ltv_cents: int = 0


@dataclass(frozen=True)
class ScoredCandidate:
    customer_id: str
    play_key: str
    score: float
    components: dict[str, float]


@dataclass(frozen=True)
class EligibilityDiagnostics:
    """Per-rep funnel snapshot — how many of this rep's customers reached scoring."""

    candidates: int
    rejected: dict[str, int]


# Ordered eligibility checks; the first failing one is the reason returned by
# eligibility_reason(). Kept in one place so the diagnostics counter and the
# is_eligible boolean can never drift.
ELIGIBILITY_REASONS: tuple[str, ...] = (
    "play_disabled",
    "suppressed",
    "low_confidence",
    "low_ltv",
    "install_window",
    "no_purchase_date",
    "too_recent",
)


def load_plays(path: Path = DEFAULT_PLAYS_PATH) -> list[Play]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    plays: list[Play] = []
    for entry in payload.get("plays") or []:
        weights_raw = entry.get("seasonal_weights") or {}
        plays.append(
            Play(
                key=str(entry["key"]),
                base_weight=float(entry.get("base_weight", 1.0)),
                enabled=bool(entry.get("enabled", True)),
                min_days_since_install=entry.get("min_days_since_install"),
                max_days_since_install=entry.get("max_days_since_install"),
                seasonal_weights={k: float(v) for k, v in weights_raw.items()},
            )
        )
    return plays


def load_ranking_config(path: Path = DEFAULT_RANKING_PATH) -> RankingConfig:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    legacy = payload.get("legacy_bonus") or {}
    dormancy = payload.get("dormancy") or {}
    rep_match = payload.get("rep_match") or {}
    eligibility = payload.get("eligibility") or {}
    return RankingConfig(
        qbo_horizon=date.fromisoformat(legacy["qbo_horizon_iso"]),
        legacy_bonus_cents_per_year=int(legacy["bonus_cents_per_year_before_horizon"]),
        too_recent_days=int(dormancy["too_recent_days"]),
        ramp_up_days=int(dormancy["ramp_up_days"]),
        sweet_spot_end_days=int(dormancy["sweet_spot_end_days"]),
        decay_floor_days=int(dormancy["decay_floor_days"]),
        cold_floor_factor=float(dormancy["cold_floor_factor"]),
        min_rep_match_confidence=float(rep_match["min_confidence"]),
        suppression_penalty=float(payload.get("suppression_penalty", 0.0)),
        min_ltv_cents=int(eligibility.get("min_lifetime_spend_cents", 0)),
    )


def dormancy_days(customer: Customer, today: date) -> int | None:
    if customer.last_purchase_at is None:
        return None
    return max(0, (today - customer.last_purchase_at).days)


def dormancy_factor(days: int, cfg: RankingConfig) -> float:
    """Piecewise sweet-spot curve; see config/ranking.yaml for the shape."""
    if days < cfg.too_recent_days:
        return 0.0
    if days < cfg.ramp_up_days:
        span = cfg.ramp_up_days - cfg.too_recent_days
        return (days - cfg.too_recent_days) / span if span > 0 else 1.0
    if days <= cfg.sweet_spot_end_days:
        return 1.0
    if days < cfg.decay_floor_days:
        span = cfg.decay_floor_days - cfg.sweet_spot_end_days
        progress = (days - cfg.sweet_spot_end_days) / span if span > 0 else 1.0
        return 1.0 - progress * (1.0 - cfg.cold_floor_factor)
    return cfg.cold_floor_factor


def legacy_bonus_cents(customer: Customer, cfg: RankingConfig) -> int:
    """Bonus added to LTV for customers older than the QBO horizon."""
    if customer.first_known_contact_at is None:
        return 0
    years = cfg.qbo_horizon.year - customer.first_known_contact_at.year
    return max(0, years) * cfg.legacy_bonus_cents_per_year


def is_in_install_window(customer: Customer, play: Play, today: date) -> bool:
    if customer.last_install_completed_at is None:
        return play.min_days_since_install is None
    days = max(0, (today - customer.last_install_completed_at).days)
    if play.min_days_since_install is not None and days < play.min_days_since_install:
        return False
    if play.max_days_since_install is not None and days > play.max_days_since_install:  # noqa: SIM103
        return False
    return True


def eligibility_reason(  # noqa: PLR0911 -- guard-chain; one return per filter is the point
    customer: Customer, play: Play, cfg: RankingConfig, today: date
) -> str | None:
    """Return ``None`` if eligible, else a key from ``ELIGIBILITY_REASONS``."""
    if not play.enabled:
        return "play_disabled"
    if customer.suppressed:
        return "suppressed"
    if customer.rep_match_confidence < cfg.min_rep_match_confidence:
        return "low_confidence"
    if customer.ltv_cents < cfg.min_ltv_cents:
        return "low_ltv"
    if not is_in_install_window(customer, play, today):
        return "install_window"
    days = dormancy_days(customer, today)
    if days is None:
        return "no_purchase_date"
    if days < cfg.too_recent_days:
        return "too_recent"
    return None


def is_eligible(customer: Customer, play: Play, cfg: RankingConfig, today: date) -> bool:
    return eligibility_reason(customer, play, cfg, today) is None


def score(customer: Customer, play: Play, cfg: RankingConfig, today: date) -> ScoredCandidate:
    """Score a single (customer, play) pair. Caller filters with ``is_eligible`` first."""
    seasonal = play.seasonal_weight_for(today.month)
    bonus = legacy_bonus_cents(customer, cfg)
    ltv_term = math.log10(max(1, customer.ltv_cents + bonus))
    dorm_days = dormancy_days(customer, today) or 0
    dorm = dormancy_factor(dorm_days, cfg)
    raw = play.base_weight * seasonal * ltv_term * dorm * customer.rep_match_confidence
    final = raw - cfg.suppression_penalty
    return ScoredCandidate(
        customer_id=customer.id,
        play_key=play.key,
        score=final,
        components={
            "base_weight": play.base_weight,
            "seasonal": seasonal,
            "ltv_log10": ltv_term,
            "legacy_bonus_cents": float(bonus),
            "dormancy_factor": dorm,
            "rep_match_confidence": customer.rep_match_confidence,
            "suppression_penalty": cfg.suppression_penalty,
        },
    )


def rank_for_rep_with_diagnostics(
    customers: Iterable[Customer],
    play: Play,
    cfg: RankingConfig,
    rep_id: str,
    today: date,
    *,
    top_n: int | None = None,
) -> tuple[list[ScoredCandidate], EligibilityDiagnostics]:
    """Same as ``rank_for_rep`` but also returns a per-reason rejection tally.

    The tally counts only customers whose ``rep_id`` matches; the funnel
    upstream of that (Zoho owner mapping, suppressions, etc.) lives in
    ``pipeline/customers.py`` and isn't this function's concern.
    """
    rep_customers = [c for c in customers if c.rep_id == rep_id]
    rejected: dict[str, int] = {}
    eligible: list[Customer] = []
    for c in rep_customers:
        reason = eligibility_reason(c, play, cfg, today)
        if reason is None:
            eligible.append(c)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1
    scored = [score(c, play, cfg, today) for c in eligible]
    scored.sort(key=lambda s: s.score, reverse=True)
    truncated = scored if top_n is None else scored[:top_n]
    return truncated, EligibilityDiagnostics(candidates=len(rep_customers), rejected=rejected)


def rank_for_rep(
    customers: Iterable[Customer],
    play: Play,
    cfg: RankingConfig,
    rep_id: str,
    today: date,
    *,
    top_n: int | None = None,
) -> list[ScoredCandidate]:
    """Eligible-customers-only, score, sort descending, optionally truncate."""
    scored, _ = rank_for_rep_with_diagnostics(customers, play, cfg, rep_id, today, top_n=top_n)
    return scored
