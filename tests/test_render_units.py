"""Pure rendering-logic units: clip selection, ffmpeg arg builders, captions, A/B rotation."""
from __future__ import annotations


def test_plan_shots_covers_and_caps_and_cycles():
    from core.video_factory import SHOT_MAX_S, plan_shots

    # One long clip, no word gaps: a 10s scene is sliced into several shots (never one 10s shot),
    # each capped near the target, cycling the single clip; durations sum to the scene length.
    shots = plan_shots([30.0], [], 10.0)
    idxs = [i for i, _ in shots]
    durs = [d for _, d in shots]
    assert all(i == 0 for i in idxs) and len(shots) >= 3
    assert all(d <= SHOT_MAX_S + 0.01 for d in durs)
    assert abs(sum(durs) - 10.0) < 0.05                 # full coverage, no gap

    # Multiple clips → consecutive shots use different clips (variety).
    shots2 = plan_shots([5.0, 5.0, 5.0], [], 9.0)
    assert [i for i, _ in shots2][:3] == [0, 1, 2]

    # A short clip caps its own shot (never outruns its footage → no black gap).
    shots3 = plan_shots([1.0], [], 3.0)
    assert all(d <= 1.0 + 0.01 for _, d in shots3) and abs(sum(d for _, d in shots3) - 3.0) < 0.05

    # A cut snaps to a nearby word boundary when one falls in the shot window.
    snapped = plan_shots([30.0], [2.8, 5.9], 6.0)
    assert abs(snapped[0][1] - 2.8) < 0.01               # first cut lands on the 2.8s word gap


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


def test_build_scene_args_trims_shots():
    from core.video_factory import build_scene_args

    # With shot durations, each clip is trimmed to its shot length (the edited cut rhythm).
    a = build_scene_args(["c0.mp4", "c1.mp4"], "a.mp3", "s.ass", "o.mp4", 6.0, None,
                         shot_durations=[3.0, 3.0])
    fc = a[a.index("-filter_complex") + 1]
    assert fc.count("trim=0:3.000") == 2
    # Without shot durations, the legacy (no-trim) filter is emitted unchanged.
    b = build_scene_args(["c0.mp4"], "a.mp3", "s.ass", "o.mp4", 6.0, None)
    assert "trim=" not in b[b.index("-filter_complex") + 1]


def test_prefer_unused_reorders_footage():
    from core.video_factory import prefer_unused

    class C:
        def __init__(self, cid):
            self.id = cid

    pool = [C(1), C(2), C(3)]
    # Clip 1 already used → it drops behind the unused ones, order otherwise stable.
    assert [c.id for c in prefer_unused(pool, {1})] == [2, 3, 1]
    # Nothing to avoid → unchanged (fail-open).
    assert [c.id for c in prefer_unused(pool, None)] == [1, 2, 3]
    assert [c.id for c in prefer_unused(pool, set())] == [1, 2, 3]


def test_voice_check_flags_broken_tts(monkeypatch):
    """Deterministic voice sanity (zero API cost): silent, truncated or unreadable narration
    audio is caught BEFORE minutes of CPU rendering. Unlike vision QC this fails CLOSED."""
    from core import video_factory as vf

    healthy = {"mean_volume_db": -21.5, "max_volume_db": -3.0}
    monkeypatch.setattr(vf.media, "probe_duration", lambda p: 4.2)
    monkeypatch.setattr(vf.media, "probe_audio_stats", lambda p: healthy)
    assert vf.voice_check("a.mp3", "one two three four") is None

    # Effectively silent output.
    monkeypatch.setattr(vf.media, "probe_audio_stats",
                        lambda p: {"mean_volume_db": -78.0, "max_volume_db": -60.0})
    assert "silent" in vf.voice_check("a.mp3", "one two three four")

    # Undetectable volume (no report) is tolerated — silence detection needs evidence.
    monkeypatch.setattr(vf.media, "probe_audio_stats",
                        lambda p: {"mean_volume_db": None, "max_volume_db": None})
    assert vf.voice_check("a.mp3", "one two three four") is None

    # Truncated output: sub-second audio for a real sentence.
    monkeypatch.setattr(vf.media, "probe_duration", lambda p: 0.12)
    monkeypatch.setattr(vf.media, "probe_audio_stats", lambda p: healthy)
    assert "truncated" in vf.voice_check("a.mp3", "a longer narration line here")

    # Unreadable/corrupt file IS a problem.
    def boom(p):
        raise RuntimeError("corrupt")

    monkeypatch.setattr(vf.media, "probe_duration", boom)
    assert "unreadable" in vf.voice_check("a.mp3", "text here now")


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
    assert "sidechaincompress" in fc         # music is ducked UNDER the narration, not flat-mixed
    assert fc.index("sidechaincompress") < fc.index("amix")  # duck first, then mix
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
        language="en", topic="t", synopsis="s",
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

    # Drive report() the way produce() does: prep sub-band (0-30% of the scenes band) for every
    # scene first, then the render sub-band (30-100%) with encode progress + scene-done marks.
    n = 3
    band = vf._STAGE_BUDGET["scenes"]
    for si in range(n):                       # Phase A: prep
        emit(min(99.0, ((si + 1) / n * 30) / 100 * band))
    for si in range(n):                       # Phase C: render
        for p in (0, 40, 80, 100):
            emit(min(99.0, (30 + (si + p / 100) / n * 70) / 100 * band))
        emit(min(99.0, (30 + (si + 1) / n * 70) / 100 * band))
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


def test_batch_vet_plans():
    """One batched vet call for the episode; rejected scenes swap to candidate #2 and only the
    replacements get a second (single) batched call — ≤2 vision calls total, fail-open."""
    from types import SimpleNamespace

    from core.video_factory import _batch_vet_plans

    def mk_plan(n_found, pre_path):
        return {"clean": "story text",
                "found": [SimpleNamespace(download_url=f"u{i}", duration=5.0) for i in range(n_found)],
                "pre": {0: pre_path}}

    plans = [mk_plan(3, "/ws/s0_vet_0.mp4"), mk_plan(3, "/ws/s1_vet_0.mp4"), mk_plan(1, "/ws/s2_vet_0.mp4")]
    calls: list[int] = []
    downloads: list[str] = []

    def vet_batch(items):
        calls.append(len(items))
        if len(calls) == 1:
            return [True, False, False]   # scene 1 & 2 rejected
        return [True] * len(items)        # replacements accepted

    _batch_vet_plans(plans, vet_batch, path_for=lambda i, k: f"/ws/s{i}_vet_{k}.mp4",
                     download=lambda url, path: downloads.append(url))

    assert calls == [3, 1]                                 # 1 episode call + 1 replacement call
    assert plans[0]["pre"] == {0: "/ws/s0_vet_0.mp4"}      # accepted → untouched
    assert [c.download_url for c in plans[1]["found"]] == ["u1", "u2"]  # leader dropped
    assert plans[1]["pre"] == {0: "/ws/s1_vet_1.mp4"} and downloads == ["u1"]
    assert len(plans[2]["found"]) == 1                     # no second candidate → kept (fail-open)


def test_affiliate_link_in_description():
    """An affiliate link is appended to the description with a disclosure marker; absent → untouched."""
    from core.ai_engine import VideoScript
    from core.video_factory import pick_metadata

    vs = VideoScript(
        language="vi", topic="t", synopsis="s",
        scenes=[{"index": i, "narration": "n", "pexels_keywords": ["k"]} for i in range(3)],
        metadata_variations=[{"variant": v, "title": "T", "description": "Mô tả video.",
                              "tags": ["a", "b", "c"]} for v in "ABC"],
    )
    meta = pick_metadata(vs, 1, affiliate_url="https://shope.ee/x", affiliate_label="📚 Sách:")
    assert "📚 Sách: https://shope.ee/x" in meta["description"]
    assert "(affiliate link)" in meta["description"]  # disclosure is mandatory
    assert pick_metadata(vs, 1)["description"] == "Mô tả video."  # no link → untouched


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


def test_series_hashtag_stable_and_ascii():
    """Same tag every episode (computed in code, not by the model); diacritics folded."""
    from core.ai_engine import series_hashtag

    assert series_hashtag("Lịch sử VN: Nhà Trần") == "#LichSuVNNhaTran"
    assert series_hashtag("Lịch sử VN: Nhà Trần") == series_hashtag("Lịch sử VN: Nhà Trần")
    assert series_hashtag("đêm khuya") == "#DemKhuya"
    assert series_hashtag("!!!") == "#Shorts"  # degenerate topic → safe fallback


def test_metadata_prompt_bans_series_prefix_and_ep_numbers():
    """Titles must stand alone: no campaign-name prefix, no 'Ep N/Tập N'; description carries the
    stable series hashtag instead."""
    from core.ai_engine import build_script_prompt, series_hashtag

    p = build_script_prompt("Lịch sử VN: Nhà Trần", "vi", 30, 3)
    assert "NEVER put" in p and "episode numbering" in p
    assert series_hashtag("Lịch sử VN: Nhà Trần") in p  # exact tag injected, model can't drift


def test_title_prefix_prepended_and_capped():
    from core.ai_engine import VideoScript
    from core.video_factory import pick_metadata

    vs = VideoScript(
        language="vi", topic="t", synopsis="s",
        scenes=[{"index": i, "narration": "n", "pexels_keywords": ["k"]} for i in range(3)],
        metadata_variations=[{"variant": v, "title": "Một bí mật ít ai biết", "description": "d",
                              "tags": ["a", "b", "c"]} for v in "ABC"],
    )
    meta = pick_metadata(vs, 1, title_prefix="🔥 SỬ VIỆT |")
    assert meta["title"].startswith("🔥 SỬ VIỆT | Một bí mật")
    assert len(pick_metadata(vs, 1, title_prefix="X" * 90)["title"]) <= 100  # YouTube cap held
    assert pick_metadata(vs, 1)["title"] == "Một bí mật ít ai biết"  # no prefix → untouched


def test_voice_catalog_single_source_of_truth():
    """core/tts.VOICE_CHOICES is THE voice list: the form dropdown renders it and the AI designer
    may only propose from it — so a picked voice can never be one TTS can't synthesize."""
    from core.ai_engine import PROPOSABLE_VOICES
    from core.tts import DEFAULT_VOICES, VOICE_CHOICES

    assert set(VOICE_CHOICES) == {"vi", "en", "es"}
    for lang, voices in VOICE_CHOICES.items():
        ids = [vid for vid, _label in voices]
        assert PROPOSABLE_VOICES[lang] == ids            # designer list derives from the catalog
        assert DEFAULT_VOICES[lang] in ids               # the language default is always offered
        assert all(vid.startswith(f"{lang}-") for vid in ids)  # no cross-language voice ids
        assert all(label.strip() for _vid, label in voices)    # every entry has a human label


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


def test_split_sentences_and_pause():
    from core import tts

    assert tts.split_sentences("A cat. A dog? Yes!") == ["A cat.", "A dog?", "Yes!"]
    assert tts.split_sentences("no terminator here") == ["no terminator here"]
    assert tts.split_sentences("") == []
    # A question / cliffhanger earns a longer breath than a plain period.
    assert tts.pause_after("Why?") > tts.pause_after("Because.")
    assert tts.pause_after("Wait…") >= tts.pause_after("Now!")


def test_build_paced_concat_args_shape():
    from core import tts

    args = tts.build_paced_concat_args(["p0.mp3", "p1.mp3", "p2.mp3"], [0.4, 0.6], "out.mp3")
    fc = args[args.index("-filter_complex") + 1]
    assert args[:2] == ["-i", "p0.mp3"] and args.count("-i") == 3
    assert "aevalsrc=0:d=0.400" in fc and "aevalsrc=0:d=0.600" in fc  # two silence gaps
    assert "concat=n=5:v=0:a=1[out]" in fc                            # 3 parts + 2 gaps = 5 segments
    assert "libmp3lame" in args and args[-1] == "out.mp3"


def test_synthesize_paced_merges_timings(monkeypatch, tmp_path):
    """Per-sentence synthesis stitches with gaps and returns ONE timing list with absolute offsets;
    a single sentence falls through to plain synthesize()."""
    from core import tts

    # Each sentence: one word 0.0-1.0s; each part probes to 1.0s. Gap after sentence 1 = 0.35 ('.').
    def fake_synth(text, out, **k):
        open(out, "w").close()
        return [tts.WordTiming(text.split()[0], 0.0, 1.0)]

    monkeypatch.setattr(tts, "synthesize", fake_synth)
    monkeypatch.setattr("core.media.probe_duration", lambda p: 1.0)
    monkeypatch.setattr("core.ffmpeg_runner.run_ffmpeg", lambda *a, **k: None)

    merged = tts.synthesize_paced("Alpha here. Beta there.", str(tmp_path / "o.mp3"), language="en")
    assert [w.text for w in merged] == ["Alpha", "Beta"]
    assert merged[0].start == 0.0
    assert abs(merged[1].start - (1.0 + 0.35)) < 1e-6   # second sentence offset by dur + gap

    # Single sentence → straight delegation (no gaps, no concat).
    single = tts.synthesize_paced("Only one sentence here", str(tmp_path / "s.mp3"), language="en")
    assert [w.text for w in single] == ["Only"]


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
