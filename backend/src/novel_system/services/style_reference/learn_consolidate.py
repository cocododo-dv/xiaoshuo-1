"""风格参考 —「学习文风 · 通读全书」的归并步（2026-10-19）：全书各批的发现 → 每维几条有分量的发现。

通读全书时四层逐批读完（``learn_run`` 的抽取步，每批每层一次调用），每维攒下几十批、上百条发现，说法各不相同、
很多说的是同一件事。归并按维度各做一次调用（模板 ``style_ref_consolidate_findings``，走文风卡合成节点的路由——
不另设节点，作者不用再去系统配置补齐）：

- **载荷**（:func:`consolidate_payload`）：这一维全部发现（编号 r1…rN、类别、陈述、出自第几批）、各批写的「模型
  默认写法」与手法名；装不下节点预算时先去掉低置信、再去掉中置信的发现（:func:`fit_consolidate_payload`）；
- **核对**（:func:`parse_consolidation`）：模型把说同一件事的发现归成一组、给一句陈述；组里的编号必须是这一维、
  同一类的发现，一条发现只进一组；陈述过与抽取同一道长度 / 空泛词检查；
- **分量**（:func:`rank_groups`）：一组的「支持」= 它的发现出自几个不同的批；每维按支持数（再按辨识度）留观察
  ≤5、避免 ≤2——与快速学习交给文风卡合成的形状一样，只多了「在几批里见到过」；
- **落库**（:func:`persist_consolidated`）：每维一行抽取（``purpose="consolidate"``，原始载荷记模型默认写法、手法、
  每条归并发现的支持数与成员）、归并后的发现（``status="consolidated"``），证据指向成员**已经逐字核对过**的引文
  （优先取自不同的批），不新建引文。

**沿用**：读书是贵的那一步。一次通读的「身份」（:func:`reading_identity`：段落根哈希、窗口索引版本、切批版本与
上限、读了哪几层、各层抽取模板的版本与模型）不变时，下一次通读直接沿用上一次读出来的发现；归并的身份
（:func:`consolidation_identity`：读书身份 + 归并模板版本 + 模型）也没变就连归并一起沿用，只重写文风卡
（:func:`find_reusable_reading`）。记在血缘 run 行的 ``coverage_json["full"]`` 里。
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceEvidence,
    StyleReferenceExtraction,
    StyleReferenceFinding,
    StyleReferenceRun,
)
from novel_system.services.hash_engine import sha256_json_plain
from novel_system.services.style_reference.banned_adjective import check_banned_adjectives
from novel_system.services.style_reference.card import DIMENSION_LABELS
from novel_system.services.style_reference.dimensions import LAYER_TO_SUB_DIMS
from novel_system.services.style_reference.learn_extract import (
    MAX_AVOID,
    MAX_EVIDENCE,
    MAX_OBSERVATIONS,
    MODEL_DEFAULT_MAX_CHARS,
    STATEMENT_MAX_CHARS,
    STATEMENT_MIN_CHARS,
    clean_devices,
    score01,
)
from novel_system.services.style_reference.learn_llm import CONSOLIDATE_TEMPLATE
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import AnchorKind, ExtractionPurpose, FindingKind
from novel_system.services.style_reference.text_utils import compact_ws
from novel_system.services.style_reference.validation.plagiarism import normalize_text_for_matching

STATUS_CONSOLIDATED = "consolidated"
KIND_OBSERVATION = "observation"
KIND_AVOID = "avoid"
_KIND_OF = {KIND_OBSERVATION: FindingKind.OBSERVATION.value, KIND_AVOID: FindingKind.FORBIDDEN_PATTERN.value}
# 给归并模型看的各批「模型默认写法」最多几句（同义的去重后）
MAX_MODEL_DEFAULTS = 12
MAX_DEVICE_NAMES = 12
# 一维的归并结果只有不到这个比例的发现进了组 → 重问一次
MIN_ASSIGNED_SHARE = 0.3

_LAYER_OF = {dim.value: layer.value for layer, dims in LAYER_TO_SUB_DIMS.items() for dim in dims}


# ---------------------------------------------------------------------------
# 读回各批的发现
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawFinding:
    ref: str
    finding_id: str
    dimension: str
    kind: str  # FindingKind.value
    statement: str
    confidence: str
    batch: int
    distinctiveness: float | None
    quote_ids: tuple[str, ...]


@dataclass
class BatchMeta:
    model_defaults: list[str] = field(default_factory=list)
    devices: Counter = field(default_factory=Counter)
    distinctiveness: list[float] = field(default_factory=list)


def load_raw_findings(session: Session, run_id: str) -> tuple[dict[str, list[RawFinding]], dict[str, BatchMeta]]:
    """这次通读各批的发现（按维度；每维编号 r1…rN，按批、落库顺序）与各批的维度元数据。归并后的发现不算。"""
    extractions = list(
        session.scalars(
            select(StyleReferenceExtraction)
            .where(
                StyleReferenceExtraction.run_id == run_id,
                or_(
                    StyleReferenceExtraction.purpose.is_(None),
                    StyleReferenceExtraction.purpose != ExtractionPurpose.CONSOLIDATE.value,
                ),
            )
            .order_by(StyleReferenceExtraction.created_at, StyleReferenceExtraction.extraction_id)
        )
    )
    batch_of: dict[str, int] = {}
    finding_meta: dict[str, Mapping[str, Any]] = {}
    metas: dict[str, BatchMeta] = defaultdict(BatchMeta)
    for row in extractions:
        payload = row.raw_payload_json if isinstance(row.raw_payload_json, Mapping) else {}
        batch_of[str(row.extraction_id)] = int(payload.get("batch") or 0)
        meta = metas[str(row.sub_dimension)]
        default = compact_ws(payload.get("model_default"))
        if default:
            meta.model_defaults.append(default)
        meta.devices.update(clean_devices(payload.get("devices")))
        distinct = payload.get("distinctiveness")
        if isinstance(distinct, (int, float)):
            meta.distinctiveness.append(float(distinct))
        for finding_id, values in (payload.get("finding_meta") or {}).items():
            if isinstance(values, Mapping):
                finding_meta[str(finding_id)] = values
    findings = list(
        session.scalars(
            select(StyleReferenceFinding)
            .where(
                StyleReferenceFinding.run_id == run_id,
                StyleReferenceFinding.extraction_id.in_(list(batch_of) or [""]),
            )
            .order_by(StyleReferenceFinding.created_at, StyleReferenceFinding.finding_id)
        )
    )
    quotes: dict[str, list[str]] = defaultdict(list)
    for finding_id, quote_id in session.execute(
        select(StyleReferenceEvidence.finding_id, StyleReferenceEvidence.quote_id)
        .where(StyleReferenceEvidence.finding_id.in_([f.finding_id for f in findings] or [""]))
        .order_by(StyleReferenceEvidence.created_at, StyleReferenceEvidence.evidence_id)
    ).all():
        quotes[str(finding_id)].append(str(quote_id))
    by_dim: dict[str, list[RawFinding]] = defaultdict(list)
    ordered = sorted(findings, key=lambda f: (batch_of.get(str(f.extraction_id), 0), str(f.created_at), str(f.finding_id)))
    for finding in ordered:
        dim = str(finding.sub_dimension)
        items = by_dim[dim]
        distinct = (finding_meta.get(str(finding.finding_id)) or {}).get("distinctiveness")
        items.append(
            RawFinding(
                ref=f"r{len(items) + 1}",
                finding_id=str(finding.finding_id),
                dimension=dim,
                kind=str(finding.finding_kind),
                statement=str(finding.statement or ""),
                confidence=str(finding.confidence or "medium"),
                batch=batch_of.get(str(finding.extraction_id), 0),
                distinctiveness=float(distinct) if isinstance(distinct, (int, float)) else None,
                quote_ids=tuple(quotes.get(str(finding.finding_id), [])),
            )
        )
    return dict(by_dim), dict(metas)


# ---------------------------------------------------------------------------
# 载荷
# ---------------------------------------------------------------------------


def _wire_kind(kind: str) -> str:
    return KIND_OBSERVATION if kind == FindingKind.OBSERVATION.value else KIND_AVOID


def consolidate_payload(
    dimension: str,
    findings: Sequence[RawFinding],
    meta: BatchMeta | None,
    *,
    book_title: str,
    batches: int,
) -> dict[str, Any]:
    meta = meta or BatchMeta()
    defaults = list(dict.fromkeys(meta.model_defaults))[:MAX_MODEL_DEFAULTS]
    return {
        "book_title": book_title,
        "dimension": {"key": dimension, "label": DIMENSION_LABELS.get(dimension, dimension)},
        "batches": int(batches),
        "model_defaults": defaults,
        "devices": [name for name, _count in meta.devices.most_common(MAX_DEVICE_NAMES)],
        "findings": [
            {"ref": f.ref, "kind": _wire_kind(f.kind), "statement": f.statement, "batch": f.batch + 1}
            for f in findings
        ],
    }


def fit_consolidate_payload(
    build: Callable[[Sequence[RawFinding]], dict[str, Any]],
    findings: Sequence[RawFinding],
    fits: Callable[[dict[str, Any]], bool],
) -> tuple[dict[str, Any], list[RawFinding], int]:
    """装进节点预算：先全给，再去掉低置信的、再去掉中置信的（高置信的总保留）。返回 (载荷, 实际送去的发现, 去掉的条数)。"""
    ladders = (
        lambda f: True,
        lambda f: f.confidence != "low",
        lambda f: f.confidence == "high",
    )
    sent: list[RawFinding] = list(findings)
    payload = build(sent)
    for keep in ladders:
        sent = [f for f in findings if keep(f)]
        payload = build(sent)
        if fits(payload):
            break
    return payload, sent, len(findings) - len(sent)


# ---------------------------------------------------------------------------
# 核对
# ---------------------------------------------------------------------------


@dataclass
class Group:
    kind: str  # FindingKind.value
    statement: str
    members: list[RawFinding]
    distinctiveness: float | None = None

    @property
    def batches(self) -> list[int]:
        return sorted({m.batch for m in self.members})

    @property
    def support(self) -> int:
        return len(self.batches)


@dataclass
class Consolidation:
    dimension: str
    model_default: str = ""
    devices: list[str] = field(default_factory=list)
    distinctiveness: float | None = None
    groups: list[Group] = field(default_factory=list)
    assigned: int = 0
    sent: int = 0
    rejected: int = 0
    malformed: bool = False
    problems: list[str] = field(default_factory=list)


def parse_consolidation(structured: Any, dimension: str, sent: Sequence[RawFinding]) -> Consolidation:
    """核对一维的归并输出（见模块文档）。不抛异常：问题记在 ``problems``，``malformed`` 表示整体不成形。"""
    result = Consolidation(dimension=dimension, sent=len(sent))
    groups_raw = structured.get("groups") if isinstance(structured, Mapping) else None
    if not isinstance(groups_raw, list):
        result.malformed = True
        result.problems.append("输出里没有 groups 数组")
        return result
    result.model_default = compact_ws(structured.get("model_default"))[:MODEL_DEFAULT_MAX_CHARS]
    result.devices = clean_devices(structured.get("devices"))
    result.distinctiveness = score01(structured.get("distinctiveness"))
    by_ref = {f.ref: f for f in sent}
    used: set[str] = set()
    seen_statements: set[tuple[str, str]] = set()
    for raw in groups_raw:
        if not isinstance(raw, Mapping):
            continue
        kind = _KIND_OF.get(str(raw.get("kind") or "").strip().lower())
        statement = compact_ws(raw.get("statement"))
        if kind is None:
            result.rejected += 1
            continue
        if (
            len(statement) < STATEMENT_MIN_CHARS
            or len(statement) > STATEMENT_MAX_CHARS
            or check_banned_adjectives(statement)
        ):
            result.rejected += 1
            continue
        members: list[RawFinding] = []
        for ref in raw.get("refs") if isinstance(raw.get("refs"), list) else []:
            key = str(ref or "").strip().lower()
            if key.isdigit():
                key = f"r{int(key)}"
            finding = by_ref.get(key)
            if finding is None or key in used or finding.kind != kind:
                continue
            used.add(key)
            members.append(finding)
        if not members:
            result.rejected += 1
            continue
        statement_key = (kind, normalize_text_for_matching(statement))
        if statement_key in seen_statements:
            result.rejected += 1
            continue
        seen_statements.add(statement_key)
        result.groups.append(
            Group(kind=kind, statement=statement, members=members, distinctiveness=score01(raw.get("distinctiveness")))
        )
    result.assigned = len(used)
    if not any(g.kind == FindingKind.OBSERVATION.value for g in result.groups) and any(
        f.kind == FindingKind.OBSERVATION.value for f in sent
    ):
        result.problems.append("没有一组观察通过核对（组里的编号必须是输入里同一类的发现）")
    if sent and result.assigned < MIN_ASSIGNED_SHARE * len(sent):
        result.problems.append(f"只有 {result.assigned} 条发现进了组（共 {len(sent)} 条）：说同一件事的发现都要归进组里")
    return result


def needs_retry(result: Consolidation) -> bool:
    return result.malformed or bool(result.problems)


def retry_instruction(result: Consolidation) -> str:
    lines = [
        "【重试说明】上一次的归并有下面这些问题，请重新完整输出：",
        *(f"- {problem}" for problem in result.problems[:6]),
        "提醒：refs 只能写输入里的编号（如 r3），observation 组只收 observation，avoid 组只收 avoid，一条发现只进一组。",
    ]
    return "\n".join(lines)


def better(a: Consolidation | None, b: Consolidation) -> Consolidation:
    """两次尝试留更好的一次（不成形的最差；再比进组的发现数）。"""
    if a is None:
        return b
    return b if (not b.malformed, b.assigned, len(b.groups)) > (not a.malformed, a.assigned, len(a.groups)) else a


def rank_groups(groups: Sequence[Group]) -> list[Group]:
    """按支持（几个批里见到）→ 辨识度 → 成员数排，每维留观察 ≤ ``MAX_OBSERVATIONS``、避免 ≤ ``MAX_AVOID``。"""
    ordered = sorted(
        groups,
        key=lambda g: (-g.support, -(g.distinctiveness if g.distinctiveness is not None else 0.5), -len(g.members)),
    )
    kept: list[Group] = []
    counts: Counter = Counter()
    for group in ordered:
        limit = MAX_OBSERVATIONS if group.kind == FindingKind.OBSERVATION.value else MAX_AVOID
        if counts[group.kind] >= limit:
            continue
        counts[group.kind] += 1
        kept.append(group)
    return kept


def support_confidence(support: int, batches: int) -> str:
    """支持数 → 置信度（至少三成的批里见到 = high，一成 = medium）。"""
    share = support / batches if batches > 0 else 0.0
    if share >= 0.3:
        return "high"
    if share >= 0.1:
        return "medium"
    return "low"


def _spread_quotes(members: Sequence[RawFinding], limit: int = MAX_EVIDENCE) -> list[str]:
    """从成员的引文里挑 ≤``limit`` 条，先每批一条（按批序轮流），尽量覆盖不同的批。"""
    by_batch: dict[int, list[str]] = defaultdict(list)
    for member in members:
        by_batch[member.batch].extend(member.quote_ids)
    picked: list[str] = []
    depth = 0
    while len(picked) < limit:
        added = False
        for batch in sorted(by_batch):
            quotes = by_batch[batch]
            if depth < len(quotes) and quotes[depth] not in picked:
                picked.append(quotes[depth])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        depth += 1
    return picked


def persist_consolidated(
    session: Session,
    *,
    book_id: str,
    run_id: str,
    result: Consolidation,
    batches: int,
    attempts: int,
    llm_call_id: str | None,
    dropped_low_confidence: int = 0,
) -> dict[str, int]:
    """一维的归并结果落库（调用方的事务；只 flush）。返回 ``{"observations", "avoid", "evidence"}`` 计数。"""
    repo = StyleReferenceRepository(session)
    dim = result.dimension
    kept = rank_groups(result.groups)
    extraction_id = f"sr_ext_{uuid.uuid4().hex[:12]}"
    extraction = repo.create_extraction(
        extraction_id=extraction_id,
        book_id=book_id,
        run_id=run_id,
        layer=_LAYER_OF.get(dim, dim.split(".", 1)[0]),
        sub_dimension=dim,
        llm_call_id=llm_call_id,
        raw_payload_json={},
        status="done",
        validation_errors_json=[],
        purpose=ExtractionPurpose.CONSOLIDATE.value,
    )
    finding_meta: dict[str, dict[str, Any]] = {}
    counts = {"observations": 0, "avoid": 0, "evidence": 0}
    for group in kept:
        finding_id = f"sr_find_{uuid.uuid4().hex[:12]}"
        repo.create_finding(
            finding_id=finding_id,
            book_id=book_id,
            run_id=run_id,
            extraction_id=extraction_id,
            sub_dimension=dim,
            finding_kind=group.kind,
            statement=group.statement,
            confidence=support_confidence(group.support, batches),
            status=STATUS_CONSOLIDATED,
        )
        meta: dict[str, Any] = {
            "support": group.support,
            "batches": group.batches,
            "members": [m.finding_id for m in group.members],
        }
        if group.distinctiveness is not None:
            meta["distinctiveness"] = round(group.distinctiveness, 3)
        finding_meta[finding_id] = meta
        for quote_id in _spread_quotes(group.members):
            repo.create_evidence(
                evidence_id=f"sr_ev_{uuid.uuid4().hex[:12]}",
                finding_id=finding_id,
                quote_id=quote_id,
                anchor_kind=AnchorKind.PARAGRAPH_QUOTE.value,
            )
            counts["evidence"] += 1
        counts["observations" if group.kind == FindingKind.OBSERVATION.value else "avoid"] += 1
    extraction.raw_payload_json = {
        "model_default": result.model_default,
        "devices": list(result.devices),
        "distinctiveness": result.distinctiveness,
        "findings_count": len(kept),
        "attempts": int(attempts),
        "finding_meta": finding_meta,
        "batches": int(batches),
        "sent": result.sent,
        "assigned": result.assigned,
        "groups": len(result.groups),
        "dropped_low_confidence": int(dropped_low_confidence),
    }
    session.flush()
    return counts


# ---------------------------------------------------------------------------
# 沿用上一次通读
# ---------------------------------------------------------------------------


def reading_identity(
    *,
    root: str,
    index_version: str,
    plan_version: str,
    max_chars: int,
    plan: Sequence[Mapping[str, Any]],
    layers: Sequence[str],
    prompts: Mapping[str, str],
    models: Mapping[str, Any],
) -> dict[str, Any]:
    """一次通读读出来的东西由什么决定（任何一项变了就得重读）。"""
    return {
        "root": str(root or ""),
        "index_version": str(index_version or ""),
        "plan_version": str(plan_version),
        "max_chars": int(max_chars),
        "plan": sha256_json_plain([[(s["start"], s["end"]) for s in b.get("segments") or []] for b in plan])[:16],
        "layers": sorted(str(layer) for layer in layers),
        "prompts": {str(k): str(v) for k, v in sorted(prompts.items())},
        "models": {str(k): list(v) if isinstance(v, (list, tuple)) else v for k, v in sorted(models.items())},
    }


def identity_hash(identity: Mapping[str, Any]) -> str:
    return sha256_json_plain(dict(identity))[:24]


# 估算（GET 学习信息）不切批：比身份里除切法之外的部分，预告「上一次通读多半能沿用」；作业开工时按完整身份再判
_SIGNATURE_SKIP = ("plan", "max_chars")


def reading_signature(identity: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in identity.items() if key not in _SIGNATURE_SKIP}


def consolidation_identity(reading_hash: str, *, prompt_version: str, model: Any) -> dict[str, Any]:
    return {"reading": reading_hash, "prompt": str(prompt_version), "model": list(model) if isinstance(model, (list, tuple)) else model}


@dataclass(frozen=True)
class Reusable:
    read_run_id: str | None = None
    consolidated_run_id: str | None = None
    read_at: str | None = None


def find_reusable_reading(
    session: Session,
    book_id: str,
    *,
    reading_hash: str | None = None,
    consolidation_hash: str | None = None,
    signature: Mapping[str, Any] | None = None,
    excluding: str | None = None,
) -> Reusable:
    """最近一次读完了的、身份相同的通读（``coverage_json["full"]`` 里 ``read_complete``）；归并身份也相同且归并做完了
    的，连归并一起沿用。没有 → 空的 :class:`Reusable`。只给 ``signature``（估算）时按 :func:`reading_signature` 比。"""
    rows = session.execute(
        select(StyleReferenceRun.run_id, StyleReferenceRun.coverage_json, StyleReferenceRun.created_at)
        .where(StyleReferenceRun.book_id == str(book_id))
        .order_by(StyleReferenceRun.created_at.desc())
    ).all()
    for run_id, coverage, _created in rows:
        if excluding and str(run_id) == str(excluding):
            continue
        full = (coverage or {}).get("full") if isinstance(coverage, Mapping) else None
        if not isinstance(full, Mapping) or not full.get("read_complete"):
            continue
        if reading_hash is not None and full.get("reading_hash") != reading_hash:
            continue
        if signature is not None and (
            not isinstance(full.get("identity"), Mapping) or reading_signature(full["identity"]) != dict(signature)
        ):
            continue
        consolidated = None
        if consolidation_hash and full.get("consolidated") and full.get("consolidation_hash") == consolidation_hash:
            consolidated = str(full.get("consolidated_run_id") or run_id)
        return Reusable(
            read_run_id=str(full.get("read_run_id") or run_id),
            consolidated_run_id=consolidated,
            read_at=str(full.get("read_at") or "") or None,
        )
    return Reusable()


__all__ = [
    "CONSOLIDATE_TEMPLATE",
    "STATUS_CONSOLIDATED",
    "BatchMeta",
    "Consolidation",
    "Group",
    "RawFinding",
    "Reusable",
    "better",
    "consolidate_payload",
    "consolidation_identity",
    "find_reusable_reading",
    "fit_consolidate_payload",
    "identity_hash",
    "load_raw_findings",
    "needs_retry",
    "parse_consolidation",
    "persist_consolidated",
    "rank_groups",
    "reading_identity",
    "reading_signature",
    "retry_instruction",
    "support_confidence",
]
