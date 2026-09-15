"""严格 LLM(2026-09-15):导入 / 重新分类的整本 LLM 分类是可续跑的后台任务。

钉住:
- 没有 LLM 不导入(409),「仅本机」的书要求本地模型;
- 分类任务逐批落库并推进游标:某批 LLM 失败 → 书 failed(原因 + 游标保留),「继续分类」只重跑
  未完成的批次;
- 取消在下一批边界生效,活动条目显示已取消并可续跑;删书时在跑的任务退出;
- 启动恢复:心跳过期的 running 重新排队续跑,queued 直接重派,没有 LLM 时标 failed;
- 分类未完成的书不能抽取;活动清单按 durable 游标给出百分比 / 预计剩余;
- 全量三路回测没有 LLM 就 409,同步快路径仍是显式的无 LLM 模式。
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from novel_system.db.models import StyleReferenceBook
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference import import_job
from novel_system.services.style_reference.import_progress import (
    get_import_progress,
    reset_import_progress_registry,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.segmentation import llm as segmentation_llm
from tests.conftest import build_fake_paragraph_classifier
from tests.style_reference_route_helpers import (
    PREFIX,
    fake_import_llm,
    import_book,
    wait_book_status,
)
from tests.test_style_reference_routes import _seed_full_chain

LONG_TEXT = "\n\n".join(
    f"第{i + 1}段。潮水在夜里退去,露出一行脚印,她数着脚印往前走,每一步都比上一步更接近那句没人认领的对不起。"
    for i in range(60)
).encode("utf-8")


@pytest.fixture(autouse=True)
def _small_anchor_set(monkeypatch):
    """锚定集缩到 25 段:60 段的书 = 强模型 1 批 + 快模型 1 批 + 余段 2 批。"""
    monkeypatch.setattr(segmentation_llm, "ANCHOR_SIZE", 25)
    monkeypatch.setattr(import_job, "ANCHOR_SIZE", 25)
    reset_import_progress_registry()
    yield
    reset_import_progress_registry()


def _failing_on(call_no: int):
    """第 ``call_no`` 次调用抛错的假分类器。"""

    class Failing(build_fake_paragraph_classifier()):
        def generate(self, request):  # noqa: ANN001
            if self.call_count + 1 == call_no:
                self.call_count += 1
                raise RuntimeError("relay down")
            return super().generate(request)

    return Failing(rule="default")


def _gated(call_no: int):
    """第 ``call_no`` 次调用卡在 gate 上,让测试在批边界之间做事。"""
    gate = threading.Event()
    entered = threading.Event()

    class Gated(build_fake_paragraph_classifier()):
        def generate(self, request):  # noqa: ANN001
            if self.call_count + 1 == call_no:
                entered.set()
                assert gate.wait(timeout=20)
            return super().generate(request)

    return Gated(rule="default"), gate, entered


def _state(client: TestClient, book_id: str) -> dict:
    book = client.get(f"{PREFIX}/books/{book_id}").json()["data"]["book"]
    return book["stats_json"]["classification"]


def _wait_state(client: TestClient, book_id: str, states: tuple[str, ...], seconds: float = 20.0) -> dict:
    deadline = time.monotonic() + seconds
    state = _state(client, book_id)
    while state.get("state") not in states and time.monotonic() < deadline:
        time.sleep(0.02)
        state = _state(client, book_id)
    assert state.get("state") in states, state
    return state


# ---------------------------------------------------------------- resume after failure


def test_import_job_resumes_from_the_cursor_after_a_failed_batch(client: TestClient) -> None:
    failing = _failing_on(3)  # 第三次调用 = 余段第一批
    book_id = import_book(client, key="job-fail", text=LONG_TEXT, fake=failing, wait=False)
    book = wait_book_status(client, book_id, statuses=("failed",))
    state = book["stats_json"]["classification"]
    assert state["state"] == "failed"
    assert state["error"]["code"] == "STYLE_REFERENCE_CLASSIFICATION_FAILED" or state["error"]["code"] == "STYLE_REF_LLM_GENERATE_FAILED"
    assert state["batches_done"] == 2 and state["batches_total"] == 4
    assert state["cursor"] == {"phase": "rest", "offset": 0}
    assert state["fast_model_agreement"] == 1.0 and state["rest_node"] == segmentation_llm.NODE_BULK
    snap = get_import_progress("job-fail")
    assert snap["status"] == "failed" and snap["kind"] == "import"
    activity = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    entry = next(item for item in activity if item["key"] == "job-fail")
    assert entry["status"] == "failed" and entry["resumable"] is True and entry["retryable"] is True
    assert entry["classify"]["batches_done"] == 2 and entry["classify"]["batches_total"] == 4
    # 锚定段已经落库(强模型结果),余段还是未分类
    with SessionLocal() as session:
        rows = StyleReferenceRepository(session).list_paragraphs(book_id)
    assert {row.paragraph_type for row in rows[:25]} != {"unclassified"}
    assert {row.paragraph_type for row in rows[25:]} == {"unclassified"}

    # 继续分类:只重跑没完成的两批
    healthy = build_fake_paragraph_classifier()(rule="default")
    with fake_import_llm(healthy):
        resp = client.post(
            f"{PREFIX}/books/{book_id}/reclassify",
            json={"resume": True},
            headers={"X-Idempotency-Key": "job-fail-resume"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["resume"] is True
    book = wait_book_status(client, book_id)
    state = book["stats_json"]["classification"]
    assert state["state"] == "done" and state["batches_done"] == 4 and state["attempt"] == 2
    assert healthy.call_count == 2
    calibration = book["stats_json"]["classifier_calibration"]
    assert calibration["llm_classified_paragraphs"] == 60
    assert calibration["heuristic_classified_paragraphs"] == 0
    assert calibration["rest_classifier"] == "fast_llm"
    assert book["stats_json"]["paragraph_type_distribution"]
    assert book["stats_json"]["voice_signature"]
    with SessionLocal() as session:
        rows = StyleReferenceRepository(session).list_paragraphs(book_id)
    assert all(row.paragraph_type != "unclassified" for row in rows)


# ---------------------------------------------------------------- cancel


def test_cancel_stops_at_the_next_batch_boundary_and_resume_finishes(client: TestClient) -> None:
    gated, gate, entered = _gated(2)
    book_id = import_book(client, key="job-cancel", text=LONG_TEXT, fake=gated, wait=False)
    assert entered.wait(timeout=20)
    resp = client.post(
        f"{PREFIX}/books/{book_id}/classification/cancel",
        headers={"X-Idempotency-Key": "job-cancel-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["cancel_requested"] is True
    gate.set()
    book = wait_book_status(client, book_id, statuses=("failed",))
    state = book["stats_json"]["classification"]
    assert state["state"] == "cancelled"
    assert state["error"]["code"] == import_job.CANCEL_ERROR_CODE
    assert state["batches_done"] == 2  # 第二批完成后才看到取消
    activity = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    entry = next(item for item in activity if item["key"] == "job-cancel")
    assert entry["status"] == "cancelled" and entry["resumable"] is True
    assert gated.call_count == 2

    # 分类没完成的书不能抽取
    with fake_import_llm(gated):
        run = client.post(
            f"{PREFIX}/books/{book_id}/runs",
            json={"background": True},
            headers={"X-Idempotency-Key": "job-cancel-run"},
        )
    assert run.status_code == 409
    assert run.json()["error"]["code"] == "STYLE_REFERENCE_BOOK_NOT_READY"

    healthy = build_fake_paragraph_classifier()(rule="default")
    with fake_import_llm(healthy):
        resp = client.post(
            f"{PREFIX}/books/{book_id}/reclassify",
            json={"resume": True},
            headers={"X-Idempotency-Key": "job-cancel-resume"},
        )
    assert resp.status_code == 200, resp.text
    wait_book_status(client, book_id)
    assert healthy.call_count == 2


def test_delete_book_while_classifying_makes_the_worker_exit(client: TestClient) -> None:
    gated, gate, entered = _gated(1)
    book_id = import_book(client, key="job-delete", text=LONG_TEXT, fake=gated, wait=False)
    assert entered.wait(timeout=20)
    resp = client.delete(f"{PREFIX}/books/{book_id}", headers={"X-Idempotency-Key": "job-delete-1"})
    assert resp.status_code == 200, resp.text
    gate.set()
    deadline = time.monotonic() + 20
    snap = get_import_progress("job-delete")
    while snap is not None and snap["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        snap = get_import_progress("job-delete")
    assert snap is not None and snap["status"] == "failed"
    assert snap["error"]["code"] == import_job.CANCEL_ERROR_CODE
    assert client.get(f"{PREFIX}/books/{book_id}").status_code == 404


# ---------------------------------------------------------------- recovery


def _seed_ingesting_book(session, seed: str, *, state: str, heartbeat_at: str | None, paragraphs: int = 3) -> str:
    repo = StyleReferenceRepository(session)
    book_id = f"sr_book_job_{seed}"
    job = import_job.build_classification_state(kind="import", op_key=f"key-{seed}", total_paragraphs=paragraphs)
    job["state"] = state
    job["heartbeat_at"] = heartbeat_at
    job["started_at"] = heartbeat_at
    job["batches_done"] = 1
    repo.create_book(
        book_id=book_id,
        title=seed,
        source_kind="upload",
        cloud_policy="segments_only",
        text_checksum=f"chk_job_{seed}",
        total_chars=300,
        status="ingesting",
        stats_json={
            "rights_declaration": {"declared": True, "analysis_rights": True, "send_rights": True},
            "classification": job,
        },
        # worker 与心跳线程都只续 updated_at 列:恢复按它判断 worker 是否还活着
        updated_at=heartbeat_at or datetime.now(UTC).isoformat(),
    )
    for idx in range(paragraphs):
        repo.create_paragraph(
            paragraph_id=f"sr_para_job_{seed}_{idx}",
            book_id=book_id,
            paragraph_index=idx,
            paragraph_type="unclassified",
            start_offset=idx * 10,
            end_offset=idx * 10 + 9,
            text=f"第{idx}段的正文。",
            char_count=9,
            classifier_confidence=0.0,
        )
    return book_id


def test_recover_classification_jobs_requeues_stale_running_and_fails_without_llm() -> None:
    now = datetime.now(UTC)
    fresh = now.isoformat()
    stale = (now - timedelta(seconds=import_job.CLASSIFICATION_STALE_SECONDS + 60)).isoformat()
    with SessionLocal() as session:
        stale_id = _seed_ingesting_book(session, "stale", state="running", heartbeat_at=stale)
        fresh_id = _seed_ingesting_book(session, "fresh", state="running", heartbeat_at=fresh)
        queued_id = _seed_ingesting_book(session, "queued", state="queued", heartbeat_at=None)
        session.commit()

    dispatched: list[tuple[str, str | None]] = []
    with SessionLocal() as session:
        summary = import_job.recover_classification_jobs(
            session,
            llm_client=object(),
            llm_enabled=True,
            dispatch=lambda book_id, _client, op_key: dispatched.append((book_id, op_key)),
        )
    assert set(summary["dispatched"]) == {stale_id, queued_id}
    assert summary["active_skipped"] == [fresh_id]
    assert summary["failed"] == []
    assert dict(dispatched) == {stale_id: "key-stale", queued_id: "key-queued"}
    with SessionLocal() as session:
        stale_book = session.get(StyleReferenceBook, stale_id)
        state = import_job.classification_state(stale_book)
    assert state["state"] == "queued" and state["attempt"] == 2
    assert state["error"]["code"] == import_job.INTERRUPTED_ERROR_CODE

    with SessionLocal() as session:
        no_llm_id = _seed_ingesting_book(session, "nollm", state="queued", heartbeat_at=None)
        session.commit()
    with SessionLocal() as session:
        summary = import_job.recover_classification_jobs(
            session, llm_client=None, llm_enabled=False, dispatch=lambda *a: None
        )
    assert no_llm_id in summary["failed"]
    with SessionLocal() as session:
        book = session.get(StyleReferenceBook, no_llm_id)
        assert book.status == "failed"
        assert import_job.classification_state(book)["error"]["code"] == import_job.LLM_REQUIRED_AFTER_RESTART_CODE


# ---------------------------------------------------------------- activity + guards


def test_activity_lists_a_classifying_book_with_cursor_progress(client: TestClient, session) -> None:
    now = datetime.now(UTC).isoformat()
    book_id = _seed_ingesting_book(session, "act", state="running", heartbeat_at=now, paragraphs=3)
    book = session.get(StyleReferenceBook, book_id)
    state = import_job.classification_state(book)
    state.update({"batches_done": 3, "batches_total": 10, "batch_seconds": [10.0, 20.0, 30.0], "llm_calls": 3})
    book.stats_json = {**book.stats_json, "classification": state}
    session.commit()

    items = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    entry = next(item for item in items if item["key"] == "key-act")
    assert entry["kind"] == "import" and entry["status"] == "running" and entry["title"] == "act"
    assert entry["percent"] == 26  # prepare 5 + classify 70 × 0.3
    assert entry["steps"] == {"done": 3, "total": 10, "label": "批"}
    assert entry["eta_seconds"] == 140.0  # 均速 20 s × 剩余 7 批
    assert entry["cancellable"] is True and entry["classify"]["mode"] == "llm"
    assert entry["book_id"] == book_id

    # 分类未完成:抽取 409
    with fake_import_llm():
        run = client.post(
            f"{PREFIX}/books/{book_id}/runs",
            json={"background": True},
            headers={"X-Idempotency-Key": "act-run"},
        )
    assert run.status_code == 409
    assert run.json()["error"]["code"] == "STYLE_REFERENCE_BOOK_NOT_READY"
    assert run.json()["error"]["details"]["author_action"]["view"] == "styleref"

    # 正在分类时再点重新分类:409
    with fake_import_llm():
        again = client.post(
            f"{PREFIX}/books/{book_id}/reclassify",
            headers={"X-Idempotency-Key": "act-reclassify"},
        )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "STYLE_REFERENCE_CLASSIFICATION_ALREADY_ACTIVE"


def test_validate_route_refuses_async_full_without_llm_but_keeps_sync_only(
    client: TestClient, monkeypatch
) -> None:
    import novel_system.api.routes.style_reference as sr_routes

    book_id = import_book(client, key="val-book")
    _, _, profile_id = _seed_full_chain(book_id)
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (None, False))
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/validate",
        json={"generated_text": "一段完全原创的全新文本表达", "mode": "async_full"},
        headers={"X-Idempotency-Key": "val-async-no-llm"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_LLM_REQUIRED"
    sync = client.post(
        f"{PREFIX}/profiles/{profile_id}/validate",
        json={"generated_text": "一段完全原创的全新文本表达", "mode": "sync_only"},
        headers={"X-Idempotency-Key": "val-sync-no-llm"},
    )
    assert sync.status_code == 200, sync.text
    assert sync.json()["data"]["sync_result"]["mode_executed"] == "sync_only"
