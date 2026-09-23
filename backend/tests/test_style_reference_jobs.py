"""风格参考 v3：统一持久作业（jobs.py）的生命周期、所有权、取消与清扫。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from novel_system.db.models import StyleReferenceBook, StyleReferenceJob
from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.style_reference import jobs as jobs_module
from novel_system.services.style_reference.jobs import (
    JOB_ALREADY_ACTIVE_CODE,
    JOB_CANCELLED_CODE,
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


def _book(session, book_id: str = "sr_book_jobs") -> str:
    session.add(
        StyleReferenceBook(
            book_id=book_id,
            title="作业测试书",
            source_kind="upload",
            cloud_policy="allow_full_cloud",
            text_checksum=f"sum-{book_id}",
            total_chars=100,
            status="ready",
            stats_json={},
        )
    )
    session.flush()
    return book_id


@pytest.fixture(autouse=True)
def _reset_handlers():
    saved = dict(jobs_module._HANDLERS)
    yield
    jobs_module._HANDLERS.clear()
    jobs_module._HANDLERS.update(saved)


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
