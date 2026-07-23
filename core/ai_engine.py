"""Gemini script + metadata generation.

DRY: one primitive — `generate_structured` — talks to Gemini and returns a validated pydantic
object. `generate_script` and `regenerate_metadata` are thin callers reusing it. The pydantic model
is the single source of truth for the output shape; we ask Gemini for JSON, then validate on our
side (portable across SDK versions and trivially mockable in tests via `_call_gemini`).

Robustness: on a parse/validation failure we retry with a "repair" turn that feeds the error back.
`finish_reason` SAFETY/MAX_TOKENS are handled distinctly from parse errors.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

from core.config import settings
from core.usage import record_ai_call

logger = logging.getLogger(__name__)

Language = Literal["vi", "en", "es"]
T = TypeVar("T", bound=BaseModel)

# GEMINI_MODEL may be a single model or a comma-separated FALLBACK CHAIN
# (e.g. "gemini-3.1-flash-lite,gemini-flash-latest"): when a model is retired (404) or its daily
# free quota is spent, generation automatically falls through to the next one.
DEFAULT_MODEL = settings.GEMINI_MODEL
_BACKOFF_BASE_SECONDS = 1.0  # patched to 0 in tests
_RATE_LIMIT_BACKOFF_SECONDS = 30.0  # per-MINUTE 429s need far longer than the standard backoff


def model_chain(model: str) -> list[str]:
    """Split a (possibly comma-separated) model setting into an ordered fallback chain."""
    return [m.strip() for m in model.split(",") if m.strip()] or [model]


# Curated free-tier rate-limit annotations for the Credentials model picker. ADVISORY numbers:
# Google moves them and paid tiers differ — the authoritative table is
# https://ai.google.dev/gemini-api/docs/rate-limits (the picker links there).
CATALOG_AS_OF = "2026-07, free tier"
GEMINI_MODEL_CATALOG: dict[str, dict] = {
    "gemini-flash-lite-latest": {
        "rpm": 15, "tpm": "250K", "rpd": 500,
        "note": "Alias — always the newest Flash-Lite. Cheapest, biggest daily quota: the "
                "factory workhorse (recommended primary).",
    },
    "gemini-3.1-flash-lite": {
        "rpm": 15, "tpm": "250K", "rpd": 500,
        "note": "Current Flash-Lite generation, pinned. Same quota as the alias but never "
                "silently changes under you.",
    },
    "gemini-flash-latest": {
        "rpm": 10, "tpm": "250K", "rpd": 250,
        "note": "Alias — always the newest Flash. Smarter than Lite, smaller daily quota: good "
                "fallback entry.",
    },
    "gemini-3.5-flash": {
        "rpm": 10, "tpm": "250K", "rpd": 20,
        "note": "Newest Flash, pinned — observed free-tier RPD is VERY low (20); poor fit as a "
                "primary for a factory.",
    },
    "gemini-2.0-flash": {
        "rpm": None, "tpm": None, "rpd": 0,
        "note": "Retired from the free tier (observed daily limit 0) — do not use.",
    },
}


def list_gemini_models(*, api_key: str) -> list[dict]:
    """Live model list from the REST models endpoint — one cheap call, NOT metered like
    generation. Returns only models that can generateContent, as
    {id, display_name, description} dicts (the 'models/' prefix stripped)."""
    import requests

    resp = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": api_key, "pageSize": 200},
        timeout=20,
    )
    resp.raise_for_status()  # NOTE: the error text embeds ?key=… — callers must not expose it
    models = []
    for m in resp.json().get("models", []):
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        models.append({
            "id": (m.get("name") or "").removeprefix("models/"),
            "display_name": m.get("displayName") or "",
            "description": (m.get("description") or "")[:300],
        })
    return models


def _is_daily_quota_error(message: str) -> bool:
    """A 429 whose quota_id is the per-DAY free-tier cap. Retrying cannot succeed until the daily
    reset — and every retry burns another request against that same cap."""
    return "429" in message and "PerDay" in message


def _is_model_not_found(message: str) -> bool:
    """A 404 for the model itself (retired/renamed). Deterministic — retrying is pure waste."""
    return "404" in message and "not found" in message.lower()


# ── Output schemas (the single source of truth for shape) ────────────────────
class Scene(BaseModel):
    index: int
    narration: str = Field(min_length=1, description="Exact text the TTS voice will speak.")
    caption_hook: str | None = Field(
        default=None, max_length=60, description="Optional short on-screen headline for the scene."
    )
    pexels_keywords: list[str] = Field(
        min_length=1, max_length=4,
        description="Stock-footage search terms, best first — ALWAYS in English regardless of the "
                    "narration language (stock libraries are indexed in English). Prefer 2-4 terms "
                    "describing DISTINCT visual concepts (different shots), not synonyms, so the "
                    "scene can cut between varied footage.",
    )


class _SynopsisMixin(BaseModel):
    # Episode memory: a one-line summary stored per episode and fed back into later prompts so the
    # series never repeats itself (no_repeat) or can genuinely continue (serial). REQUIRED
    # (min_length=1): when it was optional the model sometimes omitted it, leaving episodes with
    # no memory — which silently broke continuity for every episode after them.
    synopsis: str = Field(
        min_length=1, max_length=300,
        description="One-sentence summary of THIS episode's specific premise/content. Required.",
    )


class MetadataVariation(BaseModel):
    variant: Literal["A", "B", "C"]
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(min_length=3, max_length=15)


class VideoScript(_SynopsisMixin):
    language: Language
    topic: str
    # Up to 8 scenes covers a short; long-form (multi-minute) videos need many more, so the cap is
    # 40 and the prompt asks for a count that fits the target duration.
    scenes: list[Scene] = Field(min_length=3, max_length=40)
    metadata_variations: list[MetadataVariation] = Field(min_length=3, max_length=3)


class MetadataSet(BaseModel):
    metadata_variations: list[MetadataVariation] = Field(min_length=3, max_length=3)


class EpisodeBrief(BaseModel):
    """A research/creative brief for ONE episode — the specific substance a script is built on, so
    narration carries real detail instead of generic filler. Generated only in 'deep' mode."""
    angle: str = Field(min_length=1, max_length=200,
                       description="The single sharpest angle/hook idea for THIS episode.")
    facts: list[str] = Field(min_length=3, max_length=8,
                             description="Concrete, specific facts — real names, dates, numbers, "
                                         "places — the narration must use. No vague generalities.")
    arc: str = Field(min_length=1, max_length=600,
                     description="Emotional beat map in a few sentences: hook → build → payoff → "
                                 "cliffhanger.")


class GeminiBlockedError(RuntimeError):
    """Gemini refused the request (finish_reason SAFETY/RECITATION)."""


class GeminiError(RuntimeError):
    """Generation failed after all retries."""


# ── The raw call (mock this in tests) ────────────────────────────────────────
def _call_gemini(
    *,
    api_key: str,
    model: str,
    prompt: str,
    system_prompt: str | None,
    temperature: float,
    max_output_tokens: int,
) -> str:
    """Single point that imports and calls the Gemini SDK. Returns raw response text.

    Note: current flash models spend "thinking" tokens that count against max_output_tokens, so
    limits must be generous or the JSON gets truncated (EOF-while-parsing) — see the callers.
    """
    import google.generativeai as genai

    record_ai_call()  # quota meter: every attempt counts against the daily budget
    genai.configure(api_key=api_key)
    gen_model = genai.GenerativeModel(
        model_name=model,
        system_instruction=system_prompt,
        generation_config={
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "response_mime_type": "application/json",
        },
    )
    resp = gen_model.generate_content(prompt)

    # Distinguish a safety block from a normal empty response.
    candidates = getattr(resp, "candidates", None) or []
    for cand in candidates:
        reason = getattr(cand, "finish_reason", None)
        if reason and str(reason).upper().endswith(("SAFETY", "RECITATION")):
            raise GeminiBlockedError(f"Gemini blocked the response (finish_reason={reason}).")
    # A prompt-level block yields NO candidates (reason lives in prompt_feedback). Reading resp.text
    # then raises a bare ValueError that the retry loop would misread as a repairable parse error —
    # surface it as a non-retryable block instead.
    if not candidates:
        feedback = getattr(resp, "prompt_feedback", None)
        raise GeminiBlockedError(f"Gemini returned no candidates (prompt_feedback={feedback}).")
    return resp.text


# ── The DRY primitive ────────────────────────────────────────────────────────
def generate_structured(
    *,
    prompt: str,
    schema: type[T],
    api_key: str,
    system_prompt: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.7,
    max_output_tokens: int = 8192,  # generous: thinking tokens + full JSON must both fit
    max_retries: int = 3,
) -> T:
    """Call Gemini and return a validated instance of `schema`.

    `model` may be a comma-separated fallback chain: a retired model (404) or an exhausted daily
    quota fails over to the next entry automatically, so a Google-side model retirement or a spent
    free tier degrades instead of halting the factory."""
    models = model_chain(model)
    last: GeminiError | None = None
    for i, m in enumerate(models):
        try:
            return _generate_structured_single(
                prompt=prompt, schema=schema, api_key=api_key, system_prompt=system_prompt,
                model=m, temperature=temperature, max_output_tokens=max_output_tokens,
                max_retries=max_retries,
            )
        except GeminiError as exc:
            msg = str(exc)
            if i < len(models) - 1 and ("daily quota" in msg or "model not found" in msg):
                logger.warning("Model %s unavailable — falling back to %s", m, models[i + 1])
                last = exc
                continue
            raise
    raise last if last is not None else GeminiError("no model in the chain succeeded")


def _generate_structured_single(
    *,
    prompt: str,
    schema: type[T],
    api_key: str,
    system_prompt: str | None,
    model: str,
    temperature: float,
    max_output_tokens: int,
    max_retries: int,
) -> T:
    """One model's attempt loop: retries with backoff and a repair turn on JSON/validation errors;
    fails FAST (no retry burn) on deterministic errors — daily quota spent, model not found."""
    schema_hint = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    base_prompt = (
        f"{prompt}\n\n"
        "Return ONLY a single JSON object (no markdown, no prose) that validates against this "
        f"JSON Schema:\n{schema_hint}"
    )
    last_error: Exception | None = None
    convo = base_prompt

    for attempt in range(max_retries):
        rate_limited = False
        try:
            raw = _call_gemini(
                api_key=api_key,
                model=model,
                prompt=convo,
                system_prompt=system_prompt,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            return schema.model_validate_json(_strip_code_fence(raw))
        except GeminiBlockedError:
            raise  # not retryable — the content itself was refused
        except (ValidationError, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            logger.warning("Gemini output invalid (attempt %d/%d): %s", attempt + 1, max_retries, exc)
            convo = (
                f"{base_prompt}\n\nYour previous output failed validation with error:\n{exc}\n"
                "Return ONLY corrected, valid JSON."
            )
        except Exception as exc:  # noqa: BLE001 — transient API/network errors are retryable
            last_error = exc
            msg = str(exc)
            if _is_daily_quota_error(msg):
                # Fail FAST: the daily free-tier cap is spent, so retries cannot succeed today and
                # each one would burn yet another request against the same cap (quota efficiency).
                raise GeminiError(
                    "Gemini daily quota exhausted — resets ~midnight US-Pacific; see RUNBOOK "
                    f"'Gemini API quota & cost'. {exc}"
                ) from exc
            if _is_model_not_found(msg):
                # Fail FAST: a retired/renamed model 404s deterministically — retrying is waste.
                raise GeminiError(f"Gemini model not found ({model}) — update GEMINI_MODEL. {exc}") from exc
            rate_limited = "429" in msg
            logger.warning("Gemini call failed (attempt %d/%d): %s", attempt + 1, max_retries, exc)

        if attempt < max_retries - 1 and _BACKOFF_BASE_SECONDS:
            delay = _BACKOFF_BASE_SECONDS * (2**attempt)
            if rate_limited:
                # Per-minute rate limits (RPM/TPM) recover on their own — but only if we wait
                # meaningfully longer than the 1-2s parse-error backoff.
                delay = max(delay, _RATE_LIMIT_BACKOFF_SECONDS)
            time.sleep(delay)

    raise GeminiError(f"generate_structured failed after {max_retries} attempts: {last_error}")


def _strip_code_fence(text: str) -> str:
    """Tolerate a ```json ... ``` fence if the model adds one despite JSON mode — including a
    single-line fence with no newline after the language tag."""
    s = text.strip()
    if not s.startswith("```"):
        return s
    s = s[3:]                       # drop the opening ```
    if s[:4].lower() == "json":     # optional language tag
        s = s[4:]
    if s.startswith("\n"):
        s = s[1:]
    s = s.rsplit("```", 1)[0]       # drop the closing fence
    return s.strip()


# ── AI campaign designer (propose a whole campaign from a title, or from scratch) ─
class CampaignProposal(BaseModel):
    """A complete, ready-to-review campaign configuration proposed by Gemini."""
    topic_name: str = Field(min_length=1, max_length=120)
    language: Language
    video_format: Literal["short", "long"] = Field(
        default="short",
        description="'short' = vertical clips ≤3 min (the usual choice); 'long' = horizontal 16:9 "
                    "multi-minute videos. Only choose 'long' when the concept truly needs depth.")
    total_episodes: int = Field(ge=1, le=365)
    persona: str = Field(min_length=1)
    style_examples: str = ""
    catchphrase_open: str = ""
    catchphrase_close: str = ""
    continuity: Literal["none", "no_repeat", "serial"] = "none"
    script_depth: Literal["standard", "deep"] = Field(
        default="standard",
        description="'deep' adds a research/brief pass for fact-rich storytelling (one extra call "
                    "per episode); 'standard' is one-pass. Prefer 'deep' for narrative/history/"
                    "story channels, 'standard' for quick punchy facts.")
    voice: str = ""
    rate_pct: int = Field(default=0, ge=-20, le=20)
    subtitle_style: Literal["word", "line"] = "word"
    caption_theme: Literal["classic", "highlight", "boxed", "neon"] = "highlight"
    color_grade: Literal["none", "cinematic", "warm", "cool", "vivid", "noir"] = "none"
    motion: Literal["on", "off"] = "on"
    music_mode: Literal["none", "auto"] = "auto"
    music_mood: str = ""
    ab_testing: bool = True
    privacy: Literal["public", "unlisted", "private"] = "public"
    cta: str = ""
    title_prefix: str = Field(default="", max_length=40,
                              description="Optional short catchy channel mark prepended to titles, e.g. '🔥 SỬ VIỆT |'. Often empty.")
    posting_slots: str = Field(default="", description="One daily slot as HH:MM, or empty.")
    posting_days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(
        default_factory=list, description="Days to publish on; EMPTY means every day (the usual choice).")
    duration_min_s: int = Field(default=0, ge=0, le=180,
                                description="Target min spoken seconds per episode (0 = unset).")
    duration_max_s: int = Field(default=0, ge=0, le=180,
                                description="Target max spoken seconds per episode (0 = unset).")
    rationale: str = Field(default="", max_length=400, description="One sentence on the angle.")


# Voices the designer may choose from — derived from the ONE curated catalog in core/tts.py
# (DRY: the campaign form's per-language dropdown uses the same list) and validated server-side
# so the model can't invent an unusable voice name that would break TTS.
def _proposable_voices() -> dict[str, list[str]]:
    from core.tts import VOICE_CHOICES

    return {lang: [vid for vid, _label in voices] for lang, voices in VOICE_CHOICES.items()}


PROPOSABLE_VOICES: dict[str, list[str]] = _proposable_voices()


def propose_campaign(
    *,
    topic: str | None = None,
    language: str | None = None,
    api_key: str,
    model: str = DEFAULT_MODEL,
    nonce: int = 0,
) -> CampaignProposal:
    """Design one complete, standout campaign config. With no topic, it invents a concept; high
    temperature + a variation `nonce` make each call distinct. The chosen voice is validated
    against PROPOSABLE_VOICES (an invented voice is dropped to the language default)."""
    topic_line = (f'Design the campaign around this title/topic: "{topic}".' if topic
                  else "Invent a fresh, specific, non-generic channel concept and topic yourself.")
    lang_line = (f"The target language MUST be '{language}'." if language in ("vi", "en", "es")
                 else "Choose the most fitting target language (vi, en or es).")
    voices = ", ".join(v for vs in PROPOSABLE_VOICES.values() for v in vs)
    prompt = (
        "You are a senior short-form video channel strategist. Propose ONE complete, standout "
        "campaign configuration for an automated vertical-shorts factory. "
        f"{topic_line} {lang_line} "
        "Make it distinctive and genuinely good — a real creator's channel, never a bland template. "
        "Choose: a vivid, specific persona (region/age/speech habits, written in the target "
        "language); 2-3 short style-example lines in that voice; signature opening and closing "
        "catchphrases; a caption theme and colour grade that fit the mood; a music mood in a few "
        "English words (music_mode 'auto' unless silence truly fits, then 'none'); an edge-tts "
        f"voice chosen ONLY from this list: {voices}; a rate_pct — TTS sounds most natural "
        "slightly slowed, so prefer -8..-3 for storytelling personas and 0 only for fast-paced "
        "formats; a sensible total_episodes; one daily posting slot as HH:MM; a continuity mode; "
        "a spoken length range in seconds fitting the format (e.g. 25-45 for punchy facts, "
        "60-120 for stories); a script_depth ('deep' for narrative/history/story channels that "
        "benefit from a research pass, 'standard' for quick punchy facts); and a short "
        "call-to-action. "
        f"Variation seed {nonce}: make this proposal clearly different from any previous one. "
        "Include a one-sentence 'rationale' for the creative angle."
    )
    proposal = generate_structured(
        prompt=prompt, schema=CampaignProposal, api_key=api_key, model=model,
        temperature=1.1,  # inherits the generous default token budget (thinking + JSON)
    )
    allowed = {v for vs in PROPOSABLE_VOICES.values() for v in vs}
    if proposal.voice not in allowed:
        proposal.voice = ""  # model invented a voice → fall back to the app default
    return proposal


# Rough spoken words-per-second of the default edge-tts voices (heuristic; the ±20% tolerance in
# the length check absorbs the error). Vietnamese tokens are syllables → higher rate.
WORDS_PER_SECOND: dict[str, float] = {"en": 2.4, "vi": 3.1, "es": 2.6}


def estimate_speech_seconds(text: str, language: str, rate_pct: int = 0) -> float:
    """Estimate how long `text` takes to speak with the campaign's voice settings."""
    wps = WORDS_PER_SECOND.get(language, 2.4) * (1 + rate_pct / 100)
    return len(text.split()) / max(wps, 0.1)


def series_hashtag(topic: str) -> str:
    """A stable, ASCII CamelCase hashtag derived from the series topic — computed in code (not by
    the model) so every episode of a campaign carries the SAME tag and the series stays findable
    even though titles never mention the series name. E.g. 'Lịch sử VN: Nhà Trần' → '#LichSuVNNhaTran'."""
    import re
    import unicodedata

    ascii_topic = topic.replace("đ", "d").replace("Đ", "D")
    ascii_topic = unicodedata.normalize("NFKD", ascii_topic)
    ascii_topic = "".join(c for c in ascii_topic if not unicodedata.combining(c))
    words = re.findall(r"[A-Za-z0-9]+", ascii_topic)
    tag = "".join(w[:1].upper() + w[1:] for w in words)[:30]
    return f"#{tag}" if tag else "#Shorts"


# ── Thin callers ─────────────────────────────────────────────────────────────
_SYSTEM_BY_LANG: dict[str, str] = {
    "en": "You are a short-form video scriptwriter. Write narration in natural spoken English.",
    "vi": "Bạn là người viết kịch bản video ngắn. Viết lời dẫn bằng tiếng Việt nói tự nhiên.",
    "es": "Eres un guionista de videos cortos. Escribe la narración en español hablado y natural.",
}

# Anti-"AI-tell" rules applied to EVERY generation (script, titles, descriptions — and therefore
# subtitles, which are the narration verbatim). The goal is natural spoken language, not deception:
# operators must still follow platform synthetic-content disclosure rules (see RUNBOOK).
NATURAL_STYLE_RULES = (
    "Write exactly like a real person talking to camera, not like an essay or a listicle:\n"
    "- Spoken register: contractions, short sentences mixed with long ones, natural filler where "
    "it fits the persona. Read it aloud in your head — if it sounds like a blog post, rewrite it.\n"
    "- Use local, everyday expressions of the target language/region; light humor when natural.\n"
    "- NEVER use AI-typical phrasing: no 'let's dive in', 'in conclusion', 'delve', 'unleash', "
    "'game-changer', no numbered-list cadence, no starting every sentence the same way.\n"
    "- Titles and descriptions must sound like a real creator typed them on their phone — "
    "specific and curious, not clickbait-formula. Tags stay plain.\n"
    "- Stay in character 100% of the time, including in titles and descriptions.\n"
    "- THE HOOK RULE: the very first sentence must grab attention within 2 seconds — a question, "
    "a shock, or mid-action. Never open with greetings or introductions (a signature catchphrase "
    "is the only exception, and it must lead straight into the hook).\n"
    "- WRITE FOR THE VOICE: the narration is read aloud by a TTS voice that breathes at "
    "punctuation. Keep sentences SHORT (one idea each). Use commas where a speaker would pause, "
    "periods to land a point, and an ellipsis … for a dramatic beat. Never write long unbroken "
    "clauses. Write numbers, dates and abbreviations the way they should be SPOKEN in the target "
    "language (e.g. 'TP.HCM' → 'Thành phố Hồ Chí Minh', '1428' → 'năm một bốn hai tám' if read as "
    "a year). Avoid parentheses and quote-heavy constructions — they read badly aloud."
)

# Deterministic AI-cliché blacklist per language: the phrases that most loudly mark narration as
# machine-written. They steer generation (injected into the script + critic prompts) AND back a free
# post-draft check (find_cliches) that can force ONE targeted rewrite — the same "cheap gate before
# expensive work" pattern as voice_check and the length-fit check. Not detection evasion (ADR-006):
# the point is natural spoken language; operators still follow platform synthetic-content disclosure.
AI_CLICHES: dict[str, list[str]] = {
    "en": [
        "let's dive in", "let's dive into", "dive into the world", "in this video",
        "in today's video", "delve into", "delve deeper", "buckle up", "game-changer",
        "game changer", "unleash", "unlock the secrets", "in conclusion", "to sum up",
        "without further ado", "little did they know", "but that's not all",
        "the possibilities are endless", "it's important to note", "needless to say",
    ],
    "vi": [
        "hãy cùng tìm hiểu", "hãy cùng khám phá", "chúng ta hãy cùng",
        "trong video này chúng ta sẽ", "trong video hôm nay", "trong bài viết này",
        "không thể phủ nhận rằng", "không thể không nhắc đến", "đừng quên like và subscribe",
        "một điều thú vị là", "nói tóm lại", "có thể nói rằng", "đầu tiên phải kể đến",
    ],
    "es": [
        "vamos a sumergirnos", "en este video", "en el video de hoy", "sin más preámbulos",
        "en conclusión", "para resumir", "no se puede negar que", "es importante destacar",
        "sumérgete en", "pero eso no es todo",
    ],
}


def find_cliches(text: str, language: str) -> list[str]:
    """Return the AI-cliché phrases present in `text` (case-insensitive, order-preserving).
    Deterministic and free — steers generation and gates one targeted rewrite when hits remain."""
    low = text.lower()
    seen: list[str] = []
    for phrase in AI_CLICHES.get(language, AI_CLICHES["en"]):
        if phrase in low and phrase not in seen:
            seen.append(phrase)
    return seen


def compose_system_prompt(
    language: str,
    *,
    custom_system_prompt: str | None = None,
    persona: str | None = None,
    style_examples: str | None = None,
    catchphrase_open: str | None = None,
    catchphrase_close: str | None = None,
    playbook: list[str] | None = None,
    best_examples: list[str] | None = None,
    avoid: list[str] | None = None,
) -> str:
    """Assemble the full character sheet the model writes as. One place (DRY) so script, titles,
    descriptions and (via narration) subtitles all speak with the same human voice — and learn:
    the channel's distilled playbook, its own best-performing examples, and operator avoid-notes."""
    parts = [_SYSTEM_BY_LANG.get(language, _SYSTEM_BY_LANG["en"]), NATURAL_STYLE_RULES]
    if persona:
        parts.append(f"YOUR CHARACTER (stay in this persona everywhere):\n{persona}")
    if style_examples:
        parts.append(
            "STYLE EXAMPLES — mimic the voice, rhythm and vocabulary of these samples "
            f"(do not copy their content):\n{style_examples}"
        )
    if catchphrase_open or catchphrase_close:
        cues = []
        if catchphrase_open:
            cues.append(f"The FIRST scene's narration must open with (or naturally weave in): "
                        f"\"{catchphrase_open}\"")
        if catchphrase_close:
            cues.append(f"The LAST scene's narration must end with (or naturally weave in): "
                        f"\"{catchphrase_close}\"")
        parts.append("SIGNATURE CATCHPHRASES (fans recognise these):\n" + "\n".join(cues))
    if playbook:
        parts.append("CHANNEL PLAYBOOK — lessons learned from this channel's real performance "
                     "data; apply them (they refine tactics, never override the persona):\n"
                     + "\n".join(f"- {p}" for p in playbook[:15]))
    if best_examples:
        parts.append("THIS CHANNEL'S TOP PERFORMERS — write with the same energy (not the same "
                     "content):\n" + "\n".join(f"- {e}" for e in best_examples[:3]))
    if avoid:
        parts.append("AVOID — the operator rejected recent videos for these reasons; do not "
                     "repeat these mistakes:\n" + "\n".join(f"- {a}" for a in avoid[:10]))
    if custom_system_prompt:
        parts.append(custom_system_prompt)
    return "\n\n".join(parts)


_BRIEF_SYSTEM = (
    "You are a meticulous researcher and story producer for a short-form video channel. You surface "
    "the SPECIFIC, verifiable substance a great episode is built on — real names, dates, numbers, "
    "places, cause-and-effect — and shape it into a tight emotional arc. Never pad with generic "
    "filler; if unsure of a fact, choose one you are confident is real rather than inventing."
)


def generate_brief(
    *,
    topic: str,
    language: str,
    episode: int,
    total_episodes: int,
    api_key: str,
    persona: str | None = None,
    continuity: str = "none",
    previous_synopses: list[str] | None = None,
    model: str = DEFAULT_MODEL,
) -> EpisodeBrief:
    """One research call turning a series topic into a fact-rich, arc-shaped brief for THIS episode.
    Deep mode conditions the script call on it so narration carries real detail, not waffle."""
    prev = [s for s in (previous_synopses or []) if s]
    prev_line = ("\n\nAlready covered — pick a DIFFERENT angle and facts:\n"
                 + "\n".join(f"- {s}" for s in prev[-10:])) if prev else ""
    persona_line = f"Persona/voice for context: {persona}\n" if persona else ""
    prompt = (
        f"Research episode {episode} of {total_episodes} for a short-form series about: '{topic}'. "
        f"Target language: {language}.\n{persona_line}"
        "Produce: the single sharpest angle for this one episode; 3-8 concrete facts (real names, "
        "dates, numbers, places) it should be built on; and a short emotional arc "
        "(hook → build → payoff → cliffhanger). Be specific, never generic."
        + prev_line
    )
    return generate_structured(prompt=prompt, schema=EpisodeBrief, api_key=api_key,
                               system_prompt=_BRIEF_SYSTEM, model=model, temperature=0.6)


def build_script_prompt(
    topic: str,
    language: str,
    total_episodes: int,
    episode: int,
    *,
    continuity: str = "none",
    previous_synopses: list[str] | None = None,
    duration_min_s: int | None = None,
    duration_max_s: int | None = None,
    rate_pct: int = 0,
    brief: EpisodeBrief | None = None,
    video_format: str = "short",
) -> str:
    length_line = ""
    if duration_min_s and duration_max_s:
        wps = WORDS_PER_SECOND.get(language, 2.4) * (1 + rate_pct / 100)
        lo, hi = int(duration_min_s * wps), int(duration_max_s * wps)
        length_line = (
            f"TOTAL LENGTH: all scene narrations together must run {duration_min_s}-"
            f"{duration_max_s} seconds when spoken — approximately {lo}-{hi} words in {language}. "
            "Fit the scene count and sentence lengths to this budget. "
        )
    is_long = video_format == "long"
    if is_long:
        format_line = ("Create a HORIZONTAL (16:9) long-form video script for episode "
                       f"{episode} of {total_episodes} in a series about: '{topic}'. ")
        scene_line = ("Produce 12-30 scenes covering the full length — each a beat of the story, "
                      "with narration, an on-screen caption hook that doubles as a chapter title, "
                      "and 1-4 stock-footage keywords. ")
        # Long-form is browsed as a series, so episode/part numbering in the title HELPS discovery.
        title_line = ("TITLE RULES: a strong, specific title; a part/episode number is welcome "
                      "('Part 3', 'Tập 5'). Open with the most curious element. ")
        hashtag_line = (f"voice. End with 3-6 relevant hashtags plus EXACTLY this series hashtag: "
                        f"{series_hashtag(topic)} (fans find the whole series through it).")
    else:
        format_line = ("Create a vertical short-form video script for episode "
                       f"{episode} of {total_episodes} in a series about: '{topic}'. ")
        scene_line = ("Produce 3-6 scenes; each scene has narration the voice will speak, an "
                      "optional short on-screen caption hook, and 1-4 stock-footage keywords. ")
        title_line = ("TITLE RULES (Shorts are discovered one by one — every title must stand "
                      "alone): NEVER put the series/campaign name in the title, and NEVER include "
                      "episode numbering of any form ('Ep 5', 'Tập 3', 'Part 2', '#12'). Open with "
                      "the most curious/emotional element in the first 40 characters; ideally stay "
                      "under 70 characters. Each of the 3 variants takes a genuinely different "
                      "angle (question / bold claim / mid-action). ")
        hashtag_line = (f"voice. End with 3-5 hashtags: relevant topical ones plus #Shorts and "
                        f"EXACTLY this series hashtag: {series_hashtag(topic)} (fans find the whole "
                        "series through it).")
    base = (
        format_line +
        f"Language: {language}. "
        + length_line + scene_line +
        "Also produce exactly 3 distinct A/B metadata variations (variant A/B/C) each with a title "
        "(<=100 chars), a description, and 5-15 tags, all in the same persona/voice. Include a "
        "one-sentence 'synopsis' of this episode's specific premise. Keep it original and engaging. "
        "IMPORTANT: pexels_keywords must be ENGLISH visual search terms (e.g. 'river night fog'), "
        "even when the narration language is not English.\n"
        + title_line + "\n"
        "DESCRIPTION RULES: the FIRST line re-hooks (it is the only line viewers see uncollapsed) "
        "— never start with the series name. Then 1-3 short lines of context in the persona's "
        + hashtag_line
    )
    banned = AI_CLICHES.get(language, AI_CLICHES["en"])
    base += ("\nBANNED PHRASES — never use these AI-tell clichés or close variants anywhere "
             "(narration, titles, description): " + "; ".join(f"'{b}'" for b in banned) + ".")
    if brief is not None:
        facts = "\n".join(f"- {f}" for f in brief.facts)
        base += (
            "\n\nRESEARCH BRIEF — build the script ONLY from this specific substance; weave the "
            "facts in naturally as spoken narration (do not list them):\n"
            f"Angle: {brief.angle}\nFacts:\n{facts}\nArc to follow: {brief.arc}"
        )
    prev = [s for s in (previous_synopses or []) if s]
    if continuity == "no_repeat" and prev:
        listing = "\n".join(f"- {s}" for s in prev)
        base += (
            "\n\nEPISODE MEMORY — these episodes already exist:\n" + listing +
            "\nThis episode MUST have a clearly different premise, angle and details from ALL of "
            "the above. Do not reuse their hooks or twists."
        )
    elif continuity == "serial" and prev:
        listing = "\n".join(f"- Episode so far: {s}" for s in prev[-5:])
        base += (
            "\n\nSERIAL STORY — this is one continuing story:\n" + listing +
            f"\nThe previous episode ended with: \"{prev[-1]}\". Continue DIRECTLY from there — "
            "same characters, same world, advancing the plot. Open with a one-line hook that "
            "reminds viewers where we left off."
        )
    return base


def generate_script(
    *,
    topic: str,
    language: str,
    total_episodes: int,
    episode: int,
    api_key: str,
    custom_system_prompt: str | None = None,
    persona: str | None = None,
    style_examples: str | None = None,
    catchphrase_open: str | None = None,
    catchphrase_close: str | None = None,
    continuity: str = "none",
    previous_synopses: list[str] | None = None,
    playbook: list[str] | None = None,
    best_examples: list[str] | None = None,
    avoid: list[str] | None = None,
    self_critique: bool = True,
    duration_min_s: int | None = None,
    duration_max_s: int | None = None,
    rate_pct: int = 0,
    script_depth: str = "standard",
    video_format: str = "short",
    model: str = DEFAULT_MODEL,
) -> VideoScript:
    system = compose_system_prompt(
        language,
        custom_system_prompt=custom_system_prompt,
        persona=persona,
        style_examples=style_examples,
        catchphrase_open=catchphrase_open,
        catchphrase_close=catchphrase_close,
        playbook=playbook,
        best_examples=best_examples,
        avoid=avoid,
    )
    # Deep mode: one research pass first, so the script is built from specific facts + a real arc
    # instead of one-shot waffle. Optional and fail-open — a brief failure never blocks the episode.
    brief = None
    if script_depth == "deep":
        try:
            brief = generate_brief(
                topic=topic, language=language, episode=episode, total_episodes=total_episodes,
                api_key=api_key, persona=persona, continuity=continuity,
                previous_synopses=previous_synopses, model=model,
            )
        except Exception:  # noqa: BLE001 — the brief is an enhancement, not a gate
            logger.warning("Episode brief generation failed — proceeding without it.")
    prompt = build_script_prompt(
        topic, language, total_episodes, episode,
        continuity=continuity, previous_synopses=previous_synopses,
        duration_min_s=duration_min_s, duration_max_s=duration_max_s, rate_pct=rate_pct,
        brief=brief, video_format=video_format,
    )
    temperature = 0.85 if continuity != "none" else 0.7
    script = generate_structured(
        prompt=prompt, schema=VideoScript, api_key=api_key, system_prompt=system,
        model=model, temperature=temperature,
    )

    if self_critique:
        # Generator→critic loop: one harsh editorial review; on 'rewrite', one revision with the
        # concrete issues injected. A critic failure never blocks the video (best-effort gate).
        try:
            review = critique_script(script, api_key=api_key, persona=persona,
                                     previous_synopses=previous_synopses, model=model)
            if review.verdict != "pass":
                logger.info("Critic requested a rewrite (hook=%d natural=%d persona=%d fresh=%d "
                            "grammar=%d)",
                            review.hook_score, review.natural_score, review.persona_score,
                            review.fresh_score, review.grammar_score)
                fixes = "\n".join(f"- {i}" for i in review.issues) \
                    or "- strengthen the hook and spoken rhythm"
                script = generate_structured(
                    prompt=prompt + "\n\nAn editor reviewed your previous draft and demands these "
                                    "fixes (rewrite fully, do not patch):\n" + fixes,
                    schema=VideoScript, api_key=api_key, system_prompt=system,
                    model=model, temperature=temperature,
                )
        except Exception:  # noqa: BLE001
            logger.warning("Critic/rewrite failed — keeping the current draft.")

    # Length fit (deterministic word-count check; costs one extra call ONLY when the draft misses
    # the campaign's target range by more than 20%). The estimate is heuristic — the tolerance
    # absorbs voice-speed variance; the true duration is measured at TTS time.
    if duration_min_s and duration_max_s:
        est = estimate_speech_seconds(
            " ".join(s.narration for s in script.scenes), language, rate_pct)
        if est < duration_min_s * 0.8 or est > duration_max_s * 1.2:
            need = "EXPAND it with more substance" if est < duration_min_s else "CUT it down"
            logger.info("Script speaks ~%.0fs, target %d-%ds — requesting a length fix",
                        est, duration_min_s, duration_max_s)
            try:
                script = generate_structured(
                    prompt=prompt + f"\n\nYour previous draft speaks for about {est:.0f} seconds, "
                                    f"but the target is {duration_min_s}-{duration_max_s} seconds. "
                                    f"{need} to fit the target — rewrite fully, keeping the same "
                                    "premise, persona and quality.",
                    schema=VideoScript, api_key=api_key, system_prompt=system,
                    model=model, temperature=temperature,
                )
            except Exception:  # noqa: BLE001
                logger.warning("Length-fix rewrite failed — keeping the current draft.")

    # Deterministic AI-cliché gate (free): if the final draft still contains blacklisted phrases,
    # force ONE targeted rewrite naming them. Fail-open — a rewrite failure keeps the draft.
    hits = find_cliches(" ".join(s.narration for s in script.scenes), language)
    if hits:
        logger.info("Draft contains AI clichés %s — requesting one targeted rewrite", hits)
        try:
            script = generate_structured(
                prompt=prompt + "\n\nYour draft used these banned AI-tell phrases: "
                       + "; ".join(f"'{h}'" for h in hits)
                       + ". Rewrite fully in natural spoken language, removing them and any close "
                         "variant while keeping the same premise, persona and quality.",
                schema=VideoScript, api_key=api_key, system_prompt=system,
                model=model, temperature=temperature,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Cliché-fix rewrite failed — keeping the current draft.")
    return script


# ── Vision judging (Auto-QC: the machine watches the footage and the output) ─
_AUDIO_MIME_BY_EXT: dict[str, str] = {
    ".aac": "audio/aac", ".mp3": "audio/mp3", ".wav": "audio/wav",
    ".ogg": "audio/ogg", ".flac": "audio/flac",
}


def _call_gemini_vision(
    *,
    api_key: str,
    model: str,
    prompt: str,
    image_paths: list[str],
    audio_path: str | None = None,
    temperature: float = 0.2,
    max_output_tokens: int = 2048,  # room for thinking tokens + the small verdict JSON
) -> str:
    """Single point that calls Gemini with images (and optionally one audio track).
    Returns raw response text."""
    import os

    import google.generativeai as genai
    from PIL import Image

    record_ai_call()  # quota meter
    genai.configure(api_key=api_key)
    gen_model = genai.GenerativeModel(
        model_name=model_chain(model)[0],  # vision runs single-shot on the chain's primary model
        generation_config={
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "response_mime_type": "application/json",
        },
    )
    parts: list = [prompt, *[Image.open(p) for p in image_paths]]
    if audio_path:
        ext = os.path.splitext(audio_path)[1].lower()
        with open(audio_path, "rb") as f:
            parts.append({"mime_type": _AUDIO_MIME_BY_EXT.get(ext, "audio/aac"), "data": f.read()})
    resp = gen_model.generate_content(parts)
    return resp.text


class FootageVerdict(BaseModel):
    match_score: int = Field(ge=1, le=10, description="How well the footage fits the narration.")
    reason: str = ""


def judge_footage(frame_path: str, narration: str, *, api_key: str,
                  model: str = DEFAULT_MODEL) -> FootageVerdict:
    """Does this stock clip actually fit what's being said? Single attempt — callers fail open."""
    schema_hint = json.dumps(FootageVerdict.model_json_schema(), ensure_ascii=False)
    raw = _call_gemini_vision(
        api_key=api_key, model=model, image_paths=[frame_path],
        prompt=("This frame is from a stock clip chosen as background for a short-form video "
                f"scene whose narration is:\n\"{narration}\"\n"
                "Judge whether the visual genuinely fits the narration's subject and mood. "
                f"Return ONLY JSON matching this schema:\n{schema_hint}"),
    )
    return FootageVerdict.model_validate_json(_strip_code_fence(raw))


class FootageBatchVerdicts(BaseModel):
    verdicts: list[FootageVerdict]


def judge_footage_batch(items: list[tuple[str, str]], *, api_key: str,
                        model: str = DEFAULT_MODEL) -> list[FootageVerdict]:
    """Judge N (frame_path, narration) pairs in ONE vision call — quota efficiency: an episode's
    whole footage set costs 1-2 calls instead of one per scene. Raises on a count mismatch
    (callers fail open)."""
    schema_hint = json.dumps(FootageBatchVerdicts.model_json_schema(), ensure_ascii=False)
    lines = "\n".join(f'Image {i + 1} narration: "{n}"' for i, (_, n) in enumerate(items))
    raw = _call_gemini_vision(
        api_key=api_key, model=model, image_paths=[p for p, _ in items],
        prompt=(f"You are judging stock-footage choices for {len(items)} scenes of one short "
                f"video. Image i is the candidate background for narration i:\n{lines}\n"
                "For EACH image in order, judge whether the visual genuinely fits its narration's "
                "subject and mood. Return ONLY JSON matching this schema, with exactly "
                f"{len(items)} verdicts in the same order:\n{schema_hint}"),
        max_output_tokens=4096,
    )
    verdicts = FootageBatchVerdicts.model_validate_json(_strip_code_fence(raw)).verdicts
    if len(verdicts) != len(items):
        raise ValueError(f"expected {len(items)} verdicts, got {len(verdicts)}")
    return verdicts


class VideoQCVerdict(BaseModel):
    quality_score: int = Field(ge=1, le=10)
    issues: list[str] = Field(default_factory=list, max_length=6)


def judge_video_frames(frame_paths: list[str], *, api_key: str, context: str = "",
                       audio_path: str | None = None,
                       model: str = DEFAULT_MODEL) -> VideoQCVerdict:
    """Final-output spot check: are captions readable, visuals coherent, nothing broken?
    With `audio_path` set, the SAME call also judges the voice track (clear speech, right
    language, music not drowning the narration) — audio-aware QC at zero extra API cost."""
    schema_hint = json.dumps(VideoQCVerdict.model_json_schema(), ensure_ascii=False)
    audio_line = ""
    if audio_path:
        audio_line = (
            "The video's full audio track is attached — also check the VOICE: the narration is "
            "clearly audible and natural (no garbling, artifacts or cut-off words), it matches "
            "the stated language, background music (if any) never drowns the voice, and there "
            "are no long unintended silences. "
        )
    raw = _call_gemini_vision(
        api_key=api_key, model=model, image_paths=frame_paths, audio_path=audio_path,
        prompt=("These are frames sampled from an automatically produced vertical (9:16) short "
                f"video. {context}\n"
                "Check: captions present and readable (not clipped), visuals look coherent and "
                f"intentional (no broken/black/garbled frames), overall watchable quality. {audio_line}"
                f"Return ONLY JSON matching this schema:\n{schema_hint}"),
    )
    return VideoQCVerdict.model_validate_json(_strip_code_fence(raw))


# ── Critic pass (Loop 1: every video improves before it is even rendered) ────
class ScriptCritique(BaseModel):
    hook_score: int = Field(ge=1, le=10, description="Does the first sentence grab within 2s?")
    natural_score: int = Field(ge=1, le=10, description="Does it sound like a real person talking?")
    persona_score: int = Field(ge=1, le=10, description="Is it 100% in character?")
    fresh_score: int = Field(ge=1, le=10, description="Is the premise fresh vs previous episodes?")
    grammar_score: int = Field(ge=1, le=10, description="Spelling, grammar, diacritics and "
                               "punctuation are flawless for the target language.")
    verdict: Literal["pass", "rewrite"]
    issues: list[str] = Field(default_factory=list, max_length=6,
                              description="Concrete fixes if verdict is rewrite.")


_CRITIC_SYSTEM = (
    "You are a ruthless short-form video editor. Viewers swipe away in under 2 seconds; judge "
    "this script like their thumb is already moving. Score harshly. Verdict 'rewrite' whenever "
    "the hook is weak, any sentence reads like an essay or AI text, the persona slips, the "
    "premise repeats an earlier episode, or the text contains ANY spelling, grammar, diacritics "
    "or punctuation error in the target language — the narration is burned on screen as "
    "subtitles verbatim, so a single typo is visible in every frame. Issues must be concrete, "
    "actionable edits (for language errors, quote the exact broken word/phrase)."
)


def critique_script(
    script: VideoScript,
    *,
    api_key: str,
    persona: str | None = None,
    previous_synopses: list[str] | None = None,
    model: str = DEFAULT_MODEL,
) -> ScriptCritique:
    prev = "\n".join(f"- {s}" for s in (previous_synopses or [])[-15:]) or "(none)"
    banned = "; ".join(f"'{b}'" for b in AI_CLICHES.get(script.language, AI_CLICHES["en"]))
    prompt = (
        f"Review this short-form video script:\n{script.model_dump_json()}\n\n"
        f"Persona it must embody: {persona or '(none set)'}\n"
        f"Previous episodes (must not repeat): {prev}\n"
        f"Banned AI-tell phrases — verdict 'rewrite' if ANY appear (or a close variant): {banned}"
    )
    return generate_structured(prompt=prompt, schema=ScriptCritique, api_key=api_key,
                               system_prompt=_CRITIC_SYSTEM, model=model, temperature=0.2)


# ── Playbook distiller (Loop 2: learn from real performance data) ────────────
class PlaybookUpdate(BaseModel):
    playbook: list[str] = Field(max_length=15,
                                description="Short, actionable lessons for future episodes.")
    best_examples: list[str] = Field(default_factory=list, max_length=3,
                                     description="The strongest hooks/titles to emulate.")


_DISTILLER_SYSTEM = (
    "You are a channel growth analyst. You receive per-episode performance data (retention % is "
    "the most important metric, then views). Extract ONLY patterns supported by at least 3 "
    "episodes — never generalise from a single video. Keep lessons short, concrete and about "
    "craft (hooks, pacing, premise types, wording), not vague advice. Carry forward still-valid "
    "old lessons; drop disproven ones. Maximum 15 lessons."
)


def distill_playbook(
    *,
    api_key: str,
    performance_summary: str,
    current_playbook: list[str] | None = None,
    reject_reasons: list[str] | None = None,
    model: str = DEFAULT_MODEL,
) -> PlaybookUpdate:
    """Turn episode stats + operator feedback into an updated, bounded channel playbook."""
    prompt = (
        f"EPISODE PERFORMANCE DATA:\n{performance_summary}\n\n"
        f"CURRENT PLAYBOOK:\n" + ("\n".join(f"- {p}" for p in (current_playbook or [])) or "(empty)") +
        "\n\nOPERATOR REJECTION NOTES:\n" + ("\n".join(f"- {r}" for r in (reject_reasons or [])) or "(none)") +
        "\n\nProduce the updated playbook and pick the strongest hooks/titles as best_examples."
    )
    return generate_structured(prompt=prompt, schema=PlaybookUpdate, api_key=api_key,
                               system_prompt=_DISTILLER_SYSTEM, model=model, temperature=0.3)


def regenerate_metadata(*, topic: str, language: str, api_key: str, model: str = DEFAULT_MODEL) -> MetadataSet:
    """Cheap A/B metadata refresh without re-scripting."""
    prompt = (
        f"Generate exactly 3 distinct viral A/B metadata variations (variant A/B/C) for a "
        f"short-form video about '{topic}' in {language}. Each: title (<=100 chars), description, "
        "5-15 tags."
    )
    return generate_structured(
        prompt=prompt, schema=MetadataSet, api_key=api_key,
        system_prompt=_SYSTEM_BY_LANG.get(language, _SYSTEM_BY_LANG["en"]),
        model=model, temperature=0.9,
    )
