"""风格参考 · 书的正文规则（纯函数）：哪一段是章题、哪一段不是作者正文、全书怎么切章。

- :func:`is_title_paragraph`：章题形态（第X章 / 卷X / 一 / (一) / 《题名》 / 序 / 楔子 / Chapter N …，可带副题）——分类器
  （``segmentation.heuristic``）、锚定集抽样、切章、结构画像、场景诊断共用这一条，章检测与段型分类永远同口径；
- :func:`non_body_kind`：章题 / 纯符号场分隔 / 脚注与盗版站声明 / 落款日期与「完」都不是正文；
- :func:`split_book_chapters`：全书唯一的切章器（结构画像、样例窗口索引、持久化窗口表共用）；
  :func:`title_name_part`：章题里作者起的那部分。

（从 ``structure.py`` 与 ``segmentation/heuristic.py`` 搬来；两处照旧转出这些名字。）
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from novel_system.services.style_reference.text_utils import (
    is_paratext_paragraph,
    is_scene_break_paragraph,
)

# 章节标题形态:第X章 / 卷X / 一 / (一) / 《题名》 / 序 / 楔子 …,可带 ≤30 字副题
TITLE_RE = re.compile(
    r"^(?:"
    r"第\s*[零〇一二三四五六七八九十百千两\d]+\s*[章节回卷部集幕场篇]"
    r"|卷\s*[零〇一二三四五六七八九十百\d]+"
    r"|[一二三四五六七八九十百]{1,3}"
    r"|\d{1,3}"
    r"|[（(]\s*[一二三四五六七八九十\d]+\s*[）)]"
    r"|序[章幕言]?|楔子|尾声|后记|番外|引子|终章|上篇|中篇|下篇"
    r"|chapter\s*\d+"
    r"|《[^》]{1,40}》"
    # 2026-09-14 保真修补:副题允许含空格与双语(「第一幕 卡塞尔之门 The Gate to Cassell」),
    # 但不能含分句标点(，、；),避免把普通短句当标题。
    r")(?:\s*[：:·—\-\s]\s*[^\s，、；,;][^，、；,;]{0,46})?$",
    re.IGNORECASE,
)
# 章题形态的最长字数（双语副题的章题可超过 40 字）：章题判定、切章截题、章题样例同口径
TITLE_MAX_CHARS = 48
_TITLE_SENTENCE_END_CHARS = "。！？!?…"

# 章尾的落款 / 日期行（「一九二四年二月七日」）不是散文，不算收章段。
_COLOPHON_RE = re.compile(
    r"^[（(]?[一二三四五六七八九十〇零两\d]{2,4}年"
    r"(?:[一二三四五六七八九十〇零\d]{1,3}月)?"
    r"(?:[一二三四五六七八九十〇零\d]{1,3}[日号])?[。．.]?[）)]?[。]?$"
)
_COLOPHON_MAX_CHARS = 20
# 合集里下一卷的卷首页(卷名 / 某某 著 / 题记)常粘在上一章末尾:离章末 ≤ 此行数、其后不到此字数时才当卷首页
# 切掉;后面还跟着一整章正文的「某某 著」按正文处理(防误伤单篇小说的署名行)。
FRONT_MATTER_TAIL_MAX_ROWS = 40
FRONT_MATTER_TAIL_MAX_CHARS = 1500
_VOLUME_TITLE_MAX_CHARS = 30
_FRONT_MATTER_RE = re.compile(r"^(?:[\w\u4e00-\u9fff·]{1,20}\s*著|(?:内容)?简介\s*[：:]?|作者\s*[：:].{0,20})$")


def is_title_paragraph(text: str) -> bool:
    """段落是否为章节标题形态(第X章 / 卷X / 一 / (一) / 《题名》 / 序 / 楔子 / Chapter N …):不长于
    :data:`TITLE_MAX_CHARS`、不带句末标点、整段匹配 :data:`TITLE_RE`。"""
    stripped = str(text or "").strip()
    if not stripped or len(stripped) > TITLE_MAX_CHARS:
        return False
    if any(ch in stripped for ch in _TITLE_SENTENCE_END_CHARS):
        return False
    return TITLE_RE.match(stripped) is not None


def field_value(item: Any, name: str, default: Any = None) -> Any:
    """字典取键、对象取属性（段落行可以是 ORM 行、字典或别的带属性的对象）。"""
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def marker_style(title: str) -> str:
    """章题的形态（第X章式 / 《题名》式 / 卷X式 / Chapter N 式 / 序号式 / 序跋式）。"""
    stripped = title.strip()
    head = stripped[:1]
    if stripped.startswith("第"):
        return "第X章式"
    if head == "《":
        return "《题名》式"
    if head == "卷":
        return "卷X式"
    if stripped.lower().startswith("chapter"):
        return "Chapter N 式"
    if head in "（(" or head.isdigit() or head in "一二三四五六七八九十百":
        return "序号式"
    return "序跋式"


_END_MATTER = frozenset({"完", "（完）", "(完)", "全文完", "全书完", "本书完", "终", "the end", "end", "fin", "—完—", "－完－"})


def _is_colophon(text: str) -> bool:
    if text.strip().lower() in _END_MATTER:
        return True
    return len(text) <= _COLOPHON_MAX_CHARS and _COLOPHON_RE.match(text) is not None


def collapse_ws(text: Any) -> str:
    """空白折成单个空格、去首尾（``None`` → ""）。"""
    return " ".join(str(text or "").split())


def _is_front_matter_line(text: str) -> bool:
    """书名页 / 简介行:「某某 著」「内容简介：」「作者：某某」(整段只有这一句)。"""
    return _FRONT_MATTER_RE.match(collapse_ws(text)) is not None


def _looks_like_volume_title(text: str) -> bool:
    """卷首页的书名行(「某某·卷名」):短、不带句末标点、不以引号起头。"""
    norm = collapse_ws(text)
    return (
        0 < len(norm) <= _VOLUME_TITLE_MAX_CHARS
        and not any(mark in norm for mark in "。！？!?…")
        and not norm.startswith(("“", "‘", "「", "『", '"'))
    )


def non_body_kind(text: Any) -> str | None:
    """段落不是作者正文时返回种类(``title`` 章题 / ``scene_break`` 纯符号场分隔 / ``paratext`` 脚注与盗版站
    声明 / ``colophon`` 落款日期与「完」);正文返回 ``None``。切章、样例窗口与窗口正文共用这一条判断。"""
    norm = collapse_ws(text)
    if not norm:
        return "empty"
    if is_title_paragraph(norm):
        return "title"
    if is_scene_break_paragraph(norm):
        return "scene_break"
    if is_paratext_paragraph(norm):
        return "paratext"
    if _is_colophon(norm):
        return "colophon"
    return None


@dataclass
class BookChapter:
    """切章结果里的一章:``chapter_no`` 从 1 起(书名页 / 简介块不占号),``rows`` 只有正文段。"""

    chapter_no: int
    title: str
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(int(row["chars"]) for row in self.rows)

    @property
    def scene_breaks(self) -> int:
        """章内显式场界数(章末那个不算)。"""
        return sum(1 for row in self.rows[:-1] if row.get("break_after"))


def _candidate_rows(paragraphs: Iterable[Any]) -> list[dict[str, Any]]:
    """按 paragraph_index 稳定排序(缺索引时保持输入顺序)、剥空段的原始行。"""
    indexed: list[tuple[int, int, Any]] = []
    for position, item in enumerate(paragraphs):
        raw_index = field_value(item, "paragraph_index")
        index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else position
        indexed.append((index, position, item))
    indexed.sort(key=lambda entry: (entry[0], entry[1]))
    rows: list[dict[str, Any]] = []
    for index, _position, item in indexed:
        text = str(field_value(item, "text", "") or "").strip()
        if not text:
            continue
        rows.append(
            {
                "index": int(index),
                "paragraph_id": str(field_value(item, "paragraph_id", "") or ""),
                "ptype": str(field_value(item, "paragraph_type", "") or "").strip() or "narration",
                "text": text,
                "chars": len(text),
            }
        )
    return rows


def _front_matter_tail_cut(raw: Sequence[dict[str, Any]]) -> int | None:
    """章末粘着的下一卷卷首页(卷名 / 「某某 著」/ 题记……直到下一个章题)从哪一行起不是正文。

    只认离章末 ≤ ``FRONT_MATTER_TAIL_MAX_ROWS`` 行、且其后总共不到 ``FRONT_MATTER_TAIL_MAX_CHARS`` 字的「某某 著」
    类行(其后是题记,不是一整章正文);它前面紧挨的卷名行一并切掉。
    """
    count = len(raw)
    for position, row in enumerate(raw):
        if not _is_front_matter_line(row["text"]):
            continue
        if count - 1 - position > FRONT_MATTER_TAIL_MAX_ROWS:
            continue
        if sum(int(item["chars"]) for item in raw[position + 1 :]) > FRONT_MATTER_TAIL_MAX_CHARS:
            continue
        if position > 0 and _looks_like_volume_title(raw[position - 1]["text"]):
            return position - 1
        return position
    return None


def split_book_chapters(
    paragraphs: Iterable[Any],
    *,
    scene_breaks: Iterable[int] | None = None,
) -> tuple[list[BookChapter], Counter]:
    """全书唯一的切章器(结构画像、样例窗口索引、持久化窗口表共用),返回 (章, 章题形态计数)。

    - 章题段(``is_title_paragraph``)开启新章,不入正文;连续章题只留最后一条(最贴近正文);
    - 脚注 / 盗版站声明 / 落款日期不入正文;纯符号场分隔行不入正文,在其前一段标 ``break_after``;
      导入期记录的空行型场界(``scene_breaks``:其后有场界的段索引)同样标 ``break_after``;
    - 有章题的书,第一个章题之前的书名页 / 简介块(含「某某 著」「内容简介：」行)不是正文章,不占章号;
    - 合集里粘在上一章末尾的下一卷卷首页(卷名、「某某 著」、题记)从正文里切掉;
    - 没有正文段的块不成章。
    行是 ``{"index", "paragraph_id", "ptype", "text", "chars", "break_after"}``(``text`` 为去首尾空白的原文)。
    """
    break_set = {int(item) for item in (scene_breaks or ()) if isinstance(item, int) and not isinstance(item, bool)}
    blocks: list[tuple[str, list[dict[str, Any]], bool]] = []
    markers: Counter = Counter()
    current: list[dict[str, Any]] = []
    title = ""
    leading = True
    for row in _candidate_rows(paragraphs):
        if is_title_paragraph(collapse_ws(row["text"])):
            markers[marker_style(row["text"])] += 1
            if current:
                blocks.append((title, current, leading))
            current = []
            title = collapse_ws(row["text"])[:TITLE_MAX_CHARS]
            leading = False
            continue
        current.append(row)
    if current:
        blocks.append((title, current, leading))

    bodies: list[tuple[str, list[dict[str, Any]], bool, bool]] = []
    for block_title, raw, is_leading in blocks:
        front_matter = any(_is_front_matter_line(row["text"]) for row in raw)
        cut = _front_matter_tail_cut(raw)
        if cut is not None:
            raw = raw[:cut]
        body: list[dict[str, Any]] = []
        for row in raw:
            kind = non_body_kind(row["text"])
            if kind == "scene_break":
                if body:
                    body[-1]["break_after"] = True
                continue
            if kind is not None:
                continue
            body.append({**row, "break_after": row["index"] in break_set})
        if body:
            bodies.append((block_title, body, is_leading, front_matter))
    if markers and len(bodies) > 1 and bodies[0][2] and bodies[0][3]:
        # 有章题的书:第一个章题之前的书名页 / 简介块不是正文章
        bodies = bodies[1:]
    chapters = [
        BookChapter(chapter_no=number, title=block_title, rows=body)
        for number, (block_title, body, _leading, _front) in enumerate(bodies, start=1)
    ]
    return chapters, markers


# 章题的「编号 / 分隔」部分:去掉它剩下的才是作者起的题名(「第一幕 卡塞尔之门 The Gate to Cassell」
# → 「卡塞尔之门 The Gate to Cassell」;「第二十一章」→ "")。与 TITLE_RE 同一套标记。
_TITLE_MARKER_RE = re.compile(
    r"^(?:"
    r"第\s*[零〇一二三四五六七八九十百千两\d]+\s*[章节回卷部集幕场篇]"
    r"|卷\s*[零〇一二三四五六七八九十百\d]+"
    r"|[一二三四五六七八九十百]{1,3}"
    r"|\d{1,3}"
    r"|[（(]\s*[一二三四五六七八九十\d]+\s*[）)]"
    r"|序\s*[章幕言]?|楔子|尾声|后记|番外|引子|终章|上篇|中篇|下篇"
    r"|chapter\s*\d+"
    r")(?=$|[\s：:·—\-、])\s*[：:·—\-、]?\s*",
    re.IGNORECASE,
)


def title_name_part(title: str) -> str:
    """章题里作者起的那部分(去编号、去分隔、去《》);纯编号章题返回 ""。"""
    stripped = " ".join(str(title or "").split())
    if not stripped:
        return ""
    if stripped.startswith("《") and stripped.endswith("》"):
        return stripped[1:-1].strip()
    return _TITLE_MARKER_RE.sub("", stripped, count=1).strip()


__all__ = [
    "BookChapter",
    "FRONT_MATTER_TAIL_MAX_CHARS",
    "FRONT_MATTER_TAIL_MAX_ROWS",
    "TITLE_MAX_CHARS",
    "TITLE_RE",
    "collapse_ws",
    "field_value",
    "is_title_paragraph",
    "marker_style",
    "non_body_kind",
    "split_book_chapters",
    "title_name_part",
]
