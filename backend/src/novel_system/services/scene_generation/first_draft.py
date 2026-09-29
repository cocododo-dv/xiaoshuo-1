"""首稿（中性步位）：neutral_first 写中性稿（阅读对照组），style_first 以参考作者的手笔直接从 bundle 写首稿
（``style_first_draft`` 模板 + ``[STYLE_REFERENCE]`` 前缀，走 style_draft 节点路由）。不合格就做一次确定性修复，
修复仍不合格就 fail-closed（422，不把已知不合格的稿子当完成稿）。

步位、``stage="neutral_draft"`` 稿行、尝试步名、指针与账本字段两种起草方式都一样；只有 style_first 的尝试明细
多记起草方式、模板、来源与 notices（neutral_first 的明细逐字不变）。
"""

from __future__ import annotations

import time
import uuid
from copy import deepcopy
from typing import Any

import novel_system.services.scene_generation.text_gates as text_gates
from novel_system.db.models import SceneCard, SceneRunState
from novel_system.services.errors import DomainError
from novel_system.services.llm_task_runner import LLMNodeExecutionError
from novel_system.services.scene_generation.briefs import (
    _author_note_instruction_for_bundle,
    _neutral_repair_brief,
)
from novel_system.services.scene_generation.contracts import (
    STYLE_FIRST_DRAFT_CONTENT_SOURCE,
    GenerationHost,
    NeutralGenerationResult,
    SceneGenerationPostprocessError,
    versioned_scene_artifact_id,
)
from novel_system.services.scene_generation.ledger import (
    DraftLedger,
    accepted_draft_step_key,
    counts_as_business_attempt,
    raise_original_runner_error,
    runtime_audit,
)
from novel_system.services.scene_generation.length_policy import (
    LengthPolicy,
    _neutral_length_instruction,
    _style_first_length_instruction,
)
from novel_system.services.scene_generation.notices import (
    STYLE_NOTICE_FIRST_DRAFT,
    _prompt_carries_style_reference,
    _styled_draft_gate_notices,
    style_injection_notices,
    style_notice,
)
from novel_system.services.scene_generation.text_gates import (
    _extract_scene_text,
    _visible_char_count,
)
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_prompt_injection import (
    PLACEMENT_USER_TAIL,
    ROLE_DRAFT,
    apply_style_user_tail,
    frozen_situation_tags,
)


def generate_first_draft(
    svc: GenerationHost,
    scene_id: str,
    bundle: dict[str, Any],
    *,
    author_note: str | None = None,
) -> NeutralGenerationResult:
    scene = svc.session.get(SceneCard, scene_id)
    state = svc.session.get(SceneRunState, scene_id)
    lengths = LengthPolicy.for_scene(bundle, scene)
    ledger = DraftLedger(svc.session, scene, state, bundle)
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
        prompt = svc._prompt_builder().build(bundle["snapshot"], template_name)
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
        prompt = svc._inject_style_reference(
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
        node_result = svc._llm_runner.run(
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
            svc._inject_style_reference(
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
            repaired_result = svc._llm_runner.run(
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
                svc.session.flush()
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
                svc.session.flush()
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
    svc.session.flush()

    styled_draft_gate: dict[str, Any] | None = None
    if style_first:
        # 首稿离原文更近:落库后同样过一次确定性抄袭 + 生成禁用词门(记录 + notice;
        # 升级到人工复核由 hard_qc 阶段的同一 n-gram 门完成)。
        styled_draft_gate = svc._styled_draft_style_gate(
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
    svc.session.flush()

    state.current_neutral_draft_row_id = neutral_row_id
    # 治理 §4.3：latest_valid 与 current_* 分轨——重写/失败路径清 current_* 时该指针保留
    state.latest_valid_draft_row_id = neutral_row_id
    state.current_bundle_id = bundle["bundle_id"]
    state.current_bundle_hash = bundle["bundle_snapshot_hash"]
    state.total_attempt_count += 1
    svc.session.flush()

    return NeutralGenerationResult(
        row_id=neutral_row_id,
        content=neutral_content,
        llm_call_id=node_result.llm_call_id,
        bundle_id=bundle["bundle_id"],
        bundle_hash=bundle["bundle_snapshot_hash"],
        # 检查点记的是**写出这份稿子的那次调用**的步键：修复稿被采用时是 neutral_draft_repair 那次调用，
        # 记成 neutral_draft 会让续跑的账本校验（调用的 execution_step_key 对不上）判检查点损坏
        execution_step_key=accepted_draft_step_key(
            svc.session,
            node_result.llm_call_id,
            repaired=bool(repair_audit and repair_audit.get("accepted")),
        ),
        draft_mode=draft_mode,
        notices=notices,
        styled_draft_gate=styled_draft_gate,
    )
