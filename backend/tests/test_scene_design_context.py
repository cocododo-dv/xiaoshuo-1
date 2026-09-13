"""阶段 F（2026-09-13 雪花评估第二轮）：已确认的雪花设计要到达写作，样板句不再冒充事实。

复现：阶段 A 之后起草 bundle 仍只看到第 10 步的结构简报与一句章目标——bundle_builder 不引用
任何雪花模型，02 / 03 / 04 / 06 与章表、相邻两场一个字都到不了写手面前；物化又把
「不要提前解释完整背景」「以未解决的选择、代价或发现推动下一场」这类固定文案写成 must_withhold /
hook / 空三拍的默认值，硬 QC 再把它们当 bundle 事实去卡正文。这里锁住修复：
- 设计上下文只读已确认（approved / stale）的步骤，pending_review 不算事实；
- 进入起草 bundle（紧随结构简报）与蓝图快照；预算紧时先压成要点、最后整段省略；硬 QC 不看；
- 物化不再写样板句：空槽位留空，简报明写「未规划」，回流配方与物化一致。
"""

from __future__ import annotations

import pytest

from novel_system.db.models import (
    OutlinePlan,
    RelationProfile,
    SceneCard,
    SnowflakeScenePlan,
    SnowflakeStepRun,
    StoryCharacter,
    StoryProject,
    VoiceProfile,
)
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.context_budget import apply_context_budget, collect_prompt_sections
from novel_system.services.projects import PLAN_STATUS_PENDING_REVIEW, ProjectService
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_blueprint import SceneBlueprintService
from novel_system.services.scene_design_context import (
    SCENE_DESIGN_SECTION_KEY,
    SCENE_DESIGN_SECTION_LABEL,
    build_scene_design_context,
    compress_scene_design_context,
    render_scene_design_context,
)
from novel_system.services.scene_structure_brief import (
    SCENE_STRUCTURE_SECTION_KEY,
    render_scene_structure_brief,
)
from novel_system.services.snowflake_chaptering import SnowflakeChapteringService
from novel_system.services.snowflake_planner import _scene_writer_brief
from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService
from tests.real_llm_fakes import install_online_pipeline

PROJECT_ID = "prj-design"
POV_ID = "c1"
FOE_ID = "c2"
LOGLINE = "林一鸣必须在真凶今晚动手前证明自己无罪，但唯一的证据握在想让他认罪的人手里。"
PENDING_LOGLINE = "一个还没确认的新一句话。"
SENTENCES = [
    "前科律师林一鸣在雨城重开事务所。",
    "老友的死把他拖进审讯室，他被迫接下自己的案子。",
    "他发现伪证来自警队，选择公开对抗而不是私下交易。",
    "真凶把他唯一的线人推下天台，他失去最后的证据。",
    "他用自己的执照换来证人开口，赢了官司，输了职业。",
]
PREMISE = "只有放弃体面的自保，才能换来真正的清白。"
POV_STORY = "在我看来，这座城市从来没打算放过我。" + "我每走一步都在替别人的谎言付账。" * 30


@pytest.fixture(autouse=True)
def _auto_online_pipeline(monkeypatch):
    install_online_pipeline(monkeypatch)


def _scene_rows() -> list[dict]:
    return [
        {"row_uid": "u1", "scene_seq": 1, "summary": "取账本", "primary_form": "proactive", "scene_type": "proactive",
         "location": "码头", "crucible": "退不出的困局", "pov_character_id": POV_ID, "chapter_role": "起疑", "spine": ""},
        {"row_uid": "u2", "scene_seq": 2, "summary": "审讯室里拿离开许可", "primary_form": "proactive", "scene_type": "proactive",
         "location": "审讯室", "crucible": "审讯室封闭，真凶今晚行动", "pov_character_id": POV_ID, "chapter_role": "取证", "spine": "灾一"},
        {"row_uid": "u3", "scene_seq": 3, "summary": "消化挫败", "primary_form": "reactive", "scene_type": "reactive",
         "location": "旅馆", "crucible": "无人可信", "pov_character_id": POV_ID, "chapter_role": "转向", "spine": ""},
    ]


def _seed_workspace(session) -> SnowflakeWorkspaceService:
    session.add(
        StoryProject(
            project_id=PROJECT_ID,
            title="设计上下文",
            outline_text="设计上下文大纲",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
        )
    )
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
    session.add(
        VoiceProfile(
            row_id="voice_profile_prj_design_c1_v1",
            voice_profile_id=f"VOICE_{POV_ID}",
            version=1,
            character_id=POV_ID,
            content="林一鸣的叙述声线克制、冷静。",
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
    )
    session.add(
        RelationProfile(
            row_id="relation_profile_prj_design_v1",
            relation_profile_id=f"REL_{POV_ID}_{FOE_ID}",
            left_character_id=POV_ID,
            right_character_id=FOE_ID,
            version=1,
            content="周慎握着伪造的截图，林一鸣知道对方在等他犯程序错误。",
            active_flag=1,
            runtime_eligible=1,
            runtime_eligibility_basis="direct_read",
        )
    )
    session.flush()
    service = SnowflakeWorkspaceService(session)
    service.update_step(PROJECT_ID, "scene_list", {"draft": {"scenes": _scene_rows()}})
    listed = next(step for step in service.workspace(PROJECT_ID)["steps"] if step["step_key"] == "scene_list")["draft"]["scenes"]
    details = []
    for scene in listed:
        proactive = scene["primary_form"] == "proactive"
        details.append(
            {
                **scene,
                "title": scene["summary"],
                "goal": {"u1": "拿到账本", "u2": "在审讯结束前拿到离开许可"}.get(scene["row_uid"], "") if proactive else "",
                "conflict": "三轮受阻" if proactive else "",
                "setback": {"u1": "账本被烧，线人断联", "u2": "被拘留 48 小时"}.get(scene["row_uid"], "") if proactive else "",
                "reaction": "" if proactive else "手抖，半天说不出话。",
                "dilemma": "" if proactive else "认罪换假释；抵抗则真凶得逞。",
                "decision": "" if proactive else "去找当年的证人。",
                "cost_requirement": "失去线人" if scene["row_uid"] != "u3" else "",
            }
        )
    service.update_step(PROJECT_ID, "scene_details", {"draft": {"scenes": details}})
    return service


def _seed_canon(session) -> dict[str, str]:
    """已确认的 02 / 03 / 04 / 06，外加一条**未确认**的 02 新版本（不能被当成事实）。"""
    runs = {
        "one_sentence_summary": {"summary": LOGLINE},
        "one_paragraph_summary": {"sentences": SENTENCES, "moral_premise": PREMISE},
        "character_sheets": {
            "characters": [
                {
                    "character_id": POV_ID,
                    "display_name": "林一鸣",
                    "role": "主角",
                    "goal": "证明自己没有杀老友",
                    "ambition": "重新被当成一个律师",
                    "values": ["没有什么比清白更重要", "没有什么比不再连累任何人更重要"],
                    "conflict": "警队需要一个前科律师当替罪羊",
                    "epiphany": "体面的自保比污点更致命",
                    "one_sentence_summary": "一个前科律师必须替自己辩护，但证据在想让他认罪的人手里。",
                },
                {
                    "character_id": FOE_ID,
                    "display_name": "周慎",
                    "role": "对手",
                    "goal": "让林一鸣认罪结案",
                    "one_sentence_summary": "一个想在退休前结案的警探，宁可造一份证据也不肯放走嫌疑人。",
                },
            ]
        },
        "character_synopses": {
            "characters": [
                {
                    "character_id": POV_ID,
                    "display_name": "林一鸣",
                    "role": "主角",
                    "synopsis": "信念：规则会保护守规则的人。\n旧伤：三年前被自己的当事人指认。\n欲望：清白。\n恐惧：再一次被人相信是凶手。\n关系：周慎是当年的办案警探。\n视角故事：" + POV_STORY,
                }
            ]
        },
    }
    ids: dict[str, str] = {}
    for index, (step_key, draft) in enumerate(runs.items(), start=1):
        run_id = f"run_{PROJECT_ID}_{step_key}_approved"
        session.add(
            SnowflakeStepRun(
                step_run_id=run_id,
                project_id=PROJECT_ID,
                step_key=step_key,
                version=1,
                status="approved" if step_key != "one_paragraph_summary" else "stale",
                draft_json=draft,
                approved_at="2026-09-13T00:00:00Z",
            )
        )
        ids[step_key] = run_id
    session.add(
        SnowflakeStepRun(
            step_run_id=f"run_{PROJECT_ID}_one_sentence_summary_pending",
            project_id=PROJECT_ID,
            step_key="one_sentence_summary",
            version=2,
            status="pending_review",
            draft_json={"summary": PENDING_LOGLINE},
        )
    )
    session.flush()
    return ids


def _materialize(session, service: SnowflakeWorkspaceService) -> dict:
    service.update_step(
        PROJECT_ID,
        "long_synopsis",
        {
            "draft": {
                "paragraphs": ["第一幕"],
                "chapters": [{"act": 1, "title": "雨城", "summary": "全书一章", "chapter_goal": "把林一鸣推进审讯室", "spine": "灾一"}],
            }
        },
    )
    SnowflakeChapteringService(session).autoassign(PROJECT_ID, "even")
    project = session.get(StoryProject, PROJECT_ID)
    plan_json = service._build_chaptered_outline_plan(project, service._scene_plans(PROJECT_ID))
    outline = OutlinePlan(
        plan_id="outline_plan_prj-design_01",
        project_id=PROJECT_ID,
        version=1,
        status=PLAN_STATUS_PENDING_REVIEW,
        plan_json=plan_json,
    )
    session.add(outline)
    session.flush()
    ProjectService(session).approve_outline_plan(PROJECT_ID, outline.plan_id)
    session.flush()
    return plan_json


def _plan(session, row_uid: str) -> SnowflakeScenePlan:
    return next(
        plan
        for plan in session.query(SnowflakeScenePlan).filter(SnowflakeScenePlan.project_id == PROJECT_ID).all()
        if plan.row_uid == row_uid
    )


def _card(session, row_uid: str) -> SceneCard:
    return session.get(SceneCard, _plan(session, row_uid).scene_id)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def test_design_context_renders_confirmed_design_around_the_scene(session) -> None:
    service = _seed_workspace(session)
    ids = _seed_canon(session)
    _materialize(session, service)
    card = _card(session, "u2")
    context = build_scene_design_context(card, session)
    assert context is not None
    lines = context.text.split("\n")
    assert lines[0] == f"Book logline: {LOGLINE}"
    assert PENDING_LOGLINE not in context.text  # 未确认的新版本不是事实
    spine = next(line for line in lines if line.startswith("Story spine (five sentences): "))
    assert "(1) 前科律师" in spine and "(5) 他用自己的执照" in spine
    assert f"Moral premise: {PREMISE}" in lines
    chapter = next(line for line in lines if line.startswith("Chapter: "))
    assert "第1章《雨城》" in chapter and "Act 1" in chapter and "spine 灾一" in chapter and "chapter goal: 把林一鸣推进审讯室" in chapter
    position = next(line for line in lines if line.startswith("Scene position: "))
    assert "scene 2 of 3 in the book" in position and "chapter role: 取证" in position and "spine 灾一" in position
    sheet = next(line for line in lines if line.startswith("POV character sheet — 林一鸣 (主角): "))
    assert "Goal: 证明自己没有杀老友" in sheet
    assert "Values: 没有什么比清白更重要 / 没有什么比不再连累任何人更重要" in sheet
    assert "Epiphany: 体面的自保比污点更致命" in sheet
    assert "Storyline: 一个前科律师必须替自己辩护" in sheet
    story = next(line for line in lines if line.startswith("POV story so far (视角故事, excerpt): "))
    assert story.startswith("POV story so far (视角故事, excerpt): 在我看来")
    assert story.endswith("…") and len(story) < len(POV_STORY)  # 截取，不整段照搬
    assert "Previous scene (S01, POV 林一鸣) ended on Setback: 账本被烧，线人断联" in lines
    assert "Next scene (S03) opens on Reaction: 手抖，半天说不出话。" in lines
    # stale 仍是作者确认过的内容——03 是 stale 也进上下文；引用的就是这几版
    assert set(context.step_run_ids) == {
        ids["one_sentence_summary"],
        ids["one_paragraph_summary"],
        ids["character_sheets"],
        ids["character_synopses"],
    }


def test_design_context_is_absent_without_confirmed_design_or_when_switched_off(session, monkeypatch) -> None:
    service = _seed_workspace(session)
    _materialize(session, service)
    card = _card(session, "u1")
    # 没有已确认的 02–06：只剩场景计划能给位置与相邻场，不编造书的设计
    text = render_scene_design_context(card, session)
    assert text is not None
    assert "Book logline" not in text and "POV character sheet" not in text
    assert "Next scene (S02) opens on Goal: 在审讯结束前拿到离开许可" in text
    _seed_canon(session)
    assert "Book logline" in (render_scene_design_context(card, session) or "")
    monkeypatch.setenv("NOVEL_SYSTEM_SCENE_DESIGN_CONTEXT", "false")
    assert render_scene_design_context(card, session) is None
    snapshot = BundleBuilder(session).build(card.scene_id)["snapshot"]
    assert SCENE_DESIGN_SECTION_KEY not in snapshot["inline_digests"]
    assert SCENE_DESIGN_SECTION_KEY not in snapshot["source_version_refs"]


def test_compression_keeps_the_essentials_and_drops_the_heavy_lines() -> None:
    text = "\n".join(
        [
            "Book logline: 一句话。",
            "Story spine (five sentences): (1) 一 (2) 二",
            "Moral premise: 前提。",
            "Chapter: 第1章《雨城》 · Act 1",
            "Scene position: scene 2 of 3 in the book",
            "POV character sheet — 林一鸣 (主角): Goal: 清白",
            "POV story so far (视角故事, excerpt): 很长的一段。",
            "Onstage — 周慎 (对手): 一句话。",
            "Previous scene (S01, POV 林一鸣) ended on Setback: 挫折。",
            "Next scene (S03) opens on Reaction: 反应。",
        ]
    )
    compressed = compress_scene_design_context(text)
    assert "Story spine" not in compressed and "POV story so far" not in compressed and "Onstage" not in compressed
    for kept in ("Book logline", "Moral premise", "Chapter:", "Scene position", "POV character sheet", "Previous scene", "Next scene"):
        assert kept in compressed


# ---------------------------------------------------------------------------
# 进入写作管线
# ---------------------------------------------------------------------------


def test_bundle_carries_the_design_context_right_after_the_structure_brief(session) -> None:
    service = _seed_workspace(session)
    ids = _seed_canon(session)
    _materialize(session, service)
    card = _card(session, "u2")
    snapshot = BundleBuilder(session).build(card.scene_id)["snapshot"]
    digest = snapshot["inline_digests"][SCENE_DESIGN_SECTION_KEY]
    assert digest.startswith(f"Book logline: {LOGLINE}")
    assert sorted(snapshot["source_version_refs"][SCENE_DESIGN_SECTION_KEY]) == sorted(ids.values())
    slots = [item["slot"] for item in snapshot["ordered_injections"]]
    assert slots.index(SCENE_DESIGN_SECTION_KEY) == slots.index(SCENE_STRUCTURE_SECTION_KEY) + 1
    names = [section.name for section in collect_prompt_sections(snapshot)]
    assert names.index(SCENE_DESIGN_SECTION_KEY) == names.index(SCENE_STRUCTURE_SECTION_KEY) + 1

    for template_name in ("neutral_draft", "style_first_draft"):
        prompt = PromptBuilder().build(snapshot, template_name)["user_prompt"]
        assert f"## {SCENE_DESIGN_SECTION_LABEL}" in prompt, template_name
        assert f"Moral premise: {PREMISE}" in prompt, template_name
    # 硬 QC 只审事实：设计背景不进它的提示
    hard_qc_prompt = PromptBuilder().build(snapshot, "hard_qc")["user_prompt"]
    assert f"## {SCENE_DESIGN_SECTION_LABEL}" not in hard_qc_prompt
    assert "## Scene Structure (Snowflake)" in hard_qc_prompt


def test_blueprint_snapshot_carries_the_design_context(session) -> None:
    service = _seed_workspace(session)
    _seed_canon(session)
    _materialize(session, service)
    card = _card(session, "u2")
    from novel_system.db.models import ChapterGoal

    chapter = session.get(ChapterGoal, card.chapter_id)
    snapshot = SceneBlueprintService(session)._source_snapshot(card, chapter)["snapshot"]
    assert snapshot["inline_digests"][SCENE_DESIGN_SECTION_KEY].startswith("Book logline: ")
    assert isinstance(snapshot["source_version_refs"][SCENE_DESIGN_SECTION_KEY], list)


def test_budget_compresses_then_omits_the_design_context_but_never_the_structure_brief() -> None:
    structure = "\n".join(
        [
            "Scene form: proactive scene (主动场景) — Goal → Conflict → Setback",
            "Scene crucible (坩埚): 审讯室封闭。",
            "Goal (目标): 拿到离开许可。",
            "Setback (挫折): 被拘留 48 小时。",
        ]
    )
    design = "\n".join(
        [
            f"Book logline: {LOGLINE}",
            "Story spine (five sentences): " + " ".join(f"({i}) {s}" for i, s in enumerate(SENTENCES, start=1)),
            f"Moral premise: {PREMISE}",
            "POV character sheet — 林一鸣 (主角): Goal: 证明自己没有杀老友",
            f"POV story so far (视角故事, excerpt): {POV_STORY[:300]}",
        ]
    )
    snapshot = {
        "contract_version": "BSHASH_v1",
        "stage_allowlist_name": "bundle_build_allowlist_v1",
        "scene_id": "S1",
        "chapter_id": "C1",
        "inline_digests": {
            "scene_card": " ".join(["Scene pressure"] * 20),
            SCENE_STRUCTURE_SECTION_KEY: structure,
            SCENE_DESIGN_SECTION_KEY: design,
        },
    }

    def _run(max_tokens: int, task_kind: str = "drafting"):
        return apply_context_budget(
            system_prompt="System prompt.",
            task_prompt="Task prompt.",
            bundle_snapshot=snapshot,
            sections=collect_prompt_sections(snapshot),
            max_input_tokens=max_tokens,
            task_kind=task_kind,
        )

    generous = _run(5000)
    assert generous["budget"]["section_status"][SCENE_DESIGN_SECTION_KEY]["status"] == "included"
    assert "POV story so far" in generous["user_prompt"]

    tight = _run(360)
    assert tight["budget"]["section_status"][SCENE_DESIGN_SECTION_KEY]["status"] == "compressed"
    assert "POV story so far" not in tight["user_prompt"]
    assert f"Book logline: {LOGLINE}" in tight["user_prompt"]
    assert tight["budget"]["section_status"][SCENE_STRUCTURE_SECTION_KEY]["status"] == "included"

    starved = _run(120)
    assert starved["budget"]["section_status"][SCENE_DESIGN_SECTION_KEY]["status"] == "omitted"
    assert starved["budget"]["section_status"][SCENE_STRUCTURE_SECTION_KEY]["status"] == "included"
    assert "Setback (挫折): 被拘留 48 小时。" in starved["user_prompt"]

    hard_qc = _run(5000, task_kind="hard_qc")
    assert hard_qc["budget"]["section_status"][SCENE_DESIGN_SECTION_KEY]["status"] == "omitted"


# ---------------------------------------------------------------------------
# 样板句不再冒充事实
# ---------------------------------------------------------------------------


def test_writer_brief_leaves_unplanned_slots_empty_instead_of_inventing_them() -> None:
    proactive = _scene_writer_brief("proactive", {})
    reactive = _scene_writer_brief("reactive", {})
    for brief in (proactive, reactive):
        assert brief["scene_crucible"] == ""
        assert brief["must_withhold"] == ""
        assert brief["expected_reader_emotion"] == ""
    assert proactive["goal"] == "" and proactive["conflict"] == "" and proactive["setback"] == ""
    assert reactive["reaction"] == "" and reactive["dilemma"] == "" and reactive["decision"] == ""
    # 作者真写了就原样带走
    explicit = _scene_writer_brief("proactive", {"goal": "拿到许可", "must_withhold": "别提旧案"})
    assert explicit["goal"] == "拿到许可" and explicit["must_withhold"] == "别提旧案"


def test_materialization_writes_no_boilerplate_and_resync_agrees(session) -> None:
    service = _seed_workspace(session)
    _materialize(session, service)
    full = _card(session, "u1")
    assert not full.must_include_text  # 摘要不再冒充必须包含
    assert not full.hook
    assert full.exit_change == "账本被烧，线人断联"  # 作者写的挫折仍是离场变化
    assert full.writer_brief_json.get("must_withhold", "") == ""
    brief = render_scene_structure_brief(full, session)
    assert "Must withhold" not in brief
    assert "Next-scene pull" not in brief
    reactive = _card(session, "u3")
    assert reactive.exit_change == "去找当年的证人。"
    # 物化与回流同一配方：刚物化完没有一场是「待同步」
    assert service._resync_status(PROJECT_ID, service._scene_plans(PROJECT_ID))["pending_count"] == 0


def test_prompt_templates_teach_the_design_context() -> None:
    import pathlib

    import yaml

    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    expectations = {
        "neutral_draft": ("2026-09-14.v11", "never adds an event to this scene"),
        "style_first_draft": ("2026-09-14.v6", "never restated in the prose"),
        "scene_blueprint": ("2026-09-13.v8", "ground the proposal in it"),
    }
    for name, (version, phrase) in expectations.items():
        template = templates[name]
        assert template["version"] == version, name
        assert "Scene Design Context (Snowflake)" in template["task_prompt"], name
        assert phrase in template["task_prompt"], name
    # 硬 QC 类模板不提设计上下文——它们看不到这一段
    for name in ("hard_qc", "soft_qc", "near_final_acceptance_review"):
        assert "Scene Design Context" not in templates[name]["task_prompt"], name
