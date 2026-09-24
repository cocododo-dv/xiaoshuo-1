"""风格参考 v3 — 窗口标签的唯一词表（叶子模块）。

两处共用同一套场面 / 情绪词：学习作业给窗口打标签（``style_reference_windows.tags_json``），场景蓝图在有风格绑定时
给这一场标同样的标签（``situation_tags``）——选窗按两边的重合挑「作者写同类场面的原文」。

2026-09-24（§8 O1）：标签不再依赖文风卡。v1 的「手法」（``devices``，取自文风卡各维的手法名）删掉，换成 ``dimensions``
= 这一窗最能示范的 ≤3 个维度（``dimensions.SubDimension`` 的 16 个键，中文名见 ``card.DIMENSION_LABELS``）；选窗的
「维度示范」配额按它挑。这样标签只取决于原文，重新学习不用给全书重打（``TAGS_VERSION`` 升到 v2 时重打一次）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from novel_system.services.style_reference.dimensions import SubDimension

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
DIMENSION_KEYS: tuple[str, ...] = tuple(dim.value for dim in SubDimension)
TAGS_VERSION = "window_tags_v2"
MAX_SITUATIONS = 3
MAX_MOODS = 2
MAX_DIMENSIONS = 3
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


def normalize_window_tags(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """模型给的一窗标签 → 规范形状 ``{situations, moods, dimensions, gist}``：词表外的场面 / 情绪丢弃，维度只认 16 个键
    （去重、至多 3 个），别的键（v1 的 ``devices`` 等）不进库。"""
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "situations": _pick(raw.get("situations"), SITUATION_TAGS, MAX_SITUATIONS),
        "moods": _pick(raw.get("moods"), MOOD_TAGS, MAX_MOODS),
        "dimensions": _pick(raw.get("dimensions"), DIMENSION_KEYS, MAX_DIMENSIONS),
        "gist": str(raw.get("gist") or "").strip()[:GIST_MAX_CHARS],
    }


def normalize_situation_tags(values: Any) -> list[str]:
    """场景蓝图的 ``situation_tags`` → 词表内、去重、至多 3 个。"""
    return _pick(values, SITUATION_TAGS, MAX_SITUATIONS)


__all__ = [
    "DIMENSION_KEYS",
    "GIST_MAX_CHARS",
    "MAX_DIMENSIONS",
    "MAX_MOODS",
    "MAX_SITUATIONS",
    "MOOD_TAGS",
    "SITUATION_TAGS",
    "TAGS_VERSION",
    "normalize_situation_tags",
    "normalize_window_tags",
]
