from __future__ import annotations

import uuid
from copy import deepcopy
from collections.abc import Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    FinalScene,
    LlmCall,
    OperationLog,
    OutlinePlan,
    SceneCard,
    SceneRunState,
    SnowflakeAssistantTurn,
    SnowflakeCharacterPlan,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    SnowflakeSceneTriageItem,
    SnowflakeStepRun,
    StoryCharacter,
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
    trash_emptied_snowflake_chapters,
)
from novel_system.services.project_runtime_invalidation import ProjectRuntimeInvalidationService
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
from novel_system.services.snowflake_scene_brief import (
    followed_scene_title,
    real_scene_title,
    scene_card_beats,
    scene_title_seed,
    scene_writer_brief,
)
from novel_system.services.snowflake_scene_rows import (
    CATALOG_SYNC_STEPS,
    SCENE_LIST_OWNED_FIELDS,
    SCENE_PLAN_STEPS,
    mint_row_uid,
    mint_scene_id,
    sanitize_scene_patch,
    scene_list_payload,
    scene_plan_content_signature,
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
    changed_scene_row_uids,
    field_sigs,
    recompute_stale,
    semantic_payload,
    snapshot_consumed_sigs,
)
from novel_system.services.snowflake_steps import (
    CONFIRMED_STEP_STATUSES,
    MATERIALIZATION_REQUIREMENTS,
    QUALITY_POLICY,
    RENDERING_MODES,
    SNOWFLAKE_METHOD_VERSION,
    STEP_ORDER,
    SUMMARY_LENGTH_BAND,
    diagnose_step_pressure,
    diagnose_scene_detail,
    editor_payload,
    effective_rendering_mode,
    merge_step_draft,
    step_completeness,
    step_definition_view,
    step_definition_views,
    step_guidance,
)
from novel_system.services.scene_rehome import rehome_scenes
from novel_system.services.snowflake_chaptering import (
    SnowflakeChapteringService,
    mint_chapter_row_uid,
    parse_outline_chapters,
)
from novel_system.services.snowflake_scene_order import (
    live_scene_plans_in_story_order,
    positions_from_rows,
    renumber_scene_seq,
    sort_in_story_order,
)
from novel_system.services.snowflake_triage import (
    EXCLUDED_TRIAGE_STATUSES,
    coerce_triage_status,
    excluded_scene_plan_ids,
    latest_triage_rows,
    plan_ids_with_status,
)
from novel_system.services.snowflake_direction_brief import DirectionBriefStore, delta_changed
from novel_system.services.snowflake_llm_context import approved_context_from_steps
from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService
from novel_system.services.hash_engine import sha256_text
from novel_system.services.value_coercion import coerce_string_list, int_or_default
from novel_system.services.writing_stats import WritingStatsService
from novel_system.services.snowflake_queries import latest_outline_plan, next_outline_plan_version

#: 一条 ``IN`` 查询最多带多少个 id（远低于 SQLite 的变量上限）
_IN_CHUNK = 500



# 旧名：测试从本模块 import（实现各在叶子模块）
_scene_card_beats = scene_card_beats
_coerce_triage_status = coerce_triage_status
_is_protagonist_role = is_protagonist_role


class SnowflakeWorkspaceService:
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

    @staticmethod
    def _present_turn(project_id: str, turn: dict[str, Any]) -> dict[str, Any]:
        if not turn.get("candidate_patch"):
            return turn
        return {**turn, "candidate_patch": present_draft(project_id, turn["candidate_patch"])}

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

    # 阶段 U（2026-09-17）：「先看 3 个方向」是教练日志里的一种回合，不再是独立的「候选」页签。
    # fail-closed（LLM 未启用即 409，与教练同一条路——以前 source="fallback" + 空列表让前端自己猜）；
    # 底稿与 generate / assistant 同源（draft_override 盖在存档上），作者的要求（ask）与第 10 步的
    # 聚焦场进提示；结果落成 turn_kind=candidates 的回合，回包带整条教练历史，教练下一轮就看得到
    # 作者看过哪些方向、选了哪个。
    def fe_step_candidates(self, project_id: str, step_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        self._require_step(step_key)
        body = payload or {}
        try:
            target_chars = max(40, min(int(body.get("target_chars") or 120), 400))
        except (TypeError, ValueError):
            target_chars = 120
        use_brief = body.get("use_direction_brief")
        use_brief = True if use_brief is None else bool(use_brief)
        ask = str(body.get("ask") or "").strip()[:600]
        focus_scene_id = str(body.get("focus_scene_id") or "").strip() or None
        if step_key != "scene_details":
            focus_scene_id = None
        latest_by_step = self._latest_by_step(project.project_id)
        llm_result = self._llm.step_candidates(
            project=project,
            step_key=step_key,
            target_chars=target_chars,
            latest_by_step=latest_by_step,
            draft_override=self._merged_draft_override(project.project_id, latest_by_step, step_key, body.get("draft_override")),
            author_ask=ask or None,
            focus_scene_id=focus_scene_id,
            author_direction_brief=(
                self._briefs.prompt_payload_for(project.project_id, step_key) if use_brief else None
            ),
        )
        candidates = list((llm_result.payload or {}).get("candidates") or [])
        if not candidates:
            raise DomainError(
                "SNOWFLAKE_CANDIDATES_EMPTY",
                "模型这次没有给出可用的方向，请再试一次（可以在输入框里把要求说得更具体）。",
                status_code=502,
                details={"node_id": "snowflake_step_candidates", "step_key": step_key, "llm_call_id": llm_result.llm_call_id},
            )
        turn = self._record_assistant_turn(
            project.project_id,
            step_key=step_key,
            message=ask or CANDIDATES_DEFAULT_ASK,
            focus_scene_id=focus_scene_id,
            result={"reply": "", "suggestions": [], "source": llm_result.source, "llm_call_id": llm_result.llm_call_id},
            turn_kind="candidates",
            candidates={"items": candidates, "target_chars": target_chars},
        )
        return {
            "source": llm_result.source,
            "llm_call_id": llm_result.llm_call_id,
            "candidates": candidates,
            "turn_id": turn.turn_id,
            "turn": self._present_turn(project.project_id, self._assistant_turn_payload(turn)),
            "assistant_history": self._presented_history(project.project_id),
        }

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

    def approve_step(
        self,
        project_id: str,
        step_key: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        # 阶段 X：``sync_catalog``（工作台总是带）= 这一步确认之后，把已经物化的场景卡跟上这一版构思。
        # 脚本 / 旧调用方不带它，行为与从前一样（待同步留给显式的 ``resync``）。
        sync_catalog = bool((payload or {}).get("sync_catalog")) and step_key in CATALOG_SYNC_STEPS
        latest_by_step = self._latest_by_step(project.project_id)
        run = latest_by_step.get(step_key)
        if run is None:
            raise DomainError("SNOWFLAKE_STEP_RUN_NOT_FOUND", "这一步骤还没有草稿。", status_code=404)
        if run.status in {"approved", "skipped"}:
            # 已经确认过的步骤再点一次确认：没有新的构思可跟，但上次留下的待同步（比如当时目录里
            # 还没有目标章）现在可能已经搬得动了。
            catalog_sync = self._auto_sync_catalog(project.project_id, actor_ref=actor_ref) if sync_catalog else None
            workspace = self.mutation_workspace(project.project_id)
            result = {"step": self._step_from_workspace(workspace, step_key), "workspace": workspace}
            if catalog_sync is not None:
                result["catalog_sync"] = catalog_sync
            return result
        if run.status != "pending_review":
            raise DomainError("SNOWFLAKE_STEP_RUN_NOT_APPROVABLE", "这一步骤当前状态不能被确认。", status_code=409)

        self._require_previous_gates(step_key, latest_by_step, allow_self=run.step_run_id)
        previous_run = self._runs.latest_confirmed(project.project_id, step_key, exclude_step_run_id=run.step_run_id)
        self._runs.supersede_others(run)
        run.status = "approved"
        run.approved_at = utcnow()
        run.health_json = self._step_health(
            step_key,
            run.draft_json or {},
            "approved",
            generation_source=(run.health_json or {}).get("generation_source"),
            # 出处事实随确认保留：触发入口和 generation_source 一样是这一版草稿的来历。
            trigger_source=(run.health_json or {}).get("trigger_source"),
        )
        # Snapshot "what I consumed, at what version" so a later upstream revision can
        # be diffed field-by-field instead of blindly staling everything downstream.
        run.consumed_input_sigs_json = snapshot_consumed_sigs(
            latest_by_step, list(self._input_refs(step_key, latest_by_step).keys())
        )
        self._sync_structured_step_data(project, step_key, run.draft_json or {}, run, approved=True)
        # 第一次批准只是把同一份待审稿转为 approved，并没有发生上游“修订”。
        # 导入/旧缓存可能已经把后续十步全部存成 pending_review；此时若按缺快照规则
        # 全部置 stale，前端就永远无法按依赖顺序补批准。只有存在上一版 approved run
        #（真正的重新批准）时，才计算并落地下游失效。
        downstream_impact = (
            self._mark_downstream_stale(run, previous_payload=previous_run.draft_json)
            if previous_run is not None
            else {
                "step_key": step_key,
                "affected_count": 0,
                "affected_step_run_ids": [],
                "affected_scene_plan_ids": [],
                "summary": "首次批准没有下游失效范围。",
            }
        )
        runtime_impact = (
            ProjectRuntimeInvalidationService(self.session).invalidate_for_snowflake_step(
                project.project_id,
                step_key,
                previous_payload=previous_run.draft_json if previous_run is not None else None,
                current_payload=run.draft_json or {},
            )
            if previous_run is not None
            else {
                "step_key": step_key,
                "scope": "none",
                "broad": False,
                "affected_count": 0,
                "affected_scene_ids": [],
                "summary": "First approval has no stale runtime scope.",
            }
        )
        self.session.flush()
        catalog_sync = self._auto_sync_catalog(project.project_id, actor_ref=actor_ref) if sync_catalog else None
        workspace = self.mutation_workspace(project.project_id)
        result = {
            "step": self._step_from_workspace(workspace, step_key),
            "workspace": workspace,
            "impact": self._combine_approval_impact(step_key, downstream_impact, runtime_impact),
        }
        if catalog_sync is not None:
            result["catalog_sync"] = catalog_sync
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

    def _auto_sync_catalog(self, project_id: str, *, actor_ref: str = "operator") -> dict[str, Any]:
        plans = self._scene_plans(project_id)
        excluded = self._excluded_scene_plan_ids(project_id)
        order_drift = self._scene_card_order_drift(project_id, plans)
        cards = self._scene_cards_by_id(project_id, [plan.scene_id for plan in plans])
        eligible: list[str] = []
        held: list[dict[str, Any]] = []
        for plan in plans:
            scene = cards.get(plan.scene_id)
            if scene is None:
                continue  # 还没物化的场：进目录走「整理为章节结构」
            patch = self._scene_card_resync_patch(plan, scene, excluded=plan.scene_plan_id in excluded)
            diff = self._scene_card_diff(scene, patch)
            if scene.scene_id in order_drift:
                diff["scene_order"] = order_drift[scene.scene_id]
            if not diff:
                continue
            reason = self._auto_sync_hold_reason(plan, scene, patch)
            if reason:
                held.append(
                    {
                        "scene_plan_id": plan.scene_plan_id,
                        "scene_id": plan.scene_id,
                        "title": plan.title or plan.summary or plan.scene_id,
                        "reason": reason,
                    }
                )
            else:
                eligible.append(plan.scene_plan_id)
        synced: list[str] = []
        trashed_empty_chapters: list[dict[str, Any]] = []
        notice: dict[str, Any] | None = None
        if eligible:
            outcome = self.resync_materialized_scenes(
                project_id,
                {"scene_plan_ids": eligible},
                actor_ref=f"auto_sync:{actor_ref or 'operator'}",
                include_workspace=False,
            )
            synced = [item["scene_id"] for item in outcome.get("results") or [] if item.get("synced")]
            trashed_empty_chapters = list(outcome.get("trashed_empty_chapters") or [])
            notice = outcome.get("notice")
        return {
            "synced_count": len(synced),
            "synced_scene_ids": synced,
            "held_count": len(held),
            "held": held,
            "trashed_empty_chapters": trashed_empty_chapters,
            **({"notice": notice} if notice else {}),
        }

    def _auto_sync_hold_reason(self, plan: SnowflakeScenePlan, scene: SceneCard, patch: dict[str, Any]) -> str:
        if str(plan.status or "") != "approved":
            return "plan_not_confirmed"
        if int(patch.get("trashed_flag") or 0) == 1 and self._scene_card_has_work(scene):
            return "would_trash_written_scene"
        return ""

    def _scene_card_has_work(self, scene: SceneCard) -> bool:
        if int(scene.words_current or 0) > 0:
            return True
        state = self.session.get(SceneRunState, scene.scene_id)
        if state is not None and (state.current_final_scene_row_id or str(state.scene_status or "ready") != "ready"):
            return True
        return (
            self.session.execute(select(FinalScene.row_id).where(FinalScene.scene_id == scene.scene_id).limit(1)).first()
            is not None
        )

    def resync_status(self, project_id: str) -> dict[str, Any]:
        """台子用的轻量读口：哪几场的场景卡落后于构思（不必为此拉整个工作台）。

        写作台 / AI 起草台对每一部作品都会问这一句，包括不是雪花法的作品——那不是错误：
        如实回答「这部作品没有构思侧可同步」（``supported: false``），而不是 409（浏览器会把它记成一条控制台报错）。
        """
        project = self._projects.require_project(project_id)
        if str(getattr(project, "planning_mode", "") or "") != "snowflake":
            return {"supported": False, "pending_count": 0, "pending_scene_plan_ids": [], "pending_scenes": []}
        return {"supported": True, **self._resync_status(project.project_id, self._scene_plans(project.project_id))}

    def accept_stale_step(self, project_id: str, step_key: str, payload: dict[str, Any] | None = None, *, actor_ref: str = "operator") -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        self._require_step(step_key)
        run = self._latest_by_step(project.project_id).get(step_key)
        if run is None:
            raise DomainError("SNOWFLAKE_STEP_RUN_NOT_FOUND", "这一步骤还没有草稿。", status_code=404)
        if run.status != "stale":
            raise DomainError("SNOWFLAKE_STEP_NOT_STALE", "只有被标记为过期的步骤才能确认仍然有效。", status_code=409)
        body = payload or {}
        note = str(body.get("note") or "").strip() or None
        accepted_at = utcnow()
        run.stale_accepted_at = accepted_at
        run.stale_accepted_by = actor_ref or "operator"
        run.stale_accepted_note = note
        # 阶段 E（E3 第二步）：「仍然有效」是对**现在的**上游版本说的——把消费的上游 step_run_id 与
        # 逐字段签名重新拍到当前，前端按 input_refs 对照上游版本的「上游已有新版本」提示随之清零，
        # 下一次上游修订也以作者确认过的这一版为基准比较。
        latest_by_step = self._latest_by_step(project.project_id)
        run.input_refs_json = self._input_refs(step_key, latest_by_step)
        run.consumed_input_sigs_json = snapshot_consumed_sigs(latest_by_step, list(run.input_refs_json.keys()))
        if step_key in SCENE_PLAN_STEPS:
            # R15a：09 / 10 的「已复核」同时是对这一步产出的场景计划说「仍然有效」——以前逐场复核只有一个
            # 没有界面调用的接口，作者点完步骤级的「已复核」，整理闸门仍被一场一场的「需要先复核」挡住。
            self._accept_stale_scene_plans(project.project_id, accepted_at=accepted_at, actor_ref=actor_ref, note=note)
        self.session.add(
            OperationLog(
                event_type="snowflake_step_stale_accepted",
                object_type="snowflake_step_run",
                object_ref=run.step_run_id,
                payload_json={
                    "project_id": project.project_id,
                    "step_key": step_key,
                    "accepted_at": accepted_at,
                    "accepted_by": actor_ref or "operator",
                    "note": note or "",
                    "stale_reason": run.stale_reason or "",
                },
            )
        )
        self.session.flush()
        workspace = self.mutation_workspace(project.project_id)
        return {"step": self._step_from_workspace(workspace, step_key), "workspace": workspace}

    def request_assistant(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        latest_by_step = self._latest_by_step(project.project_id)
        workspace = self._workspace_payload(project.project_id, lean=True)
        step_key = str(body.get("step_key") or workspace.get("current_step_key") or "book_brief").strip() or "book_brief"
        step = self._step_from_workspace(workspace, step_key)
        step = self._step_with_override(project.project_id, step, body.get("draft_override"), latest_by_step=latest_by_step)
        approved_context = self._approved_context(workspace)
        focus_scene_id = str(body.get("focus_scene_id") or "").strip() or None
        # 阶段 T：教练有记忆——当前要点（含作者撤下的）、继承的全书级要点、本步最近几轮问答
        conversation = self._briefs.conversation_payload(
            project.project_id,
            step_key,
            turns=workspace.get("assistant_history") or [],
        )
        llm_result = self._llm.assistant_reply(
            project=workspace["project"],
            step=step,
            message=str(body.get("message") or ""),
            approved_context=approved_context,
            latest_by_step=latest_by_step,
            focus_scene_id=focus_scene_id,
            conversation=conversation,
        )
        brief_update = llm_result.payload.get("brief_update")
        result = {
            **{key: value for key, value in llm_result.payload.items() if key != "brief_update"},
            "step_key": step_key,
            "source": llm_result.source,
            "llm_call_id": llm_result.llm_call_id,
        }
        turn = self._record_assistant_turn(
            project.project_id,
            step_key=step_key,
            message=str(body.get("message") or ""),
            focus_scene_id=focus_scene_id,
            result=result,
        )
        # 教练本轮对作者意图的完整重述 → 按 line_id 求差落到要点表（作者的条目不归教练管）
        _row, brief_delta = self._briefs.record_coach_restatement(
            project.project_id, step_key, brief_update, turn_id=turn.turn_id
        )
        if delta_changed(brief_delta):
            # 阶段 U：差异随回合落表——日志里每一轮自己说「要点 +1 / 改 1 / 撤 1」，不靠一闪而过的提示
            turn.brief_delta_json = deepcopy(brief_delta)
            self.session.add(
                OperationLog(
                    event_type="snowflake_direction_brief_restated",
                    object_type="snowflake_direction_brief",
                    object_ref=f"{project.project_id}:{step_key}",
                    payload_json={"project_id": project.project_id, "step_key": step_key, "turn_id": turn.turn_id, **brief_delta},
                )
            )
        history = self._presented_history(project.project_id)
        if result.get("candidate_patch"):
            result = {**result, "candidate_patch": present_draft(project.project_id, result["candidate_patch"])}
        return {
            **result,
            "turn_id": turn.turn_id,
            "created_at": turn.created_at,
            "assistant_history": history,
            "direction_brief": self._briefs.payload_for(project.project_id, step_key),
            "brief_delta": brief_delta,
        }

    def update_direction_brief(self, project_id: str, step_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """阶段 T：作者编辑本步意图要点（撤下 / 改写 / 加条 / 切换范围 / 恢复 / 是否继承上游）。
        请求里的 lines 就是作者要的列表；作者改过的条目归作者，教练此后不能再改写或撤下。"""
        project = self._require_snowflake_project(project_id)
        self._require_step(step_key)
        body = payload or {}
        lines = body.get("lines")
        if lines is not None and not isinstance(lines, list):
            raise DomainError("SNOWFLAKE_DIRECTION_BRIEF_INVALID", "要点列表必须是数组。", status_code=400)
        inherit = body.get("inherit_upstream")
        row = self._briefs.save_author_edit(
            project.project_id,
            step_key,
            lines=[item for item in (lines or []) if isinstance(item, dict)] if lines is not None else None,
            inherit_upstream=bool(inherit) if inherit is not None else None,
        )
        self.session.add(
            OperationLog(
                event_type="snowflake_direction_brief_edited",
                object_type="snowflake_direction_brief",
                object_ref=row.brief_id,
                payload_json={
                    "project_id": project.project_id,
                    "step_key": step_key,
                    "revision": row.revision,
                    "active_count": sum(1 for line in (row.lines_json or []) if isinstance(line, dict) and line.get("status") == "active"),
                    "inherit_upstream": bool(row.inherit_upstream),
                },
            )
        )
        self.session.flush()
        return {"direction_brief": self._briefs.payload_for(project.project_id, step_key)}

    def suggest_scene_triage(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        workspace = self._workspace_payload(project.project_id, lean=True)
        step = self._step_from_workspace(workspace, "scene_details")
        if not step.get("draft", {}).get("scenes"):
            raise DomainError("SNOWFLAKE_SCENE_DETAILS_REQUIRED", "需要先完成场景规划（场景细化）。", status_code=409)
        step = self._step_with_override(
            project.project_id, step, body.get("draft_override"), latest_by_step=self._latest_by_step(project.project_id)
        )
        llm_result = self._llm.scene_triage_suggestions(
            project=workspace["project"],
            step=step,
            approved_context=self._approved_context(workspace),
            author_direction_brief=self._briefs.prompt_payload_for(project.project_id, "scene_details"),
        )
        return {
            "items": self._attach_triage_identity(project.project_id, llm_result.payload.get("items") or []),
            "source": llm_result.source,
            "llm_call_id": llm_result.llm_call_id,
        }

    def save_scene_triage(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        items = list((payload or {}).get("items") or [])
        for item in items:
            if not isinstance(item, dict):
                continue
            scene = self._scene_plan_for_triage_item(project.project_id, item)
            diagnosis = diagnose_scene_detail(scene_plan_payload(scene))
            manual_status = coerce_triage_status(item.get("status") or item.get("manual_status"))
            recommended_status = coerce_triage_status(item.get("recommended_status")) or diagnosis["recommended_status"]
            effective_status = manual_status or recommended_status
            triage_id = str(item.get("triage_id") or "").strip()
            row = self.session.get(SnowflakeSceneTriageItem, triage_id) if triage_id else None
            if row is None:
                # 阶段 N：没带 triage_id 时按场景对回已有的记录（作者的裁定是这一场的**状态**，不是一条条日志）——
                # 否则一场会堆出多行，旧的「待删」行还留在库里，物化排除照旧生效。
                row = self._latest_triage_row(project.project_id, scene.scene_plan_id)
            if row is None:
                row = SnowflakeSceneTriageItem(
                    triage_id=f"snowflake_triage_{project.project_id}_{scene.scene_id}_{uuid.uuid4().hex[:8]}",
                    project_id=project.project_id,
                    scene_plan_id=scene.scene_plan_id,
                    scene_id=scene.scene_id,
                )
                self.session.add(row)
            row.scene_plan_id = scene.scene_plan_id
            row.scene_id = scene.scene_id
            row.recommended_status = recommended_status
            row.manual_status = manual_status
            row.effective_status = effective_status
            row.score = int_or_default(item.get("score"), diagnosis["score"])
            row.missing_fields_json = coerce_string_list(item.get("missing_fields")) or diagnosis["missing_fields"]
            row.fix_steps_json = coerce_string_list(item.get("fix_steps")) or diagnosis["fix_steps"]
            row.repair_patch_json = sanitize_scene_patch(item.get("repair_patch") or {})
            row.pressure_flags_json = coerce_string_list(item.get("pressure_flags")) or diagnosis["pressure_flags"]
            row.notes = str(item.get("notes") or "").strip()
            # blocking = 这一场被排除在物化之外（该重写 / 待删）；阶段 N 起它不再阻断全书。
            row.blocking = 1 if effective_status in EXCLUDED_TRIAGE_STATUSES else 0
            row.manual_override = 1 if manual_status and manual_status != recommended_status else 0
            row.llm_call_id = str(item.get("llm_call_id") or "").strip() or row.llm_call_id
        self.session.flush()
        workspace = self.mutation_workspace(project.project_id)
        return {"items": workspace["triage_items"], "workspace": workspace}

    def _latest_triage_row(self, project_id: str, scene_plan_id: str) -> SnowflakeSceneTriageItem | None:
        return latest_triage_rows(self.session, project_id).get(scene_plan_id)

    def _excluded_scene_plan_ids(self, project_id: str) -> set[str]:
        """作者裁定为该重写 / 待删的场景计划（阶段 N）：不物化、回流进回收站、节奏按 0 计。
        每一场只看最新的一条分诊记录（历史数据可能一场多行）。"""
        return excluded_scene_plan_ids(self.session, project_id)

    def _accept_stale_scene_plans(
        self,
        project_id: str,
        *,
        accepted_at: str,
        actor_ref: str = "operator",
        note: str | None = None,
    ) -> list[str]:
        """把还没复核的过期场景计划标成「已复核、仍然有效」，逐场留一条操作日志；返回复核了哪几场。"""
        accepted: list[str] = []
        for scene in self._scene_plans(project_id):
            if scene.status != "stale" or scene.stale_accepted_at:
                continue
            scene.stale_accepted_at = accepted_at
            scene.stale_accepted_by = actor_ref or "operator"
            scene.stale_accepted_note = note
            accepted.append(scene.scene_plan_id)
            self.session.add(
                OperationLog(
                    event_type="snowflake_scene_stale_accepted",
                    object_type="snowflake_scene_plan",
                    object_ref=scene.scene_plan_id,
                    payload_json={
                        "project_id": project_id,
                        "scene_id": scene.scene_id,
                        "scene_plan_id": scene.scene_plan_id,
                        "accepted_at": accepted_at,
                        "accepted_by": actor_ref or "operator",
                        "note": note or "",
                        "stale_reason": scene.stale_reason or "",
                    },
                )
            )
        return accepted

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

    def resync_materialized_scenes(
        self,
        project_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
        include_workspace: bool = True,
    ) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        dry_run = bool(body.get("dry_run", False))
        requested_plan_ids = {
            str(item or "").strip()
            for item in body.get("scene_plan_ids") or []
            if str(item or "").strip()
        }
        requested_scene_ids = {
            str(item or "").strip()
            for item in body.get("scene_ids") or []
            if str(item or "").strip()
        }
        all_plans = self._scene_plans(project.project_id)
        plans = all_plans
        if requested_plan_ids:
            plans = [plan for plan in plans if plan.scene_plan_id in requested_plan_ids]
        if requested_scene_ids:
            plans = [plan for plan in plans if plan.scene_id in requested_scene_ids]
        if not plans:
            raise DomainError("SNOWFLAKE_RESYNC_SCENES_NOT_FOUND", "未找到匹配的场景计划。", status_code=404)

        results: list[dict[str, Any]] = []
        affected_scene_ids: list[str] = []
        pending_moves: list[dict[str, Any]] = []
        touched_chapter_ids: set[str] = set()
        moved_scenes: dict[str, tuple[str, str]] = {}  # 阶段 Y：换了章的场，运行时行要跟着走
        excluded = self._excluded_scene_plan_ids(project.project_id)
        order_drift = self._scene_card_order_drift(project.project_id, all_plans)
        cards = self._scene_cards_by_id(project.project_id, [plan.scene_id for plan in plans])
        # 第一遍：只算补丁（不动库）。场景卡的位置 = 章 + 章内序，两样都受唯一索引
        # (chapter_id, scene_seq) 约束；一张一张地改会撞上还没搬走的那一张，所以位置留到最后
        # 由 _settle_scene_card_order 两阶段统一落位，补丁里不再带 scene_seq。
        prepared: list[tuple[SnowflakeScenePlan, SceneCard | None, dict[str, Any], dict[str, Any] | None]] = []
        for plan in plans:
            scene = cards.get(plan.scene_id)
            if scene is None:
                prepared.append((plan, None, {}, None))
                continue
            scene_patch = self._scene_card_resync_patch(plan, scene, excluded=plan.scene_plan_id in excluded)
            blocked_move = self._unmaterialized_chapter_move(project.project_id, scene, scene_patch)
            if blocked_move:
                # 搬不动就别搬：``SceneCard.chapter_id`` 是指向 chapter_goals 的外键，
                # 写一个目录里还不存在的章号 = FOREIGN KEY constraint failed，整次回流
                # 500「database operation failed」，连能同步的内容改动一起赔进去。
                # 作者重新分了章但还没「整理为章节结构」时这就是常态，不是异常。
                scene_patch.pop("chapter_id", None)
            prepared.append((plan, scene, scene_patch, blocked_move))

        if not dry_run:
            repositioned = {
                str(chapter_id)
                for _plan, scene, scene_patch, _blocked in prepared
                if scene is not None
                for chapter_id in (
                    (scene.chapter_id, scene_patch.get("chapter_id"))
                    if (
                        (scene_patch.get("chapter_id") and scene_patch.get("chapter_id") != scene.chapter_id)
                        or scene.scene_id in order_drift
                        or "trashed_flag" in scene_patch
                    )
                    else ()
                )
                if chapter_id
            }
            settle = self._park_scene_cards(project.project_id, repositioned)
        else:
            settle = None

        for plan, scene, scene_patch, blocked_move in prepared:
            if scene is None:
                results.append(
                    {
                        "scene_plan_id": plan.scene_plan_id,
                        "scene_id": plan.scene_id,
                        "synced": False,
                        "reason": "scene_not_materialized",
                        "diff": {},
                    }
                )
                continue
            source_chapter_id = str(scene.chapter_id or "")
            diff = self._scene_card_diff(scene, scene_patch)
            if scene.scene_id in order_drift:
                diff["scene_order"] = order_drift[scene.scene_id]
            if diff:
                affected_scene_ids.append(scene.scene_id)
            if not dry_run and diff:
                self._apply_scene_card_resync(scene, scene_patch)
                if source_chapter_id and scene.chapter_id and scene.chapter_id != source_chapter_id:
                    moved_scenes[scene.scene_id] = (source_chapter_id, str(scene.chapter_id))
                # 阶段 L：章目标写到场**搬进去之后**所在的章（以前先取旧章再搬，跨章移动会把目标章的
                # 章目标盖到旧章上）；两头的章都记下，最后重算 is_chapter_last。
                touched_chapter_ids.update({source_chapter_id, str(scene.chapter_id or "")})
                chapter = self.session.get(ChapterGoal, scene.chapter_id)
                if chapter is not None:
                    chapter.writer_brief_json = {
                        **dict(chapter.writer_brief_json or {}),
                        "source": "snowflake_resync",
                        "project_id": project.project_id,
                        "chapter_id": plan.chapter_id,
                        "chapter_goal": plan.chapter_goal or chapter.chapter_goal,
                    }
                    if plan.chapter_goal:
                        chapter.chapter_goal = plan.chapter_goal
                self.session.add(
                    OperationLog(
                        event_type="snowflake_scene_resynced",
                        object_type="scene_card",
                        object_ref=scene.scene_id,
                        payload_json={
                            "project_id": project.project_id,
                            "scene_id": scene.scene_id,
                            "scene_plan_id": plan.scene_plan_id,
                            "dry_run": False,
                            "diff_fields": sorted(diff.keys()),
                            "actor_ref": actor_ref or "operator",
                        },
                    )
                )
            entry = {
                "scene_plan_id": plan.scene_plan_id,
                "scene_id": scene.scene_id,
                "synced": bool(diff) and not dry_run,
                "reason": "changed" if diff else "already_current",
                "diff": diff,
            }
            if blocked_move:
                entry["blocked_chapter_move"] = blocked_move
                pending_moves.append({"scene_id": scene.scene_id, **blocked_move})
            results.append(entry)
        trashed_empty_chapters: list[dict[str, Any]] = []
        if not dry_run:
            self.session.flush()
            if settle is not None:
                self._settle_scene_card_order(project.project_id, settle)
            if moved_scenes:
                rehome_scenes(self.session, project.project_id, moved_scenes)
            remaining = touched_chapter_ids - set((settle or {}).get("chapter_ids") or ())
            if remaining:
                self._recompute_chapter_last(project.project_id, remaining)
            if settle is not None:
                # 场景卡跨章搬完之后，雪花建的旧章如果一张卡都不剩、这一版分章也不再用它，就移入回收站
                # （与「确认写入」同一条规则；章还被某个场景计划指着时绝不动它）。
                trashed_empty_chapters = trash_emptied_snowflake_chapters(
                    self.session,
                    project.project_id,
                    keep_chapter_ids={str(plan.chapter_id or "") for plan in self._scene_plans(project.project_id)},
                )

        if not dry_run:
            self.session.flush()
        affected_runtime = self._affected_runtime_summary(project.project_id, affected_scene_ids)
        result: dict[str, Any] = {
            "dry_run": dry_run,
            "results": results,
            "affected_runtime": affected_runtime,
            "trashed_empty_chapters": trashed_empty_chapters,
        }
        if include_workspace:
            # 确认即同步（approve_step）自己会在最后取一次工作台，不必在这里再算一遍
            result["workspace"] = self.mutation_workspace(project.project_id)
        if pending_moves:
            # 静默跳过等于撒谎：作者以为回流做完了，目录其实还停在上一版章节结构。
            targets = sorted({item["target_chapter_id"] for item in pending_moves})
            result["notice"] = {
                "code": "CHAPTER_MOVE_NEEDS_MATERIALIZE",
                "severity": "warning",
                "message": (
                    f"有 {len(pending_moves)} 场要搬到目录里还不存在的章"
                    f"（{'、'.join(targets[:3])}{'…' if len(targets) > 3 else ''}），"
                    "这一部分没有回流。请先「整理为章节结构」把新的章写进目录，再回流一次。"
                ),
                "pending_moves": pending_moves,
            }
        return result

    # ------------------------------------------------ 场景卡的章内顺序（回流）
    #
    # 目录里一章的场景卡 = 有场景计划的卡（顺序永远跟故事序走）+ 作者在章节编排里手加的卡（计划外，
    # 原来跟在哪张卡后面就还跟在哪）。回流不再逐张写 scene_seq：那一列受唯一索引
    # (chapter_id, scene_seq) 约束，两张卡对调、或一张卡搬进别的章，第一条 UPDATE 就会撞上还没挪走的
    # 那一张（500「database operation failed」）。改成：先把要动的章里所有活跃卡停到高位序号，
    # 改完内容 / 章归属之后，再按最终顺序一次落位。

    def _active_cards_by_chapter(self, project_id: str, chapter_ids: set[str] | None = None) -> dict[str, list[SceneCard]]:
        query = select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
        if chapter_ids is not None:
            query = query.where(SceneCard.chapter_id.in_(sorted(chapter_ids)))
        grouped: dict[str, list[SceneCard]] = {}
        for card in self.session.execute(query).scalars():
            grouped.setdefault(str(card.chapter_id), []).append(card)
        for cards in grouped.values():
            cards.sort(key=lambda card: (int(card.scene_seq or 0), str(card.scene_id)))
        return grouped

    def _scene_card_order_drift(
        self,
        project_id: str,
        scene_plans: list[SnowflakeScenePlan] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """目录里章内顺序和故事序对不上的场景卡：``scene_id → {before, after}``（都是章内第几场）。

        只比有场景计划的卡彼此之间的先后——计划外的卡（作者手加的场）和被略过 / 待删的场不占位，
        所以它们的存在不会让整章被误报成「待同步」。
        """
        plans = scene_plans if scene_plans is not None else self._scene_plans(project_id)
        rank = {plan.scene_id: index for index, plan in enumerate(plans)}
        drift: dict[str, dict[str, Any]] = {}
        for cards in self._active_cards_by_chapter(project_id).values():
            planned = [card for card in cards if card.scene_id in rank]
            wanted = sorted(planned, key=lambda card: rank[card.scene_id])
            for index, card in enumerate(planned):
                if wanted[index] is not card:
                    drift[card.scene_id] = {"before": index + 1, "after": wanted.index(card) + 1}
        return drift

    def _park_scene_cards(self, project_id: str, chapter_ids: set[str]) -> dict[str, Any] | None:
        """两阶段落位的第一阶段：记下这些章此刻的顺序，把它们的活跃卡停到不会冲突的高位序号上。"""
        if not chapter_ids:
            return None
        grouped = self._active_cards_by_chapter(project_id, chapter_ids)
        cards = [card for members in grouped.values() for card in members]
        old_order = {chapter_id: [card.scene_id for card in members] for chapter_id, members in grouped.items()}
        old_seq = {card.scene_id: int(card.scene_seq or 1) for card in cards}
        if cards:
            # 停靠位要高过这些章里**所有**卡的序号——包括回收站里的：这次回流可能正要把其中一张取回来
            # （改回「略过」/「待删」的裁定），它带着自己的旧序号变回活跃，不能和停靠位撞上。
            highest = self.session.execute(
                select(func.max(SceneCard.scene_seq)).where(
                    SceneCard.project_id == project_id, SceneCard.chapter_id.in_(sorted(chapter_ids))
                )
            ).scalar()
            park_base = int(highest or 0) + 1_000_000
            for offset, card in enumerate(cards):
                card.scene_seq = park_base + offset
            self.session.flush()
        return {"chapter_ids": set(chapter_ids), "old_order": old_order, "old_seq": old_seq}

    def _settle_scene_card_order(self, project_id: str, parked: dict[str, Any]) -> None:
        """第二阶段：每一章按「计划内的卡跟故事序、计划外的卡跟着它原来的前一张」重新编号。"""
        rank = {plan.scene_id: index for index, plan in enumerate(self._scene_plans(project_id))}
        # 计划外的卡原来前面依次是哪些计划内的卡（近的在前）：最近的那张若搬去了别的章，就跟再前面那张
        preceding_of: dict[str, list[str]] = {}
        for members in (parked.get("old_order") or {}).values():
            seen: list[str] = []
            for scene_id in members:
                if scene_id in rank:
                    seen.insert(0, scene_id)
                else:
                    preceding_of[scene_id] = list(seen)
        grouped = self._active_cards_by_chapter(project_id, set(parked["chapter_ids"]))
        for members in grouped.values():
            planned = sorted((card for card in members if card.scene_id in rank), key=lambda card: rank[card.scene_id])
            staying = {card.scene_id for card in planned}
            trailing: dict[str | None, list[SceneCard]] = {}
            for card in members:
                if card.scene_id in rank:
                    continue
                anchor = next((item for item in preceding_of.get(card.scene_id, []) if item in staying), None)
                trailing.setdefault(anchor, []).append(card)
            ordered = list(trailing.get(None, []))
            for card in planned:
                ordered.append(card)
                ordered.extend(trailing.get(card.scene_id, []))
            for index, card in enumerate(ordered, start=1):
                card.scene_seq = index
                card.is_chapter_last = 1 if index == len(ordered) else 0
        # 这次回流送进回收站的卡：把停靠前的序号还给它（回收站里的卡不受唯一索引约束）——
        # 作者从回收站手动恢复时，目录按这个序号把它插回原来的位置。
        old_seq = parked.get("old_seq") or {}
        if old_seq:
            for card in self.session.execute(
                select(SceneCard).where(SceneCard.scene_id.in_(sorted(old_seq)), SceneCard.trashed_flag == 1)
            ).scalars():
                card.scene_seq = old_seq[card.scene_id]
        self.session.flush()

    def _unmaterialized_chapter_move(
        self,
        project_id: str,
        scene: SceneCard,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        """这条补丁想把场景卡搬进一个目录里还不存在的章吗？

        重新分章只写构思侧（``SnowflakeScenePlan.chapter_id`` = 那一章钉住的目录章号，新章是刚铸的号），
        目录里的 ``ChapterGoal`` 要等「整理为章节结构」才建。两者之间的窗口里，构思侧
        指向的章号可以完全没有对应的目录行——而 ``SceneCard.chapter_id`` 是外键。
        """
        target = str(patch.get("chapter_id") or "").strip()
        if not target or target == scene.chapter_id:
            return None
        chapter = self.session.get(ChapterGoal, target)
        if chapter is not None and chapter.project_id == project_id and not chapter.trashed_flag:
            return None
        return {
            "target_chapter_id": target,
            "current_chapter_id": scene.chapter_id,
            "reason": "chapter_not_in_catalog",
        }

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

    def _resync_status(
        self,
        project_id: str,
        scene_plans: list[SnowflakeScenePlan],
        *,
        excluded: set[str] | None = None,
    ) -> dict[str, Any]:
        pending: list[dict[str, Any]] = []
        if excluded is None:
            excluded = self._excluded_scene_plan_ids(project_id)
        order_drift = self._scene_card_order_drift(project_id, scene_plans)
        cards = self._scene_cards_by_id(project_id, [plan.scene_id for plan in scene_plans])
        for plan in scene_plans:
            scene = cards.get(plan.scene_id)
            if scene is None:
                continue
            diff = self._scene_card_diff(
                scene, self._scene_card_resync_patch(plan, scene, excluded=plan.scene_plan_id in excluded)
            )
            if scene.scene_id in order_drift:
                diff["scene_order"] = order_drift[scene.scene_id]
            if not diff:
                continue
            pending.append(
                {
                    "scene_plan_id": plan.scene_plan_id,
                    "scene_id": plan.scene_id,
                    "title": plan.title or plan.summary or plan.scene_id,
                    "changed_fields": sorted(diff.keys()),
                    # 阶段 X：台子只提示**已确认**的规划落后于目录；还在改的草稿不算「待同步」
                    "plan_status": str(plan.status or ""),
                }
            )
        return {
            "pending_count": len(pending),
            "pending_scene_plan_ids": [item["scene_plan_id"] for item in pending],
            "pending_scenes": pending,
        }

    def _scene_cards_by_id(self, project_id: str, scene_ids: list[str]) -> dict[str, SceneCard]:
        """这些场景计划已经物化出的场景卡（含回收站里的）：一次 ``IN`` 查询，代替逐场 ``session.get``（B06-04 / 06）。"""
        wanted = sorted({scene_id for scene_id in scene_ids if scene_id})
        cards: dict[str, SceneCard] = {}
        for start in range(0, len(wanted), _IN_CHUNK):
            chunk = wanted[start : start + _IN_CHUNK]
            for card in self.session.execute(select(SceneCard).where(SceneCard.scene_id.in_(chunk))).scalars():
                if card.project_id == project_id:
                    cards[card.scene_id] = card
        return cards

    def _recompute_chapter_last(self, project_id: str, chapter_ids: set[str]) -> None:
        """回流搬过场之后重算每章的章末标记（scene_criticality 把章末当高潮位）——物化时算过一次，
        搬动后旧章的末场变了、新章的末场也变了，以前都没有重算。"""
        for chapter_id in sorted(cid for cid in chapter_ids if cid):
            cards = self.session.execute(
                select(SceneCard).where(
                    SceneCard.project_id == project_id,
                    SceneCard.chapter_id == chapter_id,
                    SceneCard.trashed_flag == 0,
                )
            ).scalars().all()
            if not cards:
                continue
            last = max(cards, key=lambda card: (int(card.scene_seq or 0), str(card.scene_id)))
            for card in cards:
                card.is_chapter_last = 1 if card is last else 0

    @staticmethod
    def _scene_card_resync_patch(plan: SnowflakeScenePlan, scene: SceneCard, *, excluded: bool = False) -> dict[str, Any]:
        # 阶段 C：呈现方式与篇幅带同物化一个口径——summary 场回流也拿数值带。
        rendering_mode = effective_rendering_mode(plan.scene_type, plan.rendering_mode)
        # 阶段 N：作者裁定该重写 / 待删的场与「略过」同路——卡进回收站，改回裁定时取回。
        skipped = rendering_mode == "skip" or bool(excluded)
        if rendering_mode == "summary":
            target_length_band = SUMMARY_LENGTH_BAND
        else:
            # 从概述改回完整场：规划行多半没有显式篇幅带（前端不上行它），不能让场景卡
            # 继续挂着 200-500 的概述带——回到默认 medium。
            current_band = scene.target_length_band if scene.target_length_band != SUMMARY_LENGTH_BAND else None
            target_length_band = plan.target_length_band or current_band or "medium"
        previous_brief = dict(scene.writer_brief_json or {})
        carried = {key: value for key, value in previous_brief.items() if key not in {"title", "seeded_title", "desk_edited_at"}}
        brief = {
            **carried,
            # 阶段 X：题名跟构思走，除非作者在台子上改过（``desk_edited_at`` 是阶段 X 留下的旧记号，回流时顺手清掉）
            **followed_scene_title(previous_brief, real_scene_title(plan.title, plan.summary)),
            "source": "snowflake_resync",
            "scene_plan_id": plan.scene_plan_id,
            "project_id": plan.project_id,
            "chapter_id": plan.chapter_id,
            "scene_id": plan.scene_id,
            "chapter_goal": plan.chapter_goal,
            "scene_crucible": plan.scene_crucible,
            "goal": plan.goal,
            "conflict": plan.conflict,
            "setback": plan.setback,
            "reaction": plan.reaction,
            "dilemma": plan.dilemma,
            "decision": plan.decision,
            "cost_requirement": plan.cost_requirement,
            "expected_reader_emotion": plan.expected_reader_emotion or "",
            "story_time": plan.story_time or "",
            "exception_reason": plan.exception_reason or "",
            "primary_form": plan.scene_type,
            "rendering_mode": rendering_mode,
            "timebox": target_length_band or "medium",
            # 阶段 I：规划里改成「略过」的已物化场，回流把场景卡送进回收站（可恢复）；改回来时再取回。
            # 作者自己扔进回收站的卡不带这个标记，回流不碰它。阶段 N：该重写 / 待删的裁定走同一条路。
            "skipped_by_plan": skipped,
            "excluded_by_triage": bool(excluded),
        }
        # 与物化同一配方（scene_card_beats）：两个写入方各算一套，刚物化完的每一场
        # 都会因 beats_json 不同被报成「待同步」，横幅在物化当刻就喊 N 场。
        detail = scene_plan_payload(plan)
        beats = scene_card_beats(str(detail.get("scene_type") or "proactive"), detail)
        trash_patch: dict[str, Any] = {}
        if skipped:
            trash_patch["trashed_flag"] = 1
        elif int(scene.trashed_flag or 0) and bool((scene.writer_brief_json or {}).get("skipped_by_plan")):
            trash_patch["trashed_flag"] = 0
        return {
            **trash_patch,
            "scene_goal": plan.summary or plan.goal or scene.scene_goal,
            "beats_json": beats or list(scene.beats_json or []),
            "must_include_text": plan.must_include_text or scene.must_include_text,
            "exit_change": plan.exit_change or plan.setback or plan.decision or scene.exit_change,
            "hook": plan.hook or scene.hook,
            "target_length_band": target_length_band,
            # P2：重新分章后，回流要把场景卡也搬到新章去，否则目录停留在上一版结构。
            # 章内顺序不在补丁里：它由 _settle_scene_card_order 按故事序两阶段落位，
            # 待同步判定走 _scene_card_order_drift（只比计划内的卡彼此的先后）。
            "chapter_id": plan.chapter_id or scene.chapter_id,
            "scene_type": plan.scene_type or scene.scene_type,
            "pov_character_id": plan.pov_character_id or scene.pov_character_id,
            "onstage_chars_json": list(plan.onstage_chars_json or scene.onstage_chars_json or []),
            "location": plan.location or scene.location,
            "writer_brief_json": brief,
        }

    # writer_brief_json 里承载作者内容的戏剧键：pending 检测只看它们。
    # 其余键要么是出处/标识（source、scene_plan_id/outline_plan_id、chapter_id、scene_id）、
    # 要么是 resync 补丁才回填的富化键（primary_form 与物化写的 scene_form 同义、
    # chapter_goal 汇总）——物化与 resync 两个写入方对这些键的写法天生不同，
    # 拿去整体 != 会让刚物化完的每一场都被报成待同步（纯假阳性）。
    # 场卡其余内容（scene_goal/beats/hook/location/POV/scene_type…）由顶层列对比兜底。
    _BRIEF_CONTENT_KEYS = (
        "scene_crucible", "goal", "conflict", "setback", "reaction", "dilemma", "decision", "cost_requirement",
        "expected_reader_emotion", "story_time", "exception_reason",
    )

    @staticmethod
    def _writer_brief_comparable(value: Any) -> Any:
        """writer_brief_json 的可比形态：只取戏剧内容键、剥空值（空串与缺席等价）。"""
        if not isinstance(value, dict):
            return value
        comparable: dict[str, Any] = {}
        for key in SnowflakeWorkspaceService._BRIEF_CONTENT_KEYS:
            item = value.get(key)
            if item is None or (isinstance(item, str) and not item.strip()):
                continue
            comparable[key] = item
        # 阶段 C：呈现方式只在非默认（summary）时参与比较——阶段 C 之前物化的场景卡没有这个键，
        # 把「缺席」当成 full，才不会让全书在升级当刻集体报「待同步」。
        rendering_mode = str(value.get("rendering_mode") or "").strip().lower()
        if rendering_mode and rendering_mode != "full":
            comparable["rendering_mode"] = rendering_mode
        return comparable

    @staticmethod
    def _scene_card_diff(scene: SceneCard, patch: dict[str, Any]) -> dict[str, dict[str, Any]]:
        diff: dict[str, dict[str, Any]] = {}
        for field, after in patch.items():
            before = getattr(scene, field)
            if field == "writer_brief_json":
                before_cmp = SnowflakeWorkspaceService._writer_brief_comparable(before)
                after_cmp = SnowflakeWorkspaceService._writer_brief_comparable(after)
                # 阶段 X：构思里的题名改了也算待同步——但只对播过题名的卡比（``seeded_title`` 键在）。
                # 阶段 X 之前物化的卡没有这个键；拿「缺席」去比，全书会在升级当刻集体报「待同步」。
                if isinstance(before, dict) and "seeded_title" in before and isinstance(before_cmp, dict):
                    before_cmp["seeded_title"] = str(before.get("seeded_title") or "")
                    after_cmp["seeded_title"] = str((after or {}).get("seeded_title") or "")
                if before_cmp == after_cmp:
                    continue
            if before != after:
                diff[field] = {"before": before, "after": after}
        return diff

    @staticmethod
    def _apply_scene_card_resync(scene: SceneCard, patch: dict[str, Any]) -> None:
        for field, value in patch.items():
            setattr(scene, field, value)

    def _affected_runtime_summary(self, project_id: str, scene_ids: list[str]) -> dict[str, int]:
        unique_scene_ids = list(dict.fromkeys(scene_ids))
        if not unique_scene_ids:
            return {"final_scene_count": 0, "author_draft_count": 0, "llm_call_count": 0}
        final_scene_count = self.session.query(FinalScene).filter(FinalScene.scene_id.in_(unique_scene_ids)).count()
        author_draft_count = self.session.query(AuthorDraft).filter(
            AuthorDraft.object_type == "scene",
            AuthorDraft.object_id.in_(unique_scene_ids),
        ).count()
        llm_call_count = self.session.query(LlmCall).filter(
            LlmCall.project_id == project_id,
            LlmCall.scene_id.in_(unique_scene_ids),
        ).count()
        return {
            "final_scene_count": final_scene_count,
            "author_draft_count": author_draft_count,
            "llm_call_count": llm_call_count,
        }

    def _triage_items(
        self,
        project_id: str,
        *,
        scene_plans: list[SnowflakeScenePlan] | None = None,
        triage_rows: dict[str, SnowflakeSceneTriageItem] | None = None,
    ) -> list[dict[str, Any]]:
        # 每一场只看最新的一条分诊记录——与物化 / 回流 / 设计上下文同一个口径（snowflake_triage）。
        # 以前按库里的扫描顺序取行：作者看到「通过」，整理时这一场却按更新的「待删」不建卡（B06-02）。
        stored = latest_triage_rows(self.session, project_id) if triage_rows is None else triage_rows
        items: list[dict[str, Any]] = []
        for scene in self._scene_plans(project_id) if scene_plans is None else scene_plans:
            row = stored.get(scene.scene_plan_id)
            if row is not None:
                items.append(self._triage_payload(row))
                continue
            diagnosis = diagnose_scene_detail(scene_plan_payload(scene))
            items.append(
                {
                    "triage_id": "",
                    "scene_plan_id": scene.scene_plan_id,
                    "scene_id": scene.scene_id,
                    "title": scene.title or scene.summary or scene.scene_id,
                    "primary_form": scene.scene_type,
                    "scene_type": scene.scene_type,
                    "status": "",
                    "manual_status": "",
                    "notes": "",
                    "recommended_status": diagnosis["recommended_status"],
                    "effective_status": "unreviewed",
                    "triage_source": "auto_diagnosis",
                    "score": diagnosis["score"],
                    "pressure_flags": diagnosis["pressure_flags"],
                    "missing_fields": diagnosis["missing_fields"],
                    "fix_steps": diagnosis["fix_steps"],
                    "repair_patch": {},
                    "blocking": False,
                    "manual_override": False,
                }
            )
        return items

    def _presented_history(self, project_id: str) -> list[dict[str, Any]]:
        return [self._present_turn(project_id, turn) for turn in self._assistant_history(project_id)]

    def _assistant_history(self, project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(SnowflakeAssistantTurn)
            .where(SnowflakeAssistantTurn.project_id == project_id)
            .order_by(SnowflakeAssistantTurn.created_at.desc(), SnowflakeAssistantTurn.turn_id.desc())
            .limit(max(1, min(int(limit or 50), 200)))
        ).scalars().all()
        return [self._assistant_turn_payload(row) for row in reversed(rows)]

    def _record_assistant_turn(
        self,
        project_id: str,
        *,
        step_key: str,
        message: str,
        focus_scene_id: str | None,
        result: dict[str, Any],
        turn_kind: str = "chat",
        candidates: dict[str, Any] | None = None,
    ) -> SnowflakeAssistantTurn:
        turn = SnowflakeAssistantTurn(
            turn_id=f"snowflake_assistant_turn_{project_id}_{uuid.uuid4().hex[:10]}",
            project_id=project_id,
            step_key=step_key,
            focus_scene_id=focus_scene_id,
            user_message=str(message or "").strip(),
            reply=str(result.get("reply") or "").strip(),
            suggestions_json=coerce_string_list(result.get("suggestions")),
            candidate_label=str(result.get("candidate_label") or "").strip() or None,
            candidate_patch_json=deepcopy(result.get("candidate_patch") or {}) or None,
            source=str(result.get("source") or "fallback").strip() or "fallback",
            llm_call_id=str(result.get("llm_call_id") or "").strip() or None,
            turn_kind="candidates" if turn_kind == "candidates" else "chat",
            candidates_json=deepcopy(candidates) if candidates else None,
        )
        self.session.add(turn)
        self.session.flush()
        return turn

    def _resolve_direction_turn(
        self,
        project_id: str,
        body: dict[str, Any],
        *,
        direction_text: str,
    ) -> tuple[SnowflakeAssistantTurn | None, int | None, str | None]:
        """阶段 U：方向来源的教练回合。返回 (回合, 方向回合里的第几条, 那一条的标签)；没指回合 → (None, None, None)。"""
        turn_id = str(body.get("direction_turn_id") or "").strip()
        if not turn_id:
            return None, None, None
        if not direction_text:
            raise DomainError(
                "SNOWFLAKE_DIRECTION_TEXT_REQUIRED",
                "指明了方向来源的回合，却没有带方向正文。",
                status_code=400,
                details={"direction_turn_id": turn_id},
            )
        turn = self.session.get(SnowflakeAssistantTurn, turn_id)
        if turn is None or turn.project_id != project_id:
            raise DomainError(
                "SNOWFLAKE_DIRECTION_TURN_NOT_FOUND",
                "方向来源的教练回合不存在（可能已被清理），请重新让教练给方向。",
                status_code=404,
                details={"direction_turn_id": turn_id},
            )
        if turn.turn_kind != "candidates":
            return turn, None, None
        items = list((turn.candidates_json or {}).get("items") or [])
        raw_index = body.get("direction_index")
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = -1
        if index < 0 or index >= len(items):
            raise DomainError(
                "SNOWFLAKE_DIRECTION_INDEX_INVALID",
                "要采纳的方向编号不在这一组方向里。",
                status_code=400,
                details={"direction_turn_id": turn_id, "direction_index": raw_index, "count": len(items)},
            )
        item = items[index] if isinstance(items[index], dict) else {}
        return turn, index, (str(item.get("label") or "").strip() or None)

    @staticmethod
    def _assistant_turn_payload(row: SnowflakeAssistantTurn) -> dict[str, Any]:
        turn_kind = "candidates" if (row.turn_kind or "chat") == "candidates" else "chat"
        return {
            "turn_id": row.turn_id,
            "project_id": row.project_id,
            "step_key": row.step_key,
            "focus_scene_id": row.focus_scene_id or None,
            "message": row.user_message or "",
            "reply": row.reply or "",
            "suggestions": list(row.suggestions_json or []),
            "candidate_label": row.candidate_label or None,
            "candidate_patch": deepcopy(row.candidate_patch_json or {}) or None,
            "source": row.source or "fallback",
            "llm_call_id": row.llm_call_id,
            "created_at": row.created_at,
            # 阶段 U：回合种类（chat / candidates）、方向回合的几条方向、本轮要点差异、被哪一版生成采纳过
            "turn_kind": turn_kind,
            "candidates": (
                [deepcopy(item) for item in ((row.candidates_json or {}).get("items") or []) if isinstance(item, dict)]
                if turn_kind == "candidates"
                else []
            ),
            "brief_delta": deepcopy(row.brief_delta_json) if row.brief_delta_json else None,
            "adoption": deepcopy(row.adoption_json) if row.adoption_json else None,
        }

    def _triage_payload(self, row: SnowflakeSceneTriageItem) -> dict[str, Any]:
        scene = self.session.get(SnowflakeScenePlan, row.scene_plan_id)
        return {
            "triage_id": row.triage_id,
            "scene_plan_id": row.scene_plan_id,
            "scene_id": row.scene_id,
            "title": scene.title or scene.summary or row.scene_id if scene is not None else row.scene_id,
            "primary_form": scene.scene_type if scene is not None else "",
            "scene_type": scene.scene_type if scene is not None else "",
            "status": row.manual_status or "",
            "manual_status": row.manual_status or "",
            "notes": row.notes or "",
            "recommended_status": row.recommended_status or "",
            "effective_status": row.effective_status or row.manual_status or row.recommended_status or "",
            "triage_source": "author_saved",
            "score": row.score,
            "pressure_flags": list(row.pressure_flags_json or []),
            "missing_fields": list(row.missing_fields_json or []),
            "fix_steps": list(row.fix_steps_json or []),
            "repair_patch": deepcopy(row.repair_patch_json or {}),
            "blocking": bool(row.blocking),
            "manual_override": bool(row.manual_override),
        }

    #: 见 ``snowflake_gate.materialization_gate``
    _materialization_gate = staticmethod(materialization_gate)

    def _sync_structured_step_data(
        self,
        project: StoryProject,
        step_key: str,
        draft: dict[str, Any],
        run: SnowflakeStepRun,
        *,
        approved: bool = False,
    ) -> dict[str, Any] | None:
        """同步结构化步数据。返回「作者必须知道、但不属于草稿」的事实（目前只有章表收缩）。"""
        if step_key in {"character_sheets", "character_synopses", "character_bibles"}:
            self._sync_character_plans(project.project_id, step_key, draft.get("characters") or [], approved=approved)
        if step_key == "long_synopsis":
            return self._sync_chapter_plans(project.project_id, draft, run, approved=approved)
        if step_key in {"scene_list", "scene_details"}:
            self._sync_scene_plans(project.project_id, step_key, draft.get("scenes") or [], run, approved=approved)
        return None

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

    def _sync_character_plans(self, project_id: str, step_key: str, characters: list[Any], *, approved: bool) -> None:
        # 草稿已按规范口径落库（角色 id 带作品前缀、缺 id 的已铸号）；按 character_id 一次预读本作品的角色计划
        plans = {
            row.character_id: row
            for row in self.session.execute(
                select(SnowflakeCharacterPlan).where(SnowflakeCharacterPlan.project_id == project_id)
            ).scalars()
        }
        for item in characters:
            if not isinstance(item, dict):
                continue
            character_id = str(item.get("character_id") or "").strip()
            if not character_id:
                continue  # 完全空的成员不进名册（也没有铸号）
            display_name = str(item.get("display_name") or item.get("name") or character_id).strip()
            plan = plans.get(character_id)
            if plan is None:
                # 旧规则 snowflake_character_plan_{project}_{raw} 恰好等于 snowflake_character_plan_{规范 id}
                plan = SnowflakeCharacterPlan(
                    character_plan_id=f"snowflake_character_plan_{character_id}",
                    project_id=project_id,
                    character_id=character_id,
                    display_name=display_name,
                )
                self.session.add(plan)
                plans[character_id] = plan
            plan.display_name = display_name
            plan.role = item.get("role") or plan.role
            plan.source_step_key = step_key
            plan.status = "approved" if approved else "draft"
            plan.stale_reason = None
            if step_key == "character_sheets":
                plan.summary_json = item
            elif step_key == "character_synopses":
                plan.synopsis_json = item
            elif step_key == "character_bibles":
                plan.bible_json = item

            if approved and step_key in {"character_sheets", "character_bibles"}:
                self._sync_story_character(project_id, character_id, display_name, item, step_key)

    def _sync_story_character(self, project_id: str, character_id: str, display_name: str, item: dict[str, Any], step_key: str) -> None:
        row = self.session.get(StoryCharacter, character_id)
        if row is not None and row.project_id != project_id:
            # 全局主键上别的作品的人：绝不改写（B06-01 修掉的就是这个跨作品覆盖）
            return
        if row is None:
            row = StoryCharacter(
                character_id=character_id,
                project_id=project_id,
                display_name=display_name,
                role=item.get("role"),
                summary_json={},
                bible_json={},
                status="approved",
            )
            self.session.add(row)
        row.display_name = display_name
        row.role = item.get("role") or row.role
        row.status = "approved"
        if step_key == "character_sheets":
            row.summary_json = item
        elif step_key == "character_bibles":
            row.bible_json = item

    def _sync_scene_plans(
        self,
        project_id: str,
        step_key: str,
        scenes: list[Any],
        run: SnowflakeStepRun,
        *,
        approved: bool,
    ) -> None:
        current_chapter_id = f"{project_id}_CH01"
        seq_by_chapter: dict[str, int] = {}
        minted = False
        # P1-2：同一份 payload 里出现两次的 row_uid 必须拆开。前端 addScene 曾用
        # `"S" + (list.length + 1)` 编号，删掉中间一场后新增就会撞上仍然存活的那一场，
        # 于是后一条会绑到前一条的行上，把它的内容整段覆盖掉。
        seen_row_uids: set[str] = set()
        seen_scene_ids: set[str] = set()
        touched_row_uids: set[str] = set()
        # 本作品的全部场景计划（含软删的：同一 row_uid 回来要复活它）一次读进来，逐行对位查字典——
        # 以前每一行两三条查询，60 场的自动保存光对位就是一百多条语句（B06-06）。新建与认领 row_uid 时同步更新两张表。
        known_plans = list(
            self.session.execute(select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)).scalars()
        )
        by_row_uid: dict[str, SnowflakeScenePlan] = {}
        by_scene_id: dict[str, SnowflakeScenePlan] = {}
        for known in known_plans:
            if known.row_uid:
                by_row_uid.setdefault(known.row_uid, known)
            if known.scene_id:
                by_scene_id.setdefault(known.scene_id, known)
        for index, item in enumerate(scenes, start=1):
            if not isinstance(item, dict):
                continue
            # Identity is anchored on the immutable row_uid (P1-1). Fall back to the
            # legacy scene_id lookup so step-9 drafts and pre-migration rows still
            # bind to the plan that step-8 seeded — but never trust an author's edit
            # of scene_id / chapter_id to *re-key* an existing row.
            row_uid = str(item.get("row_uid") or "").strip()
            incoming_scene_id = str(item.get("scene_id") or "").strip()
            # 本次 payload 内重号：当作一条新戏重新铸造身份，且**不再**走 scene_id 回退
            # ——否则回退会把它又认到刚被前一条占用的那一行上，等于没拆。
            duplicate_in_payload = bool(row_uid) and row_uid in seen_row_uids
            if duplicate_in_payload:
                row_uid = ""
            plan = by_row_uid.get(row_uid) if row_uid else None
            if plan is None and not duplicate_in_payload and incoming_scene_id:
                # row_uid 缺席或未命中时回退到 scene_id 查找：规划器骨架与 LLM 结构化输出
                # 只回显 scene_id（提示词明确要求 row_uid 留空），这条回退是第 10 步能绑回
                # 第 9 步建下的行、而不是每次生成都复制一份的唯一依据。
                plan = by_scene_id.get(incoming_scene_id)
            if plan is not None and plan.row_uid and plan.row_uid in seen_row_uids:
                plan = None  # 已被本轮前一条认领，不能二次绑定
            created = plan is None
            if plan is not None and plan.removed_at:
                # 作者把删掉的场又加了回来（同一 row_uid）：复活，而不是撞唯一索引。
                plan.removed_at = None
                plan.removed_by = None
            if plan is not None and plan.orphaned_flag:
                # 孤儿标记必须在这里清，不能只在上面那个「复活」分支里清：已物化的场被删时
                # 走的是**打标记不软删**那条路（removed_at 保持 NULL），所以复活分支永远
                # 摸不到它。结果是 orphaned_flag 只写不清，分章面板的 blocker 永久挂着、
                # 「确认分章」按钮再也点不动——而它自己的提示语还写着「请先决定」。
                # 场回到了场景列表里，按定义就不再是孤儿。
                plan.orphaned_flag = 0

            input_chapter_id = str(item.get("chapter_id") or current_chapter_id or f"{project_id}_CH01").strip()
            if created:
                # First time we see this row — mint its identity exactly once.
                chapter_id = input_chapter_id
            else:
                # Already exists — system identity is locked, author input is ignored.
                chapter_id = plan.chapter_id or input_chapter_id
            current_chapter_id = chapter_id

            # scene_seq = 这一场在它所在章里的位置，按草稿行序逐章计数；草稿行自带的 scene_seq 不再采信——
            # 前端 09 发的是全书序 i + 1，模型给的什么都有，混进来会让同一列有两种语义
            # （循环结束后 renumber_scene_seq 还会按故事序对**全部**活跃场统一重算一遍）。
            scene_seq = seq_by_chapter.get(chapter_id, 0) + 1
            seq_by_chapter[chapter_id] = scene_seq

            if created:
                row_uid = row_uid or mint_row_uid()
                # P1-2 铸造规则：草稿自带 scene_id 就沿用它（骨架/LLM 输出靠这个字符串
                # 在第 9→10 步之间对位；换成别的值会让第 10 步认不回第 9 步的行）。只有
                # 在它缺席或已被占用时才用 row_uid 铸——前端 canonFromFE 恰好不发
                # scene_id，所以作者手改场景表这一路始终走 row_uid 基、天然不撞号。
                scene_id = incoming_scene_id or mint_scene_id(project_id, row_uid)
                if scene_id in seen_scene_ids or scene_id in by_scene_id:
                    scene_id = mint_scene_id(project_id, row_uid)
                plan = SnowflakeScenePlan(
                    scene_plan_id=f"snowflake_scene_plan_{project_id}_{row_uid}",
                    project_id=project_id,
                    row_uid=row_uid,
                    scene_id=scene_id,
                    chapter_id=chapter_id,
                    scene_seq=scene_seq,
                )
                self.session.add(plan)
                known_plans.append(plan)
                by_row_uid.setdefault(row_uid, plan)
                by_scene_id.setdefault(scene_id, plan)
                minted = True
            else:
                scene_id = plan.scene_id
                if not plan.row_uid:
                    # Adopt a row_uid for a legacy row matched via scene_id.
                    plan.row_uid = row_uid or mint_row_uid()
                    by_row_uid.setdefault(plan.row_uid, plan)
                    minted = True
                row_uid = plan.row_uid

            before = None if created else scene_plan_content_signature(plan)
            plan.scene_seq = scene_seq
            plan.source_step_run_id = run.step_run_id
            # Discard any author-supplied scene_id / chapter_id — those are system
            # identity, not editable narrative fields.
            patch = sanitize_scene_patch(item)
            patch.pop("scene_id", None)
            patch.pop("chapter_id", None)
            if step_key == "scene_details" and not created:
                # 形态与视角归 09（场景列表）：第 10 步只深化三拍，不改已有行的这两样（F02-01 的后端兜底——
                # 前端第 10 步曾把渲染时的默认值冻进本地计划，推 10 时把 09 刚改过的形态与视角改回去）。
                for key in SCENE_LIST_OWNED_FIELDS:
                    patch.pop(key, None)
            self._apply_scene_patch(plan, patch)
            # 阶段 G：草稿同步只把**内容真的变了**（或新建）的场打回 draft；「AI 补全这一场」和
            # 一次无谓的整表 PATCH 不再把其余几十场的确认与复核留痕一起清零。批准仍然整表置 approved。
            if approved or created or before != scene_plan_content_signature(plan):
                plan.status = "approved" if approved else "draft"
                plan.stale_reason = None
                plan.stale_accepted_at = None
                plan.stale_accepted_by = None
                plan.stale_accepted_note = None
            if created and not plan.title:
                plan.title = str(item.get("title") or item.get("summary") or f"场景 {index:02d}").strip()
            if created and not plan.chapter_title:
                plan.chapter_title = str(item.get("chapter_title") or chapter_id).strip()
            plan.diagnosis_json = diagnose_scene_detail(scene_plan_payload(plan))

            seen_row_uids.add(row_uid)
            seen_scene_ids.add(scene_id)
            touched_row_uids.add(row_uid)

            # Stamp the minted identity back onto the draft row so the persisted
            # draft_json and every later re-seed carry the same stable anchor.
            if item.get("row_uid") != row_uid or item.get("scene_id") != scene_id or item.get("chapter_id") != chapter_id:
                item["row_uid"] = row_uid
                item["scene_id"] = scene_id
                item["chapter_id"] = chapter_id
                minted = True

        if step_key == "scene_list" and touched_row_uids:
            self._reconcile_removed_scene_plans(project_id, touched_row_uids, plans=known_plans)

        # 章内序统一重算（scene_seq 的唯一写入方）。故事序的来源：09 同步用**这一份**草稿的行序；
        # 第 10 步的草稿可能只带回一部分场（分批生成 / 单场补全），不能拿它当全书顺序——读最新 09 草稿。
        self.session.flush()
        renumber_scene_seq(
            self.session,
            project_id,
            positions=positions_from_rows(scenes) if step_key == "scene_list" else None,
        )

        if minted and isinstance(run.draft_json, dict):
            run.draft_json = {**run.draft_json, "scenes": scenes}
            # 行是就地改的：旧值与新值在 JSON 比较下相等，SQLAlchemy 会判「没变」而不写库——
            # 铸好的 row_uid / scene_id 于是只活在本次回包里，落库的草稿仍然没有身份。
            flag_modified(run, "draft_json")

    def _reconcile_removed_scene_plans(
        self,
        project_id: str,
        kept_row_uids: set[str],
        *,
        plans: list[SnowflakeScenePlan] | None = None,
    ) -> None:
        """P1-3 收口：把不在本次场景列表里的场标记为已删除。

        只在 ``scene_list`` 步生效 —— 「哪些场存在」是第 9 步的职责，第 10 步只负责
        深化，它的草稿如果因为 LLM 截断少返回几场，绝不能因此删掉作者的场。

        两条护栏：
        - ``kept_row_uids`` 为空（空草稿）时调用方不会进来，避免一次空 PATCH 清空全书。
        - 已经物化成 ``SceneCard`` 的场只打 ``orphaned_flag``，不软删 —— 那边可能已经
          有正文了，删不删要作者自己决定。
        """
        if plans is None:
            rows = self.session.execute(
                select(SnowflakeScenePlan).where(
                    SnowflakeScenePlan.project_id == project_id,
                    SnowflakeScenePlan.removed_at.is_(None),
                )
            ).scalars().all()
        else:
            # 调用方（``_sync_scene_plans``）已经读过本作品的全部计划、也带上了这次新建的：按同一条件在内存里筛
            rows = [plan for plan in plans if plan.project_id == project_id and plan.removed_at is None]
        leaving = [plan for plan in rows if (plan.row_uid or "") not in kept_row_uids]
        cards = self._scene_cards_by_id(project_id, [plan.scene_id for plan in leaving])
        removed_at = utcnow()
        for plan in leaving:
            materialized = cards.get(plan.scene_id)
            if materialized is not None:
                if plan.orphaned_flag:
                    continue
                plan.orphaned_flag = 1
                event_type = "snowflake_scene_plan_orphaned"
            else:
                plan.removed_at = removed_at
                plan.removed_by = "operator"
                event_type = "snowflake_scene_plan_removed"
            self.session.add(
                OperationLog(
                    event_type=event_type,
                    object_type="snowflake_scene_plan",
                    object_ref=plan.scene_plan_id,
                    payload_json={
                        "project_id": project_id,
                        "scene_id": plan.scene_id,
                        "row_uid": plan.row_uid or "",
                        "title": plan.title or plan.summary or "",
                        "removed_at": removed_at,
                    },
                )
            )

    def _scene_plan_by_scene_id(self, project_id: str, scene_id: str) -> SnowflakeScenePlan | None:
        return self.session.execute(
            select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id, SnowflakeScenePlan.scene_id == scene_id)
        ).scalars().first()

    def _scene_plan_for_triage_item(self, project_id: str, item: dict[str, Any]) -> SnowflakeScenePlan:
        scene_plan_id = str(item.get("scene_plan_id") or "").strip()
        scene = self.session.get(SnowflakeScenePlan, scene_plan_id) if scene_plan_id else None
        if scene is None:
            scene_id = str(item.get("scene_id") or "").strip()
            scene = self._scene_plan_by_scene_id(project_id, scene_id) if scene_id else None
        if scene is None or scene.project_id != project_id or scene.removed_at:
            raise DomainError("SNOWFLAKE_SCENE_PLAN_NOT_FOUND", "未找到该场景计划。", status_code=404)
        return scene

    def _attach_triage_identity(self, project_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                scene = self._scene_plan_for_triage_item(project_id, item)
            except DomainError:
                continue
            result.append(
                {
                    **item,
                    "triage_id": item.get("triage_id") or "",
                    "scene_plan_id": scene.scene_plan_id,
                    "scene_id": scene.scene_id,
                    # FE 场景规划以 row_uid 为键（fe_scaffold 的 s.id）——带上它前端才能对位
                    "row_uid": scene.row_uid or "",
                    "title": scene.title or scene.summary or scene.scene_id,
                    "primary_form": scene.scene_type,
                    "scene_type": scene.scene_type,
                    "repair_patch": sanitize_scene_patch(item.get("repair_patch") or {}),
                }
            )
        return result

    def _apply_scene_patch(self, scene: SnowflakeScenePlan, patch: dict[str, Any]) -> None:
        if "crucible" in patch and "scene_crucible" not in patch:
            patch["scene_crucible"] = patch["crucible"]
        for key, value in patch.items():
            if key == "crucible":
                continue
            if key == "primary_form":
                scene_type = str(value or "").strip().lower()
                scene.scene_type = scene_type if scene_type in {"proactive", "reactive"} else "proactive"
                continue
            if not hasattr(scene, key):
                continue
            if key in {"onstage_chars_json", "beats_json"}:
                setattr(scene, key, coerce_string_list(value))
            elif key == "scene_seq":
                setattr(scene, key, int_or_default(value, scene.scene_seq or 1))
            elif key == "scene_type":
                scene_type = str(value or "").strip().lower()
                setattr(scene, key, scene_type if scene_type in {"proactive", "reactive"} else "proactive")
            elif key == "rendering_mode":
                mode = str(value or "").strip().lower()
                scene.rendering_mode = mode if mode in RENDERING_MODES else "full"
            else:
                setattr(scene, key, str(value or "").strip())
        # 补丁键的顺序不可依赖（SCENE_PATCH_FIELDS 是集合）：类型定下来之后再统一收口——
        # 阶段 N：概述对两种形态都合法，略过只给反应场。
        scene.rendering_mode = effective_rendering_mode(scene.scene_type, scene.rendering_mode)

    def _mark_downstream_stale(self, run: SnowflakeStepRun, *, previous_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        # P0-3: a single dependency/diff-aware judgment replaces the old "stale every
        # later step" loop. Only steps whose approval snapshot of THIS step's consumed
        # fields actually changed are marked — revising a step no longer punishes
        # downstream work that did not depend on what changed.
        # 阶段 G：已经 stale 的行也参与判定——点过「已复核」的步骤在 accept-stale 时把消费快照刷到
        # 了当时的上游版本，上游再改一次它必须重新亮起来，否则「仍然有效」会永远有效。
        candidates = self.session.execute(
            select(SnowflakeStepRun).where(
                SnowflakeStepRun.project_id == run.project_id,
                SnowflakeStepRun.step_run_id != run.step_run_id,
                SnowflakeStepRun.status.in_(["pending_review", "approved", "skipped", "stale"]),
            )
        ).scalars().all()
        hits = recompute_stale(
            changed_step_key=run.step_key,
            current_field_sigs=field_sigs(run.draft_json or {}),
            candidate_rows=candidates,
            step_order=STEP_ORDER,
        )

        affected_step_run_ids: list[str] = []
        affected_scene_plan_ids: list[str] = []
        stale_step_keys: set[str] = set()
        reasons: dict[str, str] = {}
        for hit in hits:
            row = hit.row
            row.status = "stale"
            row.stale_reason = hit.reason
            row.stale_accepted_at = None
            row.stale_accepted_by = None
            row.stale_accepted_note = None
            affected_step_run_ids.append(row.step_run_id)
            stale_step_keys.add(row.step_key)
            reasons[row.step_key] = hit.reason

        # Scene plans are the materialized output of scene_list / scene_details, so they
        # only go stale when one of those steps is itself affected — not on every change
        # that happens to sit upstream of scene_list.
        if stale_step_keys & {"scene_list", "scene_details"}:
            reason = f"{run.step_key} 改动影响了场景列表，复核场景计划。"
            # 阶段 G：09 自己重新批准时按 row_uid 只标内容真的变了（或新加）的场；
            # 旧数据没有 row_uid、或改动来自更上游（无法定位到场）→ 仍然全标。
            changed_row_uids = (
                changed_scene_row_uids(previous_payload, run.draft_json) if run.step_key == "scene_list" else None
            )
            for scene in self._scene_plans(run.project_id):
                if changed_row_uids is not None and not ({scene.row_uid or "", scene.scene_id or ""} & changed_row_uids):
                    continue
                scene.status = "stale"
                scene.stale_reason = reason
                scene.stale_accepted_at = None
                scene.stale_accepted_by = None
                scene.stale_accepted_note = None
                affected_scene_plan_ids.append(scene.scene_plan_id)
            if affected_scene_plan_ids:
                reasons["scene_plans"] = reason

        if affected_step_run_ids or affected_scene_plan_ids:
            # 一次失效级联留一条操作日志（B06-14）：以前每个受影响的步骤 / 场景计划各写一行
            # snowflake_revision_links（外加一次去重查询），那张表从来没有读者，状态也从不离开 open。
            self.session.add(
                OperationLog(
                    event_type="snowflake_downstream_marked_stale",
                    object_type="snowflake_step_run",
                    object_ref=run.step_run_id,
                    payload_json={
                        "project_id": run.project_id,
                        "step_key": run.step_key,
                        "affected_step_run_ids": affected_step_run_ids,
                        "affected_step_keys": sorted(stale_step_keys),
                        "affected_scene_plan_ids": affected_scene_plan_ids,
                        "reasons": reasons,
                    },
                )
            )

        summary = (
            f"{run.step_key} 改动影响 {len(affected_step_run_ids)} 个下游步骤、"
            f"{len(affected_scene_plan_ids)} 个场景计划。"
            if affected_step_run_ids or affected_scene_plan_ids
            else "本次修改没有影响任何下游雪花产出。"
        )
        return {
            "step_key": run.step_key,
            "affected_count": len(affected_step_run_ids) + len(affected_scene_plan_ids),
            "affected_step_run_ids": affected_step_run_ids,
            "affected_scene_plan_ids": affected_scene_plan_ids,
            "summary": summary,
        }

    @staticmethod
    def _combine_approval_impact(
        step_key: str,
        downstream_impact: dict[str, Any],
        runtime_impact: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "step_key": step_key,
            "affected_count": int(downstream_impact.get("affected_count") or 0)
            + int(runtime_impact.get("affected_count") or 0),
            "downstream": downstream_impact,
            "runtime": runtime_impact,
            "summary": "; ".join(
                part
                for part in [
                    str(downstream_impact.get("summary") or "").strip(),
                    str(runtime_impact.get("summary") or "").strip(),
                ]
                if part
            ),
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


# 阶段 U：「先看 3 个方向」没带作者要求时，回合里的「我」这一行写这句
CANDIDATES_DEFAULT_ASK = "给我 3 个不同方向"
