from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


# The goal the old catalog wrote into every new scene card. A hand-made
# placeholder chapter whose scene still carries it counts as untouched
# (catalog_placeholders), and it is one of the scaffolds below.
SCENE_GOAL_SCAFFOLD = "（本场目标待规划）"

# The canned chapter text the snowflake outline builder wrote into every
# materialized chapter until 2026-10-01 (emotional_target / ending_effect /
# must_not / notes). Nobody authored it; chapters materialized before then
# still carry it until the next 确认写入 rewrites the fields.
RETIRED_CHAPTER_BOILERPLATE = frozenset(
    {
        "让人物目标、阻碍和代价在行动中显形。",
        "用新的选择、代价或信息推动下一章。",
        "不得复制参考书原文表达、人物、设定或桥段。",
        "由雪花法分章物化，需确认后进入逐章运行。",
    }
)

# Historical UI scaffolds and canned text that were once persisted as if they
# were authored story facts. Matching is exact after trim: prose that merely
# contains one of these words is never altered.
EMPTY_STORY_SLOT_VALUES = frozenset(
    {
        "",
        "—",
        "待定",
        "（待规划）",
        "(待规划)",
        SCENE_GOAL_SCAFFOLD,
        "(本场目标待规划)",
        "待补",
        *RETIRED_CHAPTER_BOILERPLATE,
    }
)


def normalize_story_slot(value: Any) -> str:
    """Return an authored scalar, or empty text for an exact old scaffold."""

    text = "" if value is None else str(value).strip()
    return "" if text in EMPTY_STORY_SLOT_VALUES else text


def planned_text(value: Any) -> str | None:
    """A stored plan field as the author wrote it, or ``None`` when nothing was
    planned (missing, blank, or an exact old scaffold / canned text). Payloads
    that show plan fields use it so a scaffold never reads as the author's."""

    if value is None:
        return None
    return value if normalize_story_slot(value) else None


def normalize_story_slot_mapping(
    value: Mapping[str, Any] | None,
    *,
    fields: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Normalize only named top-level business slots in a mapping.

    This deliberately does not recurse into arbitrary author prose or nested
    knowledge objects. Callers name the business fields exposed to planning or
    generation prompts.
    """

    result = dict(value or {})
    selected = tuple(fields) if fields is not None else tuple(result)
    for field in selected:
        if field not in result:
            continue
        raw = result[field]
        if raw is None or isinstance(raw, (str, int, float, bool)):
            result[field] = normalize_story_slot(raw)
    return result
