"""风格参考 — 从冻结的运行时契约里读跨场景声音参照（纯函数，不写库、不调 LLM）。

- :func:`contract_voice_reference`：契约各层 ``voice_signature.features`` 的加权均值；
- :func:`contract_deliberate_repetition`：任一层画像标了刻意复沓（新鲜度预算据此不把作者的复沓当重复）。

风格参考 v3（2026-09-23）删掉了这里的「漂移驾驶」两端：归档期的 ``observe_style_drift``（写
``style_drift_observed`` 事件，以「一般中文小说」基线为尺度，对作者自己的书 95–98% 报警）与
下一场 bundle 读它的 ``latest_drift_event`` / ``latest_drift_calibration``（渲染 ``style_drift_calibration``
段、改选窗、关轮换）。像不像改由以作者自己的窗口为参照的读数衡量（``fidelity.py`` / ``readings.py``），
读数不按章键、不回灌进下一场的提示。
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _contract_layers(contract: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """契约层按 ``order`` 升序（泛 → 具体）；坏形状退化为空。"""
    if not isinstance(contract, Mapping):
        return []
    raw_layers = contract.get("layers")
    if not isinstance(raw_layers, Sequence) or isinstance(raw_layers, (str, bytes)):
        return []
    layers = [layer for layer in raw_layers if isinstance(layer, Mapping)]

    def order_of(layer: Mapping[str, Any]) -> int:
        value = _finite(layer.get("order"))
        return int(value) if value is not None else 0

    return [dict(layer) for layer in sorted(layers, key=order_of)]


def _layer_profile_json(layer: Mapping[str, Any]) -> dict[str, Any]:
    profile = layer.get("profile")
    if not isinstance(profile, Mapping):
        return {}
    profile_json = profile.get("profile_json")
    return dict(profile_json) if isinstance(profile_json, Mapping) else {}


def _layer_voice_features(layer: Mapping[str, Any]) -> dict[str, float]:
    voice = _layer_profile_json(layer).get("voice_signature")
    if not isinstance(voice, Mapping):
        return {}
    features = voice.get("features")
    if not isinstance(features, Mapping):
        return {}
    result: dict[str, float] = {}
    for name, raw in features.items():
        value = _finite(raw)
        if value is not None:
            result[str(name)] = value
    return result


def contract_voice_reference(contract: Mapping[str, Any] | None) -> dict[str, float]:
    """契约各层 ``voice_signature.features`` 的加权均值（泛 → 具体权重 1..n）。

    越具体的层权重越大（2026-09-24 指标基线合并已删，这里是唯一的按层加权）。
    没有任何层带 voice_signature 时返回 ``{}``。
    """
    layers = _contract_layers(contract)
    weighted: list[tuple[float, dict[str, float]]] = []
    for index, layer in enumerate(layers):
        features = _layer_voice_features(layer)
        if features:
            weighted.append((float(index + 1), features))
    if not weighted:
        return {}
    names: set[str] = set()
    for _weight, features in weighted:
        names.update(features)
    blended: dict[str, float] = {}
    for name in sorted(names):
        total = 0.0
        weight_sum = 0.0
        for weight, features in weighted:
            if name in features:
                total += weight * features[name]
                weight_sum += weight
        if weight_sum > 0:
            blended[name] = total / weight_sum
    return blended


def contract_deliberate_repetition(contract: Mapping[str, Any] | None) -> bool:
    """任一层画像标记 ``voice_signature.deliberate_repetition=true`` → True。"""
    for layer in _contract_layers(contract):
        voice = _layer_profile_json(layer).get("voice_signature")
        if isinstance(voice, Mapping) and voice.get("deliberate_repetition") is True:
            return True
    return False


__all__ = ["contract_deliberate_repetition", "contract_voice_reference"]
