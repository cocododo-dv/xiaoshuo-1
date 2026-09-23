"""风格参考 v3（V7 / L4）— 评审节点（软 QC、准定稿验收）的共用约定（叶子模块）。

* **分数量级归一**：模型给的分数要换算到契约的 0–1。刻度**以模板声明的为准**：评审模板的 ``structured_schema``
  给分数字段写了 ``minimum`` / ``maximum``（契约测试 ``test_prompt_template_contracts`` 要求写），提示词也写明了
  刻度——那就按声明的刻度逐个换算（0–10 的 9.3 → 0.93），落在 ``[0, 刻度]`` 之外的分数**丢掉**（不夹、不猜）：
  0–10 的提示下答 85 是答错了，不能把同一次回答里的其余分数一起按 0–100 除掉；全在 1 以下的回答也按 0–10 读
  （1.0 是十分之一，不是满分）。旧做法按「一次回答里最大的那个分」猜量级，这两种情形都读错。
* 模板没声明刻度（保存过提示词快照、还是旧版模板的安装）时才退回按一次回答推断（:func:`score_scale`，最大值
  > 10 → 0–100，> 1 → 0–10，否则 0–1）——那是没有更好依据时的兜底，不是常规路径。
* 评审节点的样例窗数不在这里：评审只看 4 窗（规划 3 窗）由渲染请求的角色决定（``inject.request.ROLE_K_CAPS``，
  唯一定义；调用方传 ``role="review"``）。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from novel_system.services.style_reference.binding_config import ALL_DIMENSIONS

JUDGE_SCALE_MAX = 10.0
# 分数恰好落在边界上的浮点噪声（10.000000001）不算越界
_SCALE_EPSILON = 1e-9


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _declared_maximum(spec: Any) -> float | None:
    """一个 schema 节点声明的分数上限：数值节点看自己的 ``maximum``；对象节点看成员（数值成员、或成员对象里的
    ``score``），取第一个声明了的。"""
    if not isinstance(spec, Mapping):
        return None
    maximum = _finite(spec.get("maximum"))
    if maximum is not None and maximum > 0:
        return maximum
    properties = spec.get("properties")
    if isinstance(properties, Mapping):
        for member in properties.values():
            found = _declared_maximum(member)
            if found is not None:
                return found
    return None


def declared_score_scale(schema: Any, *fields: str) -> float | None:
    """模板 ``structured_schema`` 给这些分数字段声明的刻度（``maximum``）：按 ``fields`` 的顺序取第一个声明了的。

    字段是数值（``{"type": "number", "minimum": 0, "maximum": 10}``）就看它自己；是对象（按维打分）就看它的成员
    （数值成员，或成员对象里的 ``score``）。都没声明（旧模板）→ ``None``，调用方退回 :func:`score_scale`。
    """
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(properties, Mapping):
        return None
    for name in fields:
        found = _declared_maximum(properties.get(name))
        if found is not None:
            return found
    return None


def score_scale(values: Iterable[Any]) -> float:
    """**兜底**：模板没声明刻度时按一次回答推断量级（1 / 10 / 100，只看数值，非数值忽略）。

    声明了刻度的模板一律用 :func:`declared_score_scale`——按回答推断会把全在 1 以下的 0–10 回答读成满分、
    被一个误写的 85 带着把整份回答按 0–100 除。"""
    numbers = [number for number in (_finite(value) for value in values) if number is not None]
    top = max(numbers, default=0.0)
    if top > 10.0:
        return 100.0
    if top > 1.0:
        return 10.0
    return 1.0


def response_score_scale(schema: Any, fields: Iterable[str], values: Iterable[Any]) -> tuple[float, str]:
    """(这一次回答用的刻度, 来源)：模板声明了就用声明的（``"declared"``），否则按回答推断（``"inferred"``）。"""
    declared = declared_score_scale(schema, *tuple(fields))
    if declared is not None:
        return declared, "declared"
    return score_scale(values), "inferred"


def normalize_score(value: Any, scale: float) -> float | None:
    """一个分数按刻度换算到 [0, 1]（四位小数）：非数值 / NaN → ``None``；不在 ``[0, scale]`` 里 → ``None``（丢掉，
    不夹到边界——越界的分是答错了刻度，夹成 0 或 1 会假装成一个极端的评分）。"""
    number = _finite(value)
    if number is None:
        return None
    top = float(scale or 1.0)
    if number < -_SCALE_EPSILON or number > top + _SCALE_EPSILON:
        return None
    return round(max(0.0, min(1.0, number / top)), 4)


def to_unit(value: Any, scale: float) -> Any:
    """旧口径：按量级换算到 [0, 1] 并夹到边界（四位小数）；非数值原样返回，NaN / 无穷 → None。

    新代码用 :func:`normalize_score`（越界丢掉）。"""
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
    "declared_score_scale",
    "judge_dimension_scores",
    "normalize_score",
    "response_score_scale",
    "score_scale",
    "to_unit",
    "unit_to_judge_scale",
]
