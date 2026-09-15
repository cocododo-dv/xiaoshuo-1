"""参考书导入进度(2026-09-15):进程内登记簿 → 分类批次 / 导入阶段汇报 → 轮询端点。

导入是一条同步请求,浏览器在几十秒到几分钟里只有一条挂起的 fetch;进度按客户端幂等键登记,
``GET …/imports/{key}/progress`` 读出。这些用例钉住:百分比单调且在阶段边界处诚实、分类批次
总量按三步(或余段超上限时只有锚定集)申报、同键在途不被改写、终态可被最后一次轮询读到。
"""

from __future__ import annotations

import io
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from novel_system.services.style_reference import import_progress
from novel_system.services.style_reference.import_progress import (
    ImportProgressRegistry,
    NullImportProgress,
    get_import_progress,
    reset_import_progress_registry,
)
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.segmentation import classify_paragraphs
from novel_system.services.style_reference.segmentation import llm as segmentation_llm
from tests.style_reference_route_helpers import fake_import_llm, wait_book_status

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


# ---------------------------------------------------------------- classifier


class _RecordingProgress:
    def __init__(self) -> None:
        self.plan: tuple[int, str] | None = None
        self.batches: list[str] = []

    def classify_plan(self, total_batches: int, mode: str) -> None:
        self.plan = (total_batches, mode)

    def classify_batch_done(self, node_id: str) -> None:
        self.batches.append(node_id)


def _paragraphs(count: int) -> list[tuple[int, int, str]]:
    bodies = ["他把灯拧暗了一点,窗外的雨声就显得更近。", "「你来了。」她说。", "几日后。"]
    return [(i * 40, i * 40 + 30, bodies[i % len(bodies)]) for i in range(count)]


def test_classifier_reports_three_step_plan_and_every_batch(
    fake_paragraph_classifier, monkeypatch, session
) -> None:
    monkeypatch.setattr(segmentation_llm, "ANCHOR_SIZE", 25)
    progress = _RecordingProgress()
    classify_paragraphs(
        _paragraphs(25 + 30),
        llm_enabled=True,
        llm_client=fake_paragraph_classifier(),
        session=session,
        scope_id="sr_book_progress_llm",
        progress=progress,
    )
    # 锚定集 1 批 × 强模型 + 1 批 × 快模型 + 余段 30 段 = 2 批
    assert progress.plan == (4, "llm")
    assert progress.batches == [
        segmentation_llm.NODE_ANCHOR,
        segmentation_llm.NODE_BULK,
        segmentation_llm.NODE_BULK,
        segmentation_llm.NODE_BULK,
    ]


def test_classifier_plan_has_no_fast_pass_when_the_book_fits_the_anchor_set(
    fake_paragraph_classifier, monkeypatch, session
) -> None:
    """2026-09-15 严格 LLM:没有余段上限;书不超过锚定集时只有强模型一遍(没有余段就不做对照)。"""
    monkeypatch.setattr(segmentation_llm, "ANCHOR_SIZE", 25)
    progress = _RecordingProgress()
    classify_paragraphs(
        _paragraphs(20),
        llm_enabled=True,
        llm_client=fake_paragraph_classifier(),
        session=session,
        scope_id="sr_book_progress_small",
        progress=progress,
    )
    assert progress.plan == (1, "llm")
    assert progress.batches == [segmentation_llm.NODE_ANCHOR]


def test_classifier_heuristic_path_reports_zero_batches() -> None:
    """离线夹具模式(llm_enabled=False,只有测试与本地语料工具会这样调):申报 0 批。"""
    progress = _RecordingProgress()
    classify_paragraphs(_paragraphs(3), llm_enabled=False, progress=progress)
    assert progress.plan == (0, "heuristic")
    assert progress.batches == []


# ---------------------------------------------------------------- ingest phases


def test_ingest_walks_the_phases_in_order(session) -> None:
    class PhaseRecorder(NullImportProgress):
        def __init__(self) -> None:
            self.events: list[Any] = []

        def phase(self, name: str) -> None:
            self.events.append(("phase", name))

        def set_totals(self, **fields: Any) -> None:
            self.events.append(("totals", fields))

        def classify_plan(self, total_batches: int, mode: str) -> None:
            self.events.append(("plan", total_batches, mode))

    recorder = PhaseRecorder()
    service = IngestService(session, llm_enabled=False, progress=recorder)
    result = service.ingest_upload(
        raw_bytes=SAMPLE_TXT,
        file_name="sample.txt",
        title="进度",
        author_label=None,
        cloud_policy="local_only",
    )
    phases = [event[1] for event in recorder.events if event[0] == "phase"]
    assert phases == ["prepare", "classify", "metrics", "persist"]
    totals = {k: v for event in recorder.events if event[0] == "totals" for k, v in event[1].items()}
    assert totals["chars_total"] == result.book.total_chars
    assert totals["paragraphs_total"] == result.paragraphs_count
    assert totals["title"] == "进度"
    assert ("plan", 0, "heuristic") in recorder.events


# ---------------------------------------------------------------- route


def _wait_progress_finished(client: TestClient, key: str, seconds: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    snap: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"{PREFIX}/imports/{key}/progress")
        if resp.status_code == 200:
            snap = resp.json()["data"]["progress"]
            if snap["status"] != "running":
                return snap
        time.sleep(0.02)
    raise AssertionError(f"progress {key} never finished: {snap}")


def test_import_upload_records_progress_readable_after_completion(client: TestClient) -> None:
    """2026-09-15 严格 LLM:导入请求只做准备,分类任务接着同一个键继续汇报到完成。"""
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
    book_id = resp.json()["data"]["book"]["book_id"]
    assert resp.json()["data"]["book"]["status"] == "ingesting"
    assert resp.json()["data"]["classification"]["state"] == "queued"

    snap = _wait_progress_finished(client, "sr-import-progress-ok")
    assert snap["status"] == "succeeded"
    assert snap["percent"] == 100
    assert snap["phase"] == "done"
    assert snap["book_id"] == book_id
    assert snap["title"] == "进度书"
    assert snap["source"] == "upload"
    assert snap["paragraphs_count"] == resp.json()["data"]["paragraphs_count"]
    assert snap["classify"]["mode"] == "llm"
    assert snap["classify"]["batches_done"] == snap["classify"]["batches_total"] >= 1
    book = wait_book_status(client, book_id)
    assert book["classification"]["state"] == "done"


def test_import_upload_failure_is_visible_in_progress(client: TestClient) -> None:
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
    snap = client.get(f"{PREFIX}/imports/sr-import-progress-bad/progress").json()["data"]["progress"]
    assert snap["status"] == "failed"
    assert snap["error"]["code"] == "STYLE_REFERENCE_BOOK_FORMAT_UNSUPPORTED"
    assert snap["book_id"] is None


def test_unknown_or_malformed_import_key_is_404(client: TestClient) -> None:
    missing = client.get(f"{PREFIX}/imports/sr-import-never/progress")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "STYLE_REFERENCE_IMPORT_PROGRESS_UNKNOWN"
    malformed = client.get(f"{PREFIX}/imports/{'x' * 200}/progress")
    assert malformed.status_code == 404
    assert malformed.json()["error"]["code"] == "STYLE_REFERENCE_IMPORT_PROGRESS_UNKNOWN"


def test_import_progress_is_polled_live_while_the_upload_runs(
    client: TestClient, monkeypatch, fake_paragraph_classifier
) -> None:
    """在途轮询:分类器每批完成时从另一条请求读进度,批次数单调增加。"""
    import novel_system.api.routes.style_reference as sr_routes

    observed: list[dict[str, Any]] = []

    class ObservingClient(fake_paragraph_classifier):
        def generate(self, request):  # noqa: ANN001
            response = super().generate(request)
            # 分类批次完成前,登记簿里已经有在跑的条目(prepare → classify)。
            snap = get_import_progress("sr-import-progress-live")
            observed.append(snap)
            return response

    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (ObservingClient(), True))
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
    # 分类在后台任务里跑(接着请求登记的同一条进度),等它结束再看观察记录
    final = _wait_progress_finished(client, "sr-import-progress-live")
    assert observed, "分类器至少被调用一次"
    assert all(snap is not None and snap["status"] == "running" for snap in observed)
    assert all(snap["phase"] == "classify" for snap in observed)
    assert [snap["classify"]["batches_done"] for snap in observed] == list(range(len(observed)))
    assert observed[0]["classify"]["batches_total"] == len(observed)
    assert final["status"] == "succeeded"
    assert final["classify"]["batches_done"] == final["classify"]["batches_total"] == len(observed)
    assert final["classify"]["llm_calls"] == len(observed)
