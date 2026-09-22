"""规划产物随设计 / 绑定变化作废（2026-09-22 结构跟随参考书）。

场景蓝图（``SceneBlueprint``）、人物压力蓝图与章故事架构（``GenerationPlanningArtifact``）都是
「有就复用」的：``SceneBlueprintService.ensure_for_scene`` 与 ``near_final.ensure_scene_planning``
只找最新一条 accepted / active 的行，从不看它是按哪一版设计、哪一本参考书做出来的。于是雪花设计
重新确认、换一本参考书或删掉绑定之后，下一次运行仍拿着旧蓝图起草——一场的结尾动作、意象锚、信息
释放顺序都还是旧参考 / 旧设计的。

本模块是叶子（只依赖 ORM）：把受影响场景 / 章的规划产物置为 ``superseded``，让下一次运行按当前
设计与绑定重新规划。调用点：``ProjectRuntimeInvalidationService``（设计变了）、
``MaterializationService.apply_profile`` 与绑定删除路由（参考变了）。作废只是状态翻转，不删行。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    GenerationPlanningArtifact,
    SceneBlueprint,
    SceneCard,
    StoryCharacter,
)

SUPERSEDED_STATUS = "superseded"
_LIVE_BLUEPRINT_STATUSES: tuple[str, ...] = ("draft", "accepted")


def supersede_scene_planning_artifacts(
    session: Session,
    *,
    scene_ids: Iterable[str],
    chapter_ids: Iterable[str] = (),
    reason: str = "",
) -> dict[str, Any]:
    """把这些场的蓝图与人物压力蓝图、这些章的章架构置为 superseded；返回各类计数。"""
    scenes = sorted({str(item) for item in scene_ids if str(item or "").strip()})
    chapters = sorted({str(item) for item in chapter_ids if str(item or "").strip()})
    counts: dict[str, Any] = {
        "reason": reason,
        "scene_blueprints": 0,
        "character_pressure": 0,
        "chapter_architecture": 0,
    }
    if scenes:
        for row in session.execute(
            select(SceneBlueprint).where(
                SceneBlueprint.scene_id.in_(scenes),
                SceneBlueprint.status.in_(_LIVE_BLUEPRINT_STATUSES),
            )
        ).scalars().all():
            row.status = SUPERSEDED_STATUS
            counts["scene_blueprints"] += 1
        for row in session.execute(
            select(GenerationPlanningArtifact).where(
                GenerationPlanningArtifact.object_type == "scene",
                GenerationPlanningArtifact.object_id.in_(scenes),
                GenerationPlanningArtifact.status == "active",
            )
        ).scalars().all():
            row.status = SUPERSEDED_STATUS
            counts["character_pressure"] += 1
    if chapters:
        for row in session.execute(
            select(GenerationPlanningArtifact).where(
                GenerationPlanningArtifact.object_type == "chapter",
                GenerationPlanningArtifact.object_id.in_(chapters),
                GenerationPlanningArtifact.status == "active",
            )
        ).scalars().all():
            row.status = SUPERSEDED_STATUS
            counts["chapter_architecture"] += 1
    if any(counts[key] for key in ("scene_blueprints", "character_pressure", "chapter_architecture")):
        session.flush()
    return counts


def supersede_for_binding_scope(
    session: Session,
    *,
    scope: str,
    scope_ref_id: str | None,
    reason: str = "style_binding_changed",
) -> dict[str, Any]:
    """参考绑定变了（应用 / 重应用 / 删除）：作废它作用范围内每一场的规划产物。

    project / character 作用域 → 该作品的全部活跃场与它们的章；scene 作用域 → 这一场与它的章
    （章架构是在这一场的运行里按这一场的契约做的）。找不到目标 → 什么都不做。
    """
    scope_value = str(scope or "").strip().lower()
    ref = str(scope_ref_id or "").strip()
    empty = {"reason": reason, "scene_blueprints": 0, "character_pressure": 0, "chapter_architecture": 0}
    if not ref:
        return empty
    if scope_value == "scene":
        scene = session.get(SceneCard, ref)
        if scene is None:
            return empty
        return supersede_scene_planning_artifacts(
            session, scene_ids=[scene.scene_id], chapter_ids=[scene.chapter_id], reason=reason
        )
    if scope_value == "character":
        character = session.get(StoryCharacter, ref)
        project_id = str(getattr(character, "project_id", "") or "") if character is not None else ""
    else:
        project_id = ref
    if not project_id:
        return empty
    rows = session.execute(
        select(SceneCard.scene_id, SceneCard.chapter_id).where(
            SceneCard.project_id == project_id, SceneCard.trashed_flag == 0
        )
    ).all()
    return supersede_scene_planning_artifacts(
        session,
        scene_ids=[scene_id for scene_id, _chapter in rows],
        chapter_ids=[chapter_id for _scene, chapter_id in rows if chapter_id],
        reason=reason,
    )


__all__ = [
    "SUPERSEDED_STATUS",
    "supersede_for_binding_scope",
    "supersede_scene_planning_artifacts",
]
