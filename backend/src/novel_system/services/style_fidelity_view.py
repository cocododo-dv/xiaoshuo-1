"""风格参考 v3（P5b，N5）—「像不像」读数的接口形状：一场、一部作品、一次运行。

读数入库只有 ``style_reference.readings.record_fidelity_reading`` 一个入口；这里只读，给三个地方用：

- ``GET /api/v1/scenes/{scene_id}/style-fidelity``（:func:`scene_style_fidelity`）：这一场每个阶段最新的读数、风格步与
  补丁的决定、最近的参考评审分；
- ``GET /api/v1/projects/{project_id}/style-fidelity``（:func:`project_style_fidelity`）：作品的读数走势、近期常见偏差、
  按维平均；
- 起草台工作台的生成摘要（:func:`current_run_style_fidelity`）：**本次运行**（bundle）的首稿读数、风格步的决定、修改稿
  读数、补丁的去留、终稿读数、评审分。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AttemptTracker,
    HumanReviewEvent,
    QcReport,
    SceneBundle,
    SceneCard,
    SceneRunState,
    StyleFidelityReading,
)
from novel_system.services.style_reference import readings
from novel_system.services.style_reference.card import DIMENSION_LABELS
from novel_system.services.style_reference.style_step import PATCH_DECISION_REVERTED, STYLE_PATCH_KEEP_STEP

MAX_DECISIONS = 10


def selected_style_row_id(session: Session, scene_id: str, bundle_id: str | None) -> str | None:
    """这次运行（bundle）**选中**的那一份风格稿行（Best-of-N 时不是最后一个槽位，L7）。

    这次运行的风格稿尝试行（``step="style_draft"``）里出现过的 ``row_id`` 才算候选；按下面的顺序认：

    1. 作者在关键场景终选门里选中的那一份（``candidate_selection`` 事件的 ``selected_row_id``）；
    2. 软 QC 第一轮评审的来源稿——选中的稿子进入软 QC（补丁之后运行态指针会移到补丁稿，这一条不会）；
    3. 运行态当前 / 最近有效的风格稿指针（风格步刚结束、还没进软 QC 时，它就是排第一的候选）。

    都认不出（没有这次运行的风格稿、旧运行）→ ``None``，调用方退回「最近一次」。"""
    if not bundle_id:
        return None
    candidates = {
        str((attempt.details_json or {}).get("row_id") or "")
        for attempt in _attempts(session, scene_id, ("style_draft",), bundle_id=bundle_id)
    }
    candidates.discard("")
    if not candidates:
        return None
    if len(candidates) == 1:
        return next(iter(candidates))
    for event in session.scalars(
        select(HumanReviewEvent)
        .where(HumanReviewEvent.scene_id == scene_id, HumanReviewEvent.event_source == "candidate_selection")
        .order_by(HumanReviewEvent.created_at.desc(), HumanReviewEvent.event_id.desc())
    ):
        selected = str((event.details_json or {}).get("selected_row_id") or "")
        if selected in candidates:
            return selected
    first_review = session.scalars(
        select(QcReport)
        .where(
            QcReport.scene_id == scene_id,
            QcReport.qc_type == "soft_qc",
            QcReport.source_bundle_id == bundle_id,
            QcReport.source_draft_row_id.in_(candidates),
        )
        .order_by(QcReport.created_at.asc(), QcReport.qc_report_id.asc())
        .limit(1)
    ).first()
    if first_review is not None:
        return str(first_review.source_draft_row_id)
    state = session.get(SceneRunState, scene_id)
    for pointer in (
        getattr(state, "current_style_draft_row_id", None),
        getattr(state, "latest_valid_draft_row_id", None),
    ) if state is not None else ():
        if pointer and str(pointer) in candidates:
            return str(pointer)
    return None


def selected_style_attempt(session: Session, scene_id: str, bundle_id: str | None) -> AttemptTracker | None:
    """这次运行选中的那一份风格稿的尝试行（带风格步的决定与 notices）；认不出选中稿时取最近一次。"""
    attempts = _attempts(session, scene_id, ("style_draft",), bundle_id=bundle_id)
    if not attempts:
        return None
    selected = selected_style_row_id(session, scene_id, bundle_id)
    if selected:
        for attempt in attempts:
            if str((attempt.details_json or {}).get("row_id") or "") == selected:
                return attempt
    return attempts[0]


def _bundle_bound(session: Session, bundle_id: str | None) -> bool:
    """这次运行的 bundle 冻结的是不是一份风格绑定（没有 bundle 行的旧数据按「不确定」放过，不在这里拦）。"""
    if not bundle_id:
        return True
    row = session.get(SceneBundle, bundle_id)
    if row is None:
        return True
    from novel_system.services.style_policy import style_policy_for_bundle

    try:
        return bool(style_policy_for_bundle(row.frozen_snapshot_json).bound)
    except Exception:  # noqa: BLE001 — 只读展示：判不了按不确定
        return True


def _reverted_patch_refs(session: Session, scene_id: str) -> tuple[set[str], set[str]]:
    """被退回的补丁留下的（补丁后读数 id, 补丁后软 QC 报告 id）：它们评的是没有采用的那一稿，不能当这一场的评审分。"""
    readings_ids: set[str] = set()
    report_ids: set[str] = set()
    for attempt in _attempts(session, scene_id, (STYLE_PATCH_KEEP_STEP,)):
        details = attempt.details_json or {}
        if details.get("decision") != PATCH_DECISION_REVERTED:
            continue
        after = details.get("after_reading") if isinstance(details.get("after_reading"), Mapping) else {}
        if after.get("reading_id"):
            readings_ids.add(str(after["reading_id"]))
        if details.get("after_qc_report_id"):
            report_ids.add(str(details["after_qc_report_id"]))
    return readings_ids, report_ids


def _policy_payload(policy: Any) -> dict[str, Any]:
    payload = dict(policy.audit()) if hasattr(policy, "audit") else {}
    payload["book_id"] = getattr(policy, "book_id", None)
    payload["dimension_states"] = {
        dim: state
        for dim, state in dict(getattr(policy, "dimension_states", None) or {}).items()
        if state != "normal"
    }
    return payload


def _reading(session: Session, reading_id: Any) -> dict[str, Any] | None:
    if not isinstance(reading_id, str) or not reading_id:
        return None
    return readings.reading_payload(session.get(StyleFidelityReading, reading_id))


def _report_judge(report: QcReport | None) -> dict[str, Any] | None:
    for entry in (report.rewrite_brief_json or []) if report is not None else []:
        if isinstance(entry, dict) and entry.get("kind") == "reference_judge":
            judge = readings.normalize_judge(entry, source="soft_qc")
            if judge is not None:
                judge["qc_report_id"] = report.qc_report_id
            return judge
    return None


def _style_step_decision(attempt: AttemptTracker) -> dict[str, Any] | None:
    details = attempt.details_json or {}
    step = details.get("style_step")
    if not isinstance(step, Mapping):
        return None
    first = step.get("first_reading") if isinstance(step.get("first_reading"), Mapping) else {}
    revision = step.get("revision_reading") if isinstance(step.get("revision_reading"), Mapping) else {}
    dimensions = [str(dim) for dim in step.get("dimensions") or []]
    return {
        "kind": "style_step",
        "attempt_id": attempt.attempt_id,
        "bundle_id": attempt.source_bundle_id,
        "row_id": details.get("row_id"),
        "decision": step.get("decision"),
        "reason": step.get("reason"),
        "gate_reason": step.get("gate_reason"),
        "llm_call": bool(step.get("llm_call")),
        "dimensions": dimensions,
        "dimension_labels": [DIMENSION_LABELS.get(dim, dim) for dim in dimensions],
        "differences": list(step.get("differences") or []),
        "first_reading_id": first.get("reading_id"),
        "first_distance": first.get("distance"),
        "first_percentile": first.get("percentile"),
        "revision_reading_id": revision.get("reading_id"),
        "revision_distance": revision.get("distance"),
        "revision_percentile": revision.get("percentile"),
        "content_source": details.get("content_source"),
        "created_at": attempt.created_at,
    }


def _patch_decision(attempt: AttemptTracker) -> dict[str, Any]:
    details = attempt.details_json or {}
    before = details.get("before_reading") if isinstance(details.get("before_reading"), Mapping) else {}
    after = details.get("after_reading") if isinstance(details.get("after_reading"), Mapping) else {}
    before_judge = details.get("before_judge") if isinstance(details.get("before_judge"), Mapping) else {}
    after_judge = details.get("after_judge") if isinstance(details.get("after_judge"), Mapping) else {}
    return {
        "kind": "patch",
        "attempt_id": attempt.attempt_id,
        "bundle_id": attempt.source_bundle_id,
        "decision": details.get("decision"),
        "reason": details.get("reason"),
        "source_draft_row_id": details.get("source_draft_row_id"),
        "restored_row_id": details.get("restored_row_id"),
        "before_judge": before_judge.get("style_score"),
        "after_judge": after_judge.get("style_score"),
        "before_distance": before.get("distance"),
        "after_distance": after.get("distance"),
        "reading_id": after.get("reading_id"),
        "before_qc_report_id": details.get("before_qc_report_id"),
        "after_qc_report_id": details.get("after_qc_report_id"),
        "created_at": attempt.created_at,
    }


def _attempts(session: Session, scene_id: str, steps: tuple[str, ...], bundle_id: str | None = None) -> list[AttemptTracker]:
    stmt = select(AttemptTracker).where(
        AttemptTracker.scene_id == scene_id,
        AttemptTracker.step.in_(steps),
        AttemptTracker.status == "completed",
    )
    if bundle_id:
        stmt = stmt.where(AttemptTracker.source_bundle_id == bundle_id)
    return list(session.scalars(stmt.order_by(AttemptTracker.attempt_id.desc())))


def scene_decisions(session: Session, scene_id: str, *, limit: int = MAX_DECISIONS) -> list[dict[str, Any]]:
    """这一场最近的风格步决定与补丁去留（新 → 旧）。Best-of-N 的一次运行只列选中的那一份候选的风格步（L7）。"""
    out: list[dict[str, Any]] = []
    selected_by_bundle: dict[str, str | None] = {}
    for attempt in _attempts(session, scene_id, ("style_draft", STYLE_PATCH_KEEP_STEP)):
        if attempt.step == STYLE_PATCH_KEEP_STEP:
            out.append(_patch_decision(attempt))
        else:
            bundle_id = str(attempt.source_bundle_id or "")
            if bundle_id and bundle_id not in selected_by_bundle:
                selected_by_bundle[bundle_id] = selected_style_row_id(session, scene_id, bundle_id)
            selected = selected_by_bundle.get(bundle_id)
            if selected and str((attempt.details_json or {}).get("row_id") or "") != selected:
                continue
            decision = _style_step_decision(attempt)
            if decision is not None:
                out.append(decision)
        if len(out) >= limit:
            break
    return out


def scene_judge(session: Session, scene_id: str, *, profile_id: str | None = None) -> dict[str, Any] | None:
    """这一场最近的参考评审分：最近一条带评审分的读数（对照检查 / 管线补丁），没有 → 最近一份软 QC 的参考评审。

    被退回的补丁留下的评审（补丁后那一轮）评的是没有采用的稿子，跳过（L7）。"""
    reverted_readings, reverted_reports = _reverted_patch_refs(session, scene_id)
    stmt = select(StyleFidelityReading).where(
        StyleFidelityReading.scene_id == scene_id,
        StyleFidelityReading.judge_json.is_not(None),
    )
    if profile_id:
        stmt = stmt.where(StyleFidelityReading.profile_id == profile_id)
    for row in session.scalars(
        stmt.order_by(StyleFidelityReading.created_at.desc(), StyleFidelityReading.reading_id.desc()).limit(20)
    ):
        if row.reading_id in reverted_readings or not isinstance(row.judge_json, Mapping):
            continue
        return {**dict(row.judge_json), "reading_id": row.reading_id, "reading_source": row.source}
    for report in session.scalars(
        select(QcReport)
        .where(QcReport.scene_id == scene_id, QcReport.qc_type == "soft_qc")
        .order_by(QcReport.created_at.desc(), QcReport.qc_report_id.desc())
        .limit(20)
    ):
        if report.qc_report_id in reverted_reports:
            continue
        judge = _report_judge(report)
        if judge is not None:
            return judge
    return None


def scene_style_fidelity(session: Session, scene: SceneCard) -> dict[str, Any]:
    """``GET /api/v1/scenes/{scene_id}/style-fidelity``。"""
    from novel_system.services.style_policy import style_policy_for_scene

    policy = style_policy_for_scene(session, scene)
    profile_id = policy.profile_id if policy.bound else None
    latest = readings.latest_scene_readings(session, scene.scene_id, profile_id=profile_id)
    return {
        "scene_id": scene.scene_id,
        "bound": bool(policy.bound),
        "policy": _policy_payload(policy),
        "readings": {stage: readings.reading_payload(latest.get(stage)) for stage in readings.STAGES},
        "decisions": scene_decisions(session, scene.scene_id),
        # 没绑定的场景没有「参考评审」可言（L6：润色口径的软 QC 顺手给的分不是像不像）
        "judge": scene_judge(session, scene.scene_id, profile_id=profile_id) if policy.bound else None,
    }


def project_style_fidelity(session: Session, project_id: str) -> dict[str, Any]:
    """``GET /api/v1/projects/{project_id}/style-fidelity``（作品当前的绑定 = project / global 层）。"""
    from novel_system.services.style_policy import style_policy_live

    policy = style_policy_live(
        session,
        SimpleNamespace(project_id=project_id, scene_id=None, pov_character_id=None, onstage_chars_json=[]),
        freeze_contract=False,
    )
    profile_id = policy.profile_id if policy.bound else None
    return {
        "project_id": project_id,
        "bound": bool(policy.bound),
        "profile_id": profile_id,
        "policy": _policy_payload(policy),
        **readings.project_fidelity_summary(session, project_id, profile_id=profile_id),
    }


def current_run_style_fidelity(session: Session, scene_id: str, bundle_id: str | None) -> dict[str, Any] | None:
    """工作台生成摘要里的 ``style_fidelity``：本次运行（bundle）的首稿读数 → 风格步的决定 → 修改稿读数 → 补丁的去留
    → 终稿读数 → 评审分。这一场这次运行没有任何读数 / 决定（未绑定、neutral_first 的旧运行）→ ``None``。"""
    if not bundle_id:
        return None
    # L7：Best-of-N 时取**选中**的那一份候选的风格步（不是最后一个槽位）
    step_attempts = [
        attempt
        for attempt in _attempts(session, scene_id, ("style_draft",), bundle_id=bundle_id)
        if isinstance((attempt.details_json or {}).get("style_step"), Mapping)
    ]
    selected_row = selected_style_row_id(session, scene_id, bundle_id)
    step_attempt = next(
        (
            attempt
            for attempt in step_attempts
            if selected_row and str((attempt.details_json or {}).get("row_id") or "") == selected_row
        ),
        step_attempts[0] if step_attempts else None,
    )
    patch_attempt = next(iter(_attempts(session, scene_id, (STYLE_PATCH_KEEP_STEP,), bundle_id=bundle_id)), None)
    style_step = _style_step_decision(step_attempt) if step_attempt is not None else None
    patch = _patch_decision(patch_attempt) if patch_attempt is not None else None
    if patch is not None:
        patch["reading"] = _reading(session, patch.get("reading_id"))
    state = session.get(SceneRunState, scene_id)
    final = None
    final_row_id = getattr(state, "current_final_scene_row_id", None) if state is not None else None
    if final_row_id:
        row = session.scalars(
            select(StyleFidelityReading)
            .where(
                StyleFidelityReading.scene_id == scene_id,
                StyleFidelityReading.stage == readings.STAGE_FINAL,
                StyleFidelityReading.draft_ref == final_row_id,
            )
            .order_by(StyleFidelityReading.created_at.desc())
            .limit(1)
        ).first()
        final = readings.reading_payload(row)
    judge = None
    if _bundle_bound(session, bundle_id):
        if patch is not None and patch.get("decision") == PATCH_DECISION_REVERTED and patch.get("before_qc_report_id"):
            # L7：补丁被退回——这一场留下的是补丁前的稿子，给补丁前那一轮评审（补丁后那轮评的是没采用的稿子）
            judge = _report_judge(session.get(QcReport, str(patch["before_qc_report_id"])))
        else:
            for report in session.scalars(
                select(QcReport)
                .where(
                    QcReport.scene_id == scene_id,
                    QcReport.qc_type == "soft_qc",
                    QcReport.source_bundle_id == bundle_id,
                )
                .order_by(QcReport.created_at.desc(), QcReport.qc_report_id.desc())
            ):
                judge = _report_judge(report)
                if judge is not None:
                    break
    first = _reading(session, (style_step or {}).get("first_reading_id"))
    revision = _reading(session, (style_step or {}).get("revision_reading_id"))
    if not any((style_step, patch, final, judge, first, revision)):
        return None
    return {
        "first_draft": first,
        "style_step": style_step,
        "revision": revision,
        "patch": patch,
        "final": final,
        "judge": judge,
    }


__all__ = [
    "current_run_style_fidelity",
    "project_style_fidelity",
    "scene_decisions",
    "scene_judge",
    "scene_style_fidelity",
    "selected_style_attempt",
    "selected_style_row_id",
]
