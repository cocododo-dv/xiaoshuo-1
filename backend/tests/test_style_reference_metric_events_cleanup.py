"""PR-11 — cleanup_metric_events 按 days_threshold 删旧 metric events。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.cleanup import cleanup_metric_events
from novel_system.services.style_reference.repository import StyleReferenceRepository


def _ts(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _seed(events: list[tuple[str, int]]) -> None:
    """每个 (event_id, days_ago) 元组落一行。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        for eid, days_ago in events:
            repo.create_metric_event(
                event_id=eid,
                event_kind="injection_invoked",
                outcome="hit",
                created_at=_ts(days_ago),
            )
        session.commit()


def test_cleanup_deletes_events_older_than_threshold():
    _seed([
        ("sr_metric_old_1", 100),  # 100 天前 — 删
        ("sr_metric_old_2", 95),   # 95 天前 — 删
        ("sr_metric_fresh_1", 30), # 30 天前 — 留
        ("sr_metric_fresh_2", 1),  # 昨天 — 留
    ])
    with SessionLocal() as session:
        result = cleanup_metric_events(session, days_threshold=90, dry_run=False)
        session.commit()
        # 实际剩余 2 行
        remaining = StyleReferenceRepository(session).list_metric_events()
    assert result["deleted_count"] == 2
    assert result["dry_run"] is False
    assert result["days_threshold"] == 90
    assert len(remaining) == 2
    assert {r.event_id for r in remaining} == {"sr_metric_fresh_1", "sr_metric_fresh_2"}


def test_cleanup_keeps_events_within_threshold():
    _seed([
        ("sr_metric_a", 89),  # 89 天前 — 在 90 天阈值内,留
        ("sr_metric_b", 1),
    ])
    with SessionLocal() as session:
        result = cleanup_metric_events(session, days_threshold=90, dry_run=False)
        session.commit()
        remaining = StyleReferenceRepository(session).list_metric_events()
    assert result["deleted_count"] == 0
    assert len(remaining) == 2


def test_cleanup_dry_run_does_not_delete():
    _seed([
        ("sr_metric_old", 365),
        ("sr_metric_fresh", 1),
    ])
    with SessionLocal() as session:
        result = cleanup_metric_events(session, days_threshold=90, dry_run=True)
        session.commit()
        remaining = StyleReferenceRepository(session).list_metric_events()
    assert result["deleted_count"] == 1
    assert result["dry_run"] is True
    # dry_run 不删,仍有 2 行
    assert len(remaining) == 2


def test_cleanup_custom_days_threshold():
    _seed([
        ("sr_metric_d10", 10),
        ("sr_metric_d3", 3),
        ("sr_metric_today", 0),
    ])
    # 自定义阈值 7 天:10 天前的删,3 天和今天的留
    with SessionLocal() as session:
        result = cleanup_metric_events(session, days_threshold=7, dry_run=False)
        session.commit()
        remaining = StyleReferenceRepository(session).list_metric_events()
    assert result["deleted_count"] == 1
    assert result["days_threshold"] == 7
    assert {r.event_id for r in remaining} == {"sr_metric_d3", "sr_metric_today"}


# ---------------------------------------------------------------------------
# 2026-09-24 §8 C8:清扫线程启动时跑一次、之后每 24 小时一次(独立 session,异常只记日志)
# ---------------------------------------------------------------------------


@pytest.fixture
def _maintenance_registry(monkeypatch):
    """每个用例从干净的登记簿开始(cleanup 模块导入时登记的那项照样在),最后恢复。"""
    from novel_system.services.style_reference import jobs as jobs_module

    saved = dict(jobs_module._MAINTENANCE)
    saved_last = dict(jobs_module._MAINTENANCE_LAST_RUN)
    jobs_module._MAINTENANCE_LAST_RUN.clear()
    yield jobs_module
    jobs_module._MAINTENANCE.clear()
    jobs_module._MAINTENANCE.update(saved)
    jobs_module._MAINTENANCE_LAST_RUN.clear()
    jobs_module._MAINTENANCE_LAST_RUN.update(saved_last)


def test_metric_events_retention_is_registered_with_the_sweeper_and_really_deletes(_maintenance_registry) -> None:
    from novel_system.services.style_reference import cleanup

    jobs_module = _maintenance_registry
    task, interval = jobs_module._MAINTENANCE[cleanup.METRIC_EVENTS_MAINTENANCE_TASK]
    assert task is cleanup.run_metric_events_retention and interval == 24 * 3600
    assert cleanup.METRIC_EVENTS_RETENTION_DAYS == 90
    _seed([("sr_metric_ret_old", 100), ("sr_metric_ret_fresh", 1)])
    summary = cleanup.run_metric_events_retention()  # 自己开会话、自己提交
    assert summary["deleted_count"] == 1 and summary["dry_run"] is False and summary["days_threshold"] == 90
    with SessionLocal() as session:
        assert {r.event_id for r in StyleReferenceRepository(session).list_metric_events()} == {"sr_metric_ret_fresh"}


def test_sweeper_tick_runs_the_cleanup_once_at_start_then_every_24_hours(_maintenance_registry, monkeypatch) -> None:
    from novel_system.services.style_reference import cleanup

    jobs_module = _maintenance_registry
    calls: list[tuple[int, bool]] = []

    def spy(session, *, days_threshold, dry_run):  # noqa: ANN001
        calls.append((days_threshold, dry_run))
        return {"deleted_count": 0}

    monkeypatch.setattr(cleanup, "cleanup_metric_events", spy)
    monkeypatch.setattr(jobs_module, "sweep_and_dispatch", lambda: [])
    jobs_module.sweeper_tick(now=0.0)  # 启动的那一拍:跑
    jobs_module.sweeper_tick(now=30.0)  # 30 秒后的清扫拍:不到 24 小时,不跑
    jobs_module.sweeper_tick(now=23 * 3600.0)
    assert calls == [(90, False)]
    jobs_module.sweeper_tick(now=24 * 3600.0 + 1)  # 24 小时到了:再跑
    assert calls == [(90, False), (90, False)]
    # start_job_sweeper 清掉「上次跑过」的记录:重新启动的线程第一拍又跑一次
    jobs_module._MAINTENANCE_LAST_RUN.clear()
    jobs_module.sweeper_tick(now=24 * 3600.0 + 2)
    assert len(calls) == 3


def test_a_failing_maintenance_task_is_logged_and_does_not_stop_the_sweeper(_maintenance_registry, monkeypatch, caplog) -> None:
    from novel_system.services.style_reference import cleanup

    jobs_module = _maintenance_registry
    swept: list[int] = []
    monkeypatch.setattr(jobs_module, "sweep_and_dispatch", lambda: swept.append(1) or [])

    def boom(session, **_kwargs):  # noqa: ANN001
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cleanup, "cleanup_metric_events", boom)
    ran: list[str] = []
    jobs_module.register_maintenance_task("other_task", lambda: ran.append("other"), interval_seconds=60)
    with caplog.at_level("ERROR"):
        jobs_module.sweeper_tick(now=0.0)  # 不抛
        jobs_module.sweeper_tick(now=1.0)
    assert swept == [1, 1] and ran == ["other"]
    assert any("maintenance task" in record.message and "database is locked" in (record.exc_text or "") for record in caplog.records)
    # 失败的任务下一个间隔再试(不是立刻重试)
    assert jobs_module._MAINTENANCE_LAST_RUN[cleanup.METRIC_EVENTS_MAINTENANCE_TASK] == 0.0
    good: list[int] = []
    monkeypatch.setattr(cleanup, "cleanup_metric_events", lambda session, **_k: good.append(1) or {"deleted_count": 0})
    assert jobs_module.run_due_maintenance(now=12 * 3600.0) == ["other_task"]  # 还不到 24 小时:只有另一项到期
    assert good == []
    assert jobs_module.run_due_maintenance(now=24 * 3600.0 + 1) == [cleanup.METRIC_EVENTS_MAINTENANCE_TASK, "other_task"]
    assert good == [1]
