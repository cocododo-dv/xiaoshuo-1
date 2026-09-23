"""全书样例窗口的切窗规则(2026-09-14 风格保真修补 WP3;2026-09-23 风格参考 v3 起只剩切窗)。

把整本书**确定性**地切成连续窗口(≤ ``window_paragraphs`` 段 / ≤ ``window_max_chars`` 字,不跨章题、
不跨场界、不含副文本,<600 字的窗不要),每窗带章号与章内位置(opening / closing / middle / whole)。
切章走 ``structure.split_book_chapters``(结构画像与窗口共用唯一的切章器)。

持久化的窗口索引(特征、典型度、标签)在 ``windows.py``(``exemplar_windows_v3``);选窗在
``inject/selection.py``。原来存在 ``profile_json.exemplar_windows`` 里的 dict 索引(v2)与它的辨识度打分已随
注入 v3 删除。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from novel_system.services.style_reference.structure import split_book_chapters

DEFAULT_WINDOW_PARAGRAPHS = 60
DEFAULT_WINDOW_MAX_CHARS = 4000
DEFAULT_MIN_WINDOW_CHARS = 600
# 短尾窗并入前一窗时允许超出单窗上限的比例(否则丢弃短尾窗)
_TAIL_MERGE_SLACK = 1.25
# 位置标签
POSITION_OPENING = "opening"
POSITION_CLOSING = "closing"
POSITION_MIDDLE = "middle"
POSITION_WHOLE = "whole"


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def cut_chapter_windows(
    chapter: list[dict[str, Any]],
    *,
    window_paragraphs: int = DEFAULT_WINDOW_PARAGRAPHS,
    window_max_chars: int = DEFAULT_WINDOW_MAX_CHARS,
    min_window_chars: int = DEFAULT_MIN_WINDOW_CHARS,
) -> list[list[dict[str, Any]]]:
    """一章正文 → 窗口:按段落顺序贪心切,字数或段数将超限时封窗,遇场界(``break_after``)封窗;
    短尾窗并入前一窗(放宽 25%)或丢弃;最后短于 ``min_window_chars`` 的窗口不要(章只有一窗时也一样——
    几百字的「章」多半是卷首语 / 内容简介 / 目录残片,不是作者的场景)。"""
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for row in chapter:
        if current and (
            current_chars + row["chars"] > window_max_chars or len(current) >= window_paragraphs
        ):
            windows.append(current)
            current = []
            current_chars = 0
        current.append(row)
        current_chars += row["chars"]
        if row.get("break_after"):
            # 场界:窗口不跨场(下一段另起一窗)
            windows.append(current)
            current = []
            current_chars = 0
    if current:
        windows.append(current)
    if len(windows) >= 2 and sum(r["chars"] for r in windows[-1]) < min_window_chars:
        tail = windows.pop()
        previous = windows[-1]
        merged_chars = sum(r["chars"] for r in previous) + sum(r["chars"] for r in tail)
        if merged_chars <= int(window_max_chars * _TAIL_MERGE_SLACK) and len(previous) + len(tail) <= int(
            window_paragraphs * _TAIL_MERGE_SLACK
        ):
            previous.extend(tail)
    return [win for win in windows if sum(r["chars"] for r in win) >= min_window_chars]


def window_position(position_index: int, count: int) -> str:
    """章内第 ``position_index`` 个窗口(共 ``count`` 个)的位置标签。"""
    if count == 1:
        return POSITION_WHOLE
    if position_index == 0:
        return POSITION_OPENING
    if position_index == count - 1:
        return POSITION_CLOSING
    return POSITION_MIDDLE


def book_windows(
    paragraphs: Iterable[Any],
    *,
    scene_breaks: Iterable[int] | None = None,
    window_paragraphs: int = DEFAULT_WINDOW_PARAGRAPHS,
    window_max_chars: int = DEFAULT_WINDOW_MAX_CHARS,
    min_window_chars: int = DEFAULT_MIN_WINDOW_CHARS,
) -> tuple[list[tuple[int, str, list[dict[str, Any]]]], int, int]:
    """整本书 → [(章号, 位置, 窗口正文行)] + (非空段数, 章数)。切章走 ``structure.split_book_chapters``。"""
    items = list(paragraphs)
    non_empty = sum(1 for item in items if str(_field(item, "text", "") or "").strip())
    chapters, _markers = split_book_chapters(items, scene_breaks=scene_breaks)
    window_paragraphs = max(1, int(window_paragraphs))
    window_max_chars = max(200, int(window_max_chars))
    min_window_chars = max(0, int(min_window_chars))
    result: list[tuple[int, str, list[dict[str, Any]]]] = []
    for chapter in chapters:
        kept = cut_chapter_windows(
            chapter.rows,
            window_paragraphs=window_paragraphs,
            window_max_chars=window_max_chars,
            min_window_chars=min_window_chars,
        )
        for position_index, win in enumerate(kept):
            result.append((chapter.chapter_no, window_position(position_index, len(kept)), win))
    return result, non_empty, len(chapters)


__all__ = [
    "DEFAULT_MIN_WINDOW_CHARS",
    "DEFAULT_WINDOW_MAX_CHARS",
    "DEFAULT_WINDOW_PARAGRAPHS",
    "POSITION_CLOSING",
    "POSITION_MIDDLE",
    "POSITION_OPENING",
    "POSITION_WHOLE",
    "book_windows",
    "cut_chapter_windows",
    "window_position",
]
