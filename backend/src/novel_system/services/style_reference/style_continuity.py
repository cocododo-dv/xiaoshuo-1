"""风格参考 — 从冻结的运行时契约里读跨场景声音参照（纯函数，不写库、不调 LLM）。

- :func:`contract_deliberate_repetition`：生效的那一层画像标了刻意复沓（新鲜度预算据此不把作者的复沓当重复）。

风格参考 v3（2026-09-23）删掉了这里的「漂移驾驶」两端：归档期的 ``observe_style_drift``（写
``style_drift_observed`` 事件，以「一般中文小说」基线为尺度，对作者自己的书 95–98% 报警）与
下一场 bundle 读它的 ``latest_drift_event`` / ``latest_drift_calibration``（渲染 ``style_drift_calibration``
段、改选窗、关轮换）。像不像改由以作者自己的窗口为参照的读数衡量（``fidelity.py`` / ``readings.py``），
读数不按章键、不回灌进下一场的提示。
"""

from __future__ import annotations

from typing import Any, Mapping

from novel_system.services.style_reference.runtime_contract import contract_layer


def _layer_profile_json(layer: Mapping[str, Any]) -> dict[str, Any]:
    profile = layer.get("profile")
    if not isinstance(profile, Mapping):
        return {}
    profile_json = profile.get("profile_json")
    return dict(profile_json) if isinstance(profile_json, Mapping) else {}


def contract_deliberate_repetition(contract: Mapping[str, Any] | None) -> bool:
    """契约里生效的那一层（``runtime_contract.contract_layer``：最具体的一层，J7——与渲染、策略读的是同一层）的画像
    标了 ``voice_signature.deliberate_repetition=true`` → True。

    原来读「任一层」：多层的 v1 契约里，一个泛一些的层（作品层）标了复沓、真正生效的场景层没标，新鲜度预算照样
    把这一场的重复当成作者的复沓放过（B10-24）。v2 契约只有一层，不受影响。"""
    voice = _layer_profile_json(contract_layer(contract)).get("voice_signature")
    return isinstance(voice, Mapping) and voice.get("deliberate_repetition") is True


__all__ = ["contract_deliberate_repetition"]
