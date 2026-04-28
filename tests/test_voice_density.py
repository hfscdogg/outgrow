"""Voice signal-density and distillation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from voice.wizard import (
    TextSample,
    distill,
    load_history,
    rank_by_density,
    signal_density,
    top_for_review,
    write_profile,
)


def test_signal_density_zero_for_empty_text() -> None:
    assert signal_density("") == 0.0


def test_signal_density_zero_for_only_stopwords() -> None:
    assert signal_density("the the the the the") == 0.0


def test_signal_density_higher_for_unique_words() -> None:
    formulaic = signal_density("thanks thanks thanks thanks thanks thanks")
    varied = signal_density(
        "Glad the speakers are dialed in. Want me to swing by next week to demo the soundbar?"
    )
    assert varied > formulaic


def test_short_message_is_penalized_below_long_one() -> None:
    short = signal_density("Sounds good!")
    long = signal_density(
        "Sounds good — I'll bring the rack mount and the new control module on Tuesday "
        "morning, probably around ten if that works."
    )
    assert long > short


def test_rank_by_density_returns_descending_order() -> None:
    samples = [
        TextSample("ok"),
        TextSample(
            "Glad the projector landed on time; let me know if the lens shift was off "
            "after install or if it shifted overnight."
        ),
        TextSample("thanks thanks thanks"),
    ]
    ranked = rank_by_density(samples)
    assert [r.sample.text for r in ranked][0].startswith("Glad")


def test_top_for_review_caps_at_limit() -> None:
    samples = [TextSample(f"unique-message-{i} alpha bravo charlie") for i in range(60)]
    top = top_for_review(samples, limit=10)
    assert len(top) == 10


def test_distill_extracts_greeting_signoff_and_emoji() -> None:
    favorites = [
        TextSample("Hey Jane! Quick check-in on the soundbar. Talk soon."),
        TextSample("Hey Mark, swinging by Tuesday to recalibrate. Talk soon."),
        TextSample("Hey Sara, demo went great 🎉. Catch you Monday."),
    ]
    profile = distill("zack", favorites)
    assert profile.rep_id == "zack"
    assert profile.avg_sentence_length > 0
    assert any(g.startswith("hey") for g in profile.typical_greetings)
    assert profile.emoji_per_message > 0


def test_distill_handles_empty_favorites() -> None:
    profile = distill("zack", [])
    assert profile.rep_id == "zack"
    assert profile.avg_sentence_length == 0.0
    assert profile.typical_greetings == []


def test_load_history_reports_missing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_history(tmp_path / "missing.csv")


def test_write_profile_round_trip(tmp_path: Path) -> None:
    favorites = [TextSample("Hey Jane, thanks for the quick reply. Talk soon.")]
    profile = distill("zack", favorites)
    out = tmp_path / "zack.yaml"
    write_profile(profile, out)
    text = out.read_text(encoding="utf-8")
    assert "rep_id: zack" in text
    assert "typical_greetings" in text
