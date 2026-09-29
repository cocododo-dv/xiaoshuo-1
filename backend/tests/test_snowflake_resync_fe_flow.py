"""React 主线 SnowSync.resync 调用序列的端到端守卫（FE 回流补接）。

前端序列：PATCH steps/scene_details（10 步保存上行，force）→ GET workspace
（resync_status 是构思页回流横幅的数据源）→ POST /resync 空 body 全量同步
→ 回包 workspace 清零 + SceneCard 的 brief 三拍换新（写作台/AI 起草台经 catalog 可见）。

同时守住假阳性回归：刚物化完 pending_count 必须为 0——物化与 resync 两个写入方
对 writer_brief_json 的出处/富化键写法天生不同（source、outline_plan_id vs
scene_plan_id、primary_form、chapter_goal…），pending 检测只比较戏剧内容键
（_writer_brief_comparable 白名单），否则横幅在物化当刻就会喊「N 场待同步」。
"""
from sqlalchemy import select

from novel_system.db.models import SceneCard, SnowflakeScenePlan


import pytest as _pytest_sk
from tests.real_llm_fakes import install_skeleton_snowflake as _install_skeleton_snowflake


@_pytest_sk.fixture(autouse=True)
def _auto_skeleton_snowflake(monkeypatch):
    """假生成已退役：雪花 generate_step 走规划器骨架直通（仅回归物化/失效/收口链路）。"""
    _install_skeleton_snowflake(monkeypatch)



def _create_project(client, *, key: str) -> dict:
    response = client.post(
        "/api/v2/projects",
        json={
            "title": "Rain City Signal",
            "genre": "Urban Mystery",
            "target_chapter_count": 2,
            "target_word_count": 120000,
            "outline_text": (
                "An old letter pulls the heroine back to Rain City.\n"
                "The cold case turns out to be tied to her family.\n"
                "She must decide whether the truth is worth the cost."
            ),
        },
        headers={"X-Idempotency-Key": f"create-v2-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]


def _approve_generated_step(client, project_id: str, step_key: str) -> None:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/generate",
        json={},
        headers={"X-Idempotency-Key": f"generate-v2-{project_id}-{step_key}"},
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/steps/{step_key}/approve",
        json={},
        headers={"X-Idempotency-Key": f"approve-v2-{project_id}-{step_key}"},
    )
    assert response.status_code == 200, response.text

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


def test_fe_resync_sequence_end_to_end(client, session) -> None:
    project = _create_project(client, key="fe-resync-seq")
    pid = project["project_id"]
    for step_key in ALL_STEPS:
        _approve_generated_step(client, pid, step_key)

    r = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "fe-resync-m"},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "fe-resync-a"},
    )
    assert r.status_code == 200, r.text

    # 物化刚完成：无待同步
    ws = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    assert ws["resync_status"]["pending_count"] == 0

    # 模拟 FE 在 10 步改第一场的 goal 后自动保存（snowPushKey 的 PATCH，force=True）
    step = next(s for s in ws["steps"] if s["step_key"] == "scene_details")
    draft = dict(step["draft"])
    scenes = [dict(s) for s in draft.get("scenes") or []]
    assert scenes, "生成的 scene_details 应有场景"
    scenes[0] = {**scenes[0], "goal": "改后的目标：夜里回馆核对档案"}
    draft["scenes"] = scenes
    r = client.patch(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details",
        json={"draft": draft, "force": True},
    )
    assert r.status_code == 200, r.text

    # FE 强制重拉工作台 → 横幅数据源显示待同步（goal 的改动落在 brief 的戏剧键里）
    ws = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    assert ws["resync_status"]["pending_count"] == 1, ws["resync_status"]
    pending = ws["resync_status"]["pending_scenes"]
    assert any("writer_brief_json" in (p.get("changed_fields") or []) for p in pending), pending

    # FE 一键同步：空 body 全量 resync；回包自带清零后的 workspace
    r = client.post(f"/api/v2/projects/{pid}/snowflake-workspace/resync", json={})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["dry_run"] is False
    assert any(item["synced"] for item in data["results"]), data["results"]
    assert data["workspace"]["resync_status"]["pending_count"] == 0

    # 目录场景卡三拍换新（写作台 / AI 起草台经 catalog 读到的 brief 就是它）
    session.expire_all()
    cards = session.execute(
        select(SceneCard).where(SceneCard.project_id == pid)
    ).scalars().all()
    assert any(
        (card.writer_brief_json or {}).get("goal") == "改后的目标：夜里回馆核对档案"
        for card in cards
    ), "resync 后应有场景卡的 brief 拿到改后的 goal"

    plan = session.execute(
        select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == pid)
    ).scalars().first()
    assert plan is not None


def test_fe_resync_reactive_scene_beats_fallback_keeps_reaction_and_dilemma(client, session) -> None:
    """回归守护：_scene_card_resync_patch 曾经用「主动场景专属」的
    goal/conflict/setback 兜底重建 beats_json，反应场景 resync 时会把
    reaction/dilemma 整体丢掉、只剩 decision/hook。这里验证反应场景在
    beats_json 为空时 resync，兜底结果里 reaction 和 dilemma 都还在。"""
    project = _create_project(client, key="fe-resync-reactive-beats")
    pid = project["project_id"]
    for step_key in ALL_STEPS:
        _approve_generated_step(client, pid, step_key)

    r = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/materialize",
        json={},
        headers={"X-Idempotency-Key": "fe-resync-reactive-m"},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/outline/approve",
        json={},
        headers={"X-Idempotency-Key": "fe-resync-reactive-a"},
    )
    assert r.status_code == 200, r.text

    session.expire_all()
    reactive_plan = session.execute(
        select(SnowflakeScenePlan).where(
            SnowflakeScenePlan.project_id == pid,
            SnowflakeScenePlan.scene_type == "reactive",
        )
    ).scalars().first()
    assert reactive_plan is not None, "生成的场景规划应至少有一个反应场景"
    assert (reactive_plan.reaction or "").strip()
    assert (reactive_plan.dilemma or "").strip()

    # 清空这个反应场景的 beats_json，模拟"作者手工加场/清过节拍、从未再填"的情形。
    # 作者改一场走第 10 步的草稿（R15a 删掉了逐场的 PATCH …/scenes/{id}）
    workspace = client.get(f"/api/v2/projects/{pid}/snowflake-workspace").json()["data"]
    rows = [dict(row) for row in next(s for s in workspace["steps"] if s["step_key"] == "scene_details")["draft"]["scenes"]]
    for row in rows:
        if row["scene_plan_id"] == reactive_plan.scene_plan_id:
            row["beats_json"] = []
    clear_response = client.patch(
        f"/api/v2/projects/{pid}/snowflake-workspace/steps/scene_details",
        json={"draft": {"scenes": rows}},
    )
    assert clear_response.status_code == 200, clear_response.text

    resync_response = client.post(
        f"/api/v2/projects/{pid}/snowflake-workspace/resync",
        json={"scene_plan_ids": [reactive_plan.scene_plan_id]},
    )
    assert resync_response.status_code == 200, resync_response.text
    assert resync_response.json()["data"]["results"][0]["synced"] is True

    session.expire_all()
    scene_card = session.get(SceneCard, reactive_plan.scene_id)
    assert scene_card is not None
    beats = list(scene_card.beats_json or [])
    assert reactive_plan.reaction in beats, beats
    assert reactive_plan.dilemma in beats, beats
