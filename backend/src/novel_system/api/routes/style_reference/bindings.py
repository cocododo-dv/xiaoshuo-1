"""绑定:把画像直接用于作品(或某一场 / 某个角色)、改配置、解除;一部作品现在用的是哪一份。

- ``POST /profiles/{id}/apply``:``{scope, scope_ref_id, config}``——直接写绑定(台账 U1:不再经待办;U9:不再有
  合成后绑到「当时打开的作品」上的全局卡)。同一画像 + 同一目标 → 更新配置;**一个目标只有一条生效的绑定**:
  把另一份画像用于同一目标时旧的那条停用,响应的 ``replaced`` 里列出来;
- ``PATCH /bindings/{id}``:``{config}``,``dimension_states`` 按维合并(文风画像页一次改一维,N2);
- ``DELETE /bindings/{id}``:解除;
- ``GET /projects/{project_id}/style-binding``:这部作品现在实际生效的绑定 + 画像摘要 + v3 配置 + 现解析的
  风格策略审计(用于作品页与起草台读)。

配置一律是 v3 四键(``binding_config``):参考方式 ``reference_mode``、样例窗数 ``sample_windows``(0–16)、
维度状态 ``dimension_states``、起草方式 ``draft_mode``;绑定行的旧 ``strategy`` 列恒写 ``mixed``。
删掉的:``GET /bindings/{id}/injection-preview``(预览一律走 ``POST /profiles/{id}/injection-preview``)、
``GET /injection/task-defaults``(旧任务默认策略表,U16)、``GET /injection/layers``(只读叠层视图,没有界面调用,
2026-09-30)。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.mutations import mutate
from novel_system.api.requests.common import EmptyRequest
from novel_system.api.requests.style_reference import ApplyProfileRequest, BindingPatchRequest
from novel_system.api.response import respond
from novel_system.api.routes.style_reference._common import PATH_PREFIX, ROUTE_TAGS
from novel_system.db.models import StyleReferenceBook, StyleReferenceProfile
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.binding_apply import (
    BindingChange,
    apply_style_profile,
    binding_payload,
    project_style_binding,
    remove_binding,
    update_binding_config,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository

router = APIRouter(tags=ROUTE_TAGS)

def _cloud_policy_of(session: Session, profile_id: str) -> str | None:
    return session.scalar(
        select(StyleReferenceBook.cloud_policy)
        .join(StyleReferenceProfile, StyleReferenceProfile.book_id == StyleReferenceBook.book_id)
        .where(StyleReferenceProfile.profile_id == str(profile_id))
    )


def _change_payload(session: Session, change: BindingChange) -> dict[str, Any]:
    binding = change.binding
    return {
        "binding": binding_payload(binding, cloud_policy=_cloud_policy_of(session, binding.profile_id)),
        "created": change.created,
        "changed": change.changed,
        "replaced": change.replaced,
        "superseded_planning": change.superseded_planning,
    }


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/apply")
def apply_profile(
    profile_id: str,
    payload: ApplyProfileRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """直接把画像用于目标(见模块文档)。目标不存在 404 ``STYLE_REFERENCE_APPLY_TARGET_NOT_FOUND``;画像失效 /
    归档 409。响应:``binding``(v3 配置 + 生效的参考方式)、``created`` / ``changed``、``replaced``(被这次换下来的
    别的画像的绑定)、``superseded_planning``。"""
    body = payload.model_dump(mode="json")
    patch = payload.config.as_patch() if payload.config is not None else {}

    def _do() -> dict[str, Any]:
        change = apply_style_profile(
            session,
            profile_id,
            scope=payload.scope,
            scope_ref_id=payload.scope_ref_id,
            config=patch,
        )
        return {"profile_id": profile_id, **_change_payload(session, change)}

    return mutate(
        request,
        session,
        payload={"profile_id": profile_id, **body},
        action=_do,
    )


@router.patch(f"{PATH_PREFIX}/bindings/{{binding_id}}")
def patch_binding(
    binding_id: str,
    payload: BindingPatchRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """改一条绑定的配置(``dimension_states`` 按维合并)。配置真的变了,它作用范围内的规划产物作废。"""
    patch = payload.config.as_patch()

    def _do() -> dict[str, Any]:
        return _change_payload(session, update_binding_config(session, binding_id, patch))

    return mutate(
        request,
        session,
        payload={"binding_id": binding_id, "config": patch},
        action=_do,
    )


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/bindings")
def list_bindings(
    profile_id: str,
    request: Request,
    task_type: str | None = None,
    session: Session = Depends(get_session),
):
    """这份画像的全部绑定(含已停用的,``status`` 如实给出;按创建时间)。"""
    bindings = StyleReferenceRepository(session).list_bindings(profile_id=profile_id, task_type=task_type)
    cloud_policy = _cloud_policy_of(session, profile_id)
    return respond(request, {"bindings": [binding_payload(b, cloud_policy=cloud_policy) for b in bindings]})


@router.delete(f"{PATH_PREFIX}/bindings/{{binding_id}}")
def delete_binding(
    binding_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    """解除绑定;它作用范围内按这本参考做的规划产物作废(下一次运行重做)。"""

    def _do() -> dict[str, Any]:
        return remove_binding(session, binding_id)

    return mutate(
        request,
        session,
        payload={"binding_id": binding_id},
        action=_do,
    )


@router.get(f"{PATH_PREFIX}/projects/{{project_id}}/style-binding")
def get_project_style_binding(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """只读:这部作品现在实际生效的绑定(没有时 ``binding`` 为 null)、它的画像 / 参考书摘要、v3 配置、按当前绑定
    现解析的风格策略审计(不冻结契约、不写库)。作品不存在 404 ``STYLE_REFERENCE_PROJECT_NOT_FOUND``。"""
    if not project_id or len(project_id) > 128:
        raise DomainError("STYLE_REFERENCE_PROJECT_NOT_FOUND", "project not found", status_code=404)
    return respond(request, project_style_binding(session, project_id))
