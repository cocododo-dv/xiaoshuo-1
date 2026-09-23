"""风格参考 — 兼容层（2026-09-23 v3）。

注入已经搬进 ``services/style_reference/inject/`` 包（渲染、选窗、预算、预览、绑定解析）；这里只留其它模块还在
import 的几个名字：

- :class:`InjectionService`：只剩绑定解析（``resolve_active_binding`` / ``resolve_binding_layers`` /
  ``describe_binding_layers``）与 ``repo``——qc_engine、bundle_builder、snowflake_chaptering、candidate_rerank
  仍按这个形状调用；
- ``ordered_character_ids`` / ``scene_dialogue_heavy``（原样）；``scene_sampling_hints`` 只剩章内位置（v3 选窗
  不再看启发式段型，J4）；
- ``default_injection_strategy`` / ``injection_task_defaults``：旧策略列一律写 ``mixed``（v3 的「参考方式」在
  绑定配置里，见 ``binding_config``）。

删掉的：A / B / C / MIXED 四种渲染与 RAG 注入（J9）、强度公式与各块上限（J10）、量化软化与「风格分布指导」块
（J11）、``_WindowAffinityScorer``（窗口典型度在持久化窗口表里）、证据引文兜底选窗（J6 / J13）、多层合并（J7）、
漂移选窗参数、暴力搜索的预算拟合（J14）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.style_reference.inject.bindings import (
    describe_binding_layers as _describe_binding_layers,
)
from novel_system.services.style_reference.inject.bindings import (
    ordered_character_ids,
)
from novel_system.services.style_reference.inject.bindings import (
    resolve_active_binding as _resolve_active_binding,
)
from novel_system.services.style_reference.inject.bindings import (
    resolve_binding_layers as _resolve_binding_layers,
)
from novel_system.services.style_reference.inject.selection import (
    scene_chapter_position,
    scene_dialogue_heavy,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import InjectionStrategy, TaskType

# 已下线任务:持久层继续接受(见 TaskType 注释),但不进入前端任务卡片列表。
_RETIRED_TASK_TYPES = frozenset({TaskType.LONG_FORM_CONTINUATION})


def default_injection_strategy(task_type: TaskType | str) -> InjectionStrategy:
    """旧 ``strategy`` 列的默认值：v3 起一律 ``mixed``（怎么送参考看绑定配置的 ``reference_mode``）。"""
    TaskType(task_type.value if isinstance(task_type, TaskType) else str(task_type))
    return InjectionStrategy.MIXED


def injection_task_defaults() -> list[dict[str, Any]]:
    """TaskType → 默认策略 + 运行时刷新周期（只读，前端任务卡片数据源）。"""
    return [
        {"task_type": task.value, "default_strategy": InjectionStrategy.MIXED.value, "refresh_every_chars": 0}
        for task in TaskType
        if task not in _RETIRED_TASK_TYPES
    ]


def scene_sampling_hints(scene: Any) -> tuple[str | None, set[str]]:
    """兼容：(章内位置, 段型提示)。v3 选窗不看启发式段型，段型提示恒为空集。"""
    return scene_chapter_position(scene), set()


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


__all__ = [
    "InjectionService",
    "default_injection_strategy",
    "injection_task_defaults",
    "ordered_character_ids",
    "scene_dialogue_heavy",
    "scene_sampling_hints",
]
