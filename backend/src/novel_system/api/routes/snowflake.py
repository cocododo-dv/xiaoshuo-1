from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session, request_id_of
from novel_system.api.mutations import idempotent_response, optional_idempotent_response
from novel_system.api.request_types import EmptyRequest
from novel_system.api.response import ok
from novel_system.api.snowflake_requests import (
    LegacySnowflakeArtifactUpdateRequest,
    LegacySnowflakeStepGenerateRequest,
)
from novel_system.services.snowflake_planner import SnowflakePlannerService

router = APIRouter(tags=["snowflake"])


@router.get("/api/v1/projects/{project_id}/snowflake")
def snowflake_state(project_id: str, request: Request, session: Session = Depends(get_session)):
    return ok(SnowflakePlannerService(session).state(project_id), req_id=request_id_of(request))


@router.post("/api/v1/projects/{project_id}/snowflake/steps/{step_key}/generate")
def generate_snowflake_step(
    project_id: str,
    step_key: str,
    request: Request,
    payload: LegacySnowflakeStepGenerateRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True) if payload else {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/snowflake/steps/{step_key}/generate",
        payload={"project_id": project_id, "step_key": step_key, **body},
        action=lambda: SnowflakePlannerService(session).generate(project_id, step_key, body),
    )


@router.patch("/api/v1/projects/{project_id}/snowflake/artifacts/{artifact_id}")
def update_snowflake_artifact(
    project_id: str,
    artifact_id: str,
    payload: LegacySnowflakeArtifactUpdateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v1/projects/{project_id}/snowflake/artifacts/{artifact_id}",
        payload={"project_id": project_id, "artifact_id": artifact_id, "body": body},
        action=lambda: SnowflakePlannerService(session).update_artifact(project_id, artifact_id, body),
    )


@router.post("/api/v1/projects/{project_id}/snowflake/artifacts/{artifact_id}/approve")
def approve_snowflake_artifact(
    project_id: str,
    artifact_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload else {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/snowflake/artifacts/{artifact_id}/approve",
        payload={"project_id": project_id, "artifact_id": artifact_id, **body},
        action=lambda: SnowflakePlannerService(session).approve(project_id, artifact_id),
    )


@router.post("/api/v1/projects/{project_id}/snowflake/materialize-outline-plan")
def materialize_snowflake_outline_plan(
    project_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload else {}
    return idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/snowflake/materialize-outline-plan",
        payload={"project_id": project_id, **body},
        action=lambda: SnowflakePlannerService(session).materialize_outline_plan(project_id),
    )
