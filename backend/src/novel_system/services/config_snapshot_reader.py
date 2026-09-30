"""Read-only runtime configuration access.

Parsers and provider services depend on this small query boundary instead of
depending on the mutation-heavy ``system_config`` service that imports them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import Text, select, type_coerce
from sqlalchemy.exc import SQLAlchemyError

from novel_system.db.models import SystemConfigSnapshot
from novel_system.db.session import SessionLocal
from novel_system.services.config_cache import ContentKeyedCache
from novel_system.services.database_errors import is_database_busy_error


_TRANSIENT_DB_RETRY_DELAYS = (0.05, 0.15)

T = TypeVar("T")


def read_with_transient_retry(reader, *, sleep: Callable[[float], None] = time.sleep):
    """Retry only SQLite busy errors and never turn a DB failure into absence."""

    for delay in (*_TRANSIENT_DB_RETRY_DELAYS, None):
        try:
            return reader()
        except SQLAlchemyError as exc:
            if not is_database_busy_error(exc) or delay is None:
                raise
            sleep(delay)
    raise AssertionError("unreachable")


def _active_snapshot_value(session, category: str, column):
    """活动快照的一列（同一类别有多条活动行时取版本最高、最新的那条）；没有活动快照 → ``None`` 行。"""
    return session.execute(
        select(column)
        .where(SystemConfigSnapshot.category == category, SystemConfigSnapshot.active_flag == 1)
        .order_by(SystemConfigSnapshot.version.desc(), SystemConfigSnapshot.created_at.desc())
        .limit(1)
    ).first()


def active_snapshot(session, category: str) -> SystemConfigSnapshot | None:
    """活动快照这一行（同一类别有多条活动行时取版本最高、最新的那条）；没有 → ``None``。"""
    return session.execute(
        select(SystemConfigSnapshot)
        .where(SystemConfigSnapshot.category == category, SystemConfigSnapshot.active_flag == 1)
        .order_by(SystemConfigSnapshot.version.desc(), SystemConfigSnapshot.created_at.desc())
    ).scalars().first()


def active_config_payload(session, category: str) -> dict[str, Any] | None:
    """在调用方的会话里读活动快照的 ``parsed_json``（新 dict）；没有活动快照 → ``None``。"""
    row = _active_snapshot_value(session, category, SystemConfigSnapshot.parsed_json)
    return None if row is None else dict(row[0] or {})


def load_active_config_payload(
    category: str,
    *,
    session_factory=SessionLocal,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    def _read():
        with session_factory() as session:
            return active_config_payload(session, category)

    return read_with_transient_retry(_read, sleep=sleep)


def load_active_config_parsed(
    category: str,
    parse: Callable[[dict[str, Any]], T],
    *,
    cache: ContentKeyedCache,
    key_extra: tuple = (),
    session_factory=SessionLocal,
    sleep: Callable[[float], None] = time.sleep,
) -> T | None:
    """活动快照的 ``parse(payload)``，按快照在库里存的原文记忆；没有活动快照 → ``None``。

    ``payload`` 与 ``load_active_config_payload`` 返回的完全相同。每次调用都读一次活动快照的
    ``parsed_json`` 原文（一条窄查询，不解码 JSON），原文变了（保存 / 切换 / 回滚快照、别的进程激活新快照、
    迁移就地改写）就重新解析——所以作者在系统配置里一保存，下一次读取就是新配置。
    ``parse`` 还依赖快照之外的内容（例如仓库里的配置文件）时，把那份内容放进 ``key_extra``。
    """

    def _read():
        with session_factory() as session:
            return _active_snapshot_value(
                session, category, type_coerce(SystemConfigSnapshot.parsed_json, Text)
            )

    row = read_with_transient_retry(_read, sleep=sleep)
    if row is None:
        return None
    stored = row[0]
    return cache.get_or_build(
        ("snapshot", category, stored, *key_extra),
        lambda: parse(_decode_stored_payload(stored)),
    )


def _decode_stored_payload(stored: str | bytes | None) -> dict[str, Any]:
    """库里 ``parsed_json`` 原文 → 与 ORM 读出来的 ``dict(parsed_json or {})`` 相同的 dict。"""
    value = json.loads(stored) if stored is not None else None
    return dict(value or {})
