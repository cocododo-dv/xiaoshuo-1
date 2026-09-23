"""Style Reference (v1.1) repository CRUD 单元测试。

依赖 backend/tests/conftest.py 的 isolated_database fixture(autouse,
Base.metadata.create_all 已建好 11 张 style_reference_* 新表)。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.repository import StyleReferenceRepository


@pytest.fixture
def repo():
    with SessionLocal() as session:
        yield StyleReferenceRepository(session)


def _make_book(repo: StyleReferenceRepository, *, book_id: str = "sr_book_1") -> None:
    repo.create_book(
        book_id=book_id,
        title="鲁迅短篇集",
        author_label="鲁迅",
        source_kind="upload",
        cloud_policy="local_only",
        text_checksum=f"sha256_{book_id}",
        total_chars=82000,
        status="ready",
        stats_json={"metrics": {"avg_sentence_length": {"mean": 18.4, "std": 12.1}}},
    )


def test_book_create_get_list_delete(repo: StyleReferenceRepository) -> None:
    _make_book(repo)
    fetched = repo.get_book("sr_book_1")
    assert fetched is not None
    assert fetched.title == "鲁迅短篇集"
    assert fetched.stats_json["metrics"]["avg_sentence_length"]["mean"] == 18.4

    listed = repo.list_books(status="ready")
    assert len(listed) == 1

    deleted = repo.delete_book("sr_book_1")
    assert deleted == 1
    assert repo.get_book("sr_book_1") is None


def test_paragraph_list_filters_and_order(repo: StyleReferenceRepository) -> None:
    _make_book(repo)
    for idx in (2, 0, 1):
        repo.create_paragraph(
            paragraph_id=f"sr_para_{idx}",
            book_id="sr_book_1",
            paragraph_index=idx,
            paragraph_type="dialogue" if idx == 0 else "narration",
            start_offset=idx * 100,
            end_offset=idx * 100 + 80,
            text=f"段落 {idx}",
            char_count=80,
            classifier_confidence=0.92,
        )
    paragraphs = repo.list_paragraphs("sr_book_1")
    assert [p.paragraph_index for p in paragraphs] == [0, 1, 2]
    dialogues = repo.list_paragraphs("sr_book_1", paragraph_type="dialogue")
    assert len(dialogues) == 1
    assert dialogues[0].paragraph_id == "sr_para_0"


def test_run_extraction_finding_chain_and_unique_constraint(
    repo: StyleReferenceRepository,
) -> None:
    _make_book(repo)
    repo.create_run(
        run_id="sr_run_1",
        book_id="sr_book_1",
        status="running",
        phase="extract",
    )
    repo.create_extraction(
        extraction_id="sr_ext_1",
        book_id="sr_book_1",
        run_id="sr_run_1",
        layer="language",
        sub_dimension="language.rhetoric",
        raw_payload_json={"observations": []},
        status="done",
        validation_errors_json=[],
        purpose="extract",
    )
    repo.create_finding(
        finding_id="sr_find_1",
        book_id="sr_book_1",
        run_id="sr_run_1",
        extraction_id="sr_ext_1",
        sub_dimension="language.rhetoric",
        finding_kind="observation",
        statement="鲁迅在 rhetoric 上偏好白描",
        confidence="high",
        status="pending",
    )
    # commit so the next IntegrityError + rollback only undoes the duplicate insert
    repo.session.commit()

    # PR-3 hotfix 0038:UNIQUE(extraction_id, sub_dim, kind, statement_hash) — 同
    # statement_hash 才拒(相同 statement 重复)
    with pytest.raises(IntegrityError):
        with repo.session.begin_nested():
            repo.create_finding(
                finding_id="sr_find_dup",
                book_id="sr_book_1",
                run_id="sr_run_1",
                extraction_id="sr_ext_1",
                sub_dimension="language.rhetoric",
                finding_kind="observation",
                statement="鲁迅在 rhetoric 上偏好白描",  # 与 sr_find_1 完全一致 → hash 相同
                confidence="medium",
                status="pending",
            )

    # 同 sub_dim 同 kind 但 statement 不同 → 应允许并存(PR-3 hotfix 行为)
    repo.create_finding(
        finding_id="sr_find_3",
        book_id="sr_book_1",
        run_id="sr_run_1",
        extraction_id="sr_ext_1",
        sub_dimension="language.rhetoric",
        finding_kind="observation",
        statement="鲁迅善用反讽性短句",  # 与 sr_find_1 不同 → hash 不同
        confidence="high",
        status="pending",
    )

    # 不同 kind 可并存(observation + forbidden_pattern)
    repo.create_finding(
        finding_id="sr_find_2",
        book_id="sr_book_1",
        run_id="sr_run_1",
        extraction_id="sr_ext_1",
        sub_dimension="language.rhetoric",
        finding_kind="forbidden_pattern",
        statement="鲁迅从不堆砌华丽形容词",
        confidence="high",
        status="pending",
    )
    findings = repo.list_findings(book_id="sr_book_1", sub_dimension="language.rhetoric")
    assert {f.finding_kind for f in findings} == {"observation", "forbidden_pattern"}
    # 3 条 finding:2 个 observation(不同 statement) + 1 个 forbidden_pattern
    assert len(findings) == 3
    # statement_hash 自动填且与 statement 一致
    for f in findings:
        assert f.statement_hash, "statement_hash 必须由 repository 自动填充"


def test_quote_paragraph_id_nullable_for_counter_example(
    repo: StyleReferenceRepository,
) -> None:
    _make_book(repo)
    # 合成 quote(counter_example),paragraph_id 为空
    repo.create_quote(
        quote_id="sr_quote_synth",
        book_id="sr_book_1",
        paragraph_id=None,
        span_start=0,
        span_end=20,
        quote_text="（合成反例)",
        illustrates_dims=["language.rhetoric"],
        extracted_features={"is_synthetic": True},
    )
    quotes = repo.list_quotes("sr_book_1")
    assert len(quotes) == 1
    assert quotes[0].paragraph_id is None


def test_evidence_unique_finding_quote(repo: StyleReferenceRepository) -> None:
    _make_book(repo)
    repo.create_run(run_id="sr_run_1", book_id="sr_book_1", status="running", phase="extract")
    repo.create_extraction(
        extraction_id="sr_ext_1",
        book_id="sr_book_1",
        run_id="sr_run_1",
        layer="language",
        sub_dimension="language.rhetoric",
        raw_payload_json={},
        status="done",
        validation_errors_json=[],
        purpose="extract",
    )
    repo.create_finding(
        finding_id="sr_find_1",
        book_id="sr_book_1",
        run_id="sr_run_1",
        extraction_id="sr_ext_1",
        sub_dimension="language.rhetoric",
        finding_kind="observation",
        statement="...",
        confidence="medium",
        status="pending",
    )
    repo.create_quote(
        quote_id="sr_quote_1",
        book_id="sr_book_1",
        paragraph_id=None,
        span_start=0,
        span_end=10,
        quote_text="quote",
        illustrates_dims=[],
        extracted_features={},
    )
    repo.create_evidence(
        evidence_id="sr_ev_1",
        finding_id="sr_find_1",
        quote_id="sr_quote_1",
        anchor_kind="paragraph_quote",
        is_synthetic=0,
    )
    repo.session.commit()
    with pytest.raises(IntegrityError):
        with repo.session.begin_nested():
            repo.create_evidence(
                evidence_id="sr_ev_dup",
                finding_id="sr_find_1",
                quote_id="sr_quote_1",
                anchor_kind="paragraph_quote",
                is_synthetic=0,
            )


def test_profile_binding_banned_terms(
    repo: StyleReferenceRepository,
) -> None:
    _make_book(repo)
    repo.create_run(run_id="sr_run_1", book_id="sr_book_1", status="done", phase="done")
    repo.create_profile(
        profile_id="sr_profile_1",
        book_id="sr_book_1",
        run_id="sr_run_1",
        title="鲁迅风格 v1",
        status="active",
        profile_json={"narrative_summary": "白描"},
        coverage_json={},
        source_finding_ids_json=[],
        version_tag="v1.0",
    )
    repo.update_profile("sr_profile_1", status="archived")
    assert repo.get_profile("sr_profile_1").status == "archived"

    repo.create_binding(
        binding_id="sr_bind_1",
        profile_id="sr_profile_1",
        scope="project",
        scope_ref_id="proj_42",
        task_type="scene_generation",
        strategy="A",
        config_json={"style_intensity": 0.8},
        status="active",
    )
    bindings = repo.list_bindings(profile_id="sr_profile_1", task_type="scene_generation")
    assert len(bindings) == 1

    repo.create_banned_term(
        term_id="sr_term_1",
        profile_id="sr_profile_1",
        term="文笔优美",
        replacement_hint="改用具体动作",
        source="user",
        scope="generation",
    )
    repo.session.commit()
    with pytest.raises(IntegrityError):
        with repo.session.begin_nested():
            repo.create_banned_term(
                term_id="sr_term_dup",
                profile_id="sr_profile_1",
                term="文笔优美",
                replacement_hint=None,
                source="user",
                scope="generation",
            )
    # 同 term 不同 scope 可并存
    repo.create_banned_term(
        term_id="sr_term_2",
        profile_id="sr_profile_1",
        term="文笔优美",
        replacement_hint=None,
        source="user",
        scope="extraction",
    )
    assert len(repo.list_banned_terms("sr_profile_1")) == 2
