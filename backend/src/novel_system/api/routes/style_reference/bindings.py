"""绑定:应用画像、列出 / 删除绑定、按绑定预览、注入任务默认表、叠层只读视图。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

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
    serialize_binding,
)
from novel_system.api.routes.style_reference.profiles import injection_preview_payload
from novel_system.services.errors import DomainError
from novel_system.services.scene_planning_staleness import supersede_for_binding_scope
from novel_system.services.style_reference.inject.bindings import describe_binding_layers
from novel_system.services.style_reference.inject.preview import preview_render
from novel_system.services.style_reference.injection import injection_task_defaults
from novel_system.services.style_reference.materialization import MaterializationService
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import (
    BindingScope,
    InjectionStrategy,
    TaskType,
)

router = APIRouter(tags=ROUTE_TAGS)


class ApplyConfigMixin(BaseModel):
    """apply 时落入 binding.config_json 的注入配置(MIXED 策略消费)。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    intensity: int | None = Field(default=None, ge=0, le=100)
    sub_dimensions: list[Annotated[str, Field(min_length=1, max_length=128)]] | None = (
        Field(default=None, max_length=128)
    )
    include_positive: bool | None = None
    include_forbidden: bool | None = None
    include_metric: bool | None = None
    # 2026-09-12 风格直起(Step 2):起草方式——style_first(作者手笔直起,缺省)/
    # neutral_first(中性稿再上风格,对照组)。缺省不落库,由 injection_budget.yaml 决定。
    draft_mode: Literal["style_first", "neutral_first"] | None = None


class ApplyProfileRequest(ApplyConfigMixin):
    scope: str = Field(min_length=1, max_length=64)
    scope_ref_id: str | None = Field(default=None, max_length=255)
    task_type: str = Field(default="scene_generation", min_length=1, max_length=64)
    strategy: str | None = Field(default=None, min_length=1, max_length=64)

    def injection_config(self) -> dict[str, Any]:
        """非空注入配置 → binding.config_json(端到端打通 intensity 滑块)。"""
        config: dict[str, Any] = {}
        if self.intensity is not None:
            config["intensity"] = max(0, min(100, int(self.intensity)))
        if self.sub_dimensions:
            config["sub_dimensions"] = [str(s) for s in self.sub_dimensions]
        for key in ("include_positive", "include_forbidden", "include_metric"):
            value = getattr(self, key)
            if value is not None:
                config[key] = bool(value)
        if self.draft_mode is not None:
            config["draft_mode"] = str(self.draft_mode)
        return config


@router.post(f"{PATH_PREFIX}/profiles/{{profile_id}}/apply")
def apply_profile(
    profile_id: str,
    payload: ApplyProfileRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    body = payload.model_dump(mode="json")

    def _do() -> dict[str, Any]:
        try:
            scope = BindingScope(body["scope"])
            task_type = TaskType(body.get("task_type") or "scene_generation")
            raw_strategy = body.get("strategy")
            strategy = InjectionStrategy(raw_strategy) if raw_strategy else None
        except ValueError as exc:
            raise DomainError(
                "STYLE_REFERENCE_APPLY_PARAM_INVALID",
                str(exc),
                status_code=400,
            ) from exc
        svc = MaterializationService(session)
        result = svc.apply_profile(
            profile_id,
            scope=scope,
            scope_ref_id=body.get("scope_ref_id"),
            task_type=task_type,
            strategy=strategy,
            config_json=payload.injection_config() or None,
        )
        return {"profile_id": result.profile_id, "binding_id": result.binding_id}

    return idempotent_response(
        request,
        session,
        method="POST",
        path_template=f"{PATH_PREFIX}/profiles/{{profile_id}}/apply",
        payload={"profile_id": profile_id, **body},
        action=_do,
    )


@router.get(f"{PATH_PREFIX}/profiles/{{profile_id}}/bindings")
def list_bindings(
    profile_id: str,
    request: Request,
    task_type: str | None = None,
    session: Session = Depends(get_session),
):
    repo = StyleReferenceRepository(session)
    bindings = repo.list_bindings(profile_id=profile_id, task_type=task_type)
    return ok(
        {"bindings": [serialize_binding(b) for b in bindings]},
        req_id=req_id(request),
    )


@router.delete(f"{PATH_PREFIX}/bindings/{{binding_id}}")
def delete_binding(
    binding_id: str,
    request: Request,
    payload: EmptyRequest | None = None,
    session: Session = Depends(get_session),
):
    def _do() -> dict[str, Any]:
        repo = StyleReferenceRepository(session)
        binding = repo.get_binding(binding_id)
        # 2026-09-22 结构跟随参考书:绑定删了,它作用范围内按这本参考做的规划产物作废(下一次运行重做)
        superseded = (
            supersede_for_binding_scope(
                session,
                scope=str(binding.scope),
                scope_ref_id=binding.scope_ref_id,
                reason=f"style_binding_deleted:{binding_id}",
            )
            if binding is not None
            else None
        )
        rowcount = repo.delete_binding(binding_id)
        if rowcount == 0:
            raise DomainError(
                "STYLE_REFERENCE_BINDING_NOT_FOUND",
                f"binding {binding_id!r} not found",
                status_code=404,
            )
        return {"binding_id": binding_id, "deleted": True, "superseded_planning": superseded}

    return idempotent_response(
        request,
        session,
        method="DELETE",
        path_template=f"{PATH_PREFIX}/bindings/{{binding_id}}",
        payload={"binding_id": binding_id},
        action=_do,
    )


@router.get(f"{PATH_PREFIX}/bindings/{{binding_id}}/injection-preview")
def get_binding_injection_preview(
    binding_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """读已落盘 binding,按起草时同一套选窗与块次序渲染(v3:``inject.preview``)。"""
    repo = StyleReferenceRepository(session)
    binding = repo.get_binding(binding_id)
    if binding is None:
        raise DomainError(
            "STYLE_REFERENCE_BINDING_NOT_FOUND",
            f"binding {binding_id!r} not found",
            status_code=404,
        )
    if repo.get_profile(binding.profile_id) is None:
        raise DomainError(
            "STYLE_REFERENCE_PROFILE_NOT_FOUND",
            f"profile {binding.profile_id!r} not found",
            status_code=404,
        )
    result = preview_render(
        session,
        binding.profile_id,
        binding.config_json or {},
        strategy=binding.strategy,
        project_id=binding.scope_ref_id if binding.scope == "project" else None,
    )
    return ok(injection_preview_payload(result), req_id=req_id(request))


# ---------------------------------------------------------------------------
# Injection 只读辅助:任务默认表 + 叠层预览(前端「注入应用」页数据源)
# ---------------------------------------------------------------------------


@router.get(f"{PATH_PREFIX}/injection/task-defaults")
def get_injection_task_defaults(request: Request):
    """TaskType → 默认策略 + 运行时刷新周期(refresh 真源:llm_node_registry)。"""
    return ok({"tasks": injection_task_defaults()}, req_id=req_id(request))


@router.get(f"{PATH_PREFIX}/injection/layers")
def get_injection_layers(
    request: Request,
    project_id: str | None = None,
    task_type: str = "scene_generation",
    scene_id: str | None = None,
    character_ids: str | None = None,
    session: Session = Depends(get_session),
):
    """只读叠层预览:resolve_binding_layers 命中层 + 权重/预算分配 + 合并概要。

    character_ids 逗号分隔(onstage 多角色)。无命中层时 layers=[]、merged=null。
    """
    chars = [c.strip() for c in (character_ids or "").split(",") if c.strip()] or None
    # v3:只查列、不渲染(U10);只有最具体的一层生效(applied)
    data = describe_binding_layers(
        session,
        project_id,
        task_type,
        character_ids=chars,
        scene_id=scene_id,
    )
    return ok(data, req_id=req_id(request))
