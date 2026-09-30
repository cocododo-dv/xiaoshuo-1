"""归档 → 待抽取 → 暂存抽取结果：终稿进正史核对的前半程。"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select

from novel_system.db.models import FactCandidate, FinalScene, NarrativeEvent
from novel_system.services.canon.common import COMPLETED_EXTRACTION_OUTCOMES, DEGRADED_EXTRACTION_OUTCOMES
from novel_system.services.errors import DomainError
from novel_system.services.narrative_event_log import NarrativeEventLog


class CanonLifecycleMixin:
    def mark_archive_pending(self, final_scene_row_id: str) -> dict[str, Any]:
        final, scene, project_id, state = self._final_context(final_scene_row_id)
        if (
            state.narrative_sync_status == "synced"
            and state.narrative_sync_final_scene_row_id == final.row_id
        ):
            current = self.scene_status(project_id, scene.scene_id)
            if current["complete"]:
                return current

        # A new unverified revision must not erase the last committed canon.
        # Retire only unfinished review artifacts here; accepted facts and their
        # timeline realization switch atomically at verify/carry completion.
        # Legacy writers may mutate a FinalScene in place; in that exceptional
        # case the prior hash-bound canon cannot safely remain authoritative.
        self._supersede_stale_current_revision(final)
        self._supersede_prior_pending_revision(scene.scene_id, final.row_id)
        state.narrative_sync_status = "pending_extraction"
        state.narrative_sync_final_scene_row_id = final.row_id
        snapshot = self._ensure_scene_snapshot(final, project_id)
        snapshot.status = "pending"
        snapshot.metadata_json = {
            **dict(snapshot.metadata_json or {}),
            "extraction_outcome": "not_invoked",
            "extraction_reason": "awaiting_extraction",
            "requires_empty_confirmation": False,
            "requires_scene_confirmation": True,
        }
        self._rebuild_scene_snapshot(final, project_id)
        self._rebuild_chapter_snapshot(project_id, final.chapter_id)
        self.session.flush()
        return self.scene_status(project_id, scene.scene_id)

    def stage_extraction(
        self,
        final_scene_row_id: str,
        *,
        outcome: str,
        event_ids: Iterable[str],
        reason: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        final, scene, project_id, state = self._final_context(final_scene_row_id)
        self._require_current_final(state, final)
        requested_event_ids = list(dict.fromkeys(str(event_id) for event_id in event_ids))
        current = self.scene_status(project_id, scene.scene_id)
        if current["complete"]:
            extraction = current.get("extraction") or {}
            extracted_candidates = [
                row
                for row in self._candidate_rows(final.row_id)
                if row.source_kind == "prose_extraction"
            ]
            existing_event_ids = {
                row.staged_event_id for row in extracted_candidates if row.staged_event_id
            }
            if (
                set(requested_event_ids) == existing_event_ids
                and outcome == extraction.get("extraction_outcome")
            ):
                return {
                    "final_scene_row_id": final.row_id,
                    "outcome": outcome,
                    "candidate_ids": [row.candidate_id for row in extracted_candidates],
                    "status": state.narrative_sync_status,
                }
            raise DomainError(
                "CANON_SCENE_ALREADY_COMMITTED",
                "这一场的正史已经核对完成，不能再换一份抽取结果重新暂存",
                status_code=409,
                details={"final_scene_row_id": final.row_id},
            )
        self.mark_archive_pending(final.row_id)

        candidate_ids: list[str] = []
        for event_id in requested_event_ids:
            event = self.session.get(NarrativeEvent, event_id)
            if (
                event is None
                or event.project_id != project_id
                or event.chapter_id != final.chapter_id
                or event.scene_id != final.scene_id
            ):
                raise DomainError(
                    "CANON_STAGED_EVENT_MISMATCH",
                    "暂存的事件不属于这一场归档的终稿",
                    status_code=409,
                    details={"event_id": str(event_id), "final_scene_row_id": final.row_id},
                )
            candidate = self._candidate_from_event(final, project_id, event)
            event.final_scene_row_id = final.row_id
            if candidate.status == "pending":
                event.authority_status = "pending"
                event.source_kind = "prose_extraction"
            elif candidate.status == "accepted":
                # A resumed archive checkpoint must preserve the author's prior
                # decision without publishing it before scene verification.
                event.authority_status = "pending"
                event.source_kind = "canon_candidate_accepted"
            elif candidate.status == "rejected":
                event.authority_status = "rejected"
            else:
                raise DomainError(
                    "CANON_CANDIDATE_EVENT_CONFLICT",
                    "已被新版本替换的候选不能重新暂存",
                    status_code=409,
                    details={"candidate_id": candidate.candidate_id},
                )
            candidate_ids.append(candidate.candidate_id)

        snapshot = self._ensure_scene_snapshot(final, project_id)
        snapshot.metadata_json = {
            **dict(snapshot.metadata_json or {}),
            "extraction_outcome": outcome,
            "extraction_reason": reason,
            "extraction_error_code": error_code,
            "requires_empty_confirmation": outcome == "completed_empty",
            # Candidate decisions prove the individual facts only. They cannot
            # prove that extraction found every continuity-changing fact in the
            # scene, so every completed extraction still needs an explicit
            # scene-level completeness confirmation.
            "requires_scene_confirmation": outcome in COMPLETED_EXTRACTION_OUTCOMES,
        }
        if outcome in DEGRADED_EXTRACTION_OUTCOMES:
            state.narrative_sync_status = "degraded"
            snapshot.status = "degraded"
        elif outcome == "completed_events":
            state.narrative_sync_status = "pending_review"
            snapshot.status = "pending"
        elif outcome == "completed_empty":
            state.narrative_sync_status = "pending_review"
            snapshot.status = "pending"
        else:
            state.narrative_sync_status = "pending_extraction"
            snapshot.status = "pending"

        self._rebuild_scene_snapshot(final, project_id)
        self._rebuild_chapter_snapshot(project_id, final.chapter_id)
        self.session.flush()
        return {
            "final_scene_row_id": final.row_id,
            "outcome": outcome,
            "candidate_ids": candidate_ids,
            "status": state.narrative_sync_status,
        }

    def extract_scene_candidates(self, project_id: str, scene_id: str) -> dict[str, Any]:
        """Run an author-requested prose extraction for the current final scene.

        This is the explicit fallback when automatic archive extraction is
        disabled or degraded.  A completed extraction is never re-dispatched;
        callers receive the existing scene review state instead.
        """

        from novel_system.services.llm_accounting import LLMCallContext
        from novel_system.services.llm_task_runner import LLMNodeRunner
        from novel_system.services.prose_event_extractor import (
            extract_scene_events,
            stage_prose_events,
        )
        from novel_system.settings import get_settings

        final, scene, _owned_project_id, _state = self._current_scene_context(
            project_id,
            scene_id,
        )
        current = self.scene_status(project_id, scene_id)
        extraction = current.get("extraction") or {}
        if current["status"] in {"pending_review", "synced"} and extraction.get(
            "extraction_outcome"
        ) in COMPLETED_EXTRACTION_OUTCOMES:
            return {
                "already_extracted": True,
                "product": {
                    "outcome": extraction.get("extraction_outcome"),
                    "reason": extraction.get("extraction_reason"),
                    "error_code": extraction.get("extraction_error_code"),
                },
                "scene": current,
            }
        if not get_settings().llm_enabled:
            raise DomainError(
                "LLM_DISABLED_FOR_CANON_EXTRACTION",
                "还没有接入可用的模型，不能提取正史事实：先到系统设置里配置模型",
                status_code=409,
                details={"scene_id": scene_id, "retryable": False},
            )

        runner = LLMNodeRunner(self.session)
        step = "canon:prose_event_extract"
        context = LLMCallContext(
            scope_type="scene",
            scope_id=scene.scene_id,
            project_id=project_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            node_id="extraction",
            step=step,
            provider_execution_mode=runner.provider_execution_mode,
        )
        # 读完整场：按段落切成几段、每段一次调用（批准 #14，B11-15）
        extraction = extract_scene_events(
            final.content,
            session=self.session,
            llm_runner=runner,
            llm_context=context,
        )
        product = extraction.result
        event_ids = stage_prose_events(
            NarrativeEventLog(self.session),
            {"project_id": project_id, "chapter_id": scene.chapter_id, "scene_id": scene.scene_id},
            product.events,
            final_scene_row_id=final.row_id,
            payload=lambda ordinal: {
                "source": "prose",
                "trigger": "author_requested",
                "extract_ordinal": ordinal,
                "extract_chunk": extraction.event_chunks[ordinal],
                "llm_call_id": extraction.event_call_ids[ordinal],
            },
        )
        staged = self.stage_extraction(
            final.row_id,
            outcome=product.outcome,
            event_ids=event_ids,
            reason=product.reason,
            error_code=product.error_code,
        )
        return {
            "already_extracted": False,
            "product": product.product_snapshot(),
            "chunk_count": extraction.chunk_count,
            "staged": staged,
            "scene": self.scene_status(project_id, scene_id),
        }

    def _candidate_from_event(
        self,
        final: FinalScene,
        project_id: str,
        event: NarrativeEvent,
    ) -> FactCandidate:
        existing = self.session.execute(
            select(FactCandidate).where(FactCandidate.staged_event_id == event.event_id)
        ).scalars().first()
        if existing is not None:
            if existing.final_scene_row_id != final.row_id:
                raise DomainError(
                    "CANON_CANDIDATE_EVENT_CONFLICT",
                    "这条暂存事件已经绑定在另一版终稿上",
                    status_code=409,
                )
            return existing
        resolution = self._resolve_entity(project_id, event.entity_type, event.entity_id)
        evidence = str(event.source_text_excerpt or "").strip()
        evidence_start = final.content.find(evidence) if evidence else -1
        candidate = FactCandidate(
            candidate_id=f"factcand_{self._stable_digest(final.row_id + ':' + event.event_id)[:20]}",
            project_id=project_id,
            chapter_id=final.chapter_id,
            scene_id=final.scene_id,
            final_scene_row_id=final.row_id,
            staged_event_id=event.event_id,
            event_type=event.event_type,
            entity_type=event.entity_type,
            raw_entity_ref=event.entity_id,
            resolved_entity_id=resolution["resolved_entity_id"],
            entity_resolution_status=resolution["status"],
            entity_candidates_json=resolution["candidate_ids"],
            fact_key=event.fact_key,
            fact_value=event.fact_value,
            evidence_text=evidence or None,
            evidence_start=evidence_start if evidence_start >= 0 else None,
            evidence_end=(evidence_start + len(evidence)) if evidence_start >= 0 else None,
            source_kind="prose_extraction",
            confidence=event.confidence,
            criticality="critical",
            status="pending",
        )
        self.session.add(candidate)
        self.session.flush()
        return candidate
