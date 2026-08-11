"""Sample-judge heuristic tests."""

from __future__ import annotations

import pytest

from drafting.sample_judge import (
    JudgeProfile,
    Reason,
    judge,
)


@pytest.fixture
def profile() -> JudgeProfile:
    return JudgeProfile(
        allowed_greetings=("hey", "hi", "morning"),
        max_emoji=1,
        min_chars=20,
        max_chars=300,
    )


def _reasons(result: object) -> set[Reason]:
    return {v.reason for v in result.violations}  # type: ignore[attr-defined]


def test_clean_draft_passes(profile: JudgeProfile) -> None:
    draft = (
        "Hey Jane — wanted to check in on the soundbar setup. Let me know if "
        "you'd like me to swing by Tuesday."
    )
    result = judge(draft, profile)
    assert result.passed
    assert result.violations == ()


def test_llm_drift_phrase_flagged(profile: JudgeProfile) -> None:
    draft = "Hey Jane, I hope this finds you well — wanted to reconnect about the rack."
    result = judge(draft, profile)
    assert Reason.LLM_DRIFT in _reasons(result)


def test_banned_phrase_thinking_about_you_flagged(profile: JudgeProfile) -> None:
    draft = "Hey Jane! Just thinking about you. Hope the system is treating you well."
    result = judge(draft, profile)
    assert Reason.BANNED_PHRASE in _reasons(result)


def test_banned_phrase_case_insensitive(profile: JudgeProfile) -> None:
    draft = "Hey Jane — Thinking Of You today. Let me know if the rack needs anything."
    result = judge(draft, profile)
    assert Reason.BANNED_PHRASE in _reasons(result)


def test_concrete_opener_not_flagged_as_banned(profile: JudgeProfile) -> None:
    draft = "Hey Jane — how's the soundbar from last fall holding up? Happy to swing by."
    result = judge(draft, profile)
    assert Reason.BANNED_PHRASE not in _reasons(result)


def test_hallucinated_dollar_amount_flagged(profile: JudgeProfile) -> None:
    draft = "Hey Jane — your rebate of $1,200 is ready. Want me to apply it?"
    result = judge(draft, profile, source_dollar_amounts=frozenset())
    assert Reason.HALLUCINATED_DOLLAR_AMOUNT in _reasons(result)


def test_known_dollar_amount_not_flagged(profile: JudgeProfile) -> None:
    draft = "Hey Jane — your rebate of $1,200 is ready. Want me to apply it?"
    result = judge(draft, profile, source_dollar_amounts=frozenset({"$1,200"}))
    assert Reason.HALLUCINATED_DOLLAR_AMOUNT not in _reasons(result)


def test_off_profile_greeting_flagged(profile: JudgeProfile) -> None:
    draft = "Greetings, Jane! Wanted to follow up on the install we did last spring."
    result = judge(draft, profile)
    assert Reason.OFF_PROFILE_GREETING in _reasons(result)


def test_emoji_over_budget_flagged(profile: JudgeProfile) -> None:
    draft = "Hey Jane — install looks great 🎉🎉🎉. Catch you next week."
    result = judge(draft, profile)
    assert Reason.EMOJI_OVER_BUDGET in _reasons(result)


def test_emoji_within_budget_not_flagged(profile: JudgeProfile) -> None:
    draft = "Hey Jane — install looks great 🎉. Catch you next week if you have time."
    result = judge(draft, profile)
    assert Reason.EMOJI_OVER_BUDGET not in _reasons(result)


def test_length_too_short_flagged(profile: JudgeProfile) -> None:
    draft = "Hey Jane!"
    result = judge(draft, profile)
    assert Reason.LENGTH_TOO_SHORT in _reasons(result)


def test_length_too_long_flagged(profile: JudgeProfile) -> None:
    draft = "Hey Jane — " + ("blah " * 200)
    result = judge(draft, profile)
    assert Reason.LENGTH_TOO_LONG in _reasons(result)


def test_multiple_violations_collected(profile: JudgeProfile) -> None:
    draft = "Greetings! I hope this finds you well 🎉🎉🎉🎉."
    result = judge(draft, profile)
    reasons = _reasons(result)
    assert Reason.LLM_DRIFT in reasons
    assert Reason.OFF_PROFILE_GREETING in reasons
    assert Reason.EMOJI_OVER_BUDGET in reasons
