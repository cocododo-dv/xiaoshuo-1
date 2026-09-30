"""画像:详情(文风画像页)、文风卡行 ✓ / ✗、禁用词、本场预览(注入预览 dryrun)。画像摘要在书库载荷里
(``GET /books`` 每本书的 ``profile``);单列画像摘要的 ``GET /profiles`` 没有界面调用,2026-09-30 删除。

- ``GET /profiles/{id}``:文风画像页要的全部数据(规范化的 16 维文风卡、每句的状态与依据引文、气质、声音、结构);
- ``POST /profiles/{id}/card-lines/{line_id}``:一句 ✓(总带上)/ ✗(不用这句),不重新学习、不让画像失效(U3);
- ``POST /profiles/{id}/injection-preview``:只读的本场预览——与起草同一套选窗、同一个块次序(U6 / J12)。

删掉的:旧「示例预览」``POST /profiles/{id}/preview`` 与它的模型节点(用的是早已不用的引擎,U6);回测三件
(``POST /profiles/{id}/validate``、``GET /reports/{id}``、``GET /profiles/{id}/reports``)——「对照检查」由作业表的
check 作业与读数表接手。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import idempotent_response
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.style_reference import BannedTermCreateRequest, CardLineStateRequest
from novel_system.api.response import respond
from novel_system.api.routes.style_reference._common import PATH_PREFIX, ROUTE_TAGS
from novel_system.services.style_reference import banned_terms
from novel_system.services.style_reference.binding_config import normalize_binding_config
from novel_system.services.style_reference.card_states import set_card_line_state
from novel_system.services.style_reference.errors import profile_not_found
from novel_system.services.style_reference.inject.preview import preview_render
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.scene_preview import scene_preview_payload
from novel_system.services.style_reference.schemas import InjectionPreviewRequest
from novel_system.services.style_reference.summaries import profile_detail

router = APIRouter(tags=ROUTE_TAGS)


def _profile_or_404(session: Session, profile_id: str):
    profile = StyleReferenceRepository(session).get_profile(profile_id)
    if profile is None:
        raise profile_not_found(profile_id)
    return profile


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}")
def get_profile(
    profile_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """文风画像页:气质、16 维文风卡(按辨识度)与每句的 ✓ / ✗ 状态和依据引文、声音习惯、结构、各维计数。"""
    profile = _profile_or_404(session, profile_id)
    return respond(request, {"profile": profile_detail(session, profile)})


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


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms")
def list_banned_terms(
    profile_id: str,
    request: Request,
    scope: str | None = None,
    session: Session = Depends(get_session),
):
    return respond(request, {"terms": banned_terms.list_banned_terms(session, profile_id, scope=scope)})


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/banned-terms")
def create_banned_term(
    profile_id: str,
    payload: BannedTermCreateRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """登记一个禁用词(业务规则在 ``services/style_reference/banned_terms``:空词 / 作用域不对 400,画像不存在 404,
    同一画像 + 同一个词 + 同一个作用域重复登记返回既有的那行)。"""
    term_text = payload.term.strip()
    scope = payload.scope.strip()

    def _do() -> dict[str, Any]:
        return banned_terms.create_banned_term(
            session, profile_id, term=term_text, scope=scope, replacement_hint=payload.replacement_hint
        )

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
    """删一个禁用词:不存在 404;预置的不能删 400;删掉自动识别的本书专名时记下来,重新学习不再加回。"""

    def _do() -> dict[str, Any]:
        return banned_terms.delete_banned_term(session, term_id)

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


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/injection-preview")
def dryrun_injection_preview(
    profile_id: str,
    payload: InjectionPreviewRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """本场预览(dryrun,不写绑定、不写选窗冻结行):按入参的 v3 四键渲染——与起草同一套选窗、同一个块次序;
    给了 ``scene_id`` 就是这一场起草时会拿到的窗。返回 ``windows``(章 / 位置 / 标签 / 维度 / 梗概)、``blocks``、
    ``sizes``、生效的 ``reference_mode`` 与 ``notices``(见 ``scene_preview``)。旧 ``strategy`` / ``intensity`` 入参不再收
    (2026-09-24);旧的 ``fragments`` / ``prefix`` / ``user_tail`` / ``stats`` / ``window_refs`` 不再回(2026-09-30,
    界面一个都不读)。"""
    # idempotency-exempt: deterministic read-only preview; no binding / selection written (the
    # book's window index may be built once as a cache).
    _profile_or_404(session, profile_id)
    config: dict[str, Any] = {}
    if payload.reference_mode is not None:
        config["reference_mode"] = payload.reference_mode
    if payload.sample_windows is not None:
        config["sample_windows"] = payload.sample_windows
    if payload.dimension_states:
        config["dimension_states"] = dict(payload.dimension_states)
    if payload.draft_mode is not None:
        config["draft_mode"] = payload.draft_mode
    result = preview_render(
        session,
        profile_id,
        config,
        scene_id=payload.scene_id,
        project_id=payload.project_id,
    )
    data = scene_preview_payload(
        session,
        profile_id,
        result,
        config=normalize_binding_config(config),
        scene_id=payload.scene_id,
    )
    return respond(request, data)
