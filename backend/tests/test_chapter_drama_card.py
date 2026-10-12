"""章节编排的戏剧卡说到做到（round 2 · B2-5 第 0 步）。

卡片以前说它「会进章节蓝图和本章每一场的 AI 起草上下文」，还给了一栏「禁止包含」；可场景运行现做的章节蓝图
（``near_final_planning`` 的规划快照）从不读它，「禁止包含」也没有哪一道检查读——只有章节编排里按「AI 生成蓝图」
时它才进提示词。现在：

- 填了的六格与「禁止包含」进场景运行现做的章节蓝图（``chapter_drama`` 段，有渲染位）；人物压力蓝图不读它；
  备注是作者留给自己的话，两个写蓝图的地方（章节编排「AI 生成蓝图」与场景运行）都不送。
- 「禁止包含」一行一条（一行就是一个按字面查的写法，``A|B`` 任一即算；不按逗号 / 顿号再拆），与场景卡
  ``forbidden_text`` 一起查：起草的验收门、运行前预检、硬质检的确定性核对、成稿门、分类器复核，起草提示里的场景卡
  也照实告诉模型（``qc_constraints`` 一处）。
- 一格都没写的卡：规划快照、哈希、提示词与以前逐字相同。
"""

from __future__ import annotations

from novel_system.db.models import ChapterGoal, SceneCard, StoryProject
from novel_system.services.chapter_planning_context import ChapterPlanningContextBuilder
from novel_system.services.final_text_gate import FinalTextGateService
from novel_system.services.hash_engine import canonical_json
from novel_system.services.near_final import NearFinalPlanningService
from novel_system.services.qc_engine import HardQcEngine
from novel_system.services.qc_constraints import (
    CHAPTER_FORBIDDEN_SOURCE,
    SCENE_FORBIDDEN_SOURCE,
    literal_forbidden_hits,
    literal_forbidden_sources,
    literal_forbidden_terms,
)
from novel_system.services.quality_classifier import classify_issue
from novel_system.services.scene_digest import scene_card_digest
from novel_system.services.scene_generation.length_policy import LengthPolicy
from novel_system.services.scene_generation.text_gates import _assess_neutral_draft
from novel_system.services.scene_run_preflight import SceneRunPreflightService
from tests.real_llm_fakes import ScenePipelineOnlineFake

PROJECT_ID = "PRJ_DRAMA"
CHAPTER_ID = "PRJ_DRAMA_CH01"
SCENE_ID = "PRJ_DRAMA_CH01_SC01"

FILLED_DRAMA = {
    "promise": "林昭在雨城找到写旧信的人",
    "problem": "写信的人为什么等了十年",
    "spine": "",
    "arc": "林昭从追问变成替人保密",
    "aftertaste": "",
    "ending": "旧信落进案卷室的火盆",
    "forbidden": "铜钥匙\n林昭说出真名",
    "notes": "给自己的话：这一章别写长",
}


def _seed(session, *, drama: dict | None = None, scene_forbidden: str = "") -> tuple[ChapterGoal, SceneCard]:
    session.add(StoryProject(project_id=PROJECT_ID, title="雨城旧信", outline_text="雨城旧案。"))
    narrative: dict = {"title": "旧信", "act": "act1"}
    if drama is not None:
        narrative["drama"] = drama
    chapter = ChapterGoal(
        chapter_id=CHAPTER_ID,
        project_id=PROJECT_ID,
        planned_scene_count=1,
        chapter_goal="林昭把旧信交给案卷室。",
        narrative_json=narrative,
    )
    scene = SceneCard(
        scene_id=SCENE_ID,
        chapter_id=CHAPTER_ID,
        project_id=PROJECT_ID,
        scene_seq=1,
        scene_goal="林昭在雨里等写信的人。",
        location="雨城旧码头",
        must_include_text="",
        forbidden_text=scene_forbidden,
        target_length_band="short",
    )
    session.add_all([chapter, scene])
    session.commit()
    return chapter, scene


def _user_prompt(request) -> str:
    return "\n".join(str(message.get("content") or "") for message in request.messages if message.get("role") == "user")


# ---------------------------------------------------------------------------
# 章节蓝图：场景运行现做的那一份也读戏剧卡
# ---------------------------------------------------------------------------


def test_a_filled_drama_card_reaches_the_scene_run_chapter_blueprint_prompt(session) -> None:
    _seed(session, drama=FILLED_DRAMA)
    client = ScenePipelineOnlineFake()

    NearFinalPlanningService(session, llm_client=client).ensure_scene_planning(SCENE_ID)

    prompts = {request.node_id: _user_prompt(request) for request in client.requests}
    architecture = prompts["chapter_story_architecture"]
    assert "## Chapter Drama Card" in architecture
    for value in ("林昭在雨城找到写旧信的人", "写信的人为什么等了十年", "林昭从追问变成替人保密", "旧信落进案卷室的火盆"):
        assert value in architecture
    assert "铜钥匙" in architecture and "林昭说出真名" in architecture
    # 备注是作者留给自己的话；空格子不进
    assert "给自己的话" not in architecture
    assert '"spine"' not in architecture and '"aftertaste"' not in architecture
    # 人物压力蓝图读的是章节蓝图本身，不另读戏剧卡
    assert "## Chapter Drama Card" not in prompts["character_pressure_blueprint"]


def test_an_empty_drama_card_changes_no_planning_snapshot_byte(session) -> None:
    chapter, scene = _seed(session)
    service = NearFinalPlanningService(session)

    def snapshot() -> dict:
        return service._source_snapshot(
            scene=scene, chapter=chapter, include_chapter_architecture=False, include_chapter_drama=True
        )

    baseline = snapshot()
    chapter.narrative_json = {
        "title": "旧信",
        "act": "act1",
        # 只有空白的格子、只写了备注：都不算填了
        "drama": {"promise": "", "ending": "  ", "forbidden": "\n", "notes": "给自己的话"},
    }
    session.commit()
    blank = snapshot()

    assert blank["source_bundle_hash"] == baseline["source_bundle_hash"]
    assert blank["snapshot"] == baseline["snapshot"]
    assert "chapter_drama" not in blank["snapshot"]["inline_digests"]

    chapter.narrative_json = {"title": "旧信", "act": "act1", "drama": {"promise": "林昭在雨城找到写旧信的人"}}
    session.commit()
    filled = snapshot()
    assert filled["source_bundle_hash"] != baseline["source_bundle_hash"]
    assert filled["snapshot"]["inline_digests"]["chapter_drama"] == '{"promise": "林昭在雨城找到写旧信的人"}'


# ---------------------------------------------------------------------------
# 「禁止包含」：与场景卡 forbidden_text 一起按字面查
# ---------------------------------------------------------------------------

DRAFT_WITH_CHAPTER_TERM = "雨还在下。林昭把铜钥匙压在旧信下面，没有回头。码头的灯一盏一盏灭了，她数到第七盏才走。"


def test_the_literal_forbidden_list_is_the_scene_card_plus_the_chapter_drama_card(session) -> None:
    chapter, scene = _seed(session, drama=FILLED_DRAMA, scene_forbidden="死亡证明、铜钥匙")

    assert literal_forbidden_sources(scene) == [
        (SCENE_FORBIDDEN_SOURCE, "死亡证明、铜钥匙"),
        (CHAPTER_FORBIDDEN_SOURCE, "铜钥匙\n林昭说出真名"),
    ]
    # 两处都写了的词只算一次；场景卡在前
    assert literal_forbidden_terms(scene) == ["死亡证明", "铜钥匙", "林昭说出真名"]
    assert literal_forbidden_hits(scene, DRAFT_WITH_CHAPTER_TERM) == [(SCENE_FORBIDDEN_SOURCE, "铜钥匙")]
    # 手搭的、不在会话里的卡：只有场景卡这一处（显式给章也行）
    detached = SceneCard(scene_id="X", chapter_id=CHAPTER_ID, scene_seq=1, forbidden_text="死亡证明")
    assert literal_forbidden_sources(detached) == [(SCENE_FORBIDDEN_SOURCE, "死亡证明")]
    assert literal_forbidden_sources(detached, chapter)[-1] == (CHAPTER_FORBIDDEN_SOURCE, "铜钥匙\n林昭说出真名")


def test_chapter_forbidden_terms_are_checked_by_every_literal_gate(session) -> None:
    chapter, scene = _seed(session, drama=FILLED_DRAMA)
    content = DRAFT_WITH_CHAPTER_TERM

    # 起草的验收门（首稿 / 中性稿）
    assessment = _assess_neutral_draft(scene, content, LengthPolicy.plain(scene))
    assert "forbidden_content_present" in assessment["reasons"]
    assert assessment["forbidden_hit_count"] == 1

    # 成稿门：确定性的 Q1，证据指向本章戏剧卡
    continuity = FinalTextGateService(session)._continuity(scene, content, allow_author_waiver=False)
    forbidden = [item for item in continuity["issues"] if item["issue_key"] == "forbidden_text"]
    assert forbidden and forbidden[0]["blocking"] is True
    assert forbidden[0]["authority_ref"] == f"chapter:{CHAPTER_ID}.drama.forbidden"

    # 分类器复核模型报来的禁用词问题：本章戏剧卡的词也能证实
    verified = classify_issue({"issue_key": "forbidden_text", "message": "出现禁用词"}, scene=scene, content=content)
    assert verified["blocking"] is True
    assert verified["evidence_spans"] == [{"text": "铜钥匙"}]
    assert verified["authority_ref"] == f"chapter:{CHAPTER_ID}.drama.forbidden"

    # 起草提示里的场景卡照实告诉模型
    digest = scene_card_digest(scene, chapter)
    assert "Forbidden in this chapter: 铜钥匙；林昭说出真名" in digest
    assert "Forbidden text:" not in digest  # 场景卡自己没写


def test_preflight_reports_a_conflict_with_the_chapter_drama_card(session) -> None:
    _chapter, scene = _seed(session, drama=FILLED_DRAMA)
    scene.hook = "林昭把铜钥匙递了出去。"
    session.commit()

    conflicts = SceneRunPreflightService(session)._constraint_conflicts(scene)

    assert conflicts == [
        {
            "term": "铜钥匙",
            "required_source": "scene_card.hook",
            "forbidden_source": CHAPTER_FORBIDDEN_SOURCE,
            "severity": "blocking",
            # 作者在任务的失败说明里看到的就是这一句：说清是哪一条、在哪张卡上、去哪里改
            "human_readable_reason": "本章戏剧卡「禁止包含」里的「铜钥匙」，这一场的钩子要求写它：去章节编排的戏剧卡删掉这一条，或改这一场的钩子。",
        }
    ]


def test_an_empty_drama_card_leaves_every_literal_check_as_before(session) -> None:
    chapter, scene = _seed(session, drama={"forbidden": "  ", "notes": "铜钥匙"}, scene_forbidden="死亡证明")

    assert literal_forbidden_sources(scene) == [(SCENE_FORBIDDEN_SOURCE, "死亡证明")]
    assert "forbidden_content_present" not in _assess_neutral_draft(
        scene, DRAFT_WITH_CHAPTER_TERM, LengthPolicy.plain(scene)
    )["reasons"]
    digest = scene_card_digest(scene, chapter)
    assert "Forbidden text: 死亡证明" in digest and "Forbidden in this chapter" not in digest


# ---------------------------------------------------------------------------
# 「禁止包含」按它自己的契约读：一行一条
# ---------------------------------------------------------------------------

WHOLE_LINE = "别让林昭和她和好，暂时"


def test_a_drama_line_is_one_term_not_split_by_commas(session) -> None:
    """卡片说「一行一条」：「别让林昭和她和好，暂时」是一条，不是「别让林昭和她和好」和「暂时」两个禁用词——
    以前按场景卡的逗号 / 顿号规则拆，正文里出现「暂时」两个字就拦。``A|B`` 照旧算备选写法、不到两个字的行不算、
    防抄袭政策句照旧剔掉；场景卡的 forbidden_text 照旧按逗号 / 顿号拆。"""
    from novel_system.services.qc_constraints import REFERENCE_POLICY_SENTENCES

    drama = {"forbidden": f"{WHOLE_LINE}\n铜钥匙|旧钥匙\n雨\n{REFERENCE_POLICY_SENTENCES[0]}"}
    _chapter, scene = _seed(session, drama=drama, scene_forbidden="死亡证明、案卷室")
    scene.hook = "林昭暂时没有回头。"
    session.commit()

    assert literal_forbidden_terms(scene) == ["死亡证明", "案卷室", WHOLE_LINE, "铜钥匙|旧钥匙"]
    near_miss = "雨还在下。林昭暂时不想见她，也没想和好，她把旧信压在灯下，一个字也没回，码头的灯一盏一盏灭了。"
    assert literal_forbidden_hits(scene, near_miss) == []
    assert literal_forbidden_hits(scene, "她握着旧钥匙。") == [(CHAPTER_FORBIDDEN_SOURCE, "旧钥匙")]

    # 起草的验收门、成稿门、预检：整行没出现就什么都不拦
    assert "forbidden_content_present" not in _assess_neutral_draft(scene, near_miss, LengthPolicy.plain(scene))["reasons"]
    continuity = FinalTextGateService(session)._continuity(scene, near_miss, allow_author_waiver=False)
    assert [item for item in continuity["issues"] if item["issue_key"] == "forbidden_text"] == []
    assert SceneRunPreflightService(session)._constraint_conflicts(scene) == []

    # 整行出现了才拦
    whole = f"雨还在下。她在信上写：{WHOLE_LINE}。码头的灯一盏一盏灭了，她数到第七盏才走，没有回头。"
    assert "forbidden_content_present" in _assess_neutral_draft(scene, whole, LengthPolicy.plain(scene))["reasons"]
    continuity = FinalTextGateService(session)._continuity(scene, whole, allow_author_waiver=False)
    assert [item["authority_ref"] for item in continuity["issues"] if item["issue_key"] == "forbidden_text"] == [
        f"chapter:{CHAPTER_ID}.drama.forbidden"
    ]
    scene.hook = f"信上写着{WHOLE_LINE}。"
    session.commit()
    assert [c["term"] for c in SceneRunPreflightService(session)._constraint_conflicts(scene)] == [WHOLE_LINE]
    # 起草提示里也是一行一条
    assert f"Forbidden in this chapter: {WHOLE_LINE}；铜钥匙|旧钥匙" in scene_card_digest(scene)


def test_the_final_gate_points_at_every_source_that_hit(session) -> None:
    """场景卡和本章戏剧卡都命中时，两处都要被指到——作者得知道去哪两个地方改。"""
    _chapter, scene = _seed(session, drama={"forbidden": "铜钥匙"}, scene_forbidden="死亡证明")
    content = "雨还在下。林昭把铜钥匙压在死亡证明下面，没有回头。码头的灯一盏一盏灭了，她数到第七盏才走。"

    continuity = FinalTextGateService(session)._continuity(scene, content, allow_author_waiver=False)
    forbidden = [item for item in continuity["issues"] if item["issue_key"] == "forbidden_text"]
    assert [(item["authority_ref"], item["evidence_spans"]) for item in forbidden] == [
        (f"scene_card:{SCENE_ID}.forbidden_text", [{"text": "死亡证明"}]),
        (f"chapter:{CHAPTER_ID}.drama.forbidden", [{"text": "铜钥匙"}]),
    ]
    assert all(item["blocking"] for item in forbidden)
    assert continuity["blockers"].count("continuity:forbidden_text") == 2  # 成稿门去重后算一条拦截理由

    only_scene = FinalTextGateService(session)._continuity(scene, "林昭把死亡证明折好。", allow_author_waiver=False)
    assert [item["authority_ref"] for item in only_scene["issues"] if item["issue_key"] == "forbidden_text"] == [
        f"scene_card:{SCENE_ID}.forbidden_text"
    ]


def test_hard_qc_keeps_a_model_finding_about_a_chapter_term(session) -> None:
    """硬质检的确定性核对：模型报「出现禁用词」而正文里确实有本章戏剧卡的词，就不是误报，不能被当成和场景卡矛盾丢掉。"""
    _chapter, scene = _seed(session, drama={"forbidden": "铜钥匙"})
    engine = HardQcEngine(session)
    payload = {"pass_flag": False, "issues": [{"issue_key": "forbidden_text", "message": "正文出现禁用词「铜钥匙」"}], "rewrite_brief": []}

    kept = engine._apply_deterministic_sanity(scene, DRAFT_WITH_CHAPTER_TERM, payload)
    assert [issue["issue_key"] for issue in kept["issues"]] == ["forbidden_text"]

    dropped = engine._apply_deterministic_sanity(scene, "雨还在下，林昭没有回头。", payload)
    assert dropped["issues"] == [] and dropped["pass_flag"] is True


def test_notes_reach_neither_blueprint_writer(session) -> None:
    """备注是作者给自己的话：章节编排的「AI 生成蓝图」与场景运行现做的蓝图都不送（chapter_drama 一张键表）。"""
    _seed(session, drama=FILLED_DRAMA)

    context = ChapterPlanningContextBuilder(session).build(PROJECT_ID, CHAPTER_ID)
    payload = canonical_json(context.prompt_payload)
    assert "给自己的话" not in payload
    assert context.prompt_payload["chapter_card"]["drama"] == {
        "promise": "林昭在雨城找到写旧信的人",
        "problem": "写信的人为什么等了十年",
        "arc": "林昭从追问变成替人保密",
        "ending": "旧信落进案卷室的火盆",
        "forbidden": "铜钥匙\n林昭说出真名",
    }
    assert context.prompt_payload["author_constraints"]["forbidden"] == "铜钥匙\n林昭说出真名"
    assert "notes" not in context.prompt_payload["author_constraints"]
