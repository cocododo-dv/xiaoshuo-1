"""章节编排 LLM 规划（chapter plan）：蓝图三端点 / fill / candidates / review / apply。

覆盖设计文档 docs/chapter-arrangement-llm-design-2026-07-16.md 的验收：
- 蓝图显式化：作者 PUT 的蓝图被场景 run 的 ensure_scene_planning 复用（P1 验收）
- 补丁性质：只填空、不覆盖非空、未知场丢弃、追加有上限（P2）
- apply 幂等重放、锁章 409
- LLM 未配置：author_action 降级，不落占位蓝图
- candidates / review 输出归一（无据 finding 丢弃、坏 ref 置空）
"""
from __future__ import annotations

import json

from novel_system.db.models import ChapterGoal, GenerationPlanningArtifact, LlmCall, SceneCard, StoryProject
from novel_system.services.chapter_plan_llm import sanitize_plan_patch
from novel_system.services.llm_client import LLMResponse
from tests.accounted_llm_fakes import accounted_generate_method


import pytest as _pytest_ap
from tests.real_llm_fakes import install_online_pipeline as _install_online_pipeline


@_pytest_ap.fixture(autouse=True)
def _auto_online_pipeline(monkeypatch):
    """假生成已退役：给场景管线未显式注入的子服务兜底在线记账替身。"""
    _install_online_pipeline(monkeypatch)


_seq = 0


def _key(prefix: str = "chapter-plan") -> str:
    global _seq
    _seq += 1
    return f"{prefix}-{_seq}"


def _create_project(client) -> str:
    response = client.post(
        "/api/v2/projects",
        json={"title": f"编排规划 {_key('t')}", "outline_text": "大纲", "genre": "悬疑"},
        headers={"X-Idempotency-Key": _key("project")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["project"]["project_id"]


def _create_chapter(client, pid: str, title: str = "第一章") -> dict:
    response = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters",
        json={"title": title},
        headers={"X-Idempotency-Key": _key("chapter")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["chapter"]


def _fake_llm(captured: list, payload: dict):
    def fake_generate(self, request):  # noqa: ANN001
        captured.append(request)
        return LLMResponse(
            request_id="resp_chapter_plan",
            provider="fake-provider",
            model=request.model,
            text=json.dumps(payload, ensure_ascii=False),
            structured_output=payload,
            response_format="json_object",
            raw_response={"id": "resp_chapter_plan"},
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            finish_reason="stop",
        )

    return accounted_generate_method(fake_generate)


def _approve_chapter(session, project_id: str, chapter_id: str) -> None:
    project = session.get(StoryProject, project_id)
    chapter = session.get(ChapterGoal, chapter_id)
    assert project is not None and chapter is not None
    approved = list(project.approved_chapter_ids_json or [])
    if chapter_id not in approved:
        approved.append(chapter_id)
    project.approved_chapter_ids_json = approved
    chapter.state = "approved"
    session.commit()


_ARCH_PAYLOAD = {
    "chapter_promise": "读者看到线索反噬提问者本人",
    "escalation_path": ["新的证物指向家人", "证词把她推向公开对峙"],
    "reveal_plan": ["旧工牌的名字被磨掉"],
    "payoff_target": "她烧掉了自己写的第一封信",
    "character_shift": "从旁观取证到亲手介入",
    "ending_question": "她还能相信自己整理的档案吗",
}


# ---------- 蓝图三端点（P1） ----------


def test_architecture_put_get_supersede_and_scene_planning_reuse(client, session) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]

    empty = client.get(f"/api/v2/projects/{pid}/catalog/chapters/{chid}/architecture")
    assert empty.status_code == 200, empty.text
    assert empty.json()["data"]["architecture"] is None

    put1 = client.put(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/architecture",
        json=_ARCH_PAYLOAD,
    )
    assert put1.status_code == 200, put1.text
    arch1 = put1.json()["data"]["architecture"]
    assert arch1["payload"]["chapter_promise"] == _ARCH_PAYLOAD["chapter_promise"]
    assert arch1["llm_call_id"] is None

    got = client.get(f"/api/v2/projects/{pid}/catalog/chapters/{chid}/architecture")
    assert got.json()["data"]["architecture"]["row_id"] == arch1["row_id"]

    # P1 验收：场景 run 的 planning 段复用作者版蓝图，不再懒生成覆盖。
    from novel_system.services.near_final import NearFinalPlanningService

    scene_id = chapter["scenes"][0]["scene_id"]
    planning = NearFinalPlanningService(session).ensure_scene_planning(scene_id)
    session.commit()  # 生产路径由 orchestrator 提交；测试里释放 SQLite 写锁
    assert planning["chapter_architecture"]["row_id"] == arch1["row_id"]
    assert planning["chapter_architecture"]["payload"]["chapter_promise"] == _ARCH_PAYLOAD["chapter_promise"]

    # 再次 PUT：新行 active、旧行 superseded。
    put2 = client.put(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/architecture",
        json={**_ARCH_PAYLOAD, "chapter_promise": "改写后的承诺"},
    )
    arch2 = put2.json()["data"]["architecture"]
    assert arch2["row_id"] != arch1["row_id"]
    old = session.get(GenerationPlanningArtifact, arch1["row_id"])
    session.refresh(old)
    assert old.status == "superseded"


def test_architecture_put_requires_promise(client) -> None:
    pid = _create_project(client)
    chid = _create_chapter(client, pid)["chapter_id"]
    bad = client.put(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/architecture",
        json={"chapter_promise": ""},
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "CHAPTER_ARCHITECTURE_PROMISE_REQUIRED"


def test_design_or_binding_changes_keep_the_authors_blueprint_and_say_so(client, session) -> None:
    """B07-03：作者在章节编排里亲手写的蓝图，不因构思改动 / 换参考书被作废（下一次运行也不会拿 AI 版顶替它）；
    AI 做的蓝图照旧作废重做。留下来的作者版带一个「设计在它之后改过」的提示。"""
    from novel_system.services.scene_planning_staleness import (
        supersede_for_binding_scope,
        supersede_scene_planning_artifacts,
    )

    pid = _create_project(client)
    authored = _create_chapter(client, pid, "旧信")
    generated = _create_chapter(client, pid, "案卷")
    put = client.put(f"/api/v2/projects/{pid}/catalog/chapters/{authored['chapter_id']}/architecture", json=_ARCH_PAYLOAD)
    assert put.status_code == 200, put.text
    author_row_id = put.json()["data"]["architecture"]["row_id"]
    session.add(
        GenerationPlanningArtifact(
            row_id="planning_ai_architecture",
            artifact_type="chapter_story_architecture",
            object_type="chapter",
            object_id=generated["chapter_id"],
            chapter_id=generated["chapter_id"],
            payload_json=dict(_ARCH_PAYLOAD),
            llm_call_id="llm_call_ai_architecture",
            status="active",
            created_by="near_final_planning",
        )
    )
    session.commit()
    assert client.get(
        f"/api/v2/projects/{pid}/catalog/chapters/{authored['chapter_id']}/architecture"
    ).json()["data"]["architecture"]["design_changed"] is None

    counts = supersede_scene_planning_artifacts(
        session,
        scene_ids=[],
        chapter_ids=[authored["chapter_id"], generated["chapter_id"]],
        reason="snowflake_step:scene_details",
    )
    session.commit()
    assert counts["chapter_architecture"] == 1 and counts["author_architecture_kept"] == 1
    session.expire_all()
    assert session.get(GenerationPlanningArtifact, author_row_id).status == "active"
    assert session.get(GenerationPlanningArtifact, "planning_ai_architecture").status == "superseded"

    kept = client.get(f"/api/v2/projects/{pid}/catalog/chapters/{authored['chapter_id']}/architecture").json()["data"]
    assert kept["architecture"]["row_id"] == author_row_id
    hint = kept["architecture"]["design_changed"]
    assert hint["reason"] == "snowflake_step:scene_details" and hint["at"] and hint["message"]

    # 换参考书（绑定作用于整部作品）同样不动作者版
    binding = supersede_for_binding_scope(session, scope="project", scope_ref_id=pid)
    session.commit()
    assert binding["author_architecture_kept"] == 1 and binding["chapter_architecture"] == 0
    session.expire_all()
    assert session.get(GenerationPlanningArtifact, author_row_id).status == "active"
    latest = client.get(f"/api/v2/projects/{pid}/catalog/chapters/{authored['chapter_id']}/architecture").json()["data"]
    assert latest["architecture"]["design_changed"]["reason"] == "style_binding_changed"

    # 作者改写蓝图 = 新的一行，提示随旧行留在历史里
    rewritten = client.put(
        f"/api/v2/projects/{pid}/catalog/chapters/{authored['chapter_id']}/architecture",
        json={**_ARCH_PAYLOAD, "chapter_promise": "改写后的承诺"},
    ).json()["data"]["architecture"]
    assert rewritten["row_id"] != author_row_id and rewritten["design_changed"] is None


def _assert_llm_not_configured(response) -> None:
    """B07-12：没有可用模型时章节编排的 AI 一律 409 + 去配置的 author_action（中文），不回 200 + fallback。"""
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "CHAPTER_PLAN_LLM_NOT_CONFIGURED"
    assert error["details"]["author_action"]["target_view"] == "config"
    assert "模型" in error["message"]


def test_generate_architecture_offline_is_fail_closed_and_writes_no_placeholder(client, session) -> None:
    pid = _create_project(client)
    chid = _create_chapter(client, pid)["chapter_id"]
    response = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/architecture/generate",
        json={},
        headers={"X-Idempotency-Key": _key("arch-gen")},
    )
    _assert_llm_not_configured(response)
    # 显式生成在离线时绝不落占位蓝图（占位会被场景 run 当真注入）。
    rows = session.query(GenerationPlanningArtifact).all()
    assert rows == []


def test_generate_architecture_llm_persists_with_context(client, session, monkeypatch) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        _fake_llm(captured, _ARCH_PAYLOAD),
    )
    response = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/architecture/generate",
        json={},
        headers={"X-Idempotency-Key": _key("arch-gen-llm")},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "llm"
    arch = data["architecture"]
    assert arch["payload"]["chapter_promise"] == _ARCH_PAYLOAD["chapter_promise"]
    assert arch["llm_call_id"]
    # 上下文底座真的进了提示词：章卡、邻章交接、作者约束槽位都在。
    assert len(captured) == 1
    user_prompt = captured[0].messages[1]["content"]
    assert '"chapter_card"' in user_prompt
    assert '"neighbor_handoff"' in user_prompt
    assert '"author_constraints"' in user_prompt
    # 计量审计行存在（execute_accounted_call 落 LlmCall）。
    call = session.get(LlmCall, arch["llm_call_id"])
    assert call is not None


# ---------- sanitize 性质（P2 核心，纯函数） ----------


def test_sanitize_plan_patch_properties(client, session) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    scene_id = chapter["scenes"][0]["scene_id"]
    # 给现有场景填一个非空 goal（目录默认 brief.goal = 章标题）
    scenes = list(session.query(SceneCard).filter(SceneCard.chapter_id == chid))
    assert len(scenes) == 1
    scene = scenes[0]
    assert (scene.writer_brief_json or {}).get("goal")  # 非空前提

    patch = {
        "scenes": [
            {
                "scene_id": scene_id,
                "set": {
                    "goal": "试图覆盖非空目标",          # 必须被拒：非空
                    "conflict": "老工人们对她欲言又止",   # 允许：空槽
                    "setback": "  有人 喊错了 她的姓  ",  # 允许 + 归一空白
                    "kind": "reactive",                 # 危险字段：拒
                    "hook": "工牌的名字被磨掉了",         # 允许：空槽
                },
            },
            {"scene_id": "not_a_scene", "set": {"goal": "x"}},  # 未知场：拒
        ],
        "append_scenes": [
            {"title": "堂屋的灯", "kind": "reactive", "brief": {"reaction": "面对没开灯的祖父"}},
            {"title": "", "kind": "proactive", "brief": {}},  # 无标题：拒
        ],
        "delete_scenes": [scene_id],  # 未知顶层键：直接忽略
    }
    clean, dropped = sanitize_plan_patch(scenes, patch)
    assert clean["scenes"] == [
        {
            "scene_id": scene_id,
            "set": {
                "conflict": "老工人们对她欲言又止",
                "setback": "有人 喊错了 她的姓",
                "hook": "工牌的名字被磨掉了",
            },
        }
    ]
    assert [item["title"] for item in clean["append_scenes"]] == ["堂屋的灯"]
    reasons = {(d["scene_id"], d["field"]): d["reason"] for d in dropped}
    assert reasons[(scene_id, "goal")] == "field_not_empty"
    assert reasons[(scene_id, "kind")] == "field_not_allowed"
    assert reasons[("not_a_scene", "*")] == "unknown_scene"
    assert reasons[("", "append_scenes")] == "title_required"


def test_sanitize_append_cap(client, session) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    scenes = list(session.query(SceneCard).filter(SceneCard.chapter_id == chapter["chapter_id"]))
    patch = {
        "scenes": [],
        "append_scenes": [
            {"title": f"新场 {i}", "kind": "proactive", "brief": {"goal": "g"}} for i in range(10)
        ],
    }
    clean, dropped = sanitize_plan_patch(scenes, patch)
    # 1 张现有卡 → 上限 min(6, 1+4)=5
    assert len(clean["append_scenes"]) == 5
    assert any(d["reason"] == "append_cap_reached" for d in dropped)


def test_sanitize_drama_patch_is_fill_only(client, session) -> None:
    pid = _create_project(client)
    chapter_payload = _create_chapter(client, pid)
    chapter = session.get(ChapterGoal, chapter_payload["chapter_id"])
    assert chapter is not None
    narrative = dict(chapter.narrative_json or {})
    narrative["drama"] = {"promise": "作者已经写好的承诺"}
    chapter.narrative_json = narrative
    session.flush()
    scenes = list(session.query(SceneCard).filter(SceneCard.chapter_id == chapter.chapter_id))

    clean, dropped = sanitize_plan_patch(
        scenes,
        {
            "drama": {
                "promise": "试图覆盖作者承诺",
                "spine": "旧工牌把调查推向父亲",
                "arc": "",
                "forbidden": "试图改写护栏",
            },
            "scenes": [],
            "append_scenes": [],
        },
        chapter=chapter,
    )

    assert clean["drama"] == {"spine": "旧工牌把调查推向父亲"}
    reasons = {d["field"]: d["reason"] for d in dropped}
    assert reasons["drama.promise"] == "field_not_empty"
    assert reasons["drama.arc"] == "empty_value"
    assert reasons["drama.forbidden"] == "field_not_allowed"


# ---------- apply（P2） ----------


def test_apply_fill_only_idempotent_replay_and_skip_non_empty(client) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    scene_id = chapter["scenes"][0]["scene_id"]

    body = {
        "patch": {
            "scenes": [
                {
                    "scene_id": scene_id,
                    "set": {
                        "goal": "覆盖尝试必须被 skip",
                        "conflict": "箱底卡着一枚旧工牌",
                        "pov_character_name": "林岑",
                        "exit_change": "工牌回到了她手里",
                    },
                }
            ],
            "append_scenes": [
                {
                    "title": "回家的堤路",
                    "kind": "reactive",
                    "brief": {"reaction": "想清楚要不要问父亲", "dilemma": "家里从不谈旧事", "decision": "先不问"},
                    "hook": "堤路尽头有人等她",
                }
            ],
        }
    }
    key = _key("apply")
    first = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/apply",
        json=body,
        headers={"X-Idempotency-Key": key},
    )
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["applied"] == {"drama": 0, "scenes": 1, "appended": 1}
    assert any(item["field"] == "goal" and item["reason"] == "field_not_empty" for item in data["skipped"])
    scenes = data["chapter"]["scenes"]
    assert len(scenes) == 2
    assert scenes[0]["brief"]["conflict"] == "箱底卡着一枚旧工牌"
    assert scenes[0]["pov_character_name"] == "林岑"
    assert scenes[0]["exit_change"] == "工牌回到了她手里"
    assert scenes[1]["title"] == "回家的堤路"
    assert scenes[1]["kind"] == "reactive"
    assert scenes[1]["hook"] == "堤路尽头有人等她"

    # 同键重放：同响应、不重复追加场景。
    replay = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/apply",
        json=body,
        headers={"X-Idempotency-Key": key},
    )
    assert replay.status_code == 200, replay.text
    assert replay.headers.get("X-Idempotency-Status") == "replayed"
    assert replay.json()["data"]["applied"] == {"drama": 0, "scenes": 1, "appended": 1}
    tree = client.get(f"/api/v2/projects/{pid}/catalog").json()["data"]
    target = next(c for c in tree["chapters"] if c["chapter_id"] == chid)
    assert len(target["scenes"]) == 2  # 没有第三张卡


def test_apply_fills_empty_drama_fields_and_preserves_author_values(client) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    seeded = client.patch(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}",
        json={"drama": {"promise": "作者承诺", "forbidden": "不得梦醒"}, "promise": "作者承诺"},
    )
    assert seeded.status_code == 200, seeded.text

    response = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/apply",
        json={
            "patch": {
                "drama": {
                    "promise": "AI 不能覆盖",
                    "spine": "旧工牌把调查推向父亲",
                    "arc": "林岑从旁观转为主动追查",
                },
                "scenes": [],
                "append_scenes": [],
            }
        },
        headers={"X-Idempotency-Key": _key("apply-drama")},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["applied"] == {"drama": 2, "scenes": 0, "appended": 0}
    assert any(
        item["field"] == "drama.promise" and item["reason"] == "field_not_empty"
        for item in data["skipped"]
    )
    assert data["chapter"]["drama"] == {
        "promise": "作者承诺",
        "forbidden": "不得梦醒",
        "spine": "旧工牌把调查推向父亲",
        "arc": "林岑从旁观转为主动追查",
    }
    assert data["chapter"]["promise"] == "作者承诺"


def test_apply_locked_chapter_409(client, session) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    scene_id = chapter["scenes"][0]["scene_id"]
    _approve_chapter(session, pid, chid)
    response = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/apply",
        json={"patch": {"scenes": [{"scene_id": scene_id, "set": {"conflict": "x"}}], "append_scenes": []}},
        headers={"X-Idempotency-Key": _key("apply-locked")},
    )
    assert response.status_code == 409, response.text
    assert "APPROVED" in response.json()["error"]["code"]


def test_locked_chapter_blocks_architecture_writes_but_review_reads(client, session, monkeypatch) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    _approve_chapter(session, pid, chid)
    put = client.put(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/architecture",
        json=_ARCH_PAYLOAD,
    )
    assert put.status_code == 409
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        _fake_llm([], {"findings": []}),
    )
    review = client.post(f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/review")
    assert review.status_code == 200, review.text  # 只读体检放行
    assert list(review.json()["data"]["findings"]) == []


# ---------- fill（P2） ----------


def test_fill_offline_is_fail_closed_and_the_gap_checklist_is_a_separate_non_ai_read(client) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    response = client.post(f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/fill", json={})
    _assert_llm_not_configured(response)
    # 待补清单不是 AI：没有模型也能读，名字上就说是规则算的
    gaps = client.get(f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/gaps")
    assert gaps.status_code == 200, gaps.text
    data = gaps.json()["data"]
    assert data["source"] == "rules"
    # 默认开场卡缺 conflict/setback/pov → 清单列出空槽
    assert data["gaps"] and any("conflict" in gap for gap in data["gaps"])


def test_fill_llm_patch_sanitized_and_notes_kept(client, monkeypatch) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    scene_id = chapter["scenes"][0]["scene_id"]
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []
    payload = {
        "patch": {
            "drama": {
                "promise": "读者发现旧工牌指向林岑的父亲",
                "forbidden": "试图改写护栏",
            },
            "scenes": [
                {
                    "scene_id": scene_id,
                    "set": {"goal": "试图覆盖", "conflict": "祖父半夜起身坐在堂屋", "state": "done"},
                }
            ],
            "append_scenes": [],
        },
        "notes": [
            {"scene_id": scene_id, "field": "kind", "suggestion": "建议改为反应场", "reason": "连续主动场太密"}
        ],
        "gaps": ["第 1 场 POV 无法从上下文推断"],
    }
    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        _fake_llm(captured, payload),
    )
    response = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/fill",
        json={"mode": "fill"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "llm"
    assert data["patch"]["drama"] == {"promise": "读者发现旧工牌指向林岑的父亲"}
    assert data["patch"]["scenes"] == [
        {"scene_id": scene_id, "set": {"conflict": "祖父半夜起身坐在堂屋"}}
    ]
    dropped = {(d["scene_id"], d["field"]): d["reason"] for d in data["dropped"]}
    assert dropped[(scene_id, "goal")] == "field_not_empty"
    assert dropped[(scene_id, "state")] == "field_not_allowed"
    assert dropped[("", "drama.forbidden")] == "field_not_allowed"
    assert data["notes"][0]["suggestion"] == "建议改为反应场"
    assert data["gaps"] == ["第 1 场 POV 无法从上下文推断"]
    # 上下文底座在提示词里
    user_prompt = captured[0].messages[1]["content"]
    assert '"scene_cards_current"' in user_prompt
    assert '"mode"' in user_prompt


def test_fill_adopt_requires_candidate(client, monkeypatch) -> None:
    pid = _create_project(client)
    chid = _create_chapter(client, pid)["chapter_id"]
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    response = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/fill",
        json={"mode": "adopt"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CHAPTER_PLAN_CANDIDATE_REQUIRED"


# ---------- candidates（P3） ----------


def test_candidates_offline_is_fail_closed(client) -> None:
    pid = _create_project(client)
    chid = _create_chapter(client, pid)["chapter_id"]
    response = client.post(f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/candidates", json={})
    _assert_llm_not_configured(response)


def test_candidates_llm_normalizes_refs_and_carries_hint(client, monkeypatch) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    scene_id = chapter["scenes"][0]["scene_id"]
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    captured: list = []

    def _plan(ref):
        return [
            {
                "ref_scene_id": ref,
                "title": "交班点名",
                "kind": "proactive",
                "brief": {"goal": "替父亲完成点名", "conflict": "欲言又止", "setback": "喊错了姓"},
                "pov_character_name": "林岑",
                "exit_change": "名册上多了一个陌生名字",
                "hook": "有人在窗外看她",
                "tension_note": "引入身份疑点（新信息型压力）",
            }
        ]

    payload = {
        "candidates": [
            {"label": "双场对撞", "rationale": "承接上一章出口", "risk": "节奏太快", "scene_plan": _plan(scene_id)},
            {"label": "慢热三幕", "rationale": "给伏笔留offset", "risk": "中段泄气", "scene_plan": _plan("bogus_scene")},
            {"label": "反应场开局", "rationale": "消化上章代价", "risk": "开局无钩", "scene_plan": _plan(None)},
        ]
    }
    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        _fake_llm(captured, payload),
    )
    response = client.post(
        f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/candidates",
        json={"direction_hint": "希望更贴近家庭线"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "llm"
    assert len(data["candidates"]) == 3
    refs = [c["scene_plan"][0]["ref_scene_id"] for c in data["candidates"]]
    assert refs == [scene_id, None, None]  # 坏 ref 归一为 None（新卡）
    assert "希望更贴近家庭线" in captured[0].messages[1]["content"]


# ---------- review（P4） ----------


def test_review_offline_is_fail_closed_instead_of_rule_findings(client) -> None:
    """以前体检在没有模型时拿规则凑一份 findings 冒充 AI 体检；现在如实 409（待补清单另有只读接口）。"""
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    response = client.post(f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/review")
    _assert_llm_not_configured(response)


def test_a_failed_model_call_is_reported_in_chinese_without_the_exception_text(client, session, monkeypatch) -> None:
    pid = _create_project(client)
    chid = _create_chapter(client, pid)["chapter_id"]
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")

    def failing_generate(self, request):  # noqa: ANN001
        raise RuntimeError("upstream exploded with sk-secret-detail")

    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted", accounted_generate_method(failing_generate)
    )
    response = client.post(f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/review")
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "CHAPTER_PLAN_LLM_CALL_FAILED"
    assert "exploded" not in error["message"] and "sk-secret" not in error["message"]
    assert "章节编排" in error["message"]


def test_review_llm_drops_evidence_free_findings_and_sanitizes_patch(client, monkeypatch) -> None:
    pid = _create_project(client)
    chapter = _create_chapter(client, pid)
    chid = chapter["chapter_id"]
    scene_id = chapter["scenes"][0]["scene_id"]
    monkeypatch.setenv("NOVEL_SYSTEM_LLM_ENABLED", "true")
    payload = {
        "findings": [
            {
                "code": "EXIT_NO_CHANGE",
                "severity": "warn",
                "scene_id": scene_id,
                "field": "exit_change",
                "evidence": "该场 exit_change 为空，世界状态未变化。",
                "summary": "结尾没有改变任何东西。",
                "suggestion_patch": {
                    "scenes": [
                        {"scene_id": scene_id, "set": {"exit_change": "她拿走了名册", "goal": "覆盖尝试"}}
                    ],
                    "append_scenes": [],
                },
            },
            {"code": "TENSION_FLAT", "severity": "warn", "scene_id": None, "evidence": "", "summary": "无据断言"},
            {"code": "NOT_A_CODE", "severity": "bad", "scene_id": "nope", "evidence": "有据但 code 非法", "summary": "s"},
            # 伏笔账早已删除，伏笔逾期不再是体检项（批准 #17a）：模型硬说了也只算「其他」
            {"code": "FORESHADOW_OVERDUE", "severity": "warn", "scene_id": None, "evidence": "旧信没回收", "summary": "s"},
        ]
    }
    monkeypatch.setattr(
        "novel_system.services.llm_client.LLMClient.generate_accounted",
        _fake_llm([], payload),
    )
    response = client.post(f"/api/v2/projects/{pid}/catalog/chapters/{chid}/plan/review")
    assert response.status_code == 200, response.text
    findings = response.json()["data"]["findings"]
    assert len(findings) == 3  # 无据 finding 被丢弃
    first = findings[0]
    assert first["code"] == "EXIT_NO_CHANGE"
    # suggestion_patch 同样过 sanitize：覆盖 goal 被剔除，只留填空 exit_change
    assert first["suggestion_patch"]["scenes"] == [
        {"scene_id": scene_id, "set": {"exit_change": "她拿走了名册"}}
    ]
    second = findings[1]
    assert second["code"] == "OTHER" and second["severity"] == "info" and second["scene_id"] is None
    assert findings[2]["code"] == "OTHER"


# ---------- 上下文底座（P1） ----------


def test_context_builder_reads_scene_data_not_the_dead_chapter_fields(client, session) -> None:
    """批准 #17a（重评 R10）：章级的张力 / 视角 / 入口 / 出口 / 线索没有任何地方能填，规划提示不再读它们（库里的旧值
    原样留着，这里直接写进库当作旧数据）；邻章交接、章结构邻域、近几章的视角分布都从各场现算。
    批准 #6（重评 R5）：作者偏好档案那一槽与它的降级标记一并去掉；伏笔槽早就没有数据来源，也去掉。"""
    from novel_system.db.models import StoryCharacter
    from novel_system.services.chapter_planning_context import ChapterPlanningContextBuilder

    pid = _create_project(client)
    ch1 = _create_chapter(client, pid, "第一章")
    ch2 = _create_chapter(client, pid, "第二章")
    ch3 = _create_chapter(client, pid, "第三章")
    patched = client.patch(
        f"/api/v2/projects/{pid}/catalog/chapters/{ch2['chapter_id']}",
        json={"drama": {"forbidden": "不得出现梦醒桥段", "promise": "p"}},
    )
    assert patched.status_code == 200, patched.text
    lin = StoryCharacter(character_id=f"{pid}_lin", project_id=pid, display_name="林昭")
    session.add(lin)
    for chapter, legacy in (
        (ch1, {"exit": "旧出口", "tension": 0.4, "pov": "沈迟"}),
        (ch2, {"tension": 0.6, "entry": "旧入口", "threads": ["旧线"]}),
        (ch3, {"entry": "堂屋的旧入口"}),
    ):
        row = session.get(ChapterGoal, chapter["chapter_id"])
        row.narrative_json = {**(row.narrative_json or {}), **legacy}
    last_of_first = session.get(SceneCard, ch1["scenes"][-1]["scene_id"])
    last_of_first.exit_change = "她带着名册离开盐场"
    last_of_first.hook = "名册上多了一个名字"
    last_of_first.pov_character_id = lin.character_id
    first_of_third = session.get(SceneCard, ch3["scenes"][0]["scene_id"])
    first_of_third.writer_brief_json = {**(first_of_third.writer_brief_json or {}), "goal": "进堂屋问清旧信的来历"}
    session.commit()

    context = ChapterPlanningContextBuilder(session).build(pid, ch2["chapter_id"])
    payload = context.prompt_payload
    for dead in ("tension", "pov", "entry", "exit", "threads"):
        assert dead not in payload["chapter_card"], dead
    prev, following = payload["neighbor_handoff"]["prev"], payload["neighbor_handoff"]["next"]
    assert "exit" not in prev and "entry" not in following
    assert prev["last_scene"]["exit_change"] == "她带着名册离开盐场"
    assert prev["last_scene"]["hook"] == "名册上多了一个名字"
    assert following["first_scene"]["kind"] == "proactive"
    assert following["first_scene"]["first_beat"] == "进堂屋问清旧信的来历"
    assert "tension_neighborhood" not in payload and "foreshadow_debts" not in payload
    window = payload["structure_neighborhood"]["window"]
    assert [item["is_current"] for item in window] == [False, True, False]
    assert [item["scene_count"] for item in window] == [1, 1, 1]
    assert all("tension" not in item for item in window)
    # 近几章的视角分布数的是各场的视角人物，不是章级的旧 pov 字段
    assert payload["character_positions"]["recent_pov_distribution"] == {"林昭": 1}
    assert payload["author_constraints"]["forbidden"] == "不得出现梦醒桥段"
    assert "author_preferences" not in payload["author_constraints"]
    # 冷启动：无雪花 canon / 无蓝图 → 降级而非阻断；没有作者偏好 / 伏笔这两个永远降级的槽
    assert "snowflake_canon" in context.degraded_slots
    assert "chapter_architecture" in context.degraded_slots
    assert "author_preferences" not in context.degraded_slots
    assert "foreshadow_debts" not in context.degraded_slots
    assert context.context_fingerprint
    assert context.source_version_refs["chapter_goal"] == ch2["chapter_id"]
