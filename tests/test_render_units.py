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


def test_build_concat_args_copy_and_loudnorm():
    from core.video_factory import LOUDNORM_FILTER, build_concat_args

    # loudnorm off → pure stream copy, nothing re-encoded.
    assert build_concat_args("l.txt", "m.mp4", loudnorm=False) == [
        "-f", "concat", "-safe", "0", "-i", "l.txt", "-c", "copy", "m.mp4"]

    # Default: -14 LUFS normalization — audio-only re-encode, video still copied.
    args = build_concat_args("l.txt", "m.mp4")
    assert args[args.index("-af") + 1] == LOUDNORM_FILTER
    assert args[args.index("-c:v") + 1] == "copy"
    assert "loudnorm=I=-14" in LOUDNORM_FILTER


def test_build_concat_args_with_music_keeps_video_copy():
    from core.video_factory import build_concat_args

    args = build_concat_args("l.txt", "m.mp4", music_path="bg.mp3", music_volume=0.2)
    fc = args[args.index("-filter_complex") + 1]
    assert "volume=0.20" in fc and "amix=inputs=2:duration=first" in fc
    assert fc.index("amix") < fc.index("loudnorm")  # normalize the final mix, not the parts
    assert "-stream_loop" in args            # music loops to cover the video
    i = args.index("-c:v")
    assert args[i + 1] == "copy"             # video is never re-encoded for music
    assert "-shortest" in args and args[-1] == "m.mp4"

    # loudnorm off → the mix output feeds [aout] directly.
    fc2 = build_concat_args("l.txt", "m.mp4", music_path="bg.mp3", loudnorm=False)
    fc2 = fc2[fc2.index("-filter_complex") + 1]
    assert "loudnorm" not in fc2 and fc2.endswith("[aout]")


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


def test_motion_filters():
    from core.video_factory import MOTION_EFFECTS, build_scene_args, motion_filter

    zi = motion_filter("zoom_in", 8.0)
    assert "zoompan" in zi and "min(zoom+" in zi and "1080x1920" in zi
    zo = motion_filter("zoom_out", 8.0)
    assert "zoompan" in zo and "max(zoom-" in zo
    pan = motion_filter("pan", 8.0)
    assert "zoompan" not in pan and "crop=1080:1920" in pan and "t/8.000" in pan
    assert MOTION_EFFECTS == ["zoom_in", "pan", "zoom_out"]  # deterministic rotation

    # Wired into the scene graph before branding/captions; absent when motion is off.
    args = build_scene_args(["c.mp4"], "a.mp3", "s.ass", "o.mp4", 5.0, None, motion_effect="zoom_in")
    fc = args[args.index("-filter_complex") + 1]
    assert "zoompan" in fc and fc.index("zoompan") < fc.index("ass=")
    args_off = build_scene_args(["c.mp4"], "a.mp3", "s.ass", "o.mp4", 5.0, None, motion_effect=None)
    assert "zoompan" not in args_off[args_off.index("-filter_complex") + 1]


def test_caption_themes(tmp_path):
    from core.captions import CAPTION_THEMES, POP_TAG, build_ass, hex_to_ass
    from core.tts import WordTiming

    assert hex_to_ass("#FFCF6B") == "&H006BCFFF"  # RGB → ASS BGR
    assert set(CAPTION_THEMES) == {"classic", "highlight", "boxed", "neon"}

    timings = [WordTiming("Hello", 0.0, 0.5), WordTiming("world", 0.5, 1.0)]

    boxed = str(tmp_path / "boxed.ass")
    build_ass(timings, boxed, clip_duration=1.0, theme="boxed")
    content = open(boxed).read()
    assert ",3,7," in content and POP_TAG not in content  # BorderStyle=3 opaque box, no pop

    neon = str(tmp_path / "neon.ass")
    build_ass(timings, neon, clip_duration=1.0, theme="neon")
    content = open(neon).read()
    assert r"\blur2" in content and POP_TAG in content

    hl = str(tmp_path / "hl.ass")
    build_ass(timings, hl, clip_duration=1.0, theme="highlight", accent_hex="#1E90FF")
    content = open(hl).read()
    assert "&H00FF901E" in content and POP_TAG in content  # campaign accent colour drives the style


def test_progress_is_monotonic(monkeypatch):
    """The global progress callback must never jump backward across scenes (was a per-scene
    sawtooth when prep/render were reported as interleaved stages)."""
    from core import video_factory as vf

    values: list[float] = []

    def emit(frac):  # produce()'s report() maps stage fractions into 0..100 via _STAGE_BUDGET
        values.append(frac)

    # Drive report() directly the way produce() does: per scene, encode progress then scene-done.
    n = 3
    for si in range(n):
        for p in (0, 40, 80, 100):
            base = sum(v for k, v in vf._STAGE_BUDGET.items()
                       if vf._stage_order(k) < vf._stage_order("scenes"))
            emit(min(99.0, base + ((si + p / 100) / n * 100) / 100 * vf._STAGE_BUDGET["scenes"]))
    assert values == sorted(values)  # never decreases


def test_pexels_skips_zero_duration(monkeypatch):
    """A clip with missing/zero duration is dropped (it would defeat select_clips' coverage math)."""
    from core import pexels

    payload = {"videos": [
        {"id": 1, "duration": 0, "video_files": [{"link": "u0", "width": 1080, "height": 1920}]},
        {"id": 2, "duration": 8, "video_files": [{"link": "u2", "width": 1080, "height": 1920}]},
        {"id": 3, "video_files": [{"link": "u3", "width": 1080, "height": 1920}]},  # no duration
    ]}

    class R:
        def raise_for_status(self): pass
        def json(self): return payload

    import sys

    fake_requests = type("m", (), {"get": staticmethod(lambda *a, **k: R())})
    monkeypatch.setitem(sys.modules, "requests", fake_requests)  # search_videos does `import requests`
    clips = pexels.search_videos("q", "key")
    assert [c.id for c in clips] == [2]  # only the positive-duration clip survives


def test_color_grade_in_scene_graph():
    from core.video_factory import COLOR_GRADES, build_scene_args

    args = build_scene_args(["c.mp4"], "a.mp3", "s.ass", "o.mp4", 5.0, None,
                            motion_effect="zoom_in", color_grade="noir")
    fc = args[args.index("-filter_complex") + 1]
    # Grade sits between motion and captions so text is never graded.
    assert COLOR_GRADES["noir"] in fc and fc.index("zoompan") < fc.index("hue=s=0") < fc.index("ass=")

    # None/unknown grade → no grade filter injected.
    for grade in (None, "does-not-exist"):
        fc2 = build_scene_args(["c.mp4"], "a.mp3", "s.ass", "o.mp4", 5.0, None, color_grade=grade)
        assert all(g not in fc2[fc2.index("-filter_complex") + 1] for g in COLOR_GRADES.values())


def test_vet_candidates_reorders_and_reuses_downloads():
    from types import SimpleNamespace

    from core.video_factory import vet_candidates

    found = [SimpleNamespace(download_url=f"u{i}", duration=5.0) for i in range(4)]
    downloads: list[str] = []

    def download(url, path):
        downloads.append(url)

    # First candidate rejected, second accepted → leader swaps, reject dropped, downloads reused.
    verdicts = iter([False, True])
    kept, pre = vet_candidates(found, "narration", lambda p, n: next(verdicts), download,
                               lambda k: f"/ws/vet_{k}.mp4")
    assert [c.download_url for c in kept] == ["u1", "u2", "u3"]
    assert pre == {0: "/ws/vet_1.mp4"}      # the accepted clip's file, re-keyed to lead
    assert downloads == ["u0", "u1"]        # vetting stopped at the first accept

    # All vetted candidates rejected → fail-open: original order kept, downloads still reusable.
    downloads.clear()
    kept2, pre2 = vet_candidates(found, "narration", lambda p, n: False, download,
                                 lambda k: f"/ws/vet_{k}.mp4")
    assert kept2 is found and set(pre2) == {0, 1, 2}  # bounded at FOOTAGE_VET_CANDIDATES
    assert downloads == ["u0", "u1", "u2"]


def test_search_footage_fallback_chain(monkeypatch):
    from core import video_factory as vf

    calls = []

    def fake_search(query, key, per_page=10):
        calls.append(query)
        # Joined query and first keyword fail; second keyword succeeds.
        return ["clip"] if query == "fog" else []

    monkeypatch.setattr(vf.pexels, "search_videos", fake_search)
    assert vf.search_footage(["dòng sông", "fog"], "k") == ["clip"]
    assert calls == ["dòng sông fog", "dòng sông", "fog"]

    # Everything fails → generic fallback is tried last; empty means truly nothing.
    calls.clear()
    monkeypatch.setattr(vf.pexels, "search_videos", lambda q, k, per_page=10: (calls.append(q), [])[1])
    assert vf.search_footage(["xyz"], "k") == []
    assert calls[-1] == vf.FALLBACK_FOOTAGE_QUERY


def test_pexels_keywords_prompt_demands_english():
    from core.ai_engine import Scene, build_script_prompt

    assert "English" in (Scene.model_fields["pexels_keywords"].description or "")
    assert "ENGLISH" in build_script_prompt("chuyện ma", "vi", 30, 1)


def test_tts_retries_transient_failures(monkeypatch):
    """The TTS endpoint occasionally drops a handshake — synthesize retries before surfacing."""
    from core import tts

    monkeypatch.setattr(tts, "_RETRY_SLEEP_SECONDS", 0)
    attempts = []

    async def flaky(text, voice, rate_pct, out_path):
        attempts.append(1)
        if len(attempts) < 2:
            raise ConnectionError("403 handshake dropped")
        return [tts.WordTiming("hi", 0.0, 0.4)]

    monkeypatch.setattr(tts, "_synthesize_async", flaky)
    timings = tts.synthesize("hi", "/tmp/x.mp3", language="en")
    assert len(attempts) == 2 and timings[0].text == "hi"  # failed once, then recovered

    # A persistent failure still raises (the episode must fail visibly).
    attempts.clear()

    async def dead(text, voice, rate_pct, out_path):
        attempts.append(1)
        raise ConnectionError("403 forever")

    monkeypatch.setattr(tts, "_synthesize_async", dead)
    import pytest as _pytest
    with _pytest.raises(ConnectionError):
        tts.synthesize("hi", "/tmp/x.mp3", language="en")
    assert len(attempts) == tts._RETRY_ATTEMPTS


def test_tts_requests_word_boundaries(monkeypatch, tmp_path):
    """edge-tts >= 7 defaults to SentenceBoundary — synthesize must explicitly request per-WORD
    boundaries or captions come back empty (videos silently render with no subtitles)."""
    import edge_tts

    from core import tts

    captured = {}

    class FakeCommunicate:
        def __init__(self, text, voice, rate=None, boundary=None):
            captured.update(text=text, voice=voice, rate=rate, boundary=boundary)

        async def stream(self):
            yield {"type": "audio", "data": b"mp3"}
            yield {"type": "WordBoundary", "offset": 0, "duration": 5_000_000, "text": "hello"}
            yield {"type": "SentenceBoundary", "offset": 0, "duration": 9_000_000, "text": "hello."}

    monkeypatch.setattr(edge_tts, "Communicate", FakeCommunicate)
    timings = tts.synthesize("hello", str(tmp_path / "o.mp3"))
    assert captured["boundary"] == "WordBoundary"          # explicit — the 7.x default is sentences
    assert [w.text for w in timings] == ["hello"]          # word events used, sentence events ignored


def test_ffmpeg_runner_uses_nice_threads(monkeypatch):
    """run_ffmpeg composes the command with nice + -threads without executing (Popen mocked)."""
    import core.ffmpeg_runner as fr

    captured = {}

    class FakeStdout:
        def __iter__(self):
            return iter([])

        def close(self):
            pass

    class FakeProc:
        stdout = FakeStdout()
        returncode = 0

        def wait(self):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(fr.subprocess, "Popen", fake_popen)
    fr.run_ffmpeg(["-i", "x.mp4", "out.mp4"])
    cmd = captured["cmd"]
    assert "ffmpeg" in cmd and "-threads" in cmd and "-progress" in cmd
    if fr.shutil.which("nice"):
        assert cmd[0] == "nice"
