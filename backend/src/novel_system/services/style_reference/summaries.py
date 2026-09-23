"""风格参考 v3 — 书库与画像的读模型(台账 U10 / E10 / N9):书库列表的摘要、画像摘要、文风画像页的详情。

- :func:`book_summaries`:``GET /books`` 每本书一条——状态、段落类型的来源与一致率、最近一次分类 / 学习作业、
  这本书的画像摘要(``needs_relearn``:旧版画像、段落类型在学完之后又更新过、或正文变过)、用在了哪些作品上;
  不带 ``stats_json``(详情端点才带);
- :func:`list_profile_summaries` / :func:`profile_summaries`:``GET /profiles`` 的摘要——**不带** ``profile_json``
  (旧画像一份就有几百 KB,列表只要几个字段:画像版本、学在何时、文风卡几句、要不要重新学);
- :func:`profile_detail`:``GET /profiles/{id}`` 的文风画像页——规范化后的 16 维文风卡、每句的 ✓ / ✗ 状态与
  依据(发现 → 证据 → 引文;引文是参考作者自己的原话,只在本机给作者看)、气质、声音习惯、结构摘要、各维计数、
  ``learned_from``。

画像摘要只读列与几个小 JSON 键(``json_extract``),不加载整份 ``profile_json``。全部只读。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StoryProject,
    StyleReferenceBannedTerm,
    StyleReferenceBook,
    StyleReferenceEvidence,
    StyleReferenceFinding,
    StyleReferenceInjectionBinding,
    StyleReferenceJob,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceQuote,
)
from novel_system.services.style_reference.binding_config import (
    ALL_DIMENSIONS,
    normalize_binding_config,
)
from novel_system.services.style_reference.card import (
    DIMENSION_LABELS,
    PROFILE_VERSION_V3,
    card_from_profile_json,
    line_states_from_profile_json,
)
from novel_system.services.style_reference.import_job import (
    classification_payload,
    classification_provenance,
)
from novel_system.services.style_reference.jobs import JOB_KIND_CLASSIFY, JOB_KIND_LEARN
from novel_system.services.style_reference.learn_job import learn_payload
from novel_system.services.style_reference.paragraph_root import ROOT_KEY
from novel_system.services.style_reference.profile_fields import generation_safe_summary
from novel_system.services.style_reference.structure import render_structure_card_parts

RELEARN_LEGACY = "legacy_profile"
RELEARN_TYPES_CHANGED = "types_changed"
RELEARN_TEXT_CHANGED = "text_changed"

TYPES_REVISION_KEY = "paragraph_types_revision"
PROTECTED_SOURCE = "protected_auto"

# 文风画像页每句最多带几条原话(总数另给);发现的陈述最多几条
LINE_EVIDENCE_MAX = 6
LINE_SOURCES_MAX = 3
LEGACY_LINES_MAX = 12
_DIGIT_RE = re.compile(r"[0-9０-９]")


def _json(value: Any) -> Any:
    """``json_extract`` 取对象时返回 JSON 文本;标量原样。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 画像摘要
# ---------------------------------------------------------------------------


def _profile_light_rows(session: Session, *where: Any) -> list[dict[str, Any]]:
    stmt = select(
        StyleReferenceProfile.profile_id,
        StyleReferenceProfile.book_id,
        StyleReferenceProfile.title,
        StyleReferenceProfile.status,
        StyleReferenceProfile.version_tag,
        StyleReferenceProfile.coverage_json,
        StyleReferenceProfile.created_at,
        StyleReferenceProfile.updated_at,
        func.json_extract(StyleReferenceProfile.profile_json, "$.profile_version"),
        func.json_extract(StyleReferenceProfile.profile_json, "$.learned_from"),
    )
    for clause in where:
        stmt = stmt.where(clause)
    stmt = stmt.order_by(StyleReferenceProfile.created_at, StyleReferenceProfile.profile_id)
    rows = []
    for pid, book_id, title, status, version_tag, coverage, created, updated, version, learned in session.execute(stmt):
        learned_from = _json(learned)
        rows.append(
            {
                "profile_id": str(pid),
                "book_id": str(book_id),
                "title": title,
                "status": status,
                "version_tag": version_tag,
                "coverage": dict(coverage or {}) if isinstance(coverage, Mapping) else {},
                "created_at": created,
                "updated_at": updated,
                "profile_version": str(_json(version) or ""),
                "learned_from": dict(learned_from) if isinstance(learned_from, Mapping) else {},
            }
        )
    return rows


def _book_marks(session: Session, book_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """每本书判「要不要重新学」用的两个标记:段落类型版本与段落根哈希(只取这两个键)。"""
    ids = sorted({str(b) for b in book_ids if b})
    if not ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for book_id, revision, root in session.execute(
        select(
            StyleReferenceBook.book_id,
            func.json_extract(StyleReferenceBook.stats_json, f"$.{TYPES_REVISION_KEY}"),
            func.json_extract(StyleReferenceBook.stats_json, f"$.{ROOT_KEY}"),
        ).where(StyleReferenceBook.book_id.in_(ids))
    ):
        out[str(book_id)] = {"types_revision": _int(_json(revision)), "root": str(_json(root) or "") or None}
    return out


def relearn_reason(profile_version: str, learned_from: Mapping[str, Any], book_marks: Mapping[str, Any] | None) -> str | None:
    """这份画像该不该重新学:旧版画像(没有文风卡);学完之后段落类型又更新过;学完之后正文变过。"""
    if str(profile_version or "") != PROFILE_VERSION_V3:
        return RELEARN_LEGACY
    marks = dict(book_marks or {})
    if _int(learned_from.get("types_revision")) != _int(marks.get("types_revision")):
        return RELEARN_TYPES_CHANGED
    learned_root = str(learned_from.get("root") or "")
    book_root = str(marks.get("root") or "")
    if learned_root and book_root and learned_root != book_root:
        return RELEARN_TEXT_CHANGED
    return None


def _summary_of(row: Mapping[str, Any], marks: Mapping[str, Any] | None) -> dict[str, Any]:
    learned = dict(row.get("learned_from") or {})
    coverage = dict(row.get("coverage") or {})
    reason = relearn_reason(str(row.get("profile_version") or ""), learned, marks)
    return {
        "profile_id": row["profile_id"],
        "book_id": row["book_id"],
        "title": row.get("title"),
        "status": row.get("status"),
        "version_tag": row.get("version_tag"),
        "profile_version": row.get("profile_version") or None,
        "learned_at": learned.get("learned_at"),
        "card_lines": _int(coverage.get("card_lines")),
        "findings_count": _int(coverage.get("findings_count")),
        "quotes_count": _int(coverage.get("quotes_count")),
        "needs_relearn": reason is not None,
        "relearn_reason": reason,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def list_profile_summaries(
    session: Session,
    *,
    book_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    clauses = []
    if book_id is not None:
        clauses.append(StyleReferenceProfile.book_id == book_id)
    if status is not None:
        clauses.append(StyleReferenceProfile.status == status)
    rows = _profile_light_rows(session, *clauses)
    marks = _book_marks(session, (row["book_id"] for row in rows))
    return [_summary_of(row, marks.get(row["book_id"])) for row in rows]


def profile_summaries(session: Session, profile_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    ids = sorted({str(p) for p in profile_ids if p})
    if not ids:
        return {}
    rows = _profile_light_rows(session, StyleReferenceProfile.profile_id.in_(ids))
    marks = _book_marks(session, (row["book_id"] for row in rows))
    return {row["profile_id"]: _summary_of(row, marks.get(row["book_id"])) for row in rows}


def choose_book_profile(profiles: Sequence[Mapping[str, Any]], bound_ids: set[str]) -> Mapping[str, Any] | None:
    """一本书「那份」画像:与学习作业就地更新的是同一份——有绑定的 > active 的 > 最近更新的;归档的不算。"""
    candidates = [p for p in profiles if str(p.get("status") or "") != "archived"]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda p: (
            str(p.get("profile_id")) in bound_ids,
            str(p.get("status") or "") == "active",
            str(p.get("updated_at") or ""),
            str(p.get("profile_id")),
        ),
    )


# ---------------------------------------------------------------------------
# 书库摘要
# ---------------------------------------------------------------------------


def _book_base(book: StyleReferenceBook) -> dict[str, Any]:
    stats = book.stats_json if isinstance(book.stats_json, Mapping) else {}
    return {
        "book_id": book.book_id,
        "title": book.title,
        "author_label": book.author_label,
        "source_kind": book.source_kind,
        "source_path": book.source_path,
        "cloud_policy": book.cloud_policy,
        "text_checksum": book.text_checksum,
        "total_chars": book.total_chars,
        "status": book.status,
        "paragraph_count": _int(stats.get("paragraph_count")) or None,
        "paragraph_types_revision": _int(stats.get(TYPES_REVISION_KEY)),
        "classification_provenance": classification_provenance(stats),
        "created_at": book.created_at,
        "updated_at": book.updated_at,
    }


def book_summaries(
    session: Session,
    books: Sequence[StyleReferenceBook],
    *,
    include_stats: bool = False,
) -> list[dict[str, Any]]:
    """书库列表(或一本书的详情,``include_stats=True`` 时带整份 ``stats_json``)。批量查询,每类一条 SQL。"""
    if not books:
        return []
    book_ids = [str(b.book_id) for b in books]
    latest: dict[tuple[str, str], StyleReferenceJob] = {}
    for job in session.scalars(
        select(StyleReferenceJob)
        .where(
            StyleReferenceJob.kind.in_((JOB_KIND_CLASSIFY, JOB_KIND_LEARN)),
            StyleReferenceJob.book_id.in_(book_ids),
        )
        .order_by(StyleReferenceJob.created_at, StyleReferenceJob.job_id)
    ):
        latest[(job.kind, str(job.book_id))] = job  # 升序:最后写入的就是最近一个
    profiles = _profile_light_rows(session, StyleReferenceProfile.book_id.in_(book_ids))
    by_book: dict[str, list[dict[str, Any]]] = {}
    for row in profiles:
        by_book.setdefault(row["book_id"], []).append(row)
    bindings = list(
        session.scalars(
            select(StyleReferenceInjectionBinding)
            .where(
                StyleReferenceInjectionBinding.profile_id.in_([row["profile_id"] for row in profiles] or [""]),
                StyleReferenceInjectionBinding.status == "active",
                StyleReferenceInjectionBinding.task_type == "scene_generation",
            )
            .order_by(StyleReferenceInjectionBinding.created_at, StyleReferenceInjectionBinding.binding_id)
        )
    )
    bound_ids = {str(b.profile_id) for b in bindings}
    project_ids = sorted({str(b.scope_ref_id) for b in bindings if b.scope == "project" and b.scope_ref_id})
    project_titles = {
        str(pid): title
        for pid, title in session.execute(
            select(StoryProject.project_id, StoryProject.title).where(StoryProject.project_id.in_(project_ids or [""]))
        )
    }
    profile_book = {row["profile_id"]: row["book_id"] for row in profiles}
    applied: dict[str, list[dict[str, Any]]] = {}
    for binding in bindings:
        if binding.scope != "project" or not binding.scope_ref_id:
            continue
        book_id = profile_book.get(str(binding.profile_id))
        if not book_id:
            continue
        applied.setdefault(book_id, []).append(
            {
                "project_id": binding.scope_ref_id,
                "project_title": project_titles.get(str(binding.scope_ref_id)),
                "binding_id": binding.binding_id,
                "profile_id": binding.profile_id,
                "config": normalize_binding_config(binding.strategy, binding.config_json or {}),
            }
        )
    out: list[dict[str, Any]] = []
    for book in books:
        book_id = str(book.book_id)
        stats = book.stats_json if isinstance(book.stats_json, Mapping) else {}
        marks = {
            "types_revision": _int(stats.get(TYPES_REVISION_KEY)),
            "root": str(stats.get(ROOT_KEY) or "") or None,
        }
        own = by_book.get(book_id, [])
        chosen = choose_book_profile(own, bound_ids)
        payload = _book_base(book)
        payload.update(
            {
                "classification": classification_payload(latest.get((JOB_KIND_CLASSIFY, book_id))),
                "learn": learn_payload(latest.get((JOB_KIND_LEARN, book_id))),
                "profile": _summary_of(chosen, marks) if chosen is not None else None,
                "profile_count": len(own),
                "applied_projects": applied.get(book_id, []),
            }
        )
        if include_stats:
            payload["stats_json"] = dict(stats)
        out.append(payload)
    return out


# ---------------------------------------------------------------------------
# 文风画像详情
# ---------------------------------------------------------------------------


def _layer_of(dimension: str) -> str:
    return str(dimension).split(".", 1)[0]


def _structure_lines(profile_json: Mapping[str, Any]) -> list[str]:
    block, _samples = render_structure_card_parts(profile_json, include_samples=False)
    lines = []
    for line in block.splitlines():
        text = line.strip()
        if not text or text.startswith("["):
            continue
        lines.append(text[2:] if text.startswith("- ") else text)
    return lines


def _voice(profile_json: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("voice", "voice_signature"):
        block = profile_json.get(key)
        if isinstance(block, Mapping) and isinstance(block.get("habits"), list):
            habits = [" ".join(str(item or "").split()) for item in block.get("habits") or []]
            return {
                "habits": [h for h in habits if h],
                "deliberate_repetition": bool(block.get("deliberate_repetition")),
                "source": key,
            }
    return {"habits": [], "deliberate_repetition": False, "source": None}


def _clean_lines(values: Any, *, limit: int = LEGACY_LINES_MAX) -> list[str]:
    out: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        text = " ".join(str(value or "").split())
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _legacy_view(profile_json: Mapping[str, Any]) -> dict[str, Any]:
    """旧版画像(没有文风卡):起草时的卡替身读的就是这几组句子——含数字的句子起草时整句不带,这里如实标出。"""
    groups = []
    for key, label in (
        ("style_features", "写法特征"),
        ("narrative_patterns", "叙事手法"),
        ("calibration_guidance", "偏离时怎么改"),
        ("banned_replication_rules", "不许照搬的东西"),
    ):
        lines = _clean_lines(profile_json.get(key))
        if lines:
            groups.append(
                {
                    "key": key,
                    "label": label,
                    "lines": [{"text": text, "dropped_in_drafting": bool(_DIGIT_RE.search(text))} for text in lines],
                }
            )
    return {"summary": generation_safe_summary(profile_json), "groups": groups}


def _evidence_index(
    session: Session,
    finding_ids: set[str],
    quote_ids: set[str],
) -> tuple[dict[str, list[str]], dict[str, StyleReferenceQuote], dict[str, int], dict[str, StyleReferenceFinding]]:
    """发现 → 引文 id(按证据行的写入顺序)、引文行、段落序号、发现行——一次查询一类。"""
    by_finding: dict[str, list[str]] = {}
    if finding_ids:
        for finding_id, quote_id in session.execute(
            select(StyleReferenceEvidence.finding_id, StyleReferenceEvidence.quote_id)
            .where(StyleReferenceEvidence.finding_id.in_(sorted(finding_ids)))
            .order_by(StyleReferenceEvidence.created_at, StyleReferenceEvidence.evidence_id)
        ):
            by_finding.setdefault(str(finding_id), []).append(str(quote_id))
    all_quotes = set(quote_ids) | {q for ids in by_finding.values() for q in ids}
    quotes = {
        str(q.quote_id): q
        for q in session.scalars(
            select(StyleReferenceQuote).where(StyleReferenceQuote.quote_id.in_(sorted(all_quotes) or [""]))
        )
    }
    paragraph_ids = sorted({str(q.paragraph_id) for q in quotes.values() if q.paragraph_id})
    indexes = {
        str(pid): int(idx)
        for pid, idx in session.execute(
            select(StyleReferenceParagraph.paragraph_id, StyleReferenceParagraph.paragraph_index).where(
                StyleReferenceParagraph.paragraph_id.in_(paragraph_ids or [""])
            )
        )
    }
    findings = {
        str(f.finding_id): f
        for f in session.scalars(
            select(StyleReferenceFinding).where(StyleReferenceFinding.finding_id.in_(sorted(finding_ids) or [""]))
        )
    }
    return by_finding, quotes, indexes, findings


def profile_detail(session: Session, profile: StyleReferenceProfile) -> dict[str, Any]:
    """文风画像页的全部数据(见模块文档)。旧版画像:``has_card=False``,16 维为空壳,另给 ``legacy``。"""
    profile_json = profile.profile_json if isinstance(profile.profile_json, Mapping) else {}
    card = card_from_profile_json(profile_json)
    states = line_states_from_profile_json(profile_json)
    learned = profile_json.get("learned_from") if isinstance(profile_json.get("learned_from"), Mapping) else {}
    marks = _book_marks(session, [profile.book_id]).get(str(profile.book_id))
    version = str(profile_json.get("profile_version") or "")
    reason = relearn_reason(version, learned, marks)
    sub_dimensions = profile_json.get("sub_dimensions") if isinstance(profile_json.get("sub_dimensions"), Mapping) else {}

    finding_ids: set[str] = set()
    direct_quotes: set[str] = set()
    if card is not None:
        for _dim, line in card.all_lines():
            finding_ids.update(str(f) for f in line.finding_ids if f)
            direct_quotes.update(str(q) for q in line.evidence_quote_ids if q)
    by_finding, quotes, indexes, findings = _evidence_index(session, finding_ids, direct_quotes)

    def _line(dimension: str, line: Any) -> dict[str, Any]:
        quote_order: list[str] = []
        for qid in [*line.evidence_quote_ids, *(q for f in line.finding_ids for q in by_finding.get(str(f), []))]:
            if qid in quotes and qid not in quote_order:
                quote_order.append(qid)
        evidence = []
        for qid in quote_order[:LINE_EVIDENCE_MAX]:
            quote = quotes[qid]
            evidence.append(
                {
                    "quote_id": qid,
                    "text": quote.quote_text,
                    "paragraph_index": indexes.get(str(quote.paragraph_id)) if quote.paragraph_id else None,
                }
            )
        sources = []
        for fid in line.finding_ids[:LINE_SOURCES_MAX]:
            finding = findings.get(str(fid))
            if finding is not None:
                sources.append({"finding_id": finding.finding_id, "statement": finding.statement, "kind": finding.finding_kind})
        return {
            "line_id": line.line_id,
            "text": line.text,
            "kind": line.kind,
            "mandatory": bool(line.mandatory),
            "distinctiveness": float(line.distinctiveness),
            "state": states.get(line.line_id),
            "evidence": evidence,
            "evidence_count": len(quote_order),
            "sources": sources,
        }

    dimensions: list[dict[str, Any]] = []
    entries = {entry.dimension: entry for entry in card.dimensions} if card is not None else {}
    order = [entry.dimension for entry in card.dimensions] if card is not None else list(ALL_DIMENSIONS)
    for dimension in order:
        entry = entries.get(dimension)
        counts = sub_dimensions.get(dimension) if isinstance(sub_dimensions.get(dimension), Mapping) else {}
        lines = [_line(dimension, line) for line in entry.lines] if entry is not None else []
        dimensions.append(
            {
                "dimension": dimension,
                "layer": _layer_of(dimension),
                "label": (entry.label if entry is not None else "") or DIMENSION_LABELS.get(dimension, dimension),
                "summary": entry.summary if entry is not None else "",
                "model_default": entry.model_default if entry is not None else "",
                "distinctiveness": float(entry.distinctiveness) if entry is not None else 0.0,
                "devices": list(entry.devices) if entry is not None else [],
                "measurable_features": list(entry.measurable_features) if entry is not None else [],
                "lines": lines,
                "evidence_count": len({ev["quote_id"] for line in lines for ev in line["evidence"]} | set()),
                "observation_count": _int(counts.get("observation_count")),
                "avoid_count": _int(counts.get("forbidden_pattern_count")),
                "quote_count": _int(counts.get("quote_count")),
            }
        )
    protected_count = _int(
        session.scalar(
            select(func.count(StyleReferenceBannedTerm.term_id)).where(
                StyleReferenceBannedTerm.profile_id == profile.profile_id,
                StyleReferenceBannedTerm.source == PROTECTED_SOURCE,
            )
        )
    )
    learned_view = {
        key: learned.get(key)
        for key in ("learned_at", "types_revision", "index_version", "kernel_version", "card_version", "tags_version", "job_id")
        if key in learned
    }
    extraction = learned.get("extraction") if isinstance(learned.get("extraction"), Mapping) else None
    if extraction is not None:
        learned_view["extraction"] = {
            "windows": len(extraction.get("windows") or []),
            "chars": _int(extraction.get("chars")),
        }
    coverage = profile.coverage_json if isinstance(profile.coverage_json, Mapping) else {}
    return {
        "profile_id": profile.profile_id,
        "book_id": profile.book_id,
        "title": profile.title,
        "status": profile.status,
        "version_tag": profile.version_tag,
        "profile_version": version or None,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
        "has_card": card is not None,
        "needs_relearn": reason is not None,
        "relearn_reason": reason,
        "learned_from": learned_view,
        "temperament": list(card.temperament) if card is not None else [],
        "qualitative_summary": generation_safe_summary(profile_json),
        "dimensions": dimensions,
        "card_line_states": dict(states),
        "voice": _voice(profile_json),
        "structure": {"lines": _structure_lines(profile_json)},
        "planning_guidance": _clean_lines(profile_json.get("planning_guidance")),
        "sub_dimensions": {key: dict(value) for key, value in sub_dimensions.items() if isinstance(value, Mapping)},
        "protected_terms_count": protected_count,
        "coverage": {
            key: coverage.get(key)
            for key in ("card_lines", "findings_count", "quotes_count", "sub_dim_count")
            if key in coverage
        },
        "legacy": _legacy_view(profile_json) if card is None else None,
    }


__all__ = [
    "RELEARN_LEGACY",
    "RELEARN_TEXT_CHANGED",
    "RELEARN_TYPES_CHANGED",
    "book_summaries",
    "choose_book_profile",
    "list_profile_summaries",
    "profile_detail",
    "profile_summaries",
    "relearn_reason",
]
