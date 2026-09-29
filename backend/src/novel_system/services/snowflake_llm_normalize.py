"""雪花各 LLM 节点输出的归一：整步生成（焦点过滤 + 清洗 + 合并）、三个方向、教练回复、AI 分诊。

分章建议与起章名的归一留在 ``snowflake_workspace_llm``（分章包之后整体搬走）。纯函数。
2026-09-30 从 ``snowflake_workspace_llm.py`` 拆出（B06-08），那边原样转出这里的每一个名字。
"""

from __future__ import annotations

from typing import Any

from novel_system.services.snowflake_direction_brief import coerce_brief_update
from novel_system.services.snowflake_step_drafts import merge_step_draft
from novel_system.services.snowflake_step_diagnosis import diagnose_scene_detail
from novel_system.services.value_coercion import coerce_string_list
from novel_system.services.snowflake_llm_sanitize import (
    _assert_meaningful_generation_patch,
    _merge_patch,
    _sanitize_scene_repair_patch,
    _sanitize_step_patch,
)
from novel_system.services.snowflake_llm_context import _project_id_from_steps


def _normalize_candidates_output(output: dict[str, Any]) -> dict[str, Any]:
    """方向数组裁剪到契约形状（≤4 条；label/tag/notes 限长）。"""
    items = output.get("candidates") if isinstance(output, dict) else None
    normalized: list[dict[str, Any]] = []
    for item in (items or [])[:4]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        notes = item.get("notes") if isinstance(item.get("notes"), list) else []
        normalized.append(
            {
                "label": str(item.get("label") or f"方向 {len(normalized) + 1}").strip()[:8],
                "tag": str(item.get("tag") or "AI 方向").strip()[:16],
                "text": text,
                "notes": [str(n).strip()[:10] for n in notes[:3] if str(n).strip()],
            }
        )
    return {"candidates": normalized}


def _normalize_full_step_output(
    step_key: str,
    output: dict[str, Any],
    *,
    latest_by_step: dict[str, Any],
    project_id: str,
    base_override: dict[str, Any] | None = None,
    focus_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if focus_filter and isinstance(output, dict):
        output = _filter_output_to_focus(output, focus_filter)
    base = base_override if base_override is not None else merge_step_draft(step_key, None, latest_by_step=latest_by_step)
    patch = _sanitize_step_patch(step_key, output, latest_by_step=latest_by_step, project_id=project_id, base=base)
    _assert_meaningful_generation_patch(step_key, patch)
    return _merge_patch(base, patch)


def _filter_output_to_focus(output: dict[str, Any], focus_filter: dict[str, Any]) -> dict[str, Any]:
    """焦点定向的服务端硬约束：清洗前先把模型输出过滤到焦点成员——模型即使
    违约复述/改写了焦点外成员，也不让它进合并；全部被过滤掉则明确报错，
    绝不悄悄把定向请求变成全量重写。"""
    field_key = str(focus_filter.get("field") or "")
    id_keys = tuple(focus_filter.get("id_keys") or ())
    allowed = focus_filter.get("allowed") or set()
    members = output.get(field_key)
    if not isinstance(members, list):
        return output
    kept = [
        item
        for item in members
        if isinstance(item, dict) and ({str(item.get(key) or "").strip() for key in id_keys} & allowed)
    ]
    if members and not kept:
        raise ValueError(
            f"focused generation returned no {field_key} matching the requested focus ids; "
            "the model must echo the focus member ids verbatim — retry the request."
        )
    filtered = dict(output)
    filtered[field_key] = kept
    return filtered


def _normalize_assistant_output(
    step_key: str,
    output: dict[str, Any],
    *,
    latest_by_step: dict[str, Any],
    base_draft: dict[str, Any],
    project_id: str | None = None,
) -> dict[str, Any]:
    reply = str(output.get("reply") or "").strip()
    suggestions = coerce_string_list(output.get("suggestions"))
    candidate_label = str(output.get("candidate_label") or "").strip()
    patch = {}
    if isinstance(output.get("candidate_patch"), dict):
        patch = _sanitize_step_patch(
            step_key,
            output.get("candidate_patch") or {},
            latest_by_step=latest_by_step,
            project_id=project_id or _project_id_from_steps(latest_by_step),
            base=base_draft,
            # 教练回复不能因为补丁多了一段就整条报废：违反数量契约的键丢弃，回复与建议照常送达。
            count_policy="drop",
        )
    return {
        "step_key": step_key,
        "reply": reply,
        "suggestions": suggestions,
        "candidate_label": candidate_label or None,
        "candidate_patch": patch or None,
        # 阶段 T：教练对作者意图要点的完整重述；没有该键（旧提示词快照）→ None，本轮不动要点
        "brief_update": coerce_brief_update(output.get("brief_update")),
    }


def _normalize_triage_output(output: dict[str, Any], base_draft: dict[str, Any]) -> dict[str, Any]:
    base_items = _triage_skeleton_items(base_draft)
    updates = {}
    for item in output.get("items") or []:
        if not isinstance(item, dict):
            continue
        scene_id = str(item.get("scene_id") or "").strip()
        if not scene_id:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in {"pass", "maybe", "rewrite"}:
            continue
        updates[scene_id] = {
            "status": status,
            "notes": str(item.get("notes") or "").strip(),
            "missing_fields": coerce_string_list(item.get("missing_fields")),
            "fix_steps": coerce_string_list(item.get("fix_steps")),
            "repair_patch": _sanitize_scene_repair_patch(item.get("repair_patch") or {}),
        }
    items = []
    for item in base_items:
        scene_id = item["scene_id"]
        update = updates.get(scene_id, {})
        items.append(
            {
                **item,
                "status": update.get("status", item.get("status") or ""),
                "notes": update.get("notes", item.get("notes") or ""),
                "missing_fields": update.get("missing_fields", item.get("missing_fields") or []),
                "fix_steps": update.get("fix_steps", item.get("fix_steps") or []),
                "repair_patch": update.get("repair_patch", item.get("repair_patch") or {}),
            }
        )
    return {"items": items}


def _triage_skeleton_items(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """AI 分诊的逐场底稿：身份 + 规则层的判定、缺口与修法——模型没判到（或给了非法判定）的场就是它。

    2026-09-30（B06-20）：AI 分诊 fail-closed 之后这里只是底稿，不再是「LLM 关闭时的规则回退」：不带规则套话
    备注，也不带修复例句（``SCENE_FIELD_EXAMPLES`` 写进字段就是占位）当修复补丁。
    """
    items = []
    for index, raw_scene in enumerate(draft.get("scenes") or [], start=1):
        if not isinstance(raw_scene, dict):
            continue
        diagnosis = diagnose_scene_detail(raw_scene, index=index)
        scene_type = str(diagnosis.get("primary_form") or diagnosis.get("scene_type") or "proactive").strip().lower() or "proactive"
        items.append(
            {
                "scene_id": str(raw_scene.get("scene_id") or f"scene_{index:02d}"),
                "title": str(raw_scene.get("title") or raw_scene.get("summary") or f"Scene {index:02d}"),
                "primary_form": scene_type,
                "scene_type": scene_type,
                "status": str(diagnosis.get("recommended_status") or "maybe"),
                "notes": "",
                "missing_fields": [str(field) for field in diagnosis.get("missing_fields") or []],
                "fix_steps": list(diagnosis.get("fix_steps") or []),
                "repair_patch": {},
            }
        )
    return items
