from __future__ import annotations

import contextlib
import hashlib
from contextvars import ContextVar
import json
import logging
import math
import re
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    LlmCall,
    SceneCard,
    SceneDraft,
    SceneRunState,
)
from novel_system.services.errors import DomainError
from novel_system.services.author_instructions import render_author_note_instruction
from novel_system.services.hash_engine import canonical_json
from novel_system.services.literary_quality import (
    DIMENSION_WEIGHTS,
    analyze_literary_quality,
)
from novel_system.services.llm_audit import error_audit_summary, sanitize_audit_summary
from novel_system.services.llm_client import LLMResponse
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.llm_task_runner import (
    CONTINUITY_BUDGET_ERROR_CODE,
    CONTINUITY_BUDGET_MESSAGE,
    SCENE_SPLIT_RECOMMENDATION,
    LLMNodeContinuityError,
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.qc_constraints import (
    constraint_terms,
    contains_forbidden_term,
    source_field_satisfied,
)
from novel_system.services.style_reference.injection import (
    InjectionService,
    ordered_character_ids,
)
from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_reference.runtime_contract import (
    DRAFT_MODE_NEUTRAL_FIRST,
    DRAFT_MODE_STYLE_FIRST,
    contract_profile_objects,
    extract_style_generation_context,
)
from novel_system.services.style_prompt_injection import (  # noqa: F401  (re-export for callers/tests)
    PLACEMENT_USER_TAIL,
    ROLE_DRAFT,
    ROLE_REVISE,
    STYLE_USER_TAIL_KEY,
    STYLED_GATE_UNAVAILABLE_VERDICT,
    apply_style_user_tail,
    frozen_situation_tags,
    inject_style_reference_prefix,
)
from novel_system.services.style_reference import readings as style_readings
from novel_system.services.style_reference import style_step
from novel_system.services.style_reference.fidelity import within_author_range

_LOGGER = logging.getLogger(__name__)
_PRE_DISPATCH_ACCOUNTING_REJECTIONS = frozenset(
    {
        "LLM_SCENE_TOKEN_BUDGET_UNINITIALIZED",
        "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED",
        "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED",
        "LLM_SCENE_CALL_IN_FLIGHT",
        "LLM_ACCOUNTING_INTEGRITY_BLOCKED",
    }
)


def _counts_as_business_attempt(exc: Exception) -> bool:
    """A pre-dispatch accounting rejection is evidence, not a generation attempt."""
    original = getattr(exc, "original_error", None)
    code = str(getattr(exc, "error_code", None) or getattr(exc, "code", None) or "")
    original_code = str(
        getattr(original, "error_code", None) or getattr(original, "code", None) or ""
    )
    return not (
        isinstance(exc, LLMAccountingRejected)
        or isinstance(original, LLMAccountingRejected)
        or code in _PRE_DISPATCH_ACCOUNTING_REJECTIONS
        or original_code in _PRE_DISPATCH_ACCOUNTING_REJECTIONS
    )


class SceneGenerationPostprocessError(ValueError):
    """Stable typed failure emitted after a provider call settled successfully."""

    def __init__(self, *, llm_call_id: str | None, message: str) -> None:
        super().__init__(message)
        self.llm_call_id = llm_call_id
        self.code = "SCENE_GENERATION_RESPONSE_INVALID"
        self.error_code = self.code


@dataclass(slots=True)
class NeutralGenerationResult:
    row_id: str
    content: str
    llm_call_id: str
    bundle_id: str
    bundle_hash: str
    execution_step_key: str | None = None
    artifact_execution_id: str | None = None
    # 2026-09-12 风格直起:本步位实际的起草方式与首稿 notices / 门裁决(neutral_first 下为空)。
    draft_mode: str = DRAFT_MODE_NEUTRAL_FIRST
    notices: list[dict[str, Any]] = field(default_factory=list)
    styled_draft_gate: dict[str, Any] | None = None


@dataclass(slots=True)
class StyleGenerationResult:
    row_id: str
    content: str
    llm_call_id: str
    bundle_id: str
    bundle_hash: str
    execution_step_key: str | None = None
    artifact_execution_id: str | None = None
    ranking_audit: dict[str, Any] | None = None
    # 2026-09 风格模仿 v2（W5，规格 §2.W5.6）：风格链路的非静默提示。每项
    # ``{"code", "message", "severity", ...}``；code 见 STYLE_NOTICE_*。同一份也写进
    # AttemptTracker.details_json["notices"]，供场景运行 / 工作台响应回读。
    notices: list[dict[str, Any]] = field(default_factory=list)
    # 生成侧 styled-draft gate 的诊断字典（qc_engine.run_styled_draft_style_gate 的返回；
    # 无绑定时 None）。orchestrator 据此对 near_final_rewrite 的抄袭裁决采取行动。
    styled_draft_gate: dict[str, Any] | None = None
    # 风格参考 v3（P5b）：「首稿即风格稿」（读数在作者范围内，风格步不调模型）的产品沿用首稿的调用谱系——
    # llm_call_id / execution_step_key 是首稿那次调用的；检查点校验据此认它（见 orchestrator）。
    lineage: str | None = None
    # 风格步的决定（读数、要改的维、采用 / 保留首稿的原因），同一份也写进 AttemptTracker.details_json.style_step。
    style_step: dict[str, Any] | None = None


JSON_SCHEMA_INSTRUCTION = "Return JSON that matches the structured schema exactly."

# ---------------------------------------------------------------------------
# 2026-09 风格模仿 v2（W5）：风格链路 notices
# ---------------------------------------------------------------------------
STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL = "STYLE_DRAFT_FALLBACK_NEUTRAL"
# 2026-09-12 风格直起:首稿已按参考作者手笔直接起草(信息级,不是警告)。
STYLE_NOTICE_FIRST_DRAFT = "STYLE_FIRST_DRAFT"
STYLE_NOTICE_INJECTION_MISS = "STYLE_INJECTION_MISS"
STYLE_NOTICE_INJECTION_DEGRADED = "STYLE_INJECTION_DEGRADED"
STYLE_NOTICE_PLAGIARISM_HIT = "STYLE_PLAGIARISM_HIT"
STYLE_NOTICE_BANNED_TERM_HIT = "STYLE_BANNED_TERM_HIT"
# styled-draft gate 自身没跑成（校验异常 / 契约损坏 / 参考书已删）：抄袭 / 禁用词检查
# 没有执行过，不能与「无绑定」混为一谈。
STYLE_NOTICE_GATE_UNAVAILABLE = "STYLE_GATE_UNAVAILABLE"
# 风格参考 v3（P5b）：风格步按读数决定——首稿在作者范围内不调模型（信息级）；定向修改不更像 / 没过抄袭门时保留首稿
# （信息级）；软补丁让稿子离作者更远时退回补丁前的稿子（信息级，STYLE_PATCH_REVERTED 由编排器写）。
STYLE_NOTICE_FIRST_DRAFT_ACCEPTED = "STYLE_FIRST_DRAFT_ACCEPTED"
STYLE_NOTICE_REVISION_REJECTED = "STYLE_REVISION_REJECTED"
STYLE_NOTICE_PATCH_REVERTED = "STYLE_PATCH_REVERTED"
# 注入适配器审计里的提示（inject.render / inject.selection 的 notices）原样用它们的码翻成风格链路 notice：
# 书在冻结后改过（按当前索引挑样例）/ 云策略不让发原文 / 书不在了 / 书还没有样例窗口。
STYLE_NOTICE_REFERENCE_BOOK_CHANGED = "STYLE_REFERENCE_BOOK_CHANGED"
STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED = "STYLE_REFERENCE_SAMPLES_BLOCKED"
STYLE_NOTICE_REFERENCE_BOOK_MISSING = "STYLE_REFERENCE_BOOK_MISSING"
STYLE_NOTICE_REFERENCE_NO_WINDOWS = "STYLE_REFERENCE_NO_WINDOWS"
_RENDER_AUDIT_NOTICE_MESSAGES: dict[str, str] = {
    STYLE_NOTICE_REFERENCE_BOOK_CHANGED: "参考书的段落在冻结之后改过，本场按当前的窗口索引挑了样例。",
    STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED: "这本参考书的云端策略不允许把原文发给当前模型，本场只用了文风卡与声音特征，没有原文样例。",
    STYLE_NOTICE_REFERENCE_BOOK_MISSING: "绑定的参考书已不在书库里，本场没有原文样例。",
    STYLE_NOTICE_REFERENCE_NO_WINDOWS: "参考书还没有可用的样例窗口（段落分类未完成或正文太少），本场没有原文样例。",
}
STYLE_NOTICE_CODES: frozenset[str] = frozenset(
    {
        STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL,
        STYLE_NOTICE_INJECTION_MISS,
        STYLE_NOTICE_INJECTION_DEGRADED,
        STYLE_NOTICE_PLAGIARISM_HIT,
        STYLE_NOTICE_BANNED_TERM_HIT,
        STYLE_NOTICE_GATE_UNAVAILABLE,
        STYLE_NOTICE_FIRST_DRAFT,
        STYLE_NOTICE_FIRST_DRAFT_ACCEPTED,
        STYLE_NOTICE_REVISION_REJECTED,
        STYLE_NOTICE_PATCH_REVERTED,
        *_RENDER_AUDIT_NOTICE_MESSAGES,
    }
)
# 生成侧要跑 styled-draft gate 的阶段：落库内容是 provider 的风格化输出、且会成为终稿
# 候选的每一个阶段。style_draft 回退中性稿时不跑（内容是已批准的中性稿）。
_STYLED_GATE_GENERATION_STAGES: frozenset[str] = frozenset(
    {"style_draft", "near_final_rewrite"}
)
# 风格链路 notices 落在哪些 AttemptTracker.step 上（API 回读按 bundle 合并这几步的最近
# 一次 completed 尝试）：style_draft 与 near_final_rewrite（step=scene_literary_rewrite）。
# 2026-09-12 风格直起:style_first 下中性步位的首稿也带 notices(首稿直起 / 注入未命中 /
# 抄袭或禁用词命中);neutral_first 下该步没有 notices,合并时自然为空。
# 风格参考 v3（P5b）：软补丁的去留（保留 / 退回）记在 step=style_patch_keep 的尝试上。
STYLE_PATCH_KEEP_STEP = style_step.STYLE_PATCH_KEEP_STEP
STYLE_NOTICE_ATTEMPT_STEPS: tuple[str, ...] = (
    "neutral_draft",
    "style_draft",
    STYLE_PATCH_KEEP_STEP,
    "scene_literary_rewrite",
)
# style_first 下 style_draft 步位看到的来源稿标签(模板按标签切换「重组」与「复读」)。
FIRST_DRAFT_SOURCE_LABEL = "First Draft (already in the reference author's hand)"
NEUTRAL_DRAFT_SOURCE_LABEL = "Approved Neutral Draft"
# AttemptTracker.details_json.content_source 标记:首稿直起 / 复读稿回退到首稿。
STYLE_FIRST_DRAFT_CONTENT_SOURCE = "style_first_draft"
FIRST_DRAFT_FALLBACK_CONTENT_SOURCE = "first_draft_fallback"
# 风格参考 v3（P5b）：风格步「首稿即风格稿」产品的谱系标记（StyleGenerationResult.lineage / 检查点描述符）。
LINEAGE_FIRST_DRAFT_ACCEPTED = "first_draft_accepted"
STYLE_STEP_VERSION = "style_step_v1"
_STYLE_NOTICE_SEVERITIES = ("info", "warning", "error", "blocking")


def _source_draft_label(bundle: Mapping[str, Any] | None) -> str:
    """style_draft 步位看到的来源稿标签:style_first → 首稿(复读);否则中性稿(重组)。"""
    return FIRST_DRAFT_SOURCE_LABEL if style_policy_for_bundle(bundle).style_first else NEUTRAL_DRAFT_SOURCE_LABEL


def _source_draft_instruction(bundle: Mapping[str, Any] | None) -> str:
    if style_policy_for_bundle(bundle).style_first:
        return (
            "Revise the first draft one step closer to the reference samples without changing the approved facts; "
            "keep every passage that already sounds like the author."
        )
    return "Apply the style prompt template without changing the approved facts."


def _prompt_carries_style_reference(prompt: Mapping[str, Any] | None) -> bool:
    if not isinstance(prompt, Mapping):
        return False
    audit = prompt.get("_style_reference_runtime_audit")
    if isinstance(audit, Mapping) and str(audit.get("outcome") or "") == "injected":
        return True
    if str(prompt.get(STYLE_USER_TAIL_KEY) or "").strip():
        return True
    return "[STYLE_REFERENCE]" in str(prompt.get("system_prompt") or "")


def style_notice(
    code: str,
    message: str,
    *,
    severity: str = "warning",
    **details: Any,
) -> dict[str, Any]:
    """构造一条风格链路 notice（JSON 友好，供 API 直通）。"""
    if code not in STYLE_NOTICE_CODES:
        raise ValueError(f"unknown style notice code: {code}")
    if severity not in _STYLE_NOTICE_SEVERITIES:
        raise ValueError(f"unknown style notice severity: {severity}")
    notice: dict[str, Any] = {"code": code, "message": message, "severity": severity}
    for key, value in details.items():
        if value is not None:
            notice[key] = value
    return notice


def style_injection_notices(prompt: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """把 ``prompt["_style_reference_runtime_audit"]`` 的注入结果翻译成 notices。

    - 无审计键：没有可用的参考绑定（严格 no-op）→ 无 notice（无参考不是故障）；
    - ``outcome == "miss"``：契约已冻结但没渲染出任何块 → STYLE_INJECTION_MISS；
    - ``outcome in {"degraded", "degraded_budget"}`` → STYLE_INJECTION_DEGRADED。
    """
    if not isinstance(prompt, Mapping):
        return []
    audit = prompt.get("_style_reference_runtime_audit")
    if not isinstance(audit, Mapping):
        return []
    outcome = str(audit.get("outcome") or "")
    if outcome == "miss":
        return [
            style_notice(
                STYLE_NOTICE_INJECTION_MISS,
                "风格参考已绑定，但本次没有渲染出任何可注入的风格块；本稿未受参考风格约束。",
                severity="warning",
                contract_hash=audit.get("contract_hash"),
                profile_ids=list(audit.get("profile_ids") or []),
            )
        ]
    if outcome == "degraded":
        return [
            style_notice(
                STYLE_NOTICE_INJECTION_DEGRADED,
                "风格参考注入失败，已回退到无风格前缀的基础提示；本稿未受参考风格约束。",
                severity="error",
                error_code=audit.get("error_code"),
                runtime_contract_status=audit.get("runtime_contract_status"),
            )
        ]
    if outcome == "degraded_budget":
        return [
            style_notice(
                STYLE_NOTICE_INJECTION_DEGRADED,
                "输入预算不足，风格参考前缀被整体裁掉；本稿未受参考风格约束。",
                severity="warning",
                budget_fit=deepcopy(audit.get("budget_fit")),
            )
        ]
    return render_audit_notices(audit)


def render_audit_notices(audit: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """注入适配器审计里的 ``notices``（书改过 / 原文被云策略挡下 / 书不在 / 没有样例窗口）→ 风格链路 notices。"""
    if not isinstance(audit, Mapping):
        return []
    notices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for code in audit.get("notices") or []:
        code = str(code or "")
        message = _RENDER_AUDIT_NOTICE_MESSAGES.get(code)
        if message is None or code in seen:
            continue
        seen.add(code)
        notices.append(
            style_notice(
                code,
                message,
                severity="warning",
                samples_blocked=audit.get("samples_blocked") if code == STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED else None,
            )
        )
    return notices


_STYLED_GATE_STAGE_LABEL = {
    "neutral_draft": "首稿",
    "style_draft": "风格稿",
    "near_final_rewrite": "准终稿重写稿",
}
_STYLED_GATE_STAGE_CONSEQUENCE = {
    "neutral_draft": "hard_qc 阶段的同一门将升级为人工复核。",
    "style_draft": "该稿不得直接成稿，soft_qc 阶段将升级为人工复核。",
    "near_final_rewrite": "该重写稿已被丢弃，终稿回退为重写前已过 gate 的风格稿。",
}


def _styled_draft_gate_notices(gate: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """styled-draft gate 结果 → notices。

    plagiarism 阻断级；冻结禁用词命中 error 级；gate 自身未能执行（verdict
    ``unavailable``）error 级 STYLE_GATE_UNAVAILABLE。命中计数取 gate 的真实总数
    （``plagiarism_hit_count`` / ``forbidden_hit_count``），不是被截断到 8 条的证据列表长度。
    """
    if not isinstance(gate, Mapping):
        return []
    verdict = str(gate.get("verdict") or "")
    stage = str(gate.get("stage") or "style_draft")
    label = _STYLED_GATE_STAGE_LABEL.get(stage, "风格稿")
    consequence = _STYLED_GATE_STAGE_CONSEQUENCE.get(
        stage, _STYLED_GATE_STAGE_CONSEQUENCE["style_draft"]
    )
    notices: list[dict[str, Any]] = []
    if verdict == STYLED_GATE_UNAVAILABLE_VERDICT:
        notices.append(
            style_notice(
                STYLE_NOTICE_GATE_UNAVAILABLE,
                f"{label}的抄袭 / 冻结禁用词检查未能执行；本稿未经参考来源安全核对，"
                "soft_qc 阶段将要求人工复核。",
                severity="error",
                stage=stage,
                error=gate.get("error"),
                error_code=gate.get("error_code"),
                profile_id=gate.get("profile_id"),
                runtime_contract_hash=gate.get("runtime_contract_hash"),
            )
        )
        return notices
    if verdict == "plagiarism" or gate.get("plagiarism_passed") is False:
        plagiarism_hits = gate.get("plagiarism_hits") or []
        notices.append(
            style_notice(
                STYLE_NOTICE_PLAGIARISM_HIT,
                f"{label}与参考作品原文存在确定性 n-gram 重叠（抄袭红线命中）；{consequence}",
                severity="blocking",
                stage=stage,
                hit_count=int(gate.get("plagiarism_hit_count") or len(plagiarism_hits)),
                profile_id=gate.get("profile_id"),
                runtime_contract_hash=gate.get("runtime_contract_hash"),
            )
        )
    forbidden_hits = gate.get("forbidden_hits") or []
    if forbidden_hits:
        notices.append(
            style_notice(
                STYLE_NOTICE_BANNED_TERM_HIT,
                f"{label}命中参考画像冻结的生成禁用词；soft_qc 阶段将升级为人工复核。",
                severity="error",
                stage=stage,
                hit_count=int(gate.get("forbidden_hit_count") or len(forbidden_hits)),
                terms=[
                    str(hit.get("matched_excerpt") or hit.get("pattern_statement") or "")
                    for hit in forbidden_hits
                    if isinstance(hit, Mapping)
                ][:8],
                profile_id=gate.get("profile_id"),
            )
        )
    return notices


def latest_style_notices(
    session: Session,
    scene_id: str,
    *,
    bundle_id: str | None = None,
) -> list[dict[str, Any]]:
    """回读风格链路写进 AttemptTracker 的 notices（API 直通用）。

    对 ``STYLE_NOTICE_ATTEMPT_STEPS`` 的每一步取最近一次 completed 尝试，按步序合并去重：
    style_draft 的 notices 在前，near_final_rewrite（step=scene_literary_rewrite）在后。
    传 ``bundle_id`` 时只看该 bundle（API 层必须传——一次运行的响应不能带上别的运行的
    notices）；不传则取场景最近一次，仅供直接调用方使用。
    """
    merged: list[dict[str, Any]] = []
    for step in STYLE_NOTICE_ATTEMPT_STEPS:
        stmt = (
            select(AttemptTracker)
            .where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == step,
                AttemptTracker.status == "completed",
            )
            .order_by(AttemptTracker.attempt_id.desc())
        )
        if bundle_id:
            stmt = stmt.where(AttemptTracker.source_bundle_id == bundle_id)
        row = session.execute(stmt).scalars().first()
        if row is None:
            continue
        raw = (row.details_json or {}).get("notices")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict) and item.get("code") and item not in merged:
                merged.append(deepcopy(item))
    return merged
_STYLE_SAFETY_REPAIR_TASK_PROMPT = (
    "Edit the labeled rejected style draft directly. This is a local safety repair, not a new composition. "
    "Preserve its wording, paragraph architecture, reusable style, facts, chronology, and ending wherever they "
    "already pass; change only the exact hard-constraint failures listed below. Return one complete replacement "
    "scene_text and no commentary."
)
# 2026-09-14 风格保真修补:有绑定时修复 / 补丁的附加句——参考是唯一的风格权威,增删文字用作者
# 自己的手段,不用房风的「加动作反应 / 删修饰」。
_STYLE_BOUND_REPAIR_CLAUSE = (
    " The [STYLE_REFERENCE] block above is the style authority: keep the draft in the reference author's "
    "hand, and wherever the repair must add or remove text, do it with that author's own means as the "
    "[风格样例] show (summary, digression, dialogue, description, reflection), never with generic "
    "action-reaction filler; never reuse the samples' sentences, names, or events."
)
_STYLE_DE_TEMPLATE_REPAIR_TASK_PROMPT = (
    "Edit the labeled style draft directly. Apply only the listed de-template corrections while preserving its "
    "facts, chronology, functional paragraph architecture, broad style distribution, distinctive wording, and ending function. "
    "Do not restart the scene, recompose it from a blank page, or rewrite unaffected passages. Return one complete "
    "replacement scene_text and no commentary."
)
ANTI_TEMPLATE_GATE_DIMENSIONS = {
    "model_voice",
    "image_homogeneity",
    "repetitive_action",
    "template_action_reuse",
    "image_field_reuse",
    "syntax_monotony",
    "false_clarity",
    "summary_ending",
    "expository_dialogue",
    "decorative_imagery",
    "dialogue_as_report",
    "over_explained_motive",
    "false_poetic_closure",
    "self_repetition",
}
_STYLE_REWRITE_REGRESSION_TOLERANCE = 0.01


# §6.3 multi-strategy diversification prompts for low-dispersion retry
_DIVERSIFICATION_PROMPT = (
    "[DIVERSIFICATION] 前一轮生成的候选在表达上高度相似。请刻意尝试不同的叙述入口：\n"
    "换一种感官开场（如果之前用了视觉，试听觉或触觉）、\n"
    "换一种时间结构（如果之前是顺叙，试倒叙或插叙的片段）、\n"
    "换一种节奏（如果之前是长句铺陈，试短句切入）。\n"
    "保持场景spec的所有结构要求不变，只改变'怎么去'。\n\n"
)
# §6.3 style emphasis rotation prefixes — rotate which style dimension the LLM focuses on
_STYLE_EMPHASIS_ROTATION: list[str] = [
    (
        # 2026-09-22 风格参考优先:补候选时也不把「不做什么」抬成首要约束——先在心里复读三段样例的
        # 句法与口吻再动笔;禁忌只作校核。
        "[风格强调·样例优先] 本次生成请先在心里复读 [风格样例] 里的三段原文——它的句子怎么起、"
        "在哪儿停、旁白怎么插话、对白怎么接——再动笔,让每一段都像那位作者写的;禁忌模式只用来自检。\n\n"
    ),
    (
        "[风格强调·节奏分布优先] 本次生成关注风格参考中的整体节奏倾向——"
        "句群长短、段落功能与停顿习惯应自然呈现；不要为任何统计数字机械增删标点或拆段。\n\n"
    ),
]


def _progressive_top_up_variants(
    base_temp: float,
) -> list[tuple[float, str | None, str]]:
    """Wave 3（§5.5）渐进补候选的变体轮换：温度加宽 → 发散提示 → 风格侧重轮换。

    返回 (temperature, extra_system_prefix, strategy_label) 序列；补候选按序取用，
    每次只补 1 个。
    """
    variants: list[tuple[float, str | None, str]] = [
        (round(min(2.0, base_temp + 0.15), 3), None, "temperature_widen"),
        (
            round(min(2.0, base_temp + 0.10), 3),
            _DIVERSIFICATION_PROMPT,
            "prompt_variation",
        ),
    ]
    for idx, prefix in enumerate(_STYLE_EMPHASIS_ROTATION):
        variants.append(
            (
                round(min(2.0, base_temp + 0.05 * (idx + 1)), 3),
                prefix,
                f"style_emphasis_{idx}",
            )
        )
    return variants


def versioned_scene_artifact_id(
    prefix: str, scene_id: str, bundle: dict[str, Any]
) -> str:
    bundle_id = str(bundle.get("bundle_id") or "")
    bundle_prefix = f"bundle_{scene_id}_"
    if bundle_id.startswith(bundle_prefix):
        return f"{prefix}_{scene_id}_{bundle_id[len(bundle_prefix):]}"
    if bundle_id == f"bundle_{scene_id}":
        return f"{prefix}_{scene_id}"
    bundle_hash = str(bundle.get("bundle_snapshot_hash") or "")
    suffix = (
        bundle_hash[:12]
        if bundle_hash
        else hashlib.sha256(canonical_json(bundle).encode("utf-8")).hexdigest()[:12]
    )
    return f"{prefix}_{scene_id}_{suffix}"


def _policy_card(policy: Any) -> tuple[Any, dict[str, str]]:
    """策略冻结的画像里的文风卡与作者的 ✓ / ✗（旧画像没有卡 → ``(None, {})``）。"""
    from novel_system.services.style_reference.card import (
        card_from_profile_json,
        line_states_from_profile_json,
    )
    from novel_system.services.style_reference.runtime_contract import contract_layer

    contract = getattr(policy, "contract", None)
    layer = contract_layer(contract if isinstance(contract, Mapping) else None)
    profile = layer.get("profile") if isinstance(layer.get("profile"), Mapping) else {}
    profile_json = profile.get("profile_json") if isinstance(profile.get("profile_json"), Mapping) else {}
    return card_from_profile_json(profile_json), line_states_from_profile_json(profile_json)


def author_note_instruction(author_note: str | None) -> str:
    """Backward-compatible renderer; bundle injection now carries it to every stage."""
    return render_author_note_instruction(author_note)


def _author_note_instruction_for_bundle(
    bundle: dict[str, Any],
    author_note: str | None,
) -> str:
    note = str(author_note or "").strip()
    frozen = str(
        ((bundle.get("snapshot") or {}).get("inline_digests") or {}).get(
            "author_instruction"
        )
        or ""
    )
    return "" if note == frozen else author_note_instruction(author_note)


class SceneGenerationService:
    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        llm_runner: LLMNodeRunner | None = None,
    ) -> None:
        self.session = session
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)
        self._prompt_builder_instance: PromptBuilder | None = None

    def generate_neutral_draft(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        author_note: str | None = None,
    ) -> NeutralGenerationResult:
        """中性步位(``neutral_ready`` 检查点)的起草。

        2026-09-12 风格直起(Step 2):bundle 冻结契约的 ``draft_mode`` 决定这一步位写什么——
        ``neutral_first``:中性稿(现状,阅读对照组);``style_first``:直接以参考作者手笔从
        bundle 写首稿(``style_first_draft`` 模板 + ``[STYLE_REFERENCE]`` 前缀,走 style_draft
        节点路由)。步位、``stage="neutral_draft"`` 行、attempt step、指针、账本字段全部不变。
        """
        with _length_band_slack_for(bundle, self.session.get(SceneCard, scene_id)):
            return self._generate_first_draft(scene_id, bundle, author_note=author_note)

    def _generate_first_draft(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        author_note: str | None = None,
    ) -> NeutralGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        fallback_llm_call_id = f"llm_call_{scene_id}_{uuid.uuid4().hex[:12]}"
        started_at = time.perf_counter()
        prompt: dict[str, Any] | None = None
        # 风格参考 v3:起草方式只看这份 bundle 的 StylePolicy(未绑定 / 旧契约缺键 → neutral_first)
        policy = style_policy_for_bundle(bundle)
        draft_mode = policy.draft_mode
        style_first = policy.style_first
        template_name = "style_first_draft" if style_first else "neutral_draft"
        draft_node_id = "style_draft" if style_first else "neutral_draft"

        try:
            prompt = self._prompt_builder().build(bundle["snapshot"], template_name)
        except Exception as exc:
            self._persist_generation_failure(
                scene=scene,
                state=state,
                bundle=bundle,
                llm_call_id=fallback_llm_call_id,
                step="neutral_draft",
                execution_step_key="neutral_draft",
                started_at=started_at,
                task_config=None,
                prompt=prompt,
                request_summary={},
                exc=exc,
            )
            raise

        # neutral_first(对照组):中性稿固定事件、因果与连续性,风格参考只在后续
        # style_draft / rewrite 阶段注入。
        # style_first(2026-09-12 风格直起):同一步位注入 [STYLE_REFERENCE] 前缀,第一稿就以
        # 参考作者的手笔从 bundle 写;长度带按 style_first_length_slack 放宽(上下文变量已设)。
        base_prompt = prompt
        base_user_prompt = prompt["user_prompt"] + _author_note_instruction_for_bundle(
            bundle, author_note
        )
        notices: list[dict[str, Any]] = []
        # 风格参考 v3：首稿显式按起草口径渲染，场面标签用 bundle 冻结的（蓝图给的）——没有就从场景设计推
        first_draft_tags = frozen_situation_tags(bundle)
        if style_first:
            user_prompt = base_user_prompt + _style_first_length_instruction(scene)
            prompt = self._inject_style_reference(
                base_prompt,
                scene,
                task_type="scene_generation",
                bundle=bundle,
                context_text=None,
                final_user_prompt=user_prompt,
                placement=PLACEMENT_USER_TAIL,
                role=ROLE_DRAFT,
                situation_tags=first_draft_tags,
            )
            user_prompt = apply_style_user_tail(prompt, user_prompt)
            notices = style_injection_notices(prompt)
            if _prompt_carries_style_reference(prompt):
                notices.append(
                    style_notice(
                        STYLE_NOTICE_FIRST_DRAFT,
                        "首稿已按参考作者的手笔直接起草（未经过中性稿）；风格步先量首稿：在作者常见范围内直接采用，越界才按读数做定向修改。",
                        severity="info",
                        draft_mode=draft_mode,
                    )
                )
        else:
            user_prompt = base_user_prompt + _neutral_length_instruction(scene)
        try:
            node_result = self._llm_runner.run(
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id=draft_node_id,
                step="neutral_draft",
                prompt=prompt,
                user_prompt=user_prompt,
            )
            response = node_result.response
            neutral_content = _extract_scene_text(response)
        except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
            self._record_runner_failure_attempt(
                scene=scene,
                state=state,
                bundle=bundle,
                step="neutral_draft",
                prompt=prompt,
                exc=exc,
            )
            if isinstance(exc, LLMNodeExecutionError):
                self._raise_original_runner_error(exc)
            raise

        neutral_row_id = versioned_scene_artifact_id("draft_neutral", scene_id, bundle)
        neutral_assessment = _assess_neutral_draft(scene, neutral_content)
        repair_audit: dict[str, Any] | None = None
        if not neutral_assessment["accepted"]:
            original_content = neutral_content
            original_result = node_result
            repair_length_instruction = (
                _style_first_length_instruction(
                    scene,
                    previous_length=_visible_char_count(original_content),
                    retry=True,
                )
                if style_first
                else _neutral_length_instruction(
                    scene,
                    previous_length=_visible_char_count(original_content),
                    retry=True,
                )
            )
            repair_prompt = "\n".join(
                [
                    base_user_prompt,
                    "",
                    (
                        "## Rejected First Draft Requiring One Deterministic Repair (keep the reference author's manner)"
                        if style_first
                        else "## Rejected Neutral Draft Requiring One Deterministic Repair"
                    ),
                    original_content,
                    "",
                    "## Deterministic Neutral Repair Brief",
                    _neutral_repair_brief(
                        scene,
                        source_content=original_content,
                        assessment=neutral_assessment,
                    ),
                    repair_length_instruction,
                ]
            ).strip()
            # style_first:修复稿带同一前缀(按修复提示重新装配预算,窗口种子相同)。
            repair_prompt_payload = (
                self._inject_style_reference(
                    base_prompt,
                    scene,
                    task_type="scene_generation",
                    bundle=bundle,
                    context_text=None,
                    final_user_prompt=repair_prompt,
                    placement=PLACEMENT_USER_TAIL,
                    role=ROLE_DRAFT,
                    situation_tags=first_draft_tags,
                )
                if style_first
                else prompt
            )
            repair_prompt = apply_style_user_tail(repair_prompt_payload, repair_prompt)
            try:
                repaired_result = self._llm_runner.run(
                    scene_id=scene_id,
                    chapter_id=scene.chapter_id,
                    bundle_id=bundle["bundle_id"],
                    bundle_hash=bundle["bundle_snapshot_hash"],
                    node_id=draft_node_id,
                    step="neutral_draft_repair",
                    prompt=repair_prompt_payload,
                    user_prompt=repair_prompt,
                    # 修复是受约束的局部编辑，不是第二次创作采样。降低随机性可显著
                    # 减少“补回一个事实，却把合格长度扩写出界”的连带回退。
                    temperature_override=0.1,
                )
                repaired_content = _extract_scene_text(repaired_result.response)
                repaired_assessment = _assess_neutral_draft(scene, repaired_content)
            except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
                self._record_runner_failure_attempt(
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    step="neutral_draft_repair",
                    prompt=prompt,
                    exc=exc,
                )
                # 第一遍 provider 调用已经真实消耗了一次业务尝试。若修复在
                # provider dispatch 前被预算/连续性门拒绝，通用失败记录不会计数，
                # 这里补记一次；无论哪类失败都不能把原始不合格稿伪装成完成稿。
                if not _counts_as_business_attempt(exc):
                    state.total_attempt_count += 1
                    self.session.flush()
                if isinstance(exc, LLMNodeExecutionError):
                    self._raise_original_runner_error(exc)
                raise
            else:
                repair_accepted = bool(repaired_assessment["accepted"])
                rejected_content = (
                    original_content if repair_accepted else repaired_content
                )
                rejected_result = (
                    original_result if repair_accepted else repaired_result
                )
                rejected_hash = hashlib.sha256(
                    rejected_content.encode("utf-8")
                ).hexdigest()[:10]
                rejected_row_id = (
                    f"{neutral_row_id}_rejected_{rejected_hash}"
                )
                self.session.add(
                    SceneDraft(
                        row_id=rejected_row_id,
                        scene_id=scene_id,
                        chapter_id=scene.chapter_id,
                        stage="neutral_rejected",
                        status="rejected",
                        content=rejected_content,
                        source_bundle_id=bundle["bundle_id"],
                        source_bundle_hash=bundle["bundle_snapshot_hash"],
                        generation_llm_call_id=rejected_result.llm_call_id,
                    )
                )
                if repair_accepted:
                    neutral_content = repaired_content
                    node_result = repaired_result
                    neutral_assessment = repaired_assessment
                repair_audit = {
                    "attempted": True,
                    "accepted": repair_accepted,
                    "original_llm_call_id": original_result.llm_call_id,
                    "repair_llm_call_id": repaired_result.llm_call_id,
                    "rejected_row_id": rejected_row_id,
                    "original_assessment": _assess_neutral_draft(
                        scene, original_content
                    ),
                    "repair_assessment": repaired_assessment,
                }
                if not repair_accepted:
                    self.session.add(
                        AttemptTracker(
                            scene_id=scene_id,
                            chapter_id=scene.chapter_id,
                            step="neutral_draft",
                            status="failed",
                            source_bundle_id=bundle["bundle_id"],
                            details_json={
                                "llm_call_id": repaired_result.llm_call_id,
                                "error_code": "NEUTRAL_DRAFT_REPAIR_INVALID",
                                "rejected_row_id": rejected_row_id,
                                "validation": repaired_assessment,
                                "repair": repair_audit,
                                "business_attempt_consumed": True,
                            },
                        )
                    )
                    state.current_bundle_id = bundle["bundle_id"]
                    state.current_bundle_hash = bundle["bundle_snapshot_hash"]
                    state.total_attempt_count += 1
                    self.session.flush()
                    raise DomainError(
                        "NEUTRAL_DRAFT_REPAIR_INVALID",
                        "neutral draft remained invalid after its single deterministic repair",
                        status_code=422,
                        details={
                            "reasons": list(repaired_assessment.get("reasons") or []),
                            "target_length_range": repaired_assessment.get(
                                "target_length_range"
                            ),
                            "visible_chars": repaired_assessment.get("visible_chars"),
                        },
                    )

        self.session.add(
            SceneDraft(
                row_id=neutral_row_id,
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                stage="neutral_draft",
                content=neutral_content,
                source_bundle_id=bundle["bundle_id"],
                source_bundle_hash=bundle["bundle_snapshot_hash"],
                generation_llm_call_id=node_result.llm_call_id,
            )
        )
        self.session.flush()

        styled_draft_gate: dict[str, Any] | None = None
        if style_first:
            # 首稿离原文更近:落库后同样过一次确定性抄袭 + 冻结禁用词门(记录 + notice;
            # 升级到人工复核由 hard_qc 阶段的同一 n-gram 门完成)。
            styled_draft_gate = self._styled_draft_style_gate(
                scene, neutral_content, bundle=bundle, stage="neutral_draft"
            )
            notices.extend(_styled_draft_gate_notices(styled_draft_gate))

        attempt_details: dict[str, Any] = {
            "row_id": neutral_row_id,
            "llm_call_id": node_result.llm_call_id,
        }
        if repair_audit is not None:
            attempt_details["validation"] = neutral_assessment
            attempt_details["repair"] = repair_audit
        if style_first:
            # neutral_first(对照组)的 attempt 明细保持逐字不变;只有首稿直起才多记这些键。
            attempt_details["draft_mode"] = draft_mode
            attempt_details["template_name"] = template_name
            attempt_details["content_source"] = STYLE_FIRST_DRAFT_CONTENT_SOURCE
            attempt_details["notices"] = deepcopy(notices)
            if styled_draft_gate is not None:
                attempt_details["styled_draft_gate"] = deepcopy(styled_draft_gate)
            runtime_audit = (
                deepcopy(prompt["_style_reference_runtime_audit"])
                if isinstance(prompt, Mapping)
                and isinstance(prompt.get("_style_reference_runtime_audit"), dict)
                else None
            )
            if runtime_audit is not None:
                runtime_audit["generation_outcome"] = STYLE_FIRST_DRAFT_CONTENT_SOURCE
                runtime_audit["draft_mode"] = draft_mode
                runtime_audit["notice_codes"] = [item["code"] for item in notices]
                attempt_details["style_reference_runtime"] = runtime_audit
        self.session.add(
            AttemptTracker(
                scene_id=scene_id,
                chapter_id=scene.chapter_id,
                step="neutral_draft",
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json=attempt_details,
            )
        )
        self.session.flush()

        state.current_neutral_draft_row_id = neutral_row_id
        # 治理 §4.3：latest_valid 与 current_* 分轨——重写/失败路径清 current_* 时该指针保留
        state.latest_valid_draft_row_id = neutral_row_id
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        state.total_attempt_count += 1
        self.session.flush()

        return NeutralGenerationResult(
            row_id=neutral_row_id,
            content=neutral_content,
            llm_call_id=node_result.llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key="neutral_draft",
            draft_mode=draft_mode,
            notices=notices,
            styled_draft_gate=styled_draft_gate,
        )

    def generate_style_draft(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        neutral_draft_row_id: str,
        neutral_content: str,
        author_note: str | None = None,
        resume_base: StyleGenerationResult | None = None,
        product_callback: (
            Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None
        ) = None,
        step_reconciler: Callable[[str], None] | None = None,
    ) -> StyleGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        policy = style_policy_for_bundle(bundle)
        if policy.style_first:
            # 风格参考 v3（P5b）：作者手笔直起时风格步按读数决定（在范围内不调模型；越界定向修改；不更像保留首稿）。
            # neutral_first（阅读对照组）与未绑定仍走下面原来的「重组 / 复读」，逐字不变。
            return self._style_first_step(
                scene=scene,
                state=state,
                bundle=bundle,
                policy=policy,
                first_row_id=neutral_draft_row_id,
                first_content=neutral_content,
                author_note=author_note,
                row_id=versioned_scene_artifact_id("draft_style", scene_id, bundle),
                slot_key="initial:0",
                slot_order=0,
                execution_step_key="style_draft:0",
                resume_base=resume_base,
                product_callback=product_callback,
                step_reconciler=None,
                attempt_details_extra={"source_neutral_draft_row_id": neutral_draft_row_id},
            )
        return self._run_style_generation(
            scene=scene,
            state=state,
            bundle=bundle,
            row_id=versioned_scene_artifact_id("draft_style", scene_id, bundle),
            stage="style_draft",
            llm_step="style_draft",
            neutral_content=neutral_content,
            source_label=_source_draft_label(bundle),
            source_row_id=neutral_draft_row_id,
            extra_instruction=(
                _source_draft_instruction(bundle)
                + _author_note_instruction_for_bundle(bundle, author_note)
            ),
            source_draft_row_id=neutral_draft_row_id,
            source_draft_content=neutral_content,
            client_kind="style",
            execution_step_key="style_draft:0",
            attempt_details_extra={"source_neutral_draft_row_id": neutral_draft_row_id},
            product_slot_key="initial:0",
            product_slot_order=0,
            resume_base=resume_base,
            product_callback=product_callback,
            step_reconciler=step_reconciler,
        )

    def generate_style_draft_candidates(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        neutral_draft_row_id: str,
        neutral_content: str,
        author_note: str | None = None,
        n_candidates: int = 3,
        max_candidates: int | None = None,
        resume_candidates: list[StyleGenerationResult] | None = None,
        candidate_checkpoint: (
            Callable[[int, StyleGenerationResult], None] | None
        ) = None,
        step_reconciler: Callable[[str], None] | None = None,
        resume_bases: dict[str, StyleGenerationResult] | None = None,
        resume_products: dict[str, StyleGenerationResult] | None = None,
        product_callback: (
            Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None
        ) = None,
    ) -> list[StyleGenerationResult]:
        """Generate N style-draft candidates with evidence-gated style reranking.

        Wave 3（治理 §5.5）：低分散补救为**渐进补候选**——初始 n_candidates，
        分散度 <0.15 时在预算允许下逐个补到 max_candidates（关键 3→5、标准
        2→3），不再一次生成后整批无上限重试。

        风格评分默认 shadow，仅落可审计诊断；只有冻结的人评证据授权后，才可在
        adversarial 质量差距受限的候选间改序。连续复刻参考原文的候选由独立硬
        guard 后置，不依赖未校准的风格分数。
        """
        from novel_system.services.literary_quality import (
            adversarial_rank_score,
            get_dimension_weights,
        )

        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        policy = style_policy_for_bundle(bundle)
        if policy.style_first:
            # 风格参考 v3（P5b）：作者手笔直起时候选 = 首稿 + (N−1) 个定向修改，按读数 distance 排序
            #（取代旧的候选重排风格分）；关键场景的匿名终选门照旧。
            durable_products = dict(resume_products or {})
            if not durable_products:
                durable_products.update(
                    (f"initial:{index}", candidate)
                    for index, candidate in enumerate(resume_candidates or [])
                )
            return self._style_first_candidates(
                scene=scene,
                state=state,
                bundle=bundle,
                policy=policy,
                first_row_id=neutral_draft_row_id,
                first_content=neutral_content,
                author_note=author_note,
                n_candidates=n_candidates,
                step_reconciler=step_reconciler,
                resume_bases=dict(resume_bases or {}),
                resume_products=durable_products,
                product_callback=product_callback,
            )

        # §6 dynamic quality weights — project-level style profile can shift
        # which adversarial dimensions matter most for this particular work.
        _project_weights = (
            get_dimension_weights(
                scene.project_id,
                self.session,
            )
            if scene and scene.project_id
            else None
        )
        # Evidence-gated per-cell strategy policies were retired; ranking always
        # uses the project/built-in dimension weights.
        quality_strategy_audit: dict[str, Any] = {
            "status": "project_or_builtin_weights",
            "matched_policy_id": None,
        }

        try:
            task_config = self._llm_runner.task_config("style_draft")
            base_temp = task_config.temperature
        except KeyError:
            base_temp = 0.7

        if n_candidates <= 1:
            temperatures = [base_temp]
        else:
            spread = 0.05
            temperatures = [
                round(base_temp + spread * (2 * i / (n_candidates - 1) - 1), 3)
                for i in range(n_candidates)
            ]
            temperatures = [max(0.0, min(2.0, t)) for t in temperatures]

        durable_products = dict(resume_products or {})
        durable_bases = dict(resume_bases or {})
        if not durable_products:
            durable_products.update(
                (f"initial:{index}", candidate)
                for index, candidate in enumerate(resume_candidates or [])
            )
        candidates: list[tuple[StyleGenerationResult, float]] = [
            (
                candidate,
                adversarial_rank_score(candidate.content, weights=_project_weights),
            )
            for candidate in durable_products.values()
        ]
        for idx, temp in enumerate(temperatures):
            slot_key = f"initial:{idx}"
            if slot_key in durable_products:
                continue
            cand_row_id = (
                versioned_scene_artifact_id("draft_style_cand", scene_id, bundle)
                + f"_{idx}"
            )
            try:
                if step_reconciler is not None and slot_key not in durable_bases:
                    step_reconciler(f"style_draft:{idx}")
                result = self._run_style_generation(
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    row_id=cand_row_id,
                    stage="style_draft",
                    llm_step="style_draft",
                    neutral_content=neutral_content,
                    source_label=_source_draft_label(bundle),
                    source_row_id=neutral_draft_row_id,
                    extra_instruction=(
                        "Apply the style prompt template without changing the approved facts."
                        + _author_note_instruction_for_bundle(bundle, author_note)
                    ),
                    source_draft_row_id=neutral_draft_row_id,
                    source_draft_content=neutral_content,
                    client_kind="style",
                    temperature_override=temp,
                    execution_step_key=f"style_draft:{idx}",
                    attempt_details_extra={
                        "source_neutral_draft_row_id": neutral_draft_row_id,
                        "candidate_index": idx,
                        "temperature_override": temp,
                        "n_candidates": n_candidates,
                        "quality_strategy": quality_strategy_audit,
                    },
                    product_slot_key=slot_key,
                    product_slot_order=idx,
                    resume_base=durable_bases.get(slot_key),
                    product_callback=product_callback,
                    step_reconciler=step_reconciler,
                )
                score = adversarial_rank_score(result.content, weights=_project_weights)
                candidates.append((result, score))
                if candidate_checkpoint is not None:
                    candidate_checkpoint(idx, result)
            except (DomainError, LLMNodeExecutionError):
                _LOGGER.warning(
                    "candidate %d/%d failed for scene %s",
                    idx + 1,
                    n_candidates,
                    scene_id,
                )
                if candidate_checkpoint is not None or product_callback is not None:
                    raise
                continue

        if not candidates:
            return [
                self.generate_style_draft(
                    scene_id,
                    bundle,
                    neutral_draft_row_id=neutral_draft_row_id,
                    neutral_content=neutral_content,
                    author_note=author_note,
                    product_callback=product_callback,
                    step_reconciler=step_reconciler,
                )
            ]

        candidates.sort(key=lambda pair: pair[1], reverse=True)

        # Wave 3（§5.5）：渐进补候选——每次只补 1 个（温度加宽 / 发散提示 /
        # 风格侧重轮换作为逐个变体来源），每步过预算闸，补到上限或分散达标即停。
        candidate_cap = max(n_candidates, max_candidates or n_candidates)
        if len(candidates) >= 2 and candidate_cap > len(candidates):
            from novel_system.services.scene_budget import budget_unit, can_spend

            variants = _progressive_top_up_variants(base_temp)
            known_top_up_indices = {
                int(slot_key.rsplit(":", 1)[-1])
                for slot_key in {*durable_products, *durable_bases}
                if slot_key.startswith("topup:")
                and slot_key.rsplit(":", 1)[-1].isdigit()
            }
            pending_top_up_indices = sorted(
                index
                for index in known_top_up_indices
                if f"topup:{index}" in durable_bases
                and f"topup:{index}" not in durable_products
            )
            top_up_index = max(known_top_up_indices, default=0)
            while len(candidates) < candidate_cap:
                dispersion = _candidate_dispersion([c.content for c, _ in candidates])
                pending_top_up_index = (
                    pending_top_up_indices.pop(0) if pending_top_up_indices else None
                )
                if pending_top_up_index is None and dispersion >= 0.15:
                    break
                if pending_top_up_index is None and not can_spend(
                    state, budget_unit(state)
                ):
                    _LOGGER.warning(
                        "budget exhausted — stop progressive candidate top-up for scene %s "
                        "(dispersion=%.3f, %d candidates)",
                        scene_id,
                        dispersion,
                        len(candidates),
                    )
                    break
                if pending_top_up_index is None:
                    top_up_index += 1
                else:
                    top_up_index = pending_top_up_index
                temp, prefix, strategy = variants[(top_up_index - 1) % len(variants)]
                _LOGGER.warning(
                    "low candidate dispersion (%.3f) for scene %s — progressive top-up #%d via %s (§5.5)",
                    dispersion,
                    scene_id,
                    top_up_index,
                    strategy,
                )
                top_up_row_id = (
                    versioned_scene_artifact_id("draft_style_cand", scene_id, bundle)
                    + f"_topup_{top_up_index}"
                )
                slot_key = f"topup:{top_up_index}"
                try:
                    if step_reconciler is not None and slot_key not in durable_bases:
                        step_reconciler(f"style_draft:topup:{top_up_index}")
                    result = self._run_style_generation(
                        scene=scene,
                        state=state,
                        bundle=bundle,
                        row_id=top_up_row_id,
                        stage="style_draft",
                        llm_step="style_draft",
                        neutral_content=neutral_content,
                        source_label=_source_draft_label(bundle),
                        source_row_id=neutral_draft_row_id,
                        extra_instruction=(
                            "Apply the style prompt template without changing the approved facts."
                            + _author_note_instruction_for_bundle(bundle, author_note)
                        ),
                        source_draft_row_id=neutral_draft_row_id,
                        source_draft_content=neutral_content,
                        client_kind="style",
                        temperature_override=temp,
                        execution_step_key=f"style_draft:topup:{top_up_index}",
                        extra_system_prefix=prefix,
                        attempt_details_extra={
                            "source_neutral_draft_row_id": neutral_draft_row_id,
                            "candidate_index": f"topup_{top_up_index}",
                            "temperature_override": temp,
                            "n_candidates": n_candidates,
                            "max_candidates": candidate_cap,
                            "diversification_strategy": strategy,
                            "progressive_top_up": True,
                            "quality_strategy": quality_strategy_audit,
                        },
                        product_slot_key=slot_key,
                        product_slot_order=n_candidates + top_up_index - 1,
                        resume_base=durable_bases.get(slot_key),
                        product_callback=product_callback,
                        step_reconciler=step_reconciler,
                    )
                    candidates.append(
                        (
                            result,
                            adversarial_rank_score(
                                result.content, weights=_project_weights
                            ),
                        )
                    )
                    if candidate_checkpoint is not None:
                        candidate_checkpoint(len(candidates) - 1, result)
                except (DomainError, LLMNodeExecutionError):
                    # 失败即停：不无上限重试（Wave 3 项 5）
                    _LOGGER.warning(
                        "progressive top-up #%d failed for scene %s — stop",
                        top_up_index,
                        scene_id,
                    )
                    if candidate_checkpoint is not None or product_callback is not None:
                        raise
                    break
            candidates.sort(key=lambda pair: pair[1], reverse=True)

        # 2026-09-14 减法:候选重排层(shadow / active、基准授权)已删除——候选保持质量序;每个
        # 候选仍算一次冻结画像的贴合读数与 12 字抄袭守卫(盲选门据此剔除抄袭候选,工作台读数据此展示)。
        for rank, (result, score) in enumerate(candidates):
            result.ranking_audit = self._candidate_style_assessment(
                bundle, result, float(score), rank=rank
            )

        best_result = candidates[0][0]
        state.current_style_draft_row_id = best_result.row_id
        state.latest_valid_draft_row_id = best_result.row_id
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        # §6 Defect D: persist dispersion score for author-facing quality signal
        if len(candidates) >= 2:
            final_dispersion = _candidate_dispersion([c.content for c, _ in candidates])
            state.candidate_dispersion_score = round(final_dispersion, 4)
        self.session.flush()

        return [result for result, _ in candidates]

    def _candidate_style_assessment(
        self,
        bundle: dict[str, Any],
        result: "StyleGenerationResult",
        quality_score: float,
        *,
        rank: int,
    ) -> dict[str, Any]:
        """单个候选的风格贴合读数 + 抄袭守卫(纯函数评分核;失败只降级为质量序读数)。"""
        rerank: dict[str, Any] = {"applied_mode": "off", "reason": None}
        fallback = {
            "row_id": result.row_id,
            "quality_score": round(float(quality_score), 6),
            "style_score": None,
            "rank": rank,
            "selected": rank == 0,
            "selection_reason": "quality_order",
        }
        try:
            from novel_system.services.reference_copy_gate import check_reference_copy
            from novel_system.services.style_reference.candidate_rerank import (
                CandidateRerankPolicy,
                assess_candidate_text,
                build_style_target,
            )
            from novel_system.services.style_reference.runtime_contract import (
                contract_profile_objects,
            )

            policy = style_policy_for_bundle(bundle)
            rerank["runtime_contract_mode"] = policy.mode
            contract = policy.contract
            target = None
            if policy.mode == "absent" or contract is None:
                rerank["reason"] = (
                    "bundle_has_no_style_profile"
                    if policy.mode == "absent"
                    else (policy.error_code or "frozen_runtime_contract_unavailable")
                )
            else:
                target = build_style_target(contract_profile_objects(contract))
                if target is None:
                    rerank["reason"] = "profile_metrics_insufficient"
            assessment = assess_candidate_text(
                result.row_id,
                result.content or "",
                float(quality_score),
                target,
                CandidateRerankPolicy(),
            )
            if policy.bound and (result.content or ""):
                # 风格参考 v3：候选的原文重合走唯一抄袭门（按书一次索引、同一稿不重复扫描）
                copy = check_reference_copy(self.session, result.content or "", policy=policy)
                assessment.plagiarism_checked = True
                assessment.plagiarism_passed = not copy.hits
                assessment.plagiarism_hit_count = len(copy.hits)
                assessment.plagiarism_max_match_chars = max(
                    (hit.matched_chars for hit in copy.hits), default=0
                )
        except Exception as exc:  # noqa: BLE001 — 读数是可选增强,不阻断候选交付
            _LOGGER.warning(
                "style candidate assessment degraded for scene %s", result.row_id, exc_info=True
            )
            return {
                **fallback,
                "rerank": {
                    **rerank,
                    "reason": "assessment_internal_error",
                    "error_code": getattr(exc, "code", exc.__class__.__name__),
                },
            }
        assessment.rank = rank
        assessment.selected = rank == 0
        assessment.selection_reason = "quality_order"
        return {**assessment.to_audit_dict(), "rerank": rerank}

    # ------------------------------------------------------------------
    # 风格参考 v3（P5b，L1 / N6）：作者手笔直起时的风格步——按读数决定
    # ------------------------------------------------------------------
    # 旧的「再靠近一层的复读」真实运行里 3/3 场越改越远。现在：读首稿 → 在作者正常范围内（或读数不可信）→ 不调模型，
    # 首稿即风格稿；越界 → 只改越界的维（定向修改，模板 style_targeted_revision，走 style_draft 节点路由）→ 改完再读，
    # 不更像（或没过抄袭门 / 安全门）就保留首稿。检查点次序（neutral_ready → hard_qc_ready → style_ready）与步位不变。

    def _style_first_step(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        policy: Any,
        first_row_id: str,
        first_content: str,
        author_note: str | None,
        row_id: str,
        slot_key: str,
        slot_order: int,
        execution_step_key: str,
        resume_base: StyleGenerationResult | None = None,
        product_callback: (
            Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None
        ) = None,
        step_reconciler: Callable[[str], None] | None = None,
        first_reading: Any = None,
        first_reading_id: str | None = None,
        candidate_mode: bool = False,
        force_accept: bool = False,
        temperature_override: float | None = None,
        attempt_details_extra: dict[str, Any] | None = None,
    ) -> StyleGenerationResult:
        with _length_band_slack_for(bundle, scene):
            if resume_base is not None:
                return self._finish_style_first_resume(
                    scene=scene,
                    bundle=bundle,
                    resume_base=resume_base,
                    first_row_id=first_row_id,
                    slot_key=slot_key,
                    slot_order=slot_order,
                    execution_step_key=execution_step_key,
                    product_callback=product_callback,
                )
            thresholds = style_step.fidelity_thresholds()
            if first_reading is None and first_reading_id is None:
                first_reading, first_reading_id = self._record_first_draft_reading(
                    scene, policy, first_row_id, first_content, thresholds
                )
            revise, gate_reason = style_step.style_step_gate(first_reading, thresholds)
            if candidate_mode and first_reading is not None and first_reading.reliable:
                # Best-of-N 的修改槽位：首稿在不在范围内都改（候选按 distance 排序，首稿永远在候选里）
                revise, gate_reason = True, style_step.REASON_CANDIDATE_SLOT
            if force_accept:
                revise = False
            if not revise:
                return self._accept_first_draft(
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    first_row_id=first_row_id,
                    first_content=first_content,
                    first_reading=first_reading,
                    first_reading_id=first_reading_id,
                    reason=gate_reason,
                    thresholds=thresholds,
                    row_id=row_id,
                    slot_key=slot_key,
                    slot_order=slot_order,
                    product_callback=product_callback,
                    attempt_details_extra=attempt_details_extra,
                )
            if step_reconciler is not None:
                step_reconciler(execution_step_key)
            return self._run_targeted_revision(
                scene=scene,
                state=state,
                bundle=bundle,
                policy=policy,
                first_row_id=first_row_id,
                first_content=first_content,
                first_reading=first_reading,
                first_reading_id=first_reading_id,
                gate_reason=gate_reason,
                thresholds=thresholds,
                author_note=author_note,
                row_id=row_id,
                slot_key=slot_key,
                slot_order=slot_order,
                execution_step_key=execution_step_key,
                product_callback=product_callback,
                candidate_mode=candidate_mode,
                temperature_override=temperature_override,
                attempt_details_extra=attempt_details_extra,
            )

    def _record_first_draft_reading(
        self,
        scene: SceneCard,
        policy: Any,
        first_row_id: str,
        first_content: str,
        thresholds: Any,
    ) -> tuple[Any, str | None]:
        """读首稿并记一条 first_draft 读数（同一稿行幂等）；读数失败只记日志，按「读不出」处理。"""
        try:
            reading = style_readings.reading_for_text(self.session, policy, first_content)
        except Exception:  # noqa: BLE001 — 读数是观察：失败按读不出处理（保留首稿）
            _LOGGER.warning("first-draft fidelity reading failed for scene %s", scene.scene_id, exc_info=True)
            return None, None
        row = style_readings.record_fidelity_reading(
            self.session,
            policy=policy,
            text=first_content,
            source=style_readings.SOURCE_PIPELINE,
            stage=style_readings.STAGE_FIRST_DRAFT,
            scene_id=scene.scene_id,
            project_id=style_readings.scene_project_id(self.session, scene),
            draft_ref=first_row_id,
            reading=reading,
            max_percentile=thresholds.style_step_max_percentile,
        )
        return reading, (row.reading_id if row is not None else None)

    def _first_draft_lineage(self, first_row_id: str) -> tuple[str | None, str, str | None]:
        """首稿的 (llm_call_id, execution_step_key, execution_id)——「首稿即风格稿」的产品沿用首稿那次调用的谱系。"""
        row = self.session.get(SceneDraft, first_row_id)
        llm_call_id = str(getattr(row, "generation_llm_call_id", "") or "") or None if row is not None else None
        call = self.session.get(LlmCall, llm_call_id) if llm_call_id else None
        step_key = str(getattr(call, "execution_step_key", "") or "") or "neutral_draft"
        execution_id = str(getattr(call, "execution_id", "") or "") or None if call is not None else None
        return llm_call_id, step_key, execution_id

    @staticmethod
    def _style_first_gate_decision(decision: Mapping[str, Any]) -> dict[str, Any]:
        """产品回调里的门裁决：作者手笔直起时风格步不跑房风门（让位），只记风格步的决定。"""
        return {
            "triggered": False,
            "rewrite_pass": 0,
            "house_taste_gate": "deferred_to_reference",
            "style_step": {
                key: decision.get(key)
                for key in ("version", "decision", "reason", "llm_call", "dimensions")
                if key in decision
            },
        }

    def _emit_style_first_products(
        self,
        product: StyleGenerationResult,
        *,
        first_row_id: str,
        slot_key: str,
        slot_order: int,
        product_callback: Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None,
        decision: Mapping[str, Any],
        emit_base: bool = True,
    ) -> None:
        if product_callback is None:
            return
        if emit_base:
            product_callback(
                slot_key,
                "base",
                product,
                {
                    "slot_order": slot_order,
                    "source_neutral_draft_row_id": first_row_id,
                    "gate_decision": None,
                    "source_base_row_id": None,
                },
            )
        product_callback(
            slot_key,
            "final",
            product,
            {
                "slot_order": slot_order,
                "source_neutral_draft_row_id": first_row_id,
                "gate_decision": self._style_first_gate_decision(decision),
                "source_base_row_id": product.row_id,
                "de_template_outcome": {"status": "not_required"},
            },
        )

    def _finish_style_first_resume(
        self,
        *,
        scene: SceneCard,
        bundle: dict[str, Any],
        resume_base: StyleGenerationResult,
        first_row_id: str,
        slot_key: str,
        slot_order: int,
        execution_step_key: str,
        product_callback: Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None,
    ) -> StyleGenerationResult:
        """检查点里只有基稿（进程在基稿与终稿回调之间停了）：风格步基稿即终稿，补一次终稿回调。"""
        if resume_base.bundle_id != bundle["bundle_id"] or resume_base.bundle_hash != bundle["bundle_snapshot_hash"]:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "resumed style base does not match its locked work item",
                status_code=409,
            )
        decision: dict[str, Any] = {}
        for attempt in self.session.execute(
            select(AttemptTracker).where(
                AttemptTracker.scene_id == scene.scene_id,
                AttemptTracker.step == "style_draft",
                AttemptTracker.status == "completed",
                AttemptTracker.source_bundle_id == bundle["bundle_id"],
            )
        ).scalars():
            details = attempt.details_json or {}
            if details.get("row_id") == resume_base.row_id and isinstance(details.get("style_step"), dict):
                decision = dict(details["style_step"])
                break
        resume_base.style_step = decision or resume_base.style_step
        if decision.get("decision") == style_step.DECISION_FIRST_DRAFT_ACCEPTED:
            resume_base.lineage = LINEAGE_FIRST_DRAFT_ACCEPTED
        self._emit_style_first_products(
            resume_base,
            first_row_id=first_row_id,
            slot_key=slot_key,
            slot_order=slot_order,
            product_callback=product_callback,
            decision=decision,
            emit_base=False,
        )
        return resume_base

    def _accept_first_draft(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        first_row_id: str,
        first_content: str,
        first_reading: Any,
        first_reading_id: str | None,
        reason: str,
        thresholds: Any,
        row_id: str,
        slot_key: str,
        slot_order: int,
        product_callback: Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None,
        attempt_details_extra: dict[str, Any] | None = None,
        notice_severity: str = "info",
    ) -> StyleGenerationResult:
        """首稿即风格稿：不调模型，风格稿行就是首稿原文（谱系沿用首稿那次调用）。"""
        llm_call_id, step_key, execution_id = self._first_draft_lineage(first_row_id)
        decision = {
            "version": STYLE_STEP_VERSION,
            "decision": style_step.DECISION_FIRST_DRAFT_ACCEPTED,
            "reason": reason,
            "llm_call": False,
            "dimensions": [],
            "first_reading": style_step.reading_brief(first_reading, reading_id=first_reading_id),
            "revision_reading": None,
            "thresholds": thresholds.audit() if hasattr(thresholds, "audit") else None,
            "slot_key": slot_key,
        }
        if reason == style_step.REASON_TEMPLATE_MISSING:
            notice = style_notice(
                STYLE_NOTICE_REVISION_REJECTED,
                "首稿测得与作者有明显差距，但这台机器的提示词快照里还没有定向修改模板（请运行 sync_prompt_templates），"
                "这一场保留首稿。",
                severity="warning",
                reason=reason,
            )
        else:
            notice = style_notice(
                STYLE_NOTICE_FIRST_DRAFT_ACCEPTED,
                {
                    style_step.REASON_WITHIN_RANGE: "首稿读数在参考作者的正常范围内，风格步没有再调模型，首稿即风格稿。",
                    style_step.REASON_READING_UNRELIABLE: "首稿太短（或参考书的样例窗口太少），读数不可信；为免越改越远，首稿即风格稿。",
                    style_step.REASON_READING_UNAVAILABLE: "参考书还没有可用的读数尺子，风格步没有再调模型，首稿即风格稿。",
                    style_step.REASON_CANDIDATE_SLOT: "首稿作为候选之一参与按读数排序。",
                }.get(reason, "风格步保留了首稿。"),
                severity=notice_severity,
                reason=reason,
                percentile=getattr(first_reading, "percentile", None),
                distance=getattr(first_reading, "distance", None),
            )
        notices = [notice]
        self.session.add(
            SceneDraft(
                row_id=row_id,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                stage="style_draft",
                content=first_content,
                source_bundle_id=bundle["bundle_id"],
                source_bundle_hash=bundle["bundle_snapshot_hash"],
                generation_llm_call_id=llm_call_id,
            )
        )
        self.session.flush()
        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step="style_draft",
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json={
                    "row_id": row_id,
                    "llm_call_id": llm_call_id,
                    "source_draft_row_id": first_row_id,
                    "content_source": style_step.CONTENT_SOURCE_FIRST_DRAFT_ACCEPTED,
                    "lineage": LINEAGE_FIRST_DRAFT_ACCEPTED,
                    "draft_mode": DRAFT_MODE_STYLE_FIRST,
                    "notices": deepcopy(notices),
                    "style_step": deepcopy(decision),
                    **(attempt_details_extra or {}),
                },
            )
        )
        self.session.flush()
        state.current_style_draft_row_id = row_id
        state.latest_valid_draft_row_id = row_id
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        self.session.flush()
        product = StyleGenerationResult(
            row_id=row_id,
            content=first_content,
            llm_call_id=llm_call_id or "",
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key=step_key,
            artifact_execution_id=execution_id,
            notices=deepcopy(notices),
            lineage=LINEAGE_FIRST_DRAFT_ACCEPTED,
            style_step=deepcopy(decision),
        )
        self._emit_style_first_products(
            product,
            first_row_id=first_row_id,
            slot_key=slot_key,
            slot_order=slot_order,
            product_callback=product_callback,
            decision=decision,
        )
        return product

    def _run_targeted_revision(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        policy: Any,
        first_row_id: str,
        first_content: str,
        first_reading: Any,
        first_reading_id: str | None,
        gate_reason: str,
        thresholds: Any,
        author_note: str | None,
        row_id: str,
        slot_key: str,
        slot_order: int,
        execution_step_key: str,
        product_callback: Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None,
        candidate_mode: bool = False,
        temperature_override: float | None = None,
        attempt_details_extra: dict[str, Any] | None = None,
    ) -> StyleGenerationResult:
        """定向修改：只改越界的维（至多 4 维，重点维在前），改完再读；不更像 / 没过抄袭门 / 没过安全门 → 保留首稿。

        ``candidate_mode``（Best-of-N 的修改槽位）：过了抄袭门与安全门就作为候选留下（候选之间按 distance 排序，
        首稿永远在候选里），没过的槽位保留首稿原文（选择门按正文去重）。
        """
        template_name = "style_targeted_revision"
        if not self._prompt_builder().has_template(template_name):
            return self._accept_first_draft(
                scene=scene,
                state=state,
                bundle=bundle,
                first_row_id=first_row_id,
                first_content=first_content,
                first_reading=first_reading,
                first_reading_id=first_reading_id,
                reason=style_step.REASON_TEMPLATE_MISSING,
                thresholds=thresholds,
                row_id=row_id,
                slot_key=slot_key,
                slot_order=slot_order,
                product_callback=product_callback,
                attempt_details_extra=attempt_details_extra,
            )
        card, line_states = _policy_card(policy)
        dimensions = style_step.revision_dimensions(first_reading, getattr(policy, "dimension_states", None))
        differences = style_step.revision_differences(first_reading, dimensions)
        card_lines = style_step.card_lines_for(card, dimensions, line_states=line_states)
        fallback_llm_call_id = f"llm_call_{scene.scene_id}_{uuid.uuid4().hex[:12]}"
        started_at = time.perf_counter()
        prompt: dict[str, Any] | None = None
        try:
            prompt = self._prompt_builder().build(bundle["snapshot"], template_name)
        except Exception as exc:
            self._persist_generation_failure(
                scene=scene,
                state=state,
                bundle=bundle,
                llm_call_id=fallback_llm_call_id,
                step="style_draft",
                execution_step_key=execution_step_key,
                started_at=started_at,
                task_config=None,
                prompt=prompt,
                request_summary={},
                exc=exc,
                source_draft_row_id=first_row_id,
            )
            raise
        base_prompt = prompt
        prompt_parts = [
            base_prompt["user_prompt"] + _author_note_instruction_for_bundle(bundle, author_note),
            "",
            f"## {FIRST_DRAFT_SOURCE_LABEL}",
            first_content,
            "",
            f"Source Draft Row ID: {first_row_id}",
            "",
            style_step.revision_brief_sections(
                dimensions=dimensions, differences=differences, card_lines=card_lines
            ),
            _style_length_instruction(
                scene, source_length=_visible_char_count(first_content), style_first=True
            ).strip(),
        ]
        if JSON_SCHEMA_INSTRUCTION not in base_prompt["user_prompt"]:
            prompt_parts.extend(["", JSON_SCHEMA_INSTRUCTION])
        user_prompt = "\n".join(part for part in prompt_parts if part is not None).strip()
        prompt = self._inject_style_reference(
            base_prompt,
            scene,
            task_type="scene_generation",
            bundle=bundle,
            context_text=first_content,
            final_user_prompt=user_prompt,
            placement=PLACEMENT_USER_TAIL,
            role=ROLE_REVISE,
            revise_dimensions=dimensions,
        )
        user_prompt = apply_style_user_tail(prompt, user_prompt)
        notices: list[dict[str, Any]] = style_injection_notices(prompt)
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id="style_draft",
                step="style_draft",
                prompt=prompt,
                user_prompt=user_prompt,
                source_draft_row_id=first_row_id,
                source_draft_content=first_content,
                temperature_override=temperature_override,
                execution_step_key=execution_step_key,
            )
            revision_content = _extract_scene_text(node_result.response)
        except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
            self._record_runner_failure_attempt(
                scene=scene,
                state=state,
                bundle=bundle,
                step="style_draft",
                prompt=prompt,
                exc=exc,
                source_draft_row_id=first_row_id,
            )
            if isinstance(exc, LLMNodeExecutionError):
                self._raise_original_runner_error(exc)
            raise

        base_safety = _assess_style_base_rewrite(
            scene=scene, source_content=first_content, rewritten_content=revision_content
        )
        copy_check = None
        copy_blocked = False
        try:
            from novel_system.services.reference_copy_gate import check_reference_copy

            copy_check = check_reference_copy(self.session, revision_content, policy=policy)
            copy_blocked = bool(copy_check.blocked)
        except Exception:  # noqa: BLE001 — 抄袭门查不成：按拦下处理（fail-closed），保留首稿
            _LOGGER.warning("copy gate failed on targeted revision for scene %s", scene.scene_id, exc_info=True)
            copy_blocked = True
        try:
            revision_reading = style_readings.reading_for_text(self.session, policy, revision_content)
        except Exception:  # noqa: BLE001 — 读不出：按「不更像」处理
            _LOGGER.warning("revision fidelity reading failed for scene %s", scene.scene_id, exc_info=True)
            revision_reading = None
        if candidate_mode:
            keep = bool(base_safety["accepted"]) and not copy_blocked and revision_reading is not None
            keep_reason = (
                style_step.REASON_BASE_UNSAFE
                if not base_safety["accepted"]
                else style_step.REASON_COPY_BLOCKED
                if copy_blocked
                else style_step.REASON_REVISION_UNREADABLE
                if revision_reading is None
                else style_step.REASON_CANDIDATE_SLOT
            )
        else:
            keep, keep_reason = style_step.revision_keep_decision(
                first_reading,
                revision_reading,
                copy_blocked=copy_blocked,
                base_safe=bool(base_safety["accepted"]),
                thresholds=thresholds,
            )
        rejected_row_id: str | None = None
        if keep:
            content = revision_content
            content_source = style_step.CONTENT_SOURCE_TARGETED_REVISION
            revision_row_ref = row_id
        else:
            rejected_hash = hashlib.sha256(revision_content.encode("utf-8")).hexdigest()[:10]
            rejected_row_id = f"{row_id}_rejected_{rejected_hash}"
            self.session.add(
                SceneDraft(
                    row_id=rejected_row_id,
                    scene_id=scene.scene_id,
                    chapter_id=scene.chapter_id,
                    stage="style_rejected",
                    status="rejected",
                    content=revision_content,
                    source_bundle_id=bundle["bundle_id"],
                    source_bundle_hash=bundle["bundle_snapshot_hash"],
                    generation_llm_call_id=node_result.llm_call_id,
                )
            )
            content = first_content
            content_source = style_step.CONTENT_SOURCE_REVISION_NOT_CLOSER
            revision_row_ref = rejected_row_id
            notices.append(
                style_notice(
                    STYLE_NOTICE_REVISION_REJECTED,
                    {
                        style_step.REASON_NOT_CLOSER: "定向修改没有让稿子更像参考作者（读数没有变近），保留了首稿。",
                        style_step.REASON_COPY_BLOCKED: "定向修改稿与参考书原文连续相同或用了受保护专名，已丢弃，保留首稿。",
                        style_step.REASON_BASE_UNSAFE: "定向修改稿没过确定性安全门（长度 / 必写项 / 禁写内容 / 文本完整性），保留首稿。",
                        style_step.REASON_REVISION_UNREADABLE: "定向修改稿读不出读数，无法确认更像，保留首稿。",
                    }.get(keep_reason, "定向修改没有采用，保留首稿。"),
                    severity="info",
                    reason=keep_reason,
                    first_distance=getattr(first_reading, "distance", None),
                    revision_distance=getattr(revision_reading, "distance", None),
                    rejected_candidate_row_id=rejected_row_id,
                )
            )
        self.session.add(
            SceneDraft(
                row_id=row_id,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                stage="style_draft",
                content=content,
                source_bundle_id=bundle["bundle_id"],
                source_bundle_hash=bundle["bundle_snapshot_hash"],
                generation_llm_call_id=node_result.llm_call_id,
            )
        )
        self.session.flush()
        revision_reading_row = style_readings.record_fidelity_reading(
            self.session,
            policy=policy,
            text=revision_content,
            source=style_readings.SOURCE_PIPELINE,
            stage=style_readings.STAGE_REVISION,
            scene_id=scene.scene_id,
            project_id=style_readings.scene_project_id(self.session, scene),
            draft_ref=revision_row_ref,
            reading=revision_reading,
            copy_check=copy_check,
            max_percentile=thresholds.style_step_max_percentile,
        )
        styled_draft_gate: dict[str, Any] | None = None
        if keep:
            styled_draft_gate = self._styled_draft_style_gate(
                scene, content, bundle=bundle, stage="style_draft"
            )
            notices.extend(_styled_draft_gate_notices(styled_draft_gate))
        decision = {
            "version": STYLE_STEP_VERSION,
            "decision": (
                style_step.DECISION_REVISION_KEPT if keep else style_step.DECISION_REVISION_REJECTED
            ),
            "reason": keep_reason,
            "gate_reason": gate_reason,
            "llm_call": True,
            "dimensions": list(dimensions),
            "differences": [item.get("text") for item in differences],
            "card_line_count": sum(len(lines) for lines in card_lines.values()),
            "first_reading": style_step.reading_brief(first_reading, reading_id=first_reading_id),
            "revision_reading": style_step.reading_brief(
                revision_reading,
                reading_id=revision_reading_row.reading_id if revision_reading_row is not None else None,
            ),
            "copy_check": style_readings.copy_check_summary(copy_check),
            "base_safety_accepted": bool(base_safety["accepted"]),
            "thresholds": thresholds.audit() if hasattr(thresholds, "audit") else None,
            "candidate_mode": bool(candidate_mode),
            "slot_key": slot_key,
        }
        runtime_audit = (
            deepcopy(prompt["_style_reference_runtime_audit"])
            if isinstance(prompt, Mapping) and isinstance(prompt.get("_style_reference_runtime_audit"), dict)
            else None
        )
        if runtime_audit is not None:
            runtime_audit["generation_outcome"] = content_source
            runtime_audit["draft_mode"] = DRAFT_MODE_STYLE_FIRST
            runtime_audit["notice_codes"] = [item["code"] for item in notices]
        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step="style_draft",
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json={
                    "row_id": row_id,
                    "llm_call_id": node_result.llm_call_id,
                    "source_draft_row_id": first_row_id,
                    "template_name": template_name,
                    "base_safety": base_safety,
                    "rejected_candidate_row_id": rejected_row_id,
                    "content_source": content_source,
                    "draft_mode": DRAFT_MODE_STYLE_FIRST,
                    "notices": deepcopy(notices),
                    "style_step": deepcopy(decision),
                    **({"styled_draft_gate": deepcopy(styled_draft_gate)} if styled_draft_gate is not None else {}),
                    **({"style_reference_runtime": runtime_audit} if runtime_audit is not None else {}),
                    **(attempt_details_extra or {}),
                },
            )
        )
        self.session.flush()
        state.current_style_draft_row_id = row_id
        state.latest_valid_draft_row_id = row_id
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        self.session.flush()
        product = StyleGenerationResult(
            row_id=row_id,
            content=content,
            llm_call_id=node_result.llm_call_id,
            bundle_id=bundle["bundle_id"],
            bundle_hash=bundle["bundle_snapshot_hash"],
            execution_step_key=execution_step_key,
            notices=deepcopy(notices),
            styled_draft_gate=deepcopy(styled_draft_gate),
            style_step=deepcopy(decision),
        )
        self._emit_style_first_products(
            product,
            first_row_id=first_row_id,
            slot_key=slot_key,
            slot_order=slot_order,
            product_callback=product_callback,
            decision=decision,
        )
        return product

    def _style_first_candidates(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        policy: Any,
        first_row_id: str,
        first_content: str,
        author_note: str | None,
        n_candidates: int,
        step_reconciler: Callable[[str], None] | None,
        resume_bases: dict[str, StyleGenerationResult],
        resume_products: dict[str, StyleGenerationResult],
        product_callback: Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None,
    ) -> list[StyleGenerationResult]:
        """Best-of-N（作者手笔直起）：候选 = 首稿 + (N−1) 个定向修改，按读数 distance 排序（最像的在前）。

        首稿永远是槽位 initial:0（不调模型）；读数不可信 / 读不出时只有首稿一个候选。修改槽位不做分散度补候选
        （它们是按测得的差异定向改的，不是独立采样）。关键场景的匿名终选门不变（按正文去重后给作者选）。
        """
        with _length_band_slack_for(bundle, scene):
            thresholds = style_step.fidelity_thresholds()
            first_reading, first_reading_id = self._record_first_draft_reading(
                scene, policy, first_row_id, first_content, thresholds
            )
            usable = first_reading is not None and first_reading.reliable
            try:
                base_temp = self._llm_runner.task_config("style_draft").temperature
            except KeyError:
                base_temp = 0.7
            slot_count = max(1, int(n_candidates)) if usable else 1
            results: list[tuple[StyleGenerationResult, int]] = []
            for idx in range(slot_count):
                slot_key = f"initial:{idx}"
                if slot_key in resume_products:
                    results.append((resume_products[slot_key], idx))
                    continue
                row_id = versioned_scene_artifact_id("draft_style_cand", scene.scene_id, bundle) + f"_{idx}"
                temperature = round(min(2.0, max(0.0, float(base_temp) + 0.05 * idx)), 3)
                result = self._style_first_step(
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    policy=policy,
                    first_row_id=first_row_id,
                    first_content=first_content,
                    author_note=author_note,
                    row_id=row_id,
                    slot_key=slot_key,
                    slot_order=idx,
                    execution_step_key=f"style_draft:{idx}",
                    resume_base=resume_bases.get(slot_key),
                    product_callback=product_callback,
                    step_reconciler=step_reconciler,
                    first_reading=first_reading,
                    first_reading_id=first_reading_id,
                    candidate_mode=idx > 0,
                    force_accept=idx == 0,
                    temperature_override=temperature if idx > 0 else None,
                    attempt_details_extra={
                        "source_neutral_draft_row_id": first_row_id,
                        "candidate_index": idx,
                        "n_candidates": n_candidates,
                        **({"temperature_override": temperature} if idx > 0 else {}),
                    },
                )
                results.append((result, idx))
            ranked = self._rank_style_first_candidates(
                results, policy=policy, first_reading=first_reading, first_row_id=first_row_id
            )
            best = ranked[0]
            state.current_style_draft_row_id = best.row_id
            state.latest_valid_draft_row_id = best.row_id
            state.current_bundle_id = bundle["bundle_id"]
            state.current_bundle_hash = bundle["bundle_snapshot_hash"]
            if len(ranked) >= 2:
                state.candidate_dispersion_score = round(_candidate_dispersion([c.content for c in ranked]), 4)
            self.session.flush()
            return ranked

    def _rank_style_first_candidates(
        self,
        results: list[tuple[StyleGenerationResult, int]],
        *,
        policy: Any,
        first_reading: Any,
        first_row_id: str,
    ) -> list[StyleGenerationResult]:
        """按读数 distance 升序排（读不出的排最后，平手时槽位靠前的在前——首稿赢平手）；写每个候选的排序审计。"""
        from novel_system.services.literary_quality import adversarial_rank_score

        scored: list[tuple[StyleGenerationResult, int, Any]] = []
        for result, idx in results:
            if (result.content or "") == "":
                reading = None
            elif result.lineage == LINEAGE_FIRST_DRAFT_ACCEPTED or idx == 0:
                reading = first_reading
            else:
                try:
                    reading = style_readings.reading_for_text(self.session, policy, result.content)
                except Exception:  # noqa: BLE001 — 读不出排最后
                    reading = None
            scored.append((result, idx, reading))
        scored.sort(
            key=lambda item: (
                item[2] is None,
                float(item[2].distance) if item[2] is not None else 0.0,
                item[1],
            )
        )
        seen_texts: dict[str, str] = {}
        ranked: list[StyleGenerationResult] = []
        max_percentile = style_step.fidelity_thresholds().style_step_max_percentile
        for rank, (result, idx, reading) in enumerate(scored):
            normalized = (result.content or "").strip()
            duplicate_of = seen_texts.get(normalized)
            seen_texts.setdefault(normalized, result.row_id)
            copy_passed: bool | None = None
            try:
                from novel_system.services.reference_copy_gate import check_reference_copy

                copy_passed = not check_reference_copy(self.session, result.content or "", policy=policy).blocked
            except Exception:  # noqa: BLE001 — 抄袭门查不成：候选按未过处理（终选门会剔除）
                copy_passed = False
            result.ranking_audit = {
                "row_id": result.row_id,
                "rank": rank,
                "selected": rank == 0,
                "selection_reason": "fidelity_distance",
                "slot_index": idx,
                "quality_score": round(float(adversarial_rank_score(result.content or "")), 6),
                "style_score": None,
                "fidelity_distance": getattr(reading, "distance", None),
                "fidelity_percentile": getattr(reading, "percentile", None),
                "within_range": (
                    within_author_range(reading, max_percentile=max_percentile) if reading is not None else None
                ),
                "duplicate_of_row_id": duplicate_of,
                "plagiarism_checked": True,
                "plagiarism_passed": copy_passed,
                "rerank": {"applied_mode": "fidelity_distance", "reason": None},
            }
            ranked.append(result)
        return ranked

    def generate_style_patch(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        source_style_draft_row_id: str,
        source_style_content: str,
        rewrite_brief: list[str],
        source_qc_report_id: str,
        execution_step_key: str = "soft_patch:0",
    ) -> StyleGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        # 2026-09-14 风格保真修补:有绑定时软补丁低温、未点名的句子逐字保留——补丁的 schema 仍要求
        # 返回整篇 scene_text,温度 0.8 会把整场措辞重掷一遍。neutral_first 不变。
        style_first = style_policy_for_bundle(bundle).defers_house_taste()
        result = self._run_style_generation(
            scene=scene,
            state=state,
            bundle=bundle,
            row_id=versioned_scene_artifact_id("draft_style_patch", scene_id, bundle),
            stage="style_patch",
            llm_step="soft_patch",
            neutral_content=source_style_content,
            source_label="Current Style Draft",
            source_row_id=source_style_draft_row_id,
            extra_instruction=(
                "Apply only the controlled patch brief; do not rewrite the full scene."
                + (
                    " Keep every sentence the brief does not name verbatim; for the sentences you do change, "
                    "the [STYLE_REFERENCE] block is the style authority."
                    if style_first
                    else ""
                )
            ),
            temperature_override=0.3 if style_first else None,
            patch_brief=rewrite_brief,
            source_draft_row_id=source_style_draft_row_id,
            source_draft_content=source_style_content,
            client_kind="patch",
            execution_step_key=execution_step_key,
            attempt_details_extra={
                "source_qc_report_id": source_qc_report_id,
                "source_style_draft_row_id": source_style_draft_row_id,
                "rewrite_brief": rewrite_brief,
            },
            # 风格参考 v3（P5b）：作者手笔直起时软补丁按改稿口径渲染（「只改不像的地方，已经像的原样留下」）
            render_role=ROLE_REVISE if style_first else None,
        )
        state.soft_patch_count += 1
        return result

    def generate_near_final_rewrite(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        source_draft_row_id: str,
        source_content: str,
        revision_brief: list[str],
        source_evaluation_id: str,
        execution_step_key: str = "near_final_rewrite:0",
    ) -> StyleGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        return self._run_style_generation(
            scene=scene,
            state=state,
            bundle=bundle,
            row_id=versioned_scene_artifact_id(
                "draft_near_final_rewrite", scene_id, bundle
            ),
            stage="near_final_rewrite",
            llm_step="scene_literary_rewrite",
            neutral_content=source_content,
            source_label="Near-Final Draft Under Review",
            source_row_id=source_draft_row_id,
            extra_instruction=(
                "Rewrite the full scene under the same facts. Treat the brief below as a literary rewrite brief, "
                "not a local patch request."
            ),
            patch_brief=revision_brief,
            source_draft_row_id=source_draft_row_id,
            source_draft_content=source_content,
            client_kind="style",
            execution_step_key=execution_step_key,
            attempt_details_extra={
                "source_evaluation_id": source_evaluation_id,
                "source_style_draft_row_id": source_draft_row_id,
                "rewrite_brief": revision_brief,
            },
        )

    def _run_style_generation(self, **kwargs: Any) -> StyleGenerationResult:
        """风格通道公共入口:按 bundle 的 StylePolicy(与场景的呈现方式)设长度带放宽,再进真正的实现。"""
        with _length_band_slack_for(kwargs.get("bundle"), kwargs.get("scene")):
            return self._run_style_generation_inner(**kwargs)

    def _run_style_generation_inner(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        row_id: str,
        stage: str,
        llm_step: str,
        neutral_content: str,
        source_label: str,
        source_row_id: str,
        extra_instruction: str,
        source_draft_row_id: str,
        source_draft_content: str,
        client_kind: str,
        patch_brief: list[str] | None = None,
        attempt_details_extra: dict[str, Any] | None = None,
        temperature_override: float | None = None,
        extra_system_prefix: str | None = None,
        execution_step_key: str | None = None,
        product_slot_key: str | None = None,
        product_slot_order: int | None = None,
        resume_base: StyleGenerationResult | None = None,
        product_callback: (
            Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None
        ) = None,
        step_reconciler: Callable[[str], None] | None = None,
        render_role: str | None = None,
    ) -> StyleGenerationResult:
        fallback_llm_call_id = f"llm_call_{scene.scene_id}_{uuid.uuid4().hex[:12]}"
        started_at = time.perf_counter()
        prompt: dict[str, Any] | None = None

        try:
            template_name = (
                "scene_literary_rewrite"
                if llm_step == "scene_literary_rewrite"
                else "style_draft"
            )
            prompt = self._prompt_builder().build(bundle["snapshot"], template_name)
        except Exception as exc:
            self._persist_generation_failure(
                scene=scene,
                state=state,
                bundle=bundle,
                llm_call_id=fallback_llm_call_id,
                step=llm_step,
                execution_step_key=execution_step_key,
                started_at=started_at,
                task_config=None,
                prompt=prompt,
                request_summary={},
                exc=exc,
                source_draft_row_id=source_draft_row_id,
            )
            raise

        # §6.3 diversification: prepend caller-supplied system prefix (prompt variation / style emphasis)
        if extra_system_prefix and prompt is not None:
            injected = dict(prompt)
            injected["system_prompt"] = extra_system_prefix + (
                prompt.get("system_prompt") or ""
            )
            prompt = injected
        base_prompt = prompt
        style_first = style_policy_for_bundle(bundle).style_first

        if stage == "style_draft":
            extra_instruction += _style_length_instruction(
                scene,
                source_length=_visible_char_count(neutral_content),
                style_first=style_first,
            )

        user_prompt = self._build_style_user_prompt(
            base_prompt["user_prompt"],
            neutral_content=neutral_content,
            source_label=source_label,
            source_row_id=source_row_id,
            extra_instruction=extra_instruction,
            patch_brief=patch_brief,
        )
        prompt = self._inject_style_reference(
            base_prompt,
            scene,
            task_type="scene_generation",
            bundle=bundle,
            context_text=neutral_content,
            final_user_prompt=user_prompt,
            placement=PLACEMENT_USER_TAIL,
            role=render_role,
        )
        user_prompt = apply_style_user_tail(prompt, user_prompt)
        # v2（规格 §2.W5.6）：注入命中与否、回退中性稿、styled-draft gate 命中都进
        # 同一份 notices——随结果对象返回并写进 AttemptTracker，绝不静默。
        notices: list[dict[str, Any]] = style_injection_notices(prompt)
        styled_draft_gate: dict[str, Any] | None = None
        if resume_base is None:
            node_id = "style_patch" if llm_step == "soft_patch" else llm_step
            try:
                node_result = self._llm_runner.run(
                    scene_id=scene.scene_id,
                    chapter_id=scene.chapter_id,
                    bundle_id=bundle["bundle_id"],
                    bundle_hash=bundle["bundle_snapshot_hash"],
                    node_id=node_id,
                    step=llm_step,
                    prompt=prompt,
                    user_prompt=user_prompt,
                    source_draft_row_id=source_draft_row_id,
                    source_draft_content=source_draft_content,
                    temperature_override=temperature_override,
                    execution_step_key=execution_step_key,
                )
                style_content = _extract_scene_text(node_result.response)
                paragraph_shape_audit: dict[str, Any] | None = None
                if stage == "style_draft":
                    style_content, paragraph_shape_audit = (
                        _normalize_style_paragraph_shape(
                            bundle=bundle,
                            text=style_content,
                        )
                    )
            except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
                self._record_runner_failure_attempt(
                    scene=scene,
                    state=state,
                    bundle=bundle,
                    step=llm_step,
                    prompt=prompt,
                    exc=exc,
                    source_draft_row_id=source_draft_row_id,
                )
                if isinstance(exc, LLMNodeExecutionError):
                    self._raise_original_runner_error(exc)
                raise

            base_safety = _assess_style_base_rewrite(
                scene=scene,
                source_content=neutral_content,
                rewritten_content=style_content,
            )
            rejected_candidate_row_id: str | None = None
            repair_source_row_id = row_id
            repair_source_content = style_content
            if stage == "style_draft" and not base_safety["accepted"]:
                rejected_hash = hashlib.sha256(
                    style_content.encode("utf-8")
                ).hexdigest()[:10]
                rejected_candidate_row_id = f"{row_id}_rejected_{rejected_hash}"
                self.session.add(
                    SceneDraft(
                        row_id=rejected_candidate_row_id,
                        scene_id=scene.scene_id,
                        chapter_id=scene.chapter_id,
                        stage="style_rejected",
                        status="rejected",
                        content=style_content,
                        source_bundle_id=bundle["bundle_id"],
                        source_bundle_hash=bundle["bundle_snapshot_hash"],
                        generation_llm_call_id=node_result.llm_call_id,
                    )
                )
                repair_source_row_id = rejected_candidate_row_id
                repair_source_content = style_content
                # 已批准的来源稿是安全降级真源(neutral_first:中性稿;style_first:已按参考
                # 手笔写成的首稿)。保留 provider 原稿为独立 rejected 行,主 style_draft 行只
                # 承载可继续进入 QC/候选选择的安全文本。
                style_content = neutral_content
                notices.append(
                    style_notice(
                        STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL,
                        (
                            "风格复读稿因长度 / 必含项 / 禁用内容 / 文本完整性未通过确定性安全门，"
                            "已回退为首稿（首稿本身已按参考作者手笔写成）；后续修复通道会尝试一次安全修复。"
                            if style_first
                            else "风格稿因长度 / 必含项 / 禁用内容 / 文本完整性未通过确定性安全门，"
                            "已回退为已批准的中性稿；后续修复通道会尝试一次带风格的安全修复。"
                        ),
                        severity="warning",
                        reasons=list(base_safety.get("reasons") or []),
                        rejected_candidate_row_id=rejected_candidate_row_id,
                        draft_mode=(DRAFT_MODE_STYLE_FIRST if style_first else None),
                    )
                )
            self.session.add(
                SceneDraft(
                    row_id=row_id,
                    scene_id=scene.scene_id,
                    chapter_id=scene.chapter_id,
                    stage=stage,
                    content=style_content,
                    source_bundle_id=bundle["bundle_id"],
                    source_bundle_hash=bundle["bundle_snapshot_hash"],
                    generation_llm_call_id=node_result.llm_call_id,
                )
            )
            self.session.flush()

            if (
                stage in _STYLED_GATE_GENERATION_STAGES
                and rejected_candidate_row_id is None
            ):
                # v2（规格 §2.W5.5）styled-draft gate：样例预算放大后，每一份落库的
                # provider 风格化输出都必须过一次确定性抄袭 + 冻结禁用词检查。
                # style_draft：命中只记 notice 与审计，升级到人工复核由 soft_qc 阶段的同一
                # gate 完成；near_final_rewrite：输出会直接成为终稿、没有后续 QC，
                # orchestrator 读 result.styled_draft_gate 对抄袭裁决采取行动。
                styled_draft_gate = self._styled_draft_style_gate(
                    scene, style_content, bundle=bundle, stage=stage
                )
                notices.extend(_styled_draft_gate_notices(styled_draft_gate))

            runtime_audit = (
                deepcopy(prompt["_style_reference_runtime_audit"])
                if isinstance(prompt.get("_style_reference_runtime_audit"), dict)
                else None
            )
            fallback_content_source = (
                FIRST_DRAFT_FALLBACK_CONTENT_SOURCE
                if style_first
                else "approved_neutral_fallback"
            )
            if runtime_audit is not None:
                runtime_audit["generation_outcome"] = (
                    fallback_content_source
                    if rejected_candidate_row_id is not None
                    else "provider_style_output"
                )
                runtime_audit["draft_mode"] = (
                    DRAFT_MODE_STYLE_FIRST if style_first else DRAFT_MODE_NEUTRAL_FIRST
                )
                runtime_audit["notice_codes"] = [item["code"] for item in notices]
            self.session.add(
                AttemptTracker(
                    scene_id=scene.scene_id,
                    chapter_id=scene.chapter_id,
                    step=llm_step,
                    status="completed",
                    source_bundle_id=bundle["bundle_id"],
                    details_json={
                        "row_id": row_id,
                        "llm_call_id": node_result.llm_call_id,
                        "source_draft_row_id": source_draft_row_id,
                        "base_safety": base_safety,
                        "rejected_candidate_row_id": rejected_candidate_row_id,
                        "content_source": (
                            fallback_content_source
                            if rejected_candidate_row_id is not None
                            else "provider_style_output"
                        ),
                        "notices": deepcopy(notices),
                        **(
                            {"styled_draft_gate": deepcopy(styled_draft_gate)}
                            if styled_draft_gate is not None
                            else {}
                        ),
                        **(
                            {
                                "paragraph_shape_normalization": (
                                    paragraph_shape_audit
                                )
                            }
                            if paragraph_shape_audit is not None
                            else {}
                        ),
                        **(
                            {"style_reference_runtime": runtime_audit}
                            if runtime_audit is not None
                            else {}
                        ),
                        **(attempt_details_extra or {}),
                    },
                )
            )
            self.session.flush()

            state.current_style_draft_row_id = row_id
            state.latest_valid_draft_row_id = row_id
            state.current_bundle_id = bundle["bundle_id"]
            state.current_bundle_hash = bundle["bundle_snapshot_hash"]
            self.session.flush()
            base_result = StyleGenerationResult(
                row_id=row_id,
                content=style_content,
                llm_call_id=node_result.llm_call_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                execution_step_key=execution_step_key,
                notices=deepcopy(notices),
                styled_draft_gate=deepcopy(styled_draft_gate),
            )
            if product_callback is not None and product_slot_key is not None:
                product_callback(
                    product_slot_key,
                    "base",
                    base_result,
                    {
                        "slot_order": product_slot_order,
                        "source_neutral_draft_row_id": source_draft_row_id,
                        "gate_decision": None,
                        "source_base_row_id": None,
                    },
                )
        else:
            if (
                resume_base.row_id != row_id
                or resume_base.bundle_id != bundle["bundle_id"]
                or resume_base.bundle_hash != bundle["bundle_snapshot_hash"]
                or resume_base.execution_step_key != execution_step_key
            ):
                raise DomainError(
                    "RUN_CHECKPOINT_CORRUPT",
                    "resumed style base does not match its locked work item",
                    status_code=409,
                )
            base_result = resume_base
            style_content = resume_base.content
            base_safety = _resume_base_safety(
                self.session,
                scene_id=scene.scene_id,
                row_id=resume_base.row_id,
                fallback=_assess_style_base_rewrite(
                    scene=scene,
                    source_content=neutral_content,
                    rewritten_content=style_content,
                ),
            )
            repair_source_row_id, repair_source_content = (
                _resume_style_repair_source(
                    self.session,
                    scene_id=scene.scene_id,
                    row_id=resume_base.row_id,
                    fallback_row_id=resume_base.row_id,
                    fallback_content=style_content,
                )
            )

        if stage == "style_draft":
            quality_source_content = (
                repair_source_content
                if not base_safety["accepted"]
                else style_content
            )
            quality_gate = _anti_template_quality_gate(
                quality_source_content,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
            )
            quality_gate["base_safety"] = base_safety
            if style_first:
                # 2026-09-12 风格直起:房风维度整体让位——只记录为 advisory_findings,不触发
                # 去模板改写;参考派生的形状包络(style_anchor_audit)与安全门仍可触发修复。
                quality_gate = _defer_house_taste_gate(quality_gate)
            style_anchor_audit = _assess_style_anchor_conformance(
                bundle=bundle,
                text=quality_source_content,
            )
            quality_gate["style_anchor_audit"] = style_anchor_audit
            if base_safety["accepted"] and style_anchor_audit.get("requires_repair"):
                quality_gate["triggered"] = True
                quality_gate["rewrite_pass"] = 1
                anchor_signal_id = f"quality:scene:{scene.scene_id}:style_structure"
                if "style_structure" not in quality_gate["risk_dimensions"]:
                    quality_gate["risk_dimensions"].insert(0, "style_structure")
                if anchor_signal_id not in quality_gate["quality_signal_ids"]:
                    quality_gate["quality_signal_ids"].insert(0, anchor_signal_id)
                quality_gate["findings"].insert(
                    0,
                    {
                        "dimension": "style_structure",
                        "severity": "taste",
                        "issue": "The safe style draft is outside a high-confidence reference-derived prose-shape envelope.",
                        "evidence_excerpt": "",
                        "recommendation": " ".join(
                            str(item)
                            for item in style_anchor_audit.get("repair_directions", [])
                            if str(item).strip()
                        ),
                        "quality_signal_id": anchor_signal_id,
                        "scene_id": scene.scene_id,
                        "chapter_id": scene.chapter_id,
                    },
                )
            if not base_safety["accepted"]:
                quality_gate["triggered"] = True
                quality_gate["rewrite_pass"] = 1
                if "style_safety" not in quality_gate["risk_dimensions"]:
                    quality_gate["risk_dimensions"].append("style_safety")
                safety_signal_id = f"quality:scene:{scene.scene_id}:style_safety"
                if safety_signal_id not in quality_gate["quality_signal_ids"]:
                    quality_gate["quality_signal_ids"].append(safety_signal_id)
                quality_gate["findings"].append(
                    {
                        "dimension": "style_safety",
                        "severity": "blocking",
                        "issue": "The first style rewrite violated deterministic fact, length, or text-integrity constraints.",
                        "evidence_excerpt": "",
                        "recommendation": (
                            "Repair the rejected style draft once: restore every required beat, stay inside the "
                            "scene target length band, and retain its safe style choices."
                        ),
                        "quality_signal_id": safety_signal_id,
                        "scene_id": scene.scene_id,
                        "chapter_id": scene.chapter_id,
                    }
                )
            if quality_gate["triggered"]:
                use_style_salvage = bool(
                    not base_safety["accepted"]
                    and _requires_style_salvage(base_safety)
                )
                de_template_step_key = (
                    (
                        f"{execution_step_key}:style_salvage"
                        if use_style_salvage
                        else f"{execution_step_key}:de_template"
                    )
                    if execution_step_key
                    else None
                )
                if step_reconciler is not None and de_template_step_key is not None:
                    step_reconciler(de_template_step_key)
                if use_style_salvage:
                    (
                        de_template_result,
                        de_template_outcome,
                    ) = self._run_style_salvage_pass(
                        scene=scene,
                        state=state,
                        bundle=bundle,
                        checkpoint_base_row_id=row_id,
                        rejected_style_row_id=repair_source_row_id,
                        rejected_style_content=repair_source_content,
                        neutral_row_id=source_draft_row_id,
                        neutral_content=neutral_content,
                        quality_gate=quality_gate,
                        execution_step_key=de_template_step_key,
                    )
                else:
                    (
                        de_template_result,
                        de_template_outcome,
                    ) = self._run_de_template_pass(
                        scene=scene,
                        state=state,
                        bundle=bundle,
                        base_prompt=base_prompt,
                        checkpoint_base_row_id=row_id,
                        source_row_id=(
                            repair_source_row_id
                            if not base_safety["accepted"]
                            else row_id
                        ),
                        source_content=quality_source_content,
                        authoritative_row_id=(
                            source_draft_row_id
                            if not base_safety["accepted"]
                            else None
                        ),
                        authoritative_content=(
                            neutral_content if not base_safety["accepted"] else None
                        ),
                        quality_gate=quality_gate,
                        execution_step_key=de_template_step_key,
                    )
                remaining_reasons = set(
                    (de_template_outcome.get("acceptance") or {}).get("reasons")
                    or []
                )
                if (
                    de_template_result is None
                    and not base_safety["accepted"]
                    and not use_style_salvage
                    and remaining_reasons == {"target_length_not_met"}
                ):
                    # 第一遍整篇安全修复可能已恢复事实/完整性，
                    # 但仍略超出长度带。此时问题已收敛为纯长度，只允许
                    # 再走一次编号式局部补丁，不再整篇重写。
                    followup_row_id = de_template_outcome.get("row_id")
                    followup_source = (
                        self.session.get(SceneDraft, followup_row_id)
                        if isinstance(followup_row_id, str) and followup_row_id
                        else None
                    )
                    if followup_source is not None:
                        followup_step_key = (
                            f"{de_template_step_key}:length_patch_followup"
                            if de_template_step_key
                            else None
                        )
                        if (
                            step_reconciler is not None
                            and followup_step_key is not None
                        ):
                            step_reconciler(followup_step_key)
                        followup_quality_gate = deepcopy(quality_gate)
                        followup_quality_gate["base_safety"] = deepcopy(
                            de_template_outcome["acceptance"]
                        )
                        prior_repair_outcome = de_template_outcome
                        (
                            de_template_result,
                            de_template_outcome,
                        ) = self._run_de_template_pass(
                            scene=scene,
                            state=state,
                            bundle=bundle,
                            base_prompt=base_prompt,
                            checkpoint_base_row_id=row_id,
                            source_row_id=followup_source.row_id,
                            source_content=followup_source.content,
                            authoritative_row_id=source_draft_row_id,
                            authoritative_content=neutral_content,
                            quality_gate=followup_quality_gate,
                            execution_step_key=followup_step_key,
                        )
                        de_template_outcome["prior_repair_outcome"] = (
                            prior_repair_outcome
                        )
                if de_template_result is not None:
                    # 修复稿替代基稿返回时，基稿阶段产生的 notices 一并随行。
                    de_template_result.notices = [
                        *deepcopy(notices),
                        *[
                            item
                            for item in (de_template_result.notices or [])
                            if item not in notices
                        ],
                    ]
                    if product_callback is not None and product_slot_key is not None:
                        product_callback(
                            product_slot_key,
                            "final",
                            de_template_result,
                            {
                                "slot_order": product_slot_order,
                                "source_neutral_draft_row_id": source_draft_row_id,
                                "gate_decision": quality_gate,
                                "source_base_row_id": base_result.row_id,
                                "de_template_outcome": de_template_outcome,
                            },
                        )
                    return de_template_result
            if product_callback is not None and product_slot_key is not None:
                product_callback(
                    product_slot_key,
                    "final",
                    base_result,
                    {
                        "slot_order": product_slot_order,
                        "source_neutral_draft_row_id": source_draft_row_id,
                        "gate_decision": quality_gate,
                        "source_base_row_id": base_result.row_id,
                        "de_template_outcome": (
                            de_template_outcome
                            if quality_gate["triggered"]
                            else {"status": "not_required"}
                        ),
                    },
                )

        return base_result

    def _run_style_salvage_pass(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        checkpoint_base_row_id: str,
        rejected_style_row_id: str,
        rejected_style_content: str,
        neutral_row_id: str,
        neutral_content: str,
        quality_gate: dict[str, Any],
        execution_step_key: str | None,
    ) -> tuple[StyleGenerationResult | None, dict[str, Any]]:
        source_suffix = hashlib.sha1(
            rejected_style_row_id.encode("utf-8")
        ).hexdigest()[:10]
        row_id = (
            versioned_scene_artifact_id(
                "draft_style_salvage",
                scene.scene_id,
                bundle,
            )
            + f"_{source_suffix}"
        )
        prompt = self._prompt_builder().build(
            bundle["snapshot"],
            "style_salvage_patch",
        )
        editable_segment_ids = _style_salvage_editable_segment_ids(neutral_content)
        annotated_source, _ = _annotate_style_length_patch_source(
            neutral_content,
            editable_segment_ids=editable_segment_ids,
        )
        _constrain_style_salvage_schema(
            prompt,
            editable_segment_ids=editable_segment_ids,
        )
        user_prompt = self._build_style_user_prompt(
            prompt["user_prompt"],
            neutral_content=annotated_source,
            source_label=(
                "Segment-addressed First Draft (already in the reference author's hand) for Style Salvage"
                if style_policy_for_bundle(bundle).style_first
                else "Segment-addressed Approved Neutral Draft for Style Salvage"
            ),
            source_row_id=neutral_row_id,
            extra_instruction=_style_salvage_instruction(
                scene,
                source_content=neutral_content,
                editable_segment_ids=editable_segment_ids,
            ),
        )
        prompt = self._inject_style_reference(
            prompt,
            scene,
            task_type="scene_generation",
            bundle=bundle,
            context_text=neutral_content,
            final_user_prompt=user_prompt,
            placement=PLACEMENT_USER_TAIL,
        )
        user_prompt = apply_style_user_tail(prompt, user_prompt)
        salvage_audit: dict[str, Any]
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id="style_patch",
                step="style_salvage_patch",
                prompt=prompt,
                user_prompt=user_prompt,
                source_draft_row_id=neutral_row_id,
                source_draft_content=neutral_content,
                temperature_override=0.2,
                execution_step_key=execution_step_key,
            )
            try:
                rewritten_content, salvage_audit = _apply_style_salvage_patch(
                    source_content=neutral_content,
                    response=node_result.response,
                    scene=scene,
                    llm_call_id=node_result.llm_call_id,
                )
            except SceneGenerationPostprocessError as patch_exc:
                rewritten_content = neutral_content
                salvage_audit = {
                    "version": "style_salvage_patch_v1",
                    "valid": False,
                    "reason": str(patch_exc).removeprefix(
                        "style salvage patch invalid: "
                    ),
                }
        except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
            self._record_runner_failure_attempt(
                scene=scene,
                state=state,
                bundle=bundle,
                step="style_salvage_patch",
                prompt=prompt,
                exc=exc,
                source_draft_row_id=neutral_row_id,
            )
            return None, {
                "status": "failed",
                "llm_call_id": exc.llm_call_id,
                "execution_step_key": execution_step_key,
                "error_code": exc.error_code,
            }

        rewritten_content, paragraph_shape_audit = _normalize_style_paragraph_shape(
            bundle=bundle,
            text=rewritten_content,
        )
        conformance = _assess_style_rewrite_conformance(
            bundle=bundle,
            source_content=neutral_content,
            rewritten_content=rewritten_content,
        )
        acceptance = _assess_de_template_rewrite(
            house_taste_deferred=style_policy_for_bundle(bundle).defers_house_taste(),
            scene=scene,
            source_content=neutral_content,
            authoritative_content=neutral_content,
            rewritten_content=rewritten_content,
            source_quality_gate=quality_gate,
            style_conformance=conformance,
        )
        salvage_reasons: list[str] = []
        if not salvage_audit.get("valid"):
            salvage_reasons.append("style_salvage_patch_invalid")
        if conformance.get("comparable") is not True:
            salvage_reasons.append("style_salvage_conformance_unavailable")
        elif float(conformance.get("score_delta") or 0.0) < -0.01:
            salvage_reasons.append("style_salvage_conformance_regressed")
        if salvage_reasons:
            acceptance["reasons"] = list(
                dict.fromkeys([*acceptance.get("reasons", []), *salvage_reasons])
            )
            acceptance["accepted"] = False
        acceptance["style_salvage_non_regression_enforced"] = True
        acceptance["style_salvage_regression_tolerance"] = 0.01

        self.session.add(
            SceneDraft(
                row_id=row_id,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                stage="style_salvage",
                status="active" if acceptance["accepted"] else "rejected",
                content=rewritten_content,
                source_bundle_id=bundle["bundle_id"],
                source_bundle_hash=bundle["bundle_snapshot_hash"],
                generation_llm_call_id=node_result.llm_call_id,
            )
        )
        self.session.flush()
        details = {
            "row_id": row_id,
            "llm_call_id": node_result.llm_call_id,
            "source_style_draft_row_id": checkpoint_base_row_id,
            "rejected_style_seed_row_id": rejected_style_row_id,
            "rejected_style_seed_visible_chars": _visible_char_count(
                rejected_style_content
            ),
            "authoritative_source_row_id": neutral_row_id,
            "quality_gate": quality_gate,
            "acceptance": acceptance,
            "style_salvage": salvage_audit,
            "paragraph_shape_normalization": paragraph_shape_audit,
            **(
                {
                    "style_reference_runtime": deepcopy(
                        prompt["_style_reference_runtime_audit"]
                    )
                }
                if isinstance(prompt.get("_style_reference_runtime_audit"), dict)
                else {}
            ),
        }
        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step="style_salvage_patch",
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json=details,
            )
        )
        self.session.flush()
        outcome = {
            "status": "completed" if acceptance["accepted"] else "rejected",
            "llm_call_id": node_result.llm_call_id,
            "execution_step_key": execution_step_key,
            "artifact_execution_id": current_llm_execution_id(),
            "accounting_status": "settled",
            "row_id": row_id,
            "acceptance": acceptance,
            "style_salvage": salvage_audit,
            "paragraph_shape_normalization": paragraph_shape_audit,
        }
        if not acceptance["accepted"]:
            return None, outcome
        state.current_style_draft_row_id = row_id
        state.latest_valid_draft_row_id = row_id
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        self.session.flush()
        return (
            StyleGenerationResult(
                row_id=row_id,
                content=rewritten_content,
                llm_call_id=node_result.llm_call_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                execution_step_key=execution_step_key,
                artifact_execution_id=current_llm_execution_id(),
            ),
            outcome,
        )

    def _run_de_template_pass(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        base_prompt: dict[str, Any],
        checkpoint_base_row_id: str,
        source_row_id: str,
        source_content: str,
        authoritative_row_id: str | None,
        authoritative_content: str | None,
        quality_gate: dict[str, Any],
        execution_step_key: str | None,
    ) -> tuple[StyleGenerationResult | None, dict[str, Any]]:
        # 每个触发去模板的候选（source_row_id 已带 _{idx}/_retry_{idx}）必须派生唯一的去模板稿 row_id，
        # 否则 Best-of-N 下 ≥2 个候选都触发反模板闸时，第二条 SceneDraft 撞主键 → IntegrityError → 整跑崩溃。
        # SceneDraft.row_id 为 opaque 主键、不被下游解析，故追加 source_row_id 的短哈希后缀即可（唯一且长度有界）。
        source_suffix = hashlib.sha1(source_row_id.encode("utf-8")).hexdigest()[:10]
        row_id = f"{versioned_scene_artifact_id('draft_style_de_template', scene.scene_id, bundle)}_{source_suffix}"
        is_safety_repair = authoritative_content is not None
        base_safety_reasons = set(
            (quality_gate.get("base_safety") or {}).get("reasons") or []
        )
        is_length_patch = bool(
            is_safety_repair
            and base_safety_reasons == {"target_length_not_met"}
            and _parse_numeric_length_band(scene.target_length_band) is not None
        )
        length_patch_audit: dict[str, Any] | None = None
        # 2026-09-14 风格保真修补:有绑定时修复 / 补丁也在作者的原文面前进行(见下方注入分支),
        # 且扩缩指令改用作者自己的手段;neutral_first 逐字不变。
        style_first = style_policy_for_bundle(bundle).defers_house_taste()
        if is_length_patch:
            # 整篇“修长度”在真实模型上会稳定退化成摘要。程序先给原文分段编号，
            # 模型只提交 segment_id + new_text；原文定位和套用不依赖模型复制精度。
            prompt = self._prompt_builder().build(
                bundle["snapshot"],
                "style_length_patch",
            )
            editable_segment_ids = _style_length_patch_editable_segment_ids(
                source_content,
                scene,
            )
            annotated_source, _ = _annotate_style_length_patch_source(
                source_content,
                editable_segment_ids=editable_segment_ids,
            )
            _constrain_style_length_patch_schema(
                prompt,
                editable_segment_ids=editable_segment_ids,
                scene=scene,
                source_length=_visible_char_count(source_content),
            )
            user_prompt = self._build_style_user_prompt(
                prompt["user_prompt"],
                neutral_content=annotated_source,
                source_label="Segment-addressed Length-only Rejected Style Draft",
                source_row_id=source_row_id,
                extra_instruction=_style_length_patch_instruction(
                    scene,
                    source_length=_visible_char_count(source_content),
                    editable_segment_ids=editable_segment_ids,
                    style_first=style_first,
                ),
            )
        else:
            if is_safety_repair:
                repair_brief = _style_safety_repair_brief(
                    scene=scene,
                    source_content=source_content,
                    authoritative_content=authoritative_content,
                    style_first=style_first,
                )
            else:
                repair_brief = _de_template_rewrite_brief(quality_gate)
            repair_length_instruction = _style_repair_length_instruction(
                scene,
                source_length=_visible_char_count(source_content),
            )
            user_prompt = self._build_style_user_prompt(
                (
                    _STYLE_SAFETY_REPAIR_TASK_PROMPT
                    if is_safety_repair
                    else _STYLE_DE_TEMPLATE_REPAIR_TASK_PROMPT
                ),
                neutral_content=source_content,
                source_label=(
                    "Rejected Style Draft Requiring One Safety Repair"
                    if authoritative_content is not None
                    else "Style Draft Requiring De-template Pass"
                ),
                source_row_id=source_row_id,
                extra_instruction=(
                    (
                        "Apply exactly one controlled safety repair. Preserve the rejected draft's reusable style; "
                        "fix only deterministic fact, length, forbidden-content, or text-integrity violations. "
                        "Do not perform a separate de-template rewrite or flatten the prose back to a neutral draft."
                        if is_safety_repair
                        else
                        "Apply exactly one controlled de-template repair. Preserve facts, names, chronology, "
                        "required objects, ending function, and the draft's reusable style. Fix only the listed "
                        "quality violations; preserve the reference-derived distribution tendencies without "
                        "turning them into counts or punctuation quotas, and do not flatten the prose back to a neutral draft."
                    )
                    + repair_length_instruction
                    + (_STYLE_BOUND_REPAIR_CLAUSE if style_first else "")
                ),
                patch_brief=repair_brief,
                patch_heading=(
                    "Safety Repair Brief"
                    if is_safety_repair
                    else "De-template Rewrite Brief"
                ),
            )
        if is_length_patch:
            if style_first:
                # 2026-09-14 风格保真修补:有绑定时长度补丁也带 [STYLE_REFERENCE](样例、声音、
                # 正向、禁忌、红线),新增 / 替换的段落以作者手笔写。context_text=None:
                # [风格分布指导] 不按被拒稿的异常篇幅算「当前」偏差,局部补丁不会变成风格重写。
                prompt = self._inject_style_reference(
                    prompt,
                    scene,
                    task_type="scene_generation",
                    bundle=bundle,
                    context_text=None,
                    final_user_prompt=user_prompt,
                    placement=PLACEMENT_USER_TAIL,
                )
        elif is_safety_repair:
            if style_first:
                # 同上:安全修复只修硬约束,但修的时候仍要看着作者的原文。
                prompt = self._inject_style_reference(
                    base_prompt,
                    scene,
                    task_type="scene_generation",
                    bundle=bundle,
                    context_text=None,
                    final_user_prompt=user_prompt,
                    placement=PLACEMENT_USER_TAIL,
                )
            else:
                # 这一遍只负责把已生成的风格稿恢复到事实、长度与正文完整性硬约束内。
                # 再注入完整画像会按“不合格源稿”的异常篇幅重算段数/分号目标，并把
                # 一个局部修复重新变成风格重写；真实基准中这会诱发过度压缩与事实丢失。
                # 被拒稿本身已承载可复用风格，故安全修复只使用冻结的原始 style 模板。
                prompt = dict(base_prompt)
        else:
            prompt = self._inject_style_reference(
                base_prompt,
                scene,
                task_type="scene_generation",
                bundle=bundle,
                context_text=source_content,
                final_user_prompt=user_prompt,
                placement=PLACEMENT_USER_TAIL,
            )
        user_prompt = apply_style_user_tail(prompt, user_prompt)
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id="style_patch",
                step="de_template",
                prompt=prompt,
                user_prompt=user_prompt,
                source_draft_row_id=source_row_id,
                source_draft_content=source_content,
                # 二改是受约束的局部修补，不应继续用创作采样温度。安全
                # 修复最保守；普通去模板仍留少量改写空间。
                temperature_override=0.1 if is_safety_repair else 0.3,
                execution_step_key=execution_step_key,
            )
            if is_length_patch:
                try:
                    rewritten_content, length_patch_audit = (
                        _apply_style_length_patch(
                            source_content=source_content,
                            response=node_result.response,
                            scene=scene,
                            llm_call_id=node_result.llm_call_id,
                        )
                    )
                except SceneGenerationPostprocessError as patch_exc:
                    # Provider 已成功结算，但 replacement 本身不可安全套用。
                    # 保留原拒稿形成 settled+rejected 审计产物，不能伪报成
                    # provider failed，也不能让无效局部补丁触碰正文。
                    rewritten_content = source_content
                    length_patch_audit = {
                        "version": "style_length_patch_v3",
                        "valid": False,
                        "reason": str(patch_exc).removeprefix(
                            "style length patch invalid: "
                        ),
                    }
            else:
                rewritten_content = _extract_scene_text(node_result.response)
            rewritten_content, paragraph_shape_audit = (
                _normalize_style_paragraph_shape(
                    bundle=bundle,
                    text=rewritten_content,
                )
            )
        except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
            self._record_runner_failure_attempt(
                scene=scene,
                state=state,
                bundle=bundle,
                step="de_template",
                prompt=prompt,
                exc=exc,
                source_draft_row_id=source_row_id,
            )
            call = (
                self.session.get(LlmCall, exc.llm_call_id)
                if exc.llm_call_id
                else None
            )
            return None, {
                "status": "failed",
                "llm_call_id": exc.llm_call_id,
                "execution_step_key": execution_step_key,
                "artifact_execution_id": (
                    call.execution_id
                    if call is not None
                    else current_llm_execution_id()
                ),
                "accounting_status": (
                    call.accounting_status if call is not None else None
                ),
                "error_code": exc.error_code,
            }

        acceptance = _assess_de_template_rewrite(
            house_taste_deferred=style_policy_for_bundle(bundle).defers_house_taste(),
            scene=scene,
            source_content=source_content,
            authoritative_content=authoritative_content,
            rewritten_content=rewritten_content,
            source_quality_gate=quality_gate,
            style_conformance=_assess_style_rewrite_conformance(
                bundle=bundle,
                source_content=source_content,
                rewritten_content=rewritten_content,
            ),
        )

        self.session.add(
            SceneDraft(
                row_id=row_id,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                stage="de_template",
                status="active" if acceptance["accepted"] else "rejected",
                content=rewritten_content,
                source_bundle_id=bundle["bundle_id"],
                source_bundle_hash=bundle["bundle_snapshot_hash"],
                generation_llm_call_id=node_result.llm_call_id,
            )
        )
        self.session.flush()

        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step="de_template",
                status="completed",
                source_bundle_id=bundle["bundle_id"],
                details_json={
                    "row_id": row_id,
                    "llm_call_id": node_result.llm_call_id,
                    # Durable work-item checkpoint 的既有契约以 base row 为父项；
                    # 安全修复实际读取的 rejected provider row 另列，避免伪装血缘。
                    "source_style_draft_row_id": checkpoint_base_row_id,
                    "repair_source_style_draft_row_id": source_row_id,
                    "authoritative_source_row_id": authoritative_row_id,
                    "quality_gate": quality_gate,
                    "acceptance": acceptance,
                    "paragraph_shape_normalization": paragraph_shape_audit,
                    **(
                        {"length_patch": length_patch_audit}
                        if length_patch_audit is not None
                        else {}
                    ),
                    **(
                        {
                            "style_reference_runtime": deepcopy(
                                prompt["_style_reference_runtime_audit"]
                            )
                        }
                        if isinstance(
                            prompt.get("_style_reference_runtime_audit"), dict
                        )
                        else {}
                    ),
                },
            )
        )
        self.session.flush()

        outcome = {
            "status": "completed" if acceptance["accepted"] else "rejected",
            "llm_call_id": node_result.llm_call_id,
            "execution_step_key": execution_step_key,
            "artifact_execution_id": current_llm_execution_id(),
            "accounting_status": "settled",
            "row_id": row_id,
            "acceptance": acceptance,
            "repair_source_style_draft_row_id": source_row_id,
            "paragraph_shape_normalization": paragraph_shape_audit,
            **(
                {"length_patch": length_patch_audit}
                if length_patch_audit is not None
                else {}
            ),
        }
        if not acceptance["accepted"]:
            # 改写稿作为审计证据保留，但不能覆盖已验证的 base 指针。调用方收到
            # None 后会继续返回 base_result，并把 rejected outcome 写入 checkpoint。
            return None, outcome

        state.current_style_draft_row_id = row_id
        state.latest_valid_draft_row_id = row_id
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        self.session.flush()

        return (
            StyleGenerationResult(
                row_id=row_id,
                content=rewritten_content,
                llm_call_id=node_result.llm_call_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                execution_step_key=execution_step_key,
            ),
            outcome,
        )

    @staticmethod
    def _build_style_user_prompt(
        base_prompt: str,
        *,
        neutral_content: str,
        source_label: str,
        source_row_id: str,
        extra_instruction: str,
        patch_brief: list[str] | None = None,
        patch_heading: str = "Patch Brief",
    ) -> str:
        prompt_parts = [
            base_prompt,
            "",
            f"## {source_label}",
            neutral_content,
            "",
            f"Source Draft Row ID: {source_row_id}",
            extra_instruction,
        ]
        if patch_brief:
            prompt_parts.extend(
                [
                    "",
                    f"## {patch_heading}",
                    "\n".join(f"- {item}" for item in patch_brief),
                ]
            )
        if JSON_SCHEMA_INSTRUCTION not in base_prompt:
            prompt_parts.extend(["", JSON_SCHEMA_INSTRUCTION])
        return "\n".join(prompt_parts).strip()

    def _prompt_builder(self) -> PromptBuilder:
        if self._prompt_builder_instance is None:
            self._prompt_builder_instance = PromptBuilder()
        return self._prompt_builder_instance

    def _inject_style_reference(
        self,
        prompt: dict[str, Any] | None,
        scene: SceneCard | None,
        *,
        task_type: str = "scene_generation",
        bundle: dict[str, Any] | None = None,
        context_text: str | None = None,
        final_user_prompt: str | None = None,
        placement: str = "system",
        role: str | None = None,
        situation_tags: Sequence[str] | None = None,
        revise_dimensions: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        """PR-8 §5.1 — 把 active StyleProfile 注入到 prompt["system_prompt"] 头部。

        v2（W5）：核心逻辑抽成模块级 ``inject_style_reference_prefix``，qc_engine 在
        soft_qc 阶段复用同一前缀；本方法只做委派，契约不变。
        2026-09-22 风格参考优先:起草通道传 ``placement=PLACEMENT_USER_TAIL``——样例块落到
        user 消息末尾,调用方随后用 :func:`apply_style_user_tail` 接上。
        风格参考 v3（P5b）：调用方显式给角色（首稿 ``draft`` + bundle 冻结的场面标签；定向修改 / 软补丁 ``revise``
        + 要改的维）；不给时适配器按落点推断（与旧行为相同）。
        """
        extra: dict[str, Any] = {}
        if role is not None:
            extra["role"] = role
        if situation_tags is not None:
            extra["situation_tags"] = situation_tags
        if revise_dimensions:
            extra["revise_dimensions"] = revise_dimensions
        return inject_style_reference_prefix(
            self.session,
            prompt,
            scene,
            bundle,
            task_type=task_type,
            context_text=context_text,
            final_user_prompt=final_user_prompt,
            placement=placement,
            **extra,
        )

    def _styled_draft_style_gate(
        self,
        scene: SceneCard,
        style_content: str,
        *,
        bundle: dict[str, Any] | None = None,
        stage: str = "style_draft",
    ) -> dict[str, Any] | None:
        """v2（规格 §2.W5.5）styled-draft gate：对已落库的风格化输出跑抄袭 + 冻结禁用词。

        契约取自本次生成用的 bundle（与注入前缀同一冻结契约）；返回
        qc_engine.run_styled_draft_style_gate 的诊断字典；无绑定 → None；gate 自身失败 →
        ``verdict="unavailable"``（失败已 WARNING 落日志，不阻断生成，但必须可见——调用方
        据此发 STYLE_GATE_UNAVAILABLE notice）。``stage`` 为 style_draft 时升级到人工复核由
        soft_qc 阶段的同一 gate 完成；near_final_rewrite 由 orchestrator 直接处置。
        """
        from novel_system.services.qc_engine import (
            run_styled_draft_style_gate,
            styled_gate_unavailable_result,
        )

        try:
            return run_styled_draft_style_gate(
                self.session,
                scene,
                style_content,
                stage=stage,
                bundle=bundle,
            )
        except Exception as exc:  # noqa: BLE001 — gate 自身故障不阻断生成，但必须可见
            _LOGGER.warning(
                "styled-draft style gate failed for scene %s (stage=%s)",
                getattr(scene, "scene_id", None),
                stage,
                exc_info=True,
            )
            return styled_gate_unavailable_result(
                stage=stage, error=type(exc).__name__
            )

    def _record_runner_failure_attempt(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        step: str,
        prompt: dict[str, Any],
        exc: LLMNodeExecutionError | SceneGenerationPostprocessError,
        source_draft_row_id: str | None = None,
    ) -> None:
        details_json: dict[str, Any] = {
            "llm_call_id": exc.llm_call_id,
            "error_code": exc.error_code,
            "message": str(getattr(exc, "message", None) or str(exc)),
            "retryable": bool(getattr(exc, "retryable", False)),
            "business_attempt_consumed": _counts_as_business_attempt(exc),
        }
        if prompt is not None:
            details_json["template_name"] = prompt.get("template_name")
            details_json["template_version"] = prompt.get("template_version")
            if isinstance(prompt.get("_style_reference_runtime_audit"), dict):
                details_json["style_reference_runtime"] = deepcopy(
                    prompt["_style_reference_runtime_audit"]
                )
        if source_draft_row_id is not None:
            details_json["source_draft_row_id"] = source_draft_row_id
        if isinstance(exc, LLMNodeContinuityError):
            details_json["continuity_warning"] = exc.continuity_warning
        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step=step,
                status="failed",
                source_bundle_id=bundle["bundle_id"],
                details_json=details_json,
            )
        )
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        if _counts_as_business_attempt(exc):
            state.total_attempt_count += 1
        self.session.flush()

    @staticmethod
    def _raise_original_runner_error(exc: LLMNodeExecutionError) -> None:
        if isinstance(exc, LLMNodeContinuityError):
            raise DomainError(
                CONTINUITY_BUDGET_ERROR_CODE,
                CONTINUITY_BUDGET_MESSAGE,
                status_code=409,
                details={
                    "continuity_warning": exc.continuity_warning,
                    "recommended_action": SCENE_SPLIT_RECOMMENDATION,
                },
            ) from exc
        if exc.original_error is not None:
            raise exc.original_error
        raise exc

    def _persist_generation_failure(
        self,
        *,
        scene: SceneCard,
        state: SceneRunState,
        bundle: dict[str, Any],
        llm_call_id: str,
        step: str,
        execution_step_key: str | None = None,
        started_at: float,
        task_config: Any | None,
        prompt: dict[str, Any] | None,
        request_summary: dict[str, Any],
        exc: Exception,
        source_draft_row_id: str | None = None,
    ) -> None:
        error_code = getattr(exc, "code", exc.__class__.__name__)
        self.session.add(
            LlmCall(
                llm_call_id=llm_call_id,
                scope_type="scene",
                scope_id=scene.scene_id,
                provider=getattr(task_config, "provider", None),
                provider_id=getattr(task_config, "provider_id", None),
                account_id=getattr(task_config, "account_id", None),
                model=getattr(task_config, "model", None),
                node_id=step,
                reasoning_level=getattr(task_config, "reasoning_level", None),
                native_reasoning_json=None,
                credential_mode=getattr(task_config, "credential_mode", None),
                prompt_hash=(
                    prompt.get("prompt_hash") if isinstance(prompt, dict) else None
                ),
                step=step,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                execution_id=current_llm_execution_id(),
                execution_step_key=execution_step_key,
                estimated_tokens=0,
                reserved_tokens=0,
                budget_charged_tokens=0,
                accounting_status="rejected",
                request_payload_summary=sanitize_audit_summary(request_summary),
                # error_audit_summary 已做过 sanitize，勿再包一层（重复 sanitize 幂等但多余）。
                response_payload_summary=error_audit_summary(exc),
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                latency_ms=int((time.perf_counter() - started_at) * 1000),
                finish_reason=None,
                error_code=error_code,
            )
        )
        self.session.flush()
        details_json: dict[str, Any] = {
            "llm_call_id": llm_call_id,
            "error_code": error_code,
            "message": str(exc),
            "execution_step_key": execution_step_key,
            "business_attempt_consumed": _counts_as_business_attempt(exc),
        }
        if prompt is not None:
            details_json["template_name"] = prompt.get("template_name")
            details_json["template_version"] = prompt.get("template_version")
        if source_draft_row_id is not None:
            details_json["source_draft_row_id"] = source_draft_row_id
        self.session.add(
            AttemptTracker(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                step=step,
                status="failed",
                source_bundle_id=bundle["bundle_id"],
                details_json=details_json,
            )
        )
        state.current_bundle_id = bundle["bundle_id"]
        state.current_bundle_hash = bundle["bundle_snapshot_hash"]
        if _counts_as_business_attempt(exc):
            state.total_attempt_count += 1
        self.session.flush()


def _candidate_dispersion(texts: list[str]) -> float:
    """Measure pairwise surface dissimilarity of candidate texts (0=identical, 1=fully disjoint).

    Blueprint §6.3: dispersion is a necessary condition for surprise — if candidates
    are highly similar, sampling hasn't explored the tail.
    Uses character-level 4-gram Jaccard distance averaged over all pairs.
    """
    if len(texts) < 2:
        return 1.0

    def _char_ngrams(text: str, n: int = 4) -> set[str]:
        return {text[i : i + n] for i in range(max(0, len(text) - n + 1))}

    ngram_sets = [_char_ngrams(t) for t in texts]
    distances: list[float] = []
    for i in range(len(ngram_sets)):
        for j in range(i + 1, len(ngram_sets)):
            a, b = ngram_sets[i], ngram_sets[j]
            union = len(a | b)
            if union == 0:
                distances.append(0.0)
            else:
                distances.append(1.0 - len(a & b) / union)
    return sum(distances) / len(distances) if distances else 0.0


def _extract_scene_text(response: LLMResponse) -> str:
    structured_output = response.structured_output or {}
    scene_text = structured_output.get("scene_text")
    if isinstance(scene_text, str) and scene_text.strip():
        return _normalize_literal_unicode_escapes(scene_text.strip())
    # 中性/风格/补丁/续写各路径共用此提取器，消息不指认具体 stage（审计 P-17）
    raise SceneGenerationPostprocessError(
        llm_call_id=getattr(response, "llm_call_id", None),
        message="llm generation response missing scene_text",
    )


def _apply_style_length_patch(
    *,
    source_content: str,
    response: LLMResponse,
    scene: SceneCard,
    llm_call_id: str,
) -> tuple[str, dict[str, Any]]:
    """验证并套用模型提交的分段编号 replacement；任何歧义都整批拒绝。"""

    def reject(reason: str) -> None:
        raise SceneGenerationPostprocessError(
            llm_call_id=llm_call_id,
            message=f"style length patch invalid: {reason}",
        )

    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is None:
        reject("target_length_range_unavailable")
    assert length_range is not None
    minimum, maximum = length_range
    source_length = _visible_char_count(source_content)
    if minimum <= source_length <= maximum:
        reject("source_length_already_valid")
    local_minimum, local_maximum, target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    direction = "expand" if source_length < minimum else "compress"

    payload = response.structured_output or {}
    raw_edits = payload.get("edits") if isinstance(payload, dict) else None
    if not isinstance(raw_edits, list) or not 1 <= len(raw_edits) <= 6:
        reject("edit_count_invalid")

    segments = _style_length_patch_segments(source_content)
    editable_segment_ids = _style_length_patch_editable_segment_ids(
        source_content,
        scene,
    )
    editable_segments = {
        segment["segment_id"]: segment
        for segment in segments
        if segment["segment_id"] in editable_segment_ids
    }
    if not editable_segments:
        reject("editable_segments_unavailable")
    edits: list[tuple[int, int, str, int, int, str]] = []
    submitted_segment_ids: list[str] = []
    for raw in raw_edits:
        if not isinstance(raw, dict):
            reject("edit_shape_invalid")
        segment_id = raw.get("segment_id")
        new_text = raw.get("new_text")
        if not isinstance(segment_id, str) or not segment_id.strip():
            reject("segment_id_invalid")
        if not isinstance(new_text, str):
            reject("new_text_invalid")
        segment_id = segment_id.strip().upper()
        segment = editable_segments.get(segment_id)
        if segment is None:
            reject("segment_id_not_editable")
        if segment_id in submitted_segment_ids:
            reject("segment_id_repeated")
        new_text = _normalize_literal_unicode_escapes(new_text)
        if "⟦S" in new_text or "SEGMENT" in new_text.upper():
            reject("segment_marker_leaked_into_new_text")
        if direction == "expand":
            start = int(segment["end"])
            end = start
            old_chars = 0
        else:
            start = int(segment["start"])
            end = int(segment["end"])
            old_chars = int(segment["visible_chars"])
        new_chars = _visible_char_count(new_text)
        delta = new_chars - old_chars
        if direction == "expand":
            if delta <= 0:
                reject("expansion_insertion_must_add_text")
        elif delta >= 0:
            reject("compression_segment_replacement_must_be_shorter")
        submitted_segment_ids.append(segment_id)
        edits.append((start, end, new_text, delta, old_chars, segment_id))

    edits.sort(key=lambda item: item[0])
    if any(
        left[1] > right[0]
        or (left[0] == left[1] == right[0] == right[1])
        for left, right in zip(edits, edits[1:])
    ):
        reject("edits_overlap")
    scope_limit = max(600, source_length // 2)
    best_choice: tuple[
        tuple[int, int, int, tuple[str, ...]],
        list[tuple[int, int, str, int, int, str]],
    ] | None = None
    for mask in range(1, 1 << len(edits)):
        selected = [
            edit for index, edit in enumerate(edits) if mask & (1 << index)
        ]
        selected_old_chars = sum(edit[4] for edit in selected)
        selected_delta = sum(edit[3] for edit in selected)
        if max(selected_old_chars, abs(selected_delta)) > scope_limit:
            continue
        candidate_length = source_length + selected_delta
        if not minimum <= candidate_length <= maximum:
            continue
        score = (
            0 if local_minimum <= candidate_length <= local_maximum else 1,
            abs(candidate_length - target),
            len(selected),
            tuple(edit[5] for edit in selected),
        )
        if best_choice is None or score < best_choice[0]:
            best_choice = (score, selected)
    if best_choice is None:
        reject("no_safe_edit_subset_reaches_target_range")
    selected_edits = best_choice[1]
    total_old_chars = sum(edit[4] for edit in selected_edits)
    total_delta = sum(edit[3] for edit in selected_edits)
    applied_segment_ids = [edit[5] for edit in selected_edits]

    patched = source_content
    for start, end, new_text, _delta, _old_chars, _segment_id in reversed(
        selected_edits
    ):
        patched = patched[:start] + new_text + patched[end:]
    output_length = _visible_char_count(patched)
    if not minimum <= output_length <= maximum:
        reject("patched_length_outside_absolute_range")
    if output_length - source_length != total_delta:
        reject("visible_delta_mismatch")

    return patched, {
        "version": "style_length_patch_v3",
        "valid": True,
        "mode": direction,
        "edit_count": len(selected_edits),
        "submitted_edit_count": len(edits),
        "omitted_edit_count": len(edits) - len(selected_edits),
        "segment_ids": applied_segment_ids,
        "source_visible_chars": source_length,
        "patched_visible_chars": output_length,
        "visible_delta": total_delta,
        "absolute_range": [minimum, maximum],
        "correction_window": [local_minimum, local_maximum],
        "correction_window_hit": local_minimum <= output_length <= local_maximum,
        "correction_target": target,
        "edited_source_visible_chars": total_old_chars,
        "deterministic_segment_address_validation": True,
        "non_overlapping": True,
    }


def _apply_style_salvage_patch(
    *,
    source_content: str,
    response: LLMResponse,
    scene: SceneCard,
    llm_call_id: str,
) -> tuple[str, dict[str, Any]]:
    """只替换一个预编号中段，保留中性安全稿的其余文字与结尾。"""

    def reject(reason: str) -> None:
        raise SceneGenerationPostprocessError(
            llm_call_id=llm_call_id,
            message=f"style salvage patch invalid: {reason}",
        )

    payload = response.structured_output or {}
    raw_edits = payload.get("edits") if isinstance(payload, dict) else None
    if not isinstance(raw_edits, list) or len(raw_edits) != 1:
        reject("exactly_one_edit_required")
    raw = raw_edits[0]
    if not isinstance(raw, dict):
        reject("edit_shape_invalid")
    segment_id = raw.get("segment_id")
    new_text = raw.get("new_text")
    if not isinstance(segment_id, str) or not segment_id.strip():
        reject("segment_id_invalid")
    if not isinstance(new_text, str):
        reject("new_text_invalid")
    segment_id = segment_id.strip().upper()
    new_text = _normalize_literal_unicode_escapes(new_text).strip()
    editable_ids = _style_salvage_editable_segment_ids(source_content)
    segment_by_id = {
        str(segment["segment_id"]): segment
        for segment in _style_length_patch_segments(source_content)
    }
    segment = segment_by_id.get(segment_id)
    if segment_id not in editable_ids or segment is None:
        reject("segment_id_not_editable")
    if "⟦S" in new_text or "SEGMENT" in new_text.upper():
        reject("segment_marker_leaked_into_new_text")
    old_text = source_content[int(segment["start"]) : int(segment["end"])]
    old_chars = int(segment["visible_chars"])
    new_chars = _visible_char_count(new_text)
    minimum_chars = max(20, math.floor(old_chars * 0.50))
    maximum_chars = max(minimum_chars, math.ceil(old_chars * 1.35))
    if not minimum_chars <= new_chars <= maximum_chars:
        reject("replacement_length_outside_local_window")
    from novel_system.services.style_reference.validation.plagiarism import (
        normalize_text_for_matching,
    )

    if normalize_text_for_matching(old_text) == normalize_text_for_matching(new_text):
        reject("replacement_not_substantively_changed")
    start = int(segment["start"])
    end = int(segment["end"])
    patched = source_content[:start] + new_text + source_content[end:]
    length_range = _parse_numeric_length_band(scene.target_length_band)
    output_chars = _visible_char_count(patched)
    if length_range is not None and not length_range[0] <= output_chars <= length_range[1]:
        reject("patched_length_outside_absolute_range")
    return patched, {
        "version": "style_salvage_patch_v1",
        "valid": True,
        "segment_id": segment_id,
        "source_visible_chars": _visible_char_count(source_content),
        "old_segment_visible_chars": old_chars,
        "new_segment_visible_chars": new_chars,
        "patched_visible_chars": output_chars,
        "replacement_window": [minimum_chars, maximum_chars],
        "substantive_change": True,
        "protected_ending": True,
    }


def _style_length_patch_segments(source_content: str) -> list[dict[str, Any]]:
    """将正文切成稳定的可定位段；最后一段由调用方固定保护。"""

    line_spans = [
        (match.start(), match.end())
        for match in re.finditer(r"[^\r\n]*\S[^\r\n]*", source_content)
    ]
    spans = line_spans
    if len(line_spans) == 1:
        line_start, line_end = line_spans[0]
        line_text = source_content[line_start:line_end]
        sentence_spans = [
            (line_start + match.start(), line_start + match.end())
            for match in re.finditer(
                r".+?(?:[。！？!?]”?|。?$)",
                line_text,
            )
            if match.group(0).strip()
        ]
        if len(sentence_spans) >= 2:
            spans = sentence_spans
    return [
        {
            "segment_id": f"S{index:03d}",
            "start": start,
            "end": end,
            "visible_chars": _visible_char_count(source_content[start:end]),
        }
        for index, (start, end) in enumerate(spans, start=1)
    ]


def _style_length_patch_editable_segment_ids(
    source_content: str,
    scene: SceneCard,
) -> list[str]:
    segments = _style_length_patch_segments(source_content)
    if len(segments) < 2:
        return []
    candidates = segments[:-1]
    length_range = _parse_numeric_length_band(scene.target_length_band)
    source_length = _visible_char_count(source_content)
    if length_range is None or source_length < length_range[0]:
        return [str(segment["segment_id"]) for segment in candidates]

    minimum, maximum = length_range
    _local_minimum, local_maximum, _target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    desired_reduction = max(1, source_length - local_maximum)
    minimum_useful_chars = max(24, min(80, math.ceil(desired_reduction / 6)))
    useful = [
        segment
        for segment in candidates
        if int(segment["visible_chars"]) >= minimum_useful_chars
    ]
    if not useful:
        useful = [max(candidates, key=lambda segment: int(segment["visible_chars"]))]
    return [str(segment["segment_id"]) for segment in useful]


def _style_salvage_editable_segment_ids(source_content: str) -> list[str]:
    segments = _style_length_patch_segments(source_content)
    if len(segments) < 2:
        return []
    source_length = max(1, _visible_char_count(source_content))
    candidates = [
        segment
        for segment in segments[:-1]
        if int(segment["visible_chars"]) >= 60
        and 0.10
        <= int(segment["visible_chars"]) / source_length
        <= 0.35
    ]
    if not candidates:
        candidates = [
            segment
            for segment in segments[:-1]
            if int(segment["visible_chars"]) >= 40
            and int(segment["visible_chars"]) / source_length <= 0.45
        ]
    candidates = sorted(
        candidates,
        key=lambda segment: (
            abs(int(segment["visible_chars"]) / source_length - 0.22),
            int(str(segment["segment_id"])[1:]),
        ),
    )[:4]
    candidate_ids = {str(segment["segment_id"]) for segment in candidates}
    return [
        str(segment["segment_id"])
        for segment in segments
        if str(segment["segment_id"]) in candidate_ids
    ]


def _annotate_style_length_patch_source(
    source_content: str,
    *,
    editable_segment_ids: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    segments = _style_length_patch_segments(source_content)
    if not segments:
        return source_content, []
    editable_ids = (
        [str(value) for value in editable_segment_ids]
        if editable_segment_ids is not None
        else [str(segment["segment_id"]) for segment in segments[:-1]]
    )
    editable_set = set(editable_ids)
    parts: list[str] = []
    cursor = 0
    for index, segment in enumerate(segments):
        start = int(segment["start"])
        end = int(segment["end"])
        segment_id = str(segment["segment_id"])
        parts.append(source_content[cursor:start])
        marker = (
            f"⟦{segment_id}:PROTECTED_ENDING⟧"
            if index == len(segments) - 1
            else (
                f"⟦{segment_id}⟧"
                if segment_id in editable_set
                else f"⟦{segment_id}:PROTECTED⟧"
            )
        )
        parts.append(marker)
        parts.append(source_content[start:end])
        cursor = end
    parts.append(source_content[cursor:])
    return "".join(parts), editable_ids


def _constrain_style_length_patch_schema(
    prompt: dict[str, Any],
    *,
    editable_segment_ids: Sequence[str],
    scene: SceneCard,
    source_length: int,
) -> None:
    """把本次可编辑 ID 收紧为 JSON Schema enum，并刷新审计 hash。"""

    schema = prompt.get("structured_schema")
    try:
        edits_schema = schema["properties"]["edits"]
        item_schema = edits_schema["items"]
        properties = item_schema["properties"]
        segment_schema = properties["segment_id"]
        new_text_schema = properties["new_text"]
    except (KeyError, TypeError):
        return
    if editable_segment_ids:
        segment_schema["enum"] = list(editable_segment_ids)
        edits_schema["maxItems"] = min(6, len(editable_segment_ids))
    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is not None and source_length < length_range[0]:
        local_minimum, local_maximum, _target = _style_repair_working_window(
            *length_range,
            source_length=source_length,
        )
        minimum_delta = max(1, local_minimum - source_length)
        maximum_delta = max(minimum_delta, local_maximum - source_length)
        item_count = min(
            len(editable_segment_ids),
            6,
            max(1, math.ceil(minimum_delta / 240)),
        )
        if item_count > 0:
            edits_schema["minItems"] = item_count
            edits_schema["maxItems"] = item_count
            new_text_schema["minLength"] = math.ceil(
                minimum_delta / item_count
            )
            new_text_schema["maxLength"] = max(
                new_text_schema["minLength"],
                maximum_delta // item_count,
            )
            new_text_schema["description"] = (
                f"One of exactly {item_count} insertions; all insertions together "
                f"must add {minimum_delta}-{maximum_delta} visible characters."
            )
    prompt["prompt_hash"] = hashlib.sha256(
        canonical_json(
            {
                "template_name": prompt.get("template_name"),
                "template_version": prompt.get("template_version"),
                "system_prompt": prompt.get("system_prompt"),
                "user_prompt": prompt.get("user_prompt"),
                "structured_schema": schema,
            }
        ).encode("utf-8")
    ).hexdigest()


def _constrain_style_salvage_schema(
    prompt: dict[str, Any],
    *,
    editable_segment_ids: Sequence[str],
) -> None:
    schema = prompt.get("structured_schema")
    try:
        edits_schema = schema["properties"]["edits"]
        segment_schema = edits_schema["items"]["properties"]["segment_id"]
    except (KeyError, TypeError):
        return
    if editable_segment_ids:
        segment_schema["enum"] = list(editable_segment_ids)
    edits_schema["minItems"] = 1
    edits_schema["maxItems"] = 1
    prompt["prompt_hash"] = hashlib.sha256(
        canonical_json(
            {
                "template_name": prompt.get("template_name"),
                "template_version": prompt.get("template_version"),
                "system_prompt": prompt.get("system_prompt"),
                "user_prompt": prompt.get("user_prompt"),
                "structured_schema": schema,
            }
        ).encode("utf-8")
    ).hexdigest()


_LITERAL_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_BARE_CJK_UNICODE_ESCAPE_RE = re.compile(
    r"(?<=[\u3400-\u9fff])u([0-9a-fA-F]{4})(?=$|[\s\u3000-\u303f\u3400-\u9fff，。！？；：、])"
)
_C1_CONTROL_RE = re.compile(r"[\u0080-\u009f]")
_ORPHAN_LOWERCASE_CJK_RE = re.compile(
    r"(?m)(?:^|[。！？!?；;：:]\s*)[A-Za-z](?=[\u3400-\u9fff])"
)
_MODEL_RESPONSE_ARTIFACT_RE = re.compile(
    r"(?i)(?:```(?:json)?|<ctrl\d+>|\blet me (?:refine|construct|rewrite|check)|"
    r"\bthe (?:actual json|draft looks|final json)|source draft row id|"
    r"[\"']scene_text[\"']\s*:)"
)
_NUMERIC_LENGTH_BAND_RE = re.compile(
    r"(?P<minimum>\d{2,6})\s*(?:-|–|—|~|～|至|到)\s*(?P<maximum>\d{2,6})"
)


def _normalize_literal_unicode_escapes(text: str) -> str:
    """只还原可无歧义识别的 Unicode 转义残片，不做通用 escape 解码。"""

    def replace_escaped(match: re.Match[str]) -> str:
        codepoint = int(match.group(1), 16)
        # 单个 surrogate 不是合法正文字符；保留给完整性门拒绝，避免制造坏串。
        if 0xD800 <= codepoint <= 0xDFFF:
            return match.group(0)
        return chr(codepoint)

    normalized = _LITERAL_UNICODE_ESCAPE_RE.sub(replace_escaped, text)

    def replace_bare_cjk(match: re.Match[str]) -> str:
        codepoint = int(match.group(1), 16)
        if 0x3000 <= codepoint <= 0x303F or 0x3400 <= codepoint <= 0x9FFF:
            return chr(codepoint)
        return match.group(0)

    return _BARE_CJK_UNICODE_ESCAPE_RE.sub(replace_bare_cjk, normalized)


def _scene_text_integrity_markers(text: str) -> list[str]:
    """返回不含正文内容的完整性标记，供改写验收和审计使用。"""
    markers: list[str] = []
    if "\ufffd" in text:
        markers.append("replacement_character")
    if "???" in text:
        markers.append("question_mark_placeholder")
    if _C1_CONTROL_RE.search(text):
        markers.append("c1_control_character")
    if _LITERAL_UNICODE_ESCAPE_RE.search(text):
        markers.append("literal_unicode_escape")
    if re.search(r"(?<![A-Za-z0-9_])u[0-9a-fA-F]{4}(?![A-Za-z0-9_])", text):
        markers.append("bare_unicode_escape")
    if _ORPHAN_LOWERCASE_CJK_RE.search(text):
        markers.append("orphan_ascii_before_cjk")
    if _MODEL_RESPONSE_ARTIFACT_RE.search(text):
        markers.append("model_response_artifact")
    return markers


def _assess_de_template_rewrite(
    *,
    scene: SceneCard,
    source_content: str,
    authoritative_content: str | None = None,
    rewritten_content: str,
    source_quality_gate: dict[str, Any],
    style_conformance: dict[str, Any] | None = None,
    house_taste_deferred: bool = False,
) -> dict[str, Any]:
    """确定性验收一次去模板改写；只阻止可证明的回退，不猜测作者审美。

    ``house_taste_deferred``(style_first):房风维度已让位,不再用启发式去模板分数判回退。
    """
    source_length = _visible_char_count(source_content)
    rewritten_length = _visible_char_count(rewritten_content)
    length_range = _parse_numeric_length_band(scene.target_length_band)
    source_length_score = _length_fitness(source_length, length_range)
    rewritten_length_score = _length_fitness(rewritten_length, length_range)

    safety_source_content = authoritative_content or source_content
    required_terms = constraint_terms(scene.must_include_text or "")
    source_required = {
        term
        for term in required_terms
        if source_field_satisfied(term, safety_source_content)
    }
    rewritten_required = {
        term for term in required_terms if source_field_satisfied(term, rewritten_content)
    }
    lost_required = sorted(source_required - rewritten_required)
    missing_required = sorted(set(required_terms) - rewritten_required)
    missing_without_regression = sorted(set(missing_required) - set(lost_required))

    forbidden_terms = constraint_terms(scene.forbidden_text or "")
    source_forbidden = {
        term
        for term in forbidden_terms
        if contains_forbidden_term(term, safety_source_content)
    }
    rewritten_forbidden = {
        term for term in forbidden_terms if contains_forbidden_term(term, rewritten_content)
    }
    new_forbidden = sorted(rewritten_forbidden - source_forbidden)

    source_integrity = _scene_text_integrity_markers(safety_source_content)
    rewritten_integrity = _scene_text_integrity_markers(rewritten_content)
    new_integrity = sorted(set(rewritten_integrity) - set(source_integrity))

    rewritten_quality_gate = _anti_template_quality_gate(
        rewritten_content,
        scene_id=scene.scene_id,
        chapter_id=scene.chapter_id,
    )
    source_quality_score = float(source_quality_gate.get("score") or 0.0)
    rewritten_quality_score = float(rewritten_quality_gate.get("score") or 0.0)
    source_risk_count = len(source_quality_gate.get("findings") or [])
    rewritten_risk_count = len(rewritten_quality_gate.get("findings") or [])
    source_target_counts = _quality_gate_dimension_counts(source_quality_gate)
    rewritten_target_counts = _quality_gate_dimension_counts(rewritten_quality_gate)
    source_target_evidence_available = any(
        isinstance(finding, dict)
        and str(finding.get("dimension") or "").strip()
        in ANTI_TEMPLATE_GATE_DIMENSIONS
        for finding in (source_quality_gate.get("findings") or [])
    )
    resolved_target_dimensions = sorted(
        dimension
        for dimension, count in source_target_counts.items()
        if rewritten_target_counts.get(dimension, 0) < count
    )
    unresolved_target_dimensions = sorted(
        dimension
        for dimension, count in source_target_counts.items()
        if rewritten_target_counts.get(dimension, 0) >= count
    )
    new_quality_risk_dimensions = sorted(
        set(rewritten_target_counts) - set(source_target_counts)
    )
    worsened_target_dimensions = sorted(
        dimension
        for dimension, count in source_target_counts.items()
        if rewritten_target_counts.get(dimension, 0) > count
    )

    reasons: list[str] = []
    if rewritten_length < 20:
        reasons.append("rewrite_too_short")
    if lost_required:
        reasons.append("required_facts_regressed")
    if missing_without_regression:
        reasons.append("required_facts_missing")
    if new_forbidden:
        reasons.append("forbidden_content_added")
    if new_integrity or len(rewritten_integrity) > len(source_integrity):
        reasons.append("text_integrity_regressed")
    if length_range is not None:
        if rewritten_length_score < 1.0:
            reasons.append("target_length_not_met")
    elif source_length > 0:
        length_ratio = rewritten_length / source_length
        if length_ratio < 0.6:
            reasons.append("rewrite_collapsed")
        elif length_ratio > 1.8:
            reasons.append("rewrite_bloated")
    # 安全修复的唯一职责是把被拒风格稿恢复到事实/长度/禁词/文本完整性硬约束内。
    # 此时 source_quality_gate 还人为追加了 style_safety finding，且原稿本身不可交付；
    # 再要求启发式去模板分数不下降，会把已经安全、仍保留风格的修复稿错误退回中性稿。
    # 普通 de-template 改写仍维持严格非回退门。
    enforce_quality_non_regression = authoritative_content is None and not house_taste_deferred
    if enforce_quality_non_regression:
        if rewritten_quality_score + 0.005 < source_quality_score:
            reasons.append("anti_template_quality_regressed")
        if rewritten_risk_count > source_risk_count:
            reasons.append("anti_template_risks_increased")
        # A repair must demonstrably remove at least one of the exact dimensions
        # that triggered it.  A flat total-risk count previously accepted edits
        # that merely exchanged one defect for another or left every requested
        # defect untouched.
        # Only demand dimension-by-dimension proof when the source gate carries
        # its actionable findings.  Older checkpoints persisted only a compact
        # ``risk_dimensions`` list; treating that compatibility fallback as
        # full evidence would reject a valid completed rewrite on resume even
        # though the old record cannot support a before/after comparison.
        if source_target_evidence_available:
            if source_target_counts and not resolved_target_dimensions:
                reasons.append("target_quality_defects_not_reduced")
            if worsened_target_dimensions or new_quality_risk_dimensions:
                reasons.append("target_quality_defects_worsened")

    # 普通去模板改写只是对已安全风格稿做局部修补，不能用通用质量收益交换
    # 对冻结风格画像的可观测偏离。仅在两稿均达到候选评分的最低文本量、指标
    # 覆盖率和置信度时启用；安全修复仍以事实/长度/禁词/文本完整性为最高优先级。
    conformance = dict(style_conformance or {})
    enforce_style_non_regression = bool(
        authoritative_content is None and conformance.get("comparable") is True
    )
    if enforce_style_non_regression and conformance.get("regressed") is True:
        reasons.append("style_conformance_regressed")

    return {
        "accepted": not reasons,
        "reasons": reasons,
        "required_fact_count": len(required_terms),
        "source_required_fact_matches": len(source_required),
        "rewritten_required_fact_matches": len(rewritten_required),
        "lost_required_fact_count": len(lost_required),
        "missing_required_fact_count": len(missing_required),
        "new_forbidden_count": len(new_forbidden),
        "source_visible_chars": source_length,
        "rewritten_visible_chars": rewritten_length,
        "target_length_range": list(length_range) if length_range is not None else None,
        "source_length_score": round(source_length_score, 4),
        "rewritten_length_score": round(rewritten_length_score, 4),
        "source_quality_score": round(source_quality_score, 4),
        "rewritten_quality_score": round(rewritten_quality_score, 4),
        "source_risk_count": source_risk_count,
        "rewritten_risk_count": rewritten_risk_count,
        "source_target_risk_counts": source_target_counts,
        "rewritten_target_risk_counts": rewritten_target_counts,
        "resolved_target_dimensions": resolved_target_dimensions,
        "unresolved_target_dimensions": unresolved_target_dimensions,
        "new_quality_risk_dimensions": new_quality_risk_dimensions,
        "worsened_target_dimensions": worsened_target_dimensions,
        "source_target_evidence_available": source_target_evidence_available,
        "quality_non_regression_enforced": enforce_quality_non_regression,
        "style_non_regression_enforced": enforce_style_non_regression,
        "style_conformance": conformance,
        "source_integrity_markers": source_integrity,
        "rewritten_integrity_markers": rewritten_integrity,
        "authoritative_source_used": authoritative_content is not None,
    }


def _quality_gate_dimension_counts(quality_gate: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    findings = quality_gate.get("findings") or []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        dimension = str(finding.get("dimension") or "").strip()
        if dimension not in ANTI_TEMPLATE_GATE_DIMENSIONS:
            continue
        counts[dimension] = counts.get(dimension, 0) + 1
    # Some recovery tests and old checkpoints persisted only risk_dimensions.
    # Use them as a one-count fallback without double-counting full findings.
    for raw_dimension in quality_gate.get("risk_dimensions") or []:
        dimension = str(raw_dimension or "").strip()
        if dimension in ANTI_TEMPLATE_GATE_DIMENSIONS and dimension not in counts:
            counts[dimension] = 1
    return dict(sorted(counts.items()))


def _assess_style_rewrite_conformance(
    *,
    bundle: dict[str, Any] | None,
    source_content: str,
    rewritten_content: str,
) -> dict[str, Any]:
    """用冻结画像审计二次改写是否显著偏离，失败时保守地不启用门禁。

    这里复用候选重排的分组、容差和最低置信度，但不改变候选重排的 shadow
    发布状态：它只保护同一稿件的一次局部修补不发生可证明的风格回退。
    """

    base_audit: dict[str, Any] = {
        "version": "style_rewrite_non_regression_v1",
        "available": False,
        "comparable": False,
        "regressed": False,
        "regression_tolerance": _STYLE_REWRITE_REGRESSION_TOLERANCE,
    }
    try:
        policy = style_policy_for_bundle(bundle)
        base_audit["runtime_contract_mode"] = policy.mode
        if policy.error_code is not None:
            return {
                **base_audit,
                "unavailable_reason": policy.error_code,
            }
        if policy.contract is None:
            return {
                **base_audit,
                "unavailable_reason": "frozen_runtime_contract_unavailable",
            }

        # 局部导入保持 scene_generation 的基础导入路径轻量，并复用已经审计过的
        # 纯文本评分器；不读取当前活动画像，不使用作者身份、主题词或隐藏评测。
        from novel_system.services.style_reference.candidate_rerank import (
            CandidateRerankPolicy,
            assess_candidate_text,
            build_style_target,
        )

        profiles = contract_profile_objects(policy.contract)
        target = build_style_target(profiles)
        if target is None:
            return {
                **base_audit,
                "unavailable_reason": "frozen_metric_target_unavailable",
            }
        policy = CandidateRerankPolicy()
        source = assess_candidate_text(
            "source",
            source_content,
            0.0,
            target,
            policy,
        )
        rewritten = assess_candidate_text(
            "rewritten",
            rewritten_content,
            0.0,
            target,
            policy,
        )
        source_score = source.style_score
        rewritten_score = rewritten.style_score
        comparable = bool(
            source.style_eligible
            and rewritten.style_eligible
            and source_score is not None
            and rewritten_score is not None
        )
        delta = (
            float(rewritten_score) - float(source_score)
            if source_score is not None and rewritten_score is not None
            else None
        )

        def score_audit(assessment: Any) -> dict[str, Any]:
            return {
                "style_score": (
                    None
                    if assessment.style_score is None
                    else round(float(assessment.style_score), 6)
                ),
                "style_confidence": round(float(assessment.style_confidence), 6),
                "metric_count": int(assessment.metric_count),
                "substantive_chars": int(assessment.substantive_chars),
                "group_scores": {
                    key: round(float(value), 6)
                    for key, value in sorted(assessment.group_scores.items())
                },
                "top_deviations": list(assessment.top_deviations),
                "eligible": bool(assessment.style_eligible),
            }

        return {
            **base_audit,
            "available": True,
            "comparable": comparable,
            "target_hash": target.target_hash,
            "source": score_audit(source),
            "rewritten": score_audit(rewritten),
            "score_delta": None if delta is None else round(delta, 6),
            "regressed": bool(
                comparable
                and delta is not None
                and delta < -_STYLE_REWRITE_REGRESSION_TOLERANCE
            ),
            **(
                {"unavailable_reason": "minimum_evidence_not_met"}
                if not comparable
                else {}
            ),
        }
    except Exception:  # noqa: BLE001 — optional evidence gate must fail open
        _LOGGER.warning("style rewrite conformance audit degraded", exc_info=True)
        return {
            **base_audit,
            "unavailable_reason": "style_conformance_internal_error",
        }


def _merge_adjacent_style_paragraphs(
    paragraphs: list[str],
    target_count: int,
) -> str:
    """按累计可见字数选择相邻边界；只删除段间空白，不改正文序列。"""

    if target_count >= len(paragraphs):
        return "\n\n".join(paragraphs)
    target_count = max(1, target_count)
    lengths = [_visible_char_count(paragraph) for paragraph in paragraphs]
    prefix = [0]
    for length in lengths:
        prefix.append(prefix[-1] + length)
    total = prefix[-1]

    boundaries: list[int] = []
    previous = 0
    for group_index in range(1, target_count):
        minimum_boundary = previous + 1
        maximum_boundary = len(paragraphs) - (target_count - group_index)
        ideal_cumulative = total * group_index / target_count
        boundary = min(
            range(minimum_boundary, maximum_boundary + 1),
            key=lambda index: (abs(prefix[index] - ideal_cumulative), index),
        )
        boundaries.append(boundary)
        previous = boundary

    groups: list[str] = []
    start = 0
    for end in [*boundaries, len(paragraphs)]:
        groups.append("".join(paragraphs[start:end]))
        start = end
    return "\n\n".join(groups)


def _normalize_style_paragraph_shape(
    *,
    bundle: dict[str, Any] | None,
    text: str,
) -> tuple[str, dict[str, Any]]:
    """只在冻结画像明确要求时合并过密段落；从不自动拆段或改字。"""

    audit: dict[str, Any] = {
        "version": "style_paragraph_normalization_v1",
        "available": False,
        "applied": False,
        "operation": "merge_adjacent_only",
    }
    try:
        policy = style_policy_for_bundle(bundle)
        audit["runtime_contract_mode"] = policy.mode
        if policy.error_code is not None:
            return text, {**audit, "reason": policy.error_code}
        if policy.contract is None:
            return text, {
                **audit,
                "reason": "frozen_runtime_contract_unavailable",
            }
        if policy.defers_house_taste():
            # 2026-09-14 风格保真修补:有绑定时不再按全书平均段密度机械合并段落——合并规则按累计
            # 字数硬拼、不认对白行,对白密的场会被黏成一段;样例本身已示范作者怎么分段。
            return text, {**audit, "reason": "deferred_to_reference"}

        from novel_system.services.style_reference.candidate_rerank import (
            build_style_target,
        )

        target = build_style_target(contract_profile_objects(policy.contract))
        if target is None:
            return text, {**audit, "reason": "style_target_unavailable"}
        paragraph_target = target.metrics.get("paragraphs_per_1k")
        if paragraph_target is None:
            return text, {
                **audit,
                "reason": "paragraph_target_unavailable",
                "target_hash": target.target_hash,
            }

        visible_chars = _visible_char_count(text)
        paragraphs = [
            part.strip()
            for part in re.split(r"\n\s*\n", text)
            if part.strip()
        ]
        current_count = len(paragraphs)
        audit.update(
            {
                "available": True,
                "target_hash": target.target_hash,
                "visible_chars": visible_chars,
                "before_paragraph_count": current_count,
                "target_rate": round(paragraph_target.mean, 4),
                "target_tolerance": round(paragraph_target.tolerance, 4),
            }
        )
        if visible_chars < 300 or current_count < 2:
            return text, {**audit, "reason": "minimum_evidence_not_met"}

        lower_rate = max(0.0, paragraph_target.mean - paragraph_target.tolerance)
        upper_rate = paragraph_target.mean + paragraph_target.tolerance
        current_rate = current_count * 1000.0 / visible_chars
        minimum_count = max(1, math.ceil(visible_chars * lower_rate / 1000.0))
        maximum_count = max(
            minimum_count,
            math.floor(visible_chars * upper_rate / 1000.0),
        )
        preferred_count = max(
            minimum_count,
            min(
                maximum_count,
                max(1, round(visible_chars * paragraph_target.mean / 1000.0)),
            ),
        )
        audit.update(
            {
                "before_rate": round(current_rate, 4),
                "acceptable_count_range": [minimum_count, maximum_count],
                "preferred_count": preferred_count,
            }
        )
        if current_rate <= upper_rate or preferred_count >= current_count:
            return text, {**audit, "reason": "not_over_segmented"}

        normalized = _merge_adjacent_style_paragraphs(
            paragraphs,
            preferred_count,
        )
        sequence_preserved = re.sub(r"\s+", "", normalized) == re.sub(
            r"\s+", "", text
        )
        if not sequence_preserved:
            return text, {**audit, "reason": "content_sequence_guard_failed"}
        after_count = len(
            [part for part in re.split(r"\n\s*\n", normalized) if part.strip()]
        )
        return normalized, {
            **audit,
            "applied": True,
            "reason": "over_segmented_merged",
            "after_paragraph_count": after_count,
            "after_rate": round(after_count * 1000.0 / visible_chars, 4),
            "content_sequence_preserved": True,
        }
    except Exception:  # noqa: BLE001 — 可选形态整理必须 fail-open
        _LOGGER.warning("style paragraph normalization degraded", exc_info=True)
        return text, {**audit, "reason": "paragraph_normalization_internal_error"}


def _assess_style_anchor_conformance(
    *,
    bundle: dict[str, Any] | None,
    text: str,
) -> dict[str, Any]:
    """用隐藏统计识别明显形态偏差，只向二改暴露定性修复方向。"""

    audit: dict[str, Any] = {
        "version": "style_distribution_repair_v2",
        "available": False,
        "requires_repair": False,
        "violations": [],
        "repair_directions": [],
    }
    try:
        policy = style_policy_for_bundle(bundle)
        audit["runtime_contract_mode"] = policy.mode
        if policy.error_code is not None:
            return {**audit, "unavailable_reason": policy.error_code}
        if policy.contract is None:
            return {
                **audit,
                "unavailable_reason": "frozen_runtime_contract_unavailable",
            }
        if policy.defers_house_taste():
            # 2026-09-14 风格保真修补:段密度 / 分号包络是全书均值,一场的形态偏离均值不是错误;
            # 有绑定时不再据此触发去模板改写(只记录),风格稿的形状由样例决定。
            return {**audit, "unavailable_reason": "deferred_to_reference", "deferred": True}

        from novel_system.services.style_reference.candidate_rerank import (
            build_style_target,
        )
        from novel_system.services.style_reference.metrics import compute_generated_metrics

        target = build_style_target(contract_profile_objects(policy.contract))
        actual = compute_generated_metrics(text)
        visible_chars = _visible_char_count(text)
        if target is None or visible_chars < 300 or not actual:
            return {
                **audit,
                "unavailable_reason": "minimum_evidence_not_met",
                "visible_chars": visible_chars,
            }

        violations: list[dict[str, Any]] = []
        directions: list[str] = []

        paragraph_target = target.metrics.get("paragraphs_per_1k")
        if paragraph_target is not None and "paragraphs_per_1k" in actual:
            current_rate = float(actual["paragraphs_per_1k"])
            lower_rate = max(0.0, paragraph_target.mean - paragraph_target.tolerance)
            upper_rate = paragraph_target.mean + paragraph_target.tolerance
            if current_rate < lower_rate or current_rate > upper_rate:
                directions.append(
                    (
                        "Paragraph structure is substantially more fragmented than the reference tendency. "
                        "Merge adjacent fragments that perform the same narrative function; never merge across "
                        "a POV, action, time, or information-release boundary."
                    )
                    if current_rate > upper_rate
                    else (
                        "Paragraph structure is substantially denser than the reference tendency. "
                        "Split only where POV, action, time, or information function genuinely changes; "
                        "do not chase a paragraph count."
                    )
                )
                violations.append(
                    {
                        "metric": "paragraphs_per_1k",
                        "actual": round(current_rate, 4),
                        "target": round(paragraph_target.mean, 4),
                        "tolerance": round(paragraph_target.tolerance, 4),
                    }
                )

        semicolon_target = target.metrics.get("semicolon_density_per_1k")
        if semicolon_target is not None and "semicolon_density_per_1k" in actual:
            current_rate = float(actual["semicolon_density_per_1k"])
            upper_rate = semicolon_target.mean + semicolon_target.tolerance
            # “分号不足”不是文学缺陷。主动补足标点最容易导致统计投机和机械腔；
            # 仅在明显过量时要求删除无语义依据的分号。
            if current_rate > upper_rate:
                directions.append(
                    "Semicolon rhythm is substantially denser than the reference tendency. "
                    "Keep semicolons only between genuinely parallel or progressive clauses; "
                    "do not replace them with another repeated punctuation pattern."
                )
                violations.append(
                    {
                        "metric": "semicolon_density_per_1k",
                        "actual": round(current_rate, 4),
                        "target": round(semicolon_target.mean, 4),
                        "tolerance": round(semicolon_target.tolerance, 4),
                    }
                )

        return {
            **audit,
            "available": True,
            "requires_repair": bool(violations),
            "target_hash": target.target_hash,
            "visible_chars": visible_chars,
            "violations": violations,
            "repair_directions": directions,
        }
    except Exception:  # noqa: BLE001 — optional prompt guidance must fail open
        _LOGGER.warning("style anchor conformance audit degraded", exc_info=True)
        return {
            **audit,
            "unavailable_reason": "style_anchor_internal_error",
        }


def _assess_style_base_rewrite(
    *,
    scene: SceneCard,
    source_content: str,
    rewritten_content: str,
) -> dict[str, Any]:
    """第一遍风格改写的硬安全门；审美质量留给后续质量门。"""
    required_terms = constraint_terms(scene.must_include_text or "")
    source_required = {
        term for term in required_terms if source_field_satisfied(term, source_content)
    }
    rewritten_required = {
        term for term in required_terms if source_field_satisfied(term, rewritten_content)
    }
    lost_required = sorted(source_required - rewritten_required)
    missing_required = sorted(set(required_terms) - rewritten_required)
    missing_without_regression = sorted(set(missing_required) - set(lost_required))

    forbidden_terms = constraint_terms(scene.forbidden_text or "")
    source_forbidden = {
        term for term in forbidden_terms if contains_forbidden_term(term, source_content)
    }
    rewritten_forbidden = {
        term for term in forbidden_terms if contains_forbidden_term(term, rewritten_content)
    }
    new_forbidden = sorted(rewritten_forbidden - source_forbidden)

    source_integrity = _scene_text_integrity_markers(source_content)
    rewritten_integrity = _scene_text_integrity_markers(rewritten_content)
    new_integrity = sorted(set(rewritten_integrity) - set(source_integrity))
    rewritten_length = _visible_char_count(rewritten_content)
    length_range = _parse_numeric_length_band(scene.target_length_band)
    length_score = _length_fitness(rewritten_length, length_range)

    reasons: list[str] = []
    if rewritten_length < 20:
        reasons.append("rewrite_too_short")
    if lost_required:
        reasons.append("required_facts_regressed")
    if missing_without_regression:
        reasons.append("required_facts_missing")
    if new_forbidden:
        reasons.append("forbidden_content_added")
    if new_integrity or len(rewritten_integrity) > len(source_integrity):
        reasons.append("text_integrity_regressed")
    if length_range is not None and length_score < 1.0:
        reasons.append("target_length_not_met")

    return {
        "accepted": not reasons,
        "reasons": reasons,
        "required_fact_count": len(required_terms),
        "source_required_fact_matches": len(source_required),
        "rewritten_required_fact_matches": len(rewritten_required),
        "lost_required_fact_count": len(lost_required),
        "missing_required_fact_count": len(missing_required),
        "new_forbidden_count": len(new_forbidden),
        "rewritten_visible_chars": rewritten_length,
        "target_length_range": list(length_range) if length_range is not None else None,
        "rewritten_length_score": round(length_score, 4),
        "source_integrity_markers": source_integrity,
        "rewritten_integrity_markers": rewritten_integrity,
    }


def _resume_base_safety(
    session: Session,
    *,
    scene_id: str,
    row_id: str,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    for attempt in session.query(AttemptTracker).filter_by(
        scene_id=scene_id,
        step="style_draft",
        status="completed",
    ):
        details = attempt.details_json or {}
        if details.get("row_id") == row_id and isinstance(
            details.get("base_safety"), dict
        ):
            return dict(details["base_safety"])
    return fallback


def _resume_style_repair_source(
    session: Session,
    *,
    scene_id: str,
    row_id: str,
    fallback_row_id: str,
    fallback_content: str,
) -> tuple[str, str]:
    """恢复 base checkpoint 时找回被拒绝的 provider 风格稿供一次定向修复。"""
    for attempt in session.query(AttemptTracker).filter_by(
        scene_id=scene_id,
        step="style_draft",
        status="completed",
    ):
        details = attempt.details_json or {}
        if details.get("row_id") != row_id:
            continue
        rejected_row_id = details.get("rejected_candidate_row_id")
        if not isinstance(rejected_row_id, str) or not rejected_row_id:
            break
        rejected = session.get(SceneDraft, rejected_row_id)
        if (
            rejected is not None
            and rejected.stage == "style_rejected"
            and rejected.status == "rejected"
            and rejected.content
        ):
            return rejected.row_id, rejected.content
        break
    return fallback_row_id, fallback_content


def _visible_char_count(text: str) -> int:
    return sum(not char.isspace() for char in text)


# 2026-09-12 风格直起:style_first 下场景卡数字长度带两侧各放宽 style_first_length_slack
# (作者自己的场景尺度优先于系统的长度带,越界才触发长度补丁)。放宽比例由本模块的公共入口
# (generate_neutral_draft / _run_style_generation)按 bundle 是否 style_bound 设进这个
# 上下文变量,所有解析长度带的判定 / 指令 / 补丁窗口自动跟随;默认 0 = 现状。
_LENGTH_BAND_SLACK: ContextVar[float] = ContextVar("scene_length_band_slack", default=0.0)
_STYLE_FIRST_LENGTH_SLACK_DEFAULT = 0.5
# 2026-09-22 结构跟随参考书:bundle 冻结的「参考作者一场多长」(bundle_builder 按参考章长 ÷ 本章场数
# 推算,inline_digests["_style_reference_scene_scale"])。style_first 下硬范围上限抬到这个尺度
# (× (1 + slack),封顶 ceiling),长度指引把它说成写作目标;neutral_first / 概述场不设。
_REFERENCE_SCENE_SCALE: ContextVar[dict[str, Any] | None] = ContextVar("scene_reference_scale", default=None)
_REFERENCE_SCENE_SCALE_KEY = "_style_reference_scene_scale"


def _bundle_inline_digests(bundle: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """bundle 既可能是 BundleBuilder 返回的外壳(``{"snapshot": {...}}``)也可能是快照本身。"""
    if not isinstance(bundle, Mapping):
        return {}
    digests = bundle.get("inline_digests")
    if isinstance(digests, Mapping):
        return digests
    snapshot = bundle.get("snapshot")
    if isinstance(snapshot, Mapping) and isinstance(snapshot.get("inline_digests"), Mapping):
        return snapshot["inline_digests"]
    return {}


def _reference_scene_scale_from_bundle(bundle: Mapping[str, Any] | None) -> dict[str, Any] | None:
    raw = _bundle_inline_digests(bundle).get(_REFERENCE_SCENE_SCALE_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        derived = int(payload.get("derived_scene_chars") or 0)
    except (TypeError, ValueError):
        return None
    if derived <= 0:
        return None
    return payload


def _reference_scale_sentence(scale: Mapping[str, Any] | None) -> str:
    """长度指引里说明参考尺度的一句(没有尺度 → 空串)。"""
    if not scale:
        return ""
    derived = int(scale.get("derived_scene_chars") or 0)
    if derived <= 0:
        return ""
    if str(scale.get("basis") or "") == "explicit_scene_breaks":
        return (
            f" Measured on the reference book: this author's scenes run about {derived:,} visible characters — "
            "write at that scale; the card's band is the plan's floor, not a ceiling."
        )
    chapter = scale.get("chapter_chars") if isinstance(scale.get("chapter_chars"), Mapping) else {}
    median = int(chapter.get("median") or 0)
    p10 = int(chapter.get("p10") or 0)
    p90 = int(chapter.get("p90") or 0)
    scenes = int(scale.get("scenes_in_chapter") or 1)
    spread = f" ({p10:,}–{p90:,} is normal)" if p10 and p90 else ""
    return (
        f" Measured on the reference book: a chapter runs about {median:,} characters{spread} and this chapter has "
        f"{scenes} scene{'s' if scenes != 1 else ''}, so a scene of this author's is about {derived:,} visible characters — "
        "write at that scale; the card's band is the plan's floor, not a ceiling."
    )


def _scene_rendering_mode(scene: Any) -> str:
    """场景卡上结构化的呈现方式(``writer_brief_json.rendering_mode``:full / summary / skip),缺省 full。"""
    brief = getattr(scene, "writer_brief_json", None) if scene is not None else None
    value = str(brief.get("rendering_mode") or "") if isinstance(brief, Mapping) else ""
    return value.strip().lower() or "full"


def _style_first_length_slack(bundle: Mapping[str, Any] | None, scene: Any = None) -> float:
    if not style_policy_for_bundle(bundle).defers_house_taste():
        return 0.0
    # 阶段 L：「概述两段」的场是作者的呈现决定（200–500 字），风格直起也不把它放宽成整场。
    # 风格参考 v3：读场景卡上结构化的 rendering_mode，不再在结构简报的渲染文本里找「Rendering mode: summary」。
    if _scene_rendering_mode(scene) == "summary":
        return 0.0
    try:
        budget = load_yaml_config("injection_budget")
    except FileNotFoundError:
        budget = {}
    try:
        slack = float(budget.get("style_first_length_slack", _STYLE_FIRST_LENGTH_SLACK_DEFAULT))
    except (TypeError, ValueError):
        slack = _STYLE_FIRST_LENGTH_SLACK_DEFAULT
    return max(0.0, min(slack, 0.9))


@contextlib.contextmanager
def _length_band_slack_for(bundle: Mapping[str, Any] | None, scene: Any = None):
    slack = _style_first_length_slack(bundle, scene)
    token = _LENGTH_BAND_SLACK.set(slack)
    # 参考尺度只在放宽生效(style_first 且非概述场)时随行;否则 None = 现状。
    scale_token = _REFERENCE_SCENE_SCALE.set(_reference_scene_scale_from_bundle(bundle) if slack > 0 else None)
    try:
        yield
    finally:
        _REFERENCE_SCENE_SCALE.reset(scale_token)
        _LENGTH_BAND_SLACK.reset(token)


def _parse_numeric_length_band(
    value: str | None, *, slack: float | None = None
) -> tuple[int, int] | None:
    match = _NUMERIC_LENGTH_BAND_RE.search(value or "")
    if match is None:
        return None
    minimum = int(match.group("minimum"))
    maximum = int(match.group("maximum"))
    if minimum <= 0 or maximum < minimum:
        return None
    effective_slack = _LENGTH_BAND_SLACK.get() if slack is None else slack
    if effective_slack > 0:
        minimum = max(1, int(round(minimum * (1.0 - effective_slack))))
        maximum = max(minimum, int(round(maximum * (1.0 + effective_slack))))
        # 2026-09-22 结构跟随参考书:硬范围上限至少抬到参考作者的场尺度 × (1 + slack)(封顶 ceiling),
        # 作者(或第 10 步的模型)定的带不再把一场压在参考尺度之下;下限不动。显式 slack 的调用
        # (计划值)不看参考尺度。
        scale = _REFERENCE_SCENE_SCALE.get() if slack is None else None
        if scale:
            derived = int(scale.get("derived_scene_chars") or 0)
            ceiling = int(scale.get("ceiling") or 0) or derived
            if derived > 0:
                maximum = max(maximum, min(int(round(derived * (1.0 + effective_slack))), max(ceiling, derived)))
    return minimum, maximum


def _length_fitness(length: int, target: tuple[int, int] | None) -> float:
    if target is None:
        return 1.0
    minimum, maximum = target
    if minimum <= length <= maximum:
        return 1.0
    if length < minimum:
        return length / minimum
    return maximum / length


def _safe_length_window(minimum: int, maximum: int) -> tuple[int, int, int]:
    width = maximum - minimum
    margin = min(50, max(10, width // 10)) if width >= 40 else 0
    safe_minimum = minimum + margin
    safe_maximum = maximum - margin
    if safe_minimum > safe_maximum:
        safe_minimum, safe_maximum = minimum, maximum
    target = round((safe_minimum + safe_maximum) / 2)
    return safe_minimum, safe_maximum, target


def _style_repair_working_window(
    minimum: int,
    maximum: int,
    *,
    source_length: int,
) -> tuple[int, int, int]:
    """长度不合格时贴近最近安全边界修，不把局部校正变成整篇伸缩。"""

    safe_minimum, safe_maximum, safe_target = _safe_length_window(
        minimum,
        maximum,
    )
    if minimum <= source_length <= maximum:
        local_minimum = max(minimum, source_length * 9 // 10)
        local_maximum = min(maximum, (source_length * 11 + 9) // 10)
        if local_minimum <= local_maximum:
            return local_minimum, local_maximum, source_length
        return minimum, maximum, min(max(source_length, minimum), maximum)

    safe_width = max(0, safe_maximum - safe_minimum)
    correction_span = min(120, max(80, safe_width // 8))
    if source_length < minimum:
        local_minimum = safe_minimum
        local_maximum = min(safe_maximum, safe_minimum + correction_span)
    else:
        local_maximum = safe_maximum
        local_minimum = max(safe_minimum, safe_maximum - correction_span)
    target = round((local_minimum + local_maximum) / 2)
    if local_minimum > local_maximum:
        return safe_minimum, safe_maximum, safe_target
    return local_minimum, local_maximum, target


def _requires_style_salvage(base_safety: dict[str, Any]) -> bool:
    """只有事实安全但极端过短的风格稿才改为局部风格挽救。"""

    if set(base_safety.get("reasons") or []) != {"target_length_not_met"}:
        return False
    length_range = base_safety.get("target_length_range")
    rewritten_length = base_safety.get("rewritten_visible_chars")
    if (
        not isinstance(length_range, list)
        or len(length_range) != 2
        or not isinstance(length_range[0], int)
        or not isinstance(rewritten_length, int)
    ):
        return False
    return rewritten_length < math.ceil(length_range[0] * 0.6)


def _neutral_repair_brief(
    scene: SceneCard,
    *,
    source_content: str,
    assessment: dict[str, Any],
) -> str:
    """把中性稿的确定性失败逐项翻译成一次有界修复，不再误称为长度重试。"""

    required_terms = constraint_terms(scene.must_include_text or "")
    missing_terms = [
        term
        for term in required_terms
        if not source_field_satisfied(term, source_content)
    ]
    forbidden_terms = constraint_terms(scene.forbidden_text or "")
    forbidden_hits = [
        term for term in forbidden_terms if contains_forbidden_term(term, source_content)
    ]
    integrity_markers = _scene_text_integrity_markers(source_content)
    lines = [
        "This is the only deterministic repair attempt. Edit the labeled draft directly and return one complete replacement scene only.",
        "Preserve every already-correct fact, causal step, character identity, chronology, and ending function.",
    ]
    if missing_terms:
        lines.append(
            "Restore each missing required constraint explicitly. A vertical bar means alternatives; include at least one literal alternative from every listed group: "
            + "；".join(missing_terms)
            + "。"
        )
    if forbidden_hits:
        lines.append(
            "Remove every currently present forbidden constraint without replacing it with a spelling variant: "
            + "；".join(forbidden_hits)
            + "。"
        )
    if integrity_markers:
        lines.append(
            "Remove response-format commentary, markdown/JSON wrappers, control tokens, malformed Unicode, and encoding artifacts; output Chinese scene prose only."
        )
    if "target_length_not_met" not in set(assessment.get("reasons") or []):
        lines.append(
            "The current length is already acceptable; do not broadly expand or compress it while fixing the listed issue."
        )
    return "\n".join(f"- {line}" for line in lines)


def _neutral_length_instruction(
    scene: SceneCard,
    *,
    previous_length: int | None = None,
    retry: bool = False,
) -> str:
    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is None:
        return ""
    minimum, maximum = length_range
    safe_minimum, safe_maximum, target = _safe_length_window(minimum, maximum)
    prior = (
        f" The previous attempt was about {previous_length} visible characters and was rejected."
        if previous_length is not None
        else ""
    )
    retry_rule = (
        " Edit the labeled rejected draft directly and return one complete replacement scene, not commentary, a continuation, or a synopsis. Preserve every required fact, causal step, and ending function."
        if retry
        else ""
    )
    delta_rule = ""
    if retry and previous_length is not None:
        if previous_length < safe_minimum:
            delta_rule = (
                f" Add at least {safe_minimum - previous_length} visible characters inside existing action-reaction, blocking, perception, or consequence; do not add a new event."
            )
        elif previous_length > safe_maximum:
            delta_rule = (
                f" Remove at least {previous_length - safe_maximum} visible characters by compressing repetition and decorative description only; do not remove a required fact."
            )
        else:
            local_minimum = max(safe_minimum, previous_length * 9 // 10)
            local_maximum = min(
                safe_maximum,
                (previous_length * 11 + 9) // 10,
            )
            delta_rule = (
                f" The previous length already passed. Keep the repaired scene within {local_minimum}-{local_maximum} visible characters, make the smallest localized edits needed, and do not restage or broadly rewrite unchanged paragraphs."
            )
    return (
        "\n\n[Deterministic Scene Length Guard]\n"
        f"Absolute final range: {minimum}-{maximum} visible non-whitespace Chinese prose characters."
        f" Aim near {target}; use {safe_minimum}-{safe_maximum} as the working window so minor counting differences cannot cross the hard boundary."
        f"{prior}{retry_rule}{delta_rule} Before returning, count once and compress or expand existing action-reaction beats; preserve every required fact and do not add a new event."
    )


def _style_first_length_instruction(
    scene: SceneCard,
    *,
    previous_length: int | None = None,
    retry: bool = False,
) -> str:
    """style_first 首稿的长度指引:场景卡的带是计划值,作者自己的尺度在放宽后的硬范围内优先。"""
    planned = _parse_numeric_length_band(scene.target_length_band, slack=0.0)
    length_range = _parse_numeric_length_band(scene.target_length_band)
    if planned is None or length_range is None:
        return ""
    minimum, maximum = length_range
    prior = (
        f" The previous attempt was about {previous_length} visible characters and was rejected."
        if previous_length is not None
        else ""
    )
    retry_rule = (
        " Edit the labeled rejected draft directly and return one complete replacement scene, not commentary, a continuation, or a synopsis. Preserve every required fact, causal step, and ending function, and keep the reference author's manner."
        if retry
        else ""
    )
    delta_rule = ""
    if retry and previous_length is not None:
        if previous_length < minimum:
            delta_rule = (
                f" Add at least {minimum - previous_length} visible characters with this author's own means; do not add a new event."
            )
        elif previous_length > maximum:
            delta_rule = (
                f" Remove at least {previous_length - maximum} visible characters; do not remove a required fact."
            )
    scale_note = _reference_scale_sentence(_REFERENCE_SCENE_SCALE.get())
    return (
        "\n\n[Scene Length Guide]\n"
        f"The scene card planned {planned[0]}-{planned[1]} visible non-whitespace Chinese prose characters. "
        f"The reference author's own scale for a scene like this takes precedence inside the hard range {minimum}-{maximum}: "
        "the scene may run shorter or longer the way that author's scenes do, but must stay inside the hard range."
        f"{scale_note} "
        "Fill or compress with this author's own means — summary, digression, dialogue, description, reflection — "
        f"not only action-reaction beats; never drop a required fact and never add a new event.{prior}{retry_rule}{delta_rule}"
    )


def _style_length_instruction(
    scene: SceneCard,
    *,
    source_length: int,
    style_first: bool = False,
) -> str:
    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is None:
        return ""
    minimum, maximum = length_range
    safe_minimum, safe_maximum, target = _safe_length_window(minimum, maximum)
    if style_first:
        planned = _parse_numeric_length_band(scene.target_length_band, slack=0.0) or length_range
        scale_note = _reference_scale_sentence(_REFERENCE_SCENE_SCALE.get())
        return (
            "\n\n[Style Revision Length Guide]\n"
            f"The first draft is about {source_length} visible characters; the scene card planned {planned[0]}-{planned[1]}. "
            f"The complete revision must stay inside the hard range {minimum}-{maximum}; within it, the reference author's own scale wins."
            f"{scale_note} "
            "Count once before returning. Fill or compress with this author's own means — summary, digression, dialogue, description, reflection — "
            "never by dropping a required beat."
        )
    return (
        "\n\n[Deterministic Style Rewrite Length Guard]\n"
        f"The approved source is about {source_length} visible characters. The complete final rewrite must be "
        f"{minimum}-{maximum}; aim near {target} and keep {safe_minimum}-{safe_maximum} as the working window. "
        "Count once before returning. Style compression is not permission to drop a required beat or fall below "
        "the lower bound; expand or compress only existing action-reaction, blocking, perception, and consequence."
    )


def _style_repair_length_instruction(
    scene: SceneCard,
    *,
    source_length: int,
) -> str:
    """二改使用局部长度窗，防止修一个问题却把合格稿整体扩写或压缩。"""

    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is None:
        if source_length <= 0:
            return ""
        local_minimum = max(20, source_length * 9 // 10)
        local_maximum = max(
            local_minimum,
            (source_length * 11 + 9) // 10,
        )
        return (
            "\n\n[Deterministic Style Repair Length Guard]\n"
            f"Keep the complete repaired scene within {local_minimum}-{local_maximum} visible non-whitespace "
            f"characters (the source is about {source_length}). Make the smallest localized edits needed; "
            "do not restage, summarize, or broadly rewrite unchanged paragraphs."
        )

    minimum, maximum = length_range
    local_minimum, local_maximum, target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    if minimum <= source_length <= maximum:
        local_rule = (
            f"The source already passes at about {source_length}; keep the repaired scene within "
            f"the local {local_minimum}-{local_maximum} window and make the smallest localized edits needed."
        )
    else:
        if source_length < minimum:
            delta_rule = (
                f"add {local_minimum - source_length}-{local_maximum - source_length} visible characters"
            )
        else:
            delta_rule = (
                f"remove {source_length - local_maximum}-{source_length - local_minimum} visible characters"
            )
        local_rule = (
            f"The source is about {source_length} and is outside the hard range; {delta_rule}, finish inside "
            f"the narrow {local_minimum}-{local_maximum} correction window, and aim near {target}. Change only "
            "existing action-reaction, blocking, perception, consequence, or removable repetition."
        )
    return (
        "\n\n[Deterministic Style Repair Length Guard]\n"
        f"Absolute final range: {minimum}-{maximum} visible non-whitespace Chinese prose characters. "
        f"{local_rule} Preserve every required fact, causal step, and ending function; count once before returning."
    )


def _style_length_patch_instruction(
    scene: SceneCard,
    *,
    source_length: int,
    editable_segment_ids: Sequence[str],
    style_first: bool = False,
) -> str:
    length_range = _parse_numeric_length_band(scene.target_length_band)
    if length_range is None:
        return ""
    minimum, maximum = length_range
    local_minimum, local_maximum, target = _style_repair_working_window(
        minimum,
        maximum,
        source_length=source_length,
    )
    if source_length < minimum:
        direction = (
            f"Expansion only: the combined replacements must add "
            f"{local_minimum - source_length}-{local_maximum - source_length} visible characters. "
            "For each selected segment_id, new_text is inserted immediately after that immutable source segment."
        )
    else:
        direction = (
            f"Compression only: the combined replacements must remove "
            f"{source_length - local_maximum}-{source_length - local_minimum} visible characters. "
            "For each selected segment_id, new_text replaces that one source segment and must be shorter. "
            "Delete only repetition or decorative description; do not replace omitted text with an ellipsis."
        )
    required_terms = constraint_terms(scene.must_include_text or "")
    required_rule = (
        " Do not alter or remove any required constraint group: "
        + "；".join(required_terms)
        + "。"
        if required_terms
        else ""
    )
    return (
        "\n\n[Deterministic Local Length Patch Contract]\n"
        f"The immutable source has about {source_length} visible characters. The final text after applying all edits "
        f"must be {local_minimum}-{local_maximum}, aiming near {target}; the absolute scene range is "
        f"{minimum}-{maximum}. {direction} Editable segment IDs: "
        f"{', '.join(editable_segment_ids) if editable_segment_ids else '(none)'}. "
        "The final source segment marked PROTECTED_ENDING is forbidden. Segment markers are addresses and must "
        "never appear in new_text."
        f"{required_rule}"
        + (
            " Every new_text is written in the reference author's hand: expand or compress with that author's "
            "own means as the [风格样例] show (summary, digression, dialogue, description, reflection), never "
            "with generic action-reaction filler, and never reuse the samples' sentences, names, or events."
            if style_first
            else ""
        )
        + " Return edits only, never scene_text or the complete scene."
    )


def _style_salvage_instruction(
    scene: SceneCard,
    *,
    source_content: str,
    editable_segment_ids: Sequence[str],
) -> str:
    segments = {
        str(segment["segment_id"]): int(segment["visible_chars"])
        for segment in _style_length_patch_segments(source_content)
    }
    windows = []
    for segment_id in editable_segment_ids:
        visible_chars = segments.get(segment_id, 0)
        windows.append(
            f"{segment_id}={max(20, math.floor(visible_chars * 0.50))}-"
            f"{max(20, math.ceil(visible_chars * 1.35))} visible characters"
        )
    required_terms = constraint_terms(scene.must_include_text or "")
    required_rule = (
        " Preserve every required constraint group wherever it appears: "
        + "；".join(required_terms)
        + "。"
        if required_terms
        else ""
    )
    return (
        "\n\n[Deterministic Bounded Style Salvage Contract]\n"
        "Replace exactly one editable segment; all other source characters and the protected ending remain "
        "immutable. Allowed segment windows: "
        + ("; ".join(windows) if windows else "(none)")
        + ". Make a substantive lexical/syntactic rewrite using the injected reusable style mechanisms, not a "
        "punctuation-only or whitespace-only change."
        + required_rule
        + " Return edits only, never scene_text or the complete scene."
    )


def _assess_neutral_draft(scene: SceneCard, content: str) -> dict[str, Any]:
    required_terms = constraint_terms(scene.must_include_text or "")
    missing_required = [
        term for term in required_terms if not source_field_satisfied(term, content)
    ]
    forbidden_terms = constraint_terms(scene.forbidden_text or "")
    forbidden_hits = [
        term for term in forbidden_terms if contains_forbidden_term(term, content)
    ]
    integrity = _scene_text_integrity_markers(content)
    visible_chars = _visible_char_count(content)
    length_range = _parse_numeric_length_band(scene.target_length_band)
    length_score = _length_fitness(visible_chars, length_range)
    reasons: list[str] = []
    if visible_chars < 20:
        reasons.append("draft_too_short")
    if missing_required:
        reasons.append("required_facts_missing")
    if forbidden_hits:
        reasons.append("forbidden_content_present")
    if integrity:
        reasons.append("text_integrity_invalid")
    if length_range is not None and length_score < 1.0:
        reasons.append("target_length_not_met")
    return {
        "accepted": not reasons,
        "reasons": reasons,
        "required_fact_count": len(required_terms),
        "missing_required_fact_count": len(missing_required),
        "forbidden_hit_count": len(forbidden_hits),
        "visible_chars": visible_chars,
        "target_length_range": list(length_range) if length_range else None,
        "length_score": round(length_score, 4),
        "integrity_markers": integrity,
    }


def _anti_template_quality_gate(
    text: str, *, scene_id: str, chapter_id: str
) -> dict[str, Any]:
    signals, findings = analyze_literary_quality(text)
    gate_weight_total = sum(
        DIMENSION_WEIGHTS[dimension]
        for dimension in ANTI_TEMPLATE_GATE_DIMENSIONS
    )
    score = round(
        sum(
            signals[dimension]["score"] * DIMENSION_WEIGHTS[dimension]
            for dimension in ANTI_TEMPLATE_GATE_DIMENSIONS
        )
        / gate_weight_total,
        4,
    )
    risky_findings = [
        {
            **finding,
            "quality_signal_id": f"quality:scene:{scene_id}:{finding.get('dimension')}",
            "scene_id": scene_id,
            "chapter_id": chapter_id,
        }
        for finding in findings
        if finding.get("dimension") in ANTI_TEMPLATE_GATE_DIMENSIONS
    ]
    triggered = bool(risky_findings)
    return {
        "triggered": triggered,
        "rewrite_pass": 1 if triggered else 0,
        "score": score,
        "risk_dimensions": [finding["dimension"] for finding in risky_findings],
        "quality_signal_ids": [
            finding["quality_signal_id"] for finding in risky_findings
        ],
        "findings": risky_findings,
    }


def _defer_house_taste_gate(quality_gate: dict[str, Any]) -> dict[str, Any]:
    """style_first:去模板门的房风维度(ANTI_TEMPLATE_GATE_DIMENSIONS)整体降级为仅记录。

    参考是唯一的风格权威:概述式结尾、句法单调、复沓、解释动机、装饰意象……在参考作者
    自己也这样写时都是手法而不是缺陷。命中保留在 ``advisory_findings`` 供审计,不再触发
    改写,也不再参与「改后不降分即拒绝」的比较。
    """
    deferred = dict(quality_gate)
    deferred["advisory_findings"] = list(quality_gate.get("findings") or [])
    deferred["advisory_risk_dimensions"] = list(quality_gate.get("risk_dimensions") or [])
    deferred["house_taste_gate"] = "deferred_to_reference"
    deferred["findings"] = []
    deferred["risk_dimensions"] = []
    deferred["quality_signal_ids"] = []
    deferred["triggered"] = False
    deferred["rewrite_pass"] = 0
    return deferred


def _de_template_rewrite_brief(quality_gate: dict[str, Any]) -> list[str]:
    brief = [
        "Run no more than this one de-template pass; do not add another rewrite loop.",
        "Keep the same plot facts, speaker identities, core choice, cost, and final hook.",
        "Preserve the reference-derived broad rhythm and paragraph tendencies, but never keep or add an awkward sentence merely to match punctuation or length statistics.",
    ]
    for finding in quality_gate.get("findings", [])[:5]:
        signal_id = finding.get("quality_signal_id", "quality:unknown")
        issue = finding.get("issue") or "anti-template risk"
        evidence = finding.get("evidence_excerpt") or ""
        recommendation = finding.get("recommendation") or ""
        brief.append(f"{signal_id}: {issue}")
        if evidence:
            brief.append(f"Evidence: {evidence}")
        if recommendation:
            brief.append(f"Fix: {recommendation}")
    return brief


def _style_safety_repair_brief(
    *,
    scene: SceneCard,
    source_content: str,
    authoritative_content: str,
    style_first: bool = False,
) -> list[str]:
    """把确定性失败翻译成一次可执行、无正文泄漏的修复清单。"""
    del authoritative_content  # 仅表明调用方已提供可信事实基线；正文不进入提示。
    required_terms = constraint_terms(scene.must_include_text or "")
    missing_terms = [
        term
        for term in required_terms
        if not source_field_satisfied(term, source_content)
    ]
    length_range = _parse_numeric_length_band(scene.target_length_band)
    current_length = _visible_char_count(source_content)
    brief = [
        "This is the only safety repair attempt. Edit the labeled rejected draft directly, keep its distinctive reusable style, and change only what the hard constraints require.",
        "Return only the complete replacement scene_text prose: no reasoning, markdown fence, JSON wrapper, schema label, or commentary.",
    ]
    if required_terms:
        brief.append(
            "Every final required constraint must be explicit. A vertical bar means alternatives; include at least one literal alternative from each group: "
            + "；".join(required_terms)
            + "。"
        )
    if missing_terms:
        brief.append(
            "Restore the currently missing required constraints: "
            + "；".join(missing_terms)
            + "。"
        )
    if length_range is not None:
        minimum, maximum = length_range
        local_minimum, local_maximum, target = _style_repair_working_window(
            minimum,
            maximum,
            source_length=current_length,
        )
        brief.append(
            f"Final visible Chinese prose length must be {minimum}-{maximum} characters; "
            f"the rejected draft is about {current_length}. Aim near {target} and keep the working "
            f"window at {local_minimum}-{local_maximum}; never use the absolute maximum as the target."
        )
        if current_length < minimum:
            brief.append(
                f"Add {local_minimum - current_length}-{local_maximum - current_length} visible characters; do not return fewer or more than that correction range. "
                "Keep every existing factual beat in order; "
                + (
                    "expand inside the same event with the reference author's own means as the [风格样例] show "
                    "(summary, digression, dialogue, description, reflection) "
                    if style_first
                    else "add concrete action-reaction, blocking, perception, or consequence inside the same event "
                )
                + f"until {local_minimum}-{local_maximum} visible characters are present."
            )
        elif current_length > maximum:
            brief.append(
                f"Remove {current_length - local_maximum}-{current_length - local_minimum} visible characters by compressing repetition only, "
                f"then stop inside {local_minimum}-{local_maximum}; do not remove any required fact, causal step, or ending hook."
            )
    if _scene_text_integrity_markers(source_content):
        brief.append(
            "Remove malformed Unicode escapes, placeholder controls, and encoding artifacts while preserving the intended Chinese prose."
        )
    brief.append(
        "Use the Scene Card as factual authority. Do not invent a new event, change chronology, or replace the ending hook."
    )
    return brief
