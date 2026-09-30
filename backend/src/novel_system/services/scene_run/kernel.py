"""检查点内核：一次运行的执行归属（四个字段）与每个检查点调用点都靠的守卫、读写、哈希。

``Orchestrator`` 直接继承它；四个字段（``_execution_id`` / ``_run_job_id`` / ``_checkpoint_service`` /
``_lease_renewer``）在 ``Orchestrator.__init__`` 里置空，``run_scene`` / ``resume_after_selection`` 开跑时设、收尾时清。
方法之间一律 ``self.X`` 互调，测试在编排器实例上的覆盖（``_reconcile_execution_step`` 等）照样拦得住。

持久化契约（检查点键名、步骤键、``sub_index``、``artifact_refs`` / ``artifact_hashes`` 键、
``RUN_CHECKPOINT_CORRUPT`` 的校验语义、``_json_hash`` 编码）一字不变。

续租时长每次调用时经模块属性 ``idempotency.owner_lease_ttl_seconds`` 取（测试打桩那里）。
"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import select

from novel_system.db.models import ChapterRunJob, LlmCall, SceneRunState
from novel_system.services import idempotency as _idempotency
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_json_plain, sha256_text
from novel_system.services.scene_run_checkpoint import (
    RUN_CHECKPOINT_ORDER,
    SceneRunCheckpointService,
    checkpoint_corrupt,
)


class RunCheckpointKernelMixin:
    # 每次运行的执行归属（``Orchestrator.__init__`` 置空；开跑时设、收尾时清）
    _execution_id: str | None
    _run_job_id: str | None
    _checkpoint_service: SceneRunCheckpointService | None
    _lease_renewer: Callable[..., Any] | None
    # 两道围栏之间读到的运行状态行（执行 id, 行）；执行开始 / 结束时清空
    _checkpoint_state_cache: tuple[str, SceneRunState] | None = None

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
            raise checkpoint_corrupt("checkpoint owner payload is invalid")
        refs = payload.get("artifact_refs")
        if not isinstance(refs, dict):
            raise checkpoint_corrupt("checkpoint artifact references are invalid")
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
        self._renew_owner_lease(lease_seconds=_idempotency.owner_lease_ttl_seconds())
        # Flush the product/state mutation first; SceneRunCheckpointService
        # refreshes the execution fence before advancing it.  Both writes are
        # still committed together below.
        self.session.flush()
        self._checkpoint_service.save_checkpoint(
            scene_id=self._fenced_checkpoint_state().scene_id,
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
            scene_id=self._fenced_checkpoint_state().scene_id,
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
            raise checkpoint_corrupt("checkpoint LLM output reference is incomplete")
        owner_execution_id = execution_id or self._execution_id
        if not owner_execution_id:
            raise checkpoint_corrupt("checkpoint execution owner is missing")
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
            raise checkpoint_corrupt(
                "checkpoint output parent LLM call does not match its execution ledger",
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
            raise checkpoint_corrupt(
                "checkpoint artifact execution owner is outside the durable execution lineage",
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
            raise checkpoint_corrupt("checkpoint artifact hashes are invalid")
        value = hashes.get(key)
        return str(value) if value is not None else None

    def _raise_checkpoint_output_missing(self, *, row_id: Any) -> None:
        raise DomainError(
            "RUN_CHECKPOINT_OUTPUT_MISSING",
            "checkpoint references a committed call/output that is missing",
            status_code=409,
            details={"row_id": row_id},
        )

    def _sub_checkpoint_progress(
        self,
        node_key: str,
        *,
        last_sub_index: int,
        legacy_complete: Callable[[dict[str, Any]], bool],
        legacy_sub_index: int,
        invalid_message: str,
    ) -> int:
        """一个带子游标的节点走到了哪一步：还没到 → -1；已经过了 → ``last_sub_index``；正停在这个节点上 → 它的
        ``sub_index``。子游标上线之前写下的完整检查点没有 ``sub_index``：``legacy_complete(refs)`` 成立时按
        ``legacy_sub_index`` 读；别的一律 ``RUN_CHECKPOINT_CORRUPT``（``invalid_message``）。"""
        state = self._active_checkpoint_state()
        current = state.run_checkpoint
        if current not in RUN_CHECKPOINT_ORDER:
            return -1
        node_index = RUN_CHECKPOINT_ORDER.index(node_key)
        current_index = RUN_CHECKPOINT_ORDER.index(current)
        if current_index < node_index:
            return -1
        if current_index > node_index:
            return last_sub_index
        payload = state.run_checkpoint_json or {}
        sub_index = payload.get("sub_index") if isinstance(payload, dict) else None
        if (
            isinstance(sub_index, int)
            and not isinstance(sub_index, bool)
            and 0 <= sub_index <= last_sub_index
        ):
            return sub_index
        refs = payload.get("artifact_refs") if isinstance(payload, dict) else None
        if sub_index is None and isinstance(refs, dict) and legacy_complete(refs):
            return legacy_sub_index
        raise checkpoint_corrupt(invalid_message)

    def _require_checkpoint_row(self, model: Any, row_id: Any) -> Any:
        """检查点引用的一行：id 不是字符串或库里没有这一行 → ``RUN_CHECKPOINT_OUTPUT_MISSING``。"""
        row = self.session.get(model, row_id) if isinstance(row_id, str) else None
        if row is None:
            self._raise_checkpoint_output_missing(row_id=row_id)
        return row

    def _active_checkpoint_state(self) -> SceneRunState:
        """这次执行拥有的运行状态行（库里 ``active_execution_id`` 仍是它，否则 ``RUN_EXECUTION_SUPERSEDED``）。

        在库里核对归属的读只在围栏处做（B01-10）：存检查点之前、对账（每次调模型之前）、执行开始时；两道围栏之间
        的读用同一个行对象（本会话的身份映射里就是它，SELECT 也不会刷新它），一次运行少上百次 SELECT。
        """
        if self._execution_id is None:
            raise RuntimeError("scene checkpoint context is not active")
        cached = self._checkpoint_state_cache
        if cached is not None and cached[0] == self._execution_id:
            return cached[1]
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
        self._checkpoint_state_cache = (self._execution_id, state)
        return state

    def _fenced_checkpoint_state(self) -> SceneRunState:
        """在库里重新核对归属后的运行状态行（围栏处用）。"""
        self._checkpoint_state_cache = None
        return self._active_checkpoint_state()

    @staticmethod
    def _text_hash(content: str) -> str:
        return sha256_text(content)

    @staticmethod
    def _json_hash(payload: Any) -> str:
        return sha256_json_plain(payload)
