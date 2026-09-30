"""各模型模块共用的时间戳工厂（``utcnow``；``from novel_system.db.models import utcnow`` 也取得到）。"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

_utcnow_lock = threading.Lock()
_utcnow_last = datetime.min.replace(tzinfo=UTC)


def utcnow() -> str:
    """进程内严格单调的 UTC ISO 时间戳。

    Windows 时钟粒度粗，连续插入常落入同一 tick，按 created_at 排序会
    退化为随机主键序；同 tick 时微秒 +1 兜底，保证排序确定。
    """
    global _utcnow_last
    with _utcnow_lock:
        now = datetime.now(UTC)
        if now <= _utcnow_last:
            now = _utcnow_last + timedelta(microseconds=1)
        _utcnow_last = now
        return now.isoformat()


__all__ = ["utcnow"]
