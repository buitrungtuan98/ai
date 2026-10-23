"""R27 — a refused voice service is not a network blip, and a paused campaign keeps its videos.

The production report, two banners on one channel: every render failed with edge-tts'
`WSServerHandshakeError: 403` (a stale library after Microsoft rotated the endpoint's sign-in) and
was told "A provider was unreachable — almost always transient"; meanwhile episodes that HAD
rendered fine were deleted as "aged out before it could publish". The failures tripped the circuit
breaker, the breaker stopped slot publishing, and the expiry sweep went on aging the stranded
buffer as if nothing held it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# The exact summary block the worker stored (the URL is verbatim from the incident).
_EDGE_403 = (
    "Traceback (most recent call last):\n"
    '  File "/app/core/tts.py", line 154, in _synthesize_async\n'
    "    async for chunk in communicate.stream():\n"
    "aiohttp.client_exceptions.WSServerHandshakeError: 403, message='Invalid response status', "
    "url='wss://speech.platform.bing.com/consumer/speech/synthesize/readaloud/edge/v1?"
    "TrustedClientToken=6A5AA1D4EAFF4E9FB37E23D68491D6F4&ConnectionId=493ef3b0fc4d48609b3e2aa1"
    "&Sec-MS-GEC=A55148D764E4DCBA&Sec-MS-GEC-Version=1-143.0.3650.75'\n"
)


# ── Classification ───────────────────────────────────────────────────────────
def test_an_edge_tts_403_is_a_voice_refusal_not_a_transient_network_blip():
    from core import failure

    d = failure.diagnose(_EDGE_403)
    assert d is not None and d["cause"] == "The voice service refused the connection"
    assert "edge-tts" in d["fix"]
    assert failure.is_transient(_EDGE_403) is False   # retrying the same library 403s identically
    # It is a systemic fault the circuit breaker SHOULD see (every render fails the same way).
    assert failure.is_infrastructure(_EDGE_403) is False


def test_edge_tts_skew_error_classifies_with_the_refusal():
    from core import failure

    msg = "edge_tts.exceptions.SkewAdjustmentError: No server date in headers."
    assert failure.diagnose(msg)["cause"] == "The voice service refused the connection"


def test_a_url_never_decides_the_class():
    """`ConnectionId=` inside a URL used to match the network class's "connection"."""
    from core import failure

    msg = "ValueError: bad thing at url='https://example.test/x?ConnectionId=1&timeout=5'"
    assert failure.diagnose(msg) is None
    # Words OUTSIDE the URL still classify as before.
    assert failure.diagnose("ConnectionResetError: connection reset by peer at "
                            "https://example.test/")["cause"] == "A provider was unreachable"


def test_a_5xx_handshake_stays_transient():
    from core import failure

    msg = "aiohttp.client_exceptions.WSServerHandshakeError: 503, message='Invalid response status'"
    assert failure.diagnose(msg)["cause"] == "A provider was unreachable"
    assert failure.is_transient(msg) is True


# ── Expiry: a breaker-paused campaign holds its finished episodes ────────────
def _campaign(session, user, channel, status):
    from database.models import Campaign

    c = Campaign(user_id=user.id, channel_id=channel.id, topic_name="R27", current_episode=0,
                 total_episodes=5, status=status,
                 config_json={"language": "en", "posting_slots": ["21:00"]})
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _ready(session, camp, channel, ep, tmp_path, aged: datetime):
    from database.models import BufferPoolItem
    from database.types import BufferStatus

    f = tmp_path / f"c{camp.id}ep{ep}.mp4"
    f.write_bytes(b"video")
    b = BufferPoolItem(campaign_id=camp.id, channel_id=channel.id, episode_number=ep,
                       video_path=str(f), status=BufferStatus.ready, created_at=aged,
                       ready_at=aged, metadata_json={"title": f"Ep {ep}"})
    session.add(b)
    session.commit()
    session.refresh(b)
    return b


def test_expiry_spares_a_breaker_paused_campaign_but_still_sweeps_active_ones(
        session, user, channel, tmp_path, monkeypatch):
    import os

    from database.types import BufferStatus, CampaignStatus
    from workers import scheduler, task_queue

    monkeypatch.setattr(task_queue, "queued_publish_buffer_ids", lambda: set())
    aged = datetime.utcnow() - timedelta(hours=200)   # past even the 180h weekday-gated cutoff
    paused = _campaign(session, user, channel, CampaignStatus.failed)
    active = _campaign(session, user, channel, CampaignStatus.active)
    held = _ready(session, paused, channel, 1, tmp_path, aged)
    stale = _ready(session, active, channel, 1, tmp_path, aged)

    scheduler.expire_stale_buffers(session)
    session.refresh(held)
    session.refresh(stale)
    assert held.status == BufferStatus.ready and os.path.exists(held.video_path)
    assert stale.status == BufferStatus.expired and not os.path.exists(stale.video_path)


# ── Auto-QC: the vision judge gets the text leg's retry + chain fallback ─────
def _verdict_json() -> str:
    import json

    return json.dumps({"quality_score": 8, "issues": []})


def test_final_qc_retries_a_rate_limit_instead_of_parking(monkeypatch):
    import core.ai_engine as ai

    monkeypatch.setattr(ai, "_BACKOFF_BASE_SECONDS", 0)
    calls: list[dict] = []

    def flaky(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise RuntimeError("429 Resource has been exhausted (per minute)")
        return _verdict_json()

    monkeypatch.setattr(ai, "_call_gemini_vision", flaky)
    v = ai.judge_video_frames(["f.jpg"], api_key="k", model="m1")
    assert v.quality_score == 8 and len(calls) == 2
    # Thinking models spend output tokens before the JSON — the 2048 cap truncated verdicts.
    assert calls[0]["max_output_tokens"] >= 8192


def test_final_qc_falls_over_the_model_chain_on_a_spent_daily_quota(monkeypatch):
    import core.ai_engine as ai

    monkeypatch.setattr(ai, "_BACKOFF_BASE_SECONDS", 0)
    used: list[str] = []

    def by_model(**kw):
        used.append(kw["model"])
        if kw["model"] == "primary":
            raise RuntimeError("429 quota exceeded: GenerateRequestsPerDayPerProjectPerModel")
        return _verdict_json()

    monkeypatch.setattr(ai, "_call_gemini_vision", by_model)
    assert ai.judge_video_frames(["f.jpg"], api_key="k", model="primary,backup").quality_score == 8
    assert used == ["primary", "backup"]   # failed FAST on the daily cap, no retry burn


# ── Requalify: more than one chance, and a spent quota waits for its reset ───
def _parked(session, camp, channel, reason: str, *, age_hours: float = 3.0, **md):
    from database.models import BufferPoolItem
    from database.types import BufferStatus

    item = BufferPoolItem(
        campaign_id=camp.id, channel_id=channel.id, episode_number=1, video_path="/no/v.mp4",
        status=BufferStatus.awaiting_review,
        metadata_json={"qc": {"passed": True, "score": None, "unavailable": True,
                              "unavailable_reason": reason}, **md})
    session.add(item)
    session.commit()
    item.created_at = datetime.utcnow() - timedelta(hours=age_hours)
    session.commit()
    session.refresh(item)
    return item


def test_requalify_tries_again_after_a_failed_rejudge_up_to_the_cap(session, user, channel,
                                                                     monkeypatch):
    from database.types import CampaignStatus
    from workers import scheduler, task_queue

    monkeypatch.setattr(task_queue, "enqueue_requalify", lambda bid: None)
    camp = _campaign(session, user, channel, CampaignStatus.active)
    earlier = (datetime.utcnow() - timedelta(hours=3)).isoformat()
    item = _parked(session, camp, channel, "Gemini is rate-limiting right now",
                   auto_requalify=earlier)            # a pre-R27 marker: one try already spent
    assert scheduler.autopilot_requalify_channel(session, channel) == 1
    session.refresh(item)
    assert item.metadata_json["auto_requalify_n"] == 2
    assert scheduler.autopilot_requalify_channel(session, channel) == 0   # spaced ≥ 2h apart

    item.metadata_json = {**item.metadata_json, "auto_requalify": earlier,
                          "auto_requalify_n": scheduler.REQUALIFY_MAX_AUTO}
    session.commit()
    assert scheduler.autopilot_requalify_channel(session, channel) == 0   # capped → the human's


def test_requalify_waits_for_the_pacific_reset_on_a_spent_daily_quota(session, user, channel,
                                                                       monkeypatch):
    from database.types import CampaignStatus
    from workers import scheduler, task_queue

    monkeypatch.setattr(task_queue, "enqueue_requalify", lambda bid: None)
    noon_pacific = datetime(2026, 10, 22, 20, 0)   # 13:00 PDT — far from the midnight reset

    class _Clock(datetime):
        @classmethod
        def utcnow(cls):
            return noon_pacific

    monkeypatch.setattr(scheduler, "datetime", _Clock)
    reason = "Gemini daily quota is spent — it resets around midnight US-Pacific"
    camp = _campaign(session, user, channel, CampaignStatus.active)
    item = _parked(session, camp, channel, reason)
    item.created_at = noon_pacific - timedelta(hours=3)   # same Pacific day: still spent
    session.commit()
    assert scheduler.autopilot_requalify_channel(session, channel) == 0

    item.created_at = noon_pacific - timedelta(hours=30)  # a reset has passed since
    session.commit()
    assert scheduler.autopilot_requalify_channel(session, channel) == 1


# ── R29: every TTS request has something to say; dark is not black ───────────
def test_no_sentence_is_sent_to_the_voice_without_a_word_in_it():
    from core import tts

    assert tts.split_sentences('Orwell wrote: "War is peace."') == ['Orwell wrote: "War is peace."']
    assert tts.split_sentences('He said "Stop." Then he left.') == ['He said "Stop."', "Then he left."]
    assert tts.split_sentences('He left. "Run," she said.') == ["He left.", '"Run," she said.']
    assert tts.split_sentences("Why? … Because.") == ["Why? …", "Because."]
    assert tts.split_sentences("Part 13. 🔥") == ["Part 13. 🔥"]
    assert tts.split_sentences("… Then silence.") == ["… Then silence."]
    assert tts.split_sentences("Xin chào. Tạm biệt!") == ["Xin chào.", "Tạm biệt!"]
    assert tts.pause_after('"Why?"') == tts.pause_after("Why?")


def test_no_audio_received_is_named_and_stays_retryable():
    from core import failure

    msg = ("edge_tts.exceptions.NoAudioReceived: No audio was received. Please verify that your "
           "parameters are correct.")
    d = failure.diagnose(msg)
    assert d["cause"] == "The voice service returned no audio for a line"
    assert "fresh script" in d["fix"]
    assert failure.is_transient(msg) is True   # the deterministic cause is fixed upstream (R30)


def test_a_paused_campaign_keeps_its_videos_after_it_resumes(session, user, channel, tmp_path,
                                                            monkeypatch):
    """R30: the hold must STOP the age clock — skipping alone deleted them on the first sweep after
    the resume."""
    import os

    from database.types import BufferStatus, CampaignStatus
    from workers import scheduler, task_queue

    monkeypatch.setattr(task_queue, "queued_publish_buffer_ids", lambda: set())
    camp = _campaign(session, user, channel, CampaignStatus.failed)
    item = _ready(session, camp, channel, 1, tmp_path, datetime.utcnow() - timedelta(hours=10))
    t0 = datetime.utcnow()
    scheduler.expire_stale_buffers(session, now=t0)                       # held: clock stops
    camp.status = CampaignStatus.active                                   # ▶ Start, 5 days later
    session.commit()
    scheduler.expire_stale_buffers(session, now=t0 + timedelta(days=5))
    session.refresh(item)
    assert item.status == BufferStatus.ready and os.path.exists(item.video_path)
    assert item.metadata_json["held_hours"] >= 119                        # the pause was banked
    scheduler.expire_stale_buffers(session, now=t0 + timedelta(days=10)) # …but it still ages
    session.refresh(item)
    assert item.status == BufferStatus.expired


def test_dark_footage_is_not_a_black_screen_but_pure_black_is(tmp_path):
    import shutil
    import subprocess

    import pytest

    from core import media

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")

    def clip(color: str) -> str:
        out = str(tmp_path / f"{color}.mp4")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"color=c={color}:s=160x284:d=3", "-y", out], check=True)
        return out

    assert media.max_black_span(clip("0x101418")) == 0.0   # a night / deep-sea shot
    assert media.max_black_span(clip("0x000000")) > 2.5    # a broken, padded gap


# ── R30: Auto-QC says WHY it could not run, and drops the audio before giving up ──
def _qc_env(monkeypatch):
    from core import qc

    monkeypatch.setattr(qc.media, "probe_duration", lambda p: 10.0)
    monkeypatch.setattr(qc, "extract_frame", lambda v, o, at: None)
    monkeypatch.setattr(qc, "extract_audio", lambda v, o: None)
    return qc


def test_final_qc_falls_back_to_frames_only_when_the_audio_is_refused(monkeypatch):
    from types import SimpleNamespace

    qc = _qc_env(monkeypatch)
    calls: list = []

    def judge(frames, *, api_key, context, audio_path=None, **kw):
        calls.append(audio_path)
        if audio_path:
            raise RuntimeError("400 Audio input is not supported for this model")
        return SimpleNamespace(quality_score=8, issues=[])

    monkeypatch.setattr(qc, "judge_video_frames", judge)
    r = qc.run_final_qc("m.mp4", api_key="k")
    assert r.score == 8 and not r.unavailable and calls[-1] is None


def test_final_qc_names_the_real_cause_without_leaking_secrets(monkeypatch):
    qc = _qc_env(monkeypatch)
    calls: list = []

    def refused(frames, **kw):
        calls.append(kw.get("audio_path"))
        raise RuntimeError("400 API key not valid. https://g.example/v1?key=AIzaSySECRET")

    monkeypatch.setattr(qc, "judge_video_frames", refused)
    r = qc.run_final_qc("m.mp4", api_key="k")
    assert r.unavailable and "API key" in r.unavailable_reason
    assert len(calls) == 1                         # a refused key is not asked twice
    reason = qc._unavailable_reason(RuntimeError("boom at https://h.example/?key=AIzaSyX"))
    assert "boom" in reason and "AIza" not in reason and "https" not in reason


# ── R31: a line the voice cannot say is carried by its neighbour, or named ────
class NoAudioReceived(Exception):
    """Stands in for edge_tts.exceptions.NoAudioReceived (matched by name)."""


def _paced_env(monkeypatch, refuses):
    from core import ffmpeg_runner, media, tts

    said: list[str] = []

    def raw(text, part, voice, rate, pitch):
        said.append(text)
        if refuses(text):
            raise NoAudioReceived("No audio was received.")
        return [tts.WordTiming(text.split()[0], 0.0, 0.5)]

    monkeypatch.setattr(tts, "_synthesize_raw", raw)
    monkeypatch.setattr(media, "probe_duration", lambda p: 1.0)
    monkeypatch.setattr(ffmpeg_runner, "run_ffmpeg", lambda args: None)
    return tts, said


def test_an_unsayable_line_is_merged_with_the_next_one(monkeypatch):
    tts, said = _paced_env(monkeypatch, lambda t: t == "ἐναντιοδρομία.")
    timings = tts.synthesize_paced("Jung named it. ἐναντιοδρομία. Opposites turn.", "o.mp3")
    assert said == ["Jung named it.", "ἐναντιοδρομία.", "ἐναντιοδρομία. Opposites turn."]
    assert len(timings) == 2                       # two requests made it: the render survives


def test_an_unsayable_last_line_folds_into_the_one_before(monkeypatch):
    tts, said = _paced_env(monkeypatch, lambda t: t == "ἐναντιοδρομία.")
    timings = tts.synthesize_paced("Jung named it. ἐναντιοδρομία.", "o.mp3")
    assert said == ["Jung named it.", "ἐναντιοδρομία.", "Jung named it. ἐναντιοδρομία."]
    assert len(timings) == 1


def test_when_nothing_is_sayable_the_error_names_the_line_and_voice(monkeypatch):
    import pytest

    from core import failure

    tts, _ = _paced_env(monkeypatch, lambda t: True)
    with pytest.raises(tts.NoAudioError) as e:
        tts.synthesize_paced("Uno. Dos.", "o.mp3", language="en", voice="en-US-AriaNeural")
    msg = f"{type(e.value).__name__}: {e.value}"
    assert "'Uno. Dos.'" in msg and "en-US-AriaNeural" in msg and "rate +0%" in msg
    assert failure.diagnose(msg)["cause"] == "The voice service returned no audio for a line"


# ── R31: a stock clip with a reserved colour space is retagged, not fatal ────
def test_retag_args_are_a_stream_copy_with_bt709_tags():
    from core import video_factory as v

    args = v.build_retag_args("in.mp4", "out.mp4", "h264")
    assert args[args.index("-c") + 1] == "copy"
    assert "matrix_coefficients=1" in args[args.index("-bsf:v") + 1]
    assert v.build_retag_args("in.webm", "out.webm", "vp9") is None   # no filter → used as-is


def test_a_reserved_colour_space_clip_comes_out_bt709(tmp_path):
    import shutil
    import subprocess

    import pytest

    from core import video_factory as v

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    good, bad = str(tmp_path / "g.mp4"), str(tmp_path / "b.mp4")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=160x90:d=1",
                    "-c:v", "libx264", "-y", good], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-i", good, "-c", "copy", "-bsf:v",
                    "h264_metadata=matrix_coefficients=3", "-y", bad], check=True)   # 3 = reserved
    (fixed,) = v.retag_clips_bt709([bad])
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=color_space", "-of", "csv=p=0", fixed],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == "bt709"


# ── R32: UI dead ends and duplicates ─────────────────────────────────────────
def _web(session, user, channel):
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus

    camp = Campaign(user_id=user.id, channel_id=channel.id, topic_name="UI", total_episodes=9,
                    current_episode=0, status=CampaignStatus.active,
                    config_json={"posting_slots": ["20:00"]})
    session.add(camp)
    session.commit()
    tasks = []
    for ep in (2, 3, 4):
        t = Task(campaign_id=camp.id, user_id=user.id, episode_number=ep, status=TaskStatus.FAILED,
                 error_message="aiohttp.client_exceptions.WSServerHandshakeError: 403, x",
                 finished_at=datetime.utcnow(), render_json={"script": {"scenes": []}})
        session.add(t)
        tasks.append(t)
    session.commit()
    return camp, tasks


def test_retry_with_a_fresh_script_drops_the_saved_one(session, user, channel, monkeypatch):
    from starlette.testclient import TestClient

    import main
    from workers import video_worker

    monkeypatch.setattr(video_worker, "enqueue_task", lambda t: "job")
    _, tasks = _web(session, user, channel)
    with TestClient(main.app) as c:
        page = c.get(f"/episodes/{tasks[0].id}").text
        assert "Retry with a fresh script" in page
        c.post(f"/api/tasks/{tasks[0].id}/retry", data={"fresh": "1"})
        c.post(f"/api/tasks/{tasks[1].id}/retry")
    session.expire_all()
    assert "script" not in (tasks[0].render_json or {})        # fresh: a new script is written
    assert "script" in tasks[1].render_json                     # plain retry: the same script


def test_a_failed_episode_promises_no_render_and_no_slot(session, user, channel):
    from core.timeline import episode_timeline

    camp, tasks = _web(session, user, channel)
    stages = {m.key: m for m in episode_timeline(session, tasks[0], camp)}
    assert stages["rendered"].at is None                       # the failure time is not a render
    assert stages["scheduled"].at is None and "retry" in stages["scheduled"].note


def test_dashboard_groups_identical_failures_into_one_row(session, user, channel):
    from starlette.testclient import TestClient

    import main

    _web(session, user, channel)
    with TestClient(main.app) as c:
        body = c.get("/").text
    assert "UI · Ep 2, 3, 4</a></b> failed" in body and "Retry all 3" in body
    assert body.count("The voice service refused the connection") == 1


# ── R33: five sections, and Review is one queue ──────────────────────────────
def test_review_is_one_queue_with_failures_and_the_menu_has_five_sections(session, user, channel):
    from starlette.testclient import TestClient

    import main

    _web(session, user, channel)
    with TestClient(main.app) as c:
        review = c.get("/assets").text
        campaigns = c.get("/campaigns").text
    assert "Failed — needs a retry or a fix" in review and "Retry all 3" in review
    nav = review.split('aria-label="Primary"', 1)[1].split("</nav>", 1)[0]
    assert [x for x in ("Home", "Review", "Campaigns", "Channels", "Setup") if x in nav] == \
        ["Home", "Review", "Campaigns", "Channels", "Setup"]
    assert nav.count('class="nav-text"') == 5
    tabs = campaigns.split('class="section-tabs"', 1)[1].split("</nav>", 1)[0]
    assert "/episodes" in tabs and "/calendar" in tabs           # the Calendar is reachable now


def test_the_wizard_is_for_new_campaigns_only(session, user, channel):
    from starlette.testclient import TestClient

    import main

    camp, _ = _web(session, user, channel)
    with TestClient(main.app) as c:
        new = c.get("/campaigns/new").text
        dup = c.get(f"/campaigns/new?from_id={camp.id}").text
        edit = c.get(f"/campaigns/{camp.id}/settings").text
    assert 'id="wz-steps"' in new and 'class="wz" data-step="1"' in new
    assert new.count("data-wz-basic") == 5                 # topic, channel, episodes, language, format
    for page in (dup, edit):                               # values already set → the full form
        assert 'id="wz-steps"' not in page and 'class="wz"' not in page


def test_a_requeued_episode_does_not_show_its_old_failure_as_a_render(session, user, channel):
    from core.timeline import episode_timeline
    from database.types import TaskStatus

    camp, tasks = _web(session, user, channel)
    tasks[0].status = TaskStatus.PENDING_QUEUE        # what Retry does; finished_at is left over
    session.commit()
    stages = {m.key: m for m in episode_timeline(session, tasks[0], camp)}
    assert stages["rendered"].at is None
