"""Quantitative validation 单测(PR-7 §7.2)。"""

from __future__ import annotations

from dataclasses import dataclass

from novel_system.services.style_reference.metrics import (
    METRIC_NAMES,
    PROSE_SHAPE_METRIC_NAMES,
)
from novel_system.services.style_reference.validation.quantitative import (
    check_quantitative,
    compute_generated_metrics,
)


@dataclass
class _FakeProfile:
    """轻量 stub,只保留 check_quantitative 用到的 profile_json 属性。"""

    profile_json: dict


def _profile_with_baseline(metrics_baseline: dict) -> _FakeProfile:
    return _FakeProfile(profile_json={"metrics_baseline": metrics_baseline})


def test_empty_generated_returns_empty() -> None:
    profile = _profile_with_baseline(
        {"avg_sentence_length": {"mean": 18.0, "std": 4.0}}
    )
    assert check_quantitative("", profile) == []


def test_missing_baseline_returns_empty() -> None:
    profile = _FakeProfile(profile_json={})
    text = "一段不太长的中文文本。"
    assert check_quantitative(text, profile) == []


def test_metric_within_tolerance_passes() -> None:
    """generated 与 baseline avg_sentence_length 相近 → passed=True。"""
    profile = _profile_with_baseline(
        {"avg_sentence_length": {"mean": 10.0, "std": 5.0}}
    )
    text = "甲乙丙丁戊。子丑寅卯辰巳午未申。"  # 句长 5, 9 → 平均 7
    reports = check_quantitative(text, profile)
    avg = next(r for r in reports if r.metric == "avg_sentence_length")
    assert avg.passed
    assert avg.tolerance >= 6.25  # 5.0 * 1.25 floor 之外


def test_metric_far_outside_tolerance_fails() -> None:
    """生成 avg_sentence_length=4 vs baseline mean=30 std=1 → deviation 远超 tolerance。"""
    profile = _profile_with_baseline(
        {"avg_sentence_length": {"mean": 30.0, "std": 1.0}}
    )
    text = "短。还短。"
    reports = check_quantitative(text, profile)
    avg = next(r for r in reports if r.metric == "avg_sentence_length")
    assert not avg.passed
    assert avg.deviation_ratio > 1.0


def test_floor_fallback_when_std_zero() -> None:
    """std=0 时 tolerance 仍 ≥ floor(避免除以零或过严)。"""
    floors = {"avg_sentence_length": 3.0}
    profile = _profile_with_baseline(
        {"avg_sentence_length": {"mean": 10.0, "std": 0.0}}
    )
    text = "一句话。"
    reports = check_quantitative(text, profile, floors=floors)
    avg = next(r for r in reports if r.metric == "avg_sentence_length")
    assert avg.tolerance >= 3.0


def test_two_profiles_yield_different_tolerances() -> None:
    """同段文本对不同 std 的 baseline,tolerance 不同。"""
    tight = _profile_with_baseline(
        {"avg_sentence_length": {"mean": 10.0, "std": 1.0}}
    )
    loose = _profile_with_baseline(
        {"avg_sentence_length": {"mean": 10.0, "std": 8.0}}
    )
    text = "一段普通的中文文本句子。"
    tight_avg = next(
        r for r in check_quantitative(text, tight) if r.metric == "avg_sentence_length"
    )
    loose_avg = next(
        r for r in check_quantitative(text, loose) if r.metric == "avg_sentence_length"
    )
    assert loose_avg.tolerance > tight_avg.tolerance


def test_dimension_routing() -> None:
    """dimension 按 metric 归类:文本指标 → language;感官词表指标已删除(旧画像里的键被忽略)。

    2026-07 勘误:paragraph_type 比例指标(dialogue_ratio 等)不再参与对照——
    生成文本在 quant 路径全部归 narration(无分类器),narration_ratio 恒 1、
    其余恒 0,对 baseline 是系统性伪偏差(对话密集参考书必然拖垮 pass_rate)。
    """
    profile = _profile_with_baseline(
        {
            "avg_sentence_length": {"mean": 10.0, "std": 3.0},
            "dialogue_ratio": {"mean": 0.3, "std": 0.1},
            "sensory_visual_per_1k": {"mean": 5.0, "std": 2.0},
        }
    )
    text = "他低头看着脚下的路,一阵阵的寒意。"
    reports = check_quantitative(text, profile)
    by_metric = {r.metric: r.dimension for r in reports}
    assert by_metric["avg_sentence_length"] == "language"
    # 2026-09-23:感官词表指标随测量核删除,旧 baseline 里残留的键不再对照
    assert "sensory_visual_per_1k" not in by_metric
    # 分类器标签依赖指标即使给了 baseline 也不得进入对照
    assert "dialogue_ratio" not in by_metric


def test_single_newline_and_blank_line_paragraphs_measure_the_same() -> None:
    """2026-09-23(V4):作者稿常用单换行分段;过去只按空行切,整场被当成一段。"""
    paragraphs = ["“先别开门。”", "他把手收了回来，站在门外听了很久。", "屋里没有声音。"]
    single = compute_generated_metrics("\n".join(paragraphs))
    blank = compute_generated_metrics("\n\n".join(paragraphs))
    html = compute_generated_metrics("".join(f"<p>{p}</p>" for p in paragraphs))
    assert single == blank == html
    assert single["single_sentence_paragraph_ratio"] == 1.0
    assert single["quote_led_paragraph_ratio"] == 1 / 3


def test_type_ratio_metrics_excluded_even_with_full_baseline() -> None:
    """全 21 项 baseline 下,8 个段型比例指标一律不出现在量化对照里。

    可证伪性:若排除逻辑被移除,生成文本(无分类器,全 narration)会让
    narration_ratio actual=1.0 / dialogue_ratio actual=0.0,两项必然 fail,
    本用例的 not-in 断言即失败。
    """
    from novel_system.services.style_reference.metrics import METRIC_NAMES
    from novel_system.services.style_reference.validation.quantitative import (
        TYPE_RATIO_METRICS,
    )

    baseline = {name: {"mean": 0.3, "std": 0.1} for name in METRIC_NAMES}
    profile = _profile_with_baseline(baseline)
    text = "他说:「今天风大。」\n\n她没有回答,只是望着窗外的雨。"
    reports = check_quantitative(text, profile)
    got_metrics = {r.metric for r in reports}
    assert got_metrics, "非比例指标应正常对照"
    assert not (got_metrics & TYPE_RATIO_METRICS), (
        f"段型比例指标漏进量化对照: {sorted(got_metrics & TYPE_RATIO_METRICS)}"
    )
    # 对照面 = 21 - 8 = 13 项纯文本统计指标
    assert len(got_metrics) == len(METRIC_NAMES) - len(TYPE_RATIO_METRICS)


def test_generated_metrics_expose_paragraph_shape_without_changing_qc_denominator() -> None:
    text = "甲乙丙丁。\n\n这是明显更长的第二段，用来制造段落长度差。"
    generated = compute_generated_metrics(text)
    assert set(PROSE_SHAPE_METRIC_NAMES) <= generated.keys()
    assert generated["paragraphs_per_1k"] > 0

    baseline = {
        name: {"mean": generated[name], "std": 1.0}
        for name in (*METRIC_NAMES, *PROSE_SHAPE_METRIC_NAMES)
    }
    reports = check_quantitative(text, _profile_with_baseline(baseline))
    got_metrics = {report.metric for report in reports}
    assert not (got_metrics & set(PROSE_SHAPE_METRIC_NAMES))
    assert len(got_metrics) == len(METRIC_NAMES) - 8
