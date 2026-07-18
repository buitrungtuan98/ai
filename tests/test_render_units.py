"""Pure rendering-logic units: clip selection, ffmpeg arg builders, captions, A/B rotation."""
from __future__ import annotations


def test_select_clips_cycles_to_cover():
    from core.video_factory import select_clips

    assert select_clips([2.0, 3.0], 4.0) == [0, 1]
    assert select_clips([10.0], 4.0) == [0]
    assert select_clips([1.0, 1.0], 3.5) == [0, 1, 0, 1]


def test_build_scene_args_single_and_branding():
    from core.video_factory import Branding, build_scene_args

    a = build_scene_args(["c0.mp4"], "a.mp3", "s.ass", "out.mp4", 8.37, None)
    fc = a[a.index("-filter_complex") + 1]
    assert "concat=n=" not in fc and fc.endswith("ass=s.ass[vout]")
    assert "8.370" in a and a[-1] == "out.mp4"

    b = Branding(watermark_path="logo.png", tint_color="0x1E90FF", tint_opacity=0.15, mirror=True)
    a2 = build_scene_args(["c0.mp4", "c1.mp4"], "a.mp3", "s.ass", "out.mp4", 5.0, b)
    fc2 = a2[a2.index("-filter_complex") + 1]
    # order: mirror -> tint -> overlay -> captions
    assert fc2.index("hflip") < fc2.index("drawbox") < fc2.index("overlay") < fc2.index("ass=")
    assert a2.count("-i") == 4  # 2 clips + audio + watermark


def test_build_concat_args_copy():
    from core.video_factory import build_concat_args

    assert build_concat_args("l.txt", "m.mp4") == ["-f", "concat", "-safe", "0", "-i", "l.txt", "-c", "copy", "m.mp4"]


def test_build_concat_args_with_music_keeps_video_copy():
    from core.video_factory import build_concat_args

    args = build_concat_args("l.txt", "m.mp4", music_path="bg.mp3", music_volume=0.2)
    fc = args[args.index("-filter_complex") + 1]
    assert "volume=0.20" in fc and "amix=inputs=2:duration=first" in fc
    assert "-stream_loop" in args            # music loops to cover the video
    i = args.index("-c:v")
    assert args[i + 1] == "copy"             # video is never re-encoded for music
    assert "-shortest" in args and args[-1] == "m.mp4"


def test_ab_rotation_and_toggle():
    from core.ai_engine import VideoScript
    from core.video_factory import pick_metadata

    vs = VideoScript(
        language="en", topic="t",
        scenes=[{"index": i, "narration": "n", "pexels_keywords": ["k"]} for i in range(3)],
        metadata_variations=[{"variant": v, "title": f"T{v}", "description": "d", "tags": ["a", "b", "c"]} for v in "ABC"],
    )
    assert pick_metadata(vs, 1)["variant"] == "A"
    assert pick_metadata(vs, 2)["variant"] == "B"
    assert pick_metadata(vs, 4)["variant"] == "A"
    # A/B disabled → always variant A (the toggle is honored, not decorative).
    assert all(pick_metadata(vs, ep, ab_testing=False)["variant"] == "A" for ep in (1, 2, 3, 4))


def test_line_style_captions(tmp_path):
    from core.captions import build_ass, group_words_into_lines
    from core.tts import WordTiming

    words = [
        WordTiming("The", 0.0, 0.2), WordTiming("sun", 0.2, 0.5), WordTiming("is", 0.5, 0.7),
        WordTiming("hot", 0.7, 1.0),
        WordTiming("Really", 2.0, 2.4), WordTiming("hot", 2.4, 2.8),  # >0.6s pause → new line
    ]
    lines = group_words_into_lines(words)
    assert [line.text for line in lines] == ["The sun is hot", "Really hot"]
    assert lines[0].start == 0.0 and lines[0].end == 1.0

    out = str(tmp_path / "line.ass")
    build_ass(words, out, clip_duration=3.0, style="line")
    content = open(out).read()
    assert content.count("Dialogue:") == 2 and "The sun is hot" in content


def test_caption_wrap_and_ass(tmp_path):
    from core.captions import build_ass, wrap_text
    from core.tts import WordTiming

    lines = wrap_text("one two three four five six seven eight nine ten", 72, 400)
    assert len(lines) >= 2

    out = str(tmp_path / "c.ass")
    timings = [WordTiming("Hello", 0.0, 0.4), WordTiming("world", 0.4, 0.9)]
    build_ass(timings, out, clip_duration=0.9)
    content = open(out).read()
    assert "[Events]" in content and content.count("Dialogue:") == 2 and "PlayResX: 1080" in content


def test_ffmpeg_runner_uses_nice_threads(monkeypatch):
    """run_ffmpeg composes the command with nice + -threads without executing (Popen mocked)."""
    import core.ffmpeg_runner as fr

    captured = {}

    class FakeProc:
        stdout = iter([])
        stderr = None
        returncode = 0

        def wait(self):
            return 0

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(fr.subprocess, "Popen", fake_popen)
    fr.run_ffmpeg(["-i", "x.mp4", "out.mp4"])
    cmd = captured["cmd"]
    assert "ffmpeg" in cmd and "-threads" in cmd and "-progress" in cmd
    if fr.shutil.which("nice"):
        assert cmd[0] == "nice"
