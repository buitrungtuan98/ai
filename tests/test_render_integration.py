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


def _assert_filter_valid(graph: str) -> None:
    """One lavfi frame through the filter — an invalid option name fails instantly."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
         "-frames:v", "1", "-vf", graph, "-f", "null", "-"],
        check=True, capture_output=True,
    )


def test_color_grades_are_valid_ffmpeg_filters():
    """Every colour grade must parse on the REAL ffmpeg — unit tests only assert the string is in
    the graph, which let an invalid colorbalance option (ms/hs) ship and fail every render that
    picked that grade."""
    from core.video_factory import COLOR_GRADES

    for name, graph in COLOR_GRADES.items():
        try:
            _assert_filter_valid(graph)
        except subprocess.CalledProcessError as exc:  # pragma: no cover - diagnostic
            raise AssertionError(f"grade {name!r} rejected by ffmpeg: {exc.stderr.decode()[-300:]}")


def test_motion_filters_are_valid_ffmpeg_filters():
    from core.video_factory import MOTION_EFFECTS, motion_filter

    for effect in MOTION_EFFECTS:
        graph = motion_filter(effect, 2.0)
        try:
            _assert_filter_valid(graph)
        except subprocess.CalledProcessError as exc:  # pragma: no cover - diagnostic
            raise AssertionError(f"motion {effect!r} rejected by ffmpeg: {exc.stderr.decode()[-300:]}")


def _assert_audio_graph_valid(fc: str, n_inputs: int, out_label: str) -> None:
    """Run a filter_complex audio graph through real ffmpeg with lavfi sine inputs — a bad option
    name (e.g. a sidechaincompress typo) fails instantly instead of on every music render."""
    inp: list[str] = []
    for _ in range(n_inputs):
        inp += ["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=24000:duration=1"]
    subprocess.run(
        ["ffmpeg", "-v", "error", *inp, "-filter_complex", fc, "-map", out_label, "-f", "null", "-"],
        check=True, capture_output=True,
    )


def test_music_ducking_graph_is_valid_ffmpeg():
    """The sidechaincompress music-ducking graph must parse on real ffmpeg (unit tests only assert
    the string, which would let an invalid compressor option ship and break every music render)."""
    from core.video_factory import build_concat_args

    args = build_concat_args("l.txt", "m.mp4", music_path="bg.mp3", music_volume=0.15)
    fc = args[args.index("-filter_complex") + 1]
    try:
        _assert_audio_graph_valid(fc, n_inputs=2, out_label="[aout]")
    except subprocess.CalledProcessError as exc:  # pragma: no cover - diagnostic
        raise AssertionError(f"music-ducking graph rejected by ffmpeg: {exc.stderr.decode()[-300:]}")


def test_paced_concat_graph_is_valid_ffmpeg():
    """The per-sentence pacing concat (aevalsrc silence + concat) must parse on real ffmpeg."""
    from core.tts import build_paced_concat_args

    args = build_paced_concat_args(["a.mp3", "b.mp3"], [0.4], "out.mp3")
    fc = args[args.index("-filter_complex") + 1]
    try:
        _assert_audio_graph_valid(fc, n_inputs=2, out_label="[out]")
    except subprocess.CalledProcessError as exc:  # pragma: no cover - diagnostic
        raise AssertionError(f"paced-concat graph rejected by ffmpeg: {exc.stderr.decode()[-300:]}")


def test_probe_audio_stats_and_voice_check(tmp_path):
    """volumedetect parsing + the voice sanity thresholds against REAL audio: digital silence is
    flagged, an ordinary tone passes."""
    from core import media
    from core.video_factory import voice_check

    d = str(tmp_path)
    silent = os.path.join(d, "silent.mp3")
    tone = os.path.join(d, "tone.mp3")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", "2", silent], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "2", tone], check=True, capture_output=True)

    assert media.probe_audio_stats(silent)["mean_volume_db"] < -60.0
    assert media.probe_audio_stats(tone)["mean_volume_db"] > -45.0
    assert "silent" in voice_check(silent, "some words to speak here")
    assert voice_check(tone, "some words to speak here") is None


def test_extract_audio_stream_copy(tmp_path):
    """Audio-aware final QC pulls the master's AAC track out without re-encoding."""
    from core import media
    from core.ffmpeg_runner import extract_audio

    d = str(tmp_path)
    master = os.path.join(d, "master.mp4")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=size=64x64:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                    "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", master], check=True, capture_output=True)
    audio = os.path.join(d, "audio.aac")
    extract_audio(master, audio)
    assert os.path.getsize(audio) > 0
    # The extracted ADTS stream must decode — and still carry the tone (not silence).
    assert media.probe_audio_stats(audio)["mean_volume_db"] > -45.0


def test_workspace_cleanup_removes_dir(tmp_path):
    from core.cleanup import RenderWorkspace

    root = str(tmp_path)
    with RenderWorkspace("job1", root=root) as ws:
        p = ws.path("x.txt")
        open(p, "w").write("data")
        assert os.path.exists(p)
    assert not os.path.exists(os.path.join(root, "job1"))
