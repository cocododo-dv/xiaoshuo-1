"""在一段阻塞操作期间周期性地跑一个心跳回调（叶子模块：只依赖标准库）。

风格参考作业的工人框架（``style_reference.jobs``）用它给作业行续心跳；LLM 执行的所有权续约
（``llm_task_runner._execution_owner_heartbeat``）是同一个「停止信号 + 守护线程」循环，可以换成它（``on_error``
钩子返回真就停下，例如租约已经丢了）。

心跳失败默认只记日志、下一个间隔再试：一次短暂的 SQLite 忙不能打断它所保护的模型调用；所有权本身由每次回调里的
条件写保证。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)


@contextmanager
def periodic_heartbeat(
    beat: Callable[[], None],
    *,
    interval_seconds: float,
    thread_name: str,
    on_error: Callable[[Exception], bool] | None = None,
    join_timeout: float | None = None,
) -> Iterator[None]:
    """``with`` 块运行期间每 ``interval_seconds`` 秒调一次 ``beat``（第一次在一个间隔之后）。

    ``beat`` 抛异常：有 ``on_error`` 就交给它，它返回真则不再续（线程退出）；没有 ``on_error`` 就记日志、下一拍再试。
    离开 ``with`` 时停下线程并等它至多 ``join_timeout`` 秒（缺省 ``min(5, 间隔 + 0.5)``）。
    """
    stop = threading.Event()
    interval = max(0.01, float(interval_seconds))

    def loop() -> None:
        while not stop.wait(interval):
            try:
                beat()
            except Exception as exc:  # noqa: BLE001 — 后台线程边界：心跳失败不能带走它保护的操作
                if on_error is not None:
                    if on_error(exc):
                        return
                    continue
                logger.exception("heartbeat failed in %s", thread_name)

    thread = threading.Thread(target=loop, name=thread_name, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=min(5.0, interval + 0.5) if join_timeout is None else join_timeout)


__all__ = ["periodic_heartbeat"]
