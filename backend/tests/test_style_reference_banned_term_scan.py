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
from tests.style_reference_factories import make_banned_terms, make_book, make_profile


def _seed_profile_and_terms(profile_id: str, terms: list[tuple[str, str]]) -> None:
    """terms = [(term_text, scope), ...]"""
    with SessionLocal() as session:
        book_id = make_book(session, f"sr_book_{profile_id[-6:]}", title="x", cloud_policy="local_only", total_chars=10)
        make_profile(session, book_id, profile_id=profile_id, run_id=f"sr_run_{profile_id[-6:]}")
        make_banned_terms(session, profile_id, terms, key=profile_id[-6:])
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
