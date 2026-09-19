"""FE-ALIGN Phase 3 护栏：雪花物化与目录 API 同源（防分叉）。

materialize / outline approve 创建的 ChapterGoal/SceneCard 必须能被目录 API
原样读回；目录 PATCH 后再次走物化链不产生重复章。
"""
from __future__ import annotations


import pytest as _pytest_sk
from tests.real_llm_fakes import install_skeleton_snowflake as _install_skeleton_snowflake


@_pytest_sk.fixture(autouse=True)
def _auto_skeleton_snowflake(monkeypatch):
    """假生成已退役：雪花 generate_step 走规划器骨架直通（仅回归物化/失效/收口链路）。"""
    _install_skeleton_snowflake(monkeypatch)



def _create_project(client, key: str) -> dict:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": f"同源护栏 {key}",
            "genre": "悬疑",
            "target_chapter_count": 2,
            "target_word_count": 120000,
            "outline_text": "旧信把她拉回雨城。\n悬案与家族纠缠。\n她必须决定真相值不值得。",
        },
        headers={"X-Idempotency-Key": f"sss-create-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]


def _approve_generated_step(client, project_id: str, step_key: str) -> None:
    generated = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/generate",
        json={},
        headers={"X-Idempotency-Key": f"sss-generate-{project_id}-{step_key}"},
    )
    assert generated.status_code == 200, generated.text
    approved = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve",
        json={},
        headers={"X-Idempotency-Key": f"sss-approve-{project_id}-{step_key}"},
    )
    assert approved.status_code == 200, approved.text


ALL_STEPS = [
    "book_brief",
    "one_sentence_summary",
    "one_paragraph_summary",
    "character_sheets",
    "short_synopsis",
    "character_synopses",
    "long_synopsis",
    "character_bibles",
    "scene_list",
    "scene_details",
]


def _materialize_and_approve(client, project_id: str, key: str) -> dict:
    materialized = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": f"sss-materialize-{key}"},
    )
    assert materialized.status_code == 200, materialized.text
    approved = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": f"sss-outline-approve-{key}"},
    )
    assert approved.status_code == 200, approved.text
    return approved.json()["data"]


def test_materialized_structure_readable_via_catalog_api(client):
    project = _create_project(client, "read")
    pid = project["project_id"]
    for step_key in ALL_STEPS:
        _approve_generated_step(client, pid, step_key)
    approved = _materialize_and_approve(client, pid, "read")
    plan_chapters = approved["plan"]["plan_json"]["chapters"]

    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    assert len(tree["chapters"]) == len(plan_chapters)
    plan_scene_count = sum(len(c.get("scenes") or []) for c in plan_chapters)
    catalog_scene_count = sum(len(c["scenes"]) for c in tree["chapters"])
    assert catalog_scene_count == plan_scene_count

    # brief 字段（GCS/RDD 之一）按 kind 暴露
    for chapter in tree["chapters"]:
        for scene in chapter["scenes"]:
            assert scene["brief"]["kind"] in {"proactive", "reactive"}
            expected_keys = (
                {"goal", "conflict", "setback"}
                if scene["brief"]["kind"] == "proactive"
                else {"reaction", "dilemma", "decision"}
            )
            assert expected_keys <= set(scene["brief"].keys())
            assert scene["slug"] == scene["scene_id"]
            assert scene["legacy_slug"].startswith(chapter["slug"] + "s")

    # 物化把首章立为当前章
    assert tree["chapters"][0]["current"] is True


def test_catalog_patch_then_rematerialize_does_not_duplicate(client):
    project = _create_project(client, "redo")
    pid = project["project_id"]
    for step_key in ALL_STEPS:
        _approve_generated_step(client, pid, step_key)
    _materialize_and_approve(client, pid, "redo-1")

    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    chapter_count = len(tree["chapters"])
    first = tree["chapters"][0]
    patched = client.patch(
        f"/api/v2/projects/{pid}/catalog/chapters/{first['chapter_id']}",
        json={"title": "目录改过的章题", "state": "review"},
    )
    assert patched.status_code == 200, patched.text

    # 再次走物化链（同一批 scene plans → upsert 同一批行）
    _materialize_and_approve(client, pid, "redo-2")
    tree2 = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    assert len(tree2["chapters"]) == chapter_count  # 不产生重复章
    # 目录改过的标题保留（narrative_json 是目录的权威字段，物化只写 writer_brief_json）
    assert tree2["chapters"][0]["title"] == "目录改过的章题"
    assert tree2["chapters"][0]["state"] == "review"
