"""测试进程的隔离：上一个 app 的风格作业清扫线程（X04-20）进不了用例。"""

from __future__ import annotations

import threading

import pytest

from novel_system.services.style_reference import jobs
from tests.conftest import _wait_for_job_sweepers


# ---------------------------------------------------------------------------
# 风格作业清扫线程
# ---------------------------------------------------------------------------


def test_a_sweeper_still_in_its_tick_stops_when_the_next_one_starts(monkeypatch) -> None:
    """lifespan 重启时旧清扫线程还在一拍里：新线程启动不能让旧线程接着循环（各有各的停止信号）。"""
    entered = threading.Event()
    release = threading.Event()
    ticks: dict[int, int] = {}

    def fake_tick(*, now: float | None = None) -> None:
        ident = threading.get_ident()
        ticks[ident] = ticks.get(ident, 0) + 1
        if not entered.is_set():
            entered.set()
            release.wait(10)

    monkeypatch.setattr(jobs, "sweeper_tick", fake_tick)
    jobs.start_job_sweeper(interval_seconds=1.0)
    first = jobs._SWEEPER
    second = None
    try:
        assert entered.wait(5)
        jobs.shutdown_job_workers()  # 旧 app 关掉时，它的清扫线程还卡在第一拍里
        jobs.start_job_sweeper(interval_seconds=1.0)  # 下一个 app 马上启动
        second = jobs._SWEEPER
        assert second is not None and second is not first
        release.set()
        first.join(5)
        assert not first.is_alive(), "旧清扫线程在下一个 lifespan 启动后还在循环"
        assert ticks[first.ident] == 1
    finally:
        release.set()
        jobs.shutdown_job_workers()
        for thread in (first, second):
            if thread is not None:
                thread.join(5)
    assert not second.is_alive()


def test_teardown_stops_and_reports_a_sweeper_nobody_shut_down(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "sweeper_tick", lambda *, now=None: None)
    jobs.start_job_sweeper(interval_seconds=1.0)
    leaked = jobs._SWEEPER
    with pytest.raises(pytest.fail.Exception, match="仍在运行"):
        _wait_for_job_sweepers(timeout=0.5)
    assert jobs._SWEEPER is None and not leaked.is_alive()
