"""2026-09-22 结构跟随参考书 / 简报气质让位——评估后的实现（契约文档 docs/style-reference-first-2026-09-22.md §6）。

覆盖：结构画像 v2 的章题（计算 / 渲染 / 旧画像惰性补算 / AI 起章名载荷）、参考作者的场尺度
（章长中位 ÷ 本章场数 → 起草硬范围上限与长度指引）、章内位置（结构简报事实行、样例窗口标签、
开章 / 收章收口指令）、规划产物随设计 / 绑定变化作废、模板版本与措辞。
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from novel_system.db.models import (
    ChapterGoal,
    GenerationPlanningArtifact,
    SceneBlueprint,
    SceneCard,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    StoryCharacter,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.services import scene_generation as sg
from novel_system.services.prompt_builder import load_prompt_templates
from novel_system.services.scene_planning_staleness import (
    supersede_for_binding_scope,
    supersede_scene_planning_artifacts,
)
from novel_system.services.scene_structure_brief import _chapter_position_line, render_scene_structure_brief
from novel_system.services.style_prompt_injection import attach_chapter_position_mandate, chapter_position_mandate
from novel_system.services.style_reference.schemas import FEW_SHOT_CLOSING_MANDATE, FEW_SHOT_CLOSING_MANDATE_FINAL
from novel_system.services.style_reference.injection import _window_position_tag
from novel_system.services.style_reference.planning_context import (
    STRUCTURE_REFERENCE_HOW_TO_USE,
    chapter_titles_for_book,
    reference_titles_payload,
    render_planning_reference,
)
from novel_system.services.style_reference.structure import (
    REFERENCE_SCENE_CHARS_CEILING,
    chapter_boundary_habits,
    chapter_titles_summary,
    compute_structure_card,
    reference_scene_scale,
    render_structure_card_parts,
    title_name_part,
)

PROMPTS = pathlib.Path(__file__).resolve().parents[2] / "config" / "prompts.yaml"


# ---------------------------------------------------------------------------
# 结构画像 v2：章题
# ---------------------------------------------------------------------------


def _rows() -> list[dict]:
    """四章的合成书：三章带题名、一章只有编号；正文段够长，章首 / 章尾样例池不退化。"""
    rows: list[dict] = []
    index = 0

    def add(text: str, ptype: str = "narration") -> None:
        nonlocal index
        rows.append({"text": text, "paragraph_type": ptype, "paragraph_index": index})
        index += 1

    for title in ("第一章 铁门", "第二章 河边的信", "第三章 夜航船", "第四章"):
        add(title, "transition")
        for k in range(6):
            add(f"{title}正文第{k}段，" + "河水在夜里慢慢涨上来，铁门后面的人不说话，只是听。" * 6)
            add("“你听见了吗？”她问。“听见了。”他说。" * 3, "dialogue")
    return rows


def test_structure_card_v2_carries_chapter_titles() -> None:
    card = compute_structure_card(_rows())
    assert card["version"] == "structure_card_v2"
    titles = card["chapter_titles"]
    assert titles["count"] == 4 and titles["named_count"] == 3 and titles["marker_style"] == "第X章式"
    assert set(titles["samples"]) == {"铁门", "河边的信", "夜航船"}
    assert titles["name_chars"]["median"] >= 2
    assert [entry["title"] for entry in card["chapters"]] == ["第一章 铁门", "第二章 河边的信", "第三章 夜航船", "第四章"]


def test_structure_card_renders_title_line_and_title_samples() -> None:
    card = compute_structure_card(_rows())
    stats, samples = render_structure_card_parts({"structure_card": card})
    assert "- 章题：4 章有章题，形态「第X章式」，其中 3 章带题名" in stats
    assert "章题样例（题名部分，编号由系统另加）：" in samples and "「铁门」" in samples
    # 旧画像（v1，没有 chapter_titles）：不渲染章题；调用方惰性算出的章题可以补进去
    legacy = {key: value for key, value in card.items() if key != "chapter_titles"}
    stats_v1, samples_v1 = render_structure_card_parts({"structure_card": legacy})
    assert "- 章题：" not in stats_v1 and "章题样例" not in samples_v1
    stats_lazy, samples_lazy = render_structure_card_parts(
        {"structure_card": legacy}, chapter_titles=card["chapter_titles"]
    )
    assert "- 章题：4 章有章题" in stats_lazy and "「夜航船」" in samples_lazy
    # 章题不进不含样例的渲染（题名是原文）
    _stats, no_samples = render_structure_card_parts({"structure_card": card}, include_samples=False)
    assert no_samples == ""


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("第一幕 卡塞尔之门 The Gate to Cassell", "卡塞尔之门 The Gate to Cassell"),
        ("第二十一章 小丑", "小丑"),
        ("序 章 白帝城 Bai Di Cheng", "白帝城 Bai Di Cheng"),
        ("《龙族3：黑月之潮（中）》", "龙族3：黑月之潮（中）"),
        ("第二十章", ""),
        ("Chapter 3: The Gate", "The Gate"),
        ("三、归来", "归来"),
        ("（一）", ""),
        ("", ""),
    ],
)
def test_title_name_part_strips_numbering(title: str, name: str) -> None:
    assert title_name_part(title) == name


def test_chapter_titles_summary_samples_only_the_dominant_marker_style() -> None:
    summary = chapter_titles_summary(["《龙族3》", "序 章 白帝城", "第一幕 卡塞尔之门", "第二幕 黄金瞳", "第三章 恺撒", "第四章"])
    assert summary["count"] == 6 and summary["marker_style"] == "第X章式" and summary["named_count"] == 5
    assert summary["samples"] == ["卡塞尔之门", "黄金瞳", "恺撒"]
    assert chapter_titles_summary([])["count"] == 0 and chapter_titles_summary(["", "  "])["samples"] == []


# ---------------------------------------------------------------------------
# 参考作者的场尺度
# ---------------------------------------------------------------------------


_DRAGON_CARD = {
    "chapter_count": 90,
    "chapter_chars": {"median": 17948, "p10": 8559, "p90": 33371},
    "scene_break_style": "none",
    "scene_chars": {"median": 0, "p10": 0, "p90": 0},
    "opening_type_distribution": {"narration": 47, "dialogue": 28},
    "closing_type_distribution": {"narration": 40, "dialogue": 39},
}


def test_reference_scene_scale_divides_the_chapter_median_by_this_chapters_scene_count() -> None:
    scale = reference_scene_scale(_DRAGON_CARD, scenes_in_chapter=5)
    assert scale["basis"] == "chapter_median_over_scenes" and scale["derived_scene_chars"] == 3590
    assert scale["scenes_in_chapter"] == 5 and scale["ceiling"] == REFERENCE_SCENE_CHARS_CEILING
    # 两场的章：推算 8974，封顶 5000（raw 保留）
    capped = reference_scene_scale(_DRAGON_CARD, scenes_in_chapter=2)
    assert capped["derived_scene_chars"] == 5000 and capped["raw_scene_chars"] == 8974
    assert reference_scene_scale(_DRAGON_CARD, scenes_in_chapter=2, ceiling=8000)["derived_scene_chars"] == 8000
    # 有显式场界的书直接用场长中位
    explicit = {**_DRAGON_CARD, "scene_break_style": "explicit", "scene_chars": {"median": 1500, "p10": 900, "p90": 2400}}
    assert reference_scene_scale(explicit, scenes_in_chapter=5)["basis"] == "explicit_scene_breaks"
    assert reference_scene_scale(explicit, scenes_in_chapter=5)["derived_scene_chars"] == 1500
    # 单章书 / 没有章长 / 没有画像 → None
    assert reference_scene_scale({**_DRAGON_CARD, "chapter_count": 1}, scenes_in_chapter=3) is None
    assert reference_scene_scale({"chapter_count": 3, "chapter_chars": {"median": 0}}, scenes_in_chapter=3) is None
    assert reference_scene_scale(None, scenes_in_chapter=3) is None


def test_chapter_boundary_habits_read_the_dominant_types() -> None:
    assert chapter_boundary_habits(_DRAGON_CARD) == {"opening": "叙述", "closing": "叙述"}
    assert chapter_boundary_habits(None) == {"opening": "", "closing": ""}


def _scale_payload(**overrides) -> dict:
    payload = {
        "basis": "chapter_median_over_scenes",
        "derived_scene_chars": 3590,
        "raw_scene_chars": 3590,
        "chapter_chars": {"median": 17948, "p10": 8559, "p90": 33371},
        "scene_chars_median": None,
        "scenes_in_chapter": 5,
        "ceiling": 5000,
    }
    payload.update(overrides)
    return payload


def test_length_band_upper_bound_rises_to_the_reference_scale_under_style_first() -> None:
    scene = SimpleNamespace(target_length_band="1200-1500")
    slack_token = sg._LENGTH_BAND_SLACK.set(0.5)
    scale_token = sg._REFERENCE_SCENE_SCALE.set(_scale_payload())
    try:
        assert sg._parse_numeric_length_band("1200-1500") == (600, 5000)
        # 显式 slack（计划值）不看参考尺度
        assert sg._parse_numeric_length_band("1200-1500", slack=0.0) == (1200, 1500)
        # 作者自己的带已经高于参考尺度：放宽后的带更大，取大
        assert sg._parse_numeric_length_band("4000-4800") == (2000, 7200)
        guide = sg._style_first_length_instruction(scene)
        assert "planned 1200-1500" in guide and "hard range 600-5000" in guide
        assert "a chapter runs about 17,948 characters (8,559–33,371 is normal)" in guide
        assert "this chapter has 5 scenes, so a scene of this author's is about 3,590 visible characters" in guide
        style_guide = sg._style_length_instruction(scene, source_length=1400, style_first=True)
        assert "hard range 600-5000" in style_guide and "about 3,590 visible characters" in style_guide
    finally:
        sg._REFERENCE_SCENE_SCALE.reset(scale_token)
        sg._LENGTH_BAND_SLACK.reset(slack_token)
    # 没有参考尺度：现状（600–2250）；neutral_first（slack 0）：带原样
    slack_token = sg._LENGTH_BAND_SLACK.set(0.5)
    try:
        assert sg._parse_numeric_length_band("1200-1500") == (600, 2250)
        assert "Measured on the reference book" not in sg._style_first_length_instruction(scene)
    finally:
        sg._LENGTH_BAND_SLACK.reset(slack_token)
    assert sg._parse_numeric_length_band("1200-1500") == (1200, 1500)


def test_reference_scale_sentence_for_explicit_scene_breaks() -> None:
    sentence = sg._reference_scale_sentence(_scale_payload(basis="explicit_scene_breaks", derived_scene_chars=1500))
    assert "this author's scenes run about 1,500 visible characters" in sentence
    assert sg._reference_scale_sentence(None) == "" and sg._reference_scale_sentence({"derived_scene_chars": 0}) == ""


def test_length_band_context_reads_the_scale_from_the_bundle_only_when_slack_applies(monkeypatch) -> None:
    bundle = {"inline_digests": {"_style_reference_scene_scale": json.dumps(_scale_payload())}}
    monkeypatch.setattr(sg, "_style_first_length_slack", lambda _bundle, _scene=None: 0.5)
    with sg._length_band_slack_for(bundle):
        assert sg._REFERENCE_SCENE_SCALE.get()["derived_scene_chars"] == 3590
        assert sg._parse_numeric_length_band("1200-1500") == (600, 5000)
    assert sg._REFERENCE_SCENE_SCALE.get() is None
    monkeypatch.setattr(sg, "_style_first_length_slack", lambda _bundle, _scene=None: 0.0)
    with sg._length_band_slack_for(bundle):
        assert sg._REFERENCE_SCENE_SCALE.get() is None
        assert sg._parse_numeric_length_band("1200-1500") == (1200, 1500)
    # 坏摘要不炸
    assert sg._reference_scene_scale_from_bundle({"inline_digests": {"_style_reference_scene_scale": "{bad"}}) is None
    assert sg._reference_scene_scale_from_bundle({"inline_digests": {"_style_reference_scene_scale": json.dumps({"derived_scene_chars": 0})}}) is None


# ---------------------------------------------------------------------------
# 章内位置：事实行、窗口标签、收口指令
# ---------------------------------------------------------------------------


def test_chapter_position_line_names_first_last_only_and_middle_scenes() -> None:
    assert _chapter_position_line(SimpleNamespace(scene_seq=1, is_chapter_last=0)).startswith("Chapter position: first scene of the chapter")
    assert "closes the chapter" in _chapter_position_line(SimpleNamespace(scene_seq=3, is_chapter_last=1))
    assert "only scene" in _chapter_position_line(SimpleNamespace(scene_seq=1, is_chapter_last=1))
    assert _chapter_position_line(SimpleNamespace(scene_seq=2, is_chapter_last=0)) == "Chapter position: scene 2 of the chapter (neither opening nor closing it)"
    assert _chapter_position_line(SimpleNamespace(scene_seq=0, is_chapter_last=0)) == ""
    scene = SimpleNamespace(
        scene_seq=1,
        is_chapter_last=0,
        pov_character_id=None,
        onstage_chars_json=[],
        exit_change=None,
        hook=None,
        target_length_band="1200-1500",
        writer_brief_json={"scene_form": "proactive", "scene_crucible": "锁在屋里", "goal": "找日志", "conflict": "被拦", "setback": "日志被撕"},
    )
    brief = render_scene_structure_brief(scene, None)
    assert brief is not None
    lines = brief.splitlines()
    assert lines[0].startswith("Scene form:") and lines[1] == "Chapter position: first scene of the chapter — it opens the chapter"


def test_window_position_tag_marks_opening_closing_and_whole_windows() -> None:
    assert _window_position_tag({"position": "opening", "chapter": 12}) == "第12章·章首；"
    assert _window_position_tag({"position": "closing", "chapter": 3}) == "第3章·章末；"
    assert _window_position_tag({"position": "whole", "chapter": 0}) == "整章；"
    assert _window_position_tag({"position": "middle", "chapter": 5}) == ""
    assert _window_position_tag({}) == ""


def test_chapter_position_mandate_follows_the_marked_windows_and_the_authors_habits() -> None:
    contract = {"layers": [{"profile": {"profile_json": {"structure_card": _DRAGON_CARD}}}]}
    opening = chapter_position_mandate("opening", contract)
    assert "本场是本章的第一场" in opening and "标着「章首」的窗口" in opening and "多以叙述起手" in opening
    closing = chapter_position_mandate("closing", None)
    assert "本场是本章的最后一场" in closing and "标着「章末」的窗口" in closing and "多以" not in closing
    whole = chapter_position_mandate("whole", contract)
    assert "第一场" in whole and "最后一场" in whole
    assert chapter_position_mandate(None, contract) == "" and chapter_position_mandate("middle", contract) == ""


def test_chapter_position_mandate_sits_before_the_final_length_and_json_sentence() -> None:
    tail = "\n\n[风格样例](t)\n- (narration；连续1段窗口；3字)「他走。」\n[/风格样例]\n\n" + FEW_SHOT_CLOSING_MANDATE + "\n"
    mandate = chapter_position_mandate("opening", None)
    attached = attach_chapter_position_mandate(tail, mandate)
    assert attached.rstrip().endswith(FEW_SHOT_CLOSING_MANDATE_FINAL)
    assert attached.index(mandate) < attached.index(FEW_SHOT_CLOSING_MANDATE_FINAL)
    assert attached.count(FEW_SHOT_CLOSING_MANDATE_FINAL) == 1
    assert attach_chapter_position_mandate(tail, "") == tail and attach_chapter_position_mandate("", mandate) == ""
    # 没有那一句的尾巴：接在末尾
    assert attach_chapter_position_mandate("样例。\n", mandate).rstrip().endswith(mandate)


# ---------------------------------------------------------------------------
# 规划产物随设计 / 绑定变化作废
# ---------------------------------------------------------------------------

PROJECT = "P_SFR"
CHAPTER = "P_SFR_CH01"
SCENE = "P_SFR_CH01_SC01"


def _seed_scene(session, *, suffix: str = "") -> tuple[str, str, str]:
    project_id, chapter_id, scene_id = PROJECT + suffix, CHAPTER + suffix, SCENE + suffix
    session.add(StoryProject(project_id=project_id, title="结构跟随", outline_text="大纲。"))
    session.add(ChapterGoal(chapter_id=chapter_id, project_id=project_id, chapter_goal="第一章目标", planned_scene_count=1))
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            project_id=project_id,
            scene_seq=1,
            is_chapter_last=1,
            scene_goal="找到日志",
            writer_brief_json={"scene_form": "proactive", "goal": "g", "conflict": "c", "setback": "s", "scene_crucible": "x"},
        )
    )
    session.add(
        SceneBlueprint(
            row_id=f"bp_{scene_id}",
            scene_id=scene_id,
            chapter_id=chapter_id,
            blueprint_json={"visible_desire": "x"},
            status="accepted",
        )
    )
    session.add(
        GenerationPlanningArtifact(
            row_id=f"cp_{scene_id}",
            artifact_type="character_pressure_blueprint",
            object_type="scene",
            object_id=scene_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            payload_json={},
            status="active",
        )
    )
    session.add(
        GenerationPlanningArtifact(
            row_id=f"ca_{chapter_id}",
            artifact_type="chapter_story_architecture",
            object_type="chapter",
            object_id=chapter_id,
            chapter_id=chapter_id,
            payload_json={},
            status="active",
        )
    )
    session.commit()
    return project_id, chapter_id, scene_id


def _statuses(session, scene_id: str, chapter_id: str) -> tuple[str, str, str]:
    blueprint = session.get(SceneBlueprint, f"bp_{scene_id}")
    pressure = session.get(GenerationPlanningArtifact, f"cp_{scene_id}")
    architecture = session.get(GenerationPlanningArtifact, f"ca_{chapter_id}")
    return blueprint.status, pressure.status, architecture.status


def test_supersede_scene_planning_artifacts_flips_all_three_and_is_idempotent(session) -> None:
    _project, chapter_id, scene_id = _seed_scene(session)
    counts = supersede_scene_planning_artifacts(session, scene_ids=[scene_id], chapter_ids=[chapter_id], reason="test")
    assert (counts["scene_blueprints"], counts["character_pressure"], counts["chapter_architecture"]) == (1, 1, 1)
    assert _statuses(session, scene_id, chapter_id) == ("superseded", "superseded", "superseded")
    again = supersede_scene_planning_artifacts(session, scene_ids=[scene_id], chapter_ids=[chapter_id])
    assert (again["scene_blueprints"], again["character_pressure"], again["chapter_architecture"]) == (0, 0, 0)
    assert supersede_scene_planning_artifacts(session, scene_ids=[], chapter_ids=[])["scene_blueprints"] == 0


@pytest.mark.parametrize("scope", ["project", "scene", "character"])
def test_supersede_for_binding_scope_covers_project_scene_and_character_scopes(session, scope: str) -> None:
    project_id, chapter_id, scene_id = _seed_scene(session, suffix=f"_{scope}")
    session.add(StoryCharacter(character_id=f"c_{scope}", project_id=project_id, display_name="林山"))
    session.commit()
    ref = {"project": project_id, "scene": scene_id, "character": f"c_{scope}"}[scope]
    counts = supersede_for_binding_scope(session, scope=scope, scope_ref_id=ref)
    assert counts["scene_blueprints"] == 1 and counts["chapter_architecture"] == 1
    assert _statuses(session, scene_id, chapter_id) == ("superseded", "superseded", "superseded")
    assert supersede_for_binding_scope(session, scope=scope, scope_ref_id="missing")["scene_blueprints"] == 0
    assert supersede_for_binding_scope(session, scope=scope, scope_ref_id=None)["scene_blueprints"] == 0


def test_design_invalidation_supersedes_the_affected_scenes_planning(session, monkeypatch) -> None:
    from novel_system.services import project_runtime_invalidation as pri

    project_id, chapter_id, scene_id = _seed_scene(session, suffix="_inv")

    def fake_analyze(self, pid, step_key, *, previous_payload=None, current_payload=None):
        return {
            "step_key": step_key,
            "scope": "scoped",
            "broad": False,
            "affected_count": 1,
            "affected_scene_ids": [scene_id],
            "summary": "test",
        }

    monkeypatch.setattr(pri.SnowflakeImpactAnalyzer, "analyze", fake_analyze)
    impact = pri.ProjectRuntimeInvalidationService(session).invalidate_for_snowflake_step(project_id, "scene_details")
    assert impact["superseded_planning"]["scene_blueprints"] == 1
    assert impact["superseded_planning"]["chapter_architecture"] == 1
    assert impact["superseded_planning"]["reason"] == "snowflake_step:scene_details"
    assert _statuses(session, scene_id, chapter_id) == ("superseded", "superseded", "superseded")


def _seed_reference(session, *, seed: str, profile_json: dict, project_id: str | None = None) -> str:
    book_id, run_id, profile_id = f"sr_book_{seed}", f"sr_run_{seed}", f"sr_profile_{seed}"
    session.add(
        StyleReferenceBook(
            book_id=book_id,
            title="Public domain source",
            source_kind="path",
            cloud_policy="segments_only",
            text_checksum=f"checksum-{seed}",
            stats_json={"rights_declaration": {"declared": True, "send_rights": True}},
        )
    )
    session.add(StyleReferenceRun(run_id=run_id, book_id=book_id, status="done"))
    session.add(
        StyleReferenceProfile(
            profile_id=profile_id,
            book_id=book_id,
            run_id=run_id,
            title="Audited profile",
            status="active",
            profile_json=profile_json,
        )
    )
    if project_id:
        session.add(
            StyleReferenceInjectionBinding(
                binding_id=f"sr_bind_{seed}",
                profile_id=profile_id,
                scope="project",
                scope_ref_id=project_id,
                task_type="scene_generation",
                strategy="mixed",
                config_json={"intensity": 60},
                status="active",
            )
        )
    session.commit()
    return profile_id


def test_applying_a_profile_supersedes_the_projects_planning(session) -> None:
    from novel_system.services.style_reference.materialization import MaterializationService

    project_id, chapter_id, scene_id = _seed_scene(session, suffix="_apply")
    profile_id = _seed_reference(session, seed="apply", profile_json={"style_features": ["短句"]})
    MaterializationService(session).apply_profile(
        profile_id, scope="project", scope_ref_id=project_id, build_rag_index=False
    )
    session.commit()
    assert _statuses(session, scene_id, chapter_id) == ("superseded", "superseded", "superseded")


def test_deleting_a_binding_supersedes_its_scopes_planning(client, session) -> None:
    from novel_system.api.routes.style_reference import PATH_PREFIX

    project_id, chapter_id, scene_id = _seed_scene(session, suffix="_del")
    _seed_reference(session, seed="del", profile_json={"style_features": ["短句"]}, project_id=project_id)
    response = client.delete(
        f"{PATH_PREFIX}/bindings/sr_bind_del",
        headers={"X-Idempotency-Key": "sfr-delete-binding"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["deleted"] is True and data["superseded_planning"]["scene_blueprints"] == 1
    session.expire_all()
    assert _statuses(session, scene_id, chapter_id) == ("superseded", "superseded", "superseded")


# ---------------------------------------------------------------------------
# 旧画像的章题惰性补算、AI 起章名载荷、规划期场尺度
# ---------------------------------------------------------------------------


def _seed_paragraphs(session, book_id: str) -> None:
    texts = [
        ("第一章 铁门", "transition"),
        ("河水在夜里慢慢涨上来。", "narration"),
        ("“你听见了吗？”", "dialogue"),
        ("第二章 河边的信", "transition"),
        ("信是湿的。", "narration"),
        ("第三章", "transition"),
        ("他没有回头。", "narration"),
    ]
    offset = 0
    for index, (text, ptype) in enumerate(texts):
        session.add(
            StyleReferenceParagraph(
                paragraph_id=f"{book_id}_p{index}",
                book_id=book_id,
                paragraph_index=index,
                paragraph_type=ptype,
                start_offset=offset,
                end_offset=offset + len(text),
                text=text,
                char_count=len(text),
            )
        )
        offset += len(text)
    session.commit()


def test_legacy_card_gets_chapter_titles_lazily_from_the_paragraph_table(session) -> None:
    legacy_card = {key: value for key, value in compute_structure_card(_rows()).items() if key != "chapter_titles"}
    legacy_card["version"] = "structure_card_v1"
    _seed_reference(session, seed="lazy", profile_json={"structure_card": legacy_card})
    _seed_paragraphs(session, "sr_book_lazy")
    titles = chapter_titles_for_book(session, "sr_book_lazy")
    assert titles["count"] == 3 and titles["named_count"] == 2 and titles["samples"] == ["铁门", "河边的信"]
    assert chapter_titles_for_book(session, "sr_book_lazy") == titles  # 缓存命中
    assert chapter_titles_for_book(session, None) is None and chapter_titles_for_book(None, "x") is None
    contract = {
        "contract_hash": "h",
        "layers": [
            {
                "profile": {"profile_id": "sr_profile_lazy", "profile_json": {"structure_card": legacy_card}},
                "book": {"book_id": "sr_book_lazy", "cloud_llm_allowed_at_freeze": True},
            }
        ],
    }
    reference = render_planning_reference(contract, session=session)
    assert reference["chapter_titles"]["samples"] == ["铁门", "河边的信"]
    assert "- 章题：3 章有章题" in reference["structure_card"] and "「河边的信」" in reference["structure_samples"]
    assert reference["card"]["chapter_count"] == 4 and reference["samples_allowed"] is True


def test_reference_titles_payload_needs_an_active_project_binding(session) -> None:
    project_id = "P_SFR_titles"
    session.add(StoryProject(project_id=project_id, title="起章名", outline_text="大纲。"))
    session.commit()
    assert reference_titles_payload(session, project_id) is None
    card = compute_structure_card(_rows())
    _seed_reference(session, seed="titles", profile_json={"structure_card": card}, project_id=project_id)
    payload = reference_titles_payload(session, project_id)
    assert payload["profile_id"] == "sr_profile_titles" and payload["count"] == 4 and payload["named_count"] == 3
    assert set(payload["samples"]) == {"铁门", "河边的信", "夜航船"} and payload["marker_style"] == "第X章式"
    assert "照这位作家起题名的方式来起" in payload["how_to_use"]
    from novel_system.services.snowflake_chaptering import _reference_chapter_titles

    assert _reference_chapter_titles(session, project_id)["samples"] == payload["samples"]


def test_chapter_title_suggestions_carry_the_reference_and_reject_copied_samples(session, monkeypatch) -> None:
    from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService, WorkspaceLLMResult

    captured: dict = {}

    def fake_run(self, **kwargs):
        captured.update(kwargs)
        output = {
            "titles": [
                {"row_uid": "r1", "title": "铁门", "summary": "抄了参考的题名"},
                {"row_uid": "r2", "title": "白骨与山洪", "summary": "这一章把案子挖出来"},
            ]
        }
        return WorkspaceLLMResult(source="llm", llm_call_id=None, payload=kwargs["normalize_output"](output))

    monkeypatch.setattr(SnowflakeWorkspaceLLMService, "_run_structured_task", fake_run)
    service = SnowflakeWorkspaceLLMService(session)
    reference = {"profile_id": "p", "count": 4, "named_count": 3, "marker_style": "第X章式", "name_chars": {"median": 3}, "samples": ["铁门", "河边的信"], "how_to_use": "…"}
    result = service.chapter_title_suggestions(
        project={"project_id": "P_SFR_llm", "title": "何来"},
        book={"logline": "…"},
        chapters=[{"row_uid": "r1", "position": 1, "of": 2, "scenes": []}, {"row_uid": "r2", "position": 2, "of": 2, "scenes": []}],
        named_chapters=[],
        reference_titles=reference,
    )
    assert captured["template_name"] == "snowflake_chapter_titles_suggest"
    assert captured["prompt_payload"]["reference_titles"]["samples"] == ["铁门", "河边的信"]
    titles = [item["title"] for item in result.payload["titles"]]
    assert titles == ["白骨与山洪"]  # 照抄的参考题名当重复丢掉
    # 没有参考：载荷里没有这个键
    captured.clear()
    service.chapter_title_suggestions(project={"project_id": "P"}, book={}, chapters=[{"row_uid": "r1", "position": 1, "of": 1, "scenes": []}], named_chapters=[])
    assert "reference_titles" not in captured["prompt_payload"]


def test_project_reference_scale_uses_the_median_scenes_per_chapter(session) -> None:
    from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService

    project_id = "P_SFR_scale"
    session.add(StoryProject(project_id=project_id, title="尺度", outline_text="大纲。"))
    session.commit()
    service = SnowflakeWorkspaceLLMService(session)
    unchaptered = service._project_reference_scale(project_id, _DRAGON_CARD)
    assert unchaptered["scenes_per_chapter"] is None and unchaptered["derived_scene_chars"] is None
    assert unchaptered["chapter_chars_median"] == 17948 and "还没有分章" in unchaptered["note"]
    for chapter_index, count in ((1, 5), (2, 3)):
        session.add(SnowflakeChapterPlan(chapter_plan_id=f"cp{chapter_index}", project_id=project_id, row_uid=f"r{chapter_index}", chapter_seq=chapter_index))
        for k in range(count):
            session.add(
                SnowflakeScenePlan(
                    scene_plan_id=f"sp{chapter_index}_{k}",
                    project_id=project_id,
                    scene_id=f"{project_id}_CH0{chapter_index}_SC0{k + 1}",
                    chapter_id=f"{project_id}_CH0{chapter_index}",
                    chapter_plan_id=f"cp{chapter_index}",
                    scene_seq=k + 1,
                )
            )
    session.commit()
    scale = service._project_reference_scale(project_id, _DRAGON_CARD)
    assert scale["scenes_per_chapter"] == 5 and scale["derived_scene_chars"] == 3590
    assert "每场约 3590 字" in scale["note"]
    assert service._project_reference_scale(project_id, {"chapter_count": 1}) is None
    assert service._project_reference_scale(project_id, None) is None


# ---------------------------------------------------------------------------
# 模板与用法说明
# ---------------------------------------------------------------------------


def test_templates_are_bumped_and_say_how_structure_and_temperament_follow_the_reference() -> None:
    templates = load_prompt_templates(PROMPTS)
    review = templates["near_final_acceptance_review"]
    assert review.version == "2026-09-23.v10"
    assert "the Reader should feel line names the effect, not the register" in review.task_prompt
    assert "opens or closes its chapter" in review.task_prompt and "章首 / 章末" in review.task_prompt
    chapter_review = templates["chapter_near_final_review"]
    assert chapter_review.version == "2026-09-22.v3"
    assert "[STYLE_REFERENCE]" in chapter_review.system_prompt and "[风格样例]" in chapter_review.task_prompt
    titles = templates["snowflake_chapter_titles_suggest"]
    assert titles.version == "2026-09-22.v2" and "`reference_titles`" in titles.task_prompt
    assert "Never return one of those sample titles" in titles.task_prompt
    for name, version in (("snowflake_generate_scene_list", "2026-09-22.v11"), ("snowflake_generate_scene_details", "2026-09-22.v14")):
        template = templates[name]
        assert template.version == version, name
        assert "project_scale" in template.task_prompt and "情绪基调" in template.task_prompt, name
        assert "graver literary register the book brief may describe" in template.task_prompt, name
    assert "project_scale" in STRUCTURE_REFERENCE_HOW_TO_USE and "情绪基调" in STRUCTURE_REFERENCE_HOW_TO_USE


def test_bundle_wrapper_shape_is_read_for_the_scale_and_the_summary_exemption(monkeypatch) -> None:
    """真实管线里 bundle 是 BundleBuilder 的外壳 {"snapshot": {...}}：尺度与概述场豁免都要能从里面读到。"""
    wrapper = {"snapshot": {"inline_digests": {"_style_reference_scene_scale": json.dumps(_scale_payload())}}}
    assert sg._reference_scene_scale_from_bundle(wrapper)["derived_scene_chars"] == 3590
    assert sg._reference_scene_scale_from_bundle({"snapshot": {}}) is None and sg._reference_scene_scale_from_bundle(None) is None
    from types import SimpleNamespace

    from novel_system.services.style_policy import StylePolicy

    monkeypatch.setattr(
        sg,
        "style_policy_for_bundle",
        lambda _bundle, **_kwargs: StylePolicy(bound=True, style_first=True, mode="frozen"),
    )
    # 风格参考 v3：概述场豁免读场景卡上结构化的 rendering_mode（外壳形状的 bundle 照样判得出让位）
    summary_scene = SimpleNamespace(writer_brief_json={"rendering_mode": "summary"})
    assert sg._style_first_length_slack(wrapper, summary_scene) == 0.0
    assert sg._style_first_length_slack(wrapper, SimpleNamespace(writer_brief_json={})) > 0.0
