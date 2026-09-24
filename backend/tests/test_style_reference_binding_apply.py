"""风格参考 v3 直接绑定(P6a,台账 U1 / U9 / N2 / N8):``services/style_reference/binding_apply.py``。

钉住:
- 用于作品直接写绑定行(不经待办),配置落成 v3 四键,``strategy`` 恒 ``mixed``,画像随之启用;
- 同一画像再用于同一目标:复用同一行,只改给出的键,``dimension_states`` 按维合并;
- **一个目标只有一条生效的绑定**:换另一份画像时旧的那条停用,结果里列出来;再换回来,旧行复活、新行停用;
- 目标不存在 404、范围不对 / 缺目标 400、画像失效 / 归档 409;
- 规划产物只在参考真的变了时作废(语义不变的重复应用不作废);旧格式(intensity / 策略 A)的行改写成 v3 不算变;
- ``update_binding_config`` / ``remove_binding`` / ``project_style_binding``。
全部是合成数据(无真实作者原文 / 作品人物)。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    SceneBlueprint,
    StoryProject,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
)
from novel_system.services.errors import DomainError
from novel_system.services.style_reference.binding_apply import (
    apply_style_profile,
    merge_config,
    project_style_binding,
    remove_binding,
    update_binding_config,
)
from novel_system.services.style_reference.binding_config import ALL_DIMENSIONS
from tests.style_reference_inject_helpers import bind, seed_reference, seed_scene


def _project(session, project_id: str) -> str:
    if session.get(StoryProject, project_id) is None:
        session.add(StoryProject(project_id=project_id, title="合成作品", outline_text=""))
        session.commit()
    return project_id


def _profile(session, key: str, **kwargs) -> tuple[str, str]:
    return seed_reference(session, key, chapters=3, per_chapter=30, **kwargs)


def _blueprint(session, scene, row_id: str, *, status: str = "accepted") -> str:
    session.add(
        SceneBlueprint(
            row_id=row_id,
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            blueprint_json={"visible_desire": "x"},
            status=status,
        )
    )
    session.commit()
    return row_id


def test_apply_writes_a_v3_binding_and_activates_the_profile(session) -> None:
    _book, profile_id = _profile(session, "ba_new")
    session.get(StyleReferenceProfile, profile_id).status = "draft"
    session.commit()
    project_id = _project(session, "PRJ_BA_NEW")
    change = apply_style_profile(session, profile_id, scope="project", scope_ref_id=project_id)
    session.commit()
    binding = change.binding
    assert change.created is True and change.changed is True and change.replaced == []
    assert binding.strategy == "mixed" and binding.status == "active" and binding.task_type == "scene_generation"
    assert binding.config_json == {
        "reference_mode": "full",
        "sample_windows": 12,
        "dimension_states": {dim: "normal" for dim in ALL_DIMENSIONS},
        "draft_mode": "style_first",
    }
    assert session.get(StyleReferenceProfile, profile_id).status == "active"


def test_reapply_reuses_the_row_and_merges_dimension_states(session) -> None:
    _book, profile_id = _profile(session, "ba_merge")
    project_id = _project(session, "PRJ_BA_MERGE")
    first = apply_style_profile(
        session,
        profile_id,
        scope="project",
        scope_ref_id=project_id,
        config={"sample_windows": 6, "dimension_states": {"language.rhetoric": "emphasize"}},
    )
    session.commit()
    second = apply_style_profile(
        session,
        profile_id,
        scope="project",
        scope_ref_id=project_id,
        config={"draft_mode": "neutral_first", "dimension_states": {"scene.dialogue": "exclude"}},
    )
    session.commit()
    assert second.binding.binding_id == first.binding.binding_id and second.created is False
    config = second.binding.config_json
    assert config["sample_windows"] == 6 and config["draft_mode"] == "neutral_first"
    assert config["dimension_states"]["language.rhetoric"] == "emphasize"
    assert config["dimension_states"]["scene.dialogue"] == "exclude"
    # 一模一样再来一次:没有变化
    third = apply_style_profile(session, profile_id, scope="project", scope_ref_id=project_id, config={"sample_windows": 6})
    assert third.changed is False and third.superseded_planning is None


def test_one_active_binding_per_target(session) -> None:
    _book_a, profile_a = _profile(session, "ba_one_a")
    _book_b, profile_b = _profile(session, "ba_one_b")
    project_id = _project(session, "PRJ_BA_ONE")
    first = apply_style_profile(session, profile_a, scope="project", scope_ref_id=project_id)
    session.commit()
    second = apply_style_profile(session, profile_b, scope="project", scope_ref_id=project_id)
    session.commit()
    assert [item["binding_id"] for item in second.replaced] == [first.binding.binding_id]
    assert second.replaced[0]["profile_id"] == profile_a and second.replaced[0]["book_title"] == "合成书"
    statuses = {
        b.profile_id: b.status
        for b in session.scalars(
            select(StyleReferenceInjectionBinding).where(StyleReferenceInjectionBinding.scope_ref_id == project_id)
        )
    }
    assert statuses == {profile_a: "disabled", profile_b: "active"}
    # 换回来:旧行复活(同一个 binding_id),另一条停用
    back = apply_style_profile(session, profile_a, scope="project", scope_ref_id=project_id)
    session.commit()
    assert back.binding.binding_id == first.binding.binding_id and back.binding.status == "active"
    assert [item["profile_id"] for item in back.replaced] == [profile_b]
    active = session.scalars(
        select(StyleReferenceInjectionBinding).where(
            StyleReferenceInjectionBinding.scope_ref_id == project_id,
            StyleReferenceInjectionBinding.status == "active",
        )
    ).all()
    assert [b.profile_id for b in active] == [profile_a]


def test_apply_validates_the_target_and_the_profile(session) -> None:
    _book, profile_id = _profile(session, "ba_bad")
    project_id = _project(session, "PRJ_BA_BAD")
    with pytest.raises(DomainError) as missing_project:
        apply_style_profile(session, profile_id, scope="project", scope_ref_id="PRJ_NOPE")
    assert missing_project.value.status_code == 404
    assert missing_project.value.code == "STYLE_REFERENCE_APPLY_TARGET_NOT_FOUND"
    with pytest.raises(DomainError) as missing_scene:
        apply_style_profile(session, profile_id, scope="scene", scope_ref_id="SC_NOPE")
    assert missing_scene.value.status_code == 404
    for scope, ref in (("global", project_id), ("project", ""), ("chapter", project_id)):
        with pytest.raises(DomainError) as bad:
            apply_style_profile(session, profile_id, scope=scope, scope_ref_id=ref)
        assert bad.value.status_code == 400 and bad.value.code == "STYLE_REFERENCE_APPLY_PARAM_INVALID"
    with pytest.raises(DomainError) as unknown:
        apply_style_profile(session, "sr_profile_nope", scope="project", scope_ref_id=project_id)
    assert unknown.value.status_code == 404
    profile = session.get(StyleReferenceProfile, profile_id)
    profile.coverage_json = {"stale": True}
    session.commit()
    with pytest.raises(DomainError) as stale:
        apply_style_profile(session, profile_id, scope="project", scope_ref_id=project_id)
    assert stale.value.status_code == 409 and stale.value.code == "STYLE_REFERENCE_PROFILE_STALE"
    profile.coverage_json = {}
    profile.status = "archived"
    session.commit()
    with pytest.raises(DomainError) as archived:
        apply_style_profile(session, profile_id, scope="project", scope_ref_id=project_id)
    assert archived.value.status_code == 409
    assert session.scalars(select(StyleReferenceInjectionBinding)).all() == []


def test_scene_and_character_scopes(session) -> None:
    _book, profile_id = _profile(session, "ba_scope")
    scene = seed_scene(session, "BA_SC_1", project_id="PRJ_BA_SCOPE", chapter_id="BA_CH_1")
    scene_change = apply_style_profile(session, profile_id, scope="scene", scope_ref_id=scene.scene_id)
    char_change = apply_style_profile(session, profile_id, scope="character", scope_ref_id="BA_CHAR_1")
    session.commit()
    assert scene_change.binding.scope == "scene" and char_change.binding.scope == "character"
    payload = project_style_binding(session, "PRJ_BA_SCOPE")
    # 这部作品没有项目层绑定;场景级绑定计数如实给出
    assert payload["binding"] is None and payload["scene_bindings"] == 1


def test_planning_is_superseded_only_when_the_reference_changes(session) -> None:
    _book, profile_id = _profile(session, "ba_plan")
    scene = seed_scene(session, "BA_PLAN_SC", project_id="PRJ_BA_PLAN", chapter_id="BA_PLAN_CH")
    _blueprint(session, scene, "bp_first")
    first = apply_style_profile(session, profile_id, scope="project", scope_ref_id="PRJ_BA_PLAN")
    session.commit()
    assert first.superseded_planning and first.superseded_planning["scene_blueprints"] == 1
    assert session.get(SceneBlueprint, "bp_first").status == "superseded"
    # 语义不变的重复应用不作废新做的蓝图
    _blueprint(session, scene, "bp_fresh")
    same = apply_style_profile(session, profile_id, scope="project", scope_ref_id="PRJ_BA_PLAN")
    session.commit()
    assert same.changed is False and same.superseded_planning is None
    assert session.get(SceneBlueprint, "bp_fresh").status == "accepted"
    # 配置真的变了 → 作废
    update_binding_config(session, first.binding.binding_id, {"reference_mode": "card_only"})
    session.commit()
    assert session.get(SceneBlueprint, "bp_fresh").status == "superseded"


def test_legacy_rows_are_rewritten_without_counting_as_a_change(session) -> None:
    """迁移 0092 回填过的行(v3 三键、没有 draft_mode、strategy 已是 mixed)再「用于作品」:补齐第四键不算参考变了。"""
    _book, profile_id = _profile(session, "ba_legacy")
    project_id = _project(session, "PRJ_BA_LEGACY")
    legacy = bind(
        session,
        profile_id,
        binding_id="ba_legacy_bind",
        scope_ref_id=project_id,
        config_json={"reference_mode": "card_only", "sample_windows": 7, "dimension_states": {}},
    )
    change = apply_style_profile(session, profile_id, scope="project", scope_ref_id=project_id)
    session.commit()
    assert change.binding.binding_id == legacy.binding_id and change.changed is False
    assert change.binding.strategy == "mixed"
    assert change.binding.config_json["reference_mode"] == "card_only" and change.binding.config_json["sample_windows"] == 7
    assert change.binding.config_json["draft_mode"] == "style_first"


def test_update_and_remove_binding(session) -> None:
    _book, profile_id = _profile(session, "ba_patch")
    project_id = _project(session, "PRJ_BA_PATCH")
    change = apply_style_profile(session, profile_id, scope="project", scope_ref_id=project_id)
    session.commit()
    updated = update_binding_config(
        session, change.binding.binding_id, {"dimension_states": {"theme.values": "exclude"}, "sample_windows": 0}
    )
    session.commit()
    assert updated.changed is True
    assert updated.binding.config_json["dimension_states"]["theme.values"] == "exclude"
    assert updated.binding.config_json["sample_windows"] == 0
    assert updated.binding.config_json["reference_mode"] == "full"
    with pytest.raises(DomainError) as missing:
        update_binding_config(session, "sr_bind_nope", {})
    assert missing.value.status_code == 404
    removed = remove_binding(session, change.binding.binding_id)
    session.commit()
    assert removed["deleted"] is True
    assert session.get(StyleReferenceInjectionBinding, change.binding.binding_id) is None
    with pytest.raises(DomainError):
        remove_binding(session, change.binding.binding_id)


def test_merge_config_keeps_unknown_dimension_states_out() -> None:
    merged = merge_config(
        {"reference_mode": "card_only", "sample_windows": 3},
        {"dimension_states": {"language.rhetoric": "emphasize", "made.up": "exclude"}, "sample_windows": 99},
    )
    assert merged["reference_mode"] == "card_only"
    assert merged["sample_windows"] == 16  # 夹到 0–16
    assert merged["dimension_states"]["language.rhetoric"] == "emphasize"
    assert "made.up" not in merged["dimension_states"]


def test_project_style_binding_reports_the_effective_binding(session) -> None:
    _book, profile_id = _profile(session, "ba_read", cloud_policy="segments_only")
    project_id = _project(session, "PRJ_BA_READ")
    empty = project_style_binding(session, project_id)
    assert empty["binding"] is None and empty["policy"]["bound"] is False and empty["project_title"] == "合成作品"
    apply_style_profile(session, profile_id, scope="project", scope_ref_id=project_id, config={"sample_windows": 9})
    session.commit()
    payload = project_style_binding(session, project_id)
    assert payload["binding"]["profile_id"] == profile_id and payload["binding"]["config"]["sample_windows"] == 9
    # 「只发短句」的书:起草时只送文风卡
    assert payload["binding"]["effective_reference_mode"] == "card_only"
    assert payload["policy"]["bound"] is True and payload["policy"]["reference_mode"] == "card_only"
    assert payload["profile"]["profile_id"] == profile_id and payload["book"]["cloud_policy"] == "segments_only"
    with pytest.raises(DomainError) as missing:
        project_style_binding(session, "PRJ_NOPE")
    assert missing.value.status_code == 404
