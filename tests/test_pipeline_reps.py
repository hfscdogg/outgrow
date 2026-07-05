"""Rep YAML loader tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from pipeline.reps import (
    LoadedRep,
    apply_cc_review_progress,
    is_active_today,
    load_rep,
    load_reps,
    zoho_user_to_rep_id_map,
)
from pipeline.suppressions import PulseEntry
from pulse.mailer import RepProfile


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def _full_payload(**overrides) -> dict:
    base = {
        "rep_id": "zack",
        "email": "zack@getlivewire.com",
        "first_name": "Zack",
        "zoho_user_id": "5234567890123456789",
        "cc_review_remaining": 5,
        "formality": "casual",
        "avg_sentence_length": 11.5,
        "typical_greetings": ["hey"],
        "typical_signoffs": ["talk soon"],
        "emoji_per_message": 0.3,
        "distinctive_vocab": ["soundbar", "rack"],
    }
    base.update(overrides)
    return base


# ---- load_rep --------------------------------------------------------------


def test_load_rep_returns_profile_voice_zoho_id(tmp_path: Path) -> None:
    p = _write(tmp_path / "zack.yaml", _full_payload())
    rep = load_rep(p)
    assert isinstance(rep, LoadedRep)
    assert rep.profile == RepProfile(
        rep_id="zack",
        email="zack@getlivewire.com",
        first_name="Zack",
        cc_review_remaining=5,
    )
    assert rep.voice["formality"] == "casual"
    assert rep.voice["typical_greetings"] == ["hey"]
    assert rep.zoho_user_id == "5234567890123456789"
    assert rep.ooo_until is None
    assert rep.paused_until is None


def test_load_rep_parses_ooo_and_paused_dates(tmp_path: Path) -> None:
    p = _write(
        tmp_path / "zack.yaml",
        _full_payload(ooo_until="2026-06-01", paused_until="2026-05-15"),
    )
    rep = load_rep(p)
    assert rep.ooo_until == date(2026, 6, 1)
    assert rep.paused_until == date(2026, 5, 15)


def test_load_rep_handles_missing_optional_voice_fields(tmp_path: Path) -> None:
    payload = {
        "rep_id": "zack",
        "email": "zack@getlivewire.com",
        "first_name": "Zack",
        "zoho_user_id": "5234567890123456789",
    }
    p = _write(tmp_path / "zack.yaml", payload)
    rep = load_rep(p)
    assert rep.voice == {}
    assert rep.profile.cc_review_remaining == 0


def test_load_rep_strips_whitespace_from_email(tmp_path: Path) -> None:
    p = _write(tmp_path / "zack.yaml", _full_payload(email="  zack@getlivewire.com  "))
    rep = load_rep(p)
    assert rep.profile.email == "zack@getlivewire.com"


def test_load_rep_raises_on_missing_required_field(tmp_path: Path) -> None:
    payload = _full_payload()
    del payload["zoho_user_id"]
    p = _write(tmp_path / "zack.yaml", payload)
    with pytest.raises(RuntimeError, match="zoho_user_id"):
        load_rep(p)


def test_load_rep_raises_on_empty_required_field(tmp_path: Path) -> None:
    p = _write(tmp_path / "zack.yaml", _full_payload(email=""))
    with pytest.raises(RuntimeError, match="email"):
        load_rep(p)


# ---- load_reps -------------------------------------------------------------


def test_load_reps_returns_empty_list_for_empty_dir(tmp_path: Path) -> None:
    assert load_reps(tmp_path) == []


def test_load_reps_loads_all_yaml_in_alphabetical_order(tmp_path: Path) -> None:
    _write(
        tmp_path / "zack.yaml",
        _full_payload(rep_id="zack", email="zack@getlivewire.com", first_name="Zack"),
    )
    _write(
        tmp_path / "henry.yaml",
        _full_payload(
            rep_id="henry",
            email="henry@getlivewire.com",
            first_name="Henry",
            zoho_user_id="6234567890123456789",
        ),
    )
    reps = load_reps(tmp_path)
    assert [r.profile.rep_id for r in reps] == ["henry", "zack"]


def test_load_reps_ignores_non_yaml_files(tmp_path: Path) -> None:
    _write(tmp_path / "zack.yaml", _full_payload())
    (tmp_path / ".gitkeep").write_text("")
    (tmp_path / "README.md").write_text("# notes")
    reps = load_reps(tmp_path)
    assert len(reps) == 1


# ---- is_active_today -------------------------------------------------------


def _rep(**overrides) -> LoadedRep:
    base = {
        "profile": RepProfile(rep_id="zack", email="zack@getlivewire.com", first_name="Zack"),
        "voice": {},
        "zoho_user_id": "u1",
        "zoho_user_id_aliases": (),
        "ooo_until": None,
        "paused_until": None,
    }
    base.update(overrides)
    return LoadedRep(**base)  # type: ignore[arg-type]


# ---- zoho_user_id_aliases --------------------------------------------------


def test_load_rep_parses_zoho_user_id_aliases(tmp_path: Path) -> None:
    """Optional ``zoho_user_id_aliases`` list lets a rep claim former reps'
    books without mass-updating the Owner field in Zoho."""
    p = _write(
        tmp_path / "zack.yaml",
        _full_payload(zoho_user_id_aliases=["176704000111111111", "176704000222222222"]),
    )
    rep = load_rep(p)
    assert rep.zoho_user_id_aliases == ("176704000111111111", "176704000222222222")


def test_load_rep_defaults_aliases_to_empty_tuple_when_omitted(tmp_path: Path) -> None:
    p = _write(tmp_path / "zack.yaml", _full_payload())
    rep = load_rep(p)
    assert rep.zoho_user_id_aliases == ()


def test_load_rep_drops_blank_alias_entries(tmp_path: Path) -> None:
    """Defensive: a YAML with a stray blank string in the list shouldn't
    register an empty Owner ID and gobble unmapped contacts."""
    p = _write(
        tmp_path / "zack.yaml",
        _full_payload(zoho_user_id_aliases=["176704000111111111", "", "  "]),
    )
    rep = load_rep(p)
    assert rep.zoho_user_id_aliases == ("176704000111111111",)


def test_zoho_user_to_rep_id_map_registers_aliases() -> None:
    """All of: primary + every alias map to the same rep_id."""
    zack = _rep(
        profile=RepProfile(rep_id="zack", email="zack@getlivewire.com", first_name="Zack"),
        zoho_user_id="z_primary",
        zoho_user_id_aliases=("andre_uid", "brad_uid"),
    )
    henry = _rep(
        profile=RepProfile(rep_id="henry", email="henry@getlivewire.com", first_name="Henry"),
        zoho_user_id="h_primary",
    )
    mapping = zoho_user_to_rep_id_map([zack, henry])
    assert mapping == {
        "z_primary": "zack",
        "andre_uid": "zack",
        "brad_uid": "zack",
        "h_primary": "henry",
    }


# ---- apply_cc_review_progress ------------------------------------------------


def _hist(rep_id: str, pulse_date: date) -> PulseEntry:
    return PulseEntry(
        customer_id="c1",
        rep_id=rep_id,
        pulse_id=f"{pulse_date.isoformat()}-{rep_id}",
        date=pulse_date,
    )


def _rep_with_cc(rep_id: str, remaining: int) -> LoadedRep:
    return _rep(
        profile=RepProfile(
            rep_id=rep_id,
            email=f"{rep_id}@getlivewire.com",
            first_name=rep_id.title(),
            cc_review_remaining=remaining,
        )
    )


def test_cc_review_progress_subtracts_sent_pulses() -> None:
    reps = [_rep_with_cc("zack", 5)]
    history = [_hist("zack", date(2026, 6, d)) for d in (1, 2, 3)]
    adjusted = apply_cc_review_progress(reps, history)
    assert adjusted[0].profile.cc_review_remaining == 2


def test_cc_review_progress_floors_at_zero() -> None:
    reps = [_rep_with_cc("zack", 5)]
    history = [_hist("zack", date(2026, 6, d)) for d in range(1, 16)]  # 15 sends
    adjusted = apply_cc_review_progress(reps, history)
    assert adjusted[0].profile.cc_review_remaining == 0


def test_cc_review_progress_counts_per_rep_independently() -> None:
    reps = [_rep_with_cc("zack", 5), _rep_with_cc("henry", 0)]
    history = [_hist("zack", date(2026, 6, 1)), _hist("henry", date(2026, 6, 1))]
    adjusted = apply_cc_review_progress(reps, history)
    by_id = {r.profile.rep_id: r.profile.cc_review_remaining for r in adjusted}
    assert by_id == {"zack": 4, "henry": 0}


def test_cc_review_progress_no_history_leaves_reps_untouched() -> None:
    reps = [_rep_with_cc("zack", 5)]
    adjusted = apply_cc_review_progress(reps, [])
    assert adjusted[0] is reps[0]  # unchanged object, not a copy
    assert adjusted[0].profile.cc_review_remaining == 5


def test_cc_review_progress_preserves_non_profile_fields() -> None:
    rep = _rep(
        profile=RepProfile(
            rep_id="zack",
            email="zack@getlivewire.com",
            first_name="Zack",
            cc_review_remaining=5,
        ),
        zoho_user_id="z_primary",
        zoho_user_id_aliases=("andre_uid",),
    )
    adjusted = apply_cc_review_progress([rep], [_hist("zack", date(2026, 6, 1))])
    assert adjusted[0].zoho_user_id == "z_primary"
    assert adjusted[0].zoho_user_id_aliases == ("andre_uid",)
    assert adjusted[0].voice == rep.voice
    assert adjusted[0].profile.cc_review_remaining == 4


def test_is_active_when_no_ooo_or_paused() -> None:
    assert is_active_today(_rep(), date(2026, 5, 7)) is True


def test_is_inactive_when_ooo_through_today() -> None:
    assert is_active_today(_rep(ooo_until=date(2026, 5, 10)), date(2026, 5, 7)) is False


def test_is_active_after_ooo_window() -> None:
    assert is_active_today(_rep(ooo_until=date(2026, 5, 1)), date(2026, 5, 7)) is True


def test_is_inactive_when_paused_through_today() -> None:
    assert is_active_today(_rep(paused_until=date(2026, 5, 7)), date(2026, 5, 7)) is False


def test_is_active_after_paused_window() -> None:
    assert is_active_today(_rep(paused_until=date(2026, 5, 1)), date(2026, 5, 7)) is True


# ---- zoho_user_to_rep_id_map -----------------------------------------------


def test_zoho_user_to_rep_id_map_builds_lookup_from_loaded_reps() -> None:
    reps = [
        _rep(),  # zack -> u1
        LoadedRep(
            profile=RepProfile(rep_id="henry", email="henry@getlivewire.com", first_name="Henry"),
            voice={},
            zoho_user_id="u2",
            zoho_user_id_aliases=(),
            ooo_until=None,
            paused_until=None,
        ),
    ]
    assert zoho_user_to_rep_id_map(reps) == {"u1": "zack", "u2": "henry"}
