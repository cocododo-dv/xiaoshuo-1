"""风格参考 v3 — 三种作业处理器共用的脚手架（2026-09-24 §8 S5：分类 / 学习 / 对照检查各自的一份收成一份）。

作业表与工人框架在 ``jobs.py``；这里是处理器**里面**重复出现的那几样东西：

- :class:`JobStopped`：作业已停（取消 / 失败 / 丢了所有权），工人线程里还没开始的重试不再发；
- :func:`retry_attempts`：带退避的重试循环（每次开始前看一眼停止标记，退避等待被停止标记打断就抛 ``JobStopped``）；
- :func:`conflict_error_by_kind`：建作业撞上的活动作业 → 按它的种类给对应的 409；
- :class:`JobRun`：处理器基类——``fresh`` / ``check_continue`` / ``book`` / ``pre_call_check`` /
  ``checkpoint`` / ``save_cursor`` / ``call_in_worker`` / ``run_parallel``，以及把异常映射成终态的 ``run`` 生命周期
  （子类只写 ``_run`` 与两个收尾钩子）。

**并行调用循环**（``run_parallel``）的行为与之前分类 / 学习各自那份完全一样：至多 ``max_inflight`` 个调用同时在飞
（调用在守护线程，写库只在作业线程）；等待时每 ``poll_seconds`` 秒查一次取消 / 所有权；某个调用失败就不再派发新的，
已经在飞的（已经花了钱）等它们回来、成功的照常落库，然后整步失败；``stop_when`` 为真时停止派发（调用方按新情况
重新规划）；``JobLost`` / ``JobCancelled`` / ``JobInterrupted`` 与 ``BaseException``（进程被打断）立即向上抛。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, wait
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBook, StyleReferenceJob
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.jobs import (
    JOB_FAILED_CODE,
    ClaimedJob,
    DaemonCallPool,
    JobCancelled,
    JobInterrupted,
    JobLost,
    StyleJobService,
    is_worker_interruption,
)
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed

logger = logging.getLogger(__name__)

_PASSTHROUGH = (JobLost, JobCancelled, JobInterrupted)


class JobStopped(Exception):
    """作业已停（取消 / 失败 / 丢了所有权）：工人线程里还没开始的重试不再发。"""


def retry_attempts(attempts: int, backoff: Sequence[float], stop: threading.Event) -> Iterator[int]:
    """``for attempt in retry_attempts(N, (5, 15), stop)``：第 ``attempt`` 次之前先看停止标记，第二次起按 ``backoff``
    退避（越界取最后一档）；退避等待期间停止标记被置起就抛 :class:`JobStopped`。"""
    for attempt in range(int(attempts)):
        if stop.is_set():
            raise JobStopped()
        if attempt and backoff:
            delay = backoff[min(attempt - 1, len(backoff) - 1)]
            if stop.wait(max(0.0, float(delay))):
                raise JobStopped()
        yield attempt


def conflict_error_by_kind(
    other: StyleReferenceJob,
    book_id: str,
    *,
    by_kind: Mapping[str, Callable[[StyleReferenceJob, str], DomainError]],
    default: Callable[[StyleReferenceJob, str], DomainError],
) -> DomainError:
    """建 / 续作业时撞上的活动作业 ``other`` → 按它的种类给对应的 409（``by_kind`` 里没有的种类用 ``default``）。"""
    factory = by_kind.get(str(other.kind), default)
    return factory(other, book_id)


class JobRun:
    """处理器基类。子类实现 ``_run``（作业主体）、``finish_cancelled``、``finish_failed``（各自的终态附带写：
    书的状态 / run 行），并设 ``operation``（云策略检查报错时说的操作名）。"""

    operation: str = ""

    def __init__(self, session: Session, claimed: ClaimedJob, service: StyleJobService) -> None:
        self.session = session
        self.claimed = claimed
        self.service = service
        self.book_id = str(claimed.book_id or "")
        self.client: Any = None

    # ---- lifecycle ------------------------------------------------------
    def run(self) -> None:
        """异常 → 终态：取消 / 领域错误 / 其他异常在这里收尾（子类的钩子写附带状态）；丢所有权与进程中断原样上抛
        （框架回滚 / 放回队列）；进程退出期间冒出来的异常一律按中断处理。"""
        try:
            self._run()
        except (JobLost, JobInterrupted):
            self.session.rollback()
            raise
        except JobCancelled:
            self.finish_cancelled()
        except DomainError as exc:
            if is_worker_interruption(exc, self.claimed):
                self.session.rollback()
                raise JobInterrupted(self.claimed.job_id) from exc
            self.finish_failed(
                code=exc.code,
                message=str(exc.message),
                retryable=bool(getattr(exc, "retryable", False) or (exc.details or {}).get("retryable")),
                details=exc.details if isinstance(exc.details, Mapping) else None,
            )
        except Exception as exc:  # noqa: BLE001 — 作业边界：记失败（可续跑），游标保留
            if is_worker_interruption(exc, self.claimed):
                self.session.rollback()
                raise JobInterrupted(self.claimed.job_id) from exc
            logger.exception("%s job %s failed", self.claimed.kind, self.claimed.job_id)
            self.finish_failed(
                code=str(getattr(exc, "code", None) or JOB_FAILED_CODE),
                message=f"{type(exc).__name__}: {exc}",
                retryable=True,
            )

    def _run(self) -> None:
        raise NotImplementedError

    def finish_cancelled(self) -> None:
        raise NotImplementedError

    def finish_failed(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError

    # ---- checkpoints ----------------------------------------------------
    def fresh(self) -> None:
        """结束当前读事务，之后读到别的连接刚提交的取消 / 删书。"""
        self.session.commit()

    def check_continue(self) -> None:
        self.fresh()
        self.service.check_continue(self.claimed)

    def book(self) -> StyleReferenceBook:
        """重读书行（populate_existing）；书没了 = 已不是这个作业的主人（删书同事务里取消了作业）。"""
        book = self.session.execute(
            select(StyleReferenceBook)
            .where(StyleReferenceBook.book_id == self.book_id)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()
        if book is None:
            raise JobLost(self.claimed.job_id)
        return book

    def pre_call_check(self, routes: Mapping[str, Any]) -> None:
        """每次派发前：作业仍归我且没被取消、书还在、书的云策略仍允许这些节点的**实际路由**。"""
        self.check_continue()
        ensure_cloud_llm_allowed(self.book(), operation=self.operation, routes=dict(routes), llm_client=self.client)

    def checkpoint(self, *, commit: bool = True, **progress: Any) -> None:
        """一个进度写：落空（被取消收尾、被清扫重排、书被删）→ ``JobLost``；写成了就（缺省）立刻提交。"""
        if not self.service.progress(self.claimed, **progress):
            raise JobLost(self.claimed.job_id)
        if commit:
            self.session.commit()

    def save_cursor(
        self,
        cursor: Mapping[str, Any],
        *,
        progress: Mapping[str, Any] | None = None,
        write: Callable[[], None] | None = None,
    ) -> None:
        """游标（条件写，本事务第一条写）→ 调用方的写 → 进度，一次提交；游标写落空 → ``JobLost``。"""
        self.fresh()
        if not self.service.save_cursor(self.claimed, cursor):
            self.session.rollback()
            raise JobLost(self.claimed.job_id)
        if write is not None:
            write()
        if progress is not None:
            self.service.progress(self.claimed, **dict(progress))
        self.session.commit()

    # ---- calls ----------------------------------------------------------
    def call_in_worker(self, fn: Callable[[], Any], *, thread_prefix: str, poll_seconds: float) -> Any:
        """单个调用放到守护线程里，等待时照样查取消 / 所有权（取消在 ``poll_seconds`` 内生效，在飞的结果丢弃）。"""
        pool = DaemonCallPool(max_workers=1, thread_name_prefix=f"{thread_prefix}_{self.claimed.job_id[-6:]}")
        try:
            future = pool.submit(fn)
            while True:
                done, _ = wait([future], timeout=poll_seconds)
                if done:
                    return future.result()
                self.check_continue()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def run_parallel(
        self,
        items: Sequence[Any],
        submit: Callable[[DaemonCallPool, Any, threading.Event], Future],
        apply: Callable[[Any, Any], Exception | None],
        *,
        max_inflight: int,
        poll_seconds: float,
        thread_prefix: str,
        stop_when: Callable[[], bool] | None = None,
        order_key: Callable[[Any], Any] | None = None,
    ) -> None:
        """见模块文档。``submit(pool, item, stop)`` 在作业线程里派发一个调用（自己做派发前检查）；``apply(item, result)``
        在作业线程里落库，返回这一项自带的失败（分类：重试用尽仍有段没分出来）或 None；``order_key`` 给同一轮回来的
        多个结果定落库顺序（缺省按完成集合的顺序）。"""
        pending = list(items)
        in_flight: dict[Future, Any] = {}
        stop = threading.Event()
        failure: Exception | None = None
        # 调用跑在守护线程里：进程退出（--reload / 停服）不等在飞的网络请求，作业由框架放回队列
        pool = DaemonCallPool(max_workers=max_inflight, thread_name_prefix=f"{thread_prefix}_{self.claimed.job_id[-6:]}")
        try:
            while pending or in_flight:
                while pending and failure is None and len(in_flight) < max_inflight and not (stop_when and stop_when()):
                    item = pending.pop(0)
                    in_flight[submit(pool, item, stop)] = item
                if not in_flight:
                    break
                done, _ = wait(list(in_flight), timeout=poll_seconds, return_when=FIRST_COMPLETED)
                if not done:
                    self.check_continue()
                    continue
                finished = sorted(done, key=lambda future: order_key(in_flight[future])) if order_key else list(done)
                for future in finished:
                    item = in_flight.pop(future)
                    error = future.exception()
                    if error is not None:
                        if not isinstance(error, Exception) or isinstance(error, _PASSTHROUGH):
                            raise error
                        failure = failure or error
                        continue
                    own_failure = apply(item, future.result())
                    if own_failure is not None:
                        failure = failure or own_failure
                if failure is not None or (stop_when and stop_when()):
                    pending.clear()
            if failure is not None:
                raise failure
        finally:
            stop.set()
            pool.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "JobRun",
    "JobStopped",
    "conflict_error_by_kind",
    "retry_attempts",
]
