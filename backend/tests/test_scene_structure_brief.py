"""阶段 A（2026-09-13 雪花评估）：作者写下的场景结构要到达写作。

断链的复现：雪花物化把形态 / 坩埚 / 三拍 / 代价写进 ``SceneCard.writer_brief_json``，
但 ``writer_briefs.normalize_scene_writer_brief`` 只认 v2 的 13 个键，bundle / 蓝图 /
近终稿 / 预检看到的简报全是空的。这里锁住修复：
- 结构简报直读原始键，带标签渲染，POV 解析成姓名；
- 进入起草 bundle（紧跟 scene_card）、蓝图快照与近终稿快照，预算再紧也不被压；
- 预检按三拍缺口体检，而不是要求雪花作者再填一套 v2 字段；
- 环境开关可整体关闭；v2-only 简报与空简报不受影响。
"""

from __future__ import annotations

import pytest

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SceneRunState,
    RelationProfile,
    StoryCharacter,
    StoryProject,
    VoiceProfile,
)
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.context_budget import apply_context_budget, collect_prompt_sections
from novel_system.services.near_final import NearFinalPlanningService
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_run_preflight import SceneRunPreflightService
from novel_system.services.scene_structure_brief import (
    SCENE_STRUCTURE_SECTION_KEY,
    SCENE_STRUCTURE_SECTION_LABEL,
    missing_structure_fields,
    render_scene_structure_brief,
    scene_has_structure,
    scene_structure_form,
)
from tests.real_llm_fakes import install_online_pipeline

PROJECT_ID = "P_SSB"
CHAPTER_ID = "SSB01"
PROACTIVE_ID = "SSB01_SC01"
REACTIVE_ID = "SSB01_SC02"
V2_ONLY_ID = "SSB01_SC03"
INCOMPLETE_ID = "SSB01_SC04"
CATALOG_ID = "SSB01_SC05"
POV_ID = "P_SSB_CHAR01"
FOE_ID = "P_SSB_CHAR02"

SETBACK = "警探宣布以妨碍司法拘留 48 小时，正好是真凶行动的窗口期。"
DECISION = "决定认罪，但在签字前悄悄给记者发出一条暗语短信。"


@pytest.fixture(autouse=True)
def _auto_online_pipeline(monkeypatch):
    install_online_pipeline(monkeypatch)


def _snowflake_brief(**overrides):
    base = {
        "source": "snowflake_method",
        "project_id": PROJECT_ID,
        "outline_plan_id": "outline_plan_P_SSB_01",
        "reference_safety": ["不得复制参考书原文表达、人物、设定或桥段。"],
        "must_withhold": "不要提前解释完整背景，只揭示当前场景必需的信息。",
        "expected_reader_emotion": "让读者感到人物主动出击却被阻力持续抬高代价。",
        "timebox": "medium",
        "tension_target": None,
        "function_tag": None,
        "involved_foreshadowing": [],
        "causal_prerequisite_scene_id": None,
        "downstream_obligations": [],
    }
    base.update(overrides)
    return base


def _seed(session) -> None:
    session.add(StoryProject(project_id=PROJECT_ID, title="结构简报夹具", outline_text="结构简报夹具大纲。"))
    for character_id, name, role in ((POV_ID, "林一鸣", "主角"), (FOE_ID, "周慎", "对手")):
        session.add(
            StoryCharacter(
                character_id=character_id,
                project_id=PROJECT_ID,
                display_name=name,
                role=role,
                summary_json={},
                synopsis_json={},
                bible_json={},
                status="approved",
            )
        )
    # bundle 构建要求 POV 角色有活动声线档、两名同场角色有活动关系档；只为让 BundleBuilder 走通。
    session.add(
        VoiceProfile(
            row_id="voice_profile_p_ssb_char01_v1",
            voice_profile_id=f"VOICE_{POV_ID}",
            version=1,
            character_id=POV_ID,
            content="林一鸣的叙述声线克制、冷静，选择代价时会显出迟疑。",
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
    )
    session.add(
        RelationProfile(
            row_id="relation_profile_p_ssb_v1",
            relation_profile_id=f"REL_{POV_ID}_{FOE_ID}",
            left_character_id=POV_ID,
            right_character_id=FOE_ID,
            version=1,
            content="警探周慎握着伪造的截图，林一鸣知道对方在等他犯程序错误。",
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
    )
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=5,
            chapter_goal="在审讯室里拿到离开许可，却把自己推进更深的局。",
            writer_brief_json={"source": "snowflake_method", "chapter_role": "承压"},
        )
    )
    session.flush()

    def scene(scene_id: str, seq: int, **kwargs) -> None:
        defaults = dict(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=seq,
            scene_goal="在审讯结束前拿到离开许可。",
            beats_json=["提供不在场证明", "要求见律师", "拘留 48 小时"],
            location="审讯室",
            pov_character_id=POV_ID,
            onstage_chars_json=[POV_ID, FOE_ID],
            target_length_band="medium",
            exit_change="主角失去了自由，也失去了唯一的线人。",
            hook="真凶今晚就要行动。",
        )
        defaults.update(kwargs)
        session.add(SceneCard(scene_id=scene_id, **defaults))
        session.add(SceneRunState(scene_id=scene_id, scene_status="ready"))

    scene(
        PROACTIVE_ID,
        1,
        scene_type="proactive",
        writer_brief_json=_snowflake_brief(
            scene_form="proactive",
            scene_crucible="审讯室封闭，主角有前科，手机被没收，真凶今晚行动。",
            goal="在审讯结束前拿到离开许可。",
            conflict="提供不在场证明被监控截图否定；要求见律师被拖延；激怒警探反而暴露新风险。",
            setback=SETBACK,
            must_reveal=SETBACK,
            next_scene_pull="真凶今晚就要行动。",
            cost_requirement="唯一的线人从此断联。",
        ),
    )
    scene(
        REACTIVE_ID,
        2,
        scene_type="reactive",
        writer_brief_json=_snowflake_brief(
            scene_form="reactive",
            scene_crucible="真凶今晚就要行动，主角却被关着，没有人相信他。",
            reaction="手在颤抖；脑子里反复回放被篡改的视频；最后才意识到 48 小时意味着什么。",
            dilemma="认罪换假释就永远背负污点；继续抵抗真凶今晚得逞。",
            decision=DECISION,
            must_reveal=DECISION,
            cost_requirement="失去律师执照，也失去亲手抓到真凶的机会。",
        ),
    )
    scene(
        V2_ONLY_ID,
        3,
        scene_type="outline_driven",
        writer_brief_json={
            "character_desire": "拿到真相",
            "choice_under_pressure": "相信老友还是独自调查",
        },
    )
    scene(
        INCOMPLETE_ID,
        4,
        scene_type="proactive",
        writer_brief_json=_snowflake_brief(
            scene_form="proactive",
            scene_crucible="审讯室封闭。",
            goal="拿到离开许可。",
        ),
    )
    # 章节编排里手填的三拍：没有 scene_form，也没有 source=snowflake_method。
    scene(
        CATALOG_ID,
        5,
        scene_type="reactive",
        writer_brief_json={
            "reaction": "她把电话扣在桌上，半天没说话。",
            "dilemma": "报警会牵连弟弟；不报警明天就是下一个。",
            "decision": "去找当年的证人。",
        },
    )
    session.commit()


def _scene(session, scene_id: str) -> SceneCard:
    return session.get(SceneCard, scene_id)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_proactive_brief_labels_the_three_beats_and_resolves_pov_names(session) -> None:
    _seed(session)
    text = render_scene_structure_brief(_scene(session, PROACTIVE_ID), session)
    assert text is not None
    lines = text.split("\n")
    assert lines[0] == "Scene form: proactive scene (主动场景) — Goal → Conflict → Setback"
    assert "POV character: 林一鸣" in lines
    assert "Onstage characters: 林一鸣, 周慎" in lines
    assert "Scene crucible (坩埚): 审讯室封闭，主角有前科，手机被没收，真凶今晚行动。" in lines
    assert "Goal (目标): 在审讯结束前拿到离开许可。" in lines
    assert any(line.startswith("Conflict (冲突): 提供不在场证明") for line in lines)
    assert f"Setback (挫折): {SETBACK}" in lines
    assert "Cost paid (代价): 唯一的线人从此断联。" in lines
    assert "Must withhold: 不要提前解释完整背景，只揭示当前场景必需的信息。" in lines
    assert "Next-scene pull (钩子): 真凶今晚就要行动。" in lines
    assert "Target length band: medium" in lines
    # must_reveal 与挫折同文时不重复；反应三拍全空时不渲染 follow-up
    assert "Must reveal:" not in text
    assert "Follow-up beats" not in text
    assert "Reaction (反应)" not in text


def test_reactive_brief_uses_reaction_dilemma_decision(session) -> None:
    _seed(session)
    scene = _scene(session, REACTIVE_ID)
    assert scene_structure_form(scene) == "reactive"
    text = render_scene_structure_brief(scene, session)
    assert text is not None
    assert text.startswith("Scene form: reactive scene (反应场景) — Reaction → Dilemma → Decision")
    assert "Reaction (反应): 手在颤抖" in text
    assert "Dilemma (两难): 认罪换假释" in text
    assert f"Decision (决定): {DECISION}" in text
    assert "Goal (目标)" not in text


def test_catalog_three_beats_without_snowflake_source_still_render(session) -> None:
    _seed(session)
    scene = _scene(session, CATALOG_ID)
    assert scene_has_structure(scene)
    assert scene_structure_form(scene) == "reactive"
    text = render_scene_structure_brief(scene, session)
    assert text is not None
    assert "Decision (决定): 去找当年的证人。" in text
    # 没填坩埚要明说，而不是省略这一行
    assert "Scene crucible (坩埚): 未规划" in text


def test_v2_only_and_empty_briefs_render_nothing(session) -> None:
    _seed(session)
    assert not scene_has_structure(_scene(session, V2_ONLY_ID))
    assert render_scene_structure_brief(_scene(session, V2_ONLY_ID), session) is None
    blank = SceneCard(scene_id="SSB01_SC09", chapter_id=CHAPTER_ID, scene_seq=9, scene_goal="", writer_brief_json={})
    assert scene_structure_form(blank) is None
    assert render_scene_structure_brief(blank, session) is None


def test_missing_structure_fields_follow_the_scene_form(session) -> None:
    _seed(session)
    assert missing_structure_fields(_scene(session, PROACTIVE_ID)) == []
    assert missing_structure_fields(_scene(session, REACTIVE_ID)) == []
    assert missing_structure_fields(_scene(session, INCOMPLETE_ID)) == ["conflict", "setback"]
    assert missing_structure_fields(_scene(session, CATALOG_ID)) == ["scene_crucible"]
    assert missing_structure_fields(_scene(session, V2_ONLY_ID)) == []
    text = render_scene_structure_brief(_scene(session, INCOMPLETE_ID), session)
    assert text is not None
    assert "Conflict (冲突): 未规划" in text and "Setback (挫折): 未规划" in text


# ---------------------------------------------------------------------------
# 进入写作管线
# ---------------------------------------------------------------------------


def test_bundle_carries_the_structure_section_right_after_the_scene_card(session) -> None:
    _seed(session)
    snapshot = BundleBuilder(session).build(PROACTIVE_ID)["snapshot"]
    digest = snapshot["inline_digests"][SCENE_STRUCTURE_SECTION_KEY]
    assert digest.startswith("Scene form: proactive scene")
    assert f"Setback (挫折): {SETBACK}" in digest
    assert snapshot["source_version_refs"][SCENE_STRUCTURE_SECTION_KEY] == PROACTIVE_ID
    slots = [item["slot"] for item in snapshot["ordered_injections"]]
    assert slots.index(SCENE_STRUCTURE_SECTION_KEY) == slots.index("scene_card") + 1
    names = [section.name for section in collect_prompt_sections(snapshot)]
    assert names.index(SCENE_STRUCTURE_SECTION_KEY) == names.index("scene_card") + 1
    # v2 归一化通道对雪花简报仍然是空的——修复没有绕过它，而是另开了事实通道
    assert "scene_writer_brief" not in snapshot["inline_digests"]

    v2_snapshot = BundleBuilder(session).build(V2_ONLY_ID)["snapshot"]
    assert SCENE_STRUCTURE_SECTION_KEY not in v2_snapshot["inline_digests"]
    assert all(item["slot"] != SCENE_STRUCTURE_SECTION_KEY for item in v2_snapshot["ordered_injections"])


def test_drafting_prompt_renders_the_section_for_neutral_and_style_first_drafts(session) -> None:
    _seed(session)
    snapshot = BundleBuilder(session).build(REACTIVE_ID)["snapshot"]
    for template_name in ("neutral_draft", "style_first_draft", "hard_qc"):
        prompt = PromptBuilder().build(snapshot, template_name)
        assert f"## {SCENE_STRUCTURE_SECTION_LABEL}" in prompt["user_prompt"], template_name
        assert f"Decision (决定): {DECISION}" in prompt["user_prompt"], template_name


def test_structure_section_survives_a_budget_that_squeezes_everything_soft() -> None:
    brief = "\n".join(
        [
            "Scene form: proactive scene (主动场景) — Goal → Conflict → Setback",
            "POV character: 林一鸣",
            "Scene crucible (坩埚): 审讯室封闭。",
            "Goal (目标): 在审讯结束前拿到离开许可。",
            "Conflict (冲突): 三轮尝试受阻。",
            f"Setback (挫折): {SETBACK}",
        ]
    )
    anchor = "".join(f"第{i}句他把杯子放回桌上，没有看她，窗外的雨声更紧了些。" for i in range(12))
    snapshot = {
        "contract_version": "BSHASH_v1",
        "stage_allowlist_name": "bundle_build_allowlist_v1",
        "scene_id": "SSB01_SC01",
        "chapter_id": "SSB01",
        "inline_digests": {
            "scene_card": " ".join(["Scene pressure"] * 40),
            SCENE_STRUCTURE_SECTION_KEY: brief,
            "previous_scene_voice_anchor": anchor,
            "style_drift_calibration": "- 逗号再密一点",
        },
    }
    for task_kind in ("drafting", "neutral_draft", "hard_qc"):
        result = apply_context_budget(
            system_prompt="System prompt.",
            task_prompt="Task prompt.",
            bundle_snapshot=snapshot,
            sections=collect_prompt_sections(snapshot),
            max_input_tokens=150,
            task_kind=task_kind,
        )
        status = result["budget"]["section_status"]
        assert status[SCENE_STRUCTURE_SECTION_KEY]["status"] == "included", task_kind
        assert f"## {SCENE_STRUCTURE_SECTION_LABEL}" in result["user_prompt"], task_kind
        assert f"Setback (挫折): {SETBACK}" in result["user_prompt"], task_kind


def test_blueprint_and_near_final_snapshots_carry_the_structure_brief(session) -> None:
    _seed(session)
    scene = _scene(session, PROACTIVE_ID)
    chapter = session.get(ChapterGoal, CHAPTER_ID)

    blueprint_source = SceneBlueprintService(session)._source_snapshot(scene, chapter)
    blueprint_snapshot = blueprint_source["snapshot"]
    assert f"Setback (挫折): {SETBACK}" in blueprint_snapshot["inline_digests"][SCENE_STRUCTURE_SECTION_KEY]
    assert blueprint_snapshot["source_version_refs"][SCENE_STRUCTURE_SECTION_KEY] == PROACTIVE_ID
    blueprint_prompt = PromptBuilder().build(blueprint_snapshot, "scene_blueprint")["user_prompt"]
    assert f"## {SCENE_STRUCTURE_SECTION_LABEL}" in blueprint_prompt
    assert "Goal (目标): 在审讯结束前拿到离开许可。" in blueprint_prompt

    near_final_source = NearFinalPlanningService(session)._source_snapshot(
        scene=scene, chapter=chapter, include_chapter_architecture=False
    )
    near_final_snapshot = near_final_source["snapshot"]
    assert f"Setback (挫折): {SETBACK}" in near_final_snapshot["inline_digests"][SCENE_STRUCTURE_SECTION_KEY]
    assert any(item["slot"] == SCENE_STRUCTURE_SECTION_KEY for item in near_final_snapshot["ordered_injections"])


def test_preflight_checks_the_beats_instead_of_asking_for_v2_intent(session) -> None:
    _seed(session)
    preflight = SceneRunPreflightService(session)

    complete = [item["code"] for item in preflight.build(_scene(session, PROACTIVE_ID), {})["warning_items"]]
    assert "SCENE_LITERARY_INTENT_INCOMPLETE" not in complete
    assert "SCENE_STRUCTURE_INCOMPLETE" not in complete

    incomplete = preflight.build(_scene(session, INCOMPLETE_ID), {})["warning_items"]
    structure_item = next(item for item in incomplete if item["code"] == "SCENE_STRUCTURE_INCOMPLETE")
    assert "conflict, setback" in structure_item["detail"]
    assert all(item["code"] != "SCENE_LITERARY_INTENT_INCOMPLETE" for item in incomplete)

    v2_only = [item["code"] for item in preflight.build(_scene(session, V2_ONLY_ID), {})["warning_items"]]
    assert "SCENE_LITERARY_INTENT_INCOMPLETE" in v2_only
    assert "SCENE_STRUCTURE_INCOMPLETE" not in v2_only


def test_switch_off_removes_the_section_everywhere(session, monkeypatch) -> None:
    _seed(session)
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_STRUCTURE_BRIEF", "false")
    scene = _scene(session, PROACTIVE_ID)
    assert render_scene_structure_brief(scene, session) is None
    snapshot = BundleBuilder(session).build(PROACTIVE_ID)["snapshot"]
    assert SCENE_STRUCTURE_SECTION_KEY not in snapshot["inline_digests"]
    assert SCENE_STRUCTURE_SECTION_KEY not in snapshot["source_version_refs"]
    chapter = session.get(ChapterGoal, CHAPTER_ID)
    blueprint_snapshot = SceneBlueprintService(session)._source_snapshot(scene, chapter)["snapshot"]
    assert SCENE_STRUCTURE_SECTION_KEY not in blueprint_snapshot["inline_digests"]


def test_prompt_templates_teach_both_scene_forms() -> None:
    import pathlib

    import yaml

    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    expectations = {
        "neutral_draft": ("2026-09-15.v12", "never on a clean win the section did not plan"),
        "style_first_draft": ("2026-09-22.v8", "may not skip the Decision"),
        "scene_blueprint": ("2026-09-15.v10", "derive the proposal from it instead of re-inventing the scene"),
        "hard_qc": ("2026-09-15.v6", "its facts are bundle facts"),
        "soft_qc": ("2026-09-22.v9", 'issue_key "scene_shape"'),
        "near_final_acceptance_review": ("2026-09-22.v9", "the scene crucible is identifiable in the prose"),
    }
    for name, (version, phrase) in expectations.items():
        template = templates[name]
        assert template["version"] == version, name
        assert "Scene Structure (Snowflake)" in template["task_prompt"], name
        assert phrase in template["task_prompt"], name
