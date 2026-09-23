"""风格参考模块加固回归(2026-06 审查修复)。

覆盖六组新行为:
1. cloud_policy 强制执行(local_only 拒绝云端 LLM 操作;严格 LLM 后没有启发式兜底,分类节点须走本机模型)
2. LLMRequiredError / CloudPolicyBlockedError 映射 DomainError 409
3. 反抄袭红线段(anti_plagiarism_block)接线:渲染 / banned_terms 填充 / 免截断
4. 抄袭检测:规范化匹配(防标点空格绕过)+ 全书段落语料
5. sync 校验含 quantitative;semantic 路失败时 PASS 封顶 PARTIAL
6. 僵尸 run 回收 + 孤儿 pending report 回收 + 上传体积上限
"""

from __future__ import annotations

import io
import time

import pytest

from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.errors import (
    CloudPolicyBlockedError,
    LLMRequiredError,
)
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.injection import InjectionService
from novel_system.services.style_reference.preview import PreviewService
from novel_system.services.style_reference.profile_synthesizer import ProfileSynthesizer
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.run_orchestrator import RunOrchestrator
from novel_system.services.style_reference.schemas import (
    ValidateRequest,
    ValidationMode,
)
from novel_system.services.style_reference.validation import (
    ValidationOrchestrator,
    _compute_full_verdict,
    check_plagiarism,
    clear_plagiarism_corpus_cache,
    run_sync_validate,
)


@pytest.fixture(autouse=True)
def _clear_corpus_cache():
    clear_plagiarism_corpus_cache()
    yield
    clear_plagiarism_corpus_cache()


class _SentinelLLM:
    """任何调用都视为违规的 LLM client 哨兵。"""

    def __init__(self) -> None:
        self.called = False

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            self.called = True
            raise AssertionError(f"LLM client must not be called (method {name})")

        return _boom


SAMPLE_TEXT = """这是一段较长的叙述文字,介绍清晨场景与人物心情,字数足以触发分段。

他说:"今天天气不错。"

我心里想着昨天的事情,觉得有些不安。

雪花从天空缓缓飘落,落在他的肩头,他没有拂去。
"""


def _seed_book(
    seed: str,
    *,
    cloud_policy: str = "local_only",
    with_paragraphs: bool = True,
) -> str:
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book_id = f"sr_book_hd_{seed}"
        repo.create_book(
            book_id=book_id,
            title="t",
            source_kind="upload",
            cloud_policy=cloud_policy,
            text_checksum=f"chk_hd_{seed}",
            total_chars=len(SAMPLE_TEXT),
            status="ready",
            stats_json=(
                {"rights_declaration": {
                    "declared": True, "analysis_rights": True, "send_rights": True,
                }}
                if cloud_policy != "local_only"
                else {}
            ),
        )
        if with_paragraphs:
            paragraphs = [p.strip() for p in SAMPLE_TEXT.split("\n\n") if p.strip()]
            for idx, body in enumerate(paragraphs):
                repo.create_paragraph(
                    paragraph_id=f"sr_para_hd_{seed}_{idx:02d}",
                    book_id=book_id,
                    paragraph_index=idx,
                    paragraph_type="narration",
                    start_offset=0,
                    end_offset=len(body),
                    text=body,
                    char_count=len(body),
                    classifier_confidence=0.9,
                )
        session.commit()
    return book_id


def _seed_profile_for_book(seed: str, book_id: str, *, profile_json: dict | None = None) -> str:
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        run_id = f"sr_run_hd_{seed}"
        profile_id = f"sr_profile_hd_{seed}"
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        repo.create_profile(
            profile_id=profile_id,
            book_id=book_id,
            run_id=run_id,
            title="t",
            status="active",
            profile_json=profile_json or {"narrative_summary": "短句白描"},
            coverage_json={},
            source_finding_ids_json=[],
        )
        session.commit()
    return profile_id


# ---------------------------------------------------------------------------
# 1/2. cloud_policy 强制执行 + 错误映射
# ---------------------------------------------------------------------------


def test_cloud_policy_blocked_error_is_domain_error_409():
    err = CloudPolicyBlockedError(book_id="b1", operation="op")
    assert isinstance(err, DomainError)
    assert err.status_code == 409
    assert err.code == "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED"
    assert err.details["author_action"]


def test_llm_required_error_is_domain_error_409():
    err = LLMRequiredError(operation="op")
    assert isinstance(err, DomainError)
    assert err.status_code == 409
    assert err.code == "STYLE_REFERENCE_LLM_REQUIRED"
    assert err.details["author_action"]


def test_start_extract_run_blocked_for_local_only_book():
    book_id = _seed_book("run_block", cloud_policy="local_only")
    with SessionLocal() as session:
        orch = RunOrchestrator(session, llm_client=object(), llm_enabled=True)
        with pytest.raises(CloudPolicyBlockedError):
            orch.start_extract_run(book_id)


def test_reclassify_blocked_for_local_only_book():
    book_id = _seed_book("reclass_block", cloud_policy="local_only")
    with SessionLocal() as session:
        service = IngestService(session, llm_client=object(), llm_enabled=True)
        for mode in ("reclassify", "retype"):
            with pytest.raises(CloudPolicyBlockedError):
                service.start_reclassify(book_id, mode=mode)
        with pytest.raises(CloudPolicyBlockedError):
            service.resume(book_id)


def test_synthesize_blocked_for_local_only_book():
    book_id = _seed_book("synth_block", cloud_policy="local_only")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_run(run_id="sr_run_hd_sb", book_id=book_id, status="done", phase="done")
        session.commit()
        synth = ProfileSynthesizer(session, llm_client=object(), llm_enabled=True)
        with pytest.raises(CloudPolicyBlockedError):
            synth.synthesize(book_id, "sr_run_hd_sb")


def test_preview_blocked_for_local_only_book():
    book_id = _seed_book("prev_block", cloud_policy="local_only")
    profile_id = _seed_profile_for_book("prev_block", book_id)
    with SessionLocal() as session:
        svc = PreviewService(session, llm_client=object(), llm_enabled=True)
        with pytest.raises(CloudPolicyBlockedError):
            svc.generate(profile_id)


def test_ingest_local_only_with_a_cloud_llm_is_refused_not_heuristic(monkeypatch):
    """2026-09-15 严格 LLM:local_only 的段落不送云,也没有启发式兜底——分类节点走云端接入时直接拒绝,
    LLM client 一次都不许被调用;分类节点走本机模型(打桩 node_route_is_local)才建分类作业、由模型分类。"""
    from novel_system.services.style_reference import import_job
    from novel_system.services.style_reference import policy as policy_module
    from novel_system.services.style_reference.jobs import run_job_inline

    sentinel = _SentinelLLM()
    with SessionLocal() as session:
        service = IngestService(session, llm_client=sentinel, llm_enabled=True)
        with pytest.raises(CloudPolicyBlockedError) as caught:
            service.ingest_upload(
                raw_bytes=SAMPLE_TEXT.encode("utf-8"),
                file_name="local_only_book.txt",
                title="本地书",
                author_label=None,
                cloud_policy="local_only",
            )
    assert caught.value.details["author_action"]["view"] == "systemConfig"
    assert caught.value.details["node_id"] in (
        "style_ref_paragraph_classify_anchor",
        "style_ref_paragraph_classify_bulk",
    )
    assert "仅本机" in caught.value.message
    assert not sentinel.called

    from tests.conftest import build_fake_paragraph_classifier

    fake = build_fake_paragraph_classifier()(rule="default")
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda *_a, **_k: True)
    monkeypatch.setattr(import_job, "resolve_classification_client", lambda: (fake, True))
    with SessionLocal() as session:
        service = IngestService(session, llm_client=fake, llm_enabled=True)
        result = service.ingest_upload(
            raw_bytes=SAMPLE_TEXT.encode("utf-8"),
            file_name="local_only_book_local_llm.txt",
            title="本地书",
            author_label=None,
            cloud_policy="local_only",
        )
        session.commit()
        book_id, job_id = result.book.book_id, result.job.job_id
    run_job_inline(job_id)
    assert result.paragraphs_count > 0
    assert fake.call_count >= 1
    with SessionLocal() as session:
        book = StyleReferenceRepository(session).get_book(book_id)
        assert book.status == "ready"
        assert book.stats_json["classifier_calibration"]["fallback_to_heuristic"] is False
        assert book.stats_json["classification_provenance"]["source"] == "llm"


def test_async_full_for_local_only_book_is_refused_without_a_local_llm():
    """2026-09-15 严格 LLM:local_only 不出云,全量三路也不再降成 partial——云端模型直接 409。"""
    book_id = _seed_book("async_local", cloud_policy="local_only")
    profile_id = _seed_profile_for_book("async_local", book_id)
    sentinel = _SentinelLLM()
    with SessionLocal() as session:
        orch = ValidationOrchestrator(session, llm_client=sentinel, llm_enabled=True)
        with pytest.raises(CloudPolicyBlockedError):
            orch.validate(
                profile_id,
                ValidateRequest(generated_text="一段完全原创的全新文本表达", mode=ValidationMode.ASYNC_FULL),
            )
    assert not sentinel.called


def test_async_full_without_llm_is_refused():
    """全量三路必须有 LLM:未启用 → 409 STYLE_REFERENCE_LLM_REQUIRED,不再静默降级。"""
    book_id = _seed_book("async_no_llm", cloud_policy="segments_only")
    profile_id = _seed_profile_for_book("async_no_llm", book_id)
    with SessionLocal() as session:
        orch = ValidationOrchestrator(session, llm_client=None, llm_enabled=False)
        with pytest.raises(LLMRequiredError):
            orch.validate(
                profile_id,
                ValidateRequest(generated_text="一段完全原创的全新文本表达", mode=ValidationMode.ASYNC_FULL),
            )


# ---------------------------------------------------------------------------
# 3. 反抄袭红线段接线
# ---------------------------------------------------------------------------


def _seed_injection(seed: str, *, banned_terms: list[str] | None = None) -> str:
    """book + profile + project binding(strategy A)。返回 project_id。"""
    project_id = f"proj_hd_{seed}"
    book_id = _seed_book(seed, cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book(
        seed,
        book_id,
        profile_json={
            "narrative_summary": "短句白描",
            "style_features": ["短句", "白描"],
        },
    )
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_binding(
            binding_id=f"sr_bind_hd_{seed}",
            profile_id=profile_id,
            scope="project",
            scope_ref_id=project_id,
            task_type="scene_generation",
            strategy="A",
            config_json={},
            status="active",
        )
        for i, term in enumerate(banned_terms or []):
            repo.create_banned_term(
                term_id=f"sr_term_hd_{seed}_{i}",
                profile_id=profile_id,
                term=term,
                replacement_hint=None,
                source="user",
                scope="generation",
            )
        session.commit()
    return project_id


def test_anti_plagiarism_block_present_and_in_prefix():
    project_id = _seed_injection("antiplag")
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    assert "严格禁止" in fragments.anti_plagiarism_block
    prefix = fragments.to_system_prompt_prefix()
    assert "严格禁止" in prefix
    # 红线段排最后(在 [/STYLE_REFERENCE] 之前)
    assert prefix.rindex("严格禁止") > prefix.rindex(fragments.positive_block.strip()[:8])


def test_anti_plagiarism_block_includes_generation_banned_terms():
    project_id = _seed_injection("antiterm", banned_terms=["龙傲天", "玛丽苏镇"])
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    assert "- 龙傲天" in fragments.anti_plagiarism_block
    assert "- 玛丽苏镇" in fragments.anti_plagiarism_block


def test_anti_plagiarism_block_absent_when_fragments_empty():
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for("proj_nonexistent", "scene_generation")
    assert fragments.anti_plagiarism_block == ""
    assert fragments.to_system_prompt_prefix() == ""


def test_anti_plagiarism_block_survives_budget_truncation():
    """strategy B 预算截断不得动红线段。"""
    project_id = f"proj_hd_budget"
    book_id = _seed_book("budget", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book(
        "budget",
        book_id,
        profile_json={
            "narrative_summary": "概述" * 500,
            "style_features": ["要点" * 200],
        },
    )
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_binding(
            binding_id="sr_bind_hd_budget",
            profile_id=profile_id,
            scope="project",
            scope_ref_id=project_id,
            task_type="scene_generation",
            strategy="B",
            config_json={},
            status="active",
        )
        session.commit()
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    # positive 被预算截断,红线段完整保留
    assert len(fragments.positive_block) < 1000
    assert "严格禁止" in fragments.anti_plagiarism_block
    assert "抄的是句子" in fragments.anti_plagiarism_block


# ---------------------------------------------------------------------------
# 4. 抄袭检测:规范化 + 全书语料
# ---------------------------------------------------------------------------


def test_plagiarism_detects_punctuation_and_space_bypass():
    source = "雪花从天空缓缓飘落,落在他的肩头,他没有拂去。"
    # 改标点 + 插空格的微改抄袭
    bypass = "雪花从天空缓缓飘落、落在 他的肩头;他没有拂去!"
    report = check_plagiarism(bypass, [source])
    assert not report.passed
    assert report.hits


def test_plagiarism_position_maps_back_to_original_text():
    source = "雪花从天空缓缓飘落,落在他的肩头,他没有拂去。"
    generated = "开头几个字。雪花从天空缓缓飘落,落在他的肩头,他没有拂去。结尾。"
    report = check_plagiarism(generated, [source])
    assert not report.passed
    hit = report.hits[0]
    assert generated[hit.position : hit.position + len(hit.matched_text)] == hit.matched_text
    assert "雪花" in hit.matched_text


def test_plagiarism_short_common_phrases_pass():
    report = check_plagiarism("今天天气不错。", ["他说:今天天气不错。然后他走了,再没有回头看一眼。"])
    # 规范化后重叠仅 6 字(今天天气不错),低于 12 字阈值
    assert report.passed


def test_sync_validate_uses_full_book_corpus_not_only_quotes():
    """抄全书中未被引用为 quote 的段落,也必须检出 plagiarism。"""
    book_id = _seed_book("fullcorpus", cloud_policy="segments_only")
    profile_id = _seed_profile_for_book("fullcorpus", book_id)
    copied = "雪花从天空缓缓飘落,落在他的肩头,他没有拂去。"  # 段落原文,但没有任何 quote 行
    with SessionLocal() as session:
        profile = StyleReferenceRepository(session).get_profile(profile_id)
        report = run_sync_validate(copied, profile, session)
    assert report.verdict.value == "plagiarism"


# ---------------------------------------------------------------------------
# 5. sync 含 quantitative + semantic 降级语义
# ---------------------------------------------------------------------------


def test_sync_validate_includes_quantitative_check():
    """与 baseline 偏差大的文本,sync 路径应产出量化报告并降级 verdict。"""
    book_id = _seed_book("quant", cloud_policy="segments_only")
    profile_id = _seed_profile_for_book(
        "quant",
        book_id,
        profile_json={
            "narrative_summary": "短句白描",
            # 构造与超长句文本必然冲突的 baseline(均值 5 字 / 极小容差)
            "metrics_baseline": {
                "avg_sentence_length": {"mean": 5.0, "std": 0.01},
            },
        },
    )
    long_sentence_text = "这是一个被刻意写得非常非常非常非常非常非常非常非常非常长的句子它没有任何标点直到结束。"
    with SessionLocal() as session:
        profile = StyleReferenceRepository(session).get_profile(profile_id)
        report = run_sync_validate(long_sentence_text, profile, session)
    assert report.quantitative_json, "sync 路径必须产出量化对照"
    assert report.verdict.value in ("partial", "fail")


class _FakeItem:
    def __init__(self, passed: bool = True, score: float = 8.0):
        self.passed = passed
        self.score = score


class _FakePlag:
    passed = True


def test_semantic_degraded_caps_pass_to_partial():
    verdict = _compute_full_verdict(
        quant=[_FakeItem(passed=True)],
        semantic=[],
        plag=_FakePlag(),
        forbid=[],
        semantic_degraded=True,
    )
    assert verdict.value == "partial"
    verdict_ok = _compute_full_verdict(
        quant=[_FakeItem(passed=True)],
        semantic=[],
        plag=_FakePlag(),
        forbid=[],
        semantic_degraded=False,
    )
    assert verdict_ok.value == "pass"


# ---------------------------------------------------------------------------
# 6. 僵尸 run 回收
# ---------------------------------------------------------------------------


def test_stale_running_run_reaped_on_next_start():
    book_id = _seed_book("reaper", cloud_policy="segments_only")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_run(
            run_id="sr_run_hd_stale",
            book_id=book_id,
            status="running",
            phase="extract",
            started_at="2020-01-01T00:00:00+00:00",
        )
        session.commit()
        orch = RunOrchestrator(session, llm_client=object(), llm_enabled=True)
        reaped = orch._reap_stale_runs(book_id)
        session.commit()
    assert reaped == 1
    with SessionLocal() as session:
        run = StyleReferenceRepository(session).get_run("sr_run_hd_stale")
        assert run.status == "failed"
        assert run.coverage_json.get("failure_reason") == "stale_running_reaped"


def test_recent_running_run_not_reaped():
    from novel_system.db.models import utcnow

    book_id = _seed_book("reaper2", cloud_policy="segments_only")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_run(
            run_id="sr_run_hd_fresh",
            book_id=book_id,
            status="running",
            phase="extract",
            started_at=utcnow(),
        )
        session.commit()
        orch = RunOrchestrator(session, llm_client=object(), llm_enabled=True)
        assert orch._reap_stale_runs(book_id) == 0


def test_stale_queued_run_is_not_reaped_before_worker_claims_it():
    """队列背压不是 worker 中断，queued 即使等待很久也必须保留。"""

    book_id = _seed_book("reaper_queued", cloud_policy="segments_only")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_run(
            run_id="sr_run_hd_stale_queued",
            book_id=book_id,
            status="running",
            phase="extract",
            dispatch_state="queued",
            heartbeat_at="2020-01-01T00:00:00+00:00",
            started_at="2020-01-01T00:00:00+00:00",
        )
        session.commit()
        orch = RunOrchestrator(session, llm_client=object(), llm_enabled=True)
        assert orch._reap_stale_runs(book_id) == 0
        session.expire_all()
        run = repo.get_run("sr_run_hd_stale_queued")
        assert run is not None
        assert run.status == "running"
        assert run.dispatch_state == "queued"


# ---------------------------------------------------------------------------
# 7. 路由层:上传上限 / 孤儿报告回收 / LLMRequired 409
# ---------------------------------------------------------------------------

PREFIX = "/api/v2/style-reference"


def _client():
    from fastapi.testclient import TestClient

    from novel_system.api.app import create_app

    return TestClient(create_app())


def test_upload_exceeding_size_limit_returns_413(monkeypatch: pytest.MonkeyPatch):
    from novel_system.api.routes import style_reference as routes_mod

    monkeypatch.setattr(routes_mod, "MAX_UPLOAD_BYTES", 64)
    with _client() as client:
        files = {"file": ("big.txt", io.BytesIO("超限内容".encode("utf-8") * 100), "text/plain")}
        resp = client.post(
            f"{PREFIX}/books/import-upload",
            files=files,
            data={"title": "大文件", "cloud_policy": "segments_only"},
            headers={"X-Idempotency-Key": "imp_big_1"},
        )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_UPLOAD_TOO_LARGE"


def test_llm_required_maps_to_409_with_author_action():
    """LLM 未启用时 reclassify 应返回 409 + author_action(而非通用 500)。"""
    book_id = _seed_book("llm409", cloud_policy="segments_only")
    with _client() as client:
        resp = client.post(
            f"{PREFIX}/books/{book_id}/reclassify",
            headers={"X-Idempotency-Key": "rc_409_1"},
        )
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "STYLE_REFERENCE_LLM_REQUIRED"
    assert err["details"]["author_action"]


def test_orphan_pending_report_degrades_to_fail_on_poll():
    book_id = _seed_book("orphan", cloud_policy="segments_only")
    profile_id = _seed_profile_for_book("orphan", book_id)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        row = repo.create_validation_report(
            report_id="sr_rep_hd_orphan",
            profile_id=profile_id,
            target_kind="manual",
            target_ref_id=None,
            verdict="",
            quantitative_json=[],
            semantic_json=[],
            plagiarism_json={},
            forbidden_hits_json=[],
            mode_executed="async_full",
        )
        row.created_at = "2020-01-01T00:00:00+00:00"
        session.commit()
    with _client() as client:
        resp = client.get(f"{PREFIX}/reports/sr_rep_hd_orphan")
    assert resp.status_code == 200
    report = resp.json()["data"]["report"]
    assert report["verdict"] == "fail"
    assert report["status"] == "failed"


# ---------------------------------------------------------------------------
# 8. 后台 run + 进度 / apply 注入配置 / binding 唯一约束 / few-shot
# ---------------------------------------------------------------------------


def _seed_ingested_book(seed: str) -> str:
    with SessionLocal() as session:
        result = IngestService(session, llm_enabled=False).ingest_upload(
            raw_bytes=(SAMPLE_TEXT * 3).encode("utf-8"),
            file_name=f"hd_{seed}.txt",
            title=f"硬化{seed}",
            author_label=None,
            cloud_policy="segments_only",
            rights_declaration={"analysis_rights": True, "send_rights": True},
        )
        session.commit()
        return result.book.book_id


def test_background_run_returns_immediately_and_completes(fake_extractor_llm):
    book_id = _seed_ingested_book("bg")
    with SessionLocal() as session:
        orch = RunOrchestrator(
            session, llm_client=fake_extractor_llm("default"), llm_enabled=True
        )
        # 测试书 <1 万字(assessment 全 skip),force 绕过输入量门槛
        result = orch.start_extract_run(book_id, background=True, force=True)
    assert result.status == "running"
    assert result.sub_dim_results == []

    deadline = time.monotonic() + 15.0
    final = None
    while time.monotonic() < deadline:
        with SessionLocal() as session:
            run = StyleReferenceRepository(session).get_run(result.run_id)
            if run is not None and run.status in ("done", "failed", "cancelled"):
                final = (run.status, dict(run.coverage_json or {}))
                break
        time.sleep(0.1)
    assert final is not None, "后台 run 应在 15s 内完成"
    status, coverage = final
    assert status == "done"
    progress = coverage.get("progress") or {}
    assert progress.get("layers_done") == progress.get("layers_total")
    assert coverage.get("sub_dimensions"), "完成后应有 sub_dimensions 覆盖统计"


def test_apply_profile_persists_injection_config():
    from novel_system.services.style_reference.materialization import MaterializationService

    book_id = _seed_book("applycfg", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("applycfg", book_id)
    config = {"intensity": 35, "sub_dimensions": ["language.rhetoric"], "include_metric": True}
    with SessionLocal() as session:
        svc = MaterializationService(session)
        result = svc.apply_profile(
            profile_id,
            scope="project",
            scope_ref_id="proj_applycfg",
            strategy="mixed",
            config_json=config,
        )
        session.commit()
        binding = StyleReferenceRepository(session).get_binding(result.binding_id)
        assert binding.config_json == config
        assert binding.strategy == "mixed"
        # 重复 apply 调整配置 → 复用同一 binding 并更新 config
        result2 = svc.apply_profile(
            profile_id,
            scope="project",
            scope_ref_id="proj_applycfg",
            strategy="A",
            config_json={"intensity": 90},
        )
        session.commit()
        assert result2.binding_id == result.binding_id
        binding = StyleReferenceRepository(session).get_binding(result.binding_id)
        assert binding.config_json == {"intensity": 90}
        assert binding.strategy == "A"


def test_binding_unique_constraint_blocks_duplicates():
    from sqlalchemy.exc import IntegrityError

    book_id = _seed_book("uniq", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("uniq", book_id)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_binding(
            binding_id="sr_bind_hd_uniq_1",
            profile_id=profile_id,
            scope="project",
            scope_ref_id="proj_uniq",
            task_type="scene_generation",
            strategy="A",
            config_json={},
            status="active",
        )
        session.commit()
        with pytest.raises(IntegrityError):
            repo.create_binding(
                binding_id="sr_bind_hd_uniq_2",
                profile_id=profile_id,
                scope="project",
                scope_ref_id="proj_uniq",
                task_type="scene_generation",
                strategy="B",
                config_json={},
                status="active",
            )
        session.rollback()


def test_strategy_b_renders_few_shot_from_samples_index():
    book_id = _seed_book("fewshot", cloud_policy="segments_only", with_paragraphs=False)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_quote(
            quote_id="sr_q_hd_fs_1",
            book_id=book_id,
            paragraph_id=None,
            span_start=0,
            span_end=10,
            quote_text="他低头看着脚下的路,一言不发。",
            illustrates_dims=[],
            extracted_features={},
        )
        session.commit()
    profile_id = _seed_profile_for_book(
        "fewshot",
        book_id,
        profile_json={
            "narrative_summary": "短句白描",
            "style_features": ["短句"],
            "scene_samples_index": {"dialogue": ["sr_q_hd_fs_1"]},
        },
    )
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_binding(
            binding_id="sr_bind_hd_fs",
            profile_id=profile_id,
            scope="project",
            scope_ref_id="proj_fewshot",
            task_type="scene_generation",
            strategy="B",
            config_json={},
            status="active",
        )
        session.commit()
        fragments = InjectionService(session).fragments_for("proj_fewshot", "scene_generation")
    assert "风格样例" in fragments.few_shot_block
    assert "他低头看着脚下的路" in fragments.few_shot_block
    # few-shot 引用原文 → 红线段必须在场,且进入最终 prefix
    assert "严格禁止" in fragments.anti_plagiarism_block
    assert "风格样例" in fragments.to_system_prompt_prefix()


def test_strategy_a_has_no_few_shot_block():
    project_id = _seed_injection("nofs")
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(project_id, "scene_generation")
    assert fragments.few_shot_block == ""


def test_strategy_mixed_renders_few_shot_from_samples_index():
    """mixed = A + B:few-shot 样例块必须随混合策略注入(场景生成默认即 mixed)。"""
    book_id = _seed_book("fewshotmx", cloud_policy="segments_only", with_paragraphs=False)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_quote(
            quote_id="sr_q_hd_fsmx_1",
            book_id=book_id,
            paragraph_id=None,
            span_start=0,
            span_end=10,
            quote_text="他把伞收了,站在檐下看雨。",
            illustrates_dims=[],
            extracted_features={},
        )
        session.commit()
    profile_id = _seed_profile_for_book(
        "fewshotmx",
        book_id,
        profile_json={
            "narrative_summary": "短句白描",
            "style_features": ["短句"],
            "scene_samples_index": {"dialogue": ["sr_q_hd_fsmx_1"]},
        },
    )
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_binding(
            binding_id="sr_bind_hd_fsmx",
            profile_id=profile_id,
            scope="project",
            scope_ref_id="proj_fewshotmx",
            task_type="scene_generation",
            strategy="mixed",
            config_json={"intensity": 80},
            status="active",
        )
        session.commit()
        fragments = InjectionService(session).fragments_for("proj_fewshotmx", "scene_generation")
    assert "风格样例" in fragments.few_shot_block
    assert "他把伞收了" in fragments.few_shot_block
    # few-shot 引用原文 → 红线段必须在场
    assert "严格禁止" in fragments.anti_plagiarism_block
    assert "风格样例" in fragments.to_system_prompt_prefix()


def test_failed_idempotent_action_does_not_half_commit():
    """幂等层:action 中途失败时,已 flush 的半成品写入必须随回滚消失。"""
    from novel_system.services.idempotency import execute_with_idempotency

    book_id = _seed_book("halfcommit", cloud_policy="segments_only", with_paragraphs=False)

    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)

        def _action() -> dict:
            repo.create_run(
                run_id="sr_run_hd_half",
                book_id=book_id,
                status="running",
                phase="extract",
            )
            raise DomainError("BOOM", "deliberate failure", status_code=400)

        with pytest.raises(DomainError):
            execute_with_idempotency(
                session,
                idempotency_key="idem_half_1",
                method="POST",
                path_template="/test/half-commit",
                payload={},
                action=_action,
            )

    with SessionLocal() as session:
        assert StyleReferenceRepository(session).get_run("sr_run_hd_half") is None, (
            "失败 action 的半成品 run 行不应被提交"
        )


def test_fresh_pending_report_stays_pending_on_poll():
    book_id = _seed_book("orphan2", cloud_policy="segments_only")
    profile_id = _seed_profile_for_book("orphan2", book_id)
    with SessionLocal() as session:
        StyleReferenceRepository(session).create_validation_report(
            report_id="sr_rep_hd_fresh",
            profile_id=profile_id,
            target_kind="manual",
            target_ref_id=None,
            verdict="",
            quantitative_json=[],
            semantic_json=[],
            plagiarism_json={},
            forbidden_hits_json=[],
            mode_executed="async_full",
        )
        session.commit()
    with _client() as client:
        resp = client.get(f"{PREFIX}/reports/sr_rep_hd_fresh")
    report = resp.json()["data"]["report"]
    assert report["verdict"] == ""
    assert report["status"] == "pending"


def test_old_explicitly_queued_report_is_not_reaped_by_polling():
    """轮询端点不能把正常线程池背压误判为孤儿。"""

    from novel_system.api.routes.style_reference import _reap_orphan_report
    from novel_system.db.models import StyleReferenceValidationReport

    book_id = _seed_book("queued_report", cloud_policy="segments_only")
    profile_id = _seed_profile_for_book("queued_report", book_id)
    with SessionLocal() as session:
        report = StyleReferenceRepository(session).create_validation_report(
            report_id="sr_rep_hd_queued_old",
            profile_id=profile_id,
            target_kind="manual",
            target_ref_id=None,
            verdict="",
            quantitative_json=[],
            semantic_json=[],
            plagiarism_json={},
            forbidden_hits_json=[],
            mode_executed="async_full",
            status="queued",
            heartbeat_at="2020-01-01T00:00:00+00:00",
        )
        report.created_at = "2020-01-01T00:00:00+00:00"
        session.commit()
    with SessionLocal() as session:
        report = session.get(StyleReferenceValidationReport, "sr_rep_hd_queued_old")
        _reap_orphan_report(session, report)
        assert report.verdict == ""
        assert report.status == "queued"


# ---------------------------------------------------------------------------
# 9. bind_style_profile 决策卡 effect 转发注入配置(apply 决策卡 → 批准 → 真 bind)
# ---------------------------------------------------------------------------


def test_bind_style_profile_effect_forwards_injection_config():
    """风格参考 apply 决策卡批准时,effect 应把 intensity/维度/include 落到 binding.config_json。"""
    from novel_system.services.review_effects import run_effect

    book_id = _seed_book("effectcfg", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("effectcfg", book_id)
    with SessionLocal() as session:
        result = run_effect(
            session,
            "proj_effectcfg",
            {
                "type": "bind_style_profile",
                "profile_id": profile_id,
                "scope": "project",
                "strategy": "mixed",
                "intensity": 35,
                "sub_dimensions": ["language.rhetoric", "scene.dialogue"],
                "include_metric": True,
            },
        )
        session.commit()
        binding = StyleReferenceRepository(session).get_binding(result["binding_id"])
        assert binding.scope == "project"
        assert binding.scope_ref_id == "proj_effectcfg"
        assert binding.strategy == "mixed"
        assert binding.config_json.get("intensity") == 35
        assert binding.config_json.get("sub_dimensions") == ["language.rhetoric", "scene.dialogue"]
        assert binding.config_json.get("include_metric") is True


def test_bind_style_profile_effect_scene_and_character_scope():
    """立项 A — apply 决策卡 scope=scene/character + scope_ref_id 落成对应 scope 的真 binding,
    且 resolve_active_binding(scene_id=...) 命中场景级绑定(scene > character > project 优先级)。"""
    from novel_system.services.review_effects import run_effect
    from novel_system.services.style_reference.injection import InjectionService

    book_id = _seed_book("scoperef", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("scoperef", book_id)
    with SessionLocal() as session:
        scene_res = run_effect(session, "proj_scoperef", {
            "type": "bind_style_profile", "profile_id": profile_id,
            "scope": "scene", "scope_ref_id": "ch08s1",
            "task_type": "scene_generation", "strategy": "C",
        })
        char_res = run_effect(session, "proj_scoperef", {
            "type": "bind_style_profile", "profile_id": profile_id,
            "scope": "character", "scope_ref_id": "scoperef_CHAR01",
            "task_type": "scene_generation", "strategy": "B",
        })
        session.commit()
        repo = StyleReferenceRepository(session)
        sb = repo.get_binding(scene_res["binding_id"])
        cb = repo.get_binding(char_res["binding_id"])
        assert sb.scope == "scene" and sb.scope_ref_id == "ch08s1"
        assert cb.scope == "character" and cb.scope_ref_id == "scoperef_CHAR01"
        # 注入选取:scene_id 命中场景级绑定(优先级最高)
        picked = InjectionService(session).resolve_active_binding(
            "proj_scoperef", "scene_generation",
            character_ids=["scoperef_CHAR01"], scene_id="ch08s1",
        )
        assert picked is not None
        assert picked.scope == "scene" and picked.scope_ref_id == "ch08s1"
        # 角色级单独命中:scene 不匹配时,character_ids 命中角色级绑定
        picked_char = InjectionService(session).resolve_active_binding(
            "proj_scoperef", "scene_generation",
            character_ids=["scoperef_CHAR01"], scene_id="other_scene",
        )
        assert picked_char is not None
        assert picked_char.scope == "character" and picked_char.scope_ref_id == "scoperef_CHAR01"


def test_bind_style_profile_effect_scene_requires_scope_ref_id():
    """立项 A — scene/character 级绑定缺 scope_ref_id 应拒绝(防静默回退 project_id 成脏数据)。"""
    from novel_system.services.review_effects import run_effect

    book_id = _seed_book("scoperefreq", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("scoperefreq", book_id)
    with SessionLocal() as session:
        with pytest.raises(DomainError) as exc:
            run_effect(session, "proj_scoperefreq", {
                "type": "bind_style_profile", "profile_id": profile_id, "scope": "scene",
            })
        assert exc.value.status_code == 400


def test_bind_style_profile_effect_without_config_stays_empty():
    """无 config 的简单 bind(synthesize 默认决策卡路径)config_json 保持空,零回归。"""
    from novel_system.services.review_effects import run_effect

    book_id = _seed_book("effectplain", cloud_policy="segments_only", with_paragraphs=False)
    profile_id = _seed_profile_for_book("effectplain", book_id)
    with SessionLocal() as session:
        result = run_effect(
            session,
            "proj_effectplain",
            {"type": "bind_style_profile", "profile_id": profile_id},
        )
        session.commit()
        binding = StyleReferenceRepository(session).get_binding(result["binding_id"])
        assert binding.config_json == {}
        assert binding.strategy == "mixed"
        assert binding.scope_ref_id == "proj_effectplain"


# ---------------------------------------------------------------------------
# 10. GET /books/{id}/runs（矩阵深层页定位最新 run）
# ---------------------------------------------------------------------------


def test_list_book_runs_newest_first_and_status_filter():
    book_id = _seed_book("listruns", cloud_policy="segments_only", with_paragraphs=False)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_run(run_id="sr_run_lr_1", book_id=book_id, status="done", phase="done")
        repo.create_run(run_id="sr_run_lr_2", book_id=book_id, status="running", phase="extract")
        session.commit()
    with _client() as client:
        runs = client.get(f"{PREFIX}/books/{book_id}/runs").json()["data"]["runs"]
        assert [r["run_id"] for r in runs] == ["sr_run_lr_2", "sr_run_lr_1"], "应按 created_at 倒序"
        done = client.get(f"{PREFIX}/books/{book_id}/runs?status=done").json()["data"]["runs"]
        assert [r["run_id"] for r in done] == ["sr_run_lr_1"]
