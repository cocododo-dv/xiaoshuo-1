"""资料库(library)实体与关系 — 后端 P0 回归测试。

Conventions mirror tests/test_character_entity_sync.py (``client`` + ``session``
fixtures from conftest.py).
"""
from __future__ import annotations

import uuid


def _idem() -> dict:
    return {"X-Idempotency-Key": f"lib-test-{uuid.uuid4().hex[:10]}"}

from novel_system.db.models import StoryCharacter
from novel_system.services.snowflake_steps import SNOWFLAKE_METHOD_VERSION
from tests.support.seed import seed_project


def _create_project() -> dict:
    """一部雪花作品（与 v1 建作品接口落的那一行一样：雪花方法版本、探索模式）。"""
    project_id = f"PRJ_LIB_{uuid.uuid4().hex[:10].upper()}"
    seed_project(
        project_id,
        title="盐镇来信",
        genre="悬疑",
        target_chapter_count=2,
        target_word_count=100000,
        outline_text="怀梅在盐场捡到一枚注销的旧工牌。\n她追查工牌主人。\n真相牵出家史。",
        planning_mode="snowflake",
        snowflake_schema_version=SNOWFLAKE_METHOD_VERSION,
        snowflake_workflow_mode="explore",
    )
    return {"project_id": project_id}


def _seed_character(session, project_id: str, character_id: str = "CHAR_HM") -> StoryCharacter:
    character = StoryCharacter(
        character_id=character_id,
        project_id=project_id,
        display_name="苏怀梅",
        role="主角",
        summary_json={"one_line": "替父亲跑腿时撞见家史边缘的女儿"},
        status="draft",
    )
    session.add(character)
    session.commit()
    return character


def test_library_overview_merges_characters_and_entities(client, session) -> None:
    project = _create_project()
    _seed_character(session, project["project_id"])

    created = client.post(
        f"/api/v2/projects/{project['project_id']}/library/entities",
        json={"kind": "location", "name": "盐场", "summary": "故事的秩序中心", "tags": ["第一幕"]},
        headers=_idem(),
    )
    assert created.status_code == 200, created.text
    entity = created.json()["data"]
    assert entity["ref"].startswith("entity:")
    assert entity["kind"] == "location"

    overview = client.get(f"/api/v2/projects/{project['project_id']}/library")
    assert overview.status_code == 200, overview.text
    data = overview.json()["data"]
    assert [item["name"] for item in data["characters"]] == ["苏怀梅"]
    assert data["characters"][0]["kind"] == "character"
    assert data["characters"][0]["summary"] == "替父亲跑腿时撞见家史边缘的女儿"
    assert [item["name"] for item in data["entities"]] == ["盐场"]
    assert data["relations"] == []


def test_library_entity_validation_and_update(client) -> None:
    project = _create_project()

    missing_name = client.post(
        f"/api/v2/projects/{project['project_id']}/library/entities",
        json={"kind": "location", "name": "  "},
        headers=_idem(),
    )
    assert missing_name.status_code == 400
    assert missing_name.json()["error"]["code"] == "LIBRARY_ENTITY_NAME_REQUIRED"

    bad_kind = client.post(
        f"/api/v2/projects/{project['project_id']}/library/entities",
        json={"kind": "spaceship", "name": "X"},
        headers=_idem(),
    )
    assert bad_kind.status_code == 400
    assert bad_kind.json()["error"]["code"] == "LIBRARY_ENTITY_KIND_INVALID"

    entity = client.post(
        f"/api/v2/projects/{project['project_id']}/library/entities",
        json={"kind": "item", "name": "旧工牌"},
        headers=_idem(),
    ).json()["data"]

    updated = client.patch(
        f"/api/v2/projects/{project['project_id']}/library/entities/{entity['entity_id']}",
        json={"summary": "名字被磨掉的注销工牌", "status": "archived", "tags": ["线索"]},
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()["data"]
    assert body["summary"] == "名字被磨掉的注销工牌"
    assert body["status"] == "archived"
    assert body["tags"] == ["线索"]


def test_library_relations_validate_refs_and_scope(client, session) -> None:
    project = _create_project()
    character = _seed_character(session, project["project_id"])
    entity = client.post(
        f"/api/v2/projects/{project['project_id']}/library/entities",
        json={"kind": "location", "name": "祖屋"},
        headers=_idem(),
    ).json()["data"]

    relation = client.post(
        f"/api/v2/projects/{project['project_id']}/library/relations",
        json={
            "from_ref": f"character:{character.character_id}",
            "to_ref": entity["ref"],
            "kind": "lives_in",
            "note": "三代同堂的老宅",
        },
        headers=_idem(),
    )
    assert relation.status_code == 200, relation.text
    relation_body = relation.json()["data"]
    assert relation_body["kind"] == "lives_in"

    bad_ref = client.post(
        f"/api/v2/projects/{project['project_id']}/library/relations",
        json={"from_ref": "scene:SC01", "to_ref": entity["ref"]},
        headers=_idem(),
    )
    assert bad_ref.status_code == 400
    assert bad_ref.json()["error"]["code"] == "LIBRARY_RELATION_REF_INVALID"

    self_loop = client.post(
        f"/api/v2/projects/{project['project_id']}/library/relations",
        json={"from_ref": entity["ref"], "to_ref": entity["ref"]},
        headers=_idem(),
    )
    assert self_loop.status_code == 400
    assert self_loop.json()["error"]["code"] == "LIBRARY_RELATION_SELF_LOOP"

    other_project = _create_project()
    cross_scope = client.get(f"/api/v2/projects/{other_project['project_id']}/library")
    assert cross_scope.status_code == 200
    assert cross_scope.json()["data"]["entities"] == []
    assert cross_scope.json()["data"]["relations"] == []

    deleted = client.delete(
        f"/api/v2/projects/{project['project_id']}/library/relations/{relation_body['relation_id']}",
    )
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted"] is True

    overview = client.get(f"/api/v2/projects/{project['project_id']}/library")
    assert overview.json()["data"]["relations"] == []


def test_library_character_and_entity_delete_cascades_relations(client, session) -> None:
    """Q2 回归：人物 / 实体可删除，连带清理关系边；缺失对象 404。

    此前 characters/entities 无 DELETE 端点（405），前端删除仅清本地、refetch 后复活。
    """
    project = _create_project()
    pid = project["project_id"]
    character = _seed_character(session, pid)
    entity = client.post(
        f"/api/v2/projects/{pid}/library/entities",
        json={"kind": "location", "name": "盐田"},
        headers=_idem(),
    ).json()["data"]
    client.post(
        f"/api/v2/projects/{pid}/library/relations",
        json={"from_ref": f"character:{character.character_id}", "to_ref": entity["ref"], "kind": "works_at"},
        headers=_idem(),
    )

    # 删除实体 → 实体消失、相关关系边一并清理
    deleted_entity = client.delete(f"/api/v2/projects/{pid}/library/entities/{entity['entity_id']}")
    assert deleted_entity.status_code == 200, deleted_entity.text
    body = deleted_entity.json()["data"]
    assert body["deleted"] is True and body["relations_removed"] == 1
    after = client.get(f"/api/v2/projects/{pid}/library").json()["data"]
    assert after["entities"] == []
    assert after["relations"] == []  # 端点被删，悬挂边已清理

    # 删除人物 → 人物从聚合消失
    deleted_char = client.delete(f"/api/v2/projects/{pid}/library/characters/{character.character_id}")
    assert deleted_char.status_code == 200, deleted_char.text
    assert deleted_char.json()["data"]["deleted"] is True
    chars_after = client.get(f"/api/v2/projects/{pid}/library").json()["data"]["characters"]
    assert all(c["character_id"] != character.character_id for c in chars_after)

    # 重复删除 / 不存在 → 结构化 404（非 405/500）
    missing_entity = client.delete(f"/api/v2/projects/{pid}/library/entities/{entity['entity_id']}")
    assert missing_entity.status_code == 404
    assert missing_entity.json()["error"]["code"] == "LIBRARY_ENTITY_NOT_FOUND"
    missing_char = client.delete(f"/api/v2/projects/{pid}/library/characters/{character.character_id}")
    assert missing_char.status_code == 404
    assert missing_char.json()["error"]["code"] == "LIBRARY_CHARACTER_NOT_FOUND"
