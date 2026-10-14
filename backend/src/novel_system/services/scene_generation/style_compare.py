"""起草流程里的成对比较（2026-10-14 评审统一）：同一段的两个版本，哪一个更像参考作者。

用在两处「留不留」的决定上：软补丁（补丁前 vs 补丁稿，``scene_run.soft_qc``）与定向修改（首稿 vs 修改稿，读数说
修改稿更近时再问一次，``style_first``）。以前补丁的去留拿补丁前后两次绝对评审分相减——评审半分到一分的噪声就能翻转
结论；成对比较直接问「哪一个更像」，两个次序各问一次（A = 原稿 / B = 新稿，再 A = 新稿 / B = 原稿），两次指向同一边
才算数（``judge.compare_verdict``）。

提示：模板 ``style_reference_compare``，不带场景上下文（``judge.bare_prompt``），system 前面接评审口径的
``[STYLE_REFERENCE]`` 块（与软 QC 的参考评审同一套：冻结选窗的前 4 窗 + 文风卡 + 声音 + 红线），走 ``soft_qc`` 节点
路由，经管线的节点调度器派发（记账、执行步键 ``<来源步键>:compare:ab`` / ``…:ba``——一次执行里每个步键只派发一次；
续跑是新的一次执行，重新比较不冲突）。

比不成（这份画像的评审校准说评审分不清作者、参考没带上、调用失败）→ ``unavailable``，调用方退回确定性读数的规则，
不阻断起草。云策略不许把参考送给评审节点 → 原样抛 409（与软 QC 同一条红线）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.llm_task_runner import LLMNodeContinuityError, LLMNodeExecutionError
from novel_system.services.style_prompt_injection import prompt_carries_reference
from novel_system.services.style_reference.judge import (
    COMPARE_TEMPLATE,
    COMPARE_UNAVAILABLE,
    JUDGE_NODE_ID,
    STATE_UNUSABLE,
    bare_prompt,
    compare_user_prompt,
    compare_verdict,
    parse_compare_output,
)
from novel_system.services.style_reference.policy import STYLE_REFERENCE_FAIL_CLOSED_ERRORS

_LOGGER = logging.getLogger(__name__)

COMPARE_STEP = "style_reference_compare"


def compare_step_key(base_key: str, order: str) -> str:
    return f"{base_key}:compare:{order}"


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"verdict": COMPARE_UNAVAILABLE, "reason": reason, **extra}


def compare_versions(
    session: Session,
    llm_runner: Any,
    *,
    scene: Any,
    bundle: dict[str, Any],
    policy: Any,
    base_text: str,
    candidate_text: str,
    execution_step_key: str,
) -> dict[str, Any]:
    """原稿（base）与新稿（candidate）哪一个更像参考作者 → ``{verdict, answers, reasons, llm_call_ids}``
    （``verdict`` ∈ ``judge.COMPARE_*``；比不成 → ``unavailable`` + ``reason``）。"""
    from novel_system.services.prompt_builder import default_input_token_budget, load_prompt_templates
    from novel_system.services.style_prompt_injection import ROLE_REVIEW, inject_style_reference_prefix
    from novel_system.services.style_reference.judge_calibration import judge_context

    if not getattr(policy, "bound", False):
        return _unavailable("not_bound")
    context = judge_context(session, getattr(policy, "profile_id", None))
    if context.state == STATE_UNUSABLE:
        # 校准说这个评审模型分不清作者与普通文字：它的比较也不作数，省下两次调用
        return _unavailable("judge_unusable", judge_state=context.state)
    template = load_prompt_templates()[COMPARE_TEMPLATE]
    answers: list[str | None] = []
    reasons: list[str] = []
    llm_call_ids: list[str] = []
    for order, (version_a, version_b) in (("ab", (base_text, candidate_text)), ("ba", (candidate_text, base_text))):
        user_prompt = compare_user_prompt(template.task_prompt, version_a, version_b)
        prompt = bare_prompt(template, user_prompt=user_prompt, target_input_tokens=default_input_token_budget(template))
        try:
            injected = inject_style_reference_prefix(
                session,
                prompt,
                scene,
                bundle,
                task_type="scene_generation",
                final_user_prompt=user_prompt,
                role=ROLE_REVIEW,
                node_id=JUDGE_NODE_ID,
            )
        except STYLE_REFERENCE_FAIL_CLOSED_ERRORS:
            raise
        except Exception:  # noqa: BLE001 — 参考渲染失败：比不成，退回读数规则
            _LOGGER.warning("style compare prompt failed for scene %s", getattr(scene, "scene_id", None), exc_info=True)
            return _unavailable("reference_render_failed")
        if not prompt_carries_reference(injected):
            return _unavailable("reference_not_carried")
        try:
            node_result = llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=bundle["bundle_id"],
                bundle_hash=bundle["bundle_snapshot_hash"],
                node_id=JUDGE_NODE_ID,
                step=COMPARE_STEP,
                prompt=injected,
                user_prompt=user_prompt,
                execution_step_key=compare_step_key(execution_step_key, order),
            )
        except (LLMNodeExecutionError, LLMNodeContinuityError) as exc:
            _LOGGER.warning("style compare call failed for scene %s: %s", scene.scene_id, exc)
            return _unavailable(
                "call_failed", llm_call_ids=[*llm_call_ids, getattr(exc, "llm_call_id", None)], answers=answers
            )
        llm_call_ids.append(node_result.llm_call_id)
        structured = getattr(node_result.response, "structured_output", None)
        answers.append(parse_compare_output(structured))
        reasons.append(str((structured or {}).get("reason") or "").strip()[:200] if isinstance(structured, dict) else "")
    verdict = compare_verdict(answers[0], answers[1])
    result: dict[str, Any] = {
        "verdict": verdict,
        "answers": answers,
        "reasons": reasons,
        "llm_call_ids": llm_call_ids,
        "judge_state": context.state,
    }
    if verdict == COMPARE_UNAVAILABLE:
        result["reason"] = "no_answer"
    return result


__all__ = ["COMPARE_STEP", "compare_step_key", "compare_versions"]
