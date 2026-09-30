"""一场终稿换了版本时，旧版的正史怎么退役；核对完成时新版的事实怎么接上。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from novel_system.db.models import (
    CanonCommit,
    ContinuitySnapshot,
    FactCandidate,
    FinalScene,
    NarrativeEvent,
    TimelineEvent,
)
from novel_system.services.errors import DomainError


@dataclass(frozen=True)
class _RetireScope:
    """一次退役的范围：各表的筛选条件，外加要保住的终稿。"""

    candidates: tuple[Any, ...]
    events: tuple[Any, ...]
    commits: tuple[Any, ...]
    snapshots: list[ContinuitySnapshot]
    keep_final_ids: frozenset[str] = field(default_factory=frozenset)


class CanonRevisionsMixin:
    def _mint_commit_id(self, prefix: str, final: FinalScene) -> tuple[str, CanonCommit | None]:
        """「本场核对完成」提交的 id：由终稿行与正文哈希决定，重复调用拿到同一条；
        同一个 id 已经被退役过时另起一个带随机后缀的，返回 (id, 已有的有效提交或 None)。"""
        commit_id = f"{prefix}_{self._stable_digest(final.row_id + ':' + self._final_hash(final))[:20]}"
        commit = self.session.get(CanonCommit, commit_id)
        if commit is not None and commit.status != "active":
            return f"{commit_id}_{uuid.uuid4().hex[:8]}", None
        return commit_id, commit

    def _retire(self, scope: _RetireScope) -> None:
        """把一版（或几版）终稿名下的正史产物一起退役：候选、暂存 / 已认可事件、提交、它们兑现过的计划时间线、
        场景快照。``keep_final_ids`` 里的终稿（已完整核对、要保住的上一版正史）原样留着。"""
        keep = scope.keep_final_ids
        for row in self.session.execute(select(FactCandidate).where(*scope.candidates)).scalars().all():
            if row.final_scene_row_id not in keep:
                row.status = "superseded"
        for row in self.session.execute(select(NarrativeEvent).where(*scope.events)).scalars().all():
            if row.final_scene_row_id not in keep:
                row.authority_status = "superseded"
        retired_commits = [
            row
            for row in self.session.execute(select(CanonCommit).where(*scope.commits)).scalars().all()
            if row.final_scene_row_id not in keep
        ]
        for row in retired_commits:
            row.status = "superseded"
        if retired_commits:
            for timeline in self.session.execute(
                select(TimelineEvent).where(
                    TimelineEvent.realized_canon_commit_id.in_([row.commit_id for row in retired_commits])
                )
            ).scalars().all():
                timeline.realization_status = "planned"
                timeline.realized_canon_commit_id = None
                timeline.realized_scene_id = None
        for row in scope.snapshots:
            if row.final_scene_row_id not in keep:
                row.status = "superseded"

    def _supersede_prior_revision(self, scene_id: str, current_final_scene_row_id: str) -> None:
        """确认新一版时，这一场之前各版的正史一律退役（新版的事实此刻原子地接上）。"""
        self._retire(
            _RetireScope(
                candidates=(
                    FactCandidate.scene_id == scene_id,
                    FactCandidate.final_scene_row_id != current_final_scene_row_id,
                    FactCandidate.status.in_(("pending", "accepted")),
                ),
                events=(
                    NarrativeEvent.scene_id == scene_id,
                    NarrativeEvent.final_scene_row_id.is_not(None),
                    NarrativeEvent.final_scene_row_id != current_final_scene_row_id,
                    NarrativeEvent.authority_status.in_(("pending", "accepted")),
                ),
                commits=(
                    CanonCommit.scene_id == scene_id,
                    CanonCommit.final_scene_row_id != current_final_scene_row_id,
                    CanonCommit.status == "active",
                ),
                snapshots=self._prior_scene_snapshots(
                    scene_id,
                    current_final_scene_row_id,
                    ContinuitySnapshot.status != "superseded",
                ),
            )
        )

    def _supersede_stale_current_revision(self, final: FinalScene) -> None:
        """Fail closed if an older path changed prose without creating a new row."""

        current_hash = self._final_hash(final)
        stale_commit_filter = (
            CanonCommit.final_scene_row_id == final.row_id,
            CanonCommit.status == "active",
            CanonCommit.final_content_hash != current_hash,
        )
        has_stale_commit = self.session.execute(
            select(CanonCommit.commit_id).where(*stale_commit_filter).limit(1)
        ).first() is not None
        # Once the stale proof is quarantined, repair the cache so the next
        # review can bind a new commit to the actual prose bytes.
        if final.content_hash != current_hash:
            final.content_hash = current_hash
        if not has_stale_commit:
            return
        snapshot = self.session.get(ContinuitySnapshot, f"continuity_scene_{final.row_id}")
        self._retire(
            _RetireScope(
                candidates=(
                    FactCandidate.final_scene_row_id == final.row_id,
                    FactCandidate.status != "superseded",
                ),
                events=(
                    NarrativeEvent.final_scene_row_id == final.row_id,
                    NarrativeEvent.authority_status.in_(("pending", "accepted", "rejected")),
                ),
                commits=stale_commit_filter,
                snapshots=[snapshot] if snapshot is not None else [],
            )
        )

    def _supersede_prior_pending_revision(
        self,
        scene_id: str,
        current_final_scene_row_id: str,
    ) -> None:
        """Retire abandoned partial reviews while preserving completed canon."""

        prior_snapshots = self._prior_scene_snapshots(scene_id, current_final_scene_row_id)
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
        self._retire(
            _RetireScope(
                candidates=(
                    FactCandidate.scene_id == scene_id,
                    FactCandidate.final_scene_row_id != current_final_scene_row_id,
                    FactCandidate.status.in_(("pending", "accepted")),
                ),
                events=(
                    NarrativeEvent.scene_id == scene_id,
                    NarrativeEvent.final_scene_row_id.is_not(None),
                    NarrativeEvent.final_scene_row_id != current_final_scene_row_id,
                    NarrativeEvent.authority_status.in_(("pending", "accepted")),
                ),
                commits=(
                    CanonCommit.scene_id == scene_id,
                    CanonCommit.final_scene_row_id != current_final_scene_row_id,
                    CanonCommit.status == "active",
                ),
                snapshots=prior_snapshots,
                keep_final_ids=frozenset(preserved_final_ids),
            )
        )

    def _prior_scene_snapshots(
        self,
        scene_id: str,
        current_final_scene_row_id: str,
        *criteria: Any,
    ) -> list[ContinuitySnapshot]:
        return list(
            self.session.execute(
                select(ContinuitySnapshot).where(
                    ContinuitySnapshot.scene_id == scene_id,
                    ContinuitySnapshot.final_scene_row_id.is_not(None),
                    ContinuitySnapshot.final_scene_row_id != current_final_scene_row_id,
                    *criteria,
                )
            ).scalars().all()
        )

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
                    "已采纳的时间线候选没有有效的正史提交",
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
                    "已采纳的候选缺少有效的事件或与终稿对得上的提交",
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
            raise DomainError("CANON_TIMELINE_EVENT_NOT_FOUND", "找不到这个计划时间线事件", status_code=404)
        timeline.realization_status = "realized"
        timeline.realized_canon_commit_id = commit_id
        timeline.realized_scene_id = scene_id
