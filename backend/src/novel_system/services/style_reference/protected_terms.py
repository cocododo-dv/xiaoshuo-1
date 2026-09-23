"""风格参考 v3 — 受保护专名（台账 E7）：这本书的世界特有的人名、地名、组织、物件与设定词。

以前「人名设定不许用」只是一句散文（「禁止复用本书专名」），抄袭门、红线列表都拿不到具体的词。学习作业的
``protected`` 步：

1. **候选**（``proper_noun_candidates``，确定性统计，纯函数）：全书正文的 2–4 字汉字串里，出现得多、内部凝聚
   （串的次数 ≈ 它的前后缀的次数，说明它总是整体出现）、两头不是虚词、不是更长的串的一部分的那些；再按出现处的
   前后字把稳定跟着的字接上（「某某学」→「某某学院」，至多 8 字）。每个候选带出现次数与一小段上下文。
   这一步追求**召回**，真正的判断交给模型；
2. **模型确认**（节点 ``style_ref_protected_terms``）：候选 + 文风分析里提到本书特有设定的「作者不这么写」陈述
   → 这本书世界特有的名字 / 名词（人物、地点、组织、物件、设定词），真实世界的常见地名、品牌、流行文化名不算；
3. **核对**（``parse_protected_terms``）：每个词必须在原书里原样出现，2–12 字，去重；
4. **落库**（``replace_protected_terms``）：画像的生成域禁用词里 ``source="protected_auto"`` 的行整体替换；
   作者自己录入的行（``user`` 等）一概不动，同一个词作者已经录过就不再重复。抄袭门（P5）与红线列表读这些行。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBannedTerm
from novel_system.services.style_reference.text_utils import compact_ws

PROTECTED_SOURCE = "protected_auto"
PROTECTED_SCOPE = "generation"
PROTECTED_TERMS_VERSION_PREFIX = "pt_v1"
TERM_KINDS: dict[str, str] = {
    "person": "用本书自己的人物",
    "place": "用本书自己的地点",
    "organization": "用本书自己的组织",
    "item": "用本书自己的物件",
    "term": "用本书自己的设定词",
}
KIND_PLACEHOLDERS: dict[str, str] = {
    "person": "某人",
    "place": "某地",
    "organization": "某组织",
    "item": "某物",
    "term": "某设定",
}
TERM_MIN_CHARS = 2
TERM_MAX_CHARS = 12
MAX_TERMS = 300
CANDIDATE_LIMIT = 220
CONTEXT_CHARS = 8
_MAX_GROW_CHARS = 8

_CJK_RUN_RE = re.compile(r"[一-鿿]+")
# 两头出现这些字的串基本不是专名(虚词、代词、量词、最常见的动词);方位 / 时间字(上、中、前、时……)可能是姓氏或
# 地名的一部分,不在这里
_EDGE_STOP_CHARS = frozenset(
    "的了着过是在就也都还很被把给他她它我你们这那么吗呢吧啊哦呀嘛一个不没有说和与及或而但却又其之以于为所"
    "从向对到让叫将要会能可想看来去候些点儿得地已经再才只更最"
)
# 串里任何位置出现这些字就不要(名字里几乎不会有)
_INNER_STOP_CHARS = frozenset("的了着吗呢吧啊哦呀嘛么")
_COHESION_MIN = 0.35
_EXTEND_SHARE = 0.8


@dataclass(frozen=True)
class Candidate:
    term: str
    count: int
    score: float
    context: str

    def payload(self) -> dict[str, Any]:
        return {"term": self.term, "count": self.count, "context": self.context}


def _default_min_count(total_chars: int) -> int:
    return max(3, int(round(total_chars / 60_000)))


def _clean_edges(term: str) -> bool:
    if len(term) < TERM_MIN_CHARS:
        return False
    if term[0] in _EDGE_STOP_CHARS or term[-1] in _EDGE_STOP_CHARS:
        return False
    return not any(ch in _INNER_STOP_CHARS for ch in term)


def _ngram_counts(runs: Sequence[str], n: int, keep: set[str] | None) -> Counter:
    """n 字串计数；``keep`` 给了时只数前后 (n-1) 字串都在 ``keep`` 里的串（控内存）。一次 ``Counter`` 调用。"""
    if keep is None:
        return Counter(run[i : i + n] for run in runs for i in range(len(run) - n + 1))
    return Counter(
        run[i : i + n]
        for run in runs
        for i in range(len(run) - n + 1)
        if run[i : i + n - 1] in keep and run[i + 1 : i + n] in keep
    )


def _grow(term: str, corpus: str) -> str:
    """出现处后面 / 前面几乎总跟着同一个字(≥80%)时把它接上,至多 ``_MAX_GROW_CHARS`` 字。"""
    current = term
    while len(current) < _MAX_GROW_CHARS:
        after: Counter = Counter()
        before: Counter = Counter()
        total = 0
        for match in re.finditer(re.escape(current), corpus):
            total += 1
            if match.end() < len(corpus):
                after[corpus[match.end()]] += 1
            if match.start() > 0:
                before[corpus[match.start() - 1]] += 1
        if total == 0:
            break
        grown = False
        for counter, side in ((after, "after"), (before, "before")):
            if not counter:
                continue
            char, hits = counter.most_common(1)[0]
            if hits >= _EXTEND_SHARE * total and "\u4e00" <= char <= "\u9fff" and char not in _EDGE_STOP_CHARS:
                current = current + char if side == "after" else char + current
                grown = True
                break
        if not grown:
            break
    return current


def _context(term: str, corpus: str) -> str:
    index = corpus.find(term)
    if index < 0:
        return ""
    start = max(0, index - CONTEXT_CHARS)
    end = min(len(corpus), index + len(term) + CONTEXT_CHARS)
    return corpus[start:end].replace("\n", " ")


def proper_noun_candidates(
    texts: Sequence[str],
    *,
    limit: int = CANDIDATE_LIMIT,
    min_count: int | None = None,
) -> list[Candidate]:
    """全书正文 → 像专名的高频汉字串（见模块文档；确定性，按分数降序）。"""
    corpus = "\n".join(compact_ws(t) for t in texts if str(t or "").strip())
    runs = _CJK_RUN_RE.findall(corpus)
    total_chars = sum(len(run) for run in runs)
    if total_chars == 0:
        return []
    threshold = int(min_count) if min_count is not None else _default_min_count(total_chars)
    counts: dict[int, Counter] = {1: Counter(corpus)}
    counts[2] = _ngram_counts(runs, 2, None)
    counts[3] = _ngram_counts(runs, 3, {g for g, c in counts[2].items() if c >= threshold})
    counts[4] = _ngram_counts(runs, 4, {g for g, c in counts[3].items() if c >= threshold})
    # n 字串被哪个 (n+1) 字串「吞掉」得最多:它作前缀或后缀时那个更长串的最大次数(更长的串本身两头得干净——
    # 「在某城」总连着出现,吞掉「某城」的也不该是它)
    absorbed: dict[int, dict[str, int]] = {}
    for n in (2, 3):
        best: dict[str, int] = {}
        for gram, count in counts[n + 1].items():
            if count < threshold or not _clean_edges(gram):
                continue
            for part in (gram[:-1], gram[1:]):
                if count > best.get(part, 0):
                    best[part] = count
        absorbed[n] = best

    scored: list[tuple[float, str]] = []
    for n in (2, 3, 4):
        for gram, count in counts[n].items():
            if count < threshold or not _clean_edges(gram):
                continue
            denominator = max(counts[n - 1].get(gram[:-1], count), counts[n - 1].get(gram[1:], count), count)
            cohesion = count / denominator
            if cohesion < _COHESION_MIN:
                continue
            if n in absorbed and absorbed[n].get(gram, 0) >= _EXTEND_SHARE * count:
                continue  # 几乎总是更长的名字的一部分
            scored.append((count * cohesion, gram))
    scored.sort(key=lambda item: (-item[0], item[1]))
    result: list[Candidate] = []
    seen: set[str] = set()
    for score, gram in scored[: limit * 2]:
        term = _grow(gram, corpus)
        if term in seen:
            continue
        seen.add(term)
        result.append(Candidate(term=term, count=corpus.count(term), score=round(score, 3), context=_context(term, corpus)))
        if len(result) >= limit:
            break
    return result


# ---------------------------------------------------------------------------
# 模型输出核对
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtectedTerm:
    term: str
    kind: str

    def as_dict(self) -> dict[str, str]:
        return {"term": self.term, "kind": self.kind}


def parse_protected_terms(structured: Any, corpus: str, *, limit: int = MAX_TERMS) -> list[ProtectedTerm]:
    """模型给的专名 → 只留原书里原样出现、2–12 字、类别合法的，去重（保持模型给的顺序）。"""
    items = structured.get("terms") if isinstance(structured, Mapping) else None
    out: list[ProtectedTerm] = []
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if isinstance(item, Mapping):
            term = compact_ws(item.get("term")).strip("「」“”\"'《》()（）[]【】")
            kind = str(item.get("kind") or "term").strip().lower()
        else:
            term, kind = compact_ws(item).strip("「」“”\"'《》()（）[]【】"), "term"
        if kind not in TERM_KINDS:
            kind = "term"
        if not (TERM_MIN_CHARS <= len(term) <= TERM_MAX_CHARS) or term in seen:
            continue
        if term not in corpus:
            continue
        seen.add(term)
        out.append(ProtectedTerm(term=term, kind=kind))
        if len(out) >= limit:
            break
    return out


def protected_terms_version(terms: Sequence[ProtectedTerm | Mapping[str, Any]]) -> str:
    names = sorted(str(t.term if isinstance(t, ProtectedTerm) else t.get("term")) for t in terms)
    digest = hashlib.sha256("\x1f".join(names).encode("utf-8")).hexdigest()[:12]
    return f"{PROTECTED_TERMS_VERSION_PREFIX}_{digest}"


def mask_protected(text: str, terms: Sequence[ProtectedTerm | Mapping[str, Any]]) -> str:
    """把文字里的专名换成类别代称（长词先换）；标签的一句话概括用。"""
    pairs: list[tuple[str, str]] = []
    for item in terms:
        term = item.term if isinstance(item, ProtectedTerm) else str(item.get("term") or "")
        kind = item.kind if isinstance(item, ProtectedTerm) else str(item.get("kind") or "term")
        if term:
            pairs.append((term, KIND_PLACEHOLDERS.get(kind, "某设定")))
    out = str(text or "")
    for term, placeholder in sorted(pairs, key=lambda pair: -len(pair[0])):
        out = out.replace(term, placeholder)
    return out


def contains_protected(text: str, terms: Iterable[str]) -> bool:
    body = str(text or "")
    return any(term and term in body for term in terms)


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------


def replace_protected_terms(session: Session, profile_id: str, terms: Sequence[ProtectedTerm]) -> dict[str, int]:
    """画像的 ``protected_auto`` 行整体替换；作者录入的行不动（同词已录过就跳过）。只 flush。"""
    removed = session.execute(
        delete(StyleReferenceBannedTerm).where(
            StyleReferenceBannedTerm.profile_id == profile_id,
            StyleReferenceBannedTerm.source == PROTECTED_SOURCE,
        )
    ).rowcount
    existing = set(
        session.scalars(
            select(StyleReferenceBannedTerm.term).where(
                StyleReferenceBannedTerm.profile_id == profile_id,
                StyleReferenceBannedTerm.scope == PROTECTED_SCOPE,
            )
        )
    )
    created = 0
    for item in terms:
        if item.term in existing:
            continue
        existing.add(item.term)
        session.add(
            StyleReferenceBannedTerm(
                term_id=f"sr_term_{uuid.uuid4().hex[:12]}",
                profile_id=profile_id,
                term=item.term,
                replacement_hint=TERM_KINDS.get(item.kind),
                source=PROTECTED_SOURCE,
                scope=PROTECTED_SCOPE,
            )
        )
        created += 1
    session.flush()
    return {"removed": int(removed or 0), "created": created}


__all__ = [
    "CANDIDATE_LIMIT",
    "Candidate",
    "KIND_PLACEHOLDERS",
    "MAX_TERMS",
    "PROTECTED_SCOPE",
    "PROTECTED_SOURCE",
    "ProtectedTerm",
    "TERM_KINDS",
    "contains_protected",
    "mask_protected",
    "parse_protected_terms",
    "proper_noun_candidates",
    "protected_terms_version",
    "replace_protected_terms",
]
