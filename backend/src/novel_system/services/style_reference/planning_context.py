"""规划层的风格参考解析（2026-09-12 结构跟随，Step 2 Track B）。

场景运行有 ``bundle_builder.resolve_scene_style_runtime_contract``（scene > character >
project > global）；规划一章 / 排一张场表时还没有具体的场，只能按 **project + global**
作用域解析。本模块把这条路径收口成一个函数，并把冻结契约里最具体一层的
``structure_card`` / ``planning_guidance`` 渲染成规划节点可直接注入的中文块：

- ``resolve_project_style_reference(session, project_id)`` → 见 :func:`render_planning_reference`
  的返回值；无绑定 / 旧画像无键 / 解析异常 → ``None``（只记 debug 日志，规划永不因此阻断）。
- ``render_planning_reference(contract, session=...)`` → ``{"contract_hash", "profile_id",
  "structure_card", "structure_samples", "planning_guidance"}``。样例块单独成键：雪花提示词
  预算可以先卸样例、再卸整张画像（``snowflake_prompt_budget``）。

章首 / 章尾样例是参考原文：只有该书**冻结时**与**现在**都允许送云端（与 few-shot 的
「当前发送权」口径一致）才渲染；否则只给带数字的画像。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.style_reference.injection import InjectionService
from novel_system.services.style_reference.policy import cloud_llm_allowed
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import build_style_runtime_contract
from novel_system.services.style_reference.structure import (
    render_planning_guidance,
    render_structure_card_parts,
)

logger = logging.getLogger(__name__)

PLANNING_REFERENCE_TASK_TYPE = "scene_generation"
STRUCTURE_REFERENCE_HOW_TO_USE = (
    "这是作者绑定的参考作家的结构画像与场景手法：按其章 / 场尺度（每章场数与字数、"
    "开章与收章方式、对白与叙述比重、何处用概述）规划，沿用其惯用的场景类型与收场方式；"
    "只学结构与手法，绝不复用样例里的人物、地点、事件与句子。"
)


def _samples_allowed(layer: Mapping[str, Any], session: Session | None) -> bool:
    book = layer.get("book") if isinstance(layer.get("book"), Mapping) else {}
    if book.get("cloud_llm_allowed_at_freeze") is False:
        return False
    if session is None:
        return True
    book_id = str(book.get("book_id") or "")
    if not book_id:
        return False
    row = StyleReferenceRepository(session).get_book(book_id)
    return row is not None and cloud_llm_allowed(row)


def render_planning_reference(
    contract: Mapping[str, Any] | None,
    *,
    session: Session | None = None,
) -> dict[str, Any] | None:
    """从冻结契约最具体的一层渲染规划层参考块；没有可渲染内容 → ``None``。"""
    if not isinstance(contract, Mapping):
        return None
    layers = contract.get("layers")
    if not isinstance(layers, list) or not layers:
        return None
    layer = layers[-1]
    if not isinstance(layer, Mapping):
        return None
    profile = layer.get("profile") if isinstance(layer.get("profile"), Mapping) else {}
    profile_json = profile.get("profile_json") if isinstance(profile.get("profile_json"), Mapping) else {}
    structure_card, structure_samples = render_structure_card_parts(
        profile_json,
        include_samples=_samples_allowed(layer, session),
    )
    planning_guidance = render_planning_guidance(profile_json)
    if not structure_card and not planning_guidance:
        return None
    return {
        "contract_hash": str(contract.get("contract_hash") or ""),
        "profile_id": str(profile.get("profile_id") or ""),
        "structure_card": structure_card,
        "structure_samples": structure_samples,
        "planning_guidance": planning_guidance,
    }


def structure_card_text(reference: Mapping[str, Any] | None) -> str:
    """画像 + 样例合成一段（scene_blueprint 摘要 / 章规划 slot 用）。"""
    if not isinstance(reference, Mapping):
        return ""
    return "\n".join(
        part
        for part in (
            str(reference.get("structure_card") or ""),
            str(reference.get("structure_samples") or ""),
        )
        if part
    )


def resolve_project_style_reference(
    session: Session,
    project_id: str | None,
    *,
    task_type: str = PLANNING_REFERENCE_TASK_TYPE,
) -> dict[str, Any] | None:
    """按 project + global 作用域解析 active 绑定、冻结契约、渲染规划层参考块。

    任何异常都吞掉并返回 ``None``：这是规划节点的可选增强，缺参考不能让规划失败。
    """
    if not project_id:
        return None
    try:
        service = InjectionService(session)
        layers = service.resolve_binding_layers(
            str(project_id),
            task_type,
            character_ids=[],
            scene_id=None,
        )
        if not layers:
            return None
        contract = build_style_runtime_contract(service.repo, layers, task_type=task_type)
        if not contract:
            return None
        return render_planning_reference(contract, session=session)
    except Exception:  # noqa: BLE001 — 可选增强：解析失败只记日志
        logger.debug(
            "project style reference unavailable for project %s", project_id, exc_info=True
        )
        return None


__all__ = [
    "PLANNING_REFERENCE_TASK_TYPE",
    "STRUCTURE_REFERENCE_HOW_TO_USE",
    "render_planning_reference",
    "resolve_project_style_reference",
    "structure_card_text",
]
