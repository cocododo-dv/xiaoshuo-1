"""分类结果 → ``stats_json`` 里跟段落分类绑定的键(叶子模块,ingest 与 import_job 共用)。

2026-09-15:整本 LLM 分类由后台任务(``import_job``)在最后一步从段落行算这些统计;抽成叶子
模块是为了不让 ``import_job`` 反向依赖 ``ingest``(``ingest`` 已经按需导入 ``import_job``)。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from novel_system.services.style_reference.metrics import (
    MetricsEngine,
    ParagraphRecord,
    compute_prose_shape_with_variance,
)
from novel_system.services.style_reference.segmentation.types import SegmentationResult


def compute_classification_stats(
    paragraph_spans: list[tuple[int, int, str]],
    seg_result: SegmentationResult,
    *,
    metrics_engine: MetricsEngine | None = None,
) -> dict[str, Any]:
    """返回 ``metrics`` / ``prose_shape_metrics`` / ``classifier_calibration`` /
    ``paragraph_type_distribution`` 四个键。"""
    records = [
        ParagraphRecord(text=body, paragraph_type=c.paragraph_type)
        for (_s, _e, body), c in zip(paragraph_spans, seg_result.classifications)
    ]
    engine = metrics_engine or MetricsEngine()
    metrics_with_var = engine.compute_with_variance(records)
    sample_count = len(records)
    metrics_block: dict[str, dict[str, float | int]] = {
        name: {"mean": float(mean), "std": float(std), "sample_count": sample_count}
        for name, (mean, std) in metrics_with_var.items()
    }
    prose_shape_block: dict[str, dict[str, float | int]] = {
        name: {"mean": float(mean), "std": float(std), "sample_count": sample_count}
        for name, (mean, std) in compute_prose_shape_with_variance(records).items()
    }
    type_counter = Counter(c.paragraph_type for c in seg_result.classifications)
    type_distribution = (
        {ptype: round(count / sample_count, 4) for ptype, count in type_counter.items()}
        if sample_count
        else {}
    )
    return {
        "metrics": metrics_block,
        "prose_shape_metrics": prose_shape_block,
        "classifier_calibration": seg_result.calibration,
        "paragraph_type_distribution": type_distribution,
    }


__all__ = ["compute_classification_stats"]
