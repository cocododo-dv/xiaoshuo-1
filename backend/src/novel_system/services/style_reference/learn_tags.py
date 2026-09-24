"""风格参考 v3 —「学习文风」的标签步：给窗口打场面 / 情绪 / 维度标签（台账 N3；2026-09-24 §8 O1 改 v2）。

策略 C 的「检索」在 v3 变成「按本场挑样例」（P4 选窗）：场景蓝图给出本场的场面标签，选窗从作者写同类场面的
窗口里挑；「维度示范」配额按窗口的 ``dimensions``（这一窗最能示范的 ≤3 个维度）挑。标签词表只有一张（``tags.py``），
维度就是文风卡的 16 维（键 + 中文名一并送给模型，只让它填键）——标签不再依赖文风卡的内容，所以打过的窗口
重新学习时不用重打（作业只给标签版本不是当前版本的窗口打）。

- 分批（``plan_tag_batches``）：按窗口号顺序，每批 ≤ ``TAG_BATCH_WINDOWS`` 窗且 ≤ ``TAG_BATCH_MAX_CHARS`` 字；
  一窗送前 ``TAG_WINDOW_HEAD_CHARS`` 字 +「……」+ 后 ``TAG_WINDOW_TAIL_CHARS`` 字（窗口多在 2,500–4,400 字，
  这样一窗送 ≤3,000 字；实测一本 190 万字的书 520 窗：送出的原文从 180 万字降到 148 万字，省 18%，场面与情绪
  判断看开头与收尾足够）；
- 严格解析（``parse_tag_output``）：本批每一窗恰好一项、窗号原样照抄；缺 / 多 / 重复整批重试（作业里退避两次）；
  场面 / 情绪不在词表里的丢掉；维度只认 16 个键；一句话概括里的受保护专名换成类别代称（「某人」「某地」）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novel_system.services.style_reference.card import DIMENSION_LABELS
from novel_system.services.style_reference.protected_terms import mask_protected
from novel_system.services.style_reference.tags import (
    DIMENSION_KEYS,
    GIST_MAX_CHARS,
    MOOD_TAGS,
    SITUATION_TAGS,
    normalize_window_tags,
)
from novel_system.services.style_reference.text_utils import compact_ws

TAG_BATCH_WINDOWS = 8
TAG_BATCH_MAX_CHARS = 26_000
TAG_WINDOW_HEAD_CHARS = 2_200
TAG_WINDOW_TAIL_CHARS = 800
TAG_WINDOW_MAX_CHARS = TAG_WINDOW_HEAD_CHARS + TAG_WINDOW_TAIL_CHARS
_ELISION = "\n……（中略）……\n"


class TagBatchMismatch(Exception):
    """一批的输出与本批的窗口对不上（缺 / 多 / 重复 / 不是对象）：整批重试。"""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems[:6]))
        self.problems = list(problems)


def clip_window_text(text: str) -> str:
    body = str(text or "").strip()
    if len(body) <= TAG_WINDOW_MAX_CHARS:
        return body
    return body[:TAG_WINDOW_HEAD_CHARS].rstrip() + _ELISION + body[-TAG_WINDOW_TAIL_CHARS:].lstrip()


def plan_tag_batches(windows: Sequence[tuple[int, int]]) -> list[list[int]]:
    """``[(window_no, chars)]`` → 批（窗口号顺序；每批 ≤8 窗、≤26,000 送出字数）。"""
    batches: list[list[int]] = []
    current: list[int] = []
    current_chars = 0
    for window_no, chars in sorted(windows):
        size = min(int(chars), TAG_WINDOW_MAX_CHARS + len(_ELISION))
        if current and (len(current) >= TAG_BATCH_WINDOWS or current_chars + size > TAG_BATCH_MAX_CHARS):
            batches.append(current)
            current, current_chars = [], 0
        current.append(int(window_no))
        current_chars += size
    if current:
        batches.append(current)
    return batches


def dimension_vocabulary() -> list[dict[str, str]]:
    """送给模型的 16 维：``[{key, label}]``（键是它要填的，中文名帮它认维度）。"""
    return [{"key": key, "label": DIMENSION_LABELS.get(key, key)} for key in DIMENSION_KEYS]


def tag_payload(windows: Sequence[Mapping[str, Any]], *, book_title: str) -> dict[str, Any]:
    """``windows`` 每项 ``{window, chapter, position, text}``（``text`` 已经 ``clip_window_text``）。"""
    return {
        "book_title": book_title,
        "situation_vocabulary": list(SITUATION_TAGS),
        "mood_vocabulary": list(MOOD_TAGS),
        "dimension_vocabulary": dimension_vocabulary(),
        "windows": [dict(w) for w in windows],
    }


def parse_tag_output(
    structured: Any,
    expected: Sequence[int],
    *,
    protected_terms: Sequence[Mapping[str, Any]] = (),
) -> dict[int, dict[str, Any]]:
    """严格解析一批的标签（见模块文档）；对不上抛 ``TagBatchMismatch``。"""
    items = structured.get("windows") if isinstance(structured, Mapping) else None
    if not isinstance(items, list):
        raise TagBatchMismatch(["output has no windows list"])
    wanted = [int(n) for n in expected]
    wanted_set = set(wanted)
    problems: list[str] = []
    result: dict[int, dict[str, Any]] = {}
    for position, item in enumerate(items):
        if not isinstance(item, Mapping):
            problems.append(f"item {position} is not an object")
            continue
        raw_no = item.get("window")
        try:
            window_no = int(raw_no) if not isinstance(raw_no, bool) else None
        except (TypeError, ValueError):
            window_no = None
        if window_no is None:
            problems.append(f"item {position} has no integer window")
            continue
        if window_no not in wanted_set:
            problems.append(f"window {window_no} is not in this batch")
            continue
        if window_no in result:
            problems.append(f"window {window_no} appears more than once")
            continue
        tags = normalize_window_tags(item)
        gist = compact_ws(mask_protected(tags.get("gist") or "", protected_terms))
        tags["gist"] = gist[:GIST_MAX_CHARS]
        result[window_no] = tags
    missing = [n for n in wanted if n not in result]
    if missing:
        problems.append(f"{len(missing)} window(s) missing (e.g. {', '.join(str(n) for n in missing[:5])})")
    if problems:
        raise TagBatchMismatch(problems)
    return result


__all__ = [
    "TAG_BATCH_MAX_CHARS",
    "TAG_BATCH_WINDOWS",
    "TAG_WINDOW_MAX_CHARS",
    "TagBatchMismatch",
    "clip_window_text",
    "dimension_vocabulary",
    "parse_tag_output",
    "plan_tag_batches",
    "tag_payload",
]
