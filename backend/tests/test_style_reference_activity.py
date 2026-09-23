"""参考书活动面板(2026-09-15;2026-09-23 v3 作业表):进度登记簿 + 活动清单 + 各操作的进度接线。

钉住:
- 登记簿对每种 kind 的百分比单调、阶段文案、活跃守卫查询;
- ``GET …/activity`` 合并作业表(学习文风作业的七步进度、分类作业)与登记簿条目 / 回测报告;旧抽取 run 不再单列;
- 重新分类按幂等键登记进度;
- 回测 worker 先落本地三路再跑语义路,报告在 running 时已带部分结果;
- 源文重合过滤的 n-gram 索引与逐行 ``check_plagiarism`` 判定完全一致。
(合成画像 / 应用画像建 RAG 索引的进度随旧学习链路删除;学习作业见 test_style_reference_learn_job.py。)
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from novel_system.db.models import StyleReferenceValidationReport, utcnow
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.import_progress import (
    ImportProgressRegistry,
    NullImportProgress,
    get_import_progress,
    reset_import_progress_registry,
    start_import_progress,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import ValidateRequest, ValidationMode
from novel_system.services.style_reference.validation import ValidationOrchestrator
from novel_system.services.style_reference.validation.plagiarism import (
    CorpusOverlapIndex,
    check_plagiarism,
)
from tests.style_reference_route_helpers import install_fake_classifier, wait_book_status
from tests.test_style_reference_routes import _import_book
from tests.test_style_reference_validation_runner import _seed_profile, _wait_for_async

PREFIX = "/api/v2/style-reference"


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_import_progress_registry()
    yield
    reset_import_progress_registry()


# ---------------------------------------------------------------- registry


def test_registry_generic_kinds_percent_monotonic_and_labels() -> None:
    registry = ImportProgressRegistry()
    synth = registry.start(
        "k-synth", kind="synthesize", title="书", book_id="b1", target_id="run1"
    )
    seen = [registry.get("k-synth")["percent"]]
    for phase in ("collect", "voice", "llm", "filter", "derive", "persist", "index"):
        synth.phase(phase)
        seen.append(registry.get("k-synth")["percent"])
    assert seen == sorted(seen)
    assert seen[-1] == 85  # 8 + 12 + 45 + 5 + 10 + 5,index 阶段未申报步骤 → 0
    synth.set_steps(250, 1000, label="段")
    snap = registry.get("k-synth")
    assert snap["steps"] == {"done": 250, "total": 1000, "label": "段"}
    assert snap["percent"] == 89  # 85 + 15 × 0.25
    synth.set_steps(100, 1000)  # done 只增不减
    assert registry.get("k-synth")["steps"]["done"] == 250
    synth.set_steps(0, 3, label="粒度")  # 换了步骤计划(总数不同)→ 重新计数
    assert registry.get("k-synth")["steps"] == {"done": 0, "total": 3, "label": "粒度"}
    assert registry.get("k-synth")["percent"] == 85
    synth.set_steps(3, 3)
    assert registry.get("k-synth")["percent"] == 99
    synth.set_steps(1000, 1000, label="段")
    assert snap["kind_label"] == "合成画像"
    assert snap["phase_label"] == "建立 RAG 索引"
    assert snap["book_id"] == "b1" and snap["target_id"] == "run1"

    other = registry.start("k-synth-2", kind="synthesize")
    other.phase("llm", detail="第 2 次")
    other.llm_call("style_ref_synthesize_profile")
    other.llm_call("style_ref_synthesize_profile")
    second = registry.get("k-synth-2")
    assert second["phase_label"] == "模型合成 · 第 2 次"
    assert second["llm_calls"] == 2
    assert second["percent"] == 20  # collect + voice 已过,llm 阶段内无步骤

    synth.succeed(profile_id="p1")
    snap = registry.get("k-synth")
    assert snap["status"] == "succeeded"
    assert snap["percent"] == 100
    assert snap["result"] == {"profile_id": "p1"}

    # 活跃守卫查询只看在跑的条目,按 kind / book / target 过滤
    assert registry.find_running(kind="synthesize")["op_key"] == "k-synth-2"
    assert registry.find_running(kind="synthesize", book_id="b1") is None
    assert registry.find_running(kind="reclassify") is None

    with pytest.raises(ValueError):
        registry.start("k-bad", kind="nope")
    with pytest.raises(ValueError):
        other.phase("classify")

    rec = registry.start("k-rec", kind="reclassify", book_id="b1")
    rec.phase("purge")
    assert registry.get("k-rec")["percent"] == 0
    rec.phase("classify")
    rec.classify_plan(4, "llm")
    rec.classify_batch_done("a")
    rec.classify_batch_done("a")
    assert registry.get("k-rec")["percent"] == 45  # purge 5 + classify 80 × 0.5
    rec.phase("metrics")
    assert registry.get("k-rec")["percent"] == 85
    rec.phase("persist")
    assert registry.get("k-rec")["percent"] == 95
    assert registry.get("k-rec")["classify"]["batches_done"] == 2
    assert registry.get("k-rec")["kind_label"] == "重新分类"
    # 续跑的任务:已完成的批次计入进度,但不计入均速(ETA 只看本次跑的)
    resumed = registry.start("k-rec-resume", kind="reclassify", book_id="b1")
    resumed.phase("classify")
    resumed.classify_plan(10, "llm", done=6)
    assert registry.get("k-rec-resume")["classify"]["batches_done"] == 6
    assert registry.get("k-rec-resume")["percent"] == 53  # 5 + 80 × 0.6
    assert registry.get("k-rec-resume")["eta_seconds"] is None
    resumed.classify_batch_done("a")
    assert registry.get("k-rec-resume")["classify"]["batches_done"] == 7
    assert registry.get("k-rec-resume")["eta_seconds"] is not None

    assert {s["op_key"] for s in registry.snapshots()} == {"k-synth", "k-synth-2", "k-rec", "k-rec-resume"}

    null = NullImportProgress()
    null.phase("llm", detail="x")
    null.set_steps(1, 2)
    null.llm_call()
    null.succeed(profile_id="p")


# ---------------------------------------------------------------- overlap index


def test_corpus_overlap_index_matches_check_plagiarism_exactly() -> None:
    rng = random.Random(7)
    alphabet = "的一是在不了有和人这中大为上个国我以要他时来用们"
    corpus = [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(30, 80))) for _ in range(40)
    ]
    index = CorpusOverlapIndex(corpus, threshold_chars=8)
    lines: list[str] = []
    for _ in range(150):
        if rng.random() < 0.5:
            src = rng.choice(corpus)
            start = rng.randint(0, len(src) - 12)
            lines.append(src[start : start + rng.randint(6, 12)])
        else:
            lines.append("".join(rng.choice(alphabet) for _ in range(rng.randint(5, 20))))
    lines.extend(["，。！", "", "他，的 一 是"])
    positives = 0
    for line in lines:
        expected = not check_plagiarism(line, corpus, ngram_size=6, threshold_chars=8).passed
        assert index.contains_overlap(line, ngram_size=6) is expected, line
        if not index.may_overlap(line):
            assert expected is False
        positives += int(expected)
    assert positives > 0 and positives < len(lines)
    assert index.ngram_count > 0
    assert CorpusOverlapIndex([], threshold_chars=8).contains_overlap("随便一行") is False


# ---------------------------------------------------------------- activity endpoint


def test_activity_endpoint_merges_registry_and_job_rows(client: TestClient) -> None:
    """活动清单:作业表(学习文风作业的七步进度)+ 登记簿;旧的抽取 run 行不再单列(只作血缘)。"""
    from novel_system.services.style_reference.jobs import JOB_KIND_LEARN, StyleJobService

    book_id = _import_book(client)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_run(
            run_id="sr_run_act_legacy",
            book_id=book_id,
            status="running",
            phase="extract",
            dispatch_state="running",
            coverage_json={"progress": {"sub_dims_total": 4, "sub_dims_done": 1}},
            heartbeat_at=utcnow(),
            started_at=(now - timedelta(seconds=40)).isoformat(),
        )
        service = StyleJobService(session)
        job = service.create(JOB_KIND_LEARN, book_id=book_id, phase="queued")
        claimed = service.claim(job.job_id)
        service.progress(claimed, phase="extract", phase_label="学习文风 · 逐层读原文", done=3, total=10, llm_calls_delta=2)
        service.save_cursor(claimed, {"phases_done": ["windows", "select"]})
        session.commit()
    start_import_progress("sr-import-act", title="导入中的书", source="upload").phase("classify")

    resp = client.get(f"{PREFIX}/activity")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["server_time"]
    items = data["items"]
    by_key = {item["key"]: item for item in items}
    assert not any(str(key).startswith("run:") for key in by_key)

    learning = by_key[f"job:{job.job_id}"]
    assert learning["kind"] == "learn" and learning["kind_label"] == "学习文风" and learning["status"] == "running"
    assert learning["title"] == "测试" and learning["book_id"] == book_id
    assert learning["phase_label"] == "学习文风 · 逐层读原文" and learning["percent"] == 30.0
    assert learning["steps"] == {"done": 3, "total": 10} and learning["llm_calls"] == 2
    assert learning["phases_done"] == ["windows", "select"] and learning["cancellable"] is True

    imported = by_key["sr-import-act"]
    assert imported["kind"] == "import" and imported["status"] == "running"
    assert imported["phase"] == "classify" and imported["title"] == "导入中的书"

    statuses = [item["status"] for item in items]
    # 在跑的全部排在终态前面(_import_book 留下的 imp_1 导入条目是终态)
    assert statuses == sorted(statuses, key=lambda s: 0 if s == "running" else 1)
    assert "imp_1" in by_key and by_key["imp_1"]["status"] == "succeeded"


# ---------------------------------------------------------------- reclassify


def test_reclassify_route_registers_progress(
    client: TestClient, monkeypatch, fake_paragraph_classifier
) -> None:
    fake = install_fake_classifier(monkeypatch, fake_paragraph_classifier(rule="default"))
    book_id = _import_book(client, fake)
    resp = client.post(
        f"{PREFIX}/books/{book_id}/reclassify",
        headers={"X-Idempotency-Key": "sr-reclassify-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "classifying"
    # 重新分类是作业表上的分类作业:活动清单里立刻能看到(作业行 + 幂等键别名),完成后书 ready
    activity = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    entry = next(item for item in activity if item["key"] == "sr-reclassify-1")
    assert entry["kind_label"] == "重新分类" and entry["book_id"] == book_id
    job_entry = next(item for item in activity if item["key"] == entry["compat_alias_of"])
    assert job_entry["kind"] == "classify" and job_entry["mode"] == "reclassify"
    wait_book_status(client, book_id)
    deadline = time.monotonic() + 15
    done = job_entry
    while done is not None and done["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.02)
        items = client.get(f"{PREFIX}/activity").json()["data"]["items"]
        done = next((item for item in items if item["key"] == job_entry["key"]), None)
    assert done is not None and done["status"] == "succeeded" and done["percent"] == 100.0
    assert done["book_id"] == book_id and done["title"] == "测试" and done["mode"] == "reclassify"
    assert done["steps"]["total"] >= 1 and done["steps"]["done"] == done["steps"]["total"]
    assert job_entry["job_id"] == resp.json()["data"]["job_id"]


# ---------------------------------------------------------------- validation


def test_async_validation_persists_local_results_before_the_semantic_pass(
    fake_validation_llm,
) -> None:
    profile_id = _seed_profile("activity_val")
    gate = threading.Event()
    entered = threading.Event()

    class Gated(fake_validation_llm):
        def generate(self, request):  # noqa: ANN001
            if (getattr(request, "node_id", "") or "") == "style_ref_validate_semantic":
                entered.set()
                assert gate.wait(timeout=20)
            return super().generate(request)

    llm = Gated("with_quote")
    with SessionLocal() as session:
        orch = ValidationOrchestrator(session, llm_client=llm, llm_enabled=True)
        resp = orch.validate(
            profile_id,
            ValidateRequest(generated_text="一段生成的中文文本测试", mode=ValidationMode.ASYNC_FULL),
        )
        session.commit()
    report_id = resp.report_id
    try:
        assert entered.wait(timeout=20), "语义路应被调用"
        with SessionLocal() as session:
            row = session.get(StyleReferenceValidationReport, report_id)
            assert row is not None
            assert row.status == "running" and not row.verdict
            assert isinstance(row.quantitative_json, list)
            assert row.plagiarism_json.get("passed") is True
        mid = get_import_progress(f"validate:{report_id}")
        assert mid is not None
        assert mid["kind"] == "validate" and mid["status"] == "running"
        assert mid["phase"] == "semantic" and mid["target_id"] == report_id
    finally:
        gate.set()
    assert _wait_for_async(report_id, max_seconds=15.0)
    deadline = time.monotonic() + 10
    final = get_import_progress(f"validate:{report_id}")
    while final is not None and final["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
        final = get_import_progress(f"validate:{report_id}")
    assert final is not None and final["status"] == "succeeded", final
    assert final["result"]["report_id"] == report_id
    assert final["llm_calls"] == 2


