"""Shared lookups for scene / chapter planning rows (a leaf: models + SQLAlchemy only).

``scene_archive_effects`` reads the latest blueprint without the ``row_id`` tiebreak — a different
rule, kept there on purpose.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import GenerationPlanningArtifact, SceneBlueprint


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
