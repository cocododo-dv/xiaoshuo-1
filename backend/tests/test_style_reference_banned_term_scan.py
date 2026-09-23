"""生成期禁用词的字面检查（风格稿门用）。

2026-09-23 风格参考 v3（P5b）：旧校验层删除，字面检查从 ``validation/forbidden_local.py`` 搬到
``services/style_reference/banned_terms.py``；这里是原来那组单测（换成合成词）。
"""

from __future__ import annotations

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.banned_terms import (
    banned_term_hits,
    profile_banned_term_hits,
    profile_generation_banned_terms,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository


def _seed_profile_and_terms(profile_id: str, terms: list[tuple[str, str]]) -> None:
    """terms = [(term_text, scope), ...]"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=f"sr_book_{profile_id[-6:]}",
            title="x",
            source_kind="upload",
            cloud_policy="local_only",
            text_checksum=f"chk_{profile_id}",
            total_chars=10,
            status="ready",
            stats_json={},
        )
        repo.create_run(
            run_id=f"sr_run_{profile_id[-6:]}",
            book_id=f"sr_book_{profile_id[-6:]}",
            status="done",
            phase="done",
        )
        repo.create_profile(
            profile_id=profile_id,
            book_id=f"sr_book_{profile_id[-6:]}",
            run_id=f"sr_run_{profile_id[-6:]}",
            title="t",
            status="active",
            profile_json={},
            coverage_json={},
            source_finding_ids_json=[],
        )
        for i, (term, scope) in enumerate(terms):
            repo.create_banned_term(
                term_id=f"sr_term_{profile_id[-6:]}_{i}",
                profile_id=profile_id,
                term=term,
                replacement_hint=None,
                source="user",
                scope=scope,
            )
        session.commit()


def test_single_generation_term_hits() -> None:
    _seed_profile_and_terms("sr_profile_bts001", [("青桐镇", "generation")])
    with SessionLocal() as session:
        hits = profile_banned_term_hits("那年冬天他回到青桐镇，渡口还在。", "sr_profile_bts001", session)
    assert hits == [{"pattern_statement": "青桐镇", "matched_excerpt": "青桐镇", "severity": "error"}]


def test_extraction_scope_terms_never_hit_generated_text() -> None:
    """scope=extraction 只管学习时的样本，不进生成期检查。"""
    _seed_profile_and_terms("sr_profile_bts002", [("青桐镇", "extraction")])
    with SessionLocal() as session:
        assert profile_generation_banned_terms(session, "sr_profile_bts002") == []
        assert profile_banned_term_hits("他回到青桐镇。", "sr_profile_bts002", session) == []


def test_multiple_terms_are_reported_once_each() -> None:
    _seed_profile_and_terms(
        "sr_profile_bts003",
        [("青桐镇", "generation"), ("沈知秋", "generation"), ("渡鸦会", "generation")],
    )
    with SessionLocal() as session:
        hits = profile_banned_term_hits("沈知秋在青桐镇见过渡鸦会的人，青桐镇的雨一直没停。", "sr_profile_bts003", session)
    assert {hit["pattern_statement"] for hit in hits} == {"青桐镇", "沈知秋", "渡鸦会"}
    assert len(hits) == 3


def test_empty_term_table_and_empty_text() -> None:
    _seed_profile_and_terms("sr_profile_bts004", [])
    with SessionLocal() as session:
        assert profile_banned_term_hits("任何文本", "sr_profile_bts004", session) == []
        assert profile_banned_term_hits("", "sr_profile_bts004", session) == []


def test_pure_scan_dedupes_and_skips_blank_terms() -> None:
    assert banned_term_hits("雾里的灯塔。灯塔没亮。", ["灯塔", "灯塔", " ", "", None, "码头"]) == [
        {"pattern_statement": "灯塔", "matched_excerpt": "灯塔", "severity": "error"}
    ]
    assert banned_term_hits("", ["灯塔"]) == []
