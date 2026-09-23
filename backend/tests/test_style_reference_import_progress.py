"""参考书操作进度:进程内登记簿(尚未迁出的操作)+ 导入的进度(作业表上的分类作业)。

2026-09-23 风格参考 v3 起,导入 / 重新分类的整本分类是作业表上的分类作业,进度在作业行上;导入响应带
``job_id``,界面在 ``GET …/activity`` 里按 ``job:<id>`` 跟进度(P6a 起旧的 ``GET …/imports/{key}/progress``
兼容轮询删除)。这些用例钉住:登记簿的百分比单调且在阶段边界处诚实、同键在途不被改写、终态可被最后一次
轮询读到;作业条目在分类过程中与跑完之后都读得到,请求在建作业之前就失败时没有条目。
"""

from __future__ import annotations

import io
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference import import_progress
from novel_system.services.style_reference.import_job import find_job_by_op_key, legacy_progress_snapshot
from novel_system.services.style_reference.import_progress import (
    ImportProgressRegistry,
    NullImportProgress,
    reset_import_progress_registry,
)
from tests.style_reference_route_helpers import fake_import_llm, install_fake_classifier, wait_book_status

PREFIX = "/api/v2/style-reference"

SAMPLE_TXT = """这是一段较长的叙述文字,介绍清晨场景与人物心情,字数足以触发分段。

他说:"今天天气不错。"

我心里想着昨天的事情,觉得有些不安。

记得那年她还在的时候。

雪花从天空飘落。
""".encode("utf-8")


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_import_progress_registry()
    yield
    reset_import_progress_registry()


# ---------------------------------------------------------------- registry


def test_registry_percent_is_monotonic_and_honest_at_phase_boundaries() -> None:
    registry = ImportProgressRegistry()
    reporter = registry.start("sr-import-a", title="书")
    assert registry.get("sr-import-a")["percent"] == 0
    assert registry.get("sr-import-a")["status"] == "running"

    reporter.phase("prepare")
    reporter.set_totals(chars_total=12_345, paragraphs_total=300)
    assert registry.get("sr-import-a")["percent"] == 0

    reporter.phase("classify")
    reporter.classify_plan(4, "llm")
    assert registry.get("sr-import-a")["percent"] == 5  # prepare 完成,分类 0/4
    seen = [5]
    for node in ("anchor", "anchor", "bulk", "bulk"):
        reporter.classify_batch_done(node)
        seen.append(registry.get("sr-import-a")["percent"])
    assert seen == sorted(seen)
    assert seen[-1] == 75  # prepare 5 + classify 70
    snap = registry.get("sr-import-a")
    assert snap["classify"] == {
        "mode": "llm",
        "batches_done": 4,
        "batches_total": 4,
        "llm_calls": 4,
        "node_id": "bulk",
    }
    assert snap["chars_total"] == 12_345 and snap["paragraphs_total"] == 300
    assert snap["phase_label"] == "段落分类"

    reporter.phase("metrics")
    assert registry.get("sr-import-a")["percent"] == 75
    reporter.phase("persist")
    assert registry.get("sr-import-a")["percent"] == 90
    reporter.succeed(book_id="sr_book_x", paragraphs_count=300)
    snap = registry.get("sr-import-a")
    assert snap["status"] == "succeeded"
    assert snap["phase"] == "done"
    assert snap["percent"] == 100
    assert snap["book_id"] == "sr_book_x"
    assert snap["paragraphs_count"] == 300
    assert snap["error"] is None
    assert snap["elapsed_seconds"] >= 0


def test_registry_heuristic_plan_counts_as_finished_classification() -> None:
    registry = ImportProgressRegistry()
    reporter = registry.start("sr-import-h")
    reporter.phase("classify")
    reporter.classify_plan(0, "anchors_then_heuristic")
    assert registry.get("sr-import-h")["percent"] == 75
    assert registry.get("sr-import-h")["eta_seconds"] is None


def test_registry_failure_freezes_percent_and_keeps_the_error() -> None:
    registry = ImportProgressRegistry()
    reporter = registry.start("sr-import-f")
    reporter.phase("classify")
    reporter.classify_plan(2, "llm")
    reporter.classify_batch_done("anchor")
    reporter.fail(code="LLM_HTTP_RETRYABLE_FAILURE", message="provider down")
    snap = registry.get("sr-import-f")
    assert snap["status"] == "failed"
    assert snap["percent"] == 40
    assert snap["error"] == {"code": "LLM_HTTP_RETRYABLE_FAILURE", "message": "provider down"}


def test_registry_never_lets_a_second_request_overwrite_a_running_import() -> None:
    registry = ImportProgressRegistry()
    first = registry.start("sr-import-dup", title="第一条")
    duplicate = registry.start("sr-import-dup", title="重放")
    assert isinstance(duplicate, NullImportProgress)
    duplicate.fail(code="IDEMPOTENCY_REQUEST_IN_PROGRESS", message="in flight")
    assert registry.get("sr-import-dup")["status"] == "running"
    assert registry.get("sr-import-dup")["title"] == "第一条"
    first.succeed(book_id="b", paragraphs_count=1)
    # 终态之后同一个键可以再登记(作者再导一次)。
    assert not isinstance(registry.start("sr-import-dup"), NullImportProgress)
    # 空键 / 超长键不登记,拿到空实现。
    assert isinstance(registry.start(""), NullImportProgress)
    assert isinstance(registry.start("k" * 129), NullImportProgress)


def test_registry_evicts_finished_entries_after_ttl(monkeypatch) -> None:
    registry = ImportProgressRegistry(finished_ttl_seconds=100, max_entries=2)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(import_progress.time, "monotonic", lambda: clock["now"])
    registry.start("old").succeed(book_id=None, paragraphs_count=None)
    clock["now"] += 101
    assert registry.get("old") is None
    # 超过容量时只淘汰最早的终态条目,在跑的永远保留。
    running = registry.start("running")
    registry.start("done-1").succeed(book_id=None, paragraphs_count=None)
    clock["now"] += 1
    registry.start("done-2").succeed(book_id=None, paragraphs_count=None)
    assert registry.get("running") is not None
    assert registry.get("done-1") is None
    assert registry.get("done-2") is not None
    running.succeed(book_id=None, paragraphs_count=None)


# ---------------------------------------------------------------- route


def _job_entry(client: TestClient, job_id: str) -> dict[str, Any] | None:
    items = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    return next((item for item in items if item["key"] == f"job:{job_id}"), None)


def _wait_job_finished(client: TestClient, job_id: str, seconds: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    entry: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        entry = _job_entry(client, job_id)
        if entry is not None and entry["status"] not in ("queued", "running"):
            return entry
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never finished: {entry}")


def test_import_upload_progress_is_the_classify_job_in_the_activity_list(client: TestClient) -> None:
    """导入请求只做准备并建分类作业;响应带 job_id,活动清单里的 ``job:<id>`` 条目跑完读到终态。"""
    with fake_import_llm():
        resp = client.post(
            f"{PREFIX}/books/import-upload",
            files={"file": ("sample.txt", io.BytesIO(SAMPLE_TXT), "text/plain")},
            data={
                "title": "进度书",
                "cloud_policy": "segments_only",
                "rights_declaration": json.dumps({"analysis_rights": True, "send_rights": True}),
            },
            headers={"X-Idempotency-Key": "sr-import-progress-ok"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        book_id = data["book"]["book_id"]
        assert data["book"]["status"] == "ingesting"
        assert data["classification"]["state"] == "queued" and data["job_id"]
        entry = _wait_job_finished(client, data["job_id"])
    assert entry["status"] == "succeeded" and entry["percent"] == 100.0
    assert entry["kind"] == "classify" and entry["kind_label"] == "段落分类" and entry["mode"] == "import"
    assert entry["book_id"] == book_id and entry["title"] == "进度书"
    assert entry["steps"]["done"] == entry["steps"]["total"] >= 1
    book = wait_book_status(client, book_id)
    assert book["classification"]["state"] == "succeeded"


def test_an_import_that_fails_before_the_job_exists_has_no_activity_entry(client: TestClient) -> None:
    with fake_import_llm():
        resp = client.post(
            f"{PREFIX}/books/import-upload",
            files={"file": ("sample.pdf", io.BytesIO(SAMPLE_TXT), "application/pdf")},
            data={
                "title": "坏格式",
                "cloud_policy": "segments_only",
                "rights_declaration": json.dumps({"analysis_rights": True, "send_rights": True}),
            },
            headers={"X-Idempotency-Key": "sr-import-progress-bad"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_BOOK_FORMAT_UNSUPPORTED"
    items = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    assert not any(item.get("title") == "坏格式" for item in items)


def test_the_legacy_import_progress_poll_is_gone(client: TestClient) -> None:
    """旧前端的导入轮询别名删除(P6a):导入进度一律看响应里的 job_id + 活动清单。"""
    assert client.get(f"{PREFIX}/imports/sr-import-never/progress").status_code == 404


def test_import_progress_is_live_while_the_job_classifies(
    client: TestClient, monkeypatch, fake_paragraph_classifier
) -> None:
    """在途轮询:分类器每次被调用时,作业行上的进度都在 running / classify,批数单调增加。"""
    from novel_system.services.style_reference import import_job

    monkeypatch.setattr(import_job, "PARALLEL_BATCHES", 1)
    observed: list[dict[str, Any]] = []

    class ObservingClient(fake_paragraph_classifier):
        def generate(self, request):  # noqa: ANN001
            with SessionLocal() as session:
                job = find_job_by_op_key(session, "sr-import-progress-live")
                observed.append(legacy_progress_snapshot(job, title=None, total_chars=None))
            return super().generate(request)

    install_fake_classifier(monkeypatch, ObservingClient())
    resp = client.post(
        f"{PREFIX}/books/import-upload",
        files={"file": ("sample.txt", io.BytesIO(SAMPLE_TXT), "text/plain")},
        data={
            "title": "在途",
            "cloud_policy": "segments_only",
            "rights_declaration": json.dumps({"analysis_rights": True, "send_rights": True}),
        },
        headers={"X-Idempotency-Key": "sr-import-progress-live"},
    )
    assert resp.status_code == 200, resp.text
    final = _wait_job_finished(client, resp.json()["data"]["job_id"])
    assert observed, "分类器至少被调用一次"
    assert all(snap["status"] == "running" and snap["phase"] == "classify" for snap in observed)
    assert [snap["classify"]["batches_done"] for snap in observed] == list(range(len(observed)))
    assert final["status"] == "succeeded"
    assert final["steps"]["done"] == final["steps"]["total"] == len(observed)
    assert final["llm_calls"] == len(observed)
