"""风格参考 v3 —「学习文风」的合成步：逐维发现 → 文风卡（``card.DimensionCard``）。

合成把 16 维的发现（每条带编号 ref 与两处原文短引）、测量核给的**绝对**声音习惯（P1）与结构事实交给一次模型调用，
写成「代替模型默认写法，这位作者这样写」的文风卡（台账 N1）：每维一句概括、模型默认写法、1–3 条 do 与 ≤2 条
avoid（每条引用它依据的发现 ref）、手法名、辨识度；外加气质（≤4 行，必须体现）、整体概述、规划层手法。

合成之后的确定性收口（这里的纯函数）：

- ``assemble_card``：丢掉不认识的维 / 没有依据（ref 全对不上）的行 / 统计数字（引号外的阿拉伯数字、百分号）/
  超长行；ref → 发现 id 与证据引文 id；每维 do ≤3、avoid ≤2；「必须体现」全卡至多 4 条（模型没标时取辨识度最高
  的两条）；
- ``reconcile_card``（台账 E6 的对账步）：卡里关于同一种标点（逗号、问号、破折号……）或句子长短为主的说法，
  (a) 与测量核的全书实测相反的丢掉（与声音习惯同一套阈值——卡片永远不会和声音块打架），(b) 两条互相相反的只留
  证据更多的一条（再比辨识度）；
- ``filter_card``（定稿时）：含受保护专名的行、与原书有 ≥12 字连续重合的行（``CorpusOverlapIndex``，
  允许 ≤11 字的作者原话作例子）丢掉；
- ``derive_narrative_guidance``：叙事层的卡片行（avoid 带「避免：」极性标记）→ 规划 / 初稿用的叙事机制指引。

合成不再要求卡片「与声音习惯一致、不得相反」地重述声音（台账 E1 的耦合）：声音习惯是单独的块，卡片只写
声音块说不出来的东西。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceEvidence,
    StyleReferenceExtraction,
    StyleReferenceFinding,
    StyleReferenceQuote,
)
from novel_system.services.style_reference.binding_config import ALL_DIMENSIONS
from novel_system.services.style_reference.card import (
    CARD_LINE_MAX_CHARS,
    DIMENSION_LABELS,
    CardLine,
    DimensionCard,
    DimensionEntry,
    line_id_for,
    normalize_card,
)
from novel_system.services.style_reference.learn_extract import clean_devices
from novel_system.services.style_reference.narrative_guidance import mark_forbidden_narrative_statement
from novel_system.services.style_reference.schemas import FindingKind
from novel_system.services.style_reference.text_utils import compact_ws
from novel_system.services.style_reference.validation.plagiarism import normalize_text_for_matching
from novel_system.services.style_reference.voice_signature import _PUNCT_HABITS

SYNTH_QUOTES_PER_FINDING = 2
SYNTH_QUOTE_MAX_CHARS = 40
MAX_DO_LINES = 3
MAX_AVOID_LINES = 2
MAX_MANDATORY = 4
DEFAULT_MANDATORY = 2
MAX_TEMPERAMENT = 4
MAX_PLANNING_LINES = 10
PLANNING_LINE_MAX_CHARS = 90
SUMMARY_MAX_CHARS = 60
MODEL_DEFAULT_MAX_CHARS = 90
QUALITATIVE_SUMMARY_MAX_CHARS = 240
PROFILE_TITLE_MAX_CHARS = 20
MIN_CARD_LINES = 4
CARD_OVERLAP_THRESHOLD_CHARS = 12
NARRATIVE_GUIDANCE_MAX_LINES = 8

DROP_UNKNOWN_DIMENSION = "unknown_dimension"
DROP_UNGROUNDED = "ungrounded"
DROP_NUMBERS = "numbers"
DROP_TOO_LONG = "too_long"
DROP_CONTRADICTS_MEASURE = "contradicts_measure"
DROP_CONTRADICTION = "contradiction"
DROP_PROTECTED = "protected_term"
DROP_OVERLAP = "source_overlap"
DROP_DUPLICATE = "duplicate"
DROP_OVER_LIMIT = "over_limit"


# ---------------------------------------------------------------------------
# 读回这一轮的发现
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FindingRef:
    ref: str
    finding_id: str
    dimension: str
    kind: str
    statement: str
    confidence: str
    quote_ids: tuple[str, ...]
    quotes: tuple[str, ...]


def load_run_findings(session: Session, run_id: str) -> list[FindingRef]:
    """这一轮的全部发现（按维度顺序、观察在前、落库顺序），带编号 f1…fN 与证据引文。"""
    findings = list(
        session.scalars(
            select(StyleReferenceFinding)
            .where(StyleReferenceFinding.run_id == run_id)
            .order_by(StyleReferenceFinding.created_at, StyleReferenceFinding.finding_id)
        )
    )
    evidence_rows = session.execute(
        select(StyleReferenceEvidence.finding_id, StyleReferenceQuote.quote_id, StyleReferenceQuote.quote_text)
        .join(StyleReferenceQuote, StyleReferenceQuote.quote_id == StyleReferenceEvidence.quote_id)
        .where(StyleReferenceEvidence.finding_id.in_([f.finding_id for f in findings] or [""]))
        .order_by(StyleReferenceEvidence.created_at, StyleReferenceEvidence.evidence_id)
    ).all()
    quotes: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for finding_id, quote_id, quote_text in evidence_rows:
        quotes[str(finding_id)].append((str(quote_id), str(quote_text or "")))
    order = {dim: index for index, dim in enumerate(ALL_DIMENSIONS)}
    findings.sort(
        key=lambda f: (
            order.get(str(f.sub_dimension), 99),
            0 if f.finding_kind == FindingKind.OBSERVATION.value else 1,
        )
    )
    refs: list[FindingRef] = []
    for number, finding in enumerate(findings, start=1):
        pairs = quotes.get(str(finding.finding_id), [])
        refs.append(
            FindingRef(
                ref=f"f{number}",
                finding_id=str(finding.finding_id),
                dimension=str(finding.sub_dimension),
                kind=str(finding.finding_kind),
                statement=str(finding.statement or ""),
                confidence=str(finding.confidence or "medium"),
                quote_ids=tuple(quote_id for quote_id, _text in pairs),
                quotes=tuple(text for _quote_id, text in pairs),
            )
        )
    return refs


@dataclass
class DimensionMetaRow:
    model_default: str = ""
    devices: list[str] = field(default_factory=list)
    distinctiveness: float | None = None


def load_dimension_meta(session: Session, run_id: str) -> dict[str, DimensionMetaRow]:
    """抽取行里记的每维「模型默认写法 / 手法名 / 辨识度」。"""
    meta: dict[str, DimensionMetaRow] = {}
    for row in session.scalars(
        select(StyleReferenceExtraction)
        .where(StyleReferenceExtraction.run_id == run_id)
        .order_by(StyleReferenceExtraction.created_at)
    ):
        payload = row.raw_payload_json if isinstance(row.raw_payload_json, Mapping) else {}
        distinct = payload.get("distinctiveness")
        meta[str(row.sub_dimension)] = DimensionMetaRow(
            model_default=str(payload.get("model_default") or ""),
            devices=clean_devices(payload.get("devices")),
            distinctiveness=float(distinct) if isinstance(distinct, (int, float)) else None,
        )
    return meta


# ---------------------------------------------------------------------------
# 合成载荷
# ---------------------------------------------------------------------------


def _short(text: str, limit: int) -> str:
    body = compact_ws(text)
    return body if len(body) <= limit else body[: limit - 1] + "…"


def synthesis_payload(
    findings: Sequence[FindingRef],
    meta: Mapping[str, DimensionMetaRow],
    *,
    book_title: str,
    voice_habits: Sequence[str],
    structure_facts: Sequence[str],
    quotes_per_finding: int = SYNTH_QUOTES_PER_FINDING,
    max_findings_per_kind: int | None = None,
) -> dict[str, Any]:
    by_dim: dict[str, list[FindingRef]] = defaultdict(list)
    for finding in findings:
        by_dim[finding.dimension].append(finding)
    dimensions: list[dict[str, Any]] = []
    for dim in ALL_DIMENSIONS:
        items = by_dim.get(dim, [])
        dim_meta = meta.get(dim) or DimensionMetaRow()

        def rows(kind: str) -> list[dict[str, Any]]:
            selected = [f for f in items if f.kind == kind]
            if max_findings_per_kind is not None:
                selected = selected[: max(0, int(max_findings_per_kind))]
            return [
                {
                    "ref": f.ref,
                    "statement": f.statement,
                    "confidence": f.confidence,
                    "quotes": [_short(q, SYNTH_QUOTE_MAX_CHARS) for q in f.quotes[: max(0, quotes_per_finding)]],
                }
                for f in selected
            ]

        dimensions.append(
            {
                "dimension": dim,
                "label": DIMENSION_LABELS[dim],
                "model_default": dim_meta.model_default,
                "devices": list(dim_meta.devices),
                "distinctiveness": dim_meta.distinctiveness,
                "observations": rows(FindingKind.OBSERVATION.value),
                "avoid": rows(FindingKind.FORBIDDEN_PATTERN.value),
            }
        )
    return {
        "book_title": book_title,
        "dimensions": dimensions,
        "voice_habits": [str(line) for line in voice_habits if str(line).strip()],
        "structure_facts": [str(line) for line in structure_facts if str(line).strip()],
    }


def fit_synthesis_payload(build: Callable[..., dict[str, Any]], fits: Callable[[dict[str, Any]], bool]) -> tuple[dict[str, Any], str]:
    """合成载荷装进节点预算：先每条发现 2 → 1 → 0 处引文，再每维每类 5 → 3 → 2 → 1 条发现。

    ``build(quotes_per_finding=, max_findings_per_kind=)`` 生成载荷；返回 (载荷, 降级阶段)。都装不下时返回
    最小的那一份（调用方照发——预算是估计的上界）。
    """
    ladder: list[tuple[str, dict[str, Any]]] = [
        ("full", {"quotes_per_finding": 2, "max_findings_per_kind": None}),
        ("one_quote", {"quotes_per_finding": 1, "max_findings_per_kind": None}),
        ("no_quotes", {"quotes_per_finding": 0, "max_findings_per_kind": None}),
        ("findings_3", {"quotes_per_finding": 0, "max_findings_per_kind": 3}),
        ("findings_2", {"quotes_per_finding": 0, "max_findings_per_kind": 2}),
        ("findings_1", {"quotes_per_finding": 0, "max_findings_per_kind": 1}),
    ]
    payload: dict[str, Any] = {}
    for stage, kwargs in ladder:
        payload = build(**kwargs)
        if fits(payload):
            return payload, stage
    return payload, ladder[-1][0]


# ---------------------------------------------------------------------------
# 模型输出 → 文风卡
# ---------------------------------------------------------------------------

_QUOTED_RE = re.compile(r"「[^」]*」|“[^”]*”|\"[^\"]*\"|『[^』]*』")
_DIGIT_RE = re.compile(r"[0-9０-９%％‰]")
_BOUNDARY_RE = re.compile(r"[。；！？!?;]")


def has_statistics(text: str) -> bool:
    """引号外有阿拉伯数字 / 百分号 → 当作统计数字（「大约每十句一次」这类中文说法可以）。"""
    return bool(_DIGIT_RE.search(_QUOTED_RE.sub("", str(text or ""))))


def _clean_line(value: Any) -> str:
    text = compact_ws(value)
    while text[:1] in {"-", "•", "·", "*"}:
        text = text[1:].lstrip()
    return text


def _fit_length(text: str, limit: int) -> str | None:
    """超长的行按句读截到上限内（截不出一句完整的话就丢）。"""
    if len(text) <= limit:
        return text
    cut = [m.end() for m in _BOUNDARY_RE.finditer(text[:limit])]
    if cut and cut[-1] >= limit // 2:
        return text[: cut[-1]].rstrip("；;，,")
    return None


def _clamp01(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    if 1.0 < number <= 10.0:
        number /= 10.0
    return max(0.0, min(1.0, number))


def _ref_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.isdigit():
        return f"f{int(text)}"
    return text


@dataclass
class CardAssembly:
    card: DimensionCard | None
    profile_title: str = ""
    qualitative_summary: str = ""
    planning_guidance: list[str] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)
    line_count: int = 0

    def drop(self, reason: str, count: int = 1) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + count

    def to_cursor(self) -> dict[str, Any]:
        return {
            "card": self.card.model_dump(mode="json") if self.card is not None else None,
            "profile_title": self.profile_title,
            "qualitative_summary": self.qualitative_summary,
            "planning_guidance": list(self.planning_guidance),
            "dropped": dict(self.dropped),
            "line_count": self.line_count,
        }

    @classmethod
    def from_cursor(cls, raw: Mapping[str, Any]) -> "CardAssembly":
        card_raw = raw.get("card")
        return cls(
            card=DimensionCard.model_validate(card_raw) if isinstance(card_raw, Mapping) else None,
            profile_title=str(raw.get("profile_title") or ""),
            qualitative_summary=str(raw.get("qualitative_summary") or ""),
            planning_guidance=[str(x) for x in raw.get("planning_guidance") or []],
            dropped={str(k): int(v) for k, v in (raw.get("dropped") or {}).items()},
            line_count=int(raw.get("line_count") or 0),
        )


def assemble_card(
    structured: Any,
    findings: Sequence[FindingRef],
    meta: Mapping[str, DimensionMetaRow],
    *,
    generated_at: str,
) -> CardAssembly:
    """模型的文风卡输出 → 规范的 ``DimensionCard``（丢不认识的维、无依据 / 带统计数字 / 超长的行；见模块文档）。"""
    result = CardAssembly(card=None)
    if not isinstance(structured, Mapping):
        return result
    by_ref = {f.ref: f for f in findings}
    raw_dims = structured.get("dimensions")
    entries: dict[str, DimensionEntry] = {}
    for raw in raw_dims if isinstance(raw_dims, list) else []:
        if not isinstance(raw, Mapping):
            continue
        dim = str(raw.get("dimension") or "").strip()
        if dim not in DIMENSION_LABELS:
            result.drop(DROP_UNKNOWN_DIMENSION)
            continue
        if dim in entries:
            continue
        dim_meta = meta.get(dim) or DimensionMetaRow()
        dim_distinct = _clamp01(raw.get("distinctiveness"))
        if dim_distinct is None:
            dim_distinct = dim_meta.distinctiveness if dim_meta.distinctiveness is not None else 0.5
        lines: list[CardLine] = []
        for kind, field_name, limit in (("do", "do", MAX_DO_LINES), ("avoid", "avoid", MAX_AVOID_LINES)):
            kept = 0
            for item in raw.get(field_name) if isinstance(raw.get(field_name), list) else []:
                if not isinstance(item, Mapping):
                    continue
                text = _clean_line(item.get("text"))
                if not text:
                    continue
                fitted = _fit_length(text, CARD_LINE_MAX_CHARS)
                if fitted is None:
                    result.drop(DROP_TOO_LONG)
                    continue
                if has_statistics(fitted):
                    result.drop(DROP_NUMBERS)
                    continue
                refs = [by_ref[key] for key in dict.fromkeys(_ref_key(r) for r in item.get("refs") or []) if key in by_ref]
                if not refs:
                    result.drop(DROP_UNGROUNDED)
                    continue
                if kept >= limit:
                    result.drop(DROP_OVER_LIMIT)
                    continue
                quote_ids = list(dict.fromkeys(q for f in refs for q in f.quote_ids))[:6]
                distinct = _clamp01(item.get("distinctiveness"))
                lines.append(
                    CardLine(
                        line_id=line_id_for(dim, fitted),
                        text=fitted,
                        kind=kind,  # type: ignore[arg-type]
                        source="synthesis",
                        evidence_quote_ids=quote_ids,
                        finding_ids=[f.finding_id for f in refs],
                        distinctiveness=distinct if distinct is not None else dim_distinct,
                        mandatory=bool(item.get("mandatory")) and kind == "do",
                    )
                )
                kept += 1
        summary = _clean_line(raw.get("summary"))
        model_default = _clean_line(raw.get("model_default")) or dim_meta.model_default
        entries[dim] = DimensionEntry(
            dimension=dim,
            label=DIMENSION_LABELS[dim],
            summary=summary[:SUMMARY_MAX_CHARS] if not has_statistics(summary) else "",
            model_default=model_default[:MODEL_DEFAULT_MAX_CHARS] if not has_statistics(model_default) else "",
            lines=lines,
            devices=clean_devices([*clean_devices(raw.get("devices")), *dim_meta.devices]),
            distinctiveness=dim_distinct,
        )
    temperament: list[str] = []
    for item in structured.get("temperament") if isinstance(structured.get("temperament"), list) else []:
        text = _fit_length(_clean_line(item), CARD_LINE_MAX_CHARS)
        if text and not has_statistics(text) and text not in temperament:
            temperament.append(text)
    card = normalize_card(
        DimensionCard(dimensions=list(entries.values()), temperament=temperament[:MAX_TEMPERAMENT], generated_at=generated_at)
    )
    if card is not None:
        card = _limit_mandatory(card)
    result.card = card
    result.line_count = len(card.all_lines()) if card is not None else 0
    result.profile_title = _clean_line(structured.get("profile_title"))[:PROFILE_TITLE_MAX_CHARS]
    summary_text = _clean_line(structured.get("qualitative_summary"))
    result.qualitative_summary = _drop_statistic_sentences(summary_text)[:QUALITATIVE_SUMMARY_MAX_CHARS]
    planning: list[str] = []
    for item in structured.get("planning_guidance") if isinstance(structured.get("planning_guidance"), list) else []:
        text = _fit_length(_clean_line(item), PLANNING_LINE_MAX_CHARS)
        if not text or has_statistics(text):
            continue
        if "：" not in text[:8] and ":" in text[:8]:
            text = text.replace(":", "：", 1)
        if "：" not in text[:8]:
            text = f"场景：{text}"
        if text not in planning:
            planning.append(text)
    result.planning_guidance = planning[:MAX_PLANNING_LINES]
    return result


def _drop_statistic_sentences(text: str) -> str:
    parts = re.split(r"(?<=[。！？；])", text)
    return "".join(part for part in parts if part and not has_statistics(part)).strip()


def _limit_mandatory(card: DimensionCard) -> DimensionCard:
    """「必须体现」全卡至多 ``MAX_MANDATORY`` 条（辨识度高的留）；模型一条没标时取辨识度最高的两条 do。"""
    do_lines = [(dim, line) for dim, line in card.all_lines() if line.kind == "do"]
    marked = [(dim, line) for dim, line in do_lines if line.mandatory]
    if marked:
        keep = {line.line_id for _dim, line in sorted(marked, key=lambda pair: -pair[1].distinctiveness)[:MAX_MANDATORY]}
    else:
        keep = {
            line.line_id
            for _dim, line in sorted(do_lines, key=lambda pair: -pair[1].distinctiveness)[:DEFAULT_MANDATORY]
            if line.distinctiveness >= 0.6
        }
    entries = [
        entry.model_copy(
            update={"lines": [line.model_copy(update={"mandatory": line.line_id in keep}) for line in entry.lines]}
        )
        for entry in card.dimensions
    ]
    return card.model_copy(update={"dimensions": entries})


# ---------------------------------------------------------------------------
# 对账（台账 E6）
# ---------------------------------------------------------------------------

# 标点：(名字的写法, 测量核特征, 几乎不用的上限, 常用的下限)——与声音习惯同一套阈值(voice_signature._PUNCT_HABITS),
# 另加逗号 / 句号
_PUNCT_NAMES: dict[str, tuple[str, ...]] = {
    "punct_ellipsis_per_1k": ("省略号",),
    "punct_dash_per_1k": ("破折号",),
    "punct_semicolon_per_1k": ("分号",),
    "punct_exclamation_per_1k": ("感叹号", "叹号"),
    "punct_question_per_1k": ("问号",),
    "punct_colon_per_1k": ("冒号",),
    "punct_enumeration_per_1k": ("顿号",),
    "punct_comma_per_1k": ("逗号",),
    "punct_period_per_1k": ("句号",),
}
_EXTRA_THRESHOLDS: dict[str, tuple[float, float]] = {
    "punct_comma_per_1k": (20.0, 45.0),
    "punct_period_per_1k": (10.0, 35.0),
}
MARK_THRESHOLDS: dict[str, tuple[float, float]] = {
    **{name: (low, high) for name, _label, low, high in _PUNCT_HABITS},
    **_EXTRA_THRESHOLDS,
}
_FREQUENT_CUES = (
    "常用", "多用", "爱用", "惯用", "频繁", "大量", "密集", "高频", "反复", "连用", "连续", "偏爱", "常常",
    "经常", "总用", "常以", "常带", "常见", "充满", "不断", "密",
)
_RARE_CUES = (
    "几乎不用", "很少", "极少", "少用", "不用", "罕用", "避免", "回避", "不写", "稀疏", "稀少", "停用", "弃用",
    "不加", "克制", "节制", "禁用", "别用", "不太用", "鲜用", "从不", "不见", "省去", "少见", "不常用", "不多用",
    "不爱用", "不常", "少有",
)
_CUES: tuple[tuple[str, str], ...] = tuple(
    [(cue, "frequent") for cue in _FREQUENT_CUES] + [(cue, "rare") for cue in _RARE_CUES]
)
_CUE_WINDOW = 8
_SENTENCE_MAJORITY = {
    "short": ("短句为主", "以短句为主", "多用短句", "短句居多", "短句占多数", "句子普遍很短", "句子都很短"),
    "long": ("长句为主", "以长句为主", "多用长句", "长句居多", "长句占多数", "句子普遍很长", "句子都很长"),
}
# 句子长短为主的说法与全书平均句长(可见字)相反:说「短句为主」而平均 ≥ 30 字,说「长句为主」而平均 ≤ 14 字
_SHORT_CLAIM_MAX_MEAN = 30.0
_LONG_CLAIM_MIN_MEAN = 14.0


def _nearest_cue(window: str, *, before: bool) -> str | None:
    """窗口里离标点名最近的提示词的极性(前窗取结束得最晚的、后窗取开始得最早的;并列取最长的那个)。"""
    best: tuple[int, int, str] | None = None
    for cue, polarity in _CUES:
        index = window.rfind(cue) if before else window.find(cue)
        if index < 0:
            continue
        distance = len(window) - (index + len(cue)) if before else index
        key = (distance, -len(cue), polarity)
        if best is None or key < best:
            best = key
    return best[2] if best is not None else None


def _polarity(text: str, name: str) -> str | None:
    """一行里对某个标点的说法:frequent / rare / None(先看紧挨着它前面 8 字里最近的提示词,没有再看后面 8 字)。"""
    polarities: set[str] = set()
    for match in re.finditer(re.escape(name), text):
        polarity = _nearest_cue(text[max(0, match.start() - _CUE_WINDOW) : match.start()], before=True)
        if polarity is None:
            polarity = _nearest_cue(text[match.end() : match.end() + _CUE_WINDOW], before=False)
        if polarity is not None:
            polarities.add(polarity)
    return next(iter(polarities)) if len(polarities) == 1 else None


def line_claims(text: str) -> dict[str, str]:
    """一行卡片对可测事实的说法 ``{特征: frequent|rare|short|long}``（标点名 + 句子长短为主）。"""
    claims: dict[str, str] = {}
    for feature, names in _PUNCT_NAMES.items():
        for name in names:
            if name in text:
                polarity = _polarity(text, name)
                if polarity is not None:
                    claims[feature] = polarity
                break
    short = any(cue in text for cue in _SENTENCE_MAJORITY["short"])
    long_ = any(cue in text for cue in _SENTENCE_MAJORITY["long"])
    if short != long_:
        claims["sentence_majority"] = "short" if short else "long"
    return claims


def _contradicts_measure(feature: str, polarity: str, features: Mapping[str, Any]) -> bool:
    if feature == "sentence_majority":
        mean = features.get("sent_len_mean")
        if not isinstance(mean, (int, float)) or mean <= 0:
            return False
        return (polarity == "short" and mean >= _SHORT_CLAIM_MAX_MEAN) or (
            polarity == "long" and mean <= _LONG_CLAIM_MIN_MEAN
        )
    thresholds = MARK_THRESHOLDS.get(feature)
    value = features.get(feature)
    if thresholds is None or not isinstance(value, (int, float)):
        return False
    low, high = thresholds
    return (polarity == "frequent" and value < low) or (polarity == "rare" and value >= high)


def _opposite(a: str, b: str) -> bool:
    return {a, b} in ({"frequent", "rare"}, {"short", "long"})


def reconcile_card(card: DimensionCard | None, *, voice_features: Mapping[str, Any]) -> tuple[DimensionCard | None, dict[str, int]]:
    """对账（见模块文档）：返回 (对账后的卡, {丢弃原因: 条数})。确定性。"""
    dropped: dict[str, int] = {}
    if card is None:
        return None, dropped
    claims: list[tuple[str, CardLine, dict[str, str]]] = []
    removed: set[str] = set()
    for dim, line in card.all_lines():
        line_claim = line_claims(line.text)
        if any(_contradicts_measure(feature, polarity, voice_features) for feature, polarity in line_claim.items()):
            removed.add(line.line_id)
            dropped[DROP_CONTRADICTS_MEASURE] = dropped.get(DROP_CONTRADICTS_MEASURE, 0) + 1
            continue
        if line_claim:
            claims.append((dim, line, line_claim))

    def strength(line: CardLine) -> tuple[int, float, int]:
        return (len(line.evidence_quote_ids), line.distinctiveness, 1 if line.kind == "do" else 0)

    for index, (_dim_a, line_a, claim_a) in enumerate(claims):
        if line_a.line_id in removed:
            continue
        for _dim_b, line_b, claim_b in claims[index + 1 :]:
            if line_b.line_id in removed:
                continue
            if any(feature in claim_b and _opposite(polarity, claim_b[feature]) for feature, polarity in claim_a.items()):
                loser = line_b if strength(line_a) >= strength(line_b) else line_a
                removed.add(loser.line_id)
                dropped[DROP_CONTRADICTION] = dropped.get(DROP_CONTRADICTION, 0) + 1
                if loser is line_a:
                    break
    if not removed:
        return card, dropped
    return _without_lines(card, removed), dropped


def _without_lines(card: DimensionCard, line_ids: set[str]) -> DimensionCard:
    entries = [
        entry.model_copy(update={"lines": [line for line in entry.lines if line.line_id not in line_ids]})
        for entry in card.dimensions
    ]
    return card.model_copy(update={"dimensions": entries})


# ---------------------------------------------------------------------------
# 定稿过滤：受保护专名 / 原文重合
# ---------------------------------------------------------------------------


def filter_card(
    assembly: CardAssembly,
    *,
    protected: Iterable[str],
    overlaps: Callable[[str], bool],
) -> tuple[CardAssembly, dict[str, int]]:
    """丢掉含受保护专名或与原书有 ≥12 字连续重合的行 / 气质 / 规划行；概述里对应的句子删掉。"""
    terms = [t for t in protected if t]
    dropped: dict[str, int] = {}

    def bad(text: str) -> str | None:
        if any(term in text for term in terms):
            return DROP_PROTECTED
        if overlaps(text):
            return DROP_OVERLAP
        return None

    card = assembly.card
    if card is not None:
        removed: set[str] = set()
        for _dim, line in card.all_lines():
            reason = bad(line.text)
            if reason is not None:
                removed.add(line.line_id)
                dropped[reason] = dropped.get(reason, 0) + 1
        entries = []
        for entry in card.dimensions:
            updates: dict[str, Any] = {"lines": [line for line in entry.lines if line.line_id not in removed]}
            if entry.summary and bad(entry.summary):
                updates["summary"] = ""
            if entry.model_default and bad(entry.model_default):
                updates["model_default"] = ""
            updates["devices"] = [d for d in entry.devices if not any(term in d for term in terms)]
            entries.append(entry.model_copy(update=updates))
        temperament = []
        for text in card.temperament:
            reason = bad(text)
            if reason is None:
                temperament.append(text)
            else:
                dropped[reason] = dropped.get(reason, 0) + 1
        card = card.model_copy(update={"dimensions": entries, "temperament": temperament})
    planning = []
    for text in assembly.planning_guidance:
        reason = bad(text)
        if reason is None:
            planning.append(text)
        else:
            dropped[reason] = dropped.get(reason, 0) + 1
    summary = "".join(
        part for part in re.split(r"(?<=[。！？；])", assembly.qualitative_summary) if part and bad(part) is None
    ).strip()
    title = assembly.profile_title if assembly.profile_title and bad(assembly.profile_title) is None else ""
    merged = dict(assembly.dropped)
    for reason, count in dropped.items():
        merged[reason] = merged.get(reason, 0) + count
    result = CardAssembly(
        card=card,
        profile_title=title,
        qualitative_summary=summary,
        planning_guidance=planning,
        dropped=merged,
        line_count=len(card.all_lines()) if card is not None else 0,
    )
    return result, dropped


# ---------------------------------------------------------------------------
# 行状态沿用 / 叙事机制指引 / 维度摘要
# ---------------------------------------------------------------------------


def carry_line_states(
    card: DimensionCard | None,
    previous_card: DimensionCard | None,
    previous_states: Mapping[str, str],
) -> tuple[DimensionCard | None, dict[str, str], int]:
    """重新学习时沿用作者的 ✓ / ✗（按 line_id）。

    - ✗（excluded）全部保留：以后哪次重新学习又写出同一句，它仍然不用；
    - ✓（pinned）的句子新卡里没有 → 从旧卡搬回原维度（``source="pinned_carryover"``）——作者说「永远带上」的
      不因为重新学习悄悄消失。返回 (新卡, 行状态, 搬回的条数)。
    """
    states = {str(k): str(v) for k, v in (previous_states or {}).items() if str(v) in ("pinned", "excluded")}
    if card is None:
        return None, states, 0
    present = {line.line_id for _dim, line in card.all_lines()}
    carried = 0
    if previous_card is not None:
        missing: dict[str, list[CardLine]] = defaultdict(list)
        for dim, line in previous_card.all_lines():
            if states.get(line.line_id) == "pinned" and line.line_id not in present:
                missing[dim].append(line.model_copy(update={"source": "pinned_carryover"}))
        if missing:
            entries = []
            for entry in card.dimensions:
                extra = missing.get(entry.dimension, [])
                carried += len(extra)
                entries.append(entry.model_copy(update={"lines": [*entry.lines, *extra]}) if extra else entry)
            card = card.model_copy(update={"dimensions": entries})
    return card, states, carried


def derive_narrative_guidance(card: DimensionCard | None, *, limit: int = NARRATIVE_GUIDANCE_MAX_LINES) -> list[str]:
    """叙事层的卡片行 → 规划 / 初稿用的叙事机制指引（avoid 行带「避免：」极性标记），≤``limit`` 行。"""
    if card is None:
        return []
    lines: list[str] = []
    seen: set[str] = set()
    for entry in sorted(
        (e for e in card.dimensions if e.dimension.startswith("narrative.")),
        key=lambda e: -e.distinctiveness,
    ):
        for line in sorted(entry.lines, key=lambda line: (line.kind != "do", not line.mandatory, -line.distinctiveness)):
            text = line.text if line.kind == "do" else mark_forbidden_narrative_statement(line.text)
            key = normalize_text_for_matching(text)
            if not key or key in seen:
                continue
            seen.add(key)
            lines.append(text)
            if len(lines) >= limit:
                return lines
    return lines


def sub_dimension_summary(session: Session, run_id: str) -> dict[str, dict[str, Any]]:
    """``profile_json.sub_dimensions``：每维观察 / 避免条数、证据引文数（按证据计，台账 E10）与置信度。"""
    findings = list(session.scalars(select(StyleReferenceFinding).where(StyleReferenceFinding.run_id == run_id)))
    evidence_counts: dict[str, int] = defaultdict(int)
    for finding_id, _quote_id in session.execute(
        select(StyleReferenceEvidence.finding_id, StyleReferenceEvidence.quote_id).where(
            StyleReferenceEvidence.finding_id.in_([f.finding_id for f in findings] or [""])
        )
    ).all():
        evidence_counts[str(finding_id)] += 1
    summary: dict[str, dict[str, Any]] = {}
    confidences: dict[str, list[str]] = defaultdict(list)
    for finding in findings:
        entry = summary.setdefault(
            str(finding.sub_dimension),
            {"observation_count": 0, "forbidden_pattern_count": 0, "quote_count": 0, "confidence": "medium"},
        )
        if finding.finding_kind == FindingKind.OBSERVATION.value:
            entry["observation_count"] += 1
        else:
            entry["forbidden_pattern_count"] += 1
        entry["quote_count"] += evidence_counts.get(str(finding.finding_id), 0)
        confidences[str(finding.sub_dimension)].append(str(finding.confidence or "medium"))
    for dim, values in confidences.items():
        summary[dim]["confidence"] = max(set(values), key=lambda value: (values.count(value), value == "high"))
    return {dim: summary[dim] for dim in ALL_DIMENSIONS if dim in summary}


def json_clean(value: Any) -> Any:
    """画像是 JSON 列：把 numpy 标量等规整成纯 JSON 值。"""

    def fallback(item: Any) -> Any:
        try:
            return float(item)
        except (TypeError, ValueError):
            return str(item)

    return json.loads(json.dumps(value, ensure_ascii=False, default=fallback))


__all__ = [
    "CardAssembly",
    "DimensionMetaRow",
    "FindingRef",
    "MARK_THRESHOLDS",
    "MIN_CARD_LINES",
    "assemble_card",
    "carry_line_states",
    "derive_narrative_guidance",
    "filter_card",
    "fit_synthesis_payload",
    "has_statistics",
    "json_clean",
    "line_claims",
    "load_dimension_meta",
    "load_run_findings",
    "reconcile_card",
    "sub_dimension_summary",
    "synthesis_payload",
]
