from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from novel_system.database_runtime import DEFAULT_DATABASE_PATH, load_database_runtime

_ENGINE = None
_SESSION_FACTORY = None


def _running_under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _sqlite_file(database_url: str) -> Path | None:
    """SQLite URL 指向的文件(解析相对路径);内存库 / 非 SQLite / 解析不了时返回 None。"""
    if not database_url.startswith("sqlite"):
        return None
    try:
        database = make_url(database_url).database
    except Exception:  # noqa: BLE001 — URL 解析失败交给 create_engine 报
        return None
    if not database or database == ":memory:" or database.startswith("file:"):
        return None
    path = Path(database)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return path.resolve()
    except OSError:
        return None


def refuse_repository_database_under_pytest(database_url: str) -> None:
    """测试进程里拒绝连仓库自己的 ``backend/novel_system.db``(作者的实库)。

    2026-09-06 一次测试运行在环境变量还没指向临时库时就建了引擎,把 22 本夹具参考书写进了实库。
    测试夹具(``tests/conftest.py``)给每个用例一个 ``tmp_path`` 下的临时库;任何在那之前或绕过它
    建引擎的代码在这里直接失败,而不是悄悄写实库。
    """
    if not _running_under_pytest():
        return
    target = _sqlite_file(database_url)
    if target is None:
        return
    try:
        live = DEFAULT_DATABASE_PATH.resolve()
    except OSError:
        return
    # 本检出的实库;或别的检出的实库(同名 novel_system.db,不在临时目录里)——在 git worktree 里跑测试时
    # 把 NOVEL_SYSTEM_DATABASE_URL 指到主检出的实库,比较「本检出的路径」拦不住
    if target == live or (target.name == live.name and not _inside_temp_dir(target)):
        raise RuntimeError(
            "refusing to open the repository database backend/novel_system.db from a pytest run; "
            "tests must use the per-test temporary database (tests/conftest.py isolated_database)"
        )


def _inside_temp_dir(path: Path) -> bool:
    try:
        temp_root = Path(tempfile.gettempdir()).resolve()
    except OSError:
        return False
    return path == temp_root or temp_root in path.parents


def engine():
    global _ENGINE
    if _ENGINE is None:
        database_runtime = load_database_runtime()
        refuse_repository_database_under_pytest(database_runtime.database_url)
        is_sqlite = database_runtime.database_url.startswith("sqlite")
        connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
        _ENGINE = create_engine(
            database_runtime.database_url,
            connect_args=connect_args,
            future=True,
        )
        if is_sqlite:
            _install_sqlite_pragmas(
                _ENGINE,
                enforce_foreign_keys=database_runtime.sqlite_foreign_keys_enabled,
            )
    return _ENGINE


def _configure_sqlite_connection(
    dbapi_connection,
    *,
    enforce_foreign_keys: bool,
) -> None:
    original_autocommit = getattr(dbapi_connection, "autocommit", None)
    if original_autocommit is not None:
        # SQLite ignores PRAGMA foreign_keys changes inside a transaction.
        # Temporarily enter autocommit while configuring this new connection,
        # then restore the PEP-249 mode requested by ``engine()``.
        dbapi_connection.autocommit = True
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(
            "PRAGMA foreign_keys=ON"
            if enforce_foreign_keys
            else "PRAGMA foreign_keys=OFF"
        )
        cursor.execute("PRAGMA foreign_keys")
        row = cursor.fetchone()
        actual = int(row[0]) if row else -1
        expected = 1 if enforce_foreign_keys else 0
        if actual != expected:
            mode = "enabled" if enforce_foreign_keys else "disabled"
            raise RuntimeError(
                f"sqlite_foreign_keys_not_{mode}: expected={expected}, actual={actual}"
            )
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()
        if original_autocommit is not None:
            dbapi_connection.autocommit = original_autocommit


def _install_sqlite_pragmas(
    sqlalchemy_engine,
    *,
    enforce_foreign_keys: bool = True,
) -> None:
    @event.listens_for(sqlalchemy_engine, "connect")
    def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        _configure_sqlite_connection(
            dbapi_connection,
            enforce_foreign_keys=enforce_foreign_keys,
        )

    if enforce_foreign_keys:

        @event.listens_for(sqlalchemy_engine, "begin")
        def defer_sqlite_foreign_keys(sqlalchemy_connection) -> None:
            _configure_sqlite_transaction(sqlalchemy_connection)


def _install_sqlite_session_pragmas(
    session_maker,
    *,
    enforce_foreign_keys: bool = True,
) -> None:
    """Reapply deferred FKs at ORM transaction and flush boundaries.

    Python 3.12's default SQLite legacy transaction control can reuse a DBAPI
    connection after ``Session.commit()`` without reliably producing a second
    engine-level ``begin`` event.  The ORM ``after_begin`` event is the stable
    transaction boundary in that case.  Keeping both hooks also covers direct
    ``Connection`` users and ORM users.  Before an ORM flush we explicitly open
    a deferred DBAPI transaction when the legacy driver has not opened one yet;
    this prevents its first DML statement from resetting the pragma.  Read-only
    sessions remain in legacy mode, preserving the project's explicit
    ``BEGIN IMMEDIATE`` accounting lock semantics and short-lived read behavior.
    """

    if not enforce_foreign_keys:
        return

    @event.listens_for(session_maker, "after_begin")
    def defer_sqlite_session_foreign_keys(
        _session,
        _transaction,
        sqlalchemy_connection,
    ) -> None:
        if sqlalchemy_connection.dialect.name == "sqlite":
            _configure_sqlite_transaction(sqlalchemy_connection)

    @event.listens_for(session_maker, "before_flush")
    def begin_sqlite_flush_transaction(
        orm_session,
        _flush_context,
        _instances,
    ) -> None:
        sqlalchemy_connection = orm_session.connection()
        if sqlalchemy_connection.dialect.name != "sqlite":
            return
        dbapi_connection = sqlalchemy_connection.connection.dbapi_connection
        if not dbapi_connection.in_transaction:
            sqlalchemy_connection.exec_driver_sql("BEGIN")
        _configure_sqlite_transaction(sqlalchemy_connection)


def _configure_sqlite_transaction(sqlalchemy_connection) -> None:
    """Defer FK checks until commit for every SQLite transaction.

    The model layer intentionally has few ORM relationships, so SQLAlchemy
    cannot always topologically order a valid parent/child graph added in one
    unit of work.  SQLite resets ``defer_foreign_keys`` after each commit or
    rollback; the engine ``begin`` hook therefore must set and verify it for
    every transaction while keeping enforcement itself enabled.
    """

    foreign_keys = sqlalchemy_connection.exec_driver_sql(
        "PRAGMA foreign_keys"
    ).scalar_one()
    if int(foreign_keys) != 1:
        raise RuntimeError(
            f"sqlite_foreign_keys_not_enabled_at_transaction_begin: actual={foreign_keys}"
        )
    sqlalchemy_connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
    deferred = sqlalchemy_connection.exec_driver_sql(
        "PRAGMA defer_foreign_keys"
    ).scalar_one()
    if int(deferred) != 1:
        raise RuntimeError(
            f"sqlite_defer_foreign_keys_not_enabled: expected=1, actual={deferred}"
        )


def session_factory():
    global _SESSION_FACTORY
    if _SESSION_FACTORY is None:
        runtime_engine = engine()
        _SESSION_FACTORY = sessionmaker(
            bind=runtime_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        if runtime_engine.dialect.name == "sqlite":
            _install_sqlite_session_pragmas(
                _SESSION_FACTORY,
                enforce_foreign_keys=load_database_runtime().sqlite_foreign_keys_enabled,
            )
    return _SESSION_FACTORY


def SessionLocal() -> Session:
    return session_factory()()


def reset_engine() -> None:
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is not None:
        _ENGINE.dispose()
    _ENGINE = None
    _SESSION_FACTORY = None


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
