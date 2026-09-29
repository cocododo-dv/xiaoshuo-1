"""``GET /ready`` 的数据库探针（从 ``api/app.py`` 搬出）。

每次探测都读 ``alembic_version``（一条语句），与代码认的 ``CURRENT_SCHEMA_REVISION`` 比；表 / 列结构检查要对
元数据里 70 多张表各发一条 ``PRAGMA table_info``（实库 25–140 ms），而启动脚本、E2E 通道与部署探针都在轮询
``/ready``——所以结构检查按库在进程里只做一次：某个库在某个版本上查过一次全齐，之后同一版本不再查；探测看到别的
版本（迁移了、或者库退回去了）就忘掉，回到这个版本时重查（X01-17）。查出缺表 / 缺列不记，每次都重查，修好了
下一次探测就就绪。

失败统一抛 ``DomainError("SERVICE_NOT_READY", 503)``，``details.reason`` ∈ ``database_probe_failed`` /
``schema_revision_mismatch`` / ``schema_tables_missing`` / ``schema_columns_missing``。
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy import inspect as sqlalchemy_inspect, text

from novel_system.cache_registry import register_cache_reset
from novel_system.db import models  # noqa: F401 — 把全部表登记进 Base.metadata
from novel_system.db.base import Base
from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION
from novel_system.db.session import engine
from novel_system.services.errors import DomainError

logger = logging.getLogger(__name__)
SUPPORTED_DATABASE_REVISION = CURRENT_SCHEMA_REVISION

# 库的 URL → 最近一次查过结构全齐时的迁移版本
_VERIFIED_STRUCTURE: dict[str, str] = {}
_VERIFIED_LOCK = threading.Lock()


def _forget_verified_structures() -> None:
    with _VERIFIED_LOCK:
        _VERIFIED_STRUCTURE.clear()


register_cache_reset("api.readiness.verified_structure", _forget_verified_structures)


def _structure_verified(database: str, revision: str) -> bool:
    with _VERIFIED_LOCK:
        return _VERIFIED_STRUCTURE.get(database) == revision


def _remember_structure(database: str, revision: str | None) -> None:
    with _VERIFIED_LOCK:
        if revision is None:
            _VERIFIED_STRUCTURE.pop(database, None)
        else:
            _VERIFIED_STRUCTURE[database] = revision


def _missing_structure(connection) -> tuple[list[str], dict[str, list[str]]]:
    """元数据里有、库里没有的表；库里有的表缺的列（按表）。"""

    inspector = sqlalchemy_inspect(connection)
    available_tables = set(inspector.get_table_names())
    missing_columns: dict[str, list[str]] = {}
    for table_name, table in Base.metadata.tables.items():
        if table_name not in available_tables:
            continue
        present = {str(column["name"]) for column in inspector.get_columns(table_name)}
        absent = sorted({column.name for column in table.columns} - present)
        if absent:
            missing_columns[table_name] = absent
    return sorted(set(Base.metadata.tables) - available_tables), missing_columns


def check_database_ready() -> None:
    """库就绪就返回；否则抛 ``SERVICE_NOT_READY``（503）。"""

    runtime_engine = engine()
    database = str(runtime_engine.url)
    structure: tuple[list[str], dict[str, list[str]]] | None = None
    try:
        with runtime_engine.connect() as connection:
            revisions = tuple(
                str(value)
                for value in connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
                if value
            )
            if revisions == (SUPPORTED_DATABASE_REVISION,) and not _structure_verified(
                database, SUPPORTED_DATABASE_REVISION
            ):
                structure = _missing_structure(connection)
    except Exception as exc:
        logger.exception("Readiness database probe failed")
        raise DomainError(
            "SERVICE_NOT_READY",
            "database readiness probe failed",
            status_code=503,
            details={
                "retryable": True,
                "reason": "database_probe_failed",
                "expected_revision": SUPPORTED_DATABASE_REVISION,
            },
        ) from exc
    if revisions != (SUPPORTED_DATABASE_REVISION,):
        _remember_structure(database, None)
        current_revision = revisions[0] if len(revisions) == 1 else None
        logger.error(
            "Readiness schema revision mismatch expected=%s actual=%s",
            SUPPORTED_DATABASE_REVISION,
            revisions,
        )
        raise DomainError(
            "SERVICE_NOT_READY",
            "database schema revision is not ready",
            status_code=503,
            details={
                "retryable": False,
                "reason": "schema_revision_mismatch",
                "expected_revision": SUPPORTED_DATABASE_REVISION,
                "current_revision": current_revision,
            },
        )
    if structure is None:
        return
    missing_tables, missing_required_columns = structure
    if missing_tables:
        logger.error(
            "Readiness schema table check failed revision=%s missing_tables=%s",
            SUPPORTED_DATABASE_REVISION,
            missing_tables,
        )
        raise DomainError(
            "SERVICE_NOT_READY",
            "database schema is incomplete",
            status_code=503,
            details={
                "retryable": False,
                "reason": "schema_tables_missing",
                "expected_revision": SUPPORTED_DATABASE_REVISION,
                "missing_table_count": len(missing_tables),
            },
        )
    if missing_required_columns:
        missing_column_count = sum(len(columns) for columns in missing_required_columns.values())
        logger.error(
            "Readiness schema column check failed revision=%s tables=%s columns=%s",
            SUPPORTED_DATABASE_REVISION,
            len(missing_required_columns),
            missing_column_count,
        )
        raise DomainError(
            "SERVICE_NOT_READY",
            "database schema is incomplete",
            status_code=503,
            details={
                "retryable": False,
                "reason": "schema_columns_missing",
                "expected_revision": SUPPORTED_DATABASE_REVISION,
                "missing_table_count": len(missing_required_columns),
                "missing_column_count": missing_column_count,
            },
        )
    _remember_structure(database, SUPPORTED_DATABASE_REVISION)


__all__ = ["SUPPORTED_DATABASE_REVISION", "check_database_ready"]
