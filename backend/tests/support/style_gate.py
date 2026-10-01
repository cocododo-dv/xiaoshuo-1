"""风格校验门的测试脚手架：落一本参考书 + 画像 + 绑定（可带禁用词与原文段），一场待软质检的风格稿。"""

from __future__ import annotations

from novel_system.db.models import ChapterGoal, SceneCard, SceneDraft, SceneRunState, StoryProject
from novel_system.db.session import SessionLocal
from novel_system.services.llm_task_runner import begin_llm_execution, end_llm_execution
from novel_system.services.qc_engine import SoftQcEngine
from novel_system.services.style_reference.repository import StyleReferenceRepository


# ---------------------------------------------------------------- 参考书原文、绑定与一场待软质检的风格稿（test_qc_engine_style_validation_gate）


# 参考书原文段（抄袭语料）：连续 ≥12 字重叠即命中。
REFERENCE_PARAGRAPH = "月光落在青石板上，像一层薄薄的盐，他踩过去时鞋底发出细碎的声响。"
COPIED_SENTENCE = "月光落在青石板上，像一层薄薄的盐"
CLEAN_TEXT = "门外的脚步停住了，他把信封放到桌上，等对面的人先开口。"


def seed_style_binding(
    *,
    project_id: str | None,
    seed: str,
    scope: str = "project",
    scope_ref_id: str | None = None,
    profile_status: str = "active",
    profile_json: dict | None = None,
    forbidden_terms: list[str] | None = None,
    paragraphs: list[str] | None = None,
) -> str:
    """落 book + run + profile + binding，可选 banned_term / 原文段。返回 profile_id。"""
    book_id = f"sr_book_{seed}"
    run_id = f"sr_run_{seed}"
    profile_id = f"sr_profile_{seed}"
    binding_id = f"sr_bind_{seed}"
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        repo.create_book(
            book_id=book_id, title="t", source_kind="upload", cloud_policy="segments_only",
            text_checksum=f"chk_{seed}", total_chars=10, status="ready",
            stats_json={"rights_declaration": {
                "declared": True, "analysis_rights": True, "send_rights": True,
            }},
        )
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
        repo.create_profile(
            profile_id=profile_id, book_id=book_id, run_id=run_id, title="t",
            status=profile_status,
            profile_json=profile_json or {"narrative_summary": "n", "style_features": ["短句克制"]},
            coverage_json={}, source_finding_ids_json=[],
        )
        repo.create_binding(
            binding_id=binding_id, profile_id=profile_id,
            scope=scope, scope_ref_id=scope_ref_id if scope_ref_id is not None else project_id,
            # 只用文风卡（这些用例原来写的旧策略 A 的语义；2026-09-24 起 strategy 列恒 mixed、参考方式看 config）
            task_type="scene_generation", strategy="mixed",
            config_json={"reference_mode": "card_only"}, status="active",
        )
        for i, term in enumerate(forbidden_terms or []):
            repo.create_banned_term(
                term_id=f"sr_term_{seed}_{i}",
                profile_id=profile_id, scope="generation",
                term=term, source="manual",
            )
        offset = 0
        for i, text in enumerate(paragraphs or []):
            repo.create_paragraph(
                paragraph_id=f"sr_par_{seed}_{i}",
                book_id=book_id,
                paragraph_index=i,
                paragraph_type="narration",
                start_offset=offset,
                end_offset=offset + len(text),
                text=text,
                char_count=len(text),
            )
            offset += len(text)
        session.commit()
    return profile_id


SOFT_SCENE_ID = "CH810_SC01"
SOFT_DRAFT_ROW_ID = "draft_style_CH810_SC01"


def seed_soft_scene(session, *, project_id: str, draft_content: str) -> SceneCard:
    session.add(StoryProject(project_id=project_id, title="Soft QC", outline_text=""))
    session.add(
        ChapterGoal(
            chapter_id="CH810",
            project_id=project_id,
            planned_scene_count=1,
            chapter_goal="A reunion turns dangerous.",
        )
    )
    scene = SceneCard(
        scene_id=SOFT_SCENE_ID,
        chapter_id="CH810",
        project_id=project_id,
        scene_seq=1,
        pov_character_id="A",
        onstage_chars_json=["A"],
        scene_goal="Force both characters to reveal what they know.",
        must_include_text="",
    )
    session.add(scene)
    session.add(SceneRunState(scene_id=SOFT_SCENE_ID, scene_status="style_draft_ready"))
    session.add(
        SceneDraft(
            row_id=SOFT_DRAFT_ROW_ID,
            scene_id=SOFT_SCENE_ID,
            chapter_id="CH810",
            stage="style_draft",
            content=draft_content,
            source_bundle_id="bundle_CH810_SC01",
            source_bundle_hash="bundle_hash_CH810_SC01",
        )
    )
    session.commit()
    return scene


def soft_bundle() -> dict:
    return {
        "bundle_id": "bundle_CH810_SC01",
        "bundle_snapshot_hash": "bundle_hash_CH810_SC01",
        "snapshot": {
            "scene_id": SOFT_SCENE_ID,
            "chapter_id": "CH810",
            "inline_digests": {"scene_card": "Goal"},
        },
    }


def run_soft_qc(session, runner, draft_content: str):
    engine = SoftQcEngine(session, llm_runner=runner)
    state = session.get(SceneRunState, SOFT_SCENE_ID)
    state.active_execution_id = "exec-soft-style"
    state.run_execution_status = "active"
    session.commit()
    token = begin_llm_execution("exec-soft-style")
    try:
        return engine.evaluate(
            scene_id=SOFT_SCENE_ID,
            bundle=soft_bundle(),
            source_draft_row_id=SOFT_DRAFT_ROW_ID,
            source_draft_content=draft_content,
        )
    finally:
        end_llm_execution(token)
