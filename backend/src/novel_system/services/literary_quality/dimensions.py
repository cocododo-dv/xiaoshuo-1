"""规则维度的词汇：维度与权重、中文名与「问题 / 改法」、严重度与文本层、规则发现的锚点与稳定的 signal id、
统一形状（场景诊断统一的发现记录）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from novel_system.services.literary_quality.text import _compact_ws


QUALITY_DIMENSIONS: tuple[str, ...] = (
    "model_voice",
    "image_homogeneity",
    "repetitive_action",
    "expository_dialogue",
    "no_choice_scene",
    "summary_ending",
    "choice_pressure",
    "ending_drive",
    "template_action_reuse",
    "image_field_reuse",
    "syntax_monotony",
    "false_clarity",
    "painless_scene",
    "decorative_imagery",
    "dialogue_as_report",
    "over_explained_motive",
    "false_poetic_closure",
    "perception_filter",
    "self_repetition",
    "conflict_too_clean",
)


DIMENSION_WEIGHTS = {
    "model_voice": 0.08,
    "image_homogeneity": 0.05,
    "repetitive_action": 0.05,
    "expository_dialogue": 0.05,
    "no_choice_scene": 0.07,
    "summary_ending": 0.05,
    "choice_pressure": 0.07,
    "ending_drive": 0.07,
    "template_action_reuse": 0.06,
    "image_field_reuse": 0.03,
    "syntax_monotony": 0.03,
    "false_clarity": 0.02,
    "painless_scene": 0.06,
    "decorative_imagery": 0.05,
    "dialogue_as_report": 0.06,
    "over_explained_motive": 0.04,
    "false_poetic_closure": 0.03,
    "perception_filter": 0.03,
    "self_repetition": 0.04,
    "conflict_too_clean": 0.06,
    # sum = 1.00 (0.08+0.05+0.05+0.05+0.07+0.05+0.07+0.07+0.06+0.03+0.03+0.02+0.06+0.05+0.06+0.04+0.03+0.03+0.04+0.06)
}


# Deterministic prose signals can reject obvious failure modes, but they cannot
# establish literary excellence.  Automated results therefore have an explicit
# ceiling below a human judgment and are never policy evidence on their own.
AUTOMATED_DIAGNOSTIC_CEILING = 0.89


AUTOMATED_EVIDENCE_TARGET_CHARS = 80


AUTOMATED_EVIDENCE_TARGET_SENTENCES = 3


AUTOMATED_EVIDENCE_SIGNAL = "automated_evidence_sufficiency"


SEVERITY_RANK = {"blocking": 0, "revision": 1, "taste": 2, "info": 3}


QUALITY_TEXT_LAYERS = {
    "author_draft_preferred",
    "runtime",
    "runtime_final_scene",
    "chapter_memory_final",
    "chapter_assembled",
}


# 2026-09-22 场景诊断统一：规则发现的锚点种类。``text`` = 钉在 needle（命中的词 / 句）上，
# ``ending`` = 钉在结尾一拍上，``scene`` = 整场缺席（无处可钉，只列出）。
FINDING_ANCHORS: tuple[str, ...] = ("text", "ending", "scene")


RULE_SIGNAL_SOURCE = "rules"


# 规则维度（QUALITY_DIMENSIONS）的中文名与「问题 / 改法」。这里是唯一一份：文学质量视图、写作台深改面板、成稿门
# 都读服务端给出的中文，不再各自维护一张英文 → 中文的对照表。
DIMENSION_LABELS: dict[str, str] = {
    "model_voice": "模型腔",
    "image_homogeneity": "意象同质",
    "repetitive_action": "动作重复",
    "expository_dialogue": "说明式对白",
    "no_choice_scene": "无抉择场景",
    "summary_ending": "概述式收尾",
    "choice_pressure": "抉择压力",
    "ending_drive": "收束驱动",
    "template_action_reuse": "模板动作复用",
    "image_field_reuse": "意象场复用",
    "syntax_monotony": "句式单调",
    "false_clarity": "虚假清晰",
    "painless_scene": "无痛场景",
    "decorative_imagery": "装饰性意象",
    "dialogue_as_report": "对白即汇报",
    "over_explained_motive": "过度解释动机",
    "false_poetic_closure": "伪诗意收束",
    "perception_filter": "感知过滤",
    "self_repetition": "自我重复",
    "conflict_too_clean": "冲突过净",
}


# (问题, 改法)。带 {needle} 的问题句把命中的词写进去。
DIMENSION_NOTES: dict[str, tuple[str, str]] = {
    "model_voice": ("「{needle}」像模型腔，或一句空泛的情绪捷径。", "把抽象的「领悟」换成具体的选择、动作或感官后果。"),
    "image_homogeneity": ("同一个意象反复出现：{needle}。", "留一个锚定意象，其余靠动作、物件、温度、声音或空间变化换质感。"),
    "repetitive_action": ("同一个动作节拍反复出现：{needle}。", "留下最有力的一拍，其余换成选择、物件移动、沉默或走位变化。"),
    "template_action_reuse": ("动作节拍在重复同一个句子模板。", "留一拍，其余变换走位、物件、沉默或人物之间的压力。"),
    "image_field_reuse": ("氛围意象承担了太多重复的工作：{needle}。", "让一个意象负责氛围，下一拍靠物件、决定或身体位置推进。"),
    "expository_dialogue": ("对白在做解释（「{needle}」），而不是施压或留潜台词。", "把事实挪进动作、沉默、矛盾或只答一半的回答里。"),
    "dialogue_as_report": ("对白在汇报情节（「{needle}」），没有改变人物之间的压力。", "把事实变成不肯回答的问题、指控、筹码或关系里的伤口。"),
    "no_choice_scene": ("这一场在纸面上看不到明确的抉择。", "给人物两个不能兼得的选项，让其中一个看得见地付出代价。"),
    "choice_pressure": ("抉择缺少看得见的压力或代价。", "用动作写出这个选择丢掉、冒险或拒绝了什么。"),
    "summary_ending": ("结尾在解释效果（「{needle}」），而不是落在动作或画面上。", "删掉概括句，停在最后一个不可逆的动作上。"),
    "ending_drive": ("最后一拍没有把读者推进下一场。", "用新的动作、物件移动、到来、离开、揭露或拒绝收尾。"),
    "false_poetic_closure": ("结尾用诗意的笃定（「{needle}」）收束，而不是一个硬的下一步动作。", "停在看得见的动作、交出的物件、拒绝、离开或不可逆的揭露上。"),
    "decorative_imagery": ("意象（「{needle}」等）更多在渲染氛围，没有推动行动、关系、信息或主题。", "只留下能改变人物行为、揭出隐瞒或加重代价的那个意象。"),
    "syntax_monotony": ("连续几句用了同一种句式。", "用一个短句、一个压住不说的反应，或从后果开头的句子打断节奏。"),
    "false_clarity": ("把本该由压力揭示的东西直接告诉了读者（「{needle}」）。", "删掉解释，让选择、拒绝、交出物件或沉默替读者推断。"),
    "over_explained_motive": ("动机被直接解释（「{needle}」），而不是被逼成行动或省略。", "让读者从人物拒绝、拖延、隐瞒或在压力下的选择里推断动机。"),
    "painless_scene": ("结构也许清楚，但没有人疼：看不到具体的损失、背叛、风险或牺牲。", "让人物在纸面上付出代价：丢掉资源、伤一段关系、藏起什么，或背弃一个价值。"),
    "perception_filter": ("叙述用「{needle}」这类感知动词转述，而不是直接呈现。", "删掉感知动词，让刺激直接落成动作、物件或感官细节。"),
    "self_repetition": ("有一句实质内容被逐字重复。", "事实只说一次；把重复的句子换成新的后果、反应或信息。"),
    "conflict_too_clean": ("冲突解决得太干净：「{needle}」之后人物很快就互相理解了。", "留下代价或余波：一句没说出口的怨、一个被接受的半谎，或一个人物本不想给的让步。"),
}


def dimension_label(dimension: str) -> str:
    return DIMENSION_LABELS.get(str(dimension or ""), "")


def rule_signal_id(finding: Mapping[str, Any]) -> str:
    """Stable id of a rule finding, derived from what it points at — not from where.

    ``rules:<dimension>:<8 hex of the needle>`` for findings pinned to a term / sentence,
    ``rules:<dimension>:ending`` / ``rules:<dimension>:scene`` for the anchored ones.
    The same finding on the same text always gets the same id, and an id survives
    edits elsewhere in the scene, so the author's 「忽略」 sticks to the finding.
    """

    dimension = str(finding.get("dimension") or "unknown")
    needle = _compact_ws(str(finding.get("needle") or ""))
    anchor = str(finding.get("anchor") or "text")
    if needle:
        digest = hashlib.sha1(needle.encode("utf-8")).hexdigest()[:8]
        return f"{RULE_SIGNAL_SOURCE}:{dimension}:{digest}"
    return f"{RULE_SIGNAL_SOURCE}:{dimension}:{anchor if anchor in FINDING_ANCHORS and anchor != 'text' else 'scene'}"


def describe_rule_finding(finding: Mapping[str, Any]) -> tuple[str, str]:
    """Chinese (issue, recommendation) for a rule finding; falls back to the engine's English."""

    dimension = str(finding.get("dimension") or "")
    notes = DIMENSION_NOTES.get(dimension)
    if notes is None:
        return str(finding.get("issue") or ""), str(finding.get("recommendation") or "")
    needle = _compact_ws(str(finding.get("needle") or ""))
    issue, fix = notes
    if "{needle}" in issue:
        if needle:
            issue = issue.replace("{needle}", needle[:40])
        else:
            issue = re.sub(r"[（(]「\{needle\}」[)）]|「\{needle\}」|：\{needle\}", "", issue)
    return issue, fix


def unify_rule_finding(finding: Mapping[str, Any]) -> dict[str, Any]:
    """The rule engine's finding in the unified scene-diagnosis shape (no location yet)."""

    issue, recommendation = describe_rule_finding(finding)
    dimension = str(finding.get("dimension") or "unknown")
    return {
        **dict(finding),
        "signal_id": rule_signal_id(finding),
        "source": RULE_SIGNAL_SOURCE,
        "dimension": dimension,
        "label": dimension_label(dimension) or dimension,
        "severity": str(finding.get("severity") or "revision"),
        "issue": issue,
        "recommendation": recommendation,
        "issue_en": str(finding.get("issue") or ""),
        "recommendation_en": str(finding.get("recommendation") or ""),
        "needle": str(finding.get("needle") or ""),
        "anchor": str(finding.get("anchor") or "text"),
    }
