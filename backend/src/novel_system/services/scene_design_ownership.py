"""一场的设计归谁改（阶段 Y，2026-09-20「设计只有一处可改」）。叶子模块：只依赖 ORM 与 author_actions。

雪花物化 / 回流出来的场景卡，设计（形态、两组三拍、POV、离场变化、钩子、彼此的先后）来自构思第 9 / 10 步。
阶段 X 的做法是台子上照样能改，改了只在卡上打一个记号（``desk_edited_at``），构思侧不知道——两边从此各说各话，
下一次回流要么静默盖掉台面的改动，要么被记号挡住、等作者去看差异。

把台面的改动**写回**构思，要在 09 / 10 的步骤稿和前端雪花缓存之间再开一条服务端写通道——那条缓存的合并规则
是「本机为准」，正是出过「整步抹空」事故的地方；代价和风险都不值。所以反过来：

- 构思里那一行还在的雪花场景卡（``owner = plan``）：设计只在构思里改，确认后自动同步到目录；
  目录 API 拒绝改动（409 + 直达那一场的 author_action），章节规划 AI 不往里填，台子上只读；
- 手加的场、以及构思里那一行已经删掉的雪花场（卡因为写过字被作者留下）：归台面（``owner = desk``），照常可改；
- 题名（``title`` / ``seeded_title`` 有自己的跟随规则）、状态、字数、笔记不是设计，两种卡在台子上都照常改。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard, SnowflakeScenePlan
from novel_system.services.author_actions import author_action

#: ``writer_brief_json["source"]`` 为这两个值的章 / 场来自雪花物化或回流
SNOWFLAKE_SOURCES = frozenset({"snowflake_method", "snowflake_resync"})
#: 归构思所有的设计字段（目录 API 回包 ``details.fields`` 用的名字）
PLAN_OWNED_SCENE_FIELDS = (
    "kind", "goal", "conflict", "setback", "reaction", "dilemma", "decision", "pov", "exit_change", "hook",
)


def is_snowflake_origin(brief: dict[str, Any] | None) -> bool:
    return str((brief or {}).get("source") or "").strip() in SNOWFLAKE_SOURCES


def live_plan_scene_ids(session: Session, project_id: str | None) -> set[str]:
    """构思里还在的场景行所对应的场景 id。"""
    if not project_id:
        return set()
    return {
        str(scene_id)
        for scene_id in session.execute(
            select(SnowflakeScenePlan.scene_id).where(
                SnowflakeScenePlan.project_id == project_id, SnowflakeScenePlan.removed_at.is_(None)
            )
        ).scalars()
        if scene_id
    }


def design_owned_by_plan(session: Session, scene: SceneCard, *, plan_scene_ids: set[str] | None = None) -> bool:
    """这张场景卡的设计归构思侧所有吗？是 = 雪花物化 / 回流出来的卡，**并且**构思里那一行还在。"""
    if not is_snowflake_origin(scene.writer_brief_json):
        return False
    if plan_scene_ids is not None:
        return scene.scene_id in plan_scene_ids
    return (
        session.execute(
            select(SnowflakeScenePlan.scene_plan_id)
            .where(SnowflakeScenePlan.scene_id == scene.scene_id, SnowflakeScenePlan.removed_at.is_(None))
            .limit(1)
        ).first()
        is not None
    )


def plan_owned_scene_ids(session: Session, project_id: str | None, scenes: list[SceneCard]) -> set[str]:
    live = live_plan_scene_ids(session, project_id)
    return {scene.scene_id for scene in scenes if scene.scene_id in live and is_snowflake_origin(scene.writer_brief_json)}


def design_owned_by_plan_action(scene_id: str) -> dict[str, Any]:
    return author_action(
        "这一场的设计在构思里改",
        "这一场是雪花整理出来的：形态、三拍、POV、离场变化和钩子由构思第 10 步「场景规划」决定，确认后自动同步到这里。"
        "在台子上改只会让两边各说各话，所以这里不收。",
        target_view="snowflake",
        target_ref=f"snowflake_step:scene_details:{scene_id}",
        primary_button_label="在构思里改",
    )


def scene_order_owned_by_plan_action() -> dict[str, Any]:
    return author_action(
        "场序在构思里改",
        "雪花整理出来的场，先后顺序由构思第 9 步「场景列表」的行序决定，确认后自动同步到目录。",
        target_view="snowflake",
        target_ref="snowflake_step:scene_list",
        primary_button_label="去场景列表",
    )
