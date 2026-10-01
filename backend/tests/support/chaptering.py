"""分章 / 物化测试共用的造书与确认助手（B07-21：以前各测试文件互相 import 对方的私有助手）。

两种造法：``seed_chaptering`` 经 API 把 01–10 步写满并确认（12 场、三处灾难标记），``seed_story_order`` /
``seed_after_scenes`` 直接经服务写 07 与 09（17 / 12 场的真实形状）。``preview_from_scenes`` →
``confirm_chaptering`` 把一份按场景提议的分章确认写入目录。
"""

from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import SnowflakeChapterPlan, StoryProject
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService


# ---------------------------------------------------------------- 经 API 编一本 12 场、六章的书（test_snowflake_chaptering）


def create_chaptering_project(client, key: str) -> str:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": "Rain City Signal",
            "genre": "Urban Mystery",
            "target_chapter_count": 6,
            "target_word_count": 120000,
            "outline_text": "旧信把她拉回雨城。\n悬案与家族纠缠。\n她必须决定真相值不值得。",
        },
        headers={"X-Idempotency-Key": f"chp-create-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def patch_step(client, project_id: str, step_key: str, draft: dict) -> dict:
    response = client.patch(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}",
        json={"draft": draft, "force": True},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def approve_step(client, project_id: str, step_key: str) -> None:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve", json={}
    )
    assert response.status_code == 200, response.text


#: 07 长篇大纲：作者真的编出来的六章（结构化 chapters —— P2 的新契约）
CHAPTERS = [
    {"row_uid": "", "chapter_seq": 1, "act": 1, "title": "雨夜来信", "summary": "一封旧信把她拉回雨城。", "spine": "", "chapter_goal": "让她无法不回去"},
    {"row_uid": "", "chapter_seq": 2, "act": 1, "title": "旧案卷宗", "summary": "她翻出封存的案卷。", "spine": "", "chapter_goal": "把旧案摆上台面"},
    {"row_uid": "", "chapter_seq": 3, "act": 1, "title": "被迫卷入", "summary": "她被停职。", "spine": "灾一", "chapter_goal": "断掉退路"},
    {"row_uid": "", "chapter_seq": 4, "act": 2, "title": "父亲的谎", "summary": "父亲的时间线对不上。", "spine": "", "chapter_goal": "把矛头转向家里"},
    {"row_uid": "", "chapter_seq": 5, "act": 2, "title": "世界观碎", "summary": "她发现父亲在场。", "spine": "灾二", "chapter_goal": "打碎她的信念"},
    {"row_uid": "", "chapter_seq": 6, "act": 3, "title": "余波", "summary": "代价落地。", "spine": "灾三", "chapter_goal": "让代价可见"},
]


UPSTREAM = {
    "book_brief": {"category": "都市悬疑", "target_reader": "25-35 岁读者", "delight_reason": "抽丝剥茧",
                   "story_kind": "长篇", "genre_promise": "不写甜宠", "expected_reader_emotion": "紧张"},
    "one_sentence_summary": {"summary": "一位记者必须查清旧案，但真凶是她的父亲。"},
    "one_paragraph_summary": {"sentences": ["回到雨城", "灾一：被迫卷入", "灾二：世界观被打碎", "灾三：局势失控", "决战与收尾"],
                              "moral_premise": "真相高于安稳"},
    "character_sheets": {"characters": [{"character_id": "c1", "display_name": "林昭", "role": "主角", "goal": "查清旧案",
                                         "ambition": "自由", "values": ["诚实"], "conflict": "家族", "epiphany": "真相有代价"}]},
    "short_synopsis": {"paragraphs": ["铺垫", "灾一", "灾二", "灾三", "收尾"]},
    "character_synopses": {"characters": [{"character_id": "c1", "display_name": "林昭", "role": "主角",
                                           "synopsis": "信念：真相\n旧伤：母亲之死"}]},
    "long_synopsis": {"paragraphs": ["", "", "", ""], "chapters": CHAPTERS},
    "character_bibles": {"characters": [{"character_id": "c1", "display_name": "林昭", "role": "主角",
                                         "physical_profile": {"appearance": "瘦"},
                                         "personality_profile": {"strongest_trait": "固执"},
                                         "environment_profile": {"home": "雨城"},
                                         "psychological_profile": {"philosophy": "真相", "self_image": "逃兵",
                                                                   "deepest_fear": "重蹈覆辙"}}]},
}


def scene_row(uid: str, seq: int, text: str, spine: str = "") -> dict:
    return {"row_uid": uid, "scene_seq": seq, "summary": text, "primary_form": "proactive",
            "pov_character_id": "c1", "location": "雨城", "crucible": "她不能就这样走开",
            "chapter_role": "推进", "spine": spine}


def detail_row(uid: str, seq: int, text: str, spine: str = "") -> dict:
    return {"row_uid": uid, "scene_seq": seq, "title": text, "summary": text, "primary_form": "proactive",
            "location": "雨城", "crucible": "她不能就这样走开", "scene_crucible": "她不能就这样走开",
            "pov_character_id": "c1", "spine": spine,
            "goal": f"{text}·目标", "conflict": f"{text}·冲突", "setback": f"{text}·挫败",
            "cost_requirement": f"{text}·代价"}


#: 12 场，其中三场带脊柱标记，用来锚定灾一/灾二/灾三 三章
SPINE_AT = {3: "灾一", 7: "灾二", 12: "灾三"}


def seed_chaptering(client, project_id: str) -> None:
    for step_key, draft in UPSTREAM.items():
        patch_step(client, project_id, step_key, draft)
        approve_step(client, project_id, step_key)
    scenes = [scene_row(f"S{i:02d}", i, f"事件{i}", SPINE_AT.get(i, "")) for i in range(1, 13)]
    patch_step(client, project_id, "scene_list", {"scenes": scenes})
    approve_step(client, project_id, "scene_list")
    patch_step(client, project_id, "scene_details",
               {"scenes": [detail_row(f"S{i:02d}", i, f"事件{i}", SPINE_AT.get(i, "")) for i in range(1, 13)]})
    approve_step(client, project_id, "scene_details")


def pass_triage(client, project_id: str) -> None:
    workspace = client.get(f"/api/v2/projects/{project_id}/snowflake-workspace").json()["data"]
    items = [{"scene_plan_id": item["scene_plan_id"], "status": "pass"} for item in workspace["triage_items"]]
    if items:
        assert client.post(
            f"/api/v2/projects/{project_id}/snowflake-workspace/scene-triage", json={"items": items}
        ).status_code == 200


# ---------------------------------------------------------------- 直接经服务编 17 场的真实形状（test_snowflake_chaptering_story_order）


STORY_ORDER_PROJECT_ID = "prj-story-order"

#: 真实项目的形状：17 场，灾难只写在功能标签里（spine 列是空的）
STORY_ORDER_ROLES = {5: "灾难一·一幕高潮", 9: "中点逆转/道德抉择", 12: "灾难二·二幕高潮", 15: "灾难三·三幕高潮"}


def story_order_rows(count: int = 17, *, roles: dict[int, str] | None = None) -> list[dict]:
    marks = STORY_ORDER_ROLES if roles is None else roles
    return [
        {
            "row_uid": f"u{index:02d}",
            "scene_seq": index,  # 前端 09 发的是全书序 i + 1
            "summary": f"第 {index} 场",
            "primary_form": "proactive",
            "scene_type": "proactive",
            "location": "林场",
            "crucible": "退不出的困局",
            "pov_character_id": "c1",
            "chapter_role": marks.get(index, "推进"),
            "spine": "",
        }
        for index in range(1, count + 1)
    ]


def seed_story_order(session, *, chapters: list[dict] | None = None, count: int = 17) -> SnowflakeWorkspaceService:
    session.add(
        StoryProject(
            project_id=STORY_ORDER_PROJECT_ID,
            title="何来",
            outline_text="大纲",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
        )
    )
    session.flush()
    service = SnowflakeWorkspaceService(session)
    service.update_step(
        STORY_ORDER_PROJECT_ID, "long_synopsis", {"draft": {"paragraphs": ["一", "二", "三", "四", "五"], "chapters": chapters or []}}
    )
    service.update_step(STORY_ORDER_PROJECT_ID, "scene_list", {"draft": {"scenes": story_order_rows(count)}})
    return service


def preview_uids(preview: dict) -> list[list[str]]:
    return [[scene["row_uid"] for scene in chapter["scenes"]] for chapter in preview["chapters"]]


def confirm_payload(preview: dict) -> dict:
    return {
        "replace_chapters": True,
        "chapters": [
            {"row_uid": c["row_uid"], "title": c["title"], "act": c["act"], "spine": c["spine"],
             "chapter_goal": c["chapter_goal"], "summary": c["summary"]}
            for c in preview["chapters"]
        ],
        "assignments": [
            {"scene_plan_id": s["scene_plan_id"], "chapter_row_uid": c["row_uid"]}
            for c in preview["chapters"] for s in c["scenes"]
        ],
    }


def confirm_chaptering(client, project_id: str, preview: dict, key: str) -> dict:
    materialize = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/materialize",
        json=confirm_payload(preview), headers={"X-Idempotency-Key": f"{key}-mat"},
    )
    assert materialize.status_code == 200, materialize.text
    approve = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/outline/approve",
        json={}, headers={"X-Idempotency-Key": f"{key}-approve"},
    )
    assert approve.status_code == 200, approve.text
    return approve.json()["data"]


# ---------------------------------------------------------------- 分章预览 → 确认写入 → 目录（test_catalog_book_spine）


def workspace_base(project_id: str) -> str:
    return f"/api/v2/projects/{project_id}/snowflake-workspace"


def preview_from_scenes(client, project_id: str, **body) -> dict:
    response = client.post(f"{workspace_base(project_id)}/chapter-plan/preview", json={"strategy": "from_scenes", **body})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def catalog_chapters(client, project_id: str) -> list[dict]:
    response = client.get(f"/api/v2/projects/{project_id}/catalog")
    assert response.status_code == 200, response.text
    return response.json()["data"]["chapters"]


def materialized_project(client, key: str, **preview_body) -> str:
    project_id = create_chaptering_project(client, key)
    seed_chaptering(client, project_id)
    pass_triage(client, project_id)
    confirm_chaptering(client, project_id, preview_from_scenes(client, project_id, **preview_body), key)
    return project_id


# ---------------------------------------------------------------- 先列场、后分章（test_snowflake_chapters_after_scenes）


AFTER_SCENES_PROJECT_ID = "prj-chapters"


def after_scenes_rows(count: int, spine_at: dict[int, str]) -> list[dict]:
    return [
        {
            "row_uid": f"u{index:02d}",
            "scene_seq": index,
            "summary": f"第 {index} 场",
            "primary_form": "proactive",
            "scene_type": "proactive",
            "location": "雨城",
            "crucible": "退不出的困局",
            "pov_character_id": "c1",
            "chapter_role": "推进",
            "spine": spine_at.get(index, ""),
        }
        for index in range(1, count + 1)
    ]


def seed_after_scenes(session, count: int = 12, spine_at: dict[int, str] | None = None, target_chapter_count: int = 0) -> SnowflakeWorkspaceService:
    session.add(
        StoryProject(
            project_id=AFTER_SCENES_PROJECT_ID,
            title="章在场景之后",
            outline_text="大纲",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
            target_chapter_count=target_chapter_count,
        )
    )
    session.flush()
    service = SnowflakeWorkspaceService(session)
    service.update_step(AFTER_SCENES_PROJECT_ID, "long_synopsis", {"draft": {"paragraphs": ["一", "二", "三", "四", "五"], "chapters": []}})
    service.update_step(AFTER_SCENES_PROJECT_ID, "scene_list", {"draft": {"scenes": after_scenes_rows(count, spine_at or {4: "灾一", 8: "灾二", 11: "灾三"})}})
    return service


def live_chapter_plans(session) -> list[SnowflakeChapterPlan]:
    return list(
        session.execute(
            select(SnowflakeChapterPlan)
            .where(SnowflakeChapterPlan.project_id == AFTER_SCENES_PROJECT_ID, SnowflakeChapterPlan.removed_at.is_(None))
            .order_by(SnowflakeChapterPlan.chapter_seq)
        ).scalars().all()
    )
