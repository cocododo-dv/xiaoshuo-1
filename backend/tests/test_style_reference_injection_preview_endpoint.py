"""PR-9 §5.1 — GET /bindings/{id}/injection-preview + POST /profiles/{id}/injection-preview。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.repository import StyleReferenceRepository

PREFIX = "/api/v2/style-reference"


def _seed_profile_with_binding(
    *,
    seed: str,
    profile_status: str = "active",
    strategy: str = "A",
    config_json: dict | None = None,
) -> tuple[str, str]:
    book_id = f"sr_book_{seed}"
    run_id = f"sr_run_{seed}"
    profile_id = f"sr_profile_{seed}"
    binding_id = f"sr_bind_{seed}"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=book_id, title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum=f"chk_{seed}", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        repo.create_profile(
            profile_id=profile_id, book_id=book_id, run_id=run_id, title="t",
            status=profile_status,
            profile_json={
                "narrative_summary": "短句白话",
                "style_features": ["短句", "动词驱动"],
                "banned_replication_rules": ["禁堆砌"],
                "metrics_baseline": {
                    "paragraph_mean_chars": {"mean": 80.0, "std": 10.0}
                },
            },
            coverage_json={},
            source_finding_ids_json=[],
        )
        repo.create_binding(
            binding_id=binding_id, profile_id=profile_id,
            scope="project", scope_ref_id=f"proj_{seed}",
            task_type="scene_generation", strategy=strategy,
            config_json=config_json or {}, status="active",
        )
        session.commit()
    return binding_id, profile_id


def test_get_binding_preview_200_happy(client: TestClient) -> None:
    binding_id, _ = _seed_profile_with_binding(seed="gethappy", strategy="A")
    resp = client.get(f"{PREFIX}/bindings/{binding_id}/injection-preview")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "fragments" in data
    assert "prefix" in data
    assert data["fragments"]["strategy"] == "A"
    assert "短句" in data["fragments"]["positive_block"]
    assert data["prefix"].startswith("[STYLE_REFERENCE]\n")


def test_get_binding_preview_404(client: TestClient) -> None:
    resp = client.get(f"{PREFIX}/bindings/sr_bind_nonexistent/injection-preview")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "STYLE_REFERENCE_BINDING_NOT_FOUND"


def test_dryrun_preview_200(client: TestClient) -> None:
    _, profile_id = _seed_profile_with_binding(seed="dryrun")
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/injection-preview",
        json={
            "strategy": "mixed",
            "intensity": 70,
            "sub_dimensions": ["language.vocabulary"],
            "include_positive": True,
            "include_forbidden": True,
            "include_metric": False,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["fragments"]["strategy"] == "mixed"
    assert "短句" in data["fragments"]["positive_block"]
    # metric 关闭 → metric_anchor_block 空
    assert data["fragments"]["metric_anchor_block"] == ""


def test_dryrun_scene_default_uses_mixed_with_metric(client: TestClient) -> None:
    _, profile_id = _seed_profile_with_binding(seed="dryrun_default")
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/injection-preview",
        json={},
    )

    assert resp.status_code == 200, resp.text
    fragments = resp.json()["data"]["fragments"]
    assert fragments["strategy"] == "mixed"
    assert "段均字数" in fragments["metric_anchor_block"]


def test_dryrun_preview_422_invalid_intensity(client: TestClient) -> None:
    _, profile_id = _seed_profile_with_binding(seed="invalid")
    resp = client.post(
        f"{PREFIX}/profiles/{profile_id}/injection-preview",
        json={"strategy": "mixed", "intensity": 999},  # 超出 0-100
    )
    assert resp.status_code == 422


def test_dryrun_preview_404_profile(client: TestClient) -> None:
    resp = client.post(
        f"{PREFIX}/profiles/sr_profile_nonexistent/injection-preview",
        json={"strategy": "A"},
    )
    assert resp.status_code == 404


_STATS_KEYS = {
    "positive_lines", "forbidden_lines", "metric_lines", "voice_lines",
    "few_shot_windows", "few_shot_chars", "rag_snippets", "total_prefix_chars",
    "intensity_effective_total_chars", "few_shot_k",
}


def test_dryrun_strategy_a_consumes_intensity_total(client: TestClient) -> None:
    """v2 §1.5:strategy=A 同样消费 intensity(抽象总额 900 → 2400),小画像装得下时正文相同。"""
    _, profile_id = _seed_profile_with_binding(seed="strata")
    resp_a = client.post(
        f"{PREFIX}/profiles/{profile_id}/injection-preview",
        json={"strategy": "A", "intensity": 0, "sub_dimensions": []},
    )
    resp_a2 = client.post(
        f"{PREFIX}/profiles/{profile_id}/injection-preview",
        json={"strategy": "A", "intensity": 100, "sub_dimensions": ["language.vocabulary"]},
    )
    assert resp_a.status_code == 200
    assert resp_a2.status_code == 200
    low, high = resp_a.json()["data"], resp_a2.json()["data"]
    # 小画像两档都装得下 → 正文一致;但总额读数不同,A 不带样例(few_shot_k=0)
    assert low["fragments"]["positive_block"] == high["fragments"]["positive_block"]
    assert low["stats"]["intensity_effective_total_chars"] == 900
    assert high["stats"]["intensity_effective_total_chars"] == 2400
    assert low["stats"]["few_shot_k"] == 0 and low["stats"]["few_shot_windows"] == 0
    assert set(low["stats"]) == _STATS_KEYS


def test_get_binding_preview_returns_real_stats(client: TestClient) -> None:
    """GET 端点也带 stats,行数 = 各块 `- ` 条目数,总字数 = prefix 长度。"""
    binding_id, _ = _seed_profile_with_binding(
        seed="getstats", strategy="mixed", config_json={"intensity": 80}
    )
    resp = client.get(f"{PREFIX}/bindings/{binding_id}/injection-preview")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    stats = data["stats"]
    assert set(stats) == _STATS_KEYS
    assert stats["positive_lines"] == 2  # 短句 / 动词驱动
    assert stats["forbidden_lines"] == 1  # 禁堆砌
    assert stats["metric_lines"] >= 1  # 段均字数软分布
    assert stats["voice_lines"] == 0  # 旧画像无 voice_signature
    assert stats["total_prefix_chars"] == len(data["prefix"])
    assert stats["intensity_effective_total_chars"] == 900 + (2400 - 900) * 80 // 100
    assert stats["few_shot_k"] == 9  # round(3 + 7 × 0.8) = round(8.6)(2026-09-12:k 3→10)
    assert stats["few_shot_windows"] == 0  # local_only 书:原文样例不出


def test_dryrun_mixed_empty_sub_dim_equals_all_selected(client: TestClient) -> None:
    """MIXED 时 sub_dimensions=[] 等同全选(_render_forbidden 兜底)。"""
    _, profile_id = _seed_profile_with_binding(seed="empty_subdim")
    resp_empty = client.post(
        f"{PREFIX}/profiles/{profile_id}/injection-preview",
        json={"strategy": "mixed", "intensity": 50, "sub_dimensions": []},
    )
    assert resp_empty.status_code == 200
    # banned_replication_rules 应当在 forbidden_block 中(sub_dim=[] 不过滤)
    assert "禁堆砌" in resp_empty.json()["data"]["fragments"]["forbidden_block"]
