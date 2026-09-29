"""风格参考 — 从冻结的运行时契约里读跨场景声音参照（纯函数，不写库、不调 LLM）。

- :func:`contract_deliberate_repetition`：任一层画像标了刻意复沓（新鲜度预算据此不把作者的复沓当重复）。

风格参考 v3（2026-09-23）删掉了这里的「漂移驾驶」两端：归档期的 ``observe_style_drift``（写
``style_drift_observed`` 事件，以「一般中文小说」基线为尺度，对作者自己的书 95–98% 报警）与
下一场 bundle 读它的 ``latest_drift_event`` / ``latest_drift_calibration``（渲染 ``style_drift_calibration``
段、改选窗、关轮换）。像不像改由以作者自己的窗口为参照的读数衡量（``fidelity.py`` / ``readings.py``），
读数不按章键、不回灌进下一场的提示。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from novel_system.services.value_coercion import finite_or_none_accepting_bool


def _contract_layers(contract: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """契约层按 ``order`` 升序（泛 → 具体）；坏形状退化为空。"""
    if not isinstance(contract, Mapping):
        return []
    raw_layers = contract.get("layers")
    if not isinstance(raw_layers, Sequence) or isinstance(raw_layers, (str, bytes)):
        return []
    layers = [layer for layer in raw_layers if isinstance(layer, Mapping)]

    def order_of(layer: Mapping[str, Any]) -> int:
        value = finite_or_none_accepting_bool(layer.get("order"))
        return int(value) if value is not None else 0

    return [dict(layer) for layer in sorted(layers, key=order_of)]


def _layer_profile_json(layer: Mapping[str, Any]) -> dict[str, Any]:
    profile = layer.get("profile")
    if not isinstance(profile, Mapping):
        return {}
    profile_json = profile.get("profile_json")
    return dict(profile_json) if isinstance(profile_json, Mapping) else {}


def contract_deliberate_repetition(contract: Mapping[str, Any] | None) -> bool:
    """任一层画像标记 ``voice_signature.deliberate_repetition=true`` → True。"""
    for layer in _contract_layers(contract):
        voice = _layer_profile_json(layer).get("voice_signature")
        if isinstance(voice, Mapping) and voice.get("deliberate_repetition") is True:
            return True
    return False


__all__ = ["contract_deliberate_repetition"]
