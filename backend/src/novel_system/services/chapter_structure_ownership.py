"""一章的结构归谁改（阶段 Z，2026-09-20「一张章表、两扇门」）。叶子模块：只依赖 ORM 与 author_actions。

雪花整理出来的章（``SnowflakeChapterPlan.catalog_chapter_id`` 钉着的目录章）是故事序上连续的一段：
哪几场归它、它排第几、在第几幕，由构思的分章（「整理章节结构」面板 / 07 章节表）决定，确认写入时
``settle_chapter_order`` 按章表统一落位。章节编排过去照样能把这样的章拖去别的位置 / 别的卷——目录从此
和章表各说各话，下一次确认写入再悄悄改回去。和阶段 Y 的场景设计同一个答案：**一处可改**。

- ``owner = plan``：目录章来自雪花，**并且**有一行没被软删的章计划钉着它。彼此的先后与幕只在分章面板里改
  （目录 API 409）；章名两边都能改、改的是同一个名字（``snowflake_chaptering.adopt_catalog_title`` 写穿）；
  戏剧卡 / 蓝图 / 字数目标 / 删除照常归台面。
- ``owner = desk``：手建的章，以及章计划已经不在的雪花旧章（里面还留着东西、被作者留下的）——照常拖、照常改。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SnowflakeChapterPlan, SnowflakeScenePlan
from novel_system.services.author_actions import author_action
from novel_system.services.scene_design_ownership import is_snowflake_origin
from novel_system.services.snowflake_scene_order import sort_in_story_order

#: 归构思所有的章结构字段（目录 API 回包 ``details.fields`` 用的名字）
PLAN_OWNED_CHAPTER_FIELDS = ("act", "order")


def live_chapter_plans_by_catalog_id(session: Session, project_id: str | None) -> dict[str, SnowflakeChapterPlan]:
    """目录章 id → 钉着它的那一行章计划（没被软删的）。一个目录章至多被一行钉住（铸号规则保证）。"""
    if not project_id:
        return {}
    rows = session.execute(
        select(SnowflakeChapterPlan)
        .where(
            SnowflakeChapterPlan.project_id == project_id,
            SnowflakeChapterPlan.removed_at.is_(None),
            SnowflakeChapterPlan.catalog_chapter_id.is_not(None),
        )
        .order_by(SnowflakeChapterPlan.chapter_seq.asc(), SnowflakeChapterPlan.chapter_plan_id.asc())
    ).scalars()
    pinned: dict[str, SnowflakeChapterPlan] = {}
    for row in rows:
        chapter_id = str(row.catalog_chapter_id or "").strip()
        if chapter_id and chapter_id not in pinned:
            pinned[chapter_id] = row
    return pinned


def structure_owned_by_plan(
    chapter: ChapterGoal,
    pinned: dict[str, SnowflakeChapterPlan] | None,
) -> bool:
    """这一章的结构（先后、幕、成员）归构思的分章吗？"""
    if not is_snowflake_origin(chapter.writer_brief_json):
        return False
    return bool(pinned) and chapter.chapter_id in pinned


def story_scene_numbers(session: Session, project_id: str | None) -> dict[str, Any]:
    """构思里每一场是「第几场」（故事序，1 起——与分章面板、09 场景列表同一套编号），以及每个章计划装着第几到第几场。

    返回 ``{"by_scene_id": {scene_id: n}, "by_chapter_plan_id": {chapter_plan_id: {"first", "last", "count"}}}``。
    台子上的章卡靠它写出「第 6–12 场」：作者在构思里看到的编号，到了章节编排还是同一个。
    """
    if not project_id:
        return {"by_scene_id": {}, "by_chapter_plan_id": {}}
    plans = session.execute(
        select(SnowflakeScenePlan).where(
            SnowflakeScenePlan.project_id == project_id, SnowflakeScenePlan.removed_at.is_(None)
        )
    ).scalars().all()
    by_scene: dict[str, int] = {}
    by_chapter: dict[str, dict[str, int]] = {}
    for number, plan in enumerate(sort_in_story_order(session, project_id, plans), start=1):
        if plan.scene_id:
            by_scene[str(plan.scene_id)] = number
        if plan.chapter_plan_id:
            span = by_chapter.setdefault(str(plan.chapter_plan_id), {"first": number, "last": number, "count": 0})
            span["first"] = min(span["first"], number)
            span["last"] = max(span["last"], number)
            span["count"] += 1
    return {"by_scene_id": by_scene, "by_chapter_plan_id": by_chapter}


def chapter_structure_owned_by_plan_action() -> dict[str, Any]:
    return author_action(
        "章的结构在分章里改",
        "这一章是构思里分出来的：它排第几、在第几幕、装哪几场，由「整理章节结构」决定（章是场景列表上连续的一段），"
        "确认写入后目录跟着走。在目录里单独挪动只会让两边各说各话，所以这里不收。",
        target_view="snowflake",
        target_ref="snowflake_chapter_plan",
        primary_button_label="整理章节结构",
    )
