"""``services/periodic_heartbeat.py``：阻塞操作期间周期性心跳（风格作业的工人框架在用）。"""

from __future__ import annotations

import threading
import time

from novel_system.services.periodic_heartbeat import periodic_heartbeat


def _wait_for(predicate, seconds: float = 2.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_periodic_heartbeat_runs_and_stops() -> None:
    calls: list[float] = []

    with periodic_heartbeat(
        lambda: calls.append(time.monotonic()),
        interval_seconds=0.01,
        thread_name="test-heartbeat",
    ):
        _wait_for(lambda: bool(calls))

    assert calls
    stopped_count = len(calls)
    time.sleep(0.03)
    assert len(calls) == stopped_count
    assert not any(thread.name == "test-heartbeat" for thread in threading.enumerate())


def test_a_failing_beat_is_logged_and_retried(caplog) -> None:
    attempts: list[int] = []

    def flaky() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("database is locked")

    with caplog.at_level("ERROR"), periodic_heartbeat(flaky, interval_seconds=0.01, thread_name="test-flaky"):
        _wait_for(lambda: len(attempts) >= 2)
    assert len(attempts) >= 2
    assert any("heartbeat failed in test-flaky" in record.message for record in caplog.records)


def test_on_error_decides_whether_to_keep_beating() -> None:
    attempts: list[int] = []
    seen: list[str] = []

    def lost() -> None:
        attempts.append(1)
        raise RuntimeError("RUN_OWNER_LEASE_LOST")

    def stop_on_lost(exc: Exception) -> bool:
        seen.append(str(exc))
        return "LEASE_LOST" in str(exc)

    with periodic_heartbeat(lost, interval_seconds=0.01, thread_name="test-lost", on_error=stop_on_lost):
        _wait_for(lambda: bool(seen))
        time.sleep(0.05)
    # 钩子说「停」：只试了一次，线程自己退出
    assert attempts == [1] and seen == ["RUN_OWNER_LEASE_LOST"]
