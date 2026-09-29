"""检验声音基线本身的 z 值工具（只给测试用）。

2026-09-23 起管线里没有 z 值的调用方了：样例窗口的典型度在 ``windows.py``、「像不像作者」的读数看作者自己的窗口
分布（``fidelity.py``），基线（``voice_baseline.yaml``）在产品里只剩 ``deliberate_repetition`` 一处用途。这里的
z 值接口从 ``voice_signature`` 原样搬来，只用来检验测量核 + 基线能把两位公版作者分开（黄金语料测试）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from novel_system.services.style_reference import voice_signature as vs
from novel_system.services.value_coercion import finite_or_zero

# 整书签名是 n 块的聚合，块间 std 对它过宽——z 值按 1/sqrt(min(n, 16)) 收窄。
Z_MAX_AGGREGATION_BLOCKS = 16
_Z_CLIP = 8.0


def _round(value: float) -> float:
    return round(finite_or_zero(value), 6)


def _feature_values(features_or_signature: Mapping[str, Any] | None) -> dict[str, float]:
    """接受整份签名或仅 features。"""
    if not isinstance(features_or_signature, Mapping):
        return {}
    inner = features_or_signature.get("features")
    if isinstance(inner, Mapping):
        return {str(k): finite_or_zero(v) for k, v in inner.items()}
    return {str(k): finite_or_zero(v) for k, v in features_or_signature.items() if isinstance(v, (int, float))}


def block_count_of(features_or_signature: Mapping[str, Any] | None) -> int:
    """签名覆盖的基线块数（由 stats.char_count 推出）；仅 features 时视为 1 块。"""
    if not isinstance(features_or_signature, Mapping):
        return 1
    stats = features_or_signature.get("stats")
    if not isinstance(stats, Mapping):
        return 1
    try:
        char_count = float(stats.get("char_count", 0))
    except (TypeError, ValueError):
        return 1
    if not math.isfinite(char_count) or char_count <= 0:
        return 1
    return max(1, int(round(char_count / vs.BASELINE_BLOCK_CHARS)))


def _aggregation_scale(block_count: int | None) -> float:
    count = 1 if block_count is None else max(1, int(block_count))
    return math.sqrt(min(count, Z_MAX_AGGREGATION_BLOCKS))


def feature_z_scores(
    features: Mapping[str, Any],
    baseline_features: Mapping[str, Any],
    baseline_std: Mapping[str, Any] | None = None,
    *,
    block_count: int | None = None,
) -> dict[str, float]:
    """逐特征 z 值。

    ``baseline_features`` 的值可以是均值数字，也可以是 ``{"mean", "std", ...}`` 映射（voice_baseline.yaml 的形态）；
    ``baseline_std`` 显式给出时覆盖 std。std 有下限（均值的 5% 或 1e-6）避免除零；结果裁到 ±8 且恒有限。
    缺失的特征（任一侧）跳过。

    基线 std 是块级（1500 字）波动。``features`` 传整份签名时按 ``stats.char_count`` 推出它聚合的块数 n，std 按
    1/sqrt(min(n, 16)) 收窄；显式 ``block_count`` 覆盖（传 1 即得字面块级 z）。仅传 features 时 n=1。
    """
    values = _feature_values(features)
    result: dict[str, float] = {}
    if not isinstance(baseline_features, Mapping):
        return result
    scale = _aggregation_scale(block_count if block_count is not None else block_count_of(features))
    for name, value in values.items():
        entry = baseline_features.get(name)
        if entry is None:
            continue
        if isinstance(entry, Mapping):
            mean = finite_or_zero(entry.get("mean", 0.0))
            std = finite_or_zero(entry.get("std", 0.0))
        else:
            mean = finite_or_zero(entry)
            std = 0.0
        if baseline_std is not None and name in baseline_std:
            std = finite_or_zero(baseline_std.get(name))
        floor = max(1e-6, 0.05 * abs(mean))
        effective_std = max(std, floor) / scale
        z = (value - mean) / effective_std
        result[name] = _round(max(-_Z_CLIP, min(_Z_CLIP, z)))
    return result


def distinctive_features(
    features: Mapping[str, Any],
    baseline: Mapping[str, Any] | None = None,
    *,
    min_abs_z: float = 1.0,
    block_count: int | None = None,
) -> list[dict[str, Any]]:
    """相对基线偏离显著（|z| ≥ min_abs_z）的特征，按 |z| 降序：``[{"feature", "z", "direction"}]``；无基线时空表。"""
    if baseline is None:
        baseline = vs.load_voice_baseline()
    baseline_features = baseline.get("features") if isinstance(baseline, Mapping) else None
    if not isinstance(baseline_features, Mapping):
        return []
    scores = feature_z_scores(features, baseline_features, block_count=block_count)
    selected = [
        {"feature": name, "z": z, "direction": "high" if z > 0 else "low"}
        for name, z in scores.items()
        if abs(z) >= min_abs_z
    ]
    selected.sort(key=lambda item: (-abs(item["z"]), item["feature"]))
    return selected
