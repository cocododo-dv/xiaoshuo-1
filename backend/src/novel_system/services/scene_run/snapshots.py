"""检查点里哈希的行快照：每个函数把一行（或一份决定）压成固定键的字典，``_json_hash`` 之后存进
``run_checkpoint_json.artifact_hashes``（有的也原样存进 ``artifact_refs``）。

**键名与取值即契约**：库里停在半路的运行续跑时，按这里重算快照、与存下的哈希比对——改一个键、换一种取值
（如 ``list(...)`` / ``deepcopy``）就是改哈希，旧检查点会被判为损坏。两份评审快照（准终稿评审
``writer_evaluation_snapshot`` 与章级评审产品里的 ``archive_writer_evaluation_snapshot``）形状不同，是历史
原样，各自有检查点在用。
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from novel_system.db.models import (
        AttemptTracker,
        ChapterMemory,
        ChapterRollingNote,
        FinalScene,
        NarrativeEvent,
        QcReport,
        RevisionCandidate,
        SceneMemory,
        VolumeSummary,
        WriterEvaluation,
    )
    from novel_system.services.qc_engine import SoftQcDecision


def writer_evaluation_snapshot(evaluation: WriterEvaluation) -> dict[str, Any]:
    return {
        "object_type": evaluation.object_type,
        "object_id": evaluation.object_id,
        "chapter_id": evaluation.chapter_id,
        "scene_id": evaluation.scene_id,
        "rubric_id": evaluation.rubric_id,
        "source_text_ref": evaluation.source_text_ref,
        "source_bundle_id": evaluation.source_bundle_id,
        "evaluator_llm_call_id": evaluation.evaluator_llm_call_id,
        "lens": evaluation.lens,
        "overall_score": evaluation.overall_score,
        "scores": deepcopy(evaluation.scores_json or {}),
        "findings": deepcopy(evaluation.findings_json or []),
        "failure_class": evaluation.failure_class,
        "auto_rewrite_eligible": evaluation.auto_rewrite_eligible,
        "contract_field_refs": deepcopy(evaluation.contract_field_refs_json or {}),
        "promotion_blockers": deepcopy(evaluation.promotion_blockers_json or []),
        "revision_brief": deepcopy(evaluation.revision_brief_json or []),
        "requires_human_review": evaluation.requires_human_review,
        "status": evaluation.status,
    }


def revision_candidate_snapshot(candidate: RevisionCandidate) -> dict[str, Any]:
    return {
        "evaluation_id": candidate.evaluation_id,
        "object_type": candidate.object_type,
        "object_id": candidate.object_id,
        "chapter_id": candidate.chapter_id,
        "scene_id": candidate.scene_id,
        "revision_type": candidate.revision_type,
        "source_text_ref": candidate.source_text_ref,
        "proposed_text": candidate.proposed_text,
        "instruction": deepcopy(candidate.instruction_json or []),
        "diff_summary": deepcopy(candidate.diff_summary_json or {}),
        "patches": deepcopy(candidate.patches_json or []),
        "apply_mode": candidate.apply_mode,
        "target_text_ref": candidate.target_text_ref,
        "status": candidate.status,
        "author_decision_note": candidate.author_decision_note,
        "created_by": candidate.created_by,
    }


def qc_report_snapshot(report: QcReport) -> dict[str, Any]:
    return {
        "qc_type": report.qc_type,
        "status": report.status,
        "source_draft_row_id": report.source_draft_row_id,
        "source_bundle_id": report.source_bundle_id,
        "resolution_code": report.resolution_code,
        "pass_flag": report.pass_flag,
        "next_action": report.next_action,
        "issues": deepcopy(report.issues_json or []),
        "rewrite_brief": deepcopy(report.rewrite_brief_json or []),
    }


def soft_decision_snapshot(
    decision: SoftQcDecision,
    *,
    include_should_continue: bool,
) -> dict[str, Any]:
    snapshot = {
        "branch": decision.branch,
        "qc_report_id": decision.qc_report_id,
        "human_review_event_id": decision.human_review_event_id,
        "resolution_code": decision.resolution_code,
        "next_action": decision.next_action,
        "stop_reason": decision.stop_reason,
        "llm_call_id": decision.llm_call_id,
        "execution_step_key": decision.execution_step_key,
    }
    if include_should_continue:
        snapshot["should_continue"] = decision.should_continue
    return snapshot


def planning_provenance(refs: dict[str, Any], prefix: str) -> dict[str, Any]:
    return {
        "row_id": refs.get(f"{prefix}_row_id"),
        "llm_call_id": refs.get(f"{prefix}_llm_call_id"),
        "execution_step_key": refs.get(f"{prefix}_execution_step_key"),
        "artifact_execution_id": refs.get(f"{prefix}_artifact_execution_id"),
        "reused": refs.get(f"{prefix}_reused"),
    }


def archive_final_scene_snapshot(row: FinalScene) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "scene_id": row.scene_id,
        "chapter_id": row.chapter_id,
        "content": row.content,
        "status": row.status,
        "source_bundle_id": row.source_bundle_id,
        "source_bundle_hash": row.source_bundle_hash,
        "generation_llm_call_id": row.generation_llm_call_id,
        "created_at": row.created_at,
    }


def archive_scene_memory_snapshot(row: SceneMemory) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "scene_id": row.scene_id,
        "chapter_id": row.chapter_id,
        "content": row.content,
        "carry_notes_json": list(row.carry_notes_json or []),
        "source_bundle_id": row.source_bundle_id,
        "final_scene_row_id": row.final_scene_row_id,
        "source_review_id": row.source_review_id,
        "active_flag": row.active_flag,
        "runtime_eligible": row.runtime_eligible,
        "runtime_eligibility_basis": row.runtime_eligibility_basis,
        "effective_at": row.effective_at,
        "created_at": row.created_at,
    }


def archive_rolling_note_snapshot(row: ChapterRollingNote) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "scene_id": row.scene_id,
        "chapter_id": row.chapter_id,
        "source_scene_memory_row_id": row.source_scene_memory_row_id,
        "note_text": row.note_text,
        "revision_no": row.revision_no,
        "updated_at": row.updated_at,
    }


def archive_attempt_snapshot(row: AttemptTracker) -> dict[str, Any]:
    return {
        "attempt_id": row.attempt_id,
        "scene_id": row.scene_id,
        "chapter_id": row.chapter_id,
        "step": row.step,
        "status": row.status,
        "source_bundle_id": row.source_bundle_id,
        "details_json": dict(row.details_json or {}),
        "created_at": row.created_at,
    }


def narrative_event_snapshot(event: NarrativeEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "project_id": event.project_id,
        "scene_id": event.scene_id,
        "chapter_id": event.chapter_id,
        "scene_seq": event.scene_seq,
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "fact_key": event.fact_key,
        "fact_value": event.fact_value,
        "confidence": event.confidence,
        "causal_predecessor_id": event.causal_predecessor_id,
        "theme_tags": list(event.theme_tags or []),
        "obligation_ids": list(event.obligation_ids or []),
        "source_text_excerpt": event.source_text_excerpt,
        "payload_json": dict(event.payload_json or {}),
        "created_at": event.created_at,
    }


def chapter_memory_snapshot(memory: ChapterMemory) -> dict[str, Any]:
    return {
        "row_id": memory.row_id,
        "chapter_id": memory.chapter_id,
        "aggregate_stage": memory.aggregate_stage,
        "content": memory.content,
        "memory_kind": memory.memory_kind,
        "source_review_id": memory.source_review_id,
        "active_flag": memory.active_flag,
        "runtime_eligible": memory.runtime_eligible,
        "runtime_eligibility_basis": memory.runtime_eligibility_basis,
        "effective_at": memory.effective_at,
        "created_at": memory.created_at,
    }


def volume_snapshot(row: VolumeSummary) -> dict[str, Any]:
    return {
        "row_id": row.row_id,
        "project_id": row.project_id,
        "volume_seq": row.volume_seq,
        "chapter_id_start": row.chapter_id_start,
        "chapter_id_end": row.chapter_id_end,
        "chapter_count": row.chapter_count,
        "atmosphere_summary": row.atmosphere_summary,
        "factual_digest": row.factual_digest,
        "active_flag": row.active_flag,
        "runtime_eligible": row.runtime_eligible,
        "runtime_eligibility_basis": row.runtime_eligibility_basis,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def archive_writer_evaluation_snapshot(row: WriterEvaluation) -> dict[str, Any]:
    return {
        "evaluation_id": row.evaluation_id,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "chapter_id": row.chapter_id,
        "scene_id": row.scene_id,
        "rubric_id": row.rubric_id,
        "source_text_ref": row.source_text_ref,
        "source_bundle_id": row.source_bundle_id,
        "evaluator_llm_call_id": row.evaluator_llm_call_id,
        "lens": row.lens,
        "parent_evaluation_id": row.parent_evaluation_id,
        "evidence_spans_json": list(row.evidence_spans_json or []),
        "source_blueprint_row_id": row.source_blueprint_row_id,
        "failure_class": row.failure_class,
        "auto_rewrite_eligible": row.auto_rewrite_eligible,
        "contract_field_refs_json": dict(row.contract_field_refs_json or {}),
        "promotion_blockers_json": list(row.promotion_blockers_json or []),
        "overall_score": row.overall_score,
        "scores_json": dict(row.scores_json or {}),
        "findings_json": list(row.findings_json or []),
        "revision_brief_json": list(row.revision_brief_json or []),
        "requires_human_review": row.requires_human_review,
        "status": row.status,
        "created_at": row.created_at,
    }
