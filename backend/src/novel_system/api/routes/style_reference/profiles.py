"""画像:列表(摘要)与详情(文风画像页)、文风卡行 ✓ / ✗、禁用词、本场预览(注入预览 dryrun)。

- ``GET /profiles``:摘要,**不带** ``profile_json``(台账 U10);
- ``GET /profiles/{id}``:文风画像页要的全部数据(规范化的 16 维文风卡、每句的状态与依据引文、气质、声音、结构);
- ``POST /profiles/{id}/card-lines/{line_id}``:一句 ✓(总带上)/ ✗(不用这句),不重新学习、不让画像失效(U3);
- ``POST /profiles/{id}/injection-preview``:只读的本场预览——与起草同一套选窗、同一个块次序(U6 / J12)。

删掉的:旧「示例预览」``POST /profiles/{id}/preview`` 与它的模型节点(用的是早已不用的引擎,U6);回测三件
(``POST /profiles/{id}/validate``、``GET /reports/{id}``、``GET /profiles/{id}/reports``)——「对照检查」由作业表的
check 作业与读数表接手。
"""

from __future__ import annotations

import uuid
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
    req_id,
    serialize_banned_term,
)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.binding_config import normalize_binding_config
from novel_system.services.style_reference.card_states import set_card_line_state
from novel_system.services.style_reference.inject.preview import preview_render
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.scene_preview import scene_preview_payload
from novel_system.services.style_reference.schemas import (
    InjectionPreviewRequest,
    InjectionPreviewResponse,
    InjectionPreviewStats,
    SystemPromptFragments,
)
from novel_system.services.style_reference.summaries import list_profile_summaries, profile_detail
from novel_system.services.style_reference.protected_terms import PROTECTED_SOURCE, dismiss_protected_term

router = APIRouter(tags=ROUTE_TAGS)


class CardLineStateRequest(BaseModel):
    """文风卡一句的状态:``pinned`` 永远带上 / ``excluded`` 不再用 / ``null`` 清掉。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    state: Literal["pinned", "excluded"] | None = None


class BannedTermCreateRequest(BaseModel):
    """禁用词登记:generation=起草时不许出现(进红线);extraction=学习时滤掉含这个词的段落。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    term: str = Field(min_length=1, max_length=512)
    replacement_hint: str | None = Field(default=None, max_length=2_000)
    scope: str = Field(default="generation", min_length=1, max_length=64)


def _profile_or_404(session: Session, profile_id: str):
    profile = StyleReferenceRepository(session).get_profile(profile_id)
    if profile is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {profile_id!r} not found",
            status_code=404,
        )
    return profile


@router.get(f"{PATH_PREFIX}/profiles")
def list_profiles(
    request: Request,
    book_id: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
):
    """画像摘要(按创建时间):画像版本、学在何时、文风卡几句、要不要重新学;不带 ``profile_json``。"""
    return ok(
        {"profiles": list_profile_summaries(session, book_id=book_id, status=status)},
        req_id=req_id(request),
    )


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}")
def get_profile(
    profile_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """文风画像页:气质、16 维文风卡(按辨识度)与每句的 ✓ / ✗ 状态和依据引文、声音习惯、结构、各维计数。"""
    profile = _profile_or_404(session, profile_id)
    return ok({"profile": profile_detail(session, profile)}, req_id=req_id(request))


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


# ---------------------------------------------------------------------------
# Banned terms(禁用词:generation=起草红线 / extraction=学习时滤段落;protected_auto=学习作业识别的本书专名)
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
    _profile_or_404(session, profile_id)
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
        _profile_or_404(session, profile_id)
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
        if row.source == PROTECTED_SOURCE and row.profile_id:
            # 作者删掉一个自动识别的专名（多半是误收的日常词）：记下来，重新学习不再把它加回来
            dismiss_protected_term(session, str(row.profile_id), str(row.term))
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
# 本场预览(注入预览 dryrun)
# ---------------------------------------------------------------------------


def injection_preview_payload(result: dict[str, Any]) -> dict[str, Any]:
    """旧预览端点的字段(``fragments`` / ``prefix`` / ``user_tail`` / ``stats`` / ``window_refs``)。"""
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
    """本场预览(dryrun,不写绑定、不写选窗冻结行):按入参的 v3 配置渲染——与起草同一套选窗、同一个块次序;
    给了 ``scene_id`` 就是这一场起草时会拿到的窗。返回旧字段 + ``windows``(章 / 位置 / 标签 / 梗概)、``blocks``、
    ``sizes``、生效的 ``reference_mode`` 与 ``notices``(见 ``scene_preview``)。"""
    # idempotency-exempt: deterministic read-only preview; no binding / selection written (the
    # book's window index may be built once as a cache).
    _profile_or_404(session, profile_id)
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
    data = injection_preview_payload(result)
    data.update(
        scene_preview_payload(
            session,
            profile_id,
            result,
            config=normalize_binding_config(strategy, config),
            scene_id=payload.scene_id,
        )
    )
    return ok(data, req_id=req_id(request))
