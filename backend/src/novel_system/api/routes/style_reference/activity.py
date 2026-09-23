"""参考书活动清单(作业表 + 登记簿 + durable 行合成一份)与旧导入进度的兼容别名。"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from novel_system.api.deps import get_session
from novel_system.api.response import ok
from novel_system.api.routes.style_reference._common import PATH_PREFIX, ROUTE_TAGS, req_id
from novel_system.db.models import utcnow
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.activity import list_activity
from novel_system.services.style_reference.import_job import (
    find_job_by_op_key,
    legacy_progress_snapshot,
)
from novel_system.services.style_reference.import_progress import (
    IMPORT_KEY_MAX_LENGTH,
    get_import_progress,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository

router = APIRouter(tags=ROUTE_TAGS)


@router.get(f"{PATH_PREFIX}/activity")
def get_activity(request: Request, session: Session = Depends(get_session)):
    """参考书活动清单:模块里所有在跑 / 十分钟内结束的耗时操作,统一形状。

    作业表(分类作业;键 ``job:<id>``,另带兼容旧前端的幂等键别名)+ 进程内登记簿(合成画像 /
    应用画像建索引 / 回测阶段)+ durable 行(抽取 run、回测报告)合成一份;见
    ``services/style_reference/activity.py``。
    """
    return ok(
        {"items": list_activity(session), "server_time": utcnow()},
        req_id=req_id(request),
    )


_IMPORT_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,%d}$" % IMPORT_KEY_MAX_LENGTH)


@router.get(f"{PATH_PREFIX}/imports/{{import_key}}/progress")
def get_import_progress_route(import_key: str, request: Request, session: Session = Depends(get_session)):
    """兼容别名(旧前端导入轮询用,P7 删除):按幂等键找分类作业(``op_key``),返回旧的进度快照形状;
    作业表里没有时退回进程内登记簿(合成画像等尚未迁出的操作)。都不认识 → 404,前端把它当
    「尚未登记」继续等 POST 的结果。"""
    if not _IMPORT_KEY_RE.match(import_key or ""):
        raise DomainError(
            "STYLE_REFERENCE_IMPORT_PROGRESS_UNKNOWN",
            "import key is not a valid idempotency key",
            status_code=404,
        )
    job = find_job_by_op_key(session, import_key)
    if job is not None:
        book = StyleReferenceRepository(session).get_book(job.book_id) if job.book_id else None
        snapshot = legacy_progress_snapshot(
            job,
            title=book.title if book is not None else None,
            total_chars=int(book.total_chars or 0) if book is not None else None,
        )
        return ok({"progress": snapshot}, req_id=req_id(request))
    snapshot = get_import_progress(import_key)
    if snapshot is None:
        raise DomainError(
            "STYLE_REFERENCE_IMPORT_PROGRESS_UNKNOWN",
            "no import with this key is known",
            status_code=404,
        )
    return ok({"progress": snapshot}, req_id=req_id(request))
