"""作者的决定：手填候选、采纳 / 拒绝、确认本场正史、声明「事实不变」沿用上一版。"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from novel_system.db.models import (
    CanonCommit,
    ContinuitySnapshot,
    FactCandidate,
    FinalScene,
    NarrativeEvent,
    OperationLog,
    TimelineEvent,
    utcnow,
)
from novel_system.services.errors import DomainError
from novel_system.services.narrative.taxonomy import ENTITY_TYPES, EVENT_TYPES, entity_type_for_event
from novel_system.services.narrative_event_log import NarrativeEventLog


class CanonDecisionsMixin:
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
                "这一场的正史已经核对完成，不能再添加候选；要改先让终稿出新版本",
                status_code=409,
                details={"final_scene_row_id": final.row_id},
            )
        self._supersede_stale_current_revision(final)
        if event_type not in EVENT_TYPES:
            raise DomainError("CANON_EVENT_TYPE_INVALID", "不支持这种事实类型", status_code=400)
        clean_entity = str(raw_entity_ref or "").strip()
        clean_key = str(fact_key or "").strip()
        clean_value = str(fact_value or "").strip()
        clean_evidence = str(evidence_text or "").strip()
        if not all((clean_entity, clean_key, clean_value, clean_evidence)):
            raise DomainError(
                "CANON_MANUAL_FACT_INCOMPLETE",
                "人物 / 实体、事实、取值和原文证据都要填",
                status_code=400,
            )
        evidence_start = final.content.find(clean_evidence)
        if evidence_start < 0:
            raise DomainError(
                "CANON_EVIDENCE_NOT_IN_FINAL",
                "原文证据必须是当前终稿里一字不差的一段",
                status_code=409,
            )
        resolved_type = entity_type or entity_type_for_event(event_type)
        if resolved_type not in ENTITY_TYPES:
            raise DomainError("CANON_ENTITY_TYPE_INVALID", "不支持这种实体类型", status_code=400)
        if planned_timeline_event_id:
            timeline = self.session.get(TimelineEvent, planned_timeline_event_id)
            if timeline is None or timeline.project_id != project_id:
                raise DomainError("CANON_TIMELINE_EVENT_NOT_FOUND", "找不到这个计划时间线事件", status_code=404)

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
            raise DomainError("CANON_DECISION_INVALID", "只能采纳或拒绝", status_code=400)
        candidate = self.session.get(FactCandidate, candidate_id)
        if candidate is None or candidate.project_id != project_id:
            raise DomainError("CANON_CANDIDATE_NOT_FOUND", "找不到这条候选事实", status_code=404)
        final, _scene, owned_project_id, state = self._final_context(candidate.final_scene_row_id)
        if owned_project_id != project_id:
            raise DomainError("CANON_CANDIDATE_NOT_FOUND", "找不到这条候选事实", status_code=404)
        self._require_current_final(state, final)
        if expected_final_scene_row_id and expected_final_scene_row_id != final.row_id:
            raise DomainError(
                "CANON_FINAL_SCENE_CONFLICT",
                "核对期间这一场的终稿换了新版本，请刷新后重新核对",
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
                "这条候选已经有了另一个决定",
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
                    "这条候选的原文证据在当前终稿里找不到原句，不能采纳进正史",
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
                "确认之前这一场的终稿换了新版本，请刷新后重新核对",
                status_code=409,
            )
        self._supersede_stale_current_revision(final)
        pending_count = self._pending_candidate_count(final.row_id)
        if pending_count:
            raise DomainError(
                "CANON_CANDIDATES_PENDING",
                "还有候选没有采纳或拒绝，全部处理完才能确认本场正史",
                status_code=409,
                details={"pending_count": pending_count},
            )
        clean_note = str(note or "").strip()
        if not clean_note:
            raise DomainError(
                "CANON_VERIFICATION_NOTE_REQUIRED",
                "确认本场正史需要写一句核对说明",
                status_code=400,
            )
        # Verification of a replacement revision atomically retires the prior
        # revision and realizes only the accepted current-final timeline facts.
        self._supersede_prior_revision(scene.scene_id, final.row_id)
        self._activate_accepted_events(final)
        self._realize_accepted_timelines(final)
        commit_id, commit = self._mint_commit_id("canon_verify", final)
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
                "「事实不变」要指定之前另一版已经核对过的终稿",
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
                "来源那一版终稿的正史没有完整核对（或正文后来变了），不能沿用它的事实",
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
        commit_id, commit = self._mint_commit_id("canon_carry", final)
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
