"""章节运行与终稿定稿的项目流：运行本章（同步 / 后台任务）、确认定稿（含「已通读」）、重新打开，以及后台 worker。

从 ``projects.py`` 拆出（B08-09）；``projects`` 照旧再导出 ``ProjectChapterFlowService``、
``start_project_chapter_run_job_worker`` 与 ``_run_project_chapter_job_worker``（路由与测试从那里拿）。
章节运行本身在 ``chapter_runner``——测试替换运行器时 patch 本模块的 ``ChapterRunnerService``。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    OperationLog,
    StoryProject,
    utcnow,
)
from novel_system.db.session import SessionLocal
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.background_jobs import daemon_lane
from novel_system.services.chapter_manuscripts import ChapterManuscriptService
from novel_system.services.chapter_runner import ChapterRunnerService
from novel_system.services.errors import DomainError
from novel_system.services.project_payloads import project_payload
from novel_system.services.project_status import (
    NEXT_ACTION_BY_STATUS,
    PROJECT_STATUS_CHAPTER_BLOCKED,
    PROJECT_STATUS_CHAPTER_FINAL_REVIEW,
    PROJECT_STATUS_CHAPTER_READY,
    PROJECT_STATUS_CHAPTER_RUNNING,
    PROJECT_STATUS_COMPLETED,
)
from novel_system.services.run_job_leases import (
    CHAPTER_RUN_LANE,
    CHAPTER_RUN_LANE_WORKERS,
    mark_dispatched,
    unmark_dispatched,
)
from novel_system.services.scene_lookup import require_project
from novel_system.settings import get_settings


class ProjectChapterFlowService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def run_chapter(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        project = require_project(self.session, project_id)
        self._require_project_chapter(project, chapter_id)
        _require_current_chapter(project, chapter_id, _RUN_NOT_CURRENT_MESSAGE)

        project.status = PROJECT_STATUS_CHAPTER_RUNNING
        self.session.flush()
        run_result = ChapterRunnerService(self.session).run_full(chapter_id)
        project.status = _project_status_after_run(
            self.session,
            chapter_id,
            run_result.get("status"),
            failed_status=PROJECT_STATUS_CHAPTER_READY,
        )
        self.session.flush()
        return {
            "project": project_payload(project),
            "run": run_result,
        }

    def prepare_chapter_run_job(self, project_id: str, chapter_id: str) -> dict[str, Any]:
        project = require_project(self.session, project_id)
        self._require_project_chapter(project, chapter_id)
        _require_current_chapter(project, chapter_id, _RUN_NOT_CURRENT_MESSAGE)

        llm_enabled = get_settings().llm_enabled
        if not llm_enabled:
            raise DomainError(
                "LLM_DISABLED_FOR_CHAPTER_RUN",
                "LLM is disabled; enable a live model before starting chapter generation.",
                status_code=409,
                details={
                    "retryable": False,
                    "generation_mode": "offline_disabled",
                    "author_action": llm_setup_action(
                        llm_enabled=False,
                        generation_mode="offline_disabled",
                    ),
                },
            )

        run_payload, should_start_worker = ChapterRunnerService(
            self.session
        ).prepare_full_run(chapter_id)
        project.status = _project_status_after_run(self.session, chapter_id, run_payload.get("status"))
        self.session.flush()
        return {
            "project": project_payload(project),
            "run": run_payload,
            "next_action": NEXT_ACTION_BY_STATUS[project.status],
            "_start_worker": should_start_worker
            and run_payload.get("status") == "pending",
        }

    def approve_final(
        self,
        project_id: str,
        chapter_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        body = payload or {}
        project = require_project(self.session, project_id)
        self._require_project_chapter(project, chapter_id)
        _require_current_chapter(project, chapter_id, "only the current chapter final can be approved")
        revision_notes = str(body.get("revision_notes") or "").strip()
        if len(revision_notes) > 2000:
            raise DomainError(
                "CHAPTER_APPROVAL_NOTES_TOO_LONG",
                "revision_notes must be 2000 characters or fewer",
                status_code=400,
            )
        # 确认定稿是唯一的「读过并定稿」调用（批准 #10，B2-1）：「已通读」必须随它一次提交，绑定作者读到的那一份
        # （成稿中心拿到的 body_hash）。没带、或带了却没有哈希，就是什么都没绑——不能当成「读过现在这一份」
        # （HTTP 的请求模型早拦成 422，这里守直接调服务的路径）
        read_request = body.get("read_confirmation")
        if not isinstance(read_request, dict):
            raise DomainError(
                "CHAPTER_READ_CONFIRM_INVALID",
                "read_confirmation must be an object with body_hash",
                status_code=400,
            )
        read_body_hash = str(read_request.get("body_hash") or "").strip()
        if not read_body_hash:
            raise DomainError(
                "CHAPTER_READ_CONFIRM_INVALID",
                "read_confirmation.body_hash is required",
                status_code=400,
            )
        ChapterManuscriptService(self.session).require_complete(chapter_id)
        read = ChapterManuscriptService(self.session).assembled_body(chapter_id)
        # 服务器按各场当前终稿现算一次哈希；读完之后正文又变了就 409，两条审计记录写在同一个事务里
        read_confirmation = self._record_read_confirmation(
            project,
            chapter_id,
            read,
            note=read_request.get("note"),
            expected_body_hash=read_body_hash,
            actor_ref=actor_ref,
        )

        approved = list(project.approved_chapter_ids_json or [])
        if chapter_id not in approved:
            approved.append(chapter_id)
        project.approved_chapter_ids_json = approved
        chapter = self._require_project_chapter(project, chapter_id)
        chapter.state = "approved"

        next_chapter_id = self._next_chapter_id(project.project_id, chapter_id)
        if next_chapter_id:
            project.current_chapter_id = next_chapter_id
            project.status = PROJECT_STATUS_CHAPTER_READY
        else:
            project.current_chapter_id = None
            project.status = PROJECT_STATUS_COMPLETED
        approval_note = {
            "revision_notes": revision_notes,
            "actor_ref": actor_ref or "operator",
            "body_hash": read_confirmation.get("body_hash"),
            "read_confirmed_at": read_confirmation.get("confirmed_at"),
            "read_confirmed_by": read_confirmation.get("confirmed_by"),
        }
        self.session.add(
            OperationLog(
                event_type="chapter_final_approval",
                object_type="chapter",
                object_ref=chapter_id,
                payload_json={
                    "project_id": project.project_id,
                    "chapter_id": chapter_id,
                    "next_chapter_id": project.current_chapter_id,
                    "project_status": project.status,
                    **approval_note,
                },
            )
        )
        self.session.flush()
        return {
            "project": project_payload(project),
            "next_chapter_id": project.current_chapter_id,
            "approved_chapter_id": chapter_id,
            "approval_note": approval_note,
        }

    def reopen_final(
        self,
        project_id: str,
        chapter_id: str,
        *,
        reason: str,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        project = require_project(self.session, project_id)
        self._require_project_chapter(project, chapter_id)
        normalized_reason = str(reason or "").strip()
        if not normalized_reason or len(normalized_reason) > 1000:
            raise DomainError(
                "CHAPTER_REOPEN_REASON_INVALID",
                "reason must contain between 1 and 1000 characters",
                status_code=400,
            )

        approved = list(project.approved_chapter_ids_json or [])
        if chapter_id not in approved:
            raise DomainError(
                "CHAPTER_FINAL_NOT_APPROVED",
                "only a project-approved chapter can be reopened",
                status_code=409,
                details={"chapter_id": chapter_id},
            )

        reopen_index = approved.index(chapter_id)
        invalidated_chapter_ids = approved[reopen_index:]
        project.approved_chapter_ids_json = approved[:reopen_index]
        project.current_chapter_id = chapter_id
        project.status = PROJECT_STATUS_CHAPTER_READY

        for invalidated_id in invalidated_chapter_ids:
            invalidated = self.session.get(ChapterGoal, invalidated_id)
            if invalidated is None or invalidated.project_id != project.project_id:
                continue
            invalidated.state = "draft" if invalidated_id == chapter_id else "planned"

        audit_payload = {
            "project_id": project.project_id,
            "chapter_id": chapter_id,
            "reason": normalized_reason,
            "invalidated_chapter_ids": invalidated_chapter_ids,
            "remaining_approved_chapter_ids": list(
                project.approved_chapter_ids_json or []
            ),
            "actor_ref": actor_ref or "operator",
        }
        self.session.add(
            OperationLog(
                event_type="chapter_final_reopened",
                object_type="chapter",
                object_ref=chapter_id,
                payload_json=audit_payload,
            )
        )
        self.session.flush()
        return {
            "project": project_payload(project),
            "reopened_chapter_id": chapter_id,
            "invalidated_chapter_ids": invalidated_chapter_ids,
            "reason": normalized_reason,
            "actor_ref": actor_ref or "operator",
        }

    def _record_read_confirmation(
        self,
        project: StoryProject,
        chapter_id: str,
        read: dict[str, Any],
        *,
        note: Any,
        expected_body_hash: str,
        actor_ref: str,
    ) -> dict[str, Any]:
        """记一条「作者已通读」：绑定的是各场当前终稿现拼的整章正文（``read`` = ``assembled_body``）。

        ``expected_body_hash`` 是作者读的那一份的哈希（「确认定稿」一次提交时带来，调用方已验过非空）；
        和现在的正文对不上 → 409 ``CHAPTER_FINAL_BODY_CHANGED``，什么也不记。
        """
        body_hash = str(read.get("body_hash") or "")
        if not body_hash:
            raise DomainError(
                "CHAPTER_FINAL_READ_CONFIRM_UNAVAILABLE",
                "current chapter body is not available for read confirmation",
                status_code=409,
            )
        if expected_body_hash != body_hash:
            raise DomainError(
                "CHAPTER_FINAL_BODY_CHANGED",
                "这一章的正文在你通读之后又变了，请重新读一遍当前正文再确认定稿。",
                status_code=409,
                details={"chapter_id": chapter_id, "body_hash": body_hash, "expected_body_hash": expected_body_hash},
            )
        normalized_note = str(note or "").strip()
        if len(normalized_note) > 1000:
            raise DomainError(
                "CHAPTER_READ_CONFIRM_NOTE_TOO_LONG",
                "note must be 1000 characters or fewer",
                status_code=400,
            )
        confirmation = {
            "project_id": project.project_id,
            "chapter_id": chapter_id,
            "body_hash": body_hash,
            "char_count": int(read.get("char_count") or 0),
            "body_source": "assembled",
            "confirmed_at": utcnow(),
            "confirmed_by": actor_ref or "operator",
            "note": normalized_note,
        }
        self.session.add(
            OperationLog(
                event_type="chapter_final_read_confirmed",
                object_type="chapter",
                object_ref=chapter_id,
                payload_json=confirmation,
            )
        )
        self.session.flush()
        return confirmation

    def _require_project_chapter(
        self, project: StoryProject, chapter_id: str
    ) -> ChapterGoal:
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None or chapter.project_id != project.project_id:
            raise DomainError(
                "PROJECT_CHAPTER_NOT_FOUND",
                "chapter does not belong to project",
                status_code=404,
            )
        return chapter

    def _next_chapter_id(self, project_id: str, chapter_id: str) -> str | None:
        chapter_ids = [
            row[0]
            for row in self.session.execute(
                select(ChapterGoal.chapter_id)
                .where(
                    ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0
                )
                .order_by(
                    ChapterGoal.display_order.is_(None).asc(),
                    ChapterGoal.display_order.asc(),
                    ChapterGoal.chapter_id.asc(),
                )
            ).all()
        ]
        try:
            index = chapter_ids.index(chapter_id)
        except ValueError:
            return None
        return chapter_ids[index + 1] if index + 1 < len(chapter_ids) else None


_RUN_NOT_CURRENT_MESSAGE = "only the current chapter can be run from project dashboard"

def _require_current_chapter(project: StoryProject, chapter_id: str, message: str) -> None:
    """项目流（运行本章 / 确认定稿）只对「当前章」开放：按章依次推进。"""
    if project.current_chapter_id != chapter_id:
        raise DomainError("PROJECT_CHAPTER_NOT_CURRENT", message, status_code=409)


def _project_status_after_run(
    session: Session,
    chapter_id: str,
    run_status: Any,
    *,
    failed_status: str = PROJECT_STATUS_CHAPTER_BLOCKED,
) -> str:
    """一次章节运行（同步运行、后台任务的准备、后台 worker）结束后作品该停在哪个状态——三处共用一张表。

    跑完（completed）先确认整章都有权威正文，再进「本章终审」；阻断停在「待处理阻断」；任务还在排队 / 在跑
    就是「运行中」；其余回到「可以运行本章」。失败（failed）默认也停在「待处理阻断」（后台 worker 一直如此；
    任务准备拿不到 failed——失败的任务重跑时先回到 pending）；同步「运行本章」这个测试原语一直把失败放回
    「可以运行本章」，它传 ``failed_status`` 保住这一点。
    """
    status = str(run_status or "")
    if status in {"pending", "running"}:
        return PROJECT_STATUS_CHAPTER_RUNNING
    if status == "completed":
        ChapterManuscriptService(session).require_complete(chapter_id)
        return PROJECT_STATUS_CHAPTER_FINAL_REVIEW
    if status == "blocked":
        return PROJECT_STATUS_CHAPTER_BLOCKED
    if status == "failed":
        return failed_status
    return PROJECT_STATUS_CHAPTER_READY


def start_project_chapter_run_job_worker(
    project_id: str, chapter_id: str, job_id: str
) -> None:
    """项目里「运行本章」的任务交给有界的章任务车道（与不带项目的章任务同一条，B03-13/14），不再每个任务起一条
    裸线程：同一任务在本进程里只派发一次；进程在退出（车道已关）就不派发，任务行留着，下次启动的恢复接着派。
    租约由 ``ChapterRunnerService.run_full`` 登记，进程退出时就地到期。"""
    if not mark_dispatched(job_id):
        return
    lane = daemon_lane(CHAPTER_RUN_LANE, max_workers=CHAPTER_RUN_LANE_WORKERS)
    # 车道关闭时按第一个参数（任务 id）注销丢下的派发
    if not lane.submit(_run_dispatched_project_chapter_job, job_id, project_id, chapter_id):
        unmark_dispatched(job_id)


def _run_dispatched_project_chapter_job(job_id: str, project_id: str, chapter_id: str) -> None:
    try:
        _run_project_chapter_job_worker(project_id, chapter_id, job_id)
    finally:
        unmark_dispatched(job_id)


def _run_project_chapter_job_worker(
    project_id: str, chapter_id: str, job_id: str
) -> None:
    session = SessionLocal()
    try:
        project = require_project(session, project_id)
        _require_current_chapter(project, chapter_id, _RUN_NOT_CURRENT_MESSAGE)
        project.status = PROJECT_STATUS_CHAPTER_RUNNING
        session.commit()

        run_result = ChapterRunnerService(session).run_full(chapter_id)
        project = require_project(session, project_id)
        project.status = _project_status_after_run(session, chapter_id, run_result.get("status"))
        session.commit()
    except DomainError as exc:
        session.rollback()
        # Startup recovery may be invoked concurrently by multiple ASGI
        # workers.  Losing the durable chapter-job CAS is a benign duplicate
        # dispatch and must never overwrite the winning worker with FAILED.
        # RUN_OWNER_LEASE_LOST is the same situation observed after the claim:
        # another worker replaced this one, and the job now belongs to it.
        if exc.code in {"RUN_JOB_IN_PROGRESS", "RUN_JOB_NOT_CLAIMABLE", "RUN_OWNER_LEASE_LOST"}:
            return
        _mark_project_chapter_job_failed(
            job_id,
            project_id,
            chapter_id,
            exc.code,
            exc.message,
            author_action=_domain_error_author_action(exc),
        )
    except Exception as exc:  # pragma: no cover - defensive worker boundary
        session.rollback()
        _mark_project_chapter_job_failed(
            job_id,
            project_id,
            chapter_id,
            "CHAPTER_RUN_JOB_FAILED",
            str(exc) or "chapter run job failed",
        )
    finally:
        session.close()


def _domain_error_author_action(exc: DomainError) -> dict[str, Any] | None:
    details = exc.details if isinstance(exc.details, dict) else None
    action = details.get("author_action") if details else None
    return dict(action) if isinstance(action, dict) else None


def _mark_project_chapter_job_failed(
    job_id: str,
    project_id: str,
    chapter_id: str,
    error_code: str,
    error_text: str,
    *,
    author_action: dict[str, Any] | None = None,
) -> None:
    session = SessionLocal()
    try:
        job = session.get(ChapterRunJob, job_id)
        if job is not None:
            job.status = "failed"
            job.error_code = error_code
            job.error_text = error_text
            job.finished_at = utcnow()
            summary = dict(job.result_summary_json or {})
            latest_error: dict[str, Any] = {"code": error_code, "message": error_text}
            # ChapterRunnerService 在 claim 后失败时已把带 author_action 的 latest_error
            # 提交进任务行；这里是同一错误的二次落库，不能把作者指引覆盖掉。
            previous = summary.get("latest_error")
            action = author_action
            if action is None and isinstance(previous, dict) and previous.get("code") == error_code:
                previous_action = previous.get("author_action")
                action = dict(previous_action) if isinstance(previous_action, dict) else None
            if action:
                latest_error["author_action"] = action
            summary["latest_error"] = latest_error
            job.result_summary_json = summary
        project = session.get(StoryProject, project_id)
        if project is not None and project.current_chapter_id == chapter_id:
            project.status = PROJECT_STATUS_CHAPTER_BLOCKED
        session.commit()
    finally:
        session.close()
