"""雪花物化闸门（纯函数）：十步与场景计划离「整理为章节结构」还差什么——阻断项、提醒项，逐条带跳转动作。

只看传进来的各步最新版本、每场分诊、场景计划与分章现状，不碰会话；2026-09-30 从 ``SnowflakeWorkspaceService`` 拆出
（B06-07），服务类上的 ``_materialization_gate`` 照旧可用。
"""

from __future__ import annotations

from typing import Any

from novel_system.db.models import SnowflakeScenePlan, SnowflakeStepRun
from novel_system.services.snowflake_scene_rows import scene_plan_payload
from novel_system.services.snowflake_step_catalog import (
    CONFIRMED_STEP_STATUSES,
    MATERIALIZATION_REQUIRED_STEPS,
    MATERIALIZATION_WARNING_STEPS,
    effective_rendering_mode,
)
from novel_system.services.snowflake_step_catalog import step_label as step_display_label
from novel_system.services.snowflake_triage import EXCLUDED_TRIAGE_STATUSES


def materialization_gate(
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

    def add_scene_item(
        *,
        severity: str,
        kind: str,
        message: str,
        item: dict[str, Any],
        step_key: str = "scene_details",
        primary_action: dict[str, Any] | None = None,
    ) -> None:
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
                "step_key": step_key,
                "scene_id": scene_id or None,
                "scene_plan_id": scene_plan_id or None,
                "target_view": "snowflake-workbench",
                "primary_action": primary_action or {
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
        step_label = step_display_label(step_key)
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
        if run.status not in CONFIRMED_STEP_STATUSES and not (run.status == "stale" and run.stale_accepted_at):
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
        step_label = step_display_label(step_key)
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
        if run.status not in CONFIRMED_STEP_STATUSES:
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
        scene_details_run.status in CONFIRMED_STEP_STATUSES
        or (scene_details_run.status == "stale" and scene_details_run.stale_accepted_at)
    )
    if scene_details_ready and not (scene_plans or []):
        add_step_item(
            severity="blocker",
            kind="missing_scene_plans",
            message="场景细化已确认，但还没有可整理的场景计划；请先在「场景清单 / 场景细化」生成并确认场景，才能整理章节结构。",
            step_key="scene_details",
        )

    # R15a：过期的场景计划由 09 / 10 的「已复核」一处解开（步骤级复核连同场景计划一起复核）——
    # 提示指向还没复核的那一步，而不是去分诊面板「修这个场景」（那里没有复核按钮）。
    stale_plan_step = next(
        (
            key
            for key in ("scene_list", "scene_details")
            if latest_by_step.get(key) is not None
            and latest_by_step[key].status == "stale"
            and not latest_by_step[key].stale_accepted_at
        ),
        "scene_details",
    )
    stale_plan_label = step_display_label(stale_plan_step)
    for scene in scene_plans or []:
        if scene.status != "stale":
            continue
        scene_label = str(scene.title or scene.summary or scene.scene_id or "scene").strip()
        item = scene_plan_payload(scene)
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
            message=(
                f"{scene_label} 在上游改动后需要复核：到「{stale_plan_label}」看过后点「已复核」，"
                "或改完再「确认本步」，才能整理为章节结构。"
            ),
            item=item,
            step_key=stale_plan_step,
            primary_action={"type": "jump_to_step", "label": "去点「已复核」", "step_key": stale_plan_step},
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
