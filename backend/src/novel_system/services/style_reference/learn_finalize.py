"""「学习文风」作业的最后一步：写入画像（从 ``learn_job`` 拆出；作业的调度与事务仍在 ``learn_run._LearnRun``）。

- :func:`filter_learned_card`：文风卡按本书专名、这份画像的作者禁用词与原文重合（≥12 字，``BookNgramIndex``）过滤；
  滤空了就失败（``card_filtered_empty``，不可续跑）——重新学习是就地更新，一张空卡会把作者正在用的画像冲掉；
- :func:`planning_lines`：合成没给规划层手法时从场景 / 主题层的观察里挑（同样过专名与原文重合）；
- :func:`write_learned_profile`：一个事务里（调用方已先条件写作业行拿到写锁）重读画像、沿用作者的 ✓ / ✗、拼 v3 画像
  （:func:`assemble_profile_json`，纯函数）、建或就地更新画像行、替换受保护专名行、把血缘 run 行标 done；
- :func:`learn_result_summary`：作业结果（计数 / 调用 / token / 耗时，纯函数）。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceBannedTerm,
    StyleReferenceBook,
    StyleReferenceProfile,
    StyleReferenceRun,
    utcnow,
)
from novel_system.services.style_reference.card import (
    PROFILE_VERSION_V3,
    DimensionCard,
    card_from_profile_json,
    line_states_from_profile_json,
)
from novel_system.services.style_reference.fidelity import DIMENSION_FEATURES
from novel_system.services.style_reference.learn_card import (
    CardAssembly,
    carry_line_states,
    derive_narrative_guidance,
    filter_card,
)
from novel_system.services.style_reference.learn_job import REASON_CARD_FILTERED_EMPTY, LearnFailedError
from novel_system.services.style_reference.profile_fields import REFERENCE_BASIS_VERSION
from novel_system.services.style_reference.protected_terms import (
    DISMISSED_KEY,
    PROTECTED_SOURCE,
    ProtectedTerm,
    dismissed_protected_terms,
    protected_terms_version,
    replace_protected_terms,
)
from novel_system.services.style_reference.schemas import FindingKind
from novel_system.services.style_reference.structure_render import derive_planning_guidance
from novel_system.services.style_reference.validation.plagiarism import BookNgramIndex

# 文风卡的一句与原书连续重合到这个字数就丢（允许 ≤11 字的作者原话作例子）
CARD_OVERLAP_CHARS = 12


@dataclass(frozen=True)
class FilteredCard:
    """过滤后的文风卡与过滤用到的东西（规划陈述要过同一道原文重合 + 专名）。"""

    assembly: CardAssembly
    filtered: Mapping[str, Any]
    protected: list[ProtectedTerm]
    author_terms: list[str]
    overlaps: Callable[[str], bool]


@dataclass(frozen=True)
class FinalizeInputs:
    """写入画像之前汇齐的输入（作业线程在事务之外算好：读库、测量、账本）。"""

    run_id: str
    target_id: str | None
    book: StyleReferenceBook
    book_title: str
    card: FilteredCard
    dismissed: set[str]
    voice: Mapping[str, Any]
    structure: Mapping[str, Any] | None
    distribution: Mapping[str, Any] | None
    findings: Sequence[Any]
    planning: Sequence[str]
    sub_dimensions: Mapping[str, Mapping[str, Any]]
    paragraph_count: int
    learned_from: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WrittenProfile:
    """写库的结果（结果摘要要用）。"""

    profile: StyleReferenceProfile
    created: bool
    card: DimensionCard | None
    carried: Any
    protected_rows: Mapping[str, Any]
    coverage: Mapping[str, Any]


def filter_learned_card(
    session: Session,
    cursor: Mapping[str, Any],
    *,
    texts: Sequence[str],
    target_id: str | None,
    dismissed: set[str],
) -> FilteredCard:
    """游标里的文风卡按本书专名（作者删过的除外）、这份画像的作者禁用词（任何域）与原文重合过滤；滤空了就失败。"""
    overlap = BookNgramIndex(texts, threshold_chars=CARD_OVERLAP_CHARS)
    protected = [
        ProtectedTerm(term=str(t["term"]), kind=str(t.get("kind") or "term"))
        for t in (cursor.get("protected") or {}).get("terms") or []
        if str(t.get("term") or "").strip() not in dismissed
    ]
    # 作者给这份画像录入的禁用词(任何域)同样不能进卡片:画像已存在时一并过滤
    author_terms = [
        str(term)
        for term in session.scalars(
            select(StyleReferenceBannedTerm.term).where(
                StyleReferenceBannedTerm.profile_id == target_id,
                StyleReferenceBannedTerm.source != PROTECTED_SOURCE,
            )
        )
        if str(term or "").strip()
    ] if target_id else []
    assembly, filtered = filter_card(
        CardAssembly.from_cursor(cursor.get("card") or {}),
        protected=[t.term for t in protected] + author_terms,
        overlaps=overlap.overlaps,
    )
    if assembly.card is None or not assembly.card.all_lines():
        # 重新学习是就地更新:一张空卡会把作者正在用的画像冲掉——宁可失败,画像原样不动
        raise LearnFailedError(
            REASON_CARD_FILTERED_EMPTY,
            "文风卡的句子全被过滤掉了(含本书专名、这份画像的禁用词,或与原文大段重合);画像没有改动。"
            "检查这份画像的禁用词后重新「学习文风」。",
            retryable=False,
            details={"filtered": filtered, "author_terms": len(author_terms), "protected_terms": len(protected)},
            author_action={"action": "review_banned_terms", "view": "styleref", "label": "检查这份画像的禁用词"},
        )
    return FilteredCard(
        assembly=assembly,
        filtered=filtered,
        protected=protected,
        author_terms=author_terms,
        overlaps=overlap.overlaps,
    )


def planning_lines(card: FilteredCard, findings: Sequence[Any]) -> list[str]:
    """规划层手法：合成给了就用；没给就从场景 / 主题层的观察里挑（同样过专名与原文重合）。"""
    planning = list(card.assembly.planning_guidance)
    if planning:
        return planning
    return derive_planning_guidance(
        [
            {
                "finding_kind": f.kind,
                "sub_dimension": f.dimension,
                "statement": f.statement,
                "confidence": f.confidence,
                "status": "pending",
            }
            for f in findings
        ],
        overlap_filter=lambda text: card.overlaps(text) or any(t.term in text for t in card.protected),
    )


_VERSION_RE = re.compile(r"(\d+)$")


def bump_version(tag: str | None) -> str:
    match = _VERSION_RE.search(str(tag or ""))
    return f"v{int(match.group(1)) + 1}" if match else "v2"


def assemble_profile_json(
    *,
    book: StyleReferenceBook,
    card: DimensionCard | None,
    states: Mapping[str, str],
    voice: Mapping[str, Any],
    distribution: Mapping[str, Any] | None,
    structure: Mapping[str, Any] | None,
    planning: Sequence[str],
    narrative: Sequence[str],
    qualitative_summary: str,
    learned_from: Mapping[str, Any],
    protected: Sequence[ProtectedTerm],
    paragraph_count: int,
) -> dict[str, Any]:
    """v3 画像（契约 §2.2）。``voice`` = 测量核声音特征 + 具体习惯句 + 作者自身的参照分布；另写一份不带分布的
    ``voice_signature`` 过渡别名。不写 ``exemplar_windows``（窗口在窗口表）、``scene_samples_index`` 与旧的
    ``style_features`` / ``narrative_patterns`` / ``banned_replication_rules`` / ``calibration_guidance``；
    2026-09-24（S2）起也不写 v2 的 ``metrics_baseline``（指标包络已删）与 ``sub_dimensions``（各维证据计数只留在
    ``coverage_json`` / run 血缘里）。"""
    card_json = card.model_dump(mode="json") if card is not None else None
    if card_json is not None:
        for entry in card_json.get("dimensions") or []:
            entry["measurable_features"] = list(DIMENSION_FEATURES.get(entry.get("dimension"), ()))
    voice_block = {**dict(voice)}
    if distribution is not None:
        voice_block["distribution"] = dict(distribution)
    return {
        "profile_version": PROFILE_VERSION_V3,
        "dimension_card": card_json,
        "card_line_states": dict(states),
        "voice": voice_block,
        # 别名(同一次写入、内容同源,不带 distribution)。还在读它的:运行时契约的冻结白名单
        # (runtime_contract.FROZEN_PROFILE_JSON_KEYS——契约里冻结的是这个键)、style_continuity 的契约声音参照与
        # 刻意复沓判定(新鲜度预算)、scene_diagnosis 的刻意复沓校准;渲染器 / 摘要先读 ``voice`` 再退回它。
        # 这些读者都改读 ``voice`` 之前不能删。
        "voice_signature": dict(voice),
        "structure_card": dict(structure) if structure else None,
        "planning_guidance": list(planning),
        "narrative_guidance": list(narrative),
        "qualitative_summary": qualitative_summary,
        "reference_basis": {
            "version": REFERENCE_BASIS_VERSION,
            "mode": "reference_derived",
            "scope": "work_or_collection",
            "fixed_author_allowlist": False,
            "book_id": str(book.book_id),
            "source_kind": str(book.source_kind),
            "text_checksum": str(book.text_checksum),
            "source_char_count": int(book.total_chars or 0),
            "paragraph_count": int(paragraph_count),
        },
        "learned_from": dict(learned_from),
        "protected_terms_version": protected_terms_version(protected),
    }


def write_learned_profile(session: Session, inputs: FinalizeInputs, *, job_id: str) -> WrittenProfile:
    """写画像（调用方的事务里，已先条件写作业行拿到写锁）：重读画像合并行状态 → 建或就地更新画像 → 受保护专名行 →
    血缘 run 行标 done。flush 不 commit。"""
    target_id = inputs.target_id
    profile = (
        session.execute(
            select(StyleReferenceProfile)
            .where(StyleReferenceProfile.profile_id == target_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if target_id
        else None
    )
    previous_json = dict(profile.profile_json or {}) if profile is not None else {}
    assembly = inputs.card.assembly
    card, states, carried = carry_line_states(
        assembly.card,
        card_from_profile_json(previous_json),
        line_states_from_profile_json(previous_json),
    )
    if card is not None:
        card = card.model_copy(update={"generated_at": utcnow()})
    dismissed = set(inputs.dismissed) | dismissed_protected_terms(previous_json)
    protected = inputs.card.protected
    profile_json = assemble_profile_json(
        book=inputs.book,
        card=card,
        states=states,
        voice=inputs.voice,
        distribution=inputs.distribution,
        structure=inputs.structure,
        planning=inputs.planning,
        narrative=derive_narrative_guidance(card),
        qualitative_summary=assembly.qualitative_summary,
        learned_from=inputs.learned_from,
        protected=protected,
        paragraph_count=inputs.paragraph_count,
    )
    if dismissed:
        profile_json[DISMISSED_KEY] = sorted(dismissed)
    card_lines = card.all_lines() if card is not None else []
    sub_dimensions = inputs.sub_dimensions
    findings = inputs.findings
    coverage = {
        "learn_job_id": job_id,
        "sub_dim_count": len(sub_dimensions),
        "findings_count": len(findings),
        "quotes_count": sum(int(v.get("quote_count") or 0) for v in sub_dimensions.values()),
        "card_lines": len(card_lines),
    }
    title = assembly.profile_title or f"{inputs.book_title}的文风"
    created = profile is None
    if profile is None:
        profile = StyleReferenceProfile(
            profile_id=f"sr_profile_{uuid.uuid4().hex[:12]}",
            book_id=str(inputs.book.book_id),
            run_id=inputs.run_id,
            title=title,
            status="active",
            profile_json=profile_json,
            coverage_json=coverage,
            source_finding_ids_json=[f.finding_id for f in findings],
            version_tag="v1",
        )
        session.add(profile)
    else:
        profile.run_id = inputs.run_id
        profile.title = title
        profile.status = "active"
        profile.profile_json = profile_json
        profile.coverage_json = coverage
        profile.source_finding_ids_json = [f.finding_id for f in findings]
        profile.version_tag = bump_version(profile.version_tag)
    session.flush()
    protected_rows = replace_protected_terms(session, profile.profile_id, protected, dismissed=dismissed)
    run = session.get(StyleReferenceRun, inputs.run_id)
    if run is not None:
        run.status = "done"
        run.phase = "done"
        run.finished_at = utcnow()
        run.heartbeat_at = utcnow()
        run.coverage_json = {
            **dict(run.coverage_json or {}),
            "sub_dimensions": {
                dim: {
                    "findings": int(v["observation_count"]) + int(v["forbidden_pattern_count"]),
                    "quotes": int(v["quote_count"]),
                }
                for dim, v in sub_dimensions.items()
            },
        }
    return WrittenProfile(
        profile=profile,
        created=created,
        card=card,
        carried=carried,
        protected_rows=protected_rows,
        coverage=coverage,
    )


def learn_result_summary(
    inputs: FinalizeInputs,
    written: WrittenProfile,
    *,
    cursor: Mapping[str, Any],
    ledger: Mapping[str, Any],
) -> dict[str, Any]:
    """作业结果（``result_json``）：画像 / 窗口 / 发现 / 卡 / 专名 / 调用与 token / 耗时。"""
    index_state = dict(cursor.get("index") or {})
    selection = dict(cursor.get("selection") or {})
    timings = dict(cursor.get("timings") or {})
    layers_done = dict(cursor.get("layers_done") or {})
    tags_state = dict(cursor.get("tags") or {})
    findings = inputs.findings
    card = written.card
    card_lines = card.all_lines() if card is not None else []
    profile = written.profile
    return {
        "profile_id": profile.profile_id,
        "profile_created": written.created,
        "version_tag": profile.version_tag,
        "run_id": inputs.run_id,
        "book_id": str(inputs.book.book_id),
        "windows": {
            "index": int(index_state.get("window_count") or 0),
            "extraction": len(selection.get("windows") or []),
            "extraction_chars": int(selection.get("chars") or 0),
            "tagged": int(tags_state.get("tagged") or 0),
            "tag_batches": len(tags_state.get("batches") or []),
        },
        "findings": {
            "total": len(findings),
            "observations": sum(1 for f in findings if f.kind == FindingKind.OBSERVATION.value),
            "avoid": sum(1 for f in findings if f.kind == FindingKind.FORBIDDEN_PATTERN.value),
            "quotes": written.coverage["quotes_count"],
            "by_layer": {
                layer: {key: state.get(key) for key in ("attempts", "returned", "rejected")}
                for layer, state in layers_done.items()
            },
        },
        "card": {
            "lines": len(card_lines),
            "do": sum(1 for _d, line in card_lines if line.kind == "do"),
            "avoid": sum(1 for _d, line in card_lines if line.kind == "avoid"),
            "mandatory": sum(1 for _d, line in card_lines if line.mandatory),
            "temperament": len(card.temperament) if card is not None else 0,
            "carried_pins": written.carried,
            "dropped": dict(inputs.card.assembly.dropped),
            "filtered_at_finalize": inputs.card.filtered,
        },
        "protected_terms": {"count": len(inputs.card.protected), **written.protected_rows},
        "llm": {**dict(ledger), "retries": int(cursor.get("retries") or 0)},
        "seconds": {"by_phase": timings, "total": round(sum(timings.values()), 2)},
    }


__all__ = [
    "CARD_OVERLAP_CHARS",
    "FilteredCard",
    "FinalizeInputs",
    "WrittenProfile",
    "assemble_profile_json",
    "bump_version",
    "filter_learned_card",
    "learn_result_summary",
    "planning_lines",
    "write_learned_profile",
]
