"""场景运行结果的装配：每个结果都带的公共键、QC 决定的摘要、准终稿两种 payload、警告合并、finality 四件套。

``near_final_result_payload`` 的形状随检查点冻结（``near_final_ready`` 的 ``near_final`` 引用与它的哈希），改键就是
改检查点；``near_evaluation_payload`` 同样（``near_eval{0,1}_payload``）。其余是运行结果的字段（React 读
``scene_status`` / ``quality_warnings`` / ``hard_qc`` / ``soft_qc`` / ``near_final`` 等）。

``recommended_actions`` 来自作者状态投影（``author_state.compute_author_state``，经 ``_with_author_projection`` 并进
结果）；只有归档的结果在准终稿留了警告时再追加 ``author_review_optional_fix``（归档了，改不改由作者）。严格模式的
停点（``quality_warning_pending_acceptance``）不追加（B01-15 的待定问题，定为不加）：它的投影已经是
``adopt_or_patch``——稿子没归档，作者本来就要读完警告再采纳或改，这一条已经涵盖「可选的修改」；停点带的每条准终稿
警告自己也写着 ``recommended_action``。前端的裁决条读的是工作台作者状态里的这份清单，不读运行结果顶层这一份。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def base_result(state, bundle: dict[str, Any]) -> dict[str, Any]:
    """管线走完或停下时的运行结果（硬 QC 停点、终选暂停、软 QC 人工复核、严格停点、归档）都以这五个键开头：场景状态、
    这次运行冻结的 bundle、当前 QC 报告与人工复核事件（B01-15）。已归档的重放只报运行状态上的归档指针、终选后续跑撞上
    预算边界只报状态与 ``lifecycle_budget_block``，这两种结果的形状照旧、不用它。"""
    return {
        "scene_status": state.scene_status,
        "current_bundle_id": bundle["bundle_id"],
        "current_bundle_hash": bundle["bundle_snapshot_hash"],
        "current_qc_report_id": state.current_qc_report_id,
        "current_human_review_event_id": state.current_human_review_event_id,
    }


def qc_decision_payload(decision) -> dict[str, str | None]:
    """硬 / 软 QC 决定的摘要（运行结果的 ``hard_qc`` / ``soft_qc``）。硬 QC 的 ``hard_qc_decision`` 检查点哈希覆盖这几个
    键与值（再加 should_continue / llm_call_id / execution_step_key；哈希按排好序的键算）：改键就是改哈希。"""
    return {
        "branch": decision.branch,
        "qc_report_id": decision.qc_report_id,
        "human_review_event_id": decision.human_review_event_id,
        "resolution_code": decision.resolution_code,
        "next_action": decision.next_action,
        "stop_reason": decision.stop_reason,
    }


def near_evaluation_payload(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "near_final_status": result.get("near_final_status"),
        "pass_flag": bool(result.get("pass_flag")),
        "overall_score": result.get("overall_score"),
        "scores": deepcopy(result.get("scores") or {}),
        "failure_class": result.get("failure_class"),
        "requires_human_review": bool(result.get("requires_human_review")),
        "evaluation_id": result.get("evaluation_id"),
        "revision_candidate_id": result.get("revision_candidate_id"),
        "should_rewrite": bool(result.get("should_rewrite")),
        "findings": deepcopy(result.get("findings") or []),
        "revision_brief": deepcopy(result.get("revision_brief") or []),
    }


def near_final_result_payload(
    near_final: dict[str, Any],
    *,
    rewrite_count: int,
    rewrite_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "near_final_status": near_final.get("near_final_status"),
        "pass_flag": bool(near_final.get("pass_flag")),
        "overall_score": near_final.get("overall_score"),
        "failure_class": near_final.get("failure_class"),
        "requires_human_review": bool(near_final.get("requires_human_review")),
        "evaluation_id": near_final.get("evaluation_id"),
        "revision_candidate_id": near_final.get("revision_candidate_id"),
        "should_rewrite": bool(near_final.get("should_rewrite")),
        "rewrite_count": rewrite_count,
        "findings": near_final.get("findings") or [],
        "revision_brief": near_final.get("revision_brief") or [],
    }
    if rewrite_gate is not None:
        # v2（W5）：重写稿 styled-draft gate 小结只在有绑定且产生过重写时出现；
        # rejected=True 表示重写稿因抄袭被丢弃、终稿回退为来源稿。
        payload["rewrite_style_gate"] = deepcopy(rewrite_gate)
    return payload


def merged_warnings(
    existing: Any, additions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = [item for item in (existing or []) if isinstance(item, dict)]
    merged.extend(additions)
    return merged


def apply_finality(result: dict, *, gate_summary: dict, warnings) -> None:
    # finality 四件套唯一装配点：顶层三布尔与 finality 镜像必须同源同值。
    result["safe_to_archive"] = bool(
        gate_summary.get("safe_to_archive", gate_summary.get("archivable", False))
    )
    result["literary_warnings_unresolved"] = bool(
        gate_summary.get("literary_warnings_unresolved") or warnings
    )
    result["author_confirmed_final"] = bool(
        gate_summary.get("author_confirmed_final")
    )
    result["finality"] = {
        "safe_to_archive": result["safe_to_archive"],
        "literary_warnings_unresolved": result["literary_warnings_unresolved"],
        "author_confirmed_final": result["author_confirmed_final"],
    }


def soft_risk_acceptance_event_id(soft_qc) -> str | None:
    stop_reason = str(getattr(soft_qc, "stop_reason", "") or "")
    prefix = "accepted_soft_risk:"
    if not stop_reason.startswith(prefix):
        return None
    event_id = stop_reason[len(prefix) :].strip()
    return event_id or None
