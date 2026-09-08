"""RunOrchestrator 单测:LLMRequiredError / book_not_found / 落 4 表行数(PR-3)。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from novel_system.db.models import (
    StyleReferenceEvidence,
    StyleReferenceExtraction,
    StyleReferenceFinding,
    StyleReferenceQuote,
)
from novel_system.db.session import SessionLocal
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.dimensions import Layer
from novel_system.services.style_reference.errors import LLMRequiredError
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.run_orchestrator import RunOrchestrator
from novel_system.services.style_reference.repository import StyleReferenceRepository


SAMPLE_TEXT = """这是叙述段落 0,介绍清晨场景与人物心情,长度足够触发分段。

他说:"我们走吧。"

我心里想着家中的事。

记得那年的春天。

雪花从天空飘落。
"""


def _ingest(book_seed: str = "s", *, cloud_policy: str = "segments_only") -> str:
    """默认 segments_only(允许云端抽取);local_only 的拒绝行为有专门用例。"""
    with SessionLocal() as session:
        service = IngestService(session, llm_enabled=False)
        result = service.ingest_upload(
            raw_bytes=SAMPLE_TEXT.encode("utf-8"),
            file_name=f"book_{book_seed}.txt",
            title="t",
            author_label="t",
            cloud_policy=cloud_policy,
            rights_declaration=(
                {"analysis_rights": True, "send_rights": True}
                if cloud_policy != "local_only"
                else None
            ),
        )
        session.commit()
        return result.book.book_id


# ---------------------------------------------------------------------------
# LLM 不可用
# ---------------------------------------------------------------------------


def test_run_orchestrator_raises_llm_required_when_disabled() -> None:
    book_id = _ingest("disabled")
    with SessionLocal() as session:
        orch = RunOrchestrator(session, llm_client=None, llm_enabled=False)
        with pytest.raises(LLMRequiredError):
            orch.start_extract_run(book_id)


def test_run_orchestrator_book_not_found(fake_extractor_llm) -> None:
    with SessionLocal() as session:
        orch = RunOrchestrator(
            session, llm_client=fake_extractor_llm("default"), llm_enabled=True
        )
        with pytest.raises(DomainError) as exc_info:
            orch.start_extract_run("sr_book_nonexistent")
        assert exc_info.value.code == "STYLE_REFERENCE_BOOK_NOT_FOUND"


# PR-6:scene + theme 已落地,test_run_orchestrator_layer_not_supported 被移除;
# 错误码 STYLE_REFERENCE_LAYER_NOT_SUPPORTED 保留为后续兜底,但 Phase 2 4 layer
# 全支持后该路径在生产中不会触发。


# ---------------------------------------------------------------------------
# happy path:16 sub_dim × default → 64 findings,落 4 表(PR-6 全盘)
# ---------------------------------------------------------------------------


def test_run_orchestrator_happy_path_writes_4_tables(fake_extractor_llm) -> None:
    book_id = _ingest("happy")
    with SessionLocal() as session:
        orch = RunOrchestrator(
            session, llm_client=fake_extractor_llm("default"), llm_enabled=True
        )
        # SAMPLE_TEXT 仅百余字(assessment 全 skip),force 绕过输入量门槛,
        # 本用例锁定的是 16 sub_dim 落 4 表
        result = orch.start_extract_run(book_id, force=True)
        session.commit()

    assert result.status == "done"
    assert set(result.layers) == {"language", "narrative", "scene", "theme"}
    assert len(result.sub_dim_results) == 16

    # 4 表行数断言
    with SessionLocal() as session:
        ext_count = session.scalar(
            select(func.count()).select_from(StyleReferenceExtraction)
        )
        find_count = session.scalar(
            select(func.count()).select_from(StyleReferenceFinding)
        )
        quote_count = session.scalar(
            select(func.count()).select_from(StyleReferenceQuote)
        )
        ev_count = session.scalar(
            select(func.count()).select_from(StyleReferenceEvidence)
        )

    # 16 sub_dim × 1 EXTRACT 行 = 16
    assert ext_count == 16
    # 16 sub_dim × (3 obs + 1 forbid) = 64
    assert find_count == 64
    # 64 finding × 2 evidence = 128 evidence/quote
    assert ev_count == 128
    assert quote_count == 128


def test_extractor_checkpoint_called_per_sub_dim(monkeypatch) -> None:
    """后台模式的事务边界:每个 sub_dim 落库后 checkpoint(commit),
    写事务不得跨分钟级 LLM 调用持有 SQLite 写锁(database is busy 回归)。"""
    from novel_system.services.style_reference.extractors.base import (
        BaseExtractor,
        ExtractionRunResult,
    )
    from novel_system.services.style_reference.extractors.language import LanguageExtractor

    calls = {"checkpoint": 0, "extract": 0}

    def fake_extract(self, sub_dim):
        calls["extract"] += 1
        return ExtractionRunResult(sub_dimension=sub_dim)

    monkeypatch.setattr(BaseExtractor, "_extract_with_retry", fake_extract)
    monkeypatch.setattr(BaseExtractor, "__init__", lambda self, **kw: None)
    extractor = LanguageExtractor.__new__(LanguageExtractor)
    extractor._checkpoint = lambda: calls.__setitem__("checkpoint", calls["checkpoint"] + 1)
    results = extractor.extract_all_sub_dimensions()
    assert len(results) == 4
    assert calls["extract"] == 4
    assert calls["checkpoint"] == 4

    # checkpoint=None(inline 模式)不触发
    extractor2 = LanguageExtractor.__new__(LanguageExtractor)
    extractor2._checkpoint = None
    assert len(extractor2.extract_all_sub_dimensions()) == 4


def test_start_run_rejects_when_book_has_active_run() -> None:
    """并发守卫:同书已有 RUNNING run 时再启动 → 409(连点重跑抽取的防呆)。"""
    import pytest as _pytest

    from novel_system.services.errors import DomainError
    from novel_system.services.style_reference.ingest import IngestService
    from novel_system.services.style_reference.repository import StyleReferenceRepository
    from novel_system.services.style_reference.run_orchestrator import RunOrchestrator

    with SessionLocal() as session:
        ingest = IngestService(session, llm_enabled=False)
        result = ingest.ingest_upload(
            raw_bytes=("第一段。\n\n第二段。\n\n第三段。" * 40).encode("utf-8"),
            file_name="active_guard.txt",
            title="并发守卫",
            author_label=None,
            cloud_policy="segments_only",
            rights_declaration={"analysis_rights": True, "send_rights": True},
        )
        book_id = result.book.book_id
        repo = StyleReferenceRepository(session)
        repo.create_run(run_id="sr_run_active_guard", book_id=book_id, status="running", phase="extract")
        session.commit()

        orch = RunOrchestrator(session, llm_client=object(), llm_enabled=True)
        with _pytest.raises(DomainError) as exc_info:
            orch.start_extract_run(book_id, background=True)
        assert exc_info.value.code == "STYLE_REFERENCE_RUN_ALREADY_ACTIVE"
        assert exc_info.value.status_code == 409


def test_resume_run_skips_finalized_subdimensions_and_finishes_remaining_three(
    fake_extractor_llm,
) -> None:
    book_id = _ingest("resume_partial")
    run_id = "sr_run_resume_partial"
    client = fake_extractor_llm("default")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_run(
            run_id=run_id,
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
                }
            },
        )
        repo.create_extraction(
            extraction_id="sr_ext_resume_completed",
            book_id=book_id,
            run_id=run_id,
            layer="language",
            sub_dimension="language.sentence_structure",
            raw_payload_json={"findings_count": 0},
            status="done",
            validation_errors_json=[],
            purpose="extract",
        )
        session.commit()

        result = RunOrchestrator(
            session,
            llm_client=client,
            llm_enabled=True,
        ).resume_extract_run(run_id)
        session.commit()

        run = repo.get_run(run_id)
        sentence_rows = repo.list_extractions(
            run_id=run_id,
            sub_dimension="language.sentence_structure",
        )

    assert result.status == "done"
    assert len(result.sub_dim_results) == 3
    assert client.call_count == 3
    assert len(sentence_rows) == 1
    assert run.coverage_json["progress"]["layers_done"] == 1
    assert set(run.coverage_json["sub_dimensions"]) == {
        "language.sentence_structure",
        "language.vocabulary",
        "language.rhetoric",
        "language.punctuation",
    }


# ---------------------------------------------------------------------------
# v2(风格模仿 v2 · W2):rng 以 sha256(text_checksum + run_id) 定种
# ---------------------------------------------------------------------------


def test_run_orchestrator_rng_seeded_from_checksum_and_run_id() -> None:
    import random

    from novel_system.services.style_reference.sampling import derive_extraction_rng

    book_id = _ingest("rng_seed")
    with SessionLocal() as session:
        book = StyleReferenceRepository(session).get_book(book_id)
        orch = RunOrchestrator(session, llm_client=object(), llm_enabled=True)
        again = RunOrchestrator(session, llm_client=object(), llm_enabled=True)
        expected = derive_extraction_rng(book.text_checksum, "sr_run_seed_a")
        assert (
            orch._run_rng("sr_run_seed_a", book_id).random()
            == again._run_rng("sr_run_seed_a", book_id).random()
            == expected.random()
        )
        assert (
            orch._run_rng("sr_run_seed_b", book_id).random()
            != derive_extraction_rng(book.text_checksum, "sr_run_seed_a").random()
        )
        injected = random.Random(3)
        assert RunOrchestrator(
            session, llm_client=object(), llm_enabled=True, rng=injected
        )._run_rng("sr_run_x", book_id) is injected
