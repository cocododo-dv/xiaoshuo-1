"""雪花一步草稿的写侧：生成（整步 / 定向 / 略过 / 按方向）、作者保存（待审版原位改写、抹空保护、同内容不降级）、
历史与恢复，以及它们共用的健康度、草稿覆盖（draft_override）的合并。

写入即规范：角色 id 带作品前缀落库，视角 / 在场 / 全书主角的引用指着角色才补前缀（``snowflake_character_ids``）。
2026-09-30 从 ``SnowflakeWorkspaceService`` 拆出（B06-07）。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from novel_system.db.models import OperationLog, SnowflakeAssistantTurn, SnowflakeCharacterPlan, SnowflakeStepRun, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_text
from novel_system.services.snowflake_chapter_table import carry_stored_chapters, keep_live_chapter_table
from novel_system.services.snowflake_character_ids import RosterSource, canonical_character_id, canonicalize_draft
from novel_system.services.snowflake_draft_merge import overlay_keeping_members
from novel_system.services.snowflake_staleness import semantic_payload
from novel_system.services.snowflake_step_catalog import CHARACTER_STEPS, step_definition_view
from novel_system.services.snowflake_step_diagnosis import diagnose_step_pressure, step_completeness
from novel_system.services.snowflake_step_drafts import merge_step_draft
from novel_system.services.snowflake_step_runs import (
    WIPE_PRESERVED_EVENT,
    StepRunStore,
    step_run_history_payload,
    would_wipe_story,
)


class SnowflakeStepEditingMixin:
    """见模块说明。与其它 ``snowflake_*`` 混入类一起组成 ``SnowflakeWorkspaceService``（B06-07）：
    方法之间照旧经 ``self`` 互相调用，名字与签名一个不改（测试与分章包依赖它们）。"""

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
        chapters_kept = False
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
            draft_override = self._merged_draft_override(
                project.project_id,
                latest_by_step,
                step_key,
                body.get("draft_override"),
                roster=self._character_roster(project.project_id, latest_by_step),
            )
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
                    if step_key in CHARACTER_STEPS
                    else None
                ),
                draft_override=draft_override,
                author_direction_brief=brief_prompt,
                direction_kind=direction_kind,
            )
            # 模型看到的是规范口径的 id，回来的也按规范口径落库；缺 id 的新成员在这里铸号（模型回的姓名引用原样）
            draft = canonicalize_draft(
                project.project_id,
                llm_result.payload,
                roster=self._character_roster(project.project_id, latest_by_step),
                mint_missing=True,
            )
            # R11（批准 #18a）：07 的章表是分章结果的只读镜像——已经分过章就保留现表，模型给的章表不收、不同步
            chapters_kept = step_key == "long_synopsis" and keep_live_chapter_table(self.session, project.project_id, draft)
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
        sync_notice = None if chapters_kept else self._sync_structured_step_data(project, step_key, draft, run)
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
        # 写入即规范（B06-01）：角色 id 带作品前缀，缺 id 的角色铸号；视角 / 在场 / 全书主角的引用指着角色才补前缀，
        # 手填的姓名原样（04 名册还空时 09 的视角是自由文本框）
        draft = canonicalize_draft(
            project.project_id,
            merge_step_draft(step_key, body.get("draft") or {}, latest_by_step=latest_by_step),
            roster=self._character_roster(project.project_id, latest_by_step),
            mint_missing=True,
        )
        latest = latest_by_step.get(step_key)
        # R11（批准 #18a）：07 的章表是分章结果的只读镜像，前端不再上行它——没带章表时沿用存着的那一份（必须在下面的
        # 语义比较之前：缺席的章表会被默认值补成空表，已确认的 07 就被打回待审），也不去同步章表行。显式带了照旧同步。
        chapters_carried = step_key == "long_synopsis" and carry_stored_chapters(draft, body.get("draft"), latest)

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
        wipes_story = latest is not None and would_wipe_story(latest.draft_json, draft)
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
        if not chapters_carried:
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
            # 摘要与草稿都按前端口径（角色 id 剥掉服务端补的前缀）
            payload = step_run_history_payload(row, include_draft=include_draft)
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
        draft = canonicalize_draft(
            project.project_id,
            deepcopy(source_run.draft_json or {}),
            roster=self._character_roster(project.project_id, latest_by_step),
            mint_missing=True,
        )
        # R11（批准 #18a）：07 的章表是分章结果的只读镜像、章表行只有一个写入方——恢复一版旧的 07 只恢复它的文字，
        # 章结构（章行、分章面板 / 写作台起的章名、场景归属）保留现表，与整步生成同一条规矩（复核 P04-R3，主管决定）
        chapters_kept = step_key == "long_synopsis" and keep_live_chapter_table(self.session, project.project_id, draft)
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
        sync_notice = None if chapters_kept else self._sync_structured_step_data(project, step_key, draft, run)
        if sync_notice:
            # 同步落库时才知道、作者必须知道的事实与生成同一条路挂进健康度，回包也如实带着（R15a：恢复界面要在这里
            # 告诉作者）。已经分过章时恢复 07 不再动章表（见上），也就不会再报「章表收缩」；这条回报的路留着。
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

    def _character_roster(self, project_id: str, latest_by_step: dict[str, SnowflakeStepRun]) -> RosterSource:
        """本作品的角色名册（库里口径的 id）：角色计划 + 三个角色步最新草稿的成员——规范引用时据此认出哪些是角色 id。
        返回按需取的函数：前端的编号与已带前缀的 id 不必查，只有认不出一个引用（多半是手填的姓名）时才读一次库。"""

        def load() -> set[str]:
            ids = set(
                self.session.execute(
                    select(SnowflakeCharacterPlan.character_id).where(SnowflakeCharacterPlan.project_id == project_id)
                ).scalars()
            )
            for step_key in CHARACTER_STEPS:
                run = latest_by_step.get(step_key)
                members = (run.draft_json or {}).get("characters") if run is not None else None
                for item in members if isinstance(members, list) else []:
                    if isinstance(item, dict):
                        ids.add(str(item.get("character_id") or "").strip())
            return {value for value in ids if value}

        return load

    @staticmethod
    def _merged_draft_override(
        project_id: str,
        latest_by_step: dict[str, SnowflakeStepRun],
        step_key: str,
        draft_override: Any,
        *,
        roster: RosterSource | None = None,
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
            project_id,
            {key: value for key, value in draft_override.items() if not str(key).startswith("fe_")},
            roster=roster,
        )
        return overlay_keeping_members(base_payload, override_payload)

    @staticmethod
    def _step_with_override(
        project_id: str,
        step: dict[str, Any],
        draft_override: Any,
        *,
        latest_by_step: dict[str, SnowflakeStepRun],
        roster: RosterSource | None = None,
    ) -> dict[str, Any]:
        if not isinstance(draft_override, dict):
            return step
        merged_step = deepcopy(step)
        # 与 generate_step 同源:draft_override 是「作者刚编辑、还没自动保存上行」的叠加,
        # 不是删除指令。助手/场景三分类同样必须按成员对位合并——整表替换会让前端少带几个
        # 成员就把存档里的成员整片抹掉,教练/分类器于是只看到半截故事(与 generate 同一 bug)。
        # 先剥掉 fe_* 写透键,再按成员 id 对位。
        override_payload = canonicalize_draft(
            project_id,
            {key: value for key, value in draft_override.items() if not str(key).startswith("fe_")},
            roster=roster,
        )
        merged_step["draft"] = overlay_keeping_members(merged_step.get("draft") or {}, override_payload)
        merged_step["draft"] = merge_step_draft(
            str(merged_step.get("step_key") or ""),
            merged_step.get("draft") or {},
            latest_by_step=latest_by_step,
        )
        return merged_step
