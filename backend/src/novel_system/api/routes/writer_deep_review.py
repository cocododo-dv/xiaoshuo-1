from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import optional_idempotent_response
from novel_system.api.request_types import EmptyRequest, StrictRequestModel
from novel_system.api.response import ok
from novel_system.services.scene_deep_review_preferences import SceneDeepReviewPreferencesService
from novel_system.services.scene_diagnosis import SceneDiagnosisService
from novel_system.services.scene_lookup import require_scene
from novel_system.services.writer_deep_review import WriterDeepReviewService

router = APIRouter(tags=["writer-deep-review"])

OptionalIdentifier = Annotated[str, Field(max_length=255)]
PreferenceTag = Annotated[str, Field(min_length=1, max_length=128)]


class PassageTargetRangeRequest(StrictRequestModel):
    start: int | None = Field(default=None, ge=0, le=2_000_000)
    end: int | None = Field(default=None, ge=0, le=2_000_000)
    unit: str | None = Field(default=None, max_length=32)


class PassagePatchCreateRequest(StrictRequestModel):
    # Required domain fields remain optional here so PASSAGE_PATCH_INVALID is
    # retained for omissions; present values are still type/size checked.
    object_type: str | None = Field(default=None, max_length=64)
    object_id: OptionalIdentifier | None = None
    chapter_id: OptionalIdentifier | None = None
    scene_id: OptionalIdentifier | None = None
    source_text_ref: str | None = Field(default=None, max_length=1000)
    target_text_ref: str | None = Field(default=None, max_length=1000)
    source_draft_id: OptionalIdentifier | None = None
    quality_signal_id: OptionalIdentifier | None = None
    source_excerpt: str | None = Field(default=None, max_length=100_000)
    issue_dimension: str | None = Field(default=None, max_length=128)
    candidate_category: str | None = Field(default=None, max_length=64)
    target_range: PassageTargetRangeRequest | None = None
    revision_strategy: str | None = Field(default=None, max_length=4000)
    preference_tags: list[PreferenceTag] = Field(default_factory=list, max_length=64)
    # 2026-09-22 场景诊断统一：作者 / 诊断给的改法（工具条指令、发现的建议）与发现的问题句。
    # issue_dimension 从此只放维度键（发现的 dimension，或自由改写的 author_instruction）。
    instruction: str | None = Field(default=None, max_length=4000)
    issue_note: str | None = Field(default=None, max_length=2000)


class PassagePatchAcceptRequest(StrictRequestModel):
    selected_option_id: OptionalIdentifier | None = None
    note: str | None = Field(default=None, max_length=4000)


class PassagePatchRejectRequest(StrictRequestModel):
    note: str | None = Field(default=None, max_length=4000)


class DeepReviewDecisionRequest(StrictRequestModel):
    at: int = Field(ge=0, le=(1 << 63) - 1)
    text: str = Field(min_length=1, max_length=1000)


class PassageReviewRequest(StrictRequestModel):
    """「AI 看这一处」：复核一条发现（signal_id），或独立地看一段（paragraph_index / 选中的原话）/ 一段范围
    （paragraph_start–paragraph_end，含两端）；模型同时看到整场正文，跨段的矛盾也能指出。"""

    signal_id: str | None = Field(default=None, max_length=255)
    paragraph_index: int | None = Field(default=None, ge=0, le=100_000)
    paragraph_start: int | None = Field(default=None, ge=0, le=100_000)
    paragraph_end: int | None = Field(default=None, ge=0, le=100_000)
    excerpt: str | None = Field(default=None, max_length=2000)
    question: str | None = Field(default=None, max_length=2000)


class ChapterReviewRequest(StrictRequestModel):
    """「AI 通读本章」：``all`` 整章一次；``changed`` 只把上次通读之后改过字的场全文送审（未改的场沿用上次的发现）。"""

    scope: str | None = Field(default=None, pattern="^(all|changed)$")


class SceneDeepReviewPreferencesSaveRequest(StrictRequestModel):
    decision_log: list[DeepReviewDecisionRequest] = Field(default_factory=list, max_length=30)
    ignored_issue_keys: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list,
        max_length=200,
    )
    base_revision_no: int = Field(ge=0, le=(1 << 63) - 1)


@router.get("/api/v1/scenes/{scene_id}/deep-review/preferences")
def get_scene_deep_review_preferences(
    scene_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    payload = SceneDeepReviewPreferencesService(session).get(scene_id)
    return ok(payload, req_id=getattr(request.state, "request_id", None))


@router.patch("/api/v1/scenes/{scene_id}/deep-review/preferences")
def save_scene_deep_review_preferences(
    scene_id: str,
    payload: SceneDeepReviewPreferencesSaveRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")
    return optional_idempotent_response(
        request,
        session,
        method="PATCH",
        path_template="/api/v1/scenes/{scene_id}/deep-review/preferences",
        payload={"scene_id": scene_id, "body": body},
        action=lambda: SceneDeepReviewPreferencesService(session).save(
            scene_id,
            decision_log=body["decision_log"],
            ignored_issue_keys=body["ignored_issue_keys"],
            base_revision_no=body["base_revision_no"],
        ),
    )


@router.get("/api/v1/scenes/{scene_id}/deep-review")
def get_scene_deep_review(scene_id: str, request: Request, session: Session = Depends(get_session)):
    payload = WriterDeepReviewService(session).scene_summary(scene_id)
    return ok(payload, req_id=getattr(request.state, "request_id", None))


@router.post("/api/v1/scenes/{scene_id}/deep-review")
def run_scene_deep_review(
    scene_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/deep-review",
        payload={"scene_id": scene_id},
        action=lambda: WriterDeepReviewService(session).run_scene_review(scene_id, actor_ref=actor_ref),
    )


@router.post("/api/v1/scenes/{scene_id}/deep-review/passage")
def run_scene_passage_review(
    scene_id: str,
    payload: PassageReviewRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json", exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/scenes/{scene_id}/deep-review/passage",
        payload={"scene_id": scene_id, "body": body},
        action=lambda: WriterDeepReviewService(session).run_passage_review(
            scene_id,
            signal_id=body.get("signal_id"),
            paragraph_index=body.get("paragraph_index"),
            paragraph_start=body.get("paragraph_start"),
            paragraph_end=body.get("paragraph_end"),
            excerpt=body.get("excerpt"),
            question=body.get("question"),
            actor_ref=actor_ref,
        ),
    )


@router.get("/api/v1/projects/{project_id}/diagnosis-summary")
def get_project_diagnosis_summary(project_id: str, request: Request, session: Session = Depends(get_session)):
    """一本书每一场 / 每一章开着的发现数——视图挂载 / 换作品时读一次；之后的变化随各写入的响应回传
    （``diagnosis_rollup``），见 GET …/scenes/{id}/diagnosis-rollup。"""

    payload = SceneDiagnosisService(session).project_summary(project_id)
    return ok(payload, req_id=getattr(request.state, "request_id", None))


@router.get("/api/v1/scenes/{scene_id}/diagnosis-rollup")
def get_scene_diagnosis_rollup(scene_id: str, request: Request, session: Session = Depends(get_session)):
    """这一场所在那一章的计数（章条目 + 章里每一场的条目）：起草台归档终稿之后前端据此更新角标，不必拉整本书。"""

    service = SceneDiagnosisService(session)
    scene = require_scene(session, scene_id, trashed_as_conflict=True)
    return ok(service.scene_rollup(scene), req_id=getattr(request.state, "request_id", None))


@router.get("/api/v1/chapters/{chapter_id}/diagnosis-rollup")
def get_chapter_diagnosis_rollup(chapter_id: str, request: Request, session: Session = Depends(get_session)):
    """这一章的计数（章条目 + 章里每一场的条目）：章运行 / 场景运行在服务端归档了终稿之后前端据此更新角标。"""

    payload = SceneDiagnosisService(session).chapter_rollup(chapter_id)
    return ok(payload, req_id=getattr(request.state, "request_id", None))


@router.get("/api/v1/chapters/{chapter_id}/deep-review")
def get_chapter_deep_review(chapter_id: str, request: Request, session: Session = Depends(get_session)):
    payload = WriterDeepReviewService(session).chapter_summary(chapter_id)
    return ok(payload, req_id=getattr(request.state, "request_id", None))


@router.post("/api/v1/chapters/{chapter_id}/deep-review")
def run_chapter_deep_review(
    chapter_id: str,
    request: Request,
    payload: ChapterReviewRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    scope = (payload.scope if payload is not None else None) or "all"
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/chapters/{chapter_id}/deep-review",
        payload={"chapter_id": chapter_id, "scope": scope},
        action=lambda: WriterDeepReviewService(session).run_chapter_review(chapter_id, actor_ref=actor_ref, scope=scope),
    )


@router.post("/api/v1/passages/patch-candidates")
def create_passage_patch_candidate(
    payload: PassagePatchCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json", exclude_unset=True)
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/passages/patch-candidates",
        payload=body,
        action=lambda: WriterDeepReviewService(session).create_patch_candidate(body, actor_ref=actor_ref),
    )


@router.post("/api/v1/passage-patch-candidates/{patch_id}/accept")
def accept_passage_patch_candidate(
    patch_id: str,
    request: Request,
    payload: PassagePatchAcceptRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/passage-patch-candidates/{patch_id}/accept",
        payload={"patch_id": patch_id, "body": body},
        action=lambda: WriterDeepReviewService(session).accept_patch_candidate(
            patch_id, body, actor_ref=actor_ref
        ),
    )


@router.post("/api/v1/passage-patch-candidates/{patch_id}/reject")
def reject_passage_patch_candidate(
    patch_id: str,
    request: Request,
    payload: PassagePatchRejectRequest | None = None,
    session: Session = Depends(get_session),
):
    actor_ref = getattr(request.state, "operator_ref", None) or "operator"
    body = payload.model_dump(mode="json", exclude_unset=True) if payload is not None else {}
    return optional_idempotent_response(
        request,
        session,
        method="POST",
        path_template="/api/v1/passage-patch-candidates/{patch_id}/reject",
        payload={"patch_id": patch_id, "body": body},
        action=lambda: WriterDeepReviewService(session).reject_patch_candidate(
            patch_id, body, actor_ref=actor_ref
        ),
    )


@router.get("/api/v1/author-preference-profile")
def get_author_preference_profile(request: Request, session: Session = Depends(get_session)):
    payload = WriterDeepReviewService(session).author_preference_profile()
    return ok(payload, req_id=getattr(request.state, "request_id", None))
