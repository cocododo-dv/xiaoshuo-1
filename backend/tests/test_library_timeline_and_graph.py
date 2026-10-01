"""FE-ALIGN Phase 6: 资料库（时间线 / 图投影 / 人物资料卡 / 半自动派生）。"""
from __future__ import annotations

from novel_system.db.models import StoryCharacter
from tests.fixture_works import seed_fixture_works

_seq = 0


def _post(client, path, body=None):
    global _seq
    _seq += 1
    response = client.post(path, json=body or {}, headers={"X-Idempotency-Key": f"lib-{_seq}"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _create_project(client) -> str:
    global _seq
    _seq += 1
    response = client.post(
        "/api/v2/projects",
        json={"title": f"资料库测试 {_seq}", "outline_text": "大纲"},
        headers={"X-Idempotency-Key": f"lib-create-{_seq}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def test_timeline_crud_and_ordering(client):
    pid = _create_project(client)
    first = _post(client, f"/api/v2/projects/{pid}/library/timeline", {"label": "第一潮汐事件", "time_label": "二十年前", "chapter_ref": "CH02"})
    second = _post(client, f"/api/v2/projects/{pid}/library/timeline", {"label": "签名被覆写", "chapter_ref": "CH05"})
    items = client.get(f"/api/v2/projects/{pid}/library/timeline").json()["data"]["items"]
    assert [item["label"] for item in items] == ["第一潮汐事件", "签名被覆写"]

    patched = client.patch(
        f"/api/v2/projects/{pid}/library/timeline/{second['event_id']}",
        json={"display_order": 0, "note": "提前"},
    )
    assert patched.status_code == 200
    items = client.get(f"/api/v2/projects/{pid}/library/timeline").json()["data"]["items"]
    assert items[0]["label"] == "签名被覆写"

    deleted = client.delete(f"/api/v2/projects/{pid}/library/timeline/{first['event_id']}")
    assert deleted.status_code == 200
    items = client.get(f"/api/v2/projects/{pid}/library/timeline").json()["data"]["items"]
    assert len(items) == 1


def test_graph_projection(client, session):
    pid = _create_project(client)
    session.add(StoryCharacter(character_id="char-g1", project_id=pid, display_name="角色甲", status="active"))
    session.commit()
    entity = _post(client, f"/api/v2/projects/{pid}/library/entities", {"name": "旧城档案馆", "kind": "location"})
    _post(
        client,
        f"/api/v2/projects/{pid}/library/relations",
        {"from_ref": "char-g1" and "character:char-g1", "to_ref": entity["ref"], "kind": "works_at", "note": "工作于"},
    )
    graph = client.get(f"/api/v2/projects/{pid}/library/graph").json()["data"]
    node_ids = {node["id"] for node in graph["nodes"]}
    assert "character:char-g1" in node_ids
    assert entity["ref"] in node_ids
    assert graph["edges"][0]["relation"] == "works_at"
    assert graph["edges"][0]["note"] == "工作于"


def test_character_card_update_keeps_id(client, session):
    pid = _create_project(client)
    session.add(StoryCharacter(character_id="char-u1", project_id=pid, display_name="旧名", status="active"))
    session.commit()
    patched = client.patch(
        f"/api/v2/projects/{pid}/library/characters/char-u1",
        json={"name": "新名", "summary": "改写后的一行简介"},
    )
    assert patched.status_code == 200, patched.text
    data = patched.json()["data"]
    assert data["name"] == "新名"
    assert data["character_id"] == "char-u1"  # 引用经 id 不断
    assert data["summary"] == "改写后的一行简介"


def test_derive_effects_create_entity_and_timeline_via_resolve(client):
    pid = _create_project(client)
    entity_card = _post(
        client,
        "/api/v1/review-items",
        {
            "project_id": pid, "kind": "idea", "title": "发现新设定：盐钟",
            "dedupe_key": f"derive:ch:盐钟",
            "actions": [
                {"label": "确认入库", "intent": "primary", "op": "resolve",
                 "effect": {"type": "create_entity", "name": "盐钟", "kind": "item", "summary": "潮汐校准装置"}},
                {"label": "忽略", "intent": "quiet", "op": "resolve"},
            ],
        },
    )["card"]
    resolved = _post(client, f"/api/v1/review-items/{entity_card['id']}/resolve", {"action_index": 0})
    assert resolved["effect_result"]["name"] == "盐钟"
    overview = client.get(f"/api/v2/projects/{pid}/library").json()["data"]
    assert any(e["name"] == "盐钟" for e in overview["entities"])
    graph = client.get(f"/api/v2/projects/{pid}/library/graph").json()["data"]
    assert any(n["name"] == "盐钟" for n in graph["nodes"])

    event_card = _post(
        client,
        "/api/v1/review-items",
        {
            "project_id": pid, "kind": "idea", "title": "发现新事件：第三潮汐",
            "actions": [
                {"label": "确认入库", "intent": "primary", "op": "resolve",
                 "effect": {"type": "add_timeline_event", "label": "第三潮汐事件", "time_label": "二十年前", "chapter_ref": "CH02"}},
                {"label": "忽略", "intent": "quiet", "op": "resolve"},
            ],
        },
    )["card"]
    _post(client, f"/api/v1/review-items/{event_card['id']}/resolve", {"action_index": 0})
    timeline = client.get(f"/api/v2/projects/{pid}/library/timeline").json()["data"]["items"]
    assert any(e["label"] == "第三潮汐事件" for e in timeline)


def test_demo_seed_populates_library(client, session):
    seed_fixture_works(session)
    session.commit()
    overview = client.get("/api/v2/projects/work-a/library").json()["data"]
    assert any(c["name"] == "角色甲" for c in overview["characters"])
    assert any(e["name"] == "设定甲" for e in overview["entities"])
    assert overview["relations"]
    assert overview["timeline"]
    graph = client.get("/api/v2/projects/work-a/library/graph").json()["data"]
    assert len(graph["nodes"]) >= 10
    assert len(graph["edges"]) >= 5
    # 幂等
    seed_fixture_works(session)
    session.commit()
    overview2 = client.get("/api/v2/projects/work-a/library").json()["data"]
    assert len(overview2["entities"]) == len(overview["entities"])
    assert len(overview2["relations"]) == len(overview["relations"])


def test_library_creates_honor_idempotency_replay(client, raw_client):
    """FE-ALIGN 修复：资料库创建端点兑现幂等键（重放不建重复行）。"""
    pid = _create_project(client)
    key = "lib-idem-entity-1"
    headers = {"X-Idempotency-Key": key}
    first = client.post(f"/api/v2/projects/{pid}/library/entities", json={"name": "幂等灯塔"}, headers=headers)
    replay = client.post(f"/api/v2/projects/{pid}/library/entities", json={"name": "幂等灯塔"}, headers=headers)
    assert first.status_code == replay.status_code == 200
    assert replay.headers.get("X-Idempotency-Status") == "replayed"
    assert replay.json()["data"]["entity_id"] == first.json()["data"]["entity_id"]
    overview = client.get(f"/api/v2/projects/{pid}/library").json()["data"]
    assert sum(1 for e in overview["entities"] if e["name"] == "幂等灯塔") == 1

    missing = raw_client.post(f"/api/v2/projects/{pid}/library/timeline", json={"label": "无键事件"})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_library_updates_and_deletes_are_safely_replayable(client):
    pid = _create_project(client)
    created = _post(
        client,
        f"/api/v2/projects/{pid}/library/entities",
        {"name": "Replay target", "kind": "location"},
    )
    entity_id = created["entity_id"]

    patch_headers = {"X-Idempotency-Key": "lib-idem-patch-1"}
    patch_body = {"name": "Replay target updated"}
    first_patch = client.patch(
        f"/api/v2/projects/{pid}/library/entities/{entity_id}",
        json=patch_body,
        headers=patch_headers,
    )
    replayed_patch = client.patch(
        f"/api/v2/projects/{pid}/library/entities/{entity_id}",
        json=patch_body,
        headers=patch_headers,
    )

    assert first_patch.status_code == replayed_patch.status_code == 200
    assert replayed_patch.headers["X-Idempotency-Status"] == "replayed"
    assert replayed_patch.json()["data"] == first_patch.json()["data"]

    conflict = client.patch(
        f"/api/v2/projects/{pid}/library/entities/{entity_id}",
        json={"name": "Different payload"},
        headers=patch_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"

    delete_headers = {"X-Idempotency-Key": "lib-idem-delete-1"}
    first_delete = client.delete(
        f"/api/v2/projects/{pid}/library/entities/{entity_id}",
        headers=delete_headers,
    )
    replayed_delete = client.delete(
        f"/api/v2/projects/{pid}/library/entities/{entity_id}",
        headers=delete_headers,
    )

    assert first_delete.status_code == replayed_delete.status_code == 200
    assert replayed_delete.headers["X-Idempotency-Status"] == "replayed"
    assert replayed_delete.json()["data"] == first_delete.json()["data"]


def test_timeline_rejects_cross_project_entity_references(client):
    owner_id = _create_project(client)
    foreign_id = _create_project(client)
    entity = _post(
        client,
        f"/api/v2/projects/{owner_id}/library/entities",
        {"name": "Owner-only archive", "kind": "location"},
    )

    response = client.post(
        f"/api/v2/projects/{foreign_id}/library/timeline",
        json={"label": "Foreign event", "entity_refs": [entity["ref"]]},
        headers={"X-Idempotency-Key": "library-cross-project-timeline-ref"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "LIBRARY_ENTITY_NOT_FOUND"


def test_character_delete_is_blocked_while_catalog_scene_references_it(client):
    pid = _create_project(client)
    character = _post(
        client,
        f"/api/v2/projects/{pid}/library/characters",
        {"name": "Referenced POV"},
    )
    chapter = _post(
        client,
        f"/api/v2/projects/{pid}/catalog/chapters",
        {"title": "POV chapter", "with_scene": False},
    )["chapter"]
    _post(
        client,
        f"/api/v2/projects/{pid}/catalog/chapters/{chapter['chapter_id']}/scenes",
        {"title": "POV scene", "pov_character_id": character["character_id"]},
    )

    response = client.delete(
        f"/api/v2/projects/{pid}/library/characters/{character['character_id']}"
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "LIBRARY_CHARACTER_IN_USE"
    assert error["details"]["dependencies"]["catalog_scenes"] == 1
    overview = client.get(f"/api/v2/projects/{pid}/library").json()["data"]
    assert any(
        item["character_id"] == character["character_id"]
        for item in overview["characters"]
    )


def test_entity_delete_removes_timeline_references(client):
    pid = _create_project(client)
    entity = _post(
        client,
        f"/api/v2/projects/{pid}/library/entities",
        {"name": "Temporary place", "kind": "location"},
    )
    event = _post(
        client,
        f"/api/v2/projects/{pid}/library/timeline",
        {"label": "Visit", "entity_refs": [entity["ref"]]},
    )

    deleted = client.delete(
        f"/api/v2/projects/{pid}/library/entities/{entity['entity_id']}"
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["timeline_refs_removed"] == 1

    timeline = client.get(f"/api/v2/projects/{pid}/library/timeline").json()["data"]
    stored = next(item for item in timeline["items"] if item["event_id"] == event["event_id"])
    assert stored["entity_refs"] == []
