"""风格参考 v3 — 近期常见偏差（N7）：同一作品最近几次读数里反复越界的地方。

读数入库之后（``style_fidelity_readings``，P5b 的 ``readings.record_fidelity_reading`` 写），首稿的文风卡末尾补一段
「近期常见偏差」（≤3 行白话），选窗再多挑示范这些维手法的窗口。这里只读：

- :func:`recent_gaps_for_project`：作品 + 画像最近 5 次读数里越界 ≥3 次的特征 → 白话短语（``fidelity.recent_gap_phrases``）；
- :func:`gap_dimensions`：短语（或直接给的维键）→ 维度（特征短语表反查），选窗的手法配额用。

没有读数（读数表还空、作品刚绑定）→ 空，渲染与选窗照旧。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleFidelityReading
from novel_system.services.style_reference.binding_config import ALL_DIMENSIONS
from novel_system.services.style_reference.fidelity import (
    FEATURE_DIMENSIONS,
    FEATURE_PHRASES,
    recent_gap_phrases,
)

logger = logging.getLogger(__name__)

RECENT_READINGS_WINDOW = 5
RECENT_GAP_MIN_HITS = 3
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
    """作品最近 5 次读数（同一画像）里越界 ≥3 次的特征的白话短语，至多 ``limit`` 条；读不到 → ``()``。"""
    if not project_id:
        return ()
    try:
        stmt = select(StyleFidelityReading).where(StyleFidelityReading.project_id == str(project_id))
        if profile_id:
            stmt = stmt.where(StyleFidelityReading.profile_id == str(profile_id))
        rows = list(
            session.scalars(
                stmt.order_by(StyleFidelityReading.created_at.desc()).limit(RECENT_READINGS_WINDOW)
            ).all()
        )
    except Exception:  # noqa: BLE001 — 读数表读不到只是少一段补充强调
        logger.debug("recent fidelity readings unavailable for %s", project_id, exc_info=True)
        return ()
    if not rows:
        return ()
    return tuple(
        recent_gap_phrases(rows, min_hits=RECENT_GAP_MIN_HITS, window=RECENT_READINGS_WINDOW, limit=limit)
    )


__all__ = [
    "RECENT_GAP_LIMIT",
    "RECENT_GAP_MIN_HITS",
    "RECENT_READINGS_WINDOW",
    "gap_dimensions",
    "recent_gaps_for_project",
]
