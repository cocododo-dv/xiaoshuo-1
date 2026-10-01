"""FE-ALIGN Phase 5: 待办收件箱（卡片模型 / effect 后端执行 / 派生项 / 角标从列表数）。"""
from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import SceneCard, SceneRunState
from tests.fixture_works import seed_fixture_works
from tests.support.api_client import create_project

_seq = 0


def _post(client, path, body=None):
    global _seq
    _seq += 1
    response = client.post(path, json=body or {}, headers={"X-Idempotency-Key": f"rc-{_seq}"})
    return response


def _create_project(client) -> dict:
    global _seq
    _seq += 1
    return create_project(client, title=f"收件箱测试 {_seq}", outline_text="大纲", key=f"rc-create-{_seq}")


def _card(client, project_id, **overrides):
    payload = {
        "project_id": project_id,
        "kind": "qc",
        "priority": 2,
        "title": "测试卡",
        "source": "测试",
        "where": "测试位置",
        "detail": "说明",
        "actions": [{"label": "知道了", "intent": "quiet", "op": "resolve"}],
        **overrides,
    }
    response = _post(client, "/api/v1/review-items", payload)
    assert response.status_code == 200, response.text
    return response.json()["data"]["card"]


def test_card_create_list_resolve_unresolve(client):
    project = _create_project(client)
    pid = project["project_id"]
    card = _card(client, pid, title="一张普通卡")
    assert card["state"] == "open"
    assert card["kind"] == "qc"

    items = client.get(f"/api/v1/review-items?state=open&project_id={pid}").json()["data"]["items"]
    assert any(item["id"] == card["id"] for item in items)

    resolved = _post(client, f"/api/v1/review-items/{card['id']}/resolve", {"action_index": 0})
    assert resolved.status_code == 200, resolved.text
    items = client.get(f"/api/v1/review-items?state=open&project_id={pid}").json()["data"]["items"]
    assert all(item["id"] != card["id"] for item in items)

    unresolved = _post(client, f"/api/v1/review-items/{card['id']}/unresolve")
    assert unresolved.status_code == 200
    items = client.get(f"/api/v1/review-items?state=open&project_id={pid}").json()["data"]["items"]
    assert any(item["id"] == card["id"] for item in items)


def test_dedupe_key_once_task(client):
    project = _create_project(client)
    pid = project["project_id"]
    first = _card(client, pid, title="同一事项", dedupe_key="task:audit:ch09")
    second_resp = _post(
        client,
        "/api/v1/review-items",
        {"project_id": pid, "kind": "qc", "title": "同一事项又来一次", "dedupe_key": "task:audit:ch09"},
    )
    assert second_resp.status_code == 200
    second = second_resp.json()["data"]
    assert second["deduped"] is True
    assert second["card"]["id"] == first["id"]
    items = client.get(f"/api/v1/review-items?state=open&project_id={pid}").json()["data"]["items"]
    assert sum(1 for item in items if item.get("dedupe_key") == "task:audit:ch09") == 1


def test_resolve_executes_effect_in_transaction(client):
    project = _create_project(client)
    pid = project["project_id"]
    chapter = _post(client, f"/api/v2/projects/{pid}/catalog/chapters", {"title": "原始章题"}).json()["data"]["chapter"]
    cid = chapter["chapter_id"]

    rename = _card(
        client, pid, kind="decision", title="改章题决策",
        options=["新章题 A", "新章题 B"],
        actions=[
            {"label": "用 A", "intent": "primary", "op": "resolve", "effect": {"type": "rename_chapter", "chapter_id": cid, "title": "新章题 A"}},
            {"label": "用 B", "intent": "ghost", "op": "resolve", "effect": {"type": "rename_chapter", "chapter_id": cid, "title": "新章题 B"}},
        ],
    )
    resolved = _post(client, f"/api/v1/review-items/{rename['id']}/resolve", {"action_index": 1})
    assert resolved.status_code == 200, resolved.text
    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    assert tree["chapters"][0]["title"] == "新章题 B"

    insert = _card(
        client, pid, kind="qc", title="插入反应场",
        actions=[
            {"label": "采纳 · 插入反应场", "intent": "primary", "op": "resolve",
             "effect": {"type": "insert_scene", "chapter_id": cid, "at": 1,
                        "scene": {"title": "样例反应场", "kind": "reactive",
                                  "brief": {"reaction": "消化发现", "dilemma": "时间所剩无几", "decision": "（待规划）"}}}},
            {"label": "忽略", "intent": "quiet", "op": "resolve"},
        ],
    )
    resolved = _post(client, f"/api/v1/review-items/{insert['id']}/resolve", {"action_index": 0})
    assert resolved.status_code == 200, resolved.text
    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    scenes = tree["chapters"][0]["scenes"]
    assert scenes[1]["title"] == "样例反应场"
    assert scenes[1]["kind"] == "reactive"

    unknown = _card(client, pid, title="未知效果",
                    actions=[{"label": "执行", "op": "resolve", "effect": {"type": "no_such_effect"}}])
    bad = _post(client, f"/api/v1/review-items/{unknown['id']}/resolve", {"action_index": 0})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "REVIEW_EFFECT_UNKNOWN"


def test_seeded_review_effects_target_the_editable_current_chapter(client, session):
    """演示卡不能指向 import_catalog 已规范化为 approved 的历史前缀章。"""
    seed_fixture_works(session)
    session.commit()

    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    insert = next(item for item in items if item["title"].startswith("第 8 章节奏过快"))
    rename = next(item for item in items if item["title"] == "第 8 章标题在两个候选间未定")

    inserted = _post(
        client,
        f"/api/v1/review-items/{insert['id']}/resolve",
        {"project_id": "work-a", "action_index": 1},
    )
    assert inserted.status_code == 200, inserted.text

    renamed = _post(
        client,
        f"/api/v1/review-items/{rename['id']}/resolve",
        {"project_id": "work-a", "action_index": 1},
    )
    assert renamed.status_code == 200, renamed.text

    chapter = client.get("/api/v2/projects/work-a/catalog").json()["data"]["chapters"][7]
    assert chapter["title"] == "候选标题二"
    assert any(scene["title"] == "样例反应场" for scene in chapter["scenes"])


def test_derived_semantics_appear_block_resolve_vanish_and_refloat(client, session):
    seed_fixture_works(session)
    session.commit()
    # 制造一个目录异常：work-a ch08 的一场标 done 但 0 字
    scene = session.execute(
        SceneCard.__table__.select().where(SceneCard.project_id == "work-a", SceneCard.state == "todo")
    ).first()
    target_id = scene.scene_id
    row = session.get(SceneCard, target_id)
    row.state = "done"
    row.words_current = 0
    session.commit()

    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    hollow = next((i for i in items if str(i["id"]).startswith("derived:catalog:hollow:")), None)
    assert hollow is not None
    assert hollow["live"] is True
    assert "要么正文没保存" in hollow["detail"] and "either" not in hollow["detail"], "卡片文案是中文"

    # ① 不可无动作 resolve
    blocked = _post(client, f"/api/v1/review-items/{hollow['id']}/resolve", {"project_id": "work-a"})
    assert blocked.status_code == 409

    # snooze 按指纹存
    snoozed = _post(client, f"/api/v1/review-items/{hollow['id']}/snooze", {"project_id": "work-a"})
    assert snoozed.status_code == 200
    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    assert all(i["id"] != hollow["id"] for i in items)
    snoozed_list = client.get("/api/v1/review-items?state=snoozed&project_id=work-a").json()["data"]["items"]
    assert any(i["id"] == hollow["id"] for i in snoozed_list)

    # ③ 指纹复浮：源头状况变化（又一场 done 0 字）→ 新指纹，即使曾 snooze 也重新浮现
    another = session.execute(
        SceneCard.__table__.select().where(SceneCard.project_id == "work-a", SceneCard.state == "todo")
    ).first()
    row2 = session.get(SceneCard, another.scene_id)
    row2.state = "done"
    row2.words_current = 0
    session.commit()
    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    refloated = next((i for i in items if str(i["id"]).startswith("derived:catalog:hollow:")), None)
    assert refloated is not None
    assert refloated["id"] != hollow["id"]

    # ② 源头修好自动消失
    row.state = "todo"
    row2.state = "todo"
    session.commit()
    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    assert all(not str(i["id"]).startswith("derived:catalog:hollow:") for i in items)


def test_derived_pipeline_blocked_card_lifecycle(client, session):
    """贯通轮遗留 ③：管线把稿停在人工闸门 → 收件箱出 decision 卡（深链起草台）；
    作者采纳归档（目录场 done）或管线通过（archived）后自动消失。"""
    seed_fixture_works(session)
    session.commit()
    scene = session.execute(
        select(SceneCard).where(SceneCard.project_id == "work-a", SceneCard.state != "done")
    ).scalars().first()
    assert scene is not None
    state = session.get(SceneRunState, scene.scene_id)
    if state is None:
        state = SceneRunState(scene_id=scene.scene_id)
        session.add(state)
    state.scene_status = "human_review_required"
    session.commit()

    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    prefix = f"derived:pipeline:{scene.scene_id}:"
    card = next((i for i in items if str(i["id"]).startswith(prefix)), None)
    assert card is not None, [i["id"] for i in items]
    assert card["live"] is True
    assert card["kind"] == "decision"
    assert card["priority"] == 1
    nav = card["actions"][0]
    assert nav["nav_to"] == "scene"
    assert nav["nav_scene"], nav  # 场景 slug 深链（FE 入列惯用法用它）

    # 状态变化 → 新指纹（旧指纹的 snooze 不再遮它）
    state.scene_status = "soft_qc_patch_required"
    session.commit()
    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    refloated = next((i for i in items if str(i["id"]).startswith(prefix)), None)
    assert refloated is not None
    assert refloated["id"] != card["id"]

    # 作者已在起草台采纳归档（目录场 done）→ 作者主权优先，不再投递
    row = session.get(SceneCard, scene.scene_id)
    row.state = "done"
    row.words_current = 800
    session.commit()
    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    assert all(not str(i["id"]).startswith(prefix) for i in items)

    # 管线通过（archived）也不投递
    row.state = "todo"
    state.scene_status = "archived"
    session.commit()
    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    assert all(not str(i["id"]).startswith(prefix) for i in items)


def test_a_critical_scene_waiting_for_the_blinded_selection_raises_an_inbox_card(client, session):
    """关键场景停在匿名候选终选（批准 #2，重评 R2）：待办里有一张卡，点它去起草台的这一场；选完（状态离开终选）自动消失。
    以前映射表里是一个早就没有生产者的状态名，这种暂停在待办里看不到，运行本章指过来的「去待处理建议」也落空。"""
    seed_fixture_works(session)
    session.commit()
    scene = session.execute(
        select(SceneCard).where(SceneCard.project_id == "work-a", SceneCard.state != "done")
    ).scalars().first()
    state = session.get(SceneRunState, scene.scene_id)
    if state is None:
        state = SceneRunState(scene_id=scene.scene_id)
        session.add(state)
    state.scene_status = "awaiting_candidate_selection"
    session.commit()

    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    card = next((i for i in items if str(i["id"]).startswith(f"derived:pipeline:{scene.scene_id}:")), None)
    assert card is not None, [i["id"] for i in items]
    assert card["title"].endswith("关键场景 · 等你终选")
    assert card["detail"].startswith("去起草台读完候选再选一份，选完自动续跑。") and "选完这条会自动消失" in card["detail"]
    assert card["priority"] == 1 and card["kind"] == "decision"
    assert card["actions"][0]["nav_to"] == "scene" and card["actions"][0]["nav_scene"]

    state.scene_status = "archived"
    session.commit()
    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    assert all(not str(i["id"]).startswith(f"derived:pipeline:{scene.scene_id}:") for i in items)


def test_derived_cards_build_the_catalog_once_per_refresh(client, session, monkeypatch):
    """目录异常与管线停稿两类派生卡共用一份整本书的目录载荷（以前各拼一遍）。"""
    from novel_system.services.catalog import CatalogService

    seed_fixture_works(session)
    session.commit()
    scene = session.execute(
        select(SceneCard).where(SceneCard.project_id == "work-a", SceneCard.state != "done")
    ).scalars().first()
    state = session.get(SceneRunState, scene.scene_id) or SceneRunState(scene_id=scene.scene_id)
    state.scene_status = "human_review_required"
    session.add(state)
    session.commit()
    chapter_count = len(CatalogService(session).chapter_rows("work-a"))
    calls: list[str] = []
    original = CatalogService.chapter_payload

    def counting(self, project, chapter, index, **kwargs):
        calls.append(chapter.chapter_id)
        return original(self, project, chapter, index, **kwargs)

    monkeypatch.setattr(CatalogService, "chapter_payload", counting)
    items = client.get("/api/v1/review-items?state=open&project_id=work-a").json()["data"]["items"]
    assert any(str(i["id"]).startswith("derived:pipeline:") for i in items)
    assert len(calls) == chapter_count


def test_priority_one_cards_come_first_and_the_badge_is_counted_from_the_list(client, session):
    """角标 = 开着的 priority 1 卡数：前端从同一份列表数（单独的 badge 接口没有界面在用，已删——批准 #24a）。"""
    project = _create_project(client)
    pid = project["project_id"]
    _card(client, pid, priority=2, title="普通")
    _card(client, pid, priority=1, kind="decision", title="高优先")
    items = [item for item in client.get(f"/api/v1/review-items?state=open&project_id={pid}").json()["data"]["items"] if not item["live"]]
    assert [item["title"] for item in items] == ["高优先", "普通"]
    assert sum(1 for item in items if item["priority"] == 1) == 1


def test_the_inbox_takes_only_cards(client):
    """收件箱只收卡片：旧的按 review_id upsert 的载荷、单条详情与不带 state 的整表列表都没有调用方，已删。"""
    project = _create_project(client)
    pid = project["project_id"]
    legacy = _post(client, "/api/v1/review-items", {"review_id": "legacy_row", "item_type": "author_preference_profile", "candidate_text": "{}"})
    assert legacy.status_code == 422
    missing_state = client.get(f"/api/v1/review-items?project_id={pid}")
    assert missing_state.status_code == 400 and missing_state.json()["error"]["code"] == "REVIEW_STATE_INVALID"
    missing_project = client.get("/api/v1/review-items?state=open")
    assert missing_project.status_code == 400 and missing_project.json()["error"]["code"] == "REVIEW_PROJECT_REQUIRED"


def test_legacy_preference_decision_rows_are_not_listed(client, session):
    """写作偏好学习退役（批准 #6，重评 R5）：库里留下的「写作偏好」决策行（不论作品内还是全局）不再进待办，
    行本身留在库里；收件箱只列卡片行。"""
    from novel_system.db.models import ReviewItem

    project = _create_project(client)
    pid = project["project_id"]
    card = _card(client, pid, title="一张普通卡")
    for review_id, project_id in (("review_author_pref_project", pid), ("review_author_pref_global_global", None)):
        session.add(
            ReviewItem(
                review_id=review_id,
                item_type="author_preference_profile",
                status="pending",
                candidate_text="{}",
                candidate_payload_json={},
                project_id=project_id,
            )
        )
    session.commit()

    for state in ("open", "snoozed"):
        items = client.get(f"/api/v1/review-items?state={state}&project_id={pid}").json()["data"]["items"]
        assert not any(str(item["id"]).startswith("review_author_pref") for item in items), items
    open_items = client.get(f"/api/v1/review-items?state=open&project_id={pid}").json()["data"]["items"]
    assert [item["id"] for item in open_items if not item["live"]] == [card["id"]]
    session.expire_all()
    assert session.get(ReviewItem, "review_author_pref_project") is not None


def test_global_card_visible_in_any_project(client):
    project = _create_project(client)
    pid = project["project_id"]
    response = _post(
        client,
        "/api/v1/review-items",
        {"kind": "decision", "priority": 1, "title": "全局风格卡", "dedupe_key": "style-profile:sr_test"},
    )
    assert response.status_code == 200
    items = client.get(f"/api/v1/review-items?state=open&project_id={pid}").json()["data"]["items"]
    # project_id 为 NULL 的全局卡此版本不并入项目桶 —— 风格卡设计为全局，先确认行为
    # （列表按 project 过滤；全局卡的并入在 list_cards 处理）
    assert any(i["title"] == "全局风格卡" for i in items)
