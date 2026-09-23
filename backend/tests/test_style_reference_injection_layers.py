"""注入只读辅助端点:/injection/layers(2026-09-23 风格参考 v3;旧 /injection/task-defaults 已删,P6a)。

v3:只有最具体的一层生效(``applied``),其余命中层被遮住(``deduplicated``);层查询只查列、不渲染(U10);
旧策略列一律 ``mixed``。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from novel_system.api.app import create_app
from novel_system.db.session import SessionLocal
from tests.style_reference_factories import RIGHTS_STATS, make_binding, make_book, make_profile

PREFIX = "/api/v2/style-reference"


def _seed_profile(seed: str, *, cloud_policy: str = "allow_full_cloud") -> str:
    with SessionLocal() as session:
        book_id = make_book(session, f"sr_book_il_{seed}", cloud_policy=cloud_policy, stats=RIGHTS_STATS, total_chars=1000)
        profile_id = make_profile(
            session,
            book_id,
            profile_id=f"sr_profile_il_{seed}",
            run_id=f"sr_run_il_{seed}",
            title=f"画像{seed}",
            profile_json={"style_features": ["短句为主", "喻体即收"], "banned_replication_rules": ["禁止排比抒情"]},
        )
        session.commit()
    return profile_id


def _bind(profile_id: str, *, binding_id: str, scope: str, scope_ref_id: str, strategy: str = "mixed", config_json: dict | None = None) -> None:
    with SessionLocal() as session:
        make_binding(
            session,
            profile_id,
            binding_id=binding_id,
            scope=scope,
            scope_ref_id=scope_ref_id,
            strategy=strategy,
            config_json=config_json,
        )
        session.commit()


def test_task_defaults_endpoint_is_gone() -> None:
    """旧任务默认策略表(四种策略 / 刷新周期)没有消费方了:端点删除(台账 U16)。"""
    with TestClient(create_app()) as client:
        assert client.get(f"{PREFIX}/injection/task-defaults").status_code == 404


def test_layers_endpoint_empty_when_no_bindings() -> None:
    with TestClient(create_app()) as client:
        resp = client.get(f"{PREFIX}/injection/layers", params={"project_id": "proj_il_none"})
        assert resp.status_code == 200
        data = resp.json()["data"]
    assert data["layers"] == [] and data["merged"] is None and data["deduplicated"] == []


def test_layers_single_binding_is_applied_with_its_v3_config() -> None:
    profile_id = _seed_profile("single")
    _bind(profile_id, binding_id="sr_bind_il_single", scope="project", scope_ref_id="proj_il_s", config_json={"intensity": 50})
    with TestClient(create_app()) as client:
        data = client.get(f"{PREFIX}/injection/layers", params={"project_id": "proj_il_s"}).json()["data"]
    assert len(data["layers"]) == 1
    layer = data["layers"][0]
    assert layer["scope"] == "project" and layer["applied"] is True and layer["weight"] == 1
    assert layer["profile_title"] == "画像single" and layer["profile_status"] == "active"
    # 旧强度 50 → 8 窗(与旧 k(i) 同一公式)
    assert layer["sample_windows"] == 8 and layer["reference_mode"] == "full"
    assert data["merged"]["layer_count"] == 1 and data["merged"]["binding_id"] == "sr_bind_il_single"


def test_only_the_most_specific_layer_is_applied() -> None:
    base_profile = _seed_profile("stack")
    scene_profile = _seed_profile("stack_scene", cloud_policy="segments_only")
    _bind(base_profile, binding_id="sr_bind_il_p", scope="project", scope_ref_id="proj_il_x")
    _bind(scene_profile, binding_id="sr_bind_il_sc", scope="scene", scope_ref_id="scene_il_1", config_json={"sample_windows": 6})
    with TestClient(create_app()) as client:
        data = client.get(
            f"{PREFIX}/injection/layers", params={"project_id": "proj_il_x", "scene_id": "scene_il_1"}
        ).json()["data"]
    assert [layer["scope"] for layer in data["layers"]] == ["project", "scene"]
    assert [layer["applied"] for layer in data["layers"]] == [False, True]
    assert data["layers"][1]["rank"] < data["layers"][0]["rank"]  # scene 更具体
    assert [d["binding_id"] for d in data["deduplicated"]] == ["sr_bind_il_p"]
    merged = data["merged"]
    # segments_only 的书只送文风卡(不送窗口)——生效层的参考方式说到做到
    assert merged["binding_id"] == "sr_bind_il_sc" and merged["reference_mode"] == "card_only"
    assert merged["sample_windows"] == 6 and merged["prefix_chars"] == 0


def test_describe_layers_is_read_only() -> None:
    """describe 不写任何行(可随 UI 反复调用)。"""
    profile_id = _seed_profile("ro")
    _bind(profile_id, binding_id="sr_bind_il_ro", scope="project", scope_ref_id="proj_il_ro")
    with SessionLocal() as session:
        from novel_system.db.models import StyleReferenceMetricEvent, StyleReferenceSceneWindows, StyleReferenceWindow
        from novel_system.services.style_reference.inject.bindings import describe_binding_layers

        before = (
            session.query(StyleReferenceMetricEvent).count(),
            session.query(StyleReferenceSceneWindows).count(),
            session.query(StyleReferenceWindow).count(),
        )
        describe_binding_layers(session, "proj_il_ro", "scene_generation")
        after = (
            session.query(StyleReferenceMetricEvent).count(),
            session.query(StyleReferenceSceneWindows).count(),
            session.query(StyleReferenceWindow).count(),
        )
    assert after == before
