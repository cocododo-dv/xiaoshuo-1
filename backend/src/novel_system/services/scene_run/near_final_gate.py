"""准终稿重写稿的门：被拒的原因 → 跳过原因、gate 诊断压成小结、小结 → Q2 / Q3 警告。

小结随检查点冻结（``near_rewrite_styled_gate``），也进 near_final payload 的 ``rewrite_style_gate``；续跑按同一份
小结重建「重写被拒 → 来源稿成为终稿」的分支。编排器从这里取；``services.orchestrator`` 照旧转出这几个名字。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from novel_system.services.scene_run.constants import (
    NEAR_FINAL_REJECTION_SKIP_REASONS,
    NEAR_FINAL_REWRITE_GATE_STAGE,
    NEAR_FINAL_REWRITE_REJECTED_SKIP_REASON,
)

if TYPE_CHECKING:
    from novel_system.services.scene_generation import StyleGenerationResult


def _near_final_rejection_skip_reason(gate: Any) -> str:
    """被拒的重写稿 → skip_reason（旧检查点里的抄袭拒绝没有 ``rejected_reason``，按抄袭读）。"""
    reason = str((gate or {}).get("rejected_reason") or "style_plagiarism") if isinstance(gate, dict) else "style_plagiarism"
    return NEAR_FINAL_REJECTION_SKIP_REASONS.get(reason, NEAR_FINAL_REWRITE_REJECTED_SKIP_REASON)


def _near_final_rewrite_gate_summary(
    generation: StyleGenerationResult,
) -> dict[str, Any] | None:
    """把重写稿的 styled-draft gate 诊断压成可进检查点 / near_final payload 的小结。

    无绑定（gate 没跑、``styled_draft_gate is None``）→ ``None``，payload 形状与旧数据一致。
    """
    gate = generation.styled_draft_gate
    if not isinstance(gate, dict):
        return None
    verdict = str(gate.get("verdict") or "")
    notice_codes = [
        str(item.get("code"))
        for item in (generation.notices or [])
        if isinstance(item, dict) and item.get("code")
    ]
    return {
        "stage": NEAR_FINAL_REWRITE_GATE_STAGE,
        "verdict": verdict,
        "rejected": verdict == "plagiarism",
        "plagiarism_hit_count": gate.get("plagiarism_hit_count"),
        "forbidden_hit_count": gate.get("forbidden_hit_count"),
        "error": gate.get("error"),
        "profile_id": gate.get("profile_id"),
        "runtime_contract_hash": gate.get("runtime_contract_hash"),
        "notice_codes": notice_codes,
    }


def _near_final_rewrite_gate_warnings(gate: Any) -> list[dict[str, Any]]:
    """重写稿 gate 小结 → Q2 警告（严格模式停点；宽松模式随稿归档、醒目提示）。"""
    if not isinstance(gate, dict):
        return []
    rejected_reason = str(gate.get("rejected_reason") or "")
    if gate.get("rejected") and rejected_reason == "base_safety":
        reasons = "、".join(str(item) for item in (gate.get("rejection") or {}).get("reasons") or []) or "未知"
        return [
            {
                "issue_key": "near_final_rewrite_rejected_base_safety",
                "quality_level": "Q2",
                "message": (
                    f"准终稿重写稿没过确定性安全门（{reasons}），已丢弃；终稿保留重写前的稿子，"
                    "评审的改稿意见随稿留痕，可以按意见自己改。"
                ),
                "recommended_action": "author_review_optional_fix",
                "verified_by": "near_final_rewrite_base_safety",
            }
        ]
    if gate.get("rejected") and rejected_reason == "moved_away":
        fidelity = gate.get("rejection") or {}
        return [
            {
                "issue_key": "near_final_rewrite_rejected_moved_away",
                "quality_level": "Q3",
                "message": (
                    "准终稿重写稿离参考作者更远（在作者自己的段落里的位次 "
                    f"{fidelity.get('source_percentile')} → {fidelity.get('rewrite_percentile')}），已丢弃；"
                    "终稿保留重写前更像作者的稿子，评审的改稿意见随稿留痕。"
                ),
                "recommended_action": "author_review_optional_fix",
                "verified_by": "style_fidelity_reading",
            }
        ]
    if gate.get("rejected"):
        return [
            {
                "issue_key": "near_final_rewrite_rejected_style_plagiarism",
                "quality_level": "Q2",
                "message": (
                    "准终稿重写稿与参考作品原文存在确定性 n-gram 重叠（抄袭红线），"
                    "已被丢弃；终稿回退为重写前已过 gate 的风格稿。"
                ),
                "recommended_action": "author_review_optional_fix",
                "verified_by": "style_plagiarism_ngram",
            }
        ]
    forbidden = gate.get("forbidden_hit_count")
    if isinstance(forbidden, int) and forbidden > 0:
        return [
            {
                "issue_key": "near_final_rewrite_banned_term_replicated",
                "quality_level": "Q2",
                "message": (
                    f"准终稿重写稿用了参考画像的生成禁用词 / 受保护专名（{forbidden} 个）；"
                    "请人工复核：是参考书的专名就换成自己的，日常用词被误收的可以接受。"
                ),
                "recommended_action": "author_review_optional_fix",
                "verified_by": None,
            }
        ]
    if str(gate.get("verdict") or "") == "unavailable":
        return [
            {
                "issue_key": "near_final_rewrite_style_gate_unavailable",
                "quality_level": "Q2",
                "message": (
                    "准终稿重写稿的抄袭 / 冻结禁用词检查未能执行"
                    f"（{gate.get('error') or 'unknown'}）；本稿未经参考来源安全核对，"
                    "请人工复核。"
                ),
                "recommended_action": "author_review_optional_fix",
                "verified_by": None,
            }
        ]
    return []
