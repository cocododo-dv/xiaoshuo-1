"""作品（v1 作品 / 大纲计划 / 看板）的服务入口，也是书脊各模块的老地址。

这个文件过去 1,700 多行，装着作品增改、看板、物化引擎、章节运行与定稿流程和后台 worker（B08-09）。现在：

- ``materialization``：大纲计划 → 目录的物化（落位、移走空章 / 占位章、随章取回）；
- ``chapter_final_flow``：运行本章、通读确认、确认定稿、重新打开，以及后台 worker；
- ``project_payloads`` / ``project_status``：回包形状、字段归一与状态词表。

``ProjectService`` 留在这里；上面几个模块里别处（雪花工作区、路由、后台恢复、测试）从 ``projects`` 导入的名字，
这里照旧再导出。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, OutlinePlan, StoryProject
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.catalog_placeholders import (  # noqa: F401  (re-exported for callers / tests)
    AUTO_TRASHED_PLACEHOLDER_CHAPTER,
    trash_pristine_placeholder_chapters,
)
from novel_system.services.chapter_final_flow import (  # noqa: F401  (re-exported: 路由 / 后台恢复 / 测试从这里拿)
    ChapterRunnerService,
    ProjectChapterFlowService,
    _run_project_chapter_job_worker,
    start_project_chapter_run_job_worker,
)
from novel_system.services.errors import DomainError
from novel_system.services.materialization import (  # noqa: F401  (re-exported: 雪花工作区 / 测试从这里拿)
    AUTO_TRASHED_EMPTY_CHAPTER,
    materialize_outline_plan,
    trash_emptied_snowflake_chapters,
)
from novel_system.services.project_payloads import (  # noqa: F401  (re-exported: 雪花工作区 / 规划器 / 测试从这里拿)
    chapter_payload,
    optional_positive_int,
    optional_text,
    outline_plan_payload,
    project_payload,
    project_summary_payload,
    scene_payload,
)
from novel_system.services.project_status import (  # noqa: F401  (re-exported: 状态词表的老地址)
    NEXT_ACTION_BY_STATUS,
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_PENDING_REVIEW,
    PROJECT_STATUS_CHAPTER_BLOCKED,
    PROJECT_STATUS_CHAPTER_FINAL_REVIEW,
    PROJECT_STATUS_CHAPTER_READY,
    PROJECT_STATUS_CHAPTER_RUNNING,
    PROJECT_STATUS_COMPLETED,
    PROJECT_STATUS_OUTLINE_DRAFT,
    REFERENCE_SAFETY_RULES,
)
from novel_system.services.scene_lookup import require_project
from novel_system.services.snowflake_queries import latest_outline_plan
from novel_system.services.snowflake_steps import SNOWFLAKE_METHOD_VERSION
from novel_system.services.system_config import SystemConfigService
from novel_system.settings import get_settings


class ProjectService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        outline_text = str(payload.get("outline_text") or "").strip()
        if not outline_text:
            raise DomainError(
                "PROJECT_OUTLINE_REQUIRED", "outline_text is required", status_code=400
            )

        planning_mode = _planning_mode(payload.get("planning_mode"))
        title = str(payload.get("title") or "未命名小说").strip() or "未命名小说"
        project = StoryProject(
            project_id=self._next_project_id(),
            title=title,
            genre=optional_text(payload.get("genre")),
            target_word_count=optional_positive_int(payload.get("target_word_count")),
            target_chapter_count=optional_positive_int(
                payload.get("target_chapter_count")
            ),
            mark=optional_text(payload.get("mark")) or (title[:1] if title else None),
            accent=optional_text(payload.get("accent")),
            synopsis_line=optional_text(payload.get("synopsis_line")),
            words_target_daily=optional_positive_int(
                payload.get("words_target_daily")
            ),
            outline_text=outline_text,
            planning_mode=planning_mode,
            snowflake_schema_version=(
                SNOWFLAKE_METHOD_VERSION if planning_mode == "snowflake" else None
            ),
            snowflake_workflow_mode=_snowflake_workflow_mode(
                payload.get("snowflake_workflow_mode"),
                default="explore" if planning_mode == "snowflake" else "strict",
            ),
            status=PROJECT_STATUS_OUTLINE_DRAFT,
            approved_chapter_ids_json=[],
        )
        self.session.add(project)
        self.session.flush()
        return {"project": project_payload(project)}

    def list(self) -> dict[str, Any]:
        # FE-ALIGN P4: 软删作品默认不出现在任何列表（回收站统一列表单独供给）
        projects = (
            self.session.execute(
                select(StoryProject)
                .where(
                    (StoryProject.trashed_flag.is_(None))
                    | (StoryProject.trashed_flag == 0)
                )
                .order_by(
                    StoryProject.created_at.desc(), StoryProject.project_id.desc()
                )
            )
            .scalars()
            .all()
        )
        return {"items": [project_summary_payload(project) for project in projects]}

    # FE-ALIGN P2: 作品档案局部更新（PATCH /api/v2/projects/{id}/profile）。
    # 字数/进度类字段是只读派生（writing-stats / 目录 rollup），不在可写清单内。
    PROFILE_PATCHABLE = (
        "title",
        "genre",
        "mark",
        "accent",
        "synopsis_line",
        "target_word_count",
        "target_chapter_count",
        "words_target_daily",
    )

    def update_profile(
        self, project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        project = self.require_project(project_id)
        body = payload or {}
        for key in self.PROFILE_PATCHABLE:
            if key not in body:
                continue
            value = body[key]
            if key == "title":
                title = str(value or "").strip()
                if not title:
                    raise DomainError(
                        "PROJECT_TITLE_REQUIRED",
                        "title must not be empty",
                        status_code=400,
                    )
                project.title = title
            elif key in (
                "target_word_count",
                "target_chapter_count",
                "words_target_daily",
            ):
                setattr(project, key, optional_positive_int(value))
            else:
                setattr(project, key, optional_text(value))
        self.session.flush()
        return {"project": project_payload(project)}

    def dashboard(self, project_id: str) -> dict[str, Any]:
        project = self.require_project(project_id)
        latest_plan = self._latest_plan(project_id)
        chapters = self._chapter_payloads(project_id)
        current_chapter = next(
            (
                chapter
                for chapter in chapters
                if chapter["chapter_id"] == project.current_chapter_id
            ),
            None,
        )
        return {
            "project": project_payload(project),
            "latest_plan": outline_plan_payload(latest_plan) if latest_plan else None,
            "chapters": chapters,
            "current_chapter": current_chapter,
            "review_packet": ProjectChapterFlowService(self.session).review_packet(
                project, project.current_chapter_id
            ),
            "next_action": self._next_action(project, latest_plan),
            "runtime": self._runtime_readiness(),
        }


    def approve_outline_plan(self, project_id: str, plan_id: str) -> dict[str, Any]:
        project = self.require_project(project_id)
        plan = self._require_plan(project_id, plan_id)
        if plan.status == PLAN_STATUS_APPROVED:
            return self._approved_plan_result(project, plan)
        if plan.status != PLAN_STATUS_PENDING_REVIEW:
            raise DomainError(
                "OUTLINE_PLAN_NOT_REVIEWABLE", "outline plan is not pending review"
            )
        return materialize_outline_plan(self.session, project, plan)

    def require_project(self, project_id: str) -> StoryProject:
        project = require_project(self.session, project_id)
        return project

    def _approved_plan_result(
        self, project: StoryProject, plan: OutlinePlan
    ) -> dict[str, Any]:
        # 同一版计划再批一次（幂等重放）：不再动目录，回包与第一次批准同一套键（这次没取回 / 没移走任何东西）
        chapters = self._chapter_payloads(project.project_id)
        scene_count = sum(len(chapter.get("scenes") or []) for chapter in chapters)
        return {
            "project": project_payload(project),
            "plan": outline_plan_payload(plan),
            "created_chapter_count": len(chapters),
            "created_scene_count": scene_count,
            "restored_chapter_ids": [],
            "restored_scene_ids": [],
            "trashed_empty_chapters": [],
            "trashed_placeholder_chapters": [],
            "chapter_order_held": False,
        }

    def _require_plan(self, project_id: str, plan_id: str) -> OutlinePlan:
        plan = self.session.get(OutlinePlan, plan_id)
        if plan is None or plan.project_id != project_id:
            raise DomainError(
                "OUTLINE_PLAN_NOT_FOUND", "outline plan not found", status_code=404
            )
        return plan

    def _latest_plan(self, project_id: str) -> OutlinePlan | None:
        return latest_outline_plan(self.session, project_id)

    def _chapter_payloads(self, project_id: str) -> list[dict[str, Any]]:
        chapters = (
            self.session.execute(
                select(ChapterGoal)
                .where(
                    ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0
                )
                .order_by(ChapterGoal.chapter_id.asc())
            )
            .scalars()
            .all()
        )
        return [chapter_payload(self.session, chapter) for chapter in chapters]

    def _next_action(self, project: StoryProject, latest_plan: OutlinePlan | None) -> str:
        if project.status in NEXT_ACTION_BY_STATUS:
            return NEXT_ACTION_BY_STATUS[project.status]
        if latest_plan and latest_plan.status == PLAN_STATUS_PENDING_REVIEW:
            return "approve_outline_plan"
        return "generate_outline_plan"

    def _runtime_readiness(self) -> dict[str, Any]:
        settings = get_settings()
        generation_mode = "live" if settings.llm_enabled else "offline_disabled"
        missing_routes: list[str] = []
        provider_ready = False
        try:
            overview = SystemConfigService(self.session).llm_overview()
            readiness = overview.get("readiness") or {}
            provider_ready = bool(
                settings.llm_enabled
                and int(readiness.get("active_provider_count") or 0) > 0
            )
            missing_routes = [
                str(item)
                for item in (overview.get("missing_active_routes") or [])
                if str(item).strip()
            ]
            for item in overview.get("blocked_routes") or []:
                if isinstance(item, dict) and item.get("node_id"):
                    missing_routes.append(str(item["node_id"]))
        except (
            Exception
        ):  # pragma: no cover - readiness should not block dashboard loading
            provider_ready = bool(settings.llm_enabled)
        missing_routes = list(dict.fromkeys(missing_routes))
        next_setup_action = None
        if not settings.llm_enabled or not provider_ready or missing_routes:
            next_setup_action = llm_setup_action(
                llm_enabled=bool(settings.llm_enabled),
                generation_mode=generation_mode,
                missing_routes=[],
            )
        return {
            "llm_enabled": bool(settings.llm_enabled),
            "generation_mode": generation_mode,
            "provider_ready": provider_ready,
            "missing_routes": missing_routes,
            "next_setup_action": next_setup_action,
        }

    def _next_project_id(self) -> str:
        while True:
            project_id = f"PRJ_{uuid.uuid4().hex[:10].upper()}"
            if self.session.get(StoryProject, project_id) is None:
                return project_id


def _planning_mode(value: Any) -> str:
    mode = str(value or "").strip()
    if mode == "snowflake":
        return "snowflake"
    return "outline_driven"


def _snowflake_workflow_mode(value: Any, *, default: str = "strict") -> str:
    mode = str(value or default).strip().lower()
    if mode in {"strict", "explore"}:
        return mode
    return default
