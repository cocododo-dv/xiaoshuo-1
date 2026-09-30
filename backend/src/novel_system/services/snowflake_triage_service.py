"""雪花场景分诊（第 10 步）：AI 分诊建议（fail-closed）、作者的裁定落库、工作台上每场的分诊条目。

每场只看最新的一条分诊记录（``snowflake_triage``）；没有记录的场给规则诊断（``auto_diagnosis``）。
2026-09-30 从 ``SnowflakeWorkspaceService`` 拆出（B06-07）。
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import select

from novel_system.db.models import SnowflakeScenePlan, SnowflakeSceneTriageItem
from novel_system.services.errors import DomainError
from novel_system.services.snowflake_scene_rows import sanitize_scene_patch, scene_plan_payload
from novel_system.services.snowflake_step_diagnosis import diagnose_scene_detail
from novel_system.services.snowflake_triage import (
    EXCLUDED_TRIAGE_STATUSES,
    coerce_triage_status,
    excluded_scene_plan_ids,
    latest_triage_rows,
)
from novel_system.services.value_coercion import coerce_string_list, int_or_default


class SnowflakeTriageMixin:
    """见模块说明。与其它 ``snowflake_*`` 混入类一起组成 ``SnowflakeWorkspaceService``（B06-07）：
    方法之间照旧经 ``self`` 互相调用，名字与签名一个不改（测试与分章包依赖它们）。"""

    def suggest_scene_triage(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        workspace = self._workspace_payload(project.project_id, lean=True)
        step = self._step_from_workspace(workspace, "scene_details")
        if not step.get("draft", {}).get("scenes"):
            raise DomainError("SNOWFLAKE_SCENE_DETAILS_REQUIRED", "需要先完成场景规划（场景细化）。", status_code=409)
        latest_by_step = self._latest_by_step(project.project_id)
        step = self._step_with_override(
            project.project_id,
            step,
            body.get("draft_override"),
            latest_by_step=latest_by_step,
            roster=self._character_roster(project.project_id, latest_by_step),
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
