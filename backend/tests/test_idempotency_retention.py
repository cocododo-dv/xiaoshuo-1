"""幂等重放缓存的保留期（批准#23 / 重评 R14）：72 小时后清掉已结束的记录与早就过期的租约，活着的租约永远不动。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from novel_system.db.models import IdempotencyKey, OperationLog
from novel_system.db.session import SessionLocal
from novel_system.services import idempotency
from novel_system.services.idempotency import (
    IDEMPOTENCY_REPLAY_RETENTION_HOURS,
    IDEMPOTENCY_RETENTION_INTERVAL_SECONDS,
    IDEMPOTENCY_RETENTION_TASK,
    execute_with_idempotency,
    purge_expired_idempotency,
    release_free_pages,
    run_idempotency_retention,
)

NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _row(session, key: str, *, status: str, updated_hours_ago: float, lease_hours_ago: float | None, created_hours_ago: float | None = None):
    session.add(
        IdempotencyKey(
            idempotency_key=key,
            request_hash=f"hash-{key}",
            status=status,
            response_json={"key": key} if status == "succeeded" else None,
            worker_id="http:w",
            attempt_no=1,
            heartbeat_at=_iso(updated_hours_ago),
            lease_expires_at=None if lease_hours_ago is None else _iso(lease_hours_ago),
            created_at=_iso(created_hours_ago if created_hours_ago is not None else updated_hours_ago),
            updated_at=_iso(updated_hours_ago),
        )
    )


def test_retention_window_is_72_hours() -> None:
    assert IDEMPOTENCY_REPLAY_RETENTION_HOURS == 72


def test_purge_keeps_recent_rows_and_live_leases_and_deletes_only_expired_ones(session) -> None:
    _row(session, "old-succeeded", status="succeeded", updated_hours_ago=80, lease_hours_ago=80)
    _row(session, "old-failed", status="failed", updated_hours_ago=73, lease_hours_ago=73)
    _row(session, "old-started-expired", status="started", updated_hours_ago=90, lease_hours_ago=75)
    _row(session, "old-started-without-lease", status="started", updated_hours_ago=100, lease_hours_ago=None)
    _row(session, "recent-succeeded", status="succeeded", updated_hours_ago=71, lease_hours_ago=71)
    _row(session, "recent-failed", status="failed", updated_hours_ago=1, lease_hours_ago=1)
    # 租约 2 小时前才过期：上一个进程刚死，72 小时内还可以被同一个 key 接手
    _row(session, "started-recently-expired", status="started", updated_hours_ago=80, lease_hours_ago=2)
    # 长跑作业：100 小时前建的行，租约一直在续，现在还活着
    _row(session, "live-lease", status="started", updated_hours_ago=100, lease_hours_ago=-0.5, created_hours_ago=100)
    session.add(OperationLog(event_type="idempotency_succeeded", object_type="idempotency_key", object_ref="old-succeeded"))
    session.commit()

    assert purge_expired_idempotency(session, NOW) == 4
    session.commit()

    remaining = set(session.scalars(select(IdempotencyKey.idempotency_key)))
    assert remaining == {"recent-succeeded", "recent-failed", "started-recently-expired", "live-lease"}
    # 操作日志不动
    assert session.scalars(select(OperationLog.object_ref).where(OperationLog.object_ref == "old-succeeded")).all() == [
        "old-succeeded"
    ]


def test_a_live_lease_is_never_purged_however_old_the_row(session) -> None:
    _row(session, "long-job", status="started", updated_hours_ago=500, lease_hours_ago=-0.01, created_hours_ago=500)
    session.commit()
    assert purge_expired_idempotency(session, NOW) == 0
    assert session.get(IdempotencyKey, "long-job") is not None


def _run(session, key: str, calls: list[str]):
    def action() -> dict:
        calls.append(key)
        return {"ran": len(calls)}

    return execute_with_idempotency(
        session,
        idempotency_key=key,
        method="POST",
        path_template="/api/v1/probe",
        payload={"x": 1},
        action=action,
    )


def test_replay_inside_the_window_and_re_execution_after_a_purge(session, monkeypatch) -> None:
    started = datetime.now(UTC)
    calls: list[str] = []
    first, status = _run(session, "replay-key", calls)
    assert status is None and first["ran"] == 1

    # 1 小时后清理：还在保留期内，同一个 key 照常重放、不再执行
    assert purge_expired_idempotency(session, started + timedelta(hours=1)) == 0
    session.commit()
    replayed, status = _run(session, "replay-key", calls)
    assert status == "replayed" and replayed["ran"] == 1 and calls == ["replay-key"]

    # 73 小时后清理：记录没了，同一个 key 再来就是一次新请求
    assert purge_expired_idempotency(session, started + timedelta(hours=73)) == 1
    session.commit()
    again, status = _run(session, "replay-key", calls)
    assert status is None and again["ran"] == 2 and calls == ["replay-key", "replay-key"]


def test_maintenance_task_purges_in_its_own_session_and_commits(session, monkeypatch) -> None:
    _row(session, "old", status="succeeded", updated_hours_ago=100, lease_hours_ago=100)
    _row(session, "new", status="succeeded", updated_hours_ago=1, lease_hours_ago=1)
    session.commit()
    monkeypatch.setattr(idempotency, "utcnow", lambda: NOW)

    assert run_idempotency_retention() == 1
    with SessionLocal() as fresh:
        assert set(fresh.scalars(select(IdempotencyKey.idempotency_key))) == {"new"}
    assert run_idempotency_retention() == 0


def test_retention_runs_every_6_hours_on_the_system_maintenance_registry_but_never_on_the_first_tick(
    session, monkeypatch
) -> None:
    """重评 R14：清理登记在全系统维护登记表上（``services/maintenance.py``，随运行任务巡检线程跑），每 6 小时一次；
    启动后的第一拍不清——后端热加载新代码时，第一次大批量删除必须等部署时那份备份做完（复核补充 2 / 7）。

    登记表随 P01b 进来：它还不在树上时本例跳过。两边合并之后要在 ``idempotency.py`` 末尾登记::

        register_maintenance_task(IDEMPOTENCY_RETENTION_TASK, run_idempotency_retention,
                                  interval_seconds=IDEMPOTENCY_RETENTION_INTERVAL_SECONDS, run_at_start=False)

    没登记本例就红：保留期只在部署时的 compact_db 里跑一次，之后这张表又会一直长下去。
    """
    maintenance = pytest.importorskip("novel_system.services.maintenance")
    registered = maintenance.SYSTEM_MAINTENANCE.tasks.get(IDEMPOTENCY_RETENTION_TASK)
    assert registered is not None, "idempotency retention is not registered on the system maintenance registry"
    task, interval = registered
    assert task is run_idempotency_retention
    assert interval == IDEMPOTENCY_RETENTION_INTERVAL_SECONDS == 6 * 3600

    _row(session, "old", status="succeeded", updated_hours_ago=100, lease_hours_ago=100)
    session.commit()
    monkeypatch.setattr(idempotency, "utcnow", lambda: NOW)
    start = 1_000.0
    try:
        maintenance.reset_maintenance_schedule(now=start)
        assert IDEMPOTENCY_RETENTION_TASK not in maintenance.run_due_maintenance(now=start + 60)
        session.expire_all()
        assert session.get(IdempotencyKey, "old") is not None

        ran = maintenance.run_due_maintenance(now=start + IDEMPOTENCY_RETENTION_INTERVAL_SECONDS)
        assert IDEMPOTENCY_RETENTION_TASK in ran
        session.expire_all()
        assert session.get(IdempotencyKey, "old") is None
    finally:
        maintenance.reset_maintenance_schedule()


def test_released_pages_go_back_to_the_file_system_on_an_incremental_database(tmp_path) -> None:
    """``incremental_vacuum`` 每一步只还一页：要把游标取完。这里删掉一大块之后空闲页全部还回去。"""
    connection = sqlite3.connect(tmp_path / "incremental.db")
    try:
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE cache (id INTEGER PRIMARY KEY, body TEXT)")
        connection.executemany("INSERT INTO cache (body) VALUES (?)", [("x" * 8_000,) for _ in range(200)])
        connection.commit()
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        connection.execute("DELETE FROM cache")
        connection.commit()
        assert connection.execute("PRAGMA freelist_count").fetchone()[0] > 100

        release_free_pages(connection)
        connection.commit()

        assert connection.execute("PRAGMA freelist_count").fetchone()[0] == 0
        assert connection.execute("PRAGMA page_count").fetchone()[0] < pages // 10
    finally:
        connection.close()
