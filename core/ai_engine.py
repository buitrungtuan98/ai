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

logger = logging.getLogger(__name__)

Language = Literal["vi", "en", "es"]
T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "gemini-1.5-flash"
_BACKOFF_BASE_SECONDS = 1.0  # patched to 0 in tests


# ── Output schemas (the single source of truth for shape) ────────────────────
class Scene(BaseModel):
    index: int
    narration: str = Field(min_length=1, description="Exact text the TTS voice will speak.")
    caption_hook: str | None = Field(
        default=None, max_length=60, description="Optional short on-screen headline for the scene."
    )
    pexels_keywords: list[str] = Field(
        min_length=1, max_length=4, description="Stock-footage search terms, best first."
    )


class MetadataVariation(BaseModel):
    variant: Literal["A", "B", "C"]
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(min_length=3, max_length=15)


class VideoScript(BaseModel):
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
    "en": "You are a viral short-form video scriptwriter. Write punchy, factual narration in English.",
    "vi": "Bạn là người viết kịch bản video ngắn lan truyền. Viết lời dẫn ngắn gọn, chính xác bằng tiếng Việt.",
    "es": "Eres un guionista de videos cortos virales. Escribe una narración concisa y precisa en español.",
}


def build_script_prompt(topic: str, language: str, total_episodes: int, episode: int) -> str:
    return (
        f"Create a vertical short-form video script for episode {episode} of {total_episodes} "
        f"in a series about: '{topic}'. Language: {language}. "
        "Produce 3-6 scenes; each scene has narration the voice will speak, an optional short "
        "on-screen caption hook, and 1-4 stock-footage keywords. Also produce exactly 3 distinct "
        "A/B metadata variations (variant A/B/C) each with a title (<=100 chars), a description, "
        "and 5-15 tags. Keep it original and engaging."
    )


def generate_script(
    *,
    topic: str,
    language: str,
    total_episodes: int,
    episode: int,
    api_key: str,
    custom_system_prompt: str | None = None,
    model: str = DEFAULT_MODEL,
) -> VideoScript:
    system = _SYSTEM_BY_LANG.get(language, _SYSTEM_BY_LANG["en"])
    if custom_system_prompt:
        system = f"{system}\n{custom_system_prompt}"
    prompt = build_script_prompt(topic, language, total_episodes, episode)
    return generate_structured(
        prompt=prompt, schema=VideoScript, api_key=api_key, system_prompt=system,
        model=model, temperature=0.7,
    )


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
