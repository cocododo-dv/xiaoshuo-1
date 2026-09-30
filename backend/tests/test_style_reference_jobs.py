"""风格参考 v3：统一持久作业（jobs.py）的生命周期、所有权、取消与清扫。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from novel_system.db.models import StyleReferenceJob
from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.style_reference import jobs as jobs_module
from novel_system.services.style_reference.jobs import (
    JOB_ALREADY_ACTIVE_CODE,
    JOB_CANCELLED_CODE,
    JOB_KIND_CHECK,
    JOB_KIND_CLASSIFY,
    JOB_KIND_LEARN,
    JobCancelled,
    JobLost,
    STATE_CANCELLED,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    StyleJobService,
    job_activity_entry,
    register_job_handler,
    run_job_inline,
)
from tests.style_reference_factories import make_book


def _book(session, book_id: str = "sr_book_jobs") -> str:
    return make_book(session, book_id, title="作业测试书", text_checksum=f"sum-{book_id}", total_chars=100)


@pytest.fixture(autouse=True)
def _reset_handlers():
    saved = dict(jobs_module._HANDLERS)
    saved_hooks = dict(jobs_module._CANCEL_HOOKS)
    saved_rules = dict(jobs_module._RESUMABLE_RULES)
    saved_params = dict(jobs_module._FINISHED_PARAMS_RULES)
    yield
    jobs_module._HANDLERS.clear()
    jobs_module._HANDLERS.update(saved)
    jobs_module._CANCEL_HOOKS.clear()
    jobs_module._CANCEL_HOOKS.update(saved_hooks)
    jobs_module._RESUMABLE_RULES.clear()
    jobs_module._RESUMABLE_RULES.update(saved_rules)
    jobs_module._FINISHED_PARAMS_RULES.clear()
    jobs_module._FINISHED_PARAMS_RULES.update(saved_params)


def _stale(session, job_id: str, *, seconds: float = 3600) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    session.execute(update(StyleReferenceJob).where(StyleReferenceJob.job_id == job_id).values(heartbeat_at=old))
    session.flush()


def test_lifecycle_claim_progress_succeed_and_activity_entry(session) -> None:
    book_id = _book(session)
    service = StyleJobService(session)
    job = service.create(JOB_KIND_CLASSIFY, book_id=book_id, params={"mode": "import"}, phase="prepare")
    assert job.state == STATE_QUEUED and job.attempt == 0

    claimed = service.claim(job.job_id)
    assert claimed is not None and claimed.attempt == 1 and claimed.params == {"mode": "import"}
    # 第二个工人拿不到（条件写）
    assert service.claim(job.job_id) is None

    assert service.progress(claimed, phase="classify", phase_label="段落分类", done=3, total=10, llm_calls_delta=2)
    assert service.save_cursor(claimed, {"done_batches": [0, 1, 2]})
    entry = job_activity_entry(service.get(job.job_id, fresh=True))
    assert entry["status"] == STATE_RUNNING and entry["percent"] == 30.0
    assert entry["steps"] == {"done": 3, "total": 10} and entry["llm_calls"] == 2
    assert entry["cancellable"] and not entry["resumable"] and not entry["stalled"]

    assert service.succeed(claimed, {"paragraphs": 10})
    done = service.get(job.job_id, fresh=True)
    assert done.state == STATE_SUCCEEDED and done.result_json == {"paragraphs": 10} and done.owner_token is None
    assert done.cursor_json == {"done_batches": [0, 1, 2]}
    # 结束后旧所有者的任何写都落空
    assert not service.heartbeat(claimed)
    assert not service.progress(claimed, done=4)


def test_one_active_job_per_book_and_kind(session) -> None:
    book_id = _book(session)
    service = StyleJobService(session)
    first = service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    with pytest.raises(DomainError) as excinfo:
        service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    assert excinfo.value.code == JOB_ALREADY_ACTIVE_CODE
    assert excinfo.value.details["job_id"] == first.job_id
    # 不同 kind 不互斥
    service.create(JOB_KIND_LEARN, book_id=book_id)


def test_cancel_queued_finishes_immediately_and_running_waits_for_checkpoint(session) -> None:
    book_id = _book(session)
    service = StyleJobService(session)
    queued = service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    cancelled = service.request_cancel(queued.job_id)
    assert cancelled.state == STATE_CANCELLED and cancelled.error_json["code"] == JOB_CANCELLED_CODE

    running = service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    claimed = service.claim(running.job_id)
    after = service.request_cancel(running.job_id)
    assert after.state == STATE_RUNNING and after.cancel_requested == 1
    with pytest.raises(JobCancelled):
        service.check_continue(claimed)
    assert service.finish_cancelled(claimed)
    assert service.get(running.job_id, fresh=True).state == STATE_CANCELLED


def test_cancel_of_a_dead_worker_job_finishes_in_the_request(session) -> None:
    """工人已死（心跳过期）的作业，取消请求当场收尾——不再卡在「取消中」。"""
    book_id = _book(session)
    service = StyleJobService(session)
    job = service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    claimed = service.claim(job.job_id)
    _stale(session, job.job_id)
    after = service.request_cancel(job.job_id)
    assert after.state == STATE_CANCELLED
    with pytest.raises(JobLost):
        service.check_continue(claimed)


def test_sweep_requeues_stale_running_and_the_old_worker_loses_ownership(session) -> None:
    book_id = _book(session)
    service = StyleJobService(session)
    fresh_job = service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    fresh_claim = service.claim(fresh_job.job_id)
    stale_job = service.create(JOB_KIND_LEARN, book_id=book_id)
    stale_claim = service.claim(stale_job.job_id)
    _stale(session, stale_job.job_id)

    queued_ids = service.sweep()
    assert stale_job.job_id in queued_ids and fresh_job.job_id not in queued_ids
    assert service.get(fresh_job.job_id, fresh=True).state == STATE_RUNNING
    requeued = service.get(stale_job.job_id, fresh=True)
    assert requeued.state == STATE_QUEUED and requeued.owner_token is None
    # 旧工人被清扫之后写不进去，检查点报丢失
    assert not service.progress(stale_claim, done=1, total=2)
    with pytest.raises(JobLost):
        service.check_continue(stale_claim)
    # 新工人从游标处接着跑（attempt 递增）
    again = service.claim(stale_job.job_id)
    assert again is not None and again.attempt == stale_claim.attempt + 1
    assert service.heartbeat(fresh_claim)


def test_cancel_all_for_book_stops_old_workers(session) -> None:
    book_id = _book(session)
    service = StyleJobService(session)
    job = service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    claimed = service.claim(job.job_id)
    assert service.cancel_all_for_book(book_id) == [job.job_id]
    assert not service.save_cursor(claimed, {"x": 1})
    with pytest.raises(JobLost):
        service.check_continue(claimed)


def test_requeue_keeps_the_cursor_and_merges_params(session) -> None:
    book_id = _book(session)
    service = StyleJobService(session)
    job = service.create(JOB_KIND_CLASSIFY, book_id=book_id, params={"mode": "import"})
    claimed = service.claim(job.job_id)
    service.save_cursor(claimed, {"done": [0, 1]})
    service.fail(claimed, code="X", message="boom", retryable=True)
    entry = job_activity_entry(service.get(job.job_id, fresh=True))
    assert entry["resumable"] and entry["error"]["code"] == "X"
    requeued = service.requeue(job.job_id, params_update={"resume": True})
    assert requeued.state == STATE_QUEUED and requeued.cursor_json == {"done": [0, 1]}
    assert requeued.params_json == {"mode": "import", "resume": True} and requeued.error_json is None


def test_resumable_follows_the_rule_of_each_kind(session) -> None:
    """活动条目的 ``resumable`` 按这类作业登记的规则（B10-01）：分类按缺省（失败 / 取消的能续），学习看失败的
    ``error.retryable``，对照检查从不续跑（失败了是「重新检查」，建新作业）。"""
    from novel_system.services.style_reference.workers import install_workers

    install_workers()  # 登记各自的规则（lifespan 里也是它）
    book_id = _book(session)
    service = StyleJobService(session)

    def failed(kind: str, *, retryable: bool) -> StyleReferenceJob:
        job = service.create(kind, book_id=book_id if kind != JOB_KIND_CHECK else None, allow_parallel=True)
        service.fail(service.claim(job.job_id), code="X", message="boom", retryable=retryable)
        return service.get(job.job_id, fresh=True)

    assert job_activity_entry(failed(JOB_KIND_CLASSIFY, retryable=False))["resumable"] is True
    assert job_activity_entry(failed(JOB_KIND_LEARN, retryable=True))["resumable"] is True
    assert job_activity_entry(failed(JOB_KIND_LEARN, retryable=False))["resumable"] is False
    assert job_activity_entry(failed(JOB_KIND_CHECK, retryable=True))["resumable"] is False
    cancelled = service.create(JOB_KIND_CHECK)
    service.request_cancel(cancelled.job_id)
    assert job_activity_entry(service.get(cancelled.job_id, fresh=True))["resumable"] is False


def test_requeue_refuses_a_live_running_job(session) -> None:
    book_id = _book(session)
    service = StyleJobService(session)
    job = service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    service.claim(job.job_id)
    with pytest.raises(DomainError) as excinfo:
        service.requeue(job.job_id)
    assert excinfo.value.code == JOB_ALREADY_ACTIVE_CODE
    _stale(session, job.job_id)
    assert service.requeue(job.job_id).state == STATE_QUEUED


def _create_committed(kind: str = JOB_KIND_CLASSIFY) -> str:
    with SessionLocal() as db:
        book_id = _book(db, book_id=f"sr_book_{kind}_{uuid.uuid4().hex[:8]}")
        job = StyleJobService(db).create(kind, book_id=book_id)
        db.commit()
        return job.job_id


def test_worker_framework_success_domain_error_and_cancel_paths() -> None:
    seen: list[int] = []

    def handler(session, claimed, service):
        seen.append(claimed.attempt)
        service.progress(claimed, phase="work", done=1, total=1)
        service.succeed(claimed, {"ok": True})

    register_job_handler(JOB_KIND_CLASSIFY, handler)
    ok_id = _create_committed()
    run_job_inline(ok_id)
    with SessionLocal() as db:
        ok = db.get(StyleReferenceJob, ok_id)
        assert ok.state == STATE_SUCCEEDED and ok.result_json == {"ok": True}
    assert seen == [1]

    def failing(session, claimed, service):
        raise DomainError("STYLE_REFERENCE_CLASSIFICATION_FAILED", "模型没按要求回答", status_code=502, details={"batch": 3})

    register_job_handler(JOB_KIND_CLASSIFY, failing)
    fail_id = _create_committed()
    run_job_inline(fail_id)
    with SessionLocal() as db:
        failed = db.get(StyleReferenceJob, fail_id)
        assert failed.state == STATE_FAILED
        assert failed.error_json["code"] == "STYLE_REFERENCE_CLASSIFICATION_FAILED"
        assert failed.error_json["details"] == {"batch": 3}

    def cancelling(session, claimed, service):
        with SessionLocal() as other:
            StyleJobService(other).request_cancel(claimed.job_id)
            other.commit()
        service.check_continue(claimed)

    register_job_handler(JOB_KIND_CLASSIFY, cancelling)
    cancel_id = _create_committed()
    run_job_inline(cancel_id)
    with SessionLocal() as db:
        assert db.get(StyleReferenceJob, cancel_id).state == STATE_CANCELLED


def test_the_framework_and_job_run_record_a_failure_the_same_way() -> None:
    """失败记什么只有一条规则（jobs.job_failure）：处理器直接抛出（对照检查走这条）与经 JobRun.run（分类 / 学习）
    记下的错误码、说法、retryable、details 一样——details 里说可以重试的领域错误两边都记 retryable。"""
    from novel_system.services.style_reference.job_runtime import JobRun
    from novel_system.services.style_reference.jobs import job_failure

    error = DomainError("STYLE_REFERENCE_X_FAILED", "评审没给分", status_code=502, details={"retryable": True, "n": 1})

    def raw(session, claimed, service):
        raise error

    class _Run(JobRun):
        def _run(self) -> None:
            raise error

        def finish_cancelled(self) -> None:  # pragma: no cover — 本例不取消
            raise AssertionError

        def finish_failed(self, *, code, message, retryable, details=None) -> None:
            self.session.rollback()
            self.service.fail(self.claimed, code=code, message=message, retryable=retryable, details=details)
            self.session.commit()

    recorded = []
    for handler in (raw, lambda session, claimed, service: _Run(session, claimed, service).run()):
        register_job_handler(JOB_KIND_CLASSIFY, handler)
        job_id = _create_committed()
        run_job_inline(job_id)
        with SessionLocal() as db:
            recorded.append(dict(db.get(StyleReferenceJob, job_id).error_json))
    assert recorded[0] == recorded[1]
    assert recorded[0]["retryable"] is True and recorded[0]["details"] == {"retryable": True, "n": 1}
    code, message, retryable, details = job_failure(error)
    assert (code, message, retryable, details) == ("STYLE_REFERENCE_X_FAILED", "评审没给分", True, {"retryable": True, "n": 1})
    assert job_failure(ValueError("boom"))[:3] == ("STYLE_REFERENCE_JOB_FAILED", "ValueError: boom", True)


def test_worker_that_loses_ownership_writes_nothing() -> None:
    def handler(session, claimed, service):
        with SessionLocal() as other:
            other_service = StyleJobService(other)
            other_service.cancel_all_for_book(claimed.book_id)
            other.commit()
        service.check_continue(claimed)  # → JobLost
        service.succeed(claimed, {"should": "not happen"})  # pragma: no cover

    register_job_handler(JOB_KIND_CLASSIFY, handler)
    job_id = _create_committed()
    run_job_inline(job_id)
    with SessionLocal() as db:
        job = db.get(StyleReferenceJob, job_id)
        assert job.state == STATE_CANCELLED and job.result_json is None


# ---------------------------------------------------------------------------
# 2026-09-23 复核修正：进程退出 ≠ 作业失败；先插后查的互斥；清扫收尾带取消标记的过期作业；条件放回队列
# ---------------------------------------------------------------------------


def _job(job_id: str) -> StyleReferenceJob:
    with SessionLocal() as db:
        job = db.get(StyleReferenceJob, job_id)
        db.expunge(job)
        return job


def test_worker_shutdown_puts_the_running_job_back_in_the_queue_with_its_cursor() -> None:
    """lifespan 结束（--reload / 停服）：工人代 +1，处理器在下一个检查点抛 JobInterrupted，作业回到 queued。"""

    def handler(session, claimed, service):
        service.save_cursor(claimed, {"done": [0, 1, 2]})
        session.commit()
        jobs_module.shutdown_job_workers()
        service.check_continue(claimed)  # → JobInterrupted
        service.succeed(claimed, {"should": "not happen"})  # pragma: no cover

    register_job_handler(JOB_KIND_CLASSIFY, handler)
    job_id = _create_committed()
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == STATE_QUEUED and job.owner_token is None and job.heartbeat_at is None
    assert job.cursor_json == {"done": [0, 1, 2]} and job.error_json is None and job.attempt == 1


def test_errors_raised_while_the_process_shuts_down_are_interruptions_not_failures() -> None:
    def handler(session, claimed, service):
        raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    register_job_handler(JOB_KIND_CLASSIFY, handler)
    job_id = _create_committed()
    run_job_inline(job_id)
    assert _job(job_id).state == STATE_QUEUED

    def domain_after_shutdown(session, claimed, service):
        jobs_module.shutdown_job_workers()
        raise DomainError("STYLE_REFERENCE_CLASSIFICATION_FAILED", "连接被关", status_code=502)

    register_job_handler(JOB_KIND_CLASSIFY, domain_after_shutdown)
    other_id = _create_committed()
    run_job_inline(other_id)
    assert _job(other_id).state == STATE_QUEUED


def test_keyboard_interrupt_releases_the_job_and_propagates() -> None:
    def handler(session, claimed, service):
        raise KeyboardInterrupt

    register_job_handler(JOB_KIND_CLASSIFY, handler)
    job_id = _create_committed()
    with pytest.raises(KeyboardInterrupt):
        run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == STATE_QUEUED and job.owner_token is None
    # 立刻就能续跑（不用等心跳过期）
    with SessionLocal() as db:
        assert StyleJobService(db).claim(job_id) is not None


def test_a_claim_taken_directly_is_not_affected_by_worker_shutdown(session) -> None:
    """测试 / 工具直接拿的认领没有工人代，不受 lifespan 结束影响。"""
    book_id = _book(session)
    service = StyleJobService(session)
    claimed = service.claim(service.create(JOB_KIND_CLASSIFY, book_id=book_id).job_id)
    jobs_module.shutdown_job_workers()
    service.check_continue(claimed)  # 不抛


def test_create_rechecks_after_the_insert_so_a_racing_request_cannot_add_a_second_job(session, monkeypatch) -> None:
    """先查后插的窗口：两个请求都查不到对方。插入之后（已拿写锁）再查一次，后到的看得见先到的。"""
    book_id = _book(session)
    service = StyleJobService(session)
    first = service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    real = StyleJobService.first_conflict

    def blind_precheck(self, book_id, *, kinds, excluding=None):
        # 模拟竞态：插入前的那次检查什么也没看见
        return None if excluding is None else real(self, book_id, kinds=kinds, excluding=excluding)

    monkeypatch.setattr(StyleJobService, "first_conflict", blind_precheck)
    with pytest.raises(DomainError) as excinfo:
        service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    assert excinfo.value.code == JOB_ALREADY_ACTIVE_CODE and excinfo.value.details["job_id"] == first.job_id
    with pytest.raises(DomainError) as cross:
        service.create(
            JOB_KIND_LEARN,
            book_id=book_id,
            exclusive_with=(JOB_KIND_CLASSIFY,),
            conflict_error=lambda other: DomainError("BOOK_BUSY", other.kind, status_code=409),
        )
    assert cross.value.code == "BOOK_BUSY" and cross.value.message == JOB_KIND_CLASSIFY


def test_sweep_finishes_a_stale_job_that_was_asked_to_cancel_and_runs_the_hook(session) -> None:
    hooked: list[str] = []
    register_job_handler(
        JOB_KIND_LEARN, lambda *_: None, on_cancelled=lambda _session, job: hooked.append(job.job_id)
    )
    book_id = _book(session)
    service = StyleJobService(session)
    job = service.create(JOB_KIND_LEARN, book_id=book_id)
    claimed = service.claim(job.job_id)
    service.request_cancel(job.job_id)  # 工人还活着：只置标记
    assert service.get(job.job_id, fresh=True).state == STATE_RUNNING
    _stale(session, job.job_id)  # 工人死了
    assert job.job_id not in service.sweep()
    finished = service.get(job.job_id, fresh=True)
    assert finished.state == STATE_CANCELLED and finished.owner_token is None
    assert hooked == [job.job_id]
    assert not service.heartbeat(claimed)


def test_cancel_finished_in_the_request_or_at_claim_runs_the_hook(session) -> None:
    hooked: list[str] = []
    register_job_handler(
        JOB_KIND_LEARN, lambda *_: None, on_cancelled=lambda _session, job: hooked.append(job.job_id)
    )
    book_id = _book(session)
    service = StyleJobService(session)
    queued = service.create(JOB_KIND_LEARN, book_id=book_id)
    service.request_cancel(queued.job_id)
    assert hooked == [queued.job_id]
    # 排队中被标了取消（例：清扫放回队列之前）→ 认领时收尾，同样跑钩子
    other = service.create(JOB_KIND_LEARN, book_id=_book(session, "sr_book_jobs_2"))
    session.execute(
        update(StyleReferenceJob).where(StyleReferenceJob.job_id == other.job_id).values(cancel_requested=1)
    )
    assert service.claim(other.job_id) is None
    assert service.get(other.job_id, fresh=True).state == STATE_CANCELLED
    assert hooked == [queued.job_id, other.job_id]


def test_requeue_is_conditional_and_never_resurrects_a_finished_job(session) -> None:
    book_id = _book(session)
    service = StyleJobService(session)
    job = service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    claimed = service.claim(job.job_id)
    service.succeed(claimed, {"ok": True})
    with pytest.raises(DomainError) as excinfo:
        service.requeue(job.job_id)
    assert excinfo.value.code == jobs_module.JOB_NOT_RESUMABLE_CODE
    assert service.get(job.job_id, fresh=True).state == STATE_SUCCEEDED


def test_daemon_call_pool_runs_calls_on_daemon_threads_and_refuses_after_shutdown() -> None:
    import threading

    pool = jobs_module.DaemonCallPool(max_workers=2, thread_name_prefix="t_pool")
    future = pool.submit(lambda: threading.current_thread().daemon)
    assert future.result(timeout=5) is True
    failing = pool.submit(lambda: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        failing.result(timeout=5)
    pool.shutdown(wait=False, cancel_futures=True)
    with pytest.raises(RuntimeError, match="after shutdown"):
        pool.submit(lambda: None)


def test_check_jobs_have_their_own_lane(monkeypatch) -> None:
    """对照检查不在几十分钟的分类 / 学习后面排队：派发到自己的线程池。"""
    started: list[str] = []
    monkeypatch.setattr(jobs_module, "_run_job", lambda job_id: started.append(job_id))
    jobs_module.shutdown_job_workers(wait=True)
    try:
        assert jobs_module.dispatch_job("sr_job_a", kind=JOB_KIND_CLASSIFY)
        assert jobs_module.dispatch_job("sr_job_b", kind=jobs_module.JOB_KIND_CHECK)
        assert set(jobs_module._EXECUTORS) == {"long", "check"}
    finally:
        jobs_module.shutdown_job_workers(wait=True)
    assert sorted(started) == ["sr_job_a", "sr_job_b"]


def test_activity_lists_every_active_job_however_many_finished_recently(session) -> None:
    """界面把 /activity 当完整清单：十分钟里结束的作业再多，也不能把一个还在跑的老作业挤出去。"""
    book_id = _book(session)
    service = StyleJobService(session)
    old_active = service.create(JOB_KIND_CLASSIFY, book_id=book_id)
    service.claim(old_active.job_id)
    for index in range(5):
        job = service.create(JOB_KIND_LEARN, book_id=_book(session, f"sr_book_jobs_done_{index}"))
        service.succeed(service.claim(job.job_id), {"ok": True})
    listed = [job.job_id for job in service.list_recent(limit=3)]
    assert old_active.job_id in listed
    assert len(listed) == 4  # 1 条在跑 + 至多 3 条刚结束


# ---------------------------------------------------------------------------
# 作业表的保留期与对照检查的参数（批准 #23，重评 R14）
# ---------------------------------------------------------------------------


def test_finished_check_jobs_keep_only_a_hash_of_the_checked_text(session) -> None:
    """对照检查作业不论怎么结束（成功 / 失败 / 排队中取消 / 工人死后取消），送检的原文都不再留在作业行上：只留与
    读数表同一个哈希和字数；其它种类的参数不动。"""
    from novel_system.services.hash_engine import sha256_text
    from novel_system.services.style_reference.workers import install_workers

    install_workers()
    book_id = _book(session)
    service = StyleJobService(session)
    text = "林昭把旧信压在案卷底下，雨城的钟敲了三下。"

    def check_job() -> StyleReferenceJob:
        return service.create(
            JOB_KIND_CHECK, book_id=book_id, allow_parallel=True, params={"target": "text", "text": text, "profile_id": "p1"}
        )

    scrubbed = {"target": "text", "profile_id": "p1", "text_sha256": sha256_text(text), "text_chars": len(text)}
    succeeded = check_job()
    service.succeed(service.claim(succeeded.job_id), {"reading_id": "r1"})
    failed = check_job()
    service.fail(service.claim(failed.job_id), code="X", message="boom")
    cancelled_in_queue = check_job()
    service.request_cancel(cancelled_in_queue.job_id)
    cancelled_at_checkpoint = check_job()
    service.finish_cancelled(service.claim(cancelled_at_checkpoint.job_id))
    dead_worker = check_job()
    service.claim(dead_worker.job_id)
    session.execute(
        update(StyleReferenceJob).where(StyleReferenceJob.job_id == dead_worker.job_id).values(cancel_requested=1)
    )
    _stale(session, dead_worker.job_id)
    service.sweep()
    for job in (succeeded, failed, cancelled_in_queue, cancelled_at_checkpoint, dead_worker):
        row = service.get(job.job_id, fresh=True)
        assert row.state in (STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED)
        assert row.params_json == scrubbed, row.state
    # 还在跑的检查作业要续读原文：结束之前不动
    running = check_job()
    service.claim(running.job_id)
    assert service.get(running.job_id, fresh=True).params_json["text"] == text
    # 分类作业没有这条规则：参数照旧
    classify = service.create(JOB_KIND_CLASSIFY, book_id=book_id, params={"mode": "import"})
    service.fail(service.claim(classify.job_id), code="X", message="boom")
    assert service.get(classify.job_id, fresh=True).params_json == {"mode": "import"}


def _job_at(
    session,
    kind: str,
    book_id: str,
    *,
    state: str,
    created_days_ago: float,
    finished_days_ago: float | None = None,
    params: dict | None = None,
) -> str:
    now = datetime.now(UTC)
    job = StyleJobService(session).create(kind, book_id=book_id, allow_parallel=True, params=params or {})
    values: dict = {"state": state, "created_at": (now - timedelta(days=created_days_ago)).isoformat()}
    if finished_days_ago is not None:
        values["finished_at"] = (now - timedelta(days=finished_days_ago)).isoformat()
    session.execute(update(StyleReferenceJob).where(StyleReferenceJob.job_id == job.job_id).values(**values))
    session.flush()
    return job.job_id


def test_job_retention_prunes_old_checks_and_superseded_runs_but_never_live_jobs(session) -> None:
    from novel_system.services.style_reference.cleanup import prune_style_jobs
    from novel_system.services.style_reference.learn_job import latest_learn_job
    from novel_system.services.style_reference.workers import install_workers

    install_workers()
    book_a = _book(session, "sr_book_ret_a")
    book_b = _book(session, "sr_book_ret_b")
    old_check = _job_at(session, JOB_KIND_CHECK, book_a, state=STATE_SUCCEEDED, created_days_ago=41, finished_days_ago=40)
    young_check = _job_at(
        session,
        JOB_KIND_CHECK,
        book_a,
        state=STATE_FAILED,
        created_days_ago=6,
        finished_days_ago=5,
        params={"target": "text", "text": "案卷里夹着一封旧信"},
    )
    queued_check = _job_at(session, JOB_KIND_CHECK, book_a, state=STATE_QUEUED, created_days_ago=60)
    old_learn = _job_at(session, JOB_KIND_LEARN, book_a, state=STATE_FAILED, created_days_ago=3, finished_days_ago=3)
    latest_learn = _job_at(session, JOB_KIND_LEARN, book_a, state=STATE_FAILED, created_days_ago=1, finished_days_ago=1)
    just_finished_classify = _job_at(
        session, JOB_KIND_CLASSIFY, book_a, state=STATE_SUCCEEDED, created_days_ago=0.01, finished_days_ago=0.001
    )
    running_classify = _job_at(session, JOB_KIND_CLASSIFY, book_a, state=STATE_RUNNING, created_days_ago=0.0005)
    older_running_learn = _job_at(session, JOB_KIND_LEARN, book_b, state=STATE_RUNNING, created_days_ago=2)
    newest_learn_b = _job_at(session, JOB_KIND_LEARN, book_b, state=STATE_SUCCEEDED, created_days_ago=1, finished_days_ago=1)
    old_classify_b = _job_at(session, JOB_KIND_CLASSIFY, book_b, state=STATE_CANCELLED, created_days_ago=9, finished_days_ago=9)
    latest_classify_b = _job_at(session, JOB_KIND_CLASSIFY, book_b, state=STATE_SUCCEEDED, created_days_ago=8, finished_days_ago=8)

    summary = prune_style_jobs(session)
    remaining = set(session.scalars(select(StyleReferenceJob.job_id)).all())
    assert remaining == {
        young_check,
        queued_check,  # 没结束的不删，排了多久都一样
        latest_learn,  # 每本书每种留最近一个：「继续学习」读的就是它
        just_finished_classify,  # 还在活动面板上（刚结束），不删
        running_classify,
        older_running_learn,  # 没结束的不删，即使不是最近一个
        newest_learn_b,
        latest_classify_b,
    }
    assert {old_check, old_learn, old_classify_b}.isdisjoint(remaining)
    assert summary["deleted_check_jobs"] == 1 and summary["deleted_superseded_jobs"] == 2
    # 规则上线之前就结束了的检查作业：原文在这里补着换成哈希
    assert summary["scrubbed_check_jobs"] == 1
    young = session.get(StyleReferenceJob, young_check)
    session.refresh(young)
    assert "text" not in young.params_json and young.params_json["text_chars"] == 9
    assert latest_learn_job(session, book_a).job_id == latest_learn
    # 再跑一遍什么也不做
    again = prune_style_jobs(session)
    assert (again["deleted_check_jobs"], again["deleted_superseded_jobs"], again["scrubbed_check_jobs"]) == (0, 0, 0)


def test_job_retention_is_a_daily_maintenance_task() -> None:
    from novel_system.services.style_reference import cleanup
    from novel_system.services.style_reference.workers import install_workers

    install_workers()
    task, interval = jobs_module._MAINTENANCE[cleanup.JOB_RETENTION_MAINTENANCE_TASK]
    assert task is cleanup.run_job_retention and interval == 24 * 3600
    assert cleanup.CHECK_JOB_RETENTION_DAYS == 30

