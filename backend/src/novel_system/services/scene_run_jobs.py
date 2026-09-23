from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterRunJob, LlmCall, OperationLog, QcReport, SceneRunState, utcnow
from novel_system.db.session import SessionLocal
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.author_instructions import normalize_author_note
from novel_system.services.errors import DomainError
from novel_system.services.idempotency import owner_lease_ttl_seconds
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService, scene_job_execution_id
from novel_system.services.scene_run_preflight import SceneRunPreflightService

JOB_TYPE_SCENE_FULL = "scene_run_full"
RUN_JOB_CANCEL_REQUESTED = "RUN_JOB_CANCEL_REQUESTED_BY_AUTHOR"
RUN_JOB_CANCELLED = "RUN_JOB_CANCELLED_BY_AUTHOR"
_BUDGET_REJECTION_CODES = frozenset(
    {
        "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED",
        "LLM_PROVIDER_ATTEMPT_BUDGET_EXHAUSTED",
        "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        "LLM_USAGE_EXCEEDS_RESERVATION",
        "LLM_GLOBAL_CONCURRENCY_LIMIT",
        "LLM_DAILY_REQUEST_LIMIT",
        "LLM_DAILY_TOKEN_LIMIT",
        "LLM_MONTHLY_TOKEN_LIMIT",
        "LLM_PROJECT_DAILY_TOKEN_LIMIT",
        "LLM_DAILY_COST_LIMIT",
    }
)
_CANCELLED_JOB_REGISTRY: set[str] = set()
_CANCELLED_JOB_REGISTRY_LOCK = threading.Lock()
SCENE_RUN_STAGE_ORDER = [
    "planning_running",
    "bundle_built",
    "neutral_running",
    "hard_qc_running",
    "style_running",
    "soft_qc_running",
    "rewrite_running",
    "acceptance_review_running",
    "near_final",
    "archived",
]


@dataclass
class SceneRunJobLease:
    job_id: str
    worker_id: str
    attempt_no: int
    lease_expires_at: str
    _service: "SceneRunJobService" = field(repr=False, compare=False)

    def renew(self, *, lease_seconds: int) -> str:
        self.lease_expires_at = self._service.renew_lease(self, lease_seconds=lease_seconds)
        return self.lease_expires_at

    def renew_detached(self, *, lease_seconds: int) -> str:
        """Renew with an independent session for long provider calls."""

        with SessionLocal() as session:
            service = SceneRunJobService(session)
            detached = SceneRunJobLease(
                job_id=self.job_id,
                worker_id=self.worker_id,
                attempt_no=self.attempt_no,
                lease_expires_at=self.lease_expires_at,
                _service=service,
            )
            expires = service.renew_lease(detached, lease_seconds=lease_seconds)
            session.commit()
            return expires


class SceneRunJobService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_job(
        self,
        scene_id: str,
        *,
        actor_ref: str = "operator",
        author_note: str | None = None,
        run_policy: str = "reliable",
        budget_resume_parent_execution_id: str | None = None,
    ) -> ChapterRunJob:
        scene = AuthorLifecycleService(self.session).require_active_scene(scene_id)
        run_preflight = SceneRunPreflightService(self.session).build(scene, {})
        can_run = bool(run_preflight.get("can_run"))
        current_step = "queued" if can_run else "preflight_blocked"
        status = "queued" if can_run else "blocked"
        first_blocker = _first_preflight_blocker(run_preflight)
        now = utcnow()
        job_id = f"scene_run_{scene_id}_{uuid4().hex[:10]}"
        if can_run:
            # Preflight is read-only.  End its snapshot before taking the SQLite
            # write lock so concurrent creators cannot both upgrade stale reads.
            self.session.commit()
            _begin_immediate(self.session)
            state_exists = self.session.scalar(
                select(SceneRunState.scene_id).where(SceneRunState.scene_id == scene_id)
            )
            if state_exists is None:
                self.session.add(SceneRunState(scene_id=scene_id, scene_status="ready"))
                self.session.flush()
            claimed = self.session.execute(
                update(SceneRunState)
                .where(
                    SceneRunState.scene_id == scene_id,
                    SceneRunState.active_run_job_id.is_(None),
                )
                .values(active_run_job_id=job_id)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                self.session.rollback()
                active_job_id = self.session.scalar(
                    select(SceneRunState.active_run_job_id).where(
                        SceneRunState.scene_id == scene_id
                    )
                )
                self.session.rollback()
                raise DomainError(
                    "RUN_JOB_IN_PROGRESS",
                    "scene already has an active run job",
                    status_code=409,
                    details={"scene_id": scene_id, "job_id": active_job_id},
                )
        # FE-ALIGN G3：作者改写指令随任务下发（风格生成阶段注入提示词）
        note = normalize_author_note(author_note)
        job = ChapterRunJob(
            job_id=job_id,
            chapter_id=scene.chapter_id,
            scene_id=scene_id,
            status=status,
            job_type=JOB_TYPE_SCENE_FULL,
            payload_json={
                "scene_id": scene_id,
                "actor_ref": actor_ref,
                "current_step": current_step,
                "stage_order": SCENE_RUN_STAGE_ORDER,
                "lock_wait_ms": 0,
                "run_preflight_status": run_preflight.get("overall_status"),
                **({"author_note": note} if note else {}),
                # Wave 2：运行策略随任务下发（reliable|strict|auto，列属 Wave 3）
                **({"run_policy": run_policy} if run_policy and run_policy != "reliable" else {}),
                **(
                    {"budget_resume_parent_execution_id": budget_resume_parent_execution_id}
                    if budget_resume_parent_execution_id
                    else {}
                ),
            },
            result_summary_json={
                "scene_id": scene_id,
                "current_step": current_step,
                "latest_qc": None,
                "needs_human_review": False,
                "run_preflight": run_preflight,
                "next_action": _preflight_next_action(run_preflight) if not can_run else None,
                # 预检即拦截路径也透出结构化缺失字段（与 worker 路径同形），前端引导同源
                "error_details": {"missing_fields": list((first_blocker or {}).get("missing_fields") or [])} if first_blocker else {},
            },
            worker_id=None,
            attempt_no=0,
            heartbeat_at=now,
            finished_at=now if not can_run else None,
            error_code=first_blocker.get("code") if first_blocker else None,
            error_text=first_blocker.get("detail") or first_blocker.get("technical_hint") if first_blocker else None,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def get_job(self, job_id: str) -> ChapterRunJob:
        job = self.session.get(ChapterRunJob, job_id)
        if job is None or job.job_type != JOB_TYPE_SCENE_FULL:
            raise DomainError("RUN_JOB_NOT_FOUND", "run job not found", status_code=404)
        return job

    def latest_job(self, scene_id: str) -> ChapterRunJob:
        job = self.session.execute(
            select(ChapterRunJob)
            .where(
                ChapterRunJob.scene_id == scene_id,
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
            )
            .order_by(ChapterRunJob.created_at.desc(), ChapterRunJob.job_id.desc())
        ).scalars().first()
        if job is None:
            raise DomainError(
                "RUN_JOB_NOT_FOUND",
                "scene run job not found",
                status_code=404,
                details={"scene_id": scene_id},
            )
        return job

    def resolve_budget_resume_execution_id(self, scene_id: str) -> str:
        """Resolve a resumable execution without accepting a client checkpoint id."""
        state = self.session.get(SceneRunState, scene_id)
        try:
            latest = self.latest_job(scene_id)
        except DomainError as exc:
            if exc.code != "RUN_JOB_NOT_FOUND":
                raise
            raise DomainError(
                "RUN_BUDGET_RESUME_UNAVAILABLE",
                "no server-owned budget-blocked checkpoint is available to resume",
                status_code=409,
                details={"scene_id": scene_id, "latest_job_id": None},
            ) from exc
        eligible = bool(
            state is not None
            and state.active_execution_id
            and state.run_execution_status == "failed"
            and state.run_checkpoint
            and latest.status == "blocked"
            and latest.error_code in _BUDGET_REJECTION_CODES
            and latest.job_id == state.active_execution_id
        )
        if not eligible:
            raise DomainError(
                "RUN_BUDGET_RESUME_UNAVAILABLE",
                "no server-owned budget-blocked checkpoint is available to resume",
                status_code=409,
                details={
                    "scene_id": scene_id,
                    "latest_job_id": latest.job_id,
                    "latest_status": latest.status,
                    "latest_error_code": latest.error_code,
                },
            )
        return str(state.active_execution_id)

    def owner_for(self, job: ChapterRunJob) -> SceneRunJobLease:
        if not job.worker_id or not job.attempt_no:
            raise ValueError("running scene job has no durable owner")
        return SceneRunJobLease(
            job_id=job.job_id,
            worker_id=job.worker_id,
            attempt_no=int(job.attempt_no),
            lease_expires_at=str(job.lease_expires_at or ""),
            _service=self,
        )

    def claim_scene_active_job(self, owner: SceneRunJobLease, scene_id: str) -> None:
        claimed = self.session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == scene_id,
                or_(
                    SceneRunState.active_run_job_id.is_(None),
                    SceneRunState.active_run_job_id == owner.job_id,
                ),
            )
            .values(active_run_job_id=owner.job_id)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            self.session.rollback()
            raise DomainError(
                "RUN_JOB_IN_PROGRESS",
                "scene is owned by another active run job",
                status_code=409,
                details={"scene_id": scene_id, "job_id": owner.job_id},
            )
        self.session.flush()

    def request_cancel(
        self,
        job_id: str,
        *,
        actor_ref: str,
        reason: str | None = None,
    ) -> ChapterRunJob:
        reason_text = str(reason or "").strip()[:500]
        self.session.commit()
        _begin_immediate(self.session)
        row = self.session.execute(
            select(ChapterRunJob.status, ChapterRunJob.scene_id).where(
                ChapterRunJob.job_id == job_id,
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
            )
        ).one_or_none()
        if row is None:
            self.session.rollback()
            raise DomainError("RUN_JOB_NOT_FOUND", "run job not found", status_code=404)
        if row.status in {"cancel_requested", "cancelled"}:
            self.session.rollback()
            job = self.get_job(job_id)
            self.session.refresh(job)
            return job
        if row.status not in {"queued", "running"}:
            self.session.rollback()
            raise DomainError(
                "RUN_JOB_CANCEL_CONFLICT",
                "terminal scene run job cannot be cancelled",
                status_code=409,
                details={"job_id": job_id, "status": row.status},
            )

        target_status = "cancelled" if row.status == "queued" else "cancel_requested"
        error_code = RUN_JOB_CANCELLED if target_status == "cancelled" else RUN_JOB_CANCEL_REQUESTED
        now = utcnow()
        changed = self.session.execute(
            update(ChapterRunJob)
            .where(
                ChapterRunJob.job_id == job_id,
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
                ChapterRunJob.status == row.status,
            )
            .values(
                status=target_status,
                finished_at=now if target_status == "cancelled" else None,
                error_code=error_code,
                error_text=(
                    "scene run cancelled by author"
                    if target_status == "cancelled"
                    else "scene run cancellation requested by author"
                ),
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            self.session.rollback()
            job = self.get_job(job_id)
            self.session.refresh(job)
            if job.status in {"cancel_requested", "cancelled"}:
                return job
            raise DomainError(
                "RUN_JOB_CANCEL_CONFLICT",
                "scene run job changed before cancellation",
                status_code=409,
                details={"job_id": job_id, "status": job.status},
            )

        job = self.get_job(job_id)
        self.session.refresh(job)
        self._update_payload(job, current_step=target_status)
        self._update_summary(
            job,
            current_step=target_status,
            cancellation={
                "actor_ref": actor_ref,
                "reason": reason_text,
                "requested_at": now,
            },
        )
        self.session.add(
            OperationLog(
                event_type="scene_run_cancel_requested",
                object_type="scene_run_job",
                object_ref=job_id,
                payload_json={
                    "job_id": job_id,
                    "scene_id": row.scene_id,
                    "actor_ref": actor_ref,
                    "reason": reason_text,
                    "status_before": row.status,
                    "status_after": target_status,
                    "requested_at": now,
                },
            )
        )
        if target_status == "cancelled":
            self._record_cancelled(job, actor_ref=actor_ref, reason=reason_text, cancelled_at=now)
            self._clear_active_job(job)
        self.session.flush()
        return job

    def _scene_draft_mode(self, scene_state: SceneRunState | None) -> str | None:
        if scene_state is None or not scene_state.current_bundle_id:
            return None
        try:
            from novel_system.db.models import SceneBundle
            from novel_system.services.style_policy import style_policy_for_bundle

            bundle_row = self.session.get(SceneBundle, scene_state.current_bundle_id)
            if bundle_row is None:
                return None
            return style_policy_for_bundle(bundle_row.frozen_snapshot_json).draft_mode
        except Exception:  # noqa: BLE001 — 只读展示,不影响任务视图
            return None

    def serialize_job(self, job: ChapterRunJob) -> dict[str, Any]:
        payload = dict(job.payload_json or {})
        summary = dict(job.result_summary_json or {})
        scene_id = job.scene_id or payload.get("scene_id") or summary.get("scene_id")
        latest_qc = summary.get("latest_qc") or self._latest_qc_summary(str(scene_id or ""))
        error_details = dict(summary.get("error_details") or {})
        # C2 状态一致性债务：job 视图叠加权威场景态。场景已归档且 job 已终结时，
        # current_step 不再展示旧暂停点（如 awaiting_candidate_selection）——
        # 历史真值仍在 payload/result_summary 里，只有视图层收敛。
        scene_state = self.session.get(SceneRunState, str(scene_id)) if scene_id else None
        scene_status = scene_state.scene_status if scene_state else None
        current_step = payload.get("current_step") or summary.get("current_step") or job.status
        if scene_status == "archived" and job.status not in {"queued", "running", "cancel_requested"}:
            current_step = "archived"
        return {
            "job_id": job.job_id,
            "chapter_id": job.chapter_id,
            "scene_id": scene_id,
            "job_type": job.job_type,
            "status": job.status,
            "scene_status": scene_status,
            "current_step": current_step,
            # 2026-09-12 风格直起:运行中即可知道中性步位写的是作者手笔首稿还是中性稿
            # (bundle 冻结后才有;之前为 None,前端回退到工作台读数)。
            "draft_mode": self._scene_draft_mode(scene_state),
            "stage_order": payload.get("stage_order") or SCENE_RUN_STAGE_ORDER,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "elapsed_ms": _elapsed_ms(job.started_at or job.created_at, job.finished_at),
            "current_model_call": summary.get("current_model_call"),
            "lock_wait_ms": payload.get("lock_wait_ms", 0),
            "latest_qc": latest_qc,
            "needs_human_review": bool(summary.get("needs_human_review")),
            "error_code": job.error_code,
            "error_text": job.error_text,
            # 异步路径透出结构化缺失字段（与同步 run/full 的 error.details.missing_fields 同源），
            # 前端据此给「去补全场景卡(缺 xxx)」精确引导，不再只能拿到 error_code。
            "error_details": error_details,
            "missing_fields": list(error_details.get("missing_fields") or []),
            "author_note": payload.get("author_note") or "",
            "run_preflight": summary.get("run_preflight"),
            "result_summary": summary,
        }

    def claim_running(
        self,
        job_id: str,
        *,
        worker_id: str,
        current_step: str,
        lease_seconds: int,
    ) -> SceneRunJobLease:
        # End any caller read snapshot, then serialize the authoritative reread
        # and queued/expired-owner CAS.  This avoids SQLite WAL read->write
        # upgrades surfacing SQLITE_BUSY instead of a stable domain outcome.
        self.session.commit()
        job = self.get_job(job_id)
        self.session.refresh(job)
        self.session.rollback()
        _begin_immediate(self.session)
        job = self.get_job(job_id)
        self.session.refresh(job)
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        expires = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        # A RUNNING row without a lease is an abandoned pre-lease/crash state,
        # not an immortal owner.  The CAS below still fences on the observed
        # worker/attempt/NULL lease so only one recovery worker can take it.
        if job.status == "running" and (
            job.lease_expires_at is not None and job.lease_expires_at > now_iso
        ):
            self.session.rollback()
            raise DomainError(
                "RUN_JOB_IN_PROGRESS",
                "scene run job is already owned by an active worker",
                status_code=409,
                details={"job_id": job_id, "worker_id": job.worker_id},
            )

        summary = job.result_summary_json if isinstance(job.result_summary_json, dict) else {}
        error_details = summary.get("error_details")
        failed_is_retryable = (
            job.status == "failed"
            and isinstance(error_details, dict)
            and error_details.get("retryable") is True
        )
        if job.status not in {"queued", "running"} and not failed_is_retryable:
            self.session.rollback()
            raise DomainError(
                "RUN_JOB_NOT_CLAIMABLE",
                "scene run job status cannot be claimed by a worker",
                status_code=409,
                details={"job_id": job_id, "status": job.status},
            )

        old_status = job.status
        old_worker = job.worker_id
        old_attempt = int(job.attempt_no or 0)
        old_expiry = job.lease_expires_at
        conditions = [
            ChapterRunJob.job_id == job_id,
            ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
            ChapterRunJob.status == old_status,
            ChapterRunJob.attempt_no == old_attempt,
        ]
        conditions.append(
            ChapterRunJob.worker_id.is_(None)
            if old_worker is None
            else ChapterRunJob.worker_id == old_worker
        )
        conditions.append(
            ChapterRunJob.lease_expires_at.is_(None)
            if old_expiry is None
            else ChapterRunJob.lease_expires_at == old_expiry
        )
        claimed = self.session.execute(
            update(ChapterRunJob)
            .where(*conditions)
            .values(
                status="running",
                worker_id=worker_id,
                attempt_no=old_attempt + 1,
                started_at=job.started_at or now_iso,
                heartbeat_at=now_iso,
                lease_expires_at=expires,
                finished_at=None,
                error_code=None,
                error_text=None,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            self.session.rollback()
            current = self.get_job(job_id)
            self.session.refresh(current)
            if current.status == "running" and (
                current.lease_expires_at is not None
                and current.lease_expires_at > now_iso
            ):
                raise DomainError(
                    "RUN_JOB_IN_PROGRESS",
                    "another worker won the scene run job claim",
                    status_code=409,
                    details={"job_id": job_id, "worker_id": current.worker_id},
                )
            raise DomainError(
                "RUN_JOB_NOT_CLAIMABLE",
                "scene run job status cannot be claimed by a worker",
                status_code=409,
                details={"job_id": job_id, "status": current.status},
            )
        self.session.flush()
        job = self.get_job(job_id)
        self.session.refresh(job)
        self._update_payload(job, current_step=current_step)
        self._update_summary(job, current_step=current_step)
        self.session.flush()
        return SceneRunJobLease(
            job_id=job_id,
            worker_id=worker_id,
            attempt_no=old_attempt + 1,
            lease_expires_at=expires,
            _service=self,
        )

    def renew_lease(self, owner: SceneRunJobLease, *, lease_seconds: int) -> str:
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=max(1, lease_seconds))).isoformat()
        renewed = self.session.execute(
            update(ChapterRunJob)
            .where(
                ChapterRunJob.job_id == owner.job_id,
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
                ChapterRunJob.status.in_(("running", "cancel_requested")),
                ChapterRunJob.worker_id == owner.worker_id,
                ChapterRunJob.attempt_no == owner.attempt_no,
            )
            .values(heartbeat_at=now.isoformat(), lease_expires_at=expires)
            .execution_options(synchronize_session=False)
        )
        if renewed.rowcount != 1:
            self.session.rollback()
            raise DomainError(
                "RUN_OWNER_LEASE_LOST",
                "scene run job owner lease was lost",
                status_code=409,
                details={
                    "job_id": owner.job_id,
                    "worker_id": owner.worker_id,
                    "attempt_no": owner.attempt_no,
                },
            )
        self.session.flush()
        return expires

    def mark_finished(
        self,
        job: ChapterRunJob,
        *,
        status: str,
        current_step: str,
        result: dict[str, Any],
        owner: SceneRunJobLease | None = None,
    ) -> None:
        if owner is not None:
            self._transition_owned_job(owner, status=status)
            job = self.get_job(owner.job_id)
            self.session.refresh(job)
        job.status = status
        job.finished_at = utcnow()
        job.error_code = None
        job.error_text = None
        self._update_payload(job, current_step=current_step)
        self._update_summary(
            job,
            current_step=current_step,
            scene_status=result.get("scene_status"),
            current_final_scene_row_id=result.get("current_final_scene_row_id"),
            current_human_review_event_id=result.get("current_human_review_event_id"),
            needs_human_review=bool(result.get("current_human_review_event_id") or result.get("scene_status") == "human_review_required"),
            latest_qc=self._latest_qc_summary(
                str(job.scene_id or (job.payload_json or {}).get("scene_id") or "")
            ),
        )
        self._clear_active_job(job)
        self.session.flush()

    def mark_failed(
        self,
        job: ChapterRunJob,
        *,
        error_code: str,
        error_text: str,
        details: dict[str, Any] | None = None,
        owner: SceneRunJobLease | None = None,
        status: str = "failed",
    ) -> None:
        if owner is not None:
            self._transition_owned_job(owner, status=status)
            job = self.get_job(owner.job_id)
            self.session.refresh(job)
        job.status = status
        job.finished_at = utcnow()
        job.error_code = error_code
        job.error_text = error_text
        error_details = dict(details or {})
        self._update_payload(job, current_step=status)
        self._update_summary(
            job,
            current_step=status,
            latest_error={"code": error_code, "message": error_text, "details": error_details},
            error_details=error_details,
        )
        self.session.add(
            OperationLog(
                event_type=(
                    "scene_run_budget_blocked" if status == "blocked" else "scene_run_failed"
                ),
                object_type="scene_run_job",
                object_ref=job.job_id,
                payload_json={
                    "job_id": job.job_id,
                    "scene_id": job.scene_id,
                    "status": status,
                    "error_code": error_code,
                    "error_text": error_text,
                    "details": error_details,
                    "failed_at": job.finished_at,
                },
            )
        )
        self._clear_active_job(job)
        self.session.flush()

    def cancellation_requested(self, owner: SceneRunJobLease) -> bool:
        status = self.session.scalar(
            select(ChapterRunJob.status).where(
                ChapterRunJob.job_id == owner.job_id,
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
                ChapterRunJob.worker_id == owner.worker_id,
                ChapterRunJob.attempt_no == owner.attempt_no,
            )
        )
        self.session.rollback()
        return status in {"cancel_requested", "cancelled"}

    def mark_cancelled(
        self,
        owner: SceneRunJobLease,
        *,
        actor_ref: str | None = None,
        reason: str | None = None,
    ) -> ChapterRunJob:
        now = utcnow()
        changed = self.session.execute(
            update(ChapterRunJob)
            .where(
                ChapterRunJob.job_id == owner.job_id,
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
                ChapterRunJob.status == "cancel_requested",
                ChapterRunJob.worker_id == owner.worker_id,
                ChapterRunJob.attempt_no == owner.attempt_no,
            )
            .values(
                status="cancelled",
                finished_at=now,
                error_code=RUN_JOB_CANCELLED,
                error_text="scene run cancelled by author",
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            self.session.rollback()
            job = self.get_job(owner.job_id)
            self.session.refresh(job)
            if job.status == "cancelled":
                return job
            raise DomainError(
                "RUN_OWNER_LEASE_LOST",
                "scene run job owner was replaced before cancellation confirmation",
                status_code=409,
                details={"job_id": owner.job_id, "status": job.status},
            )
        job = self.get_job(owner.job_id)
        self.session.refresh(job)
        state = self.session.get(SceneRunState, job.scene_id) if job.scene_id else None
        if state is not None:
            self.session.refresh(state)
            if state.active_execution_id == scene_job_execution_id(job.job_id):
                SceneRunCheckpointService(self.session).mark_cancelled(
                    state.scene_id,
                    scene_job_execution_id(job.job_id),
                )
        cancellation = (
            (job.result_summary_json or {}).get("cancellation")
            if isinstance(job.result_summary_json, dict)
            else None
        )
        cancellation = cancellation if isinstance(cancellation, dict) else {}
        effective_actor_ref = str(actor_ref or cancellation.get("actor_ref") or "author")
        effective_reason = str(
            reason if reason is not None else cancellation.get("reason") or ""
        ).strip()[:500]
        self._update_payload(job, current_step="cancelled")
        self._update_summary(job, current_step="cancelled")
        self._record_cancelled(
            job,
            actor_ref=effective_actor_ref,
            reason=effective_reason,
            cancelled_at=now,
        )
        self._clear_active_job(job)
        self.session.flush()
        return job

    def _transition_owned_job(self, owner: SceneRunJobLease, *, status: str) -> None:
        changed = self.session.execute(
            update(ChapterRunJob)
            .where(
                ChapterRunJob.job_id == owner.job_id,
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
                ChapterRunJob.status == "running",
                ChapterRunJob.worker_id == owner.worker_id,
                ChapterRunJob.attempt_no == owner.attempt_no,
            )
            .values(status=status, finished_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            self.session.rollback()
            raise DomainError(
                "RUN_OWNER_LEASE_LOST",
                "scene run job owner was replaced before terminal update",
                status_code=409,
                details={"job_id": owner.job_id, "attempt_no": owner.attempt_no},
            )
        self.session.flush()

    def _clear_active_job(self, job: ChapterRunJob) -> None:
        if not job.scene_id:
            return
        self.session.execute(
            update(SceneRunState)
            .where(
                SceneRunState.scene_id == job.scene_id,
                SceneRunState.active_run_job_id == job.job_id,
            )
            .values(active_run_job_id=None)
            .execution_options(synchronize_session=False)
        )

    def _record_cancelled(
        self,
        job: ChapterRunJob,
        *,
        actor_ref: str,
        reason: str,
        cancelled_at: str,
    ) -> None:
        self.session.add(
            OperationLog(
                event_type="scene_run_cancelled",
                object_type="scene_run_job",
                object_ref=job.job_id,
                payload_json={
                    "job_id": job.job_id,
                    "scene_id": job.scene_id,
                    "actor_ref": actor_ref,
                    "reason": reason,
                    "status_after": "cancelled",
                    "cancelled_at": cancelled_at,
                },
            )
        )

    def _latest_qc_summary(self, scene_id: str) -> dict[str, Any] | None:
        if not scene_id:
            return None
        report = self.session.execute(
            select(QcReport)
            .where(QcReport.scene_id == scene_id)
            .order_by(QcReport.created_at.desc(), QcReport.qc_report_id.desc())
        ).scalars().first()
        if report is None:
            return None
        issues = report.issues_json or []
        issue_keys: list[str] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            key = str(issue.get("issue_key") or issue.get("dimension") or issue.get("code") or "").strip()
            if key and key not in issue_keys:
                issue_keys.append(key)
        return {
            "qc_report_id": report.qc_report_id,
            "qc_type": report.qc_type,
            "pass_flag": None if report.pass_flag is None else bool(report.pass_flag),
            "resolution_code": report.resolution_code,
            "next_action": report.next_action,
            "issues": issues,
            "issue_keys": issue_keys,
            "primary_issue_key": issue_keys[0] if issue_keys else None,
        }

    @staticmethod
    def _update_payload(job: ChapterRunJob, **updates: Any) -> None:
        job.payload_json = {**dict(job.payload_json or {}), **updates}

    @staticmethod
    def _update_summary(job: ChapterRunJob, **updates: Any) -> None:
        job.result_summary_json = {**dict(job.result_summary_json or {}), **updates}


def _first_preflight_blocker(run_preflight: dict[str, Any]) -> dict[str, Any]:
    blockers = run_preflight.get("blocking_items") or []
    if blockers:
        return dict(blockers[0] or {})
    conflicts = run_preflight.get("constraint_conflicts") or []
    if conflicts:
        conflict = dict(conflicts[0] or {})
        return {
            "code": "SCENE_CONSTRAINT_CONFLICT",
            "detail": conflict.get("human_readable_reason") or "scene constraint conflict",
            "technical_hint": f"{conflict.get('required_source')} conflicts with {conflict.get('forbidden_source')}",
        }
    return {}


def _preflight_next_action(run_preflight: dict[str, Any]) -> str:
    blocker = _first_preflight_blocker(run_preflight)
    return str(blocker.get("detail") or blocker.get("technical_hint") or "Resolve preflight blockers before running.")


def _begin_immediate(session: Session) -> None:
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def remember_committed_cancellation(job_id: str) -> None:
    """Optional same-process hint; durable job status remains authoritative."""

    with _CANCELLED_JOB_REGISTRY_LOCK:
        _CANCELLED_JOB_REGISTRY.add(job_id)


def is_cancellation_cached(job_id: str) -> bool:
    with _CANCELLED_JOB_REGISTRY_LOCK:
        return job_id in _CANCELLED_JOB_REGISTRY


def recover_expired_cancel_requested_jobs(
    session: Session,
    *,
    worker_id: str,
) -> list[dict[str, Any]]:
    """Confirm abandoned cancellations only after winning the expired owner CAS."""

    from novel_system.services.llm_accounting import recover_incomplete_call

    now = datetime.now(UTC)
    job_ids = list(
        session.scalars(
            select(ChapterRunJob.job_id).where(
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
                ChapterRunJob.status == "cancel_requested",
            )
        )
    )
    session.rollback()
    recovered: list[dict[str, Any]] = []
    for job_id in job_ids:
        session.commit()
        _begin_immediate(session)
        row = session.execute(
            select(
                ChapterRunJob.scene_id,
                ChapterRunJob.worker_id,
                ChapterRunJob.attempt_no,
                ChapterRunJob.lease_expires_at,
            ).where(
                ChapterRunJob.job_id == job_id,
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
                ChapterRunJob.status == "cancel_requested",
            )
        ).one_or_none()
        if row is None:
            session.rollback()
            continue
        try:
            expiry = datetime.fromisoformat(str(row.lease_expires_at)) if row.lease_expires_at else None
        except ValueError:
            expiry = None
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if expiry is None or expiry > now:
            session.rollback()
            continue
        old_attempt = int(row.attempt_no or 0)
        recovery_expiry = (now + timedelta(seconds=owner_lease_ttl_seconds())).isoformat()
        claimed = session.execute(
            update(ChapterRunJob)
            .where(
                ChapterRunJob.job_id == job_id,
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
                ChapterRunJob.status == "cancel_requested",
                ChapterRunJob.worker_id == row.worker_id,
                ChapterRunJob.attempt_no == old_attempt,
                ChapterRunJob.lease_expires_at == row.lease_expires_at,
            )
            .values(
                worker_id=worker_id,
                attempt_no=old_attempt + 1,
                heartbeat_at=now.isoformat(),
                lease_expires_at=recovery_expiry,
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            session.rollback()
            continue
        session.commit()

        incomplete_call_ids = list(
            session.scalars(
                select(LlmCall.llm_call_id).where(
                    LlmCall.run_job_id == job_id,
                    LlmCall.accounting_status == "reserved",
                )
            )
        )
        session.rollback()
        for llm_call_id in incomplete_call_ids:
            recover_incomplete_call(session, llm_call_id)

        session.commit()
        _begin_immediate(session)
        finished_at = utcnow()
        confirmed = session.execute(
            update(ChapterRunJob)
            .where(
                ChapterRunJob.job_id == job_id,
                ChapterRunJob.job_type == JOB_TYPE_SCENE_FULL,
                ChapterRunJob.status == "cancel_requested",
                ChapterRunJob.worker_id == worker_id,
                ChapterRunJob.attempt_no == old_attempt + 1,
                ChapterRunJob.lease_expires_at == recovery_expiry,
            )
            .values(
                status="cancelled",
                finished_at=finished_at,
                error_code=RUN_JOB_CANCELLED,
                error_text="scene run cancelled by author",
            )
            .execution_options(synchronize_session=False)
        )
        if confirmed.rowcount != 1:
            session.rollback()
            continue
        job = session.get(ChapterRunJob, job_id)
        assert job is not None
        session.refresh(job)
        state = session.get(SceneRunState, row.scene_id) if row.scene_id else None
        if state is not None:
            session.refresh(state)
            if state.active_execution_id == scene_job_execution_id(job_id):
                SceneRunCheckpointService(session).mark_cancelled(
                    state.scene_id,
                    scene_job_execution_id(job_id),
                )
        SceneRunJobService._update_payload(job, current_step="cancelled")
        SceneRunJobService._update_summary(job, current_step="cancelled")
        service = SceneRunJobService(session)
        service._record_cancelled(
            job,
            actor_ref="system/recovery_sweep",
            reason="expired cancellation owner lease",
            cancelled_at=finished_at,
        )
        service._clear_active_job(job)
        session.commit()
        remember_committed_cancellation(job_id)
        recovered.append(
            {
                "job_id": job_id,
                "scene_id": row.scene_id,
                "previous_worker_id": row.worker_id,
                "previous_lease_expires_at": row.lease_expires_at,
            }
        )
    return recovered


def start_scene_run_job_worker(job_id: str) -> None:
    thread = threading.Thread(target=_run_scene_job_worker, args=(job_id,), daemon=True)
    thread.start()


def _run_scene_job_worker(job_id: str) -> None:
    session = SessionLocal()
    owner: SceneRunJobLease | None = None
    scene_id = ""
    try:
        service = SceneRunJobService(session)
        job = service.get_job(job_id)
        scene_id = str(job.scene_id or (job.payload_json or {}).get("scene_id") or "")
        owner = service.claim_running(
            job_id,
            worker_id=f"scene-job-thread:{uuid4().hex}",
            current_step="neutral_running",
            lease_seconds=owner_lease_ttl_seconds(),
        )
        service.claim_scene_active_job(owner, scene_id)
        budget_resume_parent = str(
            (job.payload_json or {}).get("budget_resume_parent_execution_id") or ""
        )
        if budget_resume_parent:
            SceneRunCheckpointService(session).acquire_budget_resume(
                scene_id,
                scene_job_execution_id(job_id),
                expected_parent_execution_id=budget_resume_parent,
            )
        session.commit()
        result = Orchestrator(session).run_scene(
            scene_id,
            author_note=str((job.payload_json or {}).get("author_note") or "") or None,
            run_policy=str((job.payload_json or {}).get("run_policy") or "reliable") or "reliable",
            execution_id=scene_job_execution_id(job_id),
            run_job_id=job_id,
            lease_renewer=owner.renew,
        )
        job = service.get_job(job_id)
        if service.cancellation_requested(owner):
            job = service.get_job(job_id)
            service.mark_cancelled(owner)
            session.commit()
            remember_committed_cancellation(job_id)
            return
        state = session.get(SceneRunState, scene_id)
        scene_status = result.get("scene_status") if isinstance(result, dict) else state.scene_status if state else ""
        if scene_status == "archived":
            service.mark_finished(job, status="completed", current_step="archived", result=result, owner=owner)
        elif scene_status == "awaiting_candidate_selection":
            # Wave 3 关键场景终选停点：候选已就绪等作者选择——任务算完成而非阻塞
            service.mark_finished(job, status="completed", current_step="awaiting_candidate_selection", result=result, owner=owner)
        elif scene_status == "quality_warning_pending_acceptance":
            # Wave 2 严格模式停点：有稿可归档，等作者显式接受——任务算完成而非阻塞
            service.mark_finished(job, status="completed", current_step="awaiting_author_acceptance", result=result, owner=owner)
        elif scene_status == "human_review_required":
            service.mark_finished(job, status="blocked", current_step="blocked", result=result, owner=owner)
        elif scene_status == "near_final_revision_required":
            service.mark_finished(job, status="blocked", current_step="acceptance_review_running", result=result if isinstance(result, dict) else {}, owner=owner)
        else:
            service.mark_finished(job, status="blocked", current_step="rewrite_running", result=result if isinstance(result, dict) else {}, owner=owner)
        session.commit()
    except DomainError as exc:
        session.rollback()
        if exc.code == RUN_JOB_CANCELLED:
            _mark_worker_cancellation(job_id, owner=owner)
        else:
            _mark_worker_failure(job_id, exc.code, exc.message, details=exc.details, owner=owner)
    except Exception as exc:  # pragma: no cover - defensive worker boundary
        session.rollback()
        _mark_worker_failure(
            job_id,
            str(getattr(exc, "error_code", None) or getattr(exc, "code", None) or "RUN_JOB_FAILED"),
            str(exc) or "run job failed",
            owner=owner,
        )
    finally:
        session.close()


def _mark_worker_failure(
    job_id: str,
    error_code: str,
    error_text: str,
    details: dict[str, Any] | None = None,
    owner: SceneRunJobLease | None = None,
) -> None:
    if owner is None:
        return
    session = SessionLocal()
    try:
        service = SceneRunJobService(session)
        job = service.get_job(job_id)
        session.refresh(job)
        if job.status == "cancel_requested":
            service.mark_cancelled(owner)
            cancelled = True
        else:
            cancelled = False
            service.mark_failed(
                job,
                error_code=error_code,
                error_text=error_text,
                details=details,
                owner=owner,
                status="blocked" if _is_budget_rejection(error_code) else "failed",
            )
        session.commit()
        if cancelled:
            remember_committed_cancellation(job_id)
    finally:
        session.close()


def _mark_worker_cancellation(
    job_id: str,
    *,
    owner: SceneRunJobLease | None,
) -> None:
    if owner is None:
        return
    session = SessionLocal()
    try:
        service = SceneRunJobService(session)
        job = service.get_job(job_id)
        session.refresh(job)
        if job.status == "cancel_requested":
            service.mark_cancelled(owner)
            session.commit()
            remember_committed_cancellation(job_id)
    finally:
        session.close()


def _is_budget_rejection(error_code: str) -> bool:
    return error_code in _BUDGET_REJECTION_CODES


def _elapsed_ms(started_at: str | None, finished_at: str | None) -> int:
    if not started_at:
        return 0
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at) if finished_at else datetime.now(start.tzinfo)
        return max(0, int((end - start).total_seconds() * 1000))
    except ValueError:
        return 0
