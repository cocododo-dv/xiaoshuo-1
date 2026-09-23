"""风格评分核(2026-09-14 减法后 candidate_rerank 只剩这部分):冻结画像 → 目标包络,
候选文本 → 贴合读数。重排层(shadow / active / 基准授权)已删除;候选的原文重合由唯一抄袭门查
(``reference_copy_gate``,见 test_reference_copy_gate.py),评分核里不再带抄袭守卫。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from novel_system.services.style_reference.candidate_rerank import (
    CandidateRerankPolicy,
    assess_candidate_text,
    build_style_target,
)
from novel_system.services.style_reference.metrics import compute_generated_metrics


REFERENCE_TEXT = (
    "雨脚斜斜地落在青石上，檐铃隔一阵才轻轻响一下。"
    "她没有催问，只把灯芯拨低，让桌沿那道旧划痕重新沉进阴影里。"
    "巷口传来车轮碾水的细声，近了，又慢慢远去。"
) * 12

FAR_TEXT = (
    "快跑！快跑！为什么还不走？他喊道：现在就走！"
    "真的，真的，真的，一切都结束了！你听见了吗？"
) * 18

SAFE_TEXT = (
    "晨雾沿着荒坡退去，牧人收紧缰绳，望见远处新垒的石墙。"
    "炊烟还没有升起，几只寒鸦先从枯树上散开。"
) * 14


def _profile(profile_id: str, text: str, *, std: float = 0.0):
    metrics = compute_generated_metrics(text)
    return SimpleNamespace(
        profile_id=profile_id,
        profile_json={
            "metrics_baseline": {
                name: {"mean": value, "std": std, "sample_count": 20}
                for name, value in metrics.items()
            }
        },
    )


def test_policy_is_a_plain_threshold_bundle_without_modes() -> None:
    policy = CandidateRerankPolicy()
    assert policy.min_substantive_chars == 300
    assert policy.min_metric_count == 12
    assert policy.min_confidence == pytest.approx(0.65)
    assert not hasattr(policy, "effective_mode")
    assert not hasattr(policy, "from_mapping")


def test_layered_target_uses_generic_to_specific_weights_and_total_variance() -> None:
    first = SimpleNamespace(
        profile_id="base",
        profile_json={"metrics_baseline": {"avg_sentence_length": {"mean": 10.0, "std": 1.0}}},
    )
    second = SimpleNamespace(
        profile_id="specific",
        profile_json={"metrics_baseline": {"avg_sentence_length": {"mean": 20.0, "std": 2.0}}},
    )

    target = build_style_target([first, second], floors={"avg_sentence_length": 0.1})

    assert target is not None
    metric = target.metrics["avg_sentence_length"]
    assert metric.mean == pytest.approx(50.0 / 3.0)
    assert metric.std > 2.0  # includes the between-profile distance
    assert metric.component_count == 2
    assert target.profile_ids == ("base", "specific")


def test_candidate_closer_to_profile_scores_higher_with_group_balancing() -> None:
    target = build_style_target([_profile("profile", REFERENCE_TEXT)])
    policy = CandidateRerankPolicy()

    close = assess_candidate_text("close", REFERENCE_TEXT, 0.8, target, policy)
    far = assess_candidate_text("far", FAR_TEXT, 0.8, target, policy)

    assert close.style_eligible is True
    assert far.style_eligible is True
    # 13 项文本指标 + 5 项段落形状(2026-09-23 测量核删掉了 5 项感官词表指标)
    assert close.metric_count == 18
    assert close.style_score == pytest.approx(1.0)
    assert close.style_score > far.style_score
    assert set(close.group_scores) == {
        "paragraph_shape",
        "sentence_shape",
        "punctuation_rhythm",
        "register",
        "figurative_proxy",
    }
    audit = close.to_audit_dict()
    assert audit["scorer_version"] == "style_candidate_rerank_v2"
    assert audit["selection_reason"] == "quality_order"


def test_assessment_leaves_the_copy_check_to_the_caller() -> None:
    # 抄袭检查不在评分核里:读数的 plagiarism_* 由调用方按唯一抄袭门的结果填写
    target = build_style_target([_profile("profile", REFERENCE_TEXT)])
    assessment = assess_candidate_text("any", SAFE_TEXT, 0.5, target, CandidateRerankPolicy())
    assert assessment.plagiarism_checked is False and assessment.plagiarism_passed is None
    audit = assessment.to_audit_dict()
    assert "combined_score" not in audit and audit["plagiarism_hit_count"] == 0
