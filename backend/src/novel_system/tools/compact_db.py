"""部署时一次性压缩数据库：清掉过期的幂等重放缓存 → 切到增量自动回收 → VACUUM → 完整性与外键检查 → 截断 WAL。

为什么（批准#23 / 重评 R14）：真实库 437 MB 里有 413 MB 是「防重复提交」的重放缓存——每次构思自动保存都把
整个工作区原样存了一份，只在同一次点击的网络重试时才用得上。后端现在按 72 小时保留期定时清理
（``services/idempotency.purge_expired_idempotency``），但 SQLite 删掉的行只把页放回空闲表，文件不会变小，
每次备份照样搬这些空页；这个工具在部署时把库压回实际大小，并把 ``auto_vacuum`` 切成 INCREMENTAL，
之后每次定时清理的 ``PRAGMA incremental_vacuum`` 才能把腾出的页还给文件系统。

用法（服务全部停掉、``db_backup --backup`` 做完一份校验过的备份之后）：

    python -m novel_system.tools.compact_db PATH            # 只看：会清掉多少行、库现在多大（默认）
    python -m novel_system.tools.compact_db PATH --execute  # 真做

``PATH`` 可以是文件路径或 ``sqlite:///`` URL，必须显式给出（不读应用设置，免得压错库）。
库还在被使用时拒绝：用 ``db_backup`` 同一种探测——``wal_checkpoint(TRUNCATE)`` 忙就是有人在用；不靠
``-wal`` / ``-shm`` 文件在不在判断（崩溃后它们会留着，正常停机后会消失）。VACUUM 期间需要约一份库大小的
空闲磁盘。压缩前的备份按部署步骤永久留档：清掉的重放缓存里有一些构思草稿的中间状态只存在于那里。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from novel_system.db.models import IdempotencyKey
from novel_system.services.idempotency import (
    IDEMPOTENCY_REPLAY_RETENTION_HOURS,
    expired_idempotency_condition,
    purge_expired_idempotency,
    utcnow,
)
from novel_system.tools.db_backup import _database_checks, _file_path

BUSY_TIMEOUT_SECONDS = 0.2
_AUTO_VACUUM_MODES = {0: "none", 1: "full", 2: "incremental"}


def _resolve(path: str) -> str:
    database = _file_path(path, label="database")
    if not os.path.isfile(database):
        raise FileNotFoundError(database)
    return database


def _assert_not_in_use(database: str) -> None:
    try:
        connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            connection.close()
    except sqlite3.OperationalError as exc:
        raise RuntimeError("database is in use; stop the backend and every other user before compacting") from exc
    if row and int(row[0]) != 0:
        raise RuntimeError("database is in use (WAL checkpoint busy); stop the backend and every other user before compacting")


def _file_stats(database: str) -> dict[str, Any]:
    connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        def pragma(name: str) -> int:
            return int(connection.execute(f"PRAGMA {name}").fetchone()[0])

        auto_vacuum = pragma("auto_vacuum")
        stats = {
            "page_size": pragma("page_size"),
            "page_count": pragma("page_count"),
            "freelist_count": pragma("freelist_count"),
            "auto_vacuum": _AUTO_VACUUM_MODES.get(auto_vacuum, str(auto_vacuum)),
        }
    finally:
        connection.close()
    wal = database + "-wal"
    return {
        "file_bytes": os.path.getsize(database),
        "wal_bytes": os.path.getsize(wal) if os.path.exists(wal) else 0,
        **stats,
    }


def _has_idempotency_table(database: str) -> bool:
    connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (IdempotencyKey.__tablename__,)
        ).fetchone() is not None
    finally:
        connection.close()


def _engine(database: str):
    return create_engine(f"sqlite:///{database}", connect_args={"timeout": BUSY_TIMEOUT_SECONDS})


def _replay_counts(database: str, now: datetime) -> dict[str, int]:
    engine = _engine(database)
    try:
        with Session(engine) as session:
            total = session.scalar(select(func.count()).select_from(IdempotencyKey)) or 0
            expired = session.scalar(
                select(func.count()).select_from(IdempotencyKey).where(expired_idempotency_condition(now))
            ) or 0
    finally:
        engine.dispose()
    return {"replay_rows": int(total), "replay_rows_expired": int(expired)}


def plan(path: str, *, now: datetime | None = None) -> dict[str, Any]:
    """只看不改：库现在多大、重放缓存有多少行、按保留期会清掉多少行。"""
    database = _resolve(path)
    current = now or utcnow()
    has_table = _has_idempotency_table(database)
    counts = _replay_counts(database, current) if has_table else {"replay_rows": 0, "replay_rows_expired": 0}
    return {
        "database": database,
        "mode": "dry-run",
        "retention_hours": IDEMPOTENCY_REPLAY_RETENTION_HOURS,
        "idempotency_table": has_table,
        **counts,
        "stats": _file_stats(database),
    }


def compact(path: str, *, now: datetime | None = None) -> dict[str, Any]:
    """真做：清理 → ``auto_vacuum=INCREMENTAL`` → VACUUM → 完整性 + 外键检查 → ``wal_checkpoint(TRUNCATE)``。"""
    database = _resolve(path)
    _assert_not_in_use(database)
    current = now or utcnow()
    before = _file_stats(database)
    purged = 0
    if _has_idempotency_table(database):
        engine = _engine(database)
        try:
            with Session(engine) as session:
                purged = purge_expired_idempotency(session, current)
                session.commit()
        finally:
            engine.dispose()
    connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("VACUUM")
    finally:
        connection.close()
    checks = _database_checks(database)
    if checks["integrity"] != "ok" or checks["foreign_key_violations"]:
        raise RuntimeError(
            f"compacted database failed its checks ({checks}); restore the backup taken before compaction"
        )
    connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    try:
        row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        connection.close()
    if row and int(row[0]) != 0:
        raise RuntimeError("WAL checkpoint was busy after compaction; stop every database user and run the tool again")
    return {
        "database": database,
        "mode": "execute",
        "retention_hours": IDEMPOTENCY_REPLAY_RETENTION_HOURS,
        "replay_rows_purged": purged,
        "integrity": checks["integrity"],
        "foreign_key_violations": checks["foreign_key_violations"],
        "alembic_revisions": checks["alembic_revisions"],
        "before": before,
        "after": _file_stats(database),
    }


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="部署时压缩数据库（服务停掉、备份做完之后）")
    parser.add_argument("database", help="SQLite 文件路径或 sqlite:/// URL")
    parser.add_argument("--execute", action="store_true", help="真做；默认只报告会清掉多少行、库现在多大")
    args = parser.parse_args(argv)
    try:
        result = compact(args.database) if args.execute else plan(args.database)
    except Exception as exc:  # noqa: BLE001 — CLI 边界统一转退出码
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
