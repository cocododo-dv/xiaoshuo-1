"""Phase 1 止血回归：场景身份铸造（P1-2）与场景列表收口（P1-3）。

两个缺陷都是先在真实工作台上复现、再写的守卫：

- P1-2 撞号丢场：``scene_id`` 曾是 ``f"{chapter_id}_SC{scene_seq:02d}"``，创建时铸死
  而 ``scene_seq`` 每次保存重算，于是「删一场再加一场」必然撞号；物化时
  ``detail_by_id`` 与 ``session.get(SceneCard, scene_id)`` 双重覆盖，5 个场景计划
  静默变成 4 张场景卡。
- P1-3 幽灵场：``_sync_scene_plans`` 只增不删，被作者删掉的场永远留在库里、拿不到
  第 10 步细化、被诊断成 rewrite，用一个作者看不见的场把物化闸门永久堵死。

用例走的是前端 ``canonFromFE``（ws-snow-sync.jsx）真实发出的字段形状，而不是后端
内部结构——这两个缺陷正是在那个形状下才暴露的。
"""

from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    OperationLog,
    SceneCard,
    SnowflakeScenePlan,
)


# --------------------------------------------------------------------------- helpers


def _create_project(client, key: str, *, chapters: int = 12) -> str:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": "Rain City Signal",
            "genre": "Urban Mystery",
            "target_chapter_count": chapters,
            "target_word_count": 120000,
            "outline_text": (
                "An old letter pulls the heroine back to Rain City.\n"
                "The cold case turns out to be tied to her family.\n"
                "She must decide whether the truth is worth the cost."
            ),
        },
        headers={"X-Idempotency-Key": f"identity-create-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _patch_step(client, project_id: str, step_key: str, draft: dict) -> dict:
    response = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}",
        json={"draft": draft, "force": True},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _approve_step(client, project_id: str, step_key: str) -> None:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve",
        json={},
    )
    assert response.status_code == 200, response.text


#: 前八步的规范草稿，字段形状抄自 ws-snow-sync.jsx 的 ``canonFromFE``。
_UPSTREAM_DRAFTS: dict[str, dict] = {
    "book_brief": {
        "category": "都市悬疑",
        "target_reader": "25-35 岁女性读者",
        "delight_reason": "抽丝剥茧的推理快感",
        "story_kind": "长篇",
        "genre_promise": "不写甜宠",
        "expected_reader_emotion": "紧张",
    },
    "one_sentence_summary": {"summary": "一位记者必须查清旧案，但真凶是她的父亲。"},
    "one_paragraph_summary": {
        "sentences": ["回到雨城", "灾一：被迫卷入", "灾二：世界观被打碎", "灾三：局势失控", "决战与收尾"],
        "moral_premise": "真相高于安稳",
    },
    "character_sheets": {
        "characters": [
            {
                "character_id": "c1",
                "display_name": "林昭",
                "role": "主角",
                "goal": "查清旧案",
                "ambition": "自由",
                "values": ["诚实"],
                "conflict": "家族",
                "epiphany": "真相有代价",
            }
        ]
    },
    "short_synopsis": {"paragraphs": ["铺垫", "灾一", "灾二", "灾三", "收尾"]},
    "character_synopses": {
        "characters": [
            {"character_id": "c1", "display_name": "林昭", "role": "主角", "synopsis": "信念：真相\n旧伤：母亲之死"}
        ]
    },
    # 2026-09-30（B07-22）：章表只在结构化的 chapters 里（散文段落里的「NN 章名：」行不再解析）
    "long_synopsis": {
        "paragraphs": ["", "", "", ""],
        "chapters": [
            {"act": 1, "title": "雨夜来信", "summary": "一封旧信把她拉回雨城。"},
            {"act": 1, "title": "旧案卷宗", "summary": "她翻出封存的案卷。"},
            {"act": 2, "title": "父亲的谎", "summary": "父亲的时间线对不上。"},
            {"act": 2, "title": "追捕", "summary": "证据被销毁。"},
            {"act": 3, "title": "抉择", "summary": "公开还是保全家人。"},
            {"act": 3, "title": "余波", "summary": "代价落地。"},
        ],
    },
    "character_bibles": {
        "characters": [
            {
                "character_id": "c1",
                "display_name": "林昭",
                "role": "主角",
                "physical_profile": {"appearance": "瘦"},
                "personality_profile": {"strongest_trait": "固执"},
                "environment_profile": {"home": "雨城"},
                "psychological_profile": {
                    "philosophy": "真相",
                    "self_image": "逃兵",
                    "deepest_fear": "重蹈覆辙",
                },
            }
        ]
    },
}


def _approve_upstream(client, project_id: str) -> None:
    for step_key, draft in _UPSTREAM_DRAFTS.items():
        _patch_step(client, project_id, step_key, draft)
        _approve_step(client, project_id, step_key)


def _scene_row(row_uid: str, seq: int, text: str) -> dict:
    """09 场景列表的一行（canonFromFE("scenes") 的字段集——注意没有 chapter_id）。"""
    return {
        "row_uid": row_uid,
        "scene_seq": seq,
        "summary": text,
        "primary_form": "proactive",
        "pov_character_id": "c1",
        "location": "雨城",
        "crucible": "她不能就这样走开",
        "chapter_role": "推进",
    }


def _detail_row(row_uid: str, seq: int, text: str) -> dict:
    """10 场景规划的一行（canonFromFE("planning") 的字段集）。"""
    return {
        "row_uid": row_uid,
        "scene_seq": seq,
        "title": text,
        "summary": text,
        "primary_form": "proactive",
        "location": "雨城",
        "crucible": "她不能就这样走开",
        "scene_crucible": "她不能就这样走开",
        "pov_character_id": "c1",
        "goal": f"{text}·目标",
        "conflict": f"{text}·冲突",
        "setback": f"{text}·挫败",
        "cost_requirement": f"{text}·代价",
    }


def _active_plans(session, project_id: str) -> list[SnowflakeScenePlan]:
    return list(
        session.execute(
            select(SnowflakeScenePlan)
            .where(
                SnowflakeScenePlan.project_id == project_id,
                SnowflakeScenePlan.removed_at.is_(None),
            )
            .order_by(SnowflakeScenePlan.scene_seq.asc())
        ).scalars()
    )


def _all_plans(session, project_id: str) -> list[SnowflakeScenePlan]:
    return list(
        session.execute(
            select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)
        ).scalars()
    )


# ------------------------------------------------------------------- P1-2 撞号丢场


def test_scene_id_is_unique_after_delete_then_add(client, session) -> None:
    """删一场再加一场：新场不得与幸存场撞 scene_id。

    旧铸造规则下这里会得到两个 ``…_CH01_SC04``，物化时静默丢掉其中一场。
    """
    project_id = _create_project(client, "collision")

    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row(f"S{i:02d}", i, f"事件{i}") for i in range(1, 5)]})
    session.expire_all()
    assert len(_active_plans(session, project_id)) == 4

    # 删掉第 3 场（前端重排后 scene_seq 按索引重算）
    kept = [_scene_row("S01", 1, "事件1"), _scene_row("S02", 2, "事件2"), _scene_row("S04", 3, "事件4")]
    _patch_step(client, project_id, "scene_list", {"scenes": kept})

    # 末尾新增一场
    _patch_step(client, project_id, "scene_list", {"scenes": [*kept, _scene_row("S05", 4, "新加的一场")]})

    session.expire_all()
    plans = _active_plans(session, project_id)
    scene_ids = [plan.scene_id for plan in plans]
    assert len(scene_ids) == len(set(scene_ids)), f"scene_id 撞号：{sorted(scene_ids)}"
    # 身份锚是 row_uid，不再嵌章号/序号
    for plan in plans:
        assert plan.scene_id == f"{project_id}_SC_{plan.row_uid}"
    assert {plan.summary for plan in plans} == {"事件1", "事件2", "事件4", "新加的一场"}


def test_duplicate_row_uid_in_one_payload_does_not_overwrite_a_live_scene(client, session) -> None:
    """同一份 payload 里重号的 row_uid 必须拆成两行，不能让后者覆盖前者。

    前端 addScene 曾用 ``"S" + (list.length + 1)`` 编号，删掉中间一场后新增就会
    铸出与幸存场同号的 id —— 这里直接喂那个坏形状，验证后端自己也扛得住。
    """
    project_id = _create_project(client, "dup-uid")

    _patch_step(
        client,
        project_id,
        "scene_list",
        {"scenes": [_scene_row("S01", 1, "原有第一场"), _scene_row("S02", 2, "原有第二场")]},
    )
    session.expire_all()
    assert len(_active_plans(session, project_id)) == 2

    # 第三行复用了 S02（坏形状）——它是一场新戏，不是对 S02 的编辑
    _patch_step(
        client,
        project_id,
        "scene_list",
        {
            "scenes": [
                _scene_row("S01", 1, "原有第一场"),
                _scene_row("S02", 2, "原有第二场"),
                _scene_row("S02", 3, "另起的一场"),
            ]
        },
    )

    session.expire_all()
    plans = _active_plans(session, project_id)
    assert {plan.summary for plan in plans} == {"原有第一场", "原有第二场", "另起的一场"}, "重号把幸存场覆盖掉了"
    row_uids = [plan.row_uid for plan in plans]
    assert len(row_uids) == len(set(row_uids))


def test_no_scene_is_lost_between_scene_plans_and_scene_cards(client, session) -> None:
    """端到端守卫：活跃场景计划数 == 物化后的场景卡数。

    这是撞号缺陷唯一真正致命的后果——作者写好的一场在物化时无声消失。
    """
    project_id = _create_project(client, "no-loss")
    _approve_upstream(client, project_id)

    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row(f"S{i:02d}", i, f"事件{i}") for i in range(1, 5)]})
    kept = [_scene_row("S01", 1, "事件1"), _scene_row("S02", 2, "事件2"), _scene_row("S04", 3, "事件4")]
    _patch_step(client, project_id, "scene_list", {"scenes": kept})
    _patch_step(client, project_id, "scene_list", {"scenes": [*kept, _scene_row("S05", 4, "新加的一场")]})
    _approve_step(client, project_id, "scene_list")

    _patch_step(
        client,
        project_id,
        "scene_details",
        {
            "scenes": [
                _detail_row("S01", 1, "事件1"),
                _detail_row("S02", 2, "事件2"),
                _detail_row("S04", 3, "事件4"),
                _detail_row("S05", 4, "新加的一场"),
            ]
        },
    )
    _approve_step(client, project_id, "scene_details")

    session.expire_all()
    plans = _active_plans(session, project_id)
    assert len(plans) == 4

    triage = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/scene-triage",
        json={"items": [{"scene_plan_id": plan.scene_plan_id, "status": "pass"} for plan in plans]},
    )
    assert triage.status_code == 200, triage.text

    materialize = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
        json={"strategy": "even"},
        headers={"X-Idempotency-Key": "identity-materialize"},
    )
    assert materialize.status_code == 200, materialize.text

    approve = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "identity-outline-approve"},
    )
    assert approve.status_code == 200, approve.text

    session.expire_all()
    cards = list(
        session.execute(select(SceneCard).where(SceneCard.project_id == project_id)).scalars()
    )
    assert len(cards) == len(plans), "构思侧的场没有一一落成场景卡"
    assert {card.scene_goal for card in cards} == {"事件1", "事件2", "事件4", "新加的一场"}


# --------------------------------------------------------------------- P1-3 幽灵场


def test_removed_scene_disappears_from_the_workspace(client, session) -> None:
    """作者删掉的场：软删、从工作台读路径消失、行仍在库里可审计。"""
    project_id = _create_project(client, "ghost-workspace")

    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row(f"S{i:02d}", i, f"事件{i}") for i in range(1, 4)]})
    _patch_step(
        client,
        project_id,
        "scene_list",
        {"scenes": [_scene_row("S01", 1, "事件1"), _scene_row("S03", 2, "事件3")]},
    )

    session.expire_all()
    assert {plan.summary for plan in _active_plans(session, project_id)} == {"事件1", "事件3"}
    # 软删不物删：行还在，带着可审计的时间戳
    removed = [plan for plan in _all_plans(session, project_id) if plan.removed_at]
    assert [plan.summary for plan in removed] == ["事件2"]
    assert removed[0].removed_at

    logs = list(
        session.execute(
            select(OperationLog).where(OperationLog.event_type == "snowflake_scene_plan_removed")
        ).scalars()
    )
    assert [log.payload_json["title"] for log in logs] == ["事件2"]

    workspace = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace").json()["data"]
    board_titles = {scene["summary"] for scene in workspace["scene_board"]["scenes"]}
    assert board_titles == {"事件1", "事件3"}


def test_removed_scene_no_longer_blocks_the_materialization_gate(client, session) -> None:
    """回归：被删的场曾以 rewrite 诊断永久堵死物化闸门，而作者看不见它。"""
    project_id = _create_project(client, "ghost-gate")
    _approve_upstream(client, project_id)

    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row(f"S{i:02d}", i, f"事件{i}") for i in range(1, 4)]})
    kept = [_scene_row("S01", 1, "事件1"), _scene_row("S03", 2, "事件3")]
    _patch_step(client, project_id, "scene_list", {"scenes": kept})
    _approve_step(client, project_id, "scene_list")

    _patch_step(
        client,
        project_id,
        "scene_details",
        {"scenes": [_detail_row("S01", 1, "事件1"), _detail_row("S03", 2, "事件3")]},
    )
    _approve_step(client, project_id, "scene_details")

    workspace = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace").json()["data"]
    gate = workspace["materialization_gate"]
    assert all("事件2" not in blocker for blocker in gate["blockers"]), gate["blockers"]
    assert all("事件2" not in warning for warning in gate["warnings"]), gate["warnings"]
    assert {item["scene_id"] for item in workspace["triage_items"]}.isdisjoint(
        {plan.scene_id for plan in _all_plans(session, project_id) if plan.removed_at}
    )

    session.expire_all()
    plans = _active_plans(session, project_id)
    triage = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/scene-triage",
        json={"items": [{"scene_plan_id": plan.scene_plan_id, "status": "pass"} for plan in plans]},
    )
    assert triage.status_code == 200, triage.text

    materialize = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
        json={"strategy": "even"},
        headers={"X-Idempotency-Key": "ghost-gate-materialize"},
    )
    assert materialize.status_code == 200, materialize.text
    scene_goals = [
        scene["scene_goal"]
        for chapter in materialize.json()["data"]["plan"]["plan_json"]["chapters"]
        for scene in chapter["scenes"]
    ]
    assert "事件2" not in scene_goals, "被删的场混进了物化输出"


def test_readding_a_removed_scene_revives_it(client, session) -> None:
    """作者反悔：同一 row_uid 又出现在列表里 → 复活，而不是撞唯一索引报错。"""
    project_id = _create_project(client, "revive")

    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row("S01", 1, "事件1"), _scene_row("S02", 2, "事件2")]})
    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row("S01", 1, "事件1")]})
    session.expire_all()
    assert len(_active_plans(session, project_id)) == 1

    _patch_step(
        client,
        project_id,
        "scene_list",
        {"scenes": [_scene_row("S01", 1, "事件1"), _scene_row("S02", 2, "事件2 改")]},
    )
    session.expire_all()
    plans = _active_plans(session, project_id)
    assert {plan.summary for plan in plans} == {"事件1", "事件2 改"}
    revived = next(plan for plan in plans if plan.row_uid == "S02")
    assert revived.removed_at is None and revived.removed_by is None
    # 复活的是同一行，不是新建的一行
    assert len(_all_plans(session, project_id)) == 2


def test_materialized_scene_removed_upstream_is_flagged_not_deleted(client, session) -> None:
    """已落库的场被从构思删掉：只打 orphaned 标记，绝不静默删（那边可能已有正文）。"""
    project_id = _create_project(client, "orphan")
    _approve_upstream(client, project_id)

    scenes = [_scene_row("S01", 1, "事件1"), _scene_row("S02", 2, "事件2")]
    _patch_step(client, project_id, "scene_list", {"scenes": scenes})
    _approve_step(client, project_id, "scene_list")
    _patch_step(
        client,
        project_id,
        "scene_details",
        {"scenes": [_detail_row("S01", 1, "事件1"), _detail_row("S02", 2, "事件2")]},
    )
    _approve_step(client, project_id, "scene_details")

    session.expire_all()
    plans = _active_plans(session, project_id)
    client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/scene-triage",
        json={"items": [{"scene_plan_id": plan.scene_plan_id, "status": "pass"} for plan in plans]},
    )
    assert (
        client.post(
            f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
            json={"strategy": "even"},
            headers={"X-Idempotency-Key": "orphan-materialize"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
            json={},
            headers={"X-Idempotency-Key": "orphan-outline-approve"},
        ).status_code
        == 200
    )

    session.expire_all()
    materialized = next(plan for plan in _active_plans(session, project_id) if plan.row_uid == "S02")
    assert session.get(SceneCard, materialized.scene_id) is not None

    # 现在把它从构思的场景列表里删掉
    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row("S01", 1, "事件1")]})

    session.expire_all()
    plan = session.get(SnowflakeScenePlan, materialized.scene_plan_id)
    assert plan.removed_at is None, "已物化的场不能软删——目录侧可能已有正文"
    assert plan.orphaned_flag == 1
    assert session.get(SceneCard, materialized.scene_id) is not None
    logs = list(
        session.execute(
            select(OperationLog).where(OperationLog.event_type == "snowflake_scene_plan_orphaned")
        ).scalars()
    )
    assert len(logs) == 1 and logs[0].payload_json["scene_id"] == materialized.scene_id


def test_empty_scene_list_draft_does_not_wipe_the_book(client, session) -> None:
    """护栏：一次空草稿（LLM 空返回 / 前端竞态）绝不能清空整本书的场。"""
    project_id = _create_project(client, "empty-guard")

    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row(f"S{i:02d}", i, f"事件{i}") for i in range(1, 4)]})
    session.expire_all()
    assert len(_active_plans(session, project_id)) == 3

    _patch_step(client, project_id, "scene_list", {"scenes": []})
    session.expire_all()
    assert len(_active_plans(session, project_id)) == 3, "空草稿把全书的场清掉了"


def test_scene_details_step_never_removes_scenes(client, session) -> None:
    """收口只属于第 9 步。第 10 步若因 LLM 截断少返回几场，不得删掉作者的场。"""
    project_id = _create_project(client, "details-no-remove")

    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row(f"S{i:02d}", i, f"事件{i}") for i in range(1, 4)]})
    session.expire_all()
    assert len(_active_plans(session, project_id)) == 3

    # 第 10 步只深化了三场里的一场
    _patch_step(client, project_id, "scene_details", {"scenes": [_detail_row("S01", 1, "事件1")]})
    session.expire_all()
    plans = _active_plans(session, project_id)
    assert len(plans) == 3, "第 10 步的部分草稿删掉了场"
    assert {plan.summary for plan in plans} == {"事件1", "事件2", "事件3"}


def test_chapter_goal_rows_are_not_touched_by_removal(client, session) -> None:
    """收口只动场景计划，不碰目录侧的章。"""
    project_id = _create_project(client, "chapters-untouched")
    _approve_upstream(client, project_id)

    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row("S01", 1, "事件1"), _scene_row("S02", 2, "事件2")]})
    _approve_step(client, project_id, "scene_list")
    _patch_step(
        client,
        project_id,
        "scene_details",
        {"scenes": [_detail_row("S01", 1, "事件1"), _detail_row("S02", 2, "事件2")]},
    )
    _approve_step(client, project_id, "scene_details")

    session.expire_all()
    plans = _active_plans(session, project_id)
    client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/scene-triage",
        json={"items": [{"scene_plan_id": plan.scene_plan_id, "status": "pass"} for plan in plans]},
    )
    client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
        json={"strategy": "even"},
        headers={"X-Idempotency-Key": "chapters-untouched-materialize"},
    )
    client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "chapters-untouched-approve"},
    )

    session.expire_all()
    before = {row.chapter_id for row in session.execute(select(ChapterGoal).where(ChapterGoal.project_id == project_id)).scalars()}
    _patch_step(client, project_id, "scene_list", {"scenes": [_scene_row("S01", 1, "事件1")]})
    session.expire_all()
    after = {row.chapter_id for row in session.execute(select(ChapterGoal).where(ChapterGoal.project_id == project_id)).scalars()}
    assert before == after
