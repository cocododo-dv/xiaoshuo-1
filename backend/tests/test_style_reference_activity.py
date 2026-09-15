"""参考书活动面板(2026-09-15):进度登记簿泛化 + 活动清单 + 各操作的进度接线。

钉住:
- 登记簿对每种 kind 的百分比单调、阶段文案、活跃守卫查询;
- 抽取 run 写子维粒度进度(``coverage_json["progress"]`` 的 sub_dims_* / llm_calls /
  sub_dim_seconds),活动条目据此给出百分比与预计剩余;
- ``GET …/activity`` 合并登记簿条目与 durable 行(在跑的 run、十分钟内结束的 run、回测报告);
- 重新分类 / 合成画像按幂等键登记进度,同书第二份合成被 409 拒绝;
- 回测 worker 先落本地三路再跑语义路,报告在 running 时已带部分结果;
- apply 不再在请求里建 RAG 索引,提交后由后台 worker 建并登记进度;
- 源文重合过滤的 n-gram 索引与逐行 ``check_plagiarism`` 判定完全一致。
"""

from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import novel_system.api.routes.style_reference as sr_routes
from novel_system.db.models import StyleReferenceValidationReport, utcnow
from novel_system.db.session import SessionLocal
from novel_system.services.review_effects import run_deferred_dispatches
from novel_system.services.style_reference import rag as rag_module
from novel_system.services.style_reference.activity import run_activity_entry
from novel_system.services.style_reference.dimensions import Layer
from novel_system.services.style_reference.import_progress import (
    ImportProgressRegistry,
    NullImportProgress,
    find_running_operation,
    get_import_progress,
    reset_import_progress_registry,
    start_import_progress,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.run_orchestrator import RunOrchestrator
from novel_system.services.style_reference.schemas import ValidateRequest, ValidationMode
from novel_system.services.style_reference.validation import ValidationOrchestrator
from novel_system.services.style_reference.validation.plagiarism import (
    CorpusOverlapIndex,
    check_plagiarism,
)
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.test_review_cards import _card, _create_project, _post
from tests.style_reference_route_helpers import wait_book_status
from tests.test_style_reference_routes import _import_book, _seed_full_chain
from tests.test_style_reference_run_orchestrator import _ingest as _ingest_book
from tests.test_style_reference_synthesizer import _ingest_with_finding
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


# ---------------------------------------------------------------- extraction progress


def test_extract_run_writes_sub_dimension_progress(fake_extractor_llm) -> None:
    book_id = _ingest_book("activity_progress")
    client = fake_extractor_llm("default")
    with SessionLocal() as session:
        orch = RunOrchestrator(session, llm_client=client, llm_enabled=True)
        result = orch.start_extract_run(book_id, layers=[Layer.LANGUAGE], force=True)
        session.commit()
        run = StyleReferenceRepository(session).get_run(result.run_id)
        progress = dict(run.coverage_json["progress"])
    assert result.status == "done"
    assert progress["layers_total"] == 1 and progress["layers_done"] == 1
    assert progress["current_layer"] is None
    assert progress["sub_dims_total"] == 4 and progress["sub_dims_done"] == 4
    assert progress["current_sub_dim"] is None
    assert progress["llm_calls"] == client.call_count >= 4
    assert progress["retries"] == client.call_count - 4
    assert len(progress["sub_dim_seconds"]) == 4
    assert progress["updated_at"]


def test_run_activity_entry_reports_sub_dimension_progress_and_eta() -> None:
    now = datetime(2026, 9, 15, 8, 0, tzinfo=timezone.utc)
    base = dict(
        run_id="sr_run_x",
        book_id="b",
        status="running",
        dispatch_state="running",
        retryable=False,
        error_code=None,
        error_text=None,
        started_at="2026-09-15T07:55:00+00:00",
        created_at="2026-09-15T07:55:00+00:00",
        updated_at="2026-09-15T07:59:00+00:00",
        finished_at=None,
        coverage_json={
            "progress": {
                "layers_total": 4,
                "layers_done": 2,
                "current_layer": "scene",
                "sub_dims_total": 16,
                "sub_dims_done": 5,
                "current_sub_dim": "scene.dialogue",
                "llm_calls": 6,
                "retries": 1,
                "sub_dim_seconds": [30.0, 30.0, 30.0, 30.0, 30.0],
            }
        },
    )
    entry = run_activity_entry(SimpleNamespace(**base), title="书", now=now)
    assert entry["key"] == "run:sr_run_x"
    assert entry["kind"] == "extract" and entry["status"] == "running"
    assert entry["percent"] == int(99 * 5 / 16)
    assert entry["steps"] == {"done": 5, "total": 16, "label": "对话"}
    assert entry["phase_label"] == "场景层"
    assert entry["eta_seconds"] == 330.0
    assert entry["elapsed_seconds"] == 300.0
    assert entry["llm_calls"] == 6 and entry["retries"] == 1
    assert entry["cancellable"] is True
    assert entry["title"] == "书"

    legacy = SimpleNamespace(
        **{
            **base,
            "coverage_json": {
                "progress": {"layers_total": 4, "layers_done": 1, "current_layer": "narrative"}
            },
        }
    )
    legacy_entry = run_activity_entry(legacy, title=None, now=now)
    assert legacy_entry["percent"] == int(99 / 4)
    assert legacy_entry["steps"] == {"done": 1, "total": 4, "label": "层"}
    assert legacy_entry["eta_seconds"] is None
    assert legacy_entry["phase_label"] == "叙事层"

    done = SimpleNamespace(
        **{**base, "status": "done", "finished_at": "2026-09-15T07:59:30+00:00"}
    )
    done_entry = run_activity_entry(done, title="书", now=now)
    assert done_entry["status"] == "succeeded" and done_entry["percent"] == 100
    assert done_entry["cancellable"] is False
    assert done_entry["elapsed_seconds"] == 270.0

    failed = SimpleNamespace(
        **{
            **base,
            "status": "failed",
            "retryable": True,
            "error_code": "STYLE_REFERENCE_RUN_INTERRUPTED",
            "error_text": "heartbeat expired",
            "finished_at": "2026-09-15T07:59:30+00:00",
        }
    )
    failed_entry = run_activity_entry(failed, title="书", now=now)
    assert failed_entry["status"] == "failed" and failed_entry["retryable"] is True
    assert failed_entry["error"] == {
        "code": "STYLE_REFERENCE_RUN_INTERRUPTED",
        "message": "heartbeat expired",
    }


# ---------------------------------------------------------------- activity endpoint


def test_activity_endpoint_merges_registry_and_durable_rows(client: TestClient) -> None:
    book_id = _import_book(client)
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_run(
            run_id="sr_run_act_running",
            book_id=book_id,
            status="running",
            phase="extract",
            dispatch_state="running",
            requested_layers_json=["language"],
            coverage_json={
                "progress": {
                    "layers_total": 1,
                    "layers_done": 0,
                    "current_layer": "language",
                    "sub_dims_total": 4,
                    "sub_dims_done": 1,
                    "current_sub_dim": "language.vocabulary",
                    "llm_calls": 2,
                    "retries": 1,
                    "sub_dim_seconds": [12.0],
                }
            },
            heartbeat_at=utcnow(),
            started_at=(now - timedelta(seconds=40)).isoformat(),
        )
        repo.create_run(
            run_id="sr_run_act_recent",
            book_id=book_id,
            status="done",
            phase="done",
            dispatch_state="completed",
            coverage_json={"progress": {"layers_total": 1, "layers_done": 1, "current_layer": None}},
            started_at=(now - timedelta(seconds=120)).isoformat(),
            finished_at=(now - timedelta(seconds=60)).isoformat(),
        )
        repo.create_run(
            run_id="sr_run_act_old",
            book_id=book_id,
            status="failed",
            phase="extract",
            dispatch_state="failed",
            coverage_json={},
            started_at=(now - timedelta(hours=2)).isoformat(),
            finished_at=(now - timedelta(hours=1)).isoformat(),
            error_code="X",
            error_text="old",
        )
        session.commit()
    start_import_progress("sr-import-act", title="导入中的书", source="upload").phase("classify")

    resp = client.get(f"{PREFIX}/activity")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["server_time"]
    items = data["items"]
    by_key = {item["key"]: item for item in items}
    assert "run:sr_run_act_old" not in by_key

    running = by_key["run:sr_run_act_running"]
    assert running["kind"] == "extract" and running["status"] == "running"
    assert running["title"] == "测试" and running["book_id"] == book_id
    assert running["steps"] == {"done": 1, "total": 4, "label": "词汇"}
    assert running["percent"] == int(99 / 4)
    assert running["eta_seconds"] == 36.0
    assert running["cancellable"] is True and running["llm_calls"] == 2

    recent = by_key["run:sr_run_act_recent"]
    assert recent["status"] == "succeeded" and recent["percent"] == 100
    assert recent["elapsed_seconds"] == 60.0

    imported = by_key["sr-import-act"]
    assert imported["kind"] == "import" and imported["status"] == "running"
    assert imported["phase"] == "classify" and imported["title"] == "导入中的书"

    statuses = [item["status"] for item in items]
    assert statuses[:2] == ["running", "running"]
    # 在跑的全部排在终态前面(_import_book 留下的 imp_1 导入条目也是终态)
    assert statuses == sorted(statuses, key=lambda s: 0 if s == "running" else 1)
    assert "imp_1" in by_key and by_key["imp_1"]["status"] == "succeeded"


# ---------------------------------------------------------------- reclassify


def test_reclassify_route_registers_progress(
    client: TestClient, monkeypatch, fake_paragraph_classifier
) -> None:
    fake = fake_paragraph_classifier(rule="default")
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (fake, True))
    book_id = _import_book(client, fake)
    resp = client.post(
        f"{PREFIX}/books/{book_id}/reclassify",
        headers={"X-Idempotency-Key": "sr-reclassify-1"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["status"] == "classifying"
    # 2026-09-15 严格 LLM:重新分类是后台任务,活动清单里立刻能看到(durable 游标),完成后书 ready
    activity = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    entry = next(item for item in activity if item["key"] == "sr-reclassify-1")
    assert entry["kind_label"] == "重新分类" and entry["book_id"] == book_id
    wait_book_status(client, book_id)
    deadline = time.monotonic() + 15
    snap = client.get(f"{PREFIX}/imports/sr-reclassify-1/progress").json()["data"]["progress"]
    while snap["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        snap = client.get(f"{PREFIX}/imports/sr-reclassify-1/progress").json()["data"]["progress"]
    assert snap["kind"] == "reclassify"
    assert snap["status"] == "succeeded" and snap["phase"] == "done" and snap["percent"] == 100
    assert snap["book_id"] == book_id and snap["title"] == "测试"
    assert snap["classify"]["batches_total"] >= 1
    assert snap["classify"]["batches_done"] == snap["classify"]["batches_total"]
    assert snap["paragraphs_count"] == resp.json()["data"]["paragraphs_count"]


# ---------------------------------------------------------------- synthesize


SYNTH_RESPONSE = {
    "profile_title": "活动画像",
    "narrative_summary": "短句加反讽,冷静叙述,克制情感",
    "style_features": ["善用短句", "白描留白"],
    "narrative_patterns": ["人物对话引出冲突"],
    "banned_replication_rules": ["禁止堆砌形容词"],
    "calibration_guidance": ["每场景一处白描"],
}


class _GatedSynthLLM(AccountedGenerateMixin):
    """第一次调用卡在 gate 上,让测试在「模型合成」阶段观察登记簿与守卫。"""

    def __init__(self, response: dict, *, gate: threading.Event, entered: threading.Event) -> None:
        self.response = response
        self.gate = gate
        self.entered = entered
        self.calls = 0

    def generate(self, request):  # noqa: ANN001
        self.calls += 1
        self.entered.set()
        assert self.gate.wait(timeout=20)
        return SimpleNamespace(
            structured_output=self.response,
            text=json.dumps(self.response, ensure_ascii=False),
            usage={},
            finish_reason="stop",
            provider="fake",
            model="fake",
            response_format="json_object",
            request_id=None,
            raw_response={},
        )


def test_synthesize_route_registers_phases_and_rejects_a_second_synthesis(
    client: TestClient, monkeypatch
) -> None:
    book_id, run_id = _ingest_with_finding("activity_synth")
    gate = threading.Event()
    entered = threading.Event()
    fake = _GatedSynthLLM(SYNTH_RESPONSE, gate=gate, entered=entered)
    monkeypatch.setattr(sr_routes, "_get_llm_client_and_enabled", lambda: (fake, True))

    results: list = []
    worker = threading.Thread(
        target=lambda: results.append(
            client.post(
                f"{PREFIX}/runs/{run_id}/synthesize",
                headers={"X-Idempotency-Key": "sr-synth-1"},
            )
        )
    )
    worker.start()
    try:
        assert entered.wait(timeout=20), "合成请求应到达模型调用"
        mid = get_import_progress("sr-synth-1")
        assert mid is not None
        assert mid["kind"] == "synthesize" and mid["status"] == "running"
        assert mid["phase"] == "llm" and mid["llm_calls"] == 1
        assert mid["target_id"] == run_id and mid["book_id"] == book_id
        assert find_running_operation(kind="synthesize", book_id=book_id)["op_key"] == "sr-synth-1"

        dup = client.post(
            f"{PREFIX}/runs/{run_id}/synthesize",
            headers={"X-Idempotency-Key": "sr-synth-2"},
        )
        assert dup.status_code == 409, dup.text
        err = dup.json()["error"]
        assert err["code"] == "STYLE_REFERENCE_SYNTHESIS_ALREADY_ACTIVE"
        assert err["details"]["op_key"] == "sr-synth-1"
        assert get_import_progress("sr-synth-2") is None
    finally:
        gate.set()
        worker.join(timeout=60)

    assert results, "合成请求没有返回"
    first = results[0]
    assert first.status_code == 200, first.text
    profile_id = first.json()["data"]["profile"]["profile_id"]
    final = get_import_progress("sr-synth-1")
    assert final["status"] == "succeeded" and final["percent"] == 100
    assert final["result"] == {"profile_id": profile_id}
    assert fake.calls == 1

    # 完成后同书可以再合成(守卫只拦在跑的那份)
    again = client.post(
        f"{PREFIX}/runs/{run_id}/synthesize",
        headers={"X-Idempotency-Key": "sr-synth-3"},
    )
    assert again.status_code == 200, again.text
    assert get_import_progress("sr-synth-3")["status"] == "succeeded"


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


# ---------------------------------------------------------------- apply / rag index


def _wait_progress(key: str, *, seconds: float = 15.0, after: str | None = None) -> dict | None:
    """等登记簿里 ``key`` 到终态;``after`` 给上一条的 started_at,只接受更新的那条。"""
    deadline = time.monotonic() + seconds
    snap = get_import_progress(key)
    while time.monotonic() < deadline:
        if (
            snap is not None
            and snap["status"] != "running"
            and (after is None or snap["started_at"] != after)
        ):
            return snap
        time.sleep(0.05)
        snap = get_import_progress(key)
    return snap


def test_apply_route_schedules_background_rag_index(client: TestClient) -> None:
    book_id = _import_book(client)
    _, _, profile_id = _seed_full_chain(book_id)
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/apply",
        json={"scope": "project", "scope_ref_id": "proj_act"},
        headers={"X-Idempotency-Key": "apply_act"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["binding_id"]
    assert data["rag_index"]["status"] == "scheduled"
    assert data["rag_index"]["profile_id"] == profile_id
    snap = _wait_progress(f"rag_index:{profile_id}")
    assert snap is not None and snap["status"] == "succeeded", snap
    assert snap["kind"] == "rag_index" and snap["book_id"] == book_id
    assert snap["result"]["status"] in {"ready", "rebuilt"}
    assert snap["title"] == "测试"
    # 已就绪的索引再 apply 一次:worker 立刻返回 ready
    resp2 = client.post(
        f"{PREFIX}/profiles/{profile_id}/apply",
        json={"scope": "scene", "scope_ref_id": "scene_act"},
        headers={"X-Idempotency-Key": "apply_act_2"},
    )
    assert resp2.status_code == 200, resp2.text
    snap2 = _wait_progress(f"rag_index:{profile_id}", after=snap["started_at"])
    assert snap2 is not None and snap2["status"] == "succeeded"
    assert snap2["result"]["status"] == "ready"


def test_run_deferred_dispatches_starts_the_rag_worker_once_per_effect(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        rag_module,
        "start_style_reference_rag_index_worker",
        lambda **kw: calls.append(kw),
    )
    run_deferred_dispatches(
        {
            "deferred_dispatches": [
                {"type": "style_reference_rag_index", "profile_id": "p1", "book_id": "b1"},
                {"type": "unknown"},
                "garbage",
            ]
        }
    )
    run_deferred_dispatches(None)
    run_deferred_dispatches({})
    assert calls == [{"profile_id": "p1", "book_id": "b1"}]


def test_review_card_approval_dispatches_the_rag_index_after_commit(
    client: TestClient, session, monkeypatch
) -> None:
    dispatched: list[dict] = []
    monkeypatch.setattr(
        rag_module,
        "start_style_reference_rag_index_worker",
        lambda **kw: dispatched.append(kw),
    )
    project = _create_project(client)
    pid = project["project_id"]
    repo = StyleReferenceRepository(session)
    repo.create_book(
        book_id="sr_book_act_rc",
        title="活动",
        source_kind="upload",
        cloud_policy="segments_only",
        text_checksum="chk_act_rc",
        total_chars=50000,
        status="ready",
        stats_json={
            "rights_declaration": {"declared": True, "analysis_rights": True, "send_rights": True}
        },
    )
    repo.create_run(run_id="sr_run_act_rc", book_id="sr_book_act_rc", status="done", phase="done")
    repo.create_profile(
        profile_id="sr_profile_act_rc",
        book_id="sr_book_act_rc",
        run_id="sr_run_act_rc",
        title="活动画像",
        status="active",
        profile_json={"narrative_summary": "短句白描"},
        coverage_json={},
        source_finding_ids_json=[],
    )
    session.commit()
    card = _card(
        client,
        pid,
        kind="decision",
        title="应用风格画像",
        actions=[
            {
                "label": "批准应用",
                "intent": "primary",
                "op": "resolve",
                "effect": {
                    "type": "bind_style_profile",
                    "profile_id": "sr_profile_act_rc",
                    "scope": "project",
                    "task_type": "scene_generation",
                    "strategy": "mixed",
                },
            },
            {"label": "丢弃", "intent": "quiet", "op": "resolve"},
        ],
    )
    resolved = _post(
        client, f"/api/v1/review-items/{card['id']}/resolve", {"action_index": 0, "project_id": pid}
    )
    assert resolved.status_code == 200, resolved.text
    effect = resolved.json()["data"]["effect_result"]
    assert effect["rag_index"]["status"] == "scheduled"
    assert dispatched == [{"profile_id": "sr_profile_act_rc", "book_id": "sr_book_act_rc"}]
    bindings = client.get(f"{PREFIX}/profiles/sr_profile_act_rc/bindings").json()["data"]["bindings"]
    assert len(bindings) == 1


# ---------------------------------------------------------------- preview


def test_preview_route_forwards_paragraph_types(client: TestClient, monkeypatch) -> None:
    book_id = _import_book(client)
    _, _, profile_id = _seed_full_chain(book_id)
    captured: dict = {}

    class _Stub:
        def __init__(self, session, *, llm_client=None, llm_enabled=None) -> None:  # noqa: ANN001
            pass

        def generate(self, profile_id, *, target_types=None):  # noqa: ANN001
            captured["types"] = target_types
            types = target_types or ("dialogue", "description_env", "psychology")
            return [
                SimpleNamespace(
                    model_dump=lambda t=t: {
                        "paragraph_type": t,
                        "sample_text": "x",
                        "verdict": "pass",
                        "error": None,
                    }
                )
                for t in types
            ]

    monkeypatch.setattr(sr_routes, "PreviewService", _Stub)
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/preview",
        json={"paragraph_types": ["dialogue"]},
        headers={"X-Idempotency-Key": "pv-1"},
    )
    assert resp.status_code == 200, resp.text
    assert captured["types"] == ("dialogue",)
    assert [s["paragraph_type"] for s in resp.json()["data"]["samples"]] == ["dialogue"]

    resp2 = client.post(
        f"{PREFIX}/profiles/{profile_id}/preview",
        json={},
        headers={"X-Idempotency-Key": "pv-2"},
    )
    assert resp2.status_code == 200, resp2.text
    assert captured["types"] is None
    assert len(resp2.json()["data"]["samples"]) == 3

    bad = client.post(
        f"{PREFIX}/profiles/{profile_id}/preview",
        json={"paragraph_types": ["nope"]},
        headers={"X-Idempotency-Key": "pv-3"},
    )
    assert bad.status_code in (400, 422), bad.text
