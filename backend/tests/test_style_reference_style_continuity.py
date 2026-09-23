"""跨场景声音参照（契约读取）与归档读数槽位。

风格参考 v3（2026-09-23）删掉了漂移驾驶：归档期的 ``observe_style_drift`` / ``style_drift_observed`` 事件、
下一场 bundle 的漂移校准段与漂移优先选窗。这里留下：其它测试共用的种子 helper、契约读取
（``contract_voice_reference`` / ``contract_deliberate_repetition``）与归档读数槽位的守卫。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    SceneCard,
    SceneRunState,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceMetricEvent,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.services.scene_archive_effects import SceneArchiveEffects
from novel_system.services.style_reference import style_continuity as sc
from novel_system.services.style_reference import voice_signature as vs

# 「偏长句、少逗号」成稿：每句四十余字、几乎没有逗号，≥300 可见字。
LONG_SENTENCE_TEXT = "\n\n".join(
    [
        "他沿着河岸一直走到那座旧桥的下面才停住脚步望着对岸渐渐亮起来的灯火想起许多年前的那个傍晚也是这样的天色。",
        "母亲站在门口等他回来手里还攥着那条没有织完的围巾风把她的头发吹得乱了她却一直没有抬手去理。",
        "他把行李放在门边沿着走廊走到尽头的书房里去桌上的茶早已经凉透了窗外的河水在暮色里显得格外沉静。",
        "对岸的灯陆续亮起来他没有开灯只坐在旧椅子上等着夜色完全落下来直到屋里再也看不见自己的手。",
    ]
    * 3
)


def _reference_features(**overrides: float) -> dict[str, float]:
    """以「一般中文小说」基线均值为底、只改写几个节奏特征的参考画像声音签名。"""
    baseline = vs.load_voice_baseline()
    features = {name: float(baseline["features"][name]["mean"]) for name in vs.FEATURE_NAMES}
    features.update(overrides)
    return features


def short_dense_reference() -> dict[str, float]:
    """短句、密集停顿的参考：句均十字、逗号极密。"""
    return _reference_features(
        sent_len_mean=10.0,
        sent_len_p90=18.0,
        punct_comma_per_1k=120.0,
        sent_pauses_mean=3.5,
        clause_len_mean=5.0,
    )


def seed_work(session, *, project_id: str, chapters: int = 1, scenes_per_chapter: int = 3) -> None:
    session.add(StoryProject(project_id=project_id, title="v2 continuity", outline_text="", planning_mode="snowflake"))
    for chapter_index in range(1, chapters + 1):
        chapter_id = f"{project_id}_CH{chapter_index:02d}"
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id=project_id,
                planned_scene_count=scenes_per_chapter,
                chapter_goal=f"Chapter {chapter_index} goal.",
                display_order=chapter_index,
            )
        )
        for seq in range(1, scenes_per_chapter + 1):
            scene_id = f"{chapter_id}_SC{seq:02d}"
            session.add(
                SceneCard(
                    scene_id=scene_id,
                    chapter_id=chapter_id,
                    project_id=project_id,
                    scene_seq=seq,
                    onstage_chars_json=[],
                    scene_goal=f"Scene {seq} goal.",
                    is_chapter_last=1 if seq == scenes_per_chapter else 0,
                )
            )
            session.add(SceneRunState(scene_id=scene_id))
    session.commit()


def seed_binding(
    session,
    *,
    project_id: str,
    seed: str,
    profile_json: dict,
    scope: str = "project",
    config_json: dict | None = None,
) -> str:
    session.add(
        StyleReferenceBook(
            book_id=f"sr_book_{seed}",
            title="Public domain source",
            source_kind="path",
            cloud_policy="segments_only",
            text_checksum=f"checksum-{seed}",
            stats_json={"rights_declaration": {"declared": True, "send_rights": True}},
        )
    )
    session.add(StyleReferenceRun(run_id=f"sr_run_{seed}", book_id=f"sr_book_{seed}", status="done"))
    session.add(
        StyleReferenceProfile(
            profile_id=f"sr_profile_{seed}",
            book_id=f"sr_book_{seed}",
            run_id=f"sr_run_{seed}",
            title="Audited profile",
            status="active",
            profile_json=profile_json,
        )
    )
    session.add(
        StyleReferenceInjectionBinding(
            binding_id=f"sr_bind_{seed}",
            profile_id=f"sr_profile_{seed}",
            scope=scope,
            scope_ref_id=project_id,
            task_type="scene_generation",
            strategy="A",
            config_json=dict(config_json or {}),
            status="active",
        )
    )
    session.commit()
    return f"sr_profile_{seed}"


def _add_final_scene(session, *, scene_id: str, content: str, status: str = "archived") -> str:
    chapter_id = scene_id.rsplit("_SC", 1)[0]
    row_id = f"final_{scene_id}"
    session.add(
        FinalScene(
            row_id=row_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            content=content,
            status=status,
            source_bundle_id=f"bundle_{scene_id}",
            source_bundle_hash="h",
        )
    )
    session.commit()
    return row_id


def test_contract_voice_reference_blends_layers_generic_to_specific() -> None:
    contract = {
        "layers": [
            {"order": 1, "profile": {"profile_id": "specific", "profile_json": {"voice_signature": {"features": {"sent_len_mean": 30.0}}}}},
            {"order": 0, "profile": {"profile_id": "generic", "profile_json": {"voice_signature": {"features": {"sent_len_mean": 12.0, "punct_comma_per_1k": 80.0}}}}},
        ]
    }
    blended = sc.contract_voice_reference(contract)
    # 泛层权重 1、具体层权重 2：(12×1 + 30×2) / 3 = 24
    assert blended["sent_len_mean"] == pytest.approx(24.0)
    assert blended["punct_comma_per_1k"] == pytest.approx(80.0)
    assert sc.contract_voice_reference({"layers": [{"profile": {"profile_json": {}}}]}) == {}
    assert sc.contract_voice_reference(None) == {}


def test_contract_deliberate_repetition_any_layer() -> None:
    off = {"layers": [{"profile": {"profile_json": {"voice_signature": {"deliberate_repetition": False}}}}]}
    on = {
        "layers": [
            {"profile": {"profile_json": {"voice_signature": {"deliberate_repetition": False}}}},
            {"profile": {"profile_json": {"voice_signature": {"deliberate_repetition": True}}}},
        ]
    }
    assert sc.contract_deliberate_repetition(off) is False
    assert sc.contract_deliberate_repetition(on) is True
    assert sc.contract_deliberate_repetition({"layers": [{"profile": {"profile_json": {}}}]}) is False
    assert sc.contract_deliberate_repetition(None) is False


# ---------------------------------------------------------------------------
# 归档读数槽位（archive:style_drift:0，风格参考 v3 起记「像不像」读数）
# ---------------------------------------------------------------------------


def _effects(session) -> SceneArchiveEffects:
    return SceneArchiveEffects(session, None, execution_id=None, run_job_id=None)


def test_archive_reading_slot_writes_no_drift_event_and_passes_the_checkpoint_validator(session) -> None:
    from novel_system.services.errors import DomainError
    from novel_system.services.orchestrator import Orchestrator

    seed_work(session, project_id="P_W6_ARC", scenes_per_chapter=1)
    seed_binding(
        session,
        project_id="P_W6_ARC",
        seed="w6arc",
        profile_json={"voice_signature": {"features": short_dense_reference(), "deliberate_repetition": False}},
    )
    scene = session.get(SceneCard, "P_W6_ARC_CH01_SC01")
    _add_final_scene(session, scene_id=scene.scene_id, content=LONG_SENTENCE_TEXT)

    product = _effects(session)._record_archive_fidelity_reading(scene)
    session.commit()

    assert product["outcome"] == "not_applicable"
    kinds = set(session.scalars(select(StyleReferenceMetricEvent.event_kind)).all())
    assert "style_drift_observed" not in kinds
    # 已持久化的检查点按原 kind / step_key 续跑：读数槽位的产品必须过真实校验器
    orch = Orchestrator(session)
    reading_product = orch._archive_product(
        scene=scene,
        kind="style_drift",
        outcome=product["outcome"],
        step_key="archive:style_drift:0",
        input_hash=orch._text_hash(LONG_SENTENCE_TEXT),
        **{key: value for key, value in product.items() if key != "outcome"},
    )
    assert orch._validate_archive_drift_product(scene, reading_product, require_checkpoint_hash=False) == reading_product
    with pytest.raises(DomainError) as excinfo:
        orch._validate_archive_drift_product(
            scene, {**reading_product, "outcome": "not_an_outcome"}, require_checkpoint_hash=False
        )
    assert excinfo.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_style_continuity_no_longer_exposes_drift_steering() -> None:
    for name in ("observe_style_drift", "latest_drift_event", "latest_drift_calibration", "drift_ptype_priority"):
        assert not hasattr(sc, name), name
