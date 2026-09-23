"""pass3 R2：Style Reference 红线对抗式测试（2026-09-23 风格参考 v3 口径）。

SR-G1：原文样例只在书允许送云时进提示——「仅本机」的书遇云端模型一窗都不送；``segments_only`` 的书按 v3
「参考方式」一律只送文风卡（``effective_reference_mode`` → ``card_only``），同样不送窗口。
"""

from __future__ import annotations

import pytest

from novel_system.services.style_policy import policy_from_contract
from novel_system.services.style_reference.inject.bindings import resolve_binding_layers
from novel_system.services.style_reference.inject.render import render_style, reset_render_cache
from novel_system.services.style_reference.inject.request import PLACEMENT_USER_TAIL, StyleRenderRequest
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import build_style_runtime_contract
from tests.style_reference_inject_helpers import PROJECT_ID, bind, seed_reference


@pytest.fixture(autouse=True)
def _fresh():
    reset_render_cache()
    yield
    reset_render_cache()


def _render(session, key: str, cloud_policy: str):
    _book_id, profile_id = seed_reference(session, key, chapters=2, per_chapter=80, cloud_policy=cloud_policy)
    bind(session, profile_id, binding_id=f"g1_bind_{key}")
    layers = resolve_binding_layers(session, PROJECT_ID, "scene_generation")
    contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
    policy = policy_from_contract(contract, mode="frozen")
    return policy, render_style(session, policy, StyleRenderRequest(placement=PLACEMENT_USER_TAIL, scene_id=f"G1_{key}"))


def test_local_only_book_never_sends_windows_to_a_cloud_model(session) -> None:
    policy, rendered = _render(session, "g1local", "local_only")
    assert policy.reference_mode == "full"  # 绑定说全面模仿，但书不许送云
    assert rendered.stats["few_shot_windows"] == 0 and rendered.user_tail == ""
    assert rendered.audit["samples_blocked"] in ("cloud_policy_at_freeze", "cloud_policy_now")
    assert "[文风卡]" in rendered.system_prefix and "严格禁止" in rendered.system_prefix


def test_segments_only_book_sends_the_card_but_no_windows(session) -> None:
    policy, rendered = _render(session, "g1seg", "segments_only")
    assert policy.reference_mode == "card_only"
    assert rendered.stats["few_shot_windows"] == 0 and rendered.user_tail == ""
    assert "[文风卡]" in rendered.system_prefix and "[声音特征]" in rendered.system_prefix


def test_allow_full_cloud_book_sends_windows(session) -> None:
    policy, rendered = _render(session, "g1full", "allow_full_cloud")
    assert policy.reference_mode == "full" and rendered.stats["few_shot_windows"] >= 2
