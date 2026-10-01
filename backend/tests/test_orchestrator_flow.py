from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    FinalScene,
    SceneBundle,
    SceneDraft,
    SceneMemory,
    SceneRunState,
)
from tests.support.scene_pipeline import seed_story

pytestmark = pytest.mark.usefixtures("online_orchestrator_runner")


def test_run_full_scene_runs_without_voice_and_relation_cards(client, session) -> None:
    """2026-09-20：声线 / 关系卡是可选注入。过去缺卡在这里 409 BUNDLE_SOURCE_MISSING——可产品里早已没有
    地方能写这两类卡，真实作品的场永远过不了这一关。批准 #15（重评 R8）之后卡不再进 bundle，两张表随迁移 0098
    删掉：起草照常，bundle 里既没有这两节，也没有它们的出处。
    """
    seed_story()

    response = client.post(
        "/api/v1/scenes/CH001_SC01/run/full",
        headers={"X-Idempotency-Key": "scene-run-missing-bundle-sources"},
    )

    assert response.status_code == 200, response.text
    from novel_system.db.models import SceneBundle

    bundle = session.get(SceneBundle, response.json()["data"]["current_bundle_id"])
    assert bundle is not None
    snapshot = bundle.frozen_snapshot_json
    assert not [key for key in snapshot["source_version_refs"] if key.startswith(("voice_profile", "relation_profile"))]
    assert "voice_card" not in snapshot["inline_digests"]
    assert "relation_card" not in snapshot["inline_digests"]


def test_run_full_scene_archives_memory_and_updates_status(client, session) -> None:
    seed_story()

    response = client.post(
        "/api/v1/scenes/CH001_SC01/run/full",
        headers={"X-Idempotency-Key": "scene-run-1"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["scene_status"] == "archived"
    assert data["safe_to_archive"] is True
    assert isinstance(data["literary_warnings_unresolved"], bool)
    assert data["author_confirmed_final"] is False
    assert data["current_bundle_id"]
    assert data["current_final_scene_row_id"]
    final_scene = session.get(FinalScene, data["current_final_scene_row_id"])
    assert final_scene.content_hash == hashlib.sha256(final_scene.content.encode("utf-8")).hexdigest()

    workbench = client.get("/api/v1/scenes/CH001_SC01/workbench?include=diagnostics")
    assert workbench.status_code == 200
    workbench_data = workbench.json()["data"]
    assert workbench_data["scene_memory"]
    generation_summary = workbench_data["generation_summary"]
    assert generation_summary["step"] == "style_draft"
    assert generation_summary["raw_step"] == "style_draft"
    assert generation_summary["provider"] == "test-online-provider"
    assert generation_summary["finish_reason"] == "stop"
    assert generation_summary["error_code"] is None
    assert generation_summary["llm_call_id"]
    assert generation_summary["prompt_tokens"] > 0
    assert generation_summary["completion_tokens"] > 0
    assert (
        generation_summary["total_tokens"]
        == generation_summary["prompt_tokens"] + generation_summary["completion_tokens"]
    )
    assert workbench_data["near_final_summary"]["near_final_status"] == "near_final_ready"
    assert workbench_data["near_final_summary"]["safe_to_archive"] is True
    assert isinstance(
        workbench_data["near_final_summary"]["literary_warnings_unresolved"],
        bool,
    )
    assert workbench_data["near_final_summary"]["author_confirmed_final"] is False
    assert workbench_data["hard_qc_summary"] == {
        "qc_report_id": workbench_data["hard_qc_summary"]["qc_report_id"],
        "qc_type": "hard_qc",
        "pass_flag": True,
        "resolution_code": "hard_pass",
        "issue_keys": [],
        "next_action": "pass",
        "rewrite_brief": [],
        "created_at": workbench_data["hard_qc_summary"]["created_at"],
    }
    assert workbench_data["soft_qc_summary"] == {
        "qc_report_id": workbench_data["soft_qc_summary"]["qc_report_id"],
        "qc_type": "soft_qc",
        "pass_flag": True,
        "resolution_code": "soft_pass",
        "issue_keys": [],
        "next_action": "pass",
        "rewrite_brief": [],
        "created_at": workbench_data["soft_qc_summary"]["created_at"],
    }
    assert workbench_data["rewrite_counters"] == {
        "hard_partial_rewrite_count": 0,
        "hard_full_rewrite_count": 0,
        "soft_patch_count": 0,
        "repeat_issue_key": None,
        "repeat_issue_count": 0,
    }
    assert workbench_data["human_review_summary"] is None


def test_rerunning_scene_appends_immutable_run_artifacts_and_replays_old_final(client, session) -> None:
    seed_story()

    first_run = client.post(
        "/api/v1/scenes/CH001_SC01/run/full",
        headers={"X-Idempotency-Key": "scene-run-immutable-1"},
    )
    assert first_run.status_code == 200
    first_data = first_run.json()["data"]
    first_bundle_id = first_data["current_bundle_id"]
    first_final_row_id = first_data["current_final_scene_row_id"]
    first_bundle = session.get(SceneBundle, first_bundle_id)
    assert first_bundle is not None
    first_scene_digest = first_bundle.frozen_snapshot_json["inline_digests"]["scene_card"]

    update_scene = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": "CH001_SC01",
            "chapter_id": "CH001",
            "scene_seq": 1,
            "pov_character_id": "CHAR_A",
            "onstage_chars_json": ["CHAR_A", "CHAR_B"],
            "location": "旧城门廊",
            "scene_goal": "让第二次运行形成新的场景目标",
            "beats_json": ["二次试探", "升级", "留钩子"],
            "must_include_text": "",
            "target_length_band": "short",
            "scene_type": "reunion",
            "is_chapter_last": 0,
        },
        headers={"X-Idempotency-Key": "scene-run-immutable-update"},
    )
    assert update_scene.status_code == 200

    second_run = client.post(
        "/api/v1/scenes/CH001_SC01/run/full",
        headers={"X-Idempotency-Key": "scene-run-immutable-2"},
    )
    assert second_run.status_code == 200
    second_data = second_run.json()["data"]
    second_bundle_id = second_data["current_bundle_id"]
    second_final_row_id = second_data["current_final_scene_row_id"]

    assert first_bundle_id != second_bundle_id
    assert first_final_row_id != second_final_row_id

    state = session.get(SceneRunState, "CH001_SC01")
    assert state.current_bundle_id == second_bundle_id
    assert state.current_final_scene_row_id == second_final_row_id

    bundles = session.execute(select(SceneBundle).where(SceneBundle.scene_id == "CH001_SC01")).scalars().all()
    drafts = session.execute(select(SceneDraft).where(SceneDraft.scene_id == "CH001_SC01")).scalars().all()
    finals = session.execute(select(FinalScene).where(FinalScene.scene_id == "CH001_SC01")).scalars().all()
    memories = session.execute(select(SceneMemory).where(SceneMemory.scene_id == "CH001_SC01")).scalars().all()

    assert len(bundles) == 2
    assert len([draft for draft in drafts if draft.stage == "neutral_draft"]) == 2
    assert len([draft for draft in drafts if draft.stage == "style_draft"]) == 2
    assert len(finals) == 2
    assert len(memories) == 2

    assert session.get(SceneBundle, first_bundle_id).frozen_snapshot_json["inline_digests"]["scene_card"] == first_scene_digest
    second_scene_digest = session.get(SceneBundle, second_bundle_id).frozen_snapshot_json["inline_digests"]["scene_card"]
    assert "Goal: 让第二次运行形成新的场景目标" in second_scene_digest
    assert "Location: 旧城门廊" in second_scene_digest

    assert session.get(FinalScene, first_final_row_id).source_bundle_id == first_bundle_id


def test_workbench_generation_summary_can_resolve_from_current_final_scene_provenance(client, session) -> None:
    seed_story()

    response = client.post(
        "/api/v1/scenes/CH001_SC01/run/full",
        headers={"X-Idempotency-Key": "scene-run-final-scene-provenance"},
    )

    assert response.status_code == 200

    state = session.get(SceneRunState, "CH001_SC01")
    final_scene = session.get(FinalScene, state.current_final_scene_row_id)
    assert final_scene is not None
    assert final_scene.generation_llm_call_id

    state.current_neutral_draft_row_id = None
    state.current_style_draft_row_id = None
    session.commit()

    workbench = client.get("/api/v1/scenes/CH001_SC01/workbench")

    assert workbench.status_code == 200
    generation_summary = workbench.json()["data"]["generation_summary"]
    assert generation_summary["llm_call_id"] == final_scene.generation_llm_call_id
    assert generation_summary["step"] == "style_draft"
    assert generation_summary["raw_step"] == "style_draft"
    assert generation_summary["provider"] == "test-online-provider"
    assert generation_summary["finish_reason"] == "stop"
    assert generation_summary["error_code"] is None
    assert generation_summary["prompt_tokens"] > 0
    assert generation_summary["completion_tokens"] > 0
    assert (
        generation_summary["total_tokens"]
        == generation_summary["prompt_tokens"] + generation_summary["completion_tokens"]
    )
