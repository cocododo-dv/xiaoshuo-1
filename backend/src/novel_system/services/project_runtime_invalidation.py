from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.services.snowflake_staleness import scene_row_content
from novel_system.db.models import (
    ChapterGoal,
    ChapterState,
    FinalScene,
    QcReport,
    SceneCard,
    SceneDraft,
    SceneExecutionContract,
    SceneRunState,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    StoryProject,
)

# 2026-09-13 阶段 G（让回溯便宜）：书级设计步骤重新确认时**不再**把全书草稿 / QC / 终稿置 stale、
# 把每个场景运行态打回 needs_replan。改一句话概括或道德前提的措辞不该毁掉三十场正文——
# 已确认的设计通过起草 bundle 的 Scene Design Context 段在下一次运行时自然到达写手，
# 哪些场要重写由作者决定。这些步骤的批准回包 scope 为 advisory。
ADVISORY_STEP_KEYS: frozenset[str] = frozenset(
    {"book_brief", "one_sentence_summary", "one_paragraph_summary", "short_synopsis"}
)


class SnowflakeImpactAnalyzer:
    """Compute the smallest runtime scope affected by a snowflake approval."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def analyze(
        self,
        project_id: str,
        step_key: str,
        *,
        previous_payload: dict[str, Any] | None = None,
        current_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scene_ids = self._project_scene_ids(project_id)
        if not scene_ids:
            return self._impact(step_key, [], broad=False, summary="No materialized scenes are affected yet.")

        if step_key in ADVISORY_STEP_KEYS:
            return self._impact(
                step_key,
                [],
                broad=False,
                advisory=True,
                summary=(
                    f"{step_key} is book-level design: existing drafts stay valid and the next scene run "
                    "reads the confirmed design through the Scene Design Context section."
                ),
            )

        changed_scene_ids: list[str] | None = None
        if step_key in {"scene_details", "scene_list"}:
            # 09 / 10 都按 scene_id 逐场比对：只有内容真的变了的场需要重新规划。
            changed_scene_ids = self._changed_scene_detail_ids(previous_payload, current_payload)
        elif step_key == "long_synopsis":
            changed_chapter_uids = self._changed_chapter_row_uids(previous_payload, current_payload)
            if changed_chapter_uids is not None:
                changed_scene_ids = self._scenes_in_chapter_rows(project_id, changed_chapter_uids)
        elif step_key in {"character_sheets", "character_synopses", "character_bibles"}:
            changed_character_ids = self._changed_character_ids(previous_payload, current_payload)
            if changed_character_ids is not None:
                changed_scene_ids = self._scenes_referencing_characters(project_id, changed_character_ids)

        if changed_scene_ids is not None:
            filtered = [scene_id for scene_id in scene_ids if scene_id in set(changed_scene_ids)]
            return self._impact(
                step_key,
                filtered,
                broad=False,
                summary=self._scoped_summary(step_key, len(filtered)),
            )

        return self._impact(
            step_key,
            scene_ids,
            broad=True,
            summary=f"{step_key} can affect the project-wide runtime plan.",
        )

    def _project_scene_ids(self, project_id: str) -> list[str]:
        return [
            row.scene_id
            for row in self.session.execute(
                select(SceneCard)
                .where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
                .order_by(SceneCard.chapter_id.asc(), SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
            ).scalars().all()
        ]

    def _changed_scene_detail_ids(
        self,
        previous_payload: dict[str, Any] | None,
        current_payload: dict[str, Any] | None,
    ) -> list[str] | None:
        previous = self._scenes_by_id(previous_payload)
        current = self._scenes_by_id(current_payload)
        if previous is None or current is None:
            return None
        if set(previous) != set(current):
            return None
        # 阶段 G：按作者真正编辑的内容投影比对（与雪花失效同一把尺）——生成器写的第一版与前端
        # 回传的版本在派生键 / 空键上处处不同，整行比对会把每一场都判成「变了」。
        return [
            scene_id
            for scene_id in current
            if scene_row_content(previous[scene_id]) != scene_row_content(current[scene_id])
        ]

    def _changed_character_ids(
        self,
        previous_payload: dict[str, Any] | None,
        current_payload: dict[str, Any] | None,
    ) -> set[str] | None:
        previous = self._characters_by_id(previous_payload)
        current = self._characters_by_id(current_payload)
        if previous is None or current is None:
            return None
        if set(previous) != set(current):
            return None
        return {
            character_id
            for character_id in current
            if self._stable_json(previous[character_id]) != self._stable_json(current[character_id])
        }

    def _changed_chapter_row_uids(
        self,
        previous_payload: dict[str, Any] | None,
        current_payload: dict[str, Any] | None,
    ) -> set[str] | None:
        """07 章表按 row_uid 比对：改了 / 新增 / 删掉的章。五段展开（paragraphs）单独改动不定位到任何场。"""
        previous = self._chapters_by_uid(previous_payload)
        current = self._chapters_by_uid(current_payload)
        if previous is None or current is None:
            return None
        changed = {uid for uid in set(previous) ^ set(current)}
        for uid in set(previous) & set(current):
            if self._stable_json(previous[uid]) != self._stable_json(current[uid]):
                changed.add(uid)
        return changed

    def _chapters_by_uid(self, payload: dict[str, Any] | None) -> dict[str, dict[str, Any]] | None:
        chapters = (payload or {}).get("chapters")
        if not isinstance(chapters, list):
            return None
        by_uid: dict[str, dict[str, Any]] = {}
        for item in chapters:
            if not isinstance(item, dict):
                return None
            row_uid = str(item.get("row_uid") or "").strip()
            if not row_uid:
                return None
            by_uid[row_uid] = {key: value for key, value in item.items() if key != "chapter_seq"}
        return by_uid

    def _scenes_in_chapter_rows(self, project_id: str, row_uids: set[str]) -> list[str]:
        if not row_uids:
            return []
        chapter_plan_ids = {
            row.chapter_plan_id
            for row in self.session.execute(
                select(SnowflakeChapterPlan).where(
                    SnowflakeChapterPlan.project_id == project_id,
                    SnowflakeChapterPlan.row_uid.in_(sorted(row_uids)),
                )
            ).scalars().all()
        }
        if not chapter_plan_ids:
            return []
        return [
            plan.scene_id
            for plan in self.session.execute(
                select(SnowflakeScenePlan).where(
                    SnowflakeScenePlan.project_id == project_id,
                    SnowflakeScenePlan.chapter_plan_id.in_(sorted(chapter_plan_ids)),
                    SnowflakeScenePlan.removed_at.is_(None),
                )
            ).scalars().all()
        ]

    def _scenes_by_id(self, payload: dict[str, Any] | None) -> dict[str, dict[str, Any]] | None:
        scenes = (payload or {}).get("scenes")
        if not isinstance(scenes, list):
            return None
        by_id: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(scenes, start=1):
            if not isinstance(item, dict):
                return None
            scene_id = str(item.get("scene_id") or "").strip()
            if not scene_id:
                return None
            by_id[scene_id] = {**item, "_ordinal": index}
        return by_id

    def _characters_by_id(self, payload: dict[str, Any] | None) -> dict[str, dict[str, Any]] | None:
        characters = (payload or {}).get("characters")
        if not isinstance(characters, list):
            return None
        by_id: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(characters, start=1):
            if not isinstance(item, dict):
                return None
            character_id = str(item.get("character_id") or "").strip()
            if not character_id:
                return None
            by_id[character_id] = {**item, "_ordinal": index}
        return by_id

    def _scenes_referencing_characters(self, project_id: str, character_ids: set[str]) -> list[str]:
        if not character_ids:
            return []
        rows = self.session.execute(
            select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
        ).scalars().all()
        affected: list[str] = []
        for scene in rows:
            onstage = set(scene.onstage_chars_json or [])
            if scene.pov_character_id in character_ids or onstage.intersection(character_ids):
                affected.append(scene.scene_id)
        return affected

    @staticmethod
    def _stable_json(value: dict[str, Any]) -> str:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _scoped_summary(step_key: str, affected_count: int) -> str:
        if affected_count == 0:
            return f"{step_key} approval did not change any materialized scene runtime input."
        return f"{step_key} approval affects {affected_count} materialized scene runtime input(s)."

    @staticmethod
    def _impact(step_key: str, scene_ids: list[str], *, broad: bool, summary: str, advisory: bool = False) -> dict[str, Any]:
        return {
            "step_key": step_key,
            "scope": "advisory" if advisory else ("project" if broad else "scene"),
            "broad": broad,
            "affected_count": len(scene_ids),
            "affected_scene_ids": scene_ids,
            "summary": summary,
        }


class ProjectRuntimeInvalidationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def invalidate_for_snowflake_step(
        self,
        project_id: str,
        step_key: str,
        *,
        previous_payload: dict[str, Any] | None = None,
        current_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.session.get(StoryProject, project_id)
        if project is None:
            return {
                "step_key": step_key,
                "scope": "none",
                "broad": False,
                "affected_count": 0,
                "affected_scene_ids": [],
                "summary": "Project was not found.",
            }
        impact = SnowflakeImpactAnalyzer(self.session).analyze(
            project_id,
            step_key,
            previous_payload=previous_payload,
            current_payload=current_payload,
        )
        if not impact["affected_scene_ids"]:
            return impact
        scenes = self.session.execute(
            select(SceneCard).where(
                SceneCard.project_id == project_id,
                SceneCard.scene_id.in_(impact["affected_scene_ids"]),
                SceneCard.trashed_flag == 0,
            )
        ).scalars().all()
        if not scenes:
            return impact
        scene_ids = [scene.scene_id for scene in scenes]
        chapter_ids = sorted({scene.chapter_id for scene in scenes})

        for row in self.session.execute(
            select(SceneExecutionContract).where(
                SceneExecutionContract.scene_id.in_(scene_ids),
                SceneExecutionContract.status.in_(("active", "blocked")),
            )
        ).scalars().all():
            row.status = "stale"

        for row in self.session.execute(
            select(SceneDraft).where(SceneDraft.scene_id.in_(scene_ids), SceneDraft.status != "stale")
        ).scalars().all():
            row.status = "stale"

        for row in self.session.execute(
            select(QcReport).where(QcReport.scene_id.in_(scene_ids), QcReport.status != "stale")
        ).scalars().all():
            row.status = "stale"


        final_row_ids = [
            row.current_final_scene_row_id
            for row in self.session.execute(select(SceneRunState).where(SceneRunState.scene_id.in_(scene_ids))).scalars().all()
            if row.current_final_scene_row_id
        ]
        if final_row_ids:
            for row in self.session.execute(
                select(FinalScene).where(FinalScene.row_id.in_(final_row_ids))
            ).scalars().all():
                row.status = "stale"

        for state in self.session.execute(select(SceneRunState).where(SceneRunState.scene_id.in_(scene_ids))).scalars().all():
            state.scene_status = "needs_replan"
            state.current_bundle_id = None
            state.current_bundle_hash = None
            state.current_neutral_draft_row_id = None
            state.current_style_draft_row_id = None
            state.current_final_scene_row_id = None
            state.current_human_review_event_id = None
            state.current_qc_report_id = None
            # 治理 §4.3：latest_valid 在失败/重写路径保留，唯独项目级运行时失效才重置
            state.latest_valid_draft_row_id = None

        for chapter_id in chapter_ids:
            state = self.session.get(ChapterState, chapter_id)
            if state is None:
                state = ChapterState(
                    chapter_id=chapter_id,
                    current_phase="planning",
                    mid_aggregate_enabled_effective=0,
                    aggregate_block_reason="none",
                )
                self.session.add(state)
            state.current_phase = "planning"
            state.aggregate_block_reason = "none"

        self.session.flush()

        if project.status not in {"completed"}:
            project.status = "chapter_blocked"
        return impact


