"""风格参考 —「学习文风 · 通读全书」的按场面写法（2026-10-19，学习作业的 ``situations`` 步）。

通读全书读出几千条带逐字引文的发现，窗口又都有场面标签（日常闲谈、打斗追逐、回忆往事……）。一条发现的引文如果
**集中**落在某类场面的窗口里（这类场面占全书窗口的 p，这条发现落在其中的引文占比 q，``q / p ≥`` :data:`LIFT_MIN`
且至少 :data:`MIN_QUOTES_IN` 处），它就是这位作者写这类场面时的特别做法——全书一张的文风卡多半把它压掉了。

- :func:`rank_situation_findings`（纯函数）：每类场面（至少 :data:`MIN_WINDOWS` 窗）挑出这样的发现，按「集中度 ×
  条数」排，至多 :data:`MAX_FINDINGS_SENT` 条，编号 s1…sN；
- 每类一次调用（模板 ``style_ref_situation_lines``，走文风卡合成节点的路由）：写 2–4 句「写这类场面时他尤其这样写」，
  不重复文风卡已经写了的，在上一版上修订（``keep``）；
- :func:`parse_situation_lines`：没有依据（refs 对不上）/ 带统计数字 / 超长的句丢掉；``keep`` 用上一版原句；
- 定稿时与文风卡同一道过滤（本书专名、作者禁用词、与原书 ≥12 字连续重合，:func:`filter_situation_card`），写进画像
  ``situation_card``（读侧在 ``situation_card``）。快速学习只读了 12 窗，不做这一步。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceEvidence, StyleReferenceFinding
from novel_system.services.style_reference.card import CARD_LINE_MAX_CHARS, DIMENSION_LABELS, line_id_for
from novel_system.services.style_reference.learn_card import CONSOLIDATED_STATUS, has_statistics
from novel_system.services.style_reference.learn_evidence import QuotePosition
from novel_system.services.style_reference.situation_card import SITUATION_CARD_VERSION
from novel_system.services.style_reference.tags import SITUATION_TAGS
from novel_system.services.style_reference.text_utils import compact_ws

MIN_WINDOWS = 8
LIFT_MIN = 1.5
MIN_QUOTES_IN = 2
MAX_FINDINGS_SENT = 40
MAX_LINES = 4
MAX_CARD_LINES_SENT = 48
QUOTE_MAX_CHARS = 40
_BOUNDARY = "。；！？!?;"


@dataclass(frozen=True)
class FindingSpot:
    """一条（各批原始的）发现与它的引文落在哪些窗口。"""

    finding_id: str
    dimension: str
    statement: str
    quotes: tuple[tuple[str, int | None, str], ...]  # (quote_id, window_no, 原文)


@dataclass
class SituationCandidates:
    situation: str
    windows: int
    of_windows: int
    findings: list[dict[str, Any]] = field(default_factory=list)


def rank_situation_findings(
    window_situations: Mapping[int, Sequence[str]],
    spots: Sequence[FindingSpot],
    *,
    min_windows: int | None = None,
    lift_min: float | None = None,
    min_quotes: int | None = None,
    max_findings: int | None = None,
) -> list[SituationCandidates]:
    """每类场面依据集中落在其中的发现（见模块文档）；场面按窗数从多到少，没有候选的场面不出现。门槛缺省取模块常量。"""
    min_windows = MIN_WINDOWS if min_windows is None else min_windows
    lift_min = LIFT_MIN if lift_min is None else lift_min
    min_quotes = MIN_QUOTES_IN if min_quotes is None else min_quotes
    max_findings = MAX_FINDINGS_SENT if max_findings is None else max_findings
    total = len(window_situations)
    if total <= 0:
        return []
    counts: dict[str, int] = defaultdict(int)
    for situations in window_situations.values():
        for situation in set(situations):
            counts[situation] += 1
    out: list[SituationCandidates] = []
    for situation in sorted(counts, key=lambda s: (-counts[s], SITUATION_TAGS.index(s) if s in SITUATION_TAGS else 99)):
        windows = counts[situation]
        if windows < min_windows:
            continue
        base = windows / total
        ranked: list[tuple[float, int, FindingSpot, tuple[str, str]]] = []
        for spot in spots:
            placed = [(qid, w, text) for qid, w, text in spot.quotes if w is not None and w in window_situations]
            if not placed:
                continue
            inside = [(qid, text) for qid, w, text in placed if situation in window_situations[w]]
            if len(inside) < min_quotes:
                continue
            lift = (len(inside) / len(placed)) / base
            if lift < lift_min:
                continue
            quote = min(inside, key=lambda item: (len(item[1]), item[0]))
            ranked.append((lift, len(inside), spot, quote))
        ranked.sort(key=lambda item: (-(item[0] * item[1]), item[2].finding_id))
        if not ranked:
            continue
        candidates = SituationCandidates(situation=situation, windows=windows, of_windows=total)
        for number, (lift, inside, spot, (quote_id, text)) in enumerate(ranked[:max_findings], start=1):
            candidates.findings.append(
                {
                    "ref": f"s{number}",
                    "finding_id": spot.finding_id,
                    "dimension": spot.dimension,
                    "statement": spot.statement,
                    "quote_id": quote_id,
                    "quote": compact_ws(text)[:QUOTE_MAX_CHARS],
                    "lift": round(lift, 2),
                    "quotes_in": inside,
                }
            )
        out.append(candidates)
    return out


def load_finding_spots(
    session: Session,
    run_id: str,
    positions_for: Callable[[Sequence[str]], Mapping[str, QuotePosition]],
) -> list[FindingSpot]:
    """一次通读各批原始的发现（不含归并后的）与它们的引文落在哪些窗口。"""
    findings = list(
        session.execute(
            select(
                StyleReferenceFinding.finding_id,
                StyleReferenceFinding.sub_dimension,
                StyleReferenceFinding.statement,
            ).where(
                StyleReferenceFinding.run_id == run_id,
                or_(StyleReferenceFinding.status.is_(None), StyleReferenceFinding.status != CONSOLIDATED_STATUS),
            )
        ).all()
    )
    quotes: dict[str, list[str]] = defaultdict(list)
    ids = [str(row[0]) for row in findings]
    for start in range(0, len(ids), 500):
        for finding_id, quote_id in session.execute(
            select(StyleReferenceEvidence.finding_id, StyleReferenceEvidence.quote_id).where(
                StyleReferenceEvidence.finding_id.in_(ids[start : start + 500] or [""])
            )
        ).all():
            quotes[str(finding_id)].append(str(quote_id))
    positions = positions_for([q for values in quotes.values() for q in values])
    spots: list[FindingSpot] = []
    for finding_id, dimension, statement in findings:
        placed = tuple(
            (qid, positions[qid].window_no, positions[qid].text) for qid in quotes.get(str(finding_id), []) if qid in positions
        )
        if placed:
            spots.append(FindingSpot(str(finding_id), str(dimension), str(statement or ""), placed))
    return spots


def situation_payload(
    candidates: SituationCandidates,
    *,
    book_title: str,
    card_lines: Sequence[str],
    previous: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "book_title": book_title,
        "situation": candidates.situation,
        "windows": candidates.windows,
        "of_windows": candidates.of_windows,
        "findings": [
            {
                "ref": item["ref"],
                "dimension": DIMENSION_LABELS.get(item["dimension"], item["dimension"]),
                "statement": item["statement"],
                "quote": item["quote"],
            }
            for item in candidates.findings
        ],
        "card_lines": [str(text) for text in card_lines][:MAX_CARD_LINES_SENT],
        "previous_lines": [{"id": line_id, "text": text} for line_id, text in (previous or {}).items()],
    }


def _fit(text: str) -> str | None:
    if len(text) <= CARD_LINE_MAX_CHARS:
        return text
    cut = [i + 1 for i, ch in enumerate(text[:CARD_LINE_MAX_CHARS]) if ch in _BOUNDARY]
    return text[: cut[-1]] if cut and cut[-1] >= CARD_LINE_MAX_CHARS // 2 else None


def parse_situation_lines(
    structured: Any,
    candidates: SituationCandidates,
    *,
    previous: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """核对一类场面的输出 → 画像里的句子（见模块文档）。不抛异常，没有可用的句就是空列表。"""
    previous = dict(previous or {})
    by_ref = {item["ref"]: item for item in candidates.findings}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in structured.get("lines") if isinstance(structured, Mapping) and isinstance(structured.get("lines"), list) else []:
        if not isinstance(raw, Mapping):
            continue
        keep = str(raw.get("keep") or "").strip()
        kept = keep in previous
        text = previous[keep] if kept else compact_ws(raw.get("text")).lstrip("-•· ")
        text = _fit(text) if text else None
        if not text or has_statistics(text):
            continue
        refs = [by_ref[key] for key in dict.fromkeys(str(r or "").strip().lower() for r in raw.get("refs") or []) if key in by_ref]
        if not refs:
            continue
        line_id = keep if kept else line_id_for(f"situation:{candidates.situation}", text)
        if line_id in seen:
            continue
        seen.add(line_id)
        out.append(
            {
                "line_id": line_id,
                "text": text,
                "finding_ids": [item["finding_id"] for item in refs],
                "evidence_quote_ids": list(dict.fromkeys(item["quote_id"] for item in refs)),
                "source": "kept" if kept else "synthesis",
            }
        )
        if len(out) >= MAX_LINES:
            break
    return out


def filter_situation_card(
    situations: Mapping[str, Mapping[str, Any]],
    bad: Callable[[str], bool],
) -> dict[str, Any] | None:
    """定稿：每类场面的句子过与文风卡同一道过滤（``bad`` 为真的丢），拼成画像的 ``situation_card``；一句都不剩 → None。"""
    entries = []
    for situation in sorted(situations, key=lambda s: (-int(situations[s].get("windows") or 0), s)):
        state = situations[situation]
        lines = [line for line in state.get("lines") or [] if not bad(str(line.get("text") or ""))]
        if lines:
            entries.append({"situation": situation, "windows": int(state.get("windows") or 0), "lines": lines})
    return {"version": SITUATION_CARD_VERSION, "situations": entries} if entries else None


__all__ = [
    "LIFT_MIN",
    "MIN_QUOTES_IN",
    "MIN_WINDOWS",
    "FindingSpot",
    "SituationCandidates",
    "filter_situation_card",
    "load_finding_spots",
    "parse_situation_lines",
    "rank_situation_findings",
    "situation_payload",
]
