"""成本看板「全局用量」读数：今日 / 本月 / 本作品今日的 token、今日请求数、正在飞的调用数。

2026-09-30 重评 R3（批准#3a）：以前这里是六道只能靠环境变量打开、默认全关的全局额度闸的读数；闸删了，读数留着。
载荷键不变（每一格 ``{used, limit, enforced}``，``limit`` 恒为 ``None``、``enforced`` 恒为 ``False``），
只是去掉了按环境变量单价算的「今日金额」与 ``any_enforced``。

口径：
- 用量按物理尝试的实际 ``total_tokens`` 算（供应商超出预留时照实报的那部分也算），只数已经结算的尝试——
  还在飞的调用不预支进用量，只计入「并发」；
- 请求数 = 今天已经结算、而且真的发出去了的物理尝试；
- 时间按 UTC 日 / 月，与账本的 ``created_at`` 同一时钟。
一条 SQL 聚合算完（``ix_llm_call_attempts_created`` 按时间窗过滤），不再把整月的尝试行读成 ORM 对象。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from novel_system.db.models import LlmCall, LlmCallAttempt

PERIOD_TIMEZONE = "UTC"


def _reading(used: int | None) -> dict[str, Any]:
    return {"used": used, "limit": None, "enforced": False}


def usage_readings(
    session: Session,
    *,
    project_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    day_start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    settled = LlmCallAttempt.accounting_status != "reserved"
    today = LlmCallAttempt.created_at >= day_start
    tokens = func.coalesce(LlmCallAttempt.total_tokens, 0)
    project_today = (
        func.sum(case((and_(settled, today, LlmCall.project_id == project_id), tokens), else_=0))
        if project_id
        else None
    )
    columns = [
        func.sum(case((and_(settled, today), tokens), else_=0)),
        func.sum(case((settled, tokens), else_=0)),
        func.sum(case((and_(settled, today, LlmCallAttempt.request_dispatched_at.is_not(None)), 1), else_=0)),
    ]
    query = select(*columns, *([project_today] if project_today is not None else [])).where(
        LlmCallAttempt.created_at >= month_start
    )
    if project_today is not None:
        query = query.join(LlmCall, LlmCall.llm_call_id == LlmCallAttempt.llm_call_id)
    row = session.execute(query).one()
    daily_tokens, monthly_tokens, daily_requests = (int(value or 0) for value in row[:3])
    project_tokens = int(row[3] or 0) if project_today is not None else None
    concurrent = int(
        session.scalar(
            select(func.count()).select_from(LlmCallAttempt).where(LlmCallAttempt.accounting_status == "reserved")
        )
        or 0
    )
    return {
        "period_timezone": PERIOD_TIMEZONE,
        "daily_tokens": _reading(daily_tokens),
        "monthly_tokens": _reading(monthly_tokens),
        "project_daily_tokens": {"project_id": project_id, **_reading(project_tokens)},
        "daily_requests": _reading(daily_requests),
        "concurrent_requests": _reading(concurrent),
    }


__all__ = ["PERIOD_TIMEZONE", "usage_readings"]
