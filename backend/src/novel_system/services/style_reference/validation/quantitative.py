"""Quantitative validation:自适应 tolerance 的硬指标对照(PR-7 §7.2)。

`check_quantitative(generated_text, profile)`:
1. 把 generated_text 包装成临时 paragraphs 列表(全部归 narration)
2. MetricsEngine.compute_all → 逐项 metric
3. 对照 profile.profile_json["metrics_baseline"](学习文风作业落)
4. tolerance = max(baseline_std × 1.25, ABSOLUTE_FLOORS[metric])
5. 返 list[QuantitativeReportItem]

baseline 缺失或 generated_text 为空时返 []。

2026-07 勘误:8 个 paragraph_type 比例指标(dialogue_ratio 等)**不参与对照**。
它们度量的是分类器标签而非文本本身——生成文本在这里全部归 narration(无分类器),
narration_ratio 恒 1、其余恒 0,对照 baseline 是系统性伪偏差:对话密集的参考书
必然把 pass_rate 拖到 0.8 以下,QC gate 恒 PARTIAL/FAIL。对照只保留 13 个纯文本
统计指标(句长/标点/词表密度;感官词表指标 2026-09-23 已删除),与「quant 不依赖 paragraph_type 精确性」的
既有约定一致;8 项仍保留在 metrics_baseline / 抽取锚点 / 前端展示。

2026-08:``compute_generated_metrics`` 额外返回 5 个纯文本段落形态指标，供注入
偏差提示与候选重排使用；``check_quantitative_against_baseline`` 仍只遍历冻结的
``METRIC_NAMES``，因此不改变既有 QC pass-rate 分母。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.measure import kernel_paragraphs
from novel_system.services.style_reference.metrics import (
    METRIC_NAMES,
    MetricsEngine,
    ParagraphRecord,
    compute_prose_shape_metrics,
)
from novel_system.services.style_reference.schemas import QuantitativeReportItem

if TYPE_CHECKING:
    from novel_system.db.models import StyleReferenceProfile


DEFAULT_FLOOR = 0.1

# 分类器标签依赖指标:生成侧无 paragraph_type(全部归 narration),对照无意义,
# 从量化回测中排除(见 module docstring 2026-07 勘误)。
TYPE_RATIO_METRICS: frozenset[str] = frozenset(
    {
        "dialogue_ratio",
        "psychology_ratio",
        "description_env_ratio",
        "description_char_ratio",
        "action_ratio",
        "narration_ratio",
        "transition_ratio",
        "flashback_ratio",
    }
)


def _wrap_as_paragraphs(generated_text: str) -> list[ParagraphRecord]:
    """把 generated_text 按测量核的唯一分段规则切段(换行即段界、作者稿 HTML 先取段),全部归 narration。

    2026-09-23:过去只按空行切,作者稿用单换行分段时整场被当成一段。quant 不依赖 paragraph_type 精确性,
    只关心句长 / 标点等纯文本统计。
    """
    return [ParagraphRecord(text=p, paragraph_type="narration") for p in kernel_paragraphs(generated_text)]


def _dim_for_metric(metric: str) -> str:
    if metric.startswith("paragraph_") or metric in {
        "single_sentence_paragraph_ratio",
        "quote_led_paragraph_ratio",
    }:
        return "narrative"
    if metric in {
        "dialogue_ratio",
        "psychology_ratio",
        "description_env_ratio",
        "description_char_ratio",
        "action_ratio",
        "narration_ratio",
        "transition_ratio",
        "flashback_ratio",
    }:
        return "narrative"
    return "language"


def compute_generated_metrics(generated_text: str) -> dict[str, float]:
    """计算全部可观测指标；旧 QC 仍只消费冻结的 26 项子集。"""
    paragraphs = _wrap_as_paragraphs(generated_text)
    if not paragraphs:
        return {}
    metrics = MetricsEngine().compute_all(paragraphs)
    metrics.update(compute_prose_shape_metrics(paragraphs))
    return metrics


def check_quantitative(
    generated_text: str,
    profile: "StyleReferenceProfile",
    *,
    floors: dict[str, float] | None = None,
) -> list[QuantitativeReportItem]:
    gen_metrics = compute_generated_metrics(generated_text)
    if not gen_metrics:
        return []

    profile_json = profile.profile_json or {}
    baseline = profile_json.get("metrics_baseline") or {}
    return check_quantitative_against_baseline(
        generated_text,
        baseline,
        floors=floors,
        generated_metrics=gen_metrics,
    )


def check_quantitative_against_baseline(
    generated_text: str,
    baseline: Mapping[str, Any],
    *,
    floors: dict[str, float] | None = None,
    generated_metrics: Mapping[str, float] | None = None,
) -> list[QuantitativeReportItem]:
    """Validate against an explicit baseline (used by frozen layered contracts)."""
    gen_metrics = (
        dict(generated_metrics)
        if generated_metrics is not None
        else compute_generated_metrics(generated_text)
    )
    if not gen_metrics or not baseline:
        return []

    if floors is None:
        try:
            floors = load_yaml_config("tolerance_floors")
        except FileNotFoundError:
            floors = {}

    reports: list[QuantitativeReportItem] = []
    for name in METRIC_NAMES:
        if name in TYPE_RATIO_METRICS:
            continue  # 分类器标签依赖指标不对照(见 module docstring)
        base = baseline.get(name)
        if not isinstance(base, dict):
            continue
        if name not in gen_metrics:
            continue
        baseline_mean = float(base.get("mean", 0.0))
        baseline_std = float(base.get("std", 0.0))
        floor = float(floors.get(name, DEFAULT_FLOOR))
        tolerance = max(baseline_std * 1.25, floor)
        actual = float(gen_metrics[name])
        deviation = abs(actual - baseline_mean) / max(tolerance, 1e-6)
        reports.append(
            QuantitativeReportItem(
                dimension=_dim_for_metric(name),
                metric=name,
                target_mean=baseline_mean,
                target_std=baseline_std,
                actual=actual,
                tolerance=tolerance,
                passed=deviation <= 1.0,
                deviation_ratio=deviation,
            )
        )
    return reports
