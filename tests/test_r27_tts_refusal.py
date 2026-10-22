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
