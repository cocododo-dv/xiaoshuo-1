"""参考书活动清单:风格参考模块所有在跑 / 刚结束的耗时操作,一份统一形状。

来源合成一份:
- **作业表**(``style_reference_jobs``,2026-09-23 v3):分类作业(导入 / 重新分类 / 就地重标类型)与学习文风作业
  (kind=learn,七步进度写在作业行上),条目由 ``jobs.job_activity_entry`` 给出(键 ``job:<id>``);分类作业另带一条
  兼容旧前端的别名条目(键 = 请求的幂等键,``compat_alias_of`` 指回 ``job:`` 条目),P7 随
  ``/imports/{key}/progress`` 一起删;
- 进程内登记簿(``import_progress``):还在用它的旧操作的阶段进度(P7 随模块一起删)——进程没了就没了。

对照检查(kind=check,2026-09-23 v3 P5b)也在作业表上,条目同形;旧回测(``style_reference_validation_reports``)
随旧校验层删除,不再列出(旧表由 P7 的清理迁移删)。旧的抽取 run 不再有自己的进度来源(抽取是学习作业的一步;
run 行只作血缘),不再单列。终态条目只保留最近 ``RECENT_FINISHED_SECONDS``,让前端的最后几次轮询读到结果。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.style_reference.import_job import classification_activity_entries
from novel_system.services.style_reference.import_progress import (
    KIND_LABELS,
    list_operation_progress,
)
from novel_system.services.style_reference.jobs import (
    ACTIVE_STATES,
    JOB_KIND_CLASSIFY,
    JOB_KIND_LEARN,
    StyleJobService,
    job_activity_entry,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository

RECENT_FINISHED_SECONDS = 600
MAX_ITEMS = 50


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


def list_activity(session: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    repo = StyleReferenceRepository(session)
    items: dict[str, dict[str, Any]] = {}
    for snap in list_operation_progress():
        entry = _entry_from_snapshot(snap)
        items[entry["key"]] = entry

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
        if job.kind == JOB_KIND_LEARN:
            entry["phases_done"] = list(dict(job.cursor_json or {}).get("phases_done") or [])
        items[str(entry["key"])] = entry

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
    "MAX_ITEMS",
    "RECENT_FINISHED_SECONDS",
    "list_activity",
]
