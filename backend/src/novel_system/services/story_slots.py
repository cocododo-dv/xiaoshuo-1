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

# The goal the snowflake outline builder gave a chapter that had neither a goal
# nor a summary, until 2026-10-01: 「推进本章：<the chapter's title, or its
# id>」. The same sentence became that chapter's main_plot_push, and the
# scene_goal (and only beat) of a scene that had neither a summary nor a
# title. Nobody wrote it; rows materialized before then keep it until the next
# 确认写入. It names its chapter, so it is recognized per chapter
# (planned_chapter_goal) — never by the prefix alone.
RETIRED_CHAPTER_GOAL_PREFIX = "推进本章："

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


def chapter_title_candidates(chapter: Any) -> frozenset[str]:
    """The names the retired canned goal may have used for ``chapter`` (a
    ChapterGoal row): its catalog title, the title the last materialization
    seeded, a hand-made chapter's title, and its id (the builder's fallback).

    A chapter renamed at the desk after its last 确认写入 no longer carries the
    old name; its canned goal then goes unrecognized until the next 确认写入
    rewrites it."""

    if chapter is None:
        return frozenset()
    narrative = getattr(chapter, "narrative_json", None)
    brief = getattr(chapter, "writer_brief_json", None)
    narrative = narrative if isinstance(narrative, Mapping) else {}
    brief = brief if isinstance(brief, Mapping) else {}
    names = (
        narrative.get("title"),
        brief.get("chapter_title"),
        brief.get("title"),
        getattr(chapter, "chapter_id", None),
    )
    return frozenset(str(name).strip() for name in names if name is not None and str(name).strip())


def is_retired_chapter_goal(value: Any, titles: Iterable[Any]) -> bool:
    """``value`` is exactly the retired canned goal 「推进本章：<title>」 for one
    of ``titles`` (exact after trim; any other text starting the same way is
    the author's)."""

    text = "" if value is None else str(value).strip()
    if not text.startswith(RETIRED_CHAPTER_GOAL_PREFIX):
        return False
    named = text[len(RETIRED_CHAPTER_GOAL_PREFIX):].strip()
    return any(
        named == str(title).strip()
        for title in titles
        if title is not None and str(title).strip()
    )


def planned_chapter_goal(value: Any, chapter: Any) -> str:
    """A chapter's goal — or a field the canned goal was copied into (its
    main_plot_push, a scene's goal or beat) — exactly as stored when the
    author planned it, or ``""`` when nothing was planned: missing, blank, an
    exact old scaffold, or the retired canned goal of ``chapter``. Prompts
    print no goal line and payloads show the empty state for ``""``."""

    if not normalize_story_slot(value):
        return ""
    if is_retired_chapter_goal(value, chapter_title_candidates(chapter)):
        return ""
    return str(value)


def planned_beats(beats: Any, chapter: Any) -> list[Any]:
    """A scene card's beats without the retired canned goal of ``chapter``
    (the builder's single fallback beat); every other beat is kept as stored."""

    if not isinstance(beats, (list, tuple)):
        return []
    titles = chapter_title_candidates(chapter)
    return [beat for beat in beats if not is_retired_chapter_goal(beat, titles)]


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
