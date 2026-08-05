"""ADR-089 — a quality gate that can be passed, and a failure that recovers when the brief changes.

Three production incidents, one theme: the gate refused work for reasons the operator could neither
see nor act on, and nothing noticed when they acted anyway.

  * a channel tightened for finished VIDEO ("Reject at QC ≤ 7") made every SCRIPT need 8/10, so a
    good draft scoring 7 failed the episode twice and reported "too repetitive or generic";
  * a campaign's mandated catchphrases were counted as self-repetition and scored down by the judge,
    a gate no regeneration was allowed to pass;
  * a Page token that could read but not post ("#200 … lack of pages_manage_posts") classified as a
    transient publish failure, so it was retried at every posting slot, forever.

And underneath all three: an edit to the campaign was invisible to the machine, so episodes stayed
dead under a brief that no longer existed.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from core import autopilot, creative_brief, failure, slop_gate
from services import verification as _verification

# Captured at import, before conftest's autouse `no_live_credential_checks` replaces it: the token
# tests below drive the REAL check with a faked `requests`, so they need it back.
_REAL_CHECK = _verification.check_facebook_page


@pytest.fixture
def real_check(monkeypatch):
    monkeypatch.setattr(_verification, "check_facebook_page", _REAL_CHECK)


@pytest.fixture
def client():
    from starlette.testclient import TestClient

    import main

    with TestClient(main.app) as c:
        yield c


# ── A1: the script judge's threshold is its own (the Ep 5 incident) ───────────
def test_script_judge_threshold_is_clamped_but_can_still_be_looser(session, user, channel):
    """A channel strict about video must not become impossible to write for; a loose one keeps its
    own value — the clamp is a ceiling, not a replacement."""
    channel.autopilot_json = {"review": {"approve_min": 9, "reject_max": 7}}
    session.commit()
    assert autopilot.review_thresholds(channel)[1] == 7        # video QC still rejects ≤7
    assert autopilot.script_judge_reject_max(channel) == 4     # scripts do not

    channel.autopilot_json = {"review": {"approve_min": 5, "reject_max": 2}}
    session.commit()
    assert autopilot.script_judge_reject_max(channel) == 2     # deliberately loose stays loose

    channel.autopilot_json = None
    session.commit()
    assert autopilot.script_judge_reject_max(channel) == autopilot.SCRIPT_JUDGE_REJECT_MAX


def test_a_seven_out_of_ten_script_renders_on_a_strictly_reviewed_channel(
        session, user, channel, monkeypatch, tmp_path):
    """The incident itself: judge says 7/10 with two style notes, channel rejects video at ≤7 — the
    episode used to fail twice and blame repetition. It must render."""
    from core.video_factory import RenderResult
    from database.types import TaskStatus
    from workers import video_worker

    channel.autopilot_json = {"review": {"approve_min": 9, "reject_max": 7}}
    session.commit()
    cam, t = _seed(session, user, channel)

    monkeypatch.setattr(video_worker, "generate_script",
                        lambda **k: _script(_FRESH, "Tựa mới"))
    monkeypatch.setattr(video_worker, "_judge_script_safe",
                        lambda *a, **k: _verdict(7, ["punchier hook", "less preaching"]))
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: RenderResult(master_path=str(video), thumbnail_path="",
                                                 metadata={"title": "T", "description": "d",
                                                           "tags": []},
                                                 duration=30.0, scene_count=3))

    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.status != TaskStatus.FAILED, t.error_message
    steps = {j["step"]: j["note"] for j in (t.render_json or {}).get("journey", [])}
    assert "7/10" in steps.get("Script judge", "")            # the score is recorded, not fatal


# ── A2: mandated catchphrases are not repetition, and not the judge's business ─
def test_catchphrases_are_stripped_before_anything_is_compared():
    open_, close = "Khuya rồi đó, tắt đèn chưa?", "Ngủ ngon nha, mai kể tiếp."
    a = f"{open_} Vị tướng già về quê mở trường bên bờ sông. {close}"
    b = f"{open_} Người thợ rèn cuối cùng của làng đúc nốt quả chuông. {close}"

    # Same forced opening and closing, genuinely different episodes: raw comparison inflates the
    # score with words the writer is not allowed to drop.
    assert slop_gate.similarity(a, b) > slop_gate.similarity(
        slop_gate.without_phrases(a, (open_, close)),
        slop_gate.without_phrases(b, (open_, close)))

    recent = [{"episode": 4, "narration": b, "title": "Quả chuông"}]
    assert slop_gate.check_script(a, "Ngôi trường nhỏ", recent=recent,
                                  strip_phrases=(open_, close)).verdict == "ok"


def test_a_catchphrase_cannot_trip_the_operators_own_blacklist():
    """The blacklist is a slop filter; phrases the operator MANDATED are not slop."""
    open_, close = "chuyện cũ mình kể nhau nghe", "lòng mình chùng xuống"
    text = f"{open_}. Người thợ rèn đúc nốt quả chuông cuối cùng của làng. {close}."
    cliches = slop_gate.merged_cliches(f"{open_}\n{close}")
    assert slop_gate.check_script(text, "T", recent=[], cliches=cliches).verdict == "warn"
    assert slop_gate.check_script(text, "T", recent=[], cliches=cliches,
                                  strip_phrases=(open_, close)).verdict == "ok"


def test_the_judge_is_told_which_lines_are_mandated(monkeypatch):
    from core import ai_engine

    seen = {}

    def fake(**kw):
        seen.update(kw)
        return _verdict(8, [])

    monkeypatch.setattr(ai_engine, "generate_structured", fake)
    ai_engine.judge_script("n", "t", api_key="k",
                           required_phrases=("Khuya rồi đó, tắt đèn chưa?",))
    assert "REQUIRED BRANDING" in seen["prompt"]
    assert "Khuya rồi đó" in seen["prompt"]

    seen.clear()
    monkeypatch.setattr(ai_engine, "generate_structured", fake)
    ai_engine.judge_script("n", "t", api_key="k")
    assert "REQUIRED BRANDING" not in seen["prompt"]     # nothing mandated, nothing claimed


def test_only_switched_on_catchphrases_count_as_mandated():
    """One rule for generation, the gate and the judge — the disagreement that made the deadlock."""
    cfg = {"catchphrase_open": "A", "catchphrase_close": "B"}
    assert creative_brief.active_catchphrases(cfg) == ("A", "B")      # pre-flag campaigns: on
    assert creative_brief.active_catchphrases(
        {**cfg, "catchphrase_close_on": False}) == ("A",)
    assert creative_brief.active_catchphrases({"catchphrase_open": "  "}) == ()
    assert creative_brief.active_catchphrases(None) == ()


def test_the_judge_receives_the_campaigns_catchphrases(session, user, channel, monkeypatch):
    from core import ai_engine
    from workers import video_worker

    monkeypatch.undo()          # conftest stubs `_judge_script_safe`; this test drives the real one
    seen = {}
    monkeypatch.setattr(ai_engine, "judge_script",
                        lambda n, t, **kw: seen.update(kw) or _verdict(9, []))
    cfg = {"catchphrase_open": "Khuya rồi đó", "catchphrase_close": "Ngủ ngon nha",
           "catchphrase_close_on": False}
    video_worker._judge_script_safe(user, channel, cfg, {"narration": "n", "title": "t"}, "k", "m")
    assert seen["required_phrases"] == ("Khuya rồi đó",)


# ── A3/A4: the regenerate must actually hear the objection ────────────────────
def test_gate_objections_survive_a_full_avoid_list(session, user, channel, monkeypatch):
    """`compose_system_prompt` keeps 10 avoid-notes. A long-running campaign already has 10, so an
    APPENDED gate objection was dropped and the regenerate ran the same prompt that had just failed."""
    from database.models import Task
    from workers import video_worker

    cam, t = _seed(session, user, channel)
    cam.learning_json = {"reject_reasons": [f"old reason {i}" for i in range(10)]}
    prev = "hôm nay chúng ta nói về vị vua cuối cùng của triều đại và hành trình lưu vong của ông"
    session.add(Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                     render_json={"narration": prev, "title": "Vua lưu vong"}))
    session.commit()

    calls = []
    monkeypatch.setattr(video_worker, "generate_script",
                        lambda **k: calls.append(k.get("avoid") or [])
                        or _script(prev + " thêm vài chữ", "Tựa khác"))
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: (_ for _ in ()).throw(AssertionError("must not render")))

    video_worker.render_task(t.id)
    from core.ai_engine import compose_system_prompt

    assert any("previous draft was rejected" in a for a in calls[1][:10])
    assert "previous draft was rejected" in compose_system_prompt("vi", avoid=calls[1])


def test_a_duplicate_title_issue_quotes_the_title_it_duplicates():
    """The regenerating model never sees earlier titles — only synopses — so "duplicates episode 12"
    asked it to avoid something it could not look up."""
    recent = [{"episode": 12, "narration": "x", "title": "Vị vua lưu vong"}]
    r = slop_gate.check_script(_FRESH, "vị vua lưu vong", recent=recent)
    assert r.blocked and "Vị vua lưu vong" in " ".join(r.issues)


# ── B1: the reasoning survives the failure ───────────────────────────────────
def test_a_failed_render_keeps_the_decisions_it_made(session, user, channel, monkeypatch):
    """"How this render was judged (1 decision)" on an episode that made four — the journey was
    only written on the success path."""
    from database.types import TaskStatus
    from workers import video_worker

    cam, t = _seed(session, user, channel)
    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _script(_FRESH, "Tựa"))
    monkeypatch.setattr(video_worker, "_judge_script_safe", lambda *a, **k: _verdict(3, ["weak hook"]))
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: (_ for _ in ()).throw(AssertionError("must not render")))

    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.FAILED
    journey = (t.render_json or {}).get("journey") or []
    steps = [j["step"] for j in journey]
    # Both drafts were judged and both refusals are recorded — and they are attributed to the JUDGE,
    # not to the repetition gate that had already passed them.
    assert steps.count("Script judge") == 2
    assert steps.count("Script gate") == 2 and "Render" in steps
    assert any("3/10" in j["note"] for j in journey)
    assert len(journey) > 1


# ── B2: the two ways to fail the gate need different advice ───────────────────
def test_a_judge_rejection_and_a_repetition_block_classify_apart():
    judged = ("RuntimeError: Script failed the quality gate twice: script judge scored it 4/10 "
              "(this channel rejects 4/10 or below); the hook is generic")
    repeat = ("RuntimeError: Script failed the quality gate twice: nearly repeats episode 3 "
              "(83% of its phrasing) — write a genuinely new episode")

    dj, dr = failure.diagnose(judged), failure.diagnose(repeat)
    assert "judge" in dj["cause"].lower() and "repeat" in dr["cause"].lower()
    assert dj["href"] == "/channels"              # where the threshold that refused it lives
    assert dr["href"] == "/campaigns"             # where the topic that repeated itself lives
    assert "Reject at QC" in dj["fix"]            # names the dial, because it is the usual cause
    # The blacklist only ever warns — advising it could not fix either failure.
    assert "blacklist" not in dj["fix"] and "blacklist" not in dr["fix"]
    assert failure.is_transient(judged) is False and failure.is_transient(repeat) is False


# ── B3/B4: a live token that cannot post ─────────────────────────────────────
_NO_PERM = ("services.facebook_service.FacebookError: Facebook Reel start: (#200) Subject does not "
            "have permission to post videos on this target, due to lack of pages_manage_posts "
            "permission.")


def test_a_missing_post_permission_is_not_a_transient_publish_failure():
    """Unmatched messages default to transient=True, so this was retried by the autopilot AND
    re-published at every posting slot — an error no amount of waiting can clear."""
    msg = ("Publish failed — the rendered video is safe in the buffer.\n" + _NO_PERM
           + "\n\nTraceback (most recent call last):\n"
             '  File "/app/services/facebook_service.py", line 244, in _upload_reel\n' + _NO_PERM)
    assert failure.is_transient(msg) is False
    d = failure.diagnose(msg)
    assert d and "cannot post" in d["cause"]
    assert "pages_manage_posts" in d["fix"] and d["href"] == "/channels"
    assert "no re-render" in d["fix"]             # the video is fine; only the token is not


def test_the_channel_is_retired_when_graph_confirms_the_scope_is_missing(
        session, user, channel, monkeypatch):
    from database.types import ChannelStatus, Platform
    from services import facebook_service
    from workers import video_worker

    channel.platform = Platform.facebook
    session.commit()
    cam, _t = _seed(session, user, channel)
    monkeypatch.setattr(facebook_service, "token_definitely_dead", lambda ch: True)
    assert video_worker._mark_channel_expired(
        session, cam, facebook_service.FacebookError(_NO_PERM, code=200)) is True
    session.refresh(channel)
    assert channel.status == ChannelStatus.expired

    # …and never on a verdict Graph did not give: re-verification is what keeps this honest.
    channel.status = ChannelStatus.active
    session.commit()
    monkeypatch.setattr(facebook_service, "token_definitely_dead", lambda ch: False)
    assert video_worker._mark_channel_expired(
        session, cam, facebook_service.FacebookError(_NO_PERM, code=200)) is False


def test_the_scope_check_is_three_state(monkeypatch):
    import requests

    from services import verification

    def answer(payload, status=200):
        class R:
            status_code = status

            def json(self):
                return payload
        monkeypatch.setattr(requests, "get", lambda *a, **k: R())

    answer({"data": {"scopes": ["pages_read_engagement", "pages_show_list"]}})
    assert verification.missing_post_permission("t") is True
    answer({"data": {"scopes": ["pages_manage_posts", "pages_show_list"]}})
    assert verification.missing_post_permission("t") is False
    # No scope list is not evidence of no permissions (system-user tokens answer this way).
    answer({"data": {}})
    assert verification.missing_post_permission("t") is None
    answer({}, status=400)
    assert verification.missing_post_permission("t") is None


def test_a_read_only_page_token_is_refused_at_save_time(monkeypatch, real_check):
    """Identity was checked and capability never was, so the failure surfaced weeks later on the
    first upload — with episodes already rendered and waiting for a slot that could not accept them."""
    from services import verification

    monkeypatch.setattr(verification, "missing_post_permission", lambda token: True)
    import requests

    class R:
        status_code = 200

        def json(self):
            return {"id": "1234567890", "name": "Mẹo Bếp", "metadata": {"type": "page"}}

    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    check = verification.check_facebook_page("1234567890", "EAA" + "x" * 60)
    assert check.ok is False
    assert "pages_manage_posts" in check.detail
    assert "Mẹo Bếp" in check.detail              # names the Page, so it is clearly the right one


# ── C: the brief is versioned, edits are logged, and edits revive episodes ────
def test_only_creative_settings_change_the_brief_version(session, user, channel):
    cam, _t = _seed(session, user, channel)
    before = creative_brief.key_digests(cam, channel, user)

    cam.config_json = {**cam.config_json, "music_volume": 0.9, "caption_theme": "neon"}
    session.commit()
    assert creative_brief.changed(before, creative_brief.key_digests(cam, channel, user)) == []

    cam.config_json = {**cam.config_json, "persona": "a warmer narrator"}
    cam.topic_name = "Lịch sử VN: Miền Bắc"
    session.commit()
    after = creative_brief.key_digests(cam, channel, user)
    assert creative_brief.changed(before, after) == ["persona", "topic_name"]
    assert creative_brief.fingerprint(before) != creative_brief.fingerprint(after)
    # Digests only — the operator's own words never travel into the record.
    assert "warmer narrator" not in str(after)


def test_the_judge_threshold_is_part_of_the_brief(session, user, channel):
    cam, _t = _seed(session, user, channel)
    before = creative_brief.key_digests(cam, channel, user)
    channel.autopilot_json = {"review": {"approve_min": 5, "reject_max": 2}}
    session.commit()
    assert creative_brief.changed(
        before, creative_brief.key_digests(cam, channel, user)) == ["judge_threshold"]


def test_an_edited_brief_re_queues_the_episode_its_gate_refused(
        session, user, channel, monkeypatch):
    """The question this whole ADR answers: Story A failed the gate, the operator rewrote it to A′,
    and nothing connected the two — the episode stayed dead under a brief that no longer existed."""
    from database.types import TaskStatus
    from workers import scheduler, video_worker

    cam, t = _seed(session, user, channel)
    _fail_at_gate(session, t, cam, channel, user)

    renders = []
    monkeypatch.setattr(video_worker, "enqueue_task", lambda t: renders.append(t.id) or "job")

    # Nothing edited yet: the classifier's "a human must change something" still holds.
    assert scheduler.retry_after_brief_edits(session) == 0
    assert not renders

    cam.topic_name = "Lịch sử VN: Miền Trung"
    session.commit()
    assert scheduler.retry_after_brief_edits(session) == 1
    session.refresh(t)
    assert t.status == TaskStatus.PENDING_QUEUE
    assert t.error_message is None and renders == [t.id]

    # ONE attempt per brief version — a tick every minute must not re-render it forever.
    t.status = TaskStatus.FAILED
    session.commit()
    assert scheduler.retry_after_brief_edits(session) == 0

    # …and a FURTHER edit is a further intent, so it earns another attempt.
    cam.config_json = {**cam.config_json, "persona": "quieter"}
    session.commit()
    assert scheduler.retry_after_brief_edits(session) == 1


def test_the_brief_edit_retry_logs_what_changed(session, user, channel, monkeypatch):
    from database.models import AutopilotAction
    from sqlalchemy import select
    from workers import scheduler, video_worker

    cam, t = _seed(session, user, channel)
    _fail_at_gate(session, t, cam, channel, user)
    cam.config_json = {**cam.config_json, "persona": "warmer"}
    session.commit()
    monkeypatch.setattr(video_worker, "enqueue_task", lambda t: "job")

    scheduler.retry_after_brief_edits(session)
    row = session.scalars(select(AutopilotAction).where(
        AutopilotAction.kind == "requeued")).first()
    assert row is not None and "brief changed" in row.summary
    assert row.evidence["changed"] == ["persona"]


def test_an_expired_channel_does_not_get_its_gate_failures_re_rendered(
        session, user, channel, monkeypatch):
    """The single render slot must not be spent on a channel that cannot publish (ADR-076)."""
    from database.types import ChannelStatus
    from workers import scheduler, video_worker

    cam, t = _seed(session, user, channel)
    _fail_at_gate(session, t, cam, channel, user)
    cam.topic_name = "Đổi hẳn chủ đề"
    channel.status = ChannelStatus.expired
    session.commit()
    monkeypatch.setattr(video_worker, "enqueue_task",
                        lambda t: (_ for _ in ()).throw(AssertionError("must not render")))
    assert scheduler.retry_after_brief_edits(session) == 0


def test_the_regenerated_draft_is_told_the_brief_moved(session, user, channel, monkeypatch):
    """Avoid-notes written against the old brief ("avoid mentioning the temple") outlive the topic
    that made them true and quietly re-create the draft the gate refused."""
    from workers import video_worker

    cam, t = _seed(session, user, channel)
    _fail_at_gate(session, t, cam, channel, user)
    cam.config_json = {**cam.config_json, "persona": "warmer"}
    session.commit()

    calls = []
    monkeypatch.setattr(video_worker, "generate_script",
                        lambda **k: calls.append(k.get("avoid") or []) or _script(_FRESH, "T"))
    monkeypatch.setattr(video_worker, "_judge_script_safe", lambda *a, **k: None)
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("stop after the script")))

    video_worker.render_task(t.id)
    assert any("brief CHANGED" in a for a in calls[0])
    assert any("persona" in a for a in calls[0])


def test_editing_a_campaign_records_what_changed(client):
    """The audit trail the retry log entries refer back to — and the operator's own answer to
    "what did I change, and when?"."""
    from database.db_session import SessionLocal
    from database.models import AutopilotAction
    from sqlalchemy import select

    cam = _seed_via_web(client)
    client.post(f"/campaigns/{cam.id}/edit",
                data={"topic_name": cam.topic_name, "channel_id": str(cam.channel_id),
                      "total_episodes": str(cam.total_episodes), "language": "vi",
                      "persona": "a warmer narrator"},
                follow_redirects=False)

    db = SessionLocal()
    rows = db.scalars(select(AutopilotAction).where(
        AutopilotAction.kind == "config_edit")).all()
    assert rows and "persona" in rows[0].evidence["changed"]
    assert "persona" in rows[0].summary
    # The words themselves stay out of the audit table; only the fact that they moved is recorded.
    assert "warmer narrator" not in rows[0].summary
    db.close()


def test_the_episode_page_says_the_brief_moved(session, user, channel, client, monkeypatch):
    from database.db_session import SessionLocal
    from database.models import Campaign, Task
    from database.types import CampaignStatus, TaskStatus

    db = SessionLocal()
    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Sử Việt",
                   total_episodes=5, status=CampaignStatus.active, config_json={"language": "vi"})
    db.add(cam)
    db.commit()
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=9, status=TaskStatus.FAILED,
             finished_at=datetime.utcnow(),
             error_message="RuntimeError: Script failed the quality gate twice: script judge "
                           "scored it 4/10")
    db.add(t)
    db.commit()
    digests = creative_brief.key_digests(cam, channel, user)
    t.render_json = {"gate_failure": {"brief": digests,
                                      "fingerprint": creative_brief.fingerprint(digests),
                                      "issues": ["weak hook"], "at": "now"}}
    db.commit()
    tid = t.id

    body = client.get(f"/episodes/{tid}").text
    assert "You changed this campaign since it failed" not in body

    cam.config_json = {**cam.config_json, "persona": "warmer"}
    db.commit()
    db.close()
    body = client.get(f"/episodes/{tid}").text
    assert "You changed this campaign since it failed" in body
    assert "persona" in body


# ── helpers ──────────────────────────────────────────────────────────────────
_FRESH = ("Một vị tướng già từ chối mọi vinh hoa. Ông về quê mở trường dạy học bên bờ sông. "
          "Ba mươi năm sau, ba thủ khoa của triều đình đều từng ngồi trên manh chiếu rách ấy.")


def _seed(session, user, channel, **cfg):
    from database.models import Campaign, Task
    from database.types import CampaignStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Sử Việt",
                   total_episodes=5, status=CampaignStatus.active,
                   config_json={"language": "vi", **cfg})
    session.add(cam)
    session.commit()
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=2)
    session.add(t)
    session.commit()
    session.refresh(cam)
    session.refresh(t)
    return cam, t


def _fail_at_gate(session, task, campaign, channel, user):
    """Put a task in the state the gate leaves behind: FAILED, non-transient, brief version recorded."""
    from database.types import TaskStatus

    digests = creative_brief.key_digests(campaign, channel, user)
    task.status = TaskStatus.FAILED
    task.finished_at = datetime.utcnow()
    task.error_message = ("RuntimeError: Script failed the quality gate twice: script judge scored "
                          "it 4/10; the hook is generic")
    task.render_json = {"gate_failure": {"brief": digests,
                                         "fingerprint": creative_brief.fingerprint(digests),
                                         "issues": ["the hook is generic"],
                                         "at": datetime.utcnow().isoformat()}}
    session.commit()


def _script(narration, title):
    from core.ai_engine import VideoScript

    return VideoScript(
        language="vi", topic="Sử", synopsis="s",
        scenes=[{"index": i, "narration": narration, "pexels_keywords": ["k"]} for i in range(3)],
        metadata_variations=[{"variant": v, "title": f"{title}-{v}", "description": "d",
                              "tags": ["a", "b", "c"]} for v in "ABC"])


def _verdict(score, issues):
    from core.ai_engine import ScriptVerdict

    return ScriptVerdict(score=score, issues=issues)


def _seed_via_web(client):
    from database.db_session import SessionLocal
    from database.models import Campaign, Channel

    client.post("/channels/facebook",
                data={"channel_name": "P", "page_id": "1", "page_access_token": "t"},
                follow_redirects=False)
    db = SessionLocal()
    cid = db.query(Channel).first().id
    db.close()
    client.post("/campaigns", data={"topic_name": "Sử Việt", "channel_id": str(cid),
                                    "total_episodes": "5", "language": "vi"},
                follow_redirects=False)
    db = SessionLocal()
    cam = db.query(Campaign).first()
    db.close()
    return cam
