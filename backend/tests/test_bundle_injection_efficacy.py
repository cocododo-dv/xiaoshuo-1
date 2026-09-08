"""审计 P-4/P-5/P-6/P-7 回归：可选注入/基线功能的"生效断言"。

这些功能全部挂在 ``except Exception`` 降级路径后面——历史缺陷（查询不存在
的列、非法的 count().where()、project_id 字符串误推导、写入即销毁的裸
向量实例）都表现为"静默 no-op、测试全绿"。本文件对每个功能断言
**真实产出**（而非仅"不抛错"），并断言降级槽位账本为空。
"""

from __future__ import annotations

from novel_system.db.models import (
    ChapterGoal,
    SceneCard,
    SceneRunState,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.orchestrator import Orchestrator
from novel_system.services.prompt_builder import PromptBuilder


def _seed_catalog_style_scene(session, project_id: str = "projp6"):
    """种一个目录冷启动风格（chapter_id 含多个下划线段）的场景。"""
    session.add(
        StoryProject(
            project_id=project_id, title="T", outline_text="o", planning_mode="snowflake"
        )
    )
    chapter_id = f"{project_id}_CH_deadbeef"
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            project_id=project_id,
            chapter_goal="目标",
            display_order=1,
        )
    )
    scene = SceneCard(
        scene_id=f"{chapter_id}_SC_cafebabe",
        chapter_id=chapter_id,
        project_id=project_id,
        scene_seq=2,
        scene_goal="推进",
        pov_character_id="char_a",
        onstage_chars_json=["char_a"],
    )
    session.add(scene)
    session.add(SceneRunState(scene_id=scene.scene_id))
    session.flush()
    return scene


def test_narrative_state_digest_uses_scene_project_id(session):
    """P-6：目录式 chapter_id 下，权威状态注入必须按 scene.project_id 命中事件。"""
    scene = _seed_catalog_style_scene(session)
    session.add(
        SceneCard(
            scene_id="earlier_scene",
            chapter_id=scene.chapter_id,
            project_id=scene.project_id,
            scene_seq=1,
            scene_goal="earlier",
        )
    )
    session.flush()
    log = NarrativeEventLog(session)
    log.log_event(
        project_id=scene.project_id,
        scene_id="earlier_scene",
        chapter_id=scene.chapter_id,
        event_type="character_state",
        entity_type="character",
        entity_id="char_a",
        fact_key="injury",
        fact_value="左臂骨折",
        authority_status="accepted",
        source_kind="test_fixture",
    )
    # 事件位于同章前一场，当前场景应能按权威目录位置读取。
    digest = BundleBuilder(session)._narrative_state_digest(scene)
    assert digest is not None, "有事件时权威状态注入不应为空"
    assert "左臂骨折" in digest


def test_scene_vector_indexing_persists_via_factory(session, monkeypatch):
    """P-7：归档场景索引必须写进 get_vector_store() 工厂实例（进程级可见），
    而不是函数返回即销毁的裸 InMemoryVectorStore。"""
    from novel_system.services.vector_store import get_vector_store

    scene = _seed_catalog_style_scene(session, project_id="projp7")
    result = Orchestrator._index_scene_to_vector_store(scene, "一段正文内容用于索引")

    store = get_vector_store()
    collection = f"scenes_{scene.project_id}"
    assert store.collection_exists(collection), "索引后集合应在工厂单例中可见"
    ids = {doc["id"] for doc in store.load_collection(collection)}
    assert scene.scene_id in ids
    assert result["outcome"] == "non_persistent"
    assert result["write_status"] in {"indexed", "already_present"}
    assert result["backend"] == "memory"
    assert result["validation_scope"] == "process_local"


def test_scene_vector_indexing_rejects_stale_same_id_without_overwrite(session):
    from novel_system.services.vector_store import get_vector_store

    scene = _seed_catalog_style_scene(session, project_id="projp7_stale")
    store = get_vector_store()
    collection = f"scenes_{scene.project_id}"
    store.write_collection(collection, [{"id": scene.scene_id, "text": "stale"}])

    result = Orchestrator._index_scene_to_vector_store(scene, "current")

    assert result["outcome"] == "failed"
    assert result["error_code"] == "VECTOR_INDEX_STALE_CONTENT"
    assert store.load_collection(collection) == [{"id": scene.scene_id, "text": "stale"}]


# ---------------------------------------------------------------------------
# 风格模仿 v2（W6）：漂移校准进 bundle、新鲜度预算豁免
# ---------------------------------------------------------------------------


def test_bundle_second_scene_carries_drift_calibration_event_id_and_ptype_priority(session):
    """同章更早场景归档后写了 style_drift_observed，下一场 bundle 渲染校准行并记 event_id / 段型优先级。"""
    import json

    from novel_system.services.style_reference import style_continuity as sc
    from tests.test_style_reference_style_continuity import (
        LONG_SENTENCE_TEXT,
        seed_binding,
        seed_work,
        short_dense_reference,
    )

    seed_work(session, project_id="P_W6_BND", scenes_per_chapter=3)
    seed_binding(
        session,
        project_id="P_W6_BND",
        seed="w6bnd",
        profile_json={"voice_signature": {"features": short_dense_reference(), "deliberate_repetition": False}},
    )
    builder = BundleBuilder(session)
    first = builder.build("P_W6_BND_CH01_SC01")["snapshot"]
    assert "style_drift_calibration" not in first["inline_digests"]
    assert "_drift_ptype_priority" not in first["inline_digests"]

    scene1 = session.get(SceneCard, "P_W6_BND_CH01_SC01")
    contract = builder.session and __import__(
        "novel_system.services.bundle_builder", fromlist=["resolve_scene_style_runtime_contract"]
    ).resolve_scene_style_runtime_contract(session, scene1)
    observed = sc.observe_style_drift(session, scene1, LONG_SENTENCE_TEXT, contract)
    session.commit()
    assert observed["outcome"] == "observed" and observed["calibration_lines"]

    second = builder.build("P_W6_BND_CH01_SC02")["snapshot"]
    digest = second["inline_digests"]["style_drift_calibration"]
    assert digest.split("\n") == [f"- {line}" for line in observed["calibration_lines"]]
    refs = second["source_version_refs"]
    assert refs["style_drift_calibration_event_id"] == observed["event_id"]
    assert refs["style_drift_calibration_source_scene_id"] == "P_W6_BND_CH01_SC01"
    assert refs["style_drift_calibration_source_scope"] == "chapter"
    assert refs["style_drift_calibration_ptype_priority"] == observed["drift_ptype_priority"]
    assert refs["style_drift_calibration_line_count"] == len(observed["calibration_lines"])
    assert refs["style_drift_calibration_before_scene_seq"] == 2
    # bundle hash 投影要求 digest 值为 str：段型优先级以 JSON 字符串落 inline_digests
    priority_digest = second["inline_digests"]["_drift_ptype_priority"]
    assert isinstance(priority_digest, str)
    assert json.loads(priority_digest) == observed["drift_ptype_priority"]
    assert json.loads(priority_digest)[0] == "narration"
    slot = next(item for item in second["ordered_injections"] if item["slot"] == "style_drift_calibration")
    assert slot["digest_key"] == "style_drift_calibration"
    assert "style_drift_calibration" not in (second.get("degraded_slots") or [])

    # 第三场同样读到最近一次（仍是第一场的）读数
    third = builder.build("P_W6_BND_CH01_SC03")["snapshot"]
    assert third["source_version_refs"]["style_drift_calibration_event_id"] == observed["event_id"]


def test_freshness_budget_prunes_function_word_only_ngrams():
    from novel_system.services.bundle_builder import (
        _is_function_word_only,
        _prune_function_word_only_entries,
    )
    from novel_system.services.style_reference.voice_signature import load_voice_lexicon

    words = frozenset(w for group in load_voice_lexicon().group_words.values() for w in group)
    max_len = max(len(w) for w in words)
    assert _is_function_word_only("了，他也就", words, max_len) is True
    assert _is_function_word_only("的地得所着过", words, max_len) is True
    assert _is_function_word_only("……——，。", words, max_len) is True
    assert _is_function_word_only("他把杯子放回桌上", words, max_len) is False
    assert _is_function_word_only("comma:2:len:1", words, max_len) is False
    assert _is_function_word_only("action:低头", words, max_len) is False

    budget = {
        "avoid_recent_ngrams": ["了，他也就", "他把杯子放回桌上", "。。。", "却又还都也"],
        "avoid_action_templates": ["action:转身"],
        "vary_syntax_shapes": ["comma:2:len:1", "，，，"],
        "avoid_false_clarity": ["她知道"],
        "instruction": "keep",
        "lifetime_banned_expressions": "not a list",
    }
    removed = _prune_function_word_only_entries(budget)
    assert set(removed) == {"了，他也就", "。。。", "却又还都也", "，，，"}
    assert budget["avoid_recent_ngrams"] == ["他把杯子放回桌上"]
    assert budget["avoid_action_templates"] == ["action:转身"]
    assert budget["vary_syntax_shapes"] == ["comma:2:len:1"]
    assert budget["avoid_false_clarity"] == ["她知道"]
    assert budget["instruction"] == "keep" and budget["lifetime_banned_expressions"] == "not a list"


def test_bundle_freshness_budget_applies_exemptions_from_bound_reference(session, monkeypatch):
    """avoid_recent_ngrams 剔除纯虚词条目；画像 deliberate_repetition=true → preserve_reference_repetition。"""
    import json

    from novel_system.db.models import FinalScene
    from novel_system.services import self_repetition as self_repetition_module
    from tests.test_style_reference_style_continuity import seed_binding, seed_work

    monkeypatch.setattr(
        self_repetition_module.SelfRepetitionDetector,
        "top_repeated_ngrams",
        lambda self, chapter_id, **kwargs: ["了，他也就", "他把杯子放回桌上", "，。，。"],
    )

    def seed_prior_final(project_id: str) -> None:
        seed_work(session, project_id=project_id, scenes_per_chapter=2)
        session.add(
            FinalScene(
                row_id=f"final_{project_id}_SC01",
                scene_id=f"{project_id}_CH01_SC01",
                chapter_id=f"{project_id}_CH01",
                content="他把杯子放回桌上，没有看她。窗外的雨声更紧了些。他把杯子放回桌上，又推开。",
                status="archived",
                source_bundle_id="b",
                source_bundle_hash="h",
            )
        )
        session.commit()

    # 有画像且标记刻意复沓
    seed_prior_final("P_W6_FRESH")
    seed_binding(
        session,
        project_id="P_W6_FRESH",
        seed="w6fresh",
        profile_json={"voice_signature": {"features": {"redup_total_per_1k": 20.0}, "deliberate_repetition": True}},
    )
    snapshot = BundleBuilder(session).build("P_W6_FRESH_CH01_SC02")["snapshot"]
    budget = json.loads(snapshot["inline_digests"]["literary_freshness_budget"])
    assert budget["avoid_recent_ngrams"] == ["他把杯子放回桌上"]
    assert budget["preserve_reference_repetition"] is True
    assert budget["instruction"].startswith("Use this as a freshness budget")
    # 规格 §2.W6.3：预算里只放布尔标记；「保留参考的刻意复沓」是目标文风指令，
    # 措辞属于 style_draft 模板，不能写进 neutral_draft 也会看到的 instruction。
    assert "preserve_reference_repetition" not in budget["instruction"]
    for wording in ("deliberate cadence", "target voice", "style reference"):
        assert wording not in budget["instruction"].lower()
    neutral_prompt = PromptBuilder().build(snapshot, "neutral_draft")["user_prompt"]
    assert "## Literary Freshness Budget" in neutral_prompt
    assert '"preserve_reference_repetition": true' in neutral_prompt
    for wording in ("deliberate cadence", "target voice", "reduplication and short-sentence runs"):
        assert wording not in neutral_prompt.lower()

    # 无契约：预算不变（无 preserve 键），但纯虚词 n-gram 同样剔除
    seed_prior_final("P_W6_PLAIN")
    plain = json.loads(BundleBuilder(session).build("P_W6_PLAIN_CH01_SC02")["snapshot"]["inline_digests"]["literary_freshness_budget"])
    assert "preserve_reference_repetition" not in plain
    assert plain["avoid_recent_ngrams"] == ["他把杯子放回桌上"]
    # 标记只改变布尔键：instruction 文本在有 / 无标记时完全一致
    assert plain["instruction"] == budget["instruction"]

    # 有契约但画像不标记刻意复沓：同样没有 preserve 键
    seed_prior_final("P_W6_NOREP")
    seed_binding(
        session,
        project_id="P_W6_NOREP",
        seed="w6norep",
        profile_json={"voice_signature": {"features": {"redup_total_per_1k": 3.0}, "deliberate_repetition": False}},
    )
    norep = json.loads(BundleBuilder(session).build("P_W6_NOREP_CH01_SC02")["snapshot"]["inline_digests"]["literary_freshness_budget"])
    assert "preserve_reference_repetition" not in norep
