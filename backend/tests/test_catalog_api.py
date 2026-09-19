"""FE-ALIGN Phase 3: 目录 API（/api/v2/projects/{id}/catalog…）。"""
from __future__ import annotations

from novel_system.db.models import AuthorDraft, ChapterGoal, StoryProject

_seq = 0


def _create_project(client) -> dict:
    global _seq
    _seq += 1
    response = client.post(
        "/api/v2/projects",
        json={"title": f"目录测试 {_seq}", "outline_text": "大纲", "genre": "悬疑"},
        headers={"X-Idempotency-Key": f"catalog-create-{_seq}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]


def _post(client, path, body=None, extra_headers=None):
    global _seq
    _seq += 1
    headers = {"X-Idempotency-Key": f"catalog-{_seq}", **(extra_headers or {})}
    response = client.post(path, json=body or {}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_create_first_chapter_becomes_writing_and_current(client):
    project = _create_project(client)
    pid = project["project_id"]
    data = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "第一章"})
    assert data["chapter"]["state"] == "writing"
    assert data["chapter"]["current"] is True
    assert data["chapter"]["slug"] == "ch01"
    assert len(data["chapter"]["scenes"]) == 1  # 默认开场场景
    # 阶段 X：场景 slug = scene_id（稳定身份）；位置式旧 slug 只作为 legacy_slug 供前端迁移本机旧键
    assert data["chapter"]["scenes"][0]["slug"] == data["chapter"]["scenes"][0]["scene_id"]
    assert data["chapter"]["scenes"][0]["legacy_slug"] == "ch01s1"
    assert data["chapter"]["scenes"][0]["state"] == "writing"

    second = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "第二章", "current": False})
    assert second["chapter"]["state"] == "planned"
    assert second["chapter"]["slug"] == "ch02"

    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    assert [c["slug"] for c in tree["chapters"]] == ["ch01", "ch02"]
    assert tree["chapters"][0]["current"] is True


def test_patch_chapter_narrative_and_state(client):
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "原题"})["chapter"]
    response = client.patch(
        f"/api/v2/projects/{pid}/catalog/chapters/{chapter['chapter_id']}",
        json={
            "title": "改名章",
            "state": "draft",
            "words_target": 4200,
            "tension": 0.66,
            "pov": "林岑",
            "drama": {"promise": "p", "spine": "s"},
            "threads": [{"name": "盐钟", "role": "新引"}],
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()["data"]["chapter"]
    assert updated["title"] == "改名章"
    assert updated["state"] == "draft"
    assert updated["words"]["target"] == 4200
    assert updated["tension"] == 0.66
    assert updated["drama"]["spine"] == "s"
    assert updated["threads"][0]["name"] == "盐钟"

    bad = client.patch(
        f"/api/v2/projects/{pid}/catalog/chapters/{chapter['chapter_id']}",
        json={"state": "nonsense"},
    )
    assert bad.status_code == 400


def test_catalog_cannot_bypass_project_final_approval_or_reopen(client, session):
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "Approval guard"})["chapter"]
    chapter_id = chapter["chapter_id"]

    direct_approve = client.patch(
        f"/api/v2/projects/{pid}/catalog/chapters/{chapter_id}",
        json={"state": "approved"},
    )
    assert direct_approve.status_code == 409
    assert direct_approve.json()["error"]["code"] == "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW"

    create_approved = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters",
        json={"title": "Bypass", "state": "approved"},
        headers={"X-Idempotency-Key": "catalog-create-approved-bypass"},
    )
    assert create_approved.status_code == 409
    assert create_approved.json()["error"]["code"] == "CATALOG_CHAPTER_APPROVAL_REQUIRES_PROJECT_FLOW"

    db_project = session.get(StoryProject, pid)
    db_chapter = session.get(ChapterGoal, chapter_id)
    db_project.approved_chapter_ids_json = [chapter_id]
    db_chapter.state = "approved"
    session.commit()

    direct_reopen = client.patch(
        f"/api/v2/projects/{pid}/catalog/chapters/{chapter_id}",
        json={"state": "draft"},
    )
    assert direct_reopen.status_code == 409
    assert direct_reopen.json()["error"]["code"] == "CATALOG_APPROVED_CHAPTER_REOPEN_REQUIRED"


def test_scene_crud_insert_and_kind_brief(client):
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "章"})["chapter"]
    cid = chapter["chapter_id"]

    s2 = _post(client, f"/api/v2/projects/{pid}/catalog/chapters/{cid}/scenes", {"title": "反应场", "kind": "反应", "brief": {"reaction": "震惊", "dilemma": "走或留", "decision": "先藏起来"}})["scene"]
    assert s2["kind"] == "reactive"
    assert s2["brief"]["dilemma"] == "走或留"
    assert s2["seq"] == 2

    s_insert = _post(client, f"/api/v2/projects/{pid}/catalog/chapters/{cid}/scenes", {"title": "插入到最前", "at": 0})["scene"]
    assert s_insert["seq"] == 1

    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    scenes = tree["chapters"][0]["scenes"]
    assert [s["title"] for s in scenes] == ["插入到最前", "开场", "反应场"]
    assert [s["legacy_slug"] for s in scenes] == ["ch01s1", "ch01s2", "ch01s3"]
    # 插到最前面之后，原来那两场的 slug 一个字都没变——身份跟着行走，不跟着位置走
    assert [s["slug"] for s in scenes] == [s["scene_id"] for s in scenes]
    assert s2["slug"] == s2["scene_id"] == scenes[2]["slug"]

    patched = client.patch(
        f"/api/v2/projects/{pid}/catalog/scenes/{s2['scene_id']}",
        json={"title": "改名场", "state": "done", "brief": {"decision": "撕掉信"}},
    ).json()["data"]["scene"]
    assert patched["title"] == "改名场"
    assert patched["state"] == "done"
    assert patched["brief"]["decision"] == "撕掉信"


def test_catalog_import_then_blocked_when_not_empty(client, monkeypatch):
    monkeypatch.setenv("NOVEL_SYSTEM_ADMIN_TOKEN", "admin-token")
    project = _create_project(client)
    pid = project["project_id"]
    payload = {
        "chapters": [
            {
                "title": "迁移章一",
                "state": "approved",
                "words": {"cur": 3000, "target": 4000},
                "current": False,
                "tension": 0.4,
                "scenes": [
                    {"title": "场一", "kind": "主动", "state": "done", "goal": "g", "obstacle": "o", "turn": "t"},
                    {"title": "场二", "kind": "反应", "state": "done", "goal": "r", "obstacle": "d", "turn": "x"},
                ],
            },
            {"title": "迁移章二", "state": "writing", "current": True, "scenes": [{"title": "在写场", "kind": "主动", "state": "writing"}]},
        ]
    }
    no_token = client.post(
        f"/api/v2/projects/{pid}/catalog/import",
        json=payload,
        headers={"X-Idempotency-Key": "catalog-import-no-token"},
    )
    assert no_token.status_code == 403  # admin 保护

    data = _post(client, f"/api/v2/projects/{pid}/catalog/import", payload,
                 extra_headers={"X-Admin-Token": "admin-token"})
    assert data["created_chapter_count"] == 2
    assert data["created_scene_count"] == 3

    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    ch1 = tree["chapters"][0]
    assert ch1["words"]["cur"] == 3000  # 章级字数摊给场景后 rollup 不丢
    assert ch1["scenes"][1]["kind"] == "reactive"
    assert tree["chapters"][1]["current"] is True
    dashboard = client.get(f"/api/v1/projects/{pid}/dashboard").json()["data"]
    assert dashboard["project"]["approved_chapter_ids"] == [tree["chapters"][0]["chapter_id"]]
    assert dashboard["project"]["current_chapter_id"] == tree["chapters"][1]["chapter_id"]
    assert dashboard["project"]["status"] == "chapter_ready"

    again = client.post(
        f"/api/v2/projects/{pid}/catalog/import",
        json=payload,
        headers={"X-Idempotency-Key": "catalog-import-again", "X-Admin-Token": "admin-token"},
    )
    assert again.status_code == 409  # 非空目录拒绝导入


def test_catalog_import_rejects_non_linear_approval_or_current(client, monkeypatch):
    monkeypatch.setenv("NOVEL_SYSTEM_ADMIN_TOKEN", "admin-token")
    project = _create_project(client)
    pid = project["project_id"]
    path = f"/api/v2/projects/{pid}/catalog/import"
    headers = {"X-Admin-Token": "admin-token", "X-Idempotency-Key": "catalog-import-nonlinear-approved"}

    non_linear = client.post(
        path,
        json={
            "chapters": [
                {"title": "First", "state": "draft", "current": True},
                {"title": "Second", "state": "approved"},
            ]
        },
        headers=headers,
    )
    assert non_linear.status_code == 400
    assert non_linear.json()["error"]["code"] == "CATALOG_IMPORT_APPROVAL_ORDER_INVALID"

    wrong_current = client.post(
        path,
        json={
            "chapters": [
                {"title": "First", "state": "approved", "current": True},
                {"title": "Second", "state": "writing"},
            ]
        },
        headers={**headers, "X-Idempotency-Key": "catalog-import-wrong-current"},
    )
    assert wrong_current.status_code == 400
    assert wrong_current.json()["error"]["code"] == "CATALOG_IMPORT_CURRENT_INVALID"


def test_draft_save_updates_scene_words_and_returns_rollup(client, session):
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "章"})["chapter"]
    scene_id = chapter["scenes"][0]["scene_id"]
    draft = AuthorDraft(
        draft_id="draft-catalog-1",
        object_type="scene",
        object_id=scene_id,
        source_text_ref="test",
        content="",
        revision_no=1,
        status="current",
    )
    session.add(draft)
    session.commit()

    response = client.patch(
        "/api/v1/author-drafts/draft-catalog-1",
        json={"content": "这一场写了二十个字符的正文内容补满数。", "base_revision_no": 1},
    )
    assert response.status_code == 200, response.text
    rollup = response.json()["data"]["words_rollup"]
    assert rollup["scene_id"] == scene_id
    assert rollup["scene_words"] == 19
    assert rollup["chapter_words"] == 19
    assert "words_total" in rollup

    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    assert tree["chapters"][0]["words"]["cur"] == 19
    assert tree["chapters"][0]["scenes"][0]["words"] == 19


def test_create_chapter_idempotency_contract(client):
    """FE-ALIGN 修复：目录创建端点必须兑现幂等键（重放同响应、缺键 400、换载荷 409）。"""
    project = _create_project(client)
    pid = project["project_id"]

    key = "catalog-idem-replay-1"
    first = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters",
        json={"title": "幂等章"},
        headers={"X-Idempotency-Key": key},
    )
    assert first.status_code == 200, first.text
    replay = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters",
        json={"title": "幂等章"},
        headers={"X-Idempotency-Key": key},
    )
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("X-Idempotency-Status") == "replayed"
    assert replay.json()["data"]["chapter"]["chapter_id"] == first.json()["data"]["chapter"]["chapter_id"]
    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    assert sum(1 for ch in tree["chapters"] if ch["title"] == "幂等章") == 1

    missing = client.post(f"/api/v2/projects/{pid}/catalog/chapters", json={"title": "无键章"})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    reused = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters",
        json={"title": "另一个标题"},
        headers={"X-Idempotency-Key": key},
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"

    chapter_id = first.json()["data"]["chapter"]["chapter_id"]
    scene_key = "catalog-idem-scene-1"
    scene_first = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chapter_id}/scenes",
        json={"title": "幂等场"},
        headers={"X-Idempotency-Key": scene_key},
    )
    scene_replay = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chapter_id}/scenes",
        json={"title": "幂等场"},
        headers={"X-Idempotency-Key": scene_key},
    )
    assert scene_first.status_code == scene_replay.status_code == 200
    assert (
        scene_replay.json()["data"]["scene"]["scene_id"]
        == scene_first.json()["data"]["scene"]["scene_id"]
    )


def test_scene_pov_set_by_name_creates_character_and_round_trips(client):
    """POV 设置端点：按名设 POV → find-or-create 角色，catalog 往返回传 id/name。"""
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "章"})["chapter"]
    sid = chapter["scenes"][0]["scene_id"]

    r = client.patch(f"/api/v2/projects/{pid}/catalog/scenes/{sid}", json={"pov_character_name": "林深"})
    assert r.status_code == 200, r.text
    sc = r.json()["data"]["scene"]
    assert sc["pov_character_name"] == "林深"
    pov_id = sc["pov_character_id"]
    assert pov_id, "应已 find-or-create 出角色并绑定"

    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    sc2 = tree["chapters"][0]["scenes"][0]
    assert sc2["pov_character_id"] == pov_id and sc2["pov_character_name"] == "林深"

    # 同名再设 → 复用同一角色，不重复建
    r2 = client.patch(f"/api/v2/projects/{pid}/catalog/scenes/{sid}", json={"pov_character_name": "林深"})
    assert r2.json()["data"]["scene"]["pov_character_id"] == pov_id


def test_scene_pov_by_existing_id_bad_id_and_clear(client):
    """按既有 id 设 / 不存在 id → 400 / 空串清空。"""
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "章"})["chapter"]
    sid = chapter["scenes"][0]["scene_id"]
    pov_id = client.patch(f"/api/v2/projects/{pid}/catalog/scenes/{sid}", json={"pov_character_name": "角色甲"}).json()["data"]["scene"]["pov_character_id"]

    ok = client.patch(f"/api/v2/projects/{pid}/catalog/scenes/{sid}", json={"pov_character_id": pov_id})
    assert ok.status_code == 200 and ok.json()["data"]["scene"]["pov_character_id"] == pov_id

    bad = client.patch(f"/api/v2/projects/{pid}/catalog/scenes/{sid}", json={"pov_character_id": "CHAR_NOPE"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "CATALOG_POV_CHARACTER_NOT_FOUND"

    clr = client.patch(f"/api/v2/projects/{pid}/catalog/scenes/{sid}", json={"pov_character_id": ""})
    assert clr.status_code == 200 and clr.json()["data"]["scene"]["pov_character_id"] == ""


def test_scene_handoff_fields_persist_on_create_and_update(client):
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(
        client,
        f"/api/v2/projects/{pid}/catalog/chapters",
        {"title": "Handoff chapter", "with_scene": False},
    )["chapter"]

    created = _post(
        client,
        f"/api/v2/projects/{pid}/catalog/chapters/{chapter['chapter_id']}/scenes",
        {
            "title": "Handoff scene",
            "exit_change": "The witness changes sides.",
            "hook": "A sealed letter arrives.",
            "pov_character_name": "Lin Shen",
        },
    )["scene"]
    assert created["exit_change"] == "The witness changes sides."
    assert created["hook"] == "A sealed letter arrives."
    assert created["pov_character_name"] == "Lin Shen"

    updated = client.patch(
        f"/api/v2/projects/{pid}/catalog/scenes/{created['scene_id']}",
        json={
            "exit_change": "The witness retracts the statement.",
            "hook": "The letter is forged.",
        },
    )
    assert updated.status_code == 200, updated.text
    scene = updated.json()["data"]["scene"]
    assert scene["exit_change"] == "The witness retracts the statement."
    assert scene["hook"] == "The letter is forged."

    reloaded = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    persisted = reloaded["chapters"][0]["scenes"][0]
    assert persisted["exit_change"] == scene["exit_change"]
    assert persisted["hook"] == scene["hook"]
