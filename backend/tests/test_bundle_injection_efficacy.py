"""审计 P-4/P-5/P-6/P-7 回归：可选注入/基线功能的"生效断言"。

这些功能全部挂在 ``except Exception`` 降级路径后面——历史缺陷（查询不存在
的列、非法的 count().where()、project_id 字符串误推导、写入即销毁的裸
向量实例）都表现为"静默 no-op、测试全绿"。本文件对每个功能断言
**真实产出**（而非仅"不抛错"），并断言降级槽位账本为空。
"""

from __future__ import annotations

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    SceneCard,
    SceneRunState,
    StoryProject,
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


def _seed_book(session, project_id: str, chapters: list[tuple[str, int, int, int]]) -> dict[str, SceneCard]:
    """chapters = [(chapter 后缀, display_order, 场数, trashed_flag)] → {scene_id: SceneCard}。"""
    session.add(StoryProject(project_id=project_id, title="T", outline_text="o"))
    scenes: dict[str, SceneCard] = {}
    for suffix, display_order, scene_count, trashed in chapters:
        chapter_id = f"{project_id}_{suffix}"
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                project_id=project_id,
                chapter_goal=f"目标 {suffix}",
                display_order=display_order,
                trashed_flag=trashed,
            )
        )
        for seq in range(1, scene_count + 1):
            scene = SceneCard(
                scene_id=f"{chapter_id}_SC{seq:02d}",
                chapter_id=chapter_id,
                project_id=project_id,
                scene_seq=seq,
                scene_goal=f"推进 {suffix}-{seq}",
                location="雨城旧档案馆",
                trashed_flag=trashed,
            )
            session.add(scene)
            session.add(SceneRunState(scene_id=scene.scene_id))
            scenes[scene.scene_id] = scene
    session.flush()
    return scenes


def _add_final(
    session,
    scene: SceneCard,
    *,
    row_id: str,
    content: str,
    created_at: str,
    current: bool = False,
    status: str = "archived",
) -> None:
    session.add(
        FinalScene(
            row_id=row_id,
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            content=content,
            status=status,
            source_bundle_id="b",
            source_bundle_hash="h",
            created_at=created_at,
        )
    )
    session.flush()
    if current:
        session.get(SceneRunState, scene.scene_id).current_final_scene_row_id = row_id
        session.flush()


def test_freshness_budget_reads_only_the_current_version_of_a_rerun_scene(session):
    """B03-04：重跑过的场有好几版 FinalScene（旧版照旧 archived）；新鲜度预算只读每场的当前正文。"""
    scenes = _seed_book(session, "P_RERUN_FRESH", [("CH01", 1, 2, 0)])
    first, second = scenes["P_RERUN_FRESH_CH01_SC01"], scenes["P_RERUN_FRESH_CH01_SC02"]
    _add_final(session, first, row_id="final_old", content="旧版：林昭把旧信塞进案卷。", created_at="2026-09-01T00:00:00+00:00")
    _add_final(
        session, first, row_id="final_new", content="新版：林昭把旧信烧了。", created_at="2026-09-02T00:00:00+00:00", current=True
    )

    budget = BundleBuilder(session)._literary_freshness_budget(second)

    assert budget is not None
    assert budget["source_final_scene_ids"] == ["final_new"]
    assert budget["budget"]["source_scene_ids"] == [first.scene_id]


def test_chapter_transition_buffer_reads_the_current_version_of_the_previous_ending(session):
    """B03-04：上一章结尾那一场的当前正文由 SceneRunState 指针决定，不是最后建的那一行。"""
    scenes = _seed_book(session, "P_RERUN_TRANS", [("CH01", 1, 1, 0), ("CH02", 2, 1, 0)])
    ending = scenes["P_RERUN_TRANS_CH01_SC01"]
    _add_final(
        session, ending, row_id="final_current", content="当前结尾：雨停了，她合上案卷。", created_at="2026-09-01T00:00:00+00:00", current=True
    )
    _add_final(
        session, ending, row_id="final_abandoned", content="作废的重跑稿：他推门出去。", created_at="2026-09-03T00:00:00+00:00"
    )

    buffer = BundleBuilder(session)._chapter_transition_buffer(scenes["P_RERUN_TRANS_CH02_SC01"])

    assert buffer is not None and "当前结尾：雨停了，她合上案卷。" in buffer
    assert "作废的重跑稿" not in buffer


def test_chapter_transition_buffer_skips_a_trashed_previous_chapter(session):
    """B03-05：前面紧挨着的章在回收站里（清空后自动入回收站的章）时，过渡缓冲取的是活着的上一章。"""
    scenes = _seed_book(
        session, "P_TRASHED_PREV", [("CH01", 1, 1, 0), ("CH02", 2, 1, 1), ("CH03", 3, 1, 0)]
    )
    _add_final(
        session,
        scenes["P_TRASHED_PREV_CH01_SC01"],
        row_id="final_live_prev",
        content="上一章结尾：旧信落进雨里。",
        created_at="2026-09-01T00:00:00+00:00",
        current=True,
    )

    buffer = BundleBuilder(session)._chapter_transition_buffer(scenes["P_TRASHED_PREV_CH03_SC01"])

    assert buffer is not None and "上一章结尾：旧信落进雨里。" in buffer


def test_similar_scene_collection_indexes_one_current_text_per_scene(session):
    """B03-04：相似场景集合按场一条、取当前正文（旧版不再作为同 id 的重复文档进集合）。"""
    from novel_system.services.vector_store import get_vector_store

    scenes = _seed_book(session, "P_RERUN_SIM", [("CH01", 1, 3, 0)])
    first = scenes["P_RERUN_SIM_CH01_SC01"]
    _add_final(session, first, row_id="final_sim_old", content="旧版：案卷潮了。", created_at="2026-09-01T00:00:00+00:00")
    _add_final(
        session, first, row_id="final_sim_new", content="新版：案卷烧了。", created_at="2026-09-02T00:00:00+00:00", current=True
    )
    _add_final(
        session,
        scenes["P_RERUN_SIM_CH01_SC02"],
        row_id="final_sim_second",
        content="第二场：雨城起雾。",
        created_at="2026-09-02T00:00:00+00:00",
        current=True,
    )

    BundleBuilder(session)._similar_scene_context(scenes["P_RERUN_SIM_CH01_SC03"])

    documents = get_vector_store().load_collection("scenes_P_RERUN_SIM")
    assert sorted((doc["id"], doc["text"]) for doc in documents) == [
        (first.scene_id, "新版：案卷烧了。"),
        ("P_RERUN_SIM_CH01_SC02", "第二场：雨城起雾。"),
    ]


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


def test_bundle_never_carries_drift_calibration_even_with_legacy_events(session):
    """风格参考 v3：漂移驾驶已删——同章更早场景残留的 style_drift_observed 事件不再进下一场 bundle。"""
    from novel_system.db.models import StyleReferenceMetricEvent
    from tests.test_style_reference_style_continuity import seed_binding, seed_work, short_dense_reference

    seed_work(session, project_id="P_W6_BND", scenes_per_chapter=2)
    seed_binding(
        session,
        project_id="P_W6_BND",
        seed="w6bnd",
        profile_json={"voice_signature": {"features": short_dense_reference(), "deliberate_repetition": False}},
    )
    session.add(
        StyleReferenceMetricEvent(
            event_id="sr_metric_legacy_w6",
            event_kind="style_drift_observed",
            target_kind="scene",
            target_ref_id="P_W6_BND_CH01_SC01",
            profile_id="sr_profile_w6bnd",
            outcome="observed",
            context_json={
                "chapter_id": "P_W6_BND_CH01",
                "scene_seq": 1,
                "calibration_lines": ["前一场句子偏长"],
                "drift_ptype_priority": ["narration"],
            },
        )
    )
    session.commit()

    second = BundleBuilder(session).build("P_W6_BND_CH01_SC02")["snapshot"]
    assert "style_drift_calibration" not in second["inline_digests"]
    assert "_drift_ptype_priority" not in second["inline_digests"]
    assert not any(key.startswith("style_drift_calibration") for key in second["source_version_refs"])
    assert "style_drift_calibration" not in (second.get("degraded_slots") or [])


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

    # 有画像且标记刻意复沓(neutral_first 对照组:2026-09-12 起绑定缺省 style_first,
    # 房风词表会让位——本段断言的是现状行为,故显式钉住 neutral_first)
    seed_prior_final("P_W6_FRESH")
    seed_binding(
        session,
        project_id="P_W6_FRESH",
        seed="w6fresh",
        profile_json={"voice_signature": {"features": {"redup_total_per_1k": 20.0}, "deliberate_repetition": True}},
        config_json={"draft_mode": "neutral_first"},
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
        config_json={"draft_mode": "neutral_first"},
    )
    norep = json.loads(BundleBuilder(session).build("P_W6_NOREP_CH01_SC02")["snapshot"]["inline_digests"]["literary_freshness_budget"])
    assert "preserve_reference_repetition" not in norep

    # 2026-09-12 风格直起(style_first,缺省):两张房风词表与「以动作收尾」子句让位;
    # 风格参考 v3(L8):构式层清单(动作模板 / 意象场 / 句形 / 语义复读 / 全书已用表达)一并让位,
    # 只剩逐字层的近场重复 n-gram。
    seed_prior_final("P_W6_SFIRST")
    seed_binding(
        session,
        project_id="P_W6_SFIRST",
        seed="w6sfirst",
        profile_json={"voice_signature": {"features": {"redup_total_per_1k": 20.0}, "deliberate_repetition": True}},
    )
    bound = json.loads(BundleBuilder(session).build("P_W6_SFIRST_CH01_SC02")["snapshot"]["inline_digests"]["literary_freshness_budget"])
    assert bound["house_taste_lists"] == "deferred_to_reference"
    assert "avoid_summary_endings" not in bound and "avoid_false_clarity" not in bound
    assert "hard action" not in bound["instruction"]
    assert "copying your own earlier scenes word for word only" in bound["instruction"]
    assert bound["construction_lists"] == "deferred_to_reference"
    for key in ("avoid_action_templates", "avoid_image_fields", "vary_syntax_shapes", "semantic_repetition_alert"):
        assert key not in bound
    assert bound["avoid_recent_ngrams"] == ["他把杯子放回桌上"]
    assert bound["preserve_reference_repetition"] is True
    assert "lifetime_banned_expressions" not in bound
