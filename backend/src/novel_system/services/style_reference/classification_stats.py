"""分类结果 → ``stats_json`` 里跟段落分类绑定的键(叶子模块,ingest 与 import_job 共用)。

2026-09-15:整本 LLM 分类由后台任务(``import_job``)在最后一步从段落行算这些统计;抽成叶子
模块是为了不让 ``import_job`` 反向依赖 ``ingest``(``ingest`` 已经按需导入 ``import_job``)。
2026-09-24(风格参考 v3 S2):v2 的 21 指标 / 段落形状块(``metrics`` / ``prose_shape_metrics``,``MetricsEngine``)
随指标包络删除——「像不像」只看读数(``fidelity``);这里只剩分类器校准与段型分布。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from novel_system.services.style_reference.segmentation.types import SegmentationResult


def compute_classification_stats(
    paragraph_spans: list[tuple[int, int, str]],
    seg_result: SegmentationResult,
) -> dict[str, Any]:
    """返回 ``classifier_calibration`` / ``paragraph_type_distribution`` 两个键。"""
    sample_count = min(len(paragraph_spans), len(seg_result.classifications))
    type_counter = Counter(c.paragraph_type for c in seg_result.classifications[:sample_count])
    type_distribution = (
        {ptype: round(count / sample_count, 4) for ptype, count in type_counter.items()}
        if sample_count
        else {}
    )
    return {
        "classifier_calibration": seg_result.calibration,
        "paragraph_type_distribution": type_distribution,
    }


__all__ = ["compute_classification_stats"]
