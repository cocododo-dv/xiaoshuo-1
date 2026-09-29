from __future__ import annotations

import uuid
from copy import deepcopy
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    OperationLog,
    OutlinePlan,
    SceneCard,
    SnowflakeAssistantTurn,
    SnowflakeCharacterPlan,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    SnowflakeStepRun,
    StoryProject,
    utcnow,
)
from novel_system.services.author_actions import author_action
from novel_system.services.catalog import normalize_act
from novel_system.services.chapter_title_sync import follow_plan_titles
from novel_system.services.errors import DomainError
from novel_system.services.projects import (
    PLAN_STATUS_PENDING_REVIEW,
    ProjectService,
    outline_plan_payload,
    project_payload,
)
from novel_system.services.snowflake_character_ids import (
    canonical_character_id,
    canonicalize_draft,
    present_draft,
)
from novel_system.services.snowflake_draft_merge import (  # noqa: F401 — 旧名，测试从这里 import
    merge_member_lists as _merge_member_lists,
    overlay_keeping_members as _merge_dicts_keeping_members,
)
from novel_system.services.snowflake_step_diagnosis import is_protagonist_role
from novel_system.services.snowflake_gate import materialization_gate
from novel_system.services.snowflake_catalog_resync import SnowflakeCatalogResyncMixin
from novel_system.services.snowflake_triage_service import SnowflakeTriageMixin
from novel_system.services.snowflake_coach import SnowflakeCoachMixin
from novel_system.services.snowflake_approval import SnowflakeApprovalMixin
from novel_system.services.snowflake_structured_sync import SnowflakeStructuredSyncMixin
from novel_system.services.snowflake_scene_brief import (
    scene_card_beats,
    scene_title_seed,
    scene_writer_brief,
)
from novel_system.services.snowflake_scene_rows import (
    SCENE_PLAN_STEPS,
    scene_list_payload,
    scene_plan_payload,
)
from novel_system.services.snowflake_step_runs import (
    WIPE_PRESERVED_EVENT,
    StepRunStore,
    step_run_history_payload,
    step_run_payload,
    would_wipe_story as _would_wipe_story,
)
from novel_system.services.snowflake_staleness import (
    semantic_payload,
)
from novel_system.services.snowflake_steps import (
    CONFIRMED_STEP_STATUSES,
    MATERIALIZATION_REQUIREMENTS,
    QUALITY_POLICY,
    SNOWFLAKE_METHOD_VERSION,
    STEP_ORDER,
    SUMMARY_LENGTH_BAND,
    diagnose_step_pressure,
    editor_payload,
    effective_rendering_mode,
    merge_step_draft,
    step_completeness,
    step_definition_view,
    step_definition_views,
    step_guidance,
)
from novel_system.services.snowflake_chaptering import (
    SnowflakeChapteringService,
    mint_chapter_row_uid,
    parse_outline_chapters,
)
from novel_system.services.snowflake_scene_order import (
    live_scene_plans_in_story_order,
    positions_from_rows,
    sort_in_story_order,
)
from novel_system.services.snowflake_triage import (
    EXCLUDED_TRIAGE_STATUSES,
    coerce_triage_status,
    latest_triage_rows,
    plan_ids_with_status,
)
from novel_system.services.snowflake_direction_brief import DirectionBriefStore
from novel_system.services.snowflake_llm_context import approved_context_from_steps
from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService
from novel_system.services.hash_engine import sha256_text
from novel_system.services.value_coercion import int_or_default
from novel_system.services.writing_stats import WritingStatsService
from novel_system.services.snowflake_queries import latest_outline_plan, next_outline_plan_version

# 旧名：测试从本模块 import（实现各在叶子模块）
_scene_card_beats = scene_card_beats
_coerce_triage_status = coerce_triage_status
_is_protagonist_role = is_protagonist_role


class SnowflakeWorkspaceService(SnowflakeStructuredSyncMixin, SnowflakeApprovalMixin, SnowflakeCoachMixin, SnowflakeTriageMixin, SnowflakeCatalogResyncMixin):
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

    def workspace(self, project_id: str) -> dict[str, Any]:
        """交给前端的工作台：角色 id 按草稿口径（剥作品前缀，见 ``snowflake_character_ids``）。"""
        return self._present_workspace(project_id, self._workspace_payload(project_id))

    def mutation_workspace(self, project_id: str) -> dict[str, Any]:
        """变更回包里的工作台（B06-05）：与 GET 同一个构建，只是不带没人读的 ``scene_board``，步骤 ``artifact``
        也不再带一份 ``health`` 的深拷贝（``diagnosis_json``）——前端从变更回包里只读步骤、闸门、分诊、回流与教练历史。"""
        return self._present_workspace(project_id, self._workspace_payload(project_id, lean=True))

    def _present_workspace(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            **payload,
            "steps": [self._present_step(project_id, step) for step in payload.get("steps") or []],
            "assistant_history": [self._present_turn(project_id, turn) for turn in payload.get("assistant_history") or []],
        }

    @staticmethod
    def _present_step(project_id: str, step: dict[str, Any]) -> dict[str, Any]:
        return {**step, "draft": present_draft(project_id, step.get("draft"))}

    def _presented_step(self, project_id: str, step_key: str) -> dict[str, Any]:
        """只建这一步（自动保存 ``include_workspace=false`` 的回包，B06-05）：与工作台里的那一步同一个构建、同一个前端口径。"""
        latest_by_step = self._latest_by_step(project_id)
        scene_plans = self._scene_plans(project_id, latest_by_step=latest_by_step) if step_key in SCENE_PLAN_STEPS else None
        step = self._workspace_step(
            step_definition_view(step_key),
            latest_by_step,
            project_id=project_id,
            confirmed_steps=self._steps_with_confirmed_version(project_id),
            scene_plans=scene_plans,
            include_diagnosis=False,
        )
        return self._present_step(project_id, step)

    def _workspace_payload(self, project_id: str, *, lean: bool = False) -> dict[str, Any]:
        """工作台的库内口径（角色 id 带作品前缀）：提示词与服务端内部都读它。

        一次构建每样东西只读一遍库（B06-04）：各步最新版本、按故事序排好的场景计划（故事序用已读出的 09 草稿算）、
        每场最新的分诊记录、场景卡，各段共用。``lean``（变更回包与服务内部用，B06-05）不建 ``scene_board``，
        步骤 ``artifact`` 不带重复 ``health`` 的 ``diagnosis_json``；GET 的形状不变。
        """
        project = self._require_snowflake_project(project_id)
        latest_by_step = self._latest_by_step(project_id)
        current_step_key = self._current_step_key(latest_by_step)
        scene_plans = self._scene_plans(project_id, latest_by_step=latest_by_step)
        triage_rows = latest_triage_rows(self.session, project_id)
        triage_items = self._triage_items(project_id, scene_plans=scene_plans, triage_rows=triage_rows)
        latest_plan = self._latest_plan(project_id)
        confirmed_steps = self._steps_with_confirmed_version(project_id)
        steps = [
            self._workspace_step(
                step,
                latest_by_step,
                project_id=project_id,
                confirmed_steps=confirmed_steps,
                scene_plans=scene_plans,
                include_diagnosis=not lean,
            )
            for step in step_definition_views()
        ]
        chapter_plan_status = self._chaptering.status(project_id, scene_plans)
        gate = self._materialization_gate(latest_by_step, triage_items, scene_plans, chapter_plan_status)
        payload: dict[str, Any] = {
            "chapter_plan_status": chapter_plan_status,
            "project": project_payload(project),
            "method_version": SNOWFLAKE_METHOD_VERSION,
            "quality_policy": deepcopy(QUALITY_POLICY),
            "materialization_requirements": deepcopy(MATERIALIZATION_REQUIREMENTS),
            "current_step_key": current_step_key,
            "ready_to_materialize": gate["status"] != "blocked",
            "latest_plan": outline_plan_payload(latest_plan) if latest_plan is not None else None,
        }
        if not lean:
            payload["scene_board"] = self._scene_board(project_id, scene_plans=scene_plans)
        payload.update(
            {
                "triage_items": triage_items,
                "assistant_history": self._assistant_history(project_id),
                # 阶段 T：每步的作者意图要点（含已撤条目供恢复、继承的上游全书级条目）
                "direction_briefs": self._briefs.all_payloads(project_id),
                "materialization_gate": gate,
                "resync_status": self._resync_status(
                    project.project_id,
                    scene_plans,
                    excluded=plan_ids_with_status(triage_rows, EXCLUDED_TRIAGE_STATUSES),
                ),
                "steps": steps,
            }
        )
        return payload

    def generate_step(self, project_id: str, step_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        self._require_step(step_key)
        latest_by_step = self._latest_by_step(project.project_id)
        if self._draft_gate_mode(project) == "strict":
            self._require_previous_gates(step_key, latest_by_step)

        generation_notice: dict[str, Any] | None = None
        # FE 触发入口标签（fe_scaffold_ai / fe_candidate_adopt / …）：随 health_json
        # 落库作为这一版草稿的出处事实，与 generation_source（llm/fallback/skip）并列。
        # 请求层已界定为有界短标识，这里只做去空白。
        trigger_source = str(body.get("source") or "").strip()[:64] or None
        brief_ref: dict[str, Any] | None = None
        direction_ref: dict[str, Any] | None = None
        direction_turn: SnowflakeAssistantTurn | None = None
        direction_index: int | None = None
        if body.get("skip"):
            draft = self._skip_draft(step_key, body)
            source = "skip"
            llm_call_id = None
            status = "skipped"
            approved_at = utcnow()
        else:
            # 「采纳并结构化」通道：FE 把选中的候选正文作为方向蓝本带入，
            # require_llm=true 时 LLM 未启用要诚实报错，绝不能静默落一版启发式草稿。
            direction_text = str(body.get("direction_text") or "").strip()[:2000]
            # 「AI 补全这一场」通道：只深化指定场景（row_uid/scene_id 皆可指），
            # 清洗器按 scene_id 合并回底稿，其余场景保持原样。仅 scene_details 支持。
            focus_scene_refs = [str(ref or "").strip() for ref in (body.get("focus_scene_refs") or []) if str(ref or "").strip()]
            # 「AI 补全这个角色」通道：只深化指定角色（character_id/姓名皆可指），
            # 按 character_id 合并回底稿，其余角色保持原样。仅角色三步（04/06/08）支持。
            focus_character_refs = [str(ref or "").strip() for ref in (body.get("focus_character_refs") or []) if str(ref or "").strip()]
            # 前端按草稿口径（不带作品前缀）指角色；库里的草稿是规范口径——两种写法都认（姓名照旧认）
            focus_character_refs = list(
                dict.fromkeys([*focus_character_refs, *(canonical_character_id(project.project_id, ref) for ref in focus_character_refs)])
            )
            # draft_override：FE 带来的本地最新规范草稿（与上行 PATCH 同源），盖在
            # 存档之上作为生成底稿——消除「刚加的角色/场还没自动保存上行」的竞态。
            draft_override = self._merged_draft_override(project.project_id, latest_by_step, step_key, body.get("draft_override"))
            if body.get("require_llm") and not self._llm.llm_enabled():
                raise DomainError(
                    "SNOWFLAKE_LLM_REQUIRED",
                    "结构化生成需要可用的 LLM：请到「系统设置 → 模型与接入」启用并配置后重试。",
                    status_code=409,
                    details={"node_id": "snowflake_step_generate", "next_action": "configure_llm_then_retry"},
                )
            # 阶段 T：作者意图要点默认带入（use_direction_brief=false 是「纯探索」开关）；
            # 消费了哪一版随 health_json.direction_brief 落库，前端据此提示「本稿未采用最新要点」。
            use_brief = body.get("use_direction_brief")
            use_brief = True if use_brief is None else bool(use_brief)
            brief_rows = self._briefs.rows(project.project_id)
            brief_prompt = self._briefs.prompt_payload_for(project.project_id, step_key, brief_rows) if use_brief else None
            brief_ref = self._briefs.fingerprint_for(project.project_id, step_key, used=use_brief, rows=brief_rows)
            direction_kind = str(body.get("direction_kind") or "").strip() or None
            # 阶段 U：方向来自教练日志里的哪一回合（「先看 3 个方向」的第几条 / 教练某轮回复）。
            # 回合种类决定用法说明；生成后回合记 adoption，这一版的 health.direction 记出处。
            direction_turn, direction_index, direction_label = self._resolve_direction_turn(
                project.project_id, body, direction_text=direction_text
            )
            if direction_turn is not None:
                direction_kind = "candidate" if direction_turn.turn_kind == "candidates" else "coach_reply"
            if direction_text:
                direction_ref = {
                    "kind": direction_kind or "candidate",
                    "sha": sha256_text(direction_text)[:16],
                    "turn_id": direction_turn.turn_id if direction_turn is not None else None,
                    "candidate_index": direction_index,
                    "label": direction_label,
                }
            llm_result = self._llm.generate_step(
                project=project,
                step_key=step_key,
                latest_by_step=latest_by_step,
                adopted_direction=direction_text or None,
                focus_scene_refs=focus_scene_refs if step_key == "scene_details" else None,
                focus_character_refs=(
                    focus_character_refs
                    if step_key in {"character_sheets", "character_synopses", "character_bibles"}
                    else None
                ),
                draft_override=draft_override,
                author_direction_brief=brief_prompt,
                direction_kind=direction_kind,
            )
            # 模型看到的是规范口径的 id，回来的也按规范口径落库；缺 id 的新成员在这里铸号
            draft = canonicalize_draft(project.project_id, llm_result.payload, mint_missing=True)
            source = llm_result.source
            llm_call_id = llm_result.llm_call_id
            # 分批深化中途失败等「作者必须知道但不属于草稿」的事实随健康度落库
            generation_notice = llm_result.notice
            status = "pending_review"
            approved_at = None

        run = self._runs.new_run(
            project.project_id,
            step_key,
            draft=draft,
            status=status,
            health=self._step_health(
                step_key,
                draft,
                status,
                generation_source=source,
                generation_notice=generation_notice,
                trigger_source=trigger_source,
                direction_brief=brief_ref,
                direction=direction_ref,
            ),
            input_refs=self._input_refs(step_key, latest_by_step),
            llm_call_id=llm_call_id,
            approved_at=approved_at,
        )
        self.session.flush()
        if direction_turn is not None:
            # 「已按此生成」：回合记住它被哪一版采纳过（方向回合还记第几条）——界面打徽章，教练下一轮看得到作者选了哪个方向
            direction_turn.adoption_json = {
                "step_run_id": run.step_run_id,
                "candidate_index": direction_index,
                "adopted_at": utcnow(),
            }
            flag_modified(direction_turn, "adoption_json")
        sync_notice = self._sync_structured_step_data(project, step_key, draft, run)
        if sync_notice:
            # 章表收缩只有落库时才知道（要比对既有章行），此时 health_json 已经建好——
            # 补挂回去，绝不让「已生成」盖住「全书归属松了 N 场」。
            run.health_json = self._step_health(
                step_key, draft, status, generation_source=source,
                generation_notice=generation_notice or sync_notice,
                trigger_source=trigger_source,
                direction_brief=brief_ref,
                direction=direction_ref,
            )
        if status == "skipped":
            self._runs.supersede_others(run)
            self._mark_downstream_stale(run)
        self.session.flush()
        workspace = self.mutation_workspace(project.project_id)
        return {"step": self._step_from_workspace(workspace, step_key), "workspace": workspace}

    def update_step(
        self,
        project_id: str,
        step_key: str,
        payload: dict[str, Any] | None = None,
        *,
        include_workspace: bool = True,
    ) -> dict[str, Any]:
        """保存一步的草稿。``include_workspace=False``（前端的防抖自动保存只读回包里的 ``step``，B06-05）
        只回 ``{step, step_run}``，不再为每一次键入重建整个工作台。"""
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        self._require_step(step_key)
        latest_by_step = self._latest_by_step(project.project_id)
        if self._draft_gate_mode(project) == "strict":
            self._require_previous_gates(step_key, latest_by_step)
        # 写入即规范（B06-01）：角色 id 与视角 / 在场 / 全书主角的引用一律带作品前缀，缺 id 的角色铸号
        draft = canonicalize_draft(
            project.project_id,
            merge_step_draft(step_key, body.get("draft") or {}, latest_by_step=latest_by_step),
            mint_missing=True,
        )
        latest = latest_by_step.get(step_key)

        # 防静默回退：已批准/已跳过步骤收到无故事含义的 re-PATCH 时保持原状态与版本。
        # ``fe_*`` 是前端写穿缓存（其中 book_brief.fe_meta 会在确认任何后续步骤时变化）；
        # 若把它当故事修订，就会把第 1 步重新打回待审，并连锁 staling 全部下游。
        # 元数据仍原位写入，保证跨会话 UI 账本不丢；规范故事字段确有变化时才新建待审版本。
        # 过期（stale）的步骤同理（PRE-01）：前端每次打开构思都会把 09 / 10 各上行一次，以前这会把
        # 「已复核」的过期步骤打成一版新的待审——整理闸门认「已复核」、不认待审，作者只是打开页面就被挡住。
        # 状态与失效留痕（stale_*）原样保留。
        same_semantic_draft = latest is not None and semantic_payload(draft) == semantic_payload(latest.draft_json)
        if latest is not None and latest.status in {"approved", "skipped", "stale"} and same_semantic_draft:
            if (draft or {}) != (latest.draft_json or {}):
                latest.draft_json = draft
                self.session.flush()
            return self._step_saved_response(project.project_id, step_key, latest, include_workspace=include_workspace)

        # 2026-09-18 抹空保护：pending_review 步平时原位改写（不为每次键入造版本），但「整步抹空」不行——
        # 旧稿有成段的故事文字、新稿一个字都没有时另起一版，旧稿留在历史里可用 restore 取回。前端同步层
        # 已有水合闸门拦住「没水合过的空白默认稿」，这里是最后一道：原位改写没有历史，未确认的草稿一旦被
        # 空白覆盖就是永久丢失。作者亲手重置同样多留一版，可反悔。
        wipes_story = latest is not None and _would_wipe_story(latest.draft_json, draft)
        if latest is not None and latest.status == "pending_review" and not wipes_story:
            run = latest
            StepRunStore.rewrite_pending(
                run,
                draft=draft,
                health=self._step_health(step_key, draft, "pending_review", generation_source="author"),
                input_refs=self._input_refs(step_key, latest_by_step),
            )
        else:
            run = self._runs.new_run(
                project.project_id,
                step_key,
                draft=draft,
                status="pending_review",
                health=self._step_health(step_key, draft, "pending_review", generation_source="author"),
                input_refs=self._input_refs(step_key, latest_by_step),
            )
            if wipes_story and latest is not None and latest.status == "pending_review":
                self.session.add(
                    OperationLog(
                        event_type=WIPE_PRESERVED_EVENT,
                        object_type="snowflake_step_run",
                        object_ref=run.step_run_id,
                        payload_json={
                            "project_id": project.project_id,
                            "step_key": step_key,
                            "preserved_step_run_id": latest.step_run_id,
                            "preserved_version": latest.version,
                        },
                    )
                )

        self.session.flush()
        self._sync_structured_step_data(project, step_key, draft, run)
        self.session.flush()
        return self._step_saved_response(project.project_id, step_key, run, include_workspace=include_workspace)

    def _step_saved_response(
        self, project_id: str, step_key: str, run: SnowflakeStepRun, *, include_workspace: bool
    ) -> dict[str, Any]:
        step_run = self._step_run_payload(run, include_diagnosis=False)
        if not include_workspace:
            return {"step": self._presented_step(project_id, step_key), "step_run": step_run}
        workspace = self.mutation_workspace(project_id)
        return {"step": self._step_from_workspace(workspace, step_key), "workspace": workspace, "step_run": step_run}

    def step_history(
        self,
        project_id: str,
        step_key: str,
        *,
        include_draft: bool = False,
        step_run_id: str | None = None,
    ) -> dict[str, Any]:
        """一步的服务端版本列表（新的在前）。``step_run_id`` 只取那一版——历史页先列版本（不带草稿），
        预览某一版时再按它取草稿（R15a：「服务器上保存的版本」的前提）。"""
        project = self._require_snowflake_project(project_id)
        self._require_step(step_key)
        rows = self._runs.history(project.project_id, step_key, step_run_id=step_run_id)
        if str(step_run_id or "").strip() and not rows:
            raise DomainError("SNOWFLAKE_STEP_RUN_NOT_FOUND", "该历史版本不属于当前项目的这一步骤。", status_code=404)
        preserved = self._runs.wipe_guard_preservations([row.step_run_id for row in rows])
        items = []
        for row in rows:
            payload = step_run_history_payload(row, include_draft=include_draft)
            if include_draft:
                payload["draft"] = present_draft(project.project_id, payload.get("draft"))
            # 抹空保护新起的那一版：它记着被保住的是哪一版（界面可以据此一键取回）
            payload["wipe_guard_preserved_step_run_id"] = preserved.get(row.step_run_id)
            items.append(payload)
        return {"project_id": project.project_id, "step_key": step_key, "items": items}

    def restore_step(self, project_id: str, step_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        self._require_step(step_key)
        source_run_id = str(body.get("step_run_id") or "").strip()
        if not source_run_id:
            raise DomainError("SNOWFLAKE_STEP_RUN_REQUIRED", "缺少 step_run_id 参数。", status_code=400)
        source_run = self.session.get(SnowflakeStepRun, source_run_id)
        if source_run is None or source_run.project_id != project.project_id or source_run.step_key != step_key:
            raise DomainError("SNOWFLAKE_STEP_RUN_NOT_FOUND", "该历史版本不属于当前项目的这一步骤。", status_code=404)
        latest_by_step = self._latest_by_step(project.project_id)
        if self._draft_gate_mode(project) == "strict":
            self._require_previous_gates(step_key, latest_by_step)
        draft = canonicalize_draft(project.project_id, deepcopy(source_run.draft_json or {}), mint_missing=True)
        refs = self._input_refs(step_key, latest_by_step)
        refs["restored_from_step_run_id"] = source_run.step_run_id
        run = self._runs.new_run(
            project.project_id,
            step_key,
            draft=draft,
            status="pending_review",
            health=self._step_health(step_key, draft, "pending_review", generation_source="history_restore"),
            input_refs=refs,
        )
        self.session.flush()
        sync_notice = self._sync_structured_step_data(project, step_key, draft, run)
        if sync_notice:
            # 恢复一版旧的 07 可能让章表变短：尾部的章连同场景归属一起没了——与生成同一条路，挂进健康度，
            # 回包也如实带着（R15a：恢复界面要在这里告诉作者）。
            run.health_json = self._step_health(
                step_key, draft, "pending_review", generation_source="history_restore", generation_notice=sync_notice
            )
        self.session.flush()
        workspace = self.mutation_workspace(project.project_id)
        step_run = self._step_run_payload(run, include_diagnosis=False) or {}
        step_run["restored_from_step_run_id"] = source_run.step_run_id
        result = {
            "step": self._step_from_workspace(workspace, step_key),
            "workspace": workspace,
            "step_run": step_run,
            "restored_from": step_run_history_payload(source_run, include_draft=False),
        }
        if sync_notice:
            result["notice"] = sync_notice
        return result

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

    def _require_snowflake_project(self, project_id: str) -> StoryProject:
        project = self._projects.require_project(project_id)
        if str(getattr(project, "planning_mode", "") or "") != "snowflake":
            raise DomainError(
                "PROJECT_NOT_SNOWFLAKE",
                "该工作台只支持雪花法项目。",
                status_code=409,
            )
        return project

    def _steps_with_confirmed_version(self, project_id: str) -> set[str]:
        """还留着一版已确认内容（approved / stale）的步骤——待审的新版本就是「确认之后又改了」。"""
        rows = self.session.execute(
            select(SnowflakeStepRun.step_key).where(
                SnowflakeStepRun.project_id == project_id,
                SnowflakeStepRun.status.in_(["approved", "stale"]),
            )
        ).all()
        return {str(row[0]) for row in rows}

    def _workspace_step(
        self,
        step: Mapping[str, Any],
        latest_by_step: dict[str, SnowflakeStepRun],
        *,
        project_id: str,
        confirmed_steps: set[str] | None = None,
        scene_plans: list[SnowflakeScenePlan] | None = None,
        include_diagnosis: bool = True,
    ) -> dict[str, Any]:
        run = latest_by_step.get(step["step_key"])
        draft = self._draft_for_step(step["step_key"], run, latest_by_step, project_id=project_id, scene_plans=scene_plans)
        status = run.status if run is not None else "draft"
        approval_blockers = self._previous_gate_blockers(step["step_key"], latest_by_step)
        if confirmed_steps is None:
            confirmed_steps = self._steps_with_confirmed_version(project_id)
        # 阶段 G：确认过的步骤被改动后是「待重新确认」，不是「本机已确认、后端同步中」——前端据此
        # 不再在键入后自动补批准，而是等作者显式点「确认本步」，下游失效级联也在那一刻才发生。
        revised_after_approval = bool(run is not None and run.status == "pending_review" and step["step_key"] in confirmed_steps)
        return {
            "revised_after_approval": revised_after_approval,
            "step_key": step["step_key"],
            "label": step["label"],
            "english_label": step.get("english_label", ""),
            "phase": step["phase"],
            "description": step["description"],
            "status": status,
            "version": run.version if run is not None else 0,
            "health": deepcopy(run.health_json or {}) if run is not None else {},
            "stale_reason": run.stale_reason if run is not None else "",
            "stale_accepted_at": run.stale_accepted_at if run is not None else None,
            "stale_accepted_by": run.stale_accepted_by if run is not None else "",
            "stale_accepted_note": run.stale_accepted_note if run is not None else "",
            "can_skip": bool(step.get("skippable")),
            "can_confirm": bool(run is not None and run.status == "pending_review" and not approval_blockers),
            "approval_blockers": approval_blockers,
            "can_backtrack": run is not None and run.status in {"approved", "skipped", "stale"},
            "guidance": step_guidance(step["step_key"]),
            "gate_satisfied": self._gate_satisfied(step["step_key"], latest_by_step),
            "artifact": self._step_run_payload(run, include_diagnosis=include_diagnosis),
            "draft": draft,
            "completeness": step_completeness(step["step_key"], draft),
            "editor": editor_payload(step["step_key"]),
            "last_generation_source": (run.health_json or {}).get("generation_source") if run is not None else None,
            "last_llm_call_id": run.llm_call_id if run is not None else None,
        }

    def _draft_for_step(
        self,
        step_key: str,
        run: SnowflakeStepRun | None,
        latest_by_step: dict[str, SnowflakeStepRun],
        *,
        project_id: str,
        scene_plans: list[SnowflakeScenePlan] | None = None,
    ) -> dict[str, Any]:
        if step_key in SCENE_PLAN_STEPS:
            plans = self._scene_plans(project_id) if scene_plans is None else scene_plans
            row_payload = scene_list_payload if step_key == "scene_list" else scene_plan_payload
            scenes = [row_payload(scene) for scene in plans]
            return {"scenes": scenes} if scenes else merge_step_draft(step_key, run.draft_json if run else None, latest_by_step=latest_by_step)
        return merge_step_draft(step_key, run.draft_json if run else None, latest_by_step=latest_by_step)

    @staticmethod
    def _gate_satisfied(step_key: str, latest_by_step: dict[str, SnowflakeStepRun]) -> bool:
        run = latest_by_step.get(step_key)
        if run is None:
            return False
        if run.status in CONFIRMED_STEP_STATUSES:
            return True
        return run.status == "stale" and bool(run.stale_accepted_at)

    def _current_step_key(self, latest_by_step: dict[str, SnowflakeStepRun]) -> str | None:
        for step in step_definition_views():
            if not self._gate_satisfied(step["step_key"], latest_by_step):
                return step["step_key"]
        return None

    @staticmethod
    def _draft_gate_mode(project: StoryProject) -> str:
        mode = str(getattr(project, "snowflake_workflow_mode", "strict") or "strict").strip().lower()
        return "explore" if mode == "explore" else "strict"

    def _require_step(self, step_key: str) -> None:
        if step_key not in STEP_ORDER:
            raise DomainError("SNOWFLAKE_STEP_NOT_FOUND", "未知的雪花步骤。", status_code=404)

    def _require_previous_gates(
        self,
        step_key: str,
        latest_by_step: dict[str, SnowflakeStepRun],
        *,
        allow_self: str | None = None,
    ) -> None:
        step_index = STEP_ORDER[step_key]
        blockers = []
        for step in step_definition_views()[:step_index]:
            run = latest_by_step.get(step["step_key"])
            if allow_self and run is not None and run.step_run_id == allow_self:
                continue
            if not self._gate_satisfied(step["step_key"], latest_by_step):
                blockers.append({"step_key": step["step_key"], "label": step["label"]})
        if blockers:
            first = blockers[0]
            raise DomainError(
                "SNOWFLAKE_PREVIOUS_STEP_REQUIRED",
                "需要先确认前面的雪花步骤。",
                status_code=409,
                details={
                    "missing_previous_steps": blockers,
                    "author_action": author_action(
                        f"还差{first['label']}",
                        f"先确认「{first['label']}」，再确认当前雪花步骤。你可以继续写草稿，但确认和物化仍会守住依赖。",
                        target_view="snowflake-workbench",
                        target_ref=f"snowflake_step:{first['step_key']}",
                        primary_button_label=f"去补{first['label']}",
                        evidence_summary=[f"缺少上游步骤：{first['label']}"],
                    ),
                },
            )

    def _previous_gate_blockers(
        self,
        step_key: str,
        latest_by_step: dict[str, SnowflakeStepRun],
    ) -> list[dict[str, str]]:
        blockers: list[dict[str, str]] = []
        for step in step_definition_views()[: STEP_ORDER[step_key]]:
            if not self._gate_satisfied(step["step_key"], latest_by_step):
                blockers.append({"step_key": step["step_key"], "label": step["label"]})
        return blockers

    def _latest_by_step(self, project_id: str) -> dict[str, SnowflakeStepRun]:
        return self._runs.latest_by_step(project_id)

    def _input_refs(self, step_key: str, latest_by_step: dict[str, SnowflakeStepRun]) -> dict[str, Any]:
        step_index = STEP_ORDER[step_key]
        refs: dict[str, Any] = {}
        for step in step_definition_views()[:step_index]:
            run = latest_by_step.get(step["step_key"])
            if run is not None and self._gate_satisfied(step["step_key"], latest_by_step):
                refs[step["step_key"]] = run.step_run_id
        return refs

    def _next_plan_version(self, project_id: str) -> int:
        return next_outline_plan_version(self.session, project_id)

    def _latest_plan(self, project_id: str) -> OutlinePlan | None:
        return latest_outline_plan(self.session, project_id)

    def _scene_plans(
        self, project_id: str, *, latest_by_step: dict[str, SnowflakeStepRun] | None = None
    ) -> list[SnowflakeScenePlan]:
        """活跃场景计划，按**故事序**（09 场景列表的行序，见 ``snowflake_scene_order``）。

        软删的场（P1-3）在这里就被挡掉——工作台、诊断、闸门、回流状态和物化输入全部经过这一个
        入口，所以幽灵场不会再从任何一处冒出来。顺序同理只有这一个口径：09 / 10 两步交给前端的
        草稿就是按它排的，曾经的 ``(chapter_id, scene_seq)`` 在分章之后会把场景表按章重新洗一遍，
        新浏览器水合到的 09 于是不是作者排的那张表，下一次保存再把乱序写回去。

        ``latest_by_step``：同一次构建里刚读出的各步最新版本——故事序直接用其中的 09 草稿算，不再为排序
        把 09 再读一遍（B06-04；它与 ``story_positions`` 取的是同一行：最新一版未被取代的 09）。
        """
        if latest_by_step is None:
            return live_scene_plans_in_story_order(self.session, project_id)
        rows = self.session.execute(
            select(SnowflakeScenePlan).where(
                SnowflakeScenePlan.project_id == project_id,
                SnowflakeScenePlan.removed_at.is_(None),
            )
        ).scalars().all()
        scene_list = latest_by_step.get("scene_list")
        positions = positions_from_rows((scene_list.draft_json or {}).get("scenes")) if scene_list is not None else {}
        return sort_in_story_order(self.session, project_id, rows, positions=positions)

    def _scene_board(self, project_id: str, *, scene_plans: list[SnowflakeScenePlan] | None = None) -> dict[str, Any]:
        scenes = [scene_plan_payload(scene) for scene in (scene_plans if scene_plans is not None else self._scene_plans(project_id))]
        chapters_by_id: dict[str, dict[str, Any]] = {}
        for scene in scenes:
            chapter_id = scene["chapter_id"]
            chapter = chapters_by_id.setdefault(
                chapter_id,
                {
                    "chapter_id": chapter_id,
                    "title": scene.get("chapter_title") or chapter_id,
                    "chapter_goal": scene.get("chapter_goal") or "",
                    "scene_count": 0,
                },
            )
            chapter["scene_count"] += 1
        return {"chapters": list(chapters_by_id.values()), "scenes": scenes}

    #: 见 ``snowflake_gate.materialization_gate``
    _materialization_gate = staticmethod(materialization_gate)

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

    def _skip_draft(self, step_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = step_definition_view(step_key)
        if not step.get("skippable"):
            raise DomainError("SNOWFLAKE_STEP_NOT_SKIPPABLE", "这一步骤不能跳过。", status_code=400)
        reason = str(payload.get("skip_reason") or "").strip()
        if not reason:
            raise DomainError("SNOWFLAKE_SKIP_REASON_REQUIRED", "跳过时必须填写理由。", status_code=400)
        return {"skipped": True, "skip_reason": reason}

    def _step_health(
        self,
        step_key: str,
        draft: dict[str, Any],
        status: str,
        *,
        generation_source: str | None = None,
        generation_notice: dict[str, Any] | None = None,
        trigger_source: str | None = None,
        direction_brief: dict[str, Any] | None = None,
        direction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status == "skipped":
            health = {
                "severity": "info",
                "message": "step skipped with an explicit author reason",
                "generation_source": generation_source or "skip",
                "step_key": step_key,
                "pressure_score": 100,
                "pressure_status": "pass",
                "pressure_flags": [],
                "fix_steps": [],
                "strengths": ["step skipped with an explicit author reason"],
                "score": 100,
                "status": "pass",
                "gaps": [],
                "next_actions": [],
                "hard_blockers": [],
            }
        else:
            completeness = step_completeness(step_key, draft)
            missing = completeness.get("missing_fields") or []
            health = {
                "severity": "warning" if missing else "info",
                "message": "step has missing fields" if missing else "step draft is structurally complete",
                "generation_source": generation_source or "fallback",
                "missing_fields": missing,
                **diagnose_step_pressure(step_key, draft),
            }
            if generation_notice:
                health["generation_notice"] = deepcopy(generation_notice)
                health["severity"] = "warning"
        # 只在 FE 真的带了触发入口时写入：脚本 / 旧客户端的运行不凭空长出一个标签。
        if trigger_source:
            health["trigger_source"] = trigger_source
        # 阶段 T：这一版消费了哪一版作者意图要点（used=False = 作者本次关掉了带入）
        if direction_brief:
            health["direction_brief"] = deepcopy(direction_brief)
        # 阶段 U：这一版按哪个方向生成（方向回合的第几条 / 教练某轮回复）；没有方向时键不出现
        if direction:
            health["direction"] = deepcopy(direction)
        return health

    #: 一版草稿的元数据（``snowflake_step_runs.step_run_payload``）
    _step_run_payload = staticmethod(step_run_payload)

    @staticmethod
    def _step_from_workspace(workspace: dict[str, Any], step_key: str) -> dict[str, Any]:
        for step in workspace.get("steps") or []:
            if step.get("step_key") == step_key:
                return step
        raise DomainError("SNOWFLAKE_STEP_NOT_FOUND", "未知的雪花步骤。", status_code=404)

    @staticmethod
    def _approved_context(workspace: dict[str, Any]) -> list[dict[str, Any]]:
        """驻场教练 / AI 分诊看到的全书上下文——与整步生成的 upstream_steps 同一种条目（B06-16）。"""
        return approved_context_from_steps(workspace.get("steps") or [])

    @staticmethod
    def _merged_draft_override(
        project_id: str,
        latest_by_step: dict[str, SnowflakeStepRun],
        step_key: str,
        draft_override: Any,
    ) -> dict[str, Any] | None:
        """FE 带来的本地最新规范草稿（与上行 PATCH 同源）盖在存档之上作为生成 / 方向的底稿——
        消除「刚加的角色 / 场还没自动保存上行」的竞态；剥 fe_* 写穿键，按成员对位合并。没带 → None。"""
        if not isinstance(draft_override, dict) or not draft_override:
            return None
        latest = latest_by_step.get(step_key)
        base_payload = {
            key: value
            for key, value in (((latest.draft_json if latest is not None else None) or {}).items())
            if not str(key).startswith("fe_")
        }
        override_payload = canonicalize_draft(
            project_id, {key: value for key, value in draft_override.items() if not str(key).startswith("fe_")}
        )
        return _merge_dicts_keeping_members(base_payload, override_payload)

    @staticmethod
    def _step_with_override(
        project_id: str,
        step: dict[str, Any],
        draft_override: Any,
        *,
        latest_by_step: dict[str, SnowflakeStepRun],
    ) -> dict[str, Any]:
        if not isinstance(draft_override, dict):
            return step
        merged_step = deepcopy(step)
        # 与 generate_step 同源:draft_override 是「作者刚编辑、还没自动保存上行」的叠加,
        # 不是删除指令。助手/场景三分类同样必须按成员对位合并——整表替换会让前端少带几个
        # 成员就把存档里的成员整片抹掉,教练/分类器于是只看到半截故事(与 generate 同一 bug)。
        # 先剥掉 fe_* 写透键,再按成员 id 对位。
        override_payload = canonicalize_draft(
            project_id, {key: value for key, value in draft_override.items() if not str(key).startswith("fe_")}
        )
        merged_step["draft"] = _merge_dicts_keeping_members(merged_step.get("draft") or {}, override_payload)
        merged_step["draft"] = merge_step_draft(
            str(merged_step.get("step_key") or ""),
            merged_step.get("draft") or {},
            latest_by_step=latest_by_step,
        )
        return merged_step


