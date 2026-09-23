"""风格参考 v3 — 这份提示会发给哪个模型节点（云策略按接收提示的节点判，H1）。

「仅本机」的书只允许本机模型读到由它派生的任何东西；判定必须看**这一次调用实际走的节点路由**，不能看全局
运行时模型（全局是本机不代表起草节点走本机，反过来也一样）。注入适配器
``style_prompt_injection.inject_style_reference_prefix`` 的调用方可以用 ``node_id=`` 直接说；没说时按
``prompt["template_name"]``（``PromptBuilder.build`` 写的）推：

- :data:`TEMPLATE_NODE_IDS`：按调用点逐一核对过的「模板 → 派发它的节点」表。有的模板借别的节点的路由派发
  （``style_first_draft`` / ``style_targeted_revision`` 走 ``style_draft``；``scene_blueprint_facts`` 走
  ``scene_blueprint``；``style_ref_check_judge`` 与对照检查走 ``soft_qc``；``writer_passage_review`` 走
  ``writer_deep_review``）；有的模板会在几个节点下派发（``style_draft`` 模板既是风格稿 ``style_draft``，也是
  软补丁 / 去模板 / 安全修复的 ``style_patch``）——这时列出全部候选，判定要求**每一个**都满足；
- 表里没有的模板：模板名本身就是一个注册节点（``llm_node_registry``）时用它；
- 仍说不出 → ``()``：对「仅本机」的书按不许送处理（fail closed），对送云策略的书没有影响。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novel_system.services.style_reference.policy import normalize_node_ids

# 模板 → 派发它的节点（调用点：scene_generation / qc_engine / near_final / writer_deep_review / author_drafts /
# scene_blueprint / check_job；改了派发节点要回来改这张表）
TEMPLATE_NODE_IDS: dict[str, tuple[str, ...]] = {
    # 起草 / 改稿（风格通道）
    "style_first_draft": ("style_draft",),
    "style_targeted_revision": ("style_draft",),
    # 风格稿（style_draft）与软补丁（style_patch）共用这个模板；去模板 / 安全修复拿风格稿的提示在 style_patch 下派发
    "style_draft": ("style_draft", "style_patch"),
    "style_length_patch": ("style_patch",),
    "style_salvage_patch": ("style_patch",),
    # 准定稿改写；它的去模板修复同样借这份提示在 style_patch 下派发
    "scene_literary_rewrite": ("scene_literary_rewrite", "style_patch"),
    "neutral_draft": ("neutral_draft",),
    # 评审
    "soft_qc": ("soft_qc",),
    "style_ref_check_judge": ("soft_qc",),
    "near_final_acceptance_review": ("near_final_acceptance_review",),
    "chapter_near_final_review": ("chapter_near_final_review",),
    # 规划
    "scene_blueprint": ("scene_blueprint",),
    "scene_blueprint_facts": ("scene_blueprint",),
    "chapter_story_architecture": ("chapter_story_architecture",),
    "character_pressure_blueprint": ("character_pressure_blueprint",),
    # 写作台
    "writer_deep_review": ("writer_deep_review",),
    "writer_passage_review": ("writer_deep_review",),
    "writer_passage_patch": ("writer_passage_patch",),
    "author_proposal_generate": ("author_proposal_generate",),
}


def nodes_for_template(template_name: str | None) -> tuple[str, ...]:
    """模板 → 接收它的节点（候选全部列出）；说不出 → ``()``。"""
    name = str(template_name or "").strip()
    if not name:
        return ()
    if name in TEMPLATE_NODE_IDS:
        return TEMPLATE_NODE_IDS[name]
    from novel_system.services.llm_node_registry import get_llm_node_spec

    spec = get_llm_node_spec(name)
    if spec is not None and spec.requires_llm:
        return (spec.node_id,)
    return ()


def prompt_node_ids(prompt: Mapping[str, Any] | None, node_id: Sequence[str] | str | None = None) -> tuple[str, ...]:
    """这份提示的接收节点：调用方给的 ``node_id``（一个或几个）优先，否则按 ``prompt["template_name"]`` 推。"""
    explicit = normalize_node_ids(node_id)
    if explicit:
        return explicit
    template_name = prompt.get("template_name") if isinstance(prompt, Mapping) else None
    return nodes_for_template(str(template_name or ""))


__all__ = ["TEMPLATE_NODE_IDS", "nodes_for_template", "prompt_node_ids"]
