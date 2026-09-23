"""禁用词端到端契约:REST CRUD + 生成域禁用词进红线段。

补链路:此前 create_banned_term 无任何生产调用方——注入红线段的
{banned_terms_list} 永远为空,前端禁用词编辑器只有本地 state。
(2026-09-23 v3:抽取不再按禁用词过滤样本段——学习文风定稿时,作者录入的禁用词与受保护专名一起把文风卡里含这些词的
句子滤掉,见 test_style_reference_learn_job.py。)
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from novel_system.api.app import create_app
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.repository import StyleReferenceRepository
from tests.style_reference_factories import RIGHTS_STATS, make_book, make_profile

PREFIX = "/api/v2/style-reference"


def _seed_book_with_profile(seed: str, *, paragraphs: list[str] | None = None) -> tuple[str, str]:
    with SessionLocal() as session:
        book_id = make_book(
            session,
            f"sr_book_bt_{seed}",
            paragraphs=paragraphs or [],
            cloud_policy="segments_only",
            stats=RIGHTS_STATS,
            paragraph_id="sr_para_bt_" + seed + "_{index:02d}",
            total_chars=1000,
        )
        profile_id = make_profile(
            session,
            book_id,
            profile_id=f"sr_profile_bt_{seed}",
            run_id=f"sr_run_bt_{seed}",
            profile_json={"narrative_summary": "短句"},
        )
        session.commit()
    return book_id, profile_id


# ---------------------------------------------------------------------------
# REST CRUD
# ---------------------------------------------------------------------------


def test_banned_terms_crud_roundtrip() -> None:
    _, profile_id = _seed_book_with_profile("crud")
    with TestClient(create_app()) as client:
        # 空列表
        resp = client.get(f"{PREFIX}/profiles/{profile_id}/banned-terms")
        assert resp.status_code == 200
        assert resp.json()["data"]["terms"] == []

        # 创建 generation 域
        resp = client.post(
            f"{PREFIX}/profiles/{profile_id}/banned-terms",
            json={"term": "文笔优美", "replacement_hint": "改具体描写", "scope": "generation"},
            headers={"X-Idempotency-Key": "bt_c1"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["created"] is True
        term_id = data["term"]["term_id"]
        assert data["term"]["source"] == "user"

        # 同 (term, scope) 重复创建 → 幂等返回既有行
        resp = client.post(
            f"{PREFIX}/profiles/{profile_id}/banned-terms",
            json={"term": "文笔优美", "scope": "generation"},
            headers={"X-Idempotency-Key": "bt_c2"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["created"] is False
        assert resp.json()["data"]["term"]["term_id"] == term_id

        # extraction 域是独立条目
        resp = client.post(
            f"{PREFIX}/profiles/{profile_id}/banned-terms",
            json={"term": "潮汐之子", "scope": "extraction"},
            headers={"X-Idempotency-Key": "bt_c3"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["created"] is True

        # scope 过滤
        resp = client.get(f"{PREFIX}/profiles/{profile_id}/banned-terms?scope=generation")
        terms = resp.json()["data"]["terms"]
        assert [t["term"] for t in terms] == ["文笔优美"]

        # 删除
        resp = client.request(
            "DELETE",
            f"{PREFIX}/banned-terms/{term_id}",
            headers={"X-Idempotency-Key": "bt_d1"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is True

        resp = client.get(f"{PREFIX}/profiles/{profile_id}/banned-terms?scope=generation")
        assert resp.json()["data"]["terms"] == []


def test_banned_terms_validation_and_404() -> None:
    _, profile_id = _seed_book_with_profile("val")
    with TestClient(create_app()) as client:
        # profile 不存在
        resp = client.get(f"{PREFIX}/profiles/sr_profile_missing/banned-terms")
        assert resp.status_code == 404

        # 空 term
        resp = client.post(
            f"{PREFIX}/profiles/{profile_id}/banned-terms",
            json={"term": "   ", "scope": "generation"},
            headers={"X-Idempotency-Key": "bt_v1"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "STYLE_REFERENCE_BANNED_TERM_INVALID"

        # 非法 scope
        resp = client.post(
            f"{PREFIX}/profiles/{profile_id}/banned-terms",
            json={"term": "词", "scope": "everywhere"},
            headers={"X-Idempotency-Key": "bt_v2"},
        )
        assert resp.status_code == 400

        # 删除不存在
        resp = client.request(
            "DELETE",
            f"{PREFIX}/banned-terms/sr_term_missing",
            headers={"X-Idempotency-Key": "bt_v3"},
        )
        assert resp.status_code == 404


def test_preset_banned_term_cannot_be_deleted() -> None:
    _, profile_id = _seed_book_with_profile("preset")
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_banned_term(
            term_id="sr_term_bt_preset",
            profile_id=profile_id,
            term="震撼人心",
            replacement_hint=None,
            source="preset",
            scope="generation",
        )
        session.commit()
    with TestClient(create_app()) as client:
        resp = client.request(
            "DELETE",
            f"{PREFIX}/banned-terms/sr_term_bt_preset",
            headers={"X-Idempotency-Key": "bt_p1"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "STYLE_REFERENCE_BANNED_TERM_PROTECTED"


def test_generation_banned_term_reaches_injection_redline() -> None:
    """generation 域禁用词创建后必须实际出现在注入红线段(端到端消费)。"""
    _, profile_id = _seed_book_with_profile("inject")
    with TestClient(create_app()) as client:
        resp = client.post(
            f"{PREFIX}/profiles/{profile_id}/banned-terms",
            json={"term": "泪如雨下", "scope": "generation"},
            headers={"X-Idempotency-Key": "bt_i1"},
        )
        assert resp.status_code == 200
        resp = client.post(
            f"{PREFIX}/profiles/{profile_id}/injection-preview",
            json={"strategy": "A"},
        )
        assert resp.status_code == 200
        frags = resp.json()["data"]["fragments"]
        assert "泪如雨下" in frags["anti_plagiarism_block"]


# ---------------------------------------------------------------------------
# extraction 域:抽取采样过滤
# ---------------------------------------------------------------------------


