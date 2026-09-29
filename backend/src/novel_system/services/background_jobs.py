"""进程内后台作业的通用运行时（B03-13；从 ``style_reference/jobs.py`` 搬出，不依赖任何业务模块）。

- **工人代**：lifespan 结束（``--reload``、停服）时 +1。认领时记下当时的代，之后发现代变了 = 进程要退出：
  风格作业在下一个检查点把作业放回队列；运行任务（场景 / 章节）的租约不再续（见 ``run_job_leases``）。
- ``DaemonCallPool``：每个调用一条守护线程的「线程池」，给在飞的 LLM 调用用——进程退出不等网络请求。
- ``DaemonLane``：有界的守护工人车道（先进先出）。运行任务的工人跑在这里：一次最多 N 条管线，进程退出时
  直接丢下（作业行还在，下次启动的恢复接着跑）；``concurrent.futures`` 的线程池在解释器退出时会逐个 join，
  一条正在等模型回复的管线会把 ``--reload`` 拖住几分钟。
- ``MaintenanceRegistry``：随周期线程跑的定期任务登记簿（任务自己开会话；异常只记日志，下一个间隔再试）。
- ``PeriodicThread``：周期线程，每条有自己的停止信号（旧线程还在一拍里时新线程启动，旧线程不会接着循环）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------- 工人代
_GENERATION = 0
_GENERATION_LOCK = threading.Lock()


def current_worker_generation() -> int:
    return _GENERATION


def bump_worker_generation() -> int:
    """进程要退出：工人代 +1，返回新的代。"""
    global _GENERATION
    with _GENERATION_LOCK:
        _GENERATION += 1
        return _GENERATION


def generation_changed(generation: int | None) -> bool:
    """认领时记下的代已经过去了（``None`` = 不受进程退出影响的认领，例如测试 / 工具直接拿的）。"""
    return generation is not None and generation != _GENERATION


# ---------------------------------------------------------------------- 守护调用池
class DaemonCallPool:
    """给处理器的 LLM 调用用的「线程池」：每个调用一条守护线程，并发上限由调用方控制（在飞数）。

    ``concurrent.futures.ThreadPoolExecutor`` 的线程在解释器退出时会被逐个 join——``--reload`` / 停服要等在飞的
    网络请求回来（最长到 LLM 超时）。这里的线程是守护线程：进程退出时直接丢下，作业由框架放回队列，下次续跑；
    丢下的调用的记账预留由记账层按 TTL 回收。接口是处理器用到的那部分 ``Executor``（``submit`` / ``shutdown``）。
    """

    def __init__(self, max_workers: int | None = None, thread_name_prefix: str = "sr_call") -> None:
        del max_workers  # 并发由调用方控制
        self._prefix = thread_name_prefix
        self._closed = False
        self._lock = threading.Lock()
        self._count = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._count += 1
            name = f"{self._prefix}_{self._count}"
        future: Future = Future()

        def runner() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 — 原样交给 Future
                future.set_exception(exc)
            else:
                future.set_result(result)

        threading.Thread(target=runner, name=name, daemon=True).start()
        return future

    def shutdown(self, wait: bool = False, *, cancel_futures: bool = False) -> None:
        del wait, cancel_futures  # 守护线程不等；已开始的调用无法撤回（结果被丢弃）
        with self._lock:
            self._closed = True


# ---------------------------------------------------------------------- 有界守护车道
class DaemonLane:
    """最多 ``max_workers`` 条守护线程按先后顺序跑提交的任务；关闭后丢下还没开始的任务、不再收新任务。"""

    def __init__(self, name: str, max_workers: int) -> None:
        self.name = name
        self.max_workers = max(1, int(max_workers))
        self._queue: deque[tuple[Callable[..., Any], tuple[Any, ...]]] = deque()
        self._lock = threading.Lock()
        self._threads = 0
        self._spawned = 0
        self._closed = False

    def submit(self, fn: Callable[..., Any], *args: Any) -> bool:
        """排进车道；车道已关（进程在退出）→ False。"""
        with self._lock:
            if self._closed:
                return False
            self._queue.append((fn, args))
            if self._threads < self.max_workers:
                self._threads += 1
                self._spawned += 1
                threading.Thread(
                    target=self._work, name=f"{self.name}_{self._spawned}", daemon=True
                ).start()
        return True

    def close(self) -> list[tuple[Any, ...]]:
        """不再收新任务，丢下还没开始的任务；返回丢下的任务的参数（正在跑的照旧跑完或随进程退出）。"""
        with self._lock:
            self._closed = True
            dropped = [args for _fn, args in self._queue]
            self._queue.clear()
        return dropped

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def _work(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._closed or not self._queue:
                        return
                    fn, args = self._queue.popleft()
                try:
                    fn(*args)
                except Exception:  # noqa: BLE001 — 车道边界：一个任务失败不带走工人
                    logger.exception("%s lane task failed", self.name)
        finally:
            with self._lock:
                self._threads -= 1


_LANES: dict[str, DaemonLane] = {}
_LANES_LOCK = threading.Lock()


def daemon_lane(name: str, *, max_workers: int) -> DaemonLane:
    """按名字取车道；还没有或已关闭就新建一条（下一个 lifespan 照常派发）。"""
    with _LANES_LOCK:
        lane = _LANES.get(name)
        if lane is None or lane._closed:
            lane = DaemonLane(name, max_workers)
            _LANES[name] = lane
        return lane


def close_daemon_lanes(*names: str) -> dict[str, list[tuple[Any, ...]]]:
    """关闭车道（不给名字 = 全部），返回每条丢下的任务的参数。"""
    with _LANES_LOCK:
        targets = [name for name in (names or tuple(_LANES)) if name in _LANES]
        lanes = [_LANES.pop(name) for name in targets]
    return {lane.name: lane.close() for lane in lanes}


# ---------------------------------------------------------------------- 维护任务登记簿
MaintenanceTask = Callable[[], Any]


class MaintenanceRegistry:
    """随周期线程跑的定期任务：``register`` 登记（同名覆盖），``run_due`` 跑到期的。

    ``run_at_start=True`` 的任务在周期线程第一拍就跑（``reset_schedule`` 之后），否则等满一个间隔。
    ``tasks`` / ``last_run`` 是可直接读写的字典（风格作业模块把它们原样当 ``_MAINTENANCE`` /
    ``_MAINTENANCE_LAST_RUN`` 转出去）。
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.tasks: dict[str, tuple[MaintenanceTask, float]] = {}
        self.last_run: dict[str, float] = {}
        self._deferred: set[str] = set()

    def register(
        self,
        name: str,
        task: MaintenanceTask,
        *,
        interval_seconds: float,
        run_at_start: bool = True,
    ) -> None:
        key = str(name)
        self.tasks[key] = (task, max(1.0, float(interval_seconds)))
        if run_at_start:
            self._deferred.discard(key)
        else:
            self._deferred.add(key)

    def unregister(self, name: str) -> None:
        self.tasks.pop(str(name), None)
        self.last_run.pop(str(name), None)
        self._deferred.discard(str(name))

    def reset_schedule(self, *, now: float | None = None) -> None:
        """周期线程启动时调用：先跑的任务第一拍就跑，等一个间隔的任务从现在起计时。"""
        current = time.monotonic() if now is None else float(now)
        self.last_run.clear()
        for name in self._deferred:
            if name in self.tasks:
                self.last_run[name] = current

    def run_due(self, *, now: float | None = None) -> list[str]:
        """跑一遍到期的任务（``now`` 是单调时钟秒数，缺省当前）；返回这一轮跑了的任务名（失败的不算）。"""
        current = time.monotonic() if now is None else float(now)
        ran: list[str] = []
        for name, (task, interval) in list(self.tasks.items()):
            last = self.last_run.get(name)
            if last is not None and current - last < interval:
                continue
            self.last_run[name] = current
            try:
                task()
            except Exception:  # noqa: BLE001 — 维护任务失败只记日志，下一个间隔再试
                logger.exception("%s maintenance task %s failed", self.label, name)
                continue
            ran.append(name)
        return ran


# ---------------------------------------------------------------------- 周期线程
class PeriodicThread:
    """一条守护线程：``run_first`` 时先跑一拍，之后每 ``interval_seconds`` 秒一拍，直到自己的停止信号置位。

    停止信号每条线程一个（启动时新建、线程闭包里抓住自己那一个）：旧线程还在一拍里时下一个 lifespan 启动新线程，
    旧线程醒来看到的是自己那个已置位的信号，不会接着循环（P00a / X04-20）。
    """

    def __init__(
        self,
        name: str,
        tick: Callable[[], Any],
        *,
        interval_seconds: float,
        run_first: bool = True,
    ) -> None:
        self.stop_event = threading.Event()
        stop = self.stop_event
        interval = max(1.0, float(interval_seconds))

        def _loop() -> None:
            if run_first:
                tick()
            while not stop.wait(interval):
                tick()

        self.thread = threading.Thread(target=_loop, name=name, daemon=True)

    def start(self) -> PeriodicThread:
        self.thread.start()
        return self

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def stop(self, *, join_timeout: float | None = None) -> None:
        self.stop_event.set()
        if join_timeout is not None and self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(join_timeout)


__all__ = [
    "DaemonCallPool",
    "DaemonLane",
    "MaintenanceRegistry",
    "PeriodicThread",
    "bump_worker_generation",
    "close_daemon_lanes",
    "current_worker_generation",
    "daemon_lane",
    "generation_changed",
]
