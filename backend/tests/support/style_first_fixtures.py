"""作者手笔直起（style_first）场景的测试夹具：绑一本合成参考书、按正文里的记号给风格读数。

2026-09-30 [批准#2] 之后 Best-of-N 多稿只剩作者手笔直起那一种：终选门、盲化视图、选后续跑的测试都要一个绑了参考书的
场景。读数的数值由 :func:`install_readings` 按正文里的记号给定（决定逻辑可控）；全部是合成文本。
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard, SceneRunState, StoryProject
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference import readings
from novel_system.services.style_reference.inject.bindings import resolve_binding_layers
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import build_style_runtime_contract
from novel_system.services.style_reference.fidelity import FidelityReading
from tests.style_reference_inject_helpers import bind, seed_reference


def reading(
    distance: float,
    percentile: float,
    *,
    reliable: bool = True,
    out_of_band: list[dict] | None = None,
    emphasized: tuple[str, ...] = (),
    chars: int = 1200,
) -> FidelityReading:
    return FidelityReading(
        distance=distance,
        percentile=percentile,
        out_of_band=list(out_of_band or []),
        dimension_scores={"narrative.pacing": 6.0, "language.punctuation": 8.5},
        feature_z={},
        char_count=chars,
        window_count=28,
        reliable=reliable,
        kernel_version="measure_v1",
        reference_version="ref_test",
        emphasized_dimensions=emphasized,
    )


PACING_OUT = [
    {
        "feature": "para_len_mean",
        "dimension": "narrative.pacing",
        "z": 3.1,
        "direction": "high",
        "phrase": "段落比作者长，换段太少",
        "value": 180.0,
        "author_typical": 60.0,
    }
]


def install_readings(monkeypatch, table: dict[str, FidelityReading], default: FidelityReading | None = None) -> None:
    """``readings.reading_for_text`` 的替身：文字里含哪个记号就给哪个读数（未绑定照旧 None）。"""

    def fake(session, policy, text):  # noqa: ANN001
        if policy is None or not getattr(policy, "bound", False) or not str(text or "").strip():
            return None
        for marker, value in table.items():
            if marker in text:
                return value
        return default

    monkeypatch.setattr(readings, "reading_for_text", fake)


def bind_style_first(
    session: Session,
    key: str,
    *,
    project_id: str,
    draft_mode: str = "style_first",
    card: bool = True,
    chapters: int = 14,
    per_chapter: int = 100,
) -> tuple[str, str]:
    """一本合成参考书 + 文风卡画像，按项目作用域绑定（默认作者手笔直起）；返回 (book_id, profile_id)。"""
    book_id, profile_id = seed_reference(session, key, card=card, chapters=chapters, per_chapter=per_chapter)
    bind(
        session,
        profile_id,
        binding_id=f"bind_{key}",
        scope="project",
        scope_ref_id=project_id,
        config_json={"draft_mode": draft_mode},
    )
    return book_id, profile_id


def frozen_bundle(project_id: str, scene_id: str, chapter_id: str) -> dict:
    """按 bundle_builder 的冻结方式（status=frozen + inline contract）造一份 bundle。"""
    with SessionLocal() as session:
        layers = resolve_binding_layers(session, project_id, "scene_generation", character_ids=[], scene_id=scene_id)
        contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type="scene_generation")
    return {
        "bundle_id": f"bundle_{scene_id}_sfd",
        "bundle_snapshot_hash": "bundle_hash_sfd",
        "snapshot": {
            "contract_version": "BSHASH_v1",
            "stage_allowlist_name": "bundle_build_allowlist_v1",
            "scene_id": scene_id,
            "chapter_id": chapter_id,
            "source_version_refs": {
                "style_reference_runtime_contract_status": "frozen",
                "style_reference_runtime_contract_version": contract["contract_version"],
                "style_reference_runtime_contract_hash": contract["contract_hash"],
            },
            "inline_digests": {
                "scene_card": "Reveal the letter without explaining it.",
                "_style_reference_runtime_contract": json.dumps(contract, ensure_ascii=False, sort_keys=True),
            },
        },
    }


def seed_scene(session, *, project_id: str, scene_id: str, chapter_id: str, band: str = "short", must: str = "信封") -> SceneCard:
    session.add(StoryProject(project_id=project_id, title="读数", outline_text=""))
    session.add(ChapterGoal(chapter_id=chapter_id, project_id=project_id, planned_scene_count=1, chapter_goal="g"))
    scene = SceneCard(
        scene_id=scene_id,
        chapter_id=chapter_id,
        project_id=project_id,
        scene_seq=1,
        pov_character_id="CHAR_A",
        onstage_chars_json=["CHAR_A"],
        location="茶馆",
        scene_goal="把信交出去",
        beats_json=["到场"],
        must_include_text=must,
        target_length_band=band,
        scene_type="reveal",
        is_chapter_last=0,
    )
    session.add(scene)
    session.add(SceneRunState(scene_id=scene_id, scene_status="ready"))
    session.commit()
    return scene


def bound_scene(session, key: str, *, draft_mode: str = "style_first", card: bool = True, **scene_kwargs):
    """绑了参考书的一场（默认作者手笔直起）+ 按 bundle_builder 冻结方式造的 bundle；返回 (scene, bundle, book_id, profile_id)。"""
    project_id = f"proj_{key}"
    book_id, profile_id = bind_style_first(session, key, project_id=project_id, draft_mode=draft_mode, card=card)
    scene = seed_scene(session, project_id=project_id, scene_id=f"{key.upper()}_SC01", chapter_id=f"{key.upper()}_CH", **scene_kwargs)
    bundle = frozen_bundle(project_id, scene.scene_id, scene.chapter_id)
    return scene, bundle, book_id, profile_id
