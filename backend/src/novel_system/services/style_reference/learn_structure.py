"""风格参考 —「学习文风 · 通读全书」顺带学的结构（2026-10-19）：场景切分与章法。

通读时叙事层本来就读整章，同一次调用顺带交两样东西（模板 ``style_ref_extract_narrative``，只在载荷有 ``scope``
时填，快速学习给空数组）：

- **场景切分** ``scene_starts``：新的一场从哪一段开始、靠什么换场（时间 / 地点 / 视点 / 事件）。《龙族》这类没有
  空行分场的书，过去「一场多长」只能按「章长中位 ÷ 本作品这一章的场数」推（``structure_card.reference_scene_scale``）；
  现在按切出来的场量真实场长；
- **章法** ``chapters``：每个整章片段这一章主要做什么（铺垫 / 推进 / 冲突 / 高潮 / 揭示 / 回收 / 过渡 / 日常）、
  章末怎么收、交织几条线、几个视点。只收类别与数字，不收人名、情节——这些进规划提示，不能带出原书的内容。

本模块是纯函数：:func:`parse_structure_output` 核对一批的输出（段号必须在这一批里、每章第一段不算换场、只认整章
片段与词表里的类别），:func:`learned_scene_stats` 按切出来的场算每章场数与场长，:func:`chapter_craft` 把各章的
章法汇成分布（结构画像的 ``chapter_craft`` 键，渲染在 ``structure_render``）。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from novel_system.services.value_coercion import quantile

CHAPTER_CRAFT_VERSION = "chapter_craft_v1"
SCENE_SHIFTS: tuple[str, ...] = ("时间", "地点", "视点", "事件")
CHAPTER_FUNCTIONS: tuple[str, ...] = ("铺垫", "推进", "冲突", "高潮", "揭示", "回收", "过渡", "日常")
CHAPTER_HOOKS: tuple[str, ...] = ("悬念未解", "反转", "新威胁登场", "情绪落点", "平稳收束", "预告下一步")
MAX_THREADS = 6
MAX_POVS = 6
WHOLE_CHAPTER = "整章"


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_structure_output(structured: Any, ext_set: Any) -> dict[str, Any]:
    """一批的场景切分与章法（``ext_set`` 是这一批的 ``learn_extract.ExtractionSet``）→
    ``{"scene_starts": [[段落序号, 换场方式], …], "chapters": [{"chapter", "function", "hook", "threads", "povs"}, …]}``。

    不抛异常：段号不在这一批里、是某章第一段（那是章的开头，不是换场）、类别不在词表里的项一律丢掉；章法只认
    ``position`` 为「整章」的片段，同一片段只收一次。"""
    out: dict[str, Any] = {"scene_starts": [], "chapters": []}
    if not isinstance(structured, Mapping):
        return out
    paragraphs = getattr(ext_set, "paragraphs", {}) or {}
    first_of_block: set[int] = set()
    seen_blocks: set[int] = set()
    for number in sorted(paragraphs):
        block = int(getattr(paragraphs[number], "block", 0) or 0)
        if block not in seen_blocks:
            seen_blocks.add(block)
            first_of_block.add(number)
    starts: dict[int, str] = {}
    for item in structured.get("scene_starts") if isinstance(structured.get("scene_starts"), list) else []:
        if not isinstance(item, Mapping):
            continue
        number = _as_int(item.get("p"))
        shift = str(item.get("shift") or "").strip()
        if number is None or number not in paragraphs or number in first_of_block or shift not in SCENE_SHIFTS:
            continue
        starts.setdefault(int(paragraphs[number].paragraph_index), shift)
    out["scene_starts"] = [[index, starts[index]] for index in sorted(starts)]
    blocks = {
        int(window.get("window") or 0): window
        for window in getattr(ext_set, "windows", []) or []
        if isinstance(window, Mapping)
    }
    taken: set[int] = set()
    for item in structured.get("chapters") if isinstance(structured.get("chapters"), list) else []:
        if not isinstance(item, Mapping):
            continue
        block_no = _as_int(item.get("window"))
        block = blocks.get(block_no) if block_no is not None else None
        if block is None or block_no in taken or str(block.get("position") or "") != WHOLE_CHAPTER:
            continue
        function = str(item.get("function") or "").strip()
        hook = str(item.get("hook") or "").strip()
        threads = _as_int(item.get("threads"))
        povs = _as_int(item.get("povs"))
        if function not in CHAPTER_FUNCTIONS or hook not in CHAPTER_HOOKS:
            continue
        taken.add(int(block_no))
        out["chapters"].append(
            {
                "chapter": int(block.get("chapter") or 0),
                "function": function,
                "hook": hook,
                "threads": max(1, min(MAX_THREADS, threads)) if threads is not None else None,
                "povs": max(1, min(MAX_POVS, povs)) if povs is not None else None,
            }
        )
    return out


def merge_batches(batches: Mapping[str, Mapping[str, Any]] | None) -> tuple[list[tuple[int, str]], list[dict[str, Any]]]:
    """各批的结构（游标 / run 行里按批号存）→ (全书的换场点, 全书各章的章法)，按原书顺序。"""
    starts: dict[int, str] = {}
    chapters: dict[int, dict[str, Any]] = {}
    for _batch, structure in sorted((batches or {}).items(), key=lambda item: int(item[0])):
        if not isinstance(structure, Mapping):
            continue
        for pair in structure.get("scene_starts") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2 and _as_int(pair[0]) is not None:
                starts.setdefault(int(pair[0]), str(pair[1]))
        for record in structure.get("chapters") or []:
            if isinstance(record, Mapping) and _as_int(record.get("chapter")) is not None:
                chapters.setdefault(int(record["chapter"]), dict(record))
    return sorted(starts.items()), [chapters[key] for key in sorted(chapters)]


def _spread(values: Sequence[float]) -> dict[str, int]:
    if not values:
        return {"median": 0, "p10": 0, "p90": 0}
    return {
        "median": int(round(quantile(values, 0.5))),
        "p10": int(round(quantile(values, 0.1))),
        "p90": int(round(quantile(values, 0.9))),
    }


def learned_scene_stats(
    chapter_ranges: Iterable[tuple[int, int, int]],
    starts: Sequence[tuple[int, str]],
    chars_of: Callable[[int, int], int],
) -> dict[str, Any] | None:
    """按切出来的场算结构画像的场级键（``scene_break_style="learned"``）。``chapter_ranges`` 是读过的章
    ``(章号, 起段, 止段)``；场长 = 相邻两个换场点之间的正文字数（章首到第一个换场点、最后一个换场点到章尾也算一场）。
    一章都没读到 → None。"""
    ranges = [(int(c), int(a), int(b)) for c, a, b in chapter_ranges if int(b) >= int(a)]
    if not ranges:
        return None
    points = sorted(int(index) for index, _shift in starts)
    scenes_per_chapter: list[int] = []
    scene_chars: list[int] = []
    with_breaks = 0
    for _chapter, start, end in ranges:
        inside = [p for p in points if start < p <= end]
        bounds = [start, *inside, end + 1]
        lengths = [chars_of(bounds[i], bounds[i + 1] - 1) for i in range(len(bounds) - 1)]
        lengths = [n for n in lengths if n > 0]
        if not lengths:
            continue
        with_breaks += 1 if inside else 0
        scenes_per_chapter.append(len(lengths))
        scene_chars.extend(lengths)
    if not scenes_per_chapter:
        return None
    shifts = Counter(shift for _index, shift in starts)
    total = sum(shifts.values())
    return {
        "scene_break_style": "learned",
        "scene_break_chapters": with_breaks,
        "scene_chapters_read": len(scenes_per_chapter),
        "scenes_per_chapter": _spread(scenes_per_chapter),
        "scene_chars": _spread(scene_chars),
        "scene_shift_distribution": {
            shift: round(shifts[shift] / total, 3) for shift in SCENE_SHIFTS if shifts.get(shift)
        }
        if total
        else {},
    }


def chapter_craft(records: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """各章的章法 → 分布：章的功能、章末怎么收（占比，按多到少）、每章几条线 / 几个视点（中位与分位）、高潮章之间
    隔几章（中位）。一章都没有 → None。"""
    rows = [r for r in records if isinstance(r, Mapping) and r.get("function") and r.get("hook")]
    if not rows:
        return None

    def shares(key: str, vocabulary: Sequence[str]) -> dict[str, float]:
        counts = Counter(str(r[key]) for r in rows)
        return {
            name: round(count / len(rows), 3)
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], vocabulary.index(item[0])))
            if name in vocabulary
        }

    climaxes = [index for index, r in enumerate(rows) if r.get("function") == "高潮"]
    gaps = [b - a for a, b in zip(climaxes, climaxes[1:])]
    threads = [int(r["threads"]) for r in rows if _as_int(r.get("threads")) is not None]
    povs = [int(r["povs"]) for r in rows if _as_int(r.get("povs")) is not None]
    return {
        "version": CHAPTER_CRAFT_VERSION,
        "chapters_read": len(rows),
        "function_distribution": shares("function", CHAPTER_FUNCTIONS),
        "hook_distribution": shares("hook", CHAPTER_HOOKS),
        "threads_per_chapter": _spread(threads),
        "povs_per_chapter": _spread(povs),
        "climax_gap": int(round(quantile(gaps, 0.5))) if gaps else None,
    }


__all__ = [
    "CHAPTER_CRAFT_VERSION",
    "CHAPTER_FUNCTIONS",
    "CHAPTER_HOOKS",
    "SCENE_SHIFTS",
    "chapter_craft",
    "learned_scene_stats",
    "merge_batches",
    "parse_structure_output",
]
