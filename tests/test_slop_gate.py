"""ADR-079 — the pre-render script quality gate: kill slop where it costs one AI call, not a render.

The vision QC judges the finished video, so a bad script was only caught after 30-60 minutes of CPU
on a one-render-slot box. These tests pin the gate's judgment (pure text, 0 AI) and its worker
integration: one regenerate with the issues as avoid-notes, then an honest, non-transient failure.
"""
from __future__ import annotations


from core import slop_gate
from core.slop_gate import check_script, merged_cliches, normalize, similarity


def test_normalize_folds_diacritics_case_and_punctuation():
    assert normalize("Hãy CÙNG tìm-hiểu, nhé!") == "hay cung tim hieu nhe"
    assert normalize("Đồng   ý") == "dong y"                    # đ folds too (NFD leaves it)


def test_similarity_catches_a_reworded_repeat_but_not_a_new_episode():
    a = ("vị hoàng đế trẻ tuổi rời bỏ ngai vàng để vào chiến khu tham gia kháng chiến "
         "và cuối cùng bị lưu đày sang một xứ sở xa lạ nơi ông sống những ngày cuối đời")
    near = ("vị hoàng đế trẻ tuổi rời bỏ ngai vàng để vào chiến khu tham gia kháng chiến "
            "rồi sau đó bị lưu đày sang một xứ sở xa lạ nơi ông sống những ngày cuối cùng")
    fresh = ("một vị tướng già từ chối mọi vinh hoa phú quý để về quê dạy học "
             "và ngôi trường nhỏ của ông đã đào tạo ra ba thủ khoa của triều đình")
    assert similarity(a, near) >= slop_gate.BLOCK_SIMILARITY
    assert similarity(a, fresh) < slop_gate.WARN_SIMILARITY
    assert similarity("", a) == 0.0


def _recent(narration, title="Tập cũ", ep=3):
    return [{"episode": ep, "narration": narration, "title": title}]


LONG_FRESH = ("Một vị tướng già từ chối mọi vinh hoa. Ông về quê mở trường dạy học bên bờ sông. "
              "Ba mươi năm sau, ba thủ khoa của triều đình đều từng ngồi trên manh chiếu rách ấy.")


def test_block_on_near_repeat_and_title_duplicate():
    prev = "hôm nay chúng ta nói về vị vua cuối cùng của triều đại và hành trình lưu vong của ông"
    r = check_script(prev + " thêm vài chữ", "Tựa Mới", recent=_recent(prev))
    assert r.blocked and "nearly repeats episode 3" in r.issues[0]

    r2 = check_script(LONG_FRESH, "TẬP CŨ!", recent=_recent(prev, title="Tập cũ"))
    assert r2.blocked and "title duplicates" in " ".join(r2.issues)


def test_warn_on_cliches_and_rambling_hook_story_only():
    slop = ("Hãy cùng tìm hiểu về nhân vật này nhé. Bạn có biết rằng ông ấy rất nổi tiếng. "
            "Một câu chuyện hay.")
    r = check_script(slop, "T", recent=[])
    assert r.verdict == "warn" and "filler" in " ".join(r.issues)

    hook40 = " ".join(["từ"] * 40) + ". Phần sau ngắn."
    r2 = check_script(hook40, "T", recent=[])
    assert r2.verdict == "warn" and "opening sentence" in " ".join(r2.issues)

    # Quote mode: repetition/title checks stay, pacing heuristics don't apply to a poem.
    assert check_script(slop, "T", recent=[], content_style="quote").verdict == "ok"


def test_operator_blacklist_merges_with_defaults():
    cl = merged_cliches("câu này cấm\n\n  another banned line  ")
    assert "cau nay cam" in cl and "another banned line" in cl
    assert "in this video" in cl                                 # defaults survive
    assert merged_cliches(None) == slop_gate.DEFAULT_CLICHES


def test_old_episodes_without_fingerprints_narrow_the_check():
    r = check_script(LONG_FRESH, "Mới", recent=[{"episode": 1}])  # pre-gate task: no data
    assert r.verdict == "ok"


# ── Worker integration ────────────────────────────────────────────────────────
def _seed_campaign(session, user, channel, **cfg):
    from database.models import Campaign, Task

    from database.types import CampaignStatus

    cam = Campaign(user_id=user.id, channel_id=channel.id, topic_name="Sử Việt",
                   total_episodes=5, status=CampaignStatus.active,
                   config_json={"language": "vi", **cfg})
    session.add(cam)
    session.commit()
    session.refresh(cam)
    t = Task(campaign_id=cam.id, user_id=user.id, episode_number=2)
    session.add(t)
    session.commit()
    session.refresh(t)
    return cam, t


def _vscript(narration, title):
    from core.ai_engine import VideoScript

    return VideoScript(
        language="vi", topic="Sử", synopsis="s",
        scenes=[{"index": i, "narration": narration, "pexels_keywords": ["k"]} for i in range(3)],
        metadata_variations=[{"variant": v, "title": f"{title}-{v}", "description": "d",
                              "tags": ["a", "b", "c"]} for v in "ABC"])


def test_blocked_twice_fails_honestly_with_avoid_notes(session, user, channel, monkeypatch):
    """One regenerate carrying the gate's issues as avoid-notes; a second block fails the task with
    a message `core.failure` classifies as NON-transient — the autopilot must not burn AI calls
    re-failing it — and no script checkpoint survives (a Retry must write a fresh script)."""
    from core import failure
    from database.models import Task
    from database.types import TaskStatus
    from workers import video_worker

    cam, t = _seed_campaign(session, user, channel)
    prev = ("vị hoàng đế cuối cùng rời bỏ ngai vàng vào chiến khu kháng chiến "
            "rồi bị lưu đày sang xứ người xa lạ cho đến cuối đời")
    older = Task(campaign_id=cam.id, user_id=user.id, episode_number=1,
                 render_json={"narration": prev, "title": "Vị vua lưu vong"})
    session.add(older)
    session.commit()

    calls = []

    def same_slop(**k):
        calls.append(k.get("avoid") or [])
        return _vscript(prev + " thêm một chút", "Tựa khác")

    monkeypatch.setattr(video_worker, "generate_script", same_slop)
    monkeypatch.setattr(video_worker.video_factory, "produce",
                        lambda **k: (_ for _ in ()).throw(AssertionError("must not render slop")))

    video_worker.render_task(t.id)
    session.refresh(t)
    assert t.status == TaskStatus.FAILED
    assert "quality gate twice" in t.error_message
    assert len(calls) == 2                                        # exactly one regenerate
    assert any("your previous draft was rejected" in a for a in calls[1])
    assert not (t.render_json or {}).get("script")                # no slop checkpoint to resume
    assert failure.is_transient(t.error_message) is False         # autopilot leaves it to a human
    d = failure.diagnose(t.error_message)
    assert d and d["href"] == "/settings"


def test_warnings_ride_into_review_metadata_and_fingerprint_persists(session, user, channel,
                                                                     monkeypatch):
    from core.video_factory import RenderResult
    from database.models import BufferPoolItem
    from workers import video_worker

    cam, t = _seed_campaign(session, user, channel, publish_mode="review", auto_publish=False)
    warny = ("Hãy cùng tìm hiểu câu chuyện này nhé. Bạn có biết rằng đây là chuyện thật. "
             "Ông sinh năm một chín hai ba tại làng chài ven biển.")
    monkeypatch.setattr(video_worker, "generate_script", lambda **k: _vscript(warny, "Tựa"))
    monkeypatch.setattr(video_worker.video_factory, "produce", lambda **k: RenderResult(
        master_path="/no/m.mp4", thumbnail_path="/no/t.jpg",
        metadata={"title": "Tựa-A", "variant": "A"}, duration=10.0, scene_count=3))

    video_worker.render_task(t.id)
    buf = session.query(BufferPoolItem).filter_by(campaign_id=cam.id, episode_number=2).one()
    assert any("filler" in w for w in buf.metadata_json["slop_warnings"])
    session.refresh(t)
    assert warny in t.render_json["narration"]                    # the fingerprint future gates read
    assert t.render_json["title"] == "Tựa-A"
