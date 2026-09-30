"""三个灾难（脊柱标记）在场与章上的读法（纯函数）。

灾难标记既认场上的 ``spine`` 列，也认功能标签里的「灾难一 / 二 / 三」（``spine_from_role``）。
"""

from __future__ import annotations

import re
from typing import Any

from novel_system.db.models import SnowflakeScenePlan
from novel_system.services.snowflake_chapter_table import SPINE_MARKS

# chapter_role 里的灾难标记。提示词给模型的范例就是「灾难一·一幕高潮」——只认「灾一」的旧正则
# 连自己文档里的例子都匹配不上，于是模型生成的场景表永远没有锚，三幕铰链从不生效
# （2026-09-18 真实数据：17 场里三场标着 灾难一 / 灾难二 / 灾难三，分章却报「没有任何一章标着灾一」）。
_SPINE_ROLE_PATTERN = re.compile(
    r"灾难?\s*([一二三123])"
    r"|第\s*([一二三123])\s*(?:个|次|场|重)?\s*灾难?"
    r"|disaster\s*#?\s*([123])",
    re.IGNORECASE,
)
_SPINE_BY_ORDINAL = {"一": "灾一", "1": "灾一", "二": "灾二", "2": "灾二", "三": "灾三", "3": "灾三"}


def scene_spine(plan: SnowflakeScenePlan) -> str:
    """场景的脊柱标记：作者显式标注优先，其次从 chapter_role 里认灾难标记。

    提示词要求 LLM 「mark the three disaster scenes in chapter_role」（例如
    「灾难一·一幕高潮」），所以 LLM 生成的场景表没有显式 spine 也能锚定。
    """
    explicit = str(getattr(plan, "spine", "") or "").strip()
    if explicit:
        return explicit if explicit in SPINE_MARKS else ""
    return spine_from_role(plan.chapter_role)


def spine_from_role(chapter_role: Any) -> str:
    """功能标签里的灾难标记：灾一 / 灾难一 / 灾难 1 / 第一个灾难 / Disaster 1 → ``灾一``。"""
    match = _SPINE_ROLE_PATTERN.search(str(chapter_role or ""))
    if not match:
        return ""
    ordinal = next((group for group in match.groups() if group), "")
    return _SPINE_BY_ORDINAL.get(ordinal, "")


def spine_positions(scenes: list[SnowflakeScenePlan]) -> dict[str, int]:
    """每个灾难标记落在第几场（按传入顺序，-1 = 没有）。

    作者显式标的（``spine`` 列）压过从功能标签里认出来的；同一个标记出现多次时取**最后**一场——
    灾难是它所在章的收束点，一个两场连打的灾难要在第二场之后才断章。
    """
    positions: dict[str, int] = {}
    for mark in SPINE_MARKS:
        explicit = [
            index for index, scene in enumerate(scenes) if str(getattr(scene, "spine", "") or "").strip() == mark
        ]
        inferred = [index for index, scene in enumerate(scenes) if scene_spine(scene) == mark]
        hits = explicit or inferred
        positions[mark] = hits[-1] if hits else -1
    return positions
