"""项目里「运行本章」的任务走有界的章任务车道（B03-13/14），与不带项目的章任务同一条。"""

from __future__ import annotations

import threading
import time

from novel_system.services import chapter_final_flow
from novel_system.services.run_job_leases import busy_job_ids


def _wait_until(predicate, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_project_chapter_job_is_dispatched_once_on_the_chapter_lane(monkeypatch) -> None:
    """以前每次派发都起一条裸线程：启动恢复与路由（或两次恢复扫描）对同一任务各派一次，就有两条线程去抢同一个
    任务。现在同一任务在本进程里只派发一次，排在章任务车道上，跑完才注销。"""
    started = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, str, str]] = []

    def fake_worker(project_id: str, chapter_id: str, job_id: str) -> None:
        calls.append((project_id, chapter_id, job_id))
        started.set()
        release.wait(timeout=10)

    monkeypatch.setattr(chapter_final_flow, "_run_project_chapter_job_worker", fake_worker)
    try:
        chapter_final_flow.start_project_chapter_run_job_worker("P_LANE", "CH_LANE", "job-lane-1")
        assert started.wait(timeout=10)
        assert "job-lane-1" in busy_job_ids()
        chapter_final_flow.start_project_chapter_run_job_worker("P_LANE", "CH_LANE", "job-lane-1")
    finally:
        release.set()

    assert _wait_until(lambda: "job-lane-1" not in busy_job_ids())
    assert calls == [("P_LANE", "CH_LANE", "job-lane-1")]
