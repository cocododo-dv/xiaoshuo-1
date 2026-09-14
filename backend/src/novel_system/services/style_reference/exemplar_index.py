"""全书样例窗口索引(2026-09-14 风格保真修补 WP3)。

few-shot 样例过去只能以抽取证据引文为中心展开(每段型前 12 条、轮换池 30 个),一个项目
最多看到全书约 6%、一场约 2%,而且哪些段落能进提示由语言层抽取碰巧引用了什么决定。
本模块把整本书**确定性**地切成连续窗口(≤ ``window_paragraphs`` 段 / ≤ ``window_max_chars``
字,不跨章题、不含副文本),每窗记起止段、字数、段型构成、对白占比、章内位置与辨识度分。
合成期写入 ``profile_json.exemplar_windows``(不冻结:契约冻结了整本书的段落根哈希,根哈希
一致时段落表与合成时相同,索引可按需复算);渲染期(``injection._render_few_shot``)在
整本书里按场景需要选窗,旧画像无索引时按同一算法惰性计算并缓存。
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Iterable, Mapping, Protocol

from novel_system.services.style_reference.segmentation.heuristic import is_title_paragraph
from novel_system.services.style_reference.text_utils import (
    is_paratext_paragraph,
    is_scene_break_paragraph,
)

logger = logging.getLogger(__name__)

EXEMPLAR_INDEX_VERSION = "exemplar_windows_v1"
DEFAULT_WINDOW_PARAGRAPHS = 60
DEFAULT_WINDOW_MAX_CHARS = 4000
DEFAULT_MIN_WINDOW_CHARS = 600
DEFAULT_AFFINITY_SCAN_CHARS = 1200
# 短尾窗并入前一窗时允许超出单窗上限的比例(否则丢弃短尾窗)
_TAIL_MERGE_SLACK = 1.25
# 窗口的「主导段型」:占比 ≥ 此值的段型都算(与 injection._SCENE_DOMINANT_TYPE_SHARE 同口径)
DOMINANT_TYPE_SHARE = 0.25
# 位置标签
POSITION_OPENING = "opening"
POSITION_CLOSING = "closing"
POSITION_MIDDLE = "middle"
POSITION_WHOLE = "whole"


class WindowScorer(Protocol):
    def score(self, text: str) -> float: ...


def _field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _ordered_rows(paragraphs: Iterable[Any]) -> list[dict[str, Any]]:
    indexed: list[tuple[int, int, Any]] = []
    for position, item in enumerate(paragraphs):
        raw_index = _field(item, "paragraph_index")
        index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else position
        indexed.append((index, position, item))
    indexed.sort(key=lambda entry: (entry[0], entry[1]))
    rows: list[dict[str, Any]] = []
    for index, _position, item in indexed:
        text = str(_field(item, "text", "") or "").strip()
        if not text:
            continue
        rows.append(
            {
                "index": int(index),
                "paragraph_id": str(_field(item, "paragraph_id", "") or ""),
                "ptype": str(_field(item, "paragraph_type", "") or "").strip() or "narration",
                "text": text,
                "chars": len(text),
            }
        )
    return rows


def _split_chapters(
    rows: list[dict[str, Any]], scene_breaks: set[int] | None = None
) -> list[list[dict[str, Any]]]:
    """按章题切章;纯符号分隔行不入正文但在其前一段标 ``break_after``(空行型场界同样标)。"""
    chapters: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    break_set = set(scene_breaks or ())
    for row in rows:
        text = row["text"]
        if is_title_paragraph(text):
            if current:
                chapters.append(current)
            current = []
            continue
        if is_paratext_paragraph(text):
            continue
        if is_scene_break_paragraph(text):
            if current:
                current[-1]["break_after"] = True
            continue
        row["break_after"] = row["index"] in break_set
        current.append(row)
    if current:
        chapters.append(current)
    return chapters


def _chapter_windows(
    chapter: list[dict[str, Any]],
    *,
    window_paragraphs: int,
    window_max_chars: int,
    min_window_chars: int,
) -> list[list[dict[str, Any]]]:
    """按段落顺序贪心切窗:字数或段数将超限时封窗;短尾窗并入前一窗(放宽 25%)或丢弃。"""
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
    return windows


def build_exemplar_window_index(
    paragraphs: Iterable[Any],
    *,
    scorer: WindowScorer | None = None,
    scene_breaks: Iterable[int] | None = None,
    window_paragraphs: int = DEFAULT_WINDOW_PARAGRAPHS,
    window_max_chars: int = DEFAULT_WINDOW_MAX_CHARS,
    min_window_chars: int = DEFAULT_MIN_WINDOW_CHARS,
    affinity_scan_chars: int = DEFAULT_AFFINITY_SCAN_CHARS,
) -> dict[str, Any]:
    """从段落表确定性算出全书窗口索引(无 LLM)。

    ``paragraphs`` 元素可以是 ORM 段落行或 ``{"text", "paragraph_type", "paragraph_index",
    "paragraph_id"}`` 映射。``scorer`` 是 ``injection._WindowAffinityScorer`` 一类的对象
    (窗口前 ``affinity_scan_chars`` 字的辨识度分;缺省 0)。返回纯 JSON 值。
    """
    rows = _ordered_rows(paragraphs)
    break_set = {int(i) for i in (scene_breaks or ()) if isinstance(i, int) and not isinstance(i, bool)}
    chapters = _split_chapters(rows, break_set)
    window_paragraphs = max(1, int(window_paragraphs))
    window_max_chars = max(200, int(window_max_chars))
    min_window_chars = max(0, int(min_window_chars))
    windows: list[dict[str, Any]] = []
    for chapter_no, chapter in enumerate(chapters, start=1):
        chapter_windows = _chapter_windows(
            chapter,
            window_paragraphs=window_paragraphs,
            window_max_chars=window_max_chars,
            min_window_chars=min_window_chars,
        )
        # 短于 min_window_chars 的窗口不入索引——章只有一窗时也一样(几百字的「章」多半是
        # 卷首语 / 内容简介 / 目录残片,不是作者的场景)。
        kept = [win for win in chapter_windows if sum(r["chars"] for r in win) >= min_window_chars]
        for position_index, win in enumerate(kept):
            if len(kept) == 1:
                position = POSITION_WHOLE
            elif position_index == 0:
                position = POSITION_OPENING
            elif position_index == len(kept) - 1:
                position = POSITION_CLOSING
            else:
                position = POSITION_MIDDLE
            types = Counter(r["ptype"] for r in win)
            chars = sum(r["chars"] for r in win)
            affinity = 0.0
            if scorer is not None:
                head = "\n".join(r["text"] for r in win)[: max(0, int(affinity_scan_chars))]
                try:
                    affinity = float(scorer.score(head))
                except Exception:  # noqa: BLE001 — 辨识度分只影响排序,算不出就按 0
                    logger.debug("exemplar window affinity degraded", exc_info=True)
                    affinity = 0.0
            windows.append(
                {
                    "start": win[0]["index"],
                    "end": win[-1]["index"],
                    "chars": chars,
                    "paragraphs": len(win),
                    "chapter": chapter_no,
                    "position": position,
                    "types": dict(types),
                    "dialogue_share": round(types.get("dialogue", 0) / len(win), 3),
                    "affinity": round(affinity, 4),
                }
            )
    return {
        "version": EXEMPLAR_INDEX_VERSION,
        "window_paragraphs": window_paragraphs,
        "window_max_chars": window_max_chars,
        "min_window_chars": min_window_chars,
        "paragraph_count": len(rows),
        "chapter_count": len(chapters),
        "window_count": len(windows),
        "windows": windows,
    }


def dominant_types(window: Mapping[str, Any]) -> set[str]:
    """窗口的主导段型集合(占比 ≥ DOMINANT_TYPE_SHARE;都不够时取最多的一种)。"""
    types = window.get("types") or {}
    if not isinstance(types, Mapping) or not types:
        return set()
    total = max(1, int(window.get("paragraphs") or sum(int(v) for v in types.values()) or 1))
    dominant = {str(t) for t, c in types.items() if int(c) / total >= DOMINANT_TYPE_SHARE}
    if not dominant:
        dominant = {str(max(types, key=lambda t: int(types[t])))}
    return dominant


def primary_type(window: Mapping[str, Any]) -> str:
    types = window.get("types") or {}
    if not isinstance(types, Mapping) or not types:
        return "narration"
    return str(max(types, key=lambda t: int(types[t])))
