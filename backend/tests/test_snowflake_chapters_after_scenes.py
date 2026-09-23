"""阶段 K（2026-09-14 雪花评估第二轮）：章在场景之后。

Ingermanson 的雪花没有「章」；章是列完场之后的包装决定。本项目的 07 步曾让模型一次交出五段展开
加章表、硬编码「12–20 章」——章数在场景数之前就定了。这里锁住：
- 07 的章表可以留空（不算缺失），提示词不再硬编码章数；
- 按场景列表提议章表：三个灾难各自收束一章、章数按作品设置或每章约三场、每幕至少一章；
- 提议落库、归属重排、镜像回 07 草稿；已有章表时要显式 replace。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from sqlalchemy import select

from novel_system.db.models import SnowflakeChapterPlan, SnowflakeScenePlan, SnowflakeStepRun, StoryProject
from novel_system.services.errors import DomainError
from novel_system.services.snowflake_chaptering import SnowflakeChapteringService, propose_chapter_chunks
from novel_system.services.snowflake_steps import diagnose_step_pressure, step_completeness
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

PROJECT_ID = "prj-chapters"


def _rows(count: int, spine_at: dict[int, str]) -> list[dict]:
    return [
        {
            "row_uid": f"u{index:02d}",
            "scene_seq": index,
            "summary": f"第 {index} 场",
            "primary_form": "proactive",
            "scene_type": "proactive",
            "location": "雨城",
            "crucible": "退不出的困局",
            "pov_character_id": "c1",
            "chapter_role": "推进",
            "spine": spine_at.get(index, ""),
        }
        for index in range(1, count + 1)
    ]


def _seed(session, count: int = 12, spine_at: dict[int, str] | None = None, target_chapter_count: int = 0) -> SnowflakeWorkspaceService:
    session.add(
        StoryProject(
            project_id=PROJECT_ID,
            title="章在场景之后",
            outline_text="大纲",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
            target_chapter_count=target_chapter_count,
        )
    )
    session.flush()
    service = SnowflakeWorkspaceService(session)
    service.update_step(PROJECT_ID, "long_synopsis", {"draft": {"paragraphs": ["一", "二", "三", "四", "五"], "chapters": []}})
    service.update_step(PROJECT_ID, "scene_list", {"draft": {"scenes": _rows(count, spine_at or {4: "灾一", 8: "灾二", 11: "灾三"})}})
    return service


def _plans(session) -> list[SnowflakeScenePlan]:
    return list(
        session.execute(
            select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == PROJECT_ID).order_by(SnowflakeScenePlan.scene_seq)
        ).scalars().all()
    )


def _chapters(session) -> list[SnowflakeChapterPlan]:
    return list(
        session.execute(
            select(SnowflakeChapterPlan)
            .where(SnowflakeChapterPlan.project_id == PROJECT_ID, SnowflakeChapterPlan.removed_at.is_(None))
            .order_by(SnowflakeChapterPlan.chapter_seq)
        ).scalars().all()
    )


def test_chunks_close_each_disaster_chapter_and_respect_the_target_count(session) -> None:
    service = _seed(session, count=12, target_chapter_count=6)
    plans = service._scene_plans(PROJECT_ID)
    by_uid = {plan.row_uid: plan for plan in plans}
    chunks = propose_chapter_chunks(plans, target_chapter_count=6)
    assert len(chunks) == 6
    assert sum(len(chunk["scenes"]) for chunk in chunks) == 12
    # 灾一 / 灾二 / 灾三 各自是所在章的最后一场，且章带同名脊柱
    for mark, uid in (("灾一", "u04"), ("灾二", "u08"), ("灾三", "u11")):
        chunk = next(item for item in chunks if by_uid[uid] in item["scenes"])
        assert chunk["scenes"][-1] is by_uid[uid], mark
        assert chunk["spine"] == mark
    acts = [chunk["act"] for chunk in chunks]
    assert acts == sorted(acts) and set(acts) == {1, 2, 3}
    # 没有目标章数：按每章约三场推
    assert len(propose_chapter_chunks(plans, scenes_per_chapter=3)) == 4
    # 没有脊柱标记：整本一幕，仍然按章数均分
    plain = [plan for plan in plans]
    for plan in plain:
        plan.spine = ""
    assert [chunk["act"] for chunk in propose_chapter_chunks(plain, target_chapter_count=3)] == [1, 1, 1]


def test_propose_persists_assigns_and_mirrors_into_the_outline(session) -> None:
    service = _seed(session, count=12, target_chapter_count=6)
    chaptering = SnowflakeChapteringService(session)
    result = chaptering.propose_from_scenes(PROJECT_ID, {})
    assert result["created_chapter_count"] == 6 and result["replaced_chapter_count"] == 0
    assert result["totals"]["unassigned_count"] == 0
    chapters = _chapters(session)
    assert [chapter.chapter_seq for chapter in chapters] == [1, 2, 3, 4, 5, 6]
    assert [chapter.spine for chapter in chapters if chapter.spine] == ["灾一", "灾二", "灾三"]
    assert all(plan.chapter_plan_id for plan in _plans(session))
    # 镜像回 07 草稿：前端表格看到的就是这几章（带 row_uid）
    run = session.execute(
        select(SnowflakeStepRun).where(SnowflakeStepRun.project_id == PROJECT_ID, SnowflakeStepRun.step_key == "long_synopsis")
        .order_by(SnowflakeStepRun.version.desc())
    ).scalars().first()
    session.refresh(run)
    mirrored = run.draft_json["chapters"]
    assert [item["row_uid"] for item in mirrored] == [chapter.row_uid for chapter in chapters]
    # 章摘要 =「这一章把局面推到哪」→ 取章末那一场（第一章装第 1–2 场），不是开头那一场
    assert mirrored[0]["summary"] == "第 2 场"

    # 已有章表：不带 replace 拒绝；带 replace 重排（旧章软删）
    with pytest.raises(DomainError) as exc:
        chaptering.propose_from_scenes(PROJECT_ID, {"target_chapter_count": 4})
    assert exc.value.code == "SNOWFLAKE_CHAPTER_PLAN_EXISTS"
    result = chaptering.propose_from_scenes(PROJECT_ID, {"target_chapter_count": 4, "replace": True})
    assert result["created_chapter_count"] == 4 and result["replaced_chapter_count"] == 6
    assert len(_chapters(session)) == 4
    assert all(plan.chapter_plan_id for plan in _plans(session))
    # 分章现状对工作台也成立：不再要求先去 07 出章表
    status = chaptering.status(PROJECT_ID, service._scene_plans(PROJECT_ID))
    assert status["chapter_count"] == 4 and status["unassigned_scene_count"] == 0


def test_propose_route_and_empty_chapter_table_is_not_a_missing_field(client) -> None:
    response = client.post(
        "/api/v2/projects",
        json={"title": "章在场景之后", "genre": "悬疑", "target_chapter_count": 3, "target_word_count": 90000, "outline_text": "一\n二\n三"},
        headers={"X-Idempotency-Key": "k-create"},
    )
    assert response.status_code == 200, response.text
    pid = response.json()["data"]["project"]["project_id"]
    for step_key, draft in (
        ("long_synopsis", {"paragraphs": ["一", "二", "三", "四", "五"], "chapters": []}),
        ("scene_list", {"scenes": _rows(6, {2: "灾一", 4: "灾二", 5: "灾三"})}),
    ):
        patched = client.patch(f"/api/v2/projects/{pid}/snowflake-workspace/steps/{step_key}", json={"draft": draft})
        assert patched.status_code == 200, patched.text
    # 07 没出章表：不是缺失，也不改状态
    completeness = step_completeness("long_synopsis", {"paragraphs": ["一", "二", "三", "四", "五"], "chapters": []})
    assert "chapters" not in completeness["missing_fields"]
    assert "missing_chapters" not in diagnose_step_pressure("long_synopsis", {"paragraphs": ["一", "二", "三", "四", "五"], "chapters": []})["pressure_flags"]

    preview = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/chapter-plan/preview", json={})
    assert preview.status_code == 409 and preview.json()["error"]["code"] == "SNOWFLAKE_CHAPTER_PLAN_EMPTY"
    proposed = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/chapter-plan/propose",
        json={},
        headers={"X-Idempotency-Key": "k-propose"},
    )
    assert proposed.status_code == 200, proposed.text
    data = proposed.json()["data"]
    # 作品设置要 3 章，但三个灾难各自收束一章、灾二之后的场还要再起一章：铰链优先于章数 → 4 章
    assert data["created_chapter_count"] == 4 and data["totals"]["chapter_count"] == 4
    assert [chapter["spine"] for chapter in data["chapters"]] == ["灾一", "灾二", "灾三", ""]
    again = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/chapter-plan/preview", json={})
    assert again.status_code == 200 and again.json()["data"]["totals"]["chapter_count"] == 4


def test_outline_prompt_no_longer_hardcodes_a_chapter_count() -> None:
    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    outline = templates["snowflake_generate_long_synopsis"]
    assert "12-20 chapters" not in outline["task_prompt"]
    assert "`chapters: []`" in outline["task_prompt"] or "chapters: []" in outline["task_prompt"]
    assert "600-1000 Chinese characters" in outline["task_prompt"]
