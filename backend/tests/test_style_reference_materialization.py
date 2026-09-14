"""MaterializationService 单测(PR-4)。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import ReviewItem
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.ingest import IngestService
from novel_system.services.style_reference.materialization import (
    MaterializationService,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import BindingScope


SAMPLE_TEXT = """这是叙述。

他说:"行。"

我心里想着事。

记得那年。

雪飘落。
"""


def _seed_profile_with_findings(seed: str) -> str:
    """建一个完整链路 book + run + extraction + 4 类 findings + profile。"""
    with SessionLocal() as session:
        ingest = IngestService(session, llm_enabled=False)
        result = ingest.ingest_upload(
            raw_bytes=SAMPLE_TEXT.encode("utf-8"),
            file_name=f"b_{seed}.txt",
            title="t",
            author_label="a",
            cloud_policy="segments_only",
            rights_declaration={"analysis_rights": True, "send_rights": True},
        )
        book_id = result.book.book_id
        repo = StyleReferenceRepository(session)
        run_id = f"sr_run_{seed}"
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        extraction_id = f"sr_ext_{seed}"
        repo.create_extraction(
            extraction_id=extraction_id,
            book_id=book_id,
            run_id=run_id,
            layer="language",
            sub_dimension="language.rhetoric",
            raw_payload_json={},
            status="done",
            validation_errors_json=[],
            purpose="extract",
        )
        # 4 类 findings 各 1 条
        findings_spec = [
            ("language.rhetoric", "observation", "鲁迅善用反讽"),
            ("narrative.pacing", "observation", "对话推动节奏"),
            ("language.rhetoric", "forbidden_pattern", "禁堆华丽形容词"),
            ("scene.dialogue", "observation", "对话简洁直率"),
        ]
        finding_ids = []
        for i, (sub_dim, kind, statement) in enumerate(findings_spec):
            fid = f"sr_find_{seed}_{i}"
            repo.create_finding(
                finding_id=fid,
                book_id=book_id,
                run_id=run_id,
                extraction_id=extraction_id,
                sub_dimension=sub_dim,
                finding_kind=kind,
                statement=statement,
                confidence="high",
                status="pending",
            )
            finding_ids.append(fid)
        # profile,含 calibration_guidance 2 条
        profile = repo.create_profile(
            profile_id=f"sr_profile_{seed}",
            book_id=book_id,
            run_id=run_id,
            title="t",
            status="draft",
            profile_json={
                "narrative_summary": "ns",
                "calibration_guidance": ["calib line A", "calib line B"],
            },
            coverage_json={},
            source_finding_ids_json=finding_ids,
        )
        session.commit()
        return profile.profile_id


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_apply_builds_rag_index_and_writes_no_review_items() -> None:
    """2026-09-14 减法:apply 只落 binding + 激活 + RAG 索引,不再物化 ReviewItem。"""
    profile_id = _seed_profile_with_findings("dispatch")
    with SessionLocal() as session:
        svc = MaterializationService(session)
        result = svc.apply_profile(
            profile_id, scope=BindingScope.PROJECT, scope_ref_id="proj_x"
        )
        session.commit()

    assert result.rag_index["signature_version"].startswith(
        "zh_content_restrained_style_signature_"
    )
    assert result.rag_index["status"] in {"ready", "rebuilt"}
    assert not hasattr(result, "review_ids")
    with SessionLocal() as session:
        reviews = list(
            session.execute(
                select(ReviewItem).where(ReviewItem.review_id.like("review_style_ref_%"))
            )
            .scalars()
            .all()
        )
    assert reviews == []



def test_apply_activates_profile_for_injection() -> None:
    """Q1 回归：apply 即把 profile 置 active。

    此前 synthesize 产 DRAFT、apply 只建 active binding 却从不激活 profile 本身，
    而注入(InjectionService / scene_execution)硬要求 profile.status=='active'，
    导致真实流程(导入→抽取→合成→应用)后风格注入恒为空(no-op)。
    """
    profile_id = _seed_profile_with_findings("activate")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        assert repo.get_profile(profile_id).status == "draft"  # 合成态

    with SessionLocal() as session:
        svc = MaterializationService(session)
        svc.apply_profile(
            profile_id, scope=BindingScope.PROJECT, scope_ref_id="proj_activate"
        )
        session.commit()

    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        assert (
            repo.get_profile(profile_id).status == "active"
        )  # 修复后：apply 激活 → 注入可生效


def test_apply_creates_binding_row() -> None:
    profile_id = _seed_profile_with_findings("binding")
    with SessionLocal() as session:
        svc = MaterializationService(session)
        result = svc.apply_profile(
            profile_id, scope=BindingScope.SCENE, scope_ref_id="scene_99"
        )
        session.commit()
        repo = StyleReferenceRepository(session)
        bindings = repo.list_bindings(profile_id=profile_id)

    assert result.binding_id
    assert any(b.binding_id == result.binding_id for b in bindings)
    binding = next(b for b in bindings if b.binding_id == result.binding_id)
    assert binding.scope == "scene"
    assert binding.scope_ref_id == "scene_99"
    assert binding.status == "active"
    assert binding.strategy == "mixed"


def test_apply_idempotent_same_inputs_reuses_binding() -> None:
    profile_id = _seed_profile_with_findings("idem")
    with SessionLocal() as session:
        svc = MaterializationService(session)
        r1 = svc.apply_profile(
            profile_id, scope=BindingScope.PROJECT, scope_ref_id="proj_z"
        )
        session.commit()
    with SessionLocal() as session:
        svc = MaterializationService(session)
        r2 = svc.apply_profile(
            profile_id, scope=BindingScope.PROJECT, scope_ref_id="proj_z"
        )
        session.commit()
    # 同 profile+scope 复用 binding
    assert r1.binding_id == r2.binding_id



def test_apply_profile_not_found() -> None:
    from novel_system.services.errors import DomainError

    with SessionLocal() as session:
        svc = MaterializationService(session)
        with pytest.raises(DomainError) as exc_info:
            svc.apply_profile(
                "sr_profile_nonexistent",
                scope=BindingScope.PROJECT,
                scope_ref_id="proj_x",
            )
        assert exc_info.value.code == "STYLE_REFERENCE_PROFILE_NOT_FOUND"


def test_apply_rejects_profile_invalidated_by_source_finding_change() -> None:
    from novel_system.services.errors import DomainError

    profile_id = _seed_profile_with_findings("stale")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        profile = repo.get_profile(profile_id)
        profile.coverage_json = {
            **(profile.coverage_json or {}),
            "stale": True,
            "stale_reason": "source_finding_membership_changed",
        }
        session.commit()

    with SessionLocal() as session:
        with pytest.raises(DomainError) as exc_info:
            MaterializationService(session).apply_profile(
                profile_id,
                scope=BindingScope.PROJECT,
                scope_ref_id="proj_stale",
            )
        assert exc_info.value.code == "STYLE_REFERENCE_PROFILE_STALE"


def test_apply_rejects_archived_profile_instead_of_creating_dead_binding() -> None:
    from novel_system.services.errors import DomainError

    profile_id = _seed_profile_with_findings("archived")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.get_profile(profile_id).status = "archived"
        session.commit()

    with SessionLocal() as session:
        with pytest.raises(DomainError) as exc_info:
            MaterializationService(session).apply_profile(
                profile_id,
                scope=BindingScope.PROJECT,
                scope_ref_id="proj_archived",
            )
        assert exc_info.value.code == "STYLE_REFERENCE_PROFILE_ARCHIVED"


def test_apply_profile_requires_scope_ref_id() -> None:
    """scope_ref_id 缺失的绑定永远解析不到(_binding_rank=99 死绑定),apply 必须拒绝。"""
    from novel_system.services.errors import DomainError

    with SessionLocal() as session:
        svc = MaterializationService(session)
        for bad_ref in (None, "", "  "):
            with pytest.raises(DomainError) as exc_info:
                svc.apply_profile(
                    "sr_profile_nonexistent",
                    scope=BindingScope.PROJECT,
                    scope_ref_id=bad_ref,
                )
            assert exc_info.value.code == "STYLE_REFERENCE_APPLY_PARAM_INVALID"
