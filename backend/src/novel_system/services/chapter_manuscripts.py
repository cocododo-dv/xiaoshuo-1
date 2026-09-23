from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    RevisionCandidate,
    SceneBundle,
    SceneCard,
    SceneRunState,
)
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.errors import DomainError
from novel_system.services.reference_copy_gate import check_reference_copy, copy_gate_policies
from novel_system.services.writer_review import WriterReviewService


class ChapterManuscriptService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.lifecycle = AuthorLifecycleService(session)

    def list_manuscripts(self) -> list[dict[str, Any]]:
        chapters = self.session.execute(
            select(ChapterGoal).where(ChapterGoal.trashed_flag == 0).order_by(ChapterGoal.chapter_id.asc())
        ).scalars().all()
        return [self._chapter_list_item(chapter) for chapter in chapters]

    def manuscript_detail(self, chapter_id: str) -> dict[str, Any]:
        chapter = self.lifecycle.require_active_chapter(chapter_id)
        chapter_state = self.session.get(ChapterState, chapter_id)
        scenes = self._active_scenes(chapter_id)
        scene_states = self._scene_states(scenes)
        scene_entries, final_scenes = self._scene_entries(scenes, scene_states)
        assembled = self._assembled_payload(scene_entries)
        completion = self._completion_contract_from_assembled(assembled)
        aggregate = self._aggregate_payload(self._resolve_final_aggregate(chapter_id, chapter_state))
        source_safety_scan = self._reference_copy_scan(
            scenes,
            final_scenes,
            "\n".join([assembled["content"], aggregate["content"] if aggregate else ""]),
        )
        writer_review = WriterReviewService(self.session)
        writer_review_summary = writer_review.chapter_summary(chapter_id)
        editorial_workspace = self._editorial_workspace(
            chapter_id=chapter_id,
            scene_entries=scene_entries,
            chapter_review=writer_review_summary,
            writer_review=writer_review,
            aggregate=aggregate,
        )
        return {
            "chapter": self.lifecycle.serialize_chapter(chapter),
            "chapter_state": self.lifecycle.serialize_chapter_state(chapter_state, chapter_id),
            "completion_status": completion["completion_status"],
            "missing_scene_ids": completion["missing_scene_ids"],
            "comparison_status": self._comparison_status(assembled["content"], aggregate),
            "assembled": assembled,
            "aggregate": aggregate,
            "source_safety_scan": source_safety_scan,
            "writer_review_summary": writer_review_summary,
            "editorial_workspace": editorial_workspace,
            "canon_continuity": self._canon_continuity(chapter),
            "scenes": scene_entries,
        }

    def completion_contract(self, chapter_id: str) -> dict[str, Any]:
        """Return the canonical FinalScene coverage used by every final gate."""

        self.lifecycle.require_active_chapter(chapter_id)
        scenes = self._active_scenes(chapter_id)
        scene_states = self._scene_states(scenes)
        scene_entries, _final_scenes = self._scene_entries(scenes, scene_states)
        return self._completion_contract_from_assembled(
            self._assembled_payload(scene_entries)
        )

    def require_complete(self, chapter_id: str) -> dict[str, Any]:
        contract = self.completion_contract(chapter_id)
        if (
            contract["completion_status"] != "complete"
            or contract["missing_scene_ids"]
        ):
            raise DomainError(
                "CHAPTER_CANONICAL_MANUSCRIPT_INCOMPLETE",
                "every active scene must have non-empty canonical manuscript text before review or approval",
                status_code=409,
                details={"chapter_id": chapter_id, **contract},
            )
        return contract

    def require_publishable(self, chapter_id: str) -> dict[str, Any]:
        """Require both canonical prose coverage and committed continuity facts."""

        contract = self.require_complete(chapter_id)
        chapter = self.lifecycle.require_active_chapter(chapter_id)
        if not chapter.project_id:
            raise DomainError(
                "CHAPTER_PROJECT_REQUIRED",
                "chapter has no project owner and cannot publish continuity canon",
                status_code=409,
                details={"chapter_id": chapter_id},
            )
        continuity = CanonContinuityService(self.session).require_chapter_complete(
            chapter.project_id,
            chapter_id,
        )
        return {**contract, "canon_continuity": continuity}

    def _chapter_list_item(self, chapter: ChapterGoal) -> dict[str, Any]:
        chapter_state = self.session.get(ChapterState, chapter.chapter_id)
        scenes = self._active_scenes(chapter.chapter_id)
        scene_states = self._scene_states(scenes)
        scene_entries, _final_scenes = self._scene_entries(scenes, scene_states)
        assembled = self._assembled_payload(scene_entries)
        aggregate = self._aggregate_payload(self._resolve_final_aggregate(chapter.chapter_id, chapter_state))
        return {
            **self.lifecycle.serialize_chapter_summary(chapter),
            "scene_count": assembled["scene_count"],
            "generated_scene_count": assembled["generated_scene_count"],
            "missing_scene_ids": assembled["missing_scene_ids"],
            "completion_status": self._completion_status(
                assembled["scene_count"],
                assembled["generated_scene_count"],
            ),
            "comparison_status": self._comparison_status(assembled["content"], aggregate),
            "aggregate_row_id": aggregate["row_id"] if aggregate else None,
            "writer_review_summary": WriterReviewService(self.session).chapter_summary(chapter.chapter_id),
            "canon_continuity": self._canon_continuity(chapter),
        }

    def _canon_continuity(self, chapter: ChapterGoal) -> dict[str, Any]:
        if not chapter.project_id:
            return {
                "project_id": None,
                "chapter_id": chapter.chapter_id,
                "complete": False,
                "status": "unavailable",
                "scene_count": 0,
                "synced_scene_count": 0,
                "pending_scene_ids": [],
                "missing_final_scene_ids": [],
                "pending_candidate_count": 0,
                "scenes": [],
                "snapshot": None,
            }
        return CanonContinuityService(self.session).chapter_status(
            chapter.project_id,
            chapter.chapter_id,
        )

    def _active_scenes(self, chapter_id: str) -> list[SceneCard]:
        return self.session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()

    def _scene_states(self, scenes: list[SceneCard]) -> dict[str, SceneRunState]:
        if not scenes:
            return {}
        states = self.session.execute(
            select(SceneRunState).where(SceneRunState.scene_id.in_([scene.scene_id for scene in scenes]))
        ).scalars().all()
        return {state.scene_id: state for state in states}

    def _scene_entries(
        self,
        scenes: list[SceneCard],
        scene_states: dict[str, SceneRunState],
    ) -> tuple[list[dict[str, Any]], dict[str, FinalScene]]:
        row_ids = {
            state.current_final_scene_row_id
            for state in scene_states.values()
            if state.current_final_scene_row_id
        }
        final_scenes = (
            {
                row.row_id: row
                for row in self.session.execute(
                    select(FinalScene).where(FinalScene.row_id.in_(row_ids))
                ).scalars().all()
            }
            if row_ids
            else {}
        )
        entries = [
            self._scene_entry(
                scene,
                scene_states.get(scene.scene_id),
                final_scenes,
            )
            for scene in scenes
        ]
        return entries, final_scenes

    def _scene_entry(
        self,
        scene: SceneCard,
        scene_state: SceneRunState | None,
        final_scenes: dict[str, FinalScene],
    ) -> dict[str, Any]:
        final_scene = None
        if scene_state is not None and scene_state.current_final_scene_row_id:
            candidate = final_scenes.get(scene_state.current_final_scene_row_id)
            if candidate is not None and candidate.scene_id == scene.scene_id and candidate.chapter_id == scene.chapter_id:
                final_scene = candidate
        return {
            **self.lifecycle.serialize_author_scene(scene, scene_state),
            "final_scene": self._final_scene_payload(final_scene),
        }

    @staticmethod
    def _final_scene_payload(final_scene: FinalScene | None) -> dict[str, Any] | None:
        if final_scene is None:
            return None
        return {
            "row_id": final_scene.row_id,
            # Wave 1 前端换源：成稿中心逐场正文以后端归档为源，不再读 wr-doc 缓存
            "content": final_scene.content or "",
            "char_count": len(final_scene.content or ""),
            "created_at": final_scene.created_at,
        }

    @staticmethod
    def _assembled_payload(scene_entries: list[dict[str, Any]]) -> dict[str, Any]:
        generated_contents: list[str] = []
        missing_scene_ids: list[str] = []
        for scene in scene_entries:
            final_scene = scene.get("final_scene")
            if final_scene is None:
                missing_scene_ids.append(scene["scene_id"])
                continue
            content = str(final_scene.get("content") or "")
            if not content.strip():
                missing_scene_ids.append(scene["scene_id"])
                continue
            generated_contents.append(content)
        content = "\n".join(generated_contents)
        return {
            "content": content,
            "char_count": len(content),
            "scene_count": len(scene_entries),
            "generated_scene_count": len(generated_contents),
            "missing_scene_ids": missing_scene_ids,
        }

    def _reference_copy_scan(
        self,
        scenes: list[SceneCard],
        final_scenes: dict[str, FinalScene],
        content: str,
    ) -> dict[str, Any]:
        """整章正文过唯一抄袭门（风格参考 v3）：每场终稿冻结时的绑定 + 这一场当前的活动绑定，书取并集。

        只读展示：检查失败给 ``safe: False`` + 错误码，不拖垮成稿中心。
        """
        try:
            bundle_ids = {row.source_bundle_id for row in final_scenes.values() if row.source_bundle_id}
            bundles = (
                {
                    bundle.bundle_id: bundle
                    for bundle in self.session.execute(
                        select(SceneBundle).where(SceneBundle.bundle_id.in_(bundle_ids))
                    ).scalars().all()
                }
                if bundle_ids
                else {}
            )
            finals_by_scene = {row.scene_id: row for row in final_scenes.values()}
            policies: list[Any] = []
            seen: set[tuple[Any, ...]] = set()
            for scene in scenes:
                final = finals_by_scene.get(scene.scene_id)
                bundle = bundles.get(final.source_bundle_id) if final is not None and final.source_bundle_id else None
                for policy in copy_gate_policies(
                    self.session,
                    scope=scene,
                    bundle_snapshot=bundle.frozen_snapshot_json if bundle is not None else None,
                ):
                    key = (policy.mode, policy.binding_id, policy.contract_hash, policy.book_id)
                    if key not in seen:
                        seen.add(key)
                        policies.append(policy)
            return check_reference_copy(
                self.session,
                content,
                policy=policies[0] if policies else None,
                extra_policies=policies[1:],
            ).audit()
        except Exception as exc:  # noqa: BLE001 — 展示用读数
            return {"safe": False, "error_code": "SOURCE_SAFETY_UNAVAILABLE", "error_type": type(exc).__name__}

    def _editorial_workspace(
        self,
        *,
        chapter_id: str,
        scene_entries: list[dict[str, Any]],
        chapter_review: dict[str, Any],
        writer_review: WriterReviewService,
        aggregate: dict[str, Any] | None,
    ) -> dict[str, Any]:
        scene_ids = [scene["scene_id"] for scene in scene_entries]
        scene_review_by_id = writer_review.summaries("scene", scene_ids)
        scene_reviews = [
            {
                "scene_id": scene["scene_id"],
                "scene_seq": scene.get("scene_seq"),
                "scene_goal": scene.get("scene_goal"),
                "review": scene_review_by_id[scene["scene_id"]],
            }
            for scene in scene_entries
        ]
        candidates = self._chapter_revision_candidates(chapter_id, scene_ids)
        review_summaries = [chapter_review, *[item["review"] for item in scene_reviews]]
        return {
            "reading_source": "aggregate" if aggregate else "assembled",
            "chapter_review": chapter_review,
            "scene_reviews": scene_reviews,
            "revision_candidates": candidates,
            "open_issue_counts": self._open_issue_counts(review_summaries),
        }

    def _chapter_revision_candidates(self, chapter_id: str, scene_ids: list[str]) -> list[dict[str, Any]]:
        object_pairs = {("chapter", chapter_id), *{("scene", scene_id) for scene_id in scene_ids}}
        rows = self.session.execute(
            select(RevisionCandidate)
            .where(RevisionCandidate.chapter_id == chapter_id)
            .order_by(
                RevisionCandidate.object_type.asc(),
                RevisionCandidate.created_at.desc(),
                RevisionCandidate.revision_id.asc(),
            )
        ).scalars().all()
        serialized: list[dict[str, Any]] = []
        for row in rows:
            if (row.object_type, row.object_id) not in object_pairs:
                continue
            payload = WriterReviewService.serialize_revision(row)
            payload["scope_label"] = "chapter" if row.object_type == "chapter" else row.scene_id or row.object_id
            serialized.append(payload)
        return serialized

    @staticmethod
    def _open_issue_counts(review_summaries: list[dict[str, Any]]) -> dict[str, int]:
        open_candidates = 0
        findings = 0
        requires_human_review = 0
        reviewed_objects = 0
        for summary in review_summaries:
            evaluation = summary.get("latest_evaluation")
            if evaluation:
                reviewed_objects += 1
                findings += len(evaluation.get("findings") or [])
                if evaluation.get("requires_human_review"):
                    requires_human_review += 1
            open_candidates += sum(1 for candidate in summary.get("candidates") or [] if candidate.get("status") == "candidate")
        return {
            "open_candidates": open_candidates,
            "findings": findings,
            "requires_human_review": requires_human_review,
            "reviewed_objects": reviewed_objects,
        }

    def _resolve_final_aggregate(self, chapter_id: str, chapter_state: ChapterState | None) -> ChapterMemory | None:
        if chapter_state is not None and chapter_state.last_final_memory_row_id:
            pointed = self.session.get(ChapterMemory, chapter_state.last_final_memory_row_id)
            if pointed is not None and pointed.chapter_id == chapter_id and pointed.aggregate_stage == "final":
                return pointed

        return self.session.execute(
            select(ChapterMemory)
            .where(
                ChapterMemory.chapter_id == chapter_id,
                ChapterMemory.aggregate_stage == "final",
                ChapterMemory.active_flag == 1,
            )
            .order_by(ChapterMemory.created_at.desc(), ChapterMemory.row_id.desc())
        ).scalars().first()

    @staticmethod
    def _aggregate_payload(memory: ChapterMemory | None) -> dict[str, Any] | None:
        if memory is None:
            return None
        return {
            "row_id": memory.row_id,
            "content": memory.content or "",
            "char_count": len(memory.content or ""),
            "created_at": memory.created_at,
        }

    @staticmethod
    def _completion_status(scene_count: int, generated_scene_count: int) -> str:
        if generated_scene_count == 0:
            return "empty"
        if scene_count > 0 and generated_scene_count >= scene_count:
            return "complete"
        return "partial"

    @classmethod
    def _completion_contract_from_assembled(
        cls,
        assembled: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "completion_status": cls._completion_status(
                int(assembled.get("scene_count") or 0),
                int(assembled.get("generated_scene_count") or 0),
            ),
            "missing_scene_ids": list(assembled.get("missing_scene_ids") or []),
            "scene_count": int(assembled.get("scene_count") or 0),
            "generated_scene_count": int(
                assembled.get("generated_scene_count") or 0
            ),
        }

    @staticmethod
    def _comparison_status(assembled_content: str, aggregate: dict[str, Any] | None) -> str:
        if aggregate is None:
            return "aggregate_missing"
        if aggregate["content"] == assembled_content:
            return "aggregate_matches_current"
        return "aggregate_differs_current"
