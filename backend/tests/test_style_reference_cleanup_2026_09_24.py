"""风格参考 v3 清理（2026-09-24，契约文档 §8.1；W2 包：策略 / 契约 / 注入 / 退役）——没有自然归属的用例放这里。

- C6：规划层「仅本机」的书遇云端路由——保留「不送、不报错」，但记 warning（带 ``route.reason``）；解析异常记 warning + 堆栈；
- C7：抄袭门对「绑到非 active 画像」报 unavailable，不是 0 本书通过；
- S3：执行契约快照的形状与哈希不因「绑定只在 StylePolicy 解析」而变（项目级绑定 → 同一画像 id、同一哈希）；
  分章的参考章长提示改走 ``style_policy_live``；
- S1：``bind_style_profile`` 待办 effect 已删；画像页载荷不再有 ``legacy`` / ``sub_dimensions``；预览契约没有 v2 字段；
- S6：删书时按 ``params_json.book_id`` 清掉这本书的每场冻结选窗。

全部合成文本与中性名字。
"""

from __future__ import annotations

import hashlib
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from novel_system.db.models import StoryProject, StyleReferenceInjectionBinding, StyleReferenceProfile, StyleReferenceSceneWindows
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json
from novel_system.services.reference_copy_gate import check_reference_copy
from novel_system.services.scene_execution import EMPTY_REFERENCE_RULES, SceneExecutionContractService
from novel_system.services.style_policy import PROFILE_NOT_ACTIVE_CODE, style_policy_live
from novel_system.services.style_reference import planning_context as planning_module
from novel_system.services.style_reference import policy as policy_module
from novel_system.services.style_reference.inject.selection import purge_scene_windows_for_book
from novel_system.services.style_reference.planning_context import (
    build_planning_style_reference,
    render_planning_reference,
    resolve_project_style_reference,
)
from novel_system.services.style_reference.summaries import profile_detail
from tests.style_reference_factories import make_binding, make_book, make_profile, synthetic_paragraphs, v3_profile_json
from tests.style_reference_inject_helpers import PROJECT_ID, bind, seed_reference, seed_scene


def _scope(project_id: str) -> SimpleNamespace:
    return SimpleNamespace(project_id=project_id, scene_id=None, pov_character_id=None, onstage_chars_json=[])


# ---------------------------------------------------------------------------
# C6
# ---------------------------------------------------------------------------


def test_planning_layer_withholds_a_local_only_book_on_a_cloud_route_but_says_so(session, monkeypatch, caplog) -> None:
    """「仅本机」的书、规划节点走云端：不送、不报错（可选增强），但 warning 里有原因与路由审计。"""
    _book_id, profile_id = seed_reference(
        session,
        "c6_local",
        chapters=2,
        per_chapter=20,
        cloud_policy="local_only",
        profile_json={
            **v3_profile_json(),
            "structure_card": {"version": "structure_card_v2", "chapter_count": 3, "chapter_chars": {"median": 3000}},
        },
    )
    bind(session, profile_id, binding_id="c6_bind")
    monkeypatch.setattr(policy_module, "runtime_llm_is_local", lambda settings=None: True)
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda node_id, **_k: False)
    policy = style_policy_live(session, _scope(PROJECT_ID))
    assert policy.bound and policy.contract is not None

    with caplog.at_level(logging.WARNING, logger="novel_system.services.style_reference.planning_context"):
        assert render_planning_reference(policy.contract, session=session) is None
        assert build_planning_style_reference(policy.contract, session=session) is None
        assert resolve_project_style_reference(session, PROJECT_ID) is None
    withheld = [r for r in caplog.records if r.levelno == logging.WARNING and "withheld" in r.getMessage()]
    assert withheld, [r.getMessage() for r in caplog.records]
    assert all("cloud_policy" in r.getMessage() or "reason=" in r.getMessage() for r in withheld)
    assert any("render_planning_reference" in r.getMessage() for r in withheld)
    assert any("build_planning_style_reference" in r.getMessage() for r in withheld)

    # 路由本机 → 照常给参考（与上面对照：不是「永远不给」）
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda node_id, **_k: True)
    caplog.clear()
    assert resolve_project_style_reference(session, PROJECT_ID) is not None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_resolve_project_style_reference_logs_unexpected_failures_at_warning_with_traceback(
    session, monkeypatch, caplog
) -> None:
    _book_id, profile_id = seed_reference(session, "c6_boom", chapters=2, per_chapter=20)
    bind(session, profile_id, binding_id="c6_boom_bind")

    def _boom(*_a, **_k):
        raise RuntimeError("synthetic planning failure")

    monkeypatch.setattr(planning_module, "render_planning_reference", _boom)
    with caplog.at_level(logging.DEBUG, logger="novel_system.services.style_reference.planning_context"):
        assert resolve_project_style_reference(session, PROJECT_ID) is None
    records = [r for r in caplog.records if "unavailable" in r.getMessage()]
    assert records and records[-1].levelno == logging.WARNING
    assert records[-1].exc_info is not None and "synthetic planning failure" in str(records[-1].exc_info[1])


# ---------------------------------------------------------------------------
# C7 · 抄袭门
# ---------------------------------------------------------------------------


def test_copy_gate_reports_unavailable_for_a_binding_to_an_archived_profile(session) -> None:
    project_id = "PRJ_C7_GATE"
    session.add(StoryProject(project_id=project_id, title="C7", outline_text=""))
    book_id = make_book(session, "book_c7_gate", paragraphs=synthetic_paragraphs(6))
    profile_id = make_profile(session, book_id, profile_id="profile_c7_gate", status="archived")
    make_binding(session, profile_id, binding_id="bind_c7_gate", scope_ref_id=project_id)
    session.commit()

    policy = style_policy_live(session, _scope(project_id), freeze_contract=False)
    check = check_reference_copy(session, "干净的一句。", policy=policy)
    assert check.blocked is False
    assert check.unavailable is True and check.unavailable_reasons == (PROFILE_NOT_ACTIVE_CODE,)
    assert check.checked_books == (), "画像未启用：这一边没有查成，不能算「查过、没重合」"
    assert check.audit()["unavailable"] is True

    session.get(StyleReferenceProfile, profile_id).status = "active"
    session.commit()
    again = check_reference_copy(session, "干净的一句。", policy=style_policy_live(session, _scope(project_id), freeze_contract=False))
    assert again.unavailable is False and again.checked_books == (book_id,)


# ---------------------------------------------------------------------------
# S3 · 执行契约快照的哈希不变
# ---------------------------------------------------------------------------


def _legacy_reference_profile_ids(session, project_id: str) -> list[str]:
    """清理前 ``scene_execution._active_style_reference_profile`` 的原逻辑（就地重写，作对照）。"""
    binding = session.execute(
        select(StyleReferenceInjectionBinding)
        .where(
            StyleReferenceInjectionBinding.scope == "project",
            StyleReferenceInjectionBinding.scope_ref_id == project_id,
            StyleReferenceInjectionBinding.task_type == "scene_generation",
            StyleReferenceInjectionBinding.status == "active",
        )
        .order_by(StyleReferenceInjectionBinding.created_at.desc(), StyleReferenceInjectionBinding.binding_id.desc())
    ).scalars().first()
    if binding is None:
        return []
    profile = session.get(StyleReferenceProfile, binding.profile_id)
    return [profile.profile_id] if profile is not None else []


def _legacy_reference_rules(profile_json: dict) -> dict[str, list[str]]:
    """清理前 ``scene_execution._normalize_reference_rules`` 的原逻辑（v3 画像没有这些键 → 三个空表）。"""

    def _listify(value):
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        return [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []

    style = _listify(profile_json.get("style_rules")) or (
        _listify(profile_json.get("style_features"))
        + _listify(profile_json.get("rhythm"))
        + _listify(profile_json.get("syntax"))
        + _listify(profile_json.get("narrative_methods"))
    )
    structure = _listify(profile_json.get("structure_rules")) or (
        _listify(profile_json.get("narrative_patterns"))
        + _listify(profile_json.get("calibration_guidance"))
        + _listify(profile_json.get("structure_patterns"))
        + _listify(profile_json.get("structure_techniques"))
    )
    safety = _listify(profile_json.get("safety_rules")) or (
        _listify(profile_json.get("banned_replication_rules"))
        + _listify(profile_json.get("forbidden_copy_rules"))
        + _listify(profile_json.get("safety_constraints"))
    )
    dedupe = lambda values: list(dict.fromkeys(values))  # noqa: E731
    return {"style_rules": dedupe(style), "structure_rules": dedupe(structure), "safety_rules": dedupe(safety)}


def _snapshot_hash(snapshot: dict) -> str:
    return hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest()


def test_execution_contract_snapshot_hash_is_unchanged_for_a_project_bound_scene(session) -> None:
    """项目级绑定：``reference_profile_ids`` 由 ``style_policy_live`` 给出同一个画像 id，``reference_rules`` 固定三个空表——
    与清理前按绑定行 + 旧规则函数算出的快照逐字节相同（画像 active 与归档两种状态都一样）。"""
    scene = seed_scene(session, "S3_SC01", scene_seq=1)
    _book_id, profile_id = seed_reference(session, "s3_hash", chapters=2, per_chapter=20)
    bind(session, profile_id, binding_id="s3_bind")
    service = SceneExecutionContractService(session)

    for status in ("active", "archived"):
        session.get(StyleReferenceProfile, profile_id).status = status
        session.commit()
        scene_row, chapter, project, blueprint, rules, new_hash, _cached = service._resolve_context(scene.scene_id)
        assert rules == EMPTY_REFERENCE_RULES and rules is not EMPTY_REFERENCE_RULES
        profile_json = dict(session.get(StyleReferenceProfile, profile_id).profile_json or {})
        legacy = service._source_snapshot(scene_row, chapter, project, blueprint, _legacy_reference_rules(profile_json))
        legacy["project"]["reference_profile_ids"] = _legacy_reference_profile_ids(session, scene.project_id)
        assert legacy["project"]["reference_profile_ids"] == [profile_id]
        assert _snapshot_hash(legacy) == new_hash, status

    # 没有绑定的作品：两边都是空表 + 空 id
    other = seed_scene(session, "S3_SC02", project_id="PRJ_S3_OTHER", chapter_id="PRJ_S3_OTHER_CH01", scene_seq=1)
    scene_row, chapter, project, blueprint, rules, new_hash, _cached = service._resolve_context(other.scene_id)
    legacy = service._source_snapshot(scene_row, chapter, project, blueprint, _legacy_reference_rules({}))
    legacy["project"]["reference_profile_ids"] = []
    assert _snapshot_hash(legacy) == new_hash


# ---------------------------------------------------------------------------
# S1 · 退役
# ---------------------------------------------------------------------------


def test_bind_style_profile_review_effect_is_gone(session) -> None:
    from novel_system.services.review_effects import run_effect

    with pytest.raises(DomainError) as caught:
        run_effect(session, "PRJ_X", {"type": "bind_style_profile", "profile_id": "p", "scope": "project"})
    assert caught.value.code == "REVIEW_EFFECT_UNKNOWN"
    assert "bind_style_profile" not in caught.value.details["registered"]


def test_profile_page_payload_has_no_legacy_view_and_no_sub_dimension_counts(session) -> None:
    book_id = make_book(session, "book_s1_page", paragraphs=synthetic_paragraphs(4))
    legacy = make_profile(
        session, book_id, profile_id="profile_s1_legacy", status="archived",
        profile_json={"style_features": ["短句"], "sub_dimensions": {"language.rhetoric": {"observation_count": 3}}},
    )
    v3 = make_profile(session, book_id, profile_id="profile_s1_v3", profile_json=v3_profile_json())
    session.commit()
    for profile_id in (legacy, v3):
        payload = profile_detail(session, session.get(StyleReferenceProfile, profile_id))
        assert "legacy" not in payload and "sub_dimensions" not in payload
    old = profile_detail(session, session.get(StyleReferenceProfile, legacy))
    # 没有 v3 版本标记的旧画像不是「要重新学」，是「没学过」
    assert old["has_card"] is False and old["needs_relearn"] is False and old["relearn_reason"] is None
    assert all(not dim["lines"] for dim in old["dimensions"])
    new = profile_detail(session, session.get(StyleReferenceProfile, v3))
    assert new["has_card"] is True and new["dimensions"][0]["lines"]


def test_preview_contract_and_request_have_no_v2_fields() -> None:
    from novel_system.services.style_reference.schemas import (
        InjectionPreviewRequest,
        InjectionPreviewStats,
        InjectionStrategy,
        SystemPromptFragments,
        TaskType,
    )

    assert set(SystemPromptFragments.model_fields) == {"positive_block", "voice_block", "few_shot_block", "anti_plagiarism_block"}
    assert set(InjectionPreviewStats.model_fields) == {
        "positive_lines", "avoid_lines", "voice_lines", "few_shot_windows", "few_shot_chars", "total_prefix_chars",
        "card_chars", "few_shot_k",
    }
    assert set(InjectionPreviewRequest.model_fields) == {
        "scene_id", "reference_mode", "sample_windows", "dimension_states", "draft_mode", "project_id",
    }
    for legacy in ({"strategy": "A"}, {"intensity": 50}, {"sub_dimensions": []}, {"include_metric": True}, {"task_type": "scene_generation"}):
        with pytest.raises(ValueError):
            InjectionPreviewRequest.model_validate(legacy)
    assert [m.value for m in InjectionStrategy] == ["mixed"] and [t.value for t in TaskType] == ["scene_generation"]


# ---------------------------------------------------------------------------
# S6 · 删书时清掉这本书的每场冻结选窗
# ---------------------------------------------------------------------------


def test_purge_scene_windows_for_book_deletes_only_that_books_rows(session) -> None:
    rows = [
        StyleReferenceSceneWindows(
            selection_id=f"srsel_s6_{index}", selection_key=f"key_s6_{index}", scene_id=f"S6_SC{index}", bundle_id=None,
            contract_hash="h", window_refs_json=[], params_json=params,
        )
        for index, params in enumerate(
            (
                {"book_id": "book_s6_a", "profile_id": "p_a", "root": "r"},
                {"book_id": "book_s6_a", "profile_id": "p_a", "root": "r"},
                {"book_id": "book_s6_b", "profile_id": "p_b", "root": "r"},
                {"root": "r"},  # 写 book_id 之前的旧行：不动
            )
        )
    ]
    session.add_all(rows)
    session.commit()
    assert purge_scene_windows_for_book(session, "book_s6_a") == 2
    session.commit()
    remaining = sorted(session.scalars(select(StyleReferenceSceneWindows.selection_id)))
    assert remaining == ["srsel_s6_2", "srsel_s6_3"]
    assert purge_scene_windows_for_book(session, "") == 0 and purge_scene_windows_for_book(session, "book_s6_missing") == 0


def test_frozen_selection_records_book_and_profile_ids(session) -> None:
    from novel_system.services.style_reference.inject.render import render_style
    from novel_system.services.style_reference.inject.request import StyleRenderRequest

    scene = seed_scene(session, "S6_SC_FREEZE", scene_seq=1)
    book_id, profile_id = seed_reference(session, "s6_freeze", chapters=2, per_chapter=40)
    bind(session, profile_id, binding_id="s6_freeze_bind")
    policy = style_policy_live(session, scene)
    rendered = render_style(session, policy, StyleRenderRequest(scene_id=scene.scene_id, bundle_id="B_S6"), scene=scene)
    session.commit()
    assert rendered.audit["selection"]["persisted"] is True
    row = session.get(StyleReferenceSceneWindows, rendered.audit["selection"]["selection_id"])
    assert row.params_json["book_id"] == book_id and row.params_json["profile_id"] == profile_id
    assert purge_scene_windows_for_book(session, book_id) == 1
