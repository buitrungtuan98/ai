"""Auto-QC gate: the machine reviews footage and finished videos so a human doesn't have to.

Two checks, both powered by Gemini vision (`core/ai_engine`):
1. **Footage vetting** — before a stock clip is used, one frame is judged against the scene's
   narration; a poor match makes the renderer try the next candidate clip.
2. **Final QC** — frames sampled across the finished master are judged for readable captions and
   coherent visuals; the worker publishes on pass, re-renders once on fail, and falls back to
   human review if it still fails.

Every check **fails open**: a vision-API error never blocks or fails a render — the pipeline then
behaves exactly as it did before this gate existed. Human review stays available as the backstop.
"""
from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field

from core import media
from core.ai_engine import judge_footage, judge_footage_batch, judge_video_frames
from core.ffmpeg_runner import extract_audio, extract_frame

logger = logging.getLogger(__name__)

FOOTAGE_MATCH_THRESHOLD = 6   # 1-10; below this the clip is considered off-topic
FINAL_QC_THRESHOLD = 7        # 1-10; below this the master fails the gate
FINAL_QC_FRAMES = 4           # frames sampled evenly across the master


def make_footage_vetter(api_key: str, *, threshold: int = FOOTAGE_MATCH_THRESHOLD,
                        model: str | None = None) -> Callable[[str, str], bool]:
    """Return a `vet(clip_path, narration) -> bool` callable for the renderer.

    Extracts one frame from the clip and asks the vision judge whether it fits the narration.
    Fail-open: any error (extraction, API, parsing) accepts the clip.
    """
    def vet(clip_path: str, narration: str) -> bool:
        try:
            with tempfile.TemporaryDirectory(prefix="vet_") as tmp:
                frame = os.path.join(tmp, "frame.jpg")
                extract_frame(clip_path, frame, 1.0)
                kwargs = {"model": model} if model else {}
                verdict = judge_footage(frame, narration, api_key=api_key, **kwargs)
            if verdict.match_score < threshold:
                logger.info("Footage rejected (score %s/10): %s", verdict.match_score, verdict.reason)
                return False
            return True
        except Exception:  # noqa: BLE001 — QC must never fail a render
            logger.warning("Footage vetting errored — accepting clip (fail-open)", exc_info=True)
            return True

    return vet


def make_batch_vetter(api_key: str, *, threshold: int = FOOTAGE_MATCH_THRESHOLD,
                      model: str | None = None) -> Callable[[list[tuple[str, str]]], list[bool]]:
    """Return `vet(items) -> accepts` judging a whole episode's scene candidates in ONE vision
    call. `items` is a list of (clip_path, narration); one frame is extracted per clip.

    Fail-open: any error (extraction, API, count mismatch) accepts everything — Auto-QC must
    never block a render."""
    def vet(items: list[tuple[str, str]]) -> list[bool]:
        try:
            with tempfile.TemporaryDirectory(prefix="vet_") as tmp:
                frames: list[str] = []
                for i, (clip, _narration) in enumerate(items):
                    frame = os.path.join(tmp, f"frame_{i}.jpg")
                    extract_frame(clip, frame, 1.0)
                    frames.append(frame)
                kwargs = {"model": model} if model else {}
                verdicts = judge_footage_batch(
                    list(zip(frames, [n for _, n in items])), api_key=api_key, **kwargs)
            accepts = [v.match_score >= threshold for v in verdicts]
            for (_, narration), v, ok in zip(items, verdicts, accepts):
                if not ok:
                    logger.info("Footage rejected (score %s/10): %s", v.match_score, v.reason)
            return accepts
        except Exception:  # noqa: BLE001 — QC must never fail a render
            logger.warning("Batch footage vetting errored — accepting all (fail-open)", exc_info=True)
            return [True] * len(items)

    return vet


@dataclass
class QCResult:
    passed: bool
    score: int | None = None            # None = check could not run (fail-open pass)
    issues: list[str] = field(default_factory=list)
    # THREE states, not two (ADR-084): a judge that errored is not a judge that approved. When
    # `unavailable` is set the verdict above is a fail-open placeholder and `unavailable_reason`
    # says why in operator words — the UI shows ⚪ "could not run", never a green pass.
    unavailable: bool = False
    unavailable_reason: str | None = None

    def as_dict(self) -> dict:
        out = {"passed": self.passed, "score": self.score, "issues": self.issues}
        if self.unavailable:
            out["unavailable"] = True
            out["unavailable_reason"] = self.unavailable_reason
        return out


def _unavailable_reason(exc: Exception) -> str:
    """Why the judge could not run, in words the operator can act on — classified from the error,
    never quoted raw (provider errors can embed request URLs)."""
    msg = str(exc).lower()
    if "daily quota" in msg or "resets ~midnight" in msg:
        return "Gemini daily quota is spent — it resets around midnight US-Pacific"
    if "429" in msg or "rate" in msg and "limit" in msg:
        return "Gemini is rate-limiting right now — usually clears within minutes"
    if "model not found" in msg or "404" in msg:
        return "the configured Gemini model was not found — check the model chain on Credentials"
    return "the vision API could not be reached"


# Deterministic post-render checks — free, no API. They catch catastrophic breakage (a mostly-black
# or long-silent master) that vision QC can miss, and they still guard when the vision API fails open.
MAX_BLACK_SPAN_S = 2.5      # a continuous black stretch longer than this signals a broken render
MAX_SILENCE_SPAN_S = 3.5    # a continuous silent stretch longer than this signals broken audio


def run_deterministic_qc(master_path: str) -> QCResult:
    """Free ffmpeg sanity checks on the finished master: no long black stretch, no long silence.

    Fails CLOSED on clearly-broken output (like the render's own voice_check), but each detector
    fails OPEN individually — a probe glitch on one check must never block an otherwise-good render.
    Score is always None (this is a pass/fail gate, not a graded judgement)."""
    issues: list[str] = []
    try:
        black = media.max_black_span(master_path)
        if black > MAX_BLACK_SPAN_S:
            issues.append(f"black screen for {black:.1f}s")
    except Exception:  # noqa: BLE001 — a detector glitch must not block a good render
        logger.warning("black-frame detection errored — skipping (fail-open)", exc_info=True)
    try:
        silence = media.max_silence_span(master_path)
        if silence > MAX_SILENCE_SPAN_S:
            issues.append(f"silence for {silence:.1f}s")
    except Exception:  # noqa: BLE001
        logger.warning("silence detection errored — skipping (fail-open)", exc_info=True)
    if issues:
        logger.info("Deterministic QC flagged: %s", "; ".join(issues))
    return QCResult(passed=not issues, score=None, issues=issues)


def run_final_qc(master_path: str, *, api_key: str, context: str = "",
                 threshold: int = FINAL_QC_THRESHOLD, model: str | None = None) -> QCResult:
    """Sample frames across the finished master — plus its audio track — and ask the judge for
    ONE verdict covering visuals AND voice (clear speech, right language, music balance). The
    audio rides along in the same vision call, so voice QC costs zero extra API requests.

    Fail-open: if frames can't be extracted or the API errors, returns a pass with score None so
    the pipeline continues exactly as if the gate were off; a failed audio extraction just falls
    back to frames-only judging.
    """
    try:
        duration = media.probe_duration(master_path)
        with tempfile.TemporaryDirectory(prefix="qc_") as tmp:
            frames: list[str] = []
            for i in range(FINAL_QC_FRAMES):
                # Evenly spaced, avoiding the very first/last instants (fades, black lead-in).
                at = duration * (i + 1) / (FINAL_QC_FRAMES + 1)
                path = os.path.join(tmp, f"frame_{i}.jpg")
                extract_frame(master_path, path, at)
                frames.append(path)
            audio_path: str | None = os.path.join(tmp, "audio.aac")
            try:
                extract_audio(master_path, audio_path)
            except Exception:  # noqa: BLE001 — audio is a bonus; frames-only QC still runs
                logger.warning("Final-QC audio extraction failed — judging frames only",
                               exc_info=True)
                audio_path = None
            kwargs = {"model": model} if model else {}
            verdict = judge_video_frames(frames, api_key=api_key, context=context,
                                         audio_path=audio_path, **kwargs)
        passed = verdict.quality_score >= threshold
        if not passed:
            logger.info("Final QC failed (score %s/10): %s", verdict.quality_score, verdict.issues)
        return QCResult(passed=passed, score=verdict.quality_score, issues=verdict.issues)
    except Exception as exc:  # noqa: BLE001 — QC must never fail a render
        logger.warning("Final QC errored — no verdict (unavailable)", exc_info=True)
        # `passed=True` remains the fail-open placeholder for callers that ignore `unavailable`,
        # but every caller that ROUTES on QC must treat this as "no verdict", not "approved"
        # (ADR-084): a video the judge already failed once must not publish because the judge
        # was absent for the re-check.
        return QCResult(passed=True, score=None, issues=["qc-unavailable"],
                        unavailable=True, unavailable_reason=_unavailable_reason(exc))
