"""风格参考 v3（V7 / L4）— 评审节点（软 QC、准定稿验收）的共用约定（叶子模块）。

* **分数量级归一**：模型给的分数可能是 0–1、0–10 或 0–100（提示词没写范围时真实模型给过 9.3 与 93）。旧做法
  各自为政：软 QC 按单个值猜量级，准定稿验收直接夹到 [0, 1]——9.3 变成 1.0，实库 3/3 次 overall_score 饱和为 1.0。
  这里按**一次回答里的全部分数**定量级（最大值 > 10 → 0–100，> 1 → 0–10，否则 0–1），再统一换算到 0–1。
  同一次回答里用同一把尺：9.3 → 0.93，不是 1.0。
* **评审节点的样例窗数**：评审只看 4 窗（规划节点 3 窗，起草按绑定的窗数），同一场的评审不必把 12 窗再读一遍。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from novel_system.services.style_reference.binding_config import ALL_DIMENSIONS

REVIEW_FEW_SHOT_K_CAP = 4
JUDGE_SCALE_MAX = 10.0


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def score_scale(values: Iterable[Any]) -> float:
    """一次回答里分数用的量级：1 / 10 / 100（只看数值，非数值忽略）。"""
    numbers = [number for number in (_finite(value) for value in values) if number is not None]
    top = max(numbers, default=0.0)
    if top > 10.0:
        return 100.0
    if top > 1.0:
        return 10.0
    return 1.0


def to_unit(value: Any, scale: float) -> Any:
    """按量级换算到 [0, 1]（四位小数）；非数值原样返回，NaN / 无穷 → None。"""
    if value is None or isinstance(value, bool):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(number):
        return None
    return round(max(0.0, min(1.0, number / (scale or 1.0))), 4)


def judge_dimension_scores(raw: Any) -> dict[str, Any]:
    """评审按维打分的原始对象 → 只留 16 维里的键（值原样，待统一换算）。"""
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if str(key) in ALL_DIMENSIONS and _finite(value) is not None
    }


def unit_to_judge_scale(value: Any) -> float | None:
    """0–1 → 0–10（一位小数），落库 / 读数用评审的 10 分制。"""
    number = _finite(value)
    return None if number is None else round(max(0.0, min(1.0, number)) * JUDGE_SCALE_MAX, 1)


__all__ = [
    "JUDGE_SCALE_MAX",
    "REVIEW_FEW_SHOT_K_CAP",
    "judge_dimension_scores",
    "score_scale",
    "to_unit",
    "unit_to_judge_scale",
]
