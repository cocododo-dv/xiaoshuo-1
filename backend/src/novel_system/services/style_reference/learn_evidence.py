"""风格参考 — 示范窗索引（2026-10-19）：文风卡每句背后的逐字引文落在哪些窗口。

起草时挑「示范某一维」的样例窗（绑定里的重点维、定向改稿要改的维、近期常见偏差维；``inject.selection``）过去只看
打标签那一步模型猜的「这一窗最能示范的 ≤3 维」。学习作业读过的原文里，每条发现都带 2–4 处逐字核对过的引文，
引文落在具体的段落上——由此算出每个窗口**有原文为证**地示范了哪几维、哪几句卡，比猜的准，而且不多花一次调用：

- :func:`card_line_quotes`：文风卡每句 → 它依据的发现的引文（通读全书时卡句依据的是归并后的发现，展开成组里各批
  原始发现的全部引文——不只归并时留下的 4 处）；
- :func:`window_evidence`：引文 → 段落序号 → 当前窗口索引里的窗口，按维计数；一维在一个窗口里至少
  :data:`EVIDENCE_MIN_QUOTES` 处引文才算这一窗示范了它，每窗按次数留前 :data:`EVIDENCE_MAX_DIMS` 维；
- 写进窗口行 ``tags_json["evidence"]``（``windows.set_window_evidence``，学习定稿的事务里）；选窗读索引时有证据的窗
  用证据维，没有的（快速学习只读了 12 窗）仍用标签维。窗口索引重建时整组新建、证据随之清空，回到标签维。

同一套引文也给画像页用（2026-10-19 第三轮）：:func:`line_coverage` 记每句的依据落在哪几章（画像 ``line_evidence``，
页面据此说「贯穿全书」还是「集中在第 X–Y 章」）；:func:`line_examples` 按需取一句在全书的全部例子（画像页点开时）。
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceEvidence,
    StyleReferenceExtraction,
    StyleReferenceFinding,
    StyleReferenceParagraph,
    StyleReferenceQuote,
)
from novel_system.services.style_reference.card import DIMENSION_LABELS, DimensionCard
from novel_system.services.style_reference.windows import load_windows

EVIDENCE_VERSION = "evidence_v1"
EVIDENCE_MIN_QUOTES = 2
EVIDENCE_MAX_DIMS = 3
EVIDENCE_MAX_LINES = 6
_CHUNK = 500


def _chunks(items: Sequence[str]) -> list[list[str]]:
    values = list(items)
    return [values[i : i + _CHUNK] for i in range(0, len(values), _CHUNK)] or [[]]


def card_line_quotes(
    session: Session,
    card: DimensionCard | None,
    *,
    extra_lines: Sequence[tuple[str, Any]] = (),
) -> dict[str, tuple[str, list[str]]]:
    """文风卡每句 → ``(维度, 引文 id 列表)``（去重、按出现顺序）。归并后的发现展开成成员发现的引文。

    ``extra_lines``：卡外的句子（``(维度或场面键, 有 line_id / finding_ids / evidence_quote_ids 的对象)``，如按场面
    的写法）一并算。"""
    lines = [*(card.all_lines() if card is not None else []), *extra_lines]
    if not lines:
        return {}
    finding_ids = sorted({fid for _dim, line in lines for fid in line.finding_ids})
    rows: list[tuple[str, str, str]] = []
    for chunk in _chunks(finding_ids):
        rows.extend(
            session.execute(
                select(StyleReferenceFinding.finding_id, StyleReferenceFinding.status, StyleReferenceFinding.extraction_id).where(
                    StyleReferenceFinding.finding_id.in_(chunk or [""])
                )
            ).all()
        )
    merged_extractions = sorted({str(ext) for _fid, status, ext in rows if status == "consolidated"})
    members: dict[str, list[str]] = {}
    for chunk in _chunks(merged_extractions):
        for payload in session.scalars(
            select(StyleReferenceExtraction.raw_payload_json).where(StyleReferenceExtraction.extraction_id.in_(chunk or [""]))
        ):
            for finding_id, meta in ((payload or {}).get("finding_meta") or {}).items():
                if isinstance(meta, Mapping):
                    members[str(finding_id)] = [str(m) for m in meta.get("members") or []]
    every = sorted(set(finding_ids) | {m for ids in members.values() for m in ids})
    quotes: dict[str, list[str]] = defaultdict(list)
    for chunk in _chunks(every):
        for finding_id, quote_id in session.execute(
            select(StyleReferenceEvidence.finding_id, StyleReferenceEvidence.quote_id)
            .where(StyleReferenceEvidence.finding_id.in_(chunk or [""]))
            .order_by(StyleReferenceEvidence.created_at, StyleReferenceEvidence.evidence_id)
        ).all():
            quotes[str(finding_id)].append(str(quote_id))
    out: dict[str, tuple[str, list[str]]] = {}
    for dim, line in lines:
        ids: list[str] = list(line.evidence_quote_ids)
        for finding_id in line.finding_ids:
            ids.extend(quotes.get(finding_id, []))
            for member in members.get(finding_id, []):
                ids.extend(quotes.get(member, []))
        out[line.line_id] = (dim, list(dict.fromkeys(ids)))
    return out


class QuotePosition(NamedTuple):
    paragraph_index: int
    window_no: int | None  # 落在哪个窗口（窗口之外的零碎段 None）
    chapter: int | None  # 第几章（按它前面最近的窗口算）
    text: str


def quote_positions(session: Session, book_id: str, quote_ids: Sequence[str]) -> dict[str, QuotePosition]:
    """引文 → 段落序号 / 窗口 / 章 / 原文（当前窗口索引）。"""
    found: dict[str, tuple[int, str]] = {}
    for chunk in _chunks(sorted(set(quote_ids))):
        for quote_id, paragraph_index, text in session.execute(
            select(StyleReferenceQuote.quote_id, StyleReferenceParagraph.paragraph_index, StyleReferenceQuote.quote_text)
            .join(StyleReferenceParagraph, StyleReferenceParagraph.paragraph_id == StyleReferenceQuote.paragraph_id)
            .where(StyleReferenceQuote.quote_id.in_(chunk or [""]))
        ).all():
            found[str(quote_id)] = (int(paragraph_index), str(text or ""))
    windows = sorted(
        (int(w.start_index), int(w.end_index), int(w.window_no), int(w.chapter_no or 0)) for w in load_windows(session, book_id)
    )
    starts = [w[0] for w in windows]
    out: dict[str, QuotePosition] = {}
    for quote_id, (paragraph_index, text) in found.items():
        slot = bisect_right(starts, paragraph_index) - 1
        window = windows[slot] if slot >= 0 else None
        out[quote_id] = QuotePosition(
            paragraph_index=paragraph_index,
            window_no=window[2] if window is not None and paragraph_index <= window[1] else None,
            chapter=window[3] if window is not None else None,
            text=text,
        )
    return out


def window_evidence(
    session: Session,
    book_id: str,
    line_quotes: Mapping[str, tuple[str, Sequence[str]]],
    *,
    positions: Mapping[str, QuotePosition] | None = None,
) -> dict[int, dict[str, Any]]:
    """引文 → 窗口：``{窗号: {"dimensions": [...], "counts": {维: 引文数}, "lines": [卡句 id…]}}``（只含有证据的窗）。
    场面句（键不是 16 维之一）只记进 ``lines``，不算维。"""
    if positions is None:
        positions = quote_positions(session, book_id, [q for _dim, ids in line_quotes.values() for q in ids])
    counts: dict[int, Counter] = defaultdict(Counter)
    lines: dict[int, Counter] = defaultdict(Counter)
    for line_id, (dim, ids) in line_quotes.items():
        seen: set[tuple[int, str]] = set()
        for quote_id in ids:
            position = positions.get(quote_id)
            window_no = position.window_no if position is not None else None
            if window_no is None:
                continue
            lines[window_no][line_id] += 1
            if (window_no, quote_id) in seen or dim not in DIMENSION_LABELS:
                continue
            seen.add((window_no, quote_id))
            counts[window_no][dim] += 1
    out: dict[int, dict[str, Any]] = {}
    for window_no in lines:
        by_dim = counts.get(window_no, Counter())
        ranked = sorted(by_dim.items(), key=lambda item: (-item[1], item[0]))
        out[window_no] = {
            "dimensions": [dim for dim, n in ranked if n >= EVIDENCE_MIN_QUOTES][:EVIDENCE_MAX_DIMS],
            "counts": dict(ranked),
            "lines": [line_id for line_id, _n in lines[window_no].most_common(EVIDENCE_MAX_LINES)],
        }
    return out


def line_coverage(
    line_quotes: Mapping[str, tuple[str, Sequence[str]]],
    positions: Mapping[str, QuotePosition],
) -> dict[str, dict[str, Any]]:
    """每句的依据落在哪几章：``{line_id: {"quotes": 引文数, "chapters": [[章, 引文数], …]}}``（章按原书顺序）。"""
    out: dict[str, dict[str, Any]] = {}
    for line_id, (_dim, ids) in line_quotes.items():
        chapters: Counter = Counter()
        total = 0
        for quote_id in ids:
            position = positions.get(quote_id)
            if position is None:
                continue
            total += 1
            if position.chapter is not None:
                chapters[position.chapter] += 1
        if total:
            out[line_id] = {"quotes": total, "chapters": [[c, n] for c, n in sorted(chapters.items())]}
    return out


def line_examples(
    session: Session,
    book_id: str,
    dimension: str,
    line: Any,
    *,
    limit: int = 400,
) -> dict[str, Any]:
    """一句（文风卡句或场面句）在全书的全部例子：按原书顺序的引文（段落序号、章、原文）与按章计数。"""
    quotes = card_line_quotes(session, None, extra_lines=[(dimension, line)]).get(str(line.line_id), (dimension, []))[1]
    positions = quote_positions(session, book_id, quotes)
    ordered = sorted((p.paragraph_index, qid) for qid, p in positions.items())
    chapters: Counter = Counter(p.chapter for p in positions.values() if p.chapter is not None)
    return {
        "line_id": str(line.line_id),
        "total": len(ordered),
        "chapters": [[c, n] for c, n in sorted(chapters.items())],
        "examples": [
            {
                "quote_id": qid,
                "text": positions[qid].text,
                "paragraph_index": index,
                "chapter": positions[qid].chapter,
            }
            for index, qid in ordered[: max(0, int(limit))]
        ],
    }


__all__ = [
    "EVIDENCE_MAX_DIMS",
    "EVIDENCE_MIN_QUOTES",
    "EVIDENCE_VERSION",
    "QuotePosition",
    "card_line_quotes",
    "line_coverage",
    "line_examples",
    "quote_positions",
    "window_evidence",
]
