"""参考书活动清单:风格参考模块所有在跑 / 刚结束的耗时操作,一份统一形状。

唯一来源是**作业表**(``style_reference_jobs``,2026-09-23 v3):分类作业(导入 / 重新分类 / 就地重标类型)、
学习文风作业(kind=learn,七步进度写在作业行上)与对照检查作业(kind=check),条目由 ``jobs.job_activity_entry``
给出(键 ``job:<id>``;分类作业另带书名、分类方式与段数 / 字数,学习作业另带书名与做完的步骤)。作业行持久,
重启之后照样列得出来;抽取 run 行只作血缘,不单列。终态条目只保留最近 ``RECENT_FINISHED_SECONDS``,让前端的
最后几次轮询读到结果——这个窗口是 ``jobs`` 的那一个常量:作业表保留期清理(``cleanup.prune_style_jobs``)不删
「还在活动面板上」的作业,读的也是它,两边不会各调各的。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBook

from novel_system.services.style_reference.import_job import classification_activity_entry
from novel_system.services.style_reference.jobs import (
    ACTIVE_STATES,
    JOB_KIND_CLASSIFY,
    JOB_KIND_LEARN,
    RECENT_FINISHED_SECONDS,
    StyleJobService,
    job_activity_entry,
)

MAX_ITEMS = 50


def _book_briefs(session: Session, book_ids: set[str]) -> dict[str, tuple[str | None, int]]:
    """清单里各书的书名与字数，一条 SQL（原来每个作业查一次书行，连同整份 stats_json，B10-18）。"""
    if not book_ids:
        return {}
    rows = session.execute(
        select(StyleReferenceBook.book_id, StyleReferenceBook.title, StyleReferenceBook.total_chars).where(
            StyleReferenceBook.book_id.in_(sorted(book_ids))
        )
    )
    return {str(book_id): (title, int(total_chars or 0)) for book_id, title, total_chars in rows}


def list_activity(session: Session, *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or datetime.now(timezone.utc)
    items: dict[str, dict[str, Any]] = {}

    # 作业表:活动作业 + 十分钟内结束的作业
    jobs = StyleJobService(session).list_recent(finished_within_seconds=RECENT_FINISHED_SECONDS)
    books = _book_briefs(session, {str(job.book_id) for job in jobs if job.book_id})
    for job in jobs:
        brief = books.get(str(job.book_id)) if job.book_id else None
        title = brief[0] if brief is not None else None
        if job.kind == JOB_KIND_CLASSIFY:
            entry = classification_activity_entry(
                job, title=title, total_chars=brief[1] if brief is not None else None
            )
        else:
            entry = job_activity_entry(job, now=current)
            entry["title"] = title
            if job.kind == JOB_KIND_LEARN:
                entry["phases_done"] = list(dict(job.cursor_json or {}).get("phases_done") or [])
        items[str(entry["key"])] = entry

    def _active(entry: dict[str, Any]) -> bool:
        return entry.get("status") in ACTIVE_STATES

    ordered = list(items.values())
    # 在跑(含排队)的按开始时间倒序排前面,终态按结束时间倒序跟在后面
    running = sorted(
        (e for e in ordered if _active(e)),
        key=lambda e: str(e.get("started_at") or e.get("created_at") or ""),
        reverse=True,
    )
    finished = sorted(
        (e for e in ordered if not _active(e)),
        key=lambda e: str(e.get("finished_at") or e.get("started_at") or e.get("created_at") or ""),
        reverse=True,
    )
    return (running + finished)[:MAX_ITEMS]


__all__ = [
    "MAX_ITEMS",
    "RECENT_FINISHED_SECONDS",
    "list_activity",
]
