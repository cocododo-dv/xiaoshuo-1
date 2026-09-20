"""author_state 投影（结果闭环治理 §5.3）。

把内部管线状态（SceneRunState.scene_status + run job + 草稿/归档行）投影成
作者可见的稳定枚举，React 只消费该字段。迁移期由 API 计算、不落列（§6.1）。

判定先分「有稿性」——枚举必须能表示「无稿」（G-01"跑完但无稿"的落点）：
- 无稿 → 空稿三态：not_started / generating / generation_failed(+recovery_action)
- 有稿 → draft_ready / quality_warning / awaiting_author_choice / hard_blocked / archived

blocking_findings / quality_warnings 在本阶段（Wave 1）只从 scene_status 粗粒度
透出，Q0–Q3 分级精化随质量分类器落地（Wave 2）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterRunJob, FinalScene, QcReport, SceneDraft, SceneRunState

# scene_status → 有稿态映射（不在表内且有稿 → draft_ready）
# Wave 2（§5.4）：硬阻断词值只是候选——是否真 hard_blocked 由当前 QC 报告里
# verified Q0/Q1 的分级条目决定；状态词残留但无阻断证据 → quality_warning
# （有稿可接管），落实「只有真实 Q0/Q1 能阻断归档」。
_HARD_BLOCKED_STATUSES = frozenset(
    {
        "hard_qc_partial_rewrite_required",
        "hard_qc_full_rewrite_required",
        "human_review_required",
        "needs_replan",
    }
)
_QUALITY_WARNING_STATUSES = frozenset(
    {
        "soft_qc_patch_required",
        "near_final_revision_required",
        "soft_qc_passed_with_notes",
        "quality_warning_pending_acceptance",
    }
)

_ACTIVE_JOB_STATUSES = frozenset({"queued", "running", "cancel_requested"})

# error_code → generation_failed 的 recovery_action（§5.3：必须携带明确恢复动作）
_SCENE_CARD_ERROR_CODES = frozenset(
    {
        "SCENE_EXECUTION_CONTRACT_BLOCKED",
        "SCENE_CARD_INCOMPLETE",
    }
)


def compute_author_state(
    session: Session,
    scene_id: str,
    state: SceneRunState | None = None,
) -> dict[str, Any]:
    if state is None:
        state = session.get(SceneRunState, scene_id)

    latest_valid = _resolve_valid_draft_row_id(session, state)
    final_row_id = state.current_final_scene_row_id if state else None
    has_draft = latest_valid is not None or _final_scene_has_content(session, final_row_id)
    scene_status = state.scene_status if state else "ready"

    if not has_draft:
        return _empty_draft_projection(session, scene_id, scene_status, state)

    # Wave 2（D6）：从当前 QC 报告的分级条目精化 blocking/warning（§6.1 结构）
    classified_blocking, classified_warnings = _classified_findings(session, state)

    if scene_status == "archived" and final_row_id:
        author_state = "archived"
        can_archive = False
    elif scene_status in ("critical_scene_human_gate", "awaiting_candidate_selection"):
        # awaiting_candidate_selection：Wave 3 候选终选暂停态（§5.3 可归档=否）
        author_state = "awaiting_author_choice"
        can_archive = False
    elif scene_status in _HARD_BLOCKED_STATUSES:
        if classified_blocking:
            author_state = "hard_blocked"
            can_archive = False
        else:
            # 阻断状态词残留但报告里没有 verified Q0/Q1 —— 有稿即可接管
            #（含 Wave 2 前的历史行）：只有真实 Q0/Q1 能阻断归档。
            author_state = "quality_warning"
            can_archive = True
    elif scene_status in _QUALITY_WARNING_STATUSES:
        author_state = "quality_warning"
        can_archive = True
    else:
        author_state = "draft_ready"
        can_archive = True

    blocking_findings: list[dict[str, Any]] = []
    quality_warnings: list[dict[str, Any]] = []
    recommended_actions: list[str] = []
    if author_state == "hard_blocked":
        blocking_findings.extend(classified_blocking or [{"kind": "scene_status", "value": scene_status}])
        quality_warnings.extend(classified_warnings)
        recommended_actions.append("review_pipeline_gate")
    elif author_state == "quality_warning":
        quality_warnings.extend(classified_warnings or [{"kind": "scene_status", "value": scene_status}])
        recommended_actions.append("adopt_or_patch")
    elif author_state == "awaiting_author_choice":
        recommended_actions.append("select_candidate")
    elif author_state == "archived" and classified_warnings:
        quality_warnings.extend(classified_warnings)

    return {
        "author_state": author_state,
        "latest_valid_draft_row_id": latest_valid,
        "current_final_scene_row_id": final_row_id,
        "blocking_findings": blocking_findings,
        "quality_warnings": quality_warnings,
        "recommended_actions": recommended_actions,
        "can_edit": True,
        "can_archive": can_archive,
        "recovery_action": None,
    }


def _empty_draft_projection(
    session: Session,
    scene_id: str,
    scene_status: str,
    state: SceneRunState | None,
) -> dict[str, Any]:
    latest_job = _latest_scene_job(session, scene_id)
    if latest_job is not None and latest_job.status in _ACTIVE_JOB_STATUSES:
        author_state = "generating"
        recovery_action = None
        recommended_actions: list[str] = []
    elif scene_status != "ready" or (
        latest_job is not None and latest_job.status in {"failed", "blocked", "cancelled"}
    ):
        # 离开过管线（同步 run 无 job 行）或 job 以失败终态结束，且库里无任何有效稿
        # ——G-01 的可表示状态：不是「有稿待审」，是「生成失败无稿」
        author_state = "generation_failed"
        recovery_action = _recovery_action_for(latest_job)
        recommended_actions = [recovery_action]
    else:
        author_state = "not_started"
        recovery_action = None
        recommended_actions = []

    return {
        "author_state": author_state,
        "latest_valid_draft_row_id": None,
        "current_final_scene_row_id": state.current_final_scene_row_id if state else None,
        "blocking_findings": [],
        "quality_warnings": [],
        "recommended_actions": recommended_actions,
        "can_edit": False,
        "can_archive": False,
        "recovery_action": recovery_action,
    }


def _classified_findings(
    session: Session,
    state: SceneRunState | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """当前 QC 报告里已分级（§6.1）条目 → (blocking, warnings) 瘦身形态。"""
    if state is None or not state.current_qc_report_id:
        return [], []
    report = session.get(QcReport, state.current_qc_report_id)
    if report is None:
        return [], []
    blocking: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for issue in report.issues_json or []:
        if not isinstance(issue, dict) or not issue.get("quality_level"):
            continue
        slim = _slim_quality_issue(issue)
        if issue.get("blocking"):
            blocking.append(slim)
        else:
            warnings.append(slim)
    return blocking, warnings


def _slim_quality_issue(issue: dict[str, Any]) -> dict[str, Any]:
    """作者态五键瘦身投影（issue_key/quality_level/message/recommended_action/verified_by）。"""
    return {
        "issue_key": issue.get("issue_key"),
        "quality_level": issue.get("quality_level"),
        "message": issue.get("message") or issue.get("human_readable_reason") or "",
        "recommended_action": issue.get("recommended_action"),
        "verified_by": issue.get("verified_by"),
    }


def _recovery_action_for(job: ChapterRunJob | None) -> str:
    error_code = str(job.error_code or "") if job else ""
    upper = error_code.upper()
    if "LLM" in upper or "PROVIDER" in upper or "API_KEY" in upper:
        return "configure_llm"
    if upper in _SCENE_CARD_ERROR_CODES or "MISSING_FIELD" in upper:
        return "complete_scene_card"
    return "retry"


def _latest_scene_job(session: Session, scene_id: str) -> ChapterRunJob | None:
    return session.execute(
        select(ChapterRunJob)
        .where(
            ChapterRunJob.job_type == "scene_run_full",
            ChapterRunJob.scene_id == scene_id,
        )
        .order_by(ChapterRunJob.created_at.desc(), ChapterRunJob.job_id.desc())
    ).scalars().first()


def _resolve_valid_draft_row_id(session: Session, state: SceneRunState | None) -> str | None:
    """按 latest_valid > style > neutral 的顺序解析第一个内容非空的草稿行。"""
    if state is None:
        return None
    for row_id in (
        getattr(state, "latest_valid_draft_row_id", None),
        state.current_style_draft_row_id,
        state.current_neutral_draft_row_id,
    ):
        if not row_id:
            continue
        draft = session.get(SceneDraft, row_id)
        if draft is not None and (draft.content or "").strip():
            return row_id
    return None


def _final_scene_has_content(session: Session, final_row_id: str | None) -> bool:
    if not final_row_id:
        return False
    final = session.get(FinalScene, final_row_id)
    return final is not None and bool((final.content or "").strip())
