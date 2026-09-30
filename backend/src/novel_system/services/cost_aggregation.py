"""token / 金额聚合：场景 / 章节 / 全书三级 + 成本看板（结果闭环治理设计 §5.8/§10）。

基于 ``LlmCall``（逻辑调用，父行）+ ``LlmCallAttempt``（物理尝试）+ ``SceneRunState``（场景预算）聚合，
不复制调用日志、不新增列。单价在 ``services/pricing.py``。

以 token 为主（2026-09-30 重构 P06 · 批准#4，审计 B09-17）：各级汇总与看板的构成、排序都按 token；
金额只算价书（``config/pricing.yaml``）里写了单价的模型，其余是「未定价」——``cost`` 为 ``None``，
不再用占位估算价编一个数。每级汇总带 ``pricing``：已定价 / 未定价各多少调用、多少 token、哪些模型没有单价。

口径纪律（§5.8）：
- 跨服务的 token 分列（``tokens_by_provider``：分词器不同，只作参考）；
- 三口径 estimate / provider_actual / budget_charged，父调用只汇总一次；
- 额外成本只算真正白花的：发出去却失败了的物理尝试（重评 R2 第 5 项删了「重复质检」与「补候选」两项，
  见 :func:`_extra_cost`）。

性能（审计 B09-15）：调用与物理尝试各一次查询、只取要用的列；每条调用只折算一次价格；
归档场景 / 章节各一次聚合查询（不再逐章查）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, distinct, func, or_, select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from novel_system.db.models import (
    FinalScene,
    LlmCall,
    LlmCallAttempt,
    SceneCard,
    SceneRunState,
)
from novel_system.services import pricing

_LOGGER = logging.getLogger(__name__)

PHASE_CANDIDATE = "candidate_generation"
PHASE_QC = "quality_check"
PHASE_REVISION = "revision"
PHASE_REVIEW = "review"
PHASE_OTHER = "other"
_PHASES = (PHASE_CANDIDATE, PHASE_QC, PHASE_REVISION, PHASE_REVIEW, PHASE_OTHER)


def classify_phase(node_id: str | None, step: str | None = None) -> str:
    """把一次 LLM 调用归入四阶段之一（§5.8 候选生成/QC/修订/评审）+ other。

    候选生成不限于场景管线：雪花构思（`snowflake_step_generate` /
    `snowflake_step_candidates` / `snowflake_workspace_assistant`）、案头提案
    （`author_proposal_generate`）、大纲（`project_outline_plan`）等生成类节点
    同样计入——否则真实项目的成本大头全落在「其他」，阶段占比失去解释力。
    风格参考子系统 / 汇总压缩类节点仍归 other。
    """
    key = (node_id or step or "").lower()
    if not key:
        return PHASE_OTHER
    # revision 优先于 candidate：`scene_literary_rewrite`/`style_patch` 含 draft 语义但属修订
    if any(t in key for t in ("patch", "rewrite", "revision")):
        return PHASE_REVISION
    if any(
        t in key
        for t in ("qc", "near_final", "quality_contract", "validate")
    ):
        return PHASE_QC
    if any(
        t in key
        for t in ("deep_review", "diagnosis", "adjudicate", "critique", "literary_eval", "triage")
    ):
        return PHASE_REVIEW
    if any(
        t in key
        for t in (
            "draft", "blueprint", "continuation", "de_template", "architecture", "running",
            "generate", "generation", "candidates", "proposal", "assistant", "outline",
        )
    ):
        return PHASE_CANDIDATE
    return PHASE_OTHER


# 调用 / 物理尝试只取聚合要用的列：``request_payload_summary`` 等 JSON 摘要列不读（审计 B09-15）
_CALL_COLUMNS = (
    LlmCall.llm_call_id,
    LlmCall.provider,
    LlmCall.model,
    LlmCall.node_id,
    LlmCall.step,
    LlmCall.scene_id,
    LlmCall.chapter_id,
    LlmCall.prompt_tokens,
    LlmCall.completion_tokens,
    LlmCall.total_tokens,
    LlmCall.estimated_tokens,
    LlmCall.budget_charged_tokens,
    LlmCall.usage_is_estimate,
    LlmCall.error_code,
    LlmCall.accounting_status,
    LlmCall.latency_ms,
    LlmCall.created_at,
)
_ATTEMPT_COLUMNS = (
    LlmCallAttempt.llm_call_id,
    LlmCallAttempt.provider_attempt_no,
    LlmCallAttempt.dispatch_kind,
    LlmCallAttempt.prompt_tokens,
    LlmCallAttempt.completion_tokens,
    LlmCallAttempt.total_tokens,
    LlmCallAttempt.usage_is_estimate,
    LlmCallAttempt.accounting_status,
    LlmCallAttempt.error_code,
    LlmCallAttempt.request_dispatched_at,
)
_LEGACY_ID_CHUNK = 500
_FAILED_ATTEMPT_STATUSES = frozenset({"failed", "usage_exceeds_reservation"})
_DEGRADE_DISPATCH_KINDS = frozenset({"api_mode_degrade", "structured_output_degrade", "missing_text_degrade"})
_ACCOUNTING_MARKER = "_accounting_provider_execution_mode"


@dataclass(slots=True)
class _Ledger:
    """一个范围（场景 / 章节 / 全书）的调用、它们的物理尝试、每条调用的金额（只折算一次，价书只读一次）。"""

    calls: list[Row]
    attempts: dict[str, list[Row]]
    costs: dict[str, dict[str, Any]]
    legacy_ids: frozenset[str]
    book: pricing.PriceBook


def _legacy_parent_ids(session: Session, call_ids: list[str]) -> frozenset[str]:
    """没有物理尝试行、请求摘要里也没有记账标记的父调用：迁移 0064 之前的老记录。只对这几行读摘要列。"""
    legacy: set[str] = set()
    for start in range(0, len(call_ids), _LEGACY_ID_CHUNK):
        chunk = call_ids[start : start + _LEGACY_ID_CHUNK]
        rows = session.execute(
            select(LlmCall.llm_call_id, LlmCall.request_payload_summary).where(LlmCall.llm_call_id.in_(chunk))
        )
        for call_id, summary in rows:
            if not isinstance(summary, dict) or _ACCOUNTING_MARKER not in summary:
                legacy.add(call_id)
    return frozenset(legacy)


def _load_ledger(session: Session, condition: ColumnElement[bool]) -> _Ledger:
    calls = list(session.execute(select(*_CALL_COLUMNS).where(condition)).all())
    attempts: dict[str, list[Row]] = {}
    if calls:
        rows = session.execute(
            select(*_ATTEMPT_COLUMNS)
            .where(LlmCallAttempt.llm_call_id.in_(select(LlmCall.llm_call_id).where(condition)))
            .order_by(LlmCallAttempt.llm_call_id.asc(), LlmCallAttempt.provider_attempt_no.asc())
        )
        for attempt in rows:
            attempts.setdefault(attempt.llm_call_id, []).append(attempt)
    book = pricing.load_price_book()
    costs = {
        call.llm_call_id: pricing.compute_cost(
            call.provider, call.model, call.prompt_tokens, call.completion_tokens, at=call.created_at, book=book
        )
        for call in calls
    }
    without_attempts = [call.llm_call_id for call in calls if call.llm_call_id not in attempts]
    return _Ledger(calls, attempts, costs, _legacy_parent_ids(session, without_attempts), book)


# ---- token 桶：token / 调用数为主，金额只累计已定价的部分 ------------------------------------------

def _bucket(**fields: Any) -> dict[str, Any]:
    return {**fields, "tokens": 0, "call_count": 0, "_money": 0.0, "_priced": 0}


def _add(bucket: dict[str, Any], tokens: Any, cost: dict[str, Any]) -> None:
    bucket["tokens"] += int(tokens or 0)
    bucket["call_count"] += 1
    if cost["priced"]:
        bucket["_money"] += cost["cost"]
        bucket["_priced"] += 1


def _close(bucket: dict[str, Any], *, with_priced: bool = False) -> dict[str, Any]:
    """桶收口：金额 = 已定价部分之和；一条已定价的都没有 → ``None``（未定价），不是 0。"""
    money, priced = bucket.pop("_money"), bucket.pop("_priced")
    bucket["cost"] = money if priced else None
    if with_priced:
        bucket["priced"] = priced == bucket["call_count"]
    return bucket


def _money_sum(values: list[float | None]) -> float | None:
    priced = [value for value in values if value is not None]
    return sum(priced) if priced else None


def _summarize(ledger: _Ledger) -> dict[str, Any]:
    """一个范围的通用汇总（场景 / 章节 / 全书共用）。"""
    total = _bucket()
    phases = {phase: _bucket() for phase in _PHASES}
    providers: dict[str, dict[str, Any]] = {}
    unpriced_models: dict[tuple[str, str], dict[str, Any]] = {}
    is_estimate = False
    estimated_tokens = 0
    provider_actual_tokens = 0
    budget_charged_tokens = 0
    attempt_row_count = 0
    physical_attempt_count = 0
    pre_dispatch_attempt_count = 0
    usage_estimate_count = 0
    exception_count = 0
    retry_attempt_count = 0
    transport_retry_attempt_count = 0
    response_parse_retry_attempt_count = 0
    degrade_attempt_count = 0
    legacy_parent_without_attempt_count = 0
    legacy_unreconstructable_tokens = 0
    for call in ledger.calls:
        cost = ledger.costs[call.llm_call_id]
        tokens = int(call.total_tokens or 0)
        _add(total, tokens, cost)
        _add(phases[classify_phase(call.node_id, call.step)], tokens, cost)
        provider = call.provider or "unknown"
        _add(providers.setdefault(provider, _bucket()), tokens, cost)
        if not cost["priced"]:
            key = (provider, call.model or "unknown")
            _add(unpriced_models.setdefault(key, _bucket(provider=key[0], model=key[1])), tokens, cost)
        is_estimate = is_estimate or bool(call.usage_is_estimate)

        call_attempts = ledger.attempts.get(call.llm_call_id, [])
        if call_attempts:
            # 父调用是唯一逻辑/报表层；它的账目字段已是物理尝试之和。
            estimated_tokens += int(call.estimated_tokens or 0)
            budget_charged_tokens += int(call.budget_charged_tokens or 0)
            attempt_row_count += len(call_attempts)
            for attempt in call_attempts:
                dispatched = attempt.request_dispatched_at is not None
                if dispatched:
                    physical_attempt_count += 1
                else:
                    pre_dispatch_attempt_count += 1
                if dispatched and bool(attempt.usage_is_estimate):
                    usage_estimate_count += 1
                elif dispatched:
                    provider_actual_tokens += int(attempt.total_tokens or 0)
                if attempt.error_code:
                    exception_count += 1
                if dispatched and int(attempt.provider_attempt_no or 0) > 0:
                    retry_attempt_count += 1
                if dispatched and attempt.dispatch_kind == "transport_retry":
                    # transport_retry 同时承载普通重试和连接能力降级，不能猜测拆分。
                    transport_retry_attempt_count += 1
                if dispatched and attempt.dispatch_kind == "response_parse_retry":
                    response_parse_retry_attempt_count += 1
                if dispatched and attempt.dispatch_kind in _DEGRADE_DISPATCH_KINDS:
                    degrade_attempt_count += 1
            continue

        if call.llm_call_id in ledger.legacy_ids:
            # 0064 前的父调用没有物理尝试行和新账目字段；显式兼容读取。
            legacy_parent_without_attempt_count += 1
            legacy_unreconstructable_tokens += tokens
            estimated_tokens += int(call.estimated_tokens or tokens)
            if call.usage_is_estimate is False:
                provider_actual_tokens += tokens
            # 0065 将 legacy charge 明确回填为 0；零值不能回退成 total。
            budget_charged_tokens += int(call.budget_charged_tokens or 0)
            usage_estimate_count += int(bool(call.usage_is_estimate))
            exception_count += int(bool(call.error_code))
        else:
            estimated_tokens += int(call.estimated_tokens or 0)
            budget_charged_tokens += int(call.budget_charged_tokens or 0)

    total_tokens = total["tokens"]
    priced_call_count = total["_priced"]
    _close(total)
    phase_breakdown: dict[str, dict[str, Any]] = {}
    for phase, bucket in phases.items():
        _close(bucket)
        # 占比按 token：金额只覆盖已定价的模型，按金额算的占比会把未定价的阶段算成 0
        bucket["share"] = (bucket["tokens"] / total_tokens) if total_tokens > 0 else 0.0
        phase_breakdown[phase] = bucket
    for bucket in providers.values():
        _close(bucket)
    unpriced = sorted(
        ({key: value for key, value in _close(bucket).items() if key != "cost"} for bucket in unpriced_models.values()),
        key=lambda item: (-item["tokens"], item["provider"], item["model"]),
    )
    unpriced_tokens = sum(item["tokens"] for item in unpriced)
    estimate_legacy_suffix = (
        "_with_legacy_total_tokens_fallback"
        if legacy_parent_without_attempt_count
        else ""
    )
    actual_legacy_suffix = (
        "_with_legacy_parent_usage_fallback"
        if legacy_parent_without_attempt_count
        else ""
    )
    return {
        "total_tokens": total_tokens,
        "call_count": total["call_count"],
        "total_cost": total["cost"],
        "currency": ledger.book.currency if priced_call_count else None,
        "is_estimate": is_estimate,
        "pricing": {
            "priced_call_count": priced_call_count,
            "unpriced_call_count": total["call_count"] - priced_call_count,
            "priced_tokens": total_tokens - unpriced_tokens,
            "unpriced_tokens": unpriced_tokens,
            "complete": priced_call_count == total["call_count"],
            "unpriced_models": unpriced,
        },
        "cross_provider": len(providers) > 1,
        "tokens_by_provider": {provider: bucket["tokens"] for provider, bucket in providers.items()},
        "cost_by_provider": {provider: bucket["cost"] for provider, bucket in providers.items()},
        "phase_breakdown": phase_breakdown,
        "calibers": {
            "estimate": {
                "tokens": estimated_tokens,
                "source": f"llm_calls.estimated_tokens{estimate_legacy_suffix}",
            },
            "provider_actual": {
                "tokens": provider_actual_tokens,
                "source": (
                    "llm_call_attempts.total_tokens_with_provider_usage"
                    f"{actual_legacy_suffix}"
                ),
            },
            "budget_charged": {
                "tokens": budget_charged_tokens,
                "source": "llm_calls.budget_charged_tokens",
            },
        },
        "attempt_observability": {
            "attempt_row_count": attempt_row_count,
            "physical_attempt_count": physical_attempt_count,
            "pre_dispatch_attempt_count": pre_dispatch_attempt_count,
            "usage_estimate_count": usage_estimate_count,
            "exception_count": exception_count,
            "retry_attempt_count": retry_attempt_count,
            "transport_retry_attempt_count": transport_retry_attempt_count,
            "response_parse_retry_attempt_count": response_parse_retry_attempt_count,
            "degrade_attempt_count": degrade_attempt_count,
            "legacy_parent_without_attempt_count": legacy_parent_without_attempt_count,
            "legacy_unreconstructable_tokens": legacy_unreconstructable_tokens,
        },
    }


def _budget_view(state: SceneRunState | None) -> dict[str, Any]:
    if state is None or state.scene_token_budget is None:
        return {
            "budget": None,
            "used": int(getattr(state, "scene_tokens_used", 0) or 0) if state else 0,
            "remaining": None,
            "over_budget": False,
            "usage_ratio": None,
            "baseline": None,
            "multiplier_used": None,
            "run_policy": getattr(state, "run_policy", None) if state else None,
        }
    budget = int(state.scene_token_budget)
    used = int(state.scene_tokens_used or 0)
    from novel_system.services.scene_budget import baseline_tokens, is_scene_budget_disarmed

    if is_scene_budget_disarmed(state):
        # 解除武装的场景没有真实上限——用 budget=None 表达「不限」，避免把哨兵天文数字
        # 当成预算展示（前端 cost 视图对 budget===null 已优雅隐藏预算卡）。记账值照常透出。
        return {
            "budget": None,
            "used": used,
            "remaining": None,
            "over_budget": False,
            "usage_ratio": None,
            "baseline": None,
            "multiplier_used": None,
            "disarmed": True,
            "run_policy": state.run_policy,
        }
    # 单发基线取场景依据里记的（武装倍率不一定是 5，作者追加也不改基线）
    baseline = baseline_tokens(state)
    return {
        "budget": budget,
        "used": used,
        "remaining": max(0, budget - used),
        "over_budget": used > budget,
        "usage_ratio": round(used / budget, 4) if budget else None,
        "baseline": baseline,
        "multiplier_used": round(used / baseline, 4) if baseline else None,
        "run_policy": state.run_policy,
    }


def _extra_cost(ledger: _Ledger, total_tokens: int) -> dict[str, Any]:
    """白花的 token：发出去却失败了的物理尝试（报错 / 用量超出预留）；老记录没有尝试行时看父调用的 ``error_code``。

    重评 R2 第 5 项（批准#2 / #4）：以前还有「重复质检」（质检阶段第 2 次起的调用）与「补候选」（超出按关键度
    给的初始候选数的生成类调用）两项。可一轮普通起草本来就依次跑硬质检 / 软质检 / 准定稿评审三道不同的质检，
    还有蓝图、章节架构、人物压力几次生成类调用，于是每一场都被算出两笔并不存在的额外成本（真实库里三场各
    2 / 2 / 3 次质检被记成重复质检，5 / 1 / 1 次普通调用被记成补候选）。这两项删了，只留失败重试。
    """
    failed = _bucket()
    for call in ledger.calls:
        call_attempts = ledger.attempts.get(call.llm_call_id)
        if call_attempts:
            for attempt in call_attempts:
                if attempt.request_dispatched_at is None:
                    continue
                if not (attempt.error_code or attempt.accounting_status in _FAILED_ATTEMPT_STATUSES):
                    continue
                cost = pricing.compute_cost(
                    call.provider,
                    call.model,
                    attempt.prompt_tokens,
                    attempt.completion_tokens,
                    at=call.created_at,
                    book=ledger.book,
                )
                _add(failed, attempt.total_tokens, cost)
        elif call.error_code:
            _add(failed, call.total_tokens, ledger.costs[call.llm_call_id])
    _close(failed)
    return {
        "failed_tokens": failed["tokens"],
        "failed_attempt_count": failed["call_count"],
        "failed_cost": failed["cost"],
        "failed_share": round(min(1.0, failed["tokens"] / total_tokens), 4) if total_tokens > 0 else 0.0,
    }


def scene_cost(session: Session, scene_id: str) -> dict[str, Any]:
    ledger = _load_ledger(session, LlmCall.scene_id == scene_id)
    summary = _summarize(ledger)
    return {
        "scene_id": scene_id,
        **summary,
        "budget": _budget_view(session.get(SceneRunState, scene_id)),
        "extra_cost": _extra_cost(ledger, summary["total_tokens"]),
    }


def _count(session: Session, statement: Any) -> int:
    return int(session.scalar(statement) or 0)


def chapter_cost(session: Session, chapter_id: str) -> dict[str, Any]:
    summary = _summarize(_load_ledger(session, LlmCall.chapter_id == chapter_id))
    archived_count = _count(
        session,
        select(func.count(distinct(FinalScene.scene_id))).where(
            FinalScene.status == "archived", FinalScene.chapter_id == chapter_id
        ),
    )
    return {
        "chapter_id": chapter_id,
        **summary,
        "archived_scene_count": archived_count,
        "tokens_per_archived_scene": (
            round(summary["total_tokens"] / archived_count) if archived_count else None
        ),
        "cost_per_archived_chapter": summary["total_cost"] if archived_count else None,
    }


def _project_calls(project_id: str) -> ColumnElement[bool]:
    """项目命中的调用：``project_id`` 直接命中，或 ``scene_id`` 属于项目的场景（``LlmCall.project_id`` 可能为空）。"""
    project_scenes = select(SceneCard.scene_id).where(SceneCard.project_id == project_id)
    return or_(LlmCall.project_id == project_id, LlmCall.scene_id.in_(project_scenes))


def _project_summary(session: Session, project_id: str, ledger: _Ledger) -> dict[str, Any]:
    summary = _summarize(ledger)
    cards = session.execute(select(SceneCard.scene_id, SceneCard.chapter_id).where(SceneCard.project_id == project_id)).all()
    archived = FinalScene.status == "archived"
    archived_count = _count(
        session,
        select(func.count(distinct(FinalScene.scene_id))).where(
            archived, FinalScene.scene_id.in_(select(SceneCard.scene_id).where(SceneCard.project_id == project_id))
        ),
    )
    archived_chapter_count = _count(
        session,
        select(func.count(distinct(FinalScene.chapter_id))).where(
            archived, FinalScene.chapter_id.in_(select(SceneCard.chapter_id).where(SceneCard.project_id == project_id))
        ),
    )
    total_cost = summary["total_cost"]
    return {
        "project_id": project_id,
        **summary,
        "chapter_count": len({card.chapter_id for card in cards}),
        "scene_count": len({card.scene_id for card in cards}),
        "archived_scene_count": archived_count,
        "archived_chapter_count": archived_chapter_count,
        "tokens_per_archived_scene": (
            round(summary["total_tokens"] / archived_count) if archived_count else None
        ),
        "cost_per_archived_chapter": (
            round(total_cost / archived_chapter_count, 6)
            if archived_chapter_count and total_cost is not None
            else None
        ),
    }


def project_cost(session: Session, project_id: str) -> dict[str, Any]:
    return _project_summary(session, project_id, _load_ledger(session, _project_calls(project_id)))


# ---------------------------------------------------------------------------
# 项目成本看板（cost-dashboard）：summary 之上补趋势 / 构成 / 明细，一读拿全。构成与排序都按 token。
# ---------------------------------------------------------------------------

DASHBOARD_DEFAULT_DAYS = 30
DASHBOARD_MAX_DAYS = 365
DASHBOARD_NODE_LIMIT = 10
DASHBOARD_CALL_LIMIT = 10


def _clamp_days(days: Any) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        return DASHBOARD_DEFAULT_DAYS
    return max(1, min(DASHBOARD_MAX_DAYS, value))


def _trend(ledger: _Ledger, days: int) -> dict[str, Any]:
    """近 ``days`` 天（含今天，UTC）逐日 token / 调用数 / 金额，稠密序列——缺日补零。

    ``created_at`` 是 UTC ISO 字符串，前 10 位即日桶；缺失的不进趋势。
    """
    today = datetime.now(UTC).date()
    window = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    by_day = {day: _bucket(date=day) for day in window}
    for call in ledger.calls:
        bucket = by_day.get((call.created_at or "")[:10])
        if bucket is not None:
            _add(bucket, call.total_tokens, ledger.costs[call.llm_call_id])
    series = [_close(by_day[day]) for day in window]
    return {
        "days": days,
        "window_start": window[0],
        "window_end": window[-1],
        "series": series,
        "window_tokens": sum(item["tokens"] for item in series),
        "window_call_count": sum(item["call_count"] for item in series),
        "window_cost": _money_sum([item["cost"] for item in series]),
    }


def _by_model(ledger: _Ledger) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for call in ledger.calls:
        key = (call.provider or "unknown", call.model or "unknown")
        bucket = buckets.setdefault(key, _bucket(provider=key[0], model=key[1], is_estimate=False))
        _add(bucket, call.total_tokens, ledger.costs[call.llm_call_id])
        bucket["is_estimate"] = bucket["is_estimate"] or bool(call.usage_is_estimate)
    rows = [_close(bucket, with_priced=True) for bucket in buckets.values()]
    return sorted(rows, key=lambda b: (-b["tokens"], b["provider"], b["model"]))


def _by_node(ledger: _Ledger, limit: int) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {}
    for call in ledger.calls:
        node = call.node_id or call.step or "unknown"
        bucket = buckets.setdefault(node, _bucket(node_id=node, phase=classify_phase(call.node_id, call.step)))
        _add(bucket, call.total_tokens, ledger.costs[call.llm_call_id])
    ordered = sorted((_close(bucket) for bucket in buckets.values()), key=lambda b: (-b["tokens"], b["node_id"]))
    top, rest = ordered[:limit], ordered[limit:]
    remainder = None
    if rest:
        remainder = {
            "node_count": len(rest),
            "tokens": sum(b["tokens"] for b in rest),
            "call_count": sum(b["call_count"] for b in rest),
            "cost": _money_sum([b["cost"] for b in rest]),
        }
    return {"top": top, "remainder": remainder}


def _by_chapter(ledger: _Ledger) -> list[dict[str, Any]]:
    buckets: dict[str | None, dict[str, Any]] = {}
    scenes: dict[str | None, set[str]] = {}
    for call in ledger.calls:
        key = call.chapter_id or None
        _add(buckets.setdefault(key, _bucket(chapter_id=key)), call.total_tokens, ledger.costs[call.llm_call_id])
        if call.scene_id:
            scenes.setdefault(key, set()).add(call.scene_id)
    rows = []
    for key, bucket in buckets.items():
        _close(bucket)
        bucket["scene_count"] = len(scenes.get(key, ()))
        rows.append(bucket)
    # 未关联章节的调用（项目级节点等）排最后，其余按 token 降序
    return sorted(rows, key=lambda b: (b["chapter_id"] is None, -b["tokens"], b["chapter_id"] or ""))


def _top_calls(ledger: _Ledger, limit: int) -> list[dict[str, Any]]:
    ordered = sorted(
        ledger.calls, key=lambda call: (-int(call.total_tokens or 0), call.created_at or "", call.llm_call_id)
    )
    rows = []
    for call in ordered[:limit]:
        cost = ledger.costs[call.llm_call_id]
        rows.append(
            {
                "llm_call_id": call.llm_call_id,
                "created_at": call.created_at,
                "node_id": call.node_id or call.step,
                "phase": classify_phase(call.node_id, call.step),
                "provider": call.provider,
                "model": call.model,
                "total_tokens": int(call.total_tokens or 0),
                "priced": cost["priced"],
                "cost": cost["cost"],
                "currency": cost["currency"],
                "is_estimate": bool(call.usage_is_estimate),
                "latency_ms": call.latency_ms,
                "error_code": call.error_code,
                "accounting_status": call.accounting_status,
                "scene_id": call.scene_id,
                "chapter_id": call.chapter_id,
            }
        )
    return rows


def project_cost_dashboard(
    session: Session,
    project_id: str,
    *,
    days: Any = DASHBOARD_DEFAULT_DAYS,
    node_limit: int = DASHBOARD_NODE_LIMIT,
    call_limit: int = DASHBOARD_CALL_LIMIT,
) -> dict[str, Any]:
    """成本看板一读聚合：summary + 趋势 + 模型 / 节点 / 章节构成 + 用 token 最多的调用。

    只读，与 ``project_cost`` 同一个调用范围；调用与尝试各查一次，每条调用只折算一次价格。
    趋势按 UTC 日桶且稠密补零，前端可直接画图；空项目返回空构成不 500。
    """
    days = _clamp_days(days)
    ledger = _load_ledger(session, _project_calls(project_id))
    return {
        "project_id": project_id,
        "summary": _project_summary(session, project_id, ledger),
        "trend": _trend(ledger, days),
        "by_model": _by_model(ledger),
        "by_node": _by_node(ledger, max(1, int(node_limit))),
        "by_chapter": _by_chapter(ledger),
        "top_calls": _top_calls(ledger, max(1, int(call_limit))),
    }
