from __future__ import annotations

import hashlib
import unicodedata
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    CanonCommit,
    ChapterGoal,
    ContinuitySnapshot,
    FactCandidate,
    FinalScene,
    LibraryEntity,
    NarrativeEvent,
    OperationLog,
    SceneCard,
    SceneRunState,
    StoryCharacter,
    TimelineEvent,
    utcnow,
)
from novel_system.services.errors import DomainError
from novel_system.services.narrative_event_log import ENTITY_TYPES, EVENT_TYPES, NarrativeEventLog


_COMPLETED_EXTRACTION_OUTCOMES = {"completed_events", "completed_empty"}
_DEGRADED_EXTRACTION_OUTCOMES = {
    "rejected_before_dispatch",
    "provider_failed",
    "parse_failed",
}
_ALIAS_KEYS = {
    "alias",
    "aliases",
    "aka",
    "nickname",
    "nicknames",
    "other_names",
    "former_names",
    "别名",
    "昵称",
    "曾用名",
}
_SCENE_COMPLETION_COMMIT_KINDS = {"author_verification", "facts_unchanged"}


class CanonContinuityService:
    """Turn prose-grounded candidates into accepted, replayable story canon.

    Extraction is deliberately separated from authority: staged NarrativeEvent rows
    remain ``pending`` and runtime replay filters them out. Candidate acceptance binds
    the fact and records an audit commit, but the event enters runtime canon only when
    the whole scene receives a completion commit.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------
    # Archive / extraction lifecycle
    # ------------------------------------------------------------------

    def mark_archive_pending(self, final_scene_row_id: str) -> dict[str, Any]:
        final, scene, project_id, state = self._final_context(final_scene_row_id)
        if (
            state.narrative_sync_status == "synced"
            and state.narrative_sync_final_scene_row_id == final.row_id
            and self.scene_status(project_id, scene.scene_id)["complete"]
        ):
            return self.scene_status(project_id, scene.scene_id)

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
                "a completed scene cannot be restaged with different extraction evidence",
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
                    "staged narrative event does not belong to the archived final scene",
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
                    "a superseded fact candidate cannot be restaged",
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
            "requires_scene_confirmation": outcome in _COMPLETED_EXTRACTION_OUTCOMES,
        }
        if outcome in _DEGRADED_EXTRACTION_OUTCOMES:
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
        from novel_system.services.prose_event_extractor import extract_events_from_prose
        from novel_system.settings import get_settings

        final, scene, _owned_project_id, _state = self._current_scene_context(
            project_id,
            scene_id,
        )
        current = self.scene_status(project_id, scene_id)
        extraction = current.get("extraction") or {}
        if current["status"] in {"pending_review", "synced"} and extraction.get(
            "extraction_outcome"
        ) in _COMPLETED_EXTRACTION_OUTCOMES:
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
                "enable a live model before requesting continuity fact extraction",
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
        product = extract_events_from_prose(
            final.content,
            session=self.session,
            llm_runner=runner,
            llm_context=context,
        )
        log = NarrativeEventLog(self.session)
        event_ids: list[str] = []
        for ordinal, extracted in enumerate(product.events):
            event = log.log_event(
                project_id=project_id,
                chapter_id=scene.chapter_id,
                scene_id=scene.scene_id,
                event_type=extracted.event_type,
                entity_type=(
                    "relation"
                    if extracted.event_type == "relation_change"
                    else "character"
                ),
                entity_id=extracted.entity_id,
                fact_key=extracted.fact_key,
                fact_value=extracted.fact_value,
                confidence="extracted",
                # Never manufacture evidence from an arbitrary prose prefix. A
                # missing quote must remain missing so acceptance fails closed.
                source_text_excerpt=extracted.evidence or None,
                payload={
                    "source": "prose",
                    "trigger": "author_requested",
                    "extract_ordinal": ordinal,
                    "llm_call_id": product.llm_call_id,
                },
                authority_status="pending",
                source_kind="prose_extraction",
                final_scene_row_id=final.row_id,
            )
            event_ids.append(event.event_id)
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
            "staged": staged,
            "scene": self.scene_status(project_id, scene_id),
        }

    def create_manual_candidate(
        self,
        project_id: str,
        scene_id: str,
        *,
        event_type: str,
        raw_entity_ref: str,
        fact_key: str,
        fact_value: str,
        evidence_text: str,
        entity_type: str | None = None,
        planned_timeline_event_id: str | None = None,
    ) -> dict[str, Any]:
        final, scene, owned_project_id, state = self._current_scene_context(project_id, scene_id)
        if owned_project_id != project_id:
            raise self._scene_not_found()
        if self.scene_status(project_id, scene_id)["complete"]:
            raise DomainError(
                "CANON_SCENE_ALREADY_COMMITTED",
                "a completed scene is immutable; create or reopen a new final revision first",
                status_code=409,
                details={"final_scene_row_id": final.row_id},
            )
        self._supersede_stale_current_revision(final)
        if event_type not in EVENT_TYPES:
            raise DomainError("CANON_EVENT_TYPE_INVALID", "unsupported narrative event type", status_code=400)
        clean_entity = str(raw_entity_ref or "").strip()
        clean_key = str(fact_key or "").strip()
        clean_value = str(fact_value or "").strip()
        clean_evidence = str(evidence_text or "").strip()
        if not all((clean_entity, clean_key, clean_value, clean_evidence)):
            raise DomainError(
                "CANON_MANUAL_FACT_INCOMPLETE",
                "entity, fact key, fact value, and evidence are required",
                status_code=400,
            )
        evidence_start = final.content.find(clean_evidence)
        if evidence_start < 0:
            raise DomainError(
                "CANON_EVIDENCE_NOT_IN_FINAL",
                "manual fact evidence must be an exact excerpt from the current final scene",
                status_code=409,
            )
        resolved_type = entity_type or self._entity_type_for_event(event_type)
        if resolved_type not in ENTITY_TYPES:
            raise DomainError("CANON_ENTITY_TYPE_INVALID", "unsupported entity type", status_code=400)
        if planned_timeline_event_id:
            timeline = self.session.get(TimelineEvent, planned_timeline_event_id)
            if timeline is None or timeline.project_id != project_id:
                raise DomainError("CANON_TIMELINE_EVENT_NOT_FOUND", "timeline event not found", status_code=404)

        resolution = self._resolve_entity(project_id, resolved_type, clean_entity)
        candidate = FactCandidate(
            candidate_id=f"factcand_{uuid.uuid4().hex[:20]}",
            project_id=project_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            final_scene_row_id=final.row_id,
            event_type=event_type,
            entity_type=resolved_type,
            raw_entity_ref=clean_entity,
            resolved_entity_id=resolution["resolved_entity_id"],
            entity_resolution_status=resolution["status"],
            entity_candidates_json=resolution["candidate_ids"],
            fact_key=clean_key[:120],
            fact_value=clean_value[:2000],
            evidence_text=clean_evidence[:2000],
            evidence_start=evidence_start,
            evidence_end=evidence_start + len(clean_evidence),
            source_kind="manual",
            confidence="author_candidate",
            criticality="critical",
            planned_timeline_event_id=planned_timeline_event_id,
            status="pending",
        )
        self.session.add(candidate)
        state.narrative_sync_status = "pending_review"
        state.narrative_sync_final_scene_row_id = final.row_id
        snapshot = self._ensure_scene_snapshot(final, project_id)
        snapshot.metadata_json = {
            **dict(snapshot.metadata_json or {}),
            "manual_candidates_added": True,
        }
        self.session.flush()
        self._rebuild_scene_snapshot(final, project_id)
        self._rebuild_chapter_snapshot(project_id, scene.chapter_id)
        return self._serialize_candidate(candidate)

    # ------------------------------------------------------------------
    # Decisions / commits
    # ------------------------------------------------------------------

    def decide_candidate(
        self,
        project_id: str,
        candidate_id: str,
        *,
        action: str,
        actor_ref: str,
        selected_entity_id: str | None = None,
        note: str | None = None,
        expected_final_scene_row_id: str | None = None,
    ) -> dict[str, Any]:
        if action not in {"accept", "reject"}:
            raise DomainError("CANON_DECISION_INVALID", "action must be accept or reject", status_code=400)
        candidate = self.session.get(FactCandidate, candidate_id)
        if candidate is None or candidate.project_id != project_id:
            raise DomainError("CANON_CANDIDATE_NOT_FOUND", "fact candidate not found", status_code=404)
        final, _scene, owned_project_id, state = self._final_context(candidate.final_scene_row_id)
        if owned_project_id != project_id:
            raise DomainError("CANON_CANDIDATE_NOT_FOUND", "fact candidate not found", status_code=404)
        self._require_current_final(state, final)
        if expected_final_scene_row_id and expected_final_scene_row_id != final.row_id:
            raise DomainError(
                "CANON_FINAL_SCENE_CONFLICT",
                "the final scene changed while the fact candidate was being reviewed",
                status_code=409,
                details={
                    "expected_final_scene_row_id": expected_final_scene_row_id,
                    "current_final_scene_row_id": final.row_id,
                },
            )

        terminal = "accepted" if action == "accept" else "rejected"
        if candidate.status == terminal:
            return {
                "candidate": self._serialize_candidate(candidate),
                "commit_id": candidate.canon_commit_id,
                "scene": self.scene_status(project_id, candidate.scene_id),
            }
        if candidate.status != "pending":
            raise DomainError(
                "CANON_CANDIDATE_ALREADY_DECIDED",
                "fact candidate already has a different terminal decision",
                status_code=409,
            )

        decision_at = utcnow()
        commit_id: str | None = None
        event = self.session.get(NarrativeEvent, candidate.staged_event_id) if candidate.staged_event_id else None
        if action == "accept":
            evidence_start = candidate.evidence_start
            evidence_end = candidate.evidence_end
            evidence_text = str(candidate.evidence_text or "")
            if (
                evidence_start is None
                or evidence_end is None
                or evidence_start < 0
                or evidence_end <= evidence_start
                or final.content[evidence_start:evidence_end] != evidence_text
            ):
                raise DomainError(
                    "CANON_EVIDENCE_NOT_IN_FINAL",
                    "a fact candidate cannot enter canon without an exact excerpt from the current final scene",
                    status_code=409,
                    details={
                        "candidate_id": candidate.candidate_id,
                        "final_scene_row_id": final.row_id,
                    },
                )
            resolved_entity_id = self._resolved_entity_for_accept(
                candidate,
                selected_entity_id=selected_entity_id,
            )
            if event is None:
                event = NarrativeEventLog(self.session).log_event(
                    project_id=project_id,
                    chapter_id=candidate.chapter_id,
                    scene_id=candidate.scene_id,
                    event_type=candidate.event_type,
                    entity_type=candidate.entity_type,
                    entity_id=resolved_entity_id,
                    fact_key=candidate.fact_key,
                    fact_value=candidate.fact_value,
                    confidence="extracted",
                    source_text_excerpt=candidate.evidence_text,
                    payload={"source": candidate.source_kind},
                    authority_status="pending",
                    source_kind=candidate.source_kind,
                    final_scene_row_id=final.row_id,
                )
                candidate.staged_event_id = event.event_id
            commit_id = f"canon_commit_{candidate.candidate_id}"
            commit = self.session.get(CanonCommit, commit_id)
            if commit is None:
                commit = CanonCommit(
                    commit_id=commit_id,
                    project_id=project_id,
                    chapter_id=candidate.chapter_id,
                    scene_id=candidate.scene_id,
                    final_scene_row_id=final.row_id,
                    final_content_hash=self._final_hash(final),
                    commit_kind="candidate_acceptance",
                    candidate_ids_json=[candidate.candidate_id],
                    actor_ref=actor_ref or "operator",
                    decision_note=str(note or "").strip() or None,
                )
                self.session.add(commit)
            event.entity_id = resolved_entity_id
            event.entity_type = candidate.entity_type
            # A candidate decision proves this individual fact, but it cannot prove
            # that the scene review is complete. Keep it outside runtime replay until
            # verify_scene_complete atomically replaces the prior scene revision.
            event.authority_status = "pending"
            event.source_kind = "canon_candidate_accepted"
            event.final_scene_row_id = final.row_id
            event.canon_commit_id = commit_id
            event.confidence = "high"
            event.payload_json = {
                **dict(event.payload_json or {}),
                "source": candidate.source_kind,
                "raw_entity_ref": candidate.raw_entity_ref,
                "entity_resolution_status": candidate.entity_resolution_status,
                "fact_candidate_id": candidate.candidate_id,
                "accepted_by": actor_ref or "operator",
            }
            candidate.resolved_entity_id = resolved_entity_id
            candidate.entity_resolution_status = (
                "manual" if selected_entity_id else candidate.entity_resolution_status
            )
            candidate.status = "accepted"
            candidate.canon_commit_id = commit_id
        else:
            candidate.status = "rejected"
            if event is not None:
                event.authority_status = "rejected"

        candidate.decided_by = actor_ref or "operator"
        candidate.decided_at = decision_at
        candidate.decision_note = str(note or "").strip() or None
        self.session.add(
            OperationLog(
                event_type=f"canon_candidate_{terminal}",
                object_type="fact_candidate",
                object_ref=candidate.candidate_id,
                payload_json={
                    "project_id": project_id,
                    "chapter_id": candidate.chapter_id,
                    "scene_id": candidate.scene_id,
                    "final_scene_row_id": final.row_id,
                    "canon_commit_id": commit_id,
                    "actor_ref": actor_ref or "operator",
                },
            )
        )
        self.session.flush()
        self._rebuild_scene_snapshot(final, project_id)
        self._rebuild_chapter_snapshot(project_id, final.chapter_id)
        self.session.flush()
        return {
            "candidate": self._serialize_candidate(candidate),
            "commit_id": commit_id,
            "completion_commit_id": None,
            "scene": self.scene_status(project_id, candidate.scene_id),
        }

    def verify_scene_complete(
        self,
        project_id: str,
        scene_id: str,
        *,
        actor_ref: str,
        note: str,
        expected_final_scene_row_id: str | None = None,
    ) -> dict[str, Any]:
        final, scene, owned_project_id, state = self._current_scene_context(project_id, scene_id)
        if owned_project_id != project_id:
            raise self._scene_not_found()
        if expected_final_scene_row_id and expected_final_scene_row_id != final.row_id:
            raise DomainError(
                "CANON_FINAL_SCENE_CONFLICT",
                "the final scene changed before continuity verification",
                status_code=409,
            )
        self._supersede_stale_current_revision(final)
        pending_count = self._pending_candidate_count(final.row_id)
        if pending_count:
            raise DomainError(
                "CANON_CANDIDATES_PENDING",
                "all fact candidates must be accepted or rejected before verification",
                status_code=409,
                details={"pending_count": pending_count},
            )
        clean_note = str(note or "").strip()
        if not clean_note:
            raise DomainError(
                "CANON_VERIFICATION_NOTE_REQUIRED",
                "scene verification requires an author audit note",
                status_code=400,
            )
        # Verification of a replacement revision atomically retires the prior
        # revision and realizes only the accepted current-final timeline facts.
        self._supersede_prior_revision(scene.scene_id, final.row_id)
        self._activate_accepted_events(final)
        self._realize_accepted_timelines(final)
        commit_base = f"canon_verify_{self._stable_digest(final.row_id + ':' + self._final_hash(final))[:20]}"
        commit_id = commit_base
        commit = self.session.get(CanonCommit, commit_id)
        if commit is not None and commit.status != "active":
            commit_id = f"{commit_base}_{uuid.uuid4().hex[:8]}"
            commit = None
        if commit is None:
            decisions = self._candidate_rows(final.row_id)
            commit = CanonCommit(
                commit_id=commit_id,
                project_id=project_id,
                chapter_id=scene.chapter_id,
                scene_id=scene.scene_id,
                final_scene_row_id=final.row_id,
                final_content_hash=self._final_hash(final),
                commit_kind="author_verification",
                candidate_ids_json=[row.candidate_id for row in decisions],
                actor_ref=actor_ref or "operator",
                decision_note=clean_note,
            )
            self.session.add(commit)
        snapshot = self._ensure_scene_snapshot(final, project_id)
        snapshot.metadata_json = {
            **dict(snapshot.metadata_json or {}),
            "author_verified": True,
            "author_verification_note": clean_note,
            "requires_empty_confirmation": False,
            "requires_scene_confirmation": False,
        }
        state.narrative_sync_status = "synced"
        state.narrative_sync_final_scene_row_id = final.row_id
        self.session.add(
            OperationLog(
                event_type="canon_scene_verified",
                object_type="scene",
                object_ref=scene.scene_id,
                payload_json={
                    "project_id": project_id,
                    "chapter_id": scene.chapter_id,
                    "final_scene_row_id": final.row_id,
                    "canon_commit_id": commit_id,
                    "actor_ref": actor_ref or "operator",
                },
            )
        )
        self.session.flush()
        self._rebuild_scene_snapshot(final, project_id)
        self._rebuild_chapter_snapshot(project_id, scene.chapter_id)
        self.session.flush()
        return {**self.scene_status(project_id, scene_id), "commit_id": commit_id}

    def carry_forward_facts_unchanged(
        self,
        final_scene_row_id: str,
        *,
        source_final_scene_row_id: str | None,
        actor_ref: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Clone accepted facts when the author explicitly declares facts unchanged."""

        final, scene, project_id, state = self._final_context(final_scene_row_id)
        if not source_final_scene_row_id or source_final_scene_row_id == final.row_id:
            raise DomainError(
                "CANON_CARRY_SOURCE_REQUIRED",
                "facts_unchanged requires a distinct previously committed final scene",
                status_code=409,
                details={
                    "final_scene_row_id": final.row_id,
                    "source_final_scene_row_id": source_final_scene_row_id,
                },
            )
        source_final = self.session.get(FinalScene, source_final_scene_row_id)
        source_snapshot = self.session.get(
            ContinuitySnapshot,
            f"continuity_scene_{source_final_scene_row_id}",
        )
        source_commit_valid = self._has_valid_completion_commit(
            source_final,
            source_snapshot,
            project_id=project_id,
            scene_id=scene.scene_id,
        )
        if (
            source_final is None
            or source_final.scene_id != scene.scene_id
            or source_final.chapter_id != scene.chapter_id
            or source_snapshot is None
            or source_snapshot.status != "complete"
            or not source_commit_valid
        ):
            raise DomainError(
                "CANON_CARRY_SOURCE_NOT_COMMITTED",
                "facts cannot be carried from a final scene without a complete, hash-matched canon commit",
                status_code=409,
                details={
                    "scene_id": scene.scene_id,
                    "source_final_scene_row_id": source_final_scene_row_id,
                },
            )
        source_events = list(
            self.session.execute(
                select(NarrativeEvent).where(
                    NarrativeEvent.project_id == project_id,
                    NarrativeEvent.scene_id == scene.scene_id,
                    NarrativeEvent.final_scene_row_id == source_final_scene_row_id,
                    NarrativeEvent.authority_status == "accepted",
                )
            ).scalars().all()
        )
        source_timeline_event_ids = list(
            dict.fromkeys(
                event_id
                for event_id in self.session.execute(
                    select(FactCandidate.planned_timeline_event_id).where(
                        FactCandidate.project_id == project_id,
                        FactCandidate.scene_id == scene.scene_id,
                        FactCandidate.final_scene_row_id == source_final_scene_row_id,
                        FactCandidate.status == "accepted",
                        FactCandidate.planned_timeline_event_id.is_not(None),
                    )
                ).scalars().all()
                if event_id
            )
        )
        final.content_hash = self._final_hash(final)
        self._supersede_prior_revision(scene.scene_id, final.row_id)
        commit_base = f"canon_carry_{self._stable_digest(final.row_id + ':' + self._final_hash(final))[:20]}"
        commit_id = commit_base
        commit = self.session.get(CanonCommit, commit_id)
        if commit is not None and commit.status != "active":
            commit_id = f"{commit_base}_{uuid.uuid4().hex[:8]}"
            commit = None
        if commit is None:
            commit = CanonCommit(
                commit_id=commit_id,
                project_id=project_id,
                chapter_id=scene.chapter_id,
                scene_id=scene.scene_id,
                final_scene_row_id=final.row_id,
                final_content_hash=self._final_hash(final),
                commit_kind="facts_unchanged",
                candidate_ids_json=[],
                source_final_scene_row_id=source_final_scene_row_id,
                actor_ref=actor_ref or "operator",
                decision_note=str(note or "").strip() or None,
            )
            self.session.add(commit)
        for source in source_events:
            clone_id = f"nevt_{self._stable_digest(final.row_id + ':' + source.event_id)[:16]}"
            clone = self.session.get(NarrativeEvent, clone_id)
            if clone is None:
                clone = NarrativeEvent(
                    event_id=clone_id,
                    project_id=source.project_id,
                    scene_id=source.scene_id,
                    chapter_id=source.chapter_id,
                    scene_seq=source.scene_seq,
                    event_type=source.event_type,
                    entity_type=source.entity_type,
                    entity_id=source.entity_id,
                    fact_key=source.fact_key,
                    fact_value=source.fact_value,
                    confidence=source.confidence,
                    causal_predecessor_id=source.event_id,
                    theme_tags=list(source.theme_tags or []),
                    obligation_ids=list(source.obligation_ids or []),
                    source_text_excerpt=source.source_text_excerpt,
                    authority_status="accepted",
                    source_kind="facts_unchanged",
                    final_scene_row_id=final.row_id,
                    canon_commit_id=commit_id,
                    payload_json={
                        **dict(source.payload_json or {}),
                        "carried_forward_from_event_id": source.event_id,
                        "carried_forward_from_final_scene_row_id": source_final_scene_row_id,
                    },
                )
                self.session.add(clone)
        for timeline_event_id in source_timeline_event_ids:
            self._realize_timeline_event(
                project_id=project_id,
                scene_id=scene.scene_id,
                timeline_event_id=timeline_event_id,
                commit_id=commit_id,
            )
        snapshot = self._ensure_scene_snapshot(final, project_id)
        snapshot.metadata_json = {
            **dict(snapshot.metadata_json or {}),
            "extraction_outcome": "facts_unchanged",
            "author_verified": True,
            "requires_empty_confirmation": False,
            "requires_scene_confirmation": False,
        }
        state.narrative_sync_status = "synced"
        state.narrative_sync_final_scene_row_id = final.row_id
        self.session.flush()
        self._rebuild_scene_snapshot(final, project_id)
        self._rebuild_chapter_snapshot(project_id, scene.chapter_id)
        return {**self.scene_status(project_id, scene.scene_id), "commit_id": commit_id}

    # ------------------------------------------------------------------
    # Read models / gates
    # ------------------------------------------------------------------

    def format_recent_checkpoint_for_prompt(
        self,
        project_id: str,
        before_scene_id: str,
        *,
        pov_character_id: str | None = None,
        recent_scene_limit: int = 4,
        max_deltas: int = 24,
    ) -> str:
        """Format recent committed deltas without exposing pending candidates.

        The full event replay remains the current-state authority.  This compact
        checkpoint adds the *recent transition path* (what just changed and in
        which committed scene), which helps the model bridge chapter boundaries.
        POV scenes receive only knowledge deltas owned by the POV character.
        """

        from novel_system.services.narrative_position import NarrativePositionService

        positioned = NarrativePositionService(self.session).scenes_before(
            project_id,
            before_scene_id,
        )
        snapshots: list[ContinuitySnapshot] = []
        for scene in reversed(positioned):
            state = self.session.get(SceneRunState, scene.scene_id)
            if state is None or not state.current_final_scene_row_id:
                continue
            snapshot = self.session.get(
                ContinuitySnapshot,
                f"continuity_scene_{state.current_final_scene_row_id}",
            )
            if (
                snapshot is None
                or snapshot.project_id != project_id
                or snapshot.status != "complete"
                or snapshot.final_scene_row_id != state.current_final_scene_row_id
            ):
                continue
            final = self.session.get(FinalScene, state.current_final_scene_row_id)
            if not self._has_valid_completion_commit(
                final,
                snapshot,
                project_id=project_id,
                scene_id=scene.scene_id,
            ):
                continue
            snapshots.append(snapshot)
            if len(snapshots) >= max(1, recent_scene_limit):
                break
        snapshots.reverse()

        rows: list[tuple[ContinuitySnapshot, dict[str, Any]]] = []
        seen_event_ids: set[str] = set()
        for snapshot in snapshots:
            groups = [
                snapshot.state_deltas_json or [],
                snapshot.relationship_deltas_json or [],
                snapshot.item_deltas_json or [],
                snapshot.timeline_deltas_json or [],
            ]
            knowledge = list(snapshot.knowledge_deltas_json or [])
            if pov_character_id:
                knowledge = [
                    delta
                    for delta in knowledge
                    if delta.get("entity_id") == pov_character_id
                ]
            groups.append(knowledge)
            for delta in (item for group in groups for item in group):
                event_id = str(delta.get("event_id") or "")
                if event_id and event_id in seen_event_ids:
                    continue
                if event_id:
                    seen_event_ids.add(event_id)
                rows.append((snapshot, delta))

        if not rows:
            return ""
        rows = rows[-max(1, max_deltas):]
        lines = [
            "## Recent Committed Continuity Changes (canon only; do NOT contradict)",
        ]
        for snapshot, delta in rows:
            lines.append(
                "- "
                f"[{snapshot.chapter_id}/{snapshot.scene_id}] "
                f"{delta.get('entity_id')}.{delta.get('fact_key')} = "
                f"{delta.get('fact_value')} ({delta.get('event_type')})"
            )
        obligations = list(
            dict.fromkeys(
                obligation
                for snapshot in snapshots
                for obligation in (snapshot.open_obligations_json or [])
            )
        )
        if obligations:
            lines.append("- Open obligations: " + ", ".join(obligations[:20]))
        return "\n".join(lines)

    def scene_status(self, project_id: str, scene_id: str) -> dict[str, Any]:
        final, scene, owned_project_id, state = self._current_scene_context(
            project_id,
            scene_id,
            require_final=False,
        )
        if owned_project_id != project_id:
            raise self._scene_not_found()
        candidates = self._candidate_rows(final.row_id) if final is not None else []
        snapshot = (
            self.session.get(ContinuitySnapshot, f"continuity_scene_{final.row_id}")
            if final is not None
            else None
        )
        status = "missing_final"
        if final is not None:
            status = state.narrative_sync_status if state is not None else "pending_extraction"
            if (
                status == "synced"
                and state is not None
                and state.narrative_sync_final_scene_row_id != final.row_id
            ):
                status = "pending_extraction"
            has_valid_commit = self._has_valid_completion_commit(
                final,
                snapshot,
                project_id=project_id,
                scene_id=scene.scene_id,
            )
            if (
                status == "synced"
                and (
                    snapshot is None
                    or snapshot.status != "complete"
                    or snapshot.final_scene_row_id != final.row_id
                    or not has_valid_commit
                )
            ):
                status = "pending_review"
        return {
            "project_id": project_id,
            "chapter_id": scene.chapter_id,
            "scene_id": scene.scene_id,
            "scene_seq": scene.scene_seq,
            "final_scene_row_id": final.row_id if final is not None else None,
            "status": status,
            "complete": status == "synced",
            "pending_count": sum(row.status == "pending" for row in candidates),
            "accepted_count": sum(row.status == "accepted" for row in candidates),
            "rejected_count": sum(row.status == "rejected" for row in candidates),
            "candidates": [self._serialize_candidate(row) for row in candidates],
            "snapshot": self._serialize_snapshot(snapshot),
            "extraction": dict(snapshot.metadata_json or {}) if snapshot is not None else {},
        }

    def chapter_status(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None or chapter.project_id != project_id or chapter.trashed_flag:
            raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)
        scenes = list(
            self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq, SceneCard.scene_id)
            ).scalars().all()
        )
        items = [self.scene_status(project_id, scene.scene_id) for scene in scenes]
        missing = [item["scene_id"] for item in items if item["status"] == "missing_final"]
        pending = [
            item["scene_id"]
            for item in items
            if item["status"] not in {"missing_final", "synced"}
        ]
        return {
            "project_id": project_id,
            "chapter_id": chapter_id,
            "complete": bool(items) and not missing and not pending,
            "scene_count": len(items),
            "synced_scene_count": sum(item["status"] == "synced" for item in items),
            "pending_scene_ids": pending,
            "missing_final_scene_ids": missing,
            "pending_candidate_count": sum(item["pending_count"] for item in items),
            "scenes": items,
            "snapshot": self._serialize_snapshot(
                self.session.get(ContinuitySnapshot, f"continuity_chapter_{chapter_id}")
            ),
        }

    def require_chapter_complete(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        status = self.chapter_status(project_id, chapter_id)
        if not status["complete"]:
            raise DomainError(
                "CHAPTER_CANON_NOT_COMMITTED",
                "every final scene must complete continuity review before chapter publication",
                status_code=409,
                details=status,
            )
        return status

    # ------------------------------------------------------------------
    # Internal projection helpers
    # ------------------------------------------------------------------

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
                    "staged event is already bound to another final scene",
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

    def _resolve_entity(self, project_id: str, entity_type: str, raw_ref: str) -> dict[str, Any]:
        normalized = self._normalized_name(raw_ref)
        exact_ids: list[str] = []
        alias_ids: list[str] = []
        if entity_type in {"character", "relation"}:
            rows = list(
                self.session.execute(
                    select(StoryCharacter).where(StoryCharacter.project_id == project_id)
                ).scalars().all()
            )
            for row in rows:
                if normalized in {
                    self._normalized_name(row.character_id),
                    self._normalized_name(row.display_name),
                    self._normalized_name(f"character:{row.character_id}"),
                }:
                    exact_ids.append(row.character_id)
                elif normalized in self._character_aliases(row):
                    alias_ids.append(row.character_id)
        else:
            rows = list(
                self.session.execute(
                    select(LibraryEntity).where(LibraryEntity.project_id == project_id)
                ).scalars().all()
            )
            for row in rows:
                if entity_type in {"location", "item"} and row.kind != entity_type:
                    continue
                if normalized in {
                    self._normalized_name(row.entity_id),
                    self._normalized_name(row.name),
                    self._normalized_name(f"entity:{row.entity_id}"),
                }:
                    exact_ids.append(row.entity_id)
                elif normalized in {
                    self._normalized_name(value) for value in (row.aliases_json or [])
                }:
                    alias_ids.append(row.entity_id)

        candidate_ids = list(dict.fromkeys(exact_ids or alias_ids))
        if len(candidate_ids) == 1:
            return {
                "status": "exact" if exact_ids else "alias",
                "resolved_entity_id": candidate_ids[0],
                "candidate_ids": candidate_ids,
            }
        if len(candidate_ids) > 1:
            return {
                "status": "ambiguous",
                "resolved_entity_id": None,
                "candidate_ids": candidate_ids,
            }
        return {"status": "unresolved", "resolved_entity_id": None, "candidate_ids": []}

    def _resolved_entity_for_accept(
        self,
        candidate: FactCandidate,
        *,
        selected_entity_id: str | None,
    ) -> str:
        if selected_entity_id:
            if not self._entity_belongs_to_project(
                candidate.project_id,
                candidate.entity_type,
                selected_entity_id,
            ):
                raise DomainError(
                    "CANON_ENTITY_SELECTION_INVALID",
                    "selected entity does not belong to this project",
                    status_code=409,
                )
            if (
                candidate.entity_candidates_json
                and selected_entity_id not in candidate.entity_candidates_json
            ):
                raise DomainError(
                    "CANON_ENTITY_SELECTION_INVALID",
                    "selected entity is not one of the candidate matches",
                    status_code=409,
                )
            return selected_entity_id
        if (
            candidate.resolved_entity_id
            and candidate.entity_resolution_status in {"exact", "alias", "manual"}
        ):
            return candidate.resolved_entity_id
        raise DomainError(
            "CANON_ENTITY_RESOLUTION_REQUIRED",
            "an ambiguous or unresolved fact candidate needs an explicit entity selection",
            status_code=409,
            details={"entity_candidates": list(candidate.entity_candidates_json or [])},
        )

    def _rebuild_scene_snapshot(self, final: FinalScene, project_id: str) -> ContinuitySnapshot:
        snapshot = self._ensure_scene_snapshot(final, project_id)
        events = list(
            self.session.execute(
                select(NarrativeEvent)
                .where(
                    NarrativeEvent.project_id == project_id,
                    NarrativeEvent.scene_id == final.scene_id,
                    NarrativeEvent.final_scene_row_id == final.row_id,
                    NarrativeEvent.authority_status == "accepted",
                )
                .order_by(NarrativeEvent.created_at, NarrativeEvent.event_id)
            ).scalars().all()
        )
        commits = list(
            self.session.execute(
                select(CanonCommit)
                .where(
                    CanonCommit.project_id == project_id,
                    CanonCommit.scene_id == final.scene_id,
                    CanonCommit.final_scene_row_id == final.row_id,
                    CanonCommit.status == "active",
                )
                .order_by(CanonCommit.created_at, CanonCommit.commit_id)
            ).scalars().all()
        )
        deltas = [self._event_delta(event) for event in events]
        snapshot.state_deltas_json = [
            item for item in deltas if item["event_type"] in {"character_state", "location_change"}
        ]
        snapshot.knowledge_deltas_json = [
            item for item in deltas if item["event_type"] == "character_learns"
        ]
        snapshot.relationship_deltas_json = [
            item for item in deltas if item["event_type"] == "relation_change"
        ]
        snapshot.item_deltas_json = [
            item for item in deltas if item["event_type"] == "item_change"
        ]
        snapshot.timeline_deltas_json = [
            item
            for item in deltas
            if item["event_type"] in {"location_change", "foreshadow_plant", "foreshadow_resolve"}
        ]
        snapshot.entity_ids_json = list(dict.fromkeys(event.entity_id for event in events))
        snapshot.open_obligations_json = list(
            dict.fromkeys(
                obligation
                for event in events
                for obligation in (event.obligation_ids or [])
            )
        )
        snapshot.source_commit_ids_json = [commit.commit_id for commit in commits]
        snapshot.latest_commit_id = commits[-1].commit_id if commits else None
        snapshot.summary_text = "\n".join(self._delta_summary(item) for item in deltas)
        state = self._require_state(final.scene_id)
        has_hash_matched_commit = any(
            commit.final_content_hash == self._final_hash(final)
            and commit.commit_kind in _SCENE_COMPLETION_COMMIT_KINDS
            for commit in commits
        )
        if (
            state.narrative_sync_status == "synced"
            and state.narrative_sync_final_scene_row_id == final.row_id
            and has_hash_matched_commit
        ):
            snapshot.status = "complete"
        elif state.narrative_sync_status == "degraded":
            snapshot.status = "degraded"
        else:
            snapshot.status = "pending"
        self.session.flush()
        return snapshot

    def rebuild_chapter_snapshot(self, project_id: str, chapter_id: str) -> ContinuitySnapshot:
        """章级连续性快照是按章内的场重建的投影：场景卡跨章搬动之后，两头的章都要重算（见 scene_rehome）。"""
        return self._rebuild_chapter_snapshot(project_id, chapter_id)

    def _rebuild_chapter_snapshot(self, project_id: str, chapter_id: str) -> ContinuitySnapshot:
        scene_rows = list(
            self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq, SceneCard.scene_id)
            ).scalars().all()
        )
        current_scene_snapshots: list[ContinuitySnapshot] = []
        status_rows: list[dict[str, Any]] = []
        for scene in scene_rows:
            state = self.session.get(SceneRunState, scene.scene_id)
            final_id = state.current_final_scene_row_id if state is not None else None
            snap = (
                self.session.get(ContinuitySnapshot, f"continuity_scene_{final_id}")
                if final_id
                else None
            )
            if snap is not None:
                current_scene_snapshots.append(snap)
            authoritative_status = self.scene_status(project_id, scene.scene_id)
            status_rows.append(
                {
                    "scene_id": scene.scene_id,
                    "status": authoritative_status["status"],
                }
            )
        snapshot_id = f"continuity_chapter_{chapter_id}"
        snapshot = self.session.get(ContinuitySnapshot, snapshot_id)
        if snapshot is None:
            snapshot = ContinuitySnapshot(
                snapshot_id=snapshot_id,
                project_id=project_id,
                scope_type="chapter",
                scope_id=chapter_id,
                chapter_id=chapter_id,
                status="pending",
            )
            self.session.add(snapshot)
        snapshot.summary_text = "\n".join(
            item.summary_text for item in current_scene_snapshots if item.summary_text
        )
        snapshot.state_deltas_json = self._merge_snapshot_lists(current_scene_snapshots, "state_deltas_json")
        snapshot.knowledge_deltas_json = self._merge_snapshot_lists(
            current_scene_snapshots, "knowledge_deltas_json"
        )
        snapshot.relationship_deltas_json = self._merge_snapshot_lists(
            current_scene_snapshots, "relationship_deltas_json"
        )
        snapshot.item_deltas_json = self._merge_snapshot_lists(current_scene_snapshots, "item_deltas_json")
        snapshot.timeline_deltas_json = self._merge_snapshot_lists(
            current_scene_snapshots, "timeline_deltas_json"
        )
        snapshot.entity_ids_json = list(
            dict.fromkeys(
                entity
                for item in current_scene_snapshots
                for entity in (item.entity_ids_json or [])
            )
        )
        snapshot.open_obligations_json = list(
            dict.fromkeys(
                obligation
                for item in current_scene_snapshots
                for obligation in (item.open_obligations_json or [])
            )
        )
        snapshot.source_commit_ids_json = list(
            dict.fromkeys(
                commit_id
                for item in current_scene_snapshots
                for commit_id in (item.source_commit_ids_json or [])
            )
        )
        snapshot.latest_commit_id = (
            current_scene_snapshots[-1].latest_commit_id if current_scene_snapshots else None
        )
        snapshot.metadata_json = {"scene_statuses": status_rows}
        snapshot.status = (
            "complete"
            if status_rows and all(item["status"] == "synced" for item in status_rows)
            else "degraded"
            if any(item["status"] == "degraded" for item in status_rows)
            else "pending"
        )
        self.session.flush()
        return snapshot

    # ------------------------------------------------------------------
    # Low-level utilities
    # ------------------------------------------------------------------

    def _has_valid_completion_commit(
        self,
        final: FinalScene | None,
        snapshot: ContinuitySnapshot | None,
        *,
        project_id: str,
        scene_id: str,
    ) -> bool:
        if (
            final is None
            or snapshot is None
            or snapshot.final_scene_row_id != final.row_id
        ):
            return False
        current_hash = self._final_hash(final)
        return any(
            commit is not None
            and commit.project_id == project_id
            and commit.scene_id == scene_id
            and commit.final_scene_row_id == final.row_id
            and commit.final_content_hash == current_hash
            and commit.status == "active"
            and commit.commit_kind in _SCENE_COMPLETION_COMMIT_KINDS
            for commit in (
                self.session.get(CanonCommit, commit_id)
                for commit_id in (snapshot.source_commit_ids_json or [])
            )
        )

    def _final_context(
        self,
        final_scene_row_id: str,
    ) -> tuple[FinalScene, SceneCard, str, SceneRunState]:
        final = self.session.get(FinalScene, final_scene_row_id)
        if final is None:
            raise DomainError("FINAL_SCENE_NOT_FOUND", "final scene not found", status_code=404)
        scene = self.session.get(SceneCard, final.scene_id)
        if scene is None or scene.chapter_id != final.chapter_id:
            raise DomainError("CANON_SCENE_IDENTITY_INVALID", "final scene identity is invalid", status_code=409)
        project_id = self._scene_project_id(scene)
        return final, scene, project_id, self._require_state(scene.scene_id)

    def _current_scene_context(
        self,
        project_id: str,
        scene_id: str,
        *,
        require_final: bool = True,
    ) -> tuple[FinalScene | None, SceneCard, str, SceneRunState | None]:
        scene = self.session.get(SceneCard, scene_id)
        if scene is None or scene.trashed_flag or self._scene_project_id(scene) != project_id:
            raise self._scene_not_found()
        state = self.session.get(SceneRunState, scene_id)
        final = (
            self.session.get(FinalScene, state.current_final_scene_row_id)
            if state is not None and state.current_final_scene_row_id
            else None
        )
        if require_final and final is None:
            raise DomainError("FINAL_SCENE_NOT_FOUND", "current final scene not found", status_code=404)
        return final, scene, project_id, state

    def _scene_project_id(self, scene: SceneCard) -> str:
        if scene.project_id:
            return scene.project_id
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        if chapter is None or not chapter.project_id:
            raise DomainError("SCENE_PROJECT_REQUIRED", "scene has no project owner", status_code=409)
        return chapter.project_id

    def _require_state(self, scene_id: str) -> SceneRunState:
        state = self.session.get(SceneRunState, scene_id)
        if state is None:
            raise DomainError("SCENE_STATE_NOT_FOUND", "scene runtime state not found", status_code=409)
        return state

    @staticmethod
    def _require_current_final(state: SceneRunState, final: FinalScene) -> None:
        if state.current_final_scene_row_id != final.row_id:
            raise DomainError(
                "CANON_FINAL_SCENE_CONFLICT",
                "canon operation targets a superseded final scene",
                status_code=409,
                details={
                    "target_final_scene_row_id": final.row_id,
                    "current_final_scene_row_id": state.current_final_scene_row_id,
                },
            )

    def _supersede_prior_revision(self, scene_id: str, current_final_scene_row_id: str) -> None:
        for row in self.session.execute(
            select(FactCandidate).where(
                FactCandidate.scene_id == scene_id,
                FactCandidate.final_scene_row_id != current_final_scene_row_id,
                FactCandidate.status.in_(("pending", "accepted")),
            )
        ).scalars().all():
            row.status = "superseded"
        for row in self.session.execute(
            select(NarrativeEvent).where(
                NarrativeEvent.scene_id == scene_id,
                NarrativeEvent.final_scene_row_id.is_not(None),
                NarrativeEvent.final_scene_row_id != current_final_scene_row_id,
                NarrativeEvent.authority_status.in_(("pending", "accepted")),
            )
        ).scalars().all():
            row.authority_status = "superseded"
        prior_commits = list(
            self.session.execute(
                select(CanonCommit).where(
                    CanonCommit.scene_id == scene_id,
                    CanonCommit.final_scene_row_id != current_final_scene_row_id,
                    CanonCommit.status == "active",
                )
            ).scalars().all()
        )
        prior_commit_ids = [row.commit_id for row in prior_commits]
        for row in prior_commits:
            row.status = "superseded"
        if prior_commit_ids:
            for timeline in self.session.execute(
                select(TimelineEvent).where(
                    TimelineEvent.realized_canon_commit_id.in_(prior_commit_ids)
                )
            ).scalars().all():
                timeline.realization_status = "planned"
                timeline.realized_canon_commit_id = None
                timeline.realized_scene_id = None
        for row in self.session.execute(
            select(ContinuitySnapshot).where(
                ContinuitySnapshot.scene_id == scene_id,
                ContinuitySnapshot.final_scene_row_id.is_not(None),
                ContinuitySnapshot.final_scene_row_id != current_final_scene_row_id,
                ContinuitySnapshot.status != "superseded",
            )
        ).scalars().all():
            row.status = "superseded"

    def _supersede_stale_current_revision(self, final: FinalScene) -> None:
        """Fail closed if an older path changed prose without creating a new row."""

        current_hash = self._final_hash(final)
        stale_commits = list(
            self.session.execute(
                select(CanonCommit).where(
                    CanonCommit.final_scene_row_id == final.row_id,
                    CanonCommit.status == "active",
                    CanonCommit.final_content_hash != current_hash,
                )
            ).scalars().all()
        )
        if not stale_commits:
            if final.content_hash != current_hash:
                final.content_hash = current_hash
            return
        # Once the stale proof is quarantined, repair the cache so the next
        # review can bind a new commit to the actual prose bytes.
        final.content_hash = current_hash
        stale_commit_ids = [row.commit_id for row in stale_commits]
        for row in stale_commits:
            row.status = "superseded"
        for row in self.session.execute(
            select(FactCandidate).where(
                FactCandidate.final_scene_row_id == final.row_id,
                FactCandidate.status != "superseded",
            )
        ).scalars().all():
            row.status = "superseded"
        for row in self.session.execute(
            select(NarrativeEvent).where(
                NarrativeEvent.final_scene_row_id == final.row_id,
                NarrativeEvent.authority_status.in_(("pending", "accepted", "rejected")),
            )
        ).scalars().all():
            row.authority_status = "superseded"
        for timeline in self.session.execute(
            select(TimelineEvent).where(
                TimelineEvent.realized_canon_commit_id.in_(stale_commit_ids)
            )
        ).scalars().all():
            timeline.realization_status = "planned"
            timeline.realized_canon_commit_id = None
            timeline.realized_scene_id = None
        snapshot = self.session.get(ContinuitySnapshot, f"continuity_scene_{final.row_id}")
        if snapshot is not None:
            snapshot.status = "superseded"

    def _supersede_prior_pending_revision(
        self,
        scene_id: str,
        current_final_scene_row_id: str,
    ) -> None:
        """Retire abandoned partial reviews while preserving completed canon."""

        prior_snapshots = list(
            self.session.execute(
                select(ContinuitySnapshot).where(
                    ContinuitySnapshot.scene_id == scene_id,
                    ContinuitySnapshot.final_scene_row_id.is_not(None),
                    ContinuitySnapshot.final_scene_row_id != current_final_scene_row_id,
                )
            ).scalars().all()
        )
        preserved_final_ids: set[str] = set()
        for row in prior_snapshots:
            if row.status != "complete" or not row.final_scene_row_id:
                continue
            final = self.session.get(FinalScene, row.final_scene_row_id)
            if self._has_valid_completion_commit(
                final,
                row,
                project_id=row.project_id,
                scene_id=scene_id,
            ):
                preserved_final_ids.add(row.final_scene_row_id)
        candidates = self.session.execute(
            select(FactCandidate).where(
                FactCandidate.scene_id == scene_id,
                FactCandidate.final_scene_row_id != current_final_scene_row_id,
                FactCandidate.status.in_(("pending", "accepted")),
            )
        ).scalars().all()
        for row in candidates:
            if row.final_scene_row_id not in preserved_final_ids:
                row.status = "superseded"
        events = self.session.execute(
            select(NarrativeEvent).where(
                NarrativeEvent.scene_id == scene_id,
                NarrativeEvent.final_scene_row_id.is_not(None),
                NarrativeEvent.final_scene_row_id != current_final_scene_row_id,
                NarrativeEvent.authority_status.in_(("pending", "accepted")),
            )
        ).scalars().all()
        for row in events:
            if row.final_scene_row_id not in preserved_final_ids:
                row.authority_status = "superseded"
        retired_commits = [
            row
            for row in self.session.execute(
                select(CanonCommit).where(
                    CanonCommit.scene_id == scene_id,
                    CanonCommit.final_scene_row_id != current_final_scene_row_id,
                    CanonCommit.status == "active",
                )
            ).scalars().all()
            if row.final_scene_row_id not in preserved_final_ids
        ]
        retired_commit_ids = [row.commit_id for row in retired_commits]
        for row in retired_commits:
            row.status = "superseded"
        if retired_commit_ids:
            for timeline in self.session.execute(
                select(TimelineEvent).where(
                    TimelineEvent.realized_canon_commit_id.in_(retired_commit_ids)
                )
            ).scalars().all():
                timeline.realization_status = "planned"
                timeline.realized_canon_commit_id = None
                timeline.realized_scene_id = None
        for row in prior_snapshots:
            if row.final_scene_row_id not in preserved_final_ids:
                row.status = "superseded"

    def _ensure_scene_snapshot(self, final: FinalScene, project_id: str) -> ContinuitySnapshot:
        snapshot_id = f"continuity_scene_{final.row_id}"
        snapshot = self.session.get(ContinuitySnapshot, snapshot_id)
        if snapshot is None:
            snapshot = ContinuitySnapshot(
                snapshot_id=snapshot_id,
                project_id=project_id,
                scope_type="scene",
                scope_id=final.row_id,
                chapter_id=final.chapter_id,
                scene_id=final.scene_id,
                final_scene_row_id=final.row_id,
                status="pending",
            )
            self.session.add(snapshot)
            self.session.flush()
        return snapshot

    def _candidate_rows(self, final_scene_row_id: str) -> list[FactCandidate]:
        return list(
            self.session.execute(
                select(FactCandidate)
                .where(
                    FactCandidate.final_scene_row_id == final_scene_row_id,
                    FactCandidate.status != "superseded",
                )
                .order_by(FactCandidate.created_at, FactCandidate.candidate_id)
            ).scalars().all()
        )

    def _pending_candidate_count(self, final_scene_row_id: str) -> int:
        return sum(row.status == "pending" for row in self._candidate_rows(final_scene_row_id))

    def _entity_belongs_to_project(
        self,
        project_id: str,
        entity_type: str,
        entity_id: str,
    ) -> bool:
        if entity_type in {"character", "relation"}:
            row = self.session.get(StoryCharacter, entity_id)
            return bool(row is not None and row.project_id == project_id)
        row = self.session.get(LibraryEntity, entity_id)
        return bool(row is not None and row.project_id == project_id)

    def _character_aliases(self, row: StoryCharacter) -> set[str]:
        values: list[str] = []
        for payload in (row.summary_json, row.synopsis_json, row.bible_json):
            self._collect_alias_values(payload, values)
        return {self._normalized_name(value) for value in values if self._normalized_name(value)}

    @classmethod
    def _collect_alias_values(cls, value: Any, output: list[str], *, parent_key: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = str(key).strip().casefold()
                if normalized_key in _ALIAS_KEYS:
                    cls._collect_scalar_values(child, output)
                elif isinstance(child, (dict, list)):
                    cls._collect_alias_values(child, output, parent_key=normalized_key)
        elif isinstance(value, list):
            for child in value:
                cls._collect_alias_values(child, output, parent_key=parent_key)

    @staticmethod
    def _collect_scalar_values(value: Any, output: list[str]) -> None:
        if isinstance(value, str):
            output.extend(part.strip() for part in value.replace("，", ",").split(",") if part.strip())
        elif isinstance(value, list):
            output.extend(str(part).strip() for part in value if str(part).strip())

    @staticmethod
    def _normalized_name(value: Any) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
        return "".join(normalized.split())

    @staticmethod
    def _entity_type_for_event(event_type: str) -> str:
        if event_type == "item_change":
            return "item"
        if event_type.startswith("foreshadow_"):
            return "foreshadow"
        if event_type == "relation_change":
            return "relation"
        return "character"

    def _realize_accepted_timelines(self, final: FinalScene) -> None:
        for candidate in self._candidate_rows(final.row_id):
            if (
                candidate.status != "accepted"
                or not candidate.planned_timeline_event_id
                or not candidate.canon_commit_id
            ):
                continue
            commit = self.session.get(CanonCommit, candidate.canon_commit_id)
            if commit is None or commit.status != "active":
                raise DomainError(
                    "CANON_TIMELINE_COMMIT_INVALID",
                    "an accepted timeline candidate has no active canon commit",
                    status_code=409,
                    details={"candidate_id": candidate.candidate_id},
                )
            self._realize_timeline_event(
                project_id=candidate.project_id,
                scene_id=candidate.scene_id,
                timeline_event_id=candidate.planned_timeline_event_id,
                commit_id=candidate.canon_commit_id,
            )

    def _activate_accepted_events(self, final: FinalScene) -> None:
        """Publish reviewed facts only at the scene-completion boundary."""

        for candidate in self._candidate_rows(final.row_id):
            if candidate.status != "accepted":
                continue
            event = (
                self.session.get(NarrativeEvent, candidate.staged_event_id)
                if candidate.staged_event_id
                else None
            )
            commit = (
                self.session.get(CanonCommit, candidate.canon_commit_id)
                if candidate.canon_commit_id
                else None
            )
            if (
                event is None
                or commit is None
                or commit.status != "active"
                or commit.commit_kind != "candidate_acceptance"
                or commit.final_scene_row_id != final.row_id
                or commit.final_content_hash != self._final_hash(final)
                or event.final_scene_row_id != final.row_id
            ):
                raise DomainError(
                    "CANON_ACCEPTED_EVENT_INVALID",
                    "an accepted fact candidate has no valid event and hash-matched audit commit",
                    status_code=409,
                    details={"candidate_id": candidate.candidate_id},
                )
            event.authority_status = "accepted"
            event.source_kind = "canon_acceptance"

    def _realize_timeline_event(
        self,
        *,
        project_id: str,
        scene_id: str,
        timeline_event_id: str,
        commit_id: str,
    ) -> None:
        timeline = self.session.get(TimelineEvent, timeline_event_id)
        if timeline is None or timeline.project_id != project_id:
            raise DomainError("CANON_TIMELINE_EVENT_NOT_FOUND", "timeline event not found", status_code=404)
        timeline.realization_status = "realized"
        timeline.realized_canon_commit_id = commit_id
        timeline.realized_scene_id = scene_id

    @staticmethod
    def _event_delta(event: NarrativeEvent) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "canon_commit_id": event.canon_commit_id,
            "event_type": event.event_type,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "fact_key": event.fact_key,
            "fact_value": event.fact_value,
            "evidence": event.source_text_excerpt or "",
            "scene_id": event.scene_id,
        }

    @staticmethod
    def _delta_summary(delta: dict[str, Any]) -> str:
        return (
            f"- [{delta['event_type']}] {delta['entity_id']} · "
            f"{delta['fact_key']} = {delta['fact_value']}"
        )

    @staticmethod
    def _merge_snapshot_lists(rows: list[ContinuitySnapshot], field: str) -> list[dict[str, Any]]:
        return [item for row in rows for item in (getattr(row, field) or [])]

    def _serialize_candidate(self, row: FactCandidate) -> dict[str, Any]:
        final = self.session.get(FinalScene, row.final_scene_row_id)
        evidence_grounded = bool(
            final is not None
            and row.evidence_start is not None
            and row.evidence_end is not None
            and row.evidence_start >= 0
            and row.evidence_end > row.evidence_start
            and final.content[row.evidence_start : row.evidence_end] == (row.evidence_text or "")
        )
        return {
            "candidate_id": row.candidate_id,
            "project_id": row.project_id,
            "chapter_id": row.chapter_id,
            "scene_id": row.scene_id,
            "final_scene_row_id": row.final_scene_row_id,
            "event_type": row.event_type,
            "entity_type": row.entity_type,
            "raw_entity_ref": row.raw_entity_ref,
            "resolved_entity_id": row.resolved_entity_id,
            "entity_resolution_status": row.entity_resolution_status,
            "entity_candidates": list(row.entity_candidates_json or []),
            "entity_options": self._entity_options(row),
            "fact_key": row.fact_key,
            "fact_value": row.fact_value,
            "evidence": {
                "text": row.evidence_text or "",
                "start": row.evidence_start,
                "end": row.evidence_end,
                "grounded": evidence_grounded,
            },
            "source_kind": row.source_kind,
            "confidence": row.confidence,
            "criticality": row.criticality,
            "planned_timeline_event_id": row.planned_timeline_event_id,
            "status": row.status,
            "canon_commit_id": row.canon_commit_id,
            "decided_by": row.decided_by,
            "decided_at": row.decided_at,
            "decision_note": row.decision_note,
            "created_at": row.created_at,
        }

    def _entity_options(self, row: FactCandidate) -> list[dict[str, str]]:
        values: list[dict[str, str]] = []
        for entity_id in row.entity_candidates_json or []:
            character = self.session.get(StoryCharacter, entity_id)
            if character is not None and character.project_id == row.project_id:
                values.append({"entity_id": entity_id, "label": character.display_name})
                continue
            entity = self.session.get(LibraryEntity, entity_id)
            if entity is not None and entity.project_id == row.project_id:
                values.append({"entity_id": entity_id, "label": entity.name})
        return values

    @staticmethod
    def _serialize_snapshot(row: ContinuitySnapshot | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "snapshot_id": row.snapshot_id,
            "scope_type": row.scope_type,
            "scope_id": row.scope_id,
            "status": row.status,
            "summary_text": row.summary_text or "",
            "state_deltas": list(row.state_deltas_json or []),
            "knowledge_deltas": list(row.knowledge_deltas_json or []),
            "relationship_deltas": list(row.relationship_deltas_json or []),
            "item_deltas": list(row.item_deltas_json or []),
            "timeline_deltas": list(row.timeline_deltas_json or []),
            "open_obligations": list(row.open_obligations_json or []),
            "entity_ids": list(row.entity_ids_json or []),
            "source_commit_ids": list(row.source_commit_ids_json or []),
            "metadata": dict(row.metadata_json or {}),
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _final_hash(final: FinalScene) -> str:
        # The prose itself is authoritative. A cached content_hash can become
        # stale if an older mutation path edits a FinalScene in place.
        return hashlib.sha256((final.content or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _stable_digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _scene_not_found() -> DomainError:
        return DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
