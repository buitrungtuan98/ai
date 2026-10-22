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
