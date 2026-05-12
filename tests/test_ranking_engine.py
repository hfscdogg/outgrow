"""Ranking engine tests.

Pure unit tests — no I/O, no sockets, no fixtures. Every customer/play/
config is constructed inline with named kwargs so the math is auditable
in the test source.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import pytest

from ranking.engine import (
    ELIGIBILITY_REASONS,
    Customer,
    EligibilityDiagnostics,
    Play,
    RankingConfig,
    dormancy_days,
    dormancy_factor,
    eligibility_reason,
    is_eligible,
    is_in_install_window,
    legacy_bonus_cents,
    load_plays,
    load_ranking_config,
    rank_for_rep,
    rank_for_rep_with_diagnostics,
    score,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _cfg(**overrides: object) -> RankingConfig:
    base = {
        "qbo_horizon": date(2017, 1, 1),
        "legacy_bonus_cents_per_year": 5_000_000,
        "too_recent_days": 30,
        "ramp_up_days": 180,
        "sweet_spot_end_days": 720,
        "decay_floor_days": 1825,
        "cold_floor_factor": 0.5,
        "min_rep_match_confidence": 0.5,
        "suppression_penalty": 0.0,
    }
    base.update(overrides)
    return RankingConfig(**base)  # type: ignore[arg-type]


def _play(**overrides: object) -> Play:
    base = {
        "key": "daily_proactive",
        "base_weight": 1.0,
        "enabled": True,
        "min_days_since_install": None,
        "max_days_since_install": None,
        "seasonal_weights": {},
    }
    base.update(overrides)
    return Play(**base)  # type: ignore[arg-type]


def _customer(**overrides: object) -> Customer:
    base = {
        "id": "c1",
        "rep_id": "zack",
        "ltv_cents": 5_000_000,
        "last_purchase_at": date(2025, 1, 1),
        "first_known_contact_at": date(2010, 1, 1),
        "rep_match_confidence": 0.9,
        "suppressed": False,
        "suppression_reason": None,
        "last_install_completed_at": None,
    }
    base.update(overrides)
    return Customer(**base)  # type: ignore[arg-type]


# ---- dormancy_factor curve ----


def test_dormancy_factor_too_recent_is_zero() -> None:
    assert dormancy_factor(0, _cfg()) == 0.0
    assert dormancy_factor(29, _cfg()) == 0.0


def test_dormancy_factor_ramp_up_is_linear() -> None:
    cfg = _cfg()
    # midpoint between 30 and 180: 105 days -> 0.5
    assert dormancy_factor(105, cfg) == pytest.approx(0.5, abs=1e-9)


def test_dormancy_factor_sweet_spot_is_one() -> None:
    cfg = _cfg()
    assert dormancy_factor(180, cfg) == 1.0
    assert dormancy_factor(365, cfg) == 1.0
    assert dormancy_factor(720, cfg) == 1.0


def test_dormancy_factor_decay_is_linear() -> None:
    cfg = _cfg()
    # midpoint of decay 720..1825 -> halfway from 1.0 to 0.5 -> 0.75
    midpoint = (720 + 1825) // 2
    assert dormancy_factor(midpoint, cfg) == pytest.approx(0.75, abs=0.01)


def test_dormancy_factor_cold_floor_holds() -> None:
    cfg = _cfg()
    assert dormancy_factor(1825, cfg) == 0.5
    assert dormancy_factor(10_000, cfg) == 0.5


# ---- legacy_bonus_cents ----


def test_legacy_bonus_zero_when_no_first_known_contact() -> None:
    c = _customer(first_known_contact_at=None)
    assert legacy_bonus_cents(c, _cfg()) == 0


def test_legacy_bonus_zero_when_post_qbo_horizon() -> None:
    c = _customer(first_known_contact_at=date(2020, 6, 1))
    assert legacy_bonus_cents(c, _cfg()) == 0


def test_legacy_bonus_scales_with_pre_qbo_years() -> None:
    cfg = _cfg()  # horizon=2017, $50k/yr
    c = _customer(first_known_contact_at=date(2007, 1, 1))
    assert legacy_bonus_cents(c, cfg) == 10 * 5_000_000


# ---- dormancy_days ----


def test_dormancy_days_handles_none_last_purchase() -> None:
    c = _customer(last_purchase_at=None)
    assert dormancy_days(c, date(2026, 5, 6)) is None


def test_dormancy_days_clamped_at_zero_for_future_dates() -> None:
    c = _customer(last_purchase_at=date(2026, 6, 1))
    assert dormancy_days(c, date(2026, 5, 6)) == 0


# ---- is_in_install_window ----


def test_install_window_blocks_outside_min() -> None:
    p = _play(min_days_since_install=60)
    c = _customer(last_install_completed_at=date(2026, 4, 6))  # 30 days ago
    assert is_in_install_window(c, p, date(2026, 5, 6)) is False


def test_install_window_blocks_outside_max() -> None:
    p = _play(min_days_since_install=60, max_days_since_install=180)
    c = _customer(last_install_completed_at=date(2025, 5, 6))  # 365 days ago
    assert is_in_install_window(c, p, date(2026, 5, 6)) is False


def test_install_window_passes_when_inside() -> None:
    p = _play(min_days_since_install=60, max_days_since_install=180)
    c = _customer(last_install_completed_at=date(2025, 12, 6))  # 151 days ago
    assert is_in_install_window(c, p, date(2026, 5, 6)) is True


def test_install_window_no_install_only_passes_open_ended_play() -> None:
    open_play = _play(min_days_since_install=None)
    gated_play = _play(min_days_since_install=60)
    c = _customer(last_install_completed_at=None)
    today = date(2026, 5, 6)
    assert is_in_install_window(c, open_play, today) is True
    assert is_in_install_window(c, gated_play, today) is False


# ---- is_eligible ----


def test_eligibility_disabled_play_rejects() -> None:
    assert (
        is_eligible(
            _customer(),
            _play(enabled=False),
            _cfg(),
            date(2026, 5, 6),
        )
        is False
    )


def test_eligibility_suppressed_customer_rejects() -> None:
    c = _customer(suppressed=True, suppression_reason="open_ticket")
    assert is_eligible(c, _play(), _cfg(), date(2026, 5, 6)) is False


def test_eligibility_low_rep_match_rejects() -> None:
    c = _customer(rep_match_confidence=0.4)
    assert is_eligible(c, _play(), _cfg(), date(2026, 5, 6)) is False


def test_eligibility_too_recent_purchase_rejects() -> None:
    c = _customer(last_purchase_at=date(2026, 5, 1))  # 5 days ago
    assert is_eligible(c, _play(), _cfg(), date(2026, 5, 6)) is False


def test_eligibility_no_purchase_history_rejects() -> None:
    c = _customer(last_purchase_at=None)
    assert is_eligible(c, _play(), _cfg(), date(2026, 5, 6)) is False


def test_eligibility_happy_path() -> None:
    c = _customer(last_purchase_at=date(2025, 5, 6))  # 365 days ago, sweet spot
    assert is_eligible(c, _play(), _cfg(), date(2026, 5, 6)) is True


# ---- eligibility_reason ----


def test_eligibility_reason_none_when_eligible() -> None:
    c = _customer(last_purchase_at=date(2025, 5, 6))
    assert eligibility_reason(c, _play(), _cfg(), date(2026, 5, 6)) is None


def test_eligibility_reason_classifies_each_rejection() -> None:
    today = date(2026, 5, 6)
    cfg = _cfg()
    cases = {
        "play_disabled": (_customer(), _play(enabled=False)),
        "suppressed": (_customer(suppressed=True, suppression_reason="x"), _play()),
        "low_confidence": (_customer(rep_match_confidence=0.1), _play()),
        "install_window": (
            _customer(last_install_completed_at=date(2026, 4, 30)),
            _play(min_days_since_install=60),
        ),
        "no_purchase_date": (_customer(last_purchase_at=None), _play()),
        "too_recent": (_customer(last_purchase_at=date(2026, 5, 1)), _play()),
    }
    for expected, (c, p) in cases.items():
        assert eligibility_reason(c, p, cfg, today) == expected, expected


def test_eligibility_reason_keys_are_all_documented() -> None:
    """Every key returned by eligibility_reason must appear in ELIGIBILITY_REASONS."""
    today = date(2026, 5, 6)
    cfg = _cfg()
    keys = {
        eligibility_reason(_customer(), _play(enabled=False), cfg, today),
        eligibility_reason(_customer(suppressed=True), _play(), cfg, today),
        eligibility_reason(_customer(rep_match_confidence=0.0), _play(), cfg, today),
        eligibility_reason(
            _customer(last_install_completed_at=today),
            _play(min_days_since_install=10),
            cfg,
            today,
        ),
        eligibility_reason(_customer(last_purchase_at=None), _play(), cfg, today),
        eligibility_reason(_customer(last_purchase_at=today), _play(), cfg, today),
    }
    assert keys <= set(ELIGIBILITY_REASONS)


# ---- score ----


def test_score_components_present_and_finite() -> None:
    s = score(_customer(), _play(), _cfg(), date(2026, 5, 6))
    expected = {
        "base_weight",
        "seasonal",
        "ltv_log10",
        "legacy_bonus_cents",
        "dormancy_factor",
        "rep_match_confidence",
        "suppression_penalty",
    }
    assert set(s.components.keys()) == expected
    assert math.isfinite(s.score)


def test_score_uses_seasonal_weight_for_today_month() -> None:
    play = _play(seasonal_weights={"may": 2.0})
    s = score(_customer(), play, _cfg(), date(2026, 5, 6))
    assert s.components["seasonal"] == 2.0


def test_score_seasonal_weight_defaults_to_one_when_missing() -> None:
    play = _play(seasonal_weights={"jan": 5.0})  # nothing for May
    s = score(_customer(), play, _cfg(), date(2026, 5, 6))
    assert s.components["seasonal"] == 1.0


def test_score_log10_is_safe_at_zero_ltv() -> None:
    c = _customer(ltv_cents=0, first_known_contact_at=None)
    s = score(c, _play(), _cfg(), date(2026, 5, 6))
    assert s.components["ltv_log10"] == 0.0  # log10(max(1, 0)) == 0


def test_score_legacy_bonus_lifts_pre_qbo_customer() -> None:
    cfg = _cfg()
    today = date(2026, 5, 6)
    play = _play()
    pre_qbo = _customer(
        ltv_cents=0,
        first_known_contact_at=date(2007, 1, 1),  # 10 yrs pre-horizon
    )
    modern_zero = _customer(
        ltv_cents=0,
        first_known_contact_at=date(2020, 1, 1),
    )
    pre_score = score(pre_qbo, play, cfg, today).score
    mod_score = score(modern_zero, play, cfg, today).score
    assert pre_score > mod_score


def test_score_subtracts_suppression_penalty() -> None:
    cfg_clean = _cfg(suppression_penalty=0.0)
    cfg_penalty = _cfg(suppression_penalty=0.5)
    today = date(2026, 5, 6)
    diff = (
        score(_customer(), _play(), cfg_clean, today).score
        - score(_customer(), _play(), cfg_penalty, today).score
    )
    assert diff == pytest.approx(0.5, abs=1e-9)


def test_score_rep_match_acts_as_multiplier() -> None:
    today = date(2026, 5, 6)
    high = score(_customer(rep_match_confidence=1.0), _play(), _cfg(), today).score
    low = score(_customer(rep_match_confidence=0.6), _play(), _cfg(), today).score
    # 1.0/0.6 ~= 1.667; high should be ~1.667x low (no penalty applied)
    assert high == pytest.approx(low * (1.0 / 0.6), rel=1e-6)


# ---- rank_for_rep ----


def test_rank_filters_by_rep_id() -> None:
    today = date(2026, 5, 6)
    customers = [
        _customer(id="zack-c1", rep_id="zack"),
        _customer(id="henry-c1", rep_id="henry"),
    ]
    ranked = rank_for_rep(customers, _play(), _cfg(), "zack", today)
    assert [r.customer_id for r in ranked] == ["zack-c1"]


def test_rank_skips_ineligible_customers() -> None:
    today = date(2026, 5, 6)
    customers = [
        _customer(id="ok"),
        _customer(id="suppressed", suppressed=True),
        _customer(id="too-recent", last_purchase_at=date(2026, 5, 1)),
        _customer(id="low-rep-match", rep_match_confidence=0.1),
    ]
    ranked = rank_for_rep(customers, _play(), _cfg(), "zack", today)
    assert {r.customer_id for r in ranked} == {"ok"}


def test_rank_sorts_descending_by_score() -> None:
    today = date(2026, 5, 6)
    customers = [
        _customer(id="small", ltv_cents=10_000),
        _customer(id="big", ltv_cents=50_000_000),
        _customer(id="medium", ltv_cents=500_000),
    ]
    ranked = rank_for_rep(customers, _play(), _cfg(), "zack", today)
    assert [r.customer_id for r in ranked] == ["big", "medium", "small"]


def test_rank_top_n_truncates_result() -> None:
    today = date(2026, 5, 6)
    customers = [_customer(id=f"c{i}", ltv_cents=(i + 1) * 10_000) for i in range(10)]
    ranked = rank_for_rep(customers, _play(), _cfg(), "zack", today, top_n=3)
    assert len(ranked) == 3


# ---- rank_for_rep_with_diagnostics ----


def test_diagnostics_counts_only_rep_owned_customers() -> None:
    today = date(2026, 5, 6)
    customers = [
        _customer(id="z1", rep_id="zack"),
        _customer(id="z2", rep_id="zack", suppressed=True),
        _customer(id="h1", rep_id="henry", suppressed=True),  # not counted for zack
    ]
    _, diag = rank_for_rep_with_diagnostics(customers, _play(), _cfg(), "zack", today)
    assert diag.candidates == 2
    assert diag.rejected == {"suppressed": 1}


def test_diagnostics_classifies_mixed_rejections() -> None:
    today = date(2026, 5, 6)
    customers = [
        _customer(id="ok"),
        _customer(id="suppressed", suppressed=True),
        _customer(id="low-rep-match", rep_match_confidence=0.1),
        _customer(id="too-recent", last_purchase_at=date(2026, 5, 1)),
        _customer(id="no-purchase", last_purchase_at=None),
    ]
    scored, diag = rank_for_rep_with_diagnostics(customers, _play(), _cfg(), "zack", today)
    assert [s.customer_id for s in scored] == ["ok"]
    assert diag.candidates == 5
    assert diag.rejected == {
        "suppressed": 1,
        "low_confidence": 1,
        "too_recent": 1,
        "no_purchase_date": 1,
    }


def test_diagnostics_empty_when_no_rep_customers() -> None:
    today = date(2026, 5, 6)
    customers = [_customer(id="h1", rep_id="henry")]
    scored, diag = rank_for_rep_with_diagnostics(customers, _play(), _cfg(), "zack", today)
    assert scored == []
    assert diag == EligibilityDiagnostics(candidates=0, rejected={})


def test_diagnostics_play_disabled_attributes_all_rejections() -> None:
    today = date(2026, 5, 6)
    customers = [_customer(id="c1"), _customer(id="c2")]
    _, diag = rank_for_rep_with_diagnostics(customers, _play(enabled=False), _cfg(), "zack", today)
    assert diag.candidates == 2
    assert diag.rejected == {"play_disabled": 2}


# ---- loaders read shipped configs ----


def test_load_plays_reads_shipped_config() -> None:
    plays = load_plays(REPO_ROOT / "config" / "plays.yaml")
    keys = [p.key for p in plays]
    assert "daily_proactive" in keys
    assert "reverse_dyk" in keys
    assert "referral" in keys


def test_load_plays_parses_seasonal_weights_as_floats() -> None:
    plays = load_plays(REPO_ROOT / "config" / "plays.yaml")
    daily = next(p for p in plays if p.key == "daily_proactive")
    assert daily.seasonal_weight_for(5) == 1.0
    assert daily.base_weight == 1.0
    assert daily.enabled is True


def test_load_ranking_config_reads_shipped_config() -> None:
    cfg = load_ranking_config(REPO_ROOT / "config" / "ranking.yaml")
    assert cfg.qbo_horizon == date(2017, 1, 1)
    assert cfg.legacy_bonus_cents_per_year == 5_000_000
    assert cfg.too_recent_days == 30
    assert cfg.cold_floor_factor == 0.5
    assert cfg.min_rep_match_confidence == 0.5
