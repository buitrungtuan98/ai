"""R35 — scripts/deploy.sh recovers on its own: if the new build's web OR worker never turns healthy,
the stack goes back to the last tag that passed the gate and the run still fails (ADR-100).

Driven against a fake `docker` on PATH that remembers which tag is "up" and reports a service as
unhealthy when that tag is listed in FAKE_BAD (`<tag>:<service>` pairs); FAKE_ROLLBACK_BAD makes
everything unhealthy after the second `compose up` of a run (the rollback)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FAKE_DOCKER = r"""#!/usr/bin/env bash
state="$FAKE_STATE"
echo "$* tag=${AVF_IMAGE_TAG:-}" >> "$state/calls"
up="$(cat "$state/up" 2>/dev/null || true)"
case "$1 $2" in
  "compose up") echo "$AVF_IMAGE_TAG" > "$state/up"; echo x >> "$state/ups" ;;
  "compose ps")
    if [[ "${3:-}" == "-q" ]]; then [[ -n "$up" ]] && echo "cid-$4"; fi ;;
  "inspect --format")
    if [[ "$3" == *Config.Image* ]]; then echo "ghcr.io/o/ai:$up"
    else
      svc="${4#cid-}"
      ups="$(wc -l < "$state/ups" 2>/dev/null || echo 0)"
      if [[ " ${FAKE_BAD:-} " == *" $up:$svc "* ]]; then echo unhealthy
      elif [[ -n "${FAKE_ROLLBACK_BAD:-}" && "$ups" -ge 2 ]]; then echo unhealthy
      else echo healthy; fi
    fi ;;
esac
exit 0
"""


@pytest.fixture
def box(tmp_path: Path) -> Path:
    """A deploy dir laid out like the VPS: scripts/deploy.sh + .env, and a fake docker on PATH."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "deploy.sh", tmp_path / "scripts" / "deploy.sh")
    (tmp_path / ".env").write_text("X=1\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    (tmp_path / "state").mkdir()
    return tmp_path


def deploy(
    box: Path, tag: str, bad: str = "", rollback_bad: bool = False
) -> subprocess.CompletedProcess[str]:
    (box / "state" / "ups").unlink(missing_ok=True)  # count `compose up` per run
    env = {
        **os.environ,
        "PATH": f"{box / 'bin'}:{os.environ['PATH']}",
        "FAKE_STATE": str(box / "state"),
        "FAKE_BAD": bad,
        "AVF_IMAGE_TAG": tag,
        "DEPLOY_POLL_SECONDS": "0",
        "DEPLOY_WEB_TRIES": "2",
        "DEPLOY_WORKER_TRIES": "2",
        "FAKE_ROLLBACK_BAD": "1" if rollback_bad else "",
    }
    return subprocess.run(
        ["bash", str(box / "scripts" / "deploy.sh")],
        env=env,
        capture_output=True,
        text=True,
    )


def up_tag(box: Path) -> str:
    return (box / "state" / "up").read_text().strip()


def last_good(box: Path) -> str:
    return (box / ".deploy" / "last-good-tag").read_text().strip()


def test_healthy_deploy_records_the_tag(box: Path) -> None:
    r = deploy(box, "aaa")
    assert r.returncode == 0, r.stdout + r.stderr
    assert up_tag(box) == "aaa" and last_good(box) == "aaa"


def test_unhealthy_worker_rolls_back_to_last_good(box: Path) -> None:
    assert deploy(box, "aaa").returncode == 0
    r = deploy(box, "bbb", bad="bbb:worker")
    assert r.returncode == 1  # the failed deploy still shows red in CI
    assert "rolling back to aaa" in r.stdout and "Rolled back" in r.stdout
    assert up_tag(box) == "aaa"  # …but the box runs the good build again
    assert last_good(box) == "aaa"  # and the bad tag is never remembered as good


def test_unhealthy_web_rolls_back_too(box: Path) -> None:
    assert deploy(box, "aaa").returncode == 0
    r = deploy(box, "bbb", bad="bbb:web")
    assert r.returncode == 1 and up_tag(box) == "aaa"


def test_previous_tag_falls_back_to_the_running_image(box: Path) -> None:
    """The first deploy after this change has no state file yet: the running web's tag is used."""
    (box / "state" / "up").write_text("old\n")
    r = deploy(box, "bbb", bad="bbb:worker")
    assert r.returncode == 1 and up_tag(box) == "old"


def test_already_unhealthy_stack_rolls_forward(box: Path) -> None:
    """A stack that was down BEFORE the deploy is not the new build's fault: keep the new build,
    or a broken box would refuse every fix."""
    assert deploy(box, "aaa").returncode == 0
    r = deploy(box, "bbb", bad="aaa:worker bbb:worker")
    assert r.returncode == 1 and "already unhealthy" in r.stdout
    assert up_tag(box) == "bbb"


def test_no_rollback_target_fails_without_flapping(box: Path) -> None:
    r = deploy(box, "bbb", bad="bbb:worker")
    assert r.returncode == 1 and "no earlier good tag" in r.stdout
    assert up_tag(box) == "bbb"


def test_failed_rollback_is_reported(box: Path) -> None:
    """The old build was healthy before, but not after going back to it (e.g. Redis died mid-deploy)."""
    assert deploy(box, "aaa").returncode == 0
    r = deploy(box, "bbb", bad="bbb:worker", rollback_bad=True)
    assert r.returncode == 1 and "CRITICAL" in r.stdout
