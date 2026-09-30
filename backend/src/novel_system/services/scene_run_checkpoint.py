from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall, SceneRunState, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import recover_incomplete_call


RUN_CHECKPOINT_ORDER = (
    "budget_ready",
    "planning_ready",
    "bundle_ready",
    "neutral_ready",
    "hard_qc_ready",
    "style_ready",
    "selection_wait",
    "soft_qc_ready",
    "near_final_ready",
    "archived",
)

_TERMINAL_EXECUTION_STATUSES = frozenset({"failed", "completed", "cancelled"})


def checkpoint_corrupt(message: str, *, details: dict[str, Any] | None = None) -> DomainError:
    """``RUN_CHECKPOINT_CORRUPT``（409）：检查点与库里的产物、账本或执行归属对不上——续跑不再往下走。"""
    return DomainError("RUN_CHECKPOINT_CORRUPT", message, status_code=409, details=details)


def idempotency_execution_id(idempotency_key: str) -> str:
    return f"idempotency:{idempotency_key}"


def scene_job_execution_id(job_id: str) -> str:
    return job_id


def chapter_scene_execution_id(chapter_job_id: str, scene_id: str) -> str:
    return f"{chapter_job_id}:{scene_id}"


@dataclass(frozen=True)
class ExecutionCheckpoint:
    execution_id: str
    resumed: bool
    last_node: str | None
    next_node: str | None
    checkpoint_json: dict[str, Any]
    status: str


class SceneRunCheckpointService:
    """Owns the durable scene execution fence and macro-node cursor.

    Methods deliberately flush but never commit.  Callers can therefore persist
    a product row and the checkpoint which points at it in one transaction.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def acquire_execution(self, scene_id: str, execution_id: str) -> ExecutionCheckpoint:
        state = self._state(scene_id, refresh=True)
        payload = self._checkpoint_payload(state)
        superseded = set(payload.get("superseded_execution_ids") or [])
        if execution_id in superseded:
            raise self._superseded(execution_id, state.active_execution_id)

        current = state.active_execution_id
        status = state.run_execution_status
        if current == execution_id:
            self._validate_checkpoint(state, payload)
            if status == "completed":
                return self._result(state, resumed=True)
            if status == "waiting_selection":
                raise DomainError(
                    "RUN_SELECTION_WAIT",
                    "scene execution is waiting for an explicit selection resume",
                    status_code=409,
                    details={"execution_id": execution_id},
                )
            if status == "cancelled":
                raise DomainError(
                    "RUN_EXECUTION_CANCELLED",
                    "scene execution was cancelled",
                    status_code=409,
                    details={"execution_id": execution_id},
                )
            if status != "active":
                claimed = self.session.execute(
                    update(SceneRunState)
                    .where(
                        SceneRunState.scene_id == scene_id,
                        SceneRunState.active_execution_id == execution_id,
                        SceneRunState.run_execution_status == status,
                    )
                    .values(run_execution_status="active")
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    return self._raise_lost_claim(scene_id, execution_id)
                self.session.flush()
                state = self._state(scene_id, refresh=True)
            return self._result(state, resumed=True)

        if current is not None and status not in _TERMINAL_EXECUTION_STATUSES:
            raise DomainError(
                "RUN_EXECUTION_IN_PROGRESS",
                "another execution currently owns this scene run",
                status_code=409,
                details={"active_execution_id": current, "requested_execution_id": execution_id},
            )

        new_superseded = list(payload.get("superseded_execution_ids") or [])
        if current is not None and current not in new_superseded:
            new_superseded.append(current)
        new_payload: dict[str, Any] = {
            "execution_id": execution_id,
            "superseded_execution_ids": new_superseded,
        }
        conditions = [SceneRunState.scene_id == scene_id]
        if current is None:
            conditions.append(SceneRunState.active_execution_id.is_(None))
        else:
            conditions.extend(
                (
                    SceneRunState.active_execution_id == current,
                    SceneRunState.run_execution_status == status,
                )
            )
        claimed = self.session.execute(
            update(SceneRunState)
            .where(*conditions)
            .values(
                active_execution_id=execution_id,
                run_execution_status="active",
                run_checkpoint=None,
                run_checkpoint_json=new_payload,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            return self._raise_lost_claim(scene_id, execution_id)
        self.session.flush()
        return self._result(self._state(scene_id, refresh=True), resumed=False)

    def acquire_selection_resume(self, scene_id: str, execution_id: str) -> ExecutionCheckpoint:
        """Atomically hand a selection/post-selection checkpoint to the resume owner."""
        state = self._state(scene_id, refresh=True)
        payload = self._checkpoint_payload(state)
        self._validate_checkpoint(state, payload)
        current = state.active_execution_id
        status = state.run_execution_status

        if current == execution_id:
            if status == "active":
                return self._result(state, resumed=True)
            if status == "failed":
                return self.acquire_execution(scene_id, execution_id)
            if status == "cancelled":
                raise DomainError(
                    "RUN_EXECUTION_CANCELLED",
                    "selection resume execution was cancelled",
                    status_code=409,
                    details={"execution_id": execution_id},
                )
            raise DomainError(
                "RUN_SELECTION_RESUME_IN_PROGRESS",
                "selection resume execution is not claimable",
                status_code=409,
                details={"execution_id": execution_id, "status": status},
            )
        checkpoint = state.run_checkpoint
        post_selection_retry = (
            status == "failed"
            and checkpoint in RUN_CHECKPOINT_ORDER
            and RUN_CHECKPOINT_ORDER.index(checkpoint) >= RUN_CHECKPOINT_ORDER.index("selection_wait")
            and bool(payload.get("selection_origin_execution_id"))
        )
        if not (
            (checkpoint == "selection_wait" and status in {"waiting_selection", "failed"})
            or post_selection_retry
        ):
            raise DomainError(
                "RUN_EXECUTION_IN_PROGRESS",
                "selection checkpoint is already owned by another execution",
                status_code=409,
                details={"active_execution_id": current, "requested_execution_id": execution_id},
            )

        superseded = list(payload.get("superseded_execution_ids") or [])
        if current is not None and current not in superseded:
            superseded.append(current)
        artifact_lineage = list(payload.get("artifact_execution_lineage_ids") or [])
        if current is not None and current not in artifact_lineage:
            artifact_lineage.append(current)
        next_payload = {
            **payload,
            "execution_id": execution_id,
            "selection_parent_execution_id": current,
            "selection_origin_execution_id": payload.get("selection_origin_execution_id") or current,
            "superseded_execution_ids": superseded,
            "artifact_execution_lineage_ids": artifact_lineage,
        }
        handed_off = self.session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                SceneRunState.active_execution_id == current,
                SceneRunState.run_execution_status == status,
                SceneRunState.run_checkpoint == checkpoint,
            )
            .values(
                active_execution_id=execution_id,
                run_execution_status="active",
                run_checkpoint_json=next_payload,
            )
            .execution_options(synchronize_session=False)
        )
        if handed_off.rowcount != 1:
            self.session.rollback()
            latest = self._state(scene_id, refresh=True)
            raise DomainError(
                "RUN_EXECUTION_IN_PROGRESS",
                "another selection resume owner won the checkpoint handoff",
                status_code=409,
                details={"active_execution_id": latest.active_execution_id},
            )
        self.session.flush()
        return self._result(self._state(scene_id, refresh=True), resumed=True)

    def acquire_budget_resume(
        self,
        scene_id: str,
        execution_id: str,
        *,
        expected_parent_execution_id: str,
    ) -> ExecutionCheckpoint:
        """Handoff a failed macro checkpoint to a fresh server-owned execution."""
        state = self._state(scene_id, refresh=True)
        payload = self._checkpoint_payload(state)
        self._validate_checkpoint(state, payload)
        current = state.active_execution_id
        if (
            current != expected_parent_execution_id
            or state.run_execution_status != "failed"
            or not state.run_checkpoint
        ):
            raise DomainError(
                "RUN_BUDGET_RESUME_UNAVAILABLE",
                "budget checkpoint is no longer owned by the expected failed execution",
                status_code=409,
                details={
                    "active_execution_id": current,
                    "expected_parent_execution_id": expected_parent_execution_id,
                    "status": state.run_execution_status,
                },
            )
        superseded = list(payload.get("superseded_execution_ids") or [])
        if current not in superseded:
            superseded.append(current)
        artifact_lineage = list(payload.get("artifact_execution_lineage_ids") or [])
        if current not in artifact_lineage:
            artifact_lineage.append(current)
        next_payload = {
            **payload,
            "execution_id": execution_id,
            "budget_resume_parent_execution_id": current,
            "superseded_execution_ids": superseded,
            "artifact_execution_lineage_ids": artifact_lineage,
        }
        handed_off = self.session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                SceneRunState.active_execution_id == current,
                SceneRunState.run_execution_status == "failed",
            )
            .values(
                active_execution_id=execution_id,
                run_execution_status="active",
                run_checkpoint_json=next_payload,
            )
            .execution_options(synchronize_session=False)
        )
        if handed_off.rowcount != 1:
            self.session.rollback()
            latest = self._state(scene_id, refresh=True)
            raise DomainError(
                "RUN_EXECUTION_IN_PROGRESS",
                "another budget resume owner won the checkpoint handoff",
                status_code=409,
                details={"active_execution_id": latest.active_execution_id},
            )
        self.session.flush()
        return self._result(self._state(scene_id, refresh=True), resumed=True)

    def save_checkpoint(
        self,
        *,
        scene_id: str,
        execution_id: str,
        node_key: str,
        sub_index: int | None = None,
        artifact_refs: dict[str, Any] | None = None,
        artifact_hashes: dict[str, str] | None = None,
        strategy: str | None = None,
        branch: str | None = None,
    ) -> ExecutionCheckpoint:
        if node_key not in RUN_CHECKPOINT_ORDER:
            raise checkpoint_corrupt(f"unknown checkpoint node: {node_key}")
        state = self._state(scene_id, refresh=True)
        if state.active_execution_id != execution_id:
            raise self._superseded(execution_id, state.active_execution_id)
        if state.run_execution_status != "active":
            raise checkpoint_corrupt("checkpoint cannot advance a non-active execution")

        previous = self._checkpoint_payload(state)
        self._validate_checkpoint(state, previous)
        previous_node = state.run_checkpoint
        previous_sub_index = previous.get("sub_index")
        self._validate_advance(
            previous_node=previous_node,
            previous_sub_index=previous_sub_index,
            node_key=node_key,
            sub_index=sub_index,
        )

        cumulative_refs = dict(previous.get("artifact_refs") or {})
        cumulative_refs.update(artifact_refs or {})
        cumulative_hashes = dict(previous.get("artifact_hashes") or {})
        cumulative_hashes.update(artifact_hashes or {})
        payload: dict[str, Any] = {
            "execution_id": execution_id,
            "node_key": node_key,
            "sub_index": sub_index,
            "artifact_refs": cumulative_refs,
            "artifact_hashes": cumulative_hashes,
            "strategy": strategy if strategy is not None else previous.get("strategy"),
            "branch": branch if branch is not None else previous.get("branch"),
            "superseded_execution_ids": list(previous.get("superseded_execution_ids") or []),
            "artifact_execution_lineage_ids": list(previous.get("artifact_execution_lineage_ids") or []),
            "selection_parent_execution_id": previous.get("selection_parent_execution_id"),
            "selection_origin_execution_id": previous.get("selection_origin_execution_id"),
            "budget_resume_parent_execution_id": previous.get("budget_resume_parent_execution_id"),
        }
        state.run_checkpoint = node_key
        state.run_checkpoint_json = payload
        self.session.flush()
        return self._result(state, resumed=True)

    def reconcile_step_output(
        self,
        *,
        scene_id: str,
        execution_id: str,
        execution_step_key: str,
        output_exists: bool,
        allow_local_rejected_output: bool = False,
        ledger_scope: Literal["scene", "chapter"] = "scene",
    ) -> str:
        """步位产物与它的账本行对账。``ledger_scope="chapter"``：这一步的调用记在章上（章级评审），账本行不带场景。"""
        effective_scene_id = scene_id if ledger_scope == "scene" else None
        calls = self.session.execute(
            select(LlmCall)
            .where(
                LlmCall.scene_id == effective_scene_id,
                LlmCall.execution_id == execution_id,
                LlmCall.execution_step_key == execution_step_key,
            )
            .order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())
        ).scalars().all()
        if not calls:
            if output_exists:
                raise checkpoint_corrupt("checkpoint output has no execution-step ledger row")
            return "retry"

        if not output_exists:
            # Recover every provisional parent before classifying the step. A
            # dispatched reserved row is itself blocking, but raising before
            # recovery would leak its reservation and leave accounting counters
            # stale. Undispatched siblings must also be released in this pass.
            for call in calls:
                if call.accounting_status == "reserved":
                    recover_incomplete_call(self.session, call.llm_call_id)
            self.session.expire_all()
            calls = self.session.execute(
                select(LlmCall)
                .where(
                    LlmCall.scene_id == effective_scene_id,
                    LlmCall.execution_id == execution_id,
                    LlmCall.execution_step_key == execution_step_key,
                )
                .order_by(LlmCall.created_at.asc(), LlmCall.llm_call_id.asc())
            ).scalars().all()

        blocking_calls = [
            call
            for call in calls
            if call.request_dispatched_at is not None
            or call.accounting_status
            in {"settled", "failed", "usage_exceeds_reservation"}
        ]
        if output_exists:
            locally_rejected_calls = [
                call
                for call in calls
                if call.accounting_status == "rejected"
                and call.request_dispatched_at is None
            ]
            valid_local_degraded = (
                allow_local_rejected_output
                and not blocking_calls
                and bool(locally_rejected_calls)
                and all(
                    call.request_dispatched_at is None
                    and call.accounting_status in {"rejected", "released"}
                    for call in calls
                )
            )
            if len(blocking_calls) != 1 and not valid_local_degraded:
                raise checkpoint_corrupt(
                    "checkpoint output must resolve to exactly one dispatched or explicitly local-degraded ledger row"
                )
            return "complete"

        if blocking_calls:
            call = blocking_calls[0]
            raise DomainError(
                "RUN_CHECKPOINT_OUTPUT_MISSING",
                "provider execution was dispatched/settled but its checkpoint output is missing",
                status_code=409,
                details={
                    "llm_call_id": call.llm_call_id,
                    "execution_id": execution_id,
                    "execution_step_key": execution_step_key,
                    "blocking_ledger_rows": len(blocking_calls),
                },
            )

        return "retry"

    def mark_failed(self, scene_id: str, execution_id: str) -> None:
        self._mark_terminal(scene_id, execution_id, "failed")

    def mark_completed(self, scene_id: str, execution_id: str) -> None:
        self._mark_terminal(scene_id, execution_id, "completed")

    def mark_cancelled(self, scene_id: str, execution_id: str) -> None:
        state = self._state(scene_id, refresh=True)
        payload = self._checkpoint_payload(state)
        self._validate_checkpoint(state, payload)
        if state.active_execution_id != execution_id:
            raise self._superseded(execution_id, state.active_execution_id)
        previous_status = state.run_execution_status
        if previous_status == "cancelled" and state.run_checkpoint == "cancelled":
            return
        if previous_status in {
            "usage_exceeds_reservation",
            "accounting_integrity_blocked",
        }:
            # Author cancellation may terminate the owning job, but must never
            # erase a durable accounting safety fence.  Keeping this checkpoint
            # blocks a future execution until the accounting incident is repaired.
            return
        if previous_status not in {
            "active",
            "failed",
            "completed",
            "waiting_selection",
        }:
            raise checkpoint_corrupt(
                f"cannot mark execution cancelled from {previous_status}"
            )
        cancelled_payload = {
            **payload,
            "execution_id": execution_id,
            "node_key": "cancelled",
            "cancelled_from_node": state.run_checkpoint,
            "cancelled_at": utcnow(),
        }
        changed = self.session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                SceneRunState.active_execution_id == execution_id,
                SceneRunState.run_execution_status == previous_status,
            )
            .values(
                run_execution_status="cancelled",
                run_checkpoint="cancelled",
                run_checkpoint_json=cancelled_payload,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            state = self._state(scene_id, refresh=True)
            if state.active_execution_id != execution_id:
                raise self._superseded(execution_id, state.active_execution_id)
            if state.run_execution_status != "cancelled" or state.run_checkpoint != "cancelled":
                raise checkpoint_corrupt(
                    f"cannot mark execution cancelled from {state.run_execution_status}"
                )
        self.session.flush()

    def finalize_after_author_archive(self, scene_id: str) -> bool:
        """作者采纳归档后，把无主执行残留收敛为终态 archived 视图。

        adopt-current 不持有 execution：上一次执行留下的
        run_execution_status=failed / run_checkpoint=soft_qc_ready 在归档后
        仍是数据库真值，会误导运维与后续恢复判断（C2 证据点名的状态一致性
        债务）。只收敛无主终态执行（failed/cancelled/completed）；活跃执行
        （active/waiting_selection）有 owner 不得抢占，会计安全栅栏
        （usage_exceeds_reservation/accounting_integrity_blocked）在事故修复
        前必须保留阻断，二者都不收敛。原状态/断点写入 checkpoint JSON 审计。
        """
        state = self._state(scene_id, refresh=True)
        current = state.active_execution_id
        status = state.run_execution_status
        if current is None or status not in _TERMINAL_EXECUTION_STATUSES:
            return False
        if status == "completed" and state.run_checkpoint == "archived":
            return False
        try:
            payload = self._checkpoint_payload(state)
        except DomainError:
            # 残留 JSON 已损坏也不阻断归档收敛——以最小 payload 重建终态视图
            payload = {}
        finalized_payload = {
            **payload,
            "execution_id": current,
            "node_key": "archived",
            "finalized_by": "author_adoption",
            "finalized_from_status": status,
            "finalized_from_node": state.run_checkpoint,
            "finalized_at": utcnow(),
        }
        changed = self.session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                SceneRunState.active_execution_id == current,
                SceneRunState.run_execution_status == status,
            )
            .values(
                run_execution_status="completed",
                run_checkpoint="archived",
                run_checkpoint_json=finalized_payload,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            # 并发下有新执行刚完成 claim —— 残留已被接管，不再收敛
            return False
        self.session.flush()
        return True

    def mark_waiting_selection(self, scene_id: str, execution_id: str) -> None:
        state = self._state(scene_id, refresh=True)
        if state.run_checkpoint != "selection_wait":
            raise checkpoint_corrupt("selection wait status requires a selection_wait checkpoint")
        changed = self.session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                SceneRunState.active_execution_id == execution_id,
                SceneRunState.run_execution_status == "active",
                SceneRunState.run_checkpoint == "selection_wait",
            )
            .values(run_execution_status="waiting_selection")
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise self._superseded(execution_id, state.active_execution_id)
        self.session.flush()

    def _mark_terminal(self, scene_id: str, execution_id: str, status: str) -> None:
        changed = self.session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                SceneRunState.active_execution_id == execution_id,
                SceneRunState.run_execution_status == "active",
            )
            .values(run_execution_status=status)
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            state = self._state(scene_id, refresh=True)
            if state.active_execution_id != execution_id:
                raise self._superseded(execution_id, state.active_execution_id)
            if state.run_execution_status != status:
                raise checkpoint_corrupt(f"cannot mark execution {status} from {state.run_execution_status}")
        self.session.flush()

    def _result(self, state: SceneRunState, *, resumed: bool) -> ExecutionCheckpoint:
        payload = self._checkpoint_payload(state)
        last_node = state.run_checkpoint
        return ExecutionCheckpoint(
            execution_id=state.active_execution_id or "",
            resumed=resumed,
            last_node=last_node,
            next_node=self._next_node(last_node),
            checkpoint_json=payload,
            status=state.run_execution_status or "active",
        )

    @staticmethod
    def _next_node(node_key: str | None) -> str | None:
        if node_key is None:
            return RUN_CHECKPOINT_ORDER[0]
        if node_key == "cancelled":
            return None
        if node_key == "selection_wait":
            return "soft_qc_ready"
        index = RUN_CHECKPOINT_ORDER.index(node_key)
        if index == len(RUN_CHECKPOINT_ORDER) - 1:
            return None
        return RUN_CHECKPOINT_ORDER[index + 1]

    @staticmethod
    def _validate_advance(
        *,
        previous_node: str | None,
        previous_sub_index: Any,
        node_key: str,
        sub_index: int | None,
    ) -> None:
        if sub_index is not None and (not isinstance(sub_index, int) or sub_index < 0):
            raise checkpoint_corrupt("sub_index must be a non-negative integer")
        if previous_node == node_key:
            if sub_index is None:
                raise checkpoint_corrupt("checkpoint sub_index did not advance")
            if isinstance(previous_sub_index, int) and sub_index <= previous_sub_index:
                raise checkpoint_corrupt("checkpoint sub_index did not advance")
            return
        expected = SceneRunCheckpointService._next_node(previous_node)
        # selection_wait is an optional branch; ordinary runs advance style -> soft QC.
        if previous_node == "style_ready" and node_key == "soft_qc_ready":
            return
        if node_key != expected:
            raise checkpoint_corrupt(
                f"checkpoint advanced out of order: expected {expected}, got {node_key}"
            )

    @staticmethod
    def _validate_checkpoint(state: SceneRunState, payload: dict[str, Any]) -> None:
        if state.run_checkpoint is None:
            if payload.get("node_key") is not None:
                raise checkpoint_corrupt("checkpoint JSON has a node but cursor is empty")
            return
        if state.run_checkpoint == "cancelled":
            if state.run_execution_status != "cancelled":
                raise checkpoint_corrupt(
                    "cancelled checkpoint requires cancelled execution status"
                )
            if payload.get("execution_id") != state.active_execution_id:
                raise checkpoint_corrupt(
                    "checkpoint execution does not match active execution"
                )
            if payload.get("node_key") != "cancelled":
                raise checkpoint_corrupt(
                    "checkpoint JSON does not match checkpoint cursor"
                )
            if not isinstance(payload.get("artifact_refs", {}), dict):
                raise checkpoint_corrupt(
                    "checkpoint artifact references are invalid"
                )
            if not isinstance(payload.get("artifact_hashes", {}), dict):
                raise checkpoint_corrupt(
                    "checkpoint artifact hashes are invalid"
                )
            return
        if state.run_checkpoint not in RUN_CHECKPOINT_ORDER:
            raise checkpoint_corrupt("stored checkpoint node is unknown")
        if payload.get("execution_id") != state.active_execution_id:
            raise checkpoint_corrupt("checkpoint execution does not match active execution")
        if payload.get("node_key") != state.run_checkpoint:
            raise checkpoint_corrupt("checkpoint JSON does not match checkpoint cursor")
        if not isinstance(payload.get("artifact_refs", {}), dict):
            raise checkpoint_corrupt("checkpoint artifact references are invalid")
        if not isinstance(payload.get("artifact_hashes", {}), dict):
            raise checkpoint_corrupt("checkpoint artifact hashes are invalid")

    @staticmethod
    def _checkpoint_payload(state: SceneRunState) -> dict[str, Any]:
        payload = state.run_checkpoint_json
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise checkpoint_corrupt("checkpoint JSON is not an object")
        return dict(payload)

    def _state(self, scene_id: str, *, refresh: bool) -> SceneRunState:
        state = self.session.get(SceneRunState, scene_id)
        if state is None:
            raise DomainError("SCENE_STATE_NOT_FOUND", "scene run state not found", status_code=404)
        if refresh:
            self.session.refresh(state)
        return state

    def _raise_lost_claim(self, scene_id: str, execution_id: str) -> ExecutionCheckpoint:
        state = self._state(scene_id, refresh=True)
        if state.active_execution_id != execution_id:
            raise DomainError(
                "RUN_EXECUTION_IN_PROGRESS",
                "another execution won the scene run claim",
                status_code=409,
                details={"active_execution_id": state.active_execution_id},
            )
        return self._result(state, resumed=state.run_checkpoint is not None)

    @staticmethod
    def _superseded(execution_id: str, active_execution_id: str | None) -> DomainError:
        return DomainError(
            "RUN_EXECUTION_SUPERSEDED",
            "execution no longer owns this scene run",
            status_code=409,
            details={"execution_id": execution_id, "active_execution_id": active_execution_id},
        )
