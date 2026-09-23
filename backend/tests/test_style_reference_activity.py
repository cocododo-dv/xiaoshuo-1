"""参考书活动清单(2026-09-15;2026-09-23 v3 起只读作业表):``GET …/activity`` 与各操作的进度接线。

钉住:
- 活动清单只列作业表条目(``job:<id>``,kind ∈ classify / learn / check):学习文风作业的七步进度、分类作业
  (导入 / 重新分类)、对照检查作业同形列出;旧抽取 run、旧回测报告、进程内登记簿与给旧前端的别名条目都没有;
- 导入 / 重新分类的响应带 ``job_id``,分类过程中与跑完之后都能在清单里按 ``job:<id>`` 读到进度,请求在建作业
  之前就失败时没有条目;旧的 ``GET …/imports/{key}/progress`` 轮询已删除;
- 源文重合过滤的 n-gram 索引与逐行 ``check_plagiarism`` 判定完全一致。
(学习作业本身见 test_style_reference_learn_job.py。)
"""

from __future__ import annotations

import io
import json
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from novel_system.db.models import StyleReferenceJob, utcnow
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.validation.plagiarism import (
    CorpusOverlapIndex,
    check_plagiarism,
)
from tests.style_reference_route_helpers import fake_import_llm, install_fake_classifier, wait_book_status
from tests.test_style_reference_routes import _import_book, _seed_full_chain

PREFIX = "/api/v2/style-reference"


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


SAMPLE_TXT = """这是一段较长的叙述文字,介绍清晨场景与人物心情,字数足以触发分段。

他说:"今天天气不错。"

我心里想着昨天的事情,觉得有些不安。

记得那年她还在的时候。

雪花从天空飘落。
""".encode("utf-8")


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


def test_activity_endpoint_lists_job_rows_only(client: TestClient) -> None:
    """活动清单:只有作业表条目(学习文风作业的七步进度 + 导入留下的分类作业);旧抽取 run 行不单列,
    没有别名条目。"""
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

    resp = client.get(f"{PREFIX}/activity")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["server_time"]
    items = data["items"]
    by_key = {item["key"]: item for item in items}
    assert all(str(key).startswith("job:") for key in by_key), by_key.keys()
    assert not any("compat_alias_of" in item for item in items)

    learning = by_key[f"job:{job.job_id}"]
    assert learning["kind"] == "learn" and learning["kind_label"] == "学习文风" and learning["status"] == "running"
    assert learning["title"] == "测试" and learning["book_id"] == book_id
    assert learning["phase_label"] == "学习文风 · 逐层读原文" and learning["percent"] == 30.0
    assert learning["steps"] == {"done": 3, "total": 10} and learning["llm_calls"] == 2
    assert learning["phases_done"] == ["windows", "select"] and learning["cancellable"] is True

    imported = [item for item in items if item["kind"] == "classify"]
    assert len(imported) == 1 and imported[0]["mode"] == "import" and imported[0]["op_key"] == "imp_1"
    assert imported[0]["status"] == "succeeded" and imported[0]["book_id"] == book_id

    statuses = [item["status"] for item in items]
    # 在跑的全部排在终态前面
    assert statuses == sorted(statuses, key=lambda s: 0 if s in ("queued", "running") else 1)


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
    """旧前端的导入轮询删除:导入进度一律看响应里的 job_id + 活动清单。"""
    assert client.get(f"{PREFIX}/imports/sr-import-never/progress").status_code == 404


def test_import_progress_is_live_while_the_job_classifies(
    client: TestClient, monkeypatch, fake_paragraph_classifier
) -> None:
    """在途读数:分类器每次被调用时,作业条目都在 running / classify,已完成批数单调增加。"""
    from novel_system.services.style_reference import import_job

    monkeypatch.setattr(import_job, "PARALLEL_BATCHES", 1)
    observed: list[dict[str, Any]] = []

    class ObservingClient(fake_paragraph_classifier):
        def generate(self, request):  # noqa: ANN001
            with SessionLocal() as session:
                job = session.execute(
                    select(StyleReferenceJob).where(StyleReferenceJob.op_key == "sr-import-progress-live")
                ).scalar_one()
                observed.append(import_job.classification_activity_entry(job, title=None, total_chars=None))
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
    assert [snap["steps"]["done"] for snap in observed] == list(range(len(observed)))
    assert final["status"] == "succeeded"
    assert final["steps"]["done"] == final["steps"]["total"] == len(observed)
    assert final["llm_calls"] == len(observed)


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
    # 重新分类是作业表上的分类作业:活动清单里立刻能按响应的 job_id 看到,完成后书 ready
    job_id = resp.json()["data"]["job_id"]
    activity = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    job_entry = next(item for item in activity if item["key"] == f"job:{job_id}")
    assert job_entry["kind"] == "classify" and job_entry["mode"] == "reclassify"
    assert job_entry["mode_label"] == "重新分类" and job_entry["book_id"] == book_id
    assert job_entry["op_key"] == "sr-reclassify-1"
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
    assert job_entry["job_id"] == job_id


# ---------------------------------------------------------------- style check (取代旧回测)


def test_check_jobs_are_listed_like_every_other_job(client: TestClient) -> None:
    from novel_system.services.style_reference.jobs import JOB_KIND_CHECK, StyleJobService

    book_id = _import_book(client)
    _, _, profile_id = _seed_full_chain(book_id)
    with SessionLocal() as session:
        service = StyleJobService(session)
        job = service.create(JOB_KIND_CHECK, book_id=book_id, profile_id=profile_id, phase="queued", allow_parallel=True)
        claimed = service.claim(job.job_id)
        service.progress(claimed, phase="judge", phase_label="参考评审", done=1, total=3)
        session.commit()

    resp = client.get(f"{PREFIX}/activity")
    assert resp.status_code == 200, resp.text
    by_key = {item["key"]: item for item in resp.json()["data"]["items"]}
    checking = by_key[f"job:{job.job_id}"]
    assert checking["kind"] == "check" and checking["kind_label"] == "对照检查"
    assert checking["status"] == "running" and checking["phase_label"] == "参考评审"
    assert checking["steps"] == {"done": 1, "total": 3} and checking["book_id"] == book_id
    assert not any(item["kind"] == "validate" for item in by_key.values())


