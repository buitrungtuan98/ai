"""Pre-render script quality gate (ADR-079) — kill slop where it costs one AI call, not a render.

The vision QC judges the finished VIDEO, which means a bad script is only caught after 30-60 minutes
of CPU work on a box that renders exactly one video at a time. This gate sits between "the AI wrote
a script" and "TTS starts", and is deliberately deterministic (0 AI calls): it catches the failure
modes an LLM writing episode 14 of the same topic actually exhibits —

  * writing the same episode again (synopsis memory steers the premise, not the sentences);
  * filler that reads as AI slop ("hãy cùng tìm hiểu", "in this video");
  * a hook that rambles past the seconds where Shorts/Reels viewers decide to stay;
  * re-using a published title (a spam signal to both platforms).

Verdicts: "block" (regenerate once with the issues as avoid-notes, then fail honestly — never a
silent loop, per the ADR-076 cap discipline) · "warn" (render, but carry the warnings into the
review metadata so a human or the review autopilot sees them) · "ok".
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# ── Thresholds ────────────────────────────────────────────────────────────────
NGRAM_N = 3
BLOCK_SIMILARITY = 0.50   # this much 3-gram overlap with a previous episode = the same episode
WARN_SIMILARITY = 0.35
CLICHE_WARN_COUNT = 2     # distinct clichés in one script before it smells generated
HOOK_MAX_WORDS = 26       # a first sentence longer than this has spent the decisive seconds
RECENT_EPISODES = 8       # how many previous episodes a new script is compared against

# Fillers that mark machine-written narration. Matched on normalized text (lowercase, diacritics
# folded) so "Hãy cùng tìm hiểu" and "hay cung tim hieu" both hit. Operators extend this list from
# Settings (`settings_json["slop_blacklist"]`, one phrase per line) — merged, never replaced.
DEFAULT_CLICHES = (
    # Vietnamese
    "hay cung tim hieu", "hay cung kham pha", "trong video nay", "ban co biet rang",
    "khong the tin duoc", "that khong the tin", "chao mung cac ban", "dung quen like va dang ky",
    "hay theo doi den cuoi", "mot dieu thu vi la",
    # English
    "in this video", "let's dive in", "let's explore", "did you know that", "unbelievable but true",
    "welcome back to", "don't forget to like", "stay tuned until the end", "without further ado",
    "in today's video",
)


def normalize(text: str) -> str:
    """Lowercase, diacritics folded (đ→d too — NFD leaves it), punctuation stripped, spaces folded.
    All comparisons happen in this space so cosmetic edits can't dodge the gate."""
    text = (text or "").lower().replace("đ", "d")
    text = "".join(c for c in unicodedata.normalize("NFD", text)
                   if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def word_ngrams(text: str, n: int = NGRAM_N) -> set[tuple[str, ...]]:
    words = normalize(text).split()
    if len(words) < n:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of word 3-gram sets — order-aware enough to catch rewording-by-shuffling,
    cheap enough to run on every script against every recent episode."""
    ga, gb = word_ngrams(a), word_ngrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def without_phrases(text: str, phrases: tuple[str, ...] = ()) -> str:
    """`normalize(text)` with `phrases` removed — the comparison space for a script whose opening and
    closing lines are FORCED branding (ADR-089).

    A campaign with signature catchphrases makes every episode start and end with the same sentence,
    on purpose, and the scriptwriter is not allowed to drop them. Counting those shared words as
    self-repetition added a constant to every similarity score that no regeneration could remove:
    measured on this module, two genuinely different scripts score 0.00, and the same pair carrying
    one catchphrase pair scores 0.24 at ~65 words and 0.49 at quote length — a hair under the 0.50
    block, so the gate was one short episode away from being unpassable. `normalize` is idempotent,
    so the output of this function is safe to hand to `similarity`."""
    out = normalize(text)
    for phrase in phrases:
        needle = normalize(phrase)
        if needle:
            out = out.replace(needle, " ")
    return re.sub(r"\s+", " ", out).strip()


def first_sentence(text: str) -> str:
    m = re.split(r"(?<=[.!?…])\s+", (text or "").strip(), maxsplit=1)
    return m[0] if m else ""


@dataclass
class GateReport:
    verdict: str                       # "ok" | "warn" | "block"
    issues: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"


def merged_cliches(extra: str | None) -> tuple[str, ...]:
    """DEFAULT_CLICHES + the operator's own lines (already-normalized matching, so store raw)."""
    own = tuple(normalize(ln) for ln in (extra or "").splitlines() if ln.strip())
    return DEFAULT_CLICHES + tuple(p for p in own if p)


def check_script(narration: str, title: str, *, recent: list[dict],
                 cliches: tuple[str, ...] = DEFAULT_CLICHES,
                 content_style: str = "story",
                 strip_phrases: tuple[str, ...] = ()) -> GateReport:
    """Judge one script against this campaign's recent episodes. `recent` = [{narration, title,
    episode}] newest-last; entries may be partial (old tasks predate the stored fingerprint) —
    missing data narrows the check, it never fails it.

    Quote mode keeps the self-repetition and title checks (a re-generated poem is still a repeat)
    but skips the cliché/hook heuristics — they are tuned for narrated story pacing.

    `strip_phrases` are lines the campaign FORCES into every episode (its catchphrases): they are
    removed from both sides before anything is compared, because repetition the operator mandated is
    not the repetition this gate exists to catch (ADR-089)."""
    issues: list[str] = []
    verdict = "ok"
    # Compared in the phrase-stripped space; `normalize` is idempotent, so this is also what the
    # cliché scan reads (a catchphrase must not be able to trip the operator's own blacklist).
    narration_cmp = without_phrases(narration, strip_phrases)

    def worst(v: str) -> None:
        nonlocal verdict
        order = {"ok": 0, "warn": 1, "block": 2}
        if order[v] > order[verdict]:
            verdict = v

    # 1. Self-repetition — the campaign writing the same episode again.
    for prev in recent:
        prev_narration = prev.get("narration") or ""
        if not prev_narration:
            continue
        sim = similarity(narration_cmp, without_phrases(prev_narration, strip_phrases))
        if sim >= BLOCK_SIMILARITY:
            issues.append(f"nearly repeats episode {prev.get('episode', '?')} "
                          f"({round(sim * 100)}% of its phrasing) — write a genuinely new episode")
            worst("block")
            break
        if sim >= WARN_SIMILARITY:
            issues.append(f"echoes episode {prev.get('episode', '?')} "
                          f"({round(sim * 100)}% shared phrasing)")
            worst("warn")

    # 2. Title duplicate — identical (normalized) to an already-made episode's title.
    wanted = normalize(title)
    if wanted:
        for prev in recent:
            if normalize(prev.get("title") or "") == wanted:
                # The title itself is quoted back (ADR-089): the regenerating model never sees the
                # earlier episodes' titles, only their synopses, so "title duplicates episode 12"
                # asked it to avoid something it could not look up — and the most natural title for
                # a topic is exactly the one it just picked.
                issues.append(f"title duplicates episode {prev.get('episode', '?')} "
                              f"(“{(prev.get('title') or '').strip()[:80]}”) — platforms read "
                              "repeated titles as spam; title this episode differently")
                worst("block")
                break

    if content_style != "quote":
        norm_narration = narration_cmp
        # 3. Cliché filler.
        hits = [c for c in cliches if c in norm_narration]
        if len(hits) >= CLICHE_WARN_COUNT:
            issues.append("generic filler phrases: " + ", ".join(f"“{h}”" for h in hits[:4]))
            worst("warn")
        # 4. Hook length — the stay-or-swipe seconds spent on one meandering sentence.
        hook = first_sentence(narration)
        if len(normalize(hook).split()) > HOOK_MAX_WORDS:
            issues.append(f"the opening sentence runs {len(normalize(hook).split())} words — "
                          f"the hook must land inside ~{HOOK_MAX_WORDS}")
            worst("warn")

    return GateReport(verdict, issues)


def script_fingerprint(script) -> dict:
    """The compact record a finished episode leaves behind for future gates: full narration text +
    the variant-A title. Persisted in Task.render_json on success (the full script checkpoint is
    deliberately consumed there — this survives it)."""
    narration = " ".join(s.narration for s in script.scenes)
    title = script.metadata_variations[0].title if script.metadata_variations else ""
    return {"narration": narration, "title": title}
