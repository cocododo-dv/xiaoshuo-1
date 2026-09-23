"""参考书活动清单:风格参考模块所有在跑 / 刚结束的耗时操作,一份统一形状。

三类来源合成一份:
- **作业表**(``style_reference_jobs``,2026-09-23 v3):分类作业(导入 / 重新分类 / 就地重标类型)
  以及之后迁过来的学习 / 检查作业,条目由 ``jobs.job_activity_entry`` 给出(键 ``job:<id>``);
  分类作业另带一条兼容旧前端的别名条目(键 = 请求的幂等键,kind = import / reclassify,
  ``compat_alias_of`` 指回 ``job:`` 条目),P7 随 ``/imports/{key}/progress`` 一起删;
- 进程内登记簿(``import_progress``):合成画像、应用画像的 RAG 索引、回测 worker 的阶段进度
  (P3 / P5 把它们迁到作业表之前)——进程没了就没了;
- 库里的 durable 行:抽取 run(``style_reference_runs``,子维粒度进度写在
  ``coverage_json["progress"]``)与回测报告(``style_reference_validation_reports``)。

同一操作在两边都有时(回测:worker 登记阶段,报告行给终态),以 durable 行的状态为准、
登记簿的阶段 / 百分比为辅。终态条目只保留最近 ``RECENT_FINISHED_SECONDS``,让前端的
最后几次轮询读到结果;更早的历史走各自的列表端点。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceRun, StyleReferenceValidationReport
from novel_system.services.style_reference.import_job import classification_activity_entries
from novel_system.services.style_reference.import_progress import (
    KIND_LABELS,
    list_operation_progress,
)
from novel_system.services.style_reference.jobs import (
    ACTIVE_STATES,
    JOB_KIND_CLASSIFY,
    StyleJobService,
    job_activity_entry,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository

RECENT_FINISHED_SECONDS = 600
MAX_ITEMS = 50

LAYER_LABELS: dict[str, str] = {
    "language": "语言层",
    "narrative": "叙事层",
    "scene": "场景层",
    "theme": "主题层",
}
SUB_DIM_LABELS: dict[str, str] = {
    "language.sentence_structure": "句法结构",
    "language.vocabulary": "词汇",
    "language.rhetoric": "修辞",
    "language.punctuation": "标点",
    "narrative.perspective": "视角",
    "narrative.pacing": "节奏",
    "narrative.time_handling": "时间处理",
    "narrative.information_density": "信息密度",
    "scene.environment": "环境",
    "scene.character_portrayal": "人物刻画",
    "scene.dialogue": "对话",
    "scene.sensory_priority": "感官优先级",
    "theme.emotional_tone": "情感基调",
    "theme.values": "价值观",
    "theme.motifs": "母题",
    "theme.narrative_philosophy": "叙事哲学",
}
_RUN_STATUS: dict[str, str] = {
    "running": "running",
    "pending": "running",
    "done": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}
_REPORT_STATUS: dict[str, str] = {
    "queued": "running",
    "running": "running",
    "completed": "succeeded",
    "failed": "failed",
}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _seconds_between(start: str | None, end: datetime) -> float | None:
    started = _parse_iso(start)
    if started is None:
        return None
    return round(max(0.0, (end - started).total_seconds()), 1)


def _entry_from_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    kind = str(snap.get("kind") or "import")
    return {
        "key": snap["op_key"],
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "source": "registry",
        "title": snap.get("title"),
        "book_id": snap.get("book_id"),
        "target_id": snap.get("target_id"),
        "status": snap.get("status"),
        "phase": snap.get("phase"),
        "phase_label": snap.get("phase_label"),
        "percent": snap.get("percent"),
        "steps": snap.get("steps"),
        "llm_calls": snap.get("llm_calls", 0),
        "started_at": snap.get("started_at"),
        "updated_at": snap.get("updated_at"),
        "elapsed_seconds": snap.get("elapsed_seconds"),
        "eta_seconds": snap.get("eta_seconds"),
        "error": snap.get("error"),
        "result": snap.get("result"),
        "cancellable": False,
        "retryable": False,
        # 导入旧契约字段原样带上,面板文案(分类批次 / 锚定集之外)照旧
        "classify": snap.get("classify"),
        "chars_total": snap.get("chars_total"),
        "paragraphs_total": snap.get("paragraphs_total"),
        "paragraphs_count": snap.get("paragraphs_count"),
    }


def run_activity_entry(
    run: StyleReferenceRun,
    *,
    title: str | None,
    now: datetime,
) -> dict[str, Any]:
    """抽取 run → 活动条目。子维粒度进度(2026-09-15)优先,老 run 只有层粒度时退到层。"""
    progress = dict((run.coverage_json or {}).get("progress") or {})
    status = _RUN_STATUS.get(str(run.status), str(run.status))
    layers_total = int(progress.get("layers_total") or 0)
    layers_done = int(progress.get("layers_done") or 0)
    sub_total = progress.get("sub_dims_total")
    sub_done = progress.get("sub_dims_done")
    current_sub = progress.get("current_sub_dim")
    current_layer = progress.get("current_layer")
    steps: dict[str, Any] | None = None
    if sub_total:
        done = int(sub_done or 0)
        total = int(sub_total)
        percent = int(99 * done / total) if total else 0
        steps = {
            "done": done,
            "total": total,
            "label": SUB_DIM_LABELS.get(str(current_sub), current_sub) if current_sub else None,
        }
    elif layers_total:
        percent = int(99 * layers_done / layers_total)
        steps = {"done": layers_done, "total": layers_total, "label": "层"}
    else:
        percent = 0
    if status == "succeeded":
        percent = 100
    seconds = [float(s) for s in (progress.get("sub_dim_seconds") or []) if isinstance(s, (int, float))]
    eta: float | None = None
    if status == "running" and seconds and sub_total:
        remaining = max(0, int(sub_total) - int(sub_done or 0))
        eta = round(sum(seconds) / len(seconds) * remaining, 1)
    if status == "running":
        phase = str(current_layer or "")
        phase_label = LAYER_LABELS.get(phase, phase) if phase else "排队中"
        if run.dispatch_state == "queued":
            phase_label = "排队中"
    elif status == "succeeded":
        phase, phase_label = "done", "完成"
    elif status == "cancelled":
        phase, phase_label = "cancelled", "已取消"
    else:
        phase, phase_label = "failed", "失败"
    finished = _parse_iso(run.finished_at)
    end = finished if (status != "running" and finished is not None) else now
    error = None
    if run.error_code or run.error_text:
        error = {"code": str(run.error_code or ""), "message": str(run.error_text or "")}
    return {
        "key": f"run:{run.run_id}",
        "kind": "extract",
        "kind_label": KIND_LABELS["extract"],
        "source": "durable",
        "title": title,
        "book_id": run.book_id,
        "target_id": run.run_id,
        "status": status,
        "phase": phase,
        "phase_label": phase_label,
        "percent": min(100, max(0, percent)),
        "steps": steps,
        "llm_calls": int(progress.get("llm_calls") or 0),
        "retries": int(progress.get("retries") or 0),
        "started_at": run.started_at or run.created_at,
        "updated_at": run.updated_at,
        "elapsed_seconds": _seconds_between(run.started_at or run.created_at, end),
        "eta_seconds": eta,
        "error": error,
        "result": None,
        "cancellable": status == "running",
        "retryable": bool(run.retryable),
        "layers": {
            "done": layers_done,
            "total": layers_total,
            "current": current_layer,
        },
    }


def report_activity_entry(
    report: StyleReferenceValidationReport,
    *,
    title: str | None,
    registry_entry: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    status = _REPORT_STATUS.get(str(report.status), str(report.status))
    if status == "succeeded" and not report.verdict:
        status = "running"
    base = dict(registry_entry) if registry_entry else {
        "phase": "local",
        "phase_label": "排队中" if report.status == "queued" else "校验中",
        "percent": 0,
        "steps": None,
        "llm_calls": 0,
        "eta_seconds": None,
    }
    finished = _parse_iso(report.finished_at)
    end = finished if (status != "running" and finished is not None) else now
    error = None
    if report.error_code or report.error_text:
        error = {"code": str(report.error_code or ""), "message": str(report.error_text or "")}
    result = None
    if status == "succeeded":
        result = {"verdict": report.verdict, "report_id": report.report_id}
    base.update(
        {
            "key": f"validate:{report.report_id}",
            "kind": "validate",
            "kind_label": KIND_LABELS["validate"],
            "source": "durable",
            "title": title,
            "book_id": base.get("book_id"),
            "target_id": report.report_id,
            "profile_id": report.profile_id,
            "status": status,
            "started_at": report.started_at or report.created_at,
            "updated_at": report.heartbeat_at or report.created_at,
            "elapsed_seconds": _seconds_between(report.started_at or report.created_at, end),
            "error": error,
            "result": result,
            "cancellable": False,
            "retryable": bool(report.retryable),
        }
    )
    if status != "running":
        base["percent"] = 100 if status == "succeeded" else base.get("percent", 0)
        base["phase"] = "done" if status == "succeeded" else "failed"
        base["phase_label"] = "完成" if status == "succeeded" else "失败"
        base["eta_seconds"] = None
    return base


def list_activity(session: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    recent_cutoff = (current - timedelta(seconds=RECENT_FINISHED_SECONDS)).isoformat()
    repo = StyleReferenceRepository(session)
    items: dict[str, dict[str, Any]] = {}
    for snap in list_operation_progress():
        entry = _entry_from_snapshot(snap)
        items[entry["key"]] = entry

    title_cache: dict[str, str | None] = {}

    def _title(book_id: str | None) -> str | None:
        if not book_id:
            return None
        if book_id not in title_cache:
            book = repo.get_book(book_id)
            title_cache[book_id] = book.title if book is not None else None
        return title_cache[book_id]

    # 作业表:活动作业 + 十分钟内结束的作业(分类作业另带兼容旧前端的别名条目)
    for job in StyleJobService(session).list_recent(finished_within_seconds=RECENT_FINISHED_SECONDS):
        book = repo.get_book(job.book_id) if job.book_id else None
        title = book.title if book is not None else None
        if job.kind == JOB_KIND_CLASSIFY:
            for entry in classification_activity_entries(
                job, title=title, total_chars=int(book.total_chars or 0) if book is not None else None
            ):
                items[str(entry["key"])] = entry
            continue
        entry = job_activity_entry(job, now=current)
        entry["title"] = title
        items[str(entry["key"])] = entry

    runs = session.scalars(
        select(StyleReferenceRun).where(
            (StyleReferenceRun.status == "running")
            | (StyleReferenceRun.finished_at >= recent_cutoff)
        )
    ).all()
    for run in runs:
        entry = run_activity_entry(run, title=_title(run.book_id), now=current)
        items[entry["key"]] = entry

    reports = session.scalars(
        select(StyleReferenceValidationReport).where(
            StyleReferenceValidationReport.status.in_(("queued", "running"))
            | (StyleReferenceValidationReport.finished_at >= recent_cutoff)
        )
    ).all()
    for report in reports:
        key = f"validate:{report.report_id}"
        registry_entry = items.get(key)
        book_id = registry_entry.get("book_id") if registry_entry else None
        if not book_id:
            profile = repo.get_profile(report.profile_id)
            book_id = profile.book_id if profile is not None else None
        entry = report_activity_entry(
            report,
            title=_title(book_id),
            registry_entry=registry_entry,
            now=current,
        )
        entry["book_id"] = book_id
        items[key] = entry

    def _active(entry: dict[str, Any]) -> bool:
        return entry.get("status") == "running" or entry.get("status") in ACTIVE_STATES

    ordered = list(items.values())
    # 在跑(含排队)的按开始时间倒序排前面,终态按更新时间倒序跟在后面
    running = sorted(
        (e for e in ordered if _active(e)),
        key=lambda e: str(e.get("started_at") or e.get("created_at") or ""),
        reverse=True,
    )
    finished = sorted(
        (e for e in ordered if not _active(e)),
        key=lambda e: str(e.get("updated_at") or e.get("finished_at") or e.get("started_at") or ""),
        reverse=True,
    )
    return (running + finished)[:MAX_ITEMS]


__all__ = [
    "LAYER_LABELS",
    "MAX_ITEMS",
    "RECENT_FINISHED_SECONDS",
    "SUB_DIM_LABELS",
    "list_activity",
    "report_activity_entry",
    "run_activity_entry",
]
