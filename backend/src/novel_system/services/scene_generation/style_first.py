"""作者手笔直起时的风格步（风格参考 v3 P5b）：按读数决定——首稿在作者正常范围内（或读数不可信）就不调模型，
首稿即风格稿；越界才只改越界的维（定向修改，模板 ``style_targeted_revision``，走 style_draft 节点路由），改完再读，
不更像 / 没过抄袭门 / 没过安全门就保留首稿。旧的「再靠近一层的复读」真实运行里 3/3 场越改越远。

检查点次序（neutral_ready → hard_qc_ready → style_ready）与步位不变；「首稿即风格稿」的产品沿用首稿那次调用的
谱系（检查点据此认它）。
"""

from __future__ import annotations

import logging
import time
import uuid
from copy import deepcopy
from typing import Any, Callable, Mapping

from sqlalchemy import select

import novel_system.services.scene_generation.fidelity_probe as fidelity_probe
from novel_system.db.models import AttemptTracker, SceneCard, SceneRunState
from novel_system.services import reference_copy_gate
from novel_system.services.errors import DomainError
from novel_system.services.llm_task_runner import LLMNodeExecutionError
from novel_system.services.scene_generation.briefs import (
    JSON_SCHEMA_INSTRUCTION,
    _author_note_instruction_for_bundle,
)
from novel_system.services.scene_generation.contracts import (
    FIRST_DRAFT_SOURCE_LABEL,
    LINEAGE_FIRST_DRAFT_ACCEPTED,
    REASON_COPY_UNCHECKED,
    STYLE_STEP_VERSION,
    GenerationHost,
    SceneGenerationPostprocessError,
    StyleGenerationResult,
)
from novel_system.services.scene_generation.ledger import (
    DraftLedger,
    first_draft_lineage,
    raise_original_runner_error,
    runtime_audit,
)
from novel_system.services.scene_generation.length_policy import (
    LengthPolicy,
    _style_length_instruction,
)
from novel_system.services.scene_generation.notices import (
    STYLE_NOTICE_FIRST_DRAFT_ACCEPTED,
    STYLE_NOTICE_REVISION_REJECTED,
    _styled_draft_gate_notices,
    style_injection_notices,
    style_notice,
)
from novel_system.services.scene_generation.text_gates import (
    _assess_style_base_rewrite,
    _extract_scene_text,
    _visible_char_count,
)
from novel_system.services.style_prompt_injection import (
    PLACEMENT_USER_TAIL,
    ROLE_REVISE,
    apply_style_user_tail,
)
from novel_system.services.style_reference import readings as style_readings
from novel_system.services.style_reference import style_step
from novel_system.services.style_reference.card import card_from_profile_json, line_states_from_profile_json
from novel_system.services.style_reference.runtime_contract import DRAFT_MODE_STYLE_FIRST, contract_layer


_LOGGER = logging.getLogger(__name__)


def _policy_card(policy: Any) -> tuple[Any, dict[str, str]]:
    """策略冻结的画像里的文风卡与作者的 ✓ / ✗（旧画像没有卡 → ``(None, {})``）。"""
    contract = getattr(policy, "contract", None)
    layer = contract_layer(contract if isinstance(contract, Mapping) else None)
    profile = layer.get("profile") if isinstance(layer.get("profile"), Mapping) else {}
    profile_json = profile.get("profile_json") if isinstance(profile.get("profile_json"), Mapping) else {}
    return card_from_profile_json(profile_json), line_states_from_profile_json(profile_json)


def style_first_step(
    svc: GenerationHost,
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
        return finish_resume(
            svc,
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
            svc.session, scene, policy, first_row_id, first_content, thresholds
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
        return accept_first_draft(
            svc,
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
    return run_targeted_revision(
        svc,
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


def gate_decision(decision: Mapping[str, Any]) -> dict[str, Any]:
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


def emit_products(
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
            "gate_decision": gate_decision(decision),
            "source_base_row_id": product.row_id,
            "de_template_outcome": {"status": "not_required"},
        },
    )


def finish_resume(
    svc: GenerationHost,
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
    for attempt in svc.session.execute(
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
    emit_products(
        resume_base,
        first_row_id=first_row_id,
        slot_key=slot_key,
        slot_order=slot_order,
        product_callback=product_callback,
        decision=decision,
        emit_base=False,
    )
    return resume_base


def accept_first_draft(
    svc: GenerationHost,
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
    ledger = DraftLedger(svc.session, scene, state, bundle)
    llm_call_id, step_key, execution_id = first_draft_lineage(svc.session, first_row_id)
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
    svc.session.flush()
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
    svc.session.flush()
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
    emit_products(
        product,
        first_row_id=first_row_id,
        slot_key=slot_key,
        slot_order=slot_order,
        product_callback=product_callback,
        decision=decision,
    )
    return product


def run_targeted_revision(
    svc: GenerationHost,
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
    if not svc._prompt_builder().has_template(template_name):
        return accept_first_draft(
            svc,
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
    ledger = DraftLedger(svc.session, scene, state, bundle)
    fallback_llm_call_id = f"llm_call_{scene.scene_id}_{uuid.uuid4().hex[:12]}"
    started_at = time.perf_counter()
    prompt: dict[str, Any] | None = None
    try:
        prompt = svc._prompt_builder().build(bundle["snapshot"], template_name)
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
    prompt = svc._inject_style_reference(
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
        node_result = svc._llm_runner.run(
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
        copy_check = reference_copy_gate.check_reference_copy(svc.session, revision_content, policy=policy)
        # M2：只算修改稿**新带进来**的重合——首稿里本来就有的（修改稿照旧留着）不是这次修改的错，不能因此白花
        # 一次调用、还把「照抄」记到修改头上；首稿自己的重合由硬 QC / 成稿门对全文把关
        introduced = reference_copy_gate.introduced_copy(copy_check, revision_content, first_content)
        copy_blocked = bool(introduced.blocked)
        if not copy_blocked and copy_check.unavailable and not copy_check.hits:
            # 有一边没有查成（书已删 / 策略降级）又没查出命中：不是「查过、没重合」——同样保留首稿（fail-closed）
            copy_blocked = copy_unchecked = True
    except Exception:  # noqa: BLE001 — 抄袭门查不成：按拦下处理（fail-closed），保留首稿
        _LOGGER.warning("copy gate failed on targeted revision for scene %s", scene.scene_id, exc_info=True)
        copy_blocked = copy_unchecked = True
    # L3：读数在保存点里读，失败只回滚保存点（读不出按「不更像」处理），不耽误后面落库与检查点
    revision_reading, _revision_error = fidelity_probe.observe(
        svc.session, policy, revision_content, ref=scene.scene_id, what="revision"
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
    svc.session.flush()
    revision_reading_row = style_readings.record_fidelity_reading(
        svc.session,
        policy=policy,
        text=revision_content,
        source=style_readings.SOURCE_PIPELINE,
        stage=style_readings.STAGE_REVISION,
        scene_id=scene.scene_id,
        project_id=style_readings.scene_project_id(svc.session, scene),
        draft_ref=revision_row_ref,
        reading=revision_reading,
        copy_check=copy_check,
        max_percentile=thresholds.style_step_max_percentile,
    )
    styled_draft_gate: dict[str, Any] | None = None
    if keep:
        styled_draft_gate = svc._styled_draft_style_gate(
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
    svc.session.flush()
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
    emit_products(
        product,
        first_row_id=first_row_id,
        slot_key=slot_key,
        slot_order=slot_order,
        product_callback=product_callback,
        decision=decision,
    )
    return product
