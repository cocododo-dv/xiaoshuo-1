"""章表的惰性派生与「分章现状」——两处用同一条派生链（B07-06：``status`` 以前把链逐字抄了一遍）。

章表本来由分章面板的确认（``save``）落库；还没有章表行、但事实上已经分过章的作品，按优先级从已有事实派生：

1. 目录（物化过的章里装着构思的场）——让「打开分章面板」看到的是现状，而不是一份抹平现状的空白重排；
2. 场景行自带的章归属（至少两个不同的章号——前端一路给所有场盖的 ``…_CH01`` 是退化默认值，不是分章决定）。

2026-09-30（B07-22）：删掉了第三级「从 07 长篇大纲的章表派生」与 07 散文里的章行解析——07 的章表是章计划行的
镜像，不再是它的来源；库里每一部确认过 07 的作品都已经有章计划行（性能库只读核对）。场景行章号那一级仍留着：
测试夹具（``tests/snowflake_skeleton.py`` 的骨架）按它分章，删它要先改夹具。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard, SnowflakeChapterPlan, SnowflakeScenePlan
from novel_system.services.snowflake_chapter_table import (
    coerce_act,
    create_chapter_plan,
    is_pinnable_chapter_id,
    live_chapter_plans,
)
from novel_system.services.snowflake_chaptering.spine import scene_spine
from novel_system.services.snowflake_scene_order import live_scene_plans_in_story_order


def derived_chapter_rows(
    session: Session,
    project_id: str,
    scenes: list[SnowflakeScenePlan] | None = None,
) -> list[dict[str, Any]]:
    """还没有章表行时，从已有事实派生的章（目录 → 场景行章号；都没有 → 空表）。只读。"""
    scene_plans = scenes if scenes is not None else live_scene_plans_in_story_order(session, project_id)
    return _derive_from_catalog(session, project_id, scene_plans) or _derive_from_scene_chapter_ids(scene_plans)


def ensure_chapter_plans(session: Session, project_id: str) -> list[SnowflakeChapterPlan]:
    """保证项目有一份章表可用（幂等）：已经有就用（作者的分章成果，绝不覆盖），否则按 :func:`derived_chapter_rows` 补。

    放在服务层而不是迁移里：已物化项目要保持现状、场景行章号要认真假，这些都是有真实边界情况的业务逻辑，
    需要可单测、可重算、出错可重试。
    """
    existing = live_chapter_plans(session, project_id)
    if existing:
        return existing
    scenes = live_scene_plans_in_story_order(session, project_id)
    derived = derived_chapter_rows(session, project_id, scenes)
    if not derived:
        return []
    created = [create_chapter_plan(session, project_id, item) for item in derived]
    for item, row in zip(derived, created):
        # 从目录 / 场景行的章戳反推出来的章：那个章号就是它在目录里的身份，钉住
        source = str(item.get("source_chapter_id") or "").strip()
        if source and is_pinnable_chapter_id(session, project_id, source):
            row.catalog_chapter_id = source
    # 来源自带归属：归属是既成事实，不是待决策项，建完章顺手绑上，作者不必为「系统已经知道的事」再点一次确认。
    by_source_chapter_id = {
        str(item.get("source_chapter_id") or ""): row for item, row in zip(derived, created) if item.get("source_chapter_id")
    }
    for plan in scenes:
        if plan.chapter_plan_id:
            continue
        target = by_source_chapter_id.get(plan.chapter_id or "")
        if target is not None:
            plan.chapter_plan_id = target.chapter_plan_id
    session.flush()
    return live_chapter_plans(session, project_id)


def chapter_plan_status(session: Session, project_id: str, scene_plans: list[SnowflakeScenePlan]) -> dict[str, Any]:
    """分章现状（**只读**，不建行、不绑定）。

    必须和物化看到的是同一个真相：物化会先 ``ensure_chapter_plans``，把目录 / 场景行上已经存在的章归属派生并
    绑定，所以这里也要把「还没落库、但一物化就会自动绑上」的那部分算作已分章——否则工作台报 blocked、实际却能
    物化，作者对着一个假闸门发懵。派生链与 :func:`ensure_chapter_plans` 是同一个函数。
    """
    chapters = live_chapter_plans(session, project_id)
    if chapters:
        valid = {chapter.chapter_plan_id for chapter in chapters}
        unassigned = [plan for plan in scene_plans if not plan.chapter_plan_id or plan.chapter_plan_id not in valid]
        chapter_count = len(chapters)
    else:
        derived = derived_chapter_rows(session, project_id, scene_plans)
        bindable = {str(item.get("source_chapter_id") or "") for item in derived if item.get("source_chapter_id")}
        unassigned = [plan for plan in scene_plans if str(plan.chapter_id or "") not in bindable]
        chapter_count = len(derived)
    return {
        "chapter_count": chapter_count,
        "assigned_scene_count": len(scene_plans) - len(unassigned),
        "unassigned_scene_count": len(unassigned),
        "unassigned_scenes": [
            {
                "scene_plan_id": plan.scene_plan_id,
                "scene_id": plan.scene_id,
                "title": plan.title or plan.summary or plan.scene_id,
            }
            for plan in unassigned[:20]
        ],
        "chaptered": bool(chapter_count) and not unassigned,
    }


def _derive_from_catalog(session: Session, project_id: str, scenes: list[SnowflakeScenePlan]) -> list[dict[str, Any]]:
    rows = list(
        session.execute(
            select(ChapterGoal).where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
        ).scalars()
    )
    if not rows:
        return []
    # 只认**雪花物化出来的章**——章里有场景计划对应的场景卡。作者在章节编排里手建的章
    # （比如新建作品后随手点出来的「第 1 章 / 开场」）不是构思侧的分章决定：把它当章表，
    # 面板就会显示「一章 + 全书的场都未分配」，而真正要整理的章一章都没有。
    plan_scene_ids = {plan.scene_id for plan in scenes}
    materialized_chapter_ids = {
        card.chapter_id
        for card in session.execute(
            select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
        ).scalars()
        if card.scene_id in plan_scene_ids
    }
    rows = [chapter for chapter in rows if chapter.chapter_id in materialized_chapter_ids]
    if not rows:
        return []
    rows.sort(key=lambda chapter: (chapter.display_order is None, chapter.display_order or 0, chapter.chapter_id))
    derived: list[dict[str, Any]] = []
    for index, chapter in enumerate(rows, start=1):
        narrative = dict(chapter.narrative_json or {})
        brief = dict(chapter.writer_brief_json or {})
        goal = str(chapter.chapter_goal or "").strip()
        title = (
            str(narrative.get("title") or "").strip()
            or str(brief.get("chapter_title") or "").strip()
            or (goal.splitlines()[0][:24] if goal else "")
            or chapter.chapter_id
        )
        derived.append(
            {
                "row_uid": "",
                "chapter_seq": index,
                "act": coerce_act(narrative.get("act"), 1),
                "title": title,
                "summary": goal,
                "spine": str(narrative.get("spine") or "").strip(),
                "chapter_goal": goal,
                # 已物化项目里场景行的 chapter_id 就等于 ChapterGoal 的主键，
                # 所以归属可以直接按它对上，不需要作者重新指派。
                "source_chapter_id": chapter.chapter_id,
            }
        )
    return derived


def _derive_from_scene_chapter_ids(scenes: list[SnowflakeScenePlan]) -> list[dict[str, Any]]:
    """场景行自带的章归属（旧规划器骨架、以及任何回填了 chapter_id 的 LLM 输出）。

    **只在出现两个及以上不同章号时才认**：前端 ``canonFromFE("scenes")`` 不发 chapter_id，服务端于是给所有场同一个
    ``{project_id}_CH01``——那是退化默认值，不是作者的分章决定。把它当成「已分章」正是当年要修的「全书落进一章」。
    """
    ordered_chapter_ids: list[str] = []
    for plan in scenes:
        chapter_id = str(plan.chapter_id or "").strip()
        if chapter_id and chapter_id not in ordered_chapter_ids:
            ordered_chapter_ids.append(chapter_id)
    if len(ordered_chapter_ids) < 2:
        return []
    first_by_chapter: dict[str, SnowflakeScenePlan] = {}
    for plan in scenes:
        first_by_chapter.setdefault(str(plan.chapter_id or "").strip(), plan)
    derived: list[dict[str, Any]] = []
    for index, chapter_id in enumerate(ordered_chapter_ids, start=1):
        sample = first_by_chapter[chapter_id]
        title = str(sample.chapter_title or "").strip()
        derived.append(
            {
                "row_uid": "",
                "chapter_seq": index,
                "act": 1,
                "title": title if title and title != chapter_id else f"第 {index} 章",
                "summary": str(sample.chapter_goal or "").strip(),
                "spine": scene_spine(sample),
                "chapter_goal": str(sample.chapter_goal or "").strip(),
                "source_chapter_id": chapter_id,
            }
        )
    return derived
