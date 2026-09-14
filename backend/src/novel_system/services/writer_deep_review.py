from __future__ import annotations

import hashlib
import json
import logging
import uuid
from statistics import mean
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    AuthorPreferenceProfile,
    ChapterGoal,
    ChapterMemory,
    FinalScene,
    PassagePatchCandidate,
    ReviewItem,
    SceneCard,
    SceneRunState,
    StoryProject,
    WriterEvaluation,
)
from novel_system.services.author_preferences import merge_preference_summaries, safe_preference_summary_for_prompt
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.llm_task_runner import (
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_lookup import require_chapter, require_scene
from novel_system.services.style_prompt_injection import (
    PLANNING_FEW_SHOT_K_CAP,
    inject_style_reference_prefix,
    resolve_style_scope,
)
from novel_system.settings import get_settings


_LOGGER = logging.getLogger(__name__)
LITERARY_REVISION_RUBRIC_ID = "literary_revision_v1"
LITERARY_REVISION_DIMENSIONS: tuple[str, ...] = (
    "character_contradiction",
    "choice_pressure",
    "relationship_tension",
    "dialogue_subtext",
    "information_rhythm",
    "voice_distinction",
    "image_necessity",
    "repetitive_expression",
    "ending_drive",
    "theme_pressure",
)
DEEP_REVIEW_LENSES: tuple[str, ...] = ("story", "character", "prose", "reader", "theme")
SCENE_FORMS: tuple[str, ...] = (
    "plot_scene",
    "atmosphere_scene",
    "relationship_scene",
    "revelation_scene",
    "transition_scene",
)
PATCH_CATEGORIES: tuple[str, ...] = (
    "dialogue_rewrite",
    "action_replace",
    "ending_pressure",
    "information_reorder",
    "de_model_voice",
    "local_patch",
)


class WriterDeepReviewOutputError(ValueError):
    """The provider completed a call but violated a writer output contract."""


class WriterDeepReviewService:
    def __init__(self, session: Session, *, llm_client: Any | None = None, llm_runner: LLMNodeRunner | None = None) -> None:
        self.session = session
        self.prompt_builder = PromptBuilder()
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)

    def _llm_context(
        self,
        *,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        node_id: str,
        execution_step_key: str,
    ) -> LLMCallContext:
        execution_id = current_llm_execution_id()
        if object_type == "scene":
            scene = self._require_scene(scene_id or object_id)
            chapter = self._require_chapter(scene.chapter_id)
            return LLMCallContext(
                scope_type="scene",
                scope_id=scene.scene_id,
                project_id=scene.project_id or chapter.project_id,
                chapter_id=chapter.chapter_id,
                scene_id=scene.scene_id,
                node_id=node_id,
                step=node_id,
                execution_id=execution_id,
                execution_step_key=execution_step_key if execution_id is not None else None,
                provider_execution_mode=self._llm_runner.provider_execution_mode,
            )
        chapter = self._require_chapter(chapter_id or object_id)
        return LLMCallContext(
            scope_type="chapter",
            scope_id=chapter.chapter_id,
            project_id=chapter.project_id,
            chapter_id=chapter.chapter_id,
            node_id=node_id,
            step=node_id,
            execution_id=execution_id,
            execution_step_key=execution_step_key if execution_id is not None else None,
            provider_execution_mode=self._llm_runner.provider_execution_mode,
        )

    def scene_summary(self, scene_id: str) -> dict[str, Any]:
        self._require_scene(scene_id)
        return self._review_payload("scene", scene_id)

    def chapter_summary(self, chapter_id: str) -> dict[str, Any]:
        self._require_chapter(chapter_id)
        return self._review_payload("chapter", chapter_id)

    def run_scene_review(self, scene_id: str, actor_ref: str = "operator") -> dict[str, Any]:
        scene = self._require_scene(scene_id)
        source = self._scene_source(scene)
        return self._create_deep_review(
            object_type="scene",
            object_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            source=source,
            actor_ref=actor_ref,
        )

    def run_chapter_review(self, chapter_id: str, actor_ref: str = "operator") -> dict[str, Any]:
        chapter = self._require_chapter(chapter_id)
        source = self._chapter_source(chapter)
        return self._create_deep_review(
            object_type="chapter",
            object_id=chapter.chapter_id,
            chapter_id=chapter.chapter_id,
            scene_id=None,
            source=source,
            actor_ref=actor_ref,
        )

    def create_patch_candidate(self, payload: dict[str, Any], actor_ref: str = "operator") -> dict[str, Any]:
        source_excerpt = _required_text(payload, "source_excerpt")
        issue_dimension = _required_text(payload, "issue_dimension")
        object_type = _required_text(payload, "object_type")
        object_id = _required_text(payload, "object_id")
        if object_type not in {"scene", "chapter"}:
            raise DomainError("PASSAGE_PATCH_INVALID", "object_type must be scene or chapter", status_code=400)
        patch_payload = self._run_passage_patch(payload, source_excerpt=source_excerpt, issue_dimension=issue_dimension)
        row = PassagePatchCandidate(
            patch_id=f"passage_patch_{object_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            object_type=object_type,
            object_id=object_id,
            chapter_id=_optional_text(payload, "chapter_id"),
            scene_id=_optional_text(payload, "scene_id"),
            source_text_ref=_optional_text(payload, "source_text_ref") or _optional_text(payload, "target_text_ref"),
            target_text_ref=_optional_text(payload, "target_text_ref"),
            source_draft_id=_optional_text(payload, "source_draft_id"),
            generation_llm_call_id=patch_payload.get("generation_llm_call_id"),
            quality_signal_id=_optional_text(payload, "quality_signal_id"),
            source_excerpt=source_excerpt,
            issue_dimension=issue_dimension,
            candidate_category=_candidate_category(payload, issue_dimension),
            target_range_json=_target_range(payload.get("target_range")),
            revision_strategy=_revision_strategy(payload, issue_dimension),
            preference_tags_json=_preference_tags(payload, issue_dimension),
            inserted_into_author_draft=0,
            replacement_options_json=patch_payload["replacement_options"],
            rationale=patch_payload.get("rationale"),
            manual_only=1,
            status="candidate",
            author_decision="pending",
            created_by=actor_ref or "writer_deep_review",
        )
        self.session.add(row)
        self.session.flush()
        return {"candidate": self.serialize_patch_candidate(row)}

    def accept_patch_candidate(self, patch_id: str, payload: dict[str, Any], actor_ref: str = "operator") -> dict[str, Any]:
        row = self._require_patch_candidate(patch_id)
        selected_option_id = _optional_text(payload, "selected_option_id")
        option_ids = {str(option.get("option_id")) for option in row.replacement_options_json or []}
        if selected_option_id and selected_option_id not in option_ids:
            raise DomainError("PASSAGE_PATCH_OPTION_NOT_FOUND", "selected replacement option not found", status_code=404)
        row.status = "accepted"
        row.author_decision = "accepted"
        row.selected_option_id = selected_option_id
        row.author_decision_note = _optional_text(payload, "note") or row.author_decision_note
        self.session.flush()
        self._refresh_author_preference_profile(actor_ref=actor_ref)
        self.session.flush()
        return {"candidate": self.serialize_patch_candidate(row)}

    def reject_patch_candidate(self, patch_id: str, payload: dict[str, Any], actor_ref: str = "operator") -> dict[str, Any]:
        row = self._require_patch_candidate(patch_id)
        row.status = "rejected"
        row.author_decision = "rejected"
        row.author_decision_note = _optional_text(payload, "note") or row.author_decision_note
        self.session.flush()
        self._refresh_author_preference_profile(actor_ref=actor_ref)
        self.session.flush()
        return {"candidate": self.serialize_patch_candidate(row)}

    def author_preference_profile(self) -> dict[str, Any]:
        profile = self._latest_preference_profile()
        if profile is None:
            return {
                "profile": {
                    "profile_id": "author_pref_global_global",
                    "scope_type": "global",
                    "scope_ref_id": "global",
                    "status": "draft",
                    "runtime_eligible": False,
                    "summary": _empty_preference_summary(),
                    "source_patch_ids": [],
                    "created_at": None,
                    "updated_at": None,
                }
            }
        return {"profile": self.serialize_preference_profile(profile)}

    @staticmethod
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
            "scene_form": _scene_form_from_findings(row.findings_json or [], row.object_type),
            "revision_brief": row.revision_brief_json or [],
            "requires_human_review": bool(row.requires_human_review),
            "status": row.status,
            "created_at": row.created_at,
        }

    @staticmethod
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

    @staticmethod
    def serialize_preference_profile(row: AuthorPreferenceProfile) -> dict[str, Any]:
        return {
            "profile_id": row.profile_id,
            "scope_type": row.scope_type,
            "scope_ref_id": row.scope_ref_id,
            "status": row.status,
            "runtime_eligible": bool(row.runtime_eligible),
            "summary": row.summary_json or _empty_preference_summary(),
            "source_patch_ids": row.source_patch_ids_json or [],
            "created_by": row.created_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _create_deep_review(
        self,
        *,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        source: dict[str, Any],
        actor_ref: str,
    ) -> dict[str, Any]:
        for row in self.session.execute(
            select(WriterEvaluation).where(
                WriterEvaluation.object_type == object_type,
                WriterEvaluation.object_id == object_id,
                WriterEvaluation.rubric_id == LITERARY_REVISION_RUBRIC_ID,
                WriterEvaluation.parent_evaluation_id.is_(None),
            )
        ).scalars().all():
            row.status = "superseded"

        if get_settings().llm_enabled:
            return self._create_deep_review_with_llm(
                object_type=object_type,
                object_id=object_id,
                chapter_id=chapter_id,
                scene_id=scene_id,
                source=source,
            )

        lens_rows: list[WriterEvaluation] = []
        lens_payloads = _diagnose_by_lens(source["content"])
        aggregate_findings: list[dict[str, Any]] = []
        aggregate_scores = {dimension: 0.78 for dimension in LITERARY_REVISION_DIMENSIONS}
        for lens, payload in lens_payloads.items():
            aggregate_findings.extend({**finding, "lens": lens} for finding in payload["findings"])
            for dimension, score in payload["scores"].items():
                aggregate_scores[dimension] = min(aggregate_scores.get(dimension, score), score)

        if not source["content"].strip():
            aggregate_findings.append(
                _finding(
                    lens="story",
                    dimension="source_text",
                    classification="blocking",
                    issue="没有可诊断的正文。",
                    recommendation="先生成或导入正文，再运行深改诊断。",
                    evidence="",
                    why="深改必须基于作者实际文本，不能凭空判断。",
                )
            )
        aggregate_scores = _cap_scores_for_findings(aggregate_scores, aggregate_findings)
        revision_brief = _revision_brief_from_findings(aggregate_findings)
        aggregate_score = round(mean(aggregate_scores.values()), 2) if aggregate_scores else None
        parent = WriterEvaluation(
            evaluation_id=f"writer_deep_eval_{object_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            rubric_id=LITERARY_REVISION_RUBRIC_ID,
            source_text_ref=source.get("source_text_ref"),
            source_bundle_id=source.get("source_bundle_id"),
            evaluator_llm_call_id=None,
            lens="aggregate",
            parent_evaluation_id=None,
            evidence_spans_json=_evidence_spans(source["content"], aggregate_findings),
            overall_score=aggregate_score,
            scores_json=aggregate_scores,
            findings_json=aggregate_findings,
            revision_brief_json=revision_brief,
            requires_human_review=1 if any(item["severity"] == "blocking" for item in aggregate_findings) else 0,
            status="completed",
        )
        self.session.add(parent)
        self.session.flush()

        for lens, payload in lens_payloads.items():
            scores = _cap_scores_for_findings(payload["scores"], payload["findings"])
            row = WriterEvaluation(
                evaluation_id=f"writer_deep_eval_{object_type}_{object_id}_{lens}_{uuid.uuid4().hex[:8]}",
                object_type=object_type,
                object_id=object_id,
                chapter_id=chapter_id,
                scene_id=scene_id,
                rubric_id=LITERARY_REVISION_RUBRIC_ID,
                source_text_ref=source.get("source_text_ref"),
                source_bundle_id=source.get("source_bundle_id"),
                evaluator_llm_call_id=None,
                lens=lens,
                parent_evaluation_id=parent.evaluation_id,
                evidence_spans_json=_evidence_spans(source["content"], payload["findings"]),
                overall_score=round(mean(scores.values()), 2) if scores else None,
                scores_json=scores,
                findings_json=payload["findings"],
                revision_brief_json=_revision_brief_from_findings(payload["findings"]),
                requires_human_review=1 if any(item["severity"] == "blocking" for item in payload["findings"]) else 0,
                status="completed",
            )
            self.session.add(row)
            lens_rows.append(row)
        self.session.flush()
        return self._review_payload(object_type, object_id)

    def _create_deep_review_with_llm(
        self,
        *,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        source: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = {
            "object_type": object_type,
            "object_id": object_id,
            "chapter_id": chapter_id,
            "scene_id": scene_id,
            "rubric_id": LITERARY_REVISION_RUBRIC_ID,
            "dimensions": list(LITERARY_REVISION_DIMENSIONS),
            "lenses": list(DEEP_REVIEW_LENSES),
            "source": source,
            "scene_summary": source.get("content") if object_type == "scene" else None,
            "chapter_summary": source.get("content") if object_type == "chapter" else None,
        }
        prompt = self.prompt_builder.build(snapshot, "writer_deep_review")
        # 2026-09-14 WP6.3：评审在参考作者的手笔下判断「复读 / 意象必要性 / 声音辨识度」
        prompt = self._inject_style_reference_prefix(
            prompt,
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            context_text=str(source.get("content") or "") or None,
            final_user_prompt=prompt["user_prompt"],
        )
        execution_step_key = f"writer_deep_review:{object_type}:{object_id}"
        context = self._llm_context(
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            node_id="writer_deep_review",
            execution_step_key=execution_step_key,
        )
        try:
            node_result = self._llm_runner.run(
                scene_id=scene_id or object_id,
                chapter_id=chapter_id or object_id,
                bundle_id=source.get("source_text_ref") or f"writer_deep_review:{object_type}:{object_id}",
                bundle_hash=hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
                node_id="writer_deep_review",
                step="writer_deep_review",
                prompt=prompt,
                user_prompt=prompt["user_prompt"],
                execution_step_key=execution_step_key,
                context=context,
            )
        except LLMNodeExecutionError as exc:
            raise DomainError(
                "WRITER_DEEP_REVIEW_LLM_FAILED",
                exc.message,
                status_code=409,
                details={
                    "llm_call_id": exc.llm_call_id,
                    "node_id": "writer_deep_review",
                    "error_code": exc.error_code,
                    "next_action": "configure_writer_deep_review_route_and_retry",
                    "response_summary": exc.response_summary,
                },
            ) from exc
        normalized = _normalize_deep_review_output(node_result.response.structured_output or {})
        parent = WriterEvaluation(
            evaluation_id=f"writer_deep_eval_{object_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            rubric_id=LITERARY_REVISION_RUBRIC_ID,
            source_text_ref=source.get("source_text_ref"),
            source_bundle_id=source.get("source_bundle_id"),
            evaluator_llm_call_id=node_result.llm_call_id,
            lens="aggregate",
            parent_evaluation_id=None,
            evidence_spans_json=_evidence_spans(source["content"], normalized["findings"]),
            overall_score=normalized["overall_score"],
            scores_json=normalized["scores"],
            findings_json=normalized["findings"],
            revision_brief_json=normalized["revision_brief"],
            requires_human_review=1 if normalized["requires_human_review"] else 0,
            status="completed",
        )
        self.session.add(parent)
        self.session.flush()

        for payload in normalized["lens_evaluations"]:
            lens = str(payload.get("lens") or "story")
            scores = _normalize_scores(payload.get("scores"))
            findings = _normalize_findings(payload.get("findings"))
            row = WriterEvaluation(
                evaluation_id=f"writer_deep_eval_{object_type}_{object_id}_{lens}_{uuid.uuid4().hex[:8]}",
                object_type=object_type,
                object_id=object_id,
                chapter_id=chapter_id,
                scene_id=scene_id,
                rubric_id=LITERARY_REVISION_RUBRIC_ID,
                source_text_ref=source.get("source_text_ref"),
                source_bundle_id=source.get("source_bundle_id"),
                evaluator_llm_call_id=node_result.llm_call_id,
                lens=lens,
                parent_evaluation_id=parent.evaluation_id,
                evidence_spans_json=_evidence_spans(source["content"], findings),
                overall_score=_optional_score(payload.get("overall_score")) or (round(mean(scores.values()), 2) if scores else None),
                scores_json=scores,
                findings_json=findings,
                revision_brief_json=_normalize_revision_brief(payload.get("revision_brief"), findings),
                requires_human_review=1 if any(item.get("severity") == "blocking" for item in findings) else 0,
                status="completed",
            )
            self.session.add(row)
        self.session.flush()
        return self._review_payload(object_type, object_id)

    def _review_payload(self, object_type: str, object_id: str) -> dict[str, Any]:
        latest = self.session.execute(
            select(WriterEvaluation)
            .where(
                WriterEvaluation.object_type == object_type,
                WriterEvaluation.object_id == object_id,
                WriterEvaluation.rubric_id == LITERARY_REVISION_RUBRIC_ID,
                WriterEvaluation.parent_evaluation_id.is_(None),
            )
            .order_by(WriterEvaluation.created_at.desc(), WriterEvaluation.evaluation_id.desc())
        ).scalars().first()
        lenses: list[dict[str, Any]] = []
        if latest is not None:
            lens_rows = self.session.execute(
                select(WriterEvaluation)
                .where(WriterEvaluation.parent_evaluation_id == latest.evaluation_id)
                .order_by(WriterEvaluation.lens.asc(), WriterEvaluation.evaluation_id.asc())
            ).scalars().all()
            lenses = [item for item in (self.serialize_evaluation(row) for row in lens_rows) if item]
        patch_rows = self.session.execute(
            select(PassagePatchCandidate)
            .where(PassagePatchCandidate.object_type == object_type, PassagePatchCandidate.object_id == object_id)
            .order_by(PassagePatchCandidate.created_at.desc(), PassagePatchCandidate.patch_id.desc())
        ).scalars().all()
        latest_payload = self.serialize_evaluation(latest)
        return {
            "status": "reviewed" if latest else "not_run",
            "object_type": object_type,
            "object_id": object_id,
            "rubric_id": LITERARY_REVISION_RUBRIC_ID,
            "latest_evaluation": latest_payload,
            "latest_score": latest_payload["overall_score"] if latest_payload else None,
            "requires_human_review": bool(latest_payload["requires_human_review"]) if latest_payload else False,
            "lens_evaluations": lenses,
            "patch_candidates": [self.serialize_patch_candidate(row) for row in patch_rows],
        }

    def _run_passage_patch(
        self,
        payload: dict[str, Any],
        *,
        source_excerpt: str,
        issue_dimension: str,
    ) -> dict[str, Any]:
        target_text_ref = _optional_text(payload, "target_text_ref") or _optional_text(payload, "source_text_ref") or ""
        source_draft = self._source_draft(_optional_text(payload, "source_draft_id"))
        preference = self._approved_runtime_preference_profile(payload)
        snapshot = _passage_patch_snapshot(
            payload=payload,
            source_excerpt=source_excerpt,
            issue_dimension=issue_dimension,
            target_text_ref=target_text_ref,
            source_draft=source_draft,
            preference=preference,
        )
        prompt = self.prompt_builder.build(snapshot, "writer_passage_patch")
        object_type = _required_text(payload, "object_type")
        object_id = _required_text(payload, "object_id")
        user_prompt = _passage_patch_user_prompt(
            prompt["user_prompt"],
            source_excerpt=source_excerpt,
            issue_dimension=issue_dimension,
            target_text_ref=target_text_ref,
            source_draft=source_draft,
            preference=preference,
        )
        # 2026-09-14 WP6.3：局部补丁在参考作者的手笔下改句（k≤3 样例窗口，作者稿作选窗上下文）
        prompt = self._inject_style_reference_prefix(
            prompt,
            object_type=object_type,
            object_id=object_id,
            chapter_id=_optional_text(payload, "chapter_id"),
            scene_id=_optional_text(payload, "scene_id"),
            context_text=(source_draft.content if source_draft is not None else source_excerpt) or None,
            final_user_prompt=user_prompt,
        )
        execution_step_key = f"writer_passage_patch:{object_type}:{object_id}"
        context = self._llm_context(
            object_type=object_type,
            object_id=object_id,
            chapter_id=_optional_text(payload, "chapter_id"),
            scene_id=_optional_text(payload, "scene_id"),
            node_id="writer_passage_patch",
            execution_step_key=execution_step_key,
        )
        node_result = self._llm_runner.run(
            scene_id=context.scene_id,
            chapter_id=context.chapter_id,
            bundle_id=snapshot["source_version_refs"]["target_text_ref"] or "writer_passage_patch",
            bundle_hash=hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
            node_id="writer_passage_patch",
            step="writer_passage_patch",
            prompt=prompt,
            user_prompt=user_prompt,
            execution_step_key=execution_step_key,
            context=context,
        )
        try:
            normalized = _normalize_patch_output(
                node_result.response.structured_output,
                source_excerpt=source_excerpt,
                issue_dimension=issue_dimension,
                target_text_ref=target_text_ref,
            )
        except WriterDeepReviewOutputError as exc:
            raise DomainError(
                "WRITER_PASSAGE_PATCH_OUTPUT_INVALID",
                f"writer passage patch returned an invalid payload: {exc}",
                status_code=502,
                details={
                    "llm_call_id": node_result.llm_call_id,
                    "node_id": "writer_passage_patch",
                    "validation_error": str(exc),
                },
            ) from exc
        generation_llm_call_id = str(node_result.llm_call_id or "").strip()
        if not generation_llm_call_id:
            raise DomainError(
                "WRITER_PASSAGE_PATCH_OUTPUT_INVALID",
                "writer passage patch completed without an auditable LLM call id",
                status_code=502,
                details={"node_id": "writer_passage_patch", "validation_error": "generation_llm_call_id is required"},
            )
        normalized["generation_llm_call_id"] = generation_llm_call_id
        return normalized

    def _inject_style_reference_prefix(
        self,
        prompt: dict[str, Any],
        *,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        context_text: str | None,
        final_user_prompt: str,
    ) -> dict[str, Any]:
        """2026-09-14 保真修补（WP6.3）：深评 / 局部补丁按项目 / 场景的 active 绑定拿到 ``[STYLE_REFERENCE]``。

        场景对象按场景作用域（窗口按场景轮换），章对象按 project + global 作用域；样例窗口封顶
        :data:`PLANNING_FEW_SHOT_K_CAP`（评审与补丁只需少量样例定标准），被评 / 被改的文本作
        选窗上下文，并按最终 user prompt 压进模板预算。无绑定 → 提示词逐字不变；解析 / 注入
        失败 → 回退基础 prompt（可选增强，绝不阻断评审或补丁）。
        """
        try:
            scope = resolve_style_scope(
                self.session,
                scene_id=scene_id or (object_id if object_type == "scene" else None),
                chapter_id=chapter_id or (object_id if object_type == "chapter" else None),
            )
            if scope is None:
                return prompt
            injected = inject_style_reference_prefix(
                self.session,
                prompt,
                scope,
                None,
                task_type="scene_generation",
                context_text=context_text,
                final_user_prompt=final_user_prompt,
                few_shot_k_cap=PLANNING_FEW_SHOT_K_CAP,
            )
            return injected if injected is not None else prompt
        except Exception:  # noqa: BLE001 — 可选增强：注入失败只记日志，不阻断评审 / 补丁
            _LOGGER.warning(
                "writer style reference prefix skipped for %s %s",
                object_type,
                object_id,
                exc_info=True,
            )
            return prompt

    def _source_draft(self, source_draft_id: str | None) -> AuthorDraft | None:
        if not source_draft_id:
            return None
        return self.session.get(AuthorDraft, source_draft_id)

    def _approved_runtime_preference_profile(self, payload: dict[str, Any]) -> AuthorPreferenceProfile | None:
        chapter_id = _optional_text(payload, "chapter_id")
        scene_id = _optional_text(payload, "scene_id")
        scene = self.session.get(SceneCard, scene_id) if scene_id else None
        chapter = self.session.get(ChapterGoal, chapter_id or (scene.chapter_id if scene else ""))
        project_id = (scene.project_id if scene else None) or (chapter.project_id if chapter else None)
        project = self.session.get(StoryProject, project_id) if project_id else None
        scopes: list[tuple[str, str]] = [("global", "global")]
        genre = " ".join(str(project.genre or "").strip().lower().split()) if project else ""
        if genre:
            scopes.append(("genre", genre[:120]))
        if project_id:
            scopes.append(("project", project_id))
        if chapter is not None:
            scopes.append(("chapter", chapter.chapter_id))
        rows: list[AuthorPreferenceProfile] = []
        for scope_type, scope_ref_id in scopes:
            rows.extend(
                self.session.execute(
                    select(AuthorPreferenceProfile)
                    .where(
                        AuthorPreferenceProfile.scope_type == scope_type,
                        AuthorPreferenceProfile.scope_ref_id == scope_ref_id,
                        AuthorPreferenceProfile.status == "approved",
                        AuthorPreferenceProfile.runtime_eligible == 1,
                    )
                    .order_by(AuthorPreferenceProfile.updated_at.asc(), AuthorPreferenceProfile.profile_id.asc())
                ).scalars().all()
            )
        if not rows:
            return None
        summary: dict[str, Any] = {}
        for row in rows:
            summary = merge_preference_summaries(summary, row.summary_json or {})
        # Keep the existing snapshot contract while avoiding mutation of any
        # persisted profile as broader scopes are merged for this target.
        return SimpleNamespace(
            profile_id=rows[-1].profile_id,
            summary_json=safe_preference_summary_for_prompt(summary),
        )

    def _scene_source(self, scene: SceneCard) -> dict[str, Any]:
        author_draft = self._current_author_draft("scene", scene.scene_id)
        if author_draft is not None:
            return {
                "content": author_draft.content or "",
                "source_text_ref": f"author_draft:{author_draft.draft_id}",
                "source_bundle_id": None,
            }
        state = self.session.get(SceneRunState, scene.scene_id)
        final_row = self.session.get(FinalScene, state.current_final_scene_row_id) if state and state.current_final_scene_row_id else None
        if final_row is None:
            final_row = self.session.execute(
                select(FinalScene)
                .where(FinalScene.scene_id == scene.scene_id)
                .order_by(FinalScene.created_at.desc(), FinalScene.row_id.desc())
            ).scalars().first()
        return {
            "content": final_row.content if final_row else "",
            "source_text_ref": f"final_scene:{final_row.row_id}" if final_row else f"scene:{scene.scene_id}",
            "source_bundle_id": final_row.source_bundle_id if final_row else (state.current_bundle_id if state else None),
        }

    def _chapter_source(self, chapter: ChapterGoal) -> dict[str, Any]:
        author_draft = self._current_author_draft("chapter", chapter.chapter_id)
        if author_draft is not None:
            return {
                "content": author_draft.content or "",
                "source_text_ref": f"author_draft:{author_draft.draft_id}",
                "source_bundle_id": None,
            }
        final_memory = self.session.execute(
            select(ChapterMemory)
            .where(ChapterMemory.chapter_id == chapter.chapter_id, ChapterMemory.aggregate_stage == "final")
            .order_by(ChapterMemory.created_at.desc(), ChapterMemory.row_id.desc())
        ).scalars().first()
        if final_memory:
            return {
                "content": final_memory.content,
                "source_text_ref": f"chapter_memory:{final_memory.row_id}",
                "source_bundle_id": None,
            }
        scenes = self.session.execute(
            select(SceneCard).where(SceneCard.chapter_id == chapter.chapter_id, SceneCard.trashed_flag == 0).order_by(SceneCard.scene_seq.asc())
        ).scalars().all()
        parts: list[str] = []
        for scene in scenes:
            source = self._scene_source(scene)
            if source["content"]:
                parts.append(source["content"])
        return {
            "content": "\n\n".join(parts),
            "source_text_ref": f"chapter_assembled:{chapter.chapter_id}",
            "source_bundle_id": None,
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

    def _require_scene(self, scene_id: str) -> SceneCard:
        return require_scene(self.session, scene_id)

    def _require_chapter(self, chapter_id: str) -> ChapterGoal:
        return require_chapter(self.session, chapter_id)

    def _require_patch_candidate(self, patch_id: str) -> PassagePatchCandidate:
        row = self.session.get(PassagePatchCandidate, patch_id)
        if row is None:
            raise DomainError("PASSAGE_PATCH_NOT_FOUND", "passage patch candidate not found", status_code=404)
        return row

    def _latest_preference_profile(self) -> AuthorPreferenceProfile | None:
        return self.session.execute(
            select(AuthorPreferenceProfile)
            .where(AuthorPreferenceProfile.scope_type == "global", AuthorPreferenceProfile.scope_ref_id == "global")
            .order_by(AuthorPreferenceProfile.created_at.desc(), AuthorPreferenceProfile.profile_id.desc())
        ).scalars().first()

    def _refresh_author_preference_profile(self, *, actor_ref: str) -> AuthorPreferenceProfile:
        decided = self.session.execute(
            select(PassagePatchCandidate)
            .where(PassagePatchCandidate.author_decision.in_(("accepted", "rejected")))
            .order_by(PassagePatchCandidate.created_at.asc(), PassagePatchCandidate.patch_id.asc())
        ).scalars().all()
        summary = _preference_summary(decided)
        profile = self._latest_preference_profile()
        if profile is None:
            profile = AuthorPreferenceProfile(
                profile_id="author_pref_global_global",
                scope_type="global",
                scope_ref_id="global",
                status="draft",
                runtime_eligible=0,
                summary_json=summary,
                source_patch_ids_json=[row.patch_id for row in decided],
                created_by=actor_ref or "writer_deep_review",
            )
            self.session.add(profile)
        else:
            profile.status = "draft"
            profile.runtime_eligible = 0
            profile.summary_json = summary
            profile.source_patch_ids_json = [row.patch_id for row in decided]
            profile.created_by = actor_ref or profile.created_by
        self._upsert_author_preference_review(profile, actor_ref=actor_ref)
        return profile

    def _upsert_author_preference_review(self, profile: AuthorPreferenceProfile, *, actor_ref: str) -> ReviewItem:
        review_id = f"review_{profile.profile_id}"
        summary = profile.summary_json or _empty_preference_summary()
        source_patch_ids = profile.source_patch_ids_json or []
        candidate_payload = {
            "profile_id": profile.profile_id,
            "scope_type": profile.scope_type,
            "scope_ref_id": profile.scope_ref_id,
            "summary": summary,
            "source_patch_ids": source_patch_ids,
        }
        review = self.session.get(ReviewItem, review_id)
        if review is None:
            review = ReviewItem(
                review_id=review_id,
                item_type="author_preference_profile",
                status="pending",
                candidate_text=json.dumps(summary, ensure_ascii=False, sort_keys=True),
                candidate_payload_json=candidate_payload,
                active_on_approve=1,
                materialize_status="pending",
            )
            self.session.add(review)
            return review
        review.item_type = "author_preference_profile"
        review.status = "pending"
        review.candidate_text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        review.candidate_payload_json = candidate_payload
        review.active_on_approve = 1
        review.materialize_status = "pending"
        review.approved_item_row_id = None
        review.approved_item_id = None
        return review


def _passage_patch_snapshot(
    *,
    payload: dict[str, Any],
    source_excerpt: str,
    issue_dimension: str,
    target_text_ref: str,
    source_draft: AuthorDraft | None,
    preference: AuthorPreferenceProfile | None,
) -> dict[str, Any]:
    preference_summary = preference.summary_json if preference is not None else {}
    inline_digests = {
        "scene_summary": json.dumps(
            {
                "object_type": payload.get("object_type"),
                "object_id": payload.get("object_id"),
                "target_text_ref": target_text_ref,
                "source_excerpt": source_excerpt,
                "issue_dimension": issue_dimension,
                "source_draft_id": source_draft.draft_id if source_draft is not None else None,
                "source_draft_context": _compact_text(source_draft.content if source_draft is not None else source_excerpt, 1200),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    }
    if preference is not None:
        inline_digests["style_profile"] = json.dumps(
            {
                "profile_id": preference.profile_id,
                "kind": "approved_author_preference_profile",
                "summary": preference_summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    return {
        "contract_version": "WRITER_PASSAGE_PATCH_SOURCE_v1",
        "stage_allowlist_name": "writer_passage_patch",
        "scene_id": _optional_text(payload, "scene_id") or "",
        "chapter_id": _optional_text(payload, "chapter_id") or "",
        "source_version_refs": {
            "target_text_ref": target_text_ref,
            "source_draft_id": source_draft.draft_id if source_draft is not None else None,
            "author_preference_profile_id": preference.profile_id if preference is not None else None,
        },
        "resolved_ref_ids": {},
        "ordered_injections": [
            {"slot": "passage_patch_target", "ref_id": target_text_ref, "digest_key": "scene_summary"},
            {
                "slot": "author_preference_profile",
                "ref_id": preference.profile_id if preference is not None else "",
                "digest_key": "style_profile",
            },
        ],
        "inline_digests": inline_digests,
    }


def _passage_patch_user_prompt(
    base_prompt: str,
    *,
    source_excerpt: str,
    issue_dimension: str,
    target_text_ref: str,
    source_draft: AuthorDraft | None,
    preference: AuthorPreferenceProfile | None,
) -> str:
    preference_summary = preference.summary_json if preference is not None else {}
    return "\n".join(
        [
            base_prompt,
            "",
            "## Passage Patch Target",
            f"Target Text Ref: {target_text_ref}",
            f"Issue Dimension: {issue_dimension}",
            "Source Excerpt:",
            source_excerpt,
            "",
            "## Current Author Draft Context",
            _compact_text(source_draft.content if source_draft is not None else "", 1400),
            "",
            "## Approved Author Preference Profile",
            json.dumps(preference_summary, ensure_ascii=False, sort_keys=True) if preference is not None else "{}",
        ]
    )


def _normalize_patch_output(
    payload: Any,
    *,
    source_excerpt: str,
    issue_dimension: str,
    target_text_ref: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "replacement_options": _replacement_options(source_excerpt, issue_dimension),
            "rationale": "fallback because patch response was not an object",
        }
    patches = payload.get("patches")
    if not isinstance(patches, list) or not patches:
        return {
            "replacement_options": _replacement_options(source_excerpt, issue_dimension),
            "rationale": str(payload.get("rationale") or "fallback because patch list was empty"),
        }
    options: list[dict[str, Any]] = []
    for index, patch in enumerate(patches[:3], start=1):
        if not isinstance(patch, dict):
            continue
        replacement_text = patch.get("replacement_text")
        if not isinstance(replacement_text, str) or not replacement_text.strip():
            continue
        changed_dimensions = patch.get("changed_dimensions") if isinstance(patch.get("changed_dimensions"), list) else []
        dimensions = [str(item) for item in changed_dimensions if isinstance(item, str) and item.strip()]
        tone = str(patch.get("tone") or (dimensions[0] if dimensions else issue_dimension))
        options.append(
            {
                "option_id": f"option_llm_{index}",
                "tone": tone,
                "label": str(patch.get("label") or f"版本 {index}"),
                "replacement_text": replacement_text.strip(),
                "changed_dimensions": dimensions or [issue_dimension],
                "why_it_helps": str(patch.get("why_it_helps") or patch.get("reason") or ""),
                "target_text_ref": str(patch.get("target_text_ref") or target_text_ref),
                "source_excerpt": str(patch.get("source_excerpt") or source_excerpt),
                "patch_type": str(patch.get("patch_type") or "replace_excerpt"),
            }
        )
    if not options:
        # 模型完全没给可用候选 → 确定性兜底(3 个)，沿用既有语义（离线测试覆盖）
        options = _replacement_options(source_excerpt, issue_dimension)
    elif len(options) < 2:
        # Fix B：模型仅回 1 个合法候选时，用确定性变体补足到 ≥2，保留「多选改写」UX 契约。
        # 诚实纪律：补足项 option_id 带 topup 前缀 + is_fallback_topup 标记可区分、不冒充模型产物；
        # 且补足时 rationale 不得整串落入前端 /offline deterministic/i 正则
        # （否则 ws-writer.jsx 会把整次真实改写误判为「模型不可用」而整体丢弃）。
        existing = {opt["replacement_text"].strip() for opt in options}
        for variant in _replacement_options(source_excerpt, issue_dimension):
            if len(options) >= 2:
                break
            text = str(variant.get("replacement_text") or "").strip()
            if not text or text in existing:
                continue
            options.append(
                {
                    "option_id": f"option_topup_{variant['option_id']}",
                    "tone": str(variant.get("tone") or issue_dimension),
                    "label": f"{variant.get('label') or '备选'}（确定性补足）",
                    "replacement_text": text,
                    "changed_dimensions": [*(variant.get("changed_dimensions") or []), "deterministic_topup"],
                    "why_it_helps": str(variant.get("why_it_helps") or ""),
                    "target_text_ref": target_text_ref,
                    "source_excerpt": source_excerpt,
                    "patch_type": "replace_excerpt",
                    "is_fallback_topup": True,
                }
            )
            existing.add(text)

    rationale = str(payload.get("rationale") or "")
    if any(opt.get("is_fallback_topup") for opt in options):
        rationale = (rationale + "（模型仅返回单个候选，已用确定性变体补足候选数；标注「确定性补足」的选项为非模型产物。）").strip()
    return {
        "replacement_options": options,
        "rationale": rationale,
    }


def _replacement_options(source_excerpt: str, issue_dimension: str) -> list[dict[str, Any]]:
    compressed = source_excerpt.strip().rstrip("。！？")
    return [
        {
            "option_id": "option_shorter",
            "tone": "shorter",
            "label": "更短",
            "replacement_text": f"{compressed}。",
            "changed_dimensions": [issue_dimension, "information_rhythm"],
            "why_it_helps": "压掉解释余量，让动作和停顿自己承担压力。",
        },
        {
            "option_id": "option_sharper",
            "tone": "sharper",
            "label": "更狠",
            "replacement_text": f"{compressed}。她没有补充理由，只把证据袋按进掌心。",
            "changed_dimensions": [issue_dimension, "relationship_tension"],
            "why_it_helps": "让角色拒绝解释，把锋利感放进动作后果。",
        },
        {
            "option_id": "option_subtler",
            "tone": "subtler",
            "label": "更含蓄",
            "replacement_text": f"{compressed}。话音落下后，她先看了一眼门缝。",
            "changed_dimensions": [issue_dimension, "dialogue_subtext"],
            "why_it_helps": "把明说转为观察和回避，保留读者自行判断的空间。",
        },
    ]


def _compact_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    head = max(0, limit // 2)
    tail = max(0, limit - head)
    return f"{text[:head]}\n...\n{text[-tail:]}"


def _infer_scene_form(text: str) -> str:
    value = str(text or "")
    if _contains_any(value, ("选择", "决定", "必须", "代价", "公开", "保护", "不能")):
        return "plot_scene"
    if _contains_any(value, ("真相", "证据", "秘密", "录音", "发现", "揭示")):
        return "revelation_scene"
    if _contains_any(value, ("关系", "信任", "背叛", "靠近", "疏远", "沉默", "对视")):
        return "relationship_scene"
    if _contains_any(value, ("离开", "抵达", "回到", "之后", "翌日", "穿过", "转入")):
        return "transition_scene"
    if _contains_any(value, ("雨", "雪", "风", "灯", "雾", "影", "气味", "夜", "门", "窗", "月", "光")):
        return "atmosphere_scene"
    return "plot_scene"

def _candidate_category(payload: dict[str, Any], issue_dimension: str) -> str:
    explicit = _optional_text(payload, "candidate_category")
    if explicit in PATCH_CATEGORIES:
        return explicit
    dimension = str(issue_dimension or "")
    if dimension in {"dialogue_subtext", "dialogue_edge", "relationship_tension"}:
        return "dialogue_rewrite"
    if dimension in {"image_necessity", "repetitive_expression", "template_action_reuse"}:
        return "action_replace"
    if dimension in {"ending_drive", "summary_ending"}:
        return "ending_pressure"
    if dimension in {"information_rhythm", "expository_dialogue", "false_clarity"}:
        return "information_reorder"
    if dimension in {"model_voice", "prose_model_voice", "image_homogeneity", "syntax_monotony"}:
        return "de_model_voice"
    return "local_patch"


def _target_range(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("start", "end"):
        if isinstance(value.get(key), int):
            result[key] = value[key]
    unit = value.get("unit")
    if isinstance(unit, str) and unit.strip():
        result["unit"] = unit.strip()
    return result or None


def _revision_strategy(payload: dict[str, Any], issue_dimension: str) -> str:
    explicit = _optional_text(payload, "revision_strategy")
    if explicit:
        return explicit
    category = _candidate_category(payload, issue_dimension)
    return {
        "dialogue_rewrite": "用反问、截断或沉默替代解释性对白。",
        "action_replace": "用物件移动、身体位置或关系后果替代模板动作。",
        "ending_pressure": "把结尾改成推动下一场的硬动作或视觉钩子。",
        "information_reorder": "让信息通过行动分段释放，避免一次性说明。",
        "de_model_voice": "删掉抽象总结和泛化比喻，保留具体动作压力。",
    }.get(category, f"围绕 {issue_dimension} 做局部深改。")


def _preference_tags(payload: dict[str, Any], issue_dimension: str) -> list[str]:
    raw = payload.get("preference_tags")
    if isinstance(raw, list):
        tags = [str(item).strip() for item in raw if str(item).strip()]
        if tags:
            return _dedupe(tags)[:8]
    category = _candidate_category(payload, issue_dimension)
    defaults = {
        "dialogue_rewrite": ["少解释", "对白更短"],
        "action_replace": ["动作承压", "少模板手势"],
        "ending_pressure": ["结尾硬钩子"],
        "information_reorder": ["信息分段释放"],
        "de_model_voice": ["去模型腔", "少抽象总结"],
    }
    return defaults.get(category, [issue_dimension])[:8]


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token and token in text for token in tokens)


def _scene_form_from_findings(findings: list[dict[str, Any]], object_type: str | None = None) -> str | None:
    if object_type != "scene":
        return None
    for finding in findings:
        scene_form = str(finding.get("scene_form") or "")
        if scene_form in SCENE_FORMS:
            return scene_form
    return "plot_scene"


def _scene_form_note(scene_form: str) -> str:
    labels = {
        "plot_scene": "场景形态判断：情节场。已有选择、阻碍或代价信号，不必再为了钩子额外加压。",
        "atmosphere_scene": "场景形态判断：氛围场。它可以优先建立气息、视角和读者身体感，不必强行制造重大选择。",
        "relationship_scene": "场景形态判断：关系场。核心价值在关系微转，而不是外部事件大小。",
        "revelation_scene": "场景形态判断：认知/揭示场。重点是信息释放的节奏和后果。",
        "transition_scene": "场景形态判断：过渡场。它可以服务位置、时间或状态切换，但仍应有清晰的读者方向。",
    }
    return labels.get(scene_form, labels["plot_scene"])


def _scene_form_evidence(text: str) -> str:
    excerpt = str(text or "").strip()
    return excerpt[:80]


def _diagnose_by_lens(content: str) -> dict[str, dict[str, Any]]:
    findings: dict[str, list[dict[str, Any]]] = {lens: [] for lens in DEEP_REVIEW_LENSES}
    text = content or ""
    scene_form = _infer_scene_form(text)
    if len(text.strip()) < 80:
        findings["story"].append(
            _finding(
                lens="story",
                dimension="choice_pressure",
                classification="blocking",
                issue="正文太短，尚不足以承载深改判断。",
                recommendation="补足人物选择、阻碍和结尾变化后再诊断。",
                evidence=text[:40],
                why="短文本容易让系统误把设定摘要当成完整场景。",
            )
        )
    if not any(token in text for token in ("选择", "决定", "必须", "不能", "公开", "隐藏", "保护")):
        findings["character"].append(
            _finding(
                lens="character",
                dimension="choice_pressure",
                classification="blocking",
                issue="人物没有被逼到必须选择的位置。",
                recommendation="让人物在两个代价之间做出可见动作。",
                evidence=text[:36],
                why="读者需要看到人物承担后果，而不是只接收线索。",
            )
        )
    elif "解释" in text and not any(token in text for token in ("藏", "交给", "删掉", "撕掉", "承认")):
        findings["character"].append(
            _finding(
                lens="character",
                dimension="character_contradiction",
                classification="blocking",
                issue="人物说出了正确理由，但选择还没有落成不可逆动作。",
                recommendation="让人物为保护或公开付出一个立刻可见的代价。",
                evidence=_first_match(text, ("解释", "保护")),
                why="深改阶段不能只让人物站在正确立场上，必须让她失去或冒犯什么。",
            )
        )
    if not any(mark in text for mark in ("“", "\"", "说", "问", "答")) or "解释" in text:
        findings["prose"].append(
            _finding(
                lens="prose",
                dimension="dialogue_subtext",
                classification="revision",
                issue="对白承担了解释功能，潜台词压力不足。",
                recommendation="把解释改成回避、截断、反问或动作。",
                evidence=_first_match(text, ("解释", "说")),
                why="深改台需要让对白产生关系摩擦，而不是复述动机。",
            )
        )
    repeated_terms = _repeated_ai_trace_terms(text)
    if repeated_terms:
        findings["prose"].append(
            _finding(
                lens="prose",
                dimension="repetitive_expression",
                classification="revision",
                issue=f"出现重复手势或同质 AI 氛围词：{'、'.join(repeated_terms)}。",
                recommendation="保留一个核心动作，其余改成关系反应或物理后果。",
                evidence=repeated_terms[0],
                why="重复的漂亮动作会让作者声线变薄，削弱人物独特性。",
            )
        )
    if not text.rstrip().endswith(("？", "?", "。")) or not any(token in text[-80:] for token in ("心跳", "证据", "谁", "不能", "独自", "公开", "隐藏")):
        findings["reader"].append(
            _finding(
                lens="reader",
                dimension="ending_drive",
                classification="taste",
                issue="结尾可以更硬地把读者推向下一场。",
                recommendation="用一个未回答的动作或视觉钩子收束，而不是总结。",
                evidence=text[-40:],
                why="结尾不是装饰，它决定读者是否愿意继续翻页。",
            )
        )
    if not any(token in text for token in ("保护", "真相", "代价", "背叛", "公开", "隐藏")):
        findings["theme"].append(
            _finding(
                lens="theme",
                dimension="theme_pressure",
                classification="revision",
                issue="场景的主题压力还没有落到人物选择上。",
                recommendation="把主题问题压进人物的具体取舍。",
                evidence=text[:40],
                why="深改阶段需要知道这场戏触碰了作品真正关心的问题。",
            )
        )
    else:
        findings["theme"].append(
            _finding(
                lens="theme",
                dimension="theme_pressure",
                classification="taste",
                issue="主题压力已经出现，但还可以更不体面。",
                recommendation="让人物承认自己也从隐瞒中获益，而不只是正确地保护他人。",
                evidence=_first_match(text, ("保护", "真相", "公开", "隐藏")),
                why="人物有不体面的一瞬间，主题才会有重量。",
            )
        )
    if text.strip():
        findings["story"].append(
            _finding(
                lens="story",
                dimension="scene_form",
                classification="ignore_ok",
                issue=_scene_form_note(scene_form),
                recommendation="按这个场景形态检查它是否完成对应功能，不必把每一场都强行加成大钩子或 forced choice。",
                evidence=_scene_form_evidence(text),
                why="场景可以承担氛围、认知、关系微转、信息释放或过渡功能；判断形态能避免把所有文本压成同一种商业场。",
                scene_form=scene_form,
            )
        )
    payloads: dict[str, dict[str, Any]] = {}
    for lens in DEEP_REVIEW_LENSES:
        lens_findings = findings[lens]
        for finding in lens_findings:
            finding.setdefault("scene_form", scene_form)
        payloads[lens] = {
            "findings": lens_findings,
            "scores": _scores_for_findings(lens_findings),
        }
    return payloads


def _normalize_deep_review_output(payload: dict[str, Any]) -> dict[str, Any]:
    findings = _normalize_findings(payload.get("findings"))
    scores = _normalize_scores(payload.get("scores"))
    overall_score = _optional_score(payload.get("overall_score"))
    if overall_score is None:
        overall_score = round(mean(scores.values()), 2) if scores else None
    revision_brief = _normalize_revision_brief(payload.get("revision_brief"), findings)
    normalized_lenses = _normalize_lens_evaluations(payload.get("lens_evaluations"), findings)
    requires_human_review = bool(payload.get("requires_human_review"))
    if any(finding.get("severity") == "blocking" for finding in findings):
        requires_human_review = True
    return {
        "overall_score": overall_score,
        "scores": scores,
        "findings": findings,
        "revision_brief": revision_brief,
        "requires_human_review": requires_human_review,
        "lens_evaluations": normalized_lenses,
    }


def _normalize_lens_evaluations(value: Any, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 模型直出的分组是权威来源（校验后采用）;缺失的镜头再从顶层 findings 的
    # lens 标签重建——不把顶层 findings 并入模型已给出的条目,否则同一条发现
    # 会在两处同时出现时被重复计入。
    findings_by_lens: dict[str, list[dict[str, Any]]] = {lens: [] for lens in DEEP_REVIEW_LENSES}
    for finding in findings:
        findings_by_lens[_coerce_lens(finding.get("lens")) or "story"].append(finding)
    by_lens: dict[str, dict[str, Any]] = {}
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        lens = _coerce_lens(item.get("lens"))
        if lens is None:
            continue
        lens_findings = _normalize_findings(item.get("findings"))
        for finding in lens_findings:
            finding["lens"] = lens
        entry = by_lens.get(lens)
        if entry is None:
            by_lens[lens] = {
                "lens": lens,
                "overall_score": _optional_score(item.get("overall_score")),
                "scores": _normalize_scores(item.get("scores")),
                "findings": lens_findings,
                "revision_brief": _normalize_revision_brief(item.get("revision_brief"), lens_findings),
            }
        else:
            entry["findings"].extend(lens_findings)
            entry["revision_brief"].extend(_revision_brief_from_findings(lens_findings))
    for lens in DEEP_REVIEW_LENSES:
        lens_findings = findings_by_lens[lens]
        if lens in by_lens or not lens_findings:
            continue
        by_lens[lens] = {
            "lens": lens,
            "scores": _scores_for_findings(lens_findings),
            "findings": lens_findings,
            "revision_brief": _revision_brief_from_findings(lens_findings),
        }
    return [by_lens[lens] for lens in DEEP_REVIEW_LENSES if lens in by_lens]


def _coerce_lens(value: Any) -> str | None:
    lens = str(value or "").strip().lower()
    return lens if lens in DEEP_REVIEW_LENSES else None


def _normalize_scores(value: Any) -> dict[str, float]:
    scores = {dimension: 0.78 for dimension in LITERARY_REVISION_DIMENSIONS}
    if isinstance(value, dict):
        for dimension, raw_score in value.items():
            if dimension not in scores:
                continue
            score = _optional_score(raw_score)
            if score is not None:
                scores[dimension] = score
    return scores


def _optional_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, round(score, 2)))


def _normalize_findings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    findings: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        finding = dict(item)
        severity = str(finding.get("severity") or finding.get("classification") or "revision")
        if severity not in {"blocking", "revision", "taste", "ignore_ok"}:
            severity = "revision"
        finding["severity"] = severity
        finding["classification"] = str(finding.get("classification") or severity)
        finding["lens"] = _coerce_lens(finding.get("lens")) or "story"
        finding["dimension"] = str(finding.get("dimension") or "choice_pressure")
        finding["issue"] = str(finding.get("issue") or "")
        finding["recommendation"] = str(finding.get("recommendation") or "")
        finding["evidence_excerpt"] = str(finding.get("evidence_excerpt") or "")
        finding["evidence_location"] = str(finding.get("evidence_location") or "source text")
        finding["why_it_matters"] = str(finding.get("why_it_matters") or "")
        finding["scene_form"] = str(finding.get("scene_form") or "plot_scene")
        findings.append(finding)
    return findings


def _normalize_revision_brief(value: Any, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items = [dict(item) for item in value if isinstance(item, dict)]
        if items:
            return items
    return _revision_brief_from_findings(findings)


def _finding(
    *,
    lens: str,
    dimension: str,
    classification: str,
    issue: str,
    recommendation: str,
    evidence: str,
    why: str,
    scene_form: str | None = None,
) -> dict[str, Any]:
    return {
        "lens": lens,
        "dimension": dimension,
        "severity": classification,
        "classification": classification,
        "issue": issue,
        "recommendation": recommendation,
        "evidence_excerpt": evidence,
        "evidence_location": "source text",
        "why_it_matters": why,
        "scene_form": scene_form or "plot_scene",
    }


def _scores_for_findings(findings: list[dict[str, Any]]) -> dict[str, float]:
    scores = {dimension: 0.78 for dimension in LITERARY_REVISION_DIMENSIONS}
    for finding in findings:
        dimension = finding.get("dimension")
        if dimension not in scores:
            continue
        if finding.get("severity") == "blocking":
            scores[dimension] = min(scores[dimension], 0.42)
        elif finding.get("severity") == "revision":
            scores[dimension] = min(scores[dimension], 0.58)
        elif finding.get("severity") == "taste":
            scores[dimension] = min(scores[dimension], 0.72)
    return scores


def _cap_scores_for_findings(scores: dict[str, float], findings: list[dict[str, Any]]) -> dict[str, float]:
    capped = dict(scores)
    actionable_findings = [finding for finding in findings if finding.get("severity") != "ignore_ok"]
    if actionable_findings:
        for dimension in capped:
            capped[dimension] = min(capped[dimension], 0.85)
    for finding in actionable_findings:
        dimension = finding.get("dimension")
        if dimension in capped and finding.get("severity") == "blocking":
            capped[dimension] = min(capped[dimension], 0.42)
        elif dimension in capped and finding.get("severity") == "revision":
            capped[dimension] = min(capped[dimension], 0.58)
    return capped


def _revision_brief_from_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    brief: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("severity") == "ignore_ok":
            priority = "optional"
        elif finding.get("severity") == "taste":
            priority = "low"
        elif finding.get("severity") == "blocking":
            priority = "high"
        else:
            priority = "medium"
        brief.append(
            {
                "dimension": finding.get("dimension"),
                "classification": finding.get("severity"),
                "action": finding.get("recommendation"),
                "priority": priority,
                "evidence_excerpt": finding.get("evidence_excerpt", ""),
            }
        )
    return brief


def _evidence_spans(content: str, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for finding in findings:
        excerpt = str(finding.get("evidence_excerpt") or "")
        if not excerpt:
            continue
        start = content.find(excerpt)
        if start < 0:
            continue
        spans.append({"text": excerpt, "start": start, "end": start + len(excerpt)})
        if len(spans) >= 8:
            break
    return spans


def _preference_summary(rows: list[PassagePatchCandidate]) -> dict[str, list[str]]:
    preferred: list[str] = []
    rejected: list[str] = []
    ai_traces: list[str] = []
    preferred_categories: list[str] = []
    rejected_categories: list[str] = []
    preference_tags: list[str] = []
    for row in rows:
        category_label = _category_label(row.candidate_category)
        if row.author_decision == "accepted":
            selected = _selected_option(row)
            tone = selected.get("tone") if selected else ""
            preferred_categories.append(category_label)
            preference_tags.extend(str(item) for item in (row.preference_tags_json or []) if str(item).strip())
            if tone == "sharper":
                preferred.append("偏好更锋利的局部改写，让动作代替解释。")
            elif tone == "subtler":
                preferred.append("偏好更含蓄的局部改写，保留读者判断空间。")
            elif tone == "shorter":
                preferred.append("偏好更短的句段，压缩解释余量。")
            else:
                preferred.append(f"偏好{category_label}：{row.revision_strategy or row.issue_dimension}。")
        elif row.author_decision == "rejected":
            rejected_categories.append(category_label)
            # Free-form author notes are audit evidence, not prompt instructions.
            # Convert the decision into a controlled label before publication.
            rejected.append(f"保留作者原句；拒绝自动应用{category_label}。")
        ai_traces.extend(term for term in _repeated_ai_trace_terms(row.source_excerpt) if term not in ai_traces)
    return {
        "preferred_revision_moves": _dedupe(preferred),
        "rejected_revision_moves": _dedupe(rejected),
        "preferred_patch_categories": _dedupe(preferred_categories),
        "rejected_patch_categories": _dedupe(rejected_categories),
        "preference_tags": _dedupe(preference_tags),
        "ai_trace_terms_to_watch": _dedupe(ai_traces),
        "runtime_policy": ["偏好摘要保持 draft；审核批准前不得进入运行 bundle。"],
    }


def _selected_option(row: PassagePatchCandidate) -> dict[str, Any] | None:
    for option in row.replacement_options_json or []:
        if option.get("option_id") == row.selected_option_id:
            return option
    return None


def _empty_preference_summary() -> dict[str, list[str]]:
    return {
        "preferred_revision_moves": [],
        "rejected_revision_moves": [],
        "preferred_patch_categories": [],
        "rejected_patch_categories": [],
        "preference_tags": [],
        "ai_trace_terms_to_watch": [],
        "runtime_policy": ["偏好摘要保持 draft；审核批准前不得进入运行 bundle。"],
    }


def _category_label(value: str | None) -> str:
    return {
        "dialogue_rewrite": "对白改写",
        "action_replace": "动作替换",
        "ending_pressure": "结尾重压",
        "information_reorder": "信息释放重排",
        "de_model_voice": "去模型腔",
        "local_patch": "局部深改",
    }.get(value or "", "局部深改")


def _repeated_ai_trace_terms(text: str) -> list[str]:
    watched = ("手指", "停顿", "幽蓝", "冷光", "低声", "盐霜", "泛着")
    return [term for term in watched if text.count(term) >= 2 or (term in {"幽蓝", "冷光", "盐霜"} and term in text)]


def _first_match(text: str, terms: tuple[str, ...]) -> str:
    for term in terms:
        index = text.find(term)
        if index >= 0:
            return text[max(0, index - 10) : index + len(term) + 16]
    return text[:40]


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("PASSAGE_PATCH_INVALID", f"{key} is required", status_code=400)
    return value.strip()


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
