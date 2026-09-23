"""段落分类作业(2026-09-23 风格参考 v3:作业表 kind=classify)。

钉住(台账 I1 / I2 / I3 / I4 / I5 / I6 / I12 / I13 / I16 / L7):
- 整本每一段都由 LLM 分类:锚定集在全书上分层抽样、按字数分批、最多 3 批并行、同模型跳过快模型对照;
- 严格解析:按 paragraph_index 对齐、类型按枚举校验,缺 / 多 / 重复 / 非法整批重试,绝不补「叙述」;
- 每批失败退避重试两次,仍失败才让作业失败(游标保留),「继续分类」只重跑没做完的批;
- 成功:书 ready、``paragraph_types_revision`` +1、``classification_provenance`` 记来源与提示词版本;
- 取消:排队中 / 工人已死的作业在请求里收尾;运行中的在下一个检查点收尾;
- 重启:心跳过期 60 s 的 running 被清扫放回队列、新工人续跑;对它取消立即生效、续跑不再 409;
- 所有权:删书重导入后旧工人的写全部落空、不再调模型;每批派发前重查书的云策略;
- 就地重标类型(retype)保留抽取 / 画像 / 绑定,书全程可用;
- 旧书的类型来源按校准信息推出来(legacy_heuristic + 一致率);
- 费用预估、运行时默认策略、重复 / 空书的错误码、活动与兼容进度端点。
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from novel_system.db.models import (
    LlmCall,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceJob,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference import import_job
from novel_system.services.style_reference import policy as policy_module
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.jobs import StyleJobService, run_job_inline
from novel_system.services.style_reference.segmentation import llm as seg
from tests.accounted_llm_fakes import AccountedGenerateMixin
from tests.style_reference_route_helpers import (
    PREFIX,
    fake_import_llm,
    import_book,
    install_fake_classifier,
    wait_book_status,
    wait_classification_state,
)
from tests.test_style_reference_routes import _seed_full_chain

LONG_TEXT = "\n\n".join(
    f"第{i + 1}段。潮水在夜里退去,露出一行脚印,她数着脚印往前走,每一步都比上一步更接近那句没人认领的对不起。"
    for i in range(60)
).encode("utf-8")

_BOUNDARY_RE = re.compile(r"\[UNTRUSTED_REFERENCE_DATA:[^\]]+\]\n")


def _items(request) -> list[dict]:
    user = request.messages[-1]["content"]
    opening = _BOUNDARY_RE.search(user)
    assert opening is not None
    closing = user.find("\n[/UNTRUSTED_REFERENCE_DATA]", opening.end())
    return json.loads(user[opening.end():closing])["paragraphs"]


def _type_for(text: str) -> str:
    if any(mark in text for mark in ('"', "“", "”", "「", "」")):
        return "dialogue"
    if "记得" in text:
        return "flashback"
    if "心里" in text or "想着" in text:
        return "psychology"
    return "narration"


class ScriptedClassifier(AccountedGenerateMixin):
    """确定性的假分类器:按段落正文给类型;``script(node_id, indexes, call_no)`` 可以让某一次调用
    失败(fail)、少给一段(drop)、给非法类型(invalid)、重复一段(dup);``gate_call`` 让第 n 次调用
    卡在闸门上;记录每次调用的节点与段号、同时在飞的调用数。"""

    def __init__(self, script=None, *, delay: float = 0.0, bulk_type: str | None = None, gate_call: int | None = None):
        self.script = script
        self.delay = delay
        self.bulk_type = bulk_type
        self.lock = threading.Lock()
        self.calls: list[tuple[str, list[int]]] = []
        self.requests: list = []
        self.inflight = 0
        self.max_inflight = 0
        self.gate_call = gate_call
        self.gate = threading.Event()
        self.entered = threading.Event()

    def generate(self, request):  # noqa: ANN001
        items = _items(request)
        indexes = [int(item["paragraph_index"]) for item in items]
        with self.lock:
            self.calls.append((request.node_id, indexes))
            self.requests.append(request)
            call_no = len(self.calls)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if self.gate_call is not None and call_no == self.gate_call:
                self.entered.set()
                assert self.gate.wait(timeout=20)
            if self.delay:
                time.sleep(self.delay)
            action = self.script(request.node_id, indexes, call_no) if self.script else None
            if action == "fail":
                raise RuntimeError("relay down")
            classifications = [
                {
                    "paragraph_index": item["paragraph_index"],
                    "paragraph_type": (
                        self.bulk_type
                        if self.bulk_type and request.node_id == seg.NODE_BULK
                        else _type_for(item["text"])
                    ),
                    "confidence": "high",
                }
                for item in items
            ]
            if action == "drop":
                classifications = classifications[:-1]
            elif action == "invalid":
                classifications[0]["paragraph_type"] = "monologue"
            elif action == "dup":
                classifications.append(dict(classifications[0]))
            structured = {"classifications": classifications}
            return SimpleNamespace(
                structured_output=structured,
                text=json.dumps(structured, ensure_ascii=False),
                usage={},
                finish_reason="stop",
                request_id=None,
                provider="fake",
                model="fake",
                raw_response={},
                response_format="json_object",
            )
        finally:
            with self.lock:
                self.inflight -= 1

    def nodes(self) -> list[str]:
        return [node for node, _indexes in self.calls]


@pytest.fixture(autouse=True)
def _small_batches(monkeypatch):
    """60 段的书:锚定集 25 段、每批至多 5 段 → 强模型 5 批 + 快模型 5 批 + 余段 7 批;退避不等待。"""
    monkeypatch.setattr(seg, "ANCHOR_SIZE", 25)
    monkeypatch.setattr(seg, "BATCH_MAX_PARAGRAPHS", 5)
    monkeypatch.setattr(import_job, "BATCH_RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(import_job, "WAIT_POLL_SECONDS", 0.05)


def _use(monkeypatch, fake) -> ScriptedClassifier:
    monkeypatch.setattr(import_job, "resolve_classification_client", lambda: (fake, True))
    return fake


def _ingest(session, *, text: bytes = LONG_TEXT, op_key: str = "k-job", cloud_policy: str = "segments_only") -> tuple[str, str]:
    rights = {"analysis_rights": True, "send_rights": True} if cloud_policy != "local_only" else None
    result = IngestService(session, llm_enabled=True, op_key=op_key).ingest_upload(
        raw_bytes=text,
        file_name="book.txt",
        title="潮汐",
        author_label=None,
        cloud_policy=cloud_policy,
        rights_declaration=rights,
    )
    session.commit()
    assert result.classification_pending and result.book.status == "ingesting"
    return result.book.book_id, result.job.job_id


def _job(job_id: str) -> StyleReferenceJob:
    with SessionLocal() as session:
        job = session.get(StyleReferenceJob, job_id)
        session.expunge(job)
        return job


def _book(book_id: str) -> StyleReferenceBook:
    with SessionLocal() as session:
        book = session.get(StyleReferenceBook, book_id)
        session.expunge(book)
        return book


def _types(book_id: str) -> list[str]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(StyleReferenceParagraph.paragraph_type)
                .where(StyleReferenceParagraph.book_id == book_id)
                .order_by(StyleReferenceParagraph.paragraph_index)
            )
        )


# ---------------------------------------------------------------- happy path


def test_classify_job_classifies_every_paragraph_and_records_provenance(session, monkeypatch) -> None:
    fake = _use(monkeypatch, ScriptedClassifier())
    book_id, job_id = _ingest(session)
    assert set(_types(book_id)) == {"unclassified"}

    run_job_inline(job_id)

    job = _job(job_id)
    assert job.state == "succeeded" and job.owner_token is None
    book = _book(book_id)
    assert book.status == "ready"
    assert set(_types(book_id)) <= seg.VALID_PARAGRAPH_TYPES
    # 强模型 5 批 + 快模型对照 5 批 + 余段 7 批(一致率 1.0 → 余段走快模型)
    assert fake.nodes().count(seg.NODE_ANCHOR) == 5
    assert fake.nodes().count(seg.NODE_BULK) == 12
    assert job.result_json["batches"] == 17 and job.result_json["llm_calls"] == 17
    assert job.result_json["paragraph_types_revision"] == 1
    stats = book.stats_json
    assert stats["paragraph_types_revision"] == 1
    provenance = stats["classification_provenance"]
    assert provenance["source"] == "llm" and provenance["llm_paragraphs"] == 60
    assert provenance["heuristic_paragraphs"] == 0
    assert provenance["prompt_version"] == "2026-09-23.v3"
    assert provenance["agreement"] == 1.0 and provenance["rest_node"] == seg.NODE_BULK
    assert provenance["classified_at"] and provenance["job_id"] == job_id
    calibration = stats["classifier_calibration"]
    assert calibration["anchor_sampling"] == "stratified" and calibration["anchor_size"] == 25
    assert calibration["rest_classifier"] == "fast_llm" and calibration["heuristic_classified_paragraphs"] == 0
    assert stats["paragraph_type_distribution"] and stats["voice_signature"]
    assert "classification" not in stats  # 旧的书上 JSON 游标退役
    # 锚定集覆盖全书:前后两半都有
    anchors = sorted(i for node, indexes in fake.calls if node == seg.NODE_ANCHOR for i in indexes)
    assert anchors[0] < 12 and anchors[-1] > 48
    # 每次调用都写了账,step 记阶段 / 首段 / 段数
    with SessionLocal() as other:
        steps = list(other.scalars(select(LlmCall.step)))
    assert len(steps) == 17
    assert all(re.fullmatch(r"paragraph_classification:(anchor_strong|anchor_fast|rest):\d+:\d+", s) for s in steps)


def test_batches_run_in_parallel_but_never_more_than_three(session, monkeypatch) -> None:
    fake = _use(monkeypatch, ScriptedClassifier(delay=0.12))
    _book_id, job_id = _ingest(session)
    run_job_inline(job_id)
    assert _job(job_id).state == "succeeded"
    assert fake.max_inflight == import_job.PARALLEL_BATCHES == 3


def test_same_model_routes_skip_the_fast_calibration_pass(session, monkeypatch) -> None:
    fake = _use(monkeypatch, ScriptedClassifier())
    monkeypatch.setattr(seg, "same_model", lambda _a, _b: True)
    book_id, job_id = _ingest(session)
    run_job_inline(job_id)
    assert _job(job_id).state == "succeeded"
    # 锚定集只过强模型;余段直接走快模型节点
    assert fake.nodes().count(seg.NODE_ANCHOR) == 5 and fake.nodes().count(seg.NODE_BULK) == 7
    calibration = _book(book_id).stats_json["classifier_calibration"]
    assert calibration["calibration_skipped"] == "same_route"
    assert calibration["fast_model_agreement"] is None


def test_low_agreement_sends_the_rest_to_the_strong_node(session, monkeypatch) -> None:
    fake = _use(monkeypatch, ScriptedClassifier(bulk_type="transition"))
    book_id, job_id = _ingest(session)
    run_job_inline(job_id)
    assert _job(job_id).state == "succeeded"
    rest_nodes = fake.nodes()[10:]
    assert rest_nodes == [seg.NODE_ANCHOR] * 7
    calibration = _book(book_id).stats_json["classifier_calibration"]
    assert calibration["fallback_to_strong"] is True and calibration["rest_classifier"] == "strong_llm"
    assert calibration["fast_model_agreement"] == 0.0
    # 快模型的 transition 从不落段落表(对照结果只在游标里)
    assert "transition" not in set(_types(book_id))


# ---------------------------------------------------------------- retries / strict parsing


def test_a_flaky_batch_is_retried_and_the_job_still_succeeds(session, monkeypatch) -> None:
    seen: set[tuple[int, ...]] = set()

    def flaky(node, indexes, _call_no):
        key = tuple(indexes)
        if node == seg.NODE_BULK and 40 in indexes and key not in seen:
            seen.add(key)
            return "fail"
        return None

    fake = _use(monkeypatch, ScriptedClassifier(flaky))
    _book_id, job_id = _ingest(session)
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "succeeded"
    assert job.cursor_json["retries"] == 1 and job.cursor_json["llm_calls"] == 18
    assert len(fake.calls) == 18


@pytest.mark.parametrize("action", ["drop", "dup", "invalid"])
def test_mismatched_output_is_retried_and_never_padded(session, monkeypatch, action: str) -> None:
    once: set[str] = set()

    def mismatch_once(node, indexes, _call_no):
        if node == seg.NODE_ANCHOR and "first" not in once:
            once.add("first")
            return action
        return None

    _use(monkeypatch, ScriptedClassifier(mismatch_once))
    book_id, job_id = _ingest(session)
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "succeeded" and job.cursor_json["retries"] == 1
    # 所有段的类型都来自模型对这一段的判断(正文都是叙述),没有位置错位补出来的类型
    assert set(_types(book_id)) == {"narration"}


def test_a_batch_that_keeps_failing_fails_the_job_and_resume_finishes_only_the_rest(
    session, monkeypatch
) -> None:
    monkeypatch.setattr(import_job, "PARALLEL_BATCHES", 1)
    target: dict[str, int] = {}

    def always_fail(node, indexes, _call_no):
        return "fail" if node == seg.NODE_BULK and target["index"] in indexes else None

    failing = _use(monkeypatch, ScriptedClassifier(always_fail))
    book_id, job_id = _ingest(session)
    # 让最后一批余段一直失败(按与作业同一套抽样算出锚定集,挑一个不是锚定段的段)
    texts = [str(t) for t in session.scalars(
        select(StyleReferenceParagraph.text)
        .where(StyleReferenceParagraph.book_id == book_id)
        .order_by(StyleReferenceParagraph.paragraph_index)
    )]
    anchors = set(seg.select_anchor_positions(texts, seed=book_id))
    target["index"] = max(pos for pos in range(len(texts)) if pos not in anchors)
    run_job_inline(job_id)

    job = _job(job_id)
    assert job.state == "failed"
    assert job.error_json["code"] == "STYLE_REFERENCE_CLASSIFICATION_FAILED"
    details = job.error_json["details"]
    assert details["reason_code"] == "STYLE_REFERENCE_CLASSIFY_LLM_CALL_FAILED"
    assert details["phase"] == "rest" and details["attempts"] == import_job.BATCH_ATTEMPTS == 3
    assert details["author_action"]["view"] == "systemConfig"
    assert job.error_json["retryable"] is True
    assert _book(book_id).status == "failed"
    cursor = job.cursor_json
    assert cursor["phase"] == "rest" and cursor["rest_node"] == seg.NODE_BULK
    done_rest = sum(end - start + 1 for start, end in cursor["done"]["rest"])
    assert done_rest == 30  # 前 6 批余段做完,最后一批失败
    assert len(failing.calls) == 10 + 6 + 3

    healthy = _use(monkeypatch, ScriptedClassifier())
    with SessionLocal() as other:
        book = other.get(StyleReferenceBook, book_id)
        resumed = import_job.resume_classification(other, book, op_key="k-resume")
        other.commit()
        assert resumed.job_id == job_id and resumed.op_key == "k-resume"
    assert _book(book_id).status == "ingesting"
    run_job_inline(job_id)

    job = _job(job_id)
    assert job.state == "succeeded" and job.attempt == 2
    assert _book(book_id).status == "ready"
    # 只重跑没做完的余段批次:锚定集、对照都不再调
    assert healthy.calls == [(seg.NODE_BULK, [i for i in healthy.calls[0][1]])]
    assert target["index"] in healthy.calls[0][1]


def test_control_plane_failures_are_not_retried(session, monkeypatch) -> None:
    from novel_system.services.llm_accounting import LLMAccountingError

    def boom(*_args, **_kwargs):
        raise LLMAccountingError("LLM_ACCOUNTING_CALL_EXISTS", "logical call already exists")

    _use(monkeypatch, ScriptedClassifier())
    monkeypatch.setattr(seg, "execute_accounted_call", boom)
    book_id, job_id = _ingest(session)
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "failed" and job.error_json["code"] == "LLM_ACCOUNTING_CALL_EXISTS"
    assert _book(book_id).status == "failed"


def test_job_without_llm_fails_with_llm_required(session, monkeypatch) -> None:
    monkeypatch.setattr(import_job, "resolve_classification_client", lambda: (None, False))
    book_id, job_id = _ingest(session)
    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "failed" and job.error_json["code"] == "STYLE_REFERENCE_LLM_REQUIRED"
    assert _book(book_id).status == "failed"


# ---------------------------------------------------------------- unit: parsing / planning / anchors


def test_parse_batch_output_is_strict() -> None:
    ok = seg.parse_batch_output(
        {
            "classifications": [
                {"paragraph_index": 7, "paragraph_type": "dialogue", "confidence": "high"},
                {"paragraph_index": "8", "paragraph_type": "action", "confidence": "weird"},
            ]
        },
        [7, 8],
    )
    assert ok == {7: ("dialogue", 0.9), 8: ("action", 0.5)}
    bad_outputs = [
        None,
        {"classifications": "nope"},
        {"classifications": [{"paragraph_index": 7, "paragraph_type": "dialogue"}]},  # 缺 8
        {"classifications": [
            {"paragraph_index": 7, "paragraph_type": "dialogue"},
            {"paragraph_index": 8, "paragraph_type": "action"},
            {"paragraph_index": 9, "paragraph_type": "action"},
        ]},  # 多出
        {"classifications": [
            {"paragraph_index": 7, "paragraph_type": "dialogue"},
            {"paragraph_index": 7, "paragraph_type": "action"},
            {"paragraph_index": 8, "paragraph_type": "action"},
        ]},  # 重复
        {"classifications": [
            {"paragraph_index": 7, "paragraph_type": "dialogue"},
            {"paragraph_index": 8, "paragraph_type": "monologue"},
        ]},  # 非法类型
        {"classifications": [
            {"paragraph_index": True, "paragraph_type": "dialogue"},
            {"paragraph_index": 8, "paragraph_type": "action"},
        ]},
    ]
    for output in bad_outputs:
        with pytest.raises(seg.ClassificationBatchMismatch):
            seg.parse_batch_output(output, [7, 8])


def test_plan_batches_respects_char_and_paragraph_limits() -> None:
    texts = ["字" * 1400] * 5 + ["字" * 20_000] + ["字" * 10] * 12
    positions = list(range(len(texts)))
    batches = seg.plan_batches(positions, texts, max_chars=6000, max_paragraphs=10)
    assert batches[0] == [0, 1, 2, 3]  # 4 × 1400 = 5600,再加一段就超 6000 字
    assert batches[1][:2] == [4, 5]  # 超长段只按送出的 1500 字算
    assert all(len(batch) <= 10 for batch in batches)
    assert [pos for batch in batches for pos in batch] == positions  # 顺序不变、不丢不重
    for batch in batches:
        clipped = sum(min(len(texts[pos]), seg.PARAGRAPH_TEXT_MAX_CHARS) for pos in batch)
        assert clipped <= 6000 or len(batch) == 1
    # 单段超过上限时自成一批
    assert seg.plan_batches([0, 1, 5], texts, max_chars=1000, max_paragraphs=10) == [[0], [1], [5]]


def test_anchor_positions_are_stratified_over_the_whole_book() -> None:
    texts = ["某某 著", "内容简介"]
    for chapter in range(10):
        texts.append(f"第{chapter + 1}章 远行")
        texts.extend(f"第{chapter + 1}章里的第{i}段,他往前走了几步,又停下来看天。" for i in range(99))
    anchors = seg.select_anchor_positions(texts, seed="book-a", anchor_size=50)
    assert len(anchors) == 50
    assert anchors == sorted(anchors)
    assert all("章 远行" not in texts[pos] for pos in anchors)  # 不抽章题
    assert min(anchors) > 1  # 书名页 / 简介块跳过
    assert max(anchors) > len(texts) * 0.9 and min(anchors) < len(texts) * 0.1
    assert seg.select_anchor_positions(texts, seed="book-a", anchor_size=50) == anchors
    assert seg.select_anchor_positions(texts, seed="book-b", anchor_size=50) != anchors
    assert seg.select_anchor_positions(texts[:30], seed="x", anchor_size=50) == list(range(30))


def test_batch_items_carry_read_only_neighbour_context() -> None:
    texts = [f"第{i}段正文" for i in range(10)]
    indexes = list(range(100, 110))
    items = seg.batch_items([3, 4, 5, 8], texts, indexes)
    by_index = {item["paragraph_index"]: item for item in items}
    assert by_index[103]["context_before"] == "第2段正文" and "context_after" not in by_index[103]
    assert "context_before" not in by_index[104] and "context_after" not in by_index[104]
    assert by_index[105]["context_after"] == "第6段正文"
    assert by_index[108]["context_before"] == "第7段正文" and by_index[108]["context_after"] == "第9段正文"


def test_classify_prompts_v3_drop_the_short_paragraph_rule_and_explain_context() -> None:
    from novel_system.services.prompt_builder import load_prompt_templates

    templates = load_prompt_templates()
    for node in seg.CLASSIFY_NODE_IDS:
        template = templates[node]
        assert template.version == "2026-09-23.v3"
        assert "默认 transition" not in template.task_prompt
        assert "<30" not in template.task_prompt
        assert "仅看当前段本身" not in template.task_prompt
        assert "context_before" in template.task_prompt and "context_after" in template.task_prompt
        assert "段落长短不决定类型" in template.task_prompt


def test_classify_nodes_default_to_reasoning_off_with_room_for_a_full_batch() -> None:
    from pathlib import Path

    from novel_system.services.llm_client import load_model_routing_config
    from novel_system.services.llm_node_registry import get_llm_node_spec

    routing = load_model_routing_config(Path(__file__).resolve().parents[2] / "config" / "models.yaml")
    for node in seg.CLASSIFY_NODE_IDS:
        spec = get_llm_node_spec(node)
        route = routing.task_routing[node]
        assert spec.reasoning_level == route.reasoning_level == "off"
        # ≤100 段一批的输出 ≤~4k token;给不肯关思考的中转留一倍余量
        assert spec.max_output_tokens == route.max_output_tokens >= 4096


# ---------------------------------------------------------------- cancel / recovery / ownership


def _claim_as_dead_worker(job_id: str, *, age_seconds: float = 61.0) -> None:
    """模拟一个认领了作业然后死掉的工人(进程重启 / --reload):running、心跳 ``age_seconds`` 秒前。"""
    with SessionLocal() as other:
        claimed = StyleJobService(other).claim(job_id)
        assert claimed is not None
        stale = (datetime.now(UTC) - timedelta(seconds=age_seconds)).isoformat()
        other.execute(update(StyleReferenceJob).where(StyleReferenceJob.job_id == job_id).values(heartbeat_at=stale))
        other.commit()


def test_restart_leaves_a_stale_running_job_that_the_sweeper_requeues_and_a_new_worker_finishes(
    session, monkeypatch
) -> None:
    fake = _use(monkeypatch, ScriptedClassifier())
    book_id, job_id = _ingest(session)
    _claim_as_dead_worker(job_id)
    assert _job(job_id).state == "running"

    with SessionLocal() as other:
        queued = StyleJobService(other).sweep()
        other.commit()
    assert job_id in queued
    job = _job(job_id)
    assert job.state == "queued" and job.owner_token is None and job.attempt == 1

    run_job_inline(job_id)
    job = _job(job_id)
    assert job.state == "succeeded" and job.attempt == 2
    assert _book(book_id).status == "ready"
    assert len(fake.calls) == 17


def test_app_startup_sweeps_and_finishes_a_job_left_by_a_dead_process(session, monkeypatch) -> None:
    """lifespan 启动常驻清扫线程:进程重启后第一次清扫就把心跳过期的作业放回队列并派发,不需要人工介入。"""
    from novel_system.api.app import create_app
    from novel_system.services.style_reference import jobs

    fake = _use(monkeypatch, ScriptedClassifier())
    book_id, job_id = _ingest(session)
    _claim_as_dead_worker(job_id)
    with TestClient(create_app()) as client:
        assert jobs._SWEEPER is not None and jobs._SWEEPER.is_alive()
        assert jobs.registered_job_handler("classify") is import_job.run_classification_job
        book = wait_book_status(client, book_id)
    assert jobs._SWEEPER is None and jobs._SWEEPER_STOP.is_set()
    assert book["classification"]["state"] == "succeeded" and book["classification"]["attempt"] == 2
    assert len(fake.calls) == 17


def test_cancel_of_a_stale_running_job_finishes_in_the_request(session, monkeypatch) -> None:
    _use(monkeypatch, ScriptedClassifier())
    book_id, job_id = _ingest(session)
    _claim_as_dead_worker(job_id)
    with SessionLocal() as other:
        job = import_job.cancel_classification(other, book_id)
        other.commit()
        assert job.state == "cancelled"
    assert _book(book_id).status == "failed"
    assert _job(job_id).error_json["code"] == "STYLE_REFERENCE_JOB_CANCELLED"


def test_cancel_of_a_queued_job_finishes_in_the_request(session, monkeypatch) -> None:
    fake = _use(monkeypatch, ScriptedClassifier())
    book_id, job_id = _ingest(session)
    with SessionLocal() as other:
        job = import_job.cancel_classification(other, book_id)
        other.commit()
        assert job.state == "cancelled"
    assert _book(book_id).status == "failed"
    run_job_inline(job_id)  # 工人拿不到已取消的作业
    assert fake.calls == []


def test_stale_and_cancel_scenarios_through_the_routes(client: TestClient, monkeypatch) -> None:
    fake = install_fake_classifier(monkeypatch, ScriptedClassifier())
    # 重启后心跳过期的作业:「继续分类」不再 409,重排后跑完
    with SessionLocal() as session:
        book_id, job_id = _ingest(session, op_key="k-stale")
    _claim_as_dead_worker(job_id)
    resp = client.post(
        f"{PREFIX}/books/{book_id}/reclassify",
        json={"resume": True},
        headers={"X-Idempotency-Key": "stale-resume"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["job_id"] == job_id
    book = wait_book_status(client, book_id)
    assert book["classification"]["state"] == "succeeded"
    assert book["classification"]["attempt"] == 2
    assert book["classification_provenance"]["source"] == "llm"
    assert book["paragraph_types_revision"] == 1
    assert len(fake.calls) == 17

    # 在跑的作业:取消请求置标志,工人在下一个检查点收尾;之后可「继续分类」
    gated = install_fake_classifier(monkeypatch, ScriptedClassifier(gate_call=1))
    resp = client.post(
        f"{PREFIX}/books/{book_id}/reclassify",
        json={"mode": "reclassify"},
        headers={"X-Idempotency-Key": "stale-reclassify"},
    )
    assert resp.status_code == 200, resp.text
    assert gated.entered.wait(timeout=20)
    cancel = client.post(
        f"{PREFIX}/books/{book_id}/classification/cancel",
        headers={"X-Idempotency-Key": "stale-cancel"},
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["data"]["finished"] is False
    gated.gate.set()
    payload = wait_classification_state(client, book_id, ("cancelled",))
    assert payload["resumable"] is True and payload["error"]["code"] == "STYLE_REFERENCE_JOB_CANCELLED"
    assert wait_book_status(client, book_id, statuses=("failed",))["status"] == "failed"
    nothing = client.post(
        f"{PREFIX}/books/{book_id}/classification/cancel",
        headers={"X-Idempotency-Key": "stale-cancel-again"},
    )
    assert nothing.status_code == 409
    assert nothing.json()["error"]["code"] == "STYLE_REFERENCE_CLASSIFICATION_NOT_ACTIVE"
    install_fake_classifier(monkeypatch, ScriptedClassifier())
    resp = client.post(
        f"{PREFIX}/books/{book_id}/reclassify",
        json={"resume": True},
        headers={"X-Idempotency-Key": "stale-resume-2"},
    )
    assert resp.status_code == 200, resp.text
    assert wait_book_status(client, book_id)["classification"]["state"] == "succeeded"


def test_delete_and_reimport_as_local_only_stops_the_old_worker(client: TestClient, monkeypatch) -> None:
    """复现评审场景(I2):删书后同一份文本以「仅本机」重新导入,旧工人不得再调模型、不得写库。"""
    gated = install_fake_classifier(monkeypatch, ScriptedClassifier(gate_call=1))
    monkeypatch.setattr(import_job, "PARALLEL_BATCHES", 1)
    book_id = import_book(client, key="i2-first", text=LONG_TEXT, fake=gated, wait=False)
    assert gated.entered.wait(timeout=20)
    with SessionLocal() as session:
        old_job_id = session.scalars(
            select(StyleReferenceJob.job_id).where(StyleReferenceJob.book_id == book_id)
        ).one()

    deleted = client.delete(f"{PREFIX}/books/{book_id}", headers={"X-Idempotency-Key": "i2-delete"})
    assert deleted.status_code == 200, deleted.text

    monkeypatch.setattr(policy_module, "node_route_is_local", lambda *_a, **_k: True)
    reimported = client.post(
        f"{PREFIX}/books/import-upload",
        files={"file": ("book.txt", io.BytesIO(LONG_TEXT), "text/plain")},
        data={"title": "潮汐", "cloud_policy": "local_only"},
        headers={"X-Idempotency-Key": "i2-second"},
    )
    assert reimported.status_code == 200, reimported.text
    assert reimported.json()["data"]["book"]["book_id"] == book_id
    new_job_id = reimported.json()["data"]["job_id"]
    assert new_job_id != old_job_id
    book = wait_book_status(client, book_id)
    assert book["cloud_policy"] == "local_only"
    new_calls = book["classification"]["llm_calls"]

    gated.gate.set()  # 旧工人的那一次在飞调用回来:它已不是任何作业的主人
    time.sleep(0.5)
    with SessionLocal() as session:
        assert session.get(StyleReferenceJob, old_job_id) is None
        assert session.get(StyleReferenceJob, new_job_id).state == "succeeded"
    assert len(gated.calls) == 1 + new_calls, "旧工人删书后不得再发新的调用"
    assert _book(book_id).status == "ready"


def test_policy_is_rechecked_before_every_batch(session, monkeypatch) -> None:
    gated = _use(monkeypatch, ScriptedClassifier(gate_call=1))
    monkeypatch.setattr(import_job, "PARALLEL_BATCHES", 1)
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda *_a, **_k: False)
    book_id, job_id = _ingest(session)
    worker = threading.Thread(target=run_job_inline, args=(job_id,))
    worker.start()
    assert gated.entered.wait(timeout=20)
    with SessionLocal() as other:
        other.execute(
            update(StyleReferenceBook).where(StyleReferenceBook.book_id == book_id).values(cloud_policy="local_only")
        )
        other.commit()
    gated.gate.set()
    worker.join(timeout=20)
    job = _job(job_id)
    assert job.state == "failed" and job.error_json["code"] == "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED"
    assert "仅本机" in job.error_json["message"]
    assert len(gated.calls) == 1
    assert _book(book_id).status == "failed"


# ---------------------------------------------------------------- retype / legacy books


def test_retype_keeps_runs_profiles_and_bindings_and_bumps_the_types_revision(client: TestClient, monkeypatch) -> None:
    book_id = import_book(client, key="rt-import", text=LONG_TEXT, fake=ScriptedClassifier())
    run_id, _finding_id, profile_id = _seed_full_chain(book_id)
    with SessionLocal() as session:
        session.add(
            StyleReferenceInjectionBinding(
                binding_id="sr_bind_rt",
                profile_id=profile_id,
                scope="project",
                scope_ref_id="proj_rt",
                task_type="scene_generation",
                strategy="mixed",
                config_json={},
                status="active",
            )
        )
        book = session.get(StyleReferenceBook, book_id)
        # 老书:锚定集之外是启发式分的
        stats = dict(book.stats_json)
        stats.pop("classification_provenance", None)
        stats["classifier_calibration"] = {
            "anchor_size": 200,
            "rest_classifier": "heuristic",
            "fallback_to_heuristic": False,
            "heuristic_anchor_agreement": 0.45,
            "llm_classified_paragraphs": 200,
            "heuristic_classified_paragraphs": 26416,
        }
        book.stats_json = stats
        session.commit()
    legacy = client.get(f"{PREFIX}/books/{book_id}").json()["data"]["book"]["classification_provenance"]
    assert legacy["source"] == "legacy_heuristic" and legacy["derived"] is True
    assert legacy["heuristic_paragraphs"] == 26416 and legacy["agreement"] == 0.45

    estimate = client.get(f"{PREFIX}/books/{book_id}/classification/estimate")
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["data"]["estimate"]["est_calls"] == 17

    gated = install_fake_classifier(monkeypatch, ScriptedClassifier(gate_call=1, bulk_type=None))
    resp = client.post(
        f"{PREFIX}/books/{book_id}/reclassify",
        json={"mode": "retype"},
        headers={"X-Idempotency-Key": "rt-retype"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["mode"] == "retype"
    assert gated.entered.wait(timeout=20)
    # 就地重标期间书保持可用、派生数据都在
    during = client.get(f"{PREFIX}/books/{book_id}").json()["data"]["book"]
    assert during["status"] == "ready" and during["classification"]["mode"] == "retype"
    assert during["classification"]["state"] == "running"
    gated.gate.set()
    payload = wait_classification_state(client, book_id, ("succeeded",))
    assert payload["mode"] == "retype"
    book = client.get(f"{PREFIX}/books/{book_id}").json()["data"]["book"]
    assert book["status"] == "ready" and book["paragraph_types_revision"] == 2
    assert book["classification_provenance"]["source"] == "llm"
    assert book["classification_provenance"]["mode"] == "retype"
    with SessionLocal() as session:
        assert session.get(StyleReferenceRun, run_id) is not None
        assert session.get(StyleReferenceProfile, profile_id) is not None
        assert session.get(StyleReferenceInjectionBinding, "sr_bind_rt").status == "active"


def test_retype_needs_a_ready_book_and_refuses_a_second_active_job(client: TestClient, monkeypatch) -> None:
    gated = install_fake_classifier(monkeypatch, ScriptedClassifier(gate_call=1))
    book_id = import_book(client, key="rt2-import", text=LONG_TEXT, fake=gated, wait=False)
    assert gated.entered.wait(timeout=20)
    retype = client.post(
        f"{PREFIX}/books/{book_id}/reclassify",
        json={"mode": "retype"},
        headers={"X-Idempotency-Key": "rt2-retype"},
    )
    assert retype.status_code == 409
    assert retype.json()["error"]["code"] == "STYLE_REFERENCE_CLASSIFICATION_ALREADY_ACTIVE"
    again = client.post(
        f"{PREFIX}/books/{book_id}/reclassify",
        json={},
        headers={"X-Idempotency-Key": "rt2-destructive"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "STYLE_REFERENCE_CLASSIFICATION_ALREADY_ACTIVE"
    gated.gate.set()
    wait_book_status(client, book_id)
    with SessionLocal() as session:
        # 活动作业没被破坏式重分类的清派生数据误删
        assert session.scalars(select(StyleReferenceJob).where(StyleReferenceJob.book_id == book_id)).one().state == "succeeded"


def test_a_legacy_half_classified_book_can_resume_without_a_job_row(session, monkeypatch) -> None:
    fake = _use(monkeypatch, ScriptedClassifier())
    book_id, job_id = _ingest(session)
    with SessionLocal() as other:
        # 老版本留下的书:没有作业行,书上还挂着旧 JSON 游标,状态 failed
        other.execute(delete(StyleReferenceJob).where(StyleReferenceJob.job_id == job_id))
        book = other.get(StyleReferenceBook, book_id)
        book.status = "failed"
        book.stats_json = {**book.stats_json, "classification": {"state": "failed", "cursor": {"phase": "rest"}}}
        other.commit()
    with SessionLocal() as other:
        book = other.get(StyleReferenceBook, book_id)
        job = import_job.resume_classification(other, book, op_key="k-legacy")
        other.commit()
        new_job_id = job.job_id
    assert new_job_id != job_id
    assert "classification" not in _book(book_id).stats_json
    run_job_inline(new_job_id)
    assert _job(new_job_id).state == "succeeded"
    assert _book(book_id).status == "ready"
    assert len(fake.calls) == 17
    with SessionLocal() as other:
        ready = other.get(StyleReferenceBook, book_id)
        with pytest.raises(Exception) as caught:
            import_job.resume_classification(other, ready, op_key="k-nothing")
    assert getattr(caught.value, "code", "") == import_job.NOTHING_TO_RESUME_CODE


def test_classification_provenance_is_derived_for_older_books() -> None:
    assert import_job.classification_provenance({}) is None
    heuristic = import_job.classification_provenance(
        {"classifier_calibration": {"rest_classifier": "heuristic", "heuristic_classified_paragraphs": 5, "llm_classified_paragraphs": 2, "heuristic_anchor_agreement": 0.5}}
    )
    assert heuristic["source"] == "legacy_heuristic" and heuristic["agreement"] == 0.5
    strict = import_job.classification_provenance(
        {"classifier_calibration": {"rest_classifier": "fast_llm", "fast_model_agreement": 0.9, "llm_classified_paragraphs": 10}}
    )
    assert strict["source"] == "llm" and strict["agreement"] == 0.9 and strict["derived"] is True
    recorded = {"source": "llm", "llm_paragraphs": 3}
    assert import_job.classification_provenance({"classification_provenance": recorded}) == recorded


# ---------------------------------------------------------------- estimate / runtime / import errors


def test_estimate_uses_ledger_averages_when_present(session, monkeypatch) -> None:
    _use(monkeypatch, ScriptedClassifier())
    book_id, job_id = _ingest(session)
    with SessionLocal() as other:
        book = other.get(StyleReferenceBook, book_id)
        defaults = import_job.estimate_classification(other, book)  # 账本还是空的:全用默认值
    assert defaults["paragraphs"] == 60 and defaults["batches"] == 17 == defaults["est_calls"]
    assert defaults["phase_batches"] == {"anchor_strong": 5, "anchor_fast": 5, "rest": 7}
    assert defaults["est_input_tokens"] > 0 and defaults["est_output_tokens"] == round((25 + 25 + 35) * 32)
    assert defaults["basis"]["calls_sampled"] == 0
    assert {defaults["basis"][key] for key in (
        "input_tokens_per_char_source", "output_tokens_per_second_source", "output_tokens_per_paragraph_source"
    )} == {"default"}
    run_job_inline(job_id)
    with SessionLocal() as other:
        for i, call in enumerate(other.scalars(select(LlmCall)).all()):
            call.prompt_tokens = 1000
            call.completion_tokens = 250
            call.latency_ms = 5000
            call.reasoning_level = "off"
            call.request_payload_summary = {**(call.request_payload_summary or {}), "message_chars": 2000}
        other.commit()
        book = other.get(StyleReferenceBook, book_id)
        ledger = import_job.estimate_classification(other, book)
    basis = ledger["basis"]
    assert basis["input_tokens_per_char"] == 0.5 and basis["input_tokens_per_char_source"] == "ledger"
    assert basis["output_tokens_per_second"] == 50.0 and basis["output_tokens_per_paragraph_source"] == "ledger"
    assert basis["output_tokens_per_paragraph"] == 50.0  # 250 token ÷ 5 段
    assert ledger["est_minutes"] > 0


def test_runtime_endpoint_defaults_to_local_only_only_for_a_local_classify_route(
    client: TestClient, monkeypatch
) -> None:
    install_fake_classifier(monkeypatch, ScriptedClassifier())
    cloud = client.get(f"{PREFIX}/runtime").json()["data"]
    assert cloud["llm_enabled"] is True and cloud["llm_is_local"] is False
    assert cloud["default_cloud_policy"] == "allow_full_cloud"
    assert {route["node_id"] for route in cloud["classify_routes"]} == set(seg.CLASSIFY_NODE_IDS)
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda *_a, **_k: True)
    monkeypatch.setattr(policy_module, "_endpoint_is_local", lambda *_a, **_k: True)
    local = client.get(f"{PREFIX}/runtime").json()["data"]
    assert local["llm_is_local"] is True and local["default_cloud_policy"] == "local_only"


def test_local_only_checks_the_route_of_the_classify_node_not_the_global_provider(monkeypatch) -> None:
    """全局 provider 是本机 ollama,但分类节点路由到云端供应商:「仅本机」必须拒绝(I7)。"""
    from novel_system.services.llm_providers.base import ProviderRuntimeConfig

    class _Client:
        _provider_configs = {
            "cloud_relay": ProviderRuntimeConfig(provider_id="cloud_relay", provider_type="openai_compatible", base_url="https://relay.example.com/v1"),
            "ollama": ProviderRuntimeConfig(provider_id="ollama", provider_type="ollama", base_url="http://127.0.0.1:11434"),
        }

    monkeypatch.setattr(policy_module, "runtime_llm_is_local", lambda settings=None: True)
    cloud_route = SimpleNamespace(provider="openai_compatible", provider_id="cloud_relay", model="relay-model")
    local_route = SimpleNamespace(provider="ollama", provider_id="ollama", model="qwen")
    book = SimpleNamespace(book_id="sr_book_route", cloud_policy="local_only", stats_json={})
    with pytest.raises(Exception) as caught:
        policy_module.ensure_cloud_llm_allowed(
            book, operation="classify_book", routes={seg.NODE_BULK: cloud_route}, llm_client=_Client()
        )
    err = caught.value
    assert err.code == "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED"
    assert err.details["node_id"] == seg.NODE_BULK and err.details["base_url"] == "https://relay.example.com/v1"
    assert "仅本机" in err.message and err.details["author_action"]["view"] == "systemConfig"
    policy_module.ensure_cloud_llm_allowed(
        book, operation="classify_book", routes={seg.NODE_ANCHOR: local_route}, llm_client=_Client()
    )


def test_duplicate_and_empty_imports_are_domain_errors(client: TestClient) -> None:
    book_id = import_book(client, key="dup-1", text=LONG_TEXT, fake=ScriptedClassifier())
    with fake_import_llm(ScriptedClassifier()):
        dup = client.post(
            f"{PREFIX}/books/import-upload",
            files={"file": ("again.txt", io.BytesIO(LONG_TEXT), "text/plain")},
            data={
                "title": "再来一次",
                "cloud_policy": "segments_only",
                "rights_declaration": json.dumps({"analysis_rights": True, "send_rights": True}),
            },
            headers={"X-Idempotency-Key": "dup-2"},
        )
        empty = client.post(
            f"{PREFIX}/books/import-upload",
            files={"file": ("empty.txt", io.BytesIO(b"  \n\n \n"), "text/plain")},
            data={
                "title": "空",
                "cloud_policy": "segments_only",
                "rights_declaration": json.dumps({"analysis_rights": True, "send_rights": True}),
            },
            headers={"X-Idempotency-Key": "empty-1"},
        )
    assert dup.status_code == 409, dup.text
    error = dup.json()["error"]
    assert error["code"] == "STYLE_REFERENCE_BOOK_DUPLICATE"
    assert error["details"]["book_id"] == book_id and error["details"]["title"] == "测试"
    assert error["details"]["status"] == "ready"
    assert error["details"]["author_action"]["book_id"] == book_id
    assert empty.status_code == 400, empty.text
    assert empty.json()["error"]["code"] == "STYLE_REFERENCE_BOOK_EMPTY"


def test_local_only_import_under_a_cloud_classify_route_is_refused_in_chinese(client: TestClient, monkeypatch) -> None:
    fake = install_fake_classifier(monkeypatch, ScriptedClassifier())
    resp = client.post(
        f"{PREFIX}/books/import-upload",
        files={"file": ("local.txt", io.BytesIO(LONG_TEXT), "text/plain")},
        data={"title": "本机", "cloud_policy": "local_only"},
        headers={"X-Idempotency-Key": "local-cloud"},
    )
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED"
    assert "仅本机" in error["message"] and "needs a local LLM" not in error["message"]
    assert error["details"]["node_id"] in seg.CLASSIFY_NODE_IDS
    assert error["details"]["author_action"]["view"] == "systemConfig"
    assert fake.calls == []


# ---------------------------------------------------------------- activity / compat progress


def test_activity_lists_the_job_and_a_compat_alias(client: TestClient, monkeypatch) -> None:
    """活动清单:作业条目(新界面只认它)+ 带 ``compat_alias_of`` 的旧别名条目(P7 删);旧的导入进度轮询端点已删。"""
    gated = install_fake_classifier(monkeypatch, ScriptedClassifier(gate_call=2))
    monkeypatch.setattr(import_job, "PARALLEL_BATCHES", 1)
    book_id = import_book(client, key="act-key", text=LONG_TEXT, fake=gated, wait=False)
    assert gated.entered.wait(timeout=20)
    items = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    by_key = {item["key"]: item for item in items}
    canonical = next(item for item in items if item["key"].startswith("job:"))
    assert canonical["kind"] == "classify" and canonical["status"] == "running"
    assert canonical["book_id"] == book_id and canonical["title"] == "测试" and canonical["mode"] == "import"
    assert canonical["steps"] == {"done": 1, "total": 17}
    assert canonical["cancellable"] is True
    alias = by_key["act-key"]
    assert alias["compat_alias_of"] == canonical["key"]
    assert alias["kind"] == "import" and alias["status"] == "running" and alias["phase"] == "classify"
    assert alias["classify"]["batches_done"] == 1 and alias["classify"]["batches_total"] == 17
    assert client.get(f"{PREFIX}/imports/act-key/progress").status_code == 404
    gated.gate.set()
    wait_book_status(client, book_id)
    items = client.get(f"{PREFIX}/activity").json()["data"]["items"]
    done = next(item for item in items if item["key"] == canonical["key"])
    assert done["status"] == "succeeded" and done["percent"] == 100.0 and done["steps"] == {"done": 17, "total": 17}


def test_startup_marks_books_left_by_the_old_cursor_state_machine_as_failed(session, monkeypatch) -> None:
    """升级前书上 JSON 游标的分类没有作业行可续:启动时标 failed(「继续分类」建新作业),有活动作业的书不动。"""
    _use(monkeypatch, ScriptedClassifier())
    live_book, _live_job = _ingest(session)  # ingesting + queued 作业:正常在分类
    orphan_a, job_a = _ingest(session, text=LONG_TEXT + "甲".encode("utf-8"), op_key="k-a")
    orphan_b, job_b = _ingest(session, text=LONG_TEXT + "乙".encode("utf-8"), op_key="k-b")
    with SessionLocal() as other:
        other.execute(delete(StyleReferenceJob).where(StyleReferenceJob.job_id.in_([job_a, job_b])))
        other.execute(
            update(StyleReferenceBook).where(StyleReferenceBook.book_id == orphan_b).values(status="cancelling")
        )
        other.commit()
    with SessionLocal() as other:
        fixed = import_job.fail_orphaned_classifications(other)
    assert sorted(fixed) == sorted([orphan_a, orphan_b])
    assert _book(orphan_a).status == _book(orphan_b).status == "failed"
    assert _book(live_book).status == "ingesting"


def test_reclassify_and_retype_are_refused_while_a_learn_job_is_active(client: TestClient, monkeypatch) -> None:
    """学习文风作业在读这本书的段落类型：它排队或运行时，重分类 / 就地重标 / 继续分类一律 409。"""
    from novel_system.services.style_reference.jobs import JOB_KIND_LEARN, StyleJobService

    fake = install_fake_classifier(monkeypatch, ScriptedClassifier())
    book_id = import_book(client, key="learn-guard-import", text=LONG_TEXT, fake=fake)
    with SessionLocal() as session:
        learn = StyleJobService(session).create(JOB_KIND_LEARN, book_id=book_id)
        session.commit()
        learn_id = learn.job_id
    for key, body in (("lg-retype", {"mode": "retype"}), ("lg-destructive", {}), ("lg-resume", {"resume": True})):
        resp = client.post(f"{PREFIX}/books/{book_id}/reclassify", json=body, headers={"X-Idempotency-Key": key})
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "STYLE_REFERENCE_BOOK_LEARNING"
        assert resp.json()["error"]["details"]["job_id"] == learn_id
    with SessionLocal() as session:
        StyleJobService(session).request_cancel(learn_id)
        session.commit()
    ok = client.post(f"{PREFIX}/books/{book_id}/reclassify", json={"mode": "retype"}, headers={"X-Idempotency-Key": "lg-after"})
    assert ok.status_code == 200, ok.text
