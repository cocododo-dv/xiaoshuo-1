"""雪花一步的确认与「已复核」：确认一版（让位、消费的上游快照、结构化同步、下游失效、运行时失效范围、确认即同步），
已复核过期的步骤（连同过期的场景计划），下游失效的级联与它的留痕。

2026-09-30 从 ``SnowflakeWorkspaceService`` 拆出（B06-07）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from novel_system.db.models import OperationLog, SnowflakeStepRun, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.project_runtime_invalidation import ProjectRuntimeInvalidationService
from novel_system.services.snowflake_scene_rows import CATALOG_SYNC_STEPS, SCENE_PLAN_STEPS
from novel_system.services.snowflake_staleness import (
    changed_scene_row_uids,
    field_sigs,
    recompute_stale,
    snapshot_consumed_sigs,
)
from novel_system.services.snowflake_step_catalog import STEP_ORDER


class SnowflakeApprovalMixin:
    """见模块说明。与其它 ``snowflake_*`` 混入类一起组成 ``SnowflakeWorkspaceService``（B06-07）：
    方法之间照旧经 ``self`` 互相调用，名字与签名一个不改（测试与分章包依赖它们）。"""

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
