from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import actor_ref_of, get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.canon_continuity import (
    FactCandidateDecisionRequest,
    ManualFactCandidateRequest,
    SceneCanonVerificationRequest,
)
from novel_system.api.response import respond
from novel_system.services.canon_continuity import CanonContinuityService


router = APIRouter(tags=["canon-continuity"])


@router.get("/api/v1/projects/{project_id}/canon/scenes/{scene_id}")
def scene_canon_status(
    project_id: str,
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    return respond(request, CanonContinuityService(session).scene_status(project_id, scene_id))


@router.post("/api/v1/projects/{project_id}/canon/scenes/{scene_id}/candidates")
def create_manual_fact_candidate(
    project_id: str,
    scene_id: str,
    payload: ManualFactCandidateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    return mutate(
        request,
        session,
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
    return mutate(
        request,
        session,
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
    return mutate(
        request,
        session,
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
    return mutate(
        request,
        session,
        payload={"project_id": project_id, "scene_id": scene_id, **body},
        action=lambda: CanonContinuityService(session).verify_scene_complete(
            project_id,
            scene_id,
            actor_ref=actor_ref,
            **body,
        ),
    )
