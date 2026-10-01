"""雪花「整理为章节结构」曾给没有章目标、也没有章摘要的章补一句「推进本章：<章名>」（S2 1）。

这句话进了目录的章目标、主线推进，还成了没写摘要 / 题名的场的目标和唯一一拍：起草 / 蓝图 / 准定稿的提示把它当
作者定的章目标印出来，章节编排把它当章目标给作者看。物化还给每一章、每一场的写作简报抄一份固定的「参考书安全
规则」清单（S2 2）——没有人写过，也没有哪一处读它。

物化不再写这两样：没规划的章目标 / 主线推进 / 场目标就空着（章目标列 NOT NULL，存空串）。已经带着那句样板的
旧行（下一次「确认写入」之前），各处读的时候按「这一章的名字」认出它、当「没规划」——提示里没有目标行，界面是
平常的空状态；作者写的照原样给；也没有哪一道检查因此开始失败。
"""

from __future__ import annotations

import json

from novel_system.db.models import (
    ChapterGoal,
    OutlinePlan,
    SceneCard,
    SceneRunState,
    SnowflakeScenePlan,
    StoryProject,
)
from novel_system.services.snowflake_chapter_table import create_chapter_plan

PROJECT_ID = "PRJ_GOAL"
CANNED_MARK = "推进本章"
TITLE = "旧信"
CANNED = f"推进本章：{TITLE}"
AUTHORED_GOAL = "林昭把旧信交给案卷室。"


def _project(session) -> StoryProject:
    project = session.get(StoryProject, PROJECT_ID)
    if project is None:
        project = StoryProject(
            project_id=PROJECT_ID,
            title="样板章目标",
            outline_text="雨城旧案。",
            planning_mode="snowflake",
        )
        session.add(project)
        session.flush()
    return project


# ---------------------------------------------------------------------------
# 写入方：整理为章节结构 → 物化
# ---------------------------------------------------------------------------


def _scene_plan(session, uid: str, chapter_plan, **fields) -> SnowflakeScenePlan:
    plan = SnowflakeScenePlan(
        scene_plan_id=f"scene_plan_{PROJECT_ID}_{uid}",
        project_id=PROJECT_ID,
        row_uid=uid,
        scene_id=f"{PROJECT_ID}_{uid}",
        chapter_plan_id=chapter_plan.chapter_plan_id,
        chapter_id=f"{PROJECT_ID}_CH01",
        scene_type="proactive",
        status="approved",
        **fields,
    )
    session.add(plan)
    return plan


def _seed_plan_rows(session) -> tuple[StoryProject, list[SnowflakeScenePlan]]:
    """两章：「旧信」没有章目标也没有章摘要，里面一场写好了、一场整行空着；「雨城」写了章目标，里面一场空着。"""
    project = _project(session)
    unplanned = create_chapter_plan(session, PROJECT_ID, {"row_uid": "ch-a", "chapter_seq": 1, "title": TITLE})
    planned = create_chapter_plan(
        session,
        PROJECT_ID,
        {"row_uid": "ch-b", "chapter_seq": 2, "title": "雨城", "chapter_goal": AUTHORED_GOAL},
    )
    session.flush()
    scenes = [
        _scene_plan(
            session,
            "s1",
            unplanned,
            summary="林昭在码头拆开旧信",
            goal="拆开旧信",
            conflict="送信人不肯交",
            setback="信被雨水泡烂",
        ),
        _scene_plan(session, "s2", unplanned),
        _scene_plan(session, "s3", planned),
    ]
    session.flush()
    return project, scenes


def _outline(session) -> tuple[StoryProject, dict]:
    from novel_system.services.snowflake_chaptering.outline_plan import build_chaptered_outline_plan

    project, scenes = _seed_plan_rows(session)
    return project, build_chaptered_outline_plan(session, project, scenes, protagonist=None, excluded=set())


def test_the_outline_plan_leaves_an_unplanned_goal_empty_and_carries_no_safety_list(session) -> None:
    _project_row, plan = _outline(session)

    assert CANNED_MARK not in json.dumps(plan, ensure_ascii=False)
    assert "reference_safety" not in plan
    first, second = plan["chapters"]
    # 没规划的章：章目标 / 主线推进都空着
    assert (first["chapter_goal"], first["main_plot_push"]) == ("", "")
    written, blank = first["scenes"]
    assert written["scene_goal"] == "林昭在码头拆开旧信"
    # 整行空着的场：没有目标可写
    assert blank["scene_goal"] == ""
    # 作者写了章目标的章照旧：没写摘要的场接过章目标（原来就是这样）
    assert second["chapter_goal"] == AUTHORED_GOAL
    assert second["main_plot_push"] == AUTHORED_GOAL
    assert second["scenes"][0]["scene_goal"] == AUTHORED_GOAL


def test_materialization_stores_an_empty_goal_and_no_safety_list(session) -> None:
    from novel_system.services.materialization import materialize_outline_plan

    project, plan_json = _outline(session)
    outline = OutlinePlan(
        plan_id="outline_plan_goal_01", project_id=PROJECT_ID, version=1, status="pending_review", plan_json=plan_json
    )
    session.add(outline)
    session.flush()

    materialize_outline_plan(session, project, outline)
    session.flush()

    first_id, second_id = (item["chapter_id"] for item in plan_json["chapters"])
    first = session.get(ChapterGoal, first_id)
    # 不拿章名 / 章 id 冒充章目标，也不写那句样板
    assert first.chapter_goal == ""
    assert first.main_plot_push is None
    assert "reference_safety" not in (first.writer_brief_json or {})
    blank = session.get(SceneCard, f"{PROJECT_ID}_s2")
    assert (blank.scene_goal, blank.beats_json) == ("", [])
    for card in session.query(SceneCard).filter(SceneCard.project_id == PROJECT_ID):
        assert "reference_safety" not in (card.writer_brief_json or {})
        assert CANNED_MARK not in json.dumps([card.scene_goal, card.beats_json], ensure_ascii=False)
    # 作者写的章目标照旧落进目录
    assert session.get(ChapterGoal, second_id).chapter_goal == AUTHORED_GOAL


# ---------------------------------------------------------------------------
# 读取方：带着样板的旧行
# ---------------------------------------------------------------------------


def _legacy_rows(session, chapter_id: str = "GOAL_CH03", *, title: str = TITLE) -> tuple[ChapterGoal, SceneCard]:
    """上一次「确认写入」时物化的一章：章目标 / 主线推进是样板；一场写好了，一场整行空着（目标与唯一一拍也是样板）。"""
    _project(session)
    canned = f"推进本章：{title}"
    chapter = ChapterGoal(
        chapter_id=chapter_id,
        project_id=PROJECT_ID,
        planned_scene_count=2,
        chapter_goal=canned,
        main_plot_push=canned,
        narrative_json={"title": title, "act": "act1"},
        writer_brief_json={
            "source": "snowflake_method",
            "project_id": PROJECT_ID,
            "chapter_title": title,
            "reference_safety": ["参考书只进入抽象风格画像，不复制原文表达。"],
        },
    )
    session.add(chapter)
    session.flush()
    session.add(
        SceneCard(
            scene_id=f"{chapter_id}_SC01",
            chapter_id=chapter_id,
            project_id=PROJECT_ID,
            scene_seq=1,
            scene_goal="林昭在码头拆开旧信",
            beats_json=["拆开旧信", "送信人不肯交", "信被雨水泡烂"],
            pov_character_id="CHAR_LZ",
            scene_type="proactive",
            writer_brief_json={
                "source": "snowflake_method",
                "scene_form": "proactive",
                "goal": "拆开旧信",
                "conflict": "送信人不肯交",
                "setback": "信被雨水泡烂",
                "scene_crucible": "天亮前拿不到信，案卷就要归档",
            },
        )
    )
    blank = SceneCard(
        scene_id=f"{chapter_id}_SC02",
        chapter_id=chapter_id,
        project_id=PROJECT_ID,
        scene_seq=2,
        is_chapter_last=1,
        scene_goal=canned,
        beats_json=[canned],
        pov_character_id="CHAR_LZ",
        scene_type="proactive",
        writer_brief_json={
            "source": "snowflake_method",
            "scene_form": "proactive",
            "goal": "在雨城找到写信的人",
            "conflict": "门房说那人早搬走了",
            "setback": "新地址在案卷里",
            "scene_crucible": "案卷明早归档",
        },
    )
    session.add(blank)
    for card_id in (f"{chapter_id}_SC01", f"{chapter_id}_SC02"):
        session.add(SceneRunState(scene_id=card_id, scene_status="ready"))
    session.flush()
    return chapter, blank


def test_the_retired_goal_is_recognized_only_with_that_chapters_own_name(session) -> None:
    from novel_system.services.story_slots import planned_beats, planned_chapter_goal

    chapter, _blank = _legacy_rows(session)
    assert planned_chapter_goal(CANNED, chapter) == ""
    assert planned_chapter_goal(f"  {CANNED}  ", chapter) == ""
    assert planned_chapter_goal(f"推进本章：{chapter.chapter_id}", chapter) == ""  # 没起章名时样板用的是章 id
    # 别的章的名字、多写了一个字、只是以同样的字开头：都是作者写的，原样给（不去空白）
    assert planned_chapter_goal("推进本章：雨城", chapter) == "推进本章：雨城"
    assert planned_chapter_goal(f"{CANNED}。", chapter) == f"{CANNED}。"
    assert planned_chapter_goal("推进本章：林昭决定烧掉旧信", chapter) == "推进本章：林昭决定烧掉旧信"
    assert planned_chapter_goal(f" {AUTHORED_GOAL}\n", chapter) == f" {AUTHORED_GOAL}\n"
    # 空、旧的界面脚手架：没规划
    assert planned_chapter_goal(None, chapter) == ""
    assert planned_chapter_goal("待定", chapter) == ""
    assert planned_beats([CANNED, "拆开旧信"], chapter) == ["拆开旧信"]
    assert planned_beats(None, chapter) == []


def test_drafting_prompts_print_no_canned_goal_line(session) -> None:
    from novel_system.services.bundle_builder import BundleBuilder
    from novel_system.services.near_final import NearFinalPlanningService
    from novel_system.services.near_final_review import NearFinalAcceptanceService
    from novel_system.services.prompt_builder import PromptBuilder
    from novel_system.services.scene_blueprint import SceneBlueprintService

    chapter, blank = _legacy_rows(session)
    session.commit()

    snapshot = BundleBuilder(session).build(blank.scene_id)["snapshot"]
    assert snapshot["inline_digests"]["chapter_goal"] == ""
    assert CANNED_MARK not in json.dumps(snapshot["inline_digests"], ensure_ascii=False)
    prompt = PromptBuilder().build(snapshot, "neutral_draft")["user_prompt"]
    assert "## Chapter Goal" not in prompt and CANNED_MARK not in prompt

    blueprint = SceneBlueprintService(session)._source_snapshot(blank, chapter)["snapshot"]
    near_final = NearFinalPlanningService(session)._source_snapshot(
        scene=blank, chapter=chapter, include_chapter_architecture=False
    )["snapshot"]
    chapter_review = NearFinalAcceptanceService(session)._chapter_bundle(
        chapter, {"content": "林昭把信递了过去。", "source_text_ref": "chapter_assembled:x"}
    )["snapshot"]
    for item in (blueprint, near_final, chapter_review):
        assert item["inline_digests"]["chapter_goal"] == ""
        assert CANNED_MARK not in json.dumps(item["inline_digests"], ensure_ascii=False)
    assert "## Chapter Goal" not in PromptBuilder().build(blueprint, "scene_blueprint")["user_prompt"]

    # 编辑批评（可选的自动批评）提示里的「Scene goal」
    from novel_system.services.scene_run.critique import AutoCritiqueCheckpointMixin

    class _Dispatch(AutoCritiqueCheckpointMixin):
        def __init__(self, db) -> None:
            self.session = db

    assert _Dispatch(session)._scene_critique_context(blank, None).scene_goal == ""

    # 作者写的章目标照旧印
    chapter.chapter_goal = AUTHORED_GOAL
    session.commit()
    snapshot = BundleBuilder(session).build(blank.scene_id)["snapshot"]
    assert snapshot["inline_digests"]["chapter_goal"] == AUTHORED_GOAL
    assert f"## Chapter Goal\n{AUTHORED_GOAL}" in PromptBuilder().build(snapshot, "neutral_draft")["user_prompt"]


def test_the_execution_contract_takes_no_canned_goal(session) -> None:
    from novel_system.services.scene_execution import SceneExecutionContractService

    _chapter, blank = _legacy_rows(session)
    contracts = SceneExecutionContractService(session)

    # 第 10 步写了目标的场：契约照旧 active，来源快照与 payload 里都没有样板
    session.commit()
    contract = contracts.generate(blank.scene_id, actor_ref="test")
    assert contract.status == "active"
    assert contract.payload_json["goal"] == "在雨城找到写信的人"
    assert CANNED_MARK not in json.dumps(contract.payload_json, ensure_ascii=False)

    # 目标哪里都没写：以前样板「推进本章：旧信」顶替了它，契约放行了一场没有目标的戏；现在如实缺目标
    # （预检把作者指回构思第 10 步）
    blank.writer_brief_json = {**blank.writer_brief_json, "goal": ""}
    session.commit()
    contract = contracts.generate(blank.scene_id, actor_ref="test")
    assert contract.payload_json["goal"] == ""
    assert CANNED_MARK not in json.dumps(contract.payload_json, ensure_ascii=False)
    assert "goal" in contract.missing_fields_json

    # 没声明形态的场（章节编排手加的那种）：坩埚 / 节拍不拿场目标与唯一一拍里的样板兜底……
    blank.scene_type = None
    blank.writer_brief_json = {"source": "catalog_api", "goal": "在雨城找到写信的人"}
    session.commit()
    contract = contracts.generate(blank.scene_id, actor_ref="test")
    assert CANNED_MARK not in json.dumps(contract.payload_json, ensure_ascii=False)
    # ……场目标空着时，也不拿章的主线推进里的样板兜底
    blank.scene_goal = ""
    blank.beats_json = []
    session.commit()
    contract = contracts.generate(blank.scene_id, actor_ref="test")
    assert contract.payload_json["scene_crucible"] == ""
    assert CANNED_MARK not in json.dumps(contract.payload_json, ensure_ascii=False)


def test_catalog_and_payloads_show_the_empty_state(session) -> None:
    from novel_system.services.author_lifecycle import AuthorLifecycleService
    from novel_system.services.catalog import CatalogService
    from novel_system.services.chapter_final_flow import ProjectChapterFlowService
    from novel_system.services.chapter_plan_llm import empty_slot_gap_items, empty_slot_gaps
    from novel_system.services.project_payloads import chapter_payload
    from novel_system.services.scene_workbench import SceneWorkbenchService

    chapter, blank = _legacy_rows(session)
    session.commit()
    project = session.get(StoryProject, PROJECT_ID)

    catalog = CatalogService(session).chapter_payload(project, chapter, 0)
    assert (catalog["goal"], catalog["summary"]) == ("", "")
    blank_row = next(item for item in catalog["scenes"] if item["scene_id"] == blank.scene_id)
    assert blank_row["summary"] == ""
    assert CANNED_MARK not in blank_row["title"]

    lifecycle = AuthorLifecycleService(session)
    for payload in (lifecycle.serialize_chapter(chapter), lifecycle.serialize_chapter_summary(chapter)):
        assert (payload["chapter_goal"], payload["main_plot_push"]) == ("", None)
    v1 = chapter_payload(session, chapter)
    assert (v1["chapter_goal"], v1["main_plot_push"]) == ("", None)
    block = SceneWorkbenchService(session).payload(blank.scene_id, diagnostics=True)["chapter_goal"]
    assert (block["chapter_goal"], block["main_plot_push"]) == ("", None)
    project.status = "chapter_final_review"
    session.commit()
    packet = ProjectChapterFlowService(session).review_packet(project, chapter.chapter_id)
    assert packet is not None and packet["chapter_goal"] == ""

    # 章节编排的待补清单（不是 AI）：没起题名的场，题名同目录
    blank.pov_character_id = None
    session.commit()
    (item,) = [row for row in empty_slot_gap_items([blank], chapter) if row["scene_id"] == blank.scene_id]
    assert CANNED_MARK not in item["scene_label"]
    assert all(CANNED_MARK not in line for line in empty_slot_gaps([blank], chapter))

    # 作者写的原样给
    chapter.chapter_goal = AUTHORED_GOAL
    chapter.main_plot_push = "  旧信线被正式打开  "
    session.commit()
    catalog = CatalogService(session).chapter_payload(project, chapter, 0)
    assert (catalog["goal"], catalog["summary"]) == (AUTHORED_GOAL, "旧信线被正式打开")
    assert lifecycle.serialize_chapter(chapter)["main_plot_push"] == "  旧信线被正式打开  "


def test_the_design_context_and_the_proposal_target_take_no_canned_goal(session) -> None:
    from novel_system.services.author_drafts import AuthorDraftService
    from novel_system.services.scene_design_context import _chapter_line

    chapter, blank = _legacy_rows(session)
    session.commit()

    # 构思里没有这一场（章节编排手加的场）：设计上下文读目录章的章目标
    assert _chapter_line(session, blank, None) == f"Chapter: 《{TITLE}》"

    # 章节规划上下文：没起题名的场，题名不拿本章的样板目标
    from novel_system.services.chapter_planning_context import ChapterPlanningContextBuilder

    slot = ChapterPlanningContextBuilder(session)._scene_slot(blank, chapter)
    assert slot["title"] == blank.scene_id

    # 写作台续写候选的目标载荷（进续写提示）
    target = AuthorDraftService(session)._target_payload("scene", blank.scene_id)
    assert target["chapter_goal"] == ""
    assert target["scene_card"]["scene_goal"] == ""
    assert target["scene_card"]["beats"] == []


def test_chapter_set_review_counts_no_canned_goal_as_evidence(session) -> None:
    from novel_system.services.literary_quality.chapter_set import (
        _chapter_set_payoff_reveal_checks,
        _tension_dynamics_score,
    )

    # 章名里有「选择」：样板「推进本章：林昭的选择」替没写正文的章冒充了「有抉择」的证据
    chapter, _blank = _legacy_rows(session, "GOAL_CH04", title="林昭的选择")
    checks = _chapter_set_payoff_reveal_checks([chapter], [])
    assert checks["forced_choice_chapter_ids"] == []
    # 张力分项：两章都没规划主线推进 → 没有数据（0.5），不拿两句样板的长度差算起伏
    other, _ = _legacy_rows(session, "GOAL_CH05", title="雨城旧案的第二个证人")
    assert _tension_dynamics_score([chapter, other]) == 0.5
    # 作者写的照旧算
    chapter.chapter_goal = "林昭必须选择：交出旧信，还是保住证人"
    assert _chapter_set_payoff_reveal_checks([chapter], [])["forced_choice_chapter_ids"] == [chapter.chapter_id]


def test_deriving_chapter_plans_from_the_catalog_takes_no_canned_goal(session) -> None:
    from novel_system.services.snowflake_chaptering.derive import _derive_from_catalog

    chapter, blank = _legacy_rows(session)
    plan = SnowflakeScenePlan(
        scene_plan_id=f"scene_plan_{PROJECT_ID}_legacy",
        project_id=PROJECT_ID,
        scene_id=blank.scene_id,
        chapter_id=chapter.chapter_id,
    )
    session.add(plan)
    session.flush()

    (derived,) = _derive_from_catalog(session, PROJECT_ID, [plan])
    assert (derived["title"], derived["summary"], derived["chapter_goal"]) == (TITLE, "", "")
