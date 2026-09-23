"""风格参考 — 兼容层：:class:`InjectionService`（绑定解析的旧调用形状）。

注入在 ``services/style_reference/inject/`` 包里（请求 / 绑定解析 / 选窗 / 渲染 / 预算 / 审计 / 预览）。这里只留
bundle_builder、snowflake_chaptering 与包导出还在用的一个名字：``InjectionService(session)`` 的
``resolve_active_binding`` / ``resolve_binding_layers`` / ``describe_binding_layers`` 与 ``repo``，都转给
``inject.bindings`` 的同名函数（scene > POV 角色 > 其余角色 > project，只有最具体的一层生效）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.style_reference.inject.bindings import (
    describe_binding_layers as _describe_binding_layers,
)
from novel_system.services.style_reference.inject.bindings import (
    resolve_active_binding as _resolve_active_binding,
)
from novel_system.services.style_reference.inject.bindings import (
    resolve_binding_layers as _resolve_binding_layers,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository


class InjectionService:
    """兼容外壳：绑定解析（渲染在 ``inject.render.render_style``）。"""

    def __init__(self, session: Session):
        self.session = session
        self.repo = StyleReferenceRepository(session)

    def resolve_active_binding(
        self,
        project_id: str | None,
        task_type: str,
        *,
        character_ids: Sequence[str] | None = None,
        scene_id: str | None = None,
    ):
        return _resolve_active_binding(
            self.session, project_id, task_type, character_ids=character_ids, scene_id=scene_id
        )

    def resolve_binding_layers(
        self,
        project_id: str | None,
        task_type: str,
        *,
        character_ids: Sequence[str] | None = None,
        scene_id: str | None = None,
    ):
        return _resolve_binding_layers(
            self.session, project_id, task_type, character_ids=character_ids, scene_id=scene_id
        )

    def describe_binding_layers(
        self,
        project_id: str | None,
        task_type: str,
        *,
        character_ids: Sequence[str] | None = None,
        scene_id: str | None = None,
    ) -> dict[str, Any]:
        return _describe_binding_layers(
            self.session, project_id, task_type, character_ids=character_ids, scene_id=scene_id
        )


__all__ = ["InjectionService"]
