"""pass2 R4 红→绿：章组复审/质量总览三处诚实性与作用域修复。

- BUG-101 (P3): 跨章 key_term 重复检测应**通用词频提取**，不再硬编码 demo 词。
- BUG-102 (P3): reference_safety 在**未提供受保护词**时应返回 None（未评估），而非假绿 1.0。
- BUG-103 (P3): overview 支持可选 project_id 作用域，作者只看自己作品。
"""

from __future__ import annotations

from novel_system.db.models import (
    ChapterGoal,
    ChapterState,
    FinalScene,
    SceneCard,
    SceneRunState,
    StoryProject,
)


def _seed_chapter(
    session,
    *,
    chapter_id: str,
    content: str,
    project_id: str | None = None,
    goal: str = "主角必须在公开证据和保护证人之间做出有代价的选择。",
) -> str:
    scene_id = f"{chapter_id}_SC01"
    final_row_id = f"final_scene_{scene_id}_v1"
    if project_id is not None:
        session.add(
            StoryProject(
                project_id=project_id,
                title=f"Project {project_id}",
                outline_text="Literary quality overview scope test",
            )
        )
        session.flush()
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            project_id=project_id,
            planned_scene_count=1,
            chapter_goal=goal,
            main_plot_push="推进主线冲突。",
            emotional_target="让主角付出代价。",
            ending_effect="结尾留下必须处理的钩子。",
        )
    )
    session.flush()
    session.add(ChapterState(chapter_id=chapter_id, current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=1,
            scene_goal="主角必须选择。",
            beats_json=["事件发生", "被迫抉择", "付出代价"],
            exit_change="局势改变，代价显现。",
            hook="新的悬念浮出。",
        )
    )
    session.flush()
    session.add(
        SceneRunState(
            scene_id=scene_id,
            scene_status="archived",
            current_final_scene_row_id=final_row_id,
            current_bundle_id=f"bundle_{scene_id}_v1",
            current_bundle_hash=f"hash_{scene_id}_v1",
        )
    )
    session.add(
        FinalScene(
            row_id=final_row_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            content=content,
            status="approved",
            source_bundle_id=f"bundle_{scene_id}_v1",
            source_bundle_hash=f"hash_{scene_id}_v1",
        )
    )
    return final_row_id


# ---------------------------------------------------------------------------
# BUG-102: reference_safety 未提供受保护词时为 None（未评估），不是假绿 1.0
# ---------------------------------------------------------------------------
def test_chapter_set_review_reference_safety_not_evaluated_without_terms(client, session) -> None:
    _seed_chapter(session, chapter_id="P2RS01", content="她在零点的雨里做出选择，代价是坐标暴露。")
    session.commit()

    # 不传 protected_terms —— UI 默认场景
    r = client.post("/api/v1/literary-quality/chapter-set-review", json={"chapter_ids": ["P2RS01"]})
    assert r.status_code == 200
    assert r.json()["data"]["scores"]["reference_safety"] is None  # 修前红(==1.0)

    # 传了词且命中 —— 仍判 0.0（红线扫描真生效）
    r2 = client.post(
        "/api/v1/literary-quality/chapter-set-review",
        json={"chapter_ids": ["P2RS01"], "protected_terms": ["坐标"]},
    )
    assert r2.json()["data"]["scores"]["reference_safety"] == 0.0

    # 传了词且未命中 —— 判 1.0（真扫且干净）
    r3 = client.post(
        "/api/v1/literary-quality/chapter-set-review",
        json={"chapter_ids": ["P2RS01"], "protected_terms": ["龙族"]},
    )
    assert r3.json()["data"]["scores"]["reference_safety"] == 1.0


# ---------------------------------------------------------------------------
# BUG-101: key_term 跨章重复检测对非 demo 作品也生效（通用词频，非硬编码）
# ---------------------------------------------------------------------------
def test_chapter_set_review_key_term_generalizes_to_non_demo_work(client, session) -> None:
    # 两章共享一个杜撰反复词「星轨」，零 demo 标记词
    _seed_chapter(
        session,
        chapter_id="P2KT01",
        content="星轨在穹顶缓缓旋转，她盯着星轨数了三圈，才敢迈步。星轨没有停。",
        goal="她在观测站追查星轨异常。",
    )
    _seed_chapter(
        session,
        chapter_id="P2KT02",
        content="第二夜，星轨再次偏移。他沿着星轨的轨迹走，星轨把他引向深处。",
        goal="他循着星轨深入禁区。",
    )
    session.commit()

    r = client.post(
        "/api/v1/literary-quality/chapter-set-review",
        json={"chapter_ids": ["P2KT01", "P2KT02"]},
    )
    assert r.status_code == 200
    rp = r.json()["data"]["repeated_patterns"]
    key_terms = [p for p in rp if p["cluster_type"] == "key_term"]
    # 修前红：key_term 全空（硬编码只认 demo 词）；修后绿：通用提取抓到「星轨」跨两章
    assert any(p["token"] == "星轨" and len(p["chapter_ids"]) >= 2 for p in key_terms), rp


# ---------------------------------------------------------------------------
# BUG-103: overview 支持可选 project_id 作用域
# ---------------------------------------------------------------------------
def test_literary_quality_overview_scopes_to_project_id(client, session) -> None:
    _seed_chapter(session, chapter_id="PJA_CH", content="甲项目的零点雨与抉择。", project_id="projA")
    _seed_chapter(session, chapter_id="PJB_CH", content="乙项目的潮汐与抉择。", project_id="projB")
    session.commit()

    # 无 project_id —— 全局（向后兼容）：两项目都在
    r_all = client.get("/api/v1/literary-quality/overview")
    ids_all = {it["chapter_id"] for it in r_all.json()["data"]["items"]}
    assert {"PJA_CH", "PJB_CH"} <= ids_all

    # 带 project_id=projA —— 只看甲项目（修前红：参数被忽略，含 PJB_CH）
    r_a = client.get("/api/v1/literary-quality/overview", params={"project_id": "projA"})
    ids_a = {it["chapter_id"] for it in r_a.json()["data"]["items"]}
    assert ids_a and ids_a <= {"PJA_CH"}
    assert "PJB_CH" not in ids_a
