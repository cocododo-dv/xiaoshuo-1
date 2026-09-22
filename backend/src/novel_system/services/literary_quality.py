from __future__ import annotations

import hashlib
import math
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import AuthorDraft, ChapterGoal, ChapterMemory, ChapterState, FinalScene, SceneCard, SceneRunState
from novel_system.services.errors import DomainError
from novel_system.services.manuscript_html import plain_manuscript_text


_LOGGER = logging.getLogger(__name__)


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
    "valid_ambiguity",
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
    "valid_ambiguity": 0.00,
    "painless_scene": 0.06,
    "decorative_imagery": 0.05,
    "dialogue_as_report": 0.06,
    "over_explained_motive": 0.04,
    "false_poetic_closure": 0.03,
    "perception_filter": 0.03,
    "self_repetition": 0.04,
    "conflict_too_clean": 0.06,
    # sum = 1.00 (0.08+0.05+0.05+0.05+0.07+0.05+0.07+0.07+0.06+0.03+0.03+0.02+0+0.06+0.05+0.06+0.04+0.03+0.03+0.04+0.06)
}

STYLE_WEIGHT_ADJUSTMENTS: dict[str, dict[str, float]] = {
    "hard_boiled": {"perception_filter": 0.5, "dialogue_as_report": 1.5},
    "literary": {"syntax_monotony": 0.5, "decorative_imagery": 0.5},
    "thriller": {"template_action_reuse": 1.5, "ending_drive": 1.5},
    "wuxia": {"image_homogeneity": 0.8, "decorative_imagery": 0.7},
}
"""Multipliers applied to base weights per writing style, then renormalized to sum=1.0."""


# Deterministic prose signals can reject obvious failure modes, but they cannot
# establish literary excellence.  Automated results therefore have an explicit
# ceiling below a human judgment and are never policy evidence on their own.
AUTOMATED_DIAGNOSTIC_CEILING = 0.89
AUTOMATED_EVIDENCE_TARGET_CHARS = 80
AUTOMATED_EVIDENCE_TARGET_SENTENCES = 3
AUTOMATED_EVIDENCE_SIGNAL = "automated_evidence_sufficiency"


def _renormalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """Renormalize weight dict so values sum to 1.0, preserving relative ratios."""
    total = sum(weights.values())
    if total <= 0.0:
        return dict(DIMENSION_WEIGHTS)
    return {dim: round(w / total, 6) for dim, w in weights.items()}


def get_dimension_weights(
    project_id: str | None = None,
    session: Session | None = None,
    *,
    style_profile: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Return effective quality-dimension weights for a project.

    Resolution order:
    1. If *style_profile* is given and contains ``quality_weight_overrides``
       (a dict mapping dimension names to multiplier floats), those multipliers
       are applied to ``DIMENSION_WEIGHTS`` and the result is renormalized.
    2. If *project_id* and *session* are given, the function looks up the
       project's active ``StyleReferenceInjectionBinding`` to find a
       ``StyleReferenceProfile`` whose ``profile_json`` might contain
       ``quality_weight_overrides`` or ``style_tag`` (mapped through
       ``STYLE_WEIGHT_ADJUSTMENTS``).
    3. Falls back to the static ``DIMENSION_WEIGHTS`` constant.
    """
    overrides: dict[str, float] | None = None
    style_tag: str | None = None

    # --- explicit style_profile dict (e.g. passed from caller) ---
    if style_profile:
        overrides = style_profile.get("quality_weight_overrides")
        if not overrides:
            style_tag = style_profile.get("style_tag")

    # --- DB lookup for project-bound profile ---
    if overrides is None and style_tag is None and project_id and session:
        try:
            from novel_system.db.models import (
                StyleReferenceInjectionBinding,
                StyleReferenceProfile,
            )
            binding = session.execute(
                select(StyleReferenceInjectionBinding)
                .where(
                    StyleReferenceInjectionBinding.scope == "project",
                    StyleReferenceInjectionBinding.scope_ref_id == project_id,
                    StyleReferenceInjectionBinding.status == "active",
                )
                .order_by(StyleReferenceInjectionBinding.created_at.desc())
            ).scalars().first()
            if binding is not None:
                profile = session.get(StyleReferenceProfile, binding.profile_id)
                if profile is not None:
                    pj = profile.profile_json or {}
                    overrides = pj.get("quality_weight_overrides")
                    if not overrides:
                        style_tag = pj.get("style_tag")
        except Exception:
            _LOGGER.warning(
                "Style-bound literary weights lookup degraded project_id=%s",
                project_id,
                exc_info=True,
            )

    # --- apply overrides as multipliers ---
    if isinstance(overrides, dict) and overrides:
        adjusted = {
            dim: base * overrides.get(dim, 1.0)
            for dim, base in DIMENSION_WEIGHTS.items()
        }
        return _renormalize_weights(adjusted)

    # --- apply style_tag through STYLE_WEIGHT_ADJUSTMENTS ---
    if style_tag and style_tag in STYLE_WEIGHT_ADJUSTMENTS:
        multipliers = STYLE_WEIGHT_ADJUSTMENTS[style_tag]
        adjusted = {
            dim: base * multipliers.get(dim, 1.0)
            for dim, base in DIMENSION_WEIGHTS.items()
        }
        return _renormalize_weights(adjusted)

    return dict(DIMENSION_WEIGHTS)


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

# 21 维的中文名与「问题 / 改法」。这里是唯一一份：文学质量视图、写作台深改面板、成稿门
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
    "valid_ambiguity": "有效留白",
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
    "valid_ambiguity": ("有效留白。", ""),
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


def ignored_rule_dimensions(text: str, ignored_keys: Iterable[str]) -> set[str]:
    """Dimensions whose every rule finding on ``text`` the author has ignored in the writer.

    The final-text gate drops its Q3 warnings for those dimensions: a finding dismissed
    in the deep drawer must not come back as a warning in 成稿中心.
    """

    ignored = {str(key) for key in ignored_keys or [] if str(key)}
    if not ignored:
        return set()
    _, findings = analyze_literary_quality(text)
    by_dimension: dict[str, list[str]] = {}
    for finding in findings:
        by_dimension.setdefault(str(finding.get("dimension") or ""), []).append(rule_signal_id(finding))
    return {
        dimension
        for dimension, ids in by_dimension.items()
        if dimension and ids and all(signal_id in ignored for signal_id in ids)
    }

MODEL_VOICE_TERMS = (
    "suddenly realized",
    "somehow meaningful",
    "somehow",
    "for some reason",
    "couldn't help but",
    "as if fate",
    "everything changed forever",
    "忽然意识到",
    "突然意识到",
    "不知为何",
    "某种意义上",
    "仿佛命运",
    "气氛十分尴尬",
    "一切都变得",
    "微微一笑",
    "心中一紧",
    "不禁",
    "心头一热",
    "深吸一口气",
    "缓缓说道",
    "淡淡地说",
    "轻轻地",
    "默默地",
    "眼中闪过",
    "嘴角微扬",
    "语气平淡",
    "脸上露出",
    "声音低沉",
    "嘴角勾起",
)

EXPOSITORY_DIALOGUE_TERMS = (
    "as you know",
    "because",
    "let me explain",
    "the truth is",
    "i explain",
    "i must explain",
    "you need to know",
    "因为",
    "所以",
    "其实",
    "你知道",
    "我解释",
    "真相是",
    "这是为了",
)

CHOICE_TERMS = (
    "choose",
    "choice",
    "decide",
    "decision",
    "cannot both",
    "could not both",
    "either",
    " or ",
    "must",
    "had to",
    "cost",
    "pay",
    "risk",
    "give up",
    "refuse",
    "选择",
    "决定",
    "不能同时",
    "要么",
    "还是",
    "必须",
    "代价",
    "牺牲",
    "放弃",
)

PRESSURE_TERMS = (
    "cannot both",
    "could not both",
    "must choose",
    "had to choose",
    "at the cost",
    "risk",
    "or save",
    "or the",
    "pay",
    "give up",
    "不能同时",
    "只能",
    "必须选择",
    "代价",
    "冒险",
    "牺牲",
)

SUMMARY_ENDING_TERMS = (
    "in the end",
    "everything changed forever",
    "from then on",
    "she understood",
    "he understood",
    "finally realized",
    "all of this",
    "这一刻",
    "从此",
    "终于明白",
    "一切都",
    "他知道",
    "她知道",
)

ENDING_ACTION_TERMS = (
    "opened",
    "closed",
    "left",
    "took",
    "put",
    "handed",
    "raised",
    "fell",
    "scraped",
    "ran",
    "turned",
    "crossed",
    "stepped",
    "broke",
    "pressed",
    "held",
    "放",
    "推",
    "开",
    "关",
    "走",
    "递",
    "举",
    "落",
    "转身",
    "按",
    "握",
)

CHAPTER_SET_NEXT_PULL_TERMS = (
    "hook",
    "next",
    "arrived",
    "arrives",
    "left",
    "opened",
    "handed",
    "pressed",
    "sent",
    "called",
    "revealed",
    "refused",
    "released",
    "下一",
    "入口",
    "名单",
    "反证",
    "钩子",
    "推开",
    "递给",
    "交给",
    "离开",
    "按下",
    "拨通",
    "公开",
    "拒绝",
    "证人",
    "保护",
)
NEGATED_ACTION_CONTEXT_TERMS = (
    "no ",
    "not ",
    "never ",
    "without ",
    "did not ",
    "does not ",
    "没有",
    "并未",
    "未曾",
    "不能",
    "不再",
    "无",
)

REPETITIVE_ACTION_TERMS = (
    "turned",
    "looked",
    "nodded",
    "smiled",
    "sighed",
    "stepped",
    "held",
    "opened",
    "closed",
    "转身",
    "看",
    "点头",
    "笑",
    "叹气",
    "走",
    "握",
    "打开",
    "关上",
)

IMAGE_TERMS = (
    "moon",
    "fog",
    "shadow",
    "light",
    "dark",
    "rain",
    "wind",
    "blood",
    "fire",
    "mirror",
    "door",
    "key",
    "water",
    "hand",
    "eye",
    "window",
    "月",
    "雾",
    "影",
    "光",
    "雨",
    "风",
    "血",
    "火",
    "镜",
    "门",
    "钥匙",
    "手",
    "眼",
)

ATMOSPHERIC_IMAGE_TERMS = (
    "moon",
    "shadow",
    "dark",
    "rain",
    "wind",
    "fog",
    "cold",
    "light",
    "月",
    "月光",
    "影",
    "阴影",
    "冷",
    "风",
    "雾",
    "雾气",
    "光",
)

FALSE_CLARITY_TERMS = (
    "she knew",
    "he knew",
    "finally understood",
    "suddenly realized",
    "everything became clear",
    "她知道",
    "他知道",
    "终于明白",
    "忽然意识到",
    "突然意识到",
    "真相必须",
    "一切都变得",
    "心中了然",
    "恍然大悟",
    "这一刻她明白",
    "这一刻他明白",
    "答案已经明确",
)

COST_TERMS = (
    "cost",
    "price",
    "risk",
    "lose",
    "lost",
    "sacrifice",
    "give up",
    "betray",
    "hide",
    "代价",
    "牺牲",
    "失去",
    "冒险",
    "背叛",
    "隐瞒",
    "误伤",
    "放弃",
    "不能同时",
    "只能",
)

DECORATIVE_IMAGE_TERMS = (
    "as if fate",
    "like fate",
    "old wound",
    "destiny",
    "atmosphere",
    "仿佛命运",
    "像旧伤疤",
    "旧伤疤",
    "命运",
    "冷意",
    "回声",
    "氛围",
    "像某种",
    "仿佛",
)

REPORT_DIALOGUE_TERMS = (
    "official report",
    "the report",
    "the truth is",
    "i explain",
    "let me explain",
    "evidence shows",
    "官方报告",
    "真相是",
    "我解释",
    "解释给你听",
    "证据显示",
    "报告里",
    "这是因为",
)

MOTIVE_EXPLANATION_TERMS = (
    "because she",
    "because he",
    "because they",
    "so she",
    "so he",
    "she knew she had to",
    "he knew he had to",
    "为了",
    "因为她",
    "因为他",
    "因为他们",
    "所以她",
    "所以他",
    "她知道自己必须",
    "他知道自己必须",
    "原因是",
)

POETIC_CLOSURE_TERMS = (
    "everything changed forever",
    "as if fate",
    "echo",
    "destiny",
    "一切都变得",
    "仿佛命运",
    "命运",
    "回声",
    "余韵",
    "她知道",
    "他知道",
)

POETIC_CLOSURE_ACTION_TERMS = (
    "opened",
    "closed",
    "left",
    "took",
    "handed",
    "raised",
    "crossed",
    "stepped",
    "pressed",
    "held",
    "放下",
    "推开",
    "打开",
    "关上",
    "走出",
    "递出",
    "举起",
    "落下",
    "转身",
    "按下",
    "握住",
)

PERCEPTION_FILTER_TERMS = (
    "she noticed",
    "he noticed",
    "she felt",
    "he felt",
    "she realized",
    "he realized",
    "she sensed",
    "he sensed",
    "她觉得",
    "他觉得",
    "她看到",
    "他看到",
    "她注意到",
    "他注意到",
    "她感到",
    "他感到",
    "她意识到",
    "他意识到",
    "似乎感觉到",
    "好像听到",
    "仿佛看到",
)


# ---------------------------------------------------------------------------
# 2026-09-22 第三轮：按参考书校准 21 维规则（词表 + 维度）
# ---------------------------------------------------------------------------

RULE_NEEDLE_HABIT_PER_10K = 1.0       # 参考作者每万字用到 ≥1 次的词表词是这位作者的常用词，不当作毛病
RULE_NEEDLE_OVERUSE_FACTOR = 4.0      # …除非稿子里的密度到了参考作者的 4 倍以上（且至少 3 次）：那是过量，照提示
RULE_NEEDLE_OVERUSE_MIN_COUNT = 3
RULE_DIMENSION_HABIT_SHARE = 0.5      # 一条规则在参考书一半以上的场级窗口上都会响：这位作者的常态，只作提示
RULE_ENDING_DIMENSIONS: frozenset[str] = frozenset({"summary_ending", "ending_drive", "false_poetic_closure"})


@dataclass(frozen=True)
class RuleCalibration:
    """按绑定的参考书校准 21 维规则。

    * ``habitual_needles``：参考作者每万字用到 ``RULE_NEEDLE_HABIT_PER_10K`` 次以上的词表词——在这位作者
      的手里不是毛病，规则不再按它们提示（稿子里的密度到了参考的 ``RULE_NEEDLE_OVERUSE_FACTOR`` 倍且至少
      ``RULE_NEEDLE_OVERUSE_MIN_COUNT`` 次才算过量，照提示）。只校准「命中即毛病」的词表（``FAULT_LEXICONS``）；
      抉择 / 压力 / 代价 / 收尾动作这些「缺席才是毛病」的词表不动。
    * ``habitual_dimensions``：在参考书一半以上的场级窗口上都会响的规则（收尾三条只在真实的章尾上量）——
      这位作者的常态，发现降为 ``info`` 并带 ``calibrated`` 说明，不再当修订项。
    """

    source: str = "default"  # default | reference
    habitual_needles: frozenset[str] = frozenset()
    habitual_dimensions: frozenset[str] = frozenset()
    needle_rates: Mapping[str, float] = field(default_factory=dict)      # 参考书里每万字的次数（只记 > 0 的）
    dimension_shares: Mapping[str, float] = field(default_factory=dict)  # 参考书窗口里响过的比例
    windows: int = 0
    endings: int = 0
    chars: int = 0

    @property
    def active(self) -> bool:
        return self.source == "reference" and bool(self.habitual_needles or self.habitual_dimensions)

    @property
    def signature(self) -> str:
        if not self.active:
            return "default"
        payload = "|".join(sorted(self.habitual_needles)) + "#" + "|".join(sorted(self.habitual_dimensions))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "windows": self.windows,
            "endings": self.endings,
            "chars": self.chars,
            "habitual_needles": sorted(self.habitual_needles, key=lambda term: (-float(self.needle_rates.get(term) or 0.0), term)),
            "habitual_dimensions": sorted(self.habitual_dimensions),
            "dimension_shares": {key: round(float(value), 3) for key, value in sorted(self.dimension_shares.items())},
        }


DEFAULT_RULE_CALIBRATION = RuleCalibration()
RuleCalibrationResolver = Callable[[SceneCard], "RuleCalibration | None"]


def calibrated_lexicons(calibration: RuleCalibration | None, text: str) -> dict[str, tuple[str, ...]]:
    """规则要读的「命中即毛病」词表，去掉参考作者的常用词（稿子里过量的仍留着）。"""

    lexicons = dict(FAULT_LEXICONS)
    if calibration is None or not calibration.habitual_needles:
        return lexicons
    lowered = str(text or "").lower()
    chars = max(1, len(re.sub(r"\s+", "", str(text or ""))))

    def keep(term: str) -> bool:
        if term not in calibration.habitual_needles:
            return True
        count = lowered.count(term.lower())
        if count < RULE_NEEDLE_OVERUSE_MIN_COUNT:
            return False
        reference = max(float(calibration.needle_rates.get(term) or 0.0), RULE_NEEDLE_HABIT_PER_10K)
        return 10000.0 * count / chars >= RULE_NEEDLE_OVERUSE_FACTOR * reference

    return {name: tuple(term for term in terms if keep(term)) for name, terms in lexicons.items()}


def _apply_dimension_calibration(findings: list[dict[str, Any]], calibration: RuleCalibration | None) -> None:
    if calibration is None or not calibration.habitual_dimensions:
        return
    for finding in findings:
        dimension = str(finding.get("dimension") or "")
        if dimension not in calibration.habitual_dimensions:
            continue
        finding["severity"] = "info"
        finding["calibrated"] = {
            "kind": "dimension_habit",
            "share": round(float(calibration.dimension_shares.get(dimension) or 0.0), 3),
        }


class LiteraryQualityService:
    def __init__(self, session: Session, *, rule_calibration_resolver: RuleCalibrationResolver | None = None) -> None:
        self.session = session
        # 2026-09-22 第三轮：文学质量视图与写作台深改面板用同一份参考书校准（路由层注入 scene_diagnosis 的解析器；
        # 这里不能 import scene_diagnosis——它在本模块之上）
        self._rule_calibration_resolver = rule_calibration_resolver

    def overview(
        self,
        *,
        text_layer: str = "author_draft_preferred",
        chapter_id: str | None = None,
        risk_type: str | None = None,
        min_severity: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if text_layer not in QUALITY_TEXT_LAYERS:
            raise DomainError("LITERARY_QUALITY_LAYER_INVALID", "unsupported literary quality text layer", status_code=400)
        _validate_quality_filters(risk_type=risk_type, min_severity=min_severity)

        items: list[dict[str, Any]] = []
        for chapter in self._chapters():
            if project_id and chapter.project_id != project_id:
                continue
            if chapter_id and chapter.chapter_id != chapter_id:
                continue
            source = self._chapter_source(chapter, text_layer=text_layer)
            if source is not None:
                items.append(self._analyze_item("chapter", chapter.chapter_id, chapter.chapter_id, None, source))
        for scene in self._scenes():
            if project_id and scene.project_id != project_id:
                continue
            if chapter_id and scene.chapter_id != chapter_id:
                continue
            source = self._scene_source(scene, text_layer=text_layer)
            if source is not None:
                items.append(
                    self._analyze_item(
                        "scene",
                        scene.scene_id,
                        scene.chapter_id,
                        scene.scene_id,
                        source,
                        ignored_keys=self._scene_ignored_keys(scene),
                        scene=scene,
                    )
                )

        items = _filter_quality_items(items, risk_type=risk_type, min_severity=min_severity)
        mean_score = round(sum(item["score"] for item in items) / len(items), 4) if items else None
        risk_clusters = _risk_clusters(items, risk_type=risk_type, min_severity=min_severity)
        cross_scene_reuse = _cross_scene_reuse(items)
        return {
            "filters": {
                "text_layer": text_layer,
                "chapter_id": chapter_id,
                "risk_type": risk_type,
                "min_severity": min_severity,
                "project_id": project_id,
            },
            "summary": {
                "object_count": len(items),
                "mean_score": mean_score,
                "high_risk_count": sum(1 for item in items if item["score"] < 0.72),
                "model_voice_count": sum(1 for item in items if item["signals"]["model_voice"]["risk"]),
                "risk_cluster_count": len(risk_clusters),
                "cross_scene_reuse_count": len(cross_scene_reuse),
            },
            "items": items,
            "risk_clusters": risk_clusters,
            "fingerprints": [
                {
                    "object_type": item["object_type"],
                    "object_id": item["object_id"],
                    "chapter_id": item["chapter_id"],
                    "scene_id": item["scene_id"],
                    "source_ref": item["source_ref"],
                    "fingerprint": item["fingerprint"],
                }
                for item in items
            ],
            "cross_scene_reuse": cross_scene_reuse,
            "recommended_next_action": _top_recommended_action(items),
        }

    def analyze_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DomainError("LITERARY_QUALITY_TEXT_REQUIRED", "content is required", status_code=400)
        object_type = _optional_string(payload.get("object_type")) or "ad_hoc"
        object_id = _optional_string(payload.get("object_id")) or "scratch"
        chapter_id = _optional_string(payload.get("chapter_id")) or object_id
        scene_id = _optional_string(payload.get("scene_id"))
        scene = self.session.get(SceneCard, scene_id) if scene_id else None
        item = self._analyze_item(
            object_type,
            object_id,
            chapter_id,
            scene_id,
            {
                "text_layer": "ad_hoc",
                "source_ref": _optional_string(payload.get("source_ref")) or f"ad_hoc:{object_id}",
                "content": content,
            },
            ignored_keys=self._scene_ignored_keys(scene),
            scene=scene,
        )
        return {
            **item,
            "content": content,
            "span_findings": _span_findings(content, item["findings"]),
            "risk_clusters": _risk_clusters([item]),
            "recommended_next_action": item["recommended_next_action"],
        }

    def chapter_set_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        chapter_ids = _string_list(payload.get("chapter_ids"))
        if not chapter_ids:
            raise DomainError("LITERARY_QUALITY_CHAPTER_SET_REQUIRED", "chapter_ids are required", status_code=400)
        protected_terms = _string_list(payload.get("protected_terms"))
        text_layer = _optional_string(payload.get("text_layer")) or "author_draft_preferred"
        if text_layer not in QUALITY_TEXT_LAYERS:
            raise DomainError("LITERARY_QUALITY_LAYER_INVALID", "unsupported literary quality text layer", status_code=400)

        chapters: list[ChapterGoal] = []
        for chapter_id in chapter_ids:
            chapter = self.session.get(ChapterGoal, chapter_id)
            if chapter is None or chapter.trashed_flag:
                raise DomainError("LITERARY_QUALITY_CHAPTER_NOT_FOUND", f"chapter {chapter_id} not found", status_code=404)
            chapters.append(chapter)

        chapter_items: list[dict[str, Any]] = []
        scene_items: list[dict[str, Any]] = []
        source_rows: list[dict[str, Any]] = []
        for chapter in chapters:
            chapter_source = self._chapter_source(chapter, text_layer=text_layer)
            if chapter_source is not None:
                chapter_items.append(self._analyze_item("chapter", chapter.chapter_id, chapter.chapter_id, None, chapter_source))
                source_rows.append(
                    {
                        "object_type": "chapter",
                        "object_id": chapter.chapter_id,
                        "chapter_id": chapter.chapter_id,
                        "scene_id": None,
                        "source_ref": chapter_source["source_ref"],
                        "text_layer": chapter_source["text_layer"],
                        "content": chapter_source["content"],
                    }
                )
            for scene in self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id == chapter.chapter_id, SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
            ).scalars().all():
                scene_source = self._scene_source(scene, text_layer=text_layer)
                if scene_source is None:
                    continue
                scene_items.append(
                    self._analyze_item(
                        "scene",
                        scene.scene_id,
                        chapter.chapter_id,
                        scene.scene_id,
                        scene_source,
                        ignored_keys=self._scene_ignored_keys(scene),
                    )
                )
                source_rows.append(
                    {
                        "object_type": "scene",
                        "object_id": scene.scene_id,
                        "chapter_id": chapter.chapter_id,
                        "scene_id": scene.scene_id,
                        "source_ref": scene_source["source_ref"],
                        "text_layer": scene_source["text_layer"],
                        "content": scene_source["content"],
                    }
                )

        all_items = [*chapter_items, *scene_items]
        mean_score = round(sum(item["score"] for item in all_items) / len(all_items), 4) if all_items else None
        payoff_reveal_checks = _chapter_set_payoff_reveal_checks(chapters, source_rows)
        safety_findings = _reference_safety_findings(source_rows, protected_terms)
        repeated_patterns = _chapter_set_repeated_patterns(source_rows, scene_items)
        scores = _chapter_set_scores(
            mean_score, payoff_reveal_checks, safety_findings, len(chapters),
            source_rows=source_rows, chapters=chapters, protected_terms=protected_terms,
        )
        return {
            "chapter_ids": chapter_ids,
            "summary": {
                "chapter_count": len(chapters),
                "scene_count": len(scene_items),
                "mean_score": mean_score,
                "high_risk_count": sum(1 for item in all_items if item["score"] < 0.72),
                "repeated_pattern_count": len(repeated_patterns),
                "reference_safety_finding_count": len(safety_findings),
            },
            "scores": scores,
            "chapters": chapter_items,
            "scenes": scene_items,
            "risk_clusters": _risk_clusters(all_items),
            "repeated_patterns": repeated_patterns,
            "payoff_reveal_checks": payoff_reveal_checks,
            "reference_safety_findings": safety_findings,
            "recommended_next_action": _top_recommended_action(all_items),
        }

    def _analyze_item(
        self,
        object_type: str,
        object_id: str,
        chapter_id: str,
        scene_id: str | None,
        source: dict[str, str],
        *,
        ignored_keys: Iterable[str] = (),
        scene: SceneCard | None = None,
    ) -> dict[str, Any]:
        # 作者稿是 HTML：按可见文字算，否则「第一句」里带着 <p>，同一条发现在写作台和这里 id 不同
        text = plain_manuscript_text(source["content"] or "")
        calibration: RuleCalibration | None = None
        if scene is not None and self._rule_calibration_resolver is not None:
            try:
                calibration = self._rule_calibration_resolver(scene)
            except Exception:  # noqa: BLE001 — 校准失败按房风词表看
                _LOGGER.debug("rule calibration unavailable for %s", scene.scene_id, exc_info=True)
                calibration = None
        signals, raw_findings = analyze_literary_quality(text, calibration=calibration)
        all_findings = _enrich_findings(
            raw_findings,
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            source_ref=source["source_ref"],
            ignored_keys=ignored_keys,
        )
        # 作者在写作台忽略过的发现不再列出（分数照算——分数是文本的事实，忽略是作者的决定）
        findings = [finding for finding in all_findings if not finding.get("ignored")]
        ignored_findings = [finding for finding in all_findings if finding.get("ignored")]
        fingerprint = fingerprint_literary_quality(text)
        raw_score = round(
            sum(signals[dimension]["score"] * DIMENSION_WEIGHTS[dimension] for dimension in QUALITY_DIMENSIONS),
            4,
        )
        automated_assessment = automated_diagnostic_assessment(
            text,
            raw_diagnostic_score=raw_score,
        )
        return {
            "object_type": object_type,
            "object_id": object_id,
            "chapter_id": chapter_id,
            "scene_id": scene_id,
            "text_layer": source["text_layer"],
            "source_ref": source["source_ref"],
            "score": automated_assessment["score"],
            "automated_assessment": automated_assessment,
            "signals": signals,
            "findings": findings,
            "ignored_findings": [
                {"signal_id": finding["signal_id"], "dimension": finding["dimension"], "label": finding["label"]}
                for finding in ignored_findings
            ],
            "ignored_count": len(ignored_findings),
            "open_dimensions": list(dict.fromkeys(str(finding.get("dimension") or "") for finding in findings)),
            "rule_calibration": calibration.as_dict() if calibration is not None and calibration.active else None,
            "fingerprint": fingerprint,
            "recommended_next_action": _recommended_next_action(findings, signals),
        }

    @staticmethod
    def _scene_ignored_keys(scene: SceneCard | None) -> list[str]:
        if scene is None:
            return []
        return [str(key) for key in (scene.deep_review_ignored_keys_json or []) if str(key)]

    def _chapters(self) -> list[ChapterGoal]:
        return self.session.execute(
            select(ChapterGoal)
            .where(ChapterGoal.trashed_flag == 0)
            .order_by(ChapterGoal.chapter_id.asc())
        ).scalars().all()

    def _scenes(self) -> list[SceneCard]:
        return self.session.execute(
            select(SceneCard)
            .where(SceneCard.trashed_flag == 0)
            .order_by(SceneCard.chapter_id.asc(), SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()

    def _chapter_source(self, chapter: ChapterGoal, *, text_layer: str) -> dict[str, str] | None:
        if text_layer == "author_draft_preferred":
            draft = self._current_author_draft("chapter", chapter.chapter_id)
            if draft is not None:
                return {
                    "text_layer": "author_draft",
                    "source_ref": f"author_draft:{draft.draft_id}",
                    "content": draft.content or "",
                }
        if text_layer == "runtime_final_scene":
            return None

        if text_layer in {"author_draft_preferred", "runtime", "chapter_memory_final"}:
            memory = self._final_chapter_memory(chapter.chapter_id)
            if memory is not None:
                return {
                    "text_layer": "chapter_memory_final",
                    "source_ref": f"chapter_memory:{memory.row_id}",
                    "content": memory.content or "",
                }
            if text_layer == "chapter_memory_final":
                return None

        if text_layer in {"author_draft_preferred", "runtime", "chapter_assembled"}:
            assembled = self._assembled_chapter_text(chapter.chapter_id)
            if assembled:
                return {
                    "text_layer": "chapter_assembled",
                    "source_ref": f"chapter_assembled:{chapter.chapter_id}",
                    "content": assembled,
                }
        return None

    def _scene_source(self, scene: SceneCard, *, text_layer: str) -> dict[str, str] | None:
        if text_layer == "author_draft_preferred":
            draft = self._current_author_draft("scene", scene.scene_id)
            if draft is not None:
                return {
                    "text_layer": "author_draft",
                    "source_ref": f"author_draft:{draft.draft_id}",
                    "content": draft.content or "",
                }
        if text_layer not in {"author_draft_preferred", "runtime", "runtime_final_scene"}:
            return None

        final_scene = self._final_scene(scene.scene_id)
        if final_scene is None:
            return None
        return {
            "text_layer": "runtime_final_scene",
            "source_ref": f"final_scene:{final_scene.row_id}",
            "content": final_scene.content or "",
        }

    def _current_author_draft(self, object_type: str, object_id: str) -> AuthorDraft | None:
        return self.session.execute(
            select(AuthorDraft)
            .where(
                AuthorDraft.object_type == object_type,
                AuthorDraft.object_id == object_id,
                AuthorDraft.status == "current",
            )
            .order_by(AuthorDraft.updated_at.desc(), AuthorDraft.draft_id.desc())
        ).scalars().first()

    def _final_scene(self, scene_id: str) -> FinalScene | None:
        state = self.session.get(SceneRunState, scene_id)
        if state is not None and state.current_final_scene_row_id:
            pointed = self.session.get(FinalScene, state.current_final_scene_row_id)
            if pointed is not None and pointed.scene_id == scene_id:
                return pointed
        return self.session.execute(
            select(FinalScene)
            .where(FinalScene.scene_id == scene_id)
            .order_by(FinalScene.created_at.desc(), FinalScene.row_id.desc())
        ).scalars().first()

    def _final_chapter_memory(self, chapter_id: str) -> ChapterMemory | None:
        state = self.session.get(ChapterState, chapter_id)
        if state is not None and state.last_final_memory_row_id:
            pointed = self.session.get(ChapterMemory, state.last_final_memory_row_id)
            if pointed is not None and pointed.chapter_id == chapter_id and pointed.aggregate_stage == "final":
                return pointed
        return self.session.execute(
            select(ChapterMemory)
            .where(
                ChapterMemory.chapter_id == chapter_id,
                ChapterMemory.aggregate_stage == "final",
                ChapterMemory.active_flag == 1,
            )
            .order_by(ChapterMemory.created_at.desc(), ChapterMemory.row_id.desc())
        ).scalars().first()

    def _assembled_chapter_text(self, chapter_id: str) -> str:
        parts: list[str] = []
        scenes = self.session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()
        for scene in scenes:
            final_scene = self._final_scene(scene.scene_id)
            if final_scene is not None and final_scene.content:
                parts.append(final_scene.content)
        return "\n\n".join(parts)


def analyze_literary_quality(
    text: str,
    *,
    external_signals: dict[str, dict[str, Any]] | None = None,
    calibration: RuleCalibration | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """21 维规则。``calibration``（有风格绑定时由 scene_diagnosis 按参考书算出）：参考作者的常用词从「命中即
    毛病」的词表里去掉，作者常态的维度降为 ``info``——没有校准时就是房风词表，行为与从前逐字相同。"""

    normalized = _compact_ws(text)
    signals: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, str]] = []
    lex = calibrated_lexicons(calibration, normalized) if calibration is not None and calibration.active else dict(FAULT_LEXICONS)

    _add_term_signal(
        signals,
        findings,
        "model_voice",
        normalized,
        lex["model_voice"],
        issue="Possible model voice or generic emotional shortcut.",
        recommendation="Replace abstract realization with a concrete choice, gesture, or sensory consequence.",
    )
    _add_image_signal(signals, findings, normalized, terms=lex["image"])
    _add_repetitive_action_signal(signals, findings, normalized, terms=lex["repetitive_action"])
    _add_template_action_reuse_signal(signals, findings, normalized)
    _add_image_field_reuse_signal(signals, findings, normalized, terms=lex["atmospheric_image"])
    _add_syntax_monotony_signal(signals, findings, normalized)
    _add_self_repetition_signal(signals, findings, normalized)
    _add_false_clarity_signal(signals, findings, normalized, terms=lex["false_clarity"])
    _add_valid_ambiguity_signal(signals, findings, normalized)
    _add_expository_dialogue_signal(signals, findings, normalized, terms=lex["expository_dialogue"])
    _add_dialogue_as_report_signal(signals, findings, normalized, terms=lex["report_dialogue"])
    _add_absence_signal(
        signals,
        findings,
        "no_choice_scene",
        normalized,
        CHOICE_TERMS,
        issue="The passage does not show a clear choice on the page.",
        recommendation="Give the character two incompatible options and make one option visibly cost something.",
    )
    _add_summary_ending_signal(signals, findings, normalized, terms=lex["summary_ending"])
    _add_absence_signal(
        signals,
        findings,
        "choice_pressure",
        normalized,
        PRESSURE_TERMS,
        issue="The choice lacks visible pressure or price.",
        recommendation="State the tradeoff through action: what is lost, risked, or refused because of the choice.",
    )
    _add_ending_drive_signal(signals, findings, normalized)
    _add_painless_scene_signal(signals, findings, normalized)
    _add_conflict_too_clean_signal(
        signals, findings, normalized, conflict_terms=lex["conflict"], reconciliation_terms=lex["reconciliation"]
    )
    _add_decorative_imagery_signal(signals, findings, normalized, terms=lex["decorative_image"])
    _add_over_explained_motive_signal(signals, findings, normalized, terms=lex["motive_explanation"])
    _add_false_poetic_closure_signal(signals, findings, normalized, terms=lex["poetic_closure"])
    _add_perception_filter_signal(signals, findings, normalized, terms=lex["perception_filter"])

    if external_signals:
        for dim, signal in external_signals.items():
            if dim in QUALITY_DIMENSIONS:
                signals[dim] = signal

    _apply_dimension_calibration(findings, calibration)
    for dimension in QUALITY_DIMENSIONS:
        signals.setdefault(dimension, {"risk": False, "score": 1.0, "evidence": ""})
    evidence_sufficiency = automated_evidence_sufficiency(normalized)
    signals[AUTOMATED_EVIDENCE_SIGNAL] = {
        "risk": evidence_sufficiency < 1.0,
        "score": evidence_sufficiency,
        "evidence": _evidence_sufficiency_label(normalized),
        "diagnostic_only": True,
        "human_judgment_required": True,
    }
    return signals, findings


def _add_self_repetition_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    """Detect exact sentence reuse inside one passage.

    Cross-scene repetition can still arrive through ``external_signals``.  This
    local guard catches the higher-confidence failure where a generated scene
    repeats the same substantive sentence verbatim; previously the dimension
    existed but remained permanently clean without an external detector.
    """

    normalized_sentences: dict[str, list[str]] = {}
    for sentence in _sentences(text):
        compact = re.sub(r"[^A-Za-z0-9\u3400-\u9fff]+", "", sentence).casefold()
        if len(compact) < 10:
            continue
        normalized_sentences.setdefault(compact, []).append(sentence.strip())
    # 两次完全重复可能是有意的回环、人物口头禅或首尾照应，不能因为系统
    # 支持任意参考风格就把这种作者选择一概当成 AI 痕迹。三次及以上才作为
    # 高置信机械复读；更细腻的重复判断留给盲评。
    repeated = [items for items in normalized_sentences.values() if len(items) >= 3]
    if not repeated:
        signals["self_repetition"] = {"risk": False, "score": 1.0, "evidence": ""}
        return

    duplicate_excess = sum(len(items) - 1 for items in repeated)
    evidence = repeated[0][0]
    score = max(0.0, 1.0 - 0.35 * duplicate_excess)
    signals["self_repetition"] = {
        "risk": True,
        "score": round(score, 4),
        "evidence": evidence,
    }
    findings.append(
        _finding(
            "self_repetition",
            "revision",
            "The passage repeats a substantive sentence verbatim.",
            evidence,
            "Keep the fact once; replace the repeated sentence with a new consequence, reaction, or information beat.",
            needle=evidence,
        )
    )


def fingerprint_literary_quality(text: str) -> dict[str, Any]:
    normalized = _compact_ws(text)
    sentences = _sentences(normalized)
    action_templates = Counter(_action_template(sentence) for sentence in sentences)
    action_templates.pop("", None)
    syntax_shapes = Counter(_syntax_pattern(sentence) for sentence in sentences)
    syntax_shapes.pop("", None)

    lowered = normalized.lower()
    image_fields: Counter[str] = Counter()
    for term in tuple(dict.fromkeys((*IMAGE_TERMS, *ATMOSPHERIC_IMAGE_TERMS))):
        if re.fullmatch(r"[a-z]+", term):
            count = len(re.findall(rf"\b{re.escape(term)}\b", lowered))
        else:
            count = lowered.count(term.lower())
        if count:
            image_fields[term] = count

    dialogue_exposition: Counter[str] = Counter()
    for dialogue in _dialogue_spans(normalized):
        for term in EXPOSITORY_DIALOGUE_TERMS:
            if term.lower() in dialogue.lower():
                dialogue_exposition[term] += 1

    return {
        "action_templates": _top_counter(action_templates, limit=8),
        "image_fields": _top_counter(image_fields, limit=10),
        "syntax_shapes": _top_counter(syntax_shapes, limit=8),
        "false_clarity": [term for term in FALSE_CLARITY_TERMS if term.lower() in lowered],
        "summary_ending": [term for term in SUMMARY_ENDING_TERMS if term.lower() in _ending_slice(normalized).lower()],
        "dialogue_exposition": _top_counter(dialogue_exposition, limit=6),
        "choice_pressure": [term for term in PRESSURE_TERMS if term.lower() in lowered],
        "perception_filter": [term for term in PERCEPTION_FILTER_TERMS if term.lower() in lowered],
    }


ADVERSARIAL_DIMS: tuple[str, ...] = (
    "model_voice",
    "false_clarity",
    "over_explained_motive",
    "template_action_reuse",
    "syntax_monotony",
    "repetitive_action",
    "image_homogeneity",
    "image_field_reuse",
    "decorative_imagery",
    "false_poetic_closure",
    "expository_dialogue",
    "dialogue_as_report",
    "perception_filter",
    "self_repetition",
    # §4/§8: structural cost & conflict dimensions
    "painless_scene",
    "no_choice_scene",
    "choice_pressure",
    "conflict_too_clean",
)


def adversarial_rank_score(
    text: str,
    *,
    weights: dict[str, float] | None = None,
) -> float:
    """Diagnostic floor score across adversarial dimensions.

    Higher means fewer *known deterministic failure signals*; it does not mean
    better literature.  The score is evidence-adjusted and capped below 1.0 so
    a keyword/action-word bundle cannot masquerade as a human upper-bound
    judgment.  Candidate terminal selection remains a human decision.
    """
    if not text or not text.strip():
        return 0.0
    effective = weights if weights is not None else DIMENSION_WEIGHTS
    signals, _ = analyze_literary_quality(text)
    total_w = sum(effective.get(d, 0.0) for d in ADVERSARIAL_DIMS)
    raw_score = 1.0 if total_w <= 0.0 else round(
        sum(signals.get(d, {}).get("score", 1.0) * effective.get(d, 0.0) for d in ADVERSARIAL_DIMS) / total_w,
        4,
    )
    return automated_diagnostic_assessment(
        text,
        raw_diagnostic_score=raw_score,
    )["score"]


def automated_evidence_sufficiency(text: str) -> float:
    """Estimate whether there is enough prose to interpret heuristic signals.

    This intentionally measures evidence volume/shape, not literary merit.
    Short cue lists such as "必须选择，付出代价，她推开门" cannot produce a
    high-confidence automated judgment merely by containing expected words.
    """
    normalized = _compact_ws(text)
    if not normalized:
        return 0.0
    substantive_chars = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", normalized))
    sentence_count = len(_sentences(normalized))
    char_coverage = min(1.0, substantive_chars / AUTOMATED_EVIDENCE_TARGET_CHARS)
    sentence_coverage = min(1.0, sentence_count / AUTOMATED_EVIDENCE_TARGET_SENTENCES)
    # Either enough sustained prose *or* enough sentence-level structure makes
    # the diagnostic usable.  Requiring both would unfairly punish a deliberate
    # long-sentence style; the human-evidence ceiling still prevents either
    # shape from being mistaken for literary excellence.
    return round(max(char_coverage, sentence_coverage), 4)


def automated_diagnostic_assessment(
    text: str,
    *,
    raw_diagnostic_score: float,
) -> dict[str, Any]:
    """Attach honest scope and a human-evidence ceiling to an automatic score."""
    if not text or not text.strip():
        evidence_sufficiency = 0.0
        score = 0.0
    else:
        evidence_sufficiency = automated_evidence_sufficiency(text)
        evidence_multiplier = 0.35 + (0.65 * evidence_sufficiency)
        score = round(
            min(
                AUTOMATED_DIAGNOSTIC_CEILING,
                max(0.0, min(1.0, float(raw_diagnostic_score))) * evidence_multiplier,
            ),
            4,
        )
    return {
        "score": score,
        "raw_diagnostic_score": round(max(0.0, min(1.0, float(raw_diagnostic_score))), 4),
        "evidence_sufficiency": evidence_sufficiency,
        "automated_ceiling": AUTOMATED_DIAGNOSTIC_CEILING,
        "scope": "diagnostic_floor_only",
        "human_judgment_required": True,
        "policy_evidence_eligible": False,
        "upper_bound_requires": "frozen_hidden_human_evidence",
    }


def _evidence_sufficiency_label(text: str) -> str:
    substantive_chars = len(re.findall(r"[A-Za-z0-9\u4e00-\u9fff]", text))
    sentence_count = len(_sentences(text))
    return (
        f"{substantive_chars} substantive characters; {sentence_count} sentence units; "
        f"targets={AUTOMATED_EVIDENCE_TARGET_CHARS}/{AUTOMATED_EVIDENCE_TARGET_SENTENCES}"
    )


def candidate_dispersion(texts: list[str]) -> float:
    """Average pairwise Jaccard distance between fingerprint token sets. 0 = identical."""
    if len(texts) < 2:
        return 0.0
    fingerprints: list[set[str]] = []
    for text in texts:
        fp = fingerprint_literary_quality(text)
        tokens: set[str] = set()
        for key in ("action_templates", "image_fields", "syntax_shapes"):
            for row in fp.get(key, []):
                tokens.add(f"{key}:{row.get('value', '')}")
        fingerprints.append(tokens)
    distances: list[float] = []
    for i in range(len(fingerprints)):
        for j in range(i + 1, len(fingerprints)):
            union = fingerprints[i] | fingerprints[j]
            intersection = fingerprints[i] & fingerprints[j]
            distances.append(1.0 - (len(intersection) / len(union)) if union else 0.0)
    return round(sum(distances) / len(distances), 4) if distances else 0.0


def _validate_quality_filters(*, risk_type: str | None, min_severity: str | None) -> None:
    if risk_type and risk_type not in QUALITY_DIMENSIONS:
        raise DomainError("LITERARY_QUALITY_RISK_TYPE_INVALID", "unsupported literary quality risk_type", status_code=400)
    if min_severity and min_severity not in SEVERITY_RANK:
        raise DomainError("LITERARY_QUALITY_SEVERITY_INVALID", "unsupported literary quality min_severity", status_code=400)


def _filter_quality_items(
    items: list[dict[str, Any]],
    *,
    risk_type: str | None,
    min_severity: str | None,
) -> list[dict[str, Any]]:
    threshold = SEVERITY_RANK[min_severity] if min_severity else None
    filtered: list[dict[str, Any]] = []
    for item in items:
        if risk_type and not item.get("signals", {}).get(risk_type, {}).get("risk"):
            continue
        if risk_type:
            focused_findings = [finding for finding in item.get("findings") or [] if finding.get("dimension") == risk_type]
            item = {
                **item,
                "recommended_next_action": _recommended_next_action(focused_findings, item.get("signals") or {}),
            }
        if threshold is not None and not any(
            SEVERITY_RANK.get(finding.get("severity"), 99) <= threshold
            for finding in item.get("findings") or []
        ):
            continue
        filtered.append(item)
    return filtered


def _enrich_findings(
    findings: list[dict[str, str]],
    *,
    object_type: str,
    object_id: str,
    chapter_id: str,
    scene_id: str | None,
    source_ref: str,
    ignored_keys: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Findings in the unified shape (Chinese text, stable ``signal_id``), tagged with the object.

    ``quality_signal_id`` is kept as an alias of ``signal_id`` — it is the column name a
    passage patch candidate records for the handoff. ``ignored`` marks the findings the
    author dismissed in the writer's deep drawer (the scene's ignore list holds ids).
    """

    ignored = {str(key) for key in ignored_keys or [] if str(key)}
    enriched: list[dict[str, Any]] = []
    for finding in findings:
        unified = unify_rule_finding(finding)
        enriched.append(
            {
                **unified,
                "quality_signal_id": unified["signal_id"],
                "object_type": object_type,
                "object_id": object_id,
                "chapter_id": chapter_id,
                "scene_id": scene_id,
                "source_ref": source_ref,
                "target_text_ref": source_ref,
                "ignored": unified["signal_id"] in ignored,
            }
        )
    return enriched


def _span_findings(content: str, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for finding in findings:
        span = _locate_finding_span(
            content,
            str(finding.get("evidence_excerpt") or ""),
            needle=str(finding.get("needle") or ""),
        )
        if span is None:
            continue
        start, end, evidence = span
        spans.append(
            {
                "dimension": finding.get("dimension") or "unknown",
                "label": finding.get("label") or dimension_label(str(finding.get("dimension") or "")),
                "severity": finding.get("severity") or "revision",
                "start": start,
                "end": end,
                "evidence": evidence,
                "issue": finding.get("issue") or "",
                "recommended_action": finding.get("recommendation") or "",
                "quality_signal_id": finding.get("quality_signal_id"),
                "signal_id": finding.get("signal_id") or finding.get("quality_signal_id"),
                "ignored": bool(finding.get("ignored")),
            }
        )
    return spans


def _locate_finding_span(content: str, evidence: str, *, needle: str = "") -> tuple[int, int, str] | None:
    source = str(content or "")
    if not source:
        return None
    # 命中的词 / 句本身是最准的锚点：先钉它，再退到证据窗口
    exact = str(needle or "").strip()
    if exact:
        index = source.find(exact)
        if index >= 0:
            return index, index + len(exact), exact
    for fragment in _evidence_fragments(evidence):
        index = source.find(fragment)
        if index >= 0:
            return index, index + len(fragment), fragment
    compact_source = _compact_ws(source)
    if compact_source and compact_source in source:
        index = source.find(compact_source)
        return index, index + len(compact_source), compact_source
    fallback = source.strip()
    if not fallback:
        return None
    index = source.find(fallback)
    fragment = fallback[: min(len(fallback), 120)]
    return index, index + len(fragment), fragment


def _evidence_fragments(evidence: str) -> list[str]:
    raw = str(evidence or "").strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
        candidates.extend(part.strip() for part in raw.split(" / ") if part.strip())
    fragments: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in fragments:
            fragments.append(candidate)
        trimmed = candidate.strip(" .。!?！？,，;；")
        if trimmed and trimmed not in fragments:
            fragments.append(trimmed)
    return sorted(fragments, key=len, reverse=True)


def _recommended_next_action(findings: list[dict[str, Any]], signals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    open_findings = [finding for finding in findings if not finding.get("ignored")]
    if not open_findings:
        reason = "未发现明显文学质量风险。" if not findings else "剩下的发现都已在写作台忽略。"
        return {"action": "none", "label": "暂无动作", "reason": reason}
    priority = sorted(open_findings, key=lambda item: (SEVERITY_RANK.get(item.get("severity"), 99), item.get("dimension", "")))[0]
    dimension = priority.get("dimension") or "quality"
    # 动作名保留旧值（前端与测试都认它）；落点是写作台的深改姿态，带着这条发现的 signal_id 过去
    action = "open_deepdesk_patch"
    label = "去写作台处理这一处"
    signal_id = priority.get("signal_id") or priority.get("quality_signal_id")
    return {
        "action": action,
        "label": label,
        "risk_type": dimension,
        "signal_id": signal_id,
        "quality_signal_id": signal_id,
        "target_text_ref": priority.get("target_text_ref"),
        "source_excerpt": priority.get("evidence_excerpt") or signals.get(dimension, {}).get("evidence") or "",
        "reason": priority.get("issue") or "",
        "recommendation": priority.get("recommendation") or "",
    }


def _top_recommended_action(items: list[dict[str, Any]]) -> dict[str, Any]:
    actions = [item.get("recommended_next_action") for item in items if item.get("recommended_next_action", {}).get("action") != "none"]
    if not actions:
        return {"action": "none", "label": "暂无动作", "reason": "当前列表没有明显文学质量风险。"}
    return actions[0]


def _risk_clusters(
    items: list[dict[str, Any]],
    *,
    risk_type: str | None = None,
    min_severity: str | None = None,
) -> list[dict[str, Any]]:
    threshold = SEVERITY_RANK[min_severity] if min_severity else None
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        for finding in item.get("findings") or []:
            dimension = finding.get("dimension") or "unknown"
            if risk_type and dimension != risk_type:
                continue
            if threshold is not None and SEVERITY_RANK.get(finding.get("severity"), 99) > threshold:
                continue
            cluster = grouped.setdefault(
                dimension,
                {
                    "dimension": dimension,
                    "severity": finding.get("severity") or "revision",
                    "count": 0,
                    "object_refs": [],
                    "quality_signal_ids": [],
                    "representative_evidence": finding.get("evidence_excerpt") or "",
                    "recommendation": finding.get("recommendation") or "",
                },
            )
            cluster["count"] += 1
            ref = {
                "object_type": item["object_type"],
                "object_id": item["object_id"],
                "chapter_id": item["chapter_id"],
                "scene_id": item["scene_id"],
                "source_ref": item["source_ref"],
            }
            if ref not in cluster["object_refs"]:
                cluster["object_refs"].append(ref)
            signal_id = finding.get("quality_signal_id")
            if signal_id and signal_id not in cluster["quality_signal_ids"]:
                cluster["quality_signal_ids"].append(signal_id)
            if SEVERITY_RANK.get(finding.get("severity"), 99) < SEVERITY_RANK.get(cluster["severity"], 99):
                cluster["severity"] = finding.get("severity")
    return sorted(grouped.values(), key=lambda row: (-row["count"], SEVERITY_RANK.get(row["severity"], 99), row["dimension"]))


def _cross_scene_reuse(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        if item.get("object_type") != "scene":
            continue
        chapter_id = item.get("chapter_id") or ""
        fingerprint = item.get("fingerprint") or {}
        for cluster_type, key in (
            ("action_template", "action_templates"),
            ("image_field", "image_fields"),
            ("syntax_shape", "syntax_shapes"),
        ):
            for token_row in fingerprint.get(key) or []:
                token = token_row.get("value")
                if not token:
                    continue
                group = grouped.setdefault(
                    (chapter_id, cluster_type, token),
                    {
                        "chapter_id": chapter_id,
                        "cluster_type": cluster_type,
                        "token": token,
                        "count": 0,
                        "object_ids": [],
                        "source_refs": [],
                        "severity": "revision" if cluster_type != "image_field" else "taste",
                    },
                )
                group["count"] += int(token_row.get("count") or 0)
                if item["object_id"] not in group["object_ids"]:
                    group["object_ids"].append(item["object_id"])
                if item["source_ref"] not in group["source_refs"]:
                    group["source_refs"].append(item["source_ref"])
    rows = [row for row in grouped.values() if len(row["object_ids"]) >= 2]
    return sorted(rows, key=lambda row: (-row["count"], row["chapter_id"], row["cluster_type"], row["token"]))


def _chapter_set_payoff_reveal_checks(chapters: list[ChapterGoal], source_rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_chapter: dict[str, str] = {}
    for row in source_rows:
        by_chapter[row["chapter_id"]] = "\n".join([by_chapter.get(row["chapter_id"], ""), str(row.get("content") or "")])
    forced_choice_ids: list[str] = []
    cost_ids: list[str] = []
    next_pull_ids: list[str] = []
    for chapter in chapters:
        source_text = _compact_ws(by_chapter.get(chapter.chapter_id, ""))
        fallback_text = _compact_ws(
            "\n".join(
                [
                    chapter.chapter_goal or "",
                    chapter.main_plot_push or "",
                    chapter.emotional_target or "",
                    chapter.ending_effect or "",
                    by_chapter.get(chapter.chapter_id, ""),
                ]
            )
        )
        combined_text = source_text or fallback_text
        if _first_present_term(combined_text, CHOICE_TERMS):
            forced_choice_ids.append(chapter.chapter_id)
        if _first_present_term(combined_text, COST_TERMS) or _first_present_term(combined_text, PRESSURE_TERMS):
            cost_ids.append(chapter.chapter_id)
        if _first_non_negated_present_term(_ending_slice(combined_text), CHAPTER_SET_NEXT_PULL_TERMS):
            next_pull_ids.append(chapter.chapter_id)
    ordered_chapter_ids = [chapter.chapter_id for chapter in chapters]
    missing_forced_choice_ids = [chapter_id for chapter_id in ordered_chapter_ids if chapter_id not in forced_choice_ids]
    missing_cost_ids = [chapter_id for chapter_id in ordered_chapter_ids if chapter_id not in cost_ids]
    missing_next_pull_ids = [chapter_id for chapter_id in ordered_chapter_ids if chapter_id not in next_pull_ids]
    missing_payoff_ids = [
        chapter_id
        for chapter_id in ordered_chapter_ids
        if chapter_id in {*missing_forced_choice_ids, *missing_cost_ids, *missing_next_pull_ids}
    ]
    return {
        "has_forced_choice_count": len(forced_choice_ids),
        "has_cost_count": len(cost_ids),
        "has_next_pull_count": len(next_pull_ids),
        "forced_choice_chapter_ids": forced_choice_ids,
        "cost_chapter_ids": cost_ids,
        "next_pull_chapter_ids": next_pull_ids,
        "missing_forced_choice_chapter_ids": missing_forced_choice_ids,
        "missing_cost_chapter_ids": missing_cost_ids,
        "missing_next_pull_chapter_ids": missing_next_pull_ids,
        "missing_payoff_chapter_ids": missing_payoff_ids,
    }


def _reference_safety_findings(source_rows: list[dict[str, Any]], protected_terms: list[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for row in source_rows:
        text = str(row.get("content") or "")
        for term in protected_terms:
            if term and term in text:
                findings.append(
                    {
                        "term": term,
                        "chapter_id": row.get("chapter_id"),
                        "scene_id": row.get("scene_id"),
                        "object_type": row.get("object_type"),
                        "object_id": row.get("object_id"),
                        "source_ref": row.get("source_ref"),
                        "evidence_excerpt": _excerpt(text, term),
                    }
                )
    return findings


def _chapter_set_repeated_patterns(source_rows: list[dict[str, Any]], scene_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in scene_items:
        fingerprint = item.get("fingerprint") or {}
        for cluster_type, key in (
            ("action_template", "action_templates"),
            ("image_field", "image_fields"),
            ("syntax_shape", "syntax_shapes"),
        ):
            for token_row in fingerprint.get(key) or []:
                _add_repeated_pattern(
                    grouped,
                    cluster_type=cluster_type,
                    token=str(token_row.get("value") or ""),
                    count=int(token_row.get("count") or 0),
                    chapter_id=item.get("chapter_id"),
                    object_id=item.get("object_id"),
                )
    for row in source_rows:
        text = str(row.get("content") or "")
        for token, count in _cjk_key_term_counts(text).items():
            _add_repeated_pattern(
                grouped,
                cluster_type="key_term",
                token=token,
                count=count,
                chapter_id=row.get("chapter_id"),
                object_id=row.get("object_id"),
            )
    patterns = [
        row
        for row in grouped.values()
        if row["count"] >= 2 and len(row["chapter_ids"]) >= 2
    ]
    patterns = _dedupe_key_term_substrings(patterns)
    return sorted(patterns, key=lambda row: (-row["count"], row["cluster_type"], row["token"]))[:20]


def _dedupe_key_term_substrings(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """key_term n-gram 去碎片：若较短 token 只作为较长 token 的子串出现（计数相等），
    丢弃较短的，保留最长有意义复用词（如保留「玻璃雨」而非「玻璃」「璃雨」）。"""
    key_terms = [p for p in patterns if p["cluster_type"] == "key_term"]
    drop: set[str] = set()
    for short in key_terms:
        for long in key_terms:
            if short is long:
                continue
            st, lt = short["token"], long["token"]
            if len(lt) > len(st) and st in lt and long["count"] >= short["count"]:
                drop.add(st)
                break
    return [p for p in patterns if not (p["cluster_type"] == "key_term" and p["token"] in drop)]


def _add_repeated_pattern(
    grouped: dict[tuple[str, str], dict[str, Any]],
    *,
    cluster_type: str,
    token: str,
    count: int,
    chapter_id: str | None,
    object_id: str | None,
) -> None:
    if not token or count <= 0:
        return
    row = grouped.setdefault(
        (cluster_type, token),
        {"cluster_type": cluster_type, "token": token, "count": 0, "chapter_ids": [], "object_ids": []},
    )
    row["count"] += count
    if chapter_id and chapter_id not in row["chapter_ids"]:
        row["chapter_ids"].append(chapter_id)
    if object_id and object_id not in row["object_ids"]:
        row["object_ids"].append(object_id)


_CJK_STOP_CHARS = frozenset(
    "的了在和这那他她它我你您们是有就也都要会着过被把对与之而其于以为不没"
    "很又再上下里中来去到说做看想能将已并且或如但因所还只本该些个种样头边时"
)


def _cjk_key_term_counts(text: str) -> Counter[str]:
    """通用跨章关键词词频：滑动 2-4 字 CJK n-gram 计数，过滤纯虚词 gram。

    取代旧的 `玻璃雨/零点/证人/反证/名单` demo 硬编码（对真实新书返回空）。
    改为按词频提取每部作品自己的反复关键词，由调用方 count>=2 且 chapter_ids>=2
    过滤 + 子串去重收敛为有意义的跨章复用词。
    """
    counts: Counter[str] = Counter()
    normalized = _compact_ws(text)
    for token in re.findall(r"[\u4e00-\u9fff]{2,6}", normalized):
        run_len = len(token)
        for size in (2, 3, 4):
            if run_len < size:
                break
            for start in range(run_len - size + 1):
                gram = token[start : start + size]
                if all(ch in _CJK_STOP_CHARS for ch in gram):
                    continue
                counts[gram] += 1
    return counts


def _chapter_set_scores(
    mean_score: float | None,
    payoff_reveal_checks: dict[str, Any],
    safety_findings: list[dict[str, Any]],
    chapter_count: int,
    *,
    source_rows: list[dict[str, Any]] | None = None,
    chapters: list[ChapterGoal] | None = None,
    protected_terms: list[str] | None = None,
) -> dict[str, Any]:
    arc_score = _evaluate_cross_chapter_arc(
        payoff_reveal_checks,
        chapter_count,
        source_rows=source_rows,
        chapters=chapters,
    )
    # 诚实性：未提供受保护词 = 未做参考安全扫描 → 返回 None（前端渲染「—」），
    # 不能把「没扫」伪装成绿色满分 1.0。命中=0.0，传词且干净=1.0。
    if safety_findings:
        reference_safety: float | None = 0.0
    elif protected_terms:
        reference_safety = 1.0
    else:
        reference_safety = None
    return {
        "literary_quality": mean_score,
        "cross_chapter_arc": arc_score,
        "reference_safety": reference_safety,
    }


def _evaluate_cross_chapter_arc(
    payoff_reveal_checks: dict[str, Any],
    chapter_count: int,
    *,
    source_rows: list[dict[str, Any]] | None = None,
    chapters: list[ChapterGoal] | None = None,
) -> float:
    """Enhanced cross-chapter arc evaluation.

    Instead of just counting keyword presence, analyze:
    1. **Arc progression** (0-1): do chapters show different decision / pressure
       patterns?  Same pressure+cost terms everywhere means static arcs.
    2. **Foreshadow health** (0-1): ratio of foreshadow lifecycle signals that
       are "touched" or "resolved" vs stale (open for too many chapters).
    3. **Theme expression variety** (0-1): Shannon entropy over the distribution
       of theme expression levels used across chapters.
    4. **Tension dynamics** (0-1): normalised standard deviation of tension
       targets across chapters (higher = more dynamic, better).

    Final score: weighted average of these 4 sub-scores plus the legacy payoff
    baseline (to stay backward-compatible for projects with no source content).
    """
    denominator = max(1, chapter_count)

    # --- legacy baseline: payoff keyword coverage ---
    payoff_score = (
        payoff_reveal_checks["has_forced_choice_count"]
        + payoff_reveal_checks["has_cost_count"]
        + payoff_reveal_checks["has_next_pull_count"]
    ) / (denominator * 3)

    # When no rich source content is available, fall back to the legacy score
    if not source_rows or not chapters or chapter_count < 2:
        return round(payoff_score, 4)

    # Build per-chapter combined text
    by_chapter: dict[str, str] = {}
    for row in source_rows:
        cid = row.get("chapter_id", "")
        by_chapter[cid] = by_chapter.get(cid, "") + "\n" + str(row.get("content") or "")

    chapter_texts: list[str] = []
    for ch in chapters:
        raw = by_chapter.get(ch.chapter_id, "")
        fallback = "\n".join([
            ch.chapter_goal or "",
            ch.main_plot_push or "",
            ch.emotional_target or "",
            ch.ending_effect or "",
            raw,
        ])
        chapter_texts.append(_compact_ws(raw or fallback))

    arc_progression = _arc_progression_score(chapter_texts)
    foreshadow_health = _foreshadow_health_score(chapter_texts)
    theme_variety = _theme_variety_score(chapters)
    tension_dynamics = _tension_dynamics_score(chapters)

    # Weighted combination: payoff baseline still counts, but structural
    # sub-scores contribute the majority when available.
    combined = (
        0.30 * payoff_score
        + 0.25 * arc_progression
        + 0.15 * foreshadow_health
        + 0.15 * theme_variety
        + 0.15 * tension_dynamics
    )
    return round(combined, 4)


def _arc_progression_score(chapter_texts: list[str]) -> float:
    """Measure how different chapters' pressure/cost term sets are from each other.

    If every chapter uses exactly the same pressure and cost vocabulary the arc
    is *static* (score 0). Maximum diversity across pairs yields score 1.
    """
    if len(chapter_texts) < 2:
        return 1.0

    per_chapter_terms: list[set[str]] = []
    combined_terms = (*PRESSURE_TERMS, *COST_TERMS)
    for text in chapter_texts:
        lowered = text.lower()
        present = {t for t in combined_terms if t.lower() in lowered}
        per_chapter_terms.append(present)

    # Average pairwise Jaccard distance
    distances: list[float] = []
    for i in range(len(per_chapter_terms)):
        for j in range(i + 1, len(per_chapter_terms)):
            union = per_chapter_terms[i] | per_chapter_terms[j]
            intersection = per_chapter_terms[i] & per_chapter_terms[j]
            if union:
                distances.append(1.0 - len(intersection) / len(union))
            else:
                distances.append(0.0)
    return sum(distances) / len(distances) if distances else 0.0


def _foreshadow_health_score(chapter_texts: list[str]) -> float:
    """Score how well foreshadowing terms progress across chapters.

    A "foreshadow" is a next-pull term from an earlier chapter that later
    appears as a choice/cost term. Healthy lifecycle: open -> touched -> resolved.
    Terms that stay open for more than half the chapters are considered stale.
    """
    if len(chapter_texts) < 2:
        return 1.0

    # Track which chapters each next-pull term appears in
    pull_term_chapters: dict[str, list[int]] = {}
    for idx, text in enumerate(chapter_texts):
        lowered = text.lower()
        for term in CHAPTER_SET_NEXT_PULL_TERMS:
            if term.lower() in lowered:
                pull_term_chapters.setdefault(term, []).append(idx)

    # Track which chapters resolve or touch those terms via choice/cost vocab
    resolution_terms = (*CHOICE_TERMS, *COST_TERMS)
    resolution_chapters: dict[str, list[int]] = {}
    for idx, text in enumerate(chapter_texts):
        lowered = text.lower()
        for term in resolution_terms:
            if term.lower() in lowered:
                resolution_chapters.setdefault(term, []).append(idx)

    if not pull_term_chapters:
        return 0.5  # no foreshadowing detected at all: neutral

    total_pulls = len(pull_term_chapters)
    healthy = 0
    stale_threshold = len(chapter_texts) // 2

    for term, intro_chapters in pull_term_chapters.items():
        first_intro = min(intro_chapters)
        # Check if any resolution term appears in a later chapter
        resolved = False
        for _res_term, res_chs in resolution_chapters.items():
            if any(ch > first_intro for ch in res_chs):
                resolved = True
                break
        if resolved:
            healthy += 1
        elif len(chapter_texts) - first_intro > stale_threshold:
            # Stale: introduced early, never resolved, still too many chapters ago
            pass  # counts against health
        else:
            healthy += 0.5  # recently introduced, not yet stale

    return healthy / total_pulls if total_pulls > 0 else 0.5


def _theme_variety_score(chapters: list[ChapterGoal]) -> float:
    """Shannon entropy over theme/emotional expression levels across chapters.

    Uses ``emotional_target`` from chapter goals as the expression channel.
    Higher entropy = theme expressed through more diverse emotional tones.
    """
    if not chapters:
        return 0.0

    levels: list[str] = []
    for ch in chapters:
        target = (ch.emotional_target or "").strip().lower()
        if target:
            levels.append(target)

    if len(levels) < 2:
        return 0.5  # too little data to assess diversity

    # Shannon entropy, normalised to [0, 1]
    counter = Counter(levels)
    total = len(levels)
    entropy = -sum((c / total) * math.log2(c / total) for c in counter.values() if c > 0)
    max_entropy = math.log2(len(counter)) if len(counter) > 1 else 1.0
    return entropy / max_entropy if max_entropy > 0 else 0.0


def _tension_dynamics_score(chapters: list[ChapterGoal]) -> float:
    """Normalised standard deviation of tension indicators across chapters.

    Uses ``ending_effect`` length as a rough proxy for tension target intensity
    (chapters ending on high-tension cliffhangers tend to have longer ending
    effect descriptions). A flat curve (all same length) scores low; varied
    curves score higher.
    """
    if not chapters or len(chapters) < 2:
        return 0.5

    # Proxy: length of ending_effect + main_plot_push as tension indicator
    values: list[float] = []
    for ch in chapters:
        tension_proxy = len(ch.ending_effect or "") + len(ch.main_plot_push or "")
        values.append(float(tension_proxy))

    if not values:
        return 0.5

    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.5

    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(variance)
    # Coefficient of variation, capped at 1.0
    cv = min(std / mean, 1.0) if mean > 0 else 0.0
    return cv


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in values:
            values.append(item.strip())
    return values


def _top_counter(counter: Counter[str], *, limit: int) -> list[dict[str, Any]]:
    return [
        {"value": value, "count": count}
        for value, count in counter.most_common(limit)
        if value and count > 0
    ]


def _optional_string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _add_term_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    dimension: str,
    text: str,
    terms: tuple[str, ...],
    *,
    issue: str,
    recommendation: str,
) -> None:
    term = _first_present_term(text, terms)
    if term:
        evidence = _excerpt(text, term)
        signals[dimension] = {"risk": True, "score": 0.0, "evidence": evidence}
        findings.append(_finding(dimension, "revision", issue, evidence, recommendation, needle=term))
        return
    signals[dimension] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_absence_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    dimension: str,
    text: str,
    terms: tuple[str, ...],
    *,
    issue: str,
    recommendation: str,
) -> None:
    if _first_present_term(text, terms):
        signals[dimension] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, "")
    signals[dimension] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(_finding(dimension, "revision", issue, evidence, recommendation, anchor="scene"))


def _add_expository_dialogue_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = EXPOSITORY_DIALOGUE_TERMS,
) -> None:
    for dialogue in _dialogue_spans(text):
        term = _first_present_term(dialogue, terms)
        if term:
            evidence = _excerpt(dialogue, term)
            signals["expository_dialogue"] = {"risk": True, "score": 0.0, "evidence": evidence}
            findings.append(
                _finding(
                    "expository_dialogue",
                    "revision",
                    "Dialogue is carrying explanation instead of pressure or subtext.",
                    evidence,
                    "Move the fact into gesture, silence, contradiction, or a partial answer.",
                    needle=term,
                )
            )
            return
    signals["expository_dialogue"] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_dialogue_as_report_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = REPORT_DIALOGUE_TERMS,
) -> None:
    for dialogue in _dialogue_spans(text):
        term = _first_present_term(dialogue, terms)
        if not term:
            continue
        evidence = _excerpt(dialogue, term)
        signals["dialogue_as_report"] = {"risk": True, "score": 0.0, "evidence": evidence}
        findings.append(
            _finding(
                "dialogue_as_report",
                "revision",
                "Dialogue is reporting plot information instead of changing pressure between characters.",
                evidence,
                "Turn the fact into a withheld answer, accusation, bargaining chip, or relationship wound.",
                needle=term,
            )
        )
        return
    signals["dialogue_as_report"] = {"risk": False, "score": 1.0, "evidence": ""}


# §8: conflict-too-clean detection terms
CONFLICT_TERMS = (
    "争吵", "怒", "愤怒", "质问", "反对", "拒绝", "吼", "骂", "指责", "控诉",
    "冲突", "对峙", "反驳", "顶嘴", "争执", "不满", "激烈", "摔", "推开",
    "quarrel", "anger", "confront", "refuse", "accuse", "clash", "demand",
)
RECONCILIATION_TERMS = (
    "理解", "原谅", "接受", "点头", "叹气", "妥协", "释然", "握手", "拥抱",
    "和好", "笑了", "放下", "释怀", "微笑", "柔和", "温和", "缓和", "谅解",
    "understand", "forgive", "accept", "nod", "sigh", "compromise", "smile",
    "relent", "soften", "reconcile",
)

# 「命中即毛病」的词表——按参考书校准词表时只动这些；抉择 / 压力 / 代价 / 收尾动作是「缺席才是毛病」，不动
FAULT_LEXICONS: dict[str, tuple[str, ...]] = {
    "model_voice": MODEL_VOICE_TERMS,
    "expository_dialogue": EXPOSITORY_DIALOGUE_TERMS,
    "report_dialogue": REPORT_DIALOGUE_TERMS,
    "summary_ending": SUMMARY_ENDING_TERMS,
    "repetitive_action": REPETITIVE_ACTION_TERMS,
    "image": IMAGE_TERMS,
    "atmospheric_image": ATMOSPHERIC_IMAGE_TERMS,
    "false_clarity": FALSE_CLARITY_TERMS,
    "decorative_image": DECORATIVE_IMAGE_TERMS,
    "motive_explanation": MOTIVE_EXPLANATION_TERMS,
    "poetic_closure": POETIC_CLOSURE_TERMS,
    "perception_filter": PERCEPTION_FILTER_TERMS,
    "conflict": CONFLICT_TERMS,
    "reconciliation": RECONCILIATION_TERMS,
}


def _add_conflict_too_clean_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    conflict_terms: tuple[str, ...] = CONFLICT_TERMS,
    reconciliation_terms: tuple[str, ...] = RECONCILIATION_TERMS,
) -> None:
    """Blueprint §8: detect conflict that resolves too cleanly.

    Checks if conflict-indicating terms and reconciliation terms co-occur
    within a short text window (~300 chars), and if reconciliation terms
    outnumber or match conflict terms — this signals 'too-clean' resolution.
    """
    lowered = text.lower()
    conflict_hits = [t for t in conflict_terms if t.lower() in lowered]
    reconcile_hits = [t for t in reconciliation_terms if t.lower() in lowered]

    if not conflict_hits or not reconcile_hits:
        signals["conflict_too_clean"] = {"risk": False, "score": 1.0, "evidence": ""}
        return

    # Check proximity: do conflict and reconciliation co-occur within 300 chars?
    proximity_count = 0
    for ct in conflict_hits:
        ct_lower = ct.lower()
        pos = 0
        while True:
            idx = lowered.find(ct_lower, pos)
            if idx < 0:
                break
            window = lowered[max(0, idx - 150):idx + len(ct_lower) + 150]
            if any(rt.lower() in window for rt in reconcile_hits):
                proximity_count += 1
            pos = idx + len(ct_lower)

    if proximity_count == 0:
        signals["conflict_too_clean"] = {"risk": False, "score": 1.0, "evidence": ""}
        return

    # If reconciliation ≥ conflict in a scene, the conflict resolves too cleanly
    if len(reconcile_hits) >= len(conflict_hits):
        evidence = _excerpt(text, conflict_hits[0])
        score = max(0.0, 1.0 - (len(reconcile_hits) / max(len(conflict_hits), 1)) * 0.5)
        signals["conflict_too_clean"] = {"risk": True, "score": round(score, 2), "evidence": evidence}
        findings.append(
            _finding(
                "conflict_too_clean",
                "revision",
                (
                    f"Conflict resolves too cleanly: {len(conflict_hits)} conflict signals "
                    f"vs {len(reconcile_hits)} reconciliation signals in close proximity. "
                    "Characters understand each other too quickly."
                ),
                evidence,
                "Leave residual friction: an unspoken resentment, a half-lie accepted, "
                "or agreement that costs something the character didn't want to give.",
                needle=conflict_hits[0],
            )
        )
    else:
        signals["conflict_too_clean"] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_painless_scene_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    choice_term = _first_present_term(text, CHOICE_TERMS)
    cost_term = _first_present_term(text, COST_TERMS)
    if cost_term:
        signals["painless_scene"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, choice_term or "")
    signals["painless_scene"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "painless_scene",
            "revision",
            "The scene may be structurally clear but emotionally painless: no concrete loss, betrayal, risk, or sacrifice is visible.",
            evidence,
            "Make the character pay on the page: lose a resource, damage a bond, hide something, betray a value, or choose one safety over another.",
            needle=choice_term or "",
            anchor="text" if choice_term else "scene",
        )
    )


def _add_decorative_imagery_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = DECORATIVE_IMAGE_TERMS,
) -> None:
    hits = [term for term in terms if term.lower() in text.lower()]
    if len(hits) < 2:
        signals["decorative_imagery"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, hits[0])
    signals["decorative_imagery"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "decorative_imagery",
            "taste",
            "The image work is carrying atmosphere more than action, relationship, information, or theme pressure.",
            evidence,
            "Keep the strongest image only if it changes what a character does, reveals a withheld fact, or sharpens the cost of the choice.",
            needle=hits[0],
        )
    )


def _add_over_explained_motive_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = MOTIVE_EXPLANATION_TERMS,
) -> None:
    term = _first_present_term(text, terms)
    if not term:
        signals["over_explained_motive"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, term)
    signals["over_explained_motive"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "over_explained_motive",
            "revision",
            "The motive is being explained directly instead of being pressured into action or omission.",
            evidence,
            "Let the reader infer motive from what the character refuses, delays, hides, or chooses under pressure.",
            needle=term,
        )
    )


def _add_false_poetic_closure_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = POETIC_CLOSURE_TERMS,
) -> None:
    ending = _ending_slice(text)
    term = _first_present_term(ending, terms)
    if not term or _first_present_term(ending, POETIC_CLOSURE_ACTION_TERMS):
        signals["false_poetic_closure"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(ending, term)
    signals["false_poetic_closure"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "false_poetic_closure",
            "revision",
            "The ending closes with poetic certainty rather than a hard next-scene action.",
            evidence,
            "End on a visible action, object transfer, refusal, departure, or irreversible reveal instead of abstract resonance.",
            needle=term,
            anchor="ending",
        )
    )


def _add_perception_filter_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = PERCEPTION_FILTER_TERMS,
) -> None:
    term = _first_present_term(text, terms)
    if not term:
        signals["perception_filter"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, term)
    signals["perception_filter"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "perception_filter",
            "revision",
            "The narration routes sensation through a perception verb instead of rendering the stimulus directly.",
            evidence,
            "Delete the perception verb and let the stimulus land as action, object, or sensory detail.",
            needle=term,
        )
    )


def _add_image_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = IMAGE_TERMS,
) -> None:
    counts = Counter()
    lowered = text.lower()
    for term in terms:
        if re.fullmatch(r"[a-z]+", term):
            counts[term] = len(re.findall(rf"\b{re.escape(term)}\b", lowered))
        else:
            counts[term] = lowered.count(term.lower())
    term, count = max(counts.items(), key=lambda item: item[1]) if counts else ("", 0)
    if count >= 3:
        evidence = _excerpt(text, term)
        signals["image_homogeneity"] = {"risk": True, "score": 0.0, "evidence": evidence}
        findings.append(
            _finding(
                "image_homogeneity",
                "taste",
                f"The same image field repeats too often: {term}.",
                evidence,
                "Keep one anchor image, then vary texture through action, object, temperature, sound, or spatial detail.",
                needle=term,
            )
        )
        return
    signals["image_homogeneity"] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_repetitive_action_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = REPETITIVE_ACTION_TERMS,
) -> None:
    counts = Counter()
    lowered = text.lower()
    for term in terms:
        if re.fullmatch(r"[a-z]+", term):
            counts[term] = len(re.findall(rf"\b{re.escape(term)}\b", lowered))
        else:
            counts[term] = lowered.count(term.lower())
    term, count = max(counts.items(), key=lambda item: item[1]) if counts else ("", 0)
    if count < 4:
        signals["repetitive_action"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, term)
    signals["repetitive_action"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "repetitive_action",
            "revision",
            f"The same action beat repeats too often: {term}.",
            evidence,
            "Keep the strongest beat, then replace the others with a choice, object movement, silence, or changed blocking.",
            needle=term,
        )
    )


def _add_template_action_reuse_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    sentences = _sentences(text)
    templates = Counter(_action_template(sentence) for sentence in sentences)
    templates.pop("", None)
    template, count = max(templates.items(), key=lambda item: item[1]) if templates else ("", 0)
    if count < 3:
        signals["template_action_reuse"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    matching = [sentence for sentence in sentences if _action_template(sentence) == template]
    evidence = " / ".join(matching)[:180]
    signals["template_action_reuse"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "template_action_reuse",
            "revision",
            "Action beats are repeating the same sentence template.",
            evidence,
            "Keep one beat, then vary blocking, object movement, silence, or relational pressure.",
            needle=matching[0] if matching else "",
        )
    )


def _add_image_field_reuse_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = ATMOSPHERIC_IMAGE_TERMS,
) -> None:
    lowered = text.lower()
    hits: list[str] = []
    for term in terms:
        count = len(re.findall(rf"\b{re.escape(term)}\b", lowered)) if re.fullmatch(r"[a-z]+", term) else lowered.count(term.lower())
        hits.extend([term] * count)
    if len(hits) < 4 or len(set(hits)) < 3:
        signals["image_field_reuse"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, hits[0])
    signals["image_field_reuse"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "image_field_reuse",
            "taste",
            f"The atmospheric image field is doing too much repeated work: {', '.join(sorted(set(hits))[:5])}.",
            evidence,
            "Let one image carry mood, and make the next beat change through an object, decision, or body position.",
            needle=hits[0],
        )
    )


def _add_syntax_monotony_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    sentences = _sentences(text)
    patterns = Counter(_syntax_pattern(sentence) for sentence in sentences)
    patterns.pop("", None)
    pattern, count = max(patterns.items(), key=lambda item: item[1]) if patterns else ("", 0)
    if count < 3:
        signals["syntax_monotony"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    matching = [sentence for sentence in sentences if _syntax_pattern(sentence) == pattern]
    evidence = " / ".join(matching)[:180]
    signals["syntax_monotony"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "syntax_monotony",
            "taste",
            "Several consecutive sentences use the same syntactic shape.",
            evidence,
            "Break the rhythm with a short sentence, a withheld response, or a sentence that starts from consequence instead of gesture.",
            needle=matching[0] if matching else "",
        )
    )


def _add_false_clarity_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = FALSE_CLARITY_TERMS,
) -> None:
    term = _first_present_term(text, terms)
    if not term:
        signals["false_clarity"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(text, term)
    signals["false_clarity"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "false_clarity",
            "revision",
            "The passage tells the reader what became clear instead of letting pressure reveal it.",
            evidence,
            "Cut the explanation and let a choice, refusal, object transfer, or silence create the reader's inference.",
            needle=term,
        )
    )


def _add_valid_ambiguity_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    signals["valid_ambiguity"] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_summary_ending_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
    *,
    terms: tuple[str, ...] = SUMMARY_ENDING_TERMS,
) -> None:
    ending = _ending_slice(text)
    term = _first_present_term(ending, terms)
    if term:
        evidence = _excerpt(ending, term)
        signals["summary_ending"] = {"risk": True, "score": 0.0, "evidence": evidence}
        findings.append(
            _finding(
                "summary_ending",
                "revision",
                "The ending explains the effect instead of landing on an action or image.",
                evidence,
                "Cut the summarizing sentence and end on the last irreversible action.",
                needle=term,
                anchor="ending",
            )
        )
        return
    signals["summary_ending"] = {"risk": False, "score": 1.0, "evidence": ""}


def _add_ending_drive_signal(
    signals: dict[str, dict[str, Any]],
    findings: list[dict[str, str]],
    text: str,
) -> None:
    ending = _ending_slice(text)
    has_action = _first_present_term(ending, ENDING_ACTION_TERMS)
    if has_action and not signals.get("summary_ending", {}).get("risk"):
        signals["ending_drive"] = {"risk": False, "score": 1.0, "evidence": ""}
        return
    evidence = _excerpt(ending, "")
    signals["ending_drive"] = {"risk": True, "score": 0.0, "evidence": evidence}
    findings.append(
        _finding(
            "ending_drive",
            "revision",
            "The final beat does not push the reader into the next scene.",
            evidence,
            "End with a new action, object movement, arrival, departure, reveal, or refusal.",
            anchor="ending",
        )
    )


def _finding(
    dimension: str,
    severity: str,
    issue: str,
    evidence: str,
    recommendation: str,
    *,
    needle: str = "",
    anchor: str = "text",
) -> dict[str, str]:
    """One rule finding.

    ``needle`` is the exact text the rule matched (a term, the repeated sentence, …) —
    the workbench pins the finding to that string in the manuscript and the finding's
    stable id is derived from it. Findings without a needle carry an ``anchor``:
    ``scene`` (an absence — nothing in the text to point at) or ``ending`` (the last
    beat). ``text`` with an empty needle means "locate by the excerpt".
    """

    return {
        "dimension": dimension,
        "severity": severity,
        "issue": issue,
        "evidence_excerpt": evidence,
        "recommendation": recommendation,
        "needle": needle or "",
        "anchor": anchor if anchor in FINDING_ANCHORS else "text",
    }


def _first_present_term(text: str, terms: tuple[str, ...]) -> str:
    lowered = text.lower()
    for term in terms:
        normalized_term = term.lower()
        if normalized_term.strip() and normalized_term in lowered:
            return term
    return ""


def _first_non_negated_present_term(text: str, terms: tuple[str, ...]) -> str:
    lowered = text.lower()
    for term in terms:
        normalized_term = term.lower().strip()
        if not normalized_term:
            continue
        start = 0
        while True:
            index = lowered.find(normalized_term, start)
            if index == -1:
                break
            context = lowered[max(0, index - 18) : index]
            if not any(marker in context for marker in NEGATED_ACTION_CONTEXT_TERMS):
                return term
            start = index + len(normalized_term)
    return ""


def _dialogue_spans(text: str) -> list[str]:
    spans = re.findall(r'"([^"]+)"', text, flags=re.DOTALL)
    spans.extend(re.findall(r"“([^”]+)”", text, flags=re.DOTALL))
    spans.extend(re.findall(r"「([^」]+)」", text, flags=re.DOTALL))
    return [_compact_ws(span) for span in spans if span.strip()]


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。！？.!?]+", text) if part.strip()]


def _action_template(sentence: str) -> str:
    normalized = _compact_ws(sentence)
    lowered = normalized.lower()
    if re.search(r"^[^，。！？]{1,10}低头看着[^，。！？]*，[^。！？]*沉默了片刻", normalized):
        return "pronoun_looked_at_object_then_silence"
    for action in ("turned", "looked", "nodded", "smiled", "sighed", "转身", "低头", "看着", "点头", "笑", "叹气", "沉默"):
        if action in lowered:
            return f"action:{action}"
    return ""


def _syntax_pattern(sentence: str) -> str:
    normalized = _compact_ws(sentence)
    if not normalized:
        return ""
    if re.match(r"^[她他][^，,]{2,24}[，,][^，,]{2,24}$", normalized):
        return "pronoun_phrase_comma_phrase"
    comma_count = normalized.count("，") + normalized.count(",")
    length_bucket = min(len(normalized) // 12, 4)
    return f"comma:{comma_count}:len:{length_bucket}"


def _ending_slice(text: str) -> str:
    normalized = _compact_ws(text)
    if len(normalized) <= 180:
        return normalized
    return normalized[-180:]


def _excerpt(text: str, needle: str) -> str:
    normalized = _compact_ws(text)
    if not normalized:
        return ""
    if not needle:
        return normalized[:180]
    index = normalized.lower().find(needle.lower())
    if index < 0:
        return normalized[:180]
    start = max(0, index - 70)
    end = min(len(normalized), index + len(needle) + 70)
    return normalized[start:end]


def _compact_ws(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()
