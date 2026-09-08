from __future__ import annotations

from novel_system.db.models import (
    ChapterGoal,
    SceneBlueprint,
    SceneCard,
    SceneRunState,
    StoryProject,
)
from novel_system.services.bundle_builder import BundleBuilder


import pytest as _pytest_ap
from tests.real_llm_fakes import install_online_pipeline as _install_online_pipeline


@_pytest_ap.fixture(autouse=True)
def _auto_online_pipeline(monkeypatch):
    """假生成已退役：给场景管线未显式注入的子服务兜底在线记账替身。"""
    _install_online_pipeline(monkeypatch)


CHAPTER_ID = "BP100"
SCENE_ID = "BP100_SC01"
PROJECT_ID = "P_BP100"


def _seed_scene(session) -> None:
    session.add(
        StoryProject(
            project_id=PROJECT_ID,
            title="Blueprint Fixture",
            outline_text="Blueprint fixture outline.",
        )
    )
    session.add(
        ChapterGoal(
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            planned_scene_count=1,
            chapter_goal="A quiet reunion must turn into a choice.",
            main_plot_push="move from suspicion to action",
            emotional_target="trust becomes costly",
            ending_effect="leave the reader asking what was hidden",
            writer_brief_json={
                "chapter_promise": "a reunion reveals a dangerous silence",
                "escalation_path": "warmth, evasion, decision",
                "ending_question": "why does the friend hide the name",
            },
        )
    )
    session.add(
        SceneCard(
            scene_id=SCENE_ID,
            chapter_id=CHAPTER_ID,
            project_id=PROJECT_ID,
            scene_seq=1,
            scene_goal="The protagonist asks for the missing name and must decide whether to trust an old friend.",
            beats_json=["ask for the name", "old friend deflects", "protagonist chooses to investigate"],
            exit_change="The old friend becomes a suspect.",
            hook="The teacup stills when the name is spoken.",
            writer_brief_json={
                "character_desire": "get the truth",
                "obstacle": "the friend answers with charm instead of facts",
                "choice_under_pressure": "trust the friend or investigate alone",
                "power_shift": "the protagonist stops asking permission",
                "new_information": "the friend recognizes the missing name",
                "emotional_turn": "warmth becomes suspicion",
                "image_anchor": "the still teacup",
                "reader_aftertaste": "affection now feels dangerous",
            },
        )
    )
    session.add(SceneRunState(scene_id=SCENE_ID, scene_status="ready"))
    session.commit()


def test_legacy_v1_blueprint_rows_still_surface_in_workbench_and_bundle(client, session) -> None:
    _seed_scene(session)
    legacy = SceneBlueprint(
        row_id="scene_blueprint_legacy_v1",
        scene_id=SCENE_ID,
        chapter_id=CHAPTER_ID,
        source_bundle_id="legacy_bundle",
        source_bundle_hash="legacy_hash",
        blueprint_json={
            "choice_under_pressure": "trust the friend or investigate alone",
            "ending_reader_question": "why the name was hidden",
            "image_promise": "the still teacup",
        },
        status="accepted",
    )
    session.add(legacy)
    session.commit()

    workbench = client.get(f"/api/v1/scenes/{SCENE_ID}/workbench").json()["data"]
    assert workbench["literary_blueprint"]["row_id"] == legacy.row_id
    assert workbench["literary_blueprint"]["blueprint_json"]["choice_under_pressure"] == "trust the friend or investigate alone"

    snapshot = BundleBuilder(session).build(SCENE_ID)["snapshot"]
    assert snapshot["source_version_refs"]["scene_blueprint_row_id"] == legacy.row_id
    assert "choice_under_pressure" in snapshot["inline_digests"]["scene_blueprint"]


def _seed_blueprint_style_binding(session, *, narrative_guidance: list[str] | None) -> None:
    from novel_system.db.models import (
        StyleReferenceBook,
        StyleReferenceInjectionBinding,
        StyleReferenceProfile,
        StyleReferenceRun,
    )

    profile_json: dict = {"style_features": ["短句克制"]}
    if narrative_guidance is not None:
        profile_json["narrative_guidance"] = narrative_guidance
    session.add(
        StyleReferenceBook(
            book_id="sr_book_bp",
            title="Public domain source",
            source_kind="path",
            cloud_policy="segments_only",
            text_checksum="checksum-bp",
            stats_json={"rights_declaration": {"declared": True, "send_rights": True}},
        )
    )
    session.add(StyleReferenceRun(run_id="sr_run_bp", book_id="sr_book_bp", status="done"))
    session.add(
        StyleReferenceProfile(
            profile_id="sr_profile_bp",
            book_id="sr_book_bp",
            run_id="sr_run_bp",
            title="Audited profile",
            status="active",
            profile_json=profile_json,
        )
    )
    session.add(
        StyleReferenceInjectionBinding(
            binding_id="sr_bind_bp",
            profile_id="sr_profile_bp",
            scope="project",
            scope_ref_id=PROJECT_ID,
            task_type="scene_generation",
            strategy="A",
            status="active",
        )
    )
    session.commit()


def test_blueprint_source_snapshot_injects_only_narrative_mechanisms(session) -> None:
    """v2（规格 §2.W5.4）：规划层看参考作品的叙事取舍机制，但不看语言层特征 / 原文样例。"""
    from novel_system.services.prompt_builder import PromptBuilder
    from novel_system.services.scene_blueprint import SceneBlueprintService

    _seed_scene(session)
    _seed_blueprint_style_binding(
        session, narrative_guidance=["关键信息放段首一次给出", "结尾以动作收束，不解释动机"]
    )
    service = SceneBlueprintService(session, llm_client=object())
    scene = session.get(SceneCard, SCENE_ID)
    chapter = session.get(ChapterGoal, CHAPTER_ID)
    source = service._source_snapshot(scene, chapter)
    snapshot = source["snapshot"]

    digest = snapshot["inline_digests"]["style_narrative_guidance"]
    assert "- 关键信息放段首一次给出" in digest
    assert "- 结尾以动作收束，不解释动机" in digest
    # 语言层特征不进规划层
    assert "短句克制" not in digest
    assert {"slot": "style_narrative_guidance", "digest_key": "style_narrative_guidance"}.items() <= next(
        item for item in snapshot["ordered_injections"] if item["slot"] == "style_narrative_guidance"
    ).items()
    assert snapshot["source_version_refs"]["style_narrative_guidance_line_count"] == 2
    assert len(snapshot["source_version_refs"]["style_reference_runtime_contract_hash"]) == 64

    prompt = PromptBuilder().build(snapshot, "scene_blueprint")
    assert "## Style Reference — Narrative Mechanisms" in prompt["user_prompt"]
    assert "关键信息放段首一次给出" in prompt["user_prompt"]
    assert "If a Style Reference — Narrative Mechanisms section is present" in prompt["user_prompt"]
    assert "[STYLE_REFERENCE]" not in prompt["system_prompt"]


def test_blueprint_source_snapshot_degrades_for_legacy_profiles_and_no_binding(session) -> None:
    from novel_system.services.scene_blueprint import SceneBlueprintService

    _seed_scene(session)
    service = SceneBlueprintService(session, llm_client=object())
    scene = session.get(SceneCard, SCENE_ID)
    chapter = session.get(ChapterGoal, CHAPTER_ID)

    # 无绑定
    snapshot = service._source_snapshot(scene, chapter)["snapshot"]
    assert "style_narrative_guidance" not in snapshot["inline_digests"]
    assert all(item["slot"] != "style_narrative_guidance" for item in snapshot["ordered_injections"])

    # 旧画像：绑定存在但没有 narrative_guidance 键 → 同样不注入
    _seed_blueprint_style_binding(session, narrative_guidance=None)
    snapshot = service._source_snapshot(scene, chapter)["snapshot"]
    assert "style_narrative_guidance" not in snapshot["inline_digests"]
    assert "style_reference_runtime_contract_hash" not in snapshot["source_version_refs"]
