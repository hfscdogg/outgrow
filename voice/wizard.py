"""Voice-profile bootstrap from a rep's outbound text history.

Phase 0 dispatch tool. The rep marks favorites; the wizard distills tone
patterns into ``config/reps/<rep>.yaml``. **Raw customer texts stay in
``.cache/voice/<rep>.csv`` (gitignored) and never enter git history.**

The signal-density formula and distillation logic are pure and testable.
The interactive CLI loop is a thin wrapper that delegates to the pure
functions and is excluded from coverage.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

TOP_PRESENTED = 40
RANKED_HISTORY_CAP = 200
SHORT_MESSAGE_FLOOR = 5
GREETING_MIN_TOKENS = 2
GREETING_MAX_TOKENS = 6
SIGNOFF_MIN_TOKENS = 1
SIGNOFF_MAX_TOKENS = 5
VOCAB_MIN_TOKEN_LEN = 3
FORMALITY_MARGIN = 2

STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "is",
        "it",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "for",
        "with",
        "as",
        "be",
        "are",
        "was",
        "were",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "our",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "will",
        "would",
        "can",
        "could",
        "should",
        "just",
        "so",
        "from",
        "about",
    }
)

EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff]",
    flags=re.UNICODE,
)
URL_RE = re.compile(r"https?://\S+")
WORD_RE = re.compile(r"[A-Za-z']+")


@dataclass(frozen=True)
class TextSample:
    text: str
    sent_at: str | None = None


@dataclass(frozen=True)
class RankedSample:
    sample: TextSample
    density: float


@dataclass
class VoiceProfile:
    rep_id: str
    avg_sentence_length: float = 0.0
    typical_greetings: list[str] = field(default_factory=list)
    typical_signoffs: list[str] = field(default_factory=list)
    emoji_per_message: float = 0.0
    formality: str = "casual"
    distinctive_vocab: list[str] = field(default_factory=list)


def _tokenize(text: str) -> list[str]:
    cleaned = URL_RE.sub("", text.lower())
    return WORD_RE.findall(cleaned)


def signal_density(text: str) -> float:
    """Unique non-stopword ratio with a short-message penalty.

    Returns 0.0 for empty input. Short messages (<5 tokens) are linearly
    penalized so a 1-word "thanks!" never out-ranks a varied 30-word note.
    """
    tokens = _tokenize(text)
    if not tokens:
        return 0.0
    content = [t for t in tokens if t not in STOPWORDS]
    if not content:
        return 0.0
    unique_ratio = len(set(content)) / len(content)
    length_factor = min(1.0, len(tokens) / SHORT_MESSAGE_FLOOR)
    return unique_ratio * length_factor


def rank_by_density(samples: Iterable[TextSample]) -> list[RankedSample]:
    capped = list(samples)[:RANKED_HISTORY_CAP]
    scored = [RankedSample(s, signal_density(s.text)) for s in capped]
    return sorted(scored, key=lambda r: r.density, reverse=True)


def top_for_review(samples: Iterable[TextSample], limit: int = TOP_PRESENTED) -> list[RankedSample]:
    return rank_by_density(samples)[:limit]


def _greeting(text: str) -> str | None:
    sentences = re.split(r"[.!?\n]", text.strip(), maxsplit=1)
    if not sentences:
        return None
    first = sentences[0].strip()
    return first if GREETING_MIN_TOKENS <= len(first.split()) <= GREETING_MAX_TOKENS else None


def _signoff(text: str) -> str | None:
    sentences = [s.strip() for s in re.split(r"[.!?\n]", text.strip()) if s.strip()]
    if not sentences:
        return None
    last = sentences[-1]
    return last if SIGNOFF_MIN_TOKENS <= len(last.split()) <= SIGNOFF_MAX_TOKENS else None


def _formality(samples: list[TextSample]) -> str:
    formal_markers = {"sir", "ma'am", "maam", "regards", "sincerely", "respectfully"}
    casual_markers = {"hey", "yo", "thx", "thanks", "ya"}
    formal_hits = 0
    casual_hits = 0
    for s in samples:
        tokens = set(_tokenize(s.text))
        formal_hits += len(tokens & formal_markers)
        casual_hits += len(tokens & casual_markers)
    if formal_hits > casual_hits * FORMALITY_MARGIN:
        return "formal"
    if casual_hits > formal_hits * FORMALITY_MARGIN:
        return "casual"
    return "neutral"


def distill(rep_id: str, favorites: list[TextSample]) -> VoiceProfile:
    """Distill tone patterns from a rep's marked-favorite samples.

    Output is a profile suitable for serializing to ``config/reps/<rep>.yaml``.
    """
    if not favorites:
        return VoiceProfile(rep_id=rep_id)

    sentence_lengths: list[int] = []
    greetings = Counter[str]()
    signoffs = Counter[str]()
    emoji_count = 0
    vocab = Counter[str]()
    for sample in favorites:
        emoji_count += len(EMOJI_RE.findall(sample.text))
        for sentence in re.split(r"[.!?\n]", sample.text):
            tokens = _tokenize(sentence)
            if tokens:
                sentence_lengths.append(len(tokens))
        if g := _greeting(sample.text):
            greetings[g.lower()] += 1
        if s := _signoff(sample.text):
            signoffs[s.lower()] += 1
        for tok in _tokenize(sample.text):
            if tok not in STOPWORDS and len(tok) >= VOCAB_MIN_TOKEN_LEN:
                vocab[tok] += 1

    avg_len = sum(sentence_lengths) / len(sentence_lengths) if sentence_lengths else 0.0
    return VoiceProfile(
        rep_id=rep_id,
        avg_sentence_length=round(avg_len, 2),
        typical_greetings=[g for g, _ in greetings.most_common(3)],
        typical_signoffs=[s for s, _ in signoffs.most_common(3)],
        emoji_per_message=round(emoji_count / len(favorites), 2),
        formality=_formality(favorites),
        distinctive_vocab=[w for w, _ in vocab.most_common(15)],
    )


def load_history(path: Path) -> list[TextSample]:
    """Read a rep's history CSV from .cache/voice/<rep>.csv (gitignored)."""
    if not path.exists():
        raise FileNotFoundError(
            f"Voice history CSV not found at {path}. "
            "Export the rep's last 200 outbound texts to that path first; "
            "the .cache/ directory is gitignored."
        )
    samples: list[TextSample] = []
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            samples.append(TextSample(text=text, sent_at=row.get("sent_at")))
    return samples


def write_profile(profile: VoiceProfile, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(profile)
    with out_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Voice-profile bootstrap wizard")
    parser.add_argument("--rep", required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    samples = load_history(args.history)
    ranked = top_for_review(samples)
    print(f"Loaded {len(samples)} samples; presenting top {len(ranked)} by signal density.\n")
    favorites: list[TextSample] = []
    for i, r in enumerate(ranked, start=1):
        print(f"[{i:02d}] (density={r.density:.3f}) {r.sample.text[:140]}")
        keep = input("  keep? [y/N] ").strip().lower()
        if keep == "y":
            favorites.append(r.sample)
    profile = distill(args.rep, favorites)
    out_path = args.out or Path(f"config/reps/{args.rep}.yaml")
    write_profile(profile, out_path)
    print(f"\nWrote distilled voice profile to {out_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
