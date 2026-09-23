from __future__ import annotations

import threading
import time

from novel_system.services.style_reference.background_heartbeat import periodic_heartbeat


def test_periodic_heartbeat_runs_and_stops() -> None:
    calls: list[float] = []

    with periodic_heartbeat(
        lambda: calls.append(time.monotonic()),
        interval_seconds=0.01,
        thread_name="test-style-heartbeat",
    ):
        deadline = time.monotonic() + 1
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)

    assert calls
    stopped_count = len(calls)
    time.sleep(0.03)
    assert len(calls) == stopped_count


