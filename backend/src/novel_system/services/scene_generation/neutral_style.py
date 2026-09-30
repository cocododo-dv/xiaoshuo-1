"""风格通道的一遍（``style_draft`` / ``scene_literary_rewrite`` 模板）：neutral_first 的风格稿（重组已批准的
中性稿，含去模板 / 安全修复 / 长度补丁 / 救稿与「不更像就交付中性稿」的读数去留）、软补丁、准终稿改写。

作者手笔直起的 bundle 在入口就分流到 :mod:`.style_first`，``style_draft`` 这一段只有 neutral_first 走得到；软补丁与
准终稿改写两种起草方式都走这里（作者手笔直起时，软补丁按改稿口径渲染参考、低温、未点名的句子逐字保留）。
"""

from __future__ import annotations

import hashlib
import time
import uuid
from copy import deepcopy
from typing import Any

import novel_system.services.scene_generation.fidelity_probe as fidelity_probe
import novel_system.services.scene_generation.text_gates as text_gates
from novel_system.db.models import LlmCall, SceneCard, SceneDraft, SceneRunState
from novel_system.services.errors import DomainError
from novel_system.services.llm_task_runner import LLMNodeExecutionError, current_llm_execution_id
from novel_system.services.scene_generation.briefs import (
    _STYLE_DE_TEMPLATE_REPAIR_TASK_PROMPT,
    _STYLE_SAFETY_REPAIR_TASK_PROMPT,
    _de_template_rewrite_brief,
    _style_safety_repair_brief,
    build_style_user_prompt,
)
from novel_system.services.scene_generation.contracts import (
    STYLE_STEP_VERSION,
    GenerationHost,
    ProductCallback,
    SceneGenerationPostprocessError,
    StepReconciler,
    StyleGenerationResult,
    versioned_scene_artifact_id,
)
from novel_system.services.scene_generation.ledger import (
    DraftLedger,
    raise_original_runner_error,
    resume_base_safety,
    resume_style_repair_source,
    runtime_audit,
)
from novel_system.services.scene_generation.length_policy import (
    LengthPolicy,
    _style_length_instruction,
    _style_repair_length_instruction,
)
from novel_system.services.scene_generation.notices import (
    STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL,
    STYLE_NOTICE_REVISION_REJECTED,
    _styled_draft_gate_notices,
    style_injection_notices,
    style_notice,
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
    _assess_style_base_rewrite,
    _extract_scene_text,
    _visible_char_count,
)
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_prompt_injection import (
    PLACEMENT_USER_TAIL,
    ROLE_REVISE,
    apply_style_user_tail,
)
from novel_system.services.style_reference import readings as style_readings
from novel_system.services.style_reference import style_step
from novel_system.services.style_reference.runtime_contract import (
    DRAFT_MODE_NEUTRAL_FIRST,
    DRAFT_MODE_STYLE_FIRST,
)


# 生成侧要跑 styled-draft gate 的阶段：落库内容是 provider 的风格化输出、且会成为终稿
# 候选的每一个阶段。style_draft 回退中性稿时不跑（内容是已批准的中性稿）。
_STYLED_GATE_GENERATION_STAGES: frozenset[str] = frozenset(
    {"style_draft", "near_final_rewrite"}
)


def run_style_generation(
    svc: GenerationHost,
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
    execution_step_key: str | None = None,
    product_slot_key: str | None = None,
    product_slot_order: int | None = None,
    resume_base: StyleGenerationResult | None = None,
    product_callback: ProductCallback | None = None,
    step_reconciler: StepReconciler | None = None,
    render_role: str | None = None,
) -> StyleGenerationResult:
    # 长度带按 bundle 的 StylePolicy（与场景的呈现方式）放宽，这一遍里的验收、指引与补丁都用它
    lengths = LengthPolicy.for_scene(bundle, scene)
    ledger = DraftLedger(svc.session, scene, state, bundle)
    fallback_llm_call_id = f"llm_call_{scene.scene_id}_{uuid.uuid4().hex[:12]}"
    started_at = time.perf_counter()
    prompt: dict[str, Any] | None = None

    try:
        template_name = (
            "scene_literary_rewrite"
            if llm_step == "scene_literary_rewrite"
            else "style_draft"
        )
        prompt = svc._prompt_builder().build(bundle["snapshot"], template_name)
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

    base_prompt = prompt
    policy = style_policy_for_bundle(bundle)
    style_first = policy.style_first

    if stage == "style_draft":
        # style_draft 步只有 neutral_first 走得到（style_first 在入口就分流到风格步）
        extra_instruction += _style_length_instruction(
            lengths,
            source_length=_visible_char_count(neutral_content),
        )

    user_prompt = build_style_user_prompt(
        base_prompt["user_prompt"],
        neutral_content=neutral_content,
        source_label=source_label,
        source_row_id=source_row_id,
        extra_instruction=extra_instruction,
        patch_brief=patch_brief,
    )
    prompt = svc._inject_style_reference(
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
            node_result = svc._llm_runner.run(
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
            style_step_decision, not_closer_row_id, style_content = neutral_first_style_keep(
                svc,
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
        svc.session.flush()

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
            styled_draft_gate = svc._styled_draft_style_gate(
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
        svc.session.flush()

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
            svc.session,
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
                svc.session,
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
                ) = run_style_salvage_pass(
                    svc,
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
                ) = run_de_template_pass(
                    svc,
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
                    svc.session.get(SceneDraft, followup_row_id)
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
                    ) = run_de_template_pass(
                        svc,
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


def neutral_first_style_keep(
    svc: GenerationHost,
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
        svc.session, policy, neutral_content, ref=scene.scene_id, what="neutral-draft"
    )
    style_reading, style_error = fidelity_probe.observe(
        svc.session, policy, style_content, ref=scene.scene_id, what="style-draft"
    )
    project_id = style_readings.scene_project_id(svc.session, scene)
    neutral_row = (
        style_readings.record_fidelity_reading(
            svc.session,
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
            svc.session,
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


def run_style_salvage_pass(
    svc: GenerationHost,
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
    ledger = DraftLedger(svc.session, scene, state, bundle)
    prompt = svc._prompt_builder().build(
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
    user_prompt = build_style_user_prompt(
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
    prompt = svc._inject_style_reference(
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
        node_result = svc._llm_runner.run(
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
        svc.session,
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
    svc.session.flush()
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
    svc.session.flush()
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


def run_de_template_pass(
    svc: GenerationHost,
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
    ledger = DraftLedger(svc.session, scene, state, bundle)
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
        prompt = svc._prompt_builder().build(
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
        user_prompt = build_style_user_prompt(
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
        user_prompt = build_style_user_prompt(
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
        prompt = svc._inject_style_reference(
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
        node_result = svc._llm_runner.run(
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
            svc.session.get(LlmCall, exc.llm_call_id)
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
            svc.session,
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
    svc.session.flush()

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
    svc.session.flush()

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
