"""雪花工作台的读侧：工作台载荷（GET 全貌 / 变更回包瘦身版 / 单独一步）、每一步的形状与闸门、按故事序的场景计划。

一次构建每样东西只读一遍库（B06-04）；角色 id 交给前端时剥掉作品前缀（``snowflake_character_ids``）。
2026-09-30 从 ``SnowflakeWorkspaceService`` 拆出（B06-07）。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from sqlalchemy import select

from novel_system.db.models import OutlinePlan, SnowflakeScenePlan, SnowflakeStepRun, StoryProject
from novel_system.services.author_actions import author_action
from novel_system.services.errors import DomainError
from novel_system.services.projects import outline_plan_payload, project_payload
from novel_system.services.snowflake_character_ids import present_draft
from novel_system.services.snowflake_gate import materialization_gate
from novel_system.services.snowflake_llm_context import approved_context_from_steps
from novel_system.services.snowflake_queries import latest_outline_plan, next_outline_plan_version
from novel_system.services.snowflake_scene_order import (
    live_scene_plans_in_story_order,
    positions_from_rows,
    sort_in_story_order,
)
from novel_system.services.snowflake_scene_rows import SCENE_PLAN_STEPS, scene_list_payload, scene_plan_payload
from novel_system.services.snowflake_step_catalog import (
    CONFIRMED_STEP_STATUSES,
    MATERIALIZATION_REQUIREMENTS,
    QUALITY_POLICY,
    SNOWFLAKE_METHOD_VERSION,
    STEP_ORDER,
    step_definition_view,
    step_definition_views,
)
from novel_system.services.snowflake_step_diagnosis import step_completeness
from novel_system.services.snowflake_step_drafts import merge_step_draft
from novel_system.services.snowflake_step_guidance import editor_payload, step_guidance
from novel_system.services.snowflake_step_runs import step_run_payload
from novel_system.services.snowflake_triage import EXCLUDED_TRIAGE_STATUSES, latest_triage_rows, plan_ids_with_status


class SnowflakeWorkspaceViewMixin:
    """见模块说明。与其它 ``snowflake_*`` 混入类一起组成 ``SnowflakeWorkspaceService``（B06-07）：
    方法之间照旧经 ``self`` 互相调用，名字与签名一个不改（测试与分章包依赖它们）。"""

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
