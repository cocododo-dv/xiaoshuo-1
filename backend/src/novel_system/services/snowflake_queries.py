"""Shared snowflake version / latest-row lookups (a leaf: models + SQLAlchemy only).

The helpers take the step-run model (``SnowflakeStepRun``) explicitly: the retired v1 planner's
``SnowflakeArtifact`` table shared the same version rule until 2026-09-30 (R9); its table is kept
for a later migration, but nothing reads or writes it any more. Story order and triage verdicts have
their own leaves: ``snowflake_scene_order`` and ``snowflake_triage``.
"""

from __future__ import annotations

from typing import TypeVar

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import OutlinePlan, SnowflakeStepRun

StepRow = TypeVar("StepRow", bound=SnowflakeStepRun)


def latest_by_step(session: Session, model: type[StepRow], project_id: str) -> dict[str, StepRow]:
    """Per ``step_key`` the highest-version row that is not ``superseded`` (ties: later ``created_at``).

    One statement that loads only the winning rows (B06-04): it used to load every version of every
    step — all ``draft_json`` blobs of the history — to keep the last one per step.
    """
    ranked = (
        select(
            model.step_run_id.label("step_run_id"),
            func.row_number()
            .over(partition_by=model.step_key, order_by=(model.version.desc(), model.created_at.desc()))
            .label("rank"),
        )
        .where(model.project_id == project_id, model.status != "superseded")
        .subquery()
    )
    rows = session.execute(
        select(model)
        .where(model.step_run_id.in_(select(ranked.c.step_run_id).where(ranked.c.rank == 1)))
        .order_by(model.version.asc(), model.created_at.asc())
    ).scalars().all()
    return {row.step_key: row for row in rows}


def next_step_version(session: Session, model: type[StepRow], project_id: str, step_key: str) -> int:
    latest = session.execute(
        select(model.version)
        .where(model.project_id == project_id, model.step_key == step_key)
        .order_by(model.version.desc())
    ).scalar()
    return int(latest or 0) + 1


def latest_step_run(
    session: Session,
    project_id: str,
    step_key: str,
    *,
    statuses: tuple[str, ...] | None = None,
) -> SnowflakeStepRun | None:
    """The newest version of one step (highest ``version``, ties: later ``created_at``).

    ``statuses`` limits the candidates (e.g. ``("approved", "stale")`` = the confirmed design); by default
    anything that is not ``superseded`` counts — the same rule as :func:`latest_by_step`, for one step.
    """
    query = select(SnowflakeStepRun).where(
        SnowflakeStepRun.project_id == project_id, SnowflakeStepRun.step_key == step_key
    )
    query = (
        query.where(SnowflakeStepRun.status.in_(statuses))
        if statuses
        else query.where(SnowflakeStepRun.status != "superseded")
    )
    return session.execute(
        query.order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.created_at.desc())
    ).scalars().first()


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
