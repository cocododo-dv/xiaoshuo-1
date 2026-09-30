"""一次运行的生命周期：``run_scene`` / ``resume_after_selection`` 的入口、执行归属的设与清、终态分类、失败审计的
捕获 → 回滚 → 恢复与不可恢复时的围栏、已归档重放。

``Orchestrator`` 直接继承 ``RunLifecycleMixin``；``_capture_failure_audits`` / ``_restore_failure_audits`` 测试在实例上覆盖，
``_prepare_state_for_run`` 是 staticmethod（测试在类上调、读源码）。
"""

from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update

from novel_system.db.models import (
    AttemptTracker,
    FinalScene,
    LlmCall,
    SceneCard,
    SceneMemory,
    SceneRunState,
    utcnow,
)
from novel_system.services.author_instructions import normalize_author_note
from novel_system.services.author_state import compute_author_state
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMAccountingRejected
from novel_system.services.llm_task_runner import begin_llm_execution, end_llm_execution
from novel_system.services.scene_lookup import get_scene_or_404
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService, checkpoint_corrupt

_LOGGER = logging.getLogger(__name__)


class RunLifecycleMixin:
    def run_scene(
        self,
        scene_id: str,
        author_note: str | None = None,
        run_policy: str = "reliable",
        *,
        execution_id: str | None = None,
        run_job_id: str | None = None,
        lease_renewer=None,
    ) -> dict:
        author_note = normalize_author_note(author_note)
        get_scene_or_404(self.session, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        if state is None:
            state = SceneRunState(scene_id=scene_id, scene_status="ready")
            self.session.add(state)
            self.session.flush()

        effective_execution_id = execution_id or f"direct:{scene_id}:{uuid4().hex}"
        checkpoints = SceneRunCheckpointService(self.session)
        claim = checkpoints.acquire_execution(scene_id, effective_execution_id)
        if claim.last_node == "archived":
            self._execution_id = effective_execution_id
            self._run_job_id = run_job_id
            self._checkpoint_service = checkpoints
            try:
                self._assert_author_note_matches_bundle(
                    self._load_checkpoint_bundle(scene_id),
                    author_note,
                    scene_id=scene_id,
                )
                final_scene = self._load_archived_checkpoint(scene_id)
                if claim.status != "completed":
                    checkpoints.mark_completed(scene_id, effective_execution_id)
                    self.session.commit()
                return self._with_author_projection(
                    scene_id,
                    state,
                    {
                        "scene_status": state.scene_status,
                        "current_bundle_id": state.current_bundle_id,
                        "current_bundle_hash": state.current_bundle_hash,
                        "current_final_scene_row_id": final_scene.row_id,
                    },
                )
            finally:
                self._execution_id = None
                self._run_job_id = None
                self._checkpoint_service = None
        self._prepare_state_for_run(state, new_execution=not claim.resumed)
        state.run_policy = run_policy
        self.session.commit()

        self._execution_id = effective_execution_id
        self._run_job_id = run_job_id
        self._checkpoint_service = checkpoints
        self._lease_renewer = lease_renewer
        execution_token = begin_llm_execution(
            effective_execution_id,
            run_job_id=run_job_id,
            lease_renewer=lease_renewer,
        )
        try:
            self._raise_if_run_cancelled()
            result = self._run_scene_pipeline(
                scene_id,
                author_note=author_note,
                run_policy=run_policy,
            )
            if result.get("scene_status") in {
                "archived",
                "quality_warning_pending_acceptance",
            }:
                # Strict mode deliberately stops with a valid draft awaiting the
                # author's Q2/Q3 acceptance.  It is a successful execution
                # terminal, not a failure/retry checkpoint.
                checkpoints.mark_completed(scene_id, effective_execution_id)
            elif result.get("scene_status") == "awaiting_candidate_selection":
                checkpoints.mark_waiting_selection(scene_id, effective_execution_id)
            else:
                checkpoints.mark_failed(scene_id, effective_execution_id)
            self.session.commit()
            return result
        except DomainError as exc:
            if exc.code == "RUN_JOB_CANCELLED_BY_AUTHOR":
                checkpoints.mark_cancelled(scene_id, effective_execution_id)
                self.session.commit()
                raise
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            raise
        except Exception:
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            raise
        finally:
            end_llm_execution(execution_token)
            self._execution_id = None
            self._run_job_id = None
            self._checkpoint_service = None
            self._lease_renewer = None

    @staticmethod
    def _assert_author_note_matches_bundle(
        bundle: dict[str, Any],
        author_note: str | None,
        *,
        scene_id: str,
    ) -> None:
        expected_note = str(author_note or "").strip()
        actual_note = str(
            ((bundle.get("snapshot") or {}).get("inline_digests") or {}).get(
                "author_instruction"
            )
            or ""
        )
        if actual_note != expected_note:
            raise DomainError(
                "RUN_INPUT_MISMATCH",
                "author instruction differs from the frozen run checkpoint",
                status_code=409,
                details={"scene_id": scene_id, "field": "author_note"},
            )

    def _capture_failure_audits(
        self,
        scene_id: str,
        execution_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        self.session.flush()
        calls = (
            self.session.execute(
                select(LlmCall).where(
                    LlmCall.scene_id == scene_id,
                    LlmCall.execution_id == execution_id,
                )
            )
            .scalars()
            .all()
        )
        call_snapshots = [
            {
                column.name: deepcopy(getattr(call, column.name))
                for column in LlmCall.__table__.columns
            }
            for call in calls
        ]
        call_ids = {call.llm_call_id for call in calls}
        attempts = (
            self.session.execute(
                select(AttemptTracker).where(
                    AttemptTracker.scene_id == scene_id,
                    AttemptTracker.status == "failed",
                )
            )
            .scalars()
            .all()
        )
        attempt_snapshots: list[dict[str, Any]] = []
        for attempt in attempts:
            details = dict(attempt.details_json or {})
            llm_call_id = details.get("llm_call_id")
            if llm_call_id not in call_ids:
                continue
            attempt_snapshots.append(
                {
                    "scene_id": attempt.scene_id,
                    "chapter_id": attempt.chapter_id,
                    "step": attempt.step,
                    "status": attempt.status,
                    "source_bundle_id": attempt.source_bundle_id,
                    "details_json": deepcopy(details),
                    "created_at": attempt.created_at,
                }
            )
        return {"calls": call_snapshots, "attempts": attempt_snapshots}

    def _persist_failure_audits_or_fence(
        self,
        scene_id: str,
        execution_id: str,
        checkpoints: SceneRunCheckpointService,
    ) -> None:
        try:
            snapshots = self._capture_failure_audits(scene_id, execution_id)
        except Exception as exc:
            _LOGGER.exception(
                "failed to snapshot failure audit for scene %s execution %s",
                scene_id,
                execution_id,
            )
            self.session.rollback()
            self._persist_unrecoverable_execution_fence(
                scene_id,
                execution_id,
                phase="snapshot",
                cause=exc,
            )
            return

        self.session.rollback()
        try:
            self._restore_failure_audits(scene_id, execution_id, snapshots)
            checkpoints.mark_failed(scene_id, execution_id)
            self.session.commit()
        except Exception as exc:
            _LOGGER.exception(
                "failed to restore failure audit for scene %s execution %s",
                scene_id,
                execution_id,
            )
            self.session.rollback()
            self._persist_unrecoverable_execution_fence(
                scene_id,
                execution_id,
                phase="restore",
                cause=exc,
            )

    def _persist_unrecoverable_execution_fence(
        self,
        scene_id: str,
        execution_id: str,
        *,
        phase: str,
        cause: Exception,
    ) -> None:
        """Best-effort terminal fence when failure-audit durability is unknown."""
        try:
            state = self.session.get(SceneRunState, scene_id)
            if state is None:
                return
            self.session.refresh(state)
            if state.active_execution_id != execution_id:
                return
            if (
                state.run_execution_status == "cancelled"
                and state.run_checkpoint == "cancelled"
            ):
                return
            payload = (
                dict(state.run_checkpoint_json)
                if isinstance(state.run_checkpoint_json, dict)
                else {}
            )
            artifact_refs = payload.get("artifact_refs")
            artifact_hashes = payload.get("artifact_hashes")
            cancelled_payload = {
                **payload,
                "execution_id": execution_id,
                "node_key": "cancelled",
                "artifact_refs": (
                    artifact_refs if isinstance(artifact_refs, dict) else {}
                ),
                "artifact_hashes": (
                    artifact_hashes if isinstance(artifact_hashes, dict) else {}
                ),
                "cancelled_from_node": state.run_checkpoint,
                "cancelled_at": utcnow(),
                "unrecoverable_failure_audit": {
                    "phase": phase,
                    "error_type": type(cause).__name__,
                },
            }
            fenced = self.session.execute(
                update(SceneRunState)
                .where(
                    SceneRunState.scene_id == scene_id,
                    SceneRunState.active_execution_id == execution_id,
                    SceneRunState.run_execution_status.in_(("active", "failed")),
                )
                .values(
                    run_execution_status="cancelled",
                    run_checkpoint="cancelled",
                    run_checkpoint_json=cancelled_payload,
                )
                .execution_options(synchronize_session=False)
            )
            if fenced.rowcount != 1:
                self.session.rollback()
                return
            self.session.commit()
        except Exception:
            self.session.rollback()
            _LOGGER.exception(
                "failed to persist unrecoverable execution fence for scene %s execution %s",
                scene_id,
                execution_id,
            )

    def _restore_failure_audits(
        self,
        scene_id: str,
        execution_id: str,
        snapshots: dict[str, list[dict[str, Any]]],
    ) -> None:
        state = self.session.get(SceneRunState, scene_id)
        terminal_accounting_statuses = {
            "settled",
            "failed",
            "usage_exceeds_reservation",
        }
        restored_call_ids: set[str] = set()
        for call_snapshot in snapshots.get("calls", []):
            call_data = dict(call_snapshot)
            if (
                call_data.get("execution_id") != execution_id
                or call_data.get("scene_id") != scene_id
            ):
                continue
            llm_call_id = call_data["llm_call_id"]
            existing = self.session.get(LlmCall, llm_call_id)
            existing_status = (
                existing.accounting_status if existing is not None else None
            )
            existing_reserved = (
                int(existing.reserved_tokens or 0) if existing is not None else 0
            )
            existing_total = (
                int(existing.total_tokens or 0) if existing is not None else 0
            )
            snapshot_status = call_data.get("accounting_status")
            snapshot_total = int(call_data.get("total_tokens") or 0)
            if existing is None:
                self.session.add(
                    LlmCall(
                        scope_type=call_data.pop("scope_type", None),
                        scope_id=call_data.pop("scope_id", None),
                        **call_data,
                    )
                )
            else:
                for column in LlmCall.__table__.columns:
                    if column.primary_key:
                        continue
                    setattr(existing, column.name, deepcopy(call_data[column.name]))
            restored_call_ids.add(llm_call_id)

            if state is None or state.active_execution_id != execution_id:
                continue
            if snapshot_status in terminal_accounting_statuses:
                if existing_status == "reserved":
                    state.scene_tokens_reserved = max(
                        0,
                        int(state.scene_tokens_reserved or 0) - existing_reserved,
                    )
                if existing_status not in terminal_accounting_statuses:
                    state.scene_tokens_used = (
                        int(state.scene_tokens_used or 0) + snapshot_total
                    )
                elif snapshot_total != existing_total:
                    state.scene_tokens_used = max(
                        0,
                        int(state.scene_tokens_used or 0)
                        + snapshot_total
                        - existing_total,
                    )

        restored = 0
        restored_business_attempts = 0
        for attempt_snapshot in snapshots.get("attempts", []):
            attempt_data = dict(attempt_snapshot)
            details = dict(attempt_data.get("details_json") or {})
            llm_call_id = details.get("llm_call_id")
            if llm_call_id not in restored_call_ids:
                continue
            existing_attempts = (
                self.session.execute(
                    select(AttemptTracker).where(
                        AttemptTracker.scene_id == scene_id,
                        AttemptTracker.step == attempt_data["step"],
                        AttemptTracker.status == "failed",
                    )
                )
                .scalars()
                .all()
            )
            if any(
                (attempt.details_json or {}).get("llm_call_id") == llm_call_id
                for attempt in existing_attempts
            ):
                continue
            self.session.add(AttemptTracker(**attempt_data))
            restored += 1
            if details.get("business_attempt_consumed", True) is not False:
                restored_business_attempts += 1
        if restored_business_attempts:
            if state is not None and state.active_execution_id == execution_id:
                state.total_attempt_count = (
                    int(state.total_attempt_count or 0) + restored_business_attempts
                )
        self.session.flush()

    def _load_archived_checkpoint(self, scene_id: str) -> FinalScene:
        state = self._active_checkpoint_state()
        scene = self.session.get(SceneCard, scene_id)
        bundle = self._load_checkpoint_bundle(scene_id)
        if scene is None:
            raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
        selected_style = self._load_selected_style_checkpoint(scene_id)
        _soft_qc, soft_generation = self._load_soft_qc_checkpoint(
            scene_id,
            selected_style_generation=selected_style,
        )
        final_scene, _near_final = self._load_near_final_checkpoint(
            scene=scene,
            bundle=bundle,
            source_generation=soft_generation,
        )
        refs = (state.run_checkpoint_json or {}).get("artifact_refs", {})
        carry_notes = list(refs.get("carry_notes") or [])
        if self._json_hash(carry_notes) != self._checkpoint_hash("carry_notes"):
            raise checkpoint_corrupt("archived carry notes hash mismatch")
        contract = self.execution_contract_service.get_or_create(
            scene_id,
            actor_ref="orchestrator",
        )
        self._validate_archive_core_checkpoint(
            scene=scene,
            final_scene=final_scene,
            carry_notes=carry_notes,
            allow_terminal=True,
        )
        self._validate_archive_rule_events_checkpoint(scene)
        self._validate_archive_prose_checkpoint(scene, contract)
        self._validate_archive_vector_product(scene, final_scene)
        self._validate_archive_chapter_product(scene)
        self._validate_archive_volume_product(scene)
        self._validate_archive_chapter_evaluation_product(scene)
        self._validate_archive_drift_product(scene)
        manifest = refs.get("archive_manifest")
        expected_manifest = self._archive_manifest()
        if manifest != expected_manifest or self._json_hash(
            manifest
        ) != self._checkpoint_hash("archive_manifest"):
            raise checkpoint_corrupt("archived product manifest is incomplete or changed")
        memory_id = self._checkpoint_artifact(
            "scene_memory_row_id", expected_node_at_least="archived"
        )
        memory = self._require_checkpoint_row(SceneMemory, memory_id)
        if (
            state.run_checkpoint != "archived"
            or state.scene_status != "archived"
            or state.current_final_scene_row_id != final_scene.row_id
            or state.current_bundle_id != bundle["bundle_id"]
            or state.current_bundle_hash != bundle["bundle_snapshot_hash"]
            or final_scene.status != "archived"
            or memory.scene_id != scene_id
            or memory.chapter_id != scene.chapter_id
            or memory.final_scene_row_id != final_scene.row_id
            or memory.source_bundle_id != bundle["bundle_id"]
            or memory.content != final_scene.content
            or memory.active_flag != 1
            or memory.runtime_eligible != 1
        ):
            raise checkpoint_corrupt("archived checkpoint product graph is inconsistent")
        return final_scene

    @staticmethod
    def _prepare_state_for_run(state: SceneRunState, *, new_execution: bool) -> None:
        if not new_execution:
            return
        state.current_bundle_id = None
        state.current_bundle_hash = None
        state.current_neutral_draft_row_id = None
        state.current_style_draft_row_id = None
        state.current_final_scene_row_id = None
        state.current_human_review_event_id = None
        state.current_qc_report_id = None
        # This counter describes the current run's soft-patch loop; lifecycle
        # provider/total attempt counters and latest_valid remain cumulative.
        state.soft_patch_count = 0

    def _with_author_projection(
        self, scene_id: str, state: SceneRunState, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Wave 2 项 5：run 结果（含全部早退路径）统一附 §5.3 作者状态契约。"""
        projection = compute_author_state(self.session, scene_id, state)
        return {**payload, **projection}

    def resume_after_selection(
        self,
        scene_id: str,
        *,
        execution_id: str | None = None,
        run_job_id: str | None = None,
        lease_renewer=None,
    ) -> dict:
        state = self.session.get(SceneRunState, scene_id)
        previous_execution_id = state.active_execution_id if state is not None else None
        if not previous_execution_id:
            raise DomainError(
                "RESUME_EXECUTION_NOT_FOUND",
                "candidate selection has no durable execution to resume",
                status_code=409,
                details={"scene_id": scene_id},
            )
        effective_execution_id = (
            execution_id or f"direct-selection-resume:{scene_id}:{uuid4().hex}"
        )
        checkpoints = SceneRunCheckpointService(self.session)
        checkpoints.acquire_selection_resume(scene_id, effective_execution_id)

        self._execution_id = effective_execution_id
        self._run_job_id = run_job_id
        self._checkpoint_service = checkpoints
        self._lease_renewer = lease_renewer
        execution_token = begin_llm_execution(
            effective_execution_id,
            run_job_id=run_job_id,
            lease_renewer=lease_renewer,
        )
        try:
            # This endpoint always owns the post-selection continuation. A failed
            # retry may already be at soft/near-final sub-checkpoints; routing it
            # through the ordinary run pipeline would recreate the selection gate.
            result = self._resume_after_selection_pipeline(scene_id)
            if result.get("scene_status") == "archived":
                checkpoints.mark_completed(scene_id, effective_execution_id)
            else:
                checkpoints.mark_failed(scene_id, effective_execution_id)
            self.session.commit()
            return result
        except LLMAccountingRejected as exc:
            # Candidate selection resume is synchronous, unlike run/jobs. A
            # lifecycle boundary is an expected recoverable stop: return a
            # durable payload so the UI can request an audited top-up and retry
            # this same post-selection pipeline, never the ordinary prefix.
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            state = self.session.get(SceneRunState, scene_id)
            return self._with_author_projection(
                scene_id,
                state,
                {
                    "scene_status": getattr(state, "scene_status", None),
                    "lifecycle_budget_block": {
                        "code": getattr(
                            exc, "code", "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED"
                        ),
                        "message": str(exc),
                        "resume_mode": "selection",
                    },
                },
            )
        except Exception:
            self._persist_failure_audits_or_fence(
                scene_id,
                effective_execution_id,
                checkpoints,
            )
            raise
        finally:
            end_llm_execution(execution_token)
            self._execution_id = None
            self._run_job_id = None
            self._checkpoint_service = None
            self._lease_renewer = None
