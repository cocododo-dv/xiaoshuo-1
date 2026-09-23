from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import optional_idempotent_response
from novel_system.api.response import ok
from novel_system.services.reference_safety import ReferenceSafetyService

router = APIRouter(tags=["reference-safety"])


class SourceSafetyScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(max_length=2_000_000)
    source_profile_ids: list[
        Annotated[str, Field(min_length=1, max_length=255)]
    ] | None = Field(default=None, max_length=256)
    object_ref: str | None = Field(default=None, max_length=512)


@router.get("/api/v1/reference-safety/overview")
def get_reference_safety_overview(request: Request, session: Session = Depends(get_session)):
    return ok(
        ReferenceSafetyService(session).overview(),
        req_id=getattr(request.state, "request_id", None),
    )


@router.post("/api/v1/source-safety/scan")
def scan_source_safety(payload: SourceSafetyScanRequest, request: Request, session: Session = Depends(get_session)):
    body = payload.model_dump(mode="json")
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/source-safety/scan",
        payload=body,
        action=lambda: ReferenceSafetyService(session).scan_text(**body),
    )
