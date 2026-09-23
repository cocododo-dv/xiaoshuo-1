"""阶段 I（2026-09-14 雪花评估第二轮）：场是拍子序列。

原著：反应场可以整场写、缩成两段概述、或干脆略过（三拍照样要在心里写清楚）；一场可以接着另一组
三拍（Goldilocks 场景 1 / 8 / 13）；非赢不可时挫折写成带代价的胜利。这里锁住：
- skip：不建场景卡；已物化的卡回流进回收站、改回来取回；下一场的设计上下文整段带上被略过的三拍；节奏按 0 计；
- 次要三拍：节拍按发生顺序拼接，结构简报列出 Follow-up beats 与 Scene ends on；
- 提示词知道这些。
"""

from __future__ import annotations

import pathlib

import yaml

from novel_system.db.models import SceneCard, StoryCharacter, StoryProject
from novel_system.services.scene_design_context import render_scene_design_context
from novel_system.services.scene_structure_brief import render_scene_structure_brief
from novel_system.services.snowflake_chaptering import _rhythm_report
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService, _scene_card_beats
from tests.test_snowflake_rendering_mode import PROJECT_ID, _materialize, _plan, _seed


def _skip_u3(session, service: SnowflakeWorkspaceService) -> None:
    plan = _plan(session, "u3")
    service.update_scene_plan(PROJECT_ID, plan.scene_plan_id, {"rendering_mode": "skip"})
    assert _plan(session, "u3").rendering_mode == "skip"


def test_skipped_reactive_scene_is_not_materialized_and_weighs_nothing(session) -> None:
    service = _seed(session)
    _skip_u3(session, service)
    plan_json = _materialize(session, service)
    scene_ids = {scene["scene_id"] for chapter in plan_json["chapters"] for scene in chapter["scenes"]}
    assert _plan(session, "u3").scene_id not in scene_ids
    assert _plan(session, "u1").scene_id in scene_ids and _plan(session, "u2").scene_id in scene_ids
    assert session.get(SceneCard, _plan(session, "u3").scene_id) is None
    # 略过场之前的那一场成了章末
    cards = {scene["scene_id"]: scene for chapter in plan_json["chapters"] for scene in chapter["scenes"]}
    assert cards[_plan(session, "u2").scene_id]["is_chapter_last"] == 1
    # 主动场改 skip 不合法：收口成 full
    plan = _plan(session, "u1")
    service.update_scene_plan(PROJECT_ID, plan.scene_plan_id, {"rendering_mode": "skip"})
    assert _plan(session, "u1").rendering_mode == "full"

    report = _rhythm_report(
        [
            {
                "act": 1,
                "scene_count": 3,
                "scenes": [{"rendering_mode": "full"}, {"rendering_mode": "summary"}, {"rendering_mode": "skip"}],
            }
        ]
    )
    assert report["skipped_scene_count"] == 1 and report["summary_scene_count"] == 1
    assert report["weighted_scene_counts"] == [1.5]


def test_flipping_a_materialized_scene_to_skip_trashes_its_card_and_back_restores_it(session) -> None:
    service = _seed(session)
    _materialize(session, service)
    plan = _plan(session, "u3")
    card = session.get(SceneCard, plan.scene_id)
    assert card is not None and not card.trashed_flag
    assert service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))["pending_count"] == 0

    service.update_scene_plan(PROJECT_ID, plan.scene_plan_id, {"rendering_mode": "skip"})
    status = service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))
    pending = {item["scene_id"]: item for item in status["pending_scenes"]}
    assert plan.scene_id in pending and "trashed_flag" in pending[plan.scene_id]["changed_fields"]
    service.resync_materialized_scenes(PROJECT_ID, {"scene_ids": [plan.scene_id]})
    session.expire_all()
    card = session.get(SceneCard, plan.scene_id)
    assert card.trashed_flag == 1 and card.writer_brief_json.get("skipped_by_plan") is True
    assert card.writer_brief_json.get("rendering_mode") == "skip"
    assert service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))["pending_count"] == 0

    service.update_scene_plan(PROJECT_ID, plan.scene_plan_id, {"rendering_mode": "full"})
    service.resync_materialized_scenes(PROJECT_ID, {"scene_ids": [plan.scene_id]})
    session.expire_all()
    card = session.get(SceneCard, plan.scene_id)
    assert card.trashed_flag == 0 and card.writer_brief_json.get("skipped_by_plan") is False

    # 作者自己扔进回收站的卡（没有 skipped_by_plan 标记）回流不碰
    card.trashed_flag = 1
    card.writer_brief_json = {**card.writer_brief_json, "skipped_by_plan": False}
    session.flush()
    assert service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))["pending_count"] == 0


def test_design_context_carries_the_skipped_beat_into_the_next_scene(session) -> None:
    service = _seed(session)
    session.add(StoryCharacter(character_id="c1", project_id=PROJECT_ID, display_name="她", role="主角", summary_json={}, synopsis_json={}, bible_json={}, status="approved"))
    session.flush()
    # u2 略过；u3 是它之后的一场
    plan_u2 = _plan(session, "u2")
    service.update_scene_plan(PROJECT_ID, plan_u2.scene_plan_id, {"rendering_mode": "skip"})
    _materialize(session, service)
    card_u3 = session.get(SceneCard, _plan(session, "u3").scene_id)
    text = render_scene_design_context(card_u3, session) or ""
    assert "Previous scene (S02, POV 她) is skipped on the page (the reader never sees it); its designed beat — Reaction: 手抖，半天说不出话。; Dilemma: 报警伤弟弟；不报警明天轮到自己。; Decision: 去找当年的证人。" in text
    # u1 的下一场是被略过的 u2：标出来
    card_u1 = session.get(SceneCard, _plan(session, "u1").scene_id)
    text_u1 = render_scene_design_context(card_u1, session) or ""
    assert "Next scene (S02) is skipped on the page; it opens on Reaction: 手抖，半天说不出话。" in text_u1


def test_follow_up_beats_join_the_card_beats_and_the_brief_says_where_the_scene_ends() -> None:
    detail = {
        "goal": "拿到离开许可",
        "conflict": "三轮受阻",
        "setback": "被拘留 48 小时",
        "reaction": "手在颤抖",
        "dilemma": "认罪换假释，还是抵抗",
        "decision": "签字前给记者发暗语",
    }
    assert _scene_card_beats("proactive", detail) == ["拿到离开许可", "三轮受阻", "被拘留 48 小时", "手在颤抖", "认罪换假释，还是抵抗", "签字前给记者发暗语"]
    assert _scene_card_beats("reactive", detail) == ["手在颤抖", "认罪换假释，还是抵抗", "签字前给记者发暗语", "拿到离开许可", "三轮受阻", "被拘留 48 小时"]

    scene = SceneCard(
        scene_id="U01_SC01",
        chapter_id="U01",
        project_id="P_U",
        scene_seq=1,
        scene_goal="拿到离开许可",
        scene_type="proactive",
        writer_brief_json={"scene_form": "proactive", "scene_crucible": "审讯室封闭", **detail},
    )
    brief = render_scene_structure_brief(scene, None) or ""
    assert "Follow-up beats (after the primary trio, in this order): Reaction (反应): 手在颤抖; Dilemma (两难): 认罪换假释，还是抵抗; Decision (决定): 签字前给记者发暗语" in brief
    assert "Scene ends on: Decision (决定) (the last follow-up beat)" in brief
    single = SceneCard(scene_id="U01_SC02", chapter_id="U01", project_id="P_U", scene_seq=2, scene_goal="x", scene_type="proactive",
                       writer_brief_json={"scene_form": "proactive", "scene_crucible": "审讯室封闭", "goal": "g", "conflict": "c", "setback": "s"})
    assert "Scene ends on" not in (render_scene_structure_brief(single, None) or "")


def test_prompts_know_follow_up_beats_skip_and_costly_victory() -> None:
    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    details = templates["snowflake_generate_scene_details"]
    assert "follow-up beats" in details["task_prompt"] and '"skip"' in details["task_prompt"]
    assert "a mixed victory is a legitimate setback" in details["task_prompt"]
    for name in ("neutral_draft", "style_first_draft", "hard_qc", "scene_blueprint"):
        template = templates[name]
        assert "Follow-up beats" in template["task_prompt"], name
        assert "Scene ends on" in template["task_prompt"], name
