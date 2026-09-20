from __future__ import annotations

import hashlib
import uuid
from copy import deepcopy
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
    SnowflakeRevisionLink,
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
from novel_system.services.snowflake_planner import (
    GATE_STATUSES,
    SnowflakePlannerService,
    _beats_from_detail,
    _scene_writer_brief,
)
from novel_system.services.snowflake_staleness import (
    changed_scene_row_uids,
    stable_json,
    field_sigs,
    recompute_stale,
    semantic_payload,
    snapshot_consumed_sigs,
)
from novel_system.services.snowflake_steps import (
    MATERIALIZATION_REQUIREMENTS,
    MATERIALIZATION_REQUIRED_STEPS,
    MATERIALIZATION_WARNING_STEPS,
    QUALITY_POLICY,
    RENDERING_MODES,
    SNOWFLAKE_METHOD_VERSION,
    STEP_ORDER,
    SUMMARY_LENGTH_BAND,
    default_step_draft,
    diagnose_step_pressure,
    diagnose_scene_detail,
    editor_payload,
    effective_rendering_mode,
    get_step_definition,
    list_step_definitions,
    merge_step_draft,
    step_completeness,
    step_guidance,
)
from novel_system.services.scene_rehome import rehome_scenes
from novel_system.services.snowflake_chaptering import (
    SnowflakeChapteringService,
    mint_chapter_row_uid,
    parse_outline_chapters,
)
from novel_system.services.snowflake_scene_order import positions_from_rows, renumber_scene_seq, sort_in_story_order
from novel_system.services.snowflake_triage import EXCLUDED_TRIAGE_STATUSES, excluded_scene_plan_ids, latest_triage_rows
from novel_system.services.snowflake_direction_brief import DirectionBriefStore, delta_changed
from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService, draft_has_content

STRUCTURED_GATE_STATUSES = set(GATE_STATUSES)
SCENE_PATCH_FIELDS = {
    # P1-1: scene_id / chapter_id are system-minted identity, never author-editable.
    # chapter_title / chapter_goal / chapter_role stay editable (content, not identity).
    "chapter_title",
    "chapter_goal",
    "chapter_role",
    # 灾一/灾二/灾三：作者标在场上的结构铰链，脊柱锚点分章要用（P2）。
    # 它是内容标注，不是身份，所以可编辑。
    "spine",
    "scene_seq",
    "pov_character_id",
    "onstage_chars_json",
    "title",
    "summary",
    "primary_form",
    "scene_type",
    "location",
    "scene_crucible",
    "crucible",
    "goal",
    "conflict",
    "setback",
    "reaction",
    "dilemma",
    "decision",
    "cost_requirement",
    "beats_json",
    "must_include_text",
    "exit_change",
    "hook",
    "target_length_band",
    # 阶段 C / N：呈现方式——summary 对两种形态都合法，skip 只给反应场（见 snowflake_steps.effective_rendering_mode）。
    "rendering_mode",
    # 阶段 J：原著的几栏——读者应感到什么、故事时间（在场人物已在上面）。
    "expected_reader_emotion",
    "story_time",
    # 阶段 N：作者的破例理由（原著：不过关也可以放行，但要知道理由）。
    "exception_reason",
}



def _effective_rendering_mode(scene_type: Any, value: Any) -> str:
    """呈现方式的收口规则——单一实现在 ``snowflake_steps.effective_rendering_mode``。"""
    return effective_rendering_mode(scene_type, value)


class SnowflakeWorkspaceService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._projects = ProjectService(session)
        self._planner = SnowflakePlannerService(session)
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
        from novel_system.services.writing_stats import WritingStatsService

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
            "workspace": self.workspace(project["project_id"]),
        }

    def workspace(self, project_id: str) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        latest_by_step = self._latest_by_step(project_id)
        current_step_key = self._current_step_key(latest_by_step)
        scene_plans = self._scene_plans(project_id)
        scene_board = self._scene_board(project_id, scene_plans=scene_plans)
        triage_items = self._triage_items(project_id)
        latest_plan = self._latest_plan(project_id)
        confirmed_steps = self._steps_with_confirmed_version(project_id)
        steps = [
            self._workspace_step(step, latest_by_step, project_id=project_id, confirmed_steps=confirmed_steps)
            for step in list_step_definitions()
        ]
        chapter_plan_status = self._chaptering.status(project_id, scene_plans)
        gate = self._materialization_gate(latest_by_step, triage_items, scene_plans, chapter_plan_status)
        return {
            "chapter_plan_status": chapter_plan_status,
            "project": project_payload(project),
            "method_version": SNOWFLAKE_METHOD_VERSION,
            "quality_policy": deepcopy(QUALITY_POLICY),
            "materialization_requirements": deepcopy(MATERIALIZATION_REQUIREMENTS),
            "current_step_key": current_step_key,
            "ready_to_materialize": gate["status"] != "blocked",
            "latest_plan": outline_plan_payload(latest_plan) if latest_plan is not None else None,
            "scene_board": scene_board,
            "triage_items": triage_items,
            "assistant_history": self._assistant_history(project_id),
            # 阶段 T：每步的作者意图要点（含已撤条目供恢复、继承的上游全书级条目）
            "direction_briefs": self._briefs.all_payloads(project_id),
            "materialization_gate": gate,
            "resync_status": self._resync_status(project.project_id, scene_plans),
            "steps": steps,
        }

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
            # draft_override：FE 带来的本地最新规范草稿（与上行 PATCH 同源），盖在
            # 存档之上作为生成底稿——消除「刚加的角色/场还没自动保存上行」的竞态。
            draft_override = self._merged_draft_override(latest_by_step, step_key, body.get("draft_override"))
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
                    "sha": hashlib.sha256(direction_text.encode("utf-8")).hexdigest()[:16],
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
            draft = llm_result.payload
            source = llm_result.source
            llm_call_id = llm_result.llm_call_id
            # 分批深化中途失败等「作者必须知道但不属于草稿」的事实随健康度落库
            generation_notice = llm_result.notice
            status = "pending_review"
            approved_at = None

        run = SnowflakeStepRun(
            step_run_id=f"snowflake_step_run_{project.project_id}_{step_key}_{uuid.uuid4().hex[:10]}",
            project_id=project.project_id,
            step_key=step_key,
            version=self._next_step_version(project.project_id, step_key),
            status=status,
            draft_json=draft,
            health_json=self._step_health(
                step_key,
                draft,
                status,
                generation_source=source,
                generation_notice=generation_notice,
                trigger_source=trigger_source,
                direction_brief=brief_ref,
                direction=direction_ref,
            ),
            input_refs_json=self._input_refs(step_key, latest_by_step),
            llm_call_id=llm_call_id,
            approved_at=approved_at,
        )
        self.session.add(run)
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
            self._supersede_same_step(run)
            self._mark_downstream_stale(run)
        self.session.flush()
        workspace = self.workspace(project.project_id)
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
            context_text=str(body.get("context") or "")[:6000],
            current_draft=str(body.get("draft") or "")[:3000],
            target_chars=target_chars,
            latest_by_step=latest_by_step,
            draft_override=self._merged_draft_override(latest_by_step, step_key, body.get("draft_override")),
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
            "turn": self._assistant_turn_payload(turn),
            "assistant_history": self._assistant_history(project.project_id),
        }

    def update_step(self, project_id: str, step_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        self._require_step(step_key)
        latest_by_step = self._latest_by_step(project.project_id)
        if self._draft_gate_mode(project) == "strict":
            self._require_previous_gates(step_key, latest_by_step)
        draft = merge_step_draft(step_key, body.get("draft") or {}, latest_by_step=latest_by_step)
        latest = latest_by_step.get(step_key)

        # 防静默回退：已批准/已跳过步骤收到无故事含义的 re-PATCH 时保持原状态与版本。
        # ``fe_*`` 是前端写穿缓存（其中 book_brief.fe_meta 会在确认任何后续步骤时变化）；
        # 若把它当故事修订，就会把第 1 步重新打回待审，并连锁 staling 全部下游。
        # 元数据仍原位写入，保证跨会话 UI 账本不丢；规范故事字段确有变化时才新建待审版本。
        same_semantic_draft = latest is not None and semantic_payload(draft) == semantic_payload(latest.draft_json)
        if latest is not None and latest.status in {"approved", "skipped"} and same_semantic_draft:
            if (draft or {}) != (latest.draft_json or {}):
                latest.draft_json = draft
                self.session.flush()
            workspace = self.workspace(project.project_id)
            return {
                "step": self._step_from_workspace(workspace, step_key),
                "workspace": workspace,
                "step_run": self._step_run_payload(latest),
            }

        # 2026-09-18 抹空保护：pending_review 步平时原位改写（不为每次键入造版本），但「整步抹空」不行——
        # 旧稿有成段的故事文字、新稿一个字都没有时另起一版，旧稿留在历史里可用 restore 取回。前端同步层
        # 已有水合闸门拦住「没水合过的空白默认稿」，这里是最后一道：原位改写没有历史，未确认的草稿一旦被
        # 空白覆盖就是永久丢失。作者亲手重置同样多留一版，可反悔。
        wipes_story = latest is not None and _would_wipe_story(latest.draft_json, draft)
        if latest is not None and latest.status == "pending_review" and not wipes_story:
            run = latest
            run.draft_json = draft
            run.input_refs_json = self._input_refs(step_key, latest_by_step)
            run.health_json = self._step_health(step_key, draft, "pending_review", generation_source="author")
            run.stale_reason = None
            run.stale_accepted_at = None
            run.stale_accepted_by = None
            run.stale_accepted_note = None
        else:
            run = SnowflakeStepRun(
                step_run_id=f"snowflake_step_run_{project.project_id}_{step_key}_{uuid.uuid4().hex[:10]}",
                project_id=project.project_id,
                step_key=step_key,
                version=self._next_step_version(project.project_id, step_key),
                status="pending_review",
                draft_json=draft,
                health_json=self._step_health(step_key, draft, "pending_review", generation_source="author"),
                input_refs_json=self._input_refs(step_key, latest_by_step),
            )
            self.session.add(run)
            if wipes_story and latest is not None and latest.status == "pending_review":
                self.session.add(
                    OperationLog(
                        event_type="snowflake_step_wipe_preserved",
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
        workspace = self.workspace(project.project_id)
        return {"step": self._step_from_workspace(workspace, step_key), "workspace": workspace, "step_run": self._step_run_payload(run)}

    def step_history(self, project_id: str, step_key: str, *, include_draft: bool = False) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        self._require_step(step_key)
        rows = self.session.execute(
            select(SnowflakeStepRun)
            .where(SnowflakeStepRun.project_id == project.project_id, SnowflakeStepRun.step_key == step_key)
            .order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.updated_at.desc(), SnowflakeStepRun.created_at.desc())
        ).scalars().all()
        return {
            "project_id": project.project_id,
            "step_key": step_key,
            "items": [self._step_run_history_payload(row, include_draft=include_draft) for row in rows],
        }

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
        draft = deepcopy(source_run.draft_json or {})
        refs = self._input_refs(step_key, latest_by_step)
        refs["restored_from_step_run_id"] = source_run.step_run_id
        run = SnowflakeStepRun(
            step_run_id=f"snowflake_step_run_{project.project_id}_{step_key}_{uuid.uuid4().hex[:10]}",
            project_id=project.project_id,
            step_key=step_key,
            version=self._next_step_version(project.project_id, step_key),
            status="pending_review",
            draft_json=draft,
            health_json=self._step_health(step_key, draft, "pending_review", generation_source="history_restore"),
            input_refs_json=refs,
        )
        self.session.add(run)
        self.session.flush()
        self._sync_structured_step_data(project, step_key, draft, run)
        self.session.flush()
        workspace = self.workspace(project.project_id)
        step_run = self._step_run_payload(run) or {}
        step_run["restored_from_step_run_id"] = source_run.step_run_id
        return {
            "step": self._step_from_workspace(workspace, step_key),
            "workspace": workspace,
            "step_run": step_run,
            "restored_from": self._step_run_history_payload(source_run, include_draft=False),
        }

    def import_discovery_steps(
        self,
        project_id: str,
        step_drafts: dict[str, Any],
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        del actor_ref
        project = self._require_snowflake_project(project_id)
        allowed_order = [
            "book_brief",
            "one_sentence_summary",
            "one_paragraph_summary",
            "character_sheets",
            "character_synopses",
            "scene_list",
            "scene_details",
        ]
        latest_by_step = self._latest_by_step(project.project_id)
        imported_runs: list[SnowflakeStepRun] = []
        for step_key in allowed_order:
            raw_draft = step_drafts.get(step_key)
            if not isinstance(raw_draft, dict):
                continue
            self._require_step(step_key)
            latest = latest_by_step.get(step_key)
            if step_key in {"character_sheets", "character_synopses", "character_bibles"}:
                raw_draft = self._merge_character_import_draft(latest.draft_json if latest else {}, raw_draft)
            draft = merge_step_draft(step_key, raw_draft, latest_by_step=latest_by_step)
            if latest is not None and latest.status == "pending_review":
                run = latest
                run.draft_json = draft
                run.input_refs_json = self._input_refs(step_key, latest_by_step)
                run.health_json = self._step_health(step_key, draft, "pending_review", generation_source="author_discovery")
                run.stale_reason = None
                run.stale_accepted_at = None
                run.stale_accepted_by = None
                run.stale_accepted_note = None
            else:
                run = SnowflakeStepRun(
                    step_run_id=f"snowflake_step_run_{project.project_id}_{step_key}_{uuid.uuid4().hex[:10]}",
                    project_id=project.project_id,
                    step_key=step_key,
                    version=self._next_step_version(project.project_id, step_key),
                    status="pending_review",
                    draft_json=draft,
                    health_json=self._step_health(step_key, draft, "pending_review", generation_source="author_discovery"),
                    input_refs_json=self._input_refs(step_key, latest_by_step),
                )
                self.session.add(run)
            self.session.flush()
            self._sync_structured_step_data(project, step_key, draft, run)
            latest_by_step[step_key] = run
            imported_runs.append(run)
        self.session.flush()
        workspace = self.workspace(project.project_id)
        return {
            "imported_step_keys": [run.step_key for run in imported_runs],
            "step_runs": [self._step_run_payload(run) for run in imported_runs],
            "workspace": workspace,
        }

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
            workspace = self.workspace(project.project_id)
            result = {"step": self._step_from_workspace(workspace, step_key), "workspace": workspace}
            if catalog_sync is not None:
                result["catalog_sync"] = catalog_sync
            return result
        if run.status != "pending_review":
            raise DomainError("SNOWFLAKE_STEP_RUN_NOT_APPROVABLE", "这一步骤当前状态不能被确认。", status_code=409)

        self._require_previous_gates(step_key, latest_by_step, allow_self=run.step_run_id)
        previous_run = self._latest_approved_step_run(project.project_id, step_key, exclude_step_run_id=run.step_run_id)
        self._supersede_same_step(run)
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
        workspace = self.workspace(project.project_id)
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
        eligible: list[str] = []
        held: list[dict[str, Any]] = []
        for plan in plans:
            scene = self.session.get(SceneCard, plan.scene_id)
            if scene is None or scene.project_id != project_id:
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
        workspace = self.workspace(project.project_id)
        return {"step": self._step_from_workspace(workspace, step_key), "workspace": workspace}

    def request_assistant(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        latest_by_step = self._latest_by_step(project.project_id)
        workspace = self.workspace(project.project_id)
        step_key = str(body.get("step_key") or workspace.get("current_step_key") or "book_brief").strip() or "book_brief"
        step = self._step_from_workspace(workspace, step_key)
        step = self._step_with_override(step, body.get("draft_override"), latest_by_step=latest_by_step)
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
        history = self._assistant_history(project.project_id)
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
        workspace = self.workspace(project.project_id)
        step = self._step_from_workspace(workspace, "scene_details")
        if not step.get("draft", {}).get("scenes"):
            raise DomainError("SNOWFLAKE_SCENE_DETAILS_REQUIRED", "需要先完成场景规划（场景细化）。", status_code=409)
        step = self._step_with_override(step, body.get("draft_override"), latest_by_step=self._latest_by_step(project.project_id))
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
            diagnosis = diagnose_scene_detail(_scene_plan_payload(scene))
            manual_status = _coerce_triage_status(item.get("status") or item.get("manual_status"))
            recommended_status = _coerce_triage_status(item.get("recommended_status")) or diagnosis["recommended_status"]
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
            row.score = _coerce_int(item.get("score"), diagnosis["score"])
            row.missing_fields_json = _coerce_string_list(item.get("missing_fields")) or diagnosis["missing_fields"]
            row.fix_steps_json = _coerce_string_list(item.get("fix_steps")) or diagnosis["fix_steps"]
            row.repair_patch_json = _sanitize_scene_patch(item.get("repair_patch") or {})
            row.pressure_flags_json = _coerce_string_list(item.get("pressure_flags")) or diagnosis["pressure_flags"]
            row.notes = str(item.get("notes") or "").strip()
            # blocking = 这一场被排除在物化之外（该重写 / 待删）；阶段 N 起它不再阻断全书。
            row.blocking = 1 if effective_status in EXCLUDED_TRIAGE_STATUSES else 0
            row.manual_override = 1 if manual_status and manual_status != recommended_status else 0
            row.llm_call_id = str(item.get("llm_call_id") or "").strip() or row.llm_call_id
        self.session.flush()
        workspace = self.workspace(project.project_id)
        return {"items": workspace["triage_items"], "workspace": workspace}

    def apply_scene_triage_repair(self, project_id: str, triage_id: str) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        row = self.session.get(SnowflakeSceneTriageItem, triage_id)
        if row is None or row.project_id != project.project_id:
            raise DomainError("SNOWFLAKE_TRIAGE_NOT_FOUND", "未找到该场景急救记录。", status_code=404)
        scene = self.session.get(SnowflakeScenePlan, row.scene_plan_id)
        if scene is None or scene.project_id != project.project_id or scene.removed_at:
            raise DomainError("SNOWFLAKE_SCENE_PLAN_NOT_FOUND", "未找到该场景计划。", status_code=404)
        patch = _sanitize_scene_patch(row.repair_patch_json or {})
        if not patch:
            raise DomainError("SNOWFLAKE_TRIAGE_REPAIR_EMPTY", "该急救记录没有可应用的修复补丁。", status_code=409)
        self._apply_scene_patch(scene, patch)
        scene.diagnosis_json = diagnose_scene_detail(_scene_plan_payload(scene))
        row.recommended_status = scene.diagnosis_json["recommended_status"]
        row.score = scene.diagnosis_json["score"]
        row.missing_fields_json = scene.diagnosis_json["missing_fields"]
        row.fix_steps_json = scene.diagnosis_json["fix_steps"]
        row.pressure_flags_json = scene.diagnosis_json["pressure_flags"]
        row.effective_status = row.manual_status or row.recommended_status
        row.blocking = 1 if row.effective_status in EXCLUDED_TRIAGE_STATUSES else 0
        row.manual_override = 1 if row.manual_status and row.manual_status != row.recommended_status else 0
        self.session.flush()
        return {"triage": self._triage_payload(row), "scene": _scene_plan_payload(scene), "workspace": self.workspace(project.project_id)}

    def _latest_triage_row(self, project_id: str, scene_plan_id: str) -> SnowflakeSceneTriageItem | None:
        return latest_triage_rows(self.session, project_id).get(scene_plan_id)

    def _excluded_scene_plan_ids(self, project_id: str) -> set[str]:
        """作者裁定为该重写 / 待删的场景计划（阶段 N）：不物化、回流进回收站、节奏按 0 计。
        每一场只看最新的一条分诊记录（历史数据可能一场多行）。"""
        return excluded_scene_plan_ids(self.session, project_id)

    def accept_stale_scenes(self, project_id: str, payload: dict[str, Any] | None = None, *, actor_ref: str = "operator") -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        requested_ids = [str(item or "").strip() for item in body.get("scene_plan_ids") or [] if str(item or "").strip()]
        scenes = self._scene_plans(project.project_id)
        if requested_ids:
            requested = set(requested_ids)
            scenes = [scene for scene in scenes if scene.scene_plan_id in requested]
        if not scenes:
            raise DomainError("SNOWFLAKE_STALE_SCENES_NOT_FOUND", "未找到匹配的场景计划。", status_code=404)
        note = str(body.get("note") or "").strip() or None
        accepted_at = utcnow()
        accepted: list[dict[str, Any]] = []
        for scene in scenes:
            if scene.status != "stale":
                continue
            scene.stale_accepted_at = accepted_at
            scene.stale_accepted_by = actor_ref or "operator"
            scene.stale_accepted_note = note
            accepted.append(_scene_plan_payload(scene))
            self.session.add(
                OperationLog(
                    event_type="snowflake_scene_stale_accepted",
                    object_type="snowflake_scene_plan",
                    object_ref=scene.scene_plan_id,
                    payload_json={
                        "project_id": project.project_id,
                        "scene_id": scene.scene_id,
                        "scene_plan_id": scene.scene_plan_id,
                        "accepted_at": accepted_at,
                        "accepted_by": actor_ref or "operator",
                        "note": note or "",
                        "stale_reason": scene.stale_reason or "",
                    },
                )
            )
        if not accepted:
            raise DomainError("SNOWFLAKE_SCENES_NOT_STALE", "未找到匹配的、处于过期状态的场景计划。", status_code=409)
        self.session.flush()
        return {"accepted_scenes": accepted, "workspace": self.workspace(project.project_id)}

    def update_scene_plan(self, project_id: str, scene_plan_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        scene = self.session.get(SnowflakeScenePlan, scene_plan_id)
        if scene is None or scene.project_id != project.project_id or scene.removed_at:
            raise DomainError("SNOWFLAKE_SCENE_PLAN_NOT_FOUND", "未找到该场景计划。", status_code=404)
        self._apply_scene_patch(scene, _sanitize_scene_patch(payload or {}))
        scene.status = "draft" if scene.status in {"approved", "stale"} else scene.status
        scene.stale_accepted_at = None
        scene.stale_accepted_by = None
        scene.stale_accepted_note = None
        scene.diagnosis_json = diagnose_scene_detail(_scene_plan_payload(scene))
        self.session.flush()
        return {"scene": _scene_plan_payload(scene), "workspace": self.workspace(project.project_id)}

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
        workspace = self.workspace(project.project_id)
        gate = workspace.get("materialization_gate") or {}
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
        return {"plan": outline_plan_payload(plan), "workspace": self.workspace(project.project_id)}

    def _build_chaptered_outline_plan(
        self,
        project: StoryProject,
        scene_plans: list[SnowflakeScenePlan],
    ) -> dict[str, Any]:
        """按构思侧章表分组产出 OutlinePlan（P2）。

        与被它取代的 ``snowflake_planner._build_outline_plan`` 的关键差别：章不再从
        场景行的 ``chapter_id`` 反推（那条路上所有场都写着 ``…_CH01``，于是全书一章，
        章标题就是章 id 字符串），而是读作者在 07 里真正编出来的章 —— 标题、幕、脊柱、
        章目标都来自那里。v1 路由 ``/snowflake/materialize-outline-plan`` 仍走旧
        builder，本方法不改它。
        """
        chapters = self._chaptering.ensure_chapter_plans(project.project_id)
        # 阶段 B（雪花评估 B5）：挫折 / 胜利以主角衡量，不以 POV 衡量——Ingermanson：POV 是对手时，
        # 对手得手就是主角的挫折。把角色摘要表里的主角带进每一场的简报，结构简报会渲染它。
        protagonist = self._protagonist_hint(project.project_id)
        # 阶段 N：作者裁定该重写 / 待删的场不物化（三拍留在构思里，回流时再补建或取回）。
        excluded = self._excluded_scene_plan_ids(project.project_id)
        by_plan_id = {chapter.chapter_plan_id: chapter for chapter in chapters}
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
                if _effective_rendering_mode(item.scene_type, item.rendering_mode) != "skip"
            ]
            if not members:
                continue  # 空章不落库：预览里已经就此告警过，作者选择保留就是不要它
            # 阶段 Y：目录里的章 id 钉在章计划行上（不再按章序算）——这一章以前物化过，就还是目录里的那一行
            chapter_id = self._chaptering.catalog_chapter_id(chapter)
            goal = (chapter.chapter_goal or chapter.summary or "").strip() or f"推进本章：{chapter.title or chapter_id}"
            scenes_payload: list[dict[str, Any]] = []
            for seq, scene in enumerate(members, start=1):
                detail = _scene_plan_payload(scene)
                if protagonist is not None:
                    detail["protagonist_hint"] = protagonist["display_name"]
                    detail["protagonist_character_id"] = protagonist["character_id"]
                scene_type = detail.get("primary_form") or "proactive"
                # 阶段 C / N：summary 场（两种形态都可以）拿到数值篇幅带（起草 / 长度补丁按数值硬约束），
                # 并把呈现方式写进简报，结构简报会渲染它。
                rendering_mode = _effective_rendering_mode(scene_type, detail.get("rendering_mode"))
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
                        "beats_json": _scene_card_beats(scene_type, detail),
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
                            **_scene_writer_brief(scene_type, detail),
                            # 阶段 X：构思里起过的短题名随卡进目录（整句摘要不算题名）
                            **_scene_title_seed(detail),
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
            if _is_protagonist_role(row.role):
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
        workspace = self.workspace(project.project_id)
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
        plans = self._scene_plans(project.project_id)
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
        order_drift = self._scene_card_order_drift(project.project_id)
        # 第一遍：只算补丁（不动库）。场景卡的位置 = 章 + 章内序，两样都受唯一索引
        # (chapter_id, scene_seq) 约束；一张一张地改会撞上还没搬走的那一张，所以位置留到最后
        # 由 _settle_scene_card_order 两阶段统一落位，补丁里不再带 scene_seq。
        prepared: list[tuple[SnowflakeScenePlan, SceneCard | None, dict[str, Any], dict[str, Any] | None]] = []
        for plan in plans:
            scene = self.session.get(SceneCard, plan.scene_id)
            if scene is None or scene.project_id != project.project_id:
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
            result["workspace"] = self.workspace(project.project_id)
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
        step: dict[str, Any],
        latest_by_step: dict[str, SnowflakeStepRun],
        *,
        project_id: str,
        confirmed_steps: set[str] | None = None,
    ) -> dict[str, Any]:
        run = latest_by_step.get(step["step_key"])
        draft = self._draft_for_step(step["step_key"], run, latest_by_step, project_id=project_id)
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
            "artifact": self._step_run_payload(run),
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
    ) -> dict[str, Any]:
        if step_key == "scene_list":
            scenes = [_scene_list_payload(scene) for scene in self._scene_plans(project_id)]
            return {"scenes": scenes} if scenes else merge_step_draft(step_key, run.draft_json if run else None, latest_by_step=latest_by_step)
        if step_key == "scene_details":
            scenes = [_scene_plan_payload(scene) for scene in self._scene_plans(project_id)]
            return {"scenes": scenes} if scenes else merge_step_draft(step_key, run.draft_json if run else None, latest_by_step=latest_by_step)
        return merge_step_draft(step_key, run.draft_json if run else None, latest_by_step=latest_by_step)

    @staticmethod
    def _gate_satisfied(step_key: str, latest_by_step: dict[str, SnowflakeStepRun]) -> bool:
        run = latest_by_step.get(step_key)
        if run is None:
            return False
        if run.status in STRUCTURED_GATE_STATUSES:
            return True
        return run.status == "stale" and bool(run.stale_accepted_at)

    def _current_step_key(self, latest_by_step: dict[str, SnowflakeStepRun]) -> str | None:
        for step in list_step_definitions():
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
        for step in list_step_definitions()[:step_index]:
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
        for step in list_step_definitions()[: STEP_ORDER[step_key]]:
            if not self._gate_satisfied(step["step_key"], latest_by_step):
                blockers.append({"step_key": step["step_key"], "label": step["label"]})
        return blockers

    def _latest_by_step(self, project_id: str) -> dict[str, SnowflakeStepRun]:
        rows = self.session.execute(
            select(SnowflakeStepRun)
            .where(SnowflakeStepRun.project_id == project_id)
            .order_by(SnowflakeStepRun.version.asc(), SnowflakeStepRun.created_at.asc())
        ).scalars().all()
        latest: dict[str, SnowflakeStepRun] = {}
        for row in rows:
            if row.status == "superseded":
                continue
            latest[row.step_key] = row
        return latest

    def _input_refs(self, step_key: str, latest_by_step: dict[str, SnowflakeStepRun]) -> dict[str, Any]:
        step_index = STEP_ORDER[step_key]
        refs: dict[str, Any] = {}
        for step in list_step_definitions()[:step_index]:
            run = latest_by_step.get(step["step_key"])
            if run is not None and self._gate_satisfied(step["step_key"], latest_by_step):
                refs[step["step_key"]] = run.step_run_id
        return refs

    def _next_step_version(self, project_id: str, step_key: str) -> int:
        latest = self.session.execute(
            select(SnowflakeStepRun.version)
            .where(SnowflakeStepRun.project_id == project_id, SnowflakeStepRun.step_key == step_key)
            .order_by(SnowflakeStepRun.version.desc())
        ).scalar()
        return int(latest or 0) + 1

    def _latest_approved_step_run(
        self,
        project_id: str,
        step_key: str,
        *,
        exclude_step_run_id: str,
    ) -> SnowflakeStepRun | None:
        return self.session.execute(
            select(SnowflakeStepRun)
            .where(
                SnowflakeStepRun.project_id == project_id,
                SnowflakeStepRun.step_key == step_key,
                SnowflakeStepRun.step_run_id != exclude_step_run_id,
                SnowflakeStepRun.status.in_(["approved", "skipped"]),
            )
            .order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.created_at.desc())
        ).scalars().first()

    def _next_plan_version(self, project_id: str) -> int:
        latest = self.session.execute(
            select(OutlinePlan.version)
            .where(OutlinePlan.project_id == project_id)
            .order_by(OutlinePlan.version.desc())
        ).scalar()
        return int(latest or 0) + 1

    def _latest_plan(self, project_id: str) -> OutlinePlan | None:
        return self.session.execute(
            select(OutlinePlan)
            .where(OutlinePlan.project_id == project_id)
            .order_by(OutlinePlan.version.desc(), OutlinePlan.created_at.desc())
        ).scalars().first()

    def _scene_plans(self, project_id: str) -> list[SnowflakeScenePlan]:
        """活跃场景计划，按**故事序**（09 场景列表的行序，见 ``snowflake_scene_order``）。

        软删的场（P1-3）在这里就被挡掉——工作台、诊断、闸门、回流状态和物化输入全部经过这一个
        入口，所以幽灵场不会再从任何一处冒出来。顺序同理只有这一个口径：09 / 10 两步交给前端的
        草稿就是按它排的，曾经的 ``(chapter_id, scene_seq)`` 在分章之后会把场景表按章重新洗一遍，
        新浏览器水合到的 09 于是不是作者排的那张表，下一次保存再把乱序写回去。
        """
        rows = self.session.execute(
            select(SnowflakeScenePlan).where(
                SnowflakeScenePlan.project_id == project_id,
                SnowflakeScenePlan.removed_at.is_(None),
            )
        ).scalars().all()
        return sort_in_story_order(self.session, project_id, rows)

    def _scene_board(self, project_id: str, *, scene_plans: list[SnowflakeScenePlan] | None = None) -> dict[str, Any]:
        scenes = [_scene_plan_payload(scene) for scene in (scene_plans if scene_plans is not None else self._scene_plans(project_id))]
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

    def _resync_status(self, project_id: str, scene_plans: list[SnowflakeScenePlan]) -> dict[str, Any]:
        pending: list[dict[str, Any]] = []
        excluded = self._excluded_scene_plan_ids(project_id)
        order_drift = self._scene_card_order_drift(project_id, scene_plans)
        for plan in scene_plans:
            scene = self.session.get(SceneCard, plan.scene_id)
            if scene is None or scene.project_id != project_id:
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
        rendering_mode = _effective_rendering_mode(plan.scene_type, plan.rendering_mode)
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
            **_followed_scene_title(previous_brief, _real_scene_title(plan.title, plan.summary)),
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
        # 与物化同一配方（_scene_card_beats）：两个写入方各算一套，刚物化完的每一场
        # 都会因 beats_json 不同被报成「待同步」，横幅在物化当刻就喊 N 场。
        detail = _scene_plan_payload(plan)
        beats = _scene_card_beats(str(detail.get("scene_type") or "proactive"), detail)
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

    def _triage_items(self, project_id: str) -> list[dict[str, Any]]:
        stored = {
            row.scene_plan_id: row
            for row in self.session.execute(
                select(SnowflakeSceneTriageItem).where(SnowflakeSceneTriageItem.project_id == project_id)
            ).scalars().all()
        }
        items: list[dict[str, Any]] = []
        for scene in self._scene_plans(project_id):
            row = stored.get(scene.scene_plan_id)
            if row is not None:
                items.append(self._triage_payload(row))
                continue
            diagnosis = diagnose_scene_detail(_scene_plan_payload(scene))
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
            suggestions_json=_coerce_string_list(result.get("suggestions")),
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

    @staticmethod
    def _materialization_gate(
        latest_by_step: dict[str, SnowflakeStepRun],
        triage_items: list[dict[str, Any]],
        scene_plans: list[SnowflakeScenePlan] | None = None,
        chapter_plan_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        warnings: list[str] = []
        items: list[dict[str, Any]] = []

        def add_step_item(*, severity: str, kind: str, message: str, step_key: str) -> None:
            if severity == "blocker":
                blockers.append(message)
            else:
                warnings.append(message)
            items.append(
                {
                    "id": f"{severity}:{kind}:{step_key}",
                    "severity": severity,
                    "kind": kind,
                    "message": message,
                    "step_key": step_key,
                    "scene_id": None,
                    "scene_plan_id": None,
                    "target_view": "snowflake-workbench",
                    "primary_action": {
                        "type": "jump_to_step",
                        "label": "去补这一步" if severity == "blocker" else "查看这一步",
                        "step_key": step_key,
                    },
                    "assistant_action": {
                        "type": "draft_with_assistant",
                        "label": "让助手起草",
                        "step_key": step_key,
                    },
                }
            )

        def add_scene_item(*, severity: str, kind: str, message: str, item: dict[str, Any]) -> None:
            if severity == "blocker":
                blockers.append(message)
            else:
                warnings.append(message)
            scene_id = str(item.get("scene_id") or "").strip()
            scene_plan_id = str(item.get("scene_plan_id") or "").strip()
            items.append(
                {
                    "id": f"{severity}:{kind}:{scene_plan_id or scene_id or len(items)}",
                    "severity": severity,
                    "kind": kind,
                    "message": message,
                    "step_key": "scene_details",
                    "scene_id": scene_id or None,
                    "scene_plan_id": scene_plan_id or None,
                    "target_view": "snowflake-workbench",
                    "primary_action": {
                        "type": "open_triage",
                        "label": "去修这个场景" if severity == "blocker" else "查看这个提醒",
                        "panel": "triage",
                        "scene_id": scene_id or None,
                        "scene_plan_id": scene_plan_id or None,
                    },
                    "assistant_action": {
                        "type": "draft_with_assistant",
                        "label": "让助手起草修法",
                        "scene_id": scene_id or None,
                        "scene_plan_id": scene_plan_id or None,
                    },
                }
            )

        for step_key in MATERIALIZATION_REQUIRED_STEPS:
            run = latest_by_step.get(step_key)
            step_label = _step_display_label(step_key)
            if run is None:
                add_step_item(
                    severity="blocker",
                    kind="missing_required_step",
                    message=f"{step_label} 是整理章节结构前必需步骤。",
                    step_key=step_key,
                )
                continue
            if run.status == "skipped":
                add_step_item(
                    severity="warning",
                    kind="skipped_required_step",
                    message=f"{step_label} 已跳过；可以继续整理，但建议在生成章节前复核这一层是否仍需要补齐。",
                    step_key=step_key,
                )
                continue
            if run.status == "stale" and run.stale_accepted_at:
                add_step_item(
                    severity="warning",
                    kind="accepted_stale_required_step",
                    message=f"{step_label} 曾被标记为过期，但当前草稿已经复核并确认仍然有效。",
                    step_key=step_key,
                )
            if run.status not in STRUCTURED_GATE_STATUSES and not (run.status == "stale" and run.stale_accepted_at):
                add_step_item(
                    severity="blocker",
                    kind="unapproved_required_step",
                    message=f"{step_label} 需要先确认，才能整理章节结构。",
                    step_key=step_key,
                )
                continue
            health = run.health_json or {}
            if step_key != "scene_details" and str(health.get("status") or health.get("pressure_status") or "").strip().lower() == "rewrite":
                add_step_item(
                    severity="blocker",
                    kind="rewrite_step_health",
                    message=f"{step_label} 存在废除重写级质量阻断。",
                    step_key=step_key,
                )

        for step_key in MATERIALIZATION_WARNING_STEPS:
            run = latest_by_step.get(step_key)
            step_label = _step_display_label(step_key)
            if run is None:
                add_step_item(
                    severity="warning",
                    kind="missing_optional_step",
                    message=f"{step_label} 尚未完成；可以继续整理，但角色或长篇细化风险会保留。",
                    step_key=step_key,
                )
                continue
            if run.status == "skipped":
                reason = str((run.draft_json or {}).get("skip_reason") or "").strip()
                suffix = f"：{reason}" if reason else "。"
                add_step_item(
                    severity="warning",
                    kind="skipped_optional_step",
                    message=f"{step_label} 已跳过{suffix}",
                    step_key=step_key,
                )
                continue
            if run.status == "stale" and run.stale_accepted_at:
                add_step_item(
                    severity="warning",
                    kind="accepted_stale_optional_step",
                    message=f"{step_label} 曾被标记为过期，但当前草稿已经复核并确认仍然有效。",
                    step_key=step_key,
                )
                continue
            if run.status not in STRUCTURED_GATE_STATUSES:
                add_step_item(
                    severity="warning",
                    kind="unapproved_optional_step",
                    message=f"{step_label} 尚未确认；可以继续整理，但后续可能需要回修。",
                    step_key=step_key,
                )
        # Q3 修复：scene_details 已确认但没有任何可整理的场景计划行时，materialize 会 409
        # SNOWFLAKE_SCENES_REQUIRED。此前 gate 只检查 scene_plans 的 stale 态、从不检查「空」，
        # 导致 ready_to_materialize=True 却点不动（信号说谎）。这里补一条 blocker，让 gate 与
        # materialize 的硬要求一致：仅在 scene_details 满足、却零场景计划时触发（不影响已有 plans
        # 的 happy path，也不与「scene_details 未确认」的既有 blocker 重复）。
        scene_details_run = latest_by_step.get("scene_details")
        scene_details_ready = scene_details_run is not None and (
            scene_details_run.status in STRUCTURED_GATE_STATUSES
            or (scene_details_run.status == "stale" and scene_details_run.stale_accepted_at)
        )
        if scene_details_ready and not (scene_plans or []):
            add_step_item(
                severity="blocker",
                kind="missing_scene_plans",
                message="场景细化已确认，但还没有可整理的场景计划；请先在「场景清单 / 场景细化」生成并确认场景，才能整理章节结构。",
                step_key="scene_details",
            )

        for scene in scene_plans or []:
            if scene.status != "stale":
                continue
            scene_label = str(scene.title or scene.summary or scene.scene_id or "scene").strip()
            item = _scene_plan_payload(scene)
            if scene.stale_accepted_at:
                add_scene_item(
                    severity="warning",
                    kind="accepted_stale_scene_plan",
                    message=f"{scene_label} 曾被标记为过期，但当前场景计划已经复核并确认仍然有效。",
                    item=item,
                )
                continue
            add_scene_item(
                severity="blocker",
                kind="stale_scene_plan",
                message=f"{scene_label} 需要先复核，才能整理为章节结构。",
                item=item,
            )

        for item in triage_items:
            scene_label = str(item.get("title") or item.get("scene_id") or "scene").strip()
            scene_id = str(item.get("scene_id") or "").strip()
            status = str(item.get("effective_status") or item.get("status") or "").strip().lower()
            recommended_status = str(item.get("recommended_status") or "").strip().lower()
            triage_source = str(item.get("triage_source") or "").strip().lower()
            scene_display = f"「{scene_label}」"
            if scene_id and scene_id != scene_label:
                scene_display = f"「{scene_label}」（{scene_id}）"
            if triage_source == "auto_diagnosis" and status == "unreviewed":
                # 阶段 H：规则层的「重写」只是缺失 / 占位的机械判断，不能挡物化——作者与 LLM 分诊拍板。
                if recommended_status == "rewrite":
                    add_scene_item(
                        severity="warning",
                        kind="triage_unreviewed_rewrite",
                        message=f"{scene_display} 三拍或坩埚还缺着，系统建议重写；整理前请先确认急救判断。",
                        item=item,
                    )
                elif recommended_status == "maybe":
                    add_scene_item(
                        severity="warning",
                        kind="triage_unreviewed_maybe",
                        message=f"{scene_display} 系统建议复核修改，整理前请先确认急救判断。",
                        item=item,
                    )
                continue
            if status == "rewrite":
                # 阶段 N：该重写不再阻断全书——这一场不建卡，重建并重新分诊后经回流补建。
                add_scene_item(
                    severity="warning",
                    kind="triage_rewrite",
                    message=f"{scene_display} 被标为该重写：整理时不建它的场景卡，重建并重新分诊后经回流补建。",
                    item=item,
                )
            elif status == "cut":
                add_scene_item(
                    severity="warning",
                    kind="triage_cut",
                    message=f"{scene_display} 已标待删：整理时不建它的场景卡，三拍留在构思里；确定不要时在 09 删除，改主意时改回裁定。",
                    item=item,
                )
            elif status == "maybe":
                add_scene_item(
                    severity="warning",
                    kind="triage_maybe",
                    message=f"{scene_display} 仍需修改；允许整理，但章节生成风险较高。",
                    item=item,
                )
            if item.get("manual_override") and recommended_status == "rewrite" and status != "rewrite":
                add_scene_item(
                    severity="warning",
                    kind="triage_manual_override",
                    message=f"{scene_display} 人工覆盖了自动废除重写诊断；整理前请复核急救备注。",
                    item=item,
                )

        # 阶段 N：作者把每一场都裁成该重写 / 待删（或略过）时，物化没有东西可建——挡在前面说清楚。
        excluded_plan_ids = {
            str(item.get("scene_plan_id") or "")
            for item in triage_items
            if str(item.get("effective_status") or item.get("status") or "").strip().lower() in EXCLUDED_TRIAGE_STATUSES
        }
        materializable = [
            scene
            for scene in scene_plans or []
            if scene.scene_plan_id not in excluded_plan_ids
            and effective_rendering_mode(scene.scene_type, scene.rendering_mode) != "skip"
        ]
        if scene_plans and not materializable:
            add_step_item(
                severity="blocker",
                kind="no_materializable_scene",
                message="每一场都被裁成该重写 / 待删或略过，没有可整理的场景；先把至少一场改回可用的裁定。",
                step_key="scene_details",
            )

        # P2 分章闸门：章归属没定就物化，等于回到「全书落进一章」的老路。
        # 这一条把它挡在前面，并把作者送进分章面板而不是让他对着结果发懵。
        status_payload = chapter_plan_status or {}
        unassigned_count = int(status_payload.get("unassigned_scene_count") or 0)
        chapter_count = int(status_payload.get("chapter_count") or 0)
        if scene_plans and (not chapter_count or unassigned_count):
            message = (
                "还没有分章：章节结构要先决定每一场归哪一章。"
                if not chapter_count
                else f"还有 {unassigned_count} 场没有分到章，整理后它们不会进入章节目录。"
            )
            blockers.append(message)
            items.append(
                {
                    "id": "blocker:chapter_plan_required:project",
                    "severity": "blocker",
                    "kind": "chapter_plan_required",
                    "message": message,
                    "step_key": "long_synopsis" if not chapter_count else "scene_list",
                    "scene_id": None,
                    "scene_plan_id": None,
                    "target_view": "snowflake-workbench",
                    "primary_action": {
                        "type": "open_chapter_plan",
                        "label": "去分章",
                        "panel": "chapter_plan",
                    },
                    "assistant_action": None,
                }
            )

        return {
            "status": "blocked" if blockers else "warning" if warnings else "ready",
            "blockers": blockers,
            "warnings": warnings,
            "items": items,
        }

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
            row.act = _coerce_int(item.get("act"), 1)
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
        for index, item in enumerate(characters, start=1):
            if not isinstance(item, dict):
                continue
            character_id = str(item.get("character_id") or f"{project_id}_CHAR{index:02d}").strip()
            display_name = str(item.get("display_name") or item.get("name") or character_id).strip()
            plan_id = f"snowflake_character_plan_{project_id}_{character_id}"
            plan = self.session.get(SnowflakeCharacterPlan, plan_id)
            if plan is None:
                plan = SnowflakeCharacterPlan(
                    character_plan_id=plan_id,
                    project_id=project_id,
                    character_id=character_id,
                    display_name=display_name,
                )
                self.session.add(plan)
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
            plan = self._scene_plan_by_row_uid(project_id, row_uid) if row_uid else None
            if plan is None and not duplicate_in_payload and incoming_scene_id:
                # row_uid 缺席或未命中时回退到 scene_id 查找：规划器骨架与 LLM 结构化输出
                # 只回显 scene_id（提示词明确要求 row_uid 留空），这条回退是第 10 步能绑回
                # 第 9 步建下的行、而不是每次生成都复制一份的唯一依据。
                plan = self._scene_plan_by_scene_id(project_id, incoming_scene_id)
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
                row_uid = row_uid or _mint_row_uid()
                # P1-2 铸造规则：草稿自带 scene_id 就沿用它（骨架/LLM 输出靠这个字符串
                # 在第 9→10 步之间对位；换成别的值会让第 10 步认不回第 9 步的行）。只有
                # 在它缺席或已被占用时才用 row_uid 铸——前端 canonFromFE 恰好不发
                # scene_id，所以作者手改场景表这一路始终走 row_uid 基、天然不撞号。
                scene_id = incoming_scene_id or _mint_scene_id(project_id, row_uid)
                if scene_id in seen_scene_ids or self._scene_plan_by_scene_id(project_id, scene_id) is not None:
                    scene_id = _mint_scene_id(project_id, row_uid)
                plan = SnowflakeScenePlan(
                    scene_plan_id=f"snowflake_scene_plan_{project_id}_{row_uid}",
                    project_id=project_id,
                    row_uid=row_uid,
                    scene_id=scene_id,
                    chapter_id=chapter_id,
                    scene_seq=scene_seq,
                )
                self.session.add(plan)
                minted = True
            else:
                scene_id = plan.scene_id
                if not plan.row_uid:
                    # Adopt a row_uid for a legacy row matched via scene_id.
                    plan.row_uid = row_uid or _mint_row_uid()
                    minted = True
                row_uid = plan.row_uid

            before = None if created else _scene_plan_content_signature(plan)
            plan.scene_seq = scene_seq
            plan.source_step_run_id = run.step_run_id
            # Discard any author-supplied scene_id / chapter_id — those are system
            # identity, not editable narrative fields.
            patch = _sanitize_scene_patch(item)
            patch.pop("scene_id", None)
            patch.pop("chapter_id", None)
            self._apply_scene_patch(plan, patch)
            # 阶段 G：草稿同步只把**内容真的变了**（或新建）的场打回 draft；「AI 补全这一场」和
            # 一次无谓的整表 PATCH 不再把其余几十场的确认与复核留痕一起清零。批准仍然整表置 approved。
            if approved or created or before != _scene_plan_content_signature(plan):
                plan.status = "approved" if approved else "draft"
                plan.stale_reason = None
                plan.stale_accepted_at = None
                plan.stale_accepted_by = None
                plan.stale_accepted_note = None
            if created and not plan.title:
                plan.title = str(item.get("title") or item.get("summary") or f"场景 {index:02d}").strip()
            if created and not plan.chapter_title:
                plan.chapter_title = str(item.get("chapter_title") or chapter_id).strip()
            plan.diagnosis_json = diagnose_scene_detail(_scene_plan_payload(plan))

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
            self._reconcile_removed_scene_plans(project_id, touched_row_uids)

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

    def _reconcile_removed_scene_plans(self, project_id: str, kept_row_uids: set[str]) -> None:
        """P1-3 收口：把不在本次场景列表里的场标记为已删除。

        只在 ``scene_list`` 步生效 —— 「哪些场存在」是第 9 步的职责，第 10 步只负责
        深化，它的草稿如果因为 LLM 截断少返回几场，绝不能因此删掉作者的场。

        两条护栏：
        - ``kept_row_uids`` 为空（空草稿）时调用方不会进来，避免一次空 PATCH 清空全书。
        - 已经物化成 ``SceneCard`` 的场只打 ``orphaned_flag``，不软删 —— 那边可能已经
          有正文了，删不删要作者自己决定。
        """
        rows = self.session.execute(
            select(SnowflakeScenePlan).where(
                SnowflakeScenePlan.project_id == project_id,
                SnowflakeScenePlan.removed_at.is_(None),
            )
        ).scalars().all()
        removed_at = utcnow()
        for plan in rows:
            if (plan.row_uid or "") in kept_row_uids:
                continue
            materialized = self.session.get(SceneCard, plan.scene_id)
            if materialized is not None and materialized.project_id == project_id:
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

    def _scene_plan_by_row_uid(self, project_id: str, row_uid: str) -> SnowflakeScenePlan | None:
        if not row_uid:
            return None
        return self.session.execute(
            select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id, SnowflakeScenePlan.row_uid == row_uid)
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
                    "repair_patch": _sanitize_scene_patch(item.get("repair_patch") or {}),
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
                setattr(scene, key, _coerce_string_list(value))
            elif key == "scene_seq":
                setattr(scene, key, _coerce_int(value, scene.scene_seq or 1))
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
        scene.rendering_mode = _effective_rendering_mode(scene.scene_type, scene.rendering_mode)

    def _supersede_same_step(self, run: SnowflakeStepRun) -> None:
        rows = self.session.execute(
            select(SnowflakeStepRun).where(
                SnowflakeStepRun.project_id == run.project_id,
                SnowflakeStepRun.step_key == run.step_key,
                SnowflakeStepRun.step_run_id != run.step_run_id,
                SnowflakeStepRun.status.in_(["approved", "skipped"]),
            )
        ).scalars().all()
        for row in rows:
            row.status = "superseded"

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
        for hit in hits:
            row = hit.row
            row.status = "stale"
            row.stale_reason = hit.reason
            row.stale_accepted_at = None
            row.stale_accepted_by = None
            row.stale_accepted_note = None
            affected_step_run_ids.append(row.step_run_id)
            stale_step_keys.add(row.step_key)
            self._record_revision_link(run, affected_kind="step_run", affected_id=row.step_run_id, reason=hit.reason)

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
                self._record_revision_link(run, affected_kind="scene_plan", affected_id=scene.scene_plan_id, reason=reason)

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

    def _record_revision_link(self, run: SnowflakeStepRun, *, affected_kind: str, affected_id: str, reason: str) -> None:
        existing = self.session.execute(
            select(SnowflakeRevisionLink).where(
                SnowflakeRevisionLink.project_id == run.project_id,
                SnowflakeRevisionLink.source_step_run_id == run.step_run_id,
                SnowflakeRevisionLink.affected_kind == affected_kind,
                SnowflakeRevisionLink.affected_id == affected_id,
                SnowflakeRevisionLink.status == "open",
            )
        ).scalars().first()
        if existing is not None:
            return
        self.session.add(
            SnowflakeRevisionLink(
                revision_link_id=f"snowflake_revision_{run.project_id}_{uuid.uuid4().hex[:10]}",
                project_id=run.project_id,
                source_step_key=run.step_key,
                source_step_run_id=run.step_run_id,
                affected_kind=affected_kind,
                affected_id=affected_id,
                reason=reason,
                status="open",
            )
        )

    def _skip_draft(self, step_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = get_step_definition(step_key)
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

    @staticmethod
    def _step_run_payload(run: SnowflakeStepRun | None) -> dict[str, Any] | None:
        if run is None:
            return None
        return {
            "step_run_id": run.step_run_id,
            "artifact_id": run.step_run_id,
            "step_key": run.step_key,
            "version": run.version,
            "status": run.status,
            "diagnosis_json": deepcopy(run.health_json or {}),
            "llm_call_id": run.llm_call_id,
            "approved_at": run.approved_at,
            "stale_reason": run.stale_reason,
            "stale_accepted_at": run.stale_accepted_at,
            "stale_accepted_by": run.stale_accepted_by,
            "stale_accepted_note": run.stale_accepted_note,
            # 阶段 E：本版草稿写入时消费的上游 step_run_id（按 step_key）——前端用它对照
            # 各上游现在的 step_run_id，把「上游改了什么」拉成消费版本 vs 当前版本的 diff。
            "input_refs": deepcopy(run.input_refs_json or {}),
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }

    @staticmethod
    def _step_run_history_payload(run: SnowflakeStepRun, *, include_draft: bool = False) -> dict[str, Any]:
        payload = {
            "step_run_id": run.step_run_id,
            "version": run.version,
            "status": run.status,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "approved_at": run.approved_at,
            "stale_reason": run.stale_reason,
            "stale_accepted_at": run.stale_accepted_at,
            "stale_accepted_by": run.stale_accepted_by,
            "stale_accepted_note": run.stale_accepted_note,
            "generation_source": str((run.health_json or {}).get("generation_source") or ""),
            "trigger_source": str((run.health_json or {}).get("trigger_source") or ""),
            "draft_summary": _draft_summary(run.draft_json or {}),
        }
        if include_draft:
            payload["draft"] = deepcopy(run.draft_json or {})
        return payload

    @staticmethod
    def _merge_character_import_draft(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
        existing_characters = existing.get("characters") if isinstance(existing, dict) else None
        incoming_characters = incoming.get("characters") if isinstance(incoming, dict) else None
        if not isinstance(existing_characters, list) or not isinstance(incoming_characters, list):
            return deepcopy(incoming)
        existing_by_id: dict[str, dict[str, Any]] = {}
        merged_characters: list[dict[str, Any]] = []
        for item in existing_characters:
            if not isinstance(item, dict):
                continue
            cloned = deepcopy(item)
            identity = _character_identity(cloned)
            if identity:
                existing_by_id[identity] = cloned
            merged_characters.append(cloned)
        for item in incoming_characters:
            if not isinstance(item, dict):
                continue
            identity = _character_identity(item)
            if not identity or identity not in existing_by_id:
                cloned = deepcopy(item)
                merged_characters.append(cloned)
                if identity:
                    existing_by_id[identity] = cloned
                continue
            merged_item = _merge_preserving_existing(existing_by_id[identity], item)
            existing_by_id[identity].clear()
            existing_by_id[identity].update(merged_item)
        return {
            **deepcopy(incoming),
            "characters": merged_characters,
        }

    @staticmethod
    def _step_from_workspace(workspace: dict[str, Any], step_key: str) -> dict[str, Any]:
        for step in workspace.get("steps") or []:
            if step.get("step_key") == step_key:
                return step
        raise DomainError("SNOWFLAKE_STEP_NOT_FOUND", "未知的雪花步骤。", status_code=404)

    @staticmethod
    def _approved_context(workspace: dict[str, Any]) -> list[dict[str, Any]]:
        """驻场教练/场景急救看到的全书上下文。

        和 _upstream_step_context 同一条纪律：不能只收 gate_satisfied（approved/skipped）。
        explore 模式下作者可以一路不确认，改上游又会把下游打成 stale，只收已确认
        就等于让教练看不见这本书的故事，只能泛泛而谈或另编一套。未确认草稿照给，
        如实标注状态即可。
        """
        return [
            {
                "step_key": item["step_key"],
                "label": item["label"],
                "status": item.get("status"),
                "confirmed": bool(item.get("gate_satisfied")),
                "draft": deepcopy(item.get("draft") or {}),
            }
            for item in workspace.get("steps") or []
            if draft_has_content(item.get("draft"))
        ]

    @staticmethod
    def _merged_draft_override(
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
            for key, value in (((latest.artifact_json if latest is not None else None) or {}).items())
            if not str(key).startswith("fe_")
        }
        override_payload = {key: value for key, value in draft_override.items() if not str(key).startswith("fe_")}
        return _merge_dicts_keeping_members(base_payload, override_payload)

    @staticmethod
    def _step_with_override(
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
        override_payload = {
            key: value for key, value in draft_override.items() if not str(key).startswith("fe_")
        }
        merged_step["draft"] = _merge_dicts_keeping_members(merged_step.get("draft") or {}, override_payload)
        merged_step["draft"] = merge_step_draft(
            str(merged_step.get("step_key") or ""),
            merged_step.get("draft") or {},
            latest_by_step=latest_by_step,
        )
        return merged_step


# 阶段 U：「先看 3 个方向」没带作者要求时，回合里的「我」这一行写这句
CANDIDATES_DEFAULT_ASK = "给我 3 个不同方向"

# 抹空保护：一步草稿里不算「故事文字」的键——身份、枚举与排序，空白默认稿也会带着它们（如角色步的 role=主角）
_NON_STORY_KEYS = frozenset({
    "character_id", "row_uid", "scene_id", "scene_plan_id", "chapter_id", "role", "primary_form", "scene_type",
    "rendering_mode", "target_length_band", "act", "chapter_seq", "scene_seq", "spine", "status", "version",
    "pov_character_id", "protagonist_character_id", "onstage_chars_json",
})
_WIPE_GUARD_MIN_CHARS = 8  # 两三个字的试笔被清空不值得多留一版


def _story_text_chars(value: Any, key: str | None = None) -> int:
    if key is not None and (key in _NON_STORY_KEYS or str(key).startswith("fe_")):
        return 0
    if isinstance(value, str):
        return len(value.strip())
    if isinstance(value, list):
        return sum(_story_text_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(_story_text_chars(item, str(name)) for name, item in value.items())
    return 0


def _would_wipe_story(previous: Any, incoming: Any) -> bool:
    """整步抹空：旧稿有成段的故事文字，新稿一个字都没有（只剩身份 / 枚举 / fe_* 写穿键）。"""
    return (
        _story_text_chars(semantic_payload(previous if isinstance(previous, dict) else {})) >= _WIPE_GUARD_MIN_CHARS
        and _story_text_chars(semantic_payload(incoming if isinstance(incoming, dict) else {})) == 0
    )

_PROTAGONIST_EXCLUDE_ZH = ("对手", "反派", "对立", "配角", "敌")
_PROTAGONIST_TOKENS_EN = ("protagonist", "main character", "heroine", "hero", "lead")
_PROTAGONIST_EXCLUDE_EN = ("antagonist", "opposition", "villain", "rival", "supporting")


def _is_protagonist_role(role: Any) -> bool:
    """角色定位是不是主角：中文含「主角」且不含对手 / 反派类字眼；英文 lead / protagonist / hero(ine)。"""
    text = str(role or "").strip().lower()
    if not text:
        return False
    if "主角" in text:
        return not any(token in text for token in _PROTAGONIST_EXCLUDE_ZH)
    if any(token in text for token in _PROTAGONIST_EXCLUDE_EN):
        return False
    return any(token in text for token in _PROTAGONIST_TOKENS_EN)


# 集合步里成员身份键：按它对位合并，而不是整表替换。
_COLLECTION_ID_KEYS = {
    "scenes": ("scene_id", "row_uid"),
    "characters": ("character_id", "display_name"),
    # 章表同理：FE 少带几章不能把存档里的章整片抹掉，那会连带解绑全书的场景归属。
    "chapters": ("row_uid",),
}


def _merge_dicts_keeping_members(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """generate 的 draft_override 专用合并：集合按成员 id 对位，不整表替换。

    draft_override 的语义是「补上作者刚编辑、还没自动保存上行的内容」（见调用点注释），
    是叠加，不是删除指令——真正的删除走 PATCH save_step，那里仍然整表替换。
    朴素的整表替换会让前端少带几个成员就把存档里的成员整片抹掉：作品《何有》的
    场景规划就这样从 12 场无声掉回 5 场，而且发生在调 LLM 之前。
    """
    merged = deepcopy(base)
    for key, value in override.items():
        id_keys = _COLLECTION_ID_KEYS.get(str(key))
        if id_keys and isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = _merge_member_lists(merged[key], value, id_keys=id_keys)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts_keeping_members(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _merge_member_lists(
    base_items: list[Any],
    override_items: list[Any],
    *,
    id_keys: tuple[str, ...],
) -> list[Any]:
    """按成员 id 对位合并两份集合，保持 override 的顺序，base 独有的成员追加在尾部。

    身份匹配对 id_keys 里**任一**键取交集：同一场景在 base 里带 scene_id、在 override 里
    只带 row_uid（前端刚编辑、后端 id 还没回填的成员，两边键不同）也能对上，不会被当成两个
    成员重复留下。只有当两份成员完全没有共享任一 id 值时才视为不同成员——那本就是不同成员。
    """

    def id_values(item: Any) -> list[str]:
        if not isinstance(item, dict):
            return []
        values: list[str] = []
        for id_key in id_keys:
            value = str(item.get(id_key) or "").strip()
            if value:
                values.append(f"{id_key}:{value}")
        return values

    # 一个 base 成员按它携带的每个 id 值都建索引，这样 override 用其中任一键都能命中同一成员。
    base_by_id_value: dict[str, Any] = {}
    for item in base_items:
        for value in id_values(item):
            base_by_id_value.setdefault(value, item)

    merged: list[Any] = []
    matched_base: set[int] = set()
    for item in override_items:
        existing = None
        for value in id_values(item):
            if value in base_by_id_value:
                existing = base_by_id_value[value]
                break
        if isinstance(existing, dict) and isinstance(item, dict):
            merged.append(_merge_dicts_keeping_members(existing, item))
            matched_base.add(id(existing))
        else:
            merged.append(deepcopy(item))
    # 前端这次没带上来的成员不代表作者删了它——留在尾部，等真正的 PATCH 来决定去留。
    merged.extend(
        deepcopy(item)
        for item in base_items
        if id_values(item) and id(item) not in matched_base
    )
    return merged


def _draft_summary(value: Any, *, limit: int = 180) -> str:
    pieces: list[str] = []

    def visit(item: Any) -> None:
        if len(" ".join(pieces)) >= limit:
            return
        if isinstance(item, str):
            text = " ".join(item.split())
            if text:
                pieces.append(text)
            return
        if isinstance(item, list):
            for child in item[:8]:
                visit(child)
            return
        if isinstance(item, dict):
            for child in item.values():
                visit(child)

    visit(value)
    summary = " ".join(pieces)
    return summary[:limit].rstrip()


def _character_identity(item: dict[str, Any]) -> str:
    return str(item.get("character_id") or item.get("display_name") or item.get("name") or "").strip()


def _has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _merge_preserving_existing(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(existing)
    for key, value in incoming.items():
        if not _has_meaningful_value(value):
            continue
        current = merged.get(key)
        if not _has_meaningful_value(current):
            merged[key] = deepcopy(value)
            continue
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _merge_preserving_existing(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            merged[key] = current or deepcopy(value)
    return merged


_SCENE_PLAN_STATE_KEYS: frozenset[str] = frozenset(
    {"scene_plan_id", "status", "stale_reason", "stale_accepted_at", "stale_accepted_by", "stale_accepted_note", "diagnosis"}
)


def _scene_plan_content_signature(scene: SnowflakeScenePlan) -> str:
    """场景计划行的**内容**签名（去掉状态 / 失效留痕 / 诊断 / 身份）；同步时据此判断这一行有没有真的改。"""
    payload = {key: value for key, value in _scene_plan_payload(scene).items() if key not in _SCENE_PLAN_STATE_KEYS}
    return stable_json(payload)


def _scene_plan_payload(scene: SnowflakeScenePlan) -> dict[str, Any]:
    return {
        "scene_plan_id": scene.scene_plan_id,
        "row_uid": scene.row_uid or "",
        "scene_id": scene.scene_id,
        "chapter_plan_id": scene.chapter_plan_id or "",
        "chapter_id": scene.chapter_id,
        "chapter_title": scene.chapter_title or "",
        "chapter_goal": scene.chapter_goal or "",
        "chapter_role": scene.chapter_role or "",
        "spine": scene.spine or "",
        "scene_seq": scene.scene_seq,
        "pov_character_id": scene.pov_character_id or "",
        "onstage_chars_json": list(scene.onstage_chars_json or []),
        "title": scene.title or "",
        "summary": scene.summary or "",
        "primary_form": scene.scene_type or "proactive",
        "scene_type": scene.scene_type or "proactive",
        "location": scene.location or "",
        "scene_crucible": scene.scene_crucible or "",
        "crucible": scene.scene_crucible or "",
        "goal": scene.goal or "",
        "conflict": scene.conflict or "",
        "setback": scene.setback or "",
        "reaction": scene.reaction or "",
        "dilemma": scene.dilemma or "",
        "decision": scene.decision or "",
        "cost_requirement": scene.cost_requirement or "",
        "beats_json": list(scene.beats_json or []),
        "must_include_text": scene.must_include_text or "",
        "exit_change": scene.exit_change or "",
        "hook": scene.hook or "",
        "target_length_band": scene.target_length_band or "",
        "rendering_mode": scene.rendering_mode or "full",
        "expected_reader_emotion": scene.expected_reader_emotion or "",
        "story_time": scene.story_time or "",
        "exception_reason": scene.exception_reason or "",
        "status": scene.status,
        "stale_reason": scene.stale_reason or "",
        "stale_accepted_at": scene.stale_accepted_at,
        "stale_accepted_by": scene.stale_accepted_by or "",
        "stale_accepted_note": scene.stale_accepted_note or "",
        "diagnosis": deepcopy(scene.diagnosis_json or {}),
    }


#: 确认这两步时（工作台带 ``sync_catalog``）已物化的场景卡自动跟上构思
CATALOG_SYNC_STEPS = frozenset({"scene_list", "scene_details"})

#: 构思里的场景题名超过这个长度就当它是摘要（09 没单独起题名时 title 会跟着摘要走）
_SCENE_TITLE_SEED_MAX_CHARS = 40


def _real_scene_title(title: Any, summary: Any) -> str:
    """构思里**真起过**的场景短题名；空、和摘要一字不差、或长得像一句摘要的都不算。"""
    value = " ".join(str(title or "").split())
    if not value or value == " ".join(str(summary or "").split()) or len(value) > _SCENE_TITLE_SEED_MAX_CHARS:
        return ""
    return value


def _scene_title_seed(detail: dict[str, Any]) -> dict[str, str]:
    """物化写进场景卡简报的题名键：``title``（目录显示用，作者可在台子上改）+ ``seeded_title``
    （这次播下去的值——之后题名还等于它，就说明作者没改过，回流 / 重新物化可以跟着构思走）。"""
    real = _real_scene_title(detail.get("title"), detail.get("summary"))
    # seeded_title 总是写（哪怕是空串）：这个键在，就说明这张卡是阶段 X 之后播的，题名可以参与「待同步」比较
    return {"title": real, "seeded_title": real} if real else {"seeded_title": ""}


def _followed_scene_title(previous_brief: dict[str, Any], real: str) -> dict[str, str]:
    """回流 / 重新物化时题名怎么走：作者在台子上改过的（≠ 上次播的）原样保留，否则跟构思。"""
    current = str(previous_brief.get("title") or "").strip()
    seeded = str(previous_brief.get("seeded_title") or "").strip()
    if current and current != seeded:
        return {"title": current, "seeded_title": real}
    return {"title": real, "seeded_title": real} if real else {"seeded_title": ""}


def _scene_card_beats(scene_type: str, detail: dict[str, Any]) -> list[str]:
    """``SceneCard.beats_json`` 的唯一配方，物化与 resync 共用。

    规划行自带节拍就用它；否则按场景类型从 goal/conflict/setback（主动）或
    reaction/dilemma/decision（反应）推导——即规划器 ``_beats_from_detail`` 的口径，
    v1 物化路也是它。hook 不进节拍：它已经单独落在 ``SceneCard.hook`` 与 brief 的
    ``next_scene_pull`` 上，resync 曾额外拼进去，正是「刚物化完就待同步」的来源。
    """
    beats = _coerce_string_list(detail.get("beats_json"))
    return beats or _beats_from_detail(scene_type, detail)


def _scene_list_payload(scene: SnowflakeScenePlan) -> dict[str, Any]:
    return {
        "scene_plan_id": scene.scene_plan_id,
        "row_uid": scene.row_uid or "",
        "scene_id": scene.scene_id,
        "chapter_plan_id": scene.chapter_plan_id or "",
        "chapter_id": scene.chapter_id,
        "chapter_title": scene.chapter_title or scene.chapter_id,
        "chapter_goal": scene.chapter_goal or "",
        "spine": scene.spine or "",
        "scene_seq": scene.scene_seq,
        "pov_character_id": scene.pov_character_id or "",
        "summary": scene.summary or scene.title or "",
        "primary_form": scene.scene_type or "proactive",
        "scene_type": scene.scene_type or "proactive",
        "chapter_role": scene.chapter_role or "",
        "location": scene.location or "",
        "crucible": scene.scene_crucible or "",
    }


def _sanitize_scene_patch(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    patch: dict[str, Any] = {}
    for key in SCENE_PATCH_FIELDS:
        if key in payload:
            patch[key] = deepcopy(payload[key])
    if "primary_form" in patch:
        patch["scene_type"] = patch["primary_form"]
    return patch


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _coerce_triage_status(value: Any) -> str:
    # 阶段 N：cut（待删）是作者专用的裁定——原著「杀要杀得对：不真删，标记待删」；LLM 分诊只出 pass / maybe / rewrite。
    status = str(value or "").strip().lower()
    return status if status in {"pass", "maybe", "rewrite", "cut"} else ""


def _step_display_label(step_key: str) -> str:
    try:
        return str(get_step_definition(step_key).get("label") or step_key)
    except KeyError:
        return step_key


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mint_row_uid() -> str:
    """Mint an immutable, system-owned scene-row identity (P1-1).

    Scene identity no longer derives from the author-editable ``scene_id``; this
    uuid is minted once when a row is first seen and then never changes, so a
    reorder or an ID re-mint can never orphan a plan or break the diff chain.
    """
    return f"row_{uuid.uuid4().hex}"


def _mint_scene_id(project_id: str, row_uid: str) -> str:
    """Mint the scene's materialization identity from its immutable row anchor (P1-2).

    The old rule was ``f"{chapter_id}_SC{scene_seq:02d}"``, frozen at creation while
    ``scene_seq`` was recomputed on every save — so deleting a scene and adding another
    reliably produced two rows with the same ``scene_id``, and materialization then lost
    one of them without a word. Deriving it from ``row_uid`` instead makes it unique by
    construction and independent of both chapter membership and ordering.
    """
    return f"{project_id}_SC_{row_uid}"
