"""pass3 R2：Style Reference 红线对抗式测试（2026-09-23 风格参考 v3 口径）。

SR-G1：原文样例只在书允许送给**接收提示的那个节点**时进提示——「仅本机」的书遇云端节点一个字都不送（渲染 409，
H1），起草节点走本机模型时照常送；``segments_only`` 的书按 v3「参考方式」一律只送文风卡
（``effective_reference_mode`` → ``card_only``），同样不送窗口。
"""

from __future__ import annotations

import pytest

from novel_system.services.style_policy import policy_from_contract
from novel_system.services.style_reference import policy as policy_module
from novel_system.services.style_reference.errors import CloudPolicyBlockedError
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


def _policy(session, key: str, cloud_policy: str):
    _book_id, profile_id = seed_reference(session, key, chapters=2, per_chapter=80, cloud_policy=cloud_policy)
    bind(session, profile_id, binding_id=f"g1_bind_{key}")
    layers = resolve_binding_layers(session, PROJECT_ID, "scene_generation")
    contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
    return policy_from_contract(contract, mode="frozen")


def _render(session, key: str, cloud_policy: str):
    policy = _policy(session, key, cloud_policy)
    request = StyleRenderRequest(placement=PLACEMENT_USER_TAIL, scene_id=f"G1_{key}", node_ids=("style_draft",))
    return policy, render_style(session, policy, request)


def test_local_only_book_never_sends_anything_to_a_cloud_drafting_node(session, monkeypatch) -> None:
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda *_a, **_k: False)
    policy = _policy(session, "g1local", "local_only")
    assert policy.reference_mode == "full"  # 绑定说全面模仿，但书只许本机模型读
    request = StyleRenderRequest(placement=PLACEMENT_USER_TAIL, scene_id="G1_g1local", node_ids=("style_draft",))
    with pytest.raises(CloudPolicyBlockedError) as caught:
        render_style(session, policy, request)
    assert caught.value.details["node_id"] == "style_draft" and caught.value.status_code == 409


def test_local_only_book_sends_windows_when_the_drafting_node_is_local(session, monkeypatch) -> None:
    monkeypatch.setattr(policy_module, "node_route_is_local", lambda node_id, **_k: node_id == "style_draft")
    policy, rendered = _render(session, "g1localok", "local_only")
    assert rendered.stats["few_shot_windows"] >= 2 and "[文风卡]" in rendered.system_prefix
    assert rendered.audit["route"]["route_local"] is True and rendered.audit["samples_blocked"] is None


def test_segments_only_book_sends_the_card_but_no_windows(session) -> None:
    policy, rendered = _render(session, "g1seg", "segments_only")
    assert policy.reference_mode == "card_only"
    assert rendered.stats["few_shot_windows"] == 0 and "\n- (第" not in rendered.user_tail
    assert "[文风卡]" in rendered.system_prefix and "[声音特征]" in rendered.system_prefix


def test_allow_full_cloud_book_sends_windows(session) -> None:
    policy, rendered = _render(session, "g1full", "allow_full_cloud")
    assert policy.reference_mode == "full" and rendered.stats["few_shot_windows"] >= 2
