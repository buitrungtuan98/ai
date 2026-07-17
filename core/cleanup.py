"""Temp-media lifecycle. Nothing lingers on the 200 GB disk longer than the orphan max age.

DRY/KISS: every render's artifacts live under one job directory; cleanup is a single `rmtree`.
`RenderWorkspace` gives try/finally semantics for free — the directory is removed on success AND on
any exception. `sweep_orphans` catches the SIGKILL/OOM case where `__exit__` never ran.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from types import TracebackType

from core.config import settings

logger = logging.getLogger(__name__)


class RenderWorkspace:
    """Context manager for a per-job scratch directory under WORK_ROOT/<job_id>/."""

    def __init__(self, job_id: str, root: str | None = None) -> None:
        self.dir = Path(root or settings.WORK_ROOT) / str(job_id)

    def __enter__(self) -> "RenderWorkspace":
        self.dir.mkdir(parents=True, exist_ok=True)
        return self

    def path(self, name: str) -> str:
        return str(self.dir / name)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        logger.debug("Cleaned workspace %s", self.dir)


def sweep_orphans(root: str | None = None, max_age_minutes: int | None = None) -> int:
    """Remove workspace dirs older than the max age (crash/OOM survivors). Returns count removed."""
    base = Path(root or settings.WORK_ROOT)
    max_age = (max_age_minutes or settings.ORPHAN_MAX_AGE_MINUTES) * 60
    if not base.exists():
        return 0
    cutoff = time.time() - max_age
    removed = 0
    for child in base.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("sweep_orphans removed %d stale workspace(s)", removed)
    return removed
