from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import ForeignKey, String, create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from novel_system.db import session as db_session
from novel_system.db.models import StoryProject
from novel_system.settings import get_settings


def test_runtime_engine_enables_sqlite_foreign_keys_by_default() -> None:
    with db_session.engine().connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [(True, 1), (False, 0)],
)
def test_sqlite_connection_policy_is_explicit_and_verified(enabled, expected) -> None:
    test_engine = create_engine("sqlite:///:memory:", future=True)
    db_session._install_sqlite_pragmas(
        test_engine,
        enforce_foreign_keys=enabled,
    )
    try:
        with test_engine.connect() as connection:
            assert (
                connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
                == expected
            )
    finally:
        test_engine.dispose()


def test_foreign_key_enablement_fails_closed_when_sqlite_does_not_accept_it() -> None:
    class RefusingCursor:
        def execute(self, _statement):
            return self

        @staticmethod
        def fetchone():
            return (0,)

        @staticmethod
        def close() -> None:
            return None

    class RefusingConnection:
        @staticmethod
        def cursor():
            return RefusingCursor()

    with pytest.raises(RuntimeError, match="sqlite_foreign_keys_not_enabled"):
        db_session._configure_sqlite_connection(
            RefusingConnection(),
            enforce_foreign_keys=True,
        )


def test_runtime_foreign_key_policy_blocks_orphan_inserts() -> None:
    raw_connection = sqlite3.connect(":memory:")
    try:
        db_session._configure_sqlite_connection(
            raw_connection,
            enforce_foreign_keys=True,
        )
        raw_connection.executescript(
            """
            CREATE TABLE parent_rows (parent_id TEXT PRIMARY KEY);
            CREATE TABLE child_rows (
                child_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES parent_rows(parent_id)
            );
            """
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"
        ):
            raw_connection.execute(
                "INSERT INTO child_rows VALUES ('child', 'missing-parent')"
            )
    finally:
        raw_connection.close()


def _make_deferred_fk_engine():
    test_engine = create_engine("sqlite:///:memory:", future=True)
    db_session._install_sqlite_pragmas(
        test_engine,
        enforce_foreign_keys=True,
    )
    with test_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE parent_rows (parent_id TEXT PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE child_rows (
                child_id TEXT PRIMARY KEY,
                parent_id TEXT NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES parent_rows(parent_id)
            )
            """
        )
    return test_engine


def test_same_transaction_child_before_parent_is_validated_at_commit() -> None:
    test_engine = _make_deferred_fk_engine()
    try:
        with test_engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert (
                connection.exec_driver_sql("PRAGMA defer_foreign_keys").scalar_one()
                == 1
            )
            connection.exec_driver_sql(
                "INSERT INTO child_rows VALUES ('child', 'parent')"
            )
            connection.exec_driver_sql("INSERT INTO parent_rows VALUES ('parent')")
        with test_engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM child_rows"
                ).scalar_one()
                == 1
            )
    finally:
        test_engine.dispose()


def test_missing_parent_still_fails_when_deferred_transaction_commits() -> None:
    test_engine = _make_deferred_fk_engine()
    try:
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            with test_engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO child_rows VALUES ('orphan', 'missing-parent')"
                )
    finally:
        test_engine.dispose()


def test_deferred_fk_is_reenabled_after_commit_and_rollback() -> None:
    test_engine = _make_deferred_fk_engine()
    try:
        with test_engine.connect() as connection:
            first = connection.begin()
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert (
                connection.exec_driver_sql("PRAGMA defer_foreign_keys").scalar_one()
                == 1
            )
            connection.exec_driver_sql("INSERT INTO parent_rows VALUES ('committed')")
            first.commit()

            second = connection.begin()
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert (
                connection.exec_driver_sql("PRAGMA defer_foreign_keys").scalar_one()
                == 1
            )
            connection.exec_driver_sql("INSERT INTO parent_rows VALUES ('rolled-back')")
            second.rollback()

            third = connection.begin()
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert (
                connection.exec_driver_sql("PRAGMA defer_foreign_keys").scalar_one()
                == 1
            )
            connection.exec_driver_sql(
                "INSERT INTO child_rows VALUES ('third-child', 'third-parent')"
            )
            connection.exec_driver_sql(
                "INSERT INTO parent_rows VALUES ('third-parent')"
            )
            third.commit()
    finally:
        test_engine.dispose()


def test_reused_orm_session_reenables_deferred_foreign_keys_after_commit() -> None:
    test_engine = _make_deferred_fk_engine()
    session_maker = sessionmaker(bind=test_engine, expire_on_commit=False)
    db_session._install_sqlite_session_pragmas(session_maker)
    try:
        with session_maker() as session:
            session.execute(
                text("INSERT INTO parent_rows VALUES ('first-parent')")
            )
            session.commit()

            connection = session.connection()
            assert connection.exec_driver_sql(
                "PRAGMA defer_foreign_keys"
            ).scalar_one() == 1
            session.execute(
                text("INSERT INTO child_rows VALUES ('second-child', 'second-parent')")
            )
            session.execute(
                text("INSERT INTO parent_rows VALUES ('second-parent')")
            )
            session.commit()
    finally:
        test_engine.dispose()


def test_emergency_switch_defaults_on_and_requires_a_valid_boolean(
    monkeypatch,
) -> None:
    monkeypatch.delenv("NOVEL_SYSTEM_SQLITE_FOREIGN_KEYS_ENABLED", raising=False)
    assert (
        get_settings(include_runtime_config=False).sqlite_foreign_keys_enabled is True
    )

    monkeypatch.setenv("NOVEL_SYSTEM_SQLITE_FOREIGN_KEYS_ENABLED", "off")
    assert (
        get_settings(include_runtime_config=False).sqlite_foreign_keys_enabled is False
    )

    monkeypatch.setenv("NOVEL_SYSTEM_SQLITE_FOREIGN_KEYS_ENABLED", "typo")
    with pytest.raises(ValueError, match="must be a boolean"):
        get_settings(include_runtime_config=False)


# ---------------------------------------------------------------------------
# 一个按事务幂等的外键延迟配置器（B12-12 / X01-22）：以前每个事务在 engine begin、ORM after_begin 与每次
# flush 各发三条 PRAGMA（查 foreign_keys、开 defer_foreign_keys、再读回来），一个只读 GET 七条语句里六条是
# PRAGMA，一个两次 flush 的写事务十二条。现在同一个 DBAPI 状态里只开一次，SQLite 可能把它关掉的时候才重开。
# ---------------------------------------------------------------------------


class _FkTestBase(DeclarativeBase):
    pass


class _ParentRow(_FkTestBase):
    __tablename__ = "parent_rows"

    parent_id: Mapped[str] = mapped_column(String, primary_key=True)


class _ChildRow(_FkTestBase):
    __tablename__ = "child_rows"

    child_id: Mapped[str] = mapped_column(String, primary_key=True)
    parent_id: Mapped[str] = mapped_column(String, ForeignKey("parent_rows.parent_id"))


class _StatementLog:
    """记下经 SQLAlchemy 发出的每条语句（连接建立时的 PRAGMA 走原生游标，不在其中）。"""

    def __init__(self, engine) -> None:
        self.statements: list[str] = []
        event.listen(engine, "before_cursor_execute", self._record)

    def _record(self, _connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        self.statements.append(" ".join(str(statement).split()))

    def pragmas(self) -> list[str]:
        return [statement for statement in self.statements if statement.upper().startswith("PRAGMA")]

    def clear(self) -> None:
        self.statements.clear()


def _make_deferred_fk_session_maker():
    test_engine = _make_deferred_fk_engine()
    session_maker = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    db_session._install_sqlite_session_pragmas(session_maker)
    return test_engine, session_maker


def _stored_rows(test_engine) -> tuple[list[str], list[str]]:
    with test_engine.connect() as connection:
        parents = sorted(connection.exec_driver_sql("SELECT parent_id FROM parent_rows").scalars())
        children = sorted(connection.exec_driver_sql("SELECT child_id FROM child_rows").scalars())
    return parents, children


def test_read_only_session_defers_foreign_keys_with_one_pragma() -> None:
    test_engine, session_maker = _make_deferred_fk_session_maker()
    log = _StatementLog(test_engine)
    try:
        with session_maker() as session:
            session.execute(text("SELECT COUNT(*) FROM parent_rows")).scalar_one()
    finally:
        test_engine.dispose()

    # engine begin 与紧跟着的 ORM after_begin：一条，不是六条
    assert log.pragmas() == ["PRAGMA defer_foreign_keys=ON"]


def test_two_flushes_in_one_transaction_defer_once_and_accept_child_before_parent() -> None:
    test_engine, session_maker = _make_deferred_fk_session_maker()
    log = _StatementLog(test_engine)
    try:
        with session_maker() as session:
            session.execute(select(_ParentRow)).all()  # 事务外的读：SQLite 在它结束时关掉了延迟
            log.clear()
            session.add(_ChildRow(child_id="child", parent_id="parent"))
            session.flush()
            session.add(_ParentRow(parent_id="parent"))
            session.flush()
            session.commit()

        # 第一次 flush 先开 DBAPI 事务、再开延迟；第二次 flush 在同一个事务里，什么都不用发
        assert log.statements[:2] == ["BEGIN", "PRAGMA defer_foreign_keys=ON"]
        assert log.pragmas() == ["PRAGMA defer_foreign_keys=ON"]
        assert _stored_rows(test_engine) == (["parent"], ["child"])
    finally:
        test_engine.dispose()


def test_a_released_savepoint_that_opened_the_transaction_rearms_deferral() -> None:
    """旧式事务控制下，没有未决写时 SAVEPOINT 自己开事务、RELEASE 就是提交——SQLite 随之关掉延迟，
    会话却还在同一个 SQLAlchemy 事务里。下一次 flush 必须重新打开，而不是信一个过期的记号。"""
    test_engine, session_maker = _make_deferred_fk_session_maker()
    try:
        with session_maker() as session:
            session.execute(text("SELECT 1")).all()
            with session.begin_nested():
                session.add(_ParentRow(parent_id="first-parent"))
            session.add(_ChildRow(child_id="second-child", parent_id="second-parent"))
            session.flush()
            session.add(_ParentRow(parent_id="second-parent"))
            session.flush()
            session.commit()

        assert _stored_rows(test_engine) == (["first-parent", "second-parent"], ["second-child"])
    finally:
        test_engine.dispose()


def test_rollback_rearms_deferral_for_the_next_transaction() -> None:
    test_engine, session_maker = _make_deferred_fk_session_maker()
    try:
        with session_maker() as session:
            session.add(_ChildRow(child_id="discarded", parent_id="nobody"))
            session.flush()
            session.rollback()
            session.add(_ChildRow(child_id="kept", parent_id="later-parent"))
            session.flush()
            session.add(_ParentRow(parent_id="later-parent"))
            session.flush()
            session.commit()

        assert _stored_rows(test_engine) == (["later-parent"], ["kept"])
    finally:
        test_engine.dispose()


def test_orphan_still_fails_at_commit_after_idempotent_flushes() -> None:
    test_engine, session_maker = _make_deferred_fk_session_maker()
    try:
        with session_maker() as session:
            session.add(_ChildRow(child_id="orphan", parent_id="missing"))
            session.flush()
            session.add(_ParentRow(parent_id="unrelated"))
            session.flush()
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                session.commit()
    finally:
        test_engine.dispose()


def test_explicit_begin_immediate_keeps_deferral_for_later_flushes() -> None:
    """记账与场景任务先拿写锁（``BEGIN IMMEDIATE``）再写：事务外设下的延迟跨过 BEGIN 仍在，
    进了事务后的第一次 flush 在事务里再开一次（只一次），子行先于父行照样能提交。"""
    test_engine, session_maker = _make_deferred_fk_session_maker()
    log = _StatementLog(test_engine)
    try:
        with session_maker() as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            assert session.connection().exec_driver_sql("PRAGMA defer_foreign_keys").scalar_one() == 1
            session.add(_ChildRow(child_id="locked-child", parent_id="locked-parent"))
            session.flush()
            session.add(_ParentRow(parent_id="locked-parent"))
            session.flush()
            session.commit()

        assert log.pragmas() == [
            "PRAGMA defer_foreign_keys=ON",
            "PRAGMA defer_foreign_keys",
            "PRAGMA defer_foreign_keys=ON",
        ]
        assert _stored_rows(test_engine) == (["locked-parent"], ["locked-child"])
    finally:
        test_engine.dispose()


def test_runtime_session_read_sends_one_pragma() -> None:
    log = _StatementLog(db_session.engine())
    with db_session.SessionLocal() as session:
        session.execute(select(StoryProject)).all()

    assert log.pragmas() == ["PRAGMA defer_foreign_keys=ON"]
