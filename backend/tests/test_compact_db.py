"""部署时压缩数据库的工具（批准#23 / 重评 R14）：默认只看不改；真做时清掉过期的重放缓存、切增量自动回收、
VACUUM、完整性与外键检查、截断 WAL；库还在用时拒绝。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from novel_system.db.models import IdempotencyKey
from novel_system.tools import compact_db

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
PAYLOAD = {"workspace": "x" * 20_000}


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _make_db(path) -> str:
    database = str(path)
    engine = create_engine(f"sqlite:///{database}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("CREATE TABLE work (id INTEGER PRIMARY KEY, body TEXT)")
            connection.exec_driver_sql("INSERT INTO work (body) VALUES ('作者的正文')")
        IdempotencyKey.__table__.create(engine)
        with Session(engine) as session:
            for index in range(120):
                old = index < 100  # 100 行过期，20 行还在保留期内
                session.add(
                    IdempotencyKey(
                        idempotency_key=f"key-{index}",
                        request_hash="h",
                        status="succeeded",
                        response_json=PAYLOAD,
                        worker_id="http:w",
                        attempt_no=1,
                        lease_expires_at=_iso(80 if old else 1),
                        created_at=_iso(80 if old else 1),
                        updated_at=_iso(80 if old else 1),
                    )
                )
            session.add(
                IdempotencyKey(
                    idempotency_key="live",
                    request_hash="h",
                    status="started",
                    worker_id="http:live",
                    attempt_no=1,
                    lease_expires_at=(NOW + timedelta(minutes=5)).isoformat(),
                    created_at=_iso(200),
                    updated_at=_iso(200),
                )
            )
            session.commit()
    finally:
        engine.dispose()
    return database


def _keys(database: str) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        return {row[0] for row in connection.execute("SELECT idempotency_key FROM idempotency_keys")}
    finally:
        connection.close()


def test_dry_run_reports_and_changes_nothing(tmp_path) -> None:
    database = _make_db(tmp_path / "live-copy.db")
    before_keys = _keys(database)

    report = compact_db.plan(database, now=NOW)

    assert report["mode"] == "dry-run"
    assert report["retention_hours"] == 72
    assert (report["replay_rows"], report["replay_rows_expired"]) == (121, 100)
    assert report["stats"]["auto_vacuum"] == "none"
    assert report["stats"]["page_count"] > 0
    assert _keys(database) == before_keys
    assert compact_db.plan(database, now=NOW)["stats"]["auto_vacuum"] == "none"


def test_execute_purges_vacuums_checks_and_truncates_the_wal(tmp_path) -> None:
    database = _make_db(tmp_path / "live-copy.db")

    result = compact_db.compact(database, now=NOW)

    assert result["replay_rows_purged"] == 100
    assert _keys(database) == {f"key-{index}" for index in range(100, 120)} | {"live"}
    assert result["integrity"] == "ok"
    assert result["foreign_key_violations"] == 0
    assert result["after"]["auto_vacuum"] == "incremental"
    assert result["after"]["freelist_count"] == 0
    assert result["after"]["page_count"] < result["before"]["page_count"]
    assert result["after"]["wal_bytes"] == 0
    # 作者的数据一行不动
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT body FROM work").fetchall() == [("作者的正文",)]
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    finally:
        connection.close()


def test_refuses_a_database_that_is_still_in_use(tmp_path) -> None:
    database = _make_db(tmp_path / "busy.db")
    writer = sqlite3.connect(database)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE work SET body = 'uncommitted'")
    try:
        with pytest.raises(RuntimeError, match="in use"):
            compact_db.compact(database, now=NOW)
    finally:
        writer.rollback()
        writer.close()
    assert len(_keys(database)) == 121


def test_cli_defaults_to_dry_run_and_executes_only_when_asked(tmp_path, capsys, monkeypatch) -> None:
    database = _make_db(tmp_path / "cli.db")
    monkeypatch.setattr(compact_db, "utcnow", lambda: NOW)

    assert compact_db._main([f"sqlite:///{database}"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"
    assert len(_keys(database)) == 121

    assert compact_db._main([database, "--execute"]) == 0
    assert json.loads(capsys.readouterr().out)["replay_rows_purged"] == 100

    assert compact_db._main([str(tmp_path / "missing.db")]) == 1
    assert "error" in capsys.readouterr().err
    assert not os.path.exists(tmp_path / "missing.db")
