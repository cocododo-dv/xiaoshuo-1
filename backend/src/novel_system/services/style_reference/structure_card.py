"""风格参考 · 结构画像（2026-09-12 结构跟随，Step 2 Track B）：合成期从段落表**确定性**算出的一张结构画像（无 LLM）。

- :func:`compute_structure_card` → ``profile_json["structure_card"]``：按章题切章（``book_text.split_book_chapters``）；
  每章字数 / 段数 / 对白段占比 / 开章段型与首段摘录 / 收章段型与末段摘录；全书章数、章长分位、每章段数、段型比重、
  开章 / 收章段型分布、人称（取自 voice_signature）、章首 / 章尾样例（≤3 × ≤150 字，跨全书首 / 中 / 末取样）、
  章题形态与题名样例（:func:`chapter_titles_summary`）。无章标记 → 全书一章、``has_chapter_markers=False``。
- :func:`reference_scene_scale`：风格直起下「这位作者的一场多长」。

画像随 ``runtime_contract`` 的冻结白名单进契约；渲染成中文块在 ``structure_render``。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from novel_system.services.style_reference.book_text import (
    TITLE_MAX_CHARS,
    collapse_ws,
    marker_style,
    split_book_chapters,
    title_name_part,
)
from novel_system.services.style_reference.budget_config import REFERENCE_SCENE_CHARS_MAX
from novel_system.services.style_reference.measure import quoted_char_share
from novel_system.services.value_coercion import quantile

# 2026-09-22 结构跟随参考书:v2 多了 ``chapter_titles``(章题形态与题名样例);旧画像缺键时
# ``planning_context`` 按段落表惰性补算,不要求重新合成。
STRUCTURE_CARD_VERSION = "structure_card_v2"
STRUCTURE_SAMPLE_MAX_CHARS = 150
STRUCTURE_SAMPLES_PER_SIDE = 3
# 章题样例条数(跨全书均匀取样)与单条上限(与章题判定 book_text.TITLE_MAX_CHARS 同口径)
STRUCTURE_TITLE_SAMPLES = 8
STRUCTURE_TITLE_MAX_CHARS = TITLE_MAX_CHARS
# 2026-09-22 结构跟随参考书:style_first 下按参考章长推场长时的上限(字),防止 2 场的章把一场
# 推成万字;可由 injection_budget.yaml 的 style_first_reference_scene_chars_max 覆盖(缺省只在 budget_config 写一次)。
REFERENCE_SCENE_CHARS_CEILING = REFERENCE_SCENE_CHARS_MAX
REFERENCE_SCENE_CHARS_FLOOR = 300
# 每章条目（含 ≤150 字摘录）随契约冻结进每个 SceneBundle：条目数封顶、跨全书均匀取样，
# 长篇不会把画像撑成几十 KB。样例另取（≤3 × 2 侧），不受此上限影响。
_MAX_CHAPTER_ENTRIES = 24
# 2026-09-14(WP5):章首 / 章尾样例只从「像一章」的章里取——短于此字数的「章」多半是卷首语 /
# 内容简介 / 目录残片;含书名页标记(某某 著 / 简介)的前置章整章不入统计与样例。
_SAMPLE_CHAPTER_MIN_CHARS = 1200
_PARAGRAPH_TYPE_ORDER: tuple[str, ...] = (
    "dialogue",
    "narration",
    "psychology",
    "description_env",
    "description_char",
    "action",
    "flashback",
    "transition",
)
# 人称判定阈值与 voice_signature.render_voice_habits §12 一致。
_PERSON_FIRST_MIN = 0.55
_PERSON_THIRD_MIN = 0.6
_PERSON_SECOND_MIN = 0.4


def chapter_titles_summary(titles: Sequence[str]) -> dict[str, Any]:
    """章题画像:多少章有题、主要形态、题名字数分位、跨全书均匀取的题名样例(只取主要形态的章题,
    书名页的《书名》行之类少数派不入样例)。纯 JSON 值;没有章题 → ``count`` 为 0。"""
    cleaned = [" ".join(str(item or "").split())[:STRUCTURE_TITLE_MAX_CHARS] for item in titles]
    cleaned = [item for item in cleaned if item]
    summary: dict[str, Any] = {
        "count": len(cleaned),
        "marker_style": None,
        "named_count": 0,
        "name_chars": {"median": 0, "p10": 0, "p90": 0},
        "samples": [],
    }
    if not cleaned:
        return summary
    styles = Counter(marker_style(item) for item in cleaned)
    dominant = styles.most_common(1)[0][0]
    summary["marker_style"] = dominant
    named = [(item, title_name_part(item)) for item in cleaned if marker_style(item) == dominant]
    named = [(item, name) for item, name in named if name]
    summary["named_count"] = sum(1 for item in cleaned if title_name_part(item))
    if named:
        summary["name_chars"] = _spread([len(name) for _item, name in named])
        slots = _spread_indexes(len(named), STRUCTURE_TITLE_SAMPLES)
        summary["samples"] = [named[index][1] for index in slots]
    return summary


def reference_scene_scale(
    card: Mapping[str, Any] | None,
    *,
    scenes_in_chapter: int,
    ceiling: int = REFERENCE_SCENE_CHARS_CEILING,
) -> dict[str, Any] | None:
    """参考作者「一场多长」的推算(2026-09-22 结构跟随参考书)。

    有显式场界的书直接用场长中位;没有的书(多数)按 **章长中位 ÷ 本作品这一章的场数** 推——
    读者感受到的是章的体量,本作品一章切几场是作者的分章决定,两者相除就是这本书的场该有的尺度。
    单章书(章长 = 全书)不推。结果封顶 ``ceiling``、保底 ``REFERENCE_SCENE_CHARS_FLOOR``。
    """
    if not isinstance(card, Mapping) or int(card.get("chapter_count") or 0) <= 1:
        return None
    chapter_chars = card.get("chapter_chars") if isinstance(card.get("chapter_chars"), Mapping) else {}
    chapter_median = int(chapter_chars.get("median") or 0)
    scene_chars = card.get("scene_chars") if isinstance(card.get("scene_chars"), Mapping) else {}
    explicit_median = (
        int(scene_chars.get("median") or 0) if str(card.get("scene_break_style") or "") == "explicit" else 0
    )
    scenes = max(1, int(scenes_in_chapter or 0))
    if explicit_median > 0:
        basis, derived = "explicit_scene_breaks", explicit_median
    elif chapter_median > 0:
        basis, derived = "chapter_median_over_scenes", int(round(chapter_median / scenes))
    else:
        return None
    limit = max(REFERENCE_SCENE_CHARS_FLOOR, int(ceiling or REFERENCE_SCENE_CHARS_CEILING))
    return {
        "basis": basis,
        "derived_scene_chars": max(REFERENCE_SCENE_CHARS_FLOOR, min(derived, limit)),
        "raw_scene_chars": derived,
        "chapter_chars": {
            "median": chapter_median,
            "p10": int(chapter_chars.get("p10") or 0),
            "p90": int(chapter_chars.get("p90") or 0),
        },
        "scene_chars_median": explicit_median or None,
        "scenes_in_chapter": scenes,
        "ceiling": limit,
    }


def _spread(values: Sequence[int]) -> dict[str, int]:
    return {
        "median": int(round(quantile(values, 0.5))),
        "p10": int(round(quantile(values, 0.1))),
        "p90": int(round(quantile(values, 0.9))),
    }


def _head(text: str, limit: int = STRUCTURE_SAMPLE_MAX_CHARS) -> str:
    return text[:limit]


def _tail(text: str, limit: int = STRUCTURE_SAMPLE_MAX_CHARS) -> str:
    return text[-limit:] if len(text) > limit else text


def _share(part: int, total: int) -> float:
    return round(part / total, 3) if total > 0 else 0.0


def _chapter_entry(index: int, rows: Sequence[tuple[str, str, int]], scene_breaks: int = 0) -> dict[str, Any]:
    opening_text, opening_type, _opening_index = rows[0]
    closing_text, closing_type, _closing_index = rows[-1]
    char_count = sum(len(text) for text, _ptype, _index in rows)
    return {
        "index": index,
        "char_count": char_count,
        "paragraph_count": len(rows),
        "dialogue_share": _share(sum(1 for _text, ptype, _index in rows if ptype == "dialogue"), len(rows)),
        # 2026-09-14(WP5):显式场界数;有场界的章按 (场界 + 1) 算场数
        "scene_breaks": int(scene_breaks),
        "scene_count": int(scene_breaks) + 1 if scene_breaks > 0 else None,
        "opening_type": opening_type,
        "opening_excerpt": _head(opening_text),
        "opening_chars": len(opening_text),
        "closing_type": closing_type,
        "closing_excerpt": _tail(closing_text),
        "closing_chars": len(closing_text),
    }


def _spread_indexes(count: int, limit: int) -> list[int]:
    """跨全书均匀取 ≤limit 个章索引（含首末章），保持升序、去重。"""
    if count <= 0:
        return []
    if count <= limit:
        return list(range(count))
    if limit == 1:
        return [0]
    picked = {int(round(step * (count - 1) / (limit - 1))) for step in range(limit)}
    return sorted(picked)


def _person_profile(voice_signature: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(voice_signature, Mapping):
        return None
    features = voice_signature.get("features")
    if not isinstance(features, Mapping):
        return None

    def _value(key: str) -> float:
        try:
            return float(features.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    first = _value("person_first_share")
    second = _value("person_second_share")
    third = _value("person_third_share")
    if first + second + third <= 0:
        return None
    if first >= _PERSON_FIRST_MIN:
        dominant = "first"
    elif third >= _PERSON_THIRD_MIN:
        dominant = "third"
    elif second >= _PERSON_SECOND_MIN:
        dominant = "second"
    else:
        dominant = "mixed"
    return {
        "dominant": dominant,
        "first_share": round(first, 3),
        "second_share": round(second, 3),
        "third_share": round(third, 3),
    }


def compute_structure_card(
    paragraphs: Iterable[Any],
    *,
    voice_signature: Mapping[str, Any] | None = None,
    scene_breaks: Iterable[int] | None = None,
) -> dict[str, Any]:
    """从段落表确定性算出结构画像（无 LLM）。

    ``paragraphs`` 元素可以是 ORM 段落行或 ``{"text", "paragraph_type", "paragraph_index"}``
    映射；``scene_breaks`` 是导入期记录的空行型场界（其后有场界的段索引）。返回值是纯 JSON 值
    （int / float / str / list / dict），可直接落 ``profile_json``。
    """
    book_chapters, markers = split_book_chapters(paragraphs, scene_breaks=scene_breaks)
    chapters = [
        [(collapse_ws(row["text"]), row["ptype"], row["index"]) for row in chapter.rows] for chapter in book_chapters
    ]
    breaks_per_chapter = [chapter.scene_breaks for chapter in book_chapters]
    titles = [chapter.title for chapter in book_chapters]
    has_markers = bool(markers)
    card: dict[str, Any] = {
        "version": STRUCTURE_CARD_VERSION,
        "has_chapter_markers": has_markers,
        "chapter_marker_style": markers.most_common(1)[0][0] if has_markers else None,
        "chapter_count": len(chapters),
        "total_chars": 0,
        "paragraph_count": 0,
        "paragraph_mean_chars": 0,
        "chapter_chars": {"median": 0, "p10": 0, "p90": 0},
        "paragraphs_per_chapter": {"median": 0, "p10": 0, "p90": 0},
        "dialogue_share": 0.0,
        "dialogue_char_share": 0.0,
        "paragraph_type_shares": {},
        "opening_type_distribution": {},
        "closing_type_distribution": {},
        "person": _person_profile(voice_signature),
        # 2026-09-14(WP5):场级——显式场界(纯符号行 / 空行型)覆盖的章数、每章场数与场长分布
        "scene_break_style": "none",
        "scene_break_chapters": 0,
        "scenes_per_chapter": {"median": 0, "p10": 0, "p90": 0},
        "scene_chars": {"median": 0, "p10": 0, "p90": 0},
        "chapters": [],
        "chapters_listed": 0,
        "samples": {"chapter_openings": [], "chapter_endings": []},
        # 2026-09-22 结构跟随参考书:章题形态与题名样例(AI 起章名照此起)
        "chapter_titles": chapter_titles_summary(titles),
    }
    if not chapters:
        return card

    body_rows = [row for chapter in chapters for row in chapter]
    total_chars = sum(len(text) for text, _ptype, _index in body_rows)
    paragraph_count = len(body_rows)
    type_counts = Counter(ptype for _text, ptype, _index in body_rows)
    entries = [
        _chapter_entry(index + 1, chapter, breaks_per_chapter[index] if index < len(breaks_per_chapter) else 0)
        for index, chapter in enumerate(chapters)
    ]
    for index, entry in enumerate(entries):
        entry["title"] = titles[index] if index < len(titles) else ""
    with_breaks = [entry for entry in entries if entry["scene_count"]]
    if with_breaks and len(with_breaks) * 10 >= len(entries) * 3:
        card.update(
            {
                "scene_break_style": "explicit",
                "scene_break_chapters": len(with_breaks),
                "scenes_per_chapter": _spread([int(entry["scene_count"]) for entry in with_breaks]),
                "scene_chars": _spread(
                    [int(round(entry["char_count"] / entry["scene_count"])) for entry in with_breaks]
                ),
            }
        )
    else:
        card["scene_break_chapters"] = len(with_breaks)

    listed = _spread_indexes(len(entries), _MAX_CHAPTER_ENTRIES)
    sample_pool = [index for index, entry in enumerate(entries) if entry["char_count"] >= _SAMPLE_CHAPTER_MIN_CHARS]
    if not sample_pool:
        sample_pool = list(range(len(entries)))
    sample_slots = [sample_pool[index] for index in _spread_indexes(len(sample_pool), STRUCTURE_SAMPLES_PER_SIDE)]
    card.update(
        {
            "total_chars": total_chars,
            "paragraph_count": paragraph_count,
            "paragraph_mean_chars": int(round(total_chars / paragraph_count)) if paragraph_count else 0,
            "chapter_chars": _spread([entry["char_count"] for entry in entries]),
            "paragraphs_per_chapter": _spread([entry["paragraph_count"] for entry in entries]),
            "dialogue_share": _share(type_counts.get("dialogue", 0), paragraph_count),
            # 2026-09-23:对白字数占比用测量核的唯一定义(引号内可见字 ÷ 可见字),不再按段型标签算
            "dialogue_char_share": round(
                quoted_char_share(row["text"] for chapter in book_chapters for row in chapter.rows), 3
            ),
            "paragraph_type_shares": {
                ptype: _share(type_counts.get(ptype, 0), paragraph_count)
                for ptype in _PARAGRAPH_TYPE_ORDER
                if type_counts.get(ptype, 0) > 0
            },
            "opening_type_distribution": dict(
                Counter(entry["opening_type"] for entry in entries).most_common()
            ),
            "closing_type_distribution": dict(
                Counter(entry["closing_type"] for entry in entries).most_common()
            ),
            "chapters": [entries[index] for index in listed],
            "chapters_listed": len(listed),
            "samples": {
                "chapter_openings": [
                    {
                        "chapter_index": entries[index]["index"],
                        "paragraph_type": entries[index]["opening_type"],
                        "text": entries[index]["opening_excerpt"],
                        "truncated": entries[index]["opening_chars"] > STRUCTURE_SAMPLE_MAX_CHARS,
                    }
                    for index in sample_slots
                ],
                "chapter_endings": [
                    {
                        "chapter_index": entries[index]["index"],
                        "paragraph_type": entries[index]["closing_type"],
                        "text": entries[index]["closing_excerpt"],
                        "truncated": entries[index]["closing_chars"] > STRUCTURE_SAMPLE_MAX_CHARS,
                    }
                    for index in sample_slots
                ],
            },
        }
    )
    return card


__all__ = [
    "REFERENCE_SCENE_CHARS_CEILING",
    "REFERENCE_SCENE_CHARS_FLOOR",
    "STRUCTURE_CARD_VERSION",
    "STRUCTURE_SAMPLES_PER_SIDE",
    "STRUCTURE_SAMPLE_MAX_CHARS",
    "STRUCTURE_TITLE_MAX_CHARS",
    "STRUCTURE_TITLE_SAMPLES",
    "chapter_titles_summary",
    "compute_structure_card",
    "reference_scene_scale",
]
