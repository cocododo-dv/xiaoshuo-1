"""Shared snowflake version / latest-row lookups (a leaf: models + SQLAlchemy only).

The step-run table (``SnowflakeStepRun``, v2 workspace) and the legacy artifact table
(``SnowflakeArtifact``, v1 planner) share the same version rule, so the helpers take the model.
Story order and triage verdicts have their own leaves: ``snowflake_scene_order`` and
``snowflake_triage``.
"""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import OutlinePlan, SnowflakeArtifact, SnowflakeStepRun

StepRow = TypeVar("StepRow", SnowflakeStepRun, SnowflakeArtifact)


def latest_by_step(session: Session, model: type[StepRow], project_id: str) -> dict[str, StepRow]:
    """Per ``step_key`` the highest-version row that is not ``superseded`` (ties: later ``created_at``)."""
    rows = session.execute(
        select(model)
        .where(model.project_id == project_id)
        .order_by(model.version.asc(), model.created_at.asc())
    ).scalars().all()
    latest: dict[str, StepRow] = {}
    for row in rows:
        if row.status == "superseded":
            continue
        latest[row.step_key] = row
    return latest


def next_step_version(session: Session, model: type[StepRow], project_id: str, step_key: str) -> int:
    latest = session.execute(
        select(model.version)
        .where(model.project_id == project_id, model.step_key == step_key)
        .order_by(model.version.desc())
    ).scalar()
    return int(latest or 0) + 1


def next_outline_plan_version(session: Session, project_id: str) -> int:
    latest = session.execute(
        select(OutlinePlan.version)
        .where(OutlinePlan.project_id == project_id)
        .order_by(OutlinePlan.version.desc())
    ).scalar()
    return int(latest or 0) + 1


def latest_outline_plan(session: Session, project_id: str) -> OutlinePlan | None:
    return session.execute(
        select(OutlinePlan)
        .where(OutlinePlan.project_id == project_id)
        .order_by(OutlinePlan.version.desc(), OutlinePlan.created_at.desc())
    ).scalars().first()


def step_gate_satisfied(run: SnowflakeStepRun | None) -> bool:
    """A step counts as done for gates: approved / skipped, or stale with the staleness accepted."""
    if run is None:
        return False
    if run.status in {"approved", "skipped"}:
        return True
    return run.status == "stale" and bool(run.stale_accepted_at)
