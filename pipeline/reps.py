"""Rep YAML loader.

Reads ``config/reps/<rep_id>.yaml`` files and produces ``RepProfile``
+ voice dict + Zoho user-ID mapping. The rep YAML conflates the rep
profile fields (id, email, first_name, zoho_user_id, ooo_until,
cc_review_remaining) with the distilled voice features written by
``voice/wizard.py`` (formality, typical_greetings, etc.) — one file
per rep.

Pure functions. Required fields raise on load (rep_id, email,
first_name, zoho_user_id) so a misconfigured deploy fails fast at
startup, not at 07:00 send time.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from pulse.mailer import RepProfile

if TYPE_CHECKING:
    from pipeline.suppressions import PulseEntry

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPS_DIR = REPO_ROOT / "config" / "reps"

REQUIRED_FIELDS: tuple[str, ...] = ("rep_id", "email", "first_name", "zoho_user_id")
VOICE_FIELDS: tuple[str, ...] = (
    "formality",
    "avg_sentence_length",
    "typical_greetings",
    "typical_signoffs",
    "emoji_per_message",
    "distinctive_vocab",
)


@dataclass(frozen=True)
class LoadedRep:
    """Everything the orchestrator needs to drive one rep's pulse."""

    profile: RepProfile
    voice: dict[str, Any]
    zoho_user_id: str
    # Extra Zoho user IDs whose customers also flow to this rep. Used when
    # a former rep's book has been verbally reassigned but the Owner field
    # in Zoho hasn't been mass-updated yet. Empty for greenfield reps.
    zoho_user_id_aliases: tuple[str, ...]
    ooo_until: date | None
    paused_until: date | None


def _parse_optional_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_rep(path: Path) -> LoadedRep:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = [k for k in REQUIRED_FIELDS if not payload.get(k)]
    if missing:
        raise RuntimeError(f"{path} is missing required field(s): {', '.join(missing)}")

    profile = RepProfile(
        rep_id=str(payload["rep_id"]),
        email=str(payload["email"]).strip(),
        first_name=str(payload["first_name"]),
        cc_review_remaining=int(payload.get("cc_review_remaining", 0)),
    )
    voice = {k: payload[k] for k in VOICE_FIELDS if k in payload}
    aliases_raw = payload.get("zoho_user_id_aliases") or []
    aliases = tuple(str(a).strip() for a in aliases_raw if str(a).strip())
    return LoadedRep(
        profile=profile,
        voice=voice,
        zoho_user_id=str(payload["zoho_user_id"]),
        zoho_user_id_aliases=aliases,
        ooo_until=_parse_optional_date(payload.get("ooo_until")),
        paused_until=_parse_optional_date(payload.get("paused_until")),
    )


def load_reps(reps_dir: Path = DEFAULT_REPS_DIR) -> list[LoadedRep]:
    """Load every ``*.yaml`` in ``reps_dir`` (alphabetical order)."""
    paths = sorted(reps_dir.glob("*.yaml"))
    return [load_rep(p) for p in paths]


def is_active_today(rep: LoadedRep, today: date) -> bool:
    """``False`` if the rep is OOO or paused through ``today``."""
    if rep.ooo_until and today <= rep.ooo_until:
        return False
    if rep.paused_until and today <= rep.paused_until:  # noqa: SIM103
        return False
    return True


def zoho_user_to_rep_id_map(reps: list[LoadedRep]) -> dict[str, str]:
    """Build the ``Zoho Owner ID -> internal rep_id`` map.

    Each rep registers their primary ``zoho_user_id`` plus any aliases
    (former reps whose books were verbally reassigned). Later entries
    win on duplicates — list aliases on the rep who currently owns the
    book.
    """
    mapping: dict[str, str] = {}
    for r in reps:
        mapping[r.zoho_user_id] = r.profile.rep_id
        for alias in r.zoho_user_id_aliases:
            mapping[alias] = r.profile.rep_id
    return mapping


def apply_cc_review_progress(
    reps: Sequence[LoadedRep], history: Iterable[PulseEntry]
) -> list[LoadedRep]:
    """Auto-expire each rep's owner-CC review window against pulse history.

    ``cc_review_remaining`` in the rep YAML is the TOTAL number of review
    sends desired (e.g. 5 = "CC the owner on my first 5 pulses"). Every
    history entry for a rep is one sent pulse, so the effective remaining
    is ``yaml_value - sent_count``, floored at 0. This replaces the manual
    decrement the YAML comment promised "when Phase 2's per-rep state-store
    ships" — pulse_history.json IS that store.

    Note: history is pruned to the 90-day suppression window on write, so
    a review target larger than ~65 weekday pulses would never be reached;
    review windows are single-digit in practice.
    """
    sent_counts: dict[str, int] = {}
    for e in history:
        sent_counts[e.rep_id] = sent_counts.get(e.rep_id, 0) + 1
    adjusted: list[LoadedRep] = []
    for rep in reps:
        sent = sent_counts.get(rep.profile.rep_id, 0)
        remaining = max(0, rep.profile.cc_review_remaining - sent)
        if remaining == rep.profile.cc_review_remaining:
            adjusted.append(rep)
        else:
            new_profile = replace(rep.profile, cc_review_remaining=remaining)
            adjusted.append(replace(rep, profile=new_profile))
    return adjusted
