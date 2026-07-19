"""End-to-end ffmpeg render integration test.

Skips automatically when ffmpeg/ffprobe are absent (e.g. this sandbox, where the egress policy
blocks fetching a static binary). It DOES run in the Docker image (ffmpeg installed via apt) and in
CI, exercising the real render path — synthetic footage + audio (no Pexels/TTS needed), per-scene
re-encode with burned captions, then concat-demuxer stream-copy stitch.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed in this environment",
)


def _make_inputs(d):
    # 2s silent audio + 2s test-pattern video, both real files.
    audio = os.path.join(d, "a.mp3")
    clip = os.path.join(d, "clip.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", "2", audio], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                    "-t", "2", "-pix_fmt", "yuv420p", clip], check=True, capture_output=True)
    return audio, clip


def test_scene_render_and_concat_copy(tmp_path):
    from core import media
    from core.captions import build_ass
    from core.ffmpeg_runner import run_ffmpeg
    from core.tts import WordTiming
    from core.video_factory import build_concat_args, build_scene_args

    d = str(tmp_path)
    audio, clip = _make_inputs(d)
    dur = media.probe_duration(audio)
    assert 1.5 < dur < 2.5

    ass = os.path.join(d, "s.ass")
    build_ass([WordTiming("Hello", 0.0, 1.0), WordTiming("World", 1.0, dur)], ass, clip_duration=dur)

    progress = []
    scene0 = os.path.join(d, "scene0.mp4")
    run_ffmpeg(build_scene_args([clip], audio, ass, scene0, dur, None),
               total_duration=dur, on_progress=progress.append)
    assert os.path.exists(scene0)
    meta = media.probe_video_meta(scene0)
    assert meta["width"] == 1080 and meta["height"] == 1920  # scaled to vertical
    assert progress and progress[-1] <= 99.0

    # Second identical scene, then concat with -c copy (no re-encode).
    scene1 = os.path.join(d, "scene1.mp4")
    run_ffmpeg(build_scene_args([clip], audio, ass, scene1, dur, None))
    list_file = os.path.join(d, "list.txt")
    with open(list_file, "w") as f:
        f.write(f"file '{scene0}'\nfile '{scene1}'\n")
    master = os.path.join(d, "master.mp4")
    run_ffmpeg(build_concat_args(list_file, master))
    assert os.path.exists(master)
    assert media.probe_duration(master) > dur * 1.5  # two scenes stitched


def test_workspace_cleanup_removes_dir(tmp_path):
    from core.cleanup import RenderWorkspace

    root = str(tmp_path)
    with RenderWorkspace("job1", root=root) as ws:
        p = ws.path("x.txt")
        open(p, "w").write("data")
        assert os.path.exists(p)
    assert not os.path.exists(os.path.join(root, "job1"))
