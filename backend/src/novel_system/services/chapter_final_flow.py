"""章节运行与终稿定稿的项目流：运行本章（同步 / 后台任务）、通读确认、确认定稿、重新打开，以及后台 worker。

从 ``projects.py`` 拆出（B08-09）；``projects`` 照旧再导出 ``ProjectChapterFlowService``、
``start_project_chapter_run_job_worker`` 与 ``_run_project_chapter_job_worker``（路由与测试从那里拿）。
章节运行本身在 ``chapter_runner``——测试替换运行器时 patch 本模块的 ``ChapterRunnerService``。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    ChapterRunJob,
    FinalScene,
    OperationLog,
    QcReport,
    SceneCard,
    SceneRunState,
    StoryProject,
    utcnow,
)
from novel_system.db.session import SessionLocal
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.background_jobs import daemon_lane
from novel_system.services.chapter_manuscripts import ChapterManuscriptService
from novel_system.services.chapter_runner import ChapterRunnerService
from novel_system.services.errors import DomainError
from novel_system.services.project_payloads import project_payload, qc_issue_summaries
from novel_system.services.project_status import (
    NEXT_ACTION_BY_STATUS,
    PROJECT_STATUS_CHAPTER_BLOCKED,
    PROJECT_STATUS_CHAPTER_FINAL_REVIEW,
    PROJECT_STATUS_CHAPTER_READY,
    PROJECT_STATUS_CHAPTER_RUNNING,
    PROJECT_STATUS_COMPLETED,
    REFERENCE_SAFETY_RULES,
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
            "review_packet": self.review_packet(project, chapter_id),
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
            "review_packet": self.review_packet(project, chapter_id),
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
        read_request = body.get("read_confirmation")
        read_body_hash: str | None = None
        if read_request is not None:
            if not isinstance(read_request, dict):
                raise DomainError(
                    "CHAPTER_READ_CONFIRM_INVALID",
                    "read_confirmation must be an object with body_hash",
                    status_code=400,
                )
            # 一次提交的「已通读」绑定的是作者读到的那一份（成稿中心拿到的 body_hash）：没带哈希就什么都没绑，
            # 不能当成「读过现在这一份」记下来（HTTP 的请求模型早拦成 422，这里守直接调服务的路径）
            read_body_hash = str(read_request.get("body_hash") or "").strip()
            if not read_body_hash:
                raise DomainError(
                    "CHAPTER_READ_CONFIRM_INVALID",
                    "read_confirmation.body_hash is required",
                    status_code=400,
                )
        ChapterManuscriptService(self.session).require_publishable(chapter_id)
        read = ChapterManuscriptService(self.session).assembled_body(chapter_id)
        if read_request is not None:
            # 批准 #10：「已通读」随「确认定稿」一次提交——服务器按各场当前终稿现算一次哈希；读完之后正文又变了
            # 就 409，两条审计记录写在同一个事务里
            read_confirmation = self._record_read_confirmation(
                project,
                chapter_id,
                read,
                note=read_request.get("note"),
                expected_body_hash=read_body_hash,
                actor_ref=actor_ref,
            )
        else:
            read_confirmation = self._require_current_read_confirmation(project, chapter_id, read)

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

    def confirm_read(
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
        _require_current_chapter(project, chapter_id, "only the current chapter final can be confirmed")
        ChapterManuscriptService(self.session).require_publishable(chapter_id)
        read = ChapterManuscriptService(self.session).assembled_body(chapter_id)
        return self._record_read_confirmation(
            project,
            chapter_id,
            read,
            note=body.get("note"),
            expected_body_hash=None,
            actor_ref=actor_ref,
        )

    def _record_read_confirmation(
        self,
        project: StoryProject,
        chapter_id: str,
        read: dict[str, Any],
        *,
        note: Any,
        expected_body_hash: str | None,
        actor_ref: str,
    ) -> dict[str, Any]:
        """记一条「作者已通读」：绑定的是各场当前终稿现拼的整章正文（``read`` = ``assembled_body``）。

        ``expected_body_hash`` 是作者读的那一份的哈希（「确认定稿」一次提交时带来，调用方已验过非空；
        两步走的「通读确认」没有它，给 None）；和现在的正文对不上 → 409 ``CHAPTER_FINAL_BODY_CHANGED``，什么也不记。
        """
        body_hash = str(read.get("body_hash") or "")
        if not body_hash:
            raise DomainError(
                "CHAPTER_FINAL_READ_CONFIRM_UNAVAILABLE",
                "current chapter body is not available for read confirmation",
                status_code=409,
            )
        if expected_body_hash is not None and expected_body_hash != body_hash:
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


    def review_packet(
        self, project: StoryProject, chapter_id: str | None
    ) -> dict[str, Any] | None:
        """v1 看板 / 运行本章回包里的终审材料：只在项目停在「本章终审」时给（与看板的 next_action 同一口径）。

        能不能通读确认、能不能定稿**不看这个状态**——那两步直接读各场当前终稿（``assembled_body``）。
        「本章终审」只有「运行本章」会置上；作者在写作台写完、逐场晋升的章走不到这里，过去于是永远定不了稿（B08-01）。
        """
        if not chapter_id or project.status != PROJECT_STATUS_CHAPTER_FINAL_REVIEW:
            return None
        chapter = self.session.get(ChapterGoal, chapter_id)
        if chapter is None:
            return None
        latest_job = self._latest_job(chapter_id)
        issues_summary = []
        latest_error = (
            (latest_job.result_summary_json or {}).get("latest_error")
            if latest_job
            else None
        )
        if latest_error:
            issues_summary.append(latest_error)
        manuscript = ChapterManuscriptService(self.session).manuscript_detail(
            chapter_id
        )
        aggregate = manuscript.get("aggregate") or None
        assembled = manuscript.get("assembled") or {}
        # 正文取各场当前终稿现拼（成稿中心读的那一份），不取章汇总：汇总可能落后于逐场终稿（R13）
        body = str(assembled.get("content") or "")
        body_source = "assembled" if body else "empty"
        char_count = int(assembled.get("char_count") or len(body))
        aggregate_row_id = aggregate.get("row_id") if aggregate else None
        missing_scene_ids = list(assembled.get("missing_scene_ids") or [])
        completion_status = manuscript.get("completion_status") or "empty"
        body_empty_reason = None
        if not body:
            body_empty_reason = (
                "no_generated_scenes"
                if completion_status == "empty"
                else "manuscript_body_empty"
            )
        body_hash = str(manuscript.get("body_hash") or "")
        read_confirmation = (
            self._latest_read_confirmation(
                project.project_id, chapter.chapter_id, body_hash
            )
            if body_hash
            else None
        )
        scene_reviews = self._scene_reviews(chapter.chapter_id)
        return {
            "chapter_id": chapter.chapter_id,
            "chapter_goal": chapter.chapter_goal,
            "body": body,
            "body_hash": body_hash,
            "read_confirmation": read_confirmation,
            "body_source": body_source,
            "char_count": char_count,
            "body_empty_reason": body_empty_reason,
            "completion_status": completion_status,
            "comparison_status": manuscript.get("comparison_status"),
            "missing_scene_ids": missing_scene_ids,
            "missing_scene_labels": self._missing_scene_labels(
                missing_scene_ids, scene_reviews
            ),
            "scene_coverage": self._scene_coverage(scene_reviews),
            "target_word_count_band": self._target_word_count_band(project),
            "aggregate_row_id": aggregate_row_id,
            "source_safety_scan": manuscript.get("source_safety_scan"),
            "scene_reviews": scene_reviews,
            "issues_summary": issues_summary,
            "run_status": latest_job.status if latest_job else "idle",
            "reference_safety": list(REFERENCE_SAFETY_RULES),
        }

    def _require_current_read_confirmation(
        self, project: StoryProject, chapter_id: str, read: dict[str, Any]
    ) -> dict[str, Any]:
        body_hash = str(read.get("body_hash") or "")
        confirmation = (
            self._latest_read_confirmation(project.project_id, chapter_id, body_hash)
            if body_hash
            else None
        )
        if not body_hash or not confirmation:
            raise DomainError(
                "CHAPTER_FINAL_READ_CONFIRM_REQUIRED",
                "read and confirm the current chapter body before approving final",
                status_code=409,
                details={"chapter_id": chapter_id, "body_hash": body_hash},
            )
        return confirmation

    def _latest_read_confirmation(
        self, project_id: str, chapter_id: str, body_hash: str
    ) -> dict[str, Any] | None:
        if not body_hash:
            return None
        # 这一章的通读记录 + 本作品的「重新打开」记录（重开会作废被撤销那几章的通读）；
        # 别的作品的重开记录不读（过去是把全库的重开事件都拉回来再在内存里挑）
        rows = (
            self.session.execute(
                select(OperationLog)
                .where(
                    OperationLog.object_type == "chapter",
                    or_(
                        and_(
                            OperationLog.event_type == "chapter_final_read_confirmed",
                            OperationLog.object_ref == chapter_id,
                        ),
                        and_(
                            OperationLog.event_type == "chapter_final_reopened",
                            func.json_extract(OperationLog.payload_json, "$.project_id") == project_id,
                        ),
                    ),
                )
                .order_by(
                    OperationLog.created_at.desc(), OperationLog.operation_id.desc()
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            payload = dict(row.payload_json or {})
            if row.event_type == "chapter_final_reopened":
                if payload.get("project_id") != project_id:
                    continue
                invalidated_ids = list(payload.get("invalidated_chapter_ids") or [])
                if chapter_id == row.object_ref or chapter_id in invalidated_ids:
                    return None
                continue
            if (
                payload.get("project_id") != project_id
                or payload.get("chapter_id") != chapter_id
            ):
                continue
            if payload.get("body_hash") != body_hash:
                continue
            return {
                "chapter_id": chapter_id,
                "body_hash": body_hash,
                "confirmed_at": payload.get("confirmed_at") or row.created_at,
                "confirmed_by": payload.get("confirmed_by")
                or payload.get("actor_ref")
                or "operator",
                "note": payload.get("note") or "",
                "operation_id": row.operation_id,
            }
        return None

    def _scene_reviews(self, chapter_id: str) -> list[dict[str, Any]]:
        scenes = (
            self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
            )
            .scalars()
            .all()
        )
        reviews: list[dict[str, Any]] = []
        for scene in scenes:
            state = self.session.get(SceneRunState, scene.scene_id)
            final_row = (
                self.session.get(FinalScene, state.current_final_scene_row_id)
                if state and state.current_final_scene_row_id
                else None
            )
            qc_report = self._latest_scene_qc_report(scene.scene_id)
            qc_issues = qc_issue_summaries(qc_report)
            body = final_row.content if final_row is not None else ""
            excerpt = " ".join(str(body or "").split())
            reviews.append(
                {
                    "scene_id": scene.scene_id,
                    "scene_seq": scene.scene_seq,
                    "title": scene.scene_goal or scene.hook or scene.scene_id,
                    "body_excerpt": excerpt[:240],
                    "char_count": len(body or ""),
                    "missing": not bool(body),
                    "issues_summary": qc_issues,
                    "evidence_summary": [
                        issue.get("evidence")
                        for issue in qc_issues
                        if issue.get("evidence")
                    ],
                    "suggested_actions": [
                        issue.get("suggested_action")
                        for issue in qc_issues
                        if issue.get("suggested_action")
                    ],
                    "qc_summary": {
                        "qc_report_id": (
                            qc_report.qc_report_id if qc_report is not None else None
                        ),
                        "status": qc_report.status if qc_report is not None else "",
                        "next_action": (
                            qc_report.next_action if qc_report is not None else ""
                        ),
                        "issue_count": len(qc_issues),
                    },
                    "current_decision": "pending",
                }
            )
        return reviews

    def _latest_scene_qc_report(self, scene_id: str) -> QcReport | None:
        return (
            self.session.execute(
                select(QcReport)
                .where(QcReport.scene_id == scene_id)
                .order_by(QcReport.created_at.desc(), QcReport.qc_report_id.desc())
            )
            .scalars()
            .first()
        )

    @staticmethod
    def _scene_coverage(scene_reviews: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(scene_reviews)
        completed = sum(
            1 for item in scene_reviews if int(item.get("char_count") or 0) > 0
        )
        return {
            "completed_count": completed,
            "total_count": total,
            "percent": round((completed / total) * 100) if total else 0,
        }

    @staticmethod
    def _missing_scene_labels(
        missing_scene_ids: list[str], scene_reviews: list[dict[str, Any]]
    ) -> list[str]:
        by_id = {str(item.get("scene_id") or ""): item for item in scene_reviews}
        missing_ids = {str(item or "") for item in missing_scene_ids if str(item or "")}
        for item in scene_reviews:
            if item.get("missing"):
                scene_id = str(item.get("scene_id") or "")
                if scene_id:
                    missing_ids.add(scene_id)
        labels: list[str] = []
        for scene_id in sorted(missing_ids):
            item = by_id.get(scene_id)
            if item is None:
                labels.append(scene_id)
                continue
            seq = item.get("scene_seq")
            title = str(item.get("title") or scene_id).strip() or scene_id
            prefix = f"第 {seq} 场" if seq else "场景"
            labels.append(f"{prefix}：{title}")
        return labels

    @staticmethod
    def _target_word_count_band(project: StoryProject) -> dict[str, Any] | None:
        target_word_count = int(project.target_word_count or 0)
        target_chapter_count = int(project.target_chapter_count or 0)
        if target_word_count <= 0 or target_chapter_count <= 0:
            return None
        per_chapter = max(1, round(target_word_count / target_chapter_count))
        lower = max(1, round(per_chapter * 0.85))
        upper = max(lower, round(per_chapter * 1.15))
        return {
            "target": per_chapter,
            "min": lower,
            "max": upper,
            "label": f"{lower}-{upper} 字",
        }

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

    def _latest_job(self, chapter_id: str) -> ChapterRunJob | None:
        return (
            self.session.execute(
                select(ChapterRunJob)
                .where(ChapterRunJob.chapter_id == chapter_id)
                .order_by(ChapterRunJob.created_at.desc(), ChapterRunJob.job_id.desc())
            )
            .scalars()
            .first()
        )


_RUN_NOT_CURRENT_MESSAGE = "only the current chapter can be run from project dashboard"

def _require_current_chapter(project: StoryProject, chapter_id: str, message: str) -> None:
    """项目流（运行本章 / 通读确认 / 定稿）只对「当前章」开放：按章依次推进。"""
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
