from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import optional_idempotent_response
from novel_system.api.request_types import StrictRequestModel
from novel_system.api.response import ok
from novel_system.services.literary_quality import LiteraryQualityService
from novel_system.services.scene_diagnosis import SceneDiagnosisService


def _quality_service(session: Session) -> LiteraryQualityService:
    """文学质量视图与写作台深改面板读同一份参考书校准（2026-09-22 第三轮）。"""

    return LiteraryQualityService(session, rule_calibration_resolver=SceneDiagnosisService(session).rule_calibration_for_scene)

router = APIRouter(tags=["literary_quality"])

Identifier = Annotated[str, Field(min_length=1, max_length=255)]
ProtectedTerm = Annotated[str, Field(min_length=1, max_length=512)]


class LiteraryQualityAnalyzeTextRequest(StrictRequestModel):
    # Omission remains a domain error (LITERARY_QUALITY_TEXT_REQUIRED).
    content: str | None = Field(default=None, max_length=2_000_000)
    object_type: str | None = Field(default=None, max_length=64)
    object_id: str | None = Field(default=None, max_length=255)
    chapter_id: str | None = Field(default=None, max_length=255)
    scene_id: str | None = Field(default=None, max_length=255)
    source_ref: str | None = Field(default=None, max_length=1000)


class LiteraryQualityChapterSetRequest(StrictRequestModel):
    # Empty/missing lists retain LITERARY_QUALITY_CHAPTER_SET_REQUIRED.
    chapter_ids: list[Identifier] = Field(default_factory=list, max_length=500)
    protected_terms: list[ProtectedTerm] = Field(default_factory=list, max_length=500)
    # Keep vocabulary validation in the service for LITERARY_QUALITY_LAYER_INVALID.
    text_layer: str | None = Field(default=None, max_length=64)


@router.get("/api/v1/literary-quality/overview")
def literary_quality_overview(
    request: Request,
    text_layer: str = "author_draft_preferred",
    chapter_id: str | None = None,
    risk_type: str | None = None,
    min_severity: str | None = None,
    project_id: str | None = None,
    session: Session = Depends(get_session),
):
    payload = _quality_service(session).overview(
        text_layer=text_layer,
        chapter_id=chapter_id,
        risk_type=risk_type,
        min_severity=min_severity,
        project_id=project_id,
    )
    return ok(payload, req_id=getattr(request.state, "request_id", None))


@router.post("/api/v1/literary-quality/analyze-text")
def literary_quality_analyze_text(
    payload: LiteraryQualityAnalyzeTextRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/literary-quality/analyze-text",
        payload=body,
        action=lambda: _quality_service(session).analyze_text(body),
    )


@router.post("/api/v1/literary-quality/chapter-set-review")
def literary_quality_chapter_set_review(
    payload: LiteraryQualityChapterSetRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json", exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/literary-quality/chapter-set-review",
        payload=body,
        action=lambda: _quality_service(session).chapter_set_review(body),
    )
