"""风格参考 · 配置对比（2026-10-18）：风格棘轮（``ratchet.py``）在界面上的后台作业与载荷。

命令行工具 ``style_ratchet`` 要上服务器敲命令、结果是几份 Markdown；这里把同一套比较做成作业表里的一种作业
（``kind = ratchet``，走对照检查 / 校准那条短车道），界面「风格参考 → 配置对比」：

- **总览**（:func:`ratchet_overview`）：现在的配置（模型 / 样例窗数 / 样例位置）、可换的模型、噪声记录（A/A 量过的配置与
  平均差）、在跑的作业、历史比较（结论、盲读进度）；
- **预估**（:func:`ratchet_preview`）：只读地定留出段、看缓存——要调几次模型、每次带多少字参考；
- **开跑 / 取消**（:func:`start_ratchet_job` / :func:`cancel_ratchet`）：同一本书同一时间只跑一个比较；
- **处理器**（:func:`run_ratchet_job`）：``plan_run`` + ``execute_run``，每次模型调用之前查一次取消、写一次进度；结果写进作业行：
  ``summary``（结论与逐段数字）、``reading``（盲读三份文字与答案）、``votes``（作者的盲读选择）、``revealed``；
- **详情 / 盲读**（:func:`ratchet_run_detail` / :func:`save_reading_votes`）：每段选「最像 / 最不像」，存在作业行上。

**先读后看**：揭晓之前，详情不给答案、不给结论与逐段数字（逐段字数就能对出哪份是哪份），历史清单与活动面板也不带
结论；作者读完点「揭晓」（或不读、直接「看结论」）才给。揭晓之后盲读选择不再能改——之后的选择不是盲的。

留出段、要点与稿子的缓存、噪声记录与命令行工具共用 ``ratchet.default_out_dir()``（同一个种子 → 同一组留出段）。
进程退出时作业放回队列，重跑时缓存里已有的要点与稿子不再调用模型。配置对比作业不进保留期清理（它就是历史记录）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from novel_system.db.models import StyleReferenceJob
from novel_system.services.errors import DomainError
from novel_system.services.style_reference import ratchet as R
from novel_system.services.style_reference.errors import LLMRequiredError
from novel_system.services.style_reference.job_runtime import JobRun
from novel_system.services.style_reference.jobs import (
    ACTIVE_STATES,
    JOB_KIND_CLASSIFY,
    JOB_KIND_RATCHET,
    STATE_SUCCEEDED,
    TERMINAL_STATES,
    ClaimedJob,
    JobLost,
    StyleJobService,
    already_active_error,
    job_activity_entry,
)
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed

logger = logging.getLogger(__name__)

OPERATION = "style_ratchet"
# 与命令行工具同一个缺省种子：界面与命令行比的是同一组留出段，缓存互通
SEED = "ratchet"
PASSAGE_CHOICES: tuple[int, ...] = (3, 4, 5, 6)
RUN_LIST_LIMIT = 30
# 预估输入字数：起草一次 ≈ 参考块 + 模板与要点；提炼要点一次 ≈ 原段 + 模板
DRAFT_OVERHEAD_CHARS = 3_000
PLOT_OVERHEAD_CHARS = 1_500

RATCHET_REFUSED_CODE = "STYLE_RATCHET_REFUSED"
RATCHET_FAILED_CODE = "STYLE_RATCHET_FAILED"
RATCHET_ALREADY_ACTIVE_CODE = "STYLE_RATCHET_ALREADY_ACTIVE"
RATCHET_NOT_ACTIVE_CODE = "STYLE_RATCHET_NOT_ACTIVE"
RATCHET_RUN_NOT_FOUND_CODE = "STYLE_RATCHET_RUN_NOT_FOUND"
RATCHET_VOTES_INVALID_CODE = "STYLE_RATCHET_VOTES_INVALID"


def _domain(exc: R.RatchetError) -> DomainError:
    """棘轮自己的中文原因 → 领域错误：还没调用模型的拒绝 409，跑到一半的失败 502（可重试）。"""
    if exc.refused:
        return DomainError(RATCHET_REFUSED_CODE, str(exc), status_code=409, details={"retryable": False})
    error = DomainError(RATCHET_FAILED_CODE, str(exc), status_code=502, details={"retryable": True})
    error.retryable = True  # type: ignore[attr-defined]
    return error


def resolve_client() -> tuple[Any | None, bool]:
    """按当前运行时配置取模型客户端（测试在这里打桩）。"""
    from novel_system.services.llm_service_base import runtime_llm_client_and_enabled

    return runtime_llm_client_and_enabled()


def _specs(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> tuple[R.ArmSpec, R.ArmSpec]:
    try:
        return R.arm_spec_from_body("A", a), R.arm_spec_from_body("B", b)
    except R.RatchetError as exc:
        raise _domain(exc) from exc


def _passages(value: Any) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = R.DEFAULT_PASSAGES
    return max(R.MIN_PASSAGES, min(R.MAX_PASSAGES, count))


def _plan(session: Session, profile_id: str, a: Mapping[str, Any] | None, b: Mapping[str, Any] | None, passages: Any) -> R.Plan:
    try:
        return R.plan_run(
            session,
            profile_id=profile_id,
            specs=_specs(a, b),
            passages=_passages(passages),
            seed=SEED,
            out_dir=R.default_out_dir(),
        )
    except R.RatchetError as exc:
        raise _domain(exc) from exc


# ---------------------------------------------------------------------------
# 总览 / 预估
# ---------------------------------------------------------------------------


def model_choices(route: Any) -> list[str]:
    """「风格稿」路由所在的服务上配好的模型（换模型只在同一个服务里换）；读不出来就只给路由现在的模型。"""
    current = str(getattr(route, "model", "") or "")
    models: list[str] = []
    provider_id = str(getattr(route, "provider_id", None) or "")
    if provider_id:
        try:
            from novel_system.db.session import SessionLocal
            from novel_system.services.system_config import SystemConfigService

            with SessionLocal() as config_session:
                provider = SystemConfigService(config_session).llm_overview()["providers"].get(provider_id) or {}
            models = [str(model) for model in provider.get("models") or [] if str(model or "").strip()]
        except Exception:  # noqa: BLE001 — 读不出服务配置只是少几个选项
            logger.debug("ratchet model choices unavailable for provider %s", provider_id, exc_info=True)
    return list(dict.fromkeys([*([current] if current else []), *models]))


def noise_view(out_dir: Any = None) -> list[dict[str, Any]]:
    """噪声记录（A/A 量过的配置）：每条 = 配置说明 + 每段差的平均 + 量了几段。"""
    records = R.noise_records(out_dir or R.default_out_dir())
    out: list[dict[str, Any]] = []
    for key, entry in records.items():
        if not isinstance(entry, Mapping):
            continue
        diffs = [float(diff) for diff in entry.get("diffs") or []]
        if not diffs:
            continue
        out.append(
            {
                "key": key,
                "describe": str(entry.get("describe") or ""),
                "mean": round(sum(diffs) / len(diffs), 4),
                "passages": len(diffs),
                "runs": len(entry.get("runs") or []),
            }
        )
    return out


def _rank_of(vote: Mapping[str, Any], label: str) -> int:
    """一段里某份的名次：最像 0、最不像 2、其余 1。"""
    if vote.get("best") == label:
        return 0
    if vote.get("worst") == label:
        return 2
    return 1


def _reading_progress(result: Mapping[str, Any]) -> dict[str, Any]:
    """盲读进度；揭晓之后加上读出来的结果：每段 A / B 谁排得更前、原文被认作最像几次。"""
    reading = [item for item in result.get("reading") or [] if isinstance(item, Mapping)]
    votes = result.get("votes") if isinstance(result.get("votes"), Mapping) else {}
    complete = [
        (item, votes[str(item.get("index"))])
        for item in reading
        if isinstance(votes.get(str(item.get("index"))), Mapping)
        and votes[str(item.get("index"))].get("best")
        and votes[str(item.get("index"))].get("worst")
    ]
    out: dict[str, Any] = {"done": len(complete), "total": len(reading)}
    if not result.get("revealed"):
        return out
    prefer = {"A": 0, "B": 0, "tie": 0}
    original_best = 0
    for item, vote in complete:
        by_source = {source: label for label, source in (item.get("key") or {}).items()}
        rank_a = _rank_of(vote, by_source.get("A", ""))
        rank_b = _rank_of(vote, by_source.get("B", ""))
        prefer["A" if rank_a < rank_b else "B" if rank_b < rank_a else "tie"] += 1
        original_best += 1 if vote.get("best") == by_source.get("original") else 0
    out.update({"prefer": prefer, "original_best": original_best})
    return out


def run_list_entry(job: StyleReferenceJob) -> dict[str, Any]:
    result = dict(job.result_json or {})
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else None
    revealed = bool(result.get("revealed"))
    verdict = dict(summary.get("verdict") or {}) if summary and revealed else {}
    params = dict(job.params_json or {})
    return {
        "job_id": job.job_id,
        "status": job.state,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "passages": params.get("passages"),
        "requested": {"A": dict(params.get("a") or {}), "B": dict(params.get("b") or {})},
        "arms": dict(summary.get("arms") or {}) if summary else None,
        "same_config": bool(summary.get("same_config")) if summary else None,
        "revealed": revealed,
        # 揭晓之前不带结论（见模块说明「先读后看」）
        "headline": summary.get("headline") if summary and revealed else None,
        "winner": verdict.get("winner") if summary and revealed else None,
        "noise": summary.get("noise") if summary and revealed else None,
        "reading": _reading_progress(result) if summary else None,
        "error": dict(job.error_json) if job.error_json else None,
    }


def _ratchet_jobs(session: Session, profile_id: str, *, limit: int | None = RUN_LIST_LIMIT) -> list[StyleReferenceJob]:
    query = (
        select(StyleReferenceJob)
        .where(StyleReferenceJob.kind == JOB_KIND_RATCHET, StyleReferenceJob.profile_id == str(profile_id))
        .order_by(StyleReferenceJob.created_at.desc(), StyleReferenceJob.job_id.desc())
    )
    if limit is not None:
        query = query.limit(limit)
    return list(session.scalars(query))


def ratchet_overview(session: Session, profile_id: str) -> dict[str, Any]:
    """「配置对比」页一进来要的：能不能比（不能就说原因）、现在的配置、可选项、噪声、在跑的与历史。"""
    jobs = _ratchet_jobs(session, profile_id)
    active = next((job for job in jobs if job.state in ACTIVE_STATES), None)
    base: dict[str, Any] = {
        "profile_id": str(profile_id),
        "active": job_activity_entry(active) if active is not None else None,
        "runs": [run_list_entry(job) for job in jobs if job.state in TERMINAL_STATES],
        "noise": noise_view(),
        "passage_choices": list(PASSAGE_CHOICES),
        "passages_default": R.DEFAULT_PASSAGES,
        "windows_max": R.SAMPLE_WINDOWS_MAX,
        "default_epsilon": R.DEFAULT_EPSILON,
    }
    try:
        subject = R.resolve_subject(session, profile_id)
        arm = R.resolve_arm(subject, R.ArmSpec(label="A"))
    except R.RatchetError as exc:
        return {**base, "available": False, "reason": str(exc), "current": None, "models": []}
    current = R.arm_view(arm)
    current.pop("key", None)
    return {
        **base,
        "available": True,
        "reason": None,
        "current": {**current, "from_binding": bool(subject.binding_id)},
        "models": model_choices(arm.route),
    }


def ratchet_preview(
    session: Session,
    profile_id: str,
    *,
    a: Mapping[str, Any] | None,
    b: Mapping[str, Any] | None,
    passages: Any,
) -> dict[str, Any]:
    """只读的预估：留出段（已有套件就是它）、哪些要点与稿子已在缓存、要调几次模型、大约多少字输入。"""
    try:
        plan = _plan(session, profile_id, a, b, passages)
    finally:
        session.rollback()
    arm_a, arm_b = plan.arms
    plot_calls = len(plan.plots_needed)
    draft_calls = {arm.label: len(plan.drafts_needed(arm)) for arm in plan.arms}
    input_chars = sum(passage.chars + PLOT_OVERHEAD_CHARS for passage in plan.plots_needed) + sum(
        draft_calls[arm.label] * (plan.style_chars.get(arm.key, 0) + DRAFT_OVERHEAD_CHARS) for arm in plan.arms
    )
    return {
        "same_config": R.base_key(arm_a) == R.base_key(arm_b),
        "arms": {"A": R.arm_view(arm_a), "B": R.arm_view(arm_b)},
        "passages": [
            {**passage.meta(), "plot_cached": passage.window_no in plan.plots} for passage in plan.passages
        ],
        "plot_calls": plot_calls,
        "draft_calls": draft_calls,
        "calls": plot_calls + sum(draft_calls.values()),
        "style_chars": {"A": plan.style_chars.get(arm_a.key, 0), "B": plan.style_chars.get(arm_b.key, 0)},
        "input_chars_estimate": input_chars,
    }


# ---------------------------------------------------------------------------
# 开跑 / 取消
# ---------------------------------------------------------------------------


def _conflict(other: StyleReferenceJob) -> DomainError:
    if other.kind == JOB_KIND_RATCHET:
        return DomainError(
            RATCHET_ALREADY_ACTIVE_CODE,
            "这本书已经有一个配置对比在跑：等它完成，或先取消。",
            status_code=409,
            details={"job_id": other.job_id, "state": other.state},
        )
    return already_active_error(other)


def start_ratchet_job(
    session: Session,
    profile_id: str,
    *,
    a: Mapping[str, Any] | None,
    b: Mapping[str, Any] | None,
    passages: Any,
    op_key: str | None = None,
    llm_client: Any | None = None,
) -> StyleReferenceJob:
    """建一个配置对比作业（调用方提交后派发）。配置写法不对 / 画像或书用不了 409 ``STYLE_RATCHET_REFUSED``；
    书的云策略不许把原文送给「风格稿」路由 409；同一本书已有对比在跑 409 ``STYLE_RATCHET_ALREADY_ACTIVE``。"""
    _specs(a, b)
    try:
        subject = R.resolve_subject(session, profile_id)
    except R.RatchetError as exc:
        raise _domain(exc) from exc
    ensure_cloud_llm_allowed(subject.book, operation=OPERATION, node_ids=(R.DRAFT_NODE_ID,), llm_client=llm_client)
    job = StyleJobService(session).create(
        JOB_KIND_RATCHET,
        book_id=str(subject.book.book_id),
        profile_id=str(profile_id),
        op_key=op_key or None,
        params={
            "profile_id": str(profile_id),
            "a": dict(a or {}),
            "b": dict(b or {}),
            "passages": _passages(passages),
            "seed": SEED,
        },
        phase="queued",
        exclusive_with=(JOB_KIND_CLASSIFY,),
        conflict_error=_conflict,
    )
    job.progress_json = {"phase": "queued", "phase_label": "排队中"}
    session.flush()
    return job


def cancel_ratchet(session: Session, profile_id: str) -> StyleReferenceJob:
    active = next((job for job in _ratchet_jobs(session, profile_id) if job.state in ACTIVE_STATES), None)
    if active is None:
        raise DomainError(
            RATCHET_NOT_ACTIVE_CODE,
            "这份画像没有正在排队或运行的配置对比。",
            status_code=409,
            details={"profile_id": profile_id},
        )
    return StyleJobService(session).request_cancel(active.job_id)


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------


def run_ratchet_job(session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
    """``ratchet`` 作业处理器（见模块说明）。终态由框架写；进度写 / 结束写落空 → ``JobLost``。"""
    run = JobRun(session, claimed, service)
    params = dict(claimed.params or {})
    profile_id = str(params.get("profile_id") or claimed.profile_id or "")
    run.check_continue()
    run.checkpoint(phase="prepare", phase_label="挑留出段、看缓存", done=0, total=1)
    llm_client, enabled = resolve_client()
    if not enabled or llm_client is None:
        raise LLMRequiredError(operation=OPERATION)
    plan = _plan(session, profile_id, params.get("a"), params.get("b"), params.get("passages"))
    session.commit()

    steps = {"total": 1}

    def on_progress(done: int, total: int, label: str) -> None:
        steps["total"] = max(total, 1)
        run.checkpoint(phase="run", phase_label=label, done=done, total=steps["total"])

    try:
        result = R.execute_run(
            session,
            plan,
            llm_client=llm_client,
            epsilon=R.DEFAULT_EPSILON,
            on_progress=on_progress,
            check_continue=run.check_continue,
        )
    except R.RatchetError as exc:
        raise _domain(exc) from exc
    payload = {
        "summary": R.result_summary(plan, result),
        "reading": R.reading_payload(plan, result),
        "votes": {},
        "revealed": False,
    }
    run.check_continue()
    run.checkpoint(commit=False, phase="done", phase_label="完成", done=steps["total"], total=steps["total"])
    if not service.succeed(claimed, payload):
        raise JobLost(claimed.job_id)


def ratchet_never_resumes(_job: StyleReferenceJob) -> bool:
    """失败 / 取消后不「继续」：重新开一次比较（缓存里已有的要点与稿子不再调用模型）。"""
    return False


# ---------------------------------------------------------------------------
# 详情 / 盲读
# ---------------------------------------------------------------------------


def _ratchet_job(session: Session, job_id: str) -> StyleReferenceJob:
    job = session.get(StyleReferenceJob, str(job_id))
    if job is None or job.kind != JOB_KIND_RATCHET:
        raise DomainError(
            RATCHET_RUN_NOT_FOUND_CODE, "没有这一次配置对比。", status_code=404, details={"job_id": job_id}
        )
    return job


def ratchet_run_detail(session: Session, job_id: str) -> dict[str, Any]:
    """一次比较的全部：作业状态、盲读三份文字、作者的选择；揭晓之后再加答案、结论与逐段数字（见「先读后看」）。"""
    job = _ratchet_job(session, job_id)
    result = dict(job.result_json or {})
    revealed = bool(result.get("revealed"))
    reading: list[dict[str, Any]] = []
    for item in result.get("reading") or []:
        if not isinstance(item, Mapping):
            continue
        entry = dict(item)
        if not revealed:
            entry.pop("key", None)
        reading.append(entry)
    summary = result.get("summary") if isinstance(result.get("summary"), Mapping) else None
    if summary is not None and not revealed:
        summary = {key: summary[key] for key in ("arms", "same_config", "usage", "skipped") if key in summary}
    return {
        "job": job_activity_entry(job),
        "entry": run_list_entry(job),
        "revealed": revealed,
        "summary": summary,
        "reading": reading,
        "votes": dict(result.get("votes") or {}),
    }


def save_reading_votes(
    session: Session,
    job_id: str,
    votes: Mapping[str, Mapping[str, Any]],
    *,
    reveal: bool = False,
) -> dict[str, Any]:
    """存盲读选择（整份替换）：每段 ``{best, worst}``，都是甲 / 乙 / 丙之一、不能是同一份；段号必须是这次比较里有的。
    ``reveal``：同时揭晓。已揭晓的再存选择 409（之后的选择不是盲的）；只是再揭晓一次无害。"""
    job = _ratchet_job(session, job_id)
    if job.state != STATE_SUCCEEDED:
        raise DomainError(
            RATCHET_VOTES_INVALID_CODE, "这一次对比没有跑完，没有可盲读的稿子。", status_code=409, details={"job_id": job_id}
        )
    result = dict(job.result_json or {})
    if result.get("revealed"):
        if votes and dict(votes) != dict(result.get("votes") or {}):
            raise DomainError(
                RATCHET_VOTES_INVALID_CODE,
                "已经揭晓过答案：盲读选择不再能改。",
                status_code=409,
                details={"job_id": job_id, "revealed": True},
            )
        return {"job_id": job.job_id, "votes": dict(result.get("votes") or {}), "revealed": True}
    indexes = {str(item.get("index")) for item in result.get("reading") or [] if isinstance(item, Mapping)}
    clean: dict[str, dict[str, str | None]] = {}
    for index, vote in (votes or {}).items():
        best = (vote or {}).get("best") or None
        worst = (vote or {}).get("worst") or None
        if str(index) not in indexes or (best and best not in R.READING_LABELS) or (worst and worst not in R.READING_LABELS):
            raise DomainError(
                RATCHET_VOTES_INVALID_CODE, "盲读的选择不对：段号或甲乙丙不在这一次对比里。", status_code=422, details={"index": index}
            )
        if best and worst and best == worst:
            raise DomainError(
                RATCHET_VOTES_INVALID_CODE, "同一份不能既是最像又是最不像。", status_code=422, details={"index": index}
            )
        if best or worst:
            clean[str(index)] = {"best": best, "worst": worst}
    result["votes"] = clean
    if reveal:
        result["revealed"] = True
    job.result_json = result
    flag_modified(job, "result_json")
    session.flush()
    return {"job_id": job.job_id, "votes": clean, "revealed": bool(result.get("revealed"))}


__all__ = [
    "OPERATION",
    "PASSAGE_CHOICES",
    "RATCHET_ALREADY_ACTIVE_CODE",
    "RATCHET_FAILED_CODE",
    "RATCHET_NOT_ACTIVE_CODE",
    "RATCHET_REFUSED_CODE",
    "RATCHET_RUN_NOT_FOUND_CODE",
    "RATCHET_VOTES_INVALID_CODE",
    "SEED",
    "cancel_ratchet",
    "model_choices",
    "noise_view",
    "ratchet_never_resumes",
    "ratchet_overview",
    "ratchet_preview",
    "ratchet_run_detail",
    "resolve_client",
    "run_list_entry",
    "run_ratchet_job",
    "save_reading_votes",
    "start_ratchet_job",
]
