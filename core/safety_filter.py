"""Content safety and platform-compliance guardrails.

This module owns ALL brand-safety / Terms-of-Service policy (SRP) — that logic never leaks into the
render code. Two responsibilities:

1. `filter_text` — a profanity / brand-safety filter that removes or masks blacklisted terms in the
   narration BEFORE it reaches TTS (avoids demonetization-trigger words). Operators extend the word
   lists per language.
2. The **variation policy gate** (`check_variation_request`) — the content-variation branding feature
   is optional and NOT a detection-evasion tool (ADR-006). The bulk gate defaults OFF and this
   function surfaces the platform-ToS risk of posting many near-identical videos. It exposes no knobs
   framed around "evasion" or "uniqueness".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Minimal, non-slur default lists — operators extend these for their brand/market. Kept small on
# purpose; the point is the mechanism, not an exhaustive lexicon.
DEFAULT_BLACKLIST: dict[str, set[str]] = {
    "en": {"damn", "hell", "crap", "stupid"},
    "vi": {"đm", "vãi"},
    "es": {"mierda", "idiota"},
}

# Platform ToS: how many near-identical videos from an identical source before we refuse without an
# explicit operator override.
BULK_IDENTICAL_THRESHOLD = 1

_TOS_NOTE = (
    "Note: mass-posting near-identical content can violate platform Terms of Service regardless of "
    "byte-level differences. Ensure genuine originality and comply with each platform's policies."
)


@dataclass
class FilterResult:
    clean_text: str
    replaced: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.replaced)


def _compile(terms: set[str]) -> re.Pattern[str] | None:
    if not terms:
        return None
    # Whole-word, case-insensitive. Sort longest-first so multi-word terms match before parts.
    alts = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


def filter_text(
    text: str,
    language: str = "en",
    *,
    extra_terms: set[str] | None = None,
    mode: str = "remove",
) -> FilterResult:
    """Remove ('remove') or star-mask ('mask') blacklisted terms. Returns cleaned text + hits."""
    terms = set(DEFAULT_BLACKLIST.get(language, set()))
    if extra_terms:
        terms |= {t.lower() for t in extra_terms}
    pattern = _compile(terms)
    if pattern is None:
        return FilterResult(clean_text=text)

    hits: list[str] = []

    def _sub(m: re.Match[str]) -> str:
        hits.append(m.group(0))
        if mode == "mask":
            word = m.group(0)
            return word[0] + "*" * (len(word) - 1)
        return ""  # remove

    cleaned = pattern.sub(_sub, text)
    if mode == "remove":
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return FilterResult(clean_text=cleaned, replaced=hits)


def contains_blacklisted(text: str, language: str = "en", *, extra_terms: set[str] | None = None) -> bool:
    return filter_text(text, language, extra_terms=extra_terms).changed


@dataclass
class PolicyResult:
    allowed: bool
    warnings: list[str] = field(default_factory=list)


def check_variation_request(
    *,
    num_videos: int,
    identical_source: bool,
    allow_bulk_variation: bool = False,
) -> PolicyResult:
    """Gate the optional content-variation feature.

    Refuses to emit many videos that differ ONLY by cosmetic variation from an identical source
    unless the operator has explicitly opted in — and always surfaces the ToS risk.
    """
    warnings = [_TOS_NOTE]
    if identical_source and num_videos > BULK_IDENTICAL_THRESHOLD and not allow_bulk_variation:
        warnings.append(
            f"Refused: {num_videos} near-identical videos requested from an identical source with "
            "bulk variation disabled. Set allow_bulk_variation only for legitimate branding/testing."
        )
        return PolicyResult(allowed=False, warnings=warnings)
    return PolicyResult(allowed=True, warnings=warnings)


def assert_licensed_footage(source: str) -> None:
    """Only footage from a licensed, free-to-use provider (Pexels) is permitted. Guards against
    accidentally sourcing copyrighted material."""
    if source.lower() != "pexels":
        raise ValueError(
            f"Footage source '{source}' is not an approved licensed provider (expected 'pexels')."
        )
