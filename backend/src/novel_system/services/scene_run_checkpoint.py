from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterRunJob, LlmCall, SceneRunState, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import recover_incomplete_call
from novel_system.services.hash_engine import sha256_json_plain, sha256_text


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
            raise self._corrupt(f"unknown checkpoint node: {node_key}")
        state = self._state(scene_id, refresh=True)
        if state.active_execution_id != execution_id:
            raise self._superseded(execution_id, state.active_execution_id)
        if state.run_execution_status != "active":
            raise self._corrupt("checkpoint cannot advance a non-active execution")

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
                raise self._corrupt("checkpoint output has no execution-step ledger row")
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
                raise self._corrupt(
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
            raise self._corrupt(
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
                raise self._corrupt(
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
            raise self._corrupt("selection wait status requires a selection_wait checkpoint")
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
                raise self._corrupt(f"cannot mark execution {status} from {state.run_execution_status}")
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
            raise SceneRunCheckpointService._corrupt("sub_index must be a non-negative integer")
        if previous_node == node_key:
            if sub_index is None:
                raise SceneRunCheckpointService._corrupt("checkpoint sub_index did not advance")
            if isinstance(previous_sub_index, int) and sub_index <= previous_sub_index:
                raise SceneRunCheckpointService._corrupt("checkpoint sub_index did not advance")
            return
        expected = SceneRunCheckpointService._next_node(previous_node)
        # selection_wait is an optional branch; ordinary runs advance style -> soft QC.
        if previous_node == "style_ready" and node_key == "soft_qc_ready":
            return
        if node_key != expected:
            raise SceneRunCheckpointService._corrupt(
                f"checkpoint advanced out of order: expected {expected}, got {node_key}"
            )

    @staticmethod
    def _validate_checkpoint(state: SceneRunState, payload: dict[str, Any]) -> None:
        if state.run_checkpoint is None:
            if payload.get("node_key") is not None:
                raise SceneRunCheckpointService._corrupt("checkpoint JSON has a node but cursor is empty")
            return
        if state.run_checkpoint == "cancelled":
            if state.run_execution_status != "cancelled":
                raise SceneRunCheckpointService._corrupt(
                    "cancelled checkpoint requires cancelled execution status"
                )
            if payload.get("execution_id") != state.active_execution_id:
                raise SceneRunCheckpointService._corrupt(
                    "checkpoint execution does not match active execution"
                )
            if payload.get("node_key") != "cancelled":
                raise SceneRunCheckpointService._corrupt(
                    "checkpoint JSON does not match checkpoint cursor"
                )
            if not isinstance(payload.get("artifact_refs", {}), dict):
                raise SceneRunCheckpointService._corrupt(
                    "checkpoint artifact references are invalid"
                )
            if not isinstance(payload.get("artifact_hashes", {}), dict):
                raise SceneRunCheckpointService._corrupt(
                    "checkpoint artifact hashes are invalid"
                )
            return
        if state.run_checkpoint not in RUN_CHECKPOINT_ORDER:
            raise SceneRunCheckpointService._corrupt("stored checkpoint node is unknown")
        if payload.get("execution_id") != state.active_execution_id:
            raise SceneRunCheckpointService._corrupt("checkpoint execution does not match active execution")
        if payload.get("node_key") != state.run_checkpoint:
            raise SceneRunCheckpointService._corrupt("checkpoint JSON does not match checkpoint cursor")
        if not isinstance(payload.get("artifact_refs", {}), dict):
            raise SceneRunCheckpointService._corrupt("checkpoint artifact references are invalid")
        if not isinstance(payload.get("artifact_hashes", {}), dict):
            raise SceneRunCheckpointService._corrupt("checkpoint artifact hashes are invalid")

    @staticmethod
    def _checkpoint_payload(state: SceneRunState) -> dict[str, Any]:
        payload = state.run_checkpoint_json
        if payload is None:
            return {}
        if not isinstance(payload, dict):
            raise SceneRunCheckpointService._corrupt("checkpoint JSON is not an object")
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

    @staticmethod
    def _corrupt(message: str) -> DomainError:
        return DomainError("RUN_CHECKPOINT_CORRUPT", message, status_code=409)


class RunCheckpointContext:
    """Per-run checkpoint kernel extracted verbatim from ``Orchestrator``.

    Owns the four per-run execution-ownership fields (``_execution_id`` /
    ``_run_job_id`` / ``_checkpoint_service`` / ``_lease_renewer`` — set and
    reset once per ``run_scene`` / ``resume_after_selection``) plus the guard
    and hashing methods every checkpoint call site relies on.  The hosting
    ``Orchestrator`` exposes the four fields as forwarding properties and keeps
    one-line delegates for each method, so instance-level test overrides on the
    orchestrator keep intercepting the call sites unchanged.

    Persistence contract (checkpoint key names, step keys, sub_index,
    artifact_refs/hashes keys, RUN_CHECKPOINT_CORRUPT validation semantics) is
    byte-for-byte identical to the pre-extraction orchestrator code.
    """

    def __init__(self, session: Session, *, lease_ttl_seconds: Callable[[], int]) -> None:
        self.session = session
        # 注入而非 import：services.idempotency 顶层 import 本模块，反向依赖
        # （哪怕函数内延迟导入）会被架构环守卫拒绝；调用方须传调用时才解析
        # 真实来源的 callable，保住对 idempotency.owner_lease_ttl_seconds 的打桩。
        self._lease_ttl_seconds = lease_ttl_seconds
        self._execution_id: str | None = None
        self._run_job_id: str | None = None
        self._checkpoint_service: SceneRunCheckpointService | None = None
        self._lease_renewer = None

    def _checkpoint_reached(self, node_key: str) -> bool:
        if node_key not in RUN_CHECKPOINT_ORDER:
            return False
        state = self._active_checkpoint_state()
        current = state.run_checkpoint
        if current not in RUN_CHECKPOINT_ORDER:
            return False
        return RUN_CHECKPOINT_ORDER.index(current) >= RUN_CHECKPOINT_ORDER.index(
            node_key
        )

    def _checkpoint_artifact(self, key: str, *, expected_node_at_least: str) -> Any:
        if not self._checkpoint_reached(expected_node_at_least):
            return None
        state = self._active_checkpoint_state()
        payload = state.run_checkpoint_json or {}
        if (
            not isinstance(payload, dict)
            or payload.get("execution_id") != self._execution_id
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "checkpoint owner payload is invalid",
                status_code=409,
            )
        refs = payload.get("artifact_refs")
        if not isinstance(refs, dict):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "checkpoint artifact references are invalid",
                status_code=409,
            )
        return refs.get(key)

    def _save_run_checkpoint(
        self,
        node_key: str,
        *,
        artifact_refs: dict[str, Any] | None = None,
        artifact_hashes: dict[str, str] | None = None,
        sub_index: int | None = None,
        strategy: str | None = None,
        branch: str | None = None,
    ) -> None:
        if self._checkpoint_service is None or self._execution_id is None:
            raise RuntimeError("scene checkpoint context is not active")
        self._renew_owner_lease(lease_seconds=self._lease_ttl_seconds())
        # Flush the product/state mutation first; SceneRunCheckpointService
        # refreshes the execution fence before advancing it.  Both writes are
        # still committed together below.
        self.session.flush()
        self._checkpoint_service.save_checkpoint(
            scene_id=self._active_checkpoint_state().scene_id,
            execution_id=self._execution_id,
            node_key=node_key,
            sub_index=sub_index,
            artifact_refs=artifact_refs,
            artifact_hashes=artifact_hashes,
            strategy=strategy,
            branch=branch,
        )
        if self._run_job_id is not None:
            run_job = self.session.get(ChapterRunJob, self._run_job_id)
            if run_job is not None:
                # A cancel endpoint may have committed actor/reason while this
                # worker was awaiting the provider.  Merge checkpoint progress
                # into those authoritative JSON values instead of overwriting
                # them from expire_on_commit=False identity-map state.
                self.session.refresh(
                    run_job,
                    attribute_names=["payload_json", "result_summary_json"],
                )
                run_job.payload_json = {
                    **dict(run_job.payload_json or {}),
                    "current_step": node_key,
                    **(
                        {"current_sub_index": sub_index}
                        if sub_index is not None
                        else {}
                    ),
                }
                run_job.result_summary_json = {
                    **dict(run_job.result_summary_json or {}),
                    "current_step": node_key,
                    **(
                        {"current_sub_index": sub_index}
                        if sub_index is not None
                        else {}
                    ),
                }
        self.session.commit()
        # The just-produced artifact and its ledger/checkpoint are durable before
        # observing cancellation.  Cancellation therefore fences only the next node.
        self._raise_if_run_cancelled()

    def _reconcile_execution_step(
        self,
        execution_step_key: str,
        *,
        chapter_scope: bool = False,
    ) -> None:
        if self._checkpoint_service is None or self._execution_id is None:
            return
        self._checkpoint_service.reconcile_step_output(
            scene_id=self._active_checkpoint_state().scene_id,
            execution_id=self._execution_id,
            execution_step_key=execution_step_key,
            output_exists=False,
            ledger_scope="chapter" if chapter_scope else "scene",
        )

    def _validate_checkpoint_llm_output(
        self,
        *,
        scene_id: str,
        llm_call_id: Any,
        execution_step_key: Any,
        execution_id: str | None = None,
        allowed_accounting_statuses: tuple[str, ...] = ("settled",),
        allow_local_rejected_output: bool = False,
    ) -> LlmCall:
        if (
            self._checkpoint_service is None
            or not isinstance(llm_call_id, str)
            or not llm_call_id
            or not isinstance(execution_step_key, str)
            or not execution_step_key
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "checkpoint LLM output reference is incomplete",
                status_code=409,
            )
        owner_execution_id = execution_id or self._execution_id
        if not owner_execution_id:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "checkpoint execution owner is missing",
                status_code=409,
            )
        self._checkpoint_service.reconcile_step_output(
            scene_id=scene_id,
            execution_id=owner_execution_id,
            execution_step_key=execution_step_key,
            output_exists=True,
            allow_local_rejected_output=allow_local_rejected_output,
        )
        call = self.session.get(LlmCall, llm_call_id)
        if (
            call is None
            or call.scene_id != scene_id
            or call.execution_id != owner_execution_id
            or call.execution_step_key != execution_step_key
            or call.accounting_status not in allowed_accounting_statuses
            or (
                call.request_dispatched_at is None
                and not (
                    allow_local_rejected_output and call.accounting_status == "rejected"
                )
            )
        ):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "checkpoint output parent LLM call does not match its execution ledger",
                status_code=409,
                details={
                    "llm_call_id": llm_call_id,
                    "execution_id": owner_execution_id,
                    "execution_step_key": execution_step_key,
                },
            )
        return call

    def _validate_artifact_execution_owner(self, owner_execution_id: Any) -> str:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        allowed = {
            self._execution_id,
            (
                payload.get("selection_origin_execution_id")
                if isinstance(payload, dict)
                else None
            ),
        }
        if isinstance(payload, dict):
            allowed.update(payload.get("artifact_execution_lineage_ids") or [])
        if not isinstance(owner_execution_id, str) or owner_execution_id not in allowed:
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "checkpoint artifact execution owner is outside the durable execution lineage",
                status_code=409,
                details={"artifact_execution_id": owner_execution_id},
            )
        return owner_execution_id

    def _checkpoint_execution_owner_matches(
        self,
        execution_id: Any,
        run_job_id: Any,
    ) -> bool:
        """Match current or inherited scene-job ownership after a checkpoint handoff."""
        if execution_id == self._execution_id:
            return run_job_id == self._run_job_id
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        inherited = (
            set(payload.get("artifact_execution_lineage_ids") or [])
            if isinstance(payload, dict)
            else set()
        )
        selection_origin = (
            payload.get("selection_origin_execution_id")
            if isinstance(payload, dict)
            else None
        )
        if selection_origin:
            inherited.add(selection_origin)
        if not isinstance(execution_id, str) or execution_id not in inherited:
            return False
        # Scene jobs deliberately use job_id as execution_id. Selection-resume
        # requests instead own their products through an idempotency execution
        # and therefore have no run_job_id. Both identities are durable lineage.
        if run_job_id is None:
            return execution_id.startswith("idempotency:")
        return run_job_id == execution_id

    def _renew_owner_lease(self, *, lease_seconds: int) -> None:
        if self._lease_renewer is None:
            return
        self._lease_renewer(lease_seconds=lease_seconds)

    def _raise_if_run_cancelled(self) -> None:
        if self._run_job_id is None:
            return
        scene_id = self._active_checkpoint_state().scene_id
        row = self.session.execute(
            select(
                ChapterRunJob.status,
                ChapterRunJob.job_type,
                ChapterRunJob.scene_id,
                ChapterRunJob.payload_json,
            ).where(ChapterRunJob.job_id == self._run_job_id)
        ).one_or_none()
        status = row.status if row is not None else None
        payload = (
            row.payload_json
            if row is not None and isinstance(row.payload_json, dict)
            else {}
        )
        ownership_matches = bool(
            row is not None
            and (
                (row.job_type == "scene_run_full" and row.scene_id == scene_id)
                or (
                    row.job_type == "chapter_run_full"
                    and payload.get("current_scene_id") == scene_id
                )
            )
        )
        self.session.rollback()
        if status in {"cancel_requested", "cancelled"}:
            raise DomainError(
                "RUN_JOB_CANCELLED_BY_AUTHOR",
                "scene run cancellation was observed after the durable node boundary",
                status_code=409,
                details={"job_id": self._run_job_id, "status": status},
            )
        if status != "running" or not ownership_matches:
            raise DomainError(
                "RUN_OWNER_LEASE_LOST",
                "scene run job is no longer the active running owner",
                status_code=409,
                details={"job_id": self._run_job_id, "status": status},
            )

    def _checkpoint_hash(self, key: str) -> str | None:
        payload = self._active_checkpoint_state().run_checkpoint_json or {}
        hashes = payload.get("artifact_hashes") if isinstance(payload, dict) else None
        if not isinstance(hashes, dict):
            raise DomainError(
                "RUN_CHECKPOINT_CORRUPT",
                "checkpoint artifact hashes are invalid",
                status_code=409,
            )
        value = hashes.get(key)
        return str(value) if value is not None else None

    def _raise_checkpoint_output_missing(self, *, row_id: Any) -> None:
        raise DomainError(
            "RUN_CHECKPOINT_OUTPUT_MISSING",
            "checkpoint references a committed call/output that is missing",
            status_code=409,
            details={"row_id": row_id},
        )

    def _active_checkpoint_state(self) -> SceneRunState:
        if self._execution_id is None:
            raise RuntimeError("scene checkpoint context is not active")
        state = (
            self.session.execute(
                select(SceneRunState).where(
                    SceneRunState.active_execution_id == self._execution_id
                )
            )
            .scalars()
            .one_or_none()
        )
        if state is None:
            raise DomainError(
                "RUN_EXECUTION_SUPERSEDED",
                "scene execution no longer owns state",
                status_code=409,
            )
        return state

    @staticmethod
    def _text_hash(content: str) -> str:
        return sha256_text(content)

    @staticmethod
    def _json_hash(payload: Any) -> str:
        return sha256_json_plain(payload)
