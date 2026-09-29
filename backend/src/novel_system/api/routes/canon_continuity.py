from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session, request_id_of
from novel_system.api.mutations import optional_idempotent_response
from novel_system.api.request_types import EmptyRequest
from novel_system.api.response import ok
from novel_system.services.canon_continuity import CanonContinuityService


router = APIRouter(tags=["canon-continuity"])


class ManualFactCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event_type: str = Field(min_length=1, max_length=64)
    raw_entity_ref: str = Field(min_length=1, max_length=200)
    fact_key: str = Field(min_length=1, max_length=120)
    fact_value: str = Field(min_length=1, max_length=2000)
    evidence_text: str = Field(min_length=1, max_length=2000)
    entity_type: str | None = Field(default=None, max_length=64)
    planned_timeline_event_id: str | None = Field(default=None, max_length=255)

    @field_validator(
        "event_type",
        "raw_entity_ref",
        "fact_key",
        "fact_value",
        "evidence_text",
    )
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("value must not be blank")
        return clean


class FactCandidateDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["accept", "reject"]
    selected_entity_id: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=1000)
    expected_final_scene_row_id: str | None = Field(default=None, max_length=255)


class SceneCanonVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    note: str = Field(min_length=1, max_length=1000)
    expected_final_scene_row_id: str | None = Field(default=None, max_length=255)

    @field_validator("note")
    @classmethod
    def strip_note(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("note must not be blank")
        return clean


@router.get("/api/v1/projects/{project_id}/canon/scenes/{scene_id}")
def scene_canon_status(
    project_id: str,
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    return ok(
        CanonContinuityService(session).scene_status(project_id, scene_id),
        req_id=request_id_of(request),
    )


@router.post("/api/v1/projects/{project_id}/canon/scenes/{scene_id}/candidates")
def create_manual_fact_candidate(
    project_id: str,
    scene_id: str,
    payload: ManualFactCandidateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/canon/scenes/{scene_id}/candidates",
        payload={"project_id": project_id, "scene_id": scene_id, **body},
        action=lambda: CanonContinuityService(session).create_manual_candidate(
            project_id,
            scene_id,
            **body,
        ),
    )


@router.post("/api/v1/projects/{project_id}/canon/scenes/{scene_id}/extract")
def extract_scene_fact_candidates(
    project_id: str,
    scene_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json") if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/canon/scenes/{scene_id}/extract",
        payload={"project_id": project_id, "scene_id": scene_id, "body": body},
        action=lambda: CanonContinuityService(session).extract_scene_candidates(
            project_id,
            scene_id,
        ),
    )


@router.post("/api/v1/projects/{project_id}/canon/candidates/{candidate_id}/decision")
def decide_fact_candidate(
    project_id: str,
    candidate_id: str,
    payload: FactCandidateDecisionRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    actor_ref = actor_ref_of(request)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/canon/candidates/{candidate_id}/decision",
        payload={"project_id": project_id, "candidate_id": candidate_id, **body},
        action=lambda: CanonContinuityService(session).decide_candidate(
            project_id,
            candidate_id,
            actor_ref=actor_ref,
            **body,
        ),
    )


@router.post("/api/v1/projects/{project_id}/canon/scenes/{scene_id}/verify")
def verify_scene_canon(
    project_id: str,
    scene_id: str,
    payload: SceneCanonVerificationRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    actor_ref = actor_ref_of(request)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/projects/{project_id}/canon/scenes/{scene_id}/verify",
        payload={"project_id": project_id, "scene_id": scene_id, **body},
        action=lambda: CanonContinuityService(session).verify_scene_complete(
            project_id,
            scene_id,
            actor_ref=actor_ref,
            **body,
        ),
    )
