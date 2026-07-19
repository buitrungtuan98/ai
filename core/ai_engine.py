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

logger = logging.getLogger(__name__)

Language = Literal["vi", "en", "es"]
T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = settings.GEMINI_MODEL  # swap models via .env, no code change
_BACKOFF_BASE_SECONDS = 1.0  # patched to 0 in tests


# ── Output schemas (the single source of truth for shape) ────────────────────
class Scene(BaseModel):
    index: int
    narration: str = Field(min_length=1, description="Exact text the TTS voice will speak.")
    caption_hook: str | None = Field(
        default=None, max_length=60, description="Optional short on-screen headline for the scene."
    )
    pexels_keywords: list[str] = Field(
        min_length=1, max_length=4,
        description="Stock-footage search terms, best first — ALWAYS in English regardless of "
                    "the narration language (stock libraries are indexed in English).",
    )


class _SynopsisMixin(BaseModel):
    # Episode memory: a one-line summary stored per episode and fed back into later prompts so the
    # series never repeats itself (no_repeat) or can genuinely continue (serial).
    synopsis: str = Field(
        default="", max_length=300,
        description="One-sentence summary of THIS episode's specific premise/content.",
    )


class MetadataVariation(BaseModel):
    variant: Literal["A", "B", "C"]
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(min_length=3, max_length=15)


class VideoScript(_SynopsisMixin):
    language: Language
    topic: str
    scenes: list[Scene] = Field(min_length=3, max_length=8)
    metadata_variations: list[MetadataVariation] = Field(min_length=3, max_length=3)


class MetadataSet(BaseModel):
    metadata_variations: list[MetadataVariation] = Field(min_length=3, max_length=3)


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
    """Single point that imports and calls the Gemini SDK. Returns raw response text."""
    import google.generativeai as genai

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
    for cand in getattr(resp, "candidates", []) or []:
        reason = getattr(cand, "finish_reason", None)
        if reason and str(reason).upper().endswith(("SAFETY", "RECITATION")):
            raise GeminiBlockedError(f"Gemini blocked the response (finish_reason={reason}).")
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
    max_output_tokens: int = 4096,
    max_retries: int = 3,
) -> T:
    """Call Gemini and return a validated instance of `schema`.

    Retries with exponential backoff and a repair turn on JSON/validation errors.
    """
    schema_hint = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    base_prompt = (
        f"{prompt}\n\n"
        "Return ONLY a single JSON object (no markdown, no prose) that validates against this "
        f"JSON Schema:\n{schema_hint}"
    )
    last_error: Exception | None = None
    convo = base_prompt

    for attempt in range(max_retries):
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
            logger.warning("Gemini call failed (attempt %d/%d): %s", attempt + 1, max_retries, exc)

        if attempt < max_retries - 1 and _BACKOFF_BASE_SECONDS:
            time.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))

    raise GeminiError(f"generate_structured failed after {max_retries} attempts: {last_error}")


def _strip_code_fence(text: str) -> str:
    """Tolerate a ```json ... ``` fence if the model adds one despite JSON mode."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    return s.strip()


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
    "is the only exception, and it must lead straight into the hook)."
)


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


def build_script_prompt(
    topic: str,
    language: str,
    total_episodes: int,
    episode: int,
    *,
    continuity: str = "none",
    previous_synopses: list[str] | None = None,
) -> str:
    base = (
        f"Create a vertical short-form video script for episode {episode} of {total_episodes} "
        f"in a series about: '{topic}'. Language: {language}. "
        "Produce 3-6 scenes; each scene has narration the voice will speak, an optional short "
        "on-screen caption hook, and 1-4 stock-footage keywords. Also produce exactly 3 distinct "
        "A/B metadata variations (variant A/B/C) each with a title (<=100 chars), a description, "
        "and 5-15 tags, all in the same persona/voice. Include a one-sentence 'synopsis' of this "
        "episode's specific premise. Keep it original and engaging. "
        "IMPORTANT: pexels_keywords must be ENGLISH visual search terms (e.g. 'river night fog'), "
        "even when the narration language is not English."
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
    prompt = build_script_prompt(
        topic, language, total_episodes, episode,
        continuity=continuity, previous_synopses=previous_synopses,
    )
    temperature = 0.85 if continuity != "none" else 0.7
    script = generate_structured(
        prompt=prompt, schema=VideoScript, api_key=api_key, system_prompt=system,
        model=model, temperature=temperature,
    )
    if not self_critique:
        return script

    # Generator→critic loop: one harsh editorial review; on 'rewrite', one revision with the
    # concrete issues injected. A critic failure never blocks the video (best-effort quality gate).
    try:
        review = critique_script(script, api_key=api_key, persona=persona,
                                 previous_synopses=previous_synopses, model=model)
    except Exception:  # noqa: BLE001
        logger.warning("Critic pass failed — keeping the first draft.")
        return script
    if review.verdict == "pass":
        return script
    logger.info("Critic requested a rewrite (hook=%d natural=%d persona=%d fresh=%d)",
                review.hook_score, review.natural_score, review.persona_score, review.fresh_score)
    fixes = "\n".join(f"- {i}" for i in review.issues) or "- strengthen the hook and spoken rhythm"
    try:
        return generate_structured(
            prompt=prompt + "\n\nAn editor reviewed your previous draft and demands these fixes "
                            "(rewrite fully, do not patch):\n" + fixes,
            schema=VideoScript, api_key=api_key, system_prompt=system,
            model=model, temperature=temperature,
        )
    except Exception:  # noqa: BLE001
        logger.warning("Rewrite failed — keeping the first draft.")
        return script


# ── Critic pass (Loop 1: every video improves before it is even rendered) ────
class ScriptCritique(BaseModel):
    hook_score: int = Field(ge=1, le=10, description="Does the first sentence grab within 2s?")
    natural_score: int = Field(ge=1, le=10, description="Does it sound like a real person talking?")
    persona_score: int = Field(ge=1, le=10, description="Is it 100% in character?")
    fresh_score: int = Field(ge=1, le=10, description="Is the premise fresh vs previous episodes?")
    verdict: Literal["pass", "rewrite"]
    issues: list[str] = Field(default_factory=list, max_length=6,
                              description="Concrete fixes if verdict is rewrite.")


_CRITIC_SYSTEM = (
    "You are a ruthless short-form video editor. Viewers swipe away in under 2 seconds; judge "
    "this script like their thumb is already moving. Score harshly. Verdict 'rewrite' whenever "
    "the hook is weak, any sentence reads like an essay or AI text, the persona slips, or the "
    "premise repeats an earlier episode. Issues must be concrete, actionable edits."
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
    prompt = (
        f"Review this short-form video script:\n{script.model_dump_json()}\n\n"
        f"Persona it must embody: {persona or '(none set)'}\n"
        f"Previous episodes (must not repeat): {prev}"
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
