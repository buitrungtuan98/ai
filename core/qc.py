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

    def as_dict(self) -> dict:
        return {"passed": self.passed, "score": self.score, "issues": self.issues}


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
    except Exception:  # noqa: BLE001 — QC must never fail a render
        logger.warning("Final QC errored — passing (fail-open)", exc_info=True)
        return QCResult(passed=True, score=None, issues=["qc-unavailable"])
