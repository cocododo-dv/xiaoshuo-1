"""阶段 B（2026-09-13 雪花评估）：提示词、归一化、诊断三者说同一句话；规则层只拦缺失与占位。

金标准取自 Ingermanson《How to Write a Novel Using the Snowflake Method》第 20 章作者自己的
雪花设计（Goldilocks 第 20、21 场的第 9 步草图）与他的一句话概括——方法作者自己的例子在规则层
必须过，旧规则用 28 字阈值和关键词把它们判成 55 分「需修补」。
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from novel_system.db.models import SceneCard
from novel_system.services.scene_structure_brief import render_scene_structure_brief
from novel_system.services.snowflake_planner import _scene_writer_brief
from novel_system.services.snowflake_steps import (
    SCENE_FIELD_EXAMPLES,
    _normalize_scene_item,
    _scene_detail_seed,
    diagnose_scene_detail,
    diagnose_step_pressure,
    get_step_definition,
    merge_step_draft,
    step_guidance,
)
from novel_system.services.snowflake_workspace import _is_protagonist_role
from novel_system.services.snowflake_workspace_llm import StructuredCountMismatch, _sanitize_step_patch

GOLDILOCKS_SCENES = {
    "en_20_proactive": {
        "scene_id": "S20",
        "primary_form": "proactive",
        "title": "Goal, Conflict, Setback",
        "summary": "Goldilocks finds proof that the wolf is innocent. But when she shows it to Tiny Pig, he tries to kill her.",
        "crucible": "The wolf is in jail and the only evidence is time stamps she does not yet have.",
        "goal": "Get the time stamps for yesterday's events.",
        "conflict": "Papa Bear doesn't want to help, but he finally digs out his coffee receipt. Goldilocks steals the camera.",
        "setback": "Goldilocks finds the proof that the Big Bad Wolf is innocent, and shows it to Tiny Pig. He pulls out a syringe.",
    },
    "zh_20_proactive": {
        "scene_id": "S20",
        "primary_form": "proactive",
        "title": "目标、冲突、挫折",
        "summary": "金发姑娘找到狼无罪的证据，但给小小猪看时，他要杀她。",
        "crucible": "狼被关在牢里，唯一的证据是她还没拿到的时间戳。",
        "goal": "拿到昨天各事件的时间戳。",
        "conflict": "熊爸爸不想帮忙，最后还是翻出了咖啡小票；金发姑娘偷走了相机。",
        "setback": "她找到大灰狼无罪的证据并给小小猪看，小小猪掏出了注射器。",
    },
    "zh_21_reactive": {
        "scene_id": "S21",
        "primary_form": "reactive",
        "title": "反应、困境、决定",
        "summary": "金发姑娘吓得叫不出声，最后用胡椒喷雾喷了小小猪。",
        "crucible": "小小猪拿着注射器逼近，她孤身一人。",
        "reaction": "金发姑娘吓坏了。",
        "dilemma": "她跑不掉，打不过小小猪，也没处躲。",
        "decision": "她掏出胡椒喷雾，直喷他的眼睛。",
    },
    "en_21_reactive": {
        "scene_id": "S21",
        "primary_form": "reactive",
        "title": "Reaction, Dilemma, Decision",
        "summary": "Goldilocks is so terrified she can't scream, but finally manages to pepper spray Tiny Pig.",
        "crucible": "Tiny Pig is closing in with a syringe and she is alone.",
        "reaction": "Goldilocks is terrified.",
        "dilemma": "She can't run. She can't fight Tiny Pig. She can't hide.",
        "decision": "She pulls out her pepper spray and gives it to him right in the eyes.",
    },
}


# ---------------------------------------------------------------------------
# B4 · 规则层只认缺失与占位
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(GOLDILOCKS_SCENES))
def test_ingermansons_own_scene_plans_pass_the_rule_layer(key: str) -> None:
    scene = dict(GOLDILOCKS_SCENES[key])
    diagnosis = diagnose_scene_detail(scene)
    # 书里的草图没有「代价」栏——那是本项目有意的强化。阶段 H：缺代价只提醒，原著的场景计划直接「通过」。
    assert diagnosis["pressure_flags"] == [], diagnosis
    assert diagnosis["recommended_status"] == "pass"
    assert diagnosis["score"] == 100
    assert diagnosis["missing_fields"] == []
    assert any(step.startswith("建议：") and "免费选择" in step for step in diagnosis["fix_steps"])

    with_cost = dict(scene, cost_requirement="她偷了相机，从此在熊爸爸那里再无信用。")
    diagnosis = diagnose_scene_detail(with_cost)
    assert diagnosis["pressure_flags"] == [], diagnosis
    assert diagnosis["recommended_status"] == "pass"
    assert diagnosis["score"] == 100
    assert not any("免费选择" in step for step in diagnosis["fix_steps"])
    # 质量判断只剩建议：可以有，但不改状态、不扣分
    assert all(step.startswith("建议：") for step in diagnosis["fix_steps"])
    assert diagnosis["fix_steps"] == diagnosis["advice"]


def test_placeholder_and_generic_fields_are_still_flagged() -> None:
    scene = {
        "scene_id": "S1",
        "primary_form": "proactive",
        "title": "占位场",
        "summary": "一场还没写的戏",
        "crucible": "什么力量将角色困住、无法轻易逃脱？",  # 编辑器提示语原样留着
        "goal": SCENE_FIELD_EXAMPLES["goal"],  # 「应用修复补丁」写进来的例句
        "conflict": "They argue.",  # 泛泛短语
        "setback": "待补",
        "cost_requirement": "待补",
    }
    diagnosis = diagnose_scene_detail(scene)
    assert diagnosis["recommended_status"] == "maybe"
    # 阶段 H：泛泛短语（"They argue."）只给建议，占位（提示语 / 例句 / 待补）仍是旗标
    assert set(diagnosis["pressure_flags"]) == {
        "placeholder_crucible",
        "placeholder_goal",
        "placeholder_setback",
    }
    assert diagnosis["score"] < 100
    assert any("占位" in step for step in diagnosis["fix_steps"])
    assert any(step.startswith("建议：") and "泛泛短语" in step for step in diagnosis["advice"])
    # 旧的关键词标记名不再产生
    assert not any(flag.startswith("weak_") or flag == "fake_dilemma" for flag in diagnosis["pressure_flags"])


def test_short_concrete_fields_are_not_generic_by_length() -> None:
    scene = {
        "scene_id": "S2",
        "primary_form": "reactive",
        "title": "短而具体",
        "summary": "一句话",
        "crucible": "门锁了。",
        "reaction": "手抖。",
        "dilemma": "报警伤弟弟；不报警明天轮到自己。",
        "decision": "去找证人。",
        "cost_requirement": "弟弟从此不再信她。",
    }
    diagnosis = diagnose_scene_detail(scene)
    assert diagnosis["pressure_flags"] == []
    assert diagnosis["recommended_status"] == "pass"


# ---------------------------------------------------------------------------
# B1 · 一句话概括看要素不看字数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "summary",
    [
        "一个年轻女人梦想写小说，却害怕别人不喜欢她的作品",  # Ingermanson 自己的 logline 中译，24 字
        "少女为救妹妹替她参赛，却必须在杀戮竞技场里活下来",
        "A young woman has an impractical dream to write a novel, but she fears that other people won't like her writing.",
    ],
)
def test_compliant_loglines_pass_the_rule_layer(summary: str) -> None:
    diagnosis = diagnose_step_pressure("one_sentence_summary", {"summary": summary})
    assert diagnosis["pressure_flags"] == [], diagnosis
    assert diagnosis["status"] == "pass"


def test_logline_without_an_obstacle_only_gets_advice() -> None:
    # 阶段 H：「但 / 却」这类标记验证不了压力转折——只提醒，不改状态
    diagnosis = diagnose_step_pressure("one_sentence_summary", {"summary": "一个少年在雨城寻找失踪多年的父亲"})
    assert diagnosis["pressure_flags"] == []
    assert diagnosis["status"] == "pass"
    assert any(step.startswith("建议：") and "阻力" in step for step in diagnosis["fix_steps"])


def test_logline_copy_agrees_on_forty_characters() -> None:
    assert "40 字" in get_step_definition("one_sentence_summary")["description"]
    assert "不超过 40 字" in step_guidance("one_sentence_summary")["instruction"]


# ---------------------------------------------------------------------------
# B2 · 一页梗概恰好五段，一段话恰好五句
# ---------------------------------------------------------------------------


def _short_synopsis_base() -> dict:
    return merge_step_draft("short_synopsis", None)


def _paragraphs(count: int) -> list[str]:
    return [f"第{i}段：有行动、阻力、后果与局面变化的因果段落。" for i in range(1, count + 1)]


def test_short_synopsis_rejects_extra_paragraphs_instead_of_truncating() -> None:
    with pytest.raises(StructuredCountMismatch) as excinfo:
        _sanitize_step_patch(
            "short_synopsis",
            {"paragraphs": _paragraphs(7)},
            latest_by_step={},
            project_id="P",
            base=_short_synopsis_base(),
        )
    assert "恰好 5 段" in str(excinfo.value) and "7 段" in str(excinfo.value)

    kept = _sanitize_step_patch(
        "short_synopsis", {"paragraphs": _paragraphs(5)}, latest_by_step={}, project_id="P", base=_short_synopsis_base()
    )
    assert len(kept["paragraphs"]) == 5
    # 少于五段不拒绝：归一化会补空槽，诊断照常报缺
    partial = _sanitize_step_patch(
        "short_synopsis", {"paragraphs": _paragraphs(4)}, latest_by_step={}, project_id="P", base=_short_synopsis_base()
    )
    assert len(partial["paragraphs"]) == 4
    assert len(merge_step_draft("short_synopsis", partial)["paragraphs"]) == 5


def test_coach_patch_drops_the_key_instead_of_failing_the_reply() -> None:
    patch = _sanitize_step_patch(
        "short_synopsis",
        {"paragraphs": _paragraphs(6)},
        latest_by_step={},
        project_id="P",
        base=_short_synopsis_base(),
        count_policy="drop",
    )
    assert patch == {}


def test_one_paragraph_summary_rejects_a_sixth_sentence() -> None:
    base = merge_step_draft("one_paragraph_summary", None)
    with pytest.raises(StructuredCountMismatch):
        _sanitize_step_patch(
            "one_paragraph_summary",
            {"sentences": [f"第{i}句。" for i in range(1, 7)]},
            latest_by_step={},
            project_id="P",
            base=base,
        )
    padded = _sanitize_step_patch(
        "one_paragraph_summary", {"sentences": ["第一句。", "第二句。"]}, latest_by_step={}, project_id="P", base=base
    )
    assert padded["sentences"] == ["第一句。", "第二句。", "", "", ""]


# ---------------------------------------------------------------------------
# B3 · 默认形态不按奇偶交替
# ---------------------------------------------------------------------------


def test_scene_form_defaults_to_proactive_not_parity() -> None:
    assert [_normalize_scene_item({"summary": f"s{i}"}, index=i)["primary_form"] for i in range(1, 7)] == ["proactive"] * 6
    assert [_scene_detail_seed({"summary": f"s{i}"}, i)["primary_form"] for i in range(1, 7)] == ["proactive"] * 6
    assert _normalize_scene_item({"summary": "x", "primary_form": "reactive"}, index=1)["primary_form"] == "reactive"
    assert _scene_detail_seed({"summary": "x", "scene_type": "reactive"}, 1)["scene_type"] == "reactive"


def test_guidance_and_prompts_drop_mechanical_alternation() -> None:
    assert "不要机械交替" in step_guidance("scene_list")["instruction"]
    assert "反应场是少数" in step_guidance("scene_details")["instruction"]
    assert "交替" not in get_step_definition("scene_details")["description"].replace("不要机械交替", "")

    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    scene_list = templates["snowflake_generate_scene_list"]
    assert scene_list["version"] == "2026-09-14.v9"
    assert "Alternate proactive and reactive deliberately" not in scene_list["task_prompt"]
    assert "Reactive scenes should be a minority" in scene_list["task_prompt"]
    scene_details = templates["snowflake_generate_scene_details"]
    assert scene_details["version"] == "2026-09-14.v12"
    assert "measured against the protagonist" in scene_details["task_prompt"]
    one_sentence = templates["snowflake_generate_one_sentence_summary"]
    assert one_sentence["version"] == "2026-09-13.v3"
    assert "40 Chinese characters" in one_sentence["task_prompt"]
    assert "15-25" not in one_sentence["task_prompt"]
    short_synopsis = templates["snowflake_generate_short_synopsis"]
    assert short_synopsis["version"] == "2026-09-13.v3"
    assert "exactly 5 paragraphs" in short_synopsis["task_prompt"]
    assert "5-9 paragraphs" not in short_synopsis["task_prompt"]


# ---------------------------------------------------------------------------
# B5 · 挫折以主角衡量
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("主角", True),
        ("女主角", True),
        ("主角 / 叙述者", True),
        ("对手", False),
        ("主角的对手", False),
        ("反派", False),
        ("lead", True),
        ("Protagonist", True),
        ("heroine", True),
        ("opposition", False),
        ("Antagonist", False),
        ("", False),
        (None, False),
    ],
)
def test_is_protagonist_role(role, expected: bool) -> None:
    assert _is_protagonist_role(role) is expected


def test_writer_brief_carries_the_protagonist_and_the_structure_brief_renders_it() -> None:
    brief = _scene_writer_brief(
        "proactive",
        {
            "protagonist_hint": "林一鸣",
            "protagonist_character_id": "P_CHAR01",
            "scene_crucible": "审讯室封闭。",
            "goal": "在审讯结束前拿到离开许可。",
            "conflict": "三轮尝试受阻。",
            "setback": "被拘留 48 小时。",
        },
    )
    assert brief["protagonist_hint"] == "林一鸣"
    assert brief["protagonist_character_id"] == "P_CHAR01"

    pov_is_protagonist = SceneCard(
        scene_id="SSBB_SC01",
        chapter_id="SSBB",
        scene_seq=1,
        scene_goal="",
        scene_type="proactive",
        pov_character_id="P_CHAR01",
        writer_brief_json=brief,
    )
    text = render_scene_structure_brief(pov_is_protagonist)
    assert "Protagonist (挫折以此人衡量): 林一鸣" in text
    assert "is not the protagonist" not in text

    antagonist_pov = SceneCard(
        scene_id="SSBB_SC02",
        chapter_id="SSBB",
        scene_seq=2,
        scene_goal="",
        scene_type="proactive",
        pov_character_id="P_CHAR02",
        writer_brief_json=brief,
    )
    text = render_scene_structure_brief(antagonist_pov)
    assert "Protagonist (挫折以此人衡量): 林一鸣 — the POV character is not the protagonist" in text

    # 没有主角提示时不渲染这一行
    without = _scene_writer_brief("proactive", {"goal": "x", "conflict": "y", "setback": "z", "scene_crucible": "w"})
    assert without["protagonist_hint"] is None
    plain = SceneCard(scene_id="SSBB_SC03", chapter_id="SSBB", scene_seq=3, scene_goal="", scene_type="proactive", writer_brief_json=without)
    assert "Protagonist" not in render_scene_structure_brief(plain)


# ---------------------------------------------------------------------------
# B2 · 整步生成：数量契约被违反时重试一次，再错就如实报错
# ---------------------------------------------------------------------------


def _install_llm(monkeypatch, responder):
    from novel_system.services import snowflake_workspace_llm as mod

    monkeypatch.setattr(
        mod, "execute_accounted_call", lambda session, client, request, context, *, llm_call_id: responder(request)
    )
    monkeypatch.setattr(mod, "mark_postprocess_failure", lambda session, llm_call_id, **kwargs: None)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_llm_enabled", lambda self: True)
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_client", lambda self: object())
    monkeypatch.setattr(mod.SnowflakeWorkspaceLLMService, "_supplement_accounted_call", lambda self, **kwargs: None)


def _payload_of(request) -> dict:
    import json

    prompt = "\n".join(str(m.get("content", "")) for m in request.messages)
    return json.loads(prompt.split("Working payload:\n", 1)[1].rsplit("\n\nRequired top-level", 1)[0])


def _respond(payload: dict):
    import json

    from novel_system.services.llm_client import LLMResponse

    return LLMResponse(
        request_id="r",
        provider="p",
        model="m",
        text=json.dumps(payload, ensure_ascii=False),
        structured_output=payload,
        response_format="json_object",
        raw_response={},
        usage={},
        finish_reason="stop",
    )


def _seed_synopsis_project(session, project_id: str) -> None:
    from novel_system.db.models import StoryProject

    session.add(
        StoryProject(
            project_id=project_id,
            title="五段契约",
            outline_text="五段契约大纲",
            planning_mode="snowflake",
            snowflake_workflow_mode="explore",
            target_word_count=100000,
        )
    )
    session.flush()


def test_short_synopsis_generation_retries_once_with_the_rejection_reason(session, monkeypatch) -> None:
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    calls: list[dict] = []

    def responder(request):
        payload = _payload_of(request)
        calls.append(payload)
        count = 7 if len(calls) == 1 else 5
        return _respond({"paragraphs": _paragraphs(count)})

    _install_llm(monkeypatch, responder)
    _seed_synopsis_project(session, "prj-five")

    result = SnowflakeWorkspaceService(session).generate_step("prj-five", "short_synopsis", {})

    assert len(calls) == 2
    assert "completeness_repair" not in calls[0]
    repair = calls[1]["completeness_repair"]
    assert "7 段" in repair["instruction"] and "exactly the required number" in repair["instruction"]
    assert len(result["step"]["draft"]["paragraphs"]) == 5
    assert result["step"]["draft"]["paragraphs"][4].startswith("第5段")


def test_short_synopsis_generation_fails_loudly_after_the_second_count_mismatch(session, monkeypatch) -> None:
    from novel_system.services.errors import DomainError
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    calls: list[dict] = []

    def responder(request):
        calls.append(_payload_of(request))
        return _respond({"paragraphs": _paragraphs(8)})

    _install_llm(monkeypatch, responder)
    _seed_synopsis_project(session, "prj-eight")

    with pytest.raises(DomainError) as excinfo:
        SnowflakeWorkspaceService(session).generate_step("prj-eight", "short_synopsis", {})

    assert excinfo.value.code == "SNOWFLAKE_LLM_RESPONSE_INVALID_SCHEMA"
    assert excinfo.value.details["count_mismatch"] is True
    assert excinfo.value.details["next_action"] == "regenerate_with_exact_count"
    assert "8 段" in excinfo.value.message
    assert len(calls) == 2  # 只重试一次，不会无限循环


# ---------------------------------------------------------------------------
# 阶段 D（方法保真）：07 五段展开、角色表故事线、06 视角故事
# ---------------------------------------------------------------------------


def _long_synopsis_base() -> dict:
    return merge_step_draft("long_synopsis", None)


def test_long_synopsis_default_has_five_expansion_slots_and_never_truncates() -> None:
    """书里的第 6 步：一页梗概的五段各扩成约一页。默认五槽；归一化只补空槽，作者手写的段绝不静默丢。"""
    from novel_system.services.snowflake_steps import LONG_SYNOPSIS_PARAGRAPHS

    assert LONG_SYNOPSIS_PARAGRAPHS == 5
    assert _long_synopsis_base()["paragraphs"] == ["", "", "", "", ""]
    assert _long_synopsis_base()["chapters"] == []
    legacy = merge_step_draft("long_synopsis", {"paragraphs": ["第一幕", "第二幕", "第三幕", ""], "chapters": []})
    assert legacy["paragraphs"] == ["第一幕", "第二幕", "第三幕", "", ""]
    handwritten = merge_step_draft("long_synopsis", {"paragraphs": _paragraphs(6)})
    assert len(handwritten["paragraphs"]) == 6, "归一化不得截断作者的段落"


def test_long_synopsis_generation_rejects_a_sixth_expansion_paragraph() -> None:
    with pytest.raises(StructuredCountMismatch) as excinfo:
        _sanitize_step_patch(
            "long_synopsis",
            {"paragraphs": _paragraphs(6), "chapters": []},
            latest_by_step={},
            project_id="P",
            base=_long_synopsis_base(),
        )
    assert "恰好 5 段" in str(excinfo.value) and "6 段" in str(excinfo.value) and "章表另列" in str(excinfo.value)

    kept = _sanitize_step_patch(
        "long_synopsis", {"paragraphs": _paragraphs(5)}, latest_by_step={}, project_id="P", base=_long_synopsis_base()
    )
    assert len(kept["paragraphs"]) == 5
    # 教练补丁：丢键不报废回复（与一页梗概同一策略）
    dropped = _sanitize_step_patch(
        "long_synopsis", {"paragraphs": _paragraphs(7)}, latest_by_step={}, project_id="P",
        base=_long_synopsis_base(), count_policy="drop",
    )
    assert "paragraphs" not in dropped


def test_long_synopsis_generation_retries_once_with_the_rejection_reason(session, monkeypatch) -> None:
    from novel_system.services.snowflake_workspace import SnowflakeWorkspaceService

    calls: list[dict] = []
    chapters = [
        {"row_uid": "", "chapter_seq": 1, "act": 1, "title": "雨夜来信", "summary": "信件迫使她回乡", "spine": "灾一", "chapter_goal": "把她钉在雨城"},
        {"row_uid": "", "chapter_seq": 2, "act": 2, "title": "旧屋回声", "summary": "旧证词出现裂缝", "spine": "", "chapter_goal": "让证词开始反噬"},
    ]

    def responder(request):
        payload = _payload_of(request)
        calls.append(payload)
        count = 6 if len(calls) == 1 else 5
        return _respond({"paragraphs": _paragraphs(count), "chapters": chapters})

    _install_llm(monkeypatch, responder)
    _seed_synopsis_project(session, "prj-outline-five")

    result = SnowflakeWorkspaceService(session).generate_step("prj-outline-five", "long_synopsis", {})

    assert len(calls) == 2
    repair = calls[1]["completeness_repair"]
    assert "6 段" in repair["instruction"] and "exactly the required number" in repair["instruction"]
    draft = result["step"]["draft"]
    assert len(draft["paragraphs"]) == 5 and draft["paragraphs"][4].startswith("第5段")
    assert [c["title"] for c in draft["chapters"]] == ["雨夜来信", "旧屋回声"], "章表照常整表落库"


def test_prose_expansions_never_parse_into_chapters() -> None:
    """五段展开是散文，不是章行镜像：没有 chapters 时回退解析必须得到空表，绝不造假章。"""
    from novel_system.services.snowflake_chaptering import parse_outline_chapters

    prose = {"paragraphs": _paragraphs(5), "chapters": []}
    assert parse_outline_chapters(prose) == []
    # 历史草稿的章行格式仍然认得
    legacy = {"paragraphs": ["01 雨夜来信：信件迫使她回乡（灾一）\n02 旧屋回声：旧证词出现裂缝", "", "", ""]}
    parsed = parse_outline_chapters(legacy)
    assert [c["title"] for c in parsed] == ["雨夜来信", "旧屋回声"]
    assert parsed[0]["spine"] == "灾一"
    # 结构化章表优先，散文段落不干扰
    both = {"paragraphs": _paragraphs(5), "chapters": [{"act": 2, "title": "中点", "summary": "养母", "spine": "灾二"}]}
    assert [c["title"] for c in parse_outline_chapters(both)] == ["中点"]


def test_reference_instructions_carry_storylines_pov_story_and_five_expansions() -> None:
    from novel_system.services.snowflake_steps import _REFERENCE_STEP_INSTRUCTIONS, get_step_definition

    sheets = _REFERENCE_STEP_INSTRUCTIONS["character_sheets"]
    assert "一句话故事线" in sheets and "一段话故事线" in sheets and "没有什么比___更重要" in sheets
    assert "视角故事" in _REFERENCE_STEP_INSTRUCTIONS["character_synopses"]
    outline = _REFERENCE_STEP_INSTRUCTIONS["long_synopsis"]
    assert "恰好五段" in outline and "章节表" in outline
    template = next(f for f in get_step_definition("character_sheets")["editor"]["fields"] if f["key"] == "characters")["template"]
    assert {"one_sentence_summary", "one_paragraph_summary", "values"} <= set(template)
    label = next(f for f in get_step_definition("long_synopsis")["editor"]["fields"] if f["key"] == "paragraphs")["label"]
    assert "五段展开" in label


def test_character_sheet_diagnosis_advises_storyline_and_values_without_flagging() -> None:
    """阶段 B 的纪律：启发式只给建议，不降状态。故事线 / 价值观条数缺了是「建议」，不是旗标。"""
    full = {
        "characters": [
            {
                "display_name": "林岑", "role": "主角", "goal": "查清谁改了档案，但恩师挡路",
                "ambition": "被看见", "conflict": "恩师挡路，但她必须交出证据", "epiphany": "给活人",
                "values": ["没有什么比真相更重要", "没有什么比弟弟活着更重要"],
                "one_sentence_summary": "林岑必须交出母本，但交出去弟弟就没了退路。",
            }
        ]
    }
    diagnosis = diagnose_step_pressure("character_sheets", full)
    assert not any("故事线" in step for step in diagnosis["fix_steps"])

    thin = {"characters": [{**full["characters"][0], "values": ["没有什么比真相更重要"], "one_sentence_summary": ""}]}
    thin_diagnosis = diagnose_step_pressure("character_sheets", thin)
    advice = [step for step in thin_diagnosis["fix_steps"] if step.startswith("建议：")]
    assert advice and "林岑" in advice[0] and "一句话故事线" in advice[0] and "没有什么比___更重要" in advice[0]
    assert thin_diagnosis["pressure_flags"] == diagnosis["pressure_flags"], "建议不产生旗标"
    assert thin_diagnosis["pressure_status"] == diagnosis["pressure_status"]


def test_phase_d_prompt_versions_and_contracts() -> None:
    """阶段 D 的提示词契约：角色表故事线 + 价值观句式、06 六前缀行、07 恰好五段展开、09 读五段、准定稿评审出场景三问。"""
    templates = yaml.safe_load(
        (pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml").read_text(encoding="utf-8")
    )["templates"]
    sheets = templates["snowflake_generate_character_sheets"]
    assert sheets["version"] == "2026-09-14.v6"
    assert "没有什么比___更重要" in sheets["task_prompt"] and "in tension" in sheets["task_prompt"]
    assert "one_sentence_summary (this character's own storyline" in sheets["task_prompt"]

    synopses = templates["snowflake_generate_character_synopses"]
    assert synopses["version"] == "2026-09-13.v5"
    assert "exactly these six prefixed lines" in synopses["task_prompt"] and "视角故事：" in synopses["task_prompt"]

    outline = templates["snowflake_generate_long_synopsis"]
    assert outline["version"] == "2026-09-14.v6"
    assert "exactly 5 paragraphs" in outline["task_prompt"] and "600-1000 Chinese characters" in outline["task_prompt"]
    assert "never write chapter lists or headings into `paragraphs`" in outline["task_prompt"]
    assert "`chapters` is the source of truth for chapter membership" in outline["task_prompt"]

    scene_list = templates["snowflake_generate_scene_list"]
    assert scene_list["version"] == "2026-09-14.v9"
    assert "long_synopsis.paragraphs are five page-length expansions" in scene_list["task_prompt"]
    assert "视角故事" in scene_list["task_prompt"]

    review = templates["near_final_acceptance_review"]
    assert review["version"] == "2026-09-14.v7"
    assert "Always fill scene_story_check" in review["task_prompt"]
    assert "must not change near_final_status or pass_flag" in review["task_prompt"]
    schema = review["structured_schema"]
    assert "scene_story_check" in schema["required"]
    check = schema["properties"]["scene_story_check"]
    assert check["required"] == ["crucible_identified", "shape_landed", "verdict", "note"]
    assert check["properties"]["verdict"]["enum"] == ["yes", "no", "maybe"]
