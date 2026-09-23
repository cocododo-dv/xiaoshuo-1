"""风格参考 v3 —「学习文风」的分层抽取：同一组窗口、一层一次调用、逐字核对证据、重试合并。

- **样本**（``build_extraction_set``）：选窗步挑出的窗口（按段落区间，不依赖窗口号——索引重建也读同一段文字），
  只取正文段（跳过章题 / 场分隔 / 脚注 / 落款，与窗口索引同一条判断），每段前标【p】（整组样本里唯一的段号），
  送整段正文；
- **一层一次调用**：每层 4 个维度，每维 ≤5 条观察 + ≤2 条「作者不这么写」，每条 ≥2 条原文引文（段号 + 逐字引文），
  外加一句「通用模型在这一维的默认写法」、≤3 个手法名（≤8 字）与辨识度估计；
- **证据核对**（``parse_layer_output``）：引文必须是给定段落里连续、逐字一致的一小段（``evidence.py`` 的对齐器；
  段号给错时只允许在整组样本里唯一反查），对不上的引文丢掉，凑不够 2 条的发现丢掉；空泛形容词的陈述丢掉；
- **重试合并**（台账 E5）：输出不成形、某一维一条观察都没通过、或被丢的发现超过三分之一时重试一次（带问题清单），
  两次的有效发现**合并**去重——不再因为重试丢掉第一次已经核对通过的发现；
- **落库**（``persist_layer``）：沿用抽取 / 发现 / 引文 / 证据四张表（矩阵据此展示证据）：每维一行抽取（原始载荷
  记模型默认写法、手法名、辨识度），引文的 ``illustrates_dims`` 记所属维度（界面按证据计数，台账 E10）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceParagraph
from novel_system.services.style_reference.banned_adjective import check_banned_adjectives
from novel_system.services.style_reference.card import DIMENSION_LABELS
from novel_system.services.style_reference.dimensions import LAYER_TO_SUB_DIMS, Layer
from novel_system.services.style_reference.evidence import align_evidence_to_paragraph_lookup
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import (
    AnchorKind,
    ExtractionEvidenceInput,
    ExtractionPurpose,
    FindingKind,
)
from novel_system.services.style_reference.structure import non_body_kind
from novel_system.services.style_reference.tags import DEVICE_MAX_CHARS
from novel_system.services.style_reference.text_utils import compact_ws
from novel_system.services.style_reference.validation.plagiarism import normalize_text_for_matching

MAX_OBSERVATIONS = 5
MAX_AVOID = 2
MIN_EVIDENCE = 2
MAX_EVIDENCE = 4
MAX_DEVICES = 3
QUOTE_MIN_CHARS = 4
QUOTE_MAX_CHARS = 80
STATEMENT_MIN_CHARS = 6
STATEMENT_MAX_CHARS = 160
MODEL_DEFAULT_MAX_CHARS = 120
# 送整段:对齐器按模型实际看到的全段核对引文
_ALIGN_CHAR_LIMIT = 100_000
_CONFIDENCE = ("high", "medium", "low")

POSITION_LABELS: dict[str, str] = {"opening": "章首", "closing": "章末", "middle": "章中", "whole": "整章"}


def layer_dimensions(layer: str) -> list[str]:
    return [dim.value for dim in LAYER_TO_SUB_DIMS[Layer(layer)]]


# ---------------------------------------------------------------------------
# 样本集
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SetParagraph:
    number: int
    paragraph_id: str
    paragraph_index: int
    paragraph_type: str
    window_no: int
    text: str  # 送给模型的正文(空白压缩过)
    raw_text: str = ""  # 库里的原文:引文坐标按它记

    @property
    def source_text(self) -> str:
        return self.raw_text or self.text


@dataclass
class ExtractionSet:
    """一组抽取窗口：送给模型的窗口块 + 段号 → 段落。"""

    windows: list[dict[str, Any]]
    paragraphs: dict[int, SetParagraph]
    strata: dict[int, str] = field(default_factory=dict)

    @property
    def chars(self) -> int:
        return sum(len(p.text) for p in self.paragraphs.values())

    def lookup(self) -> dict[str, str]:
        """段落 id → 原文(对齐器在原文上核对、按原文坐标记引文)。"""
        return {p.paragraph_id: p.source_text for p in self.paragraphs.values()}

    def by_id(self) -> dict[str, SetParagraph]:
        return {p.paragraph_id: p for p in self.paragraphs.values()}

    def without_windows(self, window_nos: set[int]) -> "ExtractionSet":
        return ExtractionSet(
            windows=[w for w in self.windows if int(w["window"]) not in window_nos],
            paragraphs={n: p for n, p in self.paragraphs.items() if p.window_no not in window_nos},
            strata={k: v for k, v in self.strata.items() if k not in window_nos},
        )


def build_extraction_set(session: Session, book_id: str, selected: Sequence[Mapping[str, Any]]) -> ExtractionSet:
    """按选窗步记下的段落区间取正文段、编段号（``selected`` 是 ``Selection.to_cursor()["windows"]``）。"""
    windows: list[dict[str, Any]] = []
    paragraphs: dict[int, SetParagraph] = {}
    strata: dict[int, str] = {}
    number = 0
    for item in selected:
        window_no = int(item["window_no"])
        rows = session.execute(
            select(
                StyleReferenceParagraph.paragraph_id,
                StyleReferenceParagraph.paragraph_index,
                StyleReferenceParagraph.paragraph_type,
                StyleReferenceParagraph.text,
            )
            .where(
                StyleReferenceParagraph.book_id == str(book_id),
                StyleReferenceParagraph.paragraph_index >= int(item["start"]),
                StyleReferenceParagraph.paragraph_index <= int(item["end"]),
            )
            .order_by(StyleReferenceParagraph.paragraph_index)
        ).all()
        lines: list[str] = []
        for pid, index, ptype, text in rows:
            body = compact_ws(text)
            if not body or non_body_kind(body) is not None:
                continue
            number += 1
            paragraphs[number] = SetParagraph(
                number=number,
                paragraph_id=str(pid),
                paragraph_index=int(index),
                paragraph_type=str(ptype or ""),
                window_no=window_no,
                text=body,
                raw_text=str(text or ""),
            )
            lines.append(f"【{number}】{body}")
        if not lines:
            continue
        windows.append(
            {
                "window": window_no,
                "chapter": int(item.get("chapter") or 0),
                "position": POSITION_LABELS.get(str(item.get("position") or ""), "章中"),
                "text": "\n".join(lines),
            }
        )
        strata[window_no] = str(item.get("stratum") or "")
    return ExtractionSet(windows=windows, paragraphs=paragraphs, strata=strata)


# 预算装不下时先卸哪些窗:补位的典型窗 → 描写 / 动作 / 心理 → 叙述 / 对白 → 章末 / 章首
_DROP_ORDER = ("typical", "all", "description", "action", "psychology", "narration", "dialogue", "closing", "opening")
MIN_SET_WINDOWS = 3


def shrink_to_fit(ext_set: ExtractionSet, fits) -> tuple[ExtractionSet, list[int]]:  # noqa: ANN001
    """样本装不下节点的输入预算时按 ``_DROP_ORDER`` 整窗卸载（至少留 ``MIN_SET_WINDOWS`` 窗）。"""
    dropped: list[int] = []
    current = ext_set
    while not fits(current) and len(current.windows) > MIN_SET_WINDOWS:
        sizes = {int(w["window"]): len(str(w["text"])) for w in current.windows}

        def rank(window_no: int) -> tuple[int, int, int]:
            stratum = current.strata.get(window_no, "typical")
            order = _DROP_ORDER.index(stratum) if stratum in _DROP_ORDER else 0
            return (order, -sizes[window_no], -window_no)

        victim = min(sizes, key=rank)
        dropped.append(victim)
        current = current.without_windows({victim})
    return current, dropped


def layer_payload(ext_set: ExtractionSet, layer: str, *, book_title: str) -> dict[str, Any]:
    return {
        "book_title": book_title,
        "layer": layer,
        "dimensions": [{"key": dim, "label": DIMENSION_LABELS[dim]} for dim in layer_dimensions(layer)],
        "windows": ext_set.windows,
    }


# ---------------------------------------------------------------------------
# 解析与核对
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidEvidence:
    paragraph: SetParagraph
    span: tuple[int, int]
    quote: str


@dataclass
class ValidFinding:
    dimension: str
    kind: str  # FindingKind.value
    statement: str
    confidence: str
    distinctiveness: float | None
    evidence: list[ValidEvidence]

    def key(self) -> tuple[str, str, str]:
        return (self.dimension, self.kind, normalize_text_for_matching(self.statement))


@dataclass
class DimensionMeta:
    model_default: str = ""
    devices: list[str] = field(default_factory=list)
    distinctiveness: float | None = None


@dataclass
class LayerParse:
    layer: str
    findings: list[ValidFinding] = field(default_factory=list)
    meta: dict[str, DimensionMeta] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    returned: int = 0
    rejected: int = 0
    malformed: bool = False

    def observations(self, dimension: str) -> list[ValidFinding]:
        return [f for f in self.findings if f.dimension == dimension and f.kind == FindingKind.OBSERVATION.value]

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for dim in layer_dimensions(self.layer):
            findings = [f for f in self.findings if f.dimension == dim]
            out[dim] = {
                "observations": sum(1 for f in findings if f.kind == FindingKind.OBSERVATION.value),
                "avoid": sum(1 for f in findings if f.kind == FindingKind.FORBIDDEN_PATTERN.value),
                "evidence": sum(len(f.evidence) for f in findings),
            }
        return out


def _clamp01(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    if number > 1.0 and number <= 10.0:
        number = number / 10.0  # 0–10 尺度的模型
    return max(0.0, min(1.0, number))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip().strip("【】[]p").strip()
        if text.isdigit():
            return int(text)
    return None


def clean_devices(values: Any, *, limit: int = MAX_DEVICES) -> list[str]:
    out: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        text = compact_ws(value).strip("「」“”\"'《》")
        if not text or len(text) > DEVICE_MAX_CHARS or text in out:
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _parse_evidence(raw: Any, ext_set: ExtractionSet, lookup: dict[str, str], by_id: dict[str, SetParagraph]) -> list[ValidEvidence]:
    items = raw if isinstance(raw, list) else []
    valid: list[ValidEvidence] = []
    seen: set[tuple[str, tuple[int, int]]] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        quote = compact_ws(item.get("quote"))
        if len(quote) < QUOTE_MIN_CHARS or len(quote) > QUOTE_MAX_CHARS:
            continue
        number = _as_int(item.get("p", item.get("paragraph")))
        paragraph = ext_set.paragraphs.get(number) if number is not None else None
        try:
            candidate = ExtractionEvidenceInput(
                paragraph_id=paragraph.paragraph_id if paragraph is not None else None,
                quote=quote,
                anchor_kind=AnchorKind.PARAGRAPH_QUOTE,
            )
        except Exception:  # noqa: BLE001 — 结构不对的引文直接丢
            continue
        aligned = align_evidence_to_paragraph_lookup(candidate, lookup, prompt_char_limit=_ALIGN_CHAR_LIMIT)
        if aligned is None or aligned.span is None or aligned.paragraph_id not in by_id:
            continue
        key = (aligned.paragraph_id, (int(aligned.span[0]), int(aligned.span[1])))
        if key in seen:
            continue
        seen.add(key)
        valid.append(ValidEvidence(paragraph=by_id[aligned.paragraph_id], span=key[1], quote=aligned.quote))
        if len(valid) >= MAX_EVIDENCE:
            break
    return valid


def parse_layer_output(structured: Any, layer: str, ext_set: ExtractionSet) -> LayerParse:
    """核对一层的输出（见模块文档）。不抛异常：问题记在 ``problems``，``malformed`` 表示整体不成形。"""
    parse = LayerParse(layer=layer)
    dims = layer_dimensions(layer)
    for dim in dims:
        parse.meta[dim] = DimensionMeta()
    entries = structured.get("dimensions") if isinstance(structured, Mapping) else None
    if not isinstance(entries, list):
        parse.malformed = True
        parse.problems.append("输出里没有 dimensions 数组")
        return parse
    lookup = ext_set.lookup()
    by_id = ext_set.by_id()
    seen_dims: set[str] = set()
    seen_keys: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        dim = str(entry.get("dimension") or entry.get("key") or "").strip()
        if dim not in dims or dim in seen_dims:
            continue
        seen_dims.add(dim)
        parse.meta[dim] = DimensionMeta(
            model_default=compact_ws(entry.get("model_default"))[:MODEL_DEFAULT_MAX_CHARS],
            devices=clean_devices(entry.get("devices")),
            distinctiveness=_clamp01(entry.get("distinctiveness")),
        )
        for field_name, kind, limit in (
            ("observations", FindingKind.OBSERVATION.value, MAX_OBSERVATIONS),
            ("avoid", FindingKind.FORBIDDEN_PATTERN.value, MAX_AVOID),
        ):
            raw_items = entry.get(field_name)
            kept = 0
            for raw in raw_items if isinstance(raw_items, list) else []:
                if not isinstance(raw, Mapping):
                    continue
                parse.returned += 1
                statement = compact_ws(raw.get("statement"))
                if (
                    kept >= limit
                    or len(statement) < STATEMENT_MIN_CHARS
                    or len(statement) > STATEMENT_MAX_CHARS
                    or check_banned_adjectives(statement)
                ):
                    parse.rejected += 1
                    continue
                evidence = _parse_evidence(raw.get("evidence"), ext_set, lookup, by_id)
                if len(evidence) < MIN_EVIDENCE:
                    parse.rejected += 1
                    continue
                confidence = str(raw.get("confidence") or "medium").strip().lower()
                finding = ValidFinding(
                    dimension=dim,
                    kind=kind,
                    statement=statement,
                    confidence=confidence if confidence in _CONFIDENCE else "medium",
                    distinctiveness=_clamp01(raw.get("distinctiveness")),
                    evidence=evidence,
                )
                if finding.key() in seen_keys:
                    parse.rejected += 1
                    continue
                seen_keys.add(finding.key())
                parse.findings.append(finding)
                kept += 1
    for dim in dims:
        if dim not in seen_dims:
            parse.problems.append(f"{dim}：输出里缺这一维")
        elif not parse.observations(dim):
            parse.problems.append(f"{dim}：没有一条观察通过证据核对（每条至少两处逐字引文）")
    if parse.rejected:
        parse.problems.append(f"共有 {parse.rejected} 条发现被丢弃（引文与原文对不上、不足两处、空泛或超长）")
    return parse


def needs_retry(parse: LayerParse) -> bool:
    """输出不成形、某一维一条观察都没通过、或被丢的发现超过三分之一 → 值得再问一次。"""
    if parse.malformed:
        return True
    if any(not parse.observations(dim) for dim in layer_dimensions(parse.layer)):
        return True
    return parse.returned > 0 and parse.rejected * 3 > parse.returned


def retry_instruction(parse: LayerParse) -> str:
    lines = [
        "【重试说明】上一次的输出有下面这些问题。请重新完整输出这一层的 4 个维度（已经合格的发现可以原样再给，"
        "系统会合并去重）：",
        *(f"- {problem}" for problem in parse.problems[:12]),
        "提醒：每条 evidence 的 p 是样本里【p】标出的段号，quote 必须从那一段里逐字复制、4–60 字、不加省略号、不拼接。",
    ]
    return "\n".join(lines)


def merge_layer_parses(first: LayerParse, second: LayerParse) -> LayerParse:
    """两次尝试的有效发现合并去重（第一次的在前，每维每类仍受数量上限）。"""
    merged = LayerParse(layer=first.layer)
    for dim in layer_dimensions(first.layer):
        a = first.meta.get(dim) or DimensionMeta()
        b = second.meta.get(dim) or DimensionMeta()
        devices = clean_devices([*a.devices, *b.devices])
        merged.meta[dim] = DimensionMeta(
            model_default=a.model_default or b.model_default,
            devices=devices,
            distinctiveness=a.distinctiveness if a.distinctiveness is not None else b.distinctiveness,
        )
    seen: set[tuple[str, str, str]] = set()
    counts: dict[tuple[str, str], int] = {}
    for finding in [*first.findings, *second.findings]:
        key = finding.key()
        slot = (finding.dimension, finding.kind)
        limit = MAX_OBSERVATIONS if finding.kind == FindingKind.OBSERVATION.value else MAX_AVOID
        if key in seen or counts.get(slot, 0) >= limit:
            continue
        seen.add(key)
        counts[slot] = counts.get(slot, 0) + 1
        merged.findings.append(finding)
    merged.returned = first.returned + second.returned
    merged.rejected = first.rejected + second.rejected
    merged.malformed = first.malformed and second.malformed
    merged.problems = [p for p in second.problems if "缺这一维" in p or "没有一条观察" in p]
    return merged


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------


def persist_layer(
    session: Session,
    *,
    book_id: str,
    run_id: str,
    parse: LayerParse,
    attempts: int,
    llm_call_id: str | None,
) -> dict[str, dict[str, int]]:
    """一层的结果写进抽取 / 发现 / 引文 / 证据四张表（调用方的事务；只 flush）。返回每维计数。"""
    repo = StyleReferenceRepository(session)
    purpose = ExtractionPurpose.EXTRACT.value if attempts <= 1 else ExtractionPurpose.FULL_RETRY.value
    for dim in layer_dimensions(parse.layer):
        meta = parse.meta.get(dim) or DimensionMeta()
        findings = [f for f in parse.findings if f.dimension == dim]
        extraction_id = f"sr_ext_{uuid.uuid4().hex[:12]}"
        finding_meta: dict[str, dict[str, Any]] = {}
        extraction = repo.create_extraction(
            extraction_id=extraction_id,
            book_id=book_id,
            run_id=run_id,
            layer=parse.layer,
            sub_dimension=dim,
            llm_call_id=llm_call_id,
            raw_payload_json={},
            status="done",
            validation_errors_json=[],
            purpose=purpose,
        )
        quotes: dict[tuple[str, int, int], str] = {}
        for finding in findings:
            finding_id = f"sr_find_{uuid.uuid4().hex[:12]}"
            repo.create_finding(
                finding_id=finding_id,
                book_id=book_id,
                run_id=run_id,
                extraction_id=extraction_id,
                sub_dimension=dim,
                finding_kind=finding.kind,
                statement=finding.statement,
                confidence=finding.confidence,
                status="pending",
            )
            if finding.distinctiveness is not None:
                finding_meta[finding_id] = {"distinctiveness": round(finding.distinctiveness, 3)}
            for evidence in finding.evidence:
                key = (evidence.paragraph.paragraph_id, evidence.span[0], evidence.span[1])
                quote_id = quotes.get(key)
                if quote_id is None:
                    quote_id = f"sr_quote_{uuid.uuid4().hex[:12]}"
                    repo.create_quote(
                        quote_id=quote_id,
                        book_id=book_id,
                        paragraph_id=evidence.paragraph.paragraph_id,
                        span_start=evidence.span[0],
                        span_end=evidence.span[1],
                        quote_text=evidence.quote,
                        illustrates_dims=[dim],
                        extracted_features={
                            "anchor_kind": AnchorKind.PARAGRAPH_QUOTE.value,
                            "paragraph_type": evidence.paragraph.paragraph_type,
                            "paragraph_index": evidence.paragraph.paragraph_index,
                            "window_no": evidence.paragraph.window_no,
                        },
                    )
                    quotes[key] = quote_id
                repo.create_evidence(
                    evidence_id=f"sr_ev_{uuid.uuid4().hex[:12]}",
                    finding_id=finding_id,
                    quote_id=quote_id,
                    anchor_kind=AnchorKind.PARAGRAPH_QUOTE.value,
                )
        extraction.raw_payload_json = {
            "model_default": meta.model_default,
            "devices": list(meta.devices),
            "distinctiveness": meta.distinctiveness,
            "findings_count": len(findings),
            "attempts": int(attempts),
            "finding_meta": finding_meta,
        }
    session.flush()
    return parse.counts()


__all__ = [
    "ExtractionSet",
    "LayerParse",
    "MAX_AVOID",
    "MAX_OBSERVATIONS",
    "MIN_EVIDENCE",
    "POSITION_LABELS",
    "SetParagraph",
    "ValidEvidence",
    "ValidFinding",
    "build_extraction_set",
    "clean_devices",
    "layer_dimensions",
    "layer_payload",
    "merge_layer_parses",
    "needs_retry",
    "parse_layer_output",
    "persist_layer",
    "retry_instruction",
    "shrink_to_fit",
]
