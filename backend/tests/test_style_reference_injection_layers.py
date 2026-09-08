"""注入只读辅助端点:/injection/task-defaults + /injection/layers。

闭合前端「注入应用」页两处示意数据:任务卡片的默认策略/刷新周期、
「叠加注入层」卡片(此前为写死的 SR_LAYER_STACK)。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from novel_system.api.app import create_app
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.injection import (
    InjectionService,
    default_injection_strategy,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository

PREFIX = "/api/v2/style-reference"


def _seed_profile(seed: str) -> str:
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        book_id = f"sr_book_il_{seed}"
        repo.create_book(
            book_id=book_id,
            title="t",
            source_kind="upload",
            cloud_policy="segments_only",
            text_checksum=f"chk_il_{seed}",
            total_chars=1000,
            status="ready",
            stats_json={"rights_declaration": {
                "declared": True, "analysis_rights": True, "send_rights": True,
            }},
        )
        run_id = f"sr_run_il_{seed}"
        profile_id = f"sr_profile_il_{seed}"
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        repo.create_profile(
            profile_id=profile_id,
            book_id=book_id,
            run_id=run_id,
            title=f"画像{seed}",
            status="active",
            profile_json={
                "narrative_summary": "短句白描,逗号顿连。",
                "style_features": ["短句为主", "喻体即收"],
                "banned_replication_rules": ["禁止排比抒情"],
            },
            coverage_json={},
            source_finding_ids_json=[],
        )
        session.commit()
    return profile_id


def _bind(profile_id: str, *, binding_id: str, scope: str, scope_ref_id: str, strategy: str = "A") -> None:
    with SessionLocal() as session:
        StyleReferenceRepository(session).create_binding(
            binding_id=binding_id,
            profile_id=profile_id,
            scope=scope,
            scope_ref_id=scope_ref_id,
            task_type="scene_generation",
            strategy=strategy,
            config_json={},
            status="active",
        )
        session.commit()


# ---------------------------------------------------------------------------
# task-defaults
# ---------------------------------------------------------------------------


def test_task_defaults_endpoint_matches_node_registry() -> None:
    with TestClient(create_app()) as client:
        resp = client.get(f"{PREFIX}/injection/task-defaults")
        assert resp.status_code == 200
        tasks = {t["task_type"]: t for t in resp.json()["data"]["tasks"]}
    # long_form_continuation 生产路径已下线:不再对 UI 列出
    assert set(tasks) == {
        "project_init", "scene_generation", "fine_tuning", "key_chapter",
    }
    assert tasks["scene_generation"]["default_strategy"] == "mixed"
    assert tasks["key_chapter"]["default_strategy"] == "C"
    assert all(t["refresh_every_chars"] == 0 for t in tasks.values())
    # 持久层兼容:存量 binding 行的 task_type='long_form_continuation'
    # 仍必须能解析默认策略(枚举值保留,仅 UI 下线)
    assert default_injection_strategy("long_form_continuation") is not None


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------


def test_layers_endpoint_empty_when_no_bindings() -> None:
    with TestClient(create_app()) as client:
        resp = client.get(f"{PREFIX}/injection/layers", params={"project_id": "proj_il_none"})
        assert resp.status_code == 200
        data = resp.json()["data"]
    assert data["layers"] == []
    assert data["merged"] is None
    assert data["budget_total"] > 0


def test_layers_single_binding_gets_full_budget() -> None:
    profile_id = _seed_profile("single")
    _bind(profile_id, binding_id="sr_bind_il_single", scope="project", scope_ref_id="proj_il_s")
    with TestClient(create_app()) as client:
        resp = client.get(f"{PREFIX}/injection/layers", params={"project_id": "proj_il_s"})
        data = resp.json()["data"]
    assert len(data["layers"]) == 1
    layer = data["layers"][0]
    assert layer["scope"] == "project"
    assert layer["weight"] == 1
    assert layer["budget_chars"] == data["budget_total"]
    assert layer["profile_title"] == "画像single"
    assert data["merged"]["layer_count"] == 1
    assert data["merged"]["prefix_chars"] > 0


def test_layers_stacked_weights_and_order() -> None:
    """project + scene 双层(两个不同画像):由泛到具体,scene 层权重/预算更大,合并概要一致。"""
    base_profile = _seed_profile("stack")
    scene_profile = _seed_profile("stack_scene")
    _bind(base_profile, binding_id="sr_bind_il_p", scope="project", scope_ref_id="proj_il_x")
    _bind(scene_profile, binding_id="sr_bind_il_sc", scope="scene", scope_ref_id="scene_il_1", strategy="mixed")
    with TestClient(create_app()) as client:
        resp = client.get(
            f"{PREFIX}/injection/layers",
            params={"project_id": "proj_il_x", "scene_id": "scene_il_1"},
        )
        data = resp.json()["data"]
    assert [l["scope"] for l in data["layers"]] == ["project", "scene"]
    weights = [l["weight"] for l in data["layers"]]
    assert weights == [1, 2]
    total = data["budget_total"]
    # v2 §1.4:两层总额 = total(intensity 50)=1650 × (1 + 0.35) = 2228
    assert total == 2228
    assert data["layers"][0]["budget_chars"] == total * 1 // 3
    assert data["layers"][1]["budget_chars"] == total * 2 // 3
    assert data["layers"][1]["rank"] < data["layers"][0]["rank"]  # scene 更具体
    assert data["merged"]["layer_count"] == 2
    assert data["deduplicated"] == []
    # 合并 strategy 取最具体层
    assert data["merged"]["strategy"] == "mixed"
    assert all(l["fragment_count"] >= 1 for l in data["layers"])
    assert all("voice_block" in l["block_chars"] for l in data["layers"])


def test_layers_same_profile_across_scopes_is_deduplicated() -> None:
    """v2:同一画像绑到 project 与 scene 时只渲染一次(保留最具体层),被去重的 binding 列出。"""
    profile_id = _seed_profile("dedupe")
    _bind(profile_id, binding_id="sr_bind_il_dd_p", scope="project", scope_ref_id="proj_il_dd")
    _bind(profile_id, binding_id="sr_bind_il_dd_sc", scope="scene", scope_ref_id="scene_il_dd", strategy="mixed")
    with TestClient(create_app()) as client:
        resp = client.get(
            f"{PREFIX}/injection/layers",
            params={"project_id": "proj_il_dd", "scene_id": "scene_il_dd"},
        )
        data = resp.json()["data"]
    assert [l["scope"] for l in data["layers"]] == ["scene"]
    assert data["layers"][0]["weight"] == 1
    assert data["layers"][0]["budget_chars"] == data["budget_total"] == 1650
    assert [d["binding_id"] for d in data["deduplicated"]] == ["sr_bind_il_dd_p"]
    assert data["merged"]["layer_count"] == 1
    assert data["merged"]["strategy"] == "mixed"
    with SessionLocal() as session:
        fragments = InjectionService(session).fragments_for(
            "proj_il_dd", "scene_generation", scene_id="scene_il_dd"
        )
    # 单次渲染:正向块标题唯一、同一条特征只出现一次
    assert fragments.positive_block.count("[正向风格特征]") == 1
    assert fragments.positive_block.count("短句为主") == 1


def test_describe_layers_is_read_only() -> None:
    """describe 不写 metric 事件(可随 UI 反复调用)。"""
    profile_id = _seed_profile("ro")
    _bind(profile_id, binding_id="sr_bind_il_ro", scope="project", scope_ref_id="proj_il_ro")
    with SessionLocal() as session:
        from novel_system.db.models import StyleReferenceMetricEvent

        before = session.query(StyleReferenceMetricEvent).count()
        InjectionService(session).describe_binding_layers("proj_il_ro", "scene_generation")
        after = session.query(StyleReferenceMetricEvent).count()
    assert after == before
