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
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.style_reference.injection import InjectionService
from novel_system.services.style_reference.narrative_guidance import (
    NARRATIVE_GUIDANCE_SECTION_KEY,
    collect_narrative_guidance,
    render_narrative_section,
)
from novel_system.services.style_reference.policy import cloud_llm_allowed
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import build_style_runtime_contract
from novel_system.services.style_reference.structure import (
    render_planning_guidance,
    render_structure_card_parts,
)

logger = logging.getLogger(__name__)

# 2026-09-12 结构跟随（Step 2 Track B）/ 2026-09-14 保真修补（WP6.1）：场景蓝图与近终稿规划
# （章架构、人物压力）的来源快照共用这三个摘要键。``style_narrative_guidance`` 在
# ``context_budget.SECTION_SPECS`` 里（PromptBuilder 渲染成 Narrative Mechanisms section）；
# 结构画像 / 场景手法不在那张表里（它属于场景 bundle），由调用方通过
# :func:`style_reference_prompt_blocks` 直接渲染进 user prompt（体量由渲染器封顶：画像
# ≤1,500 字 + ≤6 条 ≤150 字样例 + ≤10 行手法）。
STYLE_STRUCTURE_CARD_KEY = "style_structure_card"
STYLE_PLANNING_GUIDANCE_KEY = "style_planning_guidance"
STYLE_REFERENCE_DIGEST_KEYS: tuple[str, ...] = (
    NARRATIVE_GUIDANCE_SECTION_KEY,
    STYLE_STRUCTURE_CARD_KEY,
    STYLE_PLANNING_GUIDANCE_KEY,
)
STRUCTURE_CARD_PROMPT_HEADING = "## Style Reference — Structure Card"
PLANNING_GUIDANCE_PROMPT_HEADING = "## Style Reference — Scene Craft"

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


@dataclass(frozen=True)
class PlanningStyleReference:
    """一份规划快照要登记的风格参考：契约哈希、按固定次序的摘要块、来源引用字段。"""

    contract_hash: str
    digests: dict[str, str]
    refs: dict[str, Any]


def build_planning_style_reference(
    contract: Mapping[str, Any] | None,
    *,
    session: Session | None = None,
) -> PlanningStyleReference | None:
    """从（按场景作用域解析出的）契约生成规划层三块摘要。

    叙事机制（``narrative_guidance``，无语言层特征）、结构画像（+ 允许送云端时的章首 / 章尾
    样例）、场景手法。无绑定 / 旧画像无键 → ``None``：调用方连契约哈希也不登记（与旧画像行为
    一致）。``refs`` 里 ``style_narrative_guidance_line_count`` 只要有任一块就记（可为 0），
    ``style_structure_card_chars`` / ``style_planning_guidance_line_count`` 只在对应块存在时记。
    """
    if not isinstance(contract, Mapping):
        return None
    contract_hash = str(contract.get("contract_hash") or "")
    lines = collect_narrative_guidance(contract)
    reference = render_planning_reference(contract, session=session)
    digests: dict[str, str] = {}
    if lines:
        digests[NARRATIVE_GUIDANCE_SECTION_KEY] = render_narrative_section(lines)
    if reference is not None:
        card = structure_card_text(reference)
        if card:
            digests[STYLE_STRUCTURE_CARD_KEY] = card
        guidance = str(reference.get("planning_guidance") or "")
        if guidance:
            digests[STYLE_PLANNING_GUIDANCE_KEY] = guidance
    if not digests:
        return None
    refs: dict[str, Any] = {
        "style_reference_runtime_contract_hash": contract_hash,
        "style_narrative_guidance_line_count": len(lines),
    }
    if STYLE_STRUCTURE_CARD_KEY in digests:
        refs["style_structure_card_chars"] = len(digests[STYLE_STRUCTURE_CARD_KEY])
    if STYLE_PLANNING_GUIDANCE_KEY in digests:
        refs["style_planning_guidance_line_count"] = sum(
            1 for line in digests[STYLE_PLANNING_GUIDANCE_KEY].splitlines() if line.startswith("- ")
        )
    return PlanningStyleReference(
        contract_hash=contract_hash,
        digests={key: digests[key] for key in STYLE_REFERENCE_DIGEST_KEYS if key in digests},
        refs=refs,
    )


def register_planning_style_reference(snapshot: dict[str, Any], reference: PlanningStyleReference) -> None:
    """把摘要块登记进来源快照（``source_version_refs`` / ``ordered_injections`` / ``inline_digests``）。

    次序固定为 :data:`STYLE_REFERENCE_DIGEST_KEYS`，``ref_id`` 是契约哈希，``digest_key`` 与
    slot 同名——快照哈希因此随参考设计变化。
    """
    refs = snapshot.setdefault("source_version_refs", {})
    injections = snapshot.setdefault("ordered_injections", [])
    digests = snapshot.setdefault("inline_digests", {})
    refs.update(reference.refs)
    for key, text in reference.digests.items():
        injections.append({"slot": key, "ref_id": reference.contract_hash, "digest_key": key})
        digests[key] = text


def snapshot_has_style_reference(snapshot: Any) -> bool:
    """来源快照是否带任一风格参考块（叙事机制 / 结构画像 / 场景手法）。"""
    if not isinstance(snapshot, Mapping):
        return False
    digests = snapshot.get("inline_digests")
    if not isinstance(digests, Mapping):
        return False
    return any(str(digests.get(key) or "").strip() for key in STYLE_REFERENCE_DIGEST_KEYS)


def style_reference_prompt_blocks(source: Mapping[str, Any] | None) -> list[str]:
    """结构画像 / 场景手法不在 SECTION_SPECS 里，直接渲染进 user prompt（体量已由渲染器封顶）。

    接受 ``{"snapshot": {...}}`` 形式的来源字典，也接受快照本身。
    """
    if not isinstance(source, Mapping):
        return []
    snapshot = source.get("snapshot") if isinstance(source.get("snapshot"), Mapping) else source
    digests = snapshot.get("inline_digests") if isinstance(snapshot, Mapping) else None
    if not isinstance(digests, Mapping):
        return []
    blocks: list[str] = []
    card = str(digests.get(STYLE_STRUCTURE_CARD_KEY) or "").strip()
    if card:
        blocks.extend(["", STRUCTURE_CARD_PROMPT_HEADING, card])
    guidance = str(digests.get(STYLE_PLANNING_GUIDANCE_KEY) or "").strip()
    if guidance:
        blocks.extend(["", PLANNING_GUIDANCE_PROMPT_HEADING, guidance])
    return blocks


__all__ = [
    "PLANNING_GUIDANCE_PROMPT_HEADING",
    "PLANNING_REFERENCE_TASK_TYPE",
    "PlanningStyleReference",
    "STRUCTURE_CARD_PROMPT_HEADING",
    "STRUCTURE_REFERENCE_HOW_TO_USE",
    "STYLE_PLANNING_GUIDANCE_KEY",
    "STYLE_REFERENCE_DIGEST_KEYS",
    "STYLE_STRUCTURE_CARD_KEY",
    "build_planning_style_reference",
    "register_planning_style_reference",
    "render_planning_reference",
    "resolve_project_style_reference",
    "snapshot_has_style_reference",
    "structure_card_text",
    "style_reference_prompt_blocks",
]
