"""规划产物随设计 / 绑定变化作废（2026-09-22 结构跟随参考书）。

场景蓝图（``SceneBlueprint``）、人物压力蓝图与章故事架构（``GenerationPlanningArtifact``）都是
「有就复用」的：``SceneBlueprintService.ensure_for_scene`` 与 ``near_final.ensure_scene_planning``
只找最新一条 accepted / active 的行，从不看它是按哪一版设计、哪一本参考书做出来的。于是雪花设计
重新确认、换一本参考书或删掉绑定之后，下一次运行仍拿着旧蓝图起草——一场的结尾动作、意象锚、信息
释放顺序都还是旧参考 / 旧设计的。

本模块是叶子（只依赖 ORM 与蓝图叶子 ``chapter_architecture``）：把受影响场景 / 章的规划产物置为 ``superseded``，让下一次运行按当前
设计与绑定重新规划。调用点：``ProjectRuntimeInvalidationService``（设计变了）、
``binding_apply``（用于作品 / 改绑定配置 / 解除）与删书（参考变了）。作废只是状态翻转，不删行。

作者在章节编排里亲手写的章蓝图（``llm_call_id`` 为空的那一行）不作废（B07-03）：它是作者的决定，不是按旧设计
算出来的缓存——作废了，章节编排里它就没了，下一次场景运行还会拿一份 AI 写的蓝图顶替它。它原样留着，记一条
「设计在它之后改过」（:data:`AUTHOR_ARCHITECTURE_KEPT_EVENT`），读蓝图时据此提示作者看一眼（:func:`design_changed_since`）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    GenerationPlanningArtifact,
    OperationLog,
    SceneBlueprint,
    SceneCard,
    StoryCharacter,
    utcnow,
)
from novel_system.services.chapter_architecture import is_author_architecture

SUPERSEDED_STATUS = "superseded"
_LIVE_BLUEPRINT_STATUSES: tuple[str, ...] = ("draft", "accepted")
#: 作者写的章蓝图因设计 / 绑定变化本该作废、按 B07-03 留下时记的操作日志（读蓝图时据此给「设计改过」的提示）
AUTHOR_ARCHITECTURE_KEPT_EVENT = "chapter_architecture_kept_after_design_change"
_DESIGN_CHANGED_MESSAGE = "这份蓝图写好之后，构思或参考书又改过：蓝图照旧保留、照旧用于起草；需要时改写或重新生成。"


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
        "author_architecture_kept": 0,
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
            if is_author_architecture(row):
                # 作者亲手写的蓝图（B07-03）：留着，记一条「设计在它之后改过」
                session.add(
                    OperationLog(
                        event_type=AUTHOR_ARCHITECTURE_KEPT_EVENT,
                        object_type="generation_planning_artifact",
                        object_ref=row.row_id,
                        payload_json={"chapter_id": row.object_id, "reason": reason, "kept_at": utcnow()},
                    )
                )
                counts["author_architecture_kept"] += 1
                continue
            row.status = SUPERSEDED_STATUS
            counts["chapter_architecture"] += 1
    if any(
        counts[key] for key in ("scene_blueprints", "character_pressure", "chapter_architecture", "author_architecture_kept")
    ):
        session.flush()
    return counts


def design_changed_since(session: Session, artifact: GenerationPlanningArtifact | None) -> dict[str, Any] | None:
    """这份（作者写的）章蓝图留下来之后，设计 / 绑定又变过吗？变过 → 最近一次的 ``{reason, at, message}``。"""
    if not is_author_architecture(artifact):
        return None
    event = session.execute(
        select(OperationLog)
        .where(
            OperationLog.event_type == AUTHOR_ARCHITECTURE_KEPT_EVENT,
            OperationLog.object_ref == artifact.row_id,
        )
        .order_by(OperationLog.operation_id.desc())
    ).scalars().first()
    if event is None:
        return None
    payload = dict(event.payload_json or {})
    return {
        "reason": str(payload.get("reason") or ""),
        "at": str(payload.get("kept_at") or event.created_at or ""),
        "message": _DESIGN_CHANGED_MESSAGE,
    }


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
    empty = {
        "reason": reason,
        "scene_blueprints": 0,
        "character_pressure": 0,
        "chapter_architecture": 0,
        "author_architecture_kept": 0,
    }
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
    "AUTHOR_ARCHITECTURE_KEPT_EVENT",
    "SUPERSEDED_STATUS",
    "design_changed_since",
    "supersede_for_binding_scope",
    "supersede_scene_planning_artifacts",
]
