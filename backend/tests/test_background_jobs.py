"""通用后台运行时（services.background_jobs，B03-13）：有界守护车道、维护任务登记簿、周期线程。

风格作业自己的用例（清扫、维护任务、线程隔离）在 test_style_reference_jobs / test_style_reference_metric_events_cleanup /
test_test_isolation；这里只测通用件本身。
"""

from __future__ import annotations

import threading
import time

from novel_system.services.background_jobs import (
    DaemonLane,
    MaintenanceRegistry,
    PeriodicThread,
    close_daemon_lanes,
    daemon_lane,
)


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return bool(predicate())


def test_daemon_lane_runs_at_most_max_workers_tasks_at_once_in_submission_order() -> None:
    lane = DaemonLane("t_lane_bounded", max_workers=2)
    release = threading.Event()
    lock = threading.Lock()
    started: list[int] = []
    running = 0
    peak = 0

    def task(index: int) -> None:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            started.append(index)
        release.wait(10)
        with lock:
            running -= 1

    for index in range(5):
        assert lane.submit(task, index)
    assert _wait_until(lambda: len(started) == 2)
    time.sleep(0.2)
    assert len(started) == 2 and lane.pending() == 3
    workers = [thread for thread in threading.enumerate() if thread.name.startswith("t_lane_bounded_")]
    assert len(workers) == 2 and all(thread.daemon for thread in workers)
    release.set()
    assert _wait_until(lambda: len(started) == 5)
    assert peak == 2
    assert sorted(started[:2]) == [0, 1] and sorted(started[2:]) == [2, 3, 4]


def test_daemon_lane_starts_a_worker_again_after_its_workers_went_idle() -> None:
    lane = DaemonLane("t_lane_idle", max_workers=1)
    done: list[int] = []
    for value in (1, 2, 3):
        assert lane.submit(done.append, value)
    assert _wait_until(lambda: done == [1, 2, 3])  # 一个工人：按提交顺序
    assert _wait_until(lambda: not any(t.name.startswith("t_lane_idle_") for t in threading.enumerate()))
    assert lane.submit(done.append, 4)
    assert _wait_until(lambda: done == [1, 2, 3, 4])


def test_a_failing_task_does_not_take_the_worker_with_it() -> None:
    lane = DaemonLane("t_lane_fail", max_workers=1)
    done: list[str] = []

    def boom() -> None:
        raise RuntimeError("task failed")

    assert lane.submit(boom)
    assert lane.submit(done.append, "after")
    assert _wait_until(lambda: done == ["after"])


def test_closing_a_lane_drops_pending_tasks_and_refuses_new_ones_until_it_is_recreated() -> None:
    release = threading.Event()
    lane = daemon_lane("t_lane_close", max_workers=1)
    started: list[str] = []

    def blocking(name: str) -> None:
        started.append(name)
        release.wait(10)

    assert lane.submit(blocking, "running")
    assert _wait_until(lambda: started == ["running"])
    assert lane.submit(blocking, "pending-a") and lane.submit(blocking, "pending-b")
    try:
        dropped = close_daemon_lanes("t_lane_close")
        assert dropped == {"t_lane_close": [("pending-a",), ("pending-b",)]}
        assert lane.submit(blocking, "late") is False
        # 下一个 lifespan：按名字取到的是一条新车道
        again = daemon_lane("t_lane_close", max_workers=1)
        assert again is not lane
    finally:
        release.set()
        close_daemon_lanes("t_lane_close")
    assert started == ["running"]


def test_maintenance_registry_defers_tasks_registered_not_to_run_at_start() -> None:
    registry = MaintenanceRegistry("test")
    ran: list[str] = []
    registry.register("eager", lambda: ran.append("eager"), interval_seconds=60)
    registry.register("deferred", lambda: ran.append("deferred"), interval_seconds=60, run_at_start=False)

    registry.reset_schedule(now=100.0)
    assert registry.run_due(now=100.0) == ["eager"]
    assert registry.run_due(now=130.0) == []
    assert registry.run_due(now=160.0) == ["eager", "deferred"]
    assert ran == ["eager", "eager", "deferred"]
    registry.unregister("deferred")
    assert "deferred" not in registry.tasks and "deferred" not in registry.last_run


def test_maintenance_registry_defers_a_task_registered_after_the_thread_started() -> None:
    """``run_at_start=False`` 从登记那一刻起计时：周期线程已在跑（``reset_schedule`` 之后）才登记的任务，下一拍不跑。"""
    registry = MaintenanceRegistry("test")
    registry.reset_schedule()
    ran: list[str] = []
    registry.register("late", lambda: ran.append("late"), interval_seconds=60, run_at_start=False)

    assert registry.run_due() == []
    # 同名重复登记（模块重新导入）不重置已有的计时
    registry.register("late", lambda: ran.append("late"), interval_seconds=60, run_at_start=False)
    assert registry.run_due(now=time.monotonic() + 61) == ["late"]
    assert ran == ["late"]


def test_periodic_thread_keeps_ticking_after_a_tick_raises() -> None:
    calls: list[int] = []

    def tick() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first tick fails")

    periodic = PeriodicThread("t_periodic_raise", tick, interval_seconds=1.0, run_first=True)
    periodic.start()
    try:
        assert _wait_until(lambda: len(calls) >= 2, timeout=5)
        assert periodic.is_alive()
    finally:
        periodic.stop(join_timeout=5)


def test_periodic_thread_without_a_first_tick_waits_a_full_interval_and_stops_on_its_own_signal() -> None:
    ticks: list[float] = []
    periodic = PeriodicThread("t_periodic", lambda: ticks.append(time.monotonic()), interval_seconds=1.0, run_first=False)
    periodic.start()
    time.sleep(0.3)
    assert ticks == []
    periodic.stop(join_timeout=5.0)
    assert not periodic.is_alive() and periodic.stop_event.is_set()
