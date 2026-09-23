"""风格参考 v3（2026-09-23）— 注入渲染包：参考怎样进每一个提示（契约文档 §2.3 / §2.5 / §3）。

取代 3,889 行的 ``injection.py`` 的大部分（J17）：

- ``request``：:class:`StyleRenderRequest`——一次渲染的全部输入（角色 / 落点 / 窗数上限 / 场景与设计 /
  改稿维 / 近期偏差），取代改属性传参（J16）；
- ``bindings``：绑定解析（scene > character（POV 在前）> project > global），只有最具体的一层生效（J7）；
  ``describe_binding_layers`` 只查列、不渲染（U10）；
- ``selection``：按本场设计挑样例、每场冻结一次（J2 / J3 / J4 / N4）；
- ``render``：``render_style``——参考方式三选一（N8 / J8）、文风卡 + 声音 + 样例 + 红线、按角色的口径、
  旧画像的卡替身；进程内缓存（J1）；
- ``fit``：贪心压预算（J14）；``audit``：不含正文的审计；
- ``preview``：与起草同一套选窗、同一个块次序的预览（J12）；
- ``gaps``：近期常见偏差（N7）。

本文件刻意不在导入时加载子模块（``preview`` 依赖 ``style_policy`` → ``runtime_contract`` →
``inject.bindings``，提前加载会形成导入环）；``from novel_system.services.style_reference.inject import X``
按需加载。
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS: dict[str, str] = {
    "StyleRenderRequest": "request",
    "infer_role": "request",
    "ROLE_DRAFT": "request",
    "ROLE_REVISE": "request",
    "ROLE_REVIEW": "request",
    "ROLE_PLAN": "request",
    "PLACEMENT_SYSTEM": "request",
    "PLACEMENT_USER_TAIL": "request",
    "REVIEW_K": "request",
    "PLAN_K": "request",
    "ordered_character_ids": "bindings",
    "resolve_active_binding": "bindings",
    "resolve_binding_layers": "bindings",
    "most_specific_binding": "bindings",
    "describe_binding_layers": "bindings",
    "WindowRef": "selection",
    "SceneSelection": "selection",
    "select_scene_windows": "selection",
    "resolve_scene_selection": "selection",
    "derive_situation_tags": "selection",
    "scene_chapter_position": "selection",
    "scene_dialogue_heavy": "selection",
    "RenderedStyle": "render",
    "render_style": "render",
    "reset_render_cache": "render",
    "chapter_position_mandate": "render",
    "attach_chapter_position_mandate": "render",
    "fit_rendered": "fit",
    "preview_render": "preview",
    "recent_gaps_for_project": "gaps",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = importlib.import_module(f"{__name__}.{module_name}")
    return getattr(module, name)
