"""管线里的原文重合门：中性步位稿（硬质检）与风格稿（风格稿落库后、软质检、准定稿重写、风格直起的首稿）过的是
同一道门（:func:`run_reference_copy_gate`）。都走唯一抄袭门（reference_copy_gate），每次裁决记一行 MetricEvent；
门自己没查成时报 ``verdict="unavailable"``（与「无绑定」区分开），怎么处置由调用方定：硬质检照常往下走，软质检挂 Q2
要人工复核，起草链路发 STYLE_GATE_UNAVAILABLE。"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard
from novel_system.services.style_prompt_injection import (
    STYLED_GATE_UNAVAILABLE_VERDICT,
)

_LOGGER = logging.getLogger(__name__)


# 2026-09 风格模仿 v2（W5，规格 §2.W5.5）：styled-draft gate。
# - 中性稿上的 style gate 只保留确定性 n-gram 抄袭（Q0）裁决；quant / 生成禁用词不再对
#   中性稿做（中性稿没有注入任何参考风格，量化容差与禁用词对它没有意义）。
# - 风格稿（style_draft 落库后 + soft_qc 阶段）跑 plagiarism + 生成禁用词：抄袭命中
#   → Q0 `style_plagiarism`，走既有 human_review 升级路径（不允许软风险接受）；禁用词命中
#   → Q2 `reference_banned_term_replicated`，soft_qc 要求人工复核（作者可接受软风险）；
#   quant 结果只记诊断，不产 issue。风格参考 v3（H1）：禁用词读现行的表（与成稿门、抄袭门同一张），
#   冻结契约里的词只渲染提示词红线；成稿门对同样的命中只报不拦的警告。
STYLE_PLAGIARISM_ISSUE_KEY = "style_plagiarism"
STYLE_BANNED_TERM_ISSUE_KEY = "reference_banned_term_replicated"
# gate 自身没跑成（校验异常 / 契约损坏 / 参考书已删）：不是正文的错，但抄袭 / 禁用词检查
# 确实没有执行——Q2（非阻断）并要求人工复核。key 不能以 ``style_`` 开头：分类器把该前缀
# 一律视作 Q3 只诊断。
STYLE_GATE_UNAVAILABLE_ISSUE_KEY = "reference_style_gate_unavailable"
STYLE_VALIDATION_PLAGIARISM_TRIGGER = "style_validation_plagiarism"
STYLED_DRAFT_GATE_EVENT_KIND = "styled_draft_gate_decided"
# 每个可能成为终稿的风格化输出都要过 gate：style_draft 落库后、soft_qc 阶段（作用于进入
# soft_qc 的任何风格稿——含 de_template / salvage / soft patch 产物）、以及
# near_final_rewrite（带同一 [STYLE_REFERENCE] 前缀重写整场，输出直接成为终稿）。
# 2026-09-12 风格直起:style_first 下中性步位的首稿也是 provider 的风格化输出,同样过门
# (stage="neutral_draft";hard_qc 侧的 n-gram 门仍照跑,升级到人工复核由它完成)。
STYLED_DRAFT_GATE_STAGES: frozenset[str] = frozenset(
    {"neutral_draft", "style_draft", "soft_qc", "near_final_rewrite"}
)
_STYLED_GATE_MAX_HITS = 8


# 中性步位稿（中性稿 / style_first 首稿）在硬质检里过的那一道：同一道门，审计行沿用旧名；这一阶段只认原文重合，
# 受保护专名 / 生成禁用词不对它下判定（见上）。
HARD_QC_GATE_STAGE = "hard_qc"
HARD_QC_GATE_EVENT_KIND = "qc_gate_decided"
_GATE_EVENT_KINDS: dict[str, str] = {
    HARD_QC_GATE_STAGE: HARD_QC_GATE_EVENT_KIND,
    **{stage: STYLED_DRAFT_GATE_EVENT_KIND for stage in STYLED_DRAFT_GATE_STAGES},
}


def _scene_has_gate_scope(scene: Any) -> bool:
    """这一场有没有能挂风格绑定的作用域（作品 / 场景 / 视角角色 / 在场角色）；没有就谈不上绑定，门不跑。"""
    return bool(
        getattr(scene, "project_id", None)
        or getattr(scene, "scene_id", None)
        or getattr(scene, "pov_character_id", None)
        or (getattr(scene, "onstage_chars_json", None) or [])
    )


def _record_gate_event(
    session: Session,
    event_kind: str,
    *,
    scene_id: str | None,
    profile_id: str | None,
    binding_id: str | None,
    outcome: str,
    started_at: float,
    context: dict[str, Any],
) -> str:
    """管线里两道原文重合门（中性步位稿 ``qc_gate_decided`` / 风格稿 ``styled_draft_gate_decided``）的审计行。"""
    from novel_system.services.style_reference.metrics_recorder import MetricsRecorder

    return MetricsRecorder.record(
        session,
        event_kind,
        target_kind="scene",
        target_ref_id=scene_id,
        profile_id=profile_id,
        binding_id=binding_id,
        outcome=outcome,
        latency_ms=int((time.perf_counter() - started_at) * 1000),
        context=context,
    )


def _styled_gate_result(
    *,
    stage: str,
    report: Any,
    profile_id: str | None,
    binding_id: str | None,
    runtime_contract_hash: str | None,
    runtime_contract_mode: str | None,
) -> dict[str, Any]:
    """把风格稿门的读数(:func:`_styled_gate_report` 的形状)压成可入 AttemptTracker / notices 的诊断字典。

    抄袭命中只记位置、长度与指纹（不落匹配原文——那正是参考作品的原文）；禁用词命中记词本身
    （短、已在 banned_terms 表里）。
    """
    plagiarism = dict(getattr(report, "plagiarism_json", None) or {})
    raw_hits = plagiarism.get("hits") if isinstance(plagiarism.get("hits"), list) else []
    plagiarism_hits = [
        {
            "position": int(hit.get("position") or 0),
            "matched_length": int(hit.get("matched_length") or 0),
            "matched_sha256": str(hit.get("matched_sha256") or ""),
        }
        for hit in raw_hits[:_STYLED_GATE_MAX_HITS]
        if isinstance(hit, dict)
    ]
    forbidden_hits = [
        {
            "pattern_statement": str(hit.get("pattern_statement") or ""),
            "matched_excerpt": str(hit.get("matched_excerpt") or ""),
            "severity": str(hit.get("severity") or "error"),
        }
        for hit in (getattr(report, "forbidden_hits_json", None) or [])[
            :_STYLED_GATE_MAX_HITS
        ]
        if isinstance(hit, dict)
    ]
    verdict_obj = getattr(report, "verdict", None)
    verdict = str(getattr(verdict_obj, "value", verdict_obj) or "")
    return {
        "stage": stage,
        "verdict": verdict,
        "plagiarism_passed": bool(plagiarism.get("passed", True)),
        "plagiarism_hits": plagiarism_hits,
        "plagiarism_hit_count": len(raw_hits),
        "forbidden_hits": forbidden_hits,
        "forbidden_hit_count": len(getattr(report, "forbidden_hits_json", None) or []),
        "profile_id": profile_id,
        "binding_id": binding_id,
        "runtime_contract_hash": runtime_contract_hash,
        "runtime_contract_mode": runtime_contract_mode,
    }


def styled_gate_unavailable_result(
    *,
    stage: str,
    error: str,
    error_code: str | None = None,
    profile_id: str | None = None,
    binding_id: str | None = None,
    runtime_contract_hash: str | None = None,
    runtime_contract_mode: str | None = None,
) -> dict[str, Any]:
    """gate 未能执行时的诊断字典：与 ``_styled_gate_result`` 同形，``verdict="unavailable"``。

    调用方不得把它当成「无绑定」：风格稿带着样例前缀生成，抄袭 / 禁用词检查却没有跑过
    ——soft_qc 要挂 Q2 issue 并要求人工复核，scene_generation 要发 STYLE_GATE_UNAVAILABLE
    notice。只记异常类型名与契约错误码，不记异常文本（可能夹带参考原文）。
    """
    return {
        "stage": stage,
        "verdict": STYLED_GATE_UNAVAILABLE_VERDICT,
        "error": error,
        "error_code": error_code,
        "plagiarism_passed": None,
        "plagiarism_hits": [],
        "plagiarism_hit_count": None,
        "forbidden_hits": [],
        "forbidden_hit_count": None,
        "profile_id": profile_id,
        "binding_id": binding_id,
        "runtime_contract_hash": runtime_contract_hash,
        "runtime_contract_mode": runtime_contract_mode,
    }


def run_styled_draft_style_gate(
    session: Session,
    scene: SceneCard,
    text: str,
    *,
    stage: str = "style_draft",
    bundle: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """v2（规格 §2.W5.5）styled-draft gate：对**已风格化**文本跑抄袭 + 生成禁用词。

    只收风格化输出的阶段（``STYLED_DRAFT_GATE_STAGES``）；门本身见 :func:`run_reference_copy_gate`，每次裁决
    （含 gate 自身失败）写一行 ``styled_draft_gate_decided`` MetricEvent。
    """
    if stage not in STYLED_DRAFT_GATE_STAGES:
        raise ValueError(f"unknown styled-draft gate stage: {stage}")
    return run_reference_copy_gate(session, scene, text, stage=stage, bundle=bundle)


def run_reference_copy_gate(
    session: Session,
    scene: SceneCard,
    text: str,
    *,
    stage: str,
    bundle: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """管线里每一道原文重合门（中性步位稿 ``hard_qc`` 与风格稿的各阶段）都是这一个函数。

    风格参考 v3：绑定与否只看 :class:`~novel_system.services.style_policy.StylePolicy`（调用方传入的 bundle
    快照 → 场景当前 SceneBundle 冻结快照 → 旧 bundle / 无 bundle 时按当前活动绑定轻量现解析）；原文重合
    走唯一抄袭门 :func:`~novel_system.services.reference_copy_gate.check_reference_copy`（按书建一次索引、
    同一稿不重复扫描）。无作用域 / 无绑定 / 契约显式 absent / 文本为空 → ``None``（不登记事件）。契约 degraded、
    绑定的书已删或检查自身失败 → ``verdict="unavailable"`` 的诊断字典（见 ``styled_gate_unavailable_result``）并
    WARNING 落日志：gate 不阻断主流程，但「检查没跑」必须与「无绑定」区分开，调用方各按自己的口径处置。

    返回诊断字典（见 ``_styled_gate_result``）：``verdict`` 为 ``plagiarism`` 表示确定性 n-gram 重叠命中（Q0）；
    风格稿阶段 ``forbidden_hits`` 非空（``verdict="fail"``）表示用了画像现行的生成禁用词 / 受保护专名，中性步位
    （``hard_qc``）不看这一项。每次裁决（含 gate 自身失败）按阶段写一行 MetricEvent（风格稿
    ``styled_draft_gate_decided``，中性步位 ``qc_gate_decided``）。
    """
    event_kind = _GATE_EVENT_KINDS.get(stage)
    if event_kind is None:
        raise ValueError(f"unknown reference copy gate stage: {stage}")
    if scene is None or not text or not str(text).strip() or not _scene_has_gate_scope(scene):
        return None
    scene_id = getattr(scene, "scene_id", None)

    started_at = time.perf_counter()
    result: dict[str, Any] | None = None
    profile_id: str | None = None
    binding_id: str | None = None
    runtime_contract_hash: str | None = None
    policy: Any = None
    outcome = "error"
    try:
        policy = scene_gate_style_policy(session, scene, bundle)
        if policy.error_code is not None:
            raise ValueError(policy.error_code)
        if not policy.bound:
            outcome = "no_binding"
            return None
        profile_id = policy.profile_id
        binding_id = policy.binding_id
        runtime_contract_hash = policy.contract_hash
        report = _styled_gate_report(
            session, policy, str(text), judge_protected_terms=stage != HARD_QC_GATE_STAGE
        )
        unavailable_reason = getattr(report, "unavailable_reason", None)
        if unavailable_reason and report.verdict != "plagiarism":
            # 风格参考 v3（L4）：绑定的参考书已删（或策略降级）——抄袭门对这本书什么也没比对，不能报「通过」。
            # 与 gate 自身失败同一形状（verdict=unavailable），软 QC 据此挂 Q2 复核、起草链路发 STYLE_GATE_UNAVAILABLE。
            _LOGGER.warning(
                "reference copy gate could not check the bound reference for scene %s (stage=%s): %s",
                getattr(scene, "scene_id", None),
                stage,
                unavailable_reason,
            )
            result = styled_gate_unavailable_result(
                stage=stage,
                error="ReferenceCheckUnavailable",
                error_code=str(unavailable_reason),
                profile_id=profile_id,
                binding_id=binding_id,
                runtime_contract_hash=runtime_contract_hash,
                runtime_contract_mode=policy.mode,
            )
            outcome = "error"
            return result
        result = _styled_gate_result(
            stage=stage,
            report=report,
            profile_id=profile_id,
            binding_id=binding_id,
            runtime_contract_hash=runtime_contract_hash,
            runtime_contract_mode=policy.mode,
        )
        outcome = result["verdict"] or "pass"
        return result
    except Exception as exc:  # noqa: BLE001 — gate 不阻断主流程，但降级必须可见
        _LOGGER.warning(
            "reference copy gate unavailable for scene %s (stage=%s)",
            getattr(scene, "scene_id", None),
            stage,
            exc_info=True,
        )
        contract_error = getattr(policy, "error_code", None) if policy is not None else None
        exc_code = getattr(exc, "code", None)
        result = styled_gate_unavailable_result(
            stage=stage,
            error=type(exc).__name__,
            error_code=(
                str(contract_error)
                if contract_error is not None
                else (str(exc_code) if exc_code is not None else None)
            ),
            profile_id=profile_id,
            binding_id=binding_id,
            runtime_contract_hash=runtime_contract_hash,
            runtime_contract_mode=getattr(policy, "mode", None) if policy is not None else None,
        )
        outcome = "error"
        return result
    finally:
        # 有画像的裁决、以及 gate 自身失败（哪怕失败在契约解析、还没解析出画像）都留
        # MetricEvent；只有「确无绑定」不登记。
        if profile_id is not None or outcome == "error":
            unavailable = (
                result is not None
                and result.get("verdict") == STYLED_GATE_UNAVAILABLE_VERDICT
            )
            event_id = _record_gate_event(
                session,
                event_kind,
                scene_id=scene_id,
                profile_id=profile_id,
                binding_id=binding_id,
                outcome=outcome,
                started_at=started_at,
                context={
                    "stage": stage,
                    "runtime_contract_hash": runtime_contract_hash,
                    "plagiarism_hit_count": (
                        result.get("plagiarism_hit_count") if result else None
                    ),
                    "forbidden_hit_count": (
                        result.get("forbidden_hit_count") if result else None
                    ),
                    **(
                        {
                            "error": result.get("error"),
                            "error_code": result.get("error_code"),
                        }
                        if unavailable and result is not None
                        else {}
                    ),
                },
            )
            if result is not None:
                result["metric_event_id"] = event_id


def scene_gate_style_policy(
    session: Session, scene: SceneCard, bundle: Mapping[str, Any] | None = None
) -> Any:
    """管线内各道门的风格策略（见 :func:`~novel_system.services.style_policy.style_policy_for_scene`）。"""
    from novel_system.services.style_policy import style_policy_for_scene

    return style_policy_for_scene(session, scene, bundle if isinstance(bundle, Mapping) else None)


def _styled_gate_report(
    session: Session, policy: Any, text: str, *, judge_protected_terms: bool = True
) -> Any:
    """一道门的读数：原文重合（抄袭门，缓存）+ 生成禁用词 / 受保护专名（``judge_protected_terms``：中性步位不看）。

    风格参考 v3（H1）：禁用词与成稿门、抄袭门**同一张现行的表**——就是抄袭门的 ``protected_hits``（画像现行的
    生成期禁用词、学习作业写的受保护专名、环境变量的全局词），同一套规范化匹配。冻结契约里的禁用词只用来渲染
    提示词的红线，不参与判定：作者删掉一个误收的词，这里立刻不再认它（以前冻结的词会让已建场景一直被拦）。
    绑定的书查不到 / 策略降级 → ``unavailable_reason``（这一道门没有查成，调用方报 unavailable，不当作通过）。
    返回与旧校验报告同形的对象，交给 :func:`_styled_gate_result` 压成诊断字典（``quantitative_json`` 恒为空：
    旧校验层的量化回测随校验层删了，诊断字典已不再带它；只剩一个风格参考测试还读这个属性）。

    没查成的缘故（``missing_books`` / ``unavailable_reasons``）有才读：``check_reference_copy`` 按模块属性现查，
    换上去的结果可以不带这两项——没说自己没查成，就是查成了。
    """
    from types import SimpleNamespace

    from novel_system.services.reference_copy_gate import check_reference_copy

    copy = check_reference_copy(session, text, policy=policy)
    forbidden = (
        [
            {"pattern_statement": term, "matched_excerpt": term, "severity": "error"}
            for term in copy.protected_terms()
        ]
        if judge_protected_terms
        else []
    )
    if copy.hits:
        verdict = "plagiarism"
    elif forbidden:
        verdict = "fail"
    else:
        verdict = "pass"
    unavailable_reason: str | None = None
    missing_books = getattr(copy, "missing_books", ())
    unavailable_reasons = getattr(copy, "unavailable_reasons", ())
    if missing_books:
        unavailable_reason = "STYLE_REFERENCE_BOOK_MISSING"
    elif unavailable_reasons:
        unavailable_reason = str(unavailable_reasons[0])
    return SimpleNamespace(
        verdict=verdict,
        plagiarism_json={
            "passed": not copy.hits,
            "hits": [
                {
                    "position": hit.start,
                    "matched_length": hit.matched_chars,
                    "matched_sha256": hit.sha256,
                }
                for hit in copy.hits
            ],
        },
        forbidden_hits_json=forbidden,
        quantitative_json=[],
        unavailable_reason=unavailable_reason,
    )
