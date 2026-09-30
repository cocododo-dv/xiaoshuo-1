"""准终稿第一轮评审与软 QC 第一轮之后「改不改」的分支表：存检查点时按它写，续跑时按它核对（B01-04）。

两张表各是一个纯函数，存与读共用同一份——以前存一份、读时再手写一份，先后次序不同。唯一没记进检查点的
输入是「这一步还能不能花钱」（``spend_allowed``：预算 / 候选上限），所以续跑时存下的控制必须等于这两种之一。
次序取存的那一边（检查点里已经存下的都是它写的；两边只在一种现在走不到的组合上不同）。
"""

from __future__ import annotations

from typing import Any


def near_final_eval0_control(eval0: dict[str, Any], *, spend_allowed: bool) -> dict[str, Any]:
    """准终稿第一轮评审之后：重写、跳过（预算）、交人工复核、通过还是不能自动重写。

    分支的次序取存的一边（先看预算，再看人工复核）。只有 ``rewrite`` 分支才重写（``rewrite_allowed``）：评审
    同时要人工复核又要求重写时（``near_final`` 现在不会这样给，但没有谁守着这条跨模块的不变式），以前钱够就
    记 ``human_review_proposal`` 却照样重写，续跑与终稿复验都判这份控制损坏；现在交人工复核、不自动重写。
    """
    rewrite_requested = not bool(eval0.get("pass_flag")) and bool(eval0.get("should_rewrite"))
    if rewrite_requested and not spend_allowed:
        branch, skip_reason = "rewrite_skipped", "budget_or_candidate_cap"
    elif eval0.get("requires_human_review"):
        branch, skip_reason = "human_review_proposal", "human_review_proposal"
    elif eval0.get("pass_flag"):
        branch, skip_reason = "pass", "no_rewrite_requested"
    elif not rewrite_requested:
        branch, skip_reason = "unresolved", "not_auto_rewrite_eligible"
    else:
        branch, skip_reason = "rewrite", None
    return {
        "branch": branch,
        "rewrite_requested": rewrite_requested,
        "rewrite_allowed": branch == "rewrite",
        "skip_reason": skip_reason,
    }


def soft_qc0_control(branch: str | None, *, spend_allowed: bool) -> dict[str, Any]:
    """软 QC 第一轮之后：修补、跳过（预算）、交人工复核还是不用修补。``patch_allowed`` 为真才修补。"""
    patch_allowed = branch == "patch" and bool(spend_allowed)
    if branch == "patch" and not patch_allowed:
        skip_reason: str | None = "budget_or_candidate_cap"
    elif branch == "human_review_required":
        skip_reason = "human_review_required"
    elif branch != "patch":
        skip_reason = "no_patch_requested"
    else:
        skip_reason = None
    return {"patch_allowed": patch_allowed, "skip_reason": skip_reason}


def is_derivable_control(control: Any, derive) -> bool:
    """``control`` 是否等于 ``derive(spend_allowed=…)`` 两种结果之一（续跑核对用）。"""
    return isinstance(control, dict) and any(
        control == derive(spend_allowed=spend_allowed) for spend_allowed in (True, False)
    )
