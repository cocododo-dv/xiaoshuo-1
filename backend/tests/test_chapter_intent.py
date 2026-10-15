"""章的说明（2026-10-16 规划层收拢第 2、3 步）：给读者什么 / 先别揭 / 备注。

- 雪花的章在构思的分章面板里写：预览带着目录章上的三格与「目录里有没有这一章」，保存 / 确认写入写进目录章的
  narrative_json.drama，重新确认写入不丢；
- 手建的章在章节编排里写（目录 PATCH drama）；
- 进蓝图的两格变了，这一章的章节蓝图让位：AI 写的作废，作者写的留着并提示设计改过；
- 原戏剧卡的另外五格不再进蓝图；
- 第 10 步的 AI 生成拿到每场所在的章、收章的那一场、章的说明。
"""

from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import ChapterGoal, GenerationPlanningArtifact, SnowflakeScenePlan
from novel_system.services.chapter_drama import chapter_drama_for_blueprint
from novel_system.services.snowflake_chapter_context import chapter_context_for_scene_planning
from novel_system.services.snowflake_prompt_budget import PROTECTED_KEYS
from tests.support.chaptering import (
    catalog_chapters as _catalog,
    confirm_chaptering as _confirm,
    confirm_payload as _payload,
    materialized_project as _materialized,
)


def _panel(client, project_id: str) -> dict:
    response = client.post(
        f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan/preview", json={"strategy": "keep_current"}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _architecture(session, chapter_id: str, row_id: str, *, llm_call_id: str | None) -> None:
    session.add(
        GenerationPlanningArtifact(
            row_id=row_id,
            artifact_type="chapter_story_architecture",
            object_type="chapter",
            object_id=chapter_id,
            chapter_id=chapter_id,
            scene_id=None,
            payload_json={"chapter_promise": "旧的承诺"},
            llm_call_id=llm_call_id,
            source_bundle_id=None,
            source_bundle_hash="h",
            status="active",
            created_by="operator",
        )
    )
    session.commit()


def test_panel_writes_the_chapter_intent_onto_the_catalog_chapter_and_reconfirming_keeps_it(client, session) -> None:
    project_id = _materialized(client, "intent-panel")
    panel = _panel(client, project_id)
    first = panel["chapters"][0]
    assert first["catalog_ready"] is True
    assert first["intent"] == {"promise": "", "forbidden": "", "notes": ""}

    payload = _payload(panel)
    payload["chapters"][0].update(
        {"promise": "林昭第一次发现旧信是假的", "forbidden_text": "寄信人的名字\n案卷编号", "notes": "节奏放慢"}
    )
    saved = client.patch(f"/api/v2/projects/{project_id}/snowflake-workspace/chapter-plan", json=payload)
    assert saved.status_code == 200, saved.text

    chapter = next(c for c in _catalog(client, project_id) if c["chapter_id"] == first["chapter_id"])
    assert chapter["drama"]["promise"] == "林昭第一次发现旧信是假的"
    assert chapter["drama"]["forbidden"] == "寄信人的名字\n案卷编号"
    assert chapter["drama"]["notes"] == "节奏放慢"
    assert _panel(client, project_id)["chapters"][0]["intent"]["promise"] == "林昭第一次发现旧信是假的"

    # 再确认写入一次（整张章表照旧带回来，不带章的说明）：三格还在
    _confirm(client, project_id, _panel(client, project_id), "intent-panel-again")
    session.expire_all()
    kept = session.get(ChapterGoal, first["chapter_id"])
    assert kept.narrative_json["drama"]["promise"] == "林昭第一次发现旧信是假的"
    assert kept.narrative_json["drama"]["forbidden"] == "寄信人的名字\n案卷编号"


def test_changing_what_reaches_the_blueprint_retires_the_ai_blueprint_and_flags_the_authors(client, session) -> None:
    project_id = _materialized(client, "intent-stale")
    chapters = _catalog(client, project_id)
    ai_chapter, author_chapter = chapters[0]["chapter_id"], chapters[1]["chapter_id"]
    _architecture(session, ai_chapter, "arch_ai", llm_call_id="llm_call_arch")
    _architecture(session, author_chapter, "arch_author", llm_call_id=None)

    for chapter_id, key in ((ai_chapter, "intent-stale-1"), (author_chapter, "intent-stale-2")):
        response = client.patch(
            f"/api/v2/projects/{project_id}/catalog/chapters/{chapter_id}",
            json={"drama": {"promise": "", "forbidden": "雨城的真名", "notes": ""}},
            headers={"X-Idempotency-Key": key},
        )
        assert response.status_code == 200, response.text

    session.expire_all()
    assert session.get(GenerationPlanningArtifact, "arch_ai").status == "superseded"
    assert session.get(GenerationPlanningArtifact, "arch_author").status == "active"
    view = client.get(f"/api/v2/projects/{project_id}/catalog/chapters/{author_chapter}/architecture").json()["data"]
    assert view["architecture"]["design_changed"]["reason"] == "chapter_intent_changed"

    # 只改备注不碰蓝图
    _architecture(session, ai_chapter, "arch_ai_2", llm_call_id="llm_call_arch_2")
    client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{ai_chapter}",
        json={"drama": {"promise": "", "forbidden": "雨城的真名", "notes": "只给自己看"}},
        headers={"X-Idempotency-Key": "intent-stale-3"},
    )
    session.expire_all()
    assert session.get(GenerationPlanningArtifact, "arch_ai_2").status == "active"


def test_only_the_two_blueprint_slots_reach_the_blueprint() -> None:
    class _Chapter:
        narrative_json = {
            "drama": {
                "promise": "给读者一次反转",
                "forbidden": "真名",
                "notes": "自用",
                "spine": "旧的主线推进",
                "aftertaste": "旧的余味",
            }
        }

    assert chapter_drama_for_blueprint(_Chapter()) == {"promise": "给读者一次反转", "forbidden": "真名"}


def test_step_10_generation_sees_each_chapter_its_closing_scene_and_the_authors_intent(client, session) -> None:
    project_id = _materialized(client, "intent-step10")
    first = _catalog(client, project_id)[0]
    client.patch(
        f"/api/v2/projects/{project_id}/catalog/chapters/{first['chapter_id']}",
        json={"drama": {"promise": "林昭读完旧信", "forbidden": "寄信人\n\n案卷编号", "notes": "不送"}},
        headers={"X-Idempotency-Key": "intent-step10-1"},
    )
    session.expire_all()

    context = chapter_context_for_scene_planning(session, project_id)

    assert "chapter_context" in PROTECTED_KEYS
    head = context["chapters"][0]
    assert head["scene_ids"] and head["closing_scene_id"] == head["scene_ids"][-1]
    assert head["reader_payoff"] == "林昭读完旧信"
    assert head["not_yet"] == ["寄信人", "案卷编号"]
    assert "notes" not in head and "不送" not in str(context)
    others = context["chapters"][1:]
    assert others and all("reader_payoff" not in item and "not_yet" not in item for item in others)

    # 定向深化的一批：只带这些场所在的章
    last_scene = others[-1]["closing_scene_id"]
    narrowed = chapter_context_for_scene_planning(session, project_id, [last_scene])
    assert [item["closing_scene_id"] for item in narrowed["chapters"]] == [last_scene]
    planned = session.execute(
        select(SnowflakeScenePlan.scene_id).where(SnowflakeScenePlan.project_id == project_id)
    ).scalars().all()
    assert set(head["scene_ids"]) <= set(planned)
