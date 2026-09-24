"""风格参考 v3 · 绑定解析（inject/bindings.py）：scene > character（POV 在前）> project > global，只有最具体的一层生效。

取代旧 ``test_style_reference_injection.py`` 里的作用域 / 多层叠加用例：v3 起不再多层合并（J7——旧合并把样例 /
声音取自「最后一层」，而角色层是 POV 优先排的，最后一层恰恰是最不重要的配角）。
"""

from __future__ import annotations

from novel_system.db.session import SessionLocal
from novel_system.services.style_policy import style_policy_live
from novel_system.services.style_reference.inject.bindings import (
    most_specific_binding,
    ordered_character_ids,
    resolve_active_binding,
    resolve_binding_layers,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import build_style_runtime_contract


def _seed(seed: str, bindings: list[dict], *, profile_status: str = "active") -> None:
    """一本书 + 每条绑定一个画像（画像标题即绑定标签，便于断言命中哪一条）。"""
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=f"bd_book_{seed}", title="t", source_kind="upload", cloud_policy="local_only",
            text_checksum=f"bd_chk_{seed}", total_chars=10, status="ready", stats_json={},
        )
        repo.create_run(run_id=f"bd_run_{seed}", book_id=f"bd_book_{seed}", status="done", phase="done")
        for spec in bindings:
            label = spec["label"]
            status = spec.get("profile_status", profile_status)
            repo.create_profile(
                profile_id=f"bd_profile_{seed}_{label}", book_id=f"bd_book_{seed}", run_id=f"bd_run_{seed}",
                title=label, status=status, profile_json={"style_features": [label]},
                coverage_json={}, source_finding_ids_json=[],
            )
            kwargs = dict(
                binding_id=f"bd_bind_{seed}_{label}", profile_id=f"bd_profile_{seed}_{label}",
                scope=spec["scope"], scope_ref_id=spec.get("ref"), task_type="scene_generation",
                strategy="mixed", config_json={}, status=spec.get("status", "active"),
            )
            if spec.get("created_at"):
                kwargs["created_at"] = spec["created_at"]
            repo.create_binding(**kwargs)
        session.commit()


def _winner(project_id, *, character_ids=None, scene_id=None) -> str | None:
    with SessionLocal() as session:
        binding = resolve_active_binding(session, project_id, "scene_generation", character_ids=character_ids, scene_id=scene_id)
        layers = resolve_binding_layers(session, project_id, "scene_generation", character_ids=character_ids, scene_id=scene_id)
        applied = most_specific_binding(layers)
        # 单选与「整组命中层里的最具体一层」必须是同一条
        assert (binding.binding_id if binding else None) == (applied.binding_id if applied else None)
        return binding.binding_id.rsplit("_", 1)[-1] if binding else None


def test_ordered_character_ids_helper() -> None:
    assert ordered_character_ids("A", ["B", "C"]) == ["A", "B", "C"]
    assert ordered_character_ids("A", ["A", "B"]) == ["A", "B"]
    assert ordered_character_ids(None, ["B", "C"]) == ["B", "C"]
    assert ordered_character_ids("A", []) == ["A"]


def test_project_beats_global_and_disabled_or_inactive_bindings_are_skipped() -> None:
    _seed("prio", [{"label": "global", "scope": "global"}, {"label": "project", "scope": "project", "ref": "P1"}])
    assert _winner("P1") == "project"
    assert _winner("P_OTHER") == "global"
    _seed(
        "dis",
        [
            {"label": "project", "scope": "project", "ref": "P2"},
            {"label": "char", "scope": "character", "ref": "C2", "status": "disabled"},
            {"label": "scene", "scope": "scene", "ref": "S2", "profile_status": "draft"},
        ],
    )
    assert _winner("P2", character_ids=["C2"], scene_id="S2") == "project"


def test_scene_then_character_then_project() -> None:
    _seed(
        "levels",
        [
            {"label": "project", "scope": "project", "ref": "P3"},
            {"label": "char", "scope": "character", "ref": "C3"},
            {"label": "scene", "scope": "scene", "ref": "S3"},
        ],
    )
    assert _winner("P3", character_ids=["C3"], scene_id="S3") == "scene"
    assert _winner("P3", character_ids=["C3"], scene_id="S_OTHER") == "char"
    assert _winner("P3", character_ids=["C3"]) == "char"
    assert _winner("P3", character_ids=["C_OTHER"]) == "project"
    assert _winner("P3") == "project"


def test_pov_character_wins_over_other_onstage_characters_regardless_of_age() -> None:
    _seed(
        "pov",
        [
            {"label": "project", "scope": "project", "ref": "P4"},
            {"label": "pov", "scope": "character", "ref": "CA", "created_at": "2026-01-01T00:00:00Z"},
            {"label": "other", "scope": "character", "ref": "CB", "created_at": "2026-05-01T00:00:00Z"},
        ],
    )
    ids = ordered_character_ids("CA", ["CB"])
    assert _winner("P4", character_ids=ids) == "pov"
    with SessionLocal() as session:
        layers = resolve_binding_layers(session, "P4", "scene_generation", character_ids=ids)
    # 整组命中层由泛到具体、角色层 POV 在前——所以「最后一层」是配角，不能拿它当生效层（J7）
    assert [b.binding_id.rsplit("_", 1)[-1] for b in layers] == ["project", "pov", "other"]
    assert most_specific_binding(layers).binding_id.endswith("_pov")
    # POV 没有绑定：台上配角命中
    assert _winner("P4", character_ids=ordered_character_ids("C_NONE", ["CB"])) == "other"


def test_same_character_keeps_only_the_newest_binding() -> None:
    _seed(
        "dedup",
        [
            {"label": "old", "scope": "character", "ref": "CD", "created_at": "2026-01-01T00:00:00Z"},
            {"label": "new", "scope": "character", "ref": "CD", "created_at": "2026-05-01T00:00:00Z"},
        ],
    )
    with SessionLocal() as session:
        layers = resolve_binding_layers(session, None, "scene_generation", character_ids=["CD"])
    assert [b.binding_id.rsplit("_", 1)[-1] for b in layers] == ["new"]


def test_contract_and_live_policy_freeze_the_pov_layer(session) -> None:
    _seed(
        "freeze",
        [
            {"label": "project", "scope": "project", "ref": "P5"},
            {"label": "pov", "scope": "character", "ref": "CA"},
            {"label": "other", "scope": "character", "ref": "CB"},
        ],
    )
    layers = resolve_binding_layers(session, "P5", "scene_generation", character_ids=["CA", "CB"])
    contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
    assert contract["binding_ids"] == ["bd_bind_freeze_pov"] and contract["layer_count"] == 1

    class _Scope:
        project_id = "P5"
        scene_id = None
        pov_character_id = "CA"
        onstage_chars_json = ["CB"]

    policy = style_policy_live(session, _Scope())
    assert policy.bound and policy.binding_id == "bd_bind_freeze_pov" and policy.mode == "live"


