"""评审行 / 改写候选 / 局部深评的序列化（writer_deep_review 与诊断载荷共用这一份）。"""

from __future__ import annotations

from typing import Any

from novel_system.db.models import PassagePatchCandidate, WriterEvaluation
from novel_system.services.scene_diagnosis.vocabulary import PASSAGE_VERDICT_LABELS, PASSAGE_VERDICTS, SCENE_FORMS


def scene_form_from_findings(findings: list[dict[str, Any]], object_type: str | None = None) -> str | None:
    if object_type != "scene":
        return None
    for finding in findings:
        scene_form = str(finding.get("scene_form") or "")
        if scene_form in SCENE_FORMS:
            return scene_form
    return "plot_scene"


def serialize_evaluation(row: WriterEvaluation | None) -> dict[str, Any] | None:
    if row is None:
        return None
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
        "lens": row.lens or "aggregate",
        "parent_evaluation_id": row.parent_evaluation_id,
        "evidence_spans": row.evidence_spans_json or [],
        "overall_score": row.overall_score,
        "scores": row.scores_json or {},
        "findings": row.findings_json or [],
        "failure_class": row.failure_class,
        "auto_rewrite_eligible": bool(row.auto_rewrite_eligible) if row.auto_rewrite_eligible is not None else None,
        "contract_field_refs": row.contract_field_refs_json or {},
        "promotion_blockers": row.promotion_blockers_json or [],
        "scene_form": scene_form_from_findings(row.findings_json or [], row.object_type),
        "revision_brief": row.revision_brief_json or [],
        "requires_human_review": bool(row.requires_human_review),
        "status": row.status,
        "created_at": row.created_at,
    }


def serialize_patch_candidate(row: PassagePatchCandidate) -> dict[str, Any]:
    return {
        "patch_id": row.patch_id,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "chapter_id": row.chapter_id,
        "scene_id": row.scene_id,
        "source_text_ref": row.source_text_ref,
        "target_text_ref": row.target_text_ref,
        "source_draft_id": row.source_draft_id,
        "generation_llm_call_id": row.generation_llm_call_id,
        "quality_signal_id": row.quality_signal_id,
        "source_excerpt": row.source_excerpt,
        "issue_dimension": row.issue_dimension,
        "candidate_category": row.candidate_category,
        "target_range": row.target_range_json or None,
        "revision_strategy": row.revision_strategy,
        "preference_tags": row.preference_tags_json or [],
        "inserted_into_author_draft": bool(row.inserted_into_author_draft),
        "replacement_options": row.replacement_options_json or [],
        "rationale": row.rationale,
        "manual_only": bool(row.manual_only),
        "status": row.status,
        "author_decision": row.author_decision,
        "selected_option_id": row.selected_option_id,
        "author_decision_note": row.author_decision_note,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def serialize_passage_review(row: WriterEvaluation, status: str) -> dict[str, Any]:
    meta = row.contract_field_refs_json if isinstance(row.contract_field_refs_json, dict) else {}
    verdict = str(meta.get("verdict") or "no_finding")
    focus_paragraphs = [int(value) for value in (meta.get("focus_paragraphs") or []) if isinstance(value, int)]
    if not focus_paragraphs and isinstance(meta.get("paragraph_index"), int):
        focus_paragraphs = [int(meta["paragraph_index"])]
    return {
        "evaluation_id": row.evaluation_id,
        "paragraph_index": meta.get("paragraph_index"),
        "focus_paragraphs": focus_paragraphs,
        "paragraph_start": meta.get("paragraph_start", focus_paragraphs[0] if focus_paragraphs else None),
        "paragraph_end": meta.get("paragraph_end", focus_paragraphs[-1] if focus_paragraphs else None),
        "whole_scene": bool(meta.get("whole_scene", False)),
        "about_signal_id": meta.get("about_signal_id"),
        "about_signal_ids": [str(value) for value in (meta.get("about_signal_ids") or ([meta["about_signal_id"]] if meta.get("about_signal_id") else []))],
        "verdict": verdict if verdict in PASSAGE_VERDICTS else "no_finding",
        "verdict_label": PASSAGE_VERDICT_LABELS.get(verdict, PASSAGE_VERDICT_LABELS["no_finding"]),
        "assessment": str(meta.get("assessment") or ""),
        "rewrite_brief": str(meta.get("rewrite_brief") or ""),
        "question": str(meta.get("question") or ""),
        "findings_count": len([item for item in (row.findings_json or []) if isinstance(item, dict)]),
        "status": status,
        "llm_call_id": row.evaluator_llm_call_id,
        "created_at": row.created_at,
    }
