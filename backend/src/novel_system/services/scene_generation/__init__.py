from __future__ import annotations

import hashlib
import logging
import time
import uuid
from copy import deepcopy
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
from novel_system.services.llm_task_runner import (
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_reference.runtime_contract import (
    DRAFT_MODE_NEUTRAL_FIRST,
    DRAFT_MODE_STYLE_FIRST,
)
from novel_system.services.style_prompt_injection import (
    PLACEMENT_USER_TAIL,
    ROLE_DRAFT,
    ROLE_REVISE,
    apply_style_user_tail,
    frozen_situation_tags,
    inject_style_reference_prefix,
)
from novel_system.services.style_reference import readings as style_readings
from novel_system.services.style_reference import style_step
from novel_system.services.style_reference.fidelity import within_author_range
from novel_system.services.scene_generation.briefs import (
    JSON_SCHEMA_INSTRUCTION,
    _NEUTRAL_STYLE_INSTRUCTION,
    _STYLE_DE_TEMPLATE_REPAIR_TASK_PROMPT,
    _STYLE_SAFETY_REPAIR_TASK_PROMPT,
    _author_note_instruction_for_bundle,
    _de_template_rewrite_brief,
    _neutral_repair_brief,
    _style_safety_repair_brief,
    author_note_instruction,
    build_style_user_prompt,
)
from novel_system.services.scene_generation.contracts import (
    FIRST_DRAFT_SOURCE_LABEL,
    LINEAGE_FIRST_DRAFT_ACCEPTED,
    NEUTRAL_DRAFT_SOURCE_LABEL,
    REASON_COPY_UNCHECKED,
    STYLE_FIRST_DRAFT_CONTENT_SOURCE,
    STYLE_STEP_VERSION,
    NeutralGenerationResult,
    ProductCallback,
    SceneGenerationPostprocessError,
    StyleGenerationResult,
    versioned_scene_artifact_id,
)
from novel_system.services.scene_generation.length_policy import (
    LengthPolicy,
    _neutral_length_instruction,
    _parse_numeric_length_band,
    _reference_scale_sentence,
    _reference_scene_scale_from_bundle,
    _style_first_length_instruction,
    _style_first_length_slack,
    _style_length_instruction,
    _style_repair_length_instruction,
)
from novel_system.services.scene_generation import fidelity_probe, text_gates
from novel_system.services.scene_generation.ledger import (
    DraftLedger,
    accepted_draft_step_key,
    counts_as_business_attempt,
    first_draft_lineage,
    raise_original_runner_error,
    resume_base_safety,
    resume_style_repair_source,
    runtime_audit,
)
from novel_system.services.scene_generation.segment_patch import (
    _annotate_style_length_patch_source,
    _apply_style_length_patch,
    _apply_style_salvage_patch,
    _constrain_style_length_patch_schema,
    _constrain_style_salvage_schema,
    _requires_style_salvage,
    _style_length_patch_editable_segment_ids,
    _style_length_patch_instruction,
    _style_salvage_editable_segment_ids,
    _style_salvage_instruction,
)
from novel_system.services.scene_generation.text_gates import (
    ConstraintSnapshot,
    _anti_template_quality_gate,
    _assess_de_template_rewrite,
    _assess_neutral_draft,
    _assess_style_base_rewrite,
    _extract_scene_text,
    _scene_text_integrity_markers,
    _visible_char_count,
    assess_rewrite_regressions,
)
from novel_system.services.scene_generation.notices import (
    STYLE_NOTICE_ATTEMPT_STEPS,
    STYLE_NOTICE_BANNED_TERM_HIT,
    STYLE_NOTICE_CODES,
    STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL,
    STYLE_NOTICE_FIRST_DRAFT,
    STYLE_NOTICE_FIRST_DRAFT_ACCEPTED,
    STYLE_NOTICE_GATE_UNAVAILABLE,
    STYLE_NOTICE_INJECTION_DEGRADED,
    STYLE_NOTICE_INJECTION_MISS,
    STYLE_NOTICE_PATCH_REVERTED,
    STYLE_NOTICE_PLAGIARISM_HIT,
    STYLE_NOTICE_REFERENCE_BOOK_CHANGED,
    STYLE_NOTICE_REFERENCE_BOOK_MISSING,
    STYLE_NOTICE_REFERENCE_NO_WINDOWS,
    STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED,
    STYLE_NOTICE_REVISION_REJECTED,
    STYLE_PATCH_KEEP_STEP,
    _prompt_carries_style_reference,
    _styled_draft_gate_notices,
    latest_style_notices,
    render_audit_notices,
    style_injection_notices,
    style_notice,
)

# 门面：编排器、路由与测试从这里取名字；子模块才是它们的家。下划线开头的是测试直接调用的内部助手——要替换
# （monkeypatch）某个助手，替换它所在的子模块，不是这里。
__all__ = [
    "FIRST_DRAFT_SOURCE_LABEL",
    "LengthPolicy",
    "LINEAGE_FIRST_DRAFT_ACCEPTED",
    "NEUTRAL_DRAFT_SOURCE_LABEL",
    "REASON_COPY_UNCHECKED",
    "STYLE_FIRST_DRAFT_CONTENT_SOURCE",
    "STYLE_NOTICE_ATTEMPT_STEPS",
    "STYLE_NOTICE_BANNED_TERM_HIT",
    "STYLE_NOTICE_CODES",
    "STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL",
    "STYLE_NOTICE_FIRST_DRAFT",
    "STYLE_NOTICE_FIRST_DRAFT_ACCEPTED",
    "STYLE_NOTICE_GATE_UNAVAILABLE",
    "STYLE_NOTICE_INJECTION_DEGRADED",
    "STYLE_NOTICE_INJECTION_MISS",
    "STYLE_NOTICE_PATCH_REVERTED",
    "STYLE_NOTICE_PLAGIARISM_HIT",
    "STYLE_NOTICE_REFERENCE_BOOK_CHANGED",
    "STYLE_NOTICE_REFERENCE_BOOK_MISSING",
    "STYLE_NOTICE_REFERENCE_NO_WINDOWS",
    "STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED",
    "STYLE_NOTICE_REVISION_REJECTED",
    "STYLE_PATCH_KEEP_STEP",
    "STYLE_STEP_VERSION",
    "NeutralGenerationResult",
    "ProductCallback",
    "SceneGenerationPostprocessError",
    "ConstraintSnapshot",
    "SceneGenerationService",
    "StyleGenerationResult",
    "_anti_template_quality_gate",
    "_apply_style_length_patch",
    "_apply_style_salvage_patch",
    "_assess_de_template_rewrite",
    "_assess_neutral_draft",
    "_assess_style_base_rewrite",
    "_extract_scene_text",
    "_neutral_length_instruction",
    "_neutral_repair_brief",
    "_parse_numeric_length_band",
    "_reference_scale_sentence",
    "_reference_scene_scale_from_bundle",
    "_style_first_length_instruction",
    "_style_first_length_slack",
    "_style_length_instruction",
    "_style_repair_length_instruction",
    "_visible_char_count",
    "assess_rewrite_regressions",
    "author_note_instruction",
    "_scene_text_integrity_markers",
    "_styled_draft_gate_notices",
    "inject_style_reference_prefix",
    "latest_style_notices",
    "render_audit_notices",
    "style_injection_notices",
    "style_notice",
    "style_step",
    "versioned_scene_artifact_id",
]

_LOGGER = logging.getLogger(__name__)
# 生成侧要跑 styled-draft gate 的阶段：落库内容是 provider 的风格化输出、且会成为终稿
# 候选的每一个阶段。style_draft 回退中性稿时不跑（内容是已批准的中性稿）。
_STYLED_GATE_GENERATION_STAGES: frozenset[str] = frozenset(
    {"style_draft", "near_final_rewrite"}
)
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
        lengths = LengthPolicy.for_scene(bundle, scene)
        ledger = DraftLedger(self.session, scene, state, bundle)
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
            ledger.persist_generation_failure(
                llm_call_id=fallback_llm_call_id,
                step="neutral_draft",
                node_id=draft_node_id,
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
            user_prompt = base_user_prompt + _style_first_length_instruction(lengths)
            prompt = self._inject_style_reference(
                base_prompt,
                scene,
                task_type="scene_generation",
                bundle=bundle,
                context_text=None,
                final_user_prompt=user_prompt,
                placement=PLACEMENT_USER_TAIL,
                role=ROLE_DRAFT,
                node_id=draft_node_id,
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
            user_prompt = base_user_prompt + _neutral_length_instruction(lengths)
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
            ledger.record_runner_failure(
                step="neutral_draft",
                prompt=prompt,
                exc=exc,
            )
            if isinstance(exc, LLMNodeExecutionError):
                raise_original_runner_error(exc)
            raise

        neutral_row_id = versioned_scene_artifact_id("draft_neutral", scene_id, bundle)
        neutral_assessment = text_gates._assess_neutral_draft(scene, neutral_content, lengths)
        repair_audit: dict[str, Any] | None = None
        if not neutral_assessment["accepted"]:
            original_content = neutral_content
            original_result = node_result
            repair_length_instruction = (
                _style_first_length_instruction(
                    lengths,
                    previous_length=_visible_char_count(original_content),
                    retry=True,
                )
                if style_first
                else _neutral_length_instruction(
                    lengths,
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
                    node_id=draft_node_id,
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
                repaired_assessment = text_gates._assess_neutral_draft(scene, repaired_content, lengths)
            except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
                ledger.record_runner_failure(
                    step="neutral_draft_repair",
                    # 记的是修复这一遍实际发出的提示（style_first 下重新注入过、审计不同），不是首稿那一份
                    prompt=repair_prompt_payload,
                    exc=exc,
                )
                # 第一遍 provider 调用已经真实消耗了一次业务尝试。若修复在
                # provider dispatch 前被预算/连续性门拒绝，通用失败记录不会计数，
                # 这里补记一次；无论哪类失败都不能把原始不合格稿伪装成完成稿。
                if not counts_as_business_attempt(exc):
                    state.total_attempt_count += 1
                    self.session.flush()
                if isinstance(exc, LLMNodeExecutionError):
                    raise_original_runner_error(exc)
                raise
            else:
                repair_accepted = bool(repaired_assessment["accepted"])
                rejected_content = (
                    original_content if repair_accepted else repaired_content
                )
                rejected_result = (
                    original_result if repair_accepted else repaired_result
                )
                rejected_row_id = ledger.add_rejected(
                    neutral_row_id,
                    stage="neutral_rejected",
                    content=rejected_content,
                    llm_call_id=rejected_result.llm_call_id,
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
                    "original_assessment": text_gates._assess_neutral_draft(
                        scene, original_content, lengths
                    ),
                    "repair_assessment": repaired_assessment,
                }
                if not repair_accepted:
                    ledger.add_attempt(
                        "neutral_draft",
                        {
                            "llm_call_id": repaired_result.llm_call_id,
                            "error_code": "NEUTRAL_DRAFT_REPAIR_INVALID",
                            "rejected_row_id": rejected_row_id,
                            "validation": repaired_assessment,
                            "repair": repair_audit,
                            "business_attempt_consumed": True,
                        },
                        status="failed",
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

        ledger.add_draft(
            neutral_row_id,
            stage="neutral_draft",
            content=neutral_content,
            llm_call_id=node_result.llm_call_id,
        )
        self.session.flush()

        styled_draft_gate: dict[str, Any] | None = None
        if style_first:
            # 首稿离原文更近:落库后同样过一次确定性抄袭 + 生成禁用词门(记录 + notice;
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
            reference_runtime = runtime_audit(
                prompt, outcome=STYLE_FIRST_DRAFT_CONTENT_SOURCE, draft_mode=draft_mode, notices=notices
            )
            if reference_runtime is not None:
                attempt_details["style_reference_runtime"] = reference_runtime
        ledger.add_attempt(
            "neutral_draft",
            attempt_details,
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
            # 检查点记的是**写出这份稿子的那次调用**的步键：修复稿被采用时是 neutral_draft_repair 那次调用，
            # 记成 neutral_draft 会让续跑的账本校验（调用的 execution_step_key 对不上）判检查点损坏
            execution_step_key=accepted_draft_step_key(
                self.session,
                node_result.llm_call_id,
                repaired=bool(repair_audit and repair_audit.get("accepted")),
            ),
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
            source_label=NEUTRAL_DRAFT_SOURCE_LABEL,
            source_row_id=neutral_draft_row_id,
            extra_instruction=(
                _NEUTRAL_STYLE_INSTRUCTION
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
        from novel_system.services.literary_quality import adversarial_rank_score

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
                adversarial_rank_score(candidate.content),
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
                    source_label=NEUTRAL_DRAFT_SOURCE_LABEL,
                    source_row_id=neutral_draft_row_id,
                    extra_instruction=(
                        _NEUTRAL_STYLE_INSTRUCTION
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
                    },
                    product_slot_key=slot_key,
                    product_slot_order=idx,
                    resume_base=durable_bases.get(slot_key),
                    product_callback=product_callback,
                    step_reconciler=step_reconciler,
                )
                score = adversarial_rank_score(result.content)
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
                        source_label=NEUTRAL_DRAFT_SOURCE_LABEL,
                        source_row_id=neutral_draft_row_id,
                        extra_instruction=(
                            _NEUTRAL_STYLE_INSTRUCTION
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
                            adversarial_rank_score(result.content),
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

        # 2026-09-14 减法:候选重排层(shadow / active、基准授权)已删除——候选保持质量序;每个候选仍读一次
        # 「像不像」读数(style_score)并过唯一抄袭门(盲选门据此剔除抄袭 / 没检查成的候选,工作台据此展示)。
        for rank, (result, score) in enumerate(candidates):
            result.ranking_audit = self._candidate_style_assessment(
                bundle, result, float(score), rank=rank
            )

        best_result = candidates[0][0]
        # §6 Defect D: persist dispersion score for author-facing quality signal
        if len(candidates) >= 2:
            final_dispersion = _candidate_dispersion([c.content for c, _ in candidates])
            state.candidate_dispersion_score = round(final_dispersion, 4)
        DraftLedger(self.session, scene, state, bundle).point_style(best_result.row_id)

        return [result for result, _ in candidates]

    def _candidate_style_assessment(
        self,
        bundle: dict[str, Any],
        result: "StyleGenerationResult",
        quality_score: float,
        *,
        rank: int,
    ) -> dict[str, Any]:
        """单个候选的审计：``style_score`` 由读数给（``1 − percentile/100``，四位小数；读不出 / 不可信 → None），
        原文重合走唯一抄袭门；候选保持质量序（风格参考 v3 S2：旧的 21 指标包络已删）。

        读数或抄袭门抛异常 → ``plagiarism_checked=False`` / ``plagiarism_passed=None``：有绑定时终选门不把
        「没检查成」的候选交给作者盲选（fail-closed；成稿门仍是最后一道），候选本身照常交付。"""
        rerank: dict[str, Any] = {"applied_mode": "off", "reason": None}
        audit: dict[str, Any] = {
            "row_id": result.row_id,
            "quality_score": round(float(quality_score), 6),
            "style_score": None,
            "fidelity_distance": None,
            "fidelity_percentile": None,
            "rank": rank,
            "selected": rank == 0,
            "selection_reason": "quality_order",
            "plagiarism_checked": False,
            "plagiarism_passed": None,
            "plagiarism_hit_count": 0,
            "plagiarism_max_match_chars": 0,
        }
        content = result.content or ""
        try:
            policy = style_policy_for_bundle(bundle)
            rerank["runtime_contract_mode"] = policy.mode
            if not policy.bound:
                rerank["reason"] = (
                    "bundle_has_no_style_profile"
                    if policy.mode == "absent"
                    else (policy.error_code or "frozen_runtime_contract_unavailable")
                )
                return {**audit, "rerank": rerank}
            if not content.strip():
                rerank["reason"] = "empty_candidate"
                return {**audit, "rerank": rerank}
            from novel_system.services.reference_copy_gate import check_reference_copy

            # 读数在保存点里读（第一次读一本书要建窗口索引、写库），失败只回滚保存点、不弄坏会话
            with self.session.begin_nested():
                reading = style_readings.reading_for_text(self.session, policy, content)
            # 风格参考 v3：候选的原文重合走唯一抄袭门（按书一次索引、同一稿不重复扫描）
            copy = check_reference_copy(self.session, content, policy=policy)
        except Exception as exc:  # noqa: BLE001 — 读数 / 抄袭门是可选增强,不阻断候选交付;但「没检查成」要如实记
            _LOGGER.warning(
                "style candidate assessment degraded for scene %s", result.row_id, exc_info=True
            )
            return {
                **audit,
                "rerank": {
                    **rerank,
                    "reason": "assessment_internal_error",
                    "error_code": getattr(exc, "code", exc.__class__.__name__),
                },
            }
        if reading is None:
            rerank["reason"] = "reading_unavailable"
        else:
            audit["fidelity_distance"] = reading.distance
            audit["fidelity_percentile"] = reading.percentile
            if reading.reliable:
                audit["style_score"] = round(1.0 - float(reading.percentile) / 100.0, 4)
            else:
                rerank["reason"] = "reading_unreliable"
        if copy.unavailable and not copy.hits:
            # 有一边没有查成（书已删 / 策略降级）：不能当成「查过、没重合」
            rerank["copy_gate"] = "unavailable"
            return {**audit, "rerank": rerank}
        audit.update(
            {
                "plagiarism_checked": True,
                "plagiarism_passed": not copy.hits,
                "plagiarism_hit_count": len(copy.hits),
                "plagiarism_max_match_chars": max((hit.matched_chars for hit in copy.hits), default=0),
            }
        )
        return {**audit, "rerank": rerank}

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
        first_reading_done: bool = False,
        first_reading_error: str | None = None,
        candidate_mode: bool = False,
        force_accept: bool = False,
        temperature_override: float | None = None,
        attempt_details_extra: dict[str, Any] | None = None,
    ) -> StyleGenerationResult:
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
        reading_error = first_reading_error
        if first_reading is None and first_reading_id is None and not first_reading_done:
            first_reading, first_reading_id, reading_error = fidelity_probe.record_first_draft_reading(
                self.session, scene, policy, first_row_id, first_content, thresholds
            )
        revise, gate_reason = style_step.style_step_gate(first_reading, thresholds)
        if reading_error is not None and first_reading is None:
            # L8：读数出错（异常）与「参考书没有可用的尺子」是两回事，原因与提示分开说
            gate_reason = style_step.REASON_READING_FAILED
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

    def _neutral_first_style_keep(
        self,
        *,
        ledger: DraftLedger,
        scene: SceneCard,
        bundle: dict[str, Any],
        policy: Any,
        row_id: str,
        neutral_row_id: str,
        neutral_content: str,
        style_content: str,
        llm_call_id: str,
        notices: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str | None, str]:
        """中性首稿再润色（neutral_first）且有绑定（风格参考 v3 S2 c）：风格稿出来后各读一次读数，用
        :func:`style_step.revision_keep_decision` 与中性稿比——不更像（distance 没有小 ``revision_min_improvement``）
        就把中性稿当风格稿交付，与作者手笔直起同一条「永不越改越远」规则、同一组阈值；读数不可信 / 读不出 /
        读数出错 → 照旧接受风格稿（决定里记原因）。

        返回 ``(风格步决定, 被退回的风格稿行 id 或 None, 交付的正文)``。读数记进 ``style_fidelity_readings``
        （中性稿 first_draft、风格稿 revision，按稿行幂等）；被退回的风格稿另存一行 ``style_rejected``。"""
        thresholds = style_step.fidelity_thresholds()
        neutral_reading, neutral_error = fidelity_probe.observe(
            self.session, policy, neutral_content, ref=scene.scene_id, what="neutral-draft"
        )
        style_reading, style_error = fidelity_probe.observe(
            self.session, policy, style_content, ref=scene.scene_id, what="style-draft"
        )
        project_id = style_readings.scene_project_id(self.session, scene)
        neutral_row = (
            style_readings.record_fidelity_reading(
                self.session,
                policy=policy,
                text=neutral_content,
                source=style_readings.SOURCE_PIPELINE,
                stage=style_readings.STAGE_FIRST_DRAFT,
                scene_id=scene.scene_id,
                project_id=project_id,
                draft_ref=neutral_row_id,
                reading=neutral_reading,
                max_percentile=thresholds.style_step_max_percentile,
            )
            if neutral_reading is not None
            else None
        )
        comparable = (
            neutral_reading is not None
            and style_reading is not None
            and bool(neutral_reading.reliable)
            and bool(style_reading.reliable)
        )
        if comparable:
            keep, reason = style_step.revision_keep_decision(
                neutral_reading,
                style_reading,
                copy_blocked=False,
                base_safe=True,
                thresholds=thresholds,
            )
        else:
            keep = True
            if (neutral_reading is None and neutral_error) or (style_reading is None and style_error):
                reason = style_step.REASON_READING_FAILED
            elif neutral_reading is None or style_reading is None:
                reason = style_step.REASON_READING_UNAVAILABLE
            else:
                reason = style_step.REASON_READING_UNRELIABLE
        rejected_row_id: str | None = None
        delivered = style_content
        if not keep:
            rejected_row_id = ledger.add_rejected(
                row_id,
                stage="style_rejected",
                content=style_content,
                llm_call_id=llm_call_id,
            )
            delivered = neutral_content
            notices.append(
                style_notice(
                    STYLE_NOTICE_REVISION_REJECTED,
                    "风格稿没有比中性稿更像参考作者（读数没有变近），这一场交付中性稿。",
                    severity="info",
                    reason=reason,
                    first_distance=getattr(neutral_reading, "distance", None),
                    revision_distance=getattr(style_reading, "distance", None),
                    rejected_candidate_row_id=rejected_row_id,
                    draft_mode=DRAFT_MODE_NEUTRAL_FIRST,
                )
            )
        style_row = (
            style_readings.record_fidelity_reading(
                self.session,
                policy=policy,
                text=style_content,
                source=style_readings.SOURCE_PIPELINE,
                stage=style_readings.STAGE_REVISION,
                scene_id=scene.scene_id,
                project_id=project_id,
                draft_ref=rejected_row_id or row_id,
                reading=style_reading,
                max_percentile=thresholds.style_step_max_percentile,
            )
            if style_reading is not None
            else None
        )
        decision = {
            "version": STYLE_STEP_VERSION,
            "decision": style_step.DECISION_REVISION_KEPT if keep else style_step.DECISION_REVISION_REJECTED,
            "reason": reason,
            "llm_call": True,
            "draft_mode": DRAFT_MODE_NEUTRAL_FIRST,
            "dimensions": [],
            "first_reading": style_step.reading_brief(
                neutral_reading, reading_id=neutral_row.reading_id if neutral_row is not None else None
            ),
            "revision_reading": style_step.reading_brief(
                style_reading, reading_id=style_row.reading_id if style_row is not None else None
            ),
            "thresholds": thresholds.audit(),
        }
        return decision, rejected_row_id, delivered

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
        ledger = DraftLedger(self.session, scene, state, bundle)
        llm_call_id, step_key, execution_id = first_draft_lineage(self.session, first_row_id)
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
                    style_step.REASON_READING_FAILED: "首稿的读数这次没算出来（读数出了错，不是参考书没有尺子），风格步没有再调模型，首稿即风格稿；下次运行会重新读。",
                    style_step.REASON_CANDIDATE_SLOT: "首稿作为候选之一参与按读数排序。",
                }.get(reason, "风格步保留了首稿。"),
                severity="warning" if reason == style_step.REASON_READING_FAILED else notice_severity,
                reason=reason,
                percentile=getattr(first_reading, "percentile", None),
                distance=getattr(first_reading, "distance", None),
            )
        notices = [notice]
        ledger.add_draft(
            row_id,
            stage="style_draft",
            content=first_content,
            llm_call_id=llm_call_id,
        )
        self.session.flush()
        ledger.add_attempt(
            "style_draft",
            {
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
        self.session.flush()
        ledger.point_style(row_id)
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
        lengths = LengthPolicy.for_scene(bundle, scene)
        ledger = DraftLedger(self.session, scene, state, bundle)
        fallback_llm_call_id = f"llm_call_{scene.scene_id}_{uuid.uuid4().hex[:12]}"
        started_at = time.perf_counter()
        prompt: dict[str, Any] | None = None
        try:
            prompt = self._prompt_builder().build(bundle["snapshot"], template_name)
        except Exception as exc:
            ledger.persist_generation_failure(
                llm_call_id=fallback_llm_call_id,
                step="style_draft",
                node_id="style_draft",
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
                lengths, source_length=_visible_char_count(first_content), style_first=True
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
            node_id="style_draft",
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
            ledger.record_runner_failure(
                step="style_draft",
                prompt=prompt,
                exc=exc,
                source_draft_row_id=first_row_id,
            )
            if isinstance(exc, LLMNodeExecutionError):
                raise_original_runner_error(exc)
            raise

        base_safety = _assess_style_base_rewrite(
            scene=scene, source_content=first_content, rewritten_content=revision_content, lengths=lengths
        )
        copy_check = None
        introduced = None
        copy_blocked = False
        copy_unchecked = False
        try:
            from novel_system.services.reference_copy_gate import check_reference_copy, introduced_copy

            copy_check = check_reference_copy(self.session, revision_content, policy=policy)
            # M2：只算修改稿**新带进来**的重合——首稿里本来就有的（修改稿照旧留着）不是这次修改的错，不能因此白花
            # 一次调用、还把「照抄」记到修改头上；首稿自己的重合由硬 QC / 成稿门对全文把关
            introduced = introduced_copy(copy_check, revision_content, first_content)
            copy_blocked = bool(introduced.blocked)
            if not copy_blocked and copy_check.unavailable and not copy_check.hits:
                # 有一边没有查成（书已删 / 策略降级）又没查出命中：不是「查过、没重合」——同样保留首稿（fail-closed）
                copy_blocked = copy_unchecked = True
        except Exception:  # noqa: BLE001 — 抄袭门查不成：按拦下处理（fail-closed），保留首稿
            _LOGGER.warning("copy gate failed on targeted revision for scene %s", scene.scene_id, exc_info=True)
            copy_blocked = copy_unchecked = True
        # L3：读数在保存点里读，失败只回滚保存点（读不出按「不更像」处理），不耽误后面落库与检查点
        revision_reading, _revision_error = fidelity_probe.observe(
            self.session, policy, revision_content, ref=scene.scene_id, what="revision"
        )
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
        if copy_unchecked and keep_reason == style_step.REASON_COPY_BLOCKED:
            # 没查成不是查出了重合：原因与提示如实说「没能检查」
            keep_reason = REASON_COPY_UNCHECKED
        rejected_row_id: str | None = None
        if keep:
            content = revision_content
            content_source = style_step.CONTENT_SOURCE_TARGETED_REVISION
            revision_row_ref = row_id
        else:
            rejected_row_id = ledger.add_rejected(
                row_id,
                stage="style_rejected",
                content=revision_content,
                llm_call_id=node_result.llm_call_id,
            )
            content = first_content
            content_source = style_step.CONTENT_SOURCE_REVISION_NOT_CLOSER
            revision_row_ref = rejected_row_id
            notices.append(
                style_notice(
                    STYLE_NOTICE_REVISION_REJECTED,
                    {
                        style_step.REASON_NOT_CLOSER: "定向修改没有让稿子更像参考作者（读数没有变近），保留了首稿。",
                        style_step.REASON_COPY_BLOCKED: "定向修改稿新带进了与参考书原文连续相同的句子，已丢弃，保留首稿。",
                        REASON_COPY_UNCHECKED: "抄袭门这次没能检查定向修改稿（参考书已不在书库或风格策略降级），为免带进没核对过的原文，保留首稿。",
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
        ledger.add_draft(
            row_id,
            stage="style_draft",
            content=content,
            llm_call_id=node_result.llm_call_id,
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
            # 去留只看修改稿新带进来的重合（首稿里本来就有的不算这次修改的）
            "copy_check_introduced": style_readings.copy_check_summary(introduced),
            "base_safety_accepted": bool(base_safety["accepted"]),
            "thresholds": thresholds.audit() if hasattr(thresholds, "audit") else None,
            "candidate_mode": bool(candidate_mode),
            "slot_key": slot_key,
        }
        reference_runtime = runtime_audit(
            prompt, outcome=content_source, draft_mode=DRAFT_MODE_STYLE_FIRST, notices=notices
        )
        ledger.add_attempt(
            "style_draft",
            {
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
                **({"style_reference_runtime": reference_runtime} if reference_runtime is not None else {}),
                **(attempt_details_extra or {}),
            },
        )
        self.session.flush()
        ledger.point_style(row_id)
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
        thresholds = style_step.fidelity_thresholds()
        first_reading, first_reading_id, first_reading_error = fidelity_probe.record_first_draft_reading(
            self.session, scene, policy, first_row_id, first_content, thresholds
        )
        usable = first_reading is not None and first_reading.reliable
        try:
            base_temp = self._llm_runner.task_config("style_draft").temperature
        except KeyError:
            base_temp = 0.7
        slot_count = max(1, int(n_candidates)) if usable else 1
        # L1：续跑时读数可能与第一次不同（书改过、这次读不出），槽位数不能因此缩回去——已经落下检查点的槽位
        # （产品或基稿）一个都不能丢，否则检查点里的工作项对不上，续跑报 RUN_CHECKPOINT_CORRUPT
        resumed_indices = [
            int(key.split(":", 1)[1])
            for key in (*resume_products, *resume_bases)
            if key.startswith("initial:") and key.split(":", 1)[1].isdigit()
        ]
        if resumed_indices:
            slot_count = max(slot_count, max(resumed_indices) + 1)
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
                first_reading_done=True,
                first_reading_error=first_reading_error,
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
        if len(ranked) >= 2:
            state.candidate_dispersion_score = round(_candidate_dispersion([c.content for c in ranked]), 4)
        DraftLedger(self.session, scene, state, bundle).point_style(best.row_id)
        return ranked

    def _rank_style_first_candidates(
        self,
        results: list[tuple[StyleGenerationResult, int]],
        *,
        policy: Any,
        first_reading: Any,
        first_row_id: str,
    ) -> list[StyleGenerationResult]:
        """先按抄袭门（与参考书原文连续相同的候选一律排最后——M2：否则一份被拦的首稿可以凭 distance 赢过干净的
        修改稿、成为风格稿），再按读数 distance 升序排（读不出的排在能读的后面，平手时槽位靠前的在前——首稿赢平手）；
        写每个候选的排序审计。"""
        from novel_system.services.literary_quality import adversarial_rank_score
        from novel_system.services.reference_copy_gate import check_reference_copy

        # 抄袭门的结论：True = 查过没重合，False = 查出重合，None = 没查成（书已删 / 策略降级 / 检查出错）
        scored: list[tuple[StyleGenerationResult, int, Any, bool | None]] = []
        for result, idx in results:
            if (result.content or "") == "":
                reading = None
            elif result.lineage == LINEAGE_FIRST_DRAFT_ACCEPTED or idx == 0:
                reading = first_reading
            else:
                # L3：读不出排最后；读数在保存点里读，失败不弄坏会话
                reading, _error = fidelity_probe.observe(
                    self.session, policy, result.content, ref=result.row_id, what="candidate"
                )
            copy_passed: bool | None
            try:
                copy = check_reference_copy(self.session, result.content or "", policy=policy)
                # 有一边没有查成又没查出命中：不能当成「查过、没重合」（终选门不把没查成的候选交给作者）
                copy_passed = None if (copy.unavailable and not copy.hits) else not copy.blocked
            except Exception:  # noqa: BLE001 — 抄袭门查不成：候选按没查成处理（排最后，终选门会剔除）
                copy_passed = None
            scored.append((result, idx, reading, copy_passed))
        scored.sort(
            key=lambda item: (
                item[3] is not True,
                item[2] is None,
                float(item[2].distance) if item[2] is not None else 0.0,
                item[1],
            )
        )
        seen_texts: dict[str, str] = {}
        ranked: list[StyleGenerationResult] = []
        max_percentile = style_step.fidelity_thresholds().style_step_max_percentile
        for rank, (result, idx, reading, copy_passed) in enumerate(scored):
            normalized = (result.content or "").strip()
            duplicate_of = seen_texts.get(normalized)
            seen_texts.setdefault(normalized, result.row_id)
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
                "plagiarism_checked": copy_passed is not None,
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

    def _run_style_generation(
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
        # 长度带按 bundle 的 StylePolicy（与场景的呈现方式）放宽，这一遍里的验收、指引与补丁都用它
        lengths = LengthPolicy.for_scene(bundle, scene)
        ledger = DraftLedger(self.session, scene, state, bundle)
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
            ledger.persist_generation_failure(
                llm_call_id=fallback_llm_call_id,
                step=llm_step,
                node_id=("style_patch" if llm_step == "soft_patch" else llm_step),
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
        policy = style_policy_for_bundle(bundle)
        style_first = policy.style_first

        if stage == "style_draft":
            # style_draft 步只有 neutral_first 走得到（style_first 在入口就分流到风格步）
            extra_instruction += _style_length_instruction(
                lengths,
                source_length=_visible_char_count(neutral_content),
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
            node_id=("style_patch" if llm_step == "soft_patch" else llm_step),
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
            except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
                ledger.record_runner_failure(
                    step=llm_step,
                    prompt=prompt,
                    exc=exc,
                    source_draft_row_id=source_draft_row_id,
                )
                if isinstance(exc, LLMNodeExecutionError):
                    raise_original_runner_error(exc)
                raise

            base_safety = _assess_style_base_rewrite(
                scene=scene,
                source_content=neutral_content,
                rewritten_content=style_content,
                lengths=lengths,
            )
            rejected_candidate_row_id: str | None = None
            repair_source_row_id = row_id
            repair_source_content = style_content
            if stage == "style_draft" and not base_safety["accepted"]:
                rejected_candidate_row_id = ledger.add_rejected(
                    row_id,
                    stage="style_rejected",
                    content=style_content,
                    llm_call_id=node_result.llm_call_id,
                )
                repair_source_row_id = rejected_candidate_row_id
                repair_source_content = style_content
                # 已批准的中性稿是安全降级真源。保留 provider 原稿为独立 rejected 行,主 style_draft 行只
                # 承载可继续进入 QC/候选选择的安全文本。
                style_content = neutral_content
                notices.append(
                    style_notice(
                        STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL,
                        "风格稿因长度 / 必含项 / 禁用内容 / 文本完整性未通过确定性安全门，"
                        "已回退为已批准的中性稿；后续修复通道会尝试一次带风格的安全修复。",
                        severity="warning",
                        reasons=list(base_safety.get("reasons") or []),
                        rejected_candidate_row_id=rejected_candidate_row_id,
                    )
                )
            # 风格参考 v3（S2 c）：中性首稿再润色且有绑定——风格稿出来后读一次读数，与中性稿比，不更像就把中性稿
            # 当风格稿交付（与作者手笔直起同一条「永不越改越远」规则）；读数不可信 / 未绑定 → 照旧接受风格稿。
            style_step_decision: dict[str, Any] | None = None
            not_closer_row_id: str | None = None
            if stage == "style_draft" and base_safety["accepted"] and policy.bound:
                style_step_decision, not_closer_row_id, style_content = self._neutral_first_style_keep(
                    ledger=ledger,
                    scene=scene,
                    bundle=bundle,
                    policy=policy,
                    row_id=row_id,
                    neutral_row_id=source_draft_row_id,
                    neutral_content=neutral_content,
                    style_content=style_content,
                    llm_call_id=node_result.llm_call_id,
                    notices=notices,
                )
            ledger.add_draft(
                row_id,
                stage=stage,
                content=style_content,
                llm_call_id=node_result.llm_call_id,
            )
            self.session.flush()

            if (
                stage in _STYLED_GATE_GENERATION_STAGES
                and rejected_candidate_row_id is None
                and not_closer_row_id is None
            ):
                # v2（规格 §2.W5.5）styled-draft gate：样例预算放大后，每一份落库的
                # provider 风格化输出都必须过一次确定性抄袭 + 生成禁用词检查。
                # style_draft：命中只记 notice 与审计，升级到人工复核由 soft_qc 阶段的同一
                # gate 完成；near_final_rewrite：输出会直接成为终稿、没有后续 QC，
                # orchestrator 读 result.styled_draft_gate 对抄袭裁决采取行动。
                styled_draft_gate = self._styled_draft_style_gate(
                    scene, style_content, bundle=bundle, stage=stage
                )
                notices.extend(_styled_draft_gate_notices(styled_draft_gate))

            content_source = (
                "approved_neutral_fallback"
                if rejected_candidate_row_id is not None
                else style_step.CONTENT_SOURCE_REVISION_NOT_CLOSER
                if not_closer_row_id is not None
                else "provider_style_output"
            )
            reference_runtime = runtime_audit(
                prompt,
                outcome=content_source,
                draft_mode=DRAFT_MODE_STYLE_FIRST if style_first else DRAFT_MODE_NEUTRAL_FIRST,
                notices=notices,
            )
            ledger.add_attempt(
                llm_step,
                {
                    "row_id": row_id,
                    "llm_call_id": node_result.llm_call_id,
                    "source_draft_row_id": source_draft_row_id,
                    "base_safety": base_safety,
                    "rejected_candidate_row_id": rejected_candidate_row_id,
                    "content_source": content_source,
                    "notices": deepcopy(notices),
                    **(
                        {"styled_draft_gate": deepcopy(styled_draft_gate)}
                        if styled_draft_gate is not None
                        else {}
                    ),
                    **(
                        {
                            "style_step": deepcopy(style_step_decision),
                            "not_closer_rejected_row_id": not_closer_row_id,
                        }
                        if style_step_decision is not None
                        else {}
                    ),
                    **(
                        {"style_reference_runtime": reference_runtime}
                        if reference_runtime is not None
                        else {}
                    ),
                    **(attempt_details_extra or {}),
                },
            )
            self.session.flush()

            ledger.point_style(row_id)
            base_result = StyleGenerationResult(
                row_id=row_id,
                content=style_content,
                llm_call_id=node_result.llm_call_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                execution_step_key=execution_step_key,
                notices=deepcopy(notices),
                styled_draft_gate=deepcopy(styled_draft_gate),
                style_step=deepcopy(style_step_decision) if style_step_decision is not None else None,
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
            base_safety = resume_base_safety(
                self.session,
                scene_id=scene.scene_id,
                row_id=resume_base.row_id,
                fallback=_assess_style_base_rewrite(
                    scene=scene,
                    source_content=neutral_content,
                    rewritten_content=style_content,
                    lengths=lengths,
                ),
            )
            repair_source_row_id, repair_source_content = (
                resume_style_repair_source(
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
            quality_gate = text_gates._anti_template_quality_gate(
                quality_source_content,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
            )
            quality_gate["base_safety"] = base_safety
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
                        lengths=lengths,
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
                        lengths=lengths,
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
                            lengths=lengths,
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
        lengths: LengthPolicy,
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
        ledger = DraftLedger(self.session, scene, state, bundle)
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
            source_label="Segment-addressed Approved Neutral Draft for Style Salvage",
            source_row_id=neutral_row_id,
            extra_instruction=_style_salvage_instruction(
                scene,
                source_content=neutral_content,
                editable_segment_ids=editable_segment_ids,
            ),
        )
        # 注入包的改稿口径（L6）：救稿只按编号改几段，不是「写这一场」；改稿角色也不带近期偏差
        prompt = self._inject_style_reference(
            prompt,
            scene,
            task_type="scene_generation",
            bundle=bundle,
            context_text=neutral_content,
            final_user_prompt=user_prompt,
            placement=PLACEMENT_USER_TAIL,
            role=ROLE_REVISE,
            node_id="style_patch",
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
                    lengths=lengths,
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
            ledger.record_runner_failure(
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

        conformance = fidelity_probe.rewrite_drift(
            self.session,
            policy_or_bundle=bundle,
            source_content=neutral_content,
            rewritten_content=rewritten_content,
        )
        acceptance = text_gates._assess_de_template_rewrite(
            scene=scene,
            source_content=neutral_content,
            authoritative_content=neutral_content,
            rewritten_content=rewritten_content,
            source_quality_gate=quality_gate,
            style_conformance=conformance,
            lengths=lengths,
        )
        salvage_reasons: list[str] = []
        if not salvage_audit.get("valid"):
            salvage_reasons.append("style_salvage_patch_invalid")
        # 消费语义不变(风格参考 v3 S2 d):两稿读数不可比(未绑定 / 不可信 / 读不出)→ 挽救补丁不采用;
        # 改写稿比来源稿远出 patch_max_distance_increase → 不采用
        if conformance.get("comparable") is not True:
            salvage_reasons.append("style_salvage_conformance_unavailable")
        elif conformance.get("regressed") is True:
            salvage_reasons.append("style_salvage_conformance_regressed")
        if salvage_reasons:
            acceptance["reasons"] = list(
                dict.fromkeys([*acceptance.get("reasons", []), *salvage_reasons])
            )
            acceptance["accepted"] = False
        acceptance["style_salvage_non_regression_enforced"] = True
        acceptance["style_salvage_max_distance_increase"] = conformance.get("max_distance_increase")

        ledger.add_draft(
            row_id,
            stage="style_salvage",
            status="active" if acceptance["accepted"] else "rejected",
            content=rewritten_content,
            llm_call_id=node_result.llm_call_id,
        )
        self.session.flush()
        reference_runtime = runtime_audit(prompt)
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
            **({"style_reference_runtime": reference_runtime} if reference_runtime is not None else {}),
        }
        ledger.add_attempt(
            "style_salvage_patch",
            details,
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
        }
        if not acceptance["accepted"]:
            return None, outcome
        ledger.point_style(row_id)
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
        lengths: LengthPolicy,
    ) -> tuple[StyleGenerationResult | None, dict[str, Any]]:
        # 每个触发去模板的候选（source_row_id 已带 _{idx}/_retry_{idx}）必须派生唯一的去模板稿 row_id，
        # 否则 Best-of-N 下 ≥2 个候选都触发反模板闸时，第二条 SceneDraft 撞主键 → IntegrityError → 整跑崩溃。
        # SceneDraft.row_id 为 opaque 主键、不被下游解析，故追加 source_row_id 的短哈希后缀即可（唯一且长度有界）。
        source_suffix = hashlib.sha1(source_row_id.encode("utf-8")).hexdigest()[:10]
        row_id = f"{versioned_scene_artifact_id('draft_style_de_template', scene.scene_id, bundle)}_{source_suffix}"
        ledger = DraftLedger(self.session, scene, state, bundle)
        is_safety_repair = authoritative_content is not None
        base_safety_reasons = set(
            (quality_gate.get("base_safety") or {}).get("reasons") or []
        )
        is_length_patch = bool(
            is_safety_repair
            and base_safety_reasons == {"target_length_not_met"}
            and lengths.hard_range() is not None
        )
        length_patch_audit: dict[str, Any] | None = None
        # 去模板 / 安全修复 / 长度补丁只在 neutral_first 的风格稿链上跑（作者手笔直起在入口就分流到风格步）。
        if is_length_patch:
            # 整篇“修长度”在真实模型上会稳定退化成摘要。程序先给原文分段编号，
            # 模型只提交 segment_id + new_text；原文定位和套用不依赖模型复制精度。
            prompt = self._prompt_builder().build(
                bundle["snapshot"],
                "style_length_patch",
            )
            editable_segment_ids = _style_length_patch_editable_segment_ids(
                source_content,
                lengths,
            )
            annotated_source, _ = _annotate_style_length_patch_source(
                source_content,
                editable_segment_ids=editable_segment_ids,
            )
            _constrain_style_length_patch_schema(
                prompt,
                editable_segment_ids=editable_segment_ids,
                lengths=lengths,
                source_length=_visible_char_count(source_content),
            )
            user_prompt = self._build_style_user_prompt(
                prompt["user_prompt"],
                neutral_content=annotated_source,
                source_label="Segment-addressed Length-only Rejected Style Draft",
                source_row_id=source_row_id,
                extra_instruction=_style_length_patch_instruction(
                    scene,
                    lengths=lengths,
                    source_length=_visible_char_count(source_content),
                    editable_segment_ids=editable_segment_ids,
                ),
            )
        else:
            if is_safety_repair:
                repair_brief = _style_safety_repair_brief(
                    scene=scene,
                    source_content=source_content,
                    authoritative_content=authoritative_content,
                    lengths=lengths,
                )
            else:
                repair_brief = _de_template_rewrite_brief(quality_gate)
            repair_length_instruction = _style_repair_length_instruction(
                lengths,
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
                ),
                patch_brief=repair_brief,
                patch_heading=(
                    "Safety Repair Brief"
                    if is_safety_repair
                    else "De-template Rewrite Brief"
                ),
            )
        # 长度补丁只按编号改几段：用上面装配好的长度补丁模板本身，不带参考前缀。
        if is_safety_repair and not is_length_patch:
            # 这一遍只负责把已生成的风格稿恢复到事实、长度与正文完整性硬约束内。
            # 再注入完整画像会按“不合格源稿”的异常篇幅重算段数/分号目标，并把
            # 一个局部修复重新变成风格重写；真实基准中这会诱发过度压缩与事实丢失。
            # 被拒稿本身已承载可复用风格，故安全修复只使用冻结的原始 style 模板。
            prompt = dict(base_prompt)
        elif not is_safety_repair:
            prompt = self._inject_style_reference(
                base_prompt,
                scene,
                task_type="scene_generation",
                bundle=bundle,
                context_text=source_content,
                final_user_prompt=user_prompt,
                placement=PLACEMENT_USER_TAIL,
                role=ROLE_REVISE,
                node_id="style_patch",
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
                            lengths=lengths,
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
        except (LLMNodeExecutionError, SceneGenerationPostprocessError) as exc:
            ledger.record_runner_failure(
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

        acceptance = text_gates._assess_de_template_rewrite(
            scene=scene,
            source_content=source_content,
            authoritative_content=authoritative_content,
            rewritten_content=rewritten_content,
            source_quality_gate=quality_gate,
            style_conformance=fidelity_probe.rewrite_drift(
                self.session,
                policy_or_bundle=bundle,
                source_content=source_content,
                rewritten_content=rewritten_content,
            ),
            lengths=lengths,
        )

        ledger.add_draft(
            row_id,
            stage="de_template",
            status="active" if acceptance["accepted"] else "rejected",
            content=rewritten_content,
            llm_call_id=node_result.llm_call_id,
        )
        self.session.flush()

        reference_runtime = runtime_audit(prompt)
        ledger.add_attempt(
            "de_template",
            {
                "row_id": row_id,
                "llm_call_id": node_result.llm_call_id,
                # Durable work-item checkpoint 的既有契约以 base row 为父项；
                # 安全修复实际读取的 rejected provider row 另列，避免伪装血缘。
                "source_style_draft_row_id": checkpoint_base_row_id,
                "repair_source_style_draft_row_id": source_row_id,
                "authoritative_source_row_id": authoritative_row_id,
                "quality_gate": quality_gate,
                "acceptance": acceptance,
                **(
                    {"length_patch": length_patch_audit}
                    if length_patch_audit is not None
                    else {}
                ),
                **({"style_reference_runtime": reference_runtime} if reference_runtime is not None else {}),
            },
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

        ledger.point_style(row_id)

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
        return build_style_user_prompt(
            base_prompt,
            neutral_content=neutral_content,
            source_label=source_label,
            source_row_id=source_row_id,
            extra_instruction=extra_instruction,
            patch_brief=patch_brief,
            patch_heading=patch_heading,
        )

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
        node_id: str | None = None,
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
        if node_id:
            # 云策略按这一遍实际派发的节点判（H1）：调用方知道是 style_draft 还是 style_patch，不让适配器按模板从严猜
            extra["node_id"] = node_id
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
        """v2（规格 §2.W5.5）styled-draft gate：对已落库的风格化输出跑抄袭 + 生成禁用词。

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


