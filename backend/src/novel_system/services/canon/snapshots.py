"""正史核对的读模型（场 / 章的状态载荷，成稿中心直接读）与场 / 章连续性快照的重建。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from novel_system.db.models import (
    CanonCommit,
    ChapterGoal,
    ContinuitySnapshot,
    FactCandidate,
    FinalScene,
    NarrativeEvent,
    SceneCard,
    SceneRunState,
)
from novel_system.services.canon.common import SCENE_COMPLETION_COMMIT_KINDS
from novel_system.services.errors import DomainError


@dataclass(frozen=True)
class _StatusInputs:
    """读模型一次载入的行：按场的运行状态、按行 id 的终稿、按快照 id 的场景快照。"""

    states: dict[str, SceneRunState]
    finals: dict[str, FinalScene]
    snapshots: dict[str, ContinuitySnapshot]


class CanonSnapshotsMixin:
    def scene_status(self, project_id: str, scene_id: str) -> dict[str, Any]:
        _final, scene, owned_project_id, _state = self._current_scene_context(
            project_id,
            scene_id,
            require_final=False,
        )
        if owned_project_id != project_id:
            raise self._scene_not_found()
        return self._scene_status_payloads(project_id, [scene])[0]

    def chapter_status(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None or chapter.project_id != project_id or chapter.trashed_flag:
            raise DomainError("CHAPTER_NOT_FOUND", "找不到这一章", status_code=404)
        items = self._scene_status_payloads(project_id, self._chapter_scene_rows(chapter_id))
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
                "这一章还有场的正史没有核对完成，全部核对完才能定稿",
                status_code=409,
                details=status,
            )
        return status

    def _chapter_scene_rows(self, chapter_id: str) -> list[SceneCard]:
        return list(
            self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq, SceneCard.scene_id)
            ).scalars().all()
        )

    def _status_inputs(self, project_id: str, scenes: list[SceneCard]) -> _StatusInputs:
        """这些场的运行状态、当前终稿、场景快照，以及快照引用的提交（预先载进会话，核对提交时不再逐条查）。"""
        for scene in scenes:
            # 与逐场读取时一样：场的归属对不上作品就当没有这一场
            if self._scene_project_id(scene) != project_id:
                raise self._scene_not_found()
        scene_ids = [scene.scene_id for scene in scenes]
        states = {
            row.scene_id: row
            for row in self.session.execute(
                select(SceneRunState).where(SceneRunState.scene_id.in_(scene_ids))
            ).scalars().all()
        } if scene_ids else {}
        final_ids = [state.current_final_scene_row_id for state in states.values() if state.current_final_scene_row_id]
        finals = {
            row.row_id: row
            for row in self.session.execute(select(FinalScene).where(FinalScene.row_id.in_(final_ids))).scalars().all()
        } if final_ids else {}
        snapshots = {
            row.snapshot_id: row
            for row in self.session.execute(
                select(ContinuitySnapshot).where(
                    ContinuitySnapshot.snapshot_id.in_([f"continuity_scene_{final_id}" for final_id in finals])
                )
            ).scalars().all()
        } if finals else {}
        commit_ids = list(
            dict.fromkeys(
                commit_id for snapshot in snapshots.values() for commit_id in (snapshot.source_commit_ids_json or [])
            )
        )
        if commit_ids:
            # 只为把行放进会话的身份映射：_has_valid_completion_commit 的 session.get 随后不再发语句
            self.session.execute(select(CanonCommit).where(CanonCommit.commit_id.in_(commit_ids))).scalars().all()
        return _StatusInputs(states=states, finals=finals, snapshots=snapshots)

    def _scene_status_core(
        self,
        project_id: str,
        scene: SceneCard,
        inputs: _StatusInputs,
    ) -> tuple[str, FinalScene | None, ContinuitySnapshot | None]:
        """一场的正史状态（不序列化候选）：返回 (状态, 当前终稿, 这版终稿的场景快照)。"""
        state = inputs.states.get(scene.scene_id)
        final_id = state.current_final_scene_row_id if state is not None else None
        final = inputs.finals.get(final_id) if final_id else None
        snapshot = inputs.snapshots.get(f"continuity_scene_{final.row_id}") if final is not None else None
        status = "missing_final"
        if final is not None:
            status = state.narrative_sync_status if state is not None else "pending_extraction"
            if (
                status == "synced"
                and state is not None
                and state.narrative_sync_final_scene_row_id != final.row_id
            ):
                status = "pending_extraction"
            if status == "synced" and (
                snapshot is None
                or snapshot.status != "complete"
                or snapshot.final_scene_row_id != final.row_id
                or not self._has_valid_completion_commit(
                    final,
                    snapshot,
                    project_id=project_id,
                    scene_id=scene.scene_id,
                )
            ):
                status = "pending_review"
        return status, final, snapshot

    def _scene_status_payloads(self, project_id: str, scenes: list[SceneCard]) -> list[dict[str, Any]]:
        inputs = self._status_inputs(project_id, scenes)
        candidates_by_final: dict[str, list[FactCandidate]] = {}
        if inputs.finals:
            for row in self.session.execute(
                select(FactCandidate)
                .where(
                    FactCandidate.final_scene_row_id.in_(list(inputs.finals)),
                    FactCandidate.status != "superseded",
                )
                .order_by(FactCandidate.created_at, FactCandidate.candidate_id)
            ).scalars().all():
                candidates_by_final.setdefault(row.final_scene_row_id, []).append(row)
        entity_labels = self._entity_labels(
            project_id,
            {
                entity_id
                for rows in candidates_by_final.values()
                for row in rows
                for entity_id in (row.entity_candidates_json or [])
            },
        )
        payloads: list[dict[str, Any]] = []
        for scene in scenes:
            status, final, snapshot = self._scene_status_core(project_id, scene, inputs)
            candidates = candidates_by_final.get(final.row_id, []) if final is not None else []
            payloads.append(
                {
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
                    "candidates": [
                        self._serialize_candidate(row, final=final, entity_labels=entity_labels)
                        for row in candidates
                    ],
                    "snapshot": self._serialize_snapshot(snapshot),
                    "extraction": dict(snapshot.metadata_json or {}) if snapshot is not None else {},
                }
            )
        return payloads

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
        snapshot.source_commit_ids_json = [commit.commit_id for commit in commits]
        snapshot.latest_commit_id = commits[-1].commit_id if commits else None
        snapshot.summary_text = "\n".join(self._delta_summary(item) for item in deltas)
        state = self._require_state(final.scene_id)
        has_hash_matched_commit = any(
            commit.final_content_hash == self._final_hash(final)
            and commit.commit_kind in SCENE_COMPLETION_COMMIT_KINDS
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
        # 只要每场的状态和当前终稿的快照：不为此去序列化每一场的候选（B11-12）
        scene_rows = self._chapter_scene_rows(chapter_id)
        inputs = self._status_inputs(project_id, scene_rows)
        current_scene_snapshots: list[ContinuitySnapshot] = []
        status_rows: list[dict[str, Any]] = []
        for scene in scene_rows:
            status, _final, _snapshot = self._scene_status_core(project_id, scene, inputs)
            state = inputs.states.get(scene.scene_id)
            final_id = state.current_final_scene_row_id if state is not None else None
            snap = inputs.snapshots.get(f"continuity_scene_{final_id}") if final_id else None
            if snap is not None:
                current_scene_snapshots.append(snap)
            status_rows.append({"scene_id": scene.scene_id, "status": status})
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

    def _serialize_candidate(
        self,
        row: FactCandidate,
        *,
        final: FinalScene | None = None,
        entity_labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if final is None or final.row_id != row.final_scene_row_id:
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
            "entity_options": self._entity_options(row, entity_labels),
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
