"""画像:列表与详情、文风卡行 ✓ / ✗、禁用词、示例预览、回测、注入预览(dryrun)。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response
from novel_system.api.request_types import EmptyRequest
from novel_system.api.response import ok
from novel_system.api.routes.style_reference._common import (
    PATH_PREFIX,
    ROUTE_TAGS,
    llm_client_and_enabled,
    req_id,
    serialize_banned_term,
    serialize_profile,
)
from novel_system.db.models import utcnow
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.card_states import set_card_line_state
from novel_system.services.style_reference.inject.preview import preview_render
from novel_system.services.style_reference.preview import PreviewService
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import (
    InjectionPreviewRequest,
    InjectionPreviewResponse,
    InjectionPreviewStats,
    SystemPromptFragments,
    ValidateRequest,
    ValidationMode,
    ValidationTargetKind,
)
from novel_system.services.style_reference.validation import (
    ValidationOrchestrator,
    start_style_reference_validation_worker,
)

router = APIRouter(tags=ROUTE_TAGS)


class PreviewRequest(BaseModel):
    """示例预览(2026-09-15):前端按段型逐张请求,进度自然可见;不传 = 三种默认段型一次生成。"""

    model_config = ConfigDict(extra="forbid")
    paragraph_types: (
        list[
            Literal[
                "dialogue",
                "description_env",
                "psychology",
                "narration",
                "action",
                "description_char",
                "transition",
                "flashback",
            ]
        ]
        | None
    ) = Field(default=None, max_length=3)


class CardLineStateRequest(BaseModel):
    """文风卡一句的状态:``pinned`` 永远带上 / ``excluded`` 不再用 / ``null`` 清掉。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    state: Literal["pinned", "excluded"] | None = None


class BannedTermCreateRequest(BaseModel):
    """禁用词登记:generation=生成期红线段填充;extraction=抽取期段落过滤。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    term: str = Field(min_length=1, max_length=512)
    replacement_hint: str | None = Field(default=None, max_length=2_000)
    scope: str = Field(default="generation", min_length=1, max_length=64)


class ValidateGeneratedRequest(BaseModel):
    """`POST /profiles/{id}/validate` body(profile_id 在 path,不在 body)。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    generated_text: str = Field(min_length=1, max_length=2_000_000)
    target_kind: str = Field(default="manual", min_length=1, max_length=64)
    target_ref_id: str | None = Field(default=None, max_length=255)
    # The route translates invalid values to STYLE_REFERENCE_VALIDATE_PARAM_INVALID.
    mode: str = Field(default="async_full", min_length=1, max_length=64)


@router.get(f"{PATH_PREFIX}/profiles")
def list_profiles(
    request: Request,
    book_id: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    profiles = repo.list_profiles(book_id=book_id, status=status)
    return ok(
        {"profiles": [serialize_profile(p) for p in profiles]},
        req_id=req_id(request),
    )


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}")
def get_profile(
    profile_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    profile = repo.get_profile(profile_id)
    if profile is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    return ok({"profile": serialize_profile(profile)}, req_id=req_id(request))


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/card-lines/{{line_id}}")
def set_profile_card_line_state(
    profile_id: str,
    line_id: str,
    payload: CardLineStateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """文风卡一句的 ✓ / ✗:``{"state": "pinned"|"excluded"|null}``(台账 U3)。

    原子地改 ``profile_json.card_line_states`` 里这一句的状态,不重新合成、不改画像状态(以前 ✗ 一条发现会把整份
    画像打回 draft,绑定它的作品随即没了风格参考)。画像没有文风卡 409 ``STYLE_REFERENCE_PROFILE_HAS_NO_CARD``;
    句子不在卡上 404 ``STYLE_REFERENCE_CARD_LINE_NOT_FOUND``。
    """
    state = payload.state

    def _do() -> dict[str, Any]:
        return set_card_line_state(session, profile_id, line_id, state)

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/card-lines/{{line_id}}",
        payload={"profile_id": profile_id, "line_id": line_id, "state": state},
        action=_do,
    )


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/preview")
def preview_profile(
    profile_id: str,
    request: Request,
    payload: PreviewRequest | None = None,
    session: Session = Depends(get_session),
):
    paragraph_types = (
        tuple(payload.paragraph_types) if payload is not None and payload.paragraph_types else None
    )

    def _do() -> dict[str, Any]:
        client, enabled = llm_client_and_enabled()
        svc = PreviewService(session, llm_client=client, llm_enabled=enabled)
        results = svc.generate(profile_id, target_types=paragraph_types)
        return {
            "profile_id": profile_id,
            "samples": [r.model_dump() for r in results],
        }

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/preview",
        payload={"profile_id": profile_id, "paragraph_types": list(paragraph_types or [])},
        action=_do,
    )


# ---------------------------------------------------------------------------
# Banned terms(禁用词:generation=注入红线段填充 / extraction=抽取段落过滤)
# ---------------------------------------------------------------------------


BANNED_TERM_SCOPES = ("generation", "extraction")


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms")
def list_banned_terms(
    profile_id: str,
    request: Request,
    scope: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    if repo.get_profile(profile_id) is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    terms = repo.list_banned_terms(profile_id, scope=scope)
    return ok(
        {"terms": [serialize_banned_term(t) for t in terms]},
        req_id=req_id(request),
    )


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms")
def create_banned_term(
    profile_id: str,
    payload: BannedTermCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    term_text = payload.term.strip()
    scope = payload.scope.strip()

    def _do() -> dict[str, Any]:
        if not term_text:
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_INVALID",
                "term must be non-empty",
                status_code=400,
            )
        if scope not in BANNED_TERM_SCOPES:
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_INVALID",
                f"scope must be one of {BANNED_TERM_SCOPES}",
                status_code=400,
            )
        repo = StyleReferenceRepository(session)
        if repo.get_profile(profile_id) is None:
            raise DomainError(
                "STYLE_REFERENCE_PROFILE_NOT_FOUND",
                f"profile {profile_id!r} not found",
                status_code=404,
            )
        # (profile_id, term, scope) 唯一:重复创建返回既有行(幂等友好)
        existing = repo.find_banned_term(profile_id, term_text, scope)
        if existing is not None:
            if payload.replacement_hint is not None:
                existing.replacement_hint = payload.replacement_hint
            return {"term": serialize_banned_term(existing), "created": False}
        row = repo.create_banned_term(
            term_id=f"sr_term_{uuid.uuid4().hex[:12]}",
            profile_id=profile_id,
            term=term_text,
            replacement_hint=payload.replacement_hint,
            source="user",
            scope=scope,
        )
        return {"term": serialize_banned_term(row), "created": True}

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms",
        payload={
            "profile_id": profile_id,
            "term": term_text,
            "scope": scope,
            "replacement_hint": payload.replacement_hint,
        },
        action=_do,
    )


@router.delete(f"{PATH_PREFIX}/banned-terms/{{term_id}}")
def delete_banned_term(
    term_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        row = repo.get_banned_term(term_id)
        if row is None:
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_NOT_FOUND",
                f"banned term {term_id!r} not found",
                status_code=404,
            )
        if row.source == "preset":
            raise DomainError(
                "STYLE_REFERENCE_BANNED_TERM_PROTECTED",
                "preset banned terms cannot be deleted",
                status_code=400,
            )
        repo.delete_banned_term(term_id)
        return {"term_id": term_id, "deleted": True}

    return idempotent_response(
        request,
        session,
        method="DELETE",
        path_template=f"{PATH_PREFIX}/banned-terms/{{term_id}}",
        payload={"term_id": term_id},
        action=_do,
    )


# ---------------------------------------------------------------------------
# PR-7 — Validation endpoints
# ---------------------------------------------------------------------------


def _serialize_validation_report(report) -> dict[str, Any]:
    status = report.status
    if not report.verdict and status == "completed":
        # Compatibility for reports created before durable async status was
        # introduced (or by a focused repository test without the new field).
        status = "queued"
    public_status = {
        "queued": "pending",
        "completed": "done",
    }.get(status, status)
    return {
        "report_id": report.report_id,
        "profile_id": report.profile_id,
        "target_kind": report.target_kind,
        "target_ref_id": report.target_ref_id,
        "verdict": report.verdict,
        "status": public_status,
        "error_code": report.error_code,
        "error_text": report.error_text,
        "retryable": bool(report.retryable),
        "started_at": report.started_at,
        "heartbeat_at": report.heartbeat_at,
        "finished_at": report.finished_at,
        "quantitative_json": report.quantitative_json or [],
        "semantic_json": report.semantic_json or [],
        "plagiarism_json": report.plagiarism_json or {},
        "forbidden_hits_json": report.forbidden_hits_json or [],
        "mode_executed": report.mode_executed,
        "created_at": report.created_at,
    }


# async_full 的 pending report(verdict 空)超过该时长视为后台 worker 孤儿
# (进程重启 / 线程池丢失),轮询端点上惰性降级为 fail,避免前端永久轮询。
REPORT_PENDING_TIMEOUT_MINUTES = 10


def _reap_orphan_report(session: Session, report) -> None:
    legacy_pending = not report.verdict and report.status == "completed"
    if report.status == "queued":
        # A queued report has not acquired a worker yet. Startup recovery owns
        # detection of a lost queue because this endpoint cannot distinguish it
        # from valid executor backpressure.
        return
    if report.status != "running" and not legacy_pending:
        return

    try:
        last_seen = datetime.fromisoformat(
            str(report.heartbeat_at or report.started_at or report.created_at).replace(
                "Z", "+00:00"
            )
        )
    except (TypeError, ValueError):
        return
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=REPORT_PENDING_TIMEOUT_MINUTES
    )
    if last_seen < cutoff:
        report.verdict = "fail"
        report.status = "failed"
        report.error_code = "STYLE_REFERENCE_VALIDATION_INTERRUPTED"
        report.error_text = (
            "async validation was interrupted; submit the text again to retry"
        )
        report.retryable = True
        report.heartbeat_at = utcnow()
        report.finished_at = utcnow()
        session.flush()


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/validate")
def validate_profile_generated(
    profile_id: str,
    payload: ValidateGeneratedRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """PR-7 §7 — sync_only / async_full 双路径 validation。"""
    body = payload.model_dump(mode="json")
    try:
        target_kind = ValidationTargetKind(body.get("target_kind") or "manual")
        mode = ValidationMode(body.get("mode") or "async_full")
    except ValueError as exc:
        raise DomainError(
            "STYLE_REFERENCE_VALIDATE_PARAM_INVALID",
            str(exc),
            status_code=400,
        ) from exc

    req = ValidateRequest(
        generated_text=body["generated_text"],
        target_kind=target_kind,
        target_ref_id=body.get("target_ref_id"),
        mode=mode,
    )
    client, enabled = llm_client_and_enabled()
    background = mode == ValidationMode.ASYNC_FULL

    def _do() -> dict[str, Any]:
        orch = ValidationOrchestrator(session, llm_client=client, llm_enabled=enabled)
        result = orch.validate(profile_id, req, defer_dispatch=background)
        return result.model_dump(mode="json")

    def _dispatch(result: dict[str, Any]) -> None:
        start_style_reference_validation_worker(
            report_id=str(result["report_id"]),
            profile_id=profile_id,
            generated_text=req.generated_text,
            llm_client=client,
            llm_enabled=enabled,
        )

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/validate",
        payload={"profile_id": profile_id, **body},
        action=_do,
        after_commit=_dispatch if background else None,
    )


@router.get(f"{PATH_PREFIX}/reports/{{report_id}}")
def get_validation_report(
    report_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    report = repo.get_validation_report(report_id)
    if report is None:
        raise DomainError(
            "STYLE_REFERENCE_REPORT_NOT_FOUND",
            f"validation report {report_id!r} not found",
            status_code=404,
        )
    _reap_orphan_report(session, report)
    return ok({"report": _serialize_validation_report(report)}, req_id=req_id(request))


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/reports")
def list_validation_reports(
    profile_id: str,
    request: Request,
    verdict: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    reports = repo.list_validation_reports(profile_id=profile_id, verdict=verdict)
    return ok(
        {"reports": [_serialize_validation_report(r) for r in reports]},
        req_id=req_id(request),
    )


# ---------------------------------------------------------------------------
# PR-9 — Injection preview endpoints
# ---------------------------------------------------------------------------


def injection_preview_payload(result: dict[str, Any]) -> dict[str, Any]:
    stats = result.get("stats") or {}
    return InjectionPreviewResponse(
        fragments=SystemPromptFragments(**result["fragments"]),
        prefix=str(result.get("prefix") or ""),
        user_tail=str(result.get("user_tail") or ""),
        stats=InjectionPreviewStats(**stats) if stats else None,
        window_refs=[dict(item) for item in result.get("window_refs") or []],
        reference_mode=result.get("reference_mode"),
        sample_windows=result.get("sample_windows"),
    ).model_dump()


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/injection-preview")
def dryrun_injection_preview(
    profile_id: str,
    payload: InjectionPreviewRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """dryrun:不写盘,按入参的绑定配置渲染(v3:与起草同一套选窗、同一个块次序;给了 scene_id 就是这一场
    起草时会拿到的窗)。"""
    # idempotency-exempt: deterministic read-only preview; no binding / selection written (the
    # book's window index may be built once as a cache).
    if StyleReferenceRepository(session).get_profile(profile_id) is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    config: dict[str, Any] = {"intensity": payload.intensity}
    if payload.reference_mode is not None:
        config["reference_mode"] = payload.reference_mode
    if payload.sample_windows is not None:
        config["sample_windows"] = payload.sample_windows
    if payload.dimension_states:
        config["dimension_states"] = dict(payload.dimension_states)
    if payload.draft_mode is not None:
        config["draft_mode"] = payload.draft_mode
    strategy = payload.strategy.value if payload.strategy is not None else None
    result = preview_render(
        session,
        profile_id,
        config,
        scene_id=payload.scene_id,
        project_id=payload.project_id,
        strategy=strategy,
    )
    return ok(injection_preview_payload(result), req_id=req_id(request))
