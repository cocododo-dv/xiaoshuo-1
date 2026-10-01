"""``GET /ready`` 的数据库探针（从 ``api/app.py`` 搬出）。

每次探测都读 ``alembic_version``（一条语句），与代码认的 ``CURRENT_SCHEMA_REVISION`` 比；表 / 列结构检查要对
元数据里 70 多张表各发一条 ``PRAGMA table_info``（实库 25–140 ms），而启动脚本、E2E 通道与部署探针都在轮询
``/ready``——所以结构检查按库在进程里只做一次：某个库在某个版本上查过一次全齐，之后同一版本不再查；探测看到别的
版本（迁移了、或者库退回去了）就忘掉，回到这个版本时重查（X01-17）。查出缺表 / 缺列不记，每次都重查，修好了
下一次探测就就绪。

失败统一抛 ``DomainError("SERVICE_NOT_READY", 503)``，``details.reason`` ∈ ``database_probe_failed`` /
``schema_revision_mismatch``（库落后于代码）/ ``schema_revision_ahead``（库里记的迁移版本这份代码不认识：库比代码新）/
``schema_tables_missing`` / ``schema_columns_missing``。

进程启动时库结构跟不上代码，lifespan 把启动恢复与后台清扫推迟到这里（``defer_startup_until_schema_ready``）：原地升级
之后第一个看到结构跟上了的 ``/api/*`` 请求或 ``/ready`` 探测补跑一次。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from sqlalchemy import inspect as sqlalchemy_inspect, text

from novel_system.cache_registry import register_cache_reset
from novel_system.db import models  # noqa: F401 — 把全部表登记进 Base.metadata
from novel_system.db.base import Base
from novel_system.db.schema_contract import CURRENT_SCHEMA_REVISION, revision_unknown_to_code
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


def _readiness_error(*, log: bool) -> DomainError | None:
    """库就绪返回 ``None``；否则返回要抛的 ``SERVICE_NOT_READY``（``log`` 时同时记日志）。"""

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
        if log:
            logger.exception("Readiness database probe failed")
        error = DomainError(
            "SERVICE_NOT_READY",
            "database readiness probe failed",
            status_code=503,
            details={
                "retryable": True,
                "reason": "database_probe_failed",
                "expected_revision": SUPPORTED_DATABASE_REVISION,
            },
        )
        error.__cause__ = exc
        return error
    if revisions != (SUPPORTED_DATABASE_REVISION,):
        _remember_structure(database, None)
        current_revision = revisions[0] if len(revisions) == 1 else None
        # 库里记着这份代码不认识的迁移：库比代码新（代码回退了 / 库来自别的分支）。这时重启也没用——启动脚本的
        # ``alembic upgrade head`` 找不到那个版本，会直接失败——要换回与库匹配的代码，所以单独报（复核 P09b-R2）
        ahead = any(revision_unknown_to_code(revision) for revision in revisions)
        if log:
            logger.error(
                "Readiness schema revision %s expected=%s actual=%s",
                "ahead of the code" if ahead else "mismatch",
                SUPPORTED_DATABASE_REVISION,
                revisions,
            )
        return DomainError(
            "SERVICE_NOT_READY",
            SCHEMA_AHEAD_MESSAGE if ahead else "database schema revision is not ready",
            status_code=503,
            details={
                "retryable": False,
                "reason": "schema_revision_ahead" if ahead else "schema_revision_mismatch",
                "expected_revision": SUPPORTED_DATABASE_REVISION,
                "current_revision": current_revision,
            },
        )
    if structure is None:
        return None
    missing_tables, missing_required_columns = structure
    if missing_tables:
        if log:
            logger.error(
                "Readiness schema table check failed revision=%s missing_tables=%s",
                SUPPORTED_DATABASE_REVISION,
                missing_tables,
            )
        return DomainError(
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
        if log:
            logger.error(
                "Readiness schema column check failed revision=%s tables=%s columns=%s",
                SUPPORTED_DATABASE_REVISION,
                len(missing_required_columns),
                missing_column_count,
            )
        return DomainError(
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
    return None


def check_database_ready() -> None:
    """库就绪就返回（进程启动时推迟的启动这时补跑）；否则抛 ``SERVICE_NOT_READY``（503）。"""

    error = _readiness_error(log=True)
    if error is not None:
        raise error
    _run_deferred_startup()


# ---------------------------------------------------------------- API 的库结构闸（B12-19，批准 #28）
# 代码比库新（``--reload`` 在 ``alembic upgrade head`` 之前热加载了新模型）时，以前每个接口都各自报
# 「database operation failed」。现在 ``/api/*`` 统一回 503 ``SERVICE_NOT_READY``、一句中文说明与 ``details.reason``。
# 反过来库比代码新（代码回退到一个迁移之前）时重启升不了级，说明换成「换回与库匹配的代码」（复核 P09b-R2）。
SCHEMA_NOT_READY_REASONS = frozenset(
    {"schema_revision_mismatch", "schema_revision_ahead", "schema_tables_missing", "schema_columns_missing"}
)
SCHEMA_UPGRADE_MESSAGE = "数据库结构需要升级：请重启后端（启动脚本会自动升级）"
SCHEMA_AHEAD_MESSAGE = "数据库结构比这份代码新：请换回与数据库匹配的代码版本（重启不会让数据库降级）"

# 库的 URL：这个进程里已经确认结构跟得上代码，或者这个库根本不归 Alembic 管（没有 alembic_version 表，
# 例如测试用 create_all 建的库）——之后的请求不再查
_SCHEMA_GATE_OPEN: set[str] = set()
_SCHEMA_GATE_LOCK = threading.Lock()
# 结构落后时每个请求都会查一次（原地升级之后不用重启就放行，推迟的启动随之补跑）：同一个原因每个进程只记一次日志
_SCHEMA_GATE_LOGGED: set[tuple[str, str]] = set()


def _forget_schema_gate() -> None:
    with _SCHEMA_GATE_LOCK:
        _SCHEMA_GATE_OPEN.clear()
        _SCHEMA_GATE_LOGGED.clear()


register_cache_reset("api.readiness.schema_gate", _forget_schema_gate)


def schema_gate_open() -> bool:
    """这个进程已经确认当前库的结构跟得上代码（不查库；引擎还没建时为假）。"""

    with _SCHEMA_GATE_LOCK:
        if not _SCHEMA_GATE_OPEN:
            return False
    return str(engine().url) in _SCHEMA_GATE_OPEN


def schema_gate_error() -> DomainError | None:
    """库结构落后于代码时要回给 ``/api/*`` 的 503；结构跟得上（或库读不了——那不是结构问题，照常放行，
    让请求自己报它的错）时返回 ``None``。结构检查与 ``/ready`` 同一份（``_readiness_error``，按库与版本缓存）。"""

    runtime_engine = engine()
    database = str(runtime_engine.url)
    with _SCHEMA_GATE_LOCK:
        if database in _SCHEMA_GATE_OPEN:
            return None
    try:
        with runtime_engine.connect() as connection:
            managed = sqlalchemy_inspect(connection).has_table("alembic_version")
    except Exception:  # noqa: BLE001 — 库读不了不是结构问题
        return None
    if not managed:
        with _SCHEMA_GATE_LOCK:
            _SCHEMA_GATE_OPEN.add(database)
        return None
    error = _readiness_error(log=False)
    if error is None:
        # 先补跑推迟的启动、再放行：同时到的请求在 _run_deferred_startup 里等它跑完
        _run_deferred_startup()
        with _SCHEMA_GATE_LOCK:
            _SCHEMA_GATE_OPEN.add(database)
        return None
    reason = str(error.details.get("reason") or "")
    if reason not in SCHEMA_NOT_READY_REASONS:
        return None
    with _SCHEMA_GATE_LOCK:
        first = (database, reason) not in _SCHEMA_GATE_LOGGED
        _SCHEMA_GATE_LOGGED.add((database, reason))
    if first:
        logger.error("API schema gate closed: %s details=%s", reason, error.details)
    return DomainError(
        "SERVICE_NOT_READY",
        SCHEMA_AHEAD_MESSAGE if reason == "schema_revision_ahead" else SCHEMA_UPGRADE_MESSAGE,
        status_code=503,
        details=dict(error.details),
    )


# ---------------------------------------------------------------- 推迟的启动（复核 P09b-R1）
# 进程启动时库结构跟不上代码：lifespan 照样登记作业处理器（不碰库），但不拿旧结构去跑启动恢复与两条后台清扫，把它们
# 登记在这里。原地 ``alembic upgrade head`` 之后，第一个看到结构跟上了的 ``/api/*`` 请求（``schema_gate_error``）或
# ``/ready`` 探测（``check_database_ready``）补跑一次，同时到的请求等它跑完——以前闸门放行了，进程却既没有启动恢复、
# 也没有清扫线程，排队的风格作业没人派发。lifespan 结束时撤销还没跑的那一个。
_DEFERRED_STARTUP: Callable[[], None] | None = None
# 补跑期间一直拿着：别的线程（同时到的请求、lifespan 的撤销）在这里等它跑完。可重入：补跑的启动万一在同一个线程里
# 又走到这里，只会看到已经取走的空位，不会自己等自己
_DEFERRED_STARTUP_LOCK = threading.RLock()


def defer_startup_until_schema_ready(start: Callable[[], None]) -> None:
    """登记推迟的启动（lifespan 在库结构跟不上代码时调用；同一时间只有一个）。"""

    global _DEFERRED_STARTUP
    with _DEFERRED_STARTUP_LOCK:
        _DEFERRED_STARTUP = start


def cancel_deferred_startup() -> None:
    """撤销还没跑的推迟启动；正在补跑的等它跑完再返回（lifespan 随后照常停清扫线程）。"""

    global _DEFERRED_STARTUP
    with _DEFERRED_STARTUP_LOCK:
        _DEFERRED_STARTUP = None


def _run_deferred_startup() -> None:
    # 不在锁外先看一眼：取走之后、跑完之前那一段，同时到的请求也得在锁上等（只有闸门放行之前与 /ready 走到这里）
    global _DEFERRED_STARTUP
    with _DEFERRED_STARTUP_LOCK:
        start, _DEFERRED_STARTUP = _DEFERRED_STARTUP, None
        if start is None:
            return
        logger.warning(
            "database schema caught up with the code: running the deferred startup recovery and background sweepers"
        )
        try:
            start()
        except Exception:  # noqa: BLE001 — 在请求线程里补跑：失败只记日志，不让这个请求替它报 500
            logger.exception("deferred startup failed; restart the backend")


__all__ = [
    "SCHEMA_AHEAD_MESSAGE",
    "SCHEMA_NOT_READY_REASONS",
    "SCHEMA_UPGRADE_MESSAGE",
    "SUPPORTED_DATABASE_REVISION",
    "cancel_deferred_startup",
    "check_database_ready",
    "defer_startup_until_schema_ready",
    "schema_gate_error",
    "schema_gate_open",
]
