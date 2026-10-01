"""FE-ALIGN Phase 2: 作品档案 / 写作统计 / dashboard v2。"""
from __future__ import annotations

from datetime import datetime, timedelta

from novel_system.db.models import AuthorDraft, ChapterGoal, SceneCard, StoryProject
from novel_system.services.writing_stats import (
    WRITING_STATS_TZ,
    WritingStatsService,
    count_words,
)
from tests.fixture_works import seed_fixture_works
from tests.support.api_client import create_project


_create_seq = 0


def _create_project(client, **overrides):
    global _create_seq
    _create_seq += 1
    payload = {
        "title": "测试作品",
        "outline_text": "一句话大纲",
        "genre": "悬疑",
        "mark": "测",
        "accent": "slate",
        "synopsis_line": "一句话简介",
        "target_word_count": 100000,
        "words_target_daily": 1000,
        **overrides,
    }
    return create_project(client, **payload, key=f"create-overview-{_create_seq}")


def test_project_profile_fields_roundtrip(client):
    project = _create_project(client)
    assert project["mark"] == "测"
    assert project["accent"] == "slate"
    assert project["synopsis_line"] == "一句话简介"
    assert project["words_target_daily"] == 1000
    assert project["is_demo"] is False

    listed = client.get("/api/v2/projects").json()["data"]["items"]
    row = next(item for item in listed if item["project_id"] == project["project_id"])
    assert row["accent"] == "slate"
    assert row["stats"]["words_total"] == 0
    assert "chapters_written" in row


def test_project_profile_patch(client):
    project = _create_project(client)
    response = client.patch(
        f"/api/v2/projects/{project['project_id']}/profile",
        json={"accent": "crimson", "words_target_daily": 2000, "title": "改名后"},
    )
    assert response.status_code == 200, response.text
    updated = response.json()["data"]["project"]
    assert updated["accent"] == "crimson"
    assert updated["words_target_daily"] == 2000
    assert updated["title"] == "改名后"

    blank_title = client.patch(
        f"/api/v2/projects/{project['project_id']}/profile", json={"title": "  "}
    )
    assert blank_title.status_code == 400


def test_count_words_matches_prototype_rule():
    assert count_words("") == 0
    assert count_words("你好 世界\n第二行") == 7
    assert count_words("<p>你好，<b>世界</b></p>") == 5


def test_writing_stats_same_day_accumulates(client, session):
    project = _create_project(client)
    svc = WritingStatsService(session)
    base = datetime(2026, 6, 10, 9, 0, tzinfo=WRITING_STATS_TZ)
    svc.record_words_delta(project["project_id"], 300, now=base)
    svc.record_words_delta(project["project_id"], 200, now=base + timedelta(hours=2))
    stats = svc.stats_payload(project["project_id"], now=base + timedelta(hours=3))
    assert stats["words_today"] == 500
    assert stats["streak_days"] == 1
    # 全书字数按目录里各场的字数求和（批准 #9），不跟保存增量走：这部作品还没有场
    assert stats["words_total"] == 0


def test_writing_stats_streak_across_days(client, session):
    project = _create_project(client)
    svc = WritingStatsService(session)
    day1 = datetime(2026, 6, 9, 22, 0, tzinfo=WRITING_STATS_TZ)
    day2 = day1 + timedelta(days=1)
    svc.record_words_delta(project["project_id"], 100, now=day1)
    svc.record_words_delta(project["project_id"], 100, now=day2)
    stats = svc.stats_payload(project["project_id"], now=day2)
    assert stats["streak_days"] == 2
    assert stats["words_today"] == 100  # 跨日清零重记


def test_writing_stats_streak_broken_after_gap(client, session):
    project = _create_project(client)
    svc = WritingStatsService(session)
    day1 = datetime(2026, 6, 1, 9, 0, tzinfo=WRITING_STATS_TZ)
    svc.record_words_delta(project["project_id"], 100, now=day1)
    # 断更两天后展示归零
    later = day1 + timedelta(days=3)
    stats = svc.stats_payload(project["project_id"], now=later)
    assert stats["streak_days"] == 0
    # 再次写作重记为 1
    svc.record_words_delta(project["project_id"], 50, now=later)
    stats = svc.stats_payload(project["project_id"], now=later)
    assert stats["streak_days"] == 1


def test_writing_stats_negative_delta_does_not_reduce_today(client, session):
    project = _create_project(client)
    svc = WritingStatsService(session)
    now = datetime(2026, 6, 10, 9, 0, tzinfo=WRITING_STATS_TZ)
    svc.record_words_delta(project["project_id"], 300, now=now)
    svc.record_words_delta(project["project_id"], -100, now=now + timedelta(minutes=5))
    stats = svc.stats_payload(project["project_id"], now=now + timedelta(minutes=6))
    assert stats["words_today"] == 300  # 负增量不回吐今日字数


def test_book_words_is_the_sum_of_the_scenes_in_the_catalog(client, session):
    """全书字数 = 目录里各场实际字数之和（批准 #9）：删场、删章、没走保存进来的字都对得上账。"""
    project = _create_project(client)
    project_id = project["project_id"]
    for chapter_id, trashed in (("ch-book-1", 0), ("ch-book-gone", 1)):
        session.add(ChapterGoal(chapter_id=chapter_id, project_id=project_id, chapter_goal="章", trashed_flag=trashed))
    session.flush()
    for seq, (scene_id, chapter_id, words, trashed) in enumerate((
        ("sc-book-1", "ch-book-1", 120, 0),
        ("sc-book-2", "ch-book-1", 80, 0),
        ("sc-book-trashed", "ch-book-1", 50, 1),
        ("sc-book-in-trashed-chapter", "ch-book-gone", 40, 1),
    ), start=1):
        session.add(
            SceneCard(
                scene_id=scene_id,
                chapter_id=chapter_id,
                project_id=project_id,
                scene_seq=seq,
                scene_goal="场",
                words_current=words,
                trashed_flag=trashed,
            )
        )
    session.commit()
    # 保存增量记账的计数器（旧口径）与全书字数无关
    WritingStatsService(session).record_words_delta(project_id, 9999)
    session.commit()

    stats = client.get(f"/api/v2/projects/{project_id}/writing-stats").json()["data"]
    assert stats["words_total"] == 200
    catalog = client.get(f"/api/v2/projects/{project_id}/catalog").json()["data"]
    assert stats["words_total"] == sum(chapter["words"]["cur"] for chapter in catalog["chapters"])


def test_author_draft_save_reports_words_delta(client, session):
    project = _create_project(client)
    chapter = ChapterGoal(
        chapter_id="ch-stats-1",
        project_id=project["project_id"],
        chapter_goal="测试章",
        planned_scene_count=1,
    )
    scene = SceneCard(
        chapter_id="ch-stats-1",
        scene_id="sc-stats-1",
        project_id=project["project_id"],
        scene_seq=1,
        scene_goal="测试场景",
    )
    draft = AuthorDraft(
        draft_id="draft-stats-1",
        object_type="scene",
        object_id="sc-stats-1",
        source_text_ref="test",
        content="",
        revision_no=1,
        status="current",
    )
    session.add_all([chapter, scene, draft])
    session.commit()

    response = client.patch(
        "/api/v1/author-drafts/draft-stats-1",
        json={"content": "正文一共十个字啊。", "base_revision_no": 1},
    )
    assert response.status_code == 200, response.text
    stats = client.get(
        f"/api/v2/projects/{project['project_id']}/writing-stats"
    ).json()["data"]
    assert stats["words_total"] == count_words("正文一共十个字啊。")
    assert stats["words_today"] == stats["words_total"]
    assert stats["streak_days"] == 1
    assert stats["last_active_at"]


def test_dashboard_v2_shape_with_fixture_works(client, session):
    seed_fixture_works(session)
    session.commit()

    data = client.get("/api/v2/projects/work-a/dashboard").json()["data"]
    assert data["resume"]["chapter_no"] == "08"
    # 阶段 X：scene_slug 是稳定的 scene_id；章内第几场单独给（主页焦点卡的「SC 03」）
    assert data["resume"]["scene_slug"].endswith("_SC03") or data["resume"]["scene_slug"]
    assert data["resume"]["scene_no"] == 3
    assert data["resume"]["scene_title"] == "样场 8-3"
    assert len(data["resume"]["last_lines"]) == 2
    assert "样例正文最后一行甲" in data["resume"]["last_lines"][0]
    assert data["resume"]["scene_words"] > 0
    assert data["brief"]["kind"] == "proactive"
    assert "样例场景目标" in data["brief"]["goal"]

    board = {row["step_key"]: row["status"] for row in data["snowflake"]}
    assert board["book_brief"] == "done"
    assert board["character_synopses"] == "active"
    assert board["character_bibles"] == "warn"
    assert board["scene_details"] == "done"

    recent = data["chapters_recent"]
    assert len(recent) == 5
    ch08 = next(r for r in recent if r["title"] == "样章08")
    assert ch08["active"] is True
    assert ch08["state"] == "writing"
    assert ch08["no"] == "08"
    assert ch08["pct"] > 0  # words rollup（场景字数求和 / words_target）

    # 全书字数 = 目录里各场字数之和（批准 #9；夹具播种的计数器基线不再显示）
    catalog = client.get("/api/v2/projects/work-a/catalog").json()["data"]
    assert data["stats"]["words_total"] == sum(chapter["words"]["cur"] for chapter in catalog["chapters"]) > 0
    assert data["stats"]["streak_days"] == 6  # streak_last_day=昨天 → 有效


def test_dashboard_v2_blank_project(client):
    project = _create_project(client)
    data = client.get(f"/api/v2/projects/{project['project_id']}/dashboard").json()["data"]
    assert data["resume"] is None
    assert data["brief"] is None
    assert all(row["status"] in {"todo", "active"} for row in data["snowflake"])
    assert data["chapters_recent"] == []


def test_fixture_works_seed_is_idempotent(client, session):
    seed_fixture_works(session)
    seed_fixture_works(session)
    session.commit()
    projects = session.query(StoryProject).filter(StoryProject.project_id.in_(["work-a", "work-b"])).all()
    assert {p.project_id for p in projects} == {"work-a", "work-b"}
    listed = client.get("/api/v2/projects").json()["data"]["items"]
    fixture_rows = [item for item in listed if item["project_id"] in ("work-a", "work-b")]
    assert len(fixture_rows) == 2
