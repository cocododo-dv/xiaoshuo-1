"""雪花构思工作台服务（十步 → 场景计划 → 整理为章节结构）的门面。

``SnowflakeWorkspaceService`` 由几个按职责分开的混入类组成（B06-07，2026-09-30）：

- ``snowflake_workspace_view``：读侧——工作台载荷、每一步的形状与闸门、按故事序的场景计划；
- ``snowflake_step_editing``：一步草稿的写侧——生成、保存、历史、恢复；
- ``snowflake_approval``：确认与「已复核」、下游失效；
- ``snowflake_structured_sync``：草稿落到角色计划 / 场景计划行；
- ``snowflake_coach``：驻场教练、「先看 3 个方向」、作者意图要点；
- ``snowflake_triage_service``：场景分诊；
- ``snowflake_catalog_resync``：构思 → 目录的回流。

留在这里的是作品列表 / 新建、整理为章节结构（物化与确认写入），以及分章的两条缝（``_sync_chapter_plans`` /
``_build_chaptered_outline_plan``，分章包之后整体搬走）。方法之间照旧经 ``self`` 调用，名字与签名都没变。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from novel_system.db.models import (
    ChapterGoal,
    OperationLog,
    OutlinePlan,
    SceneCard,
    SnowflakeCharacterPlan,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    SnowflakeStepRun,
    StoryProject,
    utcnow,
)
from novel_system.services.catalog import normalize_act
from novel_system.services.chapter_title_sync import follow_plan_titles
from novel_system.services.errors import DomainError
from novel_system.services.projects import PLAN_STATUS_PENDING_REVIEW, ProjectService, outline_plan_payload
from novel_system.services.snowflake_approval import SnowflakeApprovalMixin
from novel_system.services.snowflake_catalog_resync import SnowflakeCatalogResyncMixin
from novel_system.services.snowflake_chaptering import (
    SnowflakeChapteringService,
    mint_chapter_row_uid,
    parse_outline_chapters,
)
from novel_system.services.snowflake_coach import SnowflakeCoachMixin
from novel_system.services.snowflake_direction_brief import DirectionBriefStore
from novel_system.services.snowflake_draft_merge import merge_member_lists, overlay_keeping_members
from novel_system.services.snowflake_scene_brief import scene_card_beats, scene_title_seed, scene_writer_brief
from novel_system.services.snowflake_scene_rows import scene_plan_payload
from novel_system.services.snowflake_step_catalog import SUMMARY_LENGTH_BAND, effective_rendering_mode
from novel_system.services.snowflake_step_diagnosis import is_protagonist_role
from novel_system.services.snowflake_step_editing import SnowflakeStepEditingMixin
from novel_system.services.snowflake_step_runs import StepRunStore, would_wipe_story
from novel_system.services.snowflake_structured_sync import SnowflakeStructuredSyncMixin
from novel_system.services.snowflake_triage import EXCLUDED_TRIAGE_STATUSES  # noqa: F401 — 测试从本模块 import
from novel_system.services.snowflake_triage import coerce_triage_status
from novel_system.services.snowflake_triage_service import SnowflakeTriageMixin
from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService
from novel_system.services.snowflake_workspace_view import SnowflakeWorkspaceViewMixin
from novel_system.services.value_coercion import int_or_default
from novel_system.services.writing_stats import WritingStatsService

# 旧名：测试从本模块 import（实现各在叶子模块）
_scene_card_beats = scene_card_beats
_coerce_triage_status = coerce_triage_status
_is_protagonist_role = is_protagonist_role
_merge_dicts_keeping_members = overlay_keeping_members
_merge_member_lists = merge_member_lists
_would_wipe_story = would_wipe_story


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
        for item in items:
            project_id = item.get("project_id")
            item["stats"] = stats.stats_payload(project_id)
            item["chapters_written"] = self._chapters_written(project_id)
        return {"items": items}

    def _chapters_written(self, project_id: str) -> int:
        """FE-ALIGN P3：已动笔章数 = 有正文字数 rollup 或状态非 planned/todo 的章。"""
        chapters = self.session.execute(
            select(ChapterGoal).where(
                ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0
            )
        ).scalars().all()
        written = 0
        for chapter in chapters:
            scene_words = self.session.execute(
                select(SceneCard.words_current).where(
                    SceneCard.chapter_id == chapter.chapter_id, SceneCard.trashed_flag == 0
                )
            ).scalars().all()
            if sum(int(w or 0) for w in scene_words) > 0 or str(chapter.state or "planned") not in {"planned", "todo"}:
                written += 1
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
        """按构思侧章表分组产出 OutlinePlan（P2）。

        与被它取代的 v1 规划器 ``_build_outline_plan``（2026-09-30 退役）的关键差别：章不再从
        场景行的 ``chapter_id`` 反推（那条路上所有场都写着 ``…_CH01``，于是全书一章，
        章标题就是章 id 字符串），而是读作者在 07 里真正编出来的章 —— 标题、幕、脊柱、
        章目标都来自那里。
        """
        chapters = self._chaptering.ensure_chapter_plans(project.project_id)
        # 阶段 B（雪花评估 B5）：挫折 / 胜利以主角衡量，不以 POV 衡量——Ingermanson：POV 是对手时，
        # 对手得手就是主角的挫折。把角色摘要表里的主角带进每一场的简报，结构简报会渲染它。
        protagonist = self._protagonist_hint(project.project_id)
        # 阶段 N：作者裁定该重写 / 待删的场不物化（三拍留在构思里，回流时再补建或取回）。
        excluded = self._excluded_scene_plan_ids(project.project_id)
        grouped: dict[str, list[SnowflakeScenePlan]] = {chapter.chapter_plan_id: [] for chapter in chapters}
        for scene in scene_plans:
            key = scene.chapter_plan_id or ""
            if key in grouped and scene.scene_plan_id not in excluded:
                grouped[key].append(scene)

        chapter_payloads: list[dict[str, Any]] = []
        for index, chapter in enumerate(chapters, start=1):
            # 阶段 I：页面上略过的反应场不物化——没有正文要写；它的三拍经下一场的设计上下文到达写手。
            # 章内顺序 = 故事序：scene_plans 进来时已经按 09 场景列表的行序排好，这里只分组、不再重排。
            members = [
                item
                for item in grouped[chapter.chapter_plan_id]
                if effective_rendering_mode(item.scene_type, item.rendering_mode) != "skip"
            ]
            if not members:
                continue  # 空章不落库：预览里已经就此告警过，作者选择保留就是不要它
            # 阶段 Y：目录里的章 id 钉在章计划行上（不再按章序算）——这一章以前物化过，就还是目录里的那一行
            chapter_id = self._chaptering.catalog_chapter_id(chapter)
            goal = (chapter.chapter_goal or chapter.summary or "").strip() or f"推进本章：{chapter.title or chapter_id}"
            scenes_payload: list[dict[str, Any]] = []
            for seq, scene in enumerate(members, start=1):
                detail = scene_plan_payload(scene)
                if protagonist is not None:
                    detail["protagonist_hint"] = protagonist["display_name"]
                    detail["protagonist_character_id"] = protagonist["character_id"]
                scene_type = detail.get("primary_form") or "proactive"
                # 阶段 C / N：summary 场（两种形态都可以）拿到数值篇幅带（起草 / 长度补丁按数值硬约束），
                # 并把呈现方式写进简报，结构简报会渲染它。
                rendering_mode = effective_rendering_mode(scene_type, detail.get("rendering_mode"))
                detail["rendering_mode"] = rendering_mode
                detail["target_length_band"] = (
                    SUMMARY_LENGTH_BAND if rendering_mode == "summary" else (detail.get("target_length_band") or "medium")
                )
                scenes_payload.append(
                    {
                        "scene_id": scene.scene_id,
                        "chapter_id": chapter_id,
                        "scene_seq": seq,
                        "pov_character_id": detail.get("pov_character_id") or None,
                        "onstage_chars_json": detail.get("onstage_chars_json") or [],
                        "location": detail.get("location") or None,
                        "scene_goal": detail.get("summary") or detail.get("title") or goal,
                        "beats_json": scene_card_beats(scene_type, detail),
                        # 阶段 F：摘要不再冒充「必须包含」的硬约束（它本来就含挫折，写成硬约束会让
                        # 硬 QC 拿一句概括去卡正文）；钩子 / 离场变化没写就留空，简报只陈述作者写过的。
                        "must_include_text": detail.get("must_include_text") or "",
                        # 2026-09-20：不再把防抄袭政策句写进 forbidden_text——那个字段是「按字面查的禁用词」，
                        # 这句话会被拆出禁用词「人物」，正文里一出现就是 Q1 硬伤（见 qc_constraints）。
                        "forbidden_text": "",
                        # 与 _scene_card_resync_patch 同一配方：规划行没写离场变化就用挫折 / 决定。
                        # 两边配方不同时，刚物化完的每一场都会被报成「待同步」（纯假阳性）。
                        "exit_change": (
                            detail.get("exit_change")
                            or detail.get("setback")
                            or detail.get("decision")
                            or ""
                        ),
                        "hook": detail.get("hook") or "",
                        "target_length_band": detail["target_length_band"],
                        "primary_form": scene_type,
                        "scene_type": scene_type,
                        "is_chapter_last": 1 if seq == len(members) else 0,
                        "writer_brief_json": {
                            **scene_writer_brief(scene_type, detail),
                            # 阶段 X：构思里起过的短题名随卡进目录（整句摘要不算题名）
                            **scene_title_seed(detail),
                        },
                    }
                )
            chapter_payloads.append(
                {
                    "chapter_id": chapter_id,
                    "chapter_plan_row_uid": chapter.row_uid,
                    "title": chapter.title or chapter_id,
                    "display_order": index,
                    "planned_scene_count": len(scenes_payload),
                    "chapter_goal": goal,
                    "main_plot_push": (chapter.summary or goal).strip(),
                    "emotional_target": "让人物目标、阻碍和代价在行动中显形。",
                    "ending_effect": "用新的选择、代价或信息推动下一章。",
                    "must_not": "不得复制参考书原文表达、人物、设定或桥段。",
                    "notes": "由雪花法分章物化，需确认后进入逐章运行。",
                    # 阶段 X：幕写成目录侧的口径 act1 / act2 / act3。过去写整数 1 / 2 / 3，而章节编排按
                    # ``act === "act1"`` 分卷——雪花整理出来的章在编排台上一张都不显示。
                    "narrative_json": {
                        "title": chapter.title or chapter_id,
                        "act": normalize_act(chapter.act),
                        "spine": chapter.spine or "",
                    },
                    "writer_brief_json": {
                        "source": "snowflake_method",
                        "chapter_title": chapter.title or chapter_id,
                        "chapter_act": normalize_act(chapter.act),
                        "chapter_spine": chapter.spine or "",
                    },
                    "scenes": scenes_payload,
                }
            )

        return {
            "source": "snowflake_method",
            "project_id": project.project_id,
            "project_title": project.title,
            "outline_text": project.outline_text,
            "reference_safety": [
                "参考书只进入抽象风格画像，不复制原文表达。",
                "不得复刻参考书人物、设定、桥段、特殊意象或标志性句式。",
                "运行时只使用节奏、句法、叙事手法、结构技巧和禁复刻规则。",
            ],
            "chapters": chapter_payloads,
        }

    def _protagonist_hint(self, project_id: str) -> dict[str, str] | None:
        """全书主角：04 角色摘要表显式指定的 ``protagonist_character_id`` 优先（双主角作品由作者定），
        否则取定位为主角的第一人；都没有返回 None，简报不带这两个键。"""
        rows = self.session.execute(
            select(SnowflakeCharacterPlan)
            .where(SnowflakeCharacterPlan.project_id == project_id)
            .order_by(SnowflakeCharacterPlan.created_at.asc(), SnowflakeCharacterPlan.character_id.asc())
        ).scalars().all()
        explicit = self._explicit_protagonist_id(project_id)
        if explicit:
            for row in rows:
                if row.character_id == explicit:
                    return {"character_id": row.character_id, "display_name": row.display_name}
        for row in rows:
            if is_protagonist_role(row.role):
                return {"character_id": row.character_id, "display_name": row.display_name}
        return None

    def _explicit_protagonist_id(self, project_id: str) -> str:
        run = self.session.execute(
            select(SnowflakeStepRun)
            .where(
                SnowflakeStepRun.project_id == project_id,
                SnowflakeStepRun.step_key == "character_sheets",
                SnowflakeStepRun.status != "superseded",
            )
            .order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.created_at.desc())
        ).scalars().first()
        if run is None:
            return ""
        return str((run.draft_json or {}).get("protagonist_character_id") or "").strip()

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
        """07 长篇大纲 → 构思侧章表（P2）。

        身份锚是 ``row_uid``，规则与场景计划一致：作者改标题、改幕、重排都不会重建行，
        所以已经分好的场景归属（``SnowflakeScenePlan.chapter_plan_id``）不会因为一次
        改标题就断掉。删掉的章软删，分在里面的场退回「未分章」。

        章表**收缩**（新表比旧表短，尾部的章连同它的场景归属一起没了）是作者必须知道的
        事：返回一份摘要，由调用方挂进健康度 ``generation_notice``——绝不静默报「已生成」。
        """
        incoming = parse_outline_chapters(draft)
        if not incoming:
            return None  # 空草稿不收口——同 P1-3 的护栏，一次空 PATCH 不能清掉全书的章

        existing = {
            row.row_uid: row
            for row in self.session.execute(
                select(SnowflakeChapterPlan).where(SnowflakeChapterPlan.project_id == project_id)
            ).scalars()
        }
        seen: set[str] = set()
        minted = False
        for index, item in enumerate(incoming, start=1):
            row_uid = str(item.get("row_uid") or "").strip()
            if row_uid and row_uid in seen:
                row_uid = ""  # 本次 payload 内重号 → 当作新章
            row = existing.get(row_uid) if row_uid else None
            if row is None:
                row_uid = row_uid or mint_chapter_row_uid()
                row = SnowflakeChapterPlan(
                    chapter_plan_id=f"snowflake_chapter_plan_{project_id}_{row_uid}",
                    project_id=project_id,
                    row_uid=row_uid,
                )
                self.session.add(row)
                existing[row_uid] = row
                minted = True
            elif row.removed_at:
                row.removed_at = None
                row.removed_by = None
            row.chapter_seq = index
            row.act = int_or_default(item.get("act"), 1)
            row.title = str(item.get("title") or "").strip()
            row.summary = str(item.get("summary") or "").strip()
            row.spine = str(item.get("spine") or "").strip()
            row.chapter_goal = str(item.get("chapter_goal") or "").strip() or row.chapter_goal
            row.status = "approved" if approved else "draft"
            row.source_step_run_id = run.step_run_id
            seen.add(row_uid)
            if item.get("row_uid") != row_uid:
                item["row_uid"] = row_uid
                minted = True

        removed_at = utcnow()
        dropped: list[dict[str, Any]] = []
        for row_uid, row in existing.items():
            if row_uid in seen or row.removed_at:
                continue
            row.removed_at = removed_at
            row.removed_by = "operator"
            self.session.add(
                OperationLog(
                    event_type="snowflake_chapter_plan_removed",
                    object_type="snowflake_chapter_plan",
                    object_ref=row.chapter_plan_id,
                    payload_json={
                        "project_id": project_id,
                        "row_uid": row_uid,
                        "title": row.title or "",
                        "removed_at": removed_at,
                    },
                )
            )
            # 分在这章里的场退回「未分章」，由分章面板重新指派——绝不静默塞进别的章
            unbound = 0
            for plan in self.session.execute(
                select(SnowflakeScenePlan).where(
                    SnowflakeScenePlan.project_id == project_id,
                    SnowflakeScenePlan.chapter_plan_id == row.chapter_plan_id,
                )
            ).scalars():
                plan.chapter_plan_id = None
                unbound += 1
            dropped.append({"title": row.title or row_uid, "unbound_scene_count": unbound})

        # 把铸好的 row_uid 回写进草稿，让下一次保存和前端水合都拿到同一个锚
        if minted and isinstance(run.draft_json, dict):
            run.draft_json = {**run.draft_json, "chapters": incoming}
            flag_modified(run, "draft_json")

        # 阶段 Z「章名只有一个」：07 里改的章名不必等下一次「确认写入」——09 的章头（场景行上的章名戳）
        # 和目录里那一章（名字还是上次由章表播下去的）当场跟上。
        self.session.flush()
        follow_plan_titles(self.session, project_id)

        loosened = sum(item["unbound_scene_count"] for item in dropped)
        if not loosened:
            return None  # 没有场因此松绑 = 纯粹的章表编辑，不必打扰作者
        titles = "、".join(item["title"] for item in dropped if item["unbound_scene_count"])[:120]
        return {
            "code": "CHAPTER_PLAN_SHRUNK",
            "severity": "warning",
            "message": (
                f"章表变短了：{titles} 已从章表消失，其中 {loosened} 场退回「未分章」。"
                "请到分章面板重新指派，否则它们不会进入章节目录。"
            ),
            "dropped_chapters": dropped,
            "unbound_scene_count": loosened,
        }

