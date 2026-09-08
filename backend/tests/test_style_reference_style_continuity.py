"""风格模仿 v2（W6，规格 §1.6 / §2.W6）— 跨场景声音一致性：漂移读数、事件读取、归档效果。"""

from __future__ import annotations

import re

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
from novel_system.services.bundle_builder import (
    BundleBuilder,
    resolve_scene_style_runtime_contract,
)
from novel_system.services.scene_archive_effects import SceneArchiveEffects
from novel_system.services.style_reference import style_continuity as sc
from novel_system.services.style_reference import voice_signature as vs
from novel_system.services.style_reference.repository import StyleReferenceRepository

_DIGITS = re.compile(r"[0-9]")

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


def seed_binding(session, *, project_id: str, seed: str, profile_json: dict, scope: str = "project") -> str:
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


def _drift_events(session) -> list[StyleReferenceMetricEvent]:
    return list(
        session.scalars(
            select(StyleReferenceMetricEvent).where(
                StyleReferenceMetricEvent.event_kind == sc.STYLE_DRIFT_EVENT_KIND
            )
        ).all()
    )


# ---------------------------------------------------------------------------
# observe_style_drift
# ---------------------------------------------------------------------------


def test_observe_style_drift_flags_long_sentences_sparse_commas_and_records_event(session) -> None:
    seed_work(session, project_id="P_W6_OBS", scenes_per_chapter=2)
    profile_id = seed_binding(
        session,
        project_id="P_W6_OBS",
        seed="w6obs",
        profile_json={
            "style_features": ["短句"],
            "voice_signature": {
                "version": "voice_signature_v1",
                "features": short_dense_reference(),
                "habits": ["句子短，一句一个动作"],
                "deliberate_repetition": False,
            },
            "metrics_baseline": {"avg_sentence_length": {"mean": 10.0, "std": 2.0}},
        },
    )
    scene = session.get(SceneCard, "P_W6_OBS_CH01_SC01")
    contract = resolve_scene_style_runtime_contract(session, scene)
    assert contract is not None

    result = sc.observe_style_drift(session, scene, LONG_SENTENCE_TEXT, contract)
    session.commit()

    assert result["outcome"] == "observed"
    assert result["profile_id"] == profile_id
    assert result["scene_seq"] == 1
    deviations = result["deviations"]
    assert 1 <= len(deviations) <= sc.MAX_DEVIATIONS
    flagged = {item["feature"]: item for item in deviations}
    assert flagged["sent_len_mean"]["direction"] == "high"
    assert flagged["punct_comma_per_1k"]["direction"] == "low"
    assert all(abs(item["z"]) >= sc.DRIFT_Z_THRESHOLD for item in deviations)
    # |z| 降序
    assert [abs(item["z"]) for item in deviations] == sorted((abs(item["z"]) for item in deviations), reverse=True)

    lines = result["calibration_lines"]
    assert 1 <= len(lines) <= sc.MAX_CALIBRATION_LINES
    assert all(not _DIGITS.search(line) for line in lines)
    joined = "".join(lines)
    assert "句子偏长" in joined and "逗号停顿变少" in joined and "：" in joined
    assert result["drift_ptype_priority"] and result["drift_ptype_priority"][0] == "narration"

    events = _drift_events(session)
    assert len(events) == 1
    event = events[0]
    assert event.event_id == result["event_id"]
    assert event.target_kind == "scene" and event.target_ref_id == scene.scene_id
    assert event.profile_id == profile_id and event.binding_id == "sr_bind_w6obs"
    assert event.outcome == "drift"
    context = event.context_json
    assert context["chapter_id"] == "P_W6_OBS_CH01" and context["scene_seq"] == 1
    assert context["deviations"] == deviations
    assert context["calibration_lines"] == lines
    assert context["drift_ptype_priority"] == result["drift_ptype_priority"]
    for name in ("sent_len_mean", "punct_comma_per_1k", "avg_sentence_length"):
        reading = context["features"][name]
        assert set(reading) == {"value", "baseline_mean", "baseline_std", "z"}
    assert context["features"]["sent_len_mean"]["baseline_mean"] == 10.0
    # metrics_baseline 兼容读数：avg_sentence_length 也应判偏高
    assert context["features"]["avg_sentence_length"]["z"] > sc.DRIFT_Z_THRESHOLD


def test_observe_style_drift_in_band_text_writes_event_with_empty_calibration(session) -> None:
    """成稿贴近参考时仍写事件（outcome=in_band、无校准行）——它会取代旧的校准。"""
    seed_work(session, project_id="P_W6_INB", scenes_per_chapter=1)
    signature = vs.compute_voice_signature_for_text(LONG_SENTENCE_TEXT)
    seed_binding(
        session,
        project_id="P_W6_INB",
        seed="w6inb",
        profile_json={"voice_signature": {"features": signature["features"], "deliberate_repetition": False}},
    )
    scene = session.get(SceneCard, "P_W6_INB_CH01_SC01")
    contract = resolve_scene_style_runtime_contract(session, scene)
    result = sc.observe_style_drift(session, scene, LONG_SENTENCE_TEXT, contract)
    session.commit()
    assert result["outcome"] == "observed"
    assert result["deviations"] == [] and result["calibration_lines"] == []
    assert result["drift_ptype_priority"] == []
    (event,) = _drift_events(session)
    assert event.outcome == "in_band"
    assert event.context_json["calibration_lines"] == []


@pytest.mark.parametrize(
    ("text", "profile_json", "use_contract", "reason"),
    [
        ("太短。", {"voice_signature": {"features": {"sent_len_mean": 10.0}}}, True, "text_too_short"),
        (LONG_SENTENCE_TEXT, {"voice_signature": {"features": {"sent_len_mean": 10.0}}}, False, "no_contract"),
        (LONG_SENTENCE_TEXT, {"style_features": ["旧画像没有声音签名"]}, True, "no_voice_signature"),
    ],
)
def test_observe_style_drift_no_ops_without_event(session, text, profile_json, use_contract, reason) -> None:
    seed_work(session, project_id="P_W6_NOOP", scenes_per_chapter=1)
    seed_binding(session, project_id="P_W6_NOOP", seed="w6noop", profile_json=profile_json)
    scene = session.get(SceneCard, "P_W6_NOOP_CH01_SC01")
    contract = resolve_scene_style_runtime_contract(session, scene) if use_contract else None
    result = sc.observe_style_drift(session, scene, text, contract)
    session.commit()
    assert result["outcome"] == "no_op" and result["reason"] == reason
    assert _drift_events(session) == []


def test_observe_style_drift_no_profile_layers_is_no_op(session) -> None:
    scene = SceneCard(scene_id="X_SC01", chapter_id="X", project_id=None, scene_seq=1)
    result = sc.observe_style_drift(session, scene, LONG_SENTENCE_TEXT, {"layers": []})
    assert result == {"outcome": "no_op", "reason": "no_profile", "text_chars": sc.visible_char_count(LONG_SENTENCE_TEXT)}
    assert _drift_events(session) == []


def test_observe_style_drift_degrades_when_event_write_fails(session, monkeypatch) -> None:
    seed_work(session, project_id="P_W6_DEG", scenes_per_chapter=1)
    seed_binding(
        session,
        project_id="P_W6_DEG",
        seed="w6deg",
        profile_json={"voice_signature": {"features": short_dense_reference()}},
    )
    scene = session.get(SceneCard, "P_W6_DEG_CH01_SC01")
    contract = resolve_scene_style_runtime_contract(session, scene)
    monkeypatch.setattr(sc.MetricsRecorder, "record", staticmethod(lambda *args, **kwargs: None))
    result = sc.observe_style_drift(session, scene, LONG_SENTENCE_TEXT, contract)
    assert result["outcome"] == "degraded"
    assert result["error_code"] == "STYLE_DRIFT_EVENT_WRITE_FAILED"


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
# 校准句 / 段型优先级渲染
# ---------------------------------------------------------------------------


def test_render_calibration_lines_groups_by_family_caps_lines_and_has_no_digits() -> None:
    deviations = [
        {"feature": "sent_len_mean", "direction": "high", "z": 4.2},
        {"feature": "punct_comma_per_1k", "direction": "low", "z": -3.9},
        {"feature": "dialogue_guide_none_share", "direction": "low", "z": 2.6},
        {"feature": "four_char_segment_per_1k", "direction": "high", "z": 2.1},
        {"feature": "fw_connective_per_1k", "direction": "high", "z": 1.7},
        {"feature": "unknown_feature", "direction": "high", "z": 9.0},
    ]
    lines = sc.render_calibration_lines(deviations)
    assert len(lines) == sc.MAX_CALIBRATION_LINES == 3
    assert lines[0] == "前一场句子偏长、逗号停顿变少：回到参考的短句节奏，回到密集停顿"
    assert lines[1].startswith("前一场无引导词的对白偏少：")
    assert lines[2].startswith("前一场四字格偏多：")
    assert all(not _DIGITS.search(line) for line in lines)
    assert sc.render_calibration_lines([]) == []
    assert sc.render_calibration_lines([{"feature": "unknown", "direction": "high", "z": 3.0}]) == []


def test_drift_ptype_priority_prefers_dialogue_for_dialogue_deviations() -> None:
    priority = sc.drift_ptype_priority(
        [
            {"feature": "dialogue_guide_pre_share", "direction": "high", "z": 3.0},
            {"feature": "speech_verb_shuo_share", "direction": "high", "z": 2.0},
            {"feature": "sent_len_mean", "direction": "high", "z": 1.6},
        ]
    )
    assert priority[0] == "dialogue"
    assert set(priority) >= {"dialogue", "narration"}
    assert sc.drift_ptype_priority([]) == []


# ---------------------------------------------------------------------------
# 读端：latest_drift_event / latest_drift_calibration
# ---------------------------------------------------------------------------


def _record(session, *, event_id: str, chapter_id: str, scene_seq: int, created_at: str, lines: list[str], scene_id: str | None = None) -> None:
    StyleReferenceRepository(session).create_metric_event(
        event_id=event_id,
        event_kind=sc.STYLE_DRIFT_EVENT_KIND,
        target_kind="scene",
        target_ref_id=scene_id or f"{chapter_id}_SC{scene_seq:02d}",
        profile_id="p",
        outcome="drift" if lines else "in_band",
        context_json={
            "chapter_id": chapter_id,
            "scene_seq": scene_seq,
            "calibration_lines": lines,
            "drift_ptype_priority": ["dialogue"] if lines else [],
        },
        created_at=created_at,
    )
    session.commit()


def test_latest_drift_event_reads_only_same_chapter_earlier_scenes_newest_first(session) -> None:
    seed_work(session, project_id="P_W6_READ", chapters=2, scenes_per_chapter=3)
    ch1 = "P_W6_READ_CH01"
    _record(session, event_id="ev_seq1_old", chapter_id=ch1, scene_seq=1, created_at="2026-09-05T10:00:00", lines=["旧读数"])
    _record(session, event_id="ev_seq1_new", chapter_id=ch1, scene_seq=1, created_at="2026-09-05T11:00:00", lines=["第一场最新读数"])
    _record(session, event_id="ev_seq2", chapter_id=ch1, scene_seq=2, created_at="2026-09-05T12:00:00", lines=["第二场读数"])
    _record(session, event_id="ev_seq3", chapter_id=ch1, scene_seq=3, created_at="2026-09-05T13:00:00", lines=["第三场读数"])
    _record(session, event_id="ev_other_chapter", chapter_id="P_W6_READ_CH02", scene_seq=1, created_at="2026-09-05T14:00:00", lines=["别章读数"])
    # 别的 event_kind 不算
    StyleReferenceRepository(session).create_metric_event(
        event_id="ev_not_drift",
        event_kind="injection_invoked",
        target_kind="scene",
        target_ref_id=f"{ch1}_SC01",
        context_json={"chapter_id": ch1, "scene_seq": 1, "calibration_lines": ["不该被读到"]},
        created_at="2026-09-05T15:00:00",
    )
    session.commit()

    # 第三场（seq 3）：只看 seq<3，最新的是第二场
    event = sc.latest_drift_event(session, ch1, 3)
    assert event["event_id"] == "ev_seq2" and event["scene_id"] == f"{ch1}_SC02"
    assert event["calibration_lines"] == ["第二场读数"] and event["drift_ptype_priority"] == ["dialogue"]
    assert event["source_scope"] == "chapter" and event["chapter_id"] == ch1
    # 第二场（seq 2）：只看 seq<2，取第一场两条里最新的
    assert sc.latest_drift_event(session, ch1, 2)["event_id"] == "ev_seq1_new"
    assert sc.latest_drift_calibration(session, ch1, 2) == ["第一场最新读数"]
    # 第一场：同章没有更早场景，且本项目没有上一章 → None / []
    assert sc.latest_drift_event(session, ch1, 1) is None
    assert sc.latest_drift_calibration(session, ch1, 1) == []
    # before_scene_seq=None 不限 seq → 本章最新
    assert sc.latest_drift_event(session, ch1, None)["event_id"] == "ev_seq3"
    # 未知章节
    assert sc.latest_drift_event(session, "NO_SUCH_CHAPTER", 5) is None


def test_latest_drift_event_falls_back_to_previous_chapter_like_voice_anchor(session) -> None:
    seed_work(session, project_id="P_W6_FB", chapters=2, scenes_per_chapter=2)
    _record(session, event_id="ev_ch1_last", chapter_id="P_W6_FB_CH01", scene_seq=2, created_at="2026-09-05T10:00:00", lines=["上一章章末读数"])
    event = sc.latest_drift_event(session, "P_W6_FB_CH02", 1)
    assert event is not None and event["event_id"] == "ev_ch1_last"
    assert event["source_scope"] == "previous_chapter"
    assert sc.latest_drift_calibration(session, "P_W6_FB_CH02", 1) == ["上一章章末读数"]
    assert sc.latest_drift_event(session, "P_W6_FB_CH02", 1, fallback_previous_chapter=False) is None
    # 同章一旦有更早读数，优先同章
    _record(session, event_id="ev_ch2_first", chapter_id="P_W6_FB_CH02", scene_seq=1, created_at="2026-09-05T11:00:00", lines=["本章第一场读数"])
    assert sc.latest_drift_event(session, "P_W6_FB_CH02", 2)["event_id"] == "ev_ch2_first"


def test_latest_drift_event_uses_scene_card_seq_when_context_lacks_it(session) -> None:
    seed_work(session, project_id="P_W6_SEQ", scenes_per_chapter=2)
    StyleReferenceRepository(session).create_metric_event(
        event_id="ev_no_seq",
        event_kind=sc.STYLE_DRIFT_EVENT_KIND,
        target_kind="scene",
        target_ref_id="P_W6_SEQ_CH01_SC01",
        context_json={"chapter_id": "P_W6_SEQ_CH01", "calibration_lines": ["无 seq 的旧事件"]},
        created_at="2026-09-05T10:00:00",
    )
    session.commit()
    assert sc.latest_drift_event(session, "P_W6_SEQ_CH01", 2)["event_id"] == "ev_no_seq"
    assert sc.latest_drift_event(session, "P_W6_SEQ_CH01", 1) is None


# ---------------------------------------------------------------------------
# 归档效果：_detect_and_store_style_drift
# ---------------------------------------------------------------------------

# scene_archive_checkpoint._validate_archive_drift_product 的允许集（内联在校验器里，未导出）；
# 真正的守卫是下面的 _assert_checkpoint_validator_accepts——把读数产品按归档路径包装后交给校验器本体。
_ARCHIVE_DRIFT_OUTCOMES = {"not_applicable", "no_op", "observed", "degraded"}


def _effects(session) -> SceneArchiveEffects:
    return SceneArchiveEffects(session, None, execution_id=None, run_job_id=None)


def _assert_checkpoint_validator_accepts(session, scene: SceneCard, product: dict, *, text: str) -> None:
    """按 scene_archive_checkpoint 归档路径包装读数产品并交给真实校验器；集合分叉在这里以 409 暴露。"""
    from novel_system.services.errors import DomainError
    from novel_system.services.orchestrator import Orchestrator

    orch = Orchestrator(session)
    drift_product = orch._archive_product(
        scene=scene,
        kind="style_drift",
        outcome=product["outcome"],
        step_key="archive:style_drift:0",
        input_hash=orch._text_hash(text),
        **{key: value for key, value in product.items() if key != "outcome"},
    )
    assert orch._validate_archive_drift_product(scene, drift_product, require_checkpoint_hash=False) == drift_product
    # 证明校验器确实在看 outcome：集合外的值必须被拒
    with pytest.raises(DomainError) as excinfo:
        orch._validate_archive_drift_product(
            scene, {**drift_product, "outcome": "observed_but_not_allowed"}, require_checkpoint_hash=False
        )
    assert excinfo.value.code == "RUN_CHECKPOINT_CORRUPT"


def test_archive_effect_observes_drift_from_final_scene_and_live_bindings(session) -> None:
    seed_work(session, project_id="P_W6_ARC", scenes_per_chapter=1)
    seed_binding(
        session,
        project_id="P_W6_ARC",
        seed="w6arc",
        profile_json={"voice_signature": {"features": short_dense_reference(), "deliberate_repetition": False}},
    )
    scene = session.get(SceneCard, "P_W6_ARC_CH01_SC01")
    _add_final_scene(session, scene_id=scene.scene_id, content=LONG_SENTENCE_TEXT)

    product = _effects(session)._detect_and_store_style_drift(scene)
    session.commit()

    # 归档 checkpoint 现已接受 "observed"（风格模仿 v2 集成）：读数结果原样透出
    assert product["outcome"] == "observed"
    assert product["outcome"] in _ARCHIVE_DRIFT_OUTCOMES
    _assert_checkpoint_validator_accepts(session, scene, product, text=LONG_SENTENCE_TEXT)
    assert product["observation_outcome"] == "observed"
    assert product["contract_source"] == "live_bindings"
    assert product["deviation_count"] >= 1 and product["calibration_lines"]
    (event,) = _drift_events(session)
    assert event.event_id == product["event_id"]
    # 与 _archive_product(**details) 的具名参数不冲突
    assert not ({"kind", "step_key", "input_hash", "scene", "schema_version", "execution_id"} & set(product))


def test_archive_effect_prefers_frozen_bundle_contract_and_styled_draft_fallback(session) -> None:
    from novel_system.db.models import SceneDraft

    seed_work(session, project_id="P_W6_FRZ", scenes_per_chapter=1)
    seed_binding(
        session,
        project_id="P_W6_FRZ",
        seed="w6frz",
        profile_json={"voice_signature": {"features": short_dense_reference()}},
    )
    scene = session.get(SceneCard, "P_W6_FRZ_CH01_SC01")
    BundleBuilder(session).build(scene.scene_id)
    session.commit()
    # 冻结后撤掉 live 绑定：读数仍应从 bundle 冻结契约取到画像
    binding = session.get(StyleReferenceInjectionBinding, "sr_bind_w6frz")
    binding.status = "inactive"
    # 没有 FinalScene → 退到最新已风格化草稿
    session.add(
        SceneDraft(
            row_id="draft_w6frz",
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            stage="style_draft",
            status="active",
            content=LONG_SENTENCE_TEXT,
            source_bundle_id="b",
            source_bundle_hash="h",
        )
    )
    session.commit()

    product = _effects(session)._detect_and_store_style_drift(scene)
    session.commit()
    assert product["observation_outcome"] == "observed"
    assert product["contract_source"] == "bundle:frozen"
    assert product["profile_id"] == "sr_profile_w6frz"
    assert len(_drift_events(session)) == 1


def test_archive_effect_no_ops_without_binding_or_text(session) -> None:
    seed_work(session, project_id="P_W6_NB", scenes_per_chapter=1)
    scene = session.get(SceneCard, "P_W6_NB_CH01_SC01")
    _add_final_scene(session, scene_id=scene.scene_id, content=LONG_SENTENCE_TEXT)
    product = _effects(session)._detect_and_store_style_drift(scene)
    assert product["outcome"] == "no_op" and product["reason"] == "no_contract"
    assert product["contract_source"] == "live_bindings"

    seed_binding(session, project_id="P_W6_NB", seed="w6nb", profile_json={"voice_signature": {"features": short_dense_reference()}})
    empty_scene = SceneCard(
        scene_id="P_W6_NB_CH01_SC99", chapter_id="P_W6_NB_CH01", project_id="P_W6_NB", scene_seq=99, onstage_chars_json=[], scene_goal="empty"
    )
    session.add(empty_scene)
    session.commit()
    product = _effects(session)._detect_and_store_style_drift(empty_scene)
    assert product["outcome"] == "no_op" and product["reason"] == "text_too_short"
    assert _drift_events(session) == []


def test_archive_effect_swallows_errors_as_degraded(session, monkeypatch) -> None:
    import novel_system.services.style_reference.style_continuity as continuity_module

    seed_work(session, project_id="P_W6_ERR", scenes_per_chapter=1)
    scene = session.get(SceneCard, "P_W6_ERR_CH01_SC01")

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("voice signature exploded")

    monkeypatch.setattr(continuity_module, "observe_style_drift", boom)
    product = _effects(session)._detect_and_store_style_drift(scene)
    assert product == {"outcome": "degraded", "error_code": "RuntimeError"}
    assert product["outcome"] in _ARCHIVE_DRIFT_OUTCOMES
    _assert_checkpoint_validator_accepts(session, scene, product, text="")
