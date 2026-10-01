"""雪花构思工作台服务（十步 → 场景计划 → 整理为章节结构）的门面。

``SnowflakeWorkspaceService`` 由几个按职责分开的混入类组成（B06-07，2026-09-30）：

- ``snowflake_workspace_view``：读侧——工作台载荷、每一步的形状与闸门、按故事序的场景计划；
- ``snowflake_step_editing``：一步草稿的写侧——生成、保存、历史、恢复；
- ``snowflake_approval``：确认与「已复核」、下游失效；
- ``snowflake_structured_sync``：草稿落到角色计划 / 场景计划行；
- ``snowflake_coach``：驻场教练、「先看 3 个方向」、作者意图要点；
- ``snowflake_triage_service``：场景分诊；
- ``snowflake_catalog_resync``：构思 → 目录的回流。

留在这里的是作品列表 / 新建、整理为章节结构（物化与确认写入），以及转到分章包的同名入口
（``_sync_chapter_plans`` / ``_build_chaptered_outline_plan`` / ``_protagonist_hint``，2026-09-30 B07-17 搬走实现）。
方法之间照旧经 ``self`` 调用，名字与签名都没变。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    OutlinePlan,
    SceneCard,
    SnowflakeScenePlan,
    SnowflakeStepRun,
    StoryProject,
)
from novel_system.services.chapter_title_sync import follow_plan_titles
from novel_system.services.errors import DomainError
from novel_system.services.projects import PLAN_STATUS_PENDING_REVIEW, ProjectService, outline_plan_payload
from novel_system.services.snowflake_approval import SnowflakeApprovalMixin
from novel_system.services.snowflake_catalog_resync import SnowflakeCatalogResyncMixin
from novel_system.services.snowflake_chaptering import (
    SnowflakeChapteringService,
    build_chaptered_outline_plan,
    protagonist_hint,
    sync_long_synopsis_chapters,
)
from novel_system.services.snowflake_coach import SnowflakeCoachMixin
from novel_system.services.snowflake_direction_brief import DirectionBriefStore
from novel_system.services.snowflake_step_editing import SnowflakeStepEditingMixin
from novel_system.services.snowflake_step_runs import StepRunStore
from novel_system.services.snowflake_structured_sync import SnowflakeStructuredSyncMixin
from novel_system.services.snowflake_triage_service import SnowflakeTriageMixin
from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService
from novel_system.services.snowflake_workspace_view import SnowflakeWorkspaceViewMixin
from novel_system.services.writing_stats import WritingStatsService


class SnowflakeWorkspaceService(
    SnowflakeStepEditingMixin,
    SnowflakeWorkspaceViewMixin,
    SnowflakeStructuredSyncMixin,
    SnowflakeApprovalMixin,
    SnowflakeCoachMixin,
    SnowflakeTriageMixin,
    SnowflakeCatalogResyncMixin,
):
    def __init__(self, session: Session) -> None:
        self.session = session
        # 一步的版本：最新版、造新版、原位改写待审版、让位、历史（B06-17）
        self._runs = StepRunStore(session)
        self._projects = ProjectService(session)
        self._chaptering = SnowflakeChapteringService(session)
        self._llm = SnowflakeWorkspaceLLMService(session)
        # 阶段 T：作者意图要点（教练对话蒸馏、作者可编辑），生成 / 候选 / 分诊 / 教练都从这里读
        self._briefs = DirectionBriefStore(session)

    def list_projects(self) -> dict[str, Any]:
        rows = self._projects.list()
        items = [
            item
            for item in rows.get("items") or []
            if str(item.get("planning_mode") or "") == "snowflake"
        ]
        # FE-ALIGN P2: 切换器/主页需要每部作品的统计摘要（只读派生，D2 服务端计算）。
        stats = WritingStatsService(self.session)
        written = self._chapters_written_by_project([str(item.get("project_id") or "") for item in items])
        for item in items:
            project_id = item.get("project_id")
            item["stats"] = stats.stats_payload(project_id)
            item["chapters_written"] = written.get(str(project_id or ""), 0)
        return {"items": items}

    def _chapters_written_by_project(self, project_ids: list[str]) -> dict[str, int]:
        """FE-ALIGN P3：已动笔章数 = 有正文字数 rollup 或状态非 planned/todo 的章（回收站里的章不算）。

        两条查询算完所有作品（B06-18）：以前每部作品每一章一条查询，章越多作品列表越慢。
        """
        ids = [project_id for project_id in dict.fromkeys(project_ids) if project_id]
        if not ids:
            return {}
        chapters = self.session.execute(
            select(ChapterGoal.chapter_id, ChapterGoal.project_id, ChapterGoal.state).where(
                ChapterGoal.project_id.in_(ids), ChapterGoal.trashed_flag == 0
            )
        ).all()
        words: dict[str, int] = {}
        chapter_ids = [chapter_id for chapter_id, _project_id, _state in chapters]
        for start in range(0, len(chapter_ids), 500):
            rows = self.session.execute(
                select(SceneCard.chapter_id, func.sum(SceneCard.words_current))
                .where(SceneCard.chapter_id.in_(chapter_ids[start : start + 500]), SceneCard.trashed_flag == 0)
                .group_by(SceneCard.chapter_id)
            ).all()
            words.update({str(chapter_id): int(total or 0) for chapter_id, total in rows})
        written: dict[str, int] = {}
        for chapter_id, project_id, state in chapters:
            if words.get(str(chapter_id), 0) > 0 or str(state or "planned") not in {"planned", "todo"}:
                written[str(project_id)] = written.get(str(project_id), 0) + 1
        return written

    def create_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._projects.create({**(payload or {}), "planning_mode": "snowflake", "snowflake_workflow_mode": (payload or {}).get("snowflake_workflow_mode") or "explore"})
        project = result["project"]
        return {
            "project": project,
            "workspace": self.mutation_workspace(project["project_id"]),
        }

    # ------------------------------------------------ 阶段 X：确认即同步
    #
    # 物化之后，构思和目录是两份数据：作者在 09 / 10 改了一场、点了「确认本步」，运行时失效当场就按
    # 改动范围标了（``invalidate_for_snowflake_step``），可场景卡还停在旧三拍上，要等作者再去横幅上点一次
    # 「同步到目录」——不点，写作台 / AI 起草台读到的就是旧卡，作者感受到的是「构思和台子是两套东西」。
    # 确认一步 = 这一版构思定了，目录就该跟上。只有两种卡不自动动，留给显式回流的差异预览：
    #   - 这一场的规划还没确认 / 被上游标了需复核（``plan.status != approved``）；
    #   - 这次同步要把一张**已经有活儿**的卡送进回收站（略过 / 该重写 / 待删）。
    # （阶段 X 还有第三种：作者在台子上改过这张卡的设计。阶段 Y 起构思里那一行还在的雪花场景卡，设计
    #  只能在构思里改——目录 API 不收——台面与构思不会再各说各话，这一条也就没有了。）
    # 要搬去的章目录里还没有的卡照常同步内容、只是不搬（与显式回流同一口径），回包的 notice 会说清楚。

    def materialize(self, project_id: str, payload: dict[str, Any] | None = None, *, actor_ref: str = "operator") -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        # 分章面板确认时把 {chapters, assignments} 一并带来：先落分章再物化，同一事务，
        # 不留「分了章但没物化」或「物化了但用的是上一版分章」的中间态。
        if body.get("assignments"):
            self._chaptering.save(project.project_id, body, actor_ref=actor_ref)
        elif body.get("strategy"):
            # 脚本 / API 调用方不必往返预览，但必须**显式指名策略** —— 这仍是一次
            # 有主的分章决定，而不是服务端替作者悄悄决定（那正是本次要修掉的老毛病）。
            self._chaptering.autoassign(project.project_id, str(body["strategy"]), actor_ref=actor_ref)
        else:
            # 既没带分章也没指名策略：只做「派生已经存在的事实」——目录里已有的章、
            # 或场景行自己带着的章归属。派生不出来（前端那一路所有场都是 CH01）时
            # 什么也不做，下面的闸门会把作者送进分章面板。
            self._chaptering.ensure_chapter_plans(project.project_id)
        gate = self._workspace_payload(project.project_id, lean=True).get("materialization_gate") or {}
        if gate.get("status") == "blocked":
            raise DomainError(
                "SNOWFLAKE_NOT_READY",
                "雪花工作台还没有通过整理前的检查，暂时无法整理为章节结构；请查看具体阻断项后重试。",
                status_code=409,
                details={"materialization_gate": gate},
            )
        scene_plans = self._scene_plans(project.project_id)
        if not scene_plans:
            raise DomainError("SNOWFLAKE_SCENES_REQUIRED", "还没有可用的场景计划，无法整理为章节结构。", status_code=409)
        plan = OutlinePlan(
            plan_id=f"outline_plan_{project.project_id}_{self._next_plan_version(project.project_id):02d}_{uuid.uuid4().hex[:8]}",
            project_id=project.project_id,
            version=self._next_plan_version(project.project_id),
            status=PLAN_STATUS_PENDING_REVIEW,
            plan_json=self._build_chaptered_outline_plan(project, scene_plans),
        )
        project.status = "outline_review"
        self.session.add(plan)
        self.session.flush()
        return {"plan": outline_plan_payload(plan), "workspace": self.mutation_workspace(project.project_id)}

    def _build_chaptered_outline_plan(
        self,
        project: StoryProject,
        scene_plans: list[SnowflakeScenePlan],
    ) -> dict[str, Any]:
        """按构思侧章表分组产出 OutlinePlan（实现见 ``snowflake_chaptering.outline_plan``）。"""
        return build_chaptered_outline_plan(
            self.session,
            project,
            scene_plans,
            protagonist=self._protagonist_hint(project.project_id),
            excluded=self._excluded_scene_plan_ids(project.project_id),
        )

    def _protagonist_hint(self, project_id: str) -> dict[str, str] | None:
        """全书主角（04 显式指定的优先，否则定位为主角的第一人；见 ``snowflake_chaptering.outline_plan``）。"""
        return protagonist_hint(self.session, project_id)

    # ------------------------------------------------ 分章面板不物化的两个写入口（B06-18：路由只转一手）

    def save_chapter_plan(
        self, project_id: str, payload: dict[str, Any] | None = None, *, actor_ref: str = "operator"
    ) -> dict[str, Any]:
        """分章面板「只保存章表」（``PATCH …/chapter-plan``，不物化）：前面有步骤待确认、「确认写入」点不动时，
        作者照样能改章名 / 章摘要 / 章界并存下来（R11：07 的章表改成只读镜像之后，这是改章表的门）。
        章名当场跟到 09 的章头；目录只跟作者起的名字（目录里还是上次播下去的名字时——与 07 改章名同一条规矩），
        并章 / 拆章后按新章序重编的「第 N 章」等确认写入随物化落到目录（``follow_plan_titles``）。"""
        saved = self._chaptering.save(project_id, payload, actor_ref=actor_ref)
        follow_plan_titles(self.session, project_id)
        return {**saved, "workspace": self.mutation_workspace(project_id)}

    def resolve_orphaned_scene(
        self, project_id: str, scene_plan_id: str, *, action: str, actor_ref: str = "operator"
    ) -> dict[str, Any]:
        """处置一个孤儿场（discard / keep，见 ``snowflake_chaptering.orphans``），回包带刷新后的工作台。"""
        resolved = self._chaptering.resolve_orphan(project_id, scene_plan_id, action=action, actor_ref=actor_ref)
        return {**resolved, "workspace": self.mutation_workspace(project_id)}

    def approve_outline(self, project_id: str) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        latest_plan = self._latest_plan(project.project_id)
        if latest_plan is None:
            raise DomainError("OUTLINE_PLAN_NOT_FOUND", "未找到大纲计划。", status_code=404)
        result = self._projects.approve_outline_plan(project.project_id, latest_plan.plan_id)
        workspace = self.mutation_workspace(project.project_id)
        return {
            "plan": result["plan"],
            "workspace": workspace,
            "created_chapter_count": result.get("created_chapter_count", 0),
            "created_scene_count": result.get("created_scene_count", 0),
            # 阶段 W：重新分章后变空的旧章已移入回收站 / 这一版又用到的章已从回收站取回——前端如实告诉作者
            "trashed_empty_chapters": result.get("trashed_empty_chapters", []),
            "restored_chapter_ids": result.get("restored_chapter_ids", []),
            "restored_scene_ids": result.get("restored_scene_ids", []),
            # 阶段 X：手建的空白占位章（「第 1 章 / 开场」，一个字没写）已移入回收站
            "trashed_placeholder_chapters": result.get("trashed_placeholder_chapters", []),
            # 阶段 Y：目录里有已终审的章、按章表排会挪动它 → 这次没排章序，新章接在最后
            "chapter_order_held": bool(result.get("chapter_order_held")),
        }

    # ------------------------------------------------ 场景卡的章内顺序（回流）
    #
    # 目录里一章的场景卡 = 有场景计划的卡（顺序永远跟故事序走）+ 作者在章节编排里手加的卡（计划外，
    # 原来跟在哪张卡后面就还跟在哪）。回流不再逐张写 scene_seq：那一列受唯一索引
    # (chapter_id, scene_seq) 约束，两张卡对调、或一张卡搬进别的章，第一条 UPDATE 就会撞上还没挪走的
    # 那一张（500「database operation failed」）。改成：先把要动的章里所有活跃卡停到高位序号，
    # 改完内容 / 章归属之后，再按最终顺序一次落位。

    def _sync_chapter_plans(
        self,
        project_id: str,
        draft: dict[str, Any],
        run: SnowflakeStepRun,
        *,
        approved: bool,
    ) -> dict[str, Any] | None:
        """07 长篇大纲里的章表 → 构思侧章表行（实现见 ``snowflake_chaptering.outline_sync``；章表收缩时返回摘要）。"""
        return sync_long_synopsis_chapters(self.session, project_id, draft, run, approved=approved)
