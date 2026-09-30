"""「像不像」读数的探针：在保存点里读、失败按读不出处理、记首稿读数、比两稿有没有越改越远。

读数是观察，永远不能弄坏管线（第一次读一本书要先建窗口索引、写库）：任何失败只回滚保存点、记日志，会话照样可用。
编排器的两处同样的比较（准终稿改写、软补丁的去留）以后也走这里。``rewrite_drift`` 是测试替换的接缝：流程模块按
``fidelity_probe.rewrite_drift`` 在调用时取。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_reference import readings as style_readings
from novel_system.services.style_reference import style_step

_LOGGER = logging.getLogger(__name__)


def observe(session: Session, policy: Any, text: str, *, ref: str, what: str) -> tuple[Any, str | None]:
    """读一段文字的「像不像」读数 → (读数或 None, 出错时的错误码)。

    读数是观察，永远不能弄坏管线：在保存点里读（第一次读一本书时要建窗口索引、写库），任何失败只回滚这个保存点、
    记日志——会话照样可用，后面的落库与检查点照常（风格参考 v3 L3：否则一次已派发的调用会落不下检查点，续跑报
    ``RUN_CHECKPOINT_OUTPUT_MISSING``）。"""
    try:
        with session.begin_nested():
            return style_readings.reading_for_text(session, policy, text), None
    except Exception as exc:  # noqa: BLE001 — 读数是观察：失败按读不出处理
        _LOGGER.warning("%s fidelity reading failed (%s)", what, ref, exc_info=True)
        return None, str(getattr(exc, "code", None) or type(exc).__name__)


def record_first_draft_reading(
    session: Session,
    scene: SceneCard,
    policy: Any,
    first_row_id: str,
    first_content: str,
    thresholds: Any,
) -> tuple[Any, str | None, str | None]:
    """读首稿并记一条 first_draft 读数（同一稿行幂等）→ (读数, 读数行 id, 读数出错时的错误码)。

    读数失败只记日志、按「读不出」处理（保留首稿），但错误码单独返回（L8）：「读数出错」与「参考书没有可用的
    尺子」在决定与提示里分开说。读数在保存点里读（L3）：它可能要先给这本书建窗口索引、写库，失败只回滚保存点，
    不弄坏会话。"""
    reading, error_code = observe(session, policy, first_content, ref=scene.scene_id, what="first-draft")
    if reading is None:
        return None, None, error_code
    row = style_readings.record_fidelity_reading(
        session,
        policy=policy,
        text=first_content,
        source=style_readings.SOURCE_PIPELINE,
        stage=style_readings.STAGE_FIRST_DRAFT,
        scene_id=scene.scene_id,
        project_id=style_readings.scene_project_id(session, scene),
        draft_ref=first_row_id,
        reading=reading,
        max_percentile=thresholds.style_step_max_percentile,
    )
    return reading, (row.reading_id if row is not None else None), None


def rewrite_drift(
    session: Session,
    *,
    policy_or_bundle: Any,
    source_content: str,
    rewritten_content: str,
) -> dict[str, Any]:
    """改写不退步（风格参考 v3 S2 d）：两稿各读一次「像不像」读数——

    ``regressed`` = 改写稿 distance > 来源稿 distance + ``patch_max_distance_increase``；``comparable`` = 两边读数都可信。
    未绑定 / 读不出 / 不可信 / 读数出错 → ``comparable=False``（``regressed`` 恒 False），消费方语义不变：去模板改写
    只在 ``regressed`` 时拒，风格挽救补丁在 ``comparable is not True`` 时不采用。读数在保存点里读，失败只回滚保存点。
    ``policy_or_bundle`` 收 StylePolicy 或 bundle（bundle 按其冻结契约解析策略）。
    """
    thresholds = style_step.fidelity_thresholds()
    audit: dict[str, Any] = {
        "version": "style_rewrite_drift_v1",
        "available": False,
        "comparable": False,
        "regressed": False,
        "max_distance_increase": float(thresholds.patch_max_distance_increase),
        "source": None,
        "rewritten": None,
    }
    try:
        policy = (
            policy_or_bundle
            if hasattr(policy_or_bundle, "bound")
            else style_policy_for_bundle(policy_or_bundle)
        )
        audit["runtime_contract_mode"] = getattr(policy, "mode", None)
        if not getattr(policy, "bound", False):
            audit["unavailable_reason"] = (
                getattr(policy, "error_code", None)
                or ("bundle_has_no_style_profile" if getattr(policy, "mode", None) == "absent" else "style_policy_unbound")
            )
            return audit

        readings_by_sha: dict[str, tuple[Any, str | None]] = {}

        def _read(text: str, what: str) -> tuple[Any, str | None]:
            sha = style_readings.text_sha256(text)
            if sha not in readings_by_sha:
                readings_by_sha[sha] = observe(session, policy, text, ref="rewrite drift", what=what)
            return readings_by_sha[sha]

        def _brief(reading: Any) -> dict[str, Any] | None:
            if reading is None:
                return None
            return {
                "distance": float(reading.distance),
                "percentile": float(reading.percentile),
                "reliable": bool(reading.reliable),
                "char_count": int(reading.char_count),
            }

        source_reading, source_error = _read(source_content, "source")
        rewritten_reading, rewritten_error = _read(rewritten_content, "rewritten")
        audit["source"] = _brief(source_reading)
        audit["rewritten"] = _brief(rewritten_reading)
        if source_reading is None or rewritten_reading is None:
            audit["unavailable_reason"] = "reading_failed" if (source_error or rewritten_error) else "reading_unavailable"
            return audit
        audit["available"] = True
        comparable = bool(source_reading.reliable) and bool(rewritten_reading.reliable)
        delta = float(rewritten_reading.distance) - float(source_reading.distance)
        audit["comparable"] = comparable
        audit["distance_delta"] = round(delta, 6)
        audit["regressed"] = bool(comparable and delta > float(thresholds.patch_max_distance_increase))
        if not comparable:
            audit["unavailable_reason"] = "reading_unreliable"
        return audit
    except Exception:  # noqa: BLE001 — optional evidence gate must fail open (comparable=False)
        _LOGGER.warning("style rewrite drift audit degraded", exc_info=True)
        return {**audit, "unavailable_reason": "style_drift_internal_error"}
