"""风格参考 v3 — 场面 / 情绪标签的唯一词表（叶子模块）。

两处共用同一套词：学习作业给全书窗口打标签（``style_reference_windows.tags_json``），场景蓝图在有风格绑定时
给这一场标同样的标签（``situation_tags``）——选窗按两边的重合挑「作者写同类场面的原文」。手法（devices）是
书特有的，来自文风卡各维的 ``devices``，不在这里。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

SITUATION_TAGS: tuple[str, ...] = (
    "日常闲谈",
    "对峙审问",
    "争吵冲突",
    "打斗追逐",
    "危机应对",
    "独处内省",
    "回忆往事",
    "说明设定",
    "群像场面",
    "情感交流",
    "喜剧桥段",
    "悬疑揭示",
    "赶路转场",
    "计划商议",
    "开章引入",
    "收章落点",
)
MOOD_TAGS: tuple[str, ...] = (
    "紧张",
    "诙谐",
    "伤感",
    "温情",
    "压抑",
    "热血",
    "荒诞",
    "悬疑",
    "平静",
    "恐惧",
)
TAGS_VERSION = "window_tags_v1"
MAX_SITUATIONS = 3
MAX_MOODS = 2
MAX_DEVICES = 5
DEVICE_MAX_CHARS = 8
GIST_MAX_CHARS = 40


def _pick(values: Any, vocabulary: Iterable[str], limit: int) -> list[str]:
    allowed = tuple(vocabulary)
    out: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        text = str(value or "").strip()
        if text in allowed and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def normalize_window_tags(raw: Mapping[str, Any] | None, *, devices: Iterable[str] = ()) -> dict[str, Any]:
    """模型给的一窗标签 → 规范形状（词表外的场面 / 情绪丢弃；手法限于这本书文风卡里的手法名）。"""
    raw = raw if isinstance(raw, Mapping) else {}
    device_vocab = tuple(str(d).strip() for d in devices if str(d).strip())
    raw_devices = raw.get("devices")
    picked_devices: list[str] = []
    for value in raw_devices if isinstance(raw_devices, (list, tuple)) else []:
        text = str(value or "").strip()[:DEVICE_MAX_CHARS]
        if text and text not in picked_devices and (not device_vocab or text in device_vocab):
            picked_devices.append(text)
        if len(picked_devices) >= MAX_DEVICES:
            break
    return {
        "situations": _pick(raw.get("situations"), SITUATION_TAGS, MAX_SITUATIONS),
        "moods": _pick(raw.get("moods"), MOOD_TAGS, MAX_MOODS),
        "devices": picked_devices,
        "gist": str(raw.get("gist") or "").strip()[:GIST_MAX_CHARS],
    }


def normalize_situation_tags(values: Any) -> list[str]:
    """场景蓝图的 ``situation_tags`` → 词表内、去重、至多 3 个。"""
    return _pick(values, SITUATION_TAGS, MAX_SITUATIONS)


__all__ = [
    "DEVICE_MAX_CHARS",
    "GIST_MAX_CHARS",
    "MAX_DEVICES",
    "MAX_MOODS",
    "MAX_SITUATIONS",
    "MOOD_TAGS",
    "SITUATION_TAGS",
    "TAGS_VERSION",
    "normalize_situation_tags",
    "normalize_window_tags",
]
