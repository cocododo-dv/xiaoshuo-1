from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    ChapterRollingNote,
    FinalScene,
    SceneMemory,
    SceneRunState,
)
from novel_system.services.chapter_state import ensure_chapter_state
from novel_system.services.errors import DomainError
from novel_system.services.final_text_gate import FinalTextGateService

_LOGGER = logging.getLogger(__name__)


class Archiver:
    def __init__(self, session: Session) -> None:
        self.session = session

    def archive_final_scene(
        self,
        scene_id: str,
        final_scene_row_id: str,
        qc_report_id: str | None = None,
        *,
        carry_notes_json: list[dict[str, Any]] | None = None,
        execution_id: str | None = None,
        finalize_scene_status: bool = True,
        author_confirmed_final: bool = False,
        accepted_warning_codes: list[str] | None = None,
        observe_style_drift: bool = True,
    ) -> dict:
        final_scene = self.session.get(FinalScene, final_scene_row_id)
        state = self.session.get(SceneRunState, scene_id)
        if final_scene is None or state is None or final_scene.scene_id != scene_id:
            raise ValueError("archive target is missing or detached from scene state")
        # Every archive path converges here. Validate the exact FinalScene text,
        # not whichever upstream draft happened to be checked previously.
        final_text_gate = FinalTextGateService(self.session).evaluate(
            scene_id=scene_id,
            content=final_scene.content,
            source_bundle_id=final_scene.source_bundle_id,
            author_confirmed_final=author_confirmed_final,
            accepted_warning_codes=accepted_warning_codes,
        )
        FinalTextGateService.raise_if_not_archivable(final_text_gate, scene_id=scene_id)
        actual_content_hash = str(final_text_gate["content_hash"])
        persisted_content_hash = (final_scene.content_hash or "").strip()
        if persisted_content_hash and persisted_content_hash != actual_content_hash:
            raise DomainError(
                "FINAL_SCENE_CONTENT_HASH_MISMATCH",
                "stored final-scene content hash does not match the exact text being archived",
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "final_scene_row_id": final_scene_row_id,
                    "stored_content_hash": persisted_content_hash,
                    "actual_content_hash": actual_content_hash,
                    "final_text_gate": final_text_gate,
                },
            )
        final_scene.content_hash = actual_content_hash
        # 目录冷启动章可能没有状态行（审计 P-1）：缺行补建而不是 None 解引用崩掉整跑
        chapter_state = ensure_chapter_state(self.session, final_scene.chapter_id)

        memory_row_id = _scene_memory_row_id(scene_id, final_scene_row_id)
        memory = self.session.get(SceneMemory, memory_row_id)
        if memory is None:
            for existing_memory in self.session.execute(
                select(SceneMemory).where(
                    SceneMemory.scene_id == scene_id,
                    SceneMemory.active_flag == 1,
                )
            ).scalars().all():
                existing_memory.active_flag = 0
                existing_memory.runtime_eligible = 0
            memory = SceneMemory(
                row_id=memory_row_id,
                scene_id=scene_id,
                chapter_id=final_scene.chapter_id,
                content=final_scene.content,
                carry_notes_json=carry_notes_json or [],
                source_bundle_id=final_scene.source_bundle_id,
                final_scene_row_id=final_scene_row_id,
                active_flag=1,
                runtime_eligible=1,
                runtime_eligibility_basis="direct_read",
            )
            self.session.add(memory)
        elif (
            memory.scene_id != scene_id
            or memory.chapter_id != final_scene.chapter_id
            or memory.content != final_scene.content
            or memory.carry_notes_json != (carry_notes_json or [])
            or memory.source_bundle_id != final_scene.source_bundle_id
            or memory.final_scene_row_id != final_scene_row_id
        ):
            raise ValueError("existing archive memory conflicts with the requested final scene")
        else:
            for existing_memory in self.session.execute(
                select(SceneMemory).where(
                    SceneMemory.scene_id == scene_id,
                    SceneMemory.active_flag == 1,
                    SceneMemory.row_id != memory_row_id,
                )
            ).scalars().all():
                existing_memory.active_flag = 0
                existing_memory.runtime_eligible = 0
            memory.active_flag = 1
            memory.runtime_eligible = 1
            memory.runtime_eligibility_basis = "direct_read"

        rolling = self.session.execute(
            select(ChapterRollingNote).where(ChapterRollingNote.scene_id == scene_id)
        ).scalars().first()
        if rolling is None:
            rolling = ChapterRollingNote(
                row_id=f"rolling_{scene_id}",
                scene_id=scene_id,
                chapter_id=final_scene.chapter_id,
                source_scene_memory_row_id=memory_row_id,
                note_text=final_scene.content,
                revision_no=1,
            )
            chapter_state.chapter_passed_scene_count += 1
            self.session.add(rolling)
        else:
            if (
                rolling.source_scene_memory_row_id != memory_row_id
                or rolling.note_text != final_scene.content
            ):
                rolling.source_scene_memory_row_id = memory_row_id
                rolling.note_text = final_scene.content
                rolling.revision_no += 1

        # 治理 §5.2 状态词表统一：归档态由本事务写入的权威状态表示，
        # 下游（章节聚合/回放）不再依赖 approved/near_final_ready 的字符串巧合
        final_scene.status = "archived"
        state.current_final_scene_row_id = final_scene.row_id
        if finalize_scene_status:
            state.scene_status = "archived"
        # 正文归档与正史完成是两个不同的状态。新终稿先进入待抽取，只有
        # FactCandidate 经裁决并形成 CanonCommit 后才恢复 narrative_sync=synced。
        # 无 project 所属的历史单元数据保留兼容，但正式作品必须进入该闭环。
        from novel_system.services.canon_continuity import CanonContinuityService

        try:
            canon_continuity = CanonContinuityService(self.session).mark_archive_pending(
                final_scene.row_id
            )
        except DomainError as exc:
            if exc.code != "SCENE_PROJECT_REQUIRED":
                raise
            canon_continuity = {
                "status": "unavailable",
                "complete": False,
                "reason": "projectless_legacy_scene",
            }
        archive_attempts = self.session.execute(
            select(AttemptTracker).where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "archive",
                AttemptTracker.status == "completed",
            )
        ).scalars().all()
        matching_attempts = [
            attempt
            for attempt in archive_attempts
            if (attempt.details_json or {}).get("final_scene_row_id") == final_scene_row_id
            and (attempt.details_json or {}).get("execution_id") == execution_id
            and bool(
                ((attempt.details_json or {}).get("final_text_gate") or {}).get(
                    "author_confirmed_final"
                )
            )
            == bool(author_confirmed_final)
            and sorted(
                (((attempt.details_json or {}).get("final_text_gate") or {}).get("content_safety") or {}).get(
                    "acknowledged_codes"
                )
                or []
            )
            == sorted((final_text_gate.get("content_safety") or {}).get("acknowledged_codes") or [])
        ]
        if len(matching_attempts) > 1:
            raise ValueError("archive attempt audit is not unique")
        if matching_attempts:
            archive_attempt = matching_attempts[0]
        else:
            archive_attempt = AttemptTracker(
                scene_id=scene_id,
                chapter_id=final_scene.chapter_id,
                step="archive",
                status="completed",
                source_bundle_id=final_scene.source_bundle_id,
                details_json={
                    "final_scene_row_id": final_scene_row_id,
                    "qc_report_id": qc_report_id,
                    "execution_id": execution_id,
                    "final_text_gate": _gate_audit_summary(final_text_gate),
                },
            )
            self.session.add(archive_attempt)
        self.session.flush()

        # 2026-09-22 风格参考优先:每一条归档路径都做确定性声音漂移读数(W6)。此前只有编排器自己的
        # 归档检查点做,而起草台「采用」(adopt-current)与成稿中心走的是本函数——真实项目两场归档
        # 0 条 style_drift_observed,跨场校准从未运行。检查点路径自己读数,传 False 免得读两遍。
        style_drift: dict[str, Any] | None = None
        if observe_style_drift:
            style_drift = self._observe_style_drift(scene_id, execution_id=execution_id)

        return {
            "style_drift": style_drift,
            "scene_memory_row_id": memory_row_id,
            "chapter_rolling_note_row_id": rolling.row_id,
            "archive_attempt_id": archive_attempt.attempt_id,
            "scene_status": state.scene_status,
            "safe_to_archive": bool(final_text_gate.get("safe_to_archive")),
            "literary_warnings_unresolved": bool(
                final_text_gate.get("literary_warnings_unresolved")
            ),
            "author_confirmed_final": bool(
                final_text_gate.get("author_confirmed_final")
            ),
            "finality": dict(final_text_gate.get("finality") or {}),
            "final_text_gate": final_text_gate,
            "canon_continuity": canon_continuity,
        }


    def _observe_style_drift(
        self, scene_id: str, *, execution_id: str | None
    ) -> dict[str, Any] | None:
        """归档期漂移读数;任何异常吞掉记 warning,绝不阻断归档。"""
        try:
            from novel_system.db.models import SceneCard
            from novel_system.services.scene_archive_effects import SceneArchiveEffects

            scene = self.session.get(SceneCard, scene_id)
            if scene is None:
                return None
            effects = SceneArchiveEffects(
                self.session, None, execution_id=execution_id, run_job_id=None
            )
            return effects._detect_and_store_style_drift(scene)
        except Exception as exc:  # noqa: BLE001 — 读数失败不影响归档
            _LOGGER.warning(
                "style drift observation skipped on archive for scene %s", scene_id, exc_info=True
            )
            return {"outcome": "degraded", "error_code": exc.__class__.__name__}


def _scene_memory_row_id(scene_id: str, final_scene_row_id: str) -> str:
    final_prefix = f"final_scene_{scene_id}"
    if final_scene_row_id.startswith(final_prefix):
        return f"scene_memory_{scene_id}{final_scene_row_id[len(final_prefix):]}"
    return f"scene_memory_{scene_id}_{final_scene_row_id}"


def _gate_audit_summary(result: dict[str, Any]) -> dict[str, Any]:
    literary = result.get("literary_quality") or {}
    longform = result.get("longform_contract") or {}
    content_safety = result.get("content_safety") or {}
    return {
        "schema_version": result.get("schema_version"),
        "content_hash": result.get("content_hash"),
        "source_bundle_id": result.get("source_bundle_id"),
        "archivable": bool(result.get("archivable")),
        "safe_to_archive": bool(
            result.get("safe_to_archive", result.get("archivable"))
        ),
        "literary_warnings_unresolved": bool(
            result.get("literary_warnings_unresolved")
        ),
        "author_confirmed_final": bool(result.get("author_confirmed_final")),
        "auto_promotable": bool(result.get("auto_promotable")),
        "archive_blockers": list(result.get("archive_blockers") or []),
        "promotion_blockers": list(result.get("promotion_blockers") or []),
        "content_safety": {
            "schema_version": content_safety.get("schema_version"),
            "mode": content_safety.get("mode"),
            "acknowledged_codes": list(content_safety.get("acknowledged_codes") or []),
            "findings": [
                {
                    "code": item.get("code"),
                    "severity": item.get("severity"),
                    "review_required": bool(item.get("review_required")),
                    "acknowledged": bool(item.get("acknowledged")),
                    "blocking": bool(item.get("blocking")),
                }
                for item in (content_safety.get("findings") or [])
                if isinstance(item, dict)
            ],
        },
        "longform_contract": {
            "available": bool(longform.get("available")),
            "contract_id": longform.get("contract_id"),
            "contract_status": longform.get("contract_status"),
            "provenance": dict(longform.get("provenance") or {}),
            "bundle_integrity": dict(longform.get("bundle_integrity") or {}),
            "key_hits": list(longform.get("key_hits") or []),
            "waivers": list(longform.get("waivers") or []),
            "unresolved": list(longform.get("unresolved") or []),
            "blockers": list(longform.get("blockers") or []),
        },
        "literary_scores": dict(literary.get("scores") or {}),
        "risky_dimensions": list(literary.get("risky_dimensions") or []),
    }
