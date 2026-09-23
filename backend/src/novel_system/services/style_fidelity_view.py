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
    QcReport,
    SceneCard,
    SceneRunState,
    StyleFidelityReading,
)
from novel_system.services.style_reference import readings
from novel_system.services.style_reference.card import DIMENSION_LABELS
from novel_system.services.style_reference.style_step import STYLE_PATCH_KEEP_STEP

MAX_DECISIONS = 10


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
    """这一场最近的风格步决定与补丁去留（新 → 旧）。"""
    out: list[dict[str, Any]] = []
    for attempt in _attempts(session, scene_id, ("style_draft", STYLE_PATCH_KEEP_STEP)):
        if attempt.step == STYLE_PATCH_KEEP_STEP:
            out.append(_patch_decision(attempt))
        else:
            decision = _style_step_decision(attempt)
            if decision is not None:
                out.append(decision)
        if len(out) >= limit:
            break
    return out


def scene_judge(session: Session, scene_id: str, *, profile_id: str | None = None) -> dict[str, Any] | None:
    """这一场最近的参考评审分：最近一条带评审分的读数（对照检查 / 管线补丁），没有 → 最近一份软 QC 的参考评审。"""
    stmt = select(StyleFidelityReading).where(
        StyleFidelityReading.scene_id == scene_id,
        StyleFidelityReading.judge_json.is_not(None),
    )
    if profile_id:
        stmt = stmt.where(StyleFidelityReading.profile_id == profile_id)
    row = session.scalars(
        stmt.order_by(StyleFidelityReading.created_at.desc(), StyleFidelityReading.reading_id.desc()).limit(1)
    ).first()
    if row is not None and isinstance(row.judge_json, Mapping):
        return {**dict(row.judge_json), "reading_id": row.reading_id, "reading_source": row.source}
    for report in session.scalars(
        select(QcReport)
        .where(QcReport.scene_id == scene_id, QcReport.qc_type == "soft_qc")
        .order_by(QcReport.created_at.desc(), QcReport.qc_report_id.desc())
        .limit(20)
    ):
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
        "judge": scene_judge(session, scene.scene_id, profile_id=profile_id),
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
    step_attempt = next(
        (
            attempt
            for attempt in _attempts(session, scene_id, ("style_draft",), bundle_id=bundle_id)
            if isinstance((attempt.details_json or {}).get("style_step"), Mapping)
        ),
        None,
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
]
