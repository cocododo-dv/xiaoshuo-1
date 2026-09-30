"""正史核对的公共底座：终稿 / 场景 / 运行状态的定位与校验、候选行、场景快照行、哈希。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    CanonCommit,
    ChapterGoal,
    ContinuitySnapshot,
    FactCandidate,
    FinalScene,
    SceneCard,
    SceneRunState,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_text
from novel_system.services.narrative.taxonomy import SCENE_COMPLETION_COMMIT_KINDS

COMPLETED_EXTRACTION_OUTCOMES = {"completed_events", "completed_empty"}
DEGRADED_EXTRACTION_OUTCOMES = {
    "rejected_before_dispatch",
    "provider_failed",
    "parse_failed",
}


class CanonBase:
    session: Session

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
            and commit.commit_kind in SCENE_COMPLETION_COMMIT_KINDS
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
            raise DomainError("FINAL_SCENE_NOT_FOUND", "找不到这一版终稿", status_code=404)
        scene = self.session.get(SceneCard, final.scene_id)
        if scene is None or scene.chapter_id != final.chapter_id:
            raise DomainError("CANON_SCENE_IDENTITY_INVALID", "终稿与场景对不上", status_code=409)
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
            raise DomainError("FINAL_SCENE_NOT_FOUND", "这一场还没有当前终稿", status_code=404)
        return final, scene, project_id, state

    def _scene_project_id(self, scene: SceneCard) -> str:
        if scene.project_id:
            return scene.project_id
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        if chapter is None or not chapter.project_id:
            raise DomainError("SCENE_PROJECT_REQUIRED", "这一场没有所属作品", status_code=409)
        return chapter.project_id

    def _require_state(self, scene_id: str) -> SceneRunState:
        state = self.session.get(SceneRunState, scene_id)
        if state is None:
            raise DomainError("SCENE_STATE_NOT_FOUND", "这一场还没有运行状态", status_code=409)
        return state

    @staticmethod
    def _require_current_final(state: SceneRunState, final: FinalScene) -> None:
        if state.current_final_scene_row_id != final.row_id:
            raise DomainError(
                "CANON_FINAL_SCENE_CONFLICT",
                "操作的是已被替换的旧版终稿，请刷新后重试",
                status_code=409,
                details={
                    "target_final_scene_row_id": final.row_id,
                    "current_final_scene_row_id": state.current_final_scene_row_id,
                },
            )

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

    @staticmethod
    def _final_hash(final: FinalScene) -> str:
        # The prose itself is authoritative. A cached content_hash can become
        # stale if an older mutation path edits a FinalScene in place.
        return sha256_text(final.content)

    @staticmethod
    def _stable_digest(value: str) -> str:
        return sha256_text(value)

    @staticmethod
    def _scene_not_found() -> DomainError:
        return DomainError("SCENE_NOT_FOUND", "找不到这一场", status_code=404)
