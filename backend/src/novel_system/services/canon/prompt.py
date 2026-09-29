"""起草提示里的「最近已提交的正史变化」：往前找几场已完整核对的正史，列出它们的变化。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select

from novel_system.db.models import ContinuitySnapshot, FinalScene, SceneRunState
from novel_system.services.character_names import display_name_of, project_entity_names


class CanonPromptMixin:
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
        # 以前从这一场往前逐场各查三次（运行状态、快照、终稿），没有正史的作品每次起草都扫全书
        # （B11-06）：现在两条语句找出前面哪些场的当前终稿有「已完成」的快照，逐场的核对只做到够数为止。
        current_finals = {
            scene_id: final_row_id
            for scene_id, final_row_id in self.session.execute(
                select(SceneRunState.scene_id, SceneRunState.current_final_scene_row_id).where(
                    SceneRunState.scene_id.in_([scene.scene_id for scene in positioned])
                )
            ).all()
            if final_row_id
        }
        complete_snapshot_ids = set(
            self.session.execute(
                select(ContinuitySnapshot.snapshot_id).where(
                    ContinuitySnapshot.snapshot_id.in_(
                        [f"continuity_scene_{final_row_id}" for final_row_id in current_finals.values()]
                    ),
                    ContinuitySnapshot.project_id == project_id,
                    ContinuitySnapshot.status == "complete",
                )
            ).scalars().all()
        ) if current_finals else set()
        snapshots: list[ContinuitySnapshot] = []
        for scene in reversed(positioned):
            final_row_id = current_finals.get(scene.scene_id)
            if not final_row_id or f"continuity_scene_{final_row_id}" not in complete_snapshot_ids:
                continue
            snapshot = self.session.get(ContinuitySnapshot, f"continuity_scene_{final_row_id}")
            if (
                snapshot is None
                or snapshot.project_id != project_id
                or snapshot.status != "complete"
                or snapshot.final_scene_row_id != final_row_id
            ):
                continue
            final = self.session.get(FinalScene, final_row_id)
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
        # 快照里记的是实体 id，提示词里写名字（B11-01）；没有记录的 id 原样印
        names = project_entity_names(self.session, project_id)
        lines = [
            "## Recent Committed Continuity Changes (canon only; do NOT contradict)",
        ]
        for snapshot, delta in rows:
            entity_id = str(delta.get("entity_id") or "")
            lines.append(
                "- "
                f"[{snapshot.chapter_id}/{snapshot.scene_id}] "
                f"{display_name_of(names, entity_id) if entity_id else delta.get('entity_id')}.{delta.get('fact_key')} = "
                f"{delta.get('fact_value')} ({delta.get('event_type')})"
            )
        return "\n".join(lines)
