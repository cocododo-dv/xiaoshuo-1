"""风格参考 v3 — 近期常见偏差（N7）：同一作品最近几次读数里反复越界的地方。

读数入库之后（``style_fidelity_readings``，``readings.record_fidelity_reading`` 写），首稿的文风卡末尾补一段
「近期常见偏差」（≤3 行白话），选窗再多挑示范这些维手法的窗口。这里只读：

- :func:`recent_gaps_for_project`：作品 + 画像最近 5 次**首稿**读数里越界 ≥3 次的特征 → 白话短语
  （``readings.recent_gaps`` → ``fidelity.recent_gap_phrases``）；
- :func:`gap_dimensions`：短语（或直接给的维键）→ 维度（特征短语表反查），选窗的手法配额用。

没有读数（读数表还空、作品刚绑定）→ 空，渲染与选窗照旧。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from functools import lru_cache

from sqlalchemy.orm import Session

from novel_system.services.style_reference.binding_config import ALL_DIMENSIONS
from novel_system.services.style_reference.fidelity import (
    FEATURE_DIMENSIONS,
    FEATURE_PHRASES,
)
from novel_system.services.style_reference.readings import (
    RECENT_GAP_MIN_HITS,
    RECENT_READINGS_WINDOW,
    recent_gaps,
)

logger = logging.getLogger(__name__)

RECENT_GAP_LIMIT = 3


@lru_cache(maxsize=1)
def _phrase_dimensions() -> dict[str, str]:
    table: dict[str, str] = {}
    for feature, phrases in FEATURE_PHRASES.items():
        dimension = FEATURE_DIMENSIONS.get(feature)
        if not dimension:
            continue
        for phrase in phrases.values():
            table.setdefault(str(phrase), dimension)
    return table


def gap_dimensions(gaps: Iterable[str]) -> list[str]:
    """偏差短语 → 维度（按出现顺序去重）；也接受直接给的维键（``language.rhetoric``）。认不出的短语忽略。"""
    table = _phrase_dimensions()
    out: list[str] = []
    for gap in gaps or ():
        text = str(gap or "").strip()
        dimension = text if text in ALL_DIMENSIONS else table.get(text)
        if dimension and dimension not in out:
            out.append(dimension)
    return out


def recent_gaps_for_project(
    session: Session,
    *,
    project_id: str | None,
    profile_id: str | None,
    limit: int = RECENT_GAP_LIMIT,
) -> tuple[str, ...]:
    """作品最近 5 次首稿读数（同一画像）里越界 ≥3 次的特征的白话短语，至多 ``limit`` 条；读不到 → ``()``。

    票源只有管线的首稿读数（``readings.recent_gap_readings``）：同一场的修改 / 补丁 / 终稿读数不重复计票，
    写作台与对照检查的读数也不算「草稿里反复出现的偏差」。
    """
    if not project_id:
        return ()
    try:
        return tuple(recent_gaps(session, project_id=project_id, profile_id=profile_id, limit=limit))
    except Exception:  # noqa: BLE001 — 读数表读不到只是少一段补充强调
        logger.debug("recent fidelity readings unavailable for %s", project_id, exc_info=True)
        return ()


__all__ = [
    "RECENT_GAP_LIMIT",
    "RECENT_GAP_MIN_HITS",
    "RECENT_READINGS_WINDOW",
    "gap_dimensions",
    "recent_gaps_for_project",
]
