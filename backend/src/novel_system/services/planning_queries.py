"""Shared lookups for scene / chapter planning rows and scene final texts (a leaf: models + SQLAlchemy only).

``scene_archive_effects`` reads the latest blueprint without the ``row_id`` tiebreak — a different
rule, kept there on purpose.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import FinalScene, GenerationPlanningArtifact, SceneBlueprint, SceneRunState

# 仍算「成稿正文」的 FinalScene 状态（``superseded`` 是被成稿中心换下来的旧版）。
FINAL_TEXT_STATUSES: tuple[str, ...] = ("approved", "near_final_ready", "archived")

# 作者在 AI 起草台保存的「我的版本」场景蓝图：没有模型调用、来源记成作者（``services/scene_blueprint_view.py``）。
# 起草优先用它，设计 / 绑定再变也不作废（只提示「设计改过」）。只看 ``llm_call_id`` 不够：测试与旧数据里有
# 不带调用 id 的 AI 蓝图。
AUTHOR_BLUEPRINT_SOURCE = "author"


def is_author_scene_blueprint(row: SceneBlueprint | None) -> bool:
    """这一行是作者保存的「我的版本」吗？"""
    return row is not None and row.llm_call_id is None and row.source_bundle_id == AUTHOR_BLUEPRINT_SOURCE


def latest_scene_blueprint(session: Session, scene_id: str) -> SceneBlueprint | None:
    """The newest ``accepted`` / ``draft`` blueprint of a scene (``created_at`` then ``row_id``)."""
    return session.execute(
        select(SceneBlueprint)
        .where(SceneBlueprint.scene_id == scene_id, SceneBlueprint.status.in_(("accepted", "draft")))
        .order_by(SceneBlueprint.created_at.desc(), SceneBlueprint.row_id.desc())
    ).scalars().first()


def latest_active_planning_artifact(
    session: Session,
    *,
    artifact_type: str,
    object_type: str,
    object_id: str,
) -> GenerationPlanningArtifact | None:
    """The newest ``active`` planning artifact of that type for the object."""
    return session.execute(
        select(GenerationPlanningArtifact)
        .where(
            GenerationPlanningArtifact.artifact_type == artifact_type,
            GenerationPlanningArtifact.object_type == object_type,
            GenerationPlanningArtifact.object_id == object_id,
            GenerationPlanningArtifact.status == "active",
        )
        .order_by(GenerationPlanningArtifact.created_at.desc(), GenerationPlanningArtifact.row_id.desc())
    ).scalars().first()


def current_final_scenes(
    session: Session,
    scene_ids: Iterable[str],
    *,
    statuses: tuple[str, ...] = FINAL_TEXT_STATUSES,
) -> dict[str, FinalScene]:
    """每场的当前正文行：``SceneRunState.current_final_scene_row_id`` 指着的那一行；指针为空（重跑中、
    设计改动后作废）或指着的行不在 ``statuses`` 里时，退到这一场最新的一行（``created_at`` 再 ``row_id``）。

    重跑一场会再插一行 FinalScene，旧行照旧 ``archived``——按状态挑行会把一场的几版一起读进来（B03-04）。
    没有成稿的场不在结果里。两条查询，不按场逐条查。
    """
    ids = list(dict.fromkeys(scene_id for scene_id in scene_ids if scene_id))
    if not ids:
        return {}
    pointers = {
        scene_id: row_id
        for scene_id, row_id in session.execute(
            select(SceneRunState.scene_id, SceneRunState.current_final_scene_row_id).where(
                SceneRunState.scene_id.in_(ids)
            )
        ).all()
        if row_id
    }
    rows = session.execute(
        select(FinalScene)
        .where(FinalScene.scene_id.in_(ids), FinalScene.status.in_(statuses))
        .order_by(FinalScene.created_at.asc(), FinalScene.row_id.asc())
    ).scalars().all()
    current: dict[str, FinalScene] = {}
    for row in rows:
        current[row.scene_id] = row
    for row in rows:
        if pointers.get(row.scene_id) == row.row_id:
            current[row.scene_id] = row
    return current
