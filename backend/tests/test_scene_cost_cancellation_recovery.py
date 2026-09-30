"""Wave 6（§8 第 4 项 / §5.8 硬行为 / §7 不变量 1·2·5·9）：预算耗尽、失败恢复、
取消等价——已完成调用与草稿不因后续停摆回滚，且成本仍可解释。

顺序管线无独立取消端点：「取消尚未开始的新节点」= 预算闸 can_spend 阻止新调用
（Wave 3 已实装）。本文件锁定「停摆不丢稿 + 成本仍可解释」的不变量。
"""
from __future__ import annotations

from novel_system.db.models import FinalScene, LlmCall, SceneCard, SceneDraft, SceneRunState
from novel_system.services import cost_aggregation as ca
from novel_system.services import scene_budget
from novel_system.services.scene_budget import can_spend


def _scene_with_valid_draft(session, scene_id, chapter_id="CCH", *, budget=1000, used=0):
    session.add(SceneCard(scene_id=scene_id, chapter_id=chapter_id, project_id="CP", scene_seq=1, scene_goal="g"))
    session.add(
        SceneDraft(
            row_id=f"draft_{scene_id}", scene_id=scene_id, chapter_id=chapter_id, stage="style",
            content="已有有效正文", source_bundle_id="b", source_bundle_hash="h",
        )
    )
    session.add(
        SceneRunState(
            scene_id=scene_id, scene_token_budget=budget, scene_tokens_used=used,
            latest_valid_draft_row_id=f"draft_{scene_id}",
        )
    )
    session.flush()


def test_budget_exhaustion_blocks_new_calls_but_keeps_draft(session):
    _scene_with_valid_draft(session, "CX1", budget=1000, used=1000)
    state = session.get(SceneRunState, "CX1")
    # 预算耗尽 → 可选支出被拦（等价「取消尚未开始的新节点」）
    assert can_spend(state, 200) is False
    # 但最近有效正文指针不清空（§7 不变量 2）
    assert state.latest_valid_draft_row_id == "draft_CX1"
    assert session.get(SceneDraft, "draft_CX1") is not None


def test_legacy_0064_over_budget_cost_remains_readable_without_becoming_normal_semantics(session):
    _scene_with_valid_draft(session, "CX2", budget=1000, used=1500)
    session.add(
        LlmCall(
            llm_call_id="cxc1", provider="openai_compatible", model="gpt-5",
            scope_type="scene", scope_id="CX2",
            node_id="style_draft", step="style_draft", scene_id="CX2", chapter_id="CCH",
            project_id="CP", prompt_tokens=800, completion_tokens=400, total_tokens=1200,
        )
    )
    session.flush()
    summary = ca.scene_cost(session, "CX2")
    # 仅兼容读取 0064 历史脏数据；新在线调用由预留门禁阻止制造该状态。
    assert summary["budget"]["over_budget"] is True
    assert summary["total_tokens"] == 1_200
    assert summary["phase_breakdown"]["candidate_generation"]["tokens"] == 1_200
    assert summary["calibers"]["estimate"]["tokens"] == 1_200
    assert summary["calibers"]["provider_actual"]["tokens"] == 0
    assert summary["calibers"]["budget_charged"]["tokens"] == 0
    assert summary["attempt_observability"]["legacy_parent_without_attempt_count"] == 1
    assert summary["attempt_observability"]["legacy_unreconstructable_tokens"] == 1_200
    assert summary["calibers"]["estimate"]["source"].endswith(
        "_with_legacy_total_tokens_fallback"
    )
    assert summary["calibers"]["budget_charged"]["source"] == (
        "llm_calls.budget_charged_tokens"
    )


def test_failed_call_does_not_roll_back_draft_and_is_attributed(session):
    _scene_with_valid_draft(session, "CX3", budget=1000, used=300)
    # 一次失败调用（error_code）——记账但不撤销已有正文（§7 不变量 1）
    session.add(
        LlmCall(
            llm_call_id="cxc_fail", provider="openai_compatible", model="gpt-5",
            scope_type="scene", scope_id="CX3",
            node_id="style_patch", step="style_patch", scene_id="CX3", chapter_id="CCH",
            project_id="CP", prompt_tokens=100, completion_tokens=0, total_tokens=100,
            error_code="LLM_TIMEOUT",
        )
    )
    session.flush()
    state = session.get(SceneRunState, "CX3")
    assert state.latest_valid_draft_row_id == "draft_CX3"  # 失败不清指针
    summary = ca.scene_cost(session, "CX3")
    assert summary["extra_cost"]["failed_tokens"] == 100
    assert summary["extra_cost"]["failed_attempt_count"] == 1


def test_archived_final_survives_after_stop(session):
    _scene_with_valid_draft(session, "CX4", budget=1000, used=1000)
    session.add(
        FinalScene(
            row_id="cxfs4", scene_id="CX4", chapter_id="CCH", content="已归档正文",
            status="archived", source_bundle_id="b", source_bundle_hash="h",
        )
    )
    session.flush()
    # 停摆（预算耗尽）后已归档正文不回滚，仍可回放（§7 不变量 5）
    state = session.get(SceneRunState, "CX4")
    assert can_spend(state, 100) is False
    fs = session.get(FinalScene, "cxfs4")
    assert fs is not None and fs.status == "archived" and fs.content == "已归档正文"


def test_legacy_record_usage_bypass_is_not_available():
    assert not hasattr(scene_budget, "record_usage")
