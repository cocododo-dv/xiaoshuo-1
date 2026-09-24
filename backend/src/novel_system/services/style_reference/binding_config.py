"""风格参考 v3 — 绑定配置的唯一解释（叶子模块，不 import 其它风格模块）。

v3 的绑定配置只有四个作者可见的旋钮：

- ``reference_mode``：怎么送参考。``full`` 全面模仿（样例 + 文风卡 + 声音，推荐）/ ``samples_only`` 只用原文样例
  （对照用）/ ``card_only`` 只用文风卡、不发原文（隐私用；``segments_only`` 的书一律按这个渲染）。
- ``sample_windows``：每场的样例窗数（默认 12，范围 0–16）。
- ``dimension_states``：16 维各自 ``emphasize`` 重点 / ``normal`` 正常 / ``exclude`` 不学（缺省全部 normal）。
- ``draft_mode``：``style_first``（作者手笔直起，默认）/ ``neutral_first``（先中性后润色，对照用）。

旧绑定（策略 A/B/C/mixed + intensity + sub_dimensions + include_*）由迁移 ``20260924_0092`` 一次性回填成这四键
（A → card_only、其余 → full；intensity i → round(3 + 9·i/100) 窗；``draft_mode`` 缺键仍在契约构建期取 yaml 默认），
这里不再解释任何旧键：缺键取默认，未知键丢弃。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from novel_system.services.style_reference.dimensions import SubDimension

REFERENCE_MODE_FULL = "full"
REFERENCE_MODE_SAMPLES_ONLY = "samples_only"
REFERENCE_MODE_CARD_ONLY = "card_only"
REFERENCE_MODES = (REFERENCE_MODE_FULL, REFERENCE_MODE_SAMPLES_ONLY, REFERENCE_MODE_CARD_ONLY)

DIMENSION_EMPHASIZE = "emphasize"
DIMENSION_NORMAL = "normal"
DIMENSION_EXCLUDE = "exclude"
DIMENSION_STATES = (DIMENSION_EMPHASIZE, DIMENSION_NORMAL, DIMENSION_EXCLUDE)

DRAFT_MODE_STYLE_FIRST = "style_first"
DRAFT_MODE_NEUTRAL_FIRST = "neutral_first"
DRAFT_MODES = (DRAFT_MODE_STYLE_FIRST, DRAFT_MODE_NEUTRAL_FIRST)

DEFAULT_SAMPLE_WINDOWS = 12
MIN_SAMPLE_WINDOWS = 0
MAX_SAMPLE_WINDOWS = 16

ALL_DIMENSIONS: tuple[str, ...] = tuple(dim.value for dim in SubDimension)


def _clamp_windows(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return DEFAULT_SAMPLE_WINDOWS
    return max(MIN_SAMPLE_WINDOWS, min(MAX_SAMPLE_WINDOWS, number))


def normalize_dimension_states(raw: Any) -> dict[str, str]:
    """``{dim: state}`` → 16 维完整映射（未知维 / 未知状态丢弃，缺省 normal）。"""
    states = {dim: DIMENSION_NORMAL for dim in ALL_DIMENSIONS}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            dim = str(key or "").strip()
            state = str(value or "").strip().lower()
            if dim in states and state in DIMENSION_STATES:
                states[dim] = state
    return states


def normalize_binding_config(config_json: Mapping[str, Any] | None) -> dict[str, Any]:
    """任意绑定配置 → v3 四键（缺键取默认、越界夹回、未知键丢弃）。"""
    config = dict(config_json or {})
    mode = str(config.get("reference_mode") or "").strip().lower()
    if mode not in REFERENCE_MODES:
        mode = REFERENCE_MODE_FULL
    windows = _clamp_windows(config.get("sample_windows")) if "sample_windows" in config else DEFAULT_SAMPLE_WINDOWS
    draft_mode = str(config.get("draft_mode") or "").strip().lower()
    if draft_mode not in DRAFT_MODES:
        draft_mode = DRAFT_MODE_STYLE_FIRST
    return {
        "reference_mode": mode,
        "sample_windows": windows,
        "dimension_states": normalize_dimension_states(config.get("dimension_states")),
        "draft_mode": draft_mode,
    }


def effective_reference_mode(mode: str, *, cloud_policy: str | None) -> str:
    """书的云策略压过绑定：``segments_only`` 的书只送文风卡（不送原文窗口）。"""
    if str(cloud_policy or "") == "segments_only" and mode != REFERENCE_MODE_CARD_ONLY:
        return REFERENCE_MODE_CARD_ONLY
    return mode


def sends_samples(mode: str) -> bool:
    return mode in (REFERENCE_MODE_FULL, REFERENCE_MODE_SAMPLES_ONLY)


def sends_card(mode: str) -> bool:
    return mode in (REFERENCE_MODE_FULL, REFERENCE_MODE_CARD_ONLY)


__all__ = [
    "ALL_DIMENSIONS",
    "DEFAULT_SAMPLE_WINDOWS",
    "DIMENSION_EMPHASIZE",
    "DIMENSION_EXCLUDE",
    "DIMENSION_NORMAL",
    "DIMENSION_STATES",
    "DRAFT_MODES",
    "DRAFT_MODE_NEUTRAL_FIRST",
    "DRAFT_MODE_STYLE_FIRST",
    "MAX_SAMPLE_WINDOWS",
    "MIN_SAMPLE_WINDOWS",
    "REFERENCE_MODES",
    "REFERENCE_MODE_CARD_ONLY",
    "REFERENCE_MODE_FULL",
    "REFERENCE_MODE_SAMPLES_ONLY",
    "effective_reference_mode",
    "normalize_binding_config",
    "normalize_dimension_states",
    "sends_card",
    "sends_samples",
]
