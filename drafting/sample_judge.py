"""Heuristic auto-rejector for draft messages.

Phase 0 dispatch tool. Catches obvious draft-quality failures before they
reach a rep — LLM-drift phrases, hallucinated dollar amounts, off-profile
greetings, emoji over budget, length out of band. The judge is pure: no
LLM call, no I/O, no network.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from enum import StrEnum

EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff]",
    flags=re.UNICODE,
)
DOLLAR_RE = re.compile(r"\$\s*\d[\d,]*(?:\.\d+)?")

# Phrases that scream "GPT wrote this." Add to this list when new patterns
# show up in production drafts.
LLM_DRIFT_PHRASES: tuple[str, ...] = (
    "i hope this finds you well",
    "i hope this email finds you well",
    "i hope this message finds you well",
    "in today's fast-paced world",
    "in this ever-changing landscape",
    "delve into",
    "tapestry",
    "navigate the complexities",
    "leverage synergies",
    "at the end of the day",
    "as an ai",
    "as a language model",
    "i'd be happy to",
    "elevate your experience",
    "unlock the potential",
)


class Reason(StrEnum):
    LLM_DRIFT = "llm_drift_phrase"
    HALLUCINATED_DOLLAR_AMOUNT = "hallucinated_dollar_amount"
    OFF_PROFILE_GREETING = "off_profile_greeting"
    EMOJI_OVER_BUDGET = "emoji_over_budget"
    LENGTH_TOO_SHORT = "length_too_short"
    LENGTH_TOO_LONG = "length_too_long"


@dataclass(frozen=True)
class Violation:
    reason: Reason
    detail: str


@dataclass(frozen=True)
class JudgeProfile:
    allowed_greetings: tuple[str, ...]
    max_emoji: int
    min_chars: int
    max_chars: int


@dataclass(frozen=True)
class JudgeResult:
    passed: bool
    violations: tuple[Violation, ...]


def _first_sentence(draft: str) -> str:
    parts = re.split(r"[.!?\n]", draft.strip(), maxsplit=1)
    return parts[0].strip().lower() if parts else ""


def judge(
    draft: str,
    profile: JudgeProfile,
    *,
    source_dollar_amounts: frozenset[str] = frozenset(),
) -> JudgeResult:
    """Return all violations the heuristic detects.

    `source_dollar_amounts` is the set of dollar-amount strings that *do*
    appear in the source context (briefing, customer notes, prior order).
    Anything in the draft that is not in this set is treated as
    hallucinated.
    """
    violations: list[Violation] = []
    lower = draft.lower()

    for phrase in LLM_DRIFT_PHRASES:
        if phrase in lower:
            violations.append(Violation(Reason.LLM_DRIFT, phrase))

    for match in DOLLAR_RE.findall(draft):
        normalized = re.sub(r"\s+", "", match)
        if normalized not in {re.sub(r"\s+", "", s) for s in source_dollar_amounts}:
            violations.append(Violation(Reason.HALLUCINATED_DOLLAR_AMOUNT, normalized))

    first = _first_sentence(draft)
    if (
        profile.allowed_greetings
        and first
        and not any(first.startswith(g.lower()) for g in profile.allowed_greetings)
    ):
        violations.append(Violation(Reason.OFF_PROFILE_GREETING, first[:40]))

    emoji_count = len(EMOJI_RE.findall(draft))
    if emoji_count > profile.max_emoji:
        violations.append(
            Violation(Reason.EMOJI_OVER_BUDGET, f"{emoji_count} > {profile.max_emoji}")
        )

    char_count = len(draft.strip())
    if char_count < profile.min_chars:
        violations.append(Violation(Reason.LENGTH_TOO_SHORT, f"{char_count} < {profile.min_chars}"))
    if char_count > profile.max_chars:
        violations.append(Violation(Reason.LENGTH_TOO_LONG, f"{char_count} > {profile.max_chars}"))

    return JudgeResult(passed=not violations, violations=tuple(violations))


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Draft auto-rejector heuristic")
    parser.add_argument("--draft", required=True)
    parser.add_argument("--rep", required=True)
    args = parser.parse_args(argv)
    raise NotImplementedError(
        "CLI runner needs the loaded voice profile + source briefing. "
        f"(rep={args.rep}, draft={args.draft})"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
