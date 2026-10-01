from __future__ import annotations

from sqlalchemy import select

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    HumanReviewEvent,
    SceneCard,
    SceneMemory,
    SceneRunState,
)
from novel_system.services.literary_quality import (
    AUTOMATED_EVIDENCE_SIGNAL,
    QUALITY_DIMENSIONS,
    adversarial_rank_score,
    analyze_literary_quality,
)


def _seed_quality_scene(session, *, chapter_id: str = "LQ100", scene_id: str = "LQ100_SC01") -> str:
    final_row_id = f"final_scene_{scene_id}_v1"
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            planned_scene_count=1,
            chapter_goal="A quiet meeting must turn into a costly decision.",
        )
    )
    session.add(ChapterState(chapter_id=chapter_id, current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id=scene_id,
            chapter_id=chapter_id,
            scene_seq=1,
            scene_goal="A witness asks for protection and forces the lead to choose.",
            beats_json=["witness arrives", "lead chooses", "door opens"],
            exit_change="The lead has crossed a line.",
            hook="A key scrapes under the door.",
        )
    )
    session.add(
        SceneRunState(
            scene_id=scene_id,
            scene_status="archived",
            current_final_scene_row_id=final_row_id,
            current_bundle_id="bundle_quality",
            current_bundle_hash="hash_quality",
        )
    )
    session.add(
        FinalScene(
            row_id=final_row_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            content=(
                "The witness held the key. The lead had to choose the archive or the child. "
                "She opened the door before the bell stopped."
            ),
            status="approved",
            source_bundle_id="bundle_quality",
            source_bundle_hash="hash_quality",
        )
    )
    session.commit()
    return final_row_id


def _item(payload: dict, object_type: str, object_id: str) -> dict:
    return next(
        row
        for row in payload["items"]
        if row["object_type"] == object_type and row["object_id"] == object_id
    )


def test_literary_quality_overview_prefers_author_drafts_and_does_not_mutate_runtime(client, session) -> None:
    final_row_id = _seed_quality_scene(session)
    scene_draft = AuthorDraft(
        draft_id="author_draft_scene_LQ100_SC01_current",
        object_type="scene",
        object_id="LQ100_SC01",
        source_text_ref=f"final_scene:{final_row_id}",
        content=(
            '"Because you know the truth, I explain everything now," he said. '
            "She suddenly realized the moon was somehow meaningful. "
            "The moon watched the moonlit room; moon, moon. "
            "He turned, turned, turned, then turned again. "
            "In the end, everything changed forever."
        ),
        revision_no=3,
        status="current",
    )
    chapter_draft = AuthorDraft(
        draft_id="author_draft_chapter_LQ100_current",
        object_type="chapter",
        object_id="LQ100",
        source_text_ref="chapter_assembled:LQ100",
        content="Chapter author draft with a forced choice: choose the archive or save the child.",
        revision_no=1,
        status="current",
    )
    session.add_all([scene_draft, chapter_draft])
    session.commit()

    before_state = session.get(SceneRunState, "LQ100_SC01").scene_status
    response = client.get("/api/v1/literary-quality/overview")

    assert response.status_code == 200
    payload = response.json()["data"]
    scene_item = _item(payload, "scene", "LQ100_SC01")
    chapter_item = _item(payload, "chapter", "LQ100")

    assert scene_item["text_layer"] == "author_draft"
    assert scene_item["source_ref"] == f"author_draft:{scene_draft.draft_id}"
    assert chapter_item["text_layer"] == "author_draft"
    assert chapter_item["source_ref"] == f"author_draft:{chapter_draft.draft_id}"
    assert payload["summary"]["object_count"] >= 2
    assert payload["summary"]["model_voice_count"] >= 1
    assert scene_item["score"] < 0.75
    assert scene_item["signals"]["model_voice"]["risk"] is True
    assert scene_item["signals"]["image_homogeneity"]["risk"] is True
    assert scene_item["signals"]["repetitive_action"]["risk"] is True
    assert scene_item["signals"]["expository_dialogue"]["risk"] is True
    assert scene_item["signals"]["no_choice_scene"]["risk"] is True
    assert scene_item["signals"]["summary_ending"]["risk"] is True
    assert {"dimension", "severity", "issue", "evidence_excerpt", "recommendation"} <= set(scene_item["findings"][0])

    session.expire_all()
    assert session.get(FinalScene, final_row_id).content.startswith("The witness held the key.")
    assert session.get(SceneRunState, "LQ100_SC01").scene_status == before_state
    assert session.execute(select(HumanReviewEvent)).scalars().all() == []


def test_literary_quality_overview_falls_back_to_runtime_text_and_derives_chapter_text_on_read(client, session) -> None:
    """R13：章的正文读时现拼。存下来的章汇总（这里故意是一份过期的）既不是默认层的章源，也不是「章记忆终稿」层的
    章源——前者拼各场当前终稿，后者现拼这一章归档过的各场记忆。"""
    final_row_id = _seed_quality_scene(session, chapter_id="LQ200", scene_id="LQ200_SC01")
    final = session.get(FinalScene, final_row_id)
    session.add(
        SceneMemory(
            row_id=f"scene_memory_{final_row_id}",
            scene_id="LQ200_SC01",
            chapter_id="LQ200",
            content=final.content,
            source_bundle_id=final.source_bundle_id,
            final_scene_row_id=final_row_id,
            active_flag=1,
        )
    )
    stale = ChapterMemory(
        row_id="chapter_memory_final_LQ200_v1",
        chapter_id="LQ200",
        aggregate_stage="final",
        content="Final aggregate: she must choose the witness and opens the locked room.",
        active_flag=1,
        runtime_eligible=1,
        runtime_eligibility_basis="direct_read",
    )
    session.add(stale)
    session.get(ChapterState, "LQ200").last_final_memory_row_id = stale.row_id
    session.commit()

    response = client.get("/api/v1/literary-quality/overview?text_layer=author_draft_preferred")

    assert response.status_code == 200
    payload = response.json()["data"]
    scene_item = _item(payload, "scene", "LQ200_SC01")
    chapter_item = _item(payload, "chapter", "LQ200")

    assert scene_item["text_layer"] == "runtime_final_scene"
    assert scene_item["source_ref"] == f"final_scene:{final_row_id}"
    assert chapter_item["text_layer"] == "chapter_assembled"
    assert chapter_item["source_ref"] == "chapter_assembled:LQ200"

    runtime_response = client.get("/api/v1/literary-quality/overview?text_layer=runtime_final_scene&chapter_id=LQ200")
    assert runtime_response.status_code == 200
    runtime_items = runtime_response.json()["data"]["items"]
    assert [item["object_type"] for item in runtime_items] == ["scene"]
    assert runtime_items[0]["text_layer"] == "runtime_final_scene"

    memory_response = client.get("/api/v1/literary-quality/overview?text_layer=chapter_memory_final&chapter_id=LQ200")
    assert memory_response.status_code == 200
    memory_items = memory_response.json()["data"]["items"]
    assert [item["object_type"] for item in memory_items] == ["chapter"]
    assert memory_items[0]["text_layer"] == "chapter_memory_final"
    assert memory_items[0]["source_ref"] == "chapter_memory:LQ200"
    # 现拼的是那一场的记忆（与终稿同文），不是那份过期的汇总：两层读到的是同一段文字
    assert memory_items[0]["fingerprint"] == chapter_item["fingerprint"]


def test_literary_quality_detects_template_reuse() -> None:
    text = (
        "她低头看着钥匙，沉默了片刻。\n"
        "他低头看着录音，沉默了片刻。\n"
        "她低头看着门缝，沉默了片刻。\n"
        "月光、阴影、冷风和雾气反复压下来，月光又落在她手上。\n"
        "她忽然意识到这一切都变得不同了。她知道真相必须公开。"
    )

    signals, findings = analyze_literary_quality(text)

    assert signals["template_action_reuse"]["risk"] is True
    assert signals["image_field_reuse"]["risk"] is True
    assert signals["syntax_monotony"]["risk"] is True
    assert signals["false_clarity"]["risk"] is True
    # 批准#13c（重评 R7）：永远打满分、从不报问题的「有效留白」维度删了
    assert "valid_ambiguity" not in signals
    assert set(signals) - {AUTOMATED_EVIDENCE_SIGNAL} == set(QUALITY_DIMENSIONS)
    assert "valid_ambiguity" not in QUALITY_DIMENSIONS
    finding_dimensions = {finding["dimension"] for finding in findings}
    assert {
        "template_action_reuse",
        "image_field_reuse",
        "syntax_monotony",
        "false_clarity",
    } <= finding_dimensions


def test_literary_quality_detects_professional_writer_risks() -> None:
    text = (
        "月光像旧伤疤一样贴在档案柜上，冷意仿佛命运的回声。"
        "“真相是官方报告被改过，因为他们要保护码头，所以我现在解释给你听。”许望说。"
        "林岑忽然意识到自己必须公开证据，她知道一切都变得不同了。"
    )

    signals, findings = analyze_literary_quality(text)

    assert signals["painless_scene"]["risk"] is True
    assert signals["decorative_imagery"]["risk"] is True
    assert signals["dialogue_as_report"]["risk"] is True
    assert signals["over_explained_motive"]["risk"] is True
    assert signals["false_poetic_closure"]["risk"] is True
    dimensions = {finding["dimension"] for finding in findings}
    assert {
        "painless_scene",
        "decorative_imagery",
        "dialogue_as_report",
        "over_explained_motive",
        "false_poetic_closure",
    } <= dimensions


def test_literary_quality_overview_can_filter_professional_writer_risk(client, session) -> None:
    _seed_quality_scene(session, chapter_id="LQ250", scene_id="LQ250_SC01")
    final_scene = session.get(FinalScene, "final_scene_LQ250_SC01_v1")
    final_scene.content = (
        "月光像旧伤疤一样贴在档案柜上，冷意仿佛命运的回声。"
        "“真相是官方报告被改过，所以我解释给你听。”许望说。"
        "林岑知道自己必须公开证据。"
    )
    session.commit()

    response = client.get(
        "/api/v1/literary-quality/overview",
        params={"chapter_id": "LQ250", "risk_type": "decorative_imagery"},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["items"]
    assert payload["risk_clusters"][0]["dimension"] == "decorative_imagery"
    assert payload["items"][0]["recommended_next_action"]["risk_type"] in {
        "painless_scene",
        "decorative_imagery",
        "dialogue_as_report",
    }


def test_literary_quality_chapter_set_review_scores_cross_chapter_arc_and_safety(client, session) -> None:
    for index, chapter_id in enumerate(("LQSET01", "LQSET02", "LQSET03"), start=1):
        scene_id = f"{chapter_id}_SC01"
        final_row_id = f"final_scene_{scene_id}_v1"
        session.add(
            ChapterGoal(
                chapter_id=chapter_id,
                planned_scene_count=1,
                chapter_goal=f"第{index}章：玻璃雨逼迫主角在公开证据和保护证人之间选择。",
                main_plot_push=f"推进第{index}个未来失踪反证。",
                emotional_target="让主角付出关系或安全代价。",
                ending_effect="结尾留下下一章必须处理的反证。",
            )
        )
        session.add(ChapterState(chapter_id=chapter_id, current_phase="drafting"))
        session.add(
            SceneCard(
                scene_id=scene_id,
                chapter_id=chapter_id,
                scene_seq=1,
                scene_goal="主角必须选择公开档案还是转移证人。",
                beats_json=["玻璃雨落下", "反证出现", "必须选择", "付出代价"],
                exit_change="证人得到保护，但公开证据被延迟。",
                hook="玻璃雨停在零点，下一份名单浮出。",
            )
        )
        session.add(
            SceneRunState(
                scene_id=scene_id,
                scene_status="archived",
                current_final_scene_row_id=final_row_id,
                current_bundle_id=f"bundle_{scene_id}_v1",
                current_bundle_hash=f"hash_{scene_id}_v1",
            )
        )
        session.add(
            FinalScene(
                row_id=final_row_id,
                scene_id=scene_id,
                chapter_id=chapter_id,
                content=(
                    "零点的玻璃雨敲在废线站顶棚。她必须选择公开证据，还是先把证人送走。"
                    "她放弃了即时直播，把录音塞进防水袋，代价是自己的坐标暴露。"
                    "玻璃雨再次停住，新的名单在地面积水里浮现。"
                ),
                status="approved",
                source_bundle_id=f"bundle_{scene_id}_v1",
                source_bundle_hash=f"hash_{scene_id}_v1",
            )
        )
    session.commit()

    response = client.post(
        "/api/v1/literary-quality/chapter-set-review",
        json={
            "chapter_ids": ["LQSET01", "LQSET02", "LQSET03"],
            "protected_terms": ["灰港学院", "欧文·灰港", "镜湖档案馆"],
        },
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["chapter_ids"] == ["LQSET01", "LQSET02", "LQSET03"]
    assert payload["summary"]["chapter_count"] == 3
    assert payload["summary"]["scene_count"] == 3
    assert payload["scores"]["reference_safety"] == 1.0
    assert payload["payoff_reveal_checks"]["has_forced_choice_count"] == 3
    assert payload["payoff_reveal_checks"]["has_cost_count"] == 3
    assert payload["payoff_reveal_checks"]["has_next_pull_count"] == 3
    assert payload["reference_safety_findings"] == []
    assert any(row["token"] == "玻璃雨" for row in payload["repeated_patterns"])
    assert payload["recommended_next_action"]["action"] in {"open_deepdesk_patch", "none"}


def test_chapter_set_review_matches_protected_term_variants_like_the_copy_gate(client, session) -> None:
    """批准#12（B04-15）：章组复审的受保护专名与抄袭门同一套匹配——插了空格 / 标点的写法也认得出。"""
    final_row_id = _seed_quality_scene(session, chapter_id="LQSET_SAFE", scene_id="LQSET_SAFE_SC01")
    final = session.get(FinalScene, final_row_id)
    final.content = "林昭在灰 港-学院门口停下。欧文把旧信递给她，雨城的钟响了三下。"
    session.commit()

    response = client.post(
        "/api/v1/literary-quality/chapter-set-review",
        json={"chapter_ids": ["LQSET_SAFE"], "protected_terms": ["灰港学院", "欧文", "镜湖档案馆"]},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    scene_findings = [row for row in payload["reference_safety_findings"] if row["object_type"] == "scene"]
    assert [row["term"] for row in scene_findings] == ["灰港学院", "欧文"]
    assert "灰 港-学院" in scene_findings[0]["evidence_excerpt"]
    assert payload["scores"]["reference_safety"] == 0.0


def test_chapter_set_protected_term_scan_normalizes_each_text_once(monkeypatch) -> None:
    """章组复审的受保护专名：每段文字只规范化一次、所有词一起找——以前逐词各找一遍，规范化的开销乘上词数，
    十章配二十个词要十几秒。报出来的照旧是每段文字、每个传入的词各一条（按传入的词序，重复的词照报），
    取最早的一处；字面命中按词取摘录，变体命中按命中的位置取。"""
    from novel_system.services.literary_quality import chapter_set

    calls: list[str] = []
    real = chapter_set.find_protected_term_spans

    def counting(text, terms):  # noqa: ANN001, ANN202
        calls.append(text)
        return real(text, terms)

    monkeypatch.setattr(chapter_set, "find_protected_term_spans", counting)
    rows = [
        {"object_type": "scene", "object_id": "S1", "chapter_id": "C1", "scene_id": "S1", "source_ref": "r1",
         "content": "林昭在灰 港-学院门口停下。欧文把旧信递给她，欧文没有走。"},
        {"object_type": "scene", "object_id": "S2", "chapter_id": "C1", "scene_id": "S2", "source_ref": "r2",
         "content": "雨城的钟响了三下。灰港学院的灯还亮着。"},
        {"object_type": "chapter", "object_id": "C1", "chapter_id": "C1", "scene_id": None, "source_ref": "r3",
         "content": "案卷里没有这些名字。"},
    ]
    terms = ["欧文", "灰港学院", "镜湖档案馆", "欧文", " 灰港学院 "]

    findings = chapter_set._reference_safety_findings(rows, terms)

    assert calls == [row["content"] for row in rows]  # 逐词各找时是 3 × 5 = 15 次
    assert [(row["object_id"], row["term"]) for row in findings] == [
        ("S1", "欧文"), ("S1", "灰港学院"), ("S1", "欧文"), ("S1", " 灰港学院 "),
        ("S2", "灰港学院"), ("S2", " 灰港学院 "),
    ]
    by_key = {(row["object_id"], row["term"]): row for row in findings}
    assert "灰 港-学院" in by_key[("S1", "灰港学院")]["evidence_excerpt"]
    assert by_key[("S2", "灰港学院")]["evidence_excerpt"].startswith("雨城的钟响了三下")
    assert by_key[("S1", "欧文")]["source_ref"] == "r1"

    calls.clear()
    assert chapter_set._reference_safety_findings(rows, []) == []
    assert chapter_set._reference_safety_findings(rows, ["", "  "]) == []
    assert calls == []  # 没有要查的词就不规范化


def test_literary_quality_chapter_set_review_uses_requested_scene_text_layer(client, session) -> None:
    final_row_id = _seed_quality_scene(session, chapter_id="LQSET_LAYER", scene_id="LQSET_LAYER_SC01")
    scene_draft = AuthorDraft(
        draft_id="author_draft_scene_LQSET_LAYER_SC01_current",
        object_type="scene",
        object_id="LQSET_LAYER_SC01",
        source_text_ref=f"final_scene:{final_row_id}",
        content="作者稿里误写了灰港学院；角色只能选择保护证人，代价是公开证据被延迟。",
        revision_no=1,
        status="current",
    )
    session.add(scene_draft)
    session.commit()

    response = client.post(
        "/api/v1/literary-quality/chapter-set-review",
        json={
            "chapter_ids": ["LQSET_LAYER"],
            "text_layer": "author_draft_preferred",
            "protected_terms": ["灰港学院"],
        },
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["summary"]["scene_count"] == 1
    assert payload["scenes"][0]["text_layer"] == "author_draft"
    assert payload["scenes"][0]["source_ref"] == f"author_draft:{scene_draft.draft_id}"
    assert payload["reference_safety_findings"][0]["term"] == "灰港学院"
    assert payload["reference_safety_findings"][0]["source_ref"] == f"author_draft:{scene_draft.draft_id}"


def test_chapter_set_review_reads_the_reference_calibration_like_the_overview(session) -> None:
    """批准#13a（B04-13）：章组复审里的场与巡检、写作台深改面板用同一份参考书校准——参考作者常态的维度降为提示，
    不会在章组复审里又冒成要改的问题（以前章组复审分析各场时没把场交给校准解析器）。"""
    from novel_system.services.literary_quality import LiteraryQualityService, RuleCalibration

    final_row_id = _seed_quality_scene(session, chapter_id="LQCAL", scene_id="LQCAL_SC01")
    _signals, raw_findings = analyze_literary_quality(session.get(FinalScene, final_row_id).content)
    habit = next(finding["dimension"] for finding in raw_findings if finding["severity"] != "info")
    calibration = RuleCalibration(
        source="reference",
        dimension_stats={habit: {"fired": 40, "n": 48, "share": 0.833, "lower_bound": 0.75, "level": "habit"}},
        windows=48,
    )
    resolved: list[str] = []

    def resolver(scene):
        resolved.append(scene.scene_id)
        return calibration

    service = LiteraryQualityService(session, rule_calibration_resolver=resolver)
    overview_scene = next(
        item for item in service.overview(chapter_id="LQCAL")["items"] if item["object_type"] == "scene"
    )
    review_scene = service.chapter_set_review({"chapter_ids": ["LQCAL"]})["scenes"][0]

    assert resolved == ["LQCAL_SC01", "LQCAL_SC01"]
    assert review_scene["findings"] == overview_scene["findings"]
    assert review_scene["rule_calibration"] == overview_scene["rule_calibration"]
    assert review_scene["rule_calibration"] is not None
    calibrated = [finding for finding in review_scene["findings"] if finding["dimension"] == habit]
    assert calibrated and all(finding["severity"] == "info" for finding in calibrated)


def test_a_blank_author_draft_is_no_text_for_literary_quality(session) -> None:
    """与写作台深改面板同一条「看得见的字才算正文」（S1 17，与复核 P02b-R1 同类）：写作台一打开就建的空白作者稿
    （""、<p></p>、<p><br></p>）是一张白纸——以前文学质量照样分析它，「缺什么」的规则对白纸全会响，深改面板却什么都
    不报。空白的作者稿往下一层落：有终稿看终稿，没有就这一场不列；只有空段落的终稿也不算正文。"""
    from novel_system.services.literary_quality import LiteraryQualityService

    final_row_id = _seed_quality_scene(session, chapter_id="LQBLANK", scene_id="LQBLANK_SC01")
    session.add(SceneCard(scene_id="LQBLANK_SC02", chapter_id="LQBLANK", scene_seq=2, scene_goal="她开口。", beats_json=[]))
    session.add(SceneCard(scene_id="LQBLANK_SC03", chapter_id="LQBLANK", scene_seq=3, scene_goal="门开了。", beats_json=[]))
    session.add(
        SceneRunState(scene_id="LQBLANK_SC03", scene_status="archived", current_final_scene_row_id="final_scene_LQBLANK_SC03_v1")
    )
    session.add(
        FinalScene(
            row_id="final_scene_LQBLANK_SC03_v1",
            scene_id="LQBLANK_SC03",
            chapter_id="LQBLANK",
            content="<p> </p><p><br></p>",
            status="approved",
            source_bundle_id="bundle_quality",
            source_bundle_hash="hash_quality",
        )
    )
    for object_type, object_id, content in (
        ("scene", "LQBLANK_SC01", "<p><br></p>"),
        ("scene", "LQBLANK_SC02", "<p> </p><p></p>"),
        ("chapter", "LQBLANK", ""),
    ):
        session.add(
            AuthorDraft(
                draft_id=f"draft_{object_id}",
                object_type=object_type,
                object_id=object_id,
                source_text_ref=f"{object_type}:{object_id}",
                content=content,
                status="current",
            )
        )
    session.commit()

    service = LiteraryQualityService(session)
    items = {(item["object_type"], item["object_id"]): item for item in service.overview(chapter_id="LQBLANK")["items"]}
    # 空白作者稿、有终稿 → 看终稿
    assert items[("scene", "LQBLANK_SC01")]["text_layer"] == "runtime_final_scene"
    assert items[("scene", "LQBLANK_SC01")]["source_ref"] == f"final_scene:{final_row_id}"
    # 空白作者稿、没有终稿 / 终稿只有空段落 → 这一场没有正文，不列
    assert ("scene", "LQBLANK_SC02") not in items and ("scene", "LQBLANK_SC03") not in items
    # 章：空白的章级作者稿 → 各场当前终稿现拼（只拼有字的）
    chapter_item = items[("chapter", "LQBLANK")]
    assert chapter_item["text_layer"] == "chapter_assembled"
    review = service.chapter_set_review({"chapter_ids": ["LQBLANK"]})
    assert [scene["object_id"] for scene in review["scenes"]] == ["LQBLANK_SC01"]


def test_the_overview_reads_the_binding_calibration_without_an_injected_resolver(session) -> None:
    """B04-21：文学质量自己经 ``literary_quality.calibration_source`` 取这一场绑定的参考书的规则校准（路由不必再把场景
    诊断的解析器注入进来）——与写作台深改面板读到的是同一份。"""
    from novel_system.services.literary_quality import LiteraryQualityService
    from novel_system.services.scene_diagnosis import SceneDiagnosisService
    from tests.style_reference_factories import make_binding, make_book, make_profile, synthetic_paragraphs

    _seed_quality_scene(session, chapter_id="LQBIND", scene_id="LQBIND_SC01")
    book_id = make_book(session, "book_lqbind", paragraphs=synthetic_paragraphs(600))
    make_profile(session, book_id, profile_id="profile_lqbind")
    make_binding(session, "profile_lqbind", binding_id="bind_lqbind", scope="global")
    session.commit()

    expected = SceneDiagnosisService(session).rule_calibration_for_scene(session.get(SceneCard, "LQBIND_SC01"))
    assert expected is not None and expected.active
    item = next(
        entry
        for entry in LiteraryQualityService(session).overview(chapter_id="LQBIND")["items"]
        if entry["object_type"] == "scene"
    )
    assert item["rule_calibration"] == expected.as_dict()


def test_literary_quality_chapter_set_review_reports_missing_payoff_chapter_ids(client, session) -> None:
    session.add(
        ChapterGoal(
            chapter_id="LQSET_MISSING",
            planned_scene_count=1,
            chapter_goal="A quiet archive interlude.",
            main_plot_push="Hold atmosphere.",
            emotional_target="Stay uncertain.",
            ending_effect="Fade out.",
        )
    )
    session.add(ChapterState(chapter_id="LQSET_MISSING", current_phase="drafting"))
    session.add(
        SceneCard(
            scene_id="LQSET_MISSING_SC01",
            chapter_id="LQSET_MISSING",
            scene_seq=1,
            scene_goal="Describe the room without a decision.",
        )
    )
    session.add(
        SceneRunState(
            scene_id="LQSET_MISSING_SC01",
            scene_status="archived",
            current_final_scene_row_id="final_scene_LQSET_MISSING_SC01_v1",
        )
    )
    session.add(
        FinalScene(
            row_id="final_scene_LQSET_MISSING_SC01_v1",
            scene_id="LQSET_MISSING_SC01",
            chapter_id="LQSET_MISSING",
            content="灰尘落在档案柜上，灯光安静地停住。她看着房间，没有行动。",
            status="approved",
            source_bundle_id="bundle_LQSET_MISSING",
            source_bundle_hash="hash_LQSET_MISSING",
        )
    )
    session.commit()

    response = client.post(
        "/api/v1/literary-quality/chapter-set-review",
        json={"chapter_ids": ["LQSET_MISSING"], "text_layer": "runtime_final_scene"},
    )

    assert response.status_code == 200
    checks = response.json()["data"]["payoff_reveal_checks"]
    assert checks["missing_forced_choice_chapter_ids"] == ["LQSET_MISSING"]
    assert checks["missing_cost_chapter_ids"] == ["LQSET_MISSING"]
    assert checks["missing_next_pull_chapter_ids"] == ["LQSET_MISSING"]
    assert checks["missing_payoff_chapter_ids"] == ["LQSET_MISSING"]


def test_literary_quality_detects_perception_filter() -> None:
    text = (
        "她注意到窗外的雨已经停了。"
        "走廊里有人在争吵，选择公开证据还是保护证人。"
        "代价是暴露自己的位置。她推开门走出去。"
    )
    signals, findings = analyze_literary_quality(text)
    assert signals["perception_filter"]["risk"] is True
    assert any(f["dimension"] == "perception_filter" for f in findings)


def test_literary_quality_expanded_model_voice_catches_chinese_cliches() -> None:
    text = (
        "她微微一笑，缓缓说道："
        "“你要选择哪一个？”"
        "他必须在公开和保护之间做出选择，"
        "代价是暴露位置。"
        "他推开门。"
    )
    signals, findings = analyze_literary_quality(text)
    assert signals["model_voice"]["risk"] is True


def test_action_keyword_bundle_is_insufficient_evidence_not_literary_perfection() -> None:
    text = "必须选择，付出代价，她推开门。"

    signals, findings = analyze_literary_quality(text)
    score = adversarial_rank_score(text)

    # The phrase can satisfy several structural word lists, but it is not
    # enough prose to support an upper-bound literary judgment.
    assert signals["no_choice_scene"]["risk"] is False
    assert signals["choice_pressure"]["risk"] is False
    assert signals["automated_evidence_sufficiency"]["risk"] is True
    assert signals["automated_evidence_sufficiency"]["human_judgment_required"] is True
    assert score < 0.6
    assert score != 1.0
    assert findings == []


def test_literary_quality_dimension_weights_sum_to_one() -> None:
    from novel_system.services.literary_quality import DIMENSION_WEIGHTS
    assert abs(sum(DIMENSION_WEIGHTS.values()) - 1.0) < 1e-9
    assert set(DIMENSION_WEIGHTS) == set(QUALITY_DIMENSIONS)


def test_weighted_score_is_the_one_formula_for_every_caller() -> None:
    """B04-16：条目分、成稿门的总分与人物场景核心、对抗排名分都走 ``scoring.weighted_score``（维度组各选各的）。"""
    from novel_system.services.literary_quality import DIMENSION_WEIGHTS, weighted_score

    signals, _ = analyze_literary_quality("她低头看着钥匙，沉默了片刻。他低头看着录音，沉默了片刻。她知道真相必须公开。")
    risky = [dimension for dimension in QUALITY_DIMENSIONS if signals[dimension]["risk"]]
    assert risky
    assert weighted_score(signals, QUALITY_DIMENSIONS) == round(
        sum(signals[dimension]["score"] * DIMENSION_WEIGHTS[dimension] for dimension in QUALITY_DIMENSIONS), 4
    )
    # 成稿门：参考作者常态的维度按已满足计——全都放过就是满分
    assert weighted_score(signals, QUALITY_DIMENSIONS, forced_ok=risky) == 1.0
    # 归一：除以这组维度的权重和；没有信号的维度按满分算；权重和为 0 时给 empty
    core = ("no_choice_scene", "choice_pressure")
    assert weighted_score({}, core, normalize=True) == 1.0
    assert weighted_score(signals, core, {"no_choice_scene": 0.0}, normalize=True, empty=0.42) == 0.42


def test_self_repetition_dimension_defaults_to_no_risk() -> None:
    signals, _ = analyze_literary_quality("She opened the door. He must choose.")
    assert signals["self_repetition"]["risk"] is False
    assert signals["self_repetition"]["score"] == 1.0


def test_self_repetition_allows_deliberate_two_part_refrain() -> None:
    signals, findings = analyze_literary_quality(
        "那扇锈住的铁门还是没有打开。她走到楼下，听完雨里的脚步。"
        "那扇锈住的铁门还是没有打开。"
    )

    assert signals["self_repetition"]["risk"] is False
    assert not any(item["dimension"] == "self_repetition" for item in findings)


def test_self_repetition_detects_high_confidence_mechanical_loop() -> None:
    signals, findings = analyze_literary_quality(
        "他把目光重新移回那扇紧闭的门边。" * 4
    )

    assert signals["self_repetition"]["risk"] is True
    assert any(item["dimension"] == "self_repetition" for item in findings)


def _seed_cross_scene_template_reuse(session) -> None:
    chapter_id = "LQ300"
    session.add(
        ChapterGoal(
            chapter_id=chapter_id,
            planned_scene_count=2,
            chapter_goal="Two strong-type scenes should avoid repeating the same visible machinery.",
        )
    )
    session.add(ChapterState(chapter_id=chapter_id, current_phase="drafting"))
    for index, object_term in enumerate(("钥匙", "录音"), start=1):
        scene_id = f"{chapter_id}_SC0{index}"
        final_row_id = f"final_scene_{scene_id}_v1"
        session.add(
            SceneCard(
                scene_id=scene_id,
                chapter_id=chapter_id,
                scene_seq=index,
                scene_goal=f"林岑必须处理{object_term}带来的选择压力。",
            )
        )
        session.add(
            SceneRunState(
                scene_id=scene_id,
                scene_status="archived",
                current_final_scene_row_id=final_row_id,
            )
        )
        session.add(
            FinalScene(
                row_id=final_row_id,
                scene_id=scene_id,
                chapter_id=chapter_id,
                content=(
                    f"她低头看着{object_term}，沉默了片刻。"
                    f"他低头看着{object_term}，沉默了片刻。"
                    f"林岑低头看着{object_term}，沉默了片刻。"
                    "月光、阴影、冷风和雾气反复压下来。"
                    "她忽然意识到这一切都变得不同了。她知道真相必须公开。"
                ),
                status="approved",
                source_bundle_id=f"bundle_{scene_id}",
                source_bundle_hash=f"hash_{scene_id}",
            )
        )
    session.commit()


def test_valid_ambiguity_is_no_longer_a_risk_filter(client) -> None:
    """批准#13c（重评 R7）：删掉的「有效留白」不再是可选的风险维度——旧标签页还带着它时拿到标准的 400。"""
    response = client.get("/api/v1/literary-quality/overview", params={"risk_type": "valid_ambiguity"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "LITERARY_QUALITY_RISK_TYPE_INVALID"


def test_literary_quality_overview_exposes_filters_clusters_fingerprints_and_reuse(client, session) -> None:
    _seed_cross_scene_template_reuse(session)

    response = client.get(
        "/api/v1/literary-quality/overview",
        params={
            "chapter_id": "LQ300",
            "risk_type": "template_action_reuse",
            "min_severity": "revision",
        },
    )

    assert response.status_code == 200
    payload = response.json()["data"]

    assert payload["filters"] == {
        "text_layer": "author_draft_preferred",
        "chapter_id": "LQ300",
        "risk_type": "template_action_reuse",
        "min_severity": "revision",
        "project_id": None,
    }
    assert payload["items"]
    assert {item["chapter_id"] for item in payload["items"]} == {"LQ300"}
    assert all(item["signals"]["template_action_reuse"]["risk"] for item in payload["items"])
    assert all(item["recommended_next_action"]["action"] == "open_deepdesk_patch" for item in payload["items"])
    assert payload["risk_clusters"][0]["dimension"] == "template_action_reuse"
    assert payload["risk_clusters"][0]["count"] >= 2
    # 指纹只在各条目里（顶层那份一模一样的列表没人读，已删，B04-14）
    assert "fingerprints" not in payload
    assert any(item["object_id"].startswith("LQ300_SC") for item in payload["items"])
    assert all("action_templates" in item["fingerprint"] for item in payload["items"])
    assert any(row["cluster_type"] == "action_template" for row in payload["cross_scene_reuse"])
    assert payload["recommended_next_action"]["action"] == "open_deepdesk_patch"
    # 规则维度与中文名由服务端给（筛选项用，B04-31）
    assert [row["dimension"] for row in payload["dimensions"]] == list(QUALITY_DIMENSIONS)
    assert {row["dimension"]: row["label"] for row in payload["dimensions"]}["template_action_reuse"] == "模板动作复用"


def test_overview_reads_the_texts_in_a_fixed_number_of_queries(session) -> None:
    """B04-14：巡检一次看全书，查询数不随章 / 场的多少增长（以前逐章逐场各查两三次作者稿、终稿、章节汇总）。"""
    from sqlalchemy import event

    from novel_system.services.literary_quality import LiteraryQualityService

    engine = session.get_bind()
    statements: list[str] = []

    def count(_conn, _cursor, statement, *_args):
        statements.append(statement)

    def overview_statements(text_layer: str) -> int:
        session.expire_all()
        statements.clear()
        event.listen(engine, "before_cursor_execute", count)
        try:
            payload = LiteraryQualityService(session).overview(text_layer=text_layer)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert payload["items"]
        return len(statements)

    for index in range(2):
        _seed_quality_scene(session, chapter_id=f"LQN{index}", scene_id=f"LQN{index}_SC01")
    few = {layer: overview_statements(layer) for layer in ("author_draft_preferred", "runtime", "chapter_assembled")}
    for index in range(2, 8):
        _seed_quality_scene(session, chapter_id=f"LQN{index}", scene_id=f"LQN{index}_SC01")
    many = {layer: overview_statements(layer) for layer in ("author_draft_preferred", "runtime", "chapter_assembled")}
    assert many == few


def test_literary_quality_analyze_text_returns_quality_spine_without_database_mutation(client, session) -> None:
    _seed_quality_scene(session, chapter_id="LQ400", scene_id="LQ400_SC01")

    response = client.post(
        "/api/v1/literary-quality/analyze-text",
        json={
            "content": (
                "她低头看着钥匙，沉默了片刻。"
                "他低头看着录音，沉默了片刻。"
                "她低头看着门缝，沉默了片刻。"
                "她知道真相必须公开。"
            ),
            "object_type": "scene",
            "object_id": "scratch",
            "chapter_id": "scratch_chapter",
        },
    )

    assert response.status_code == 200
    payload = response.json()["data"]

    assert payload["object_type"] == "scene"
    assert payload["object_id"] == "scratch"
    assert payload["score"] < 0.75
    assert payload["fingerprint"]["action_templates"]
    assert payload["span_findings"]
    first_span = payload["span_findings"][0]
    assert {"dimension", "severity", "start", "end", "evidence", "recommended_action"} <= set(first_span)
    assert payload["content"][first_span["start"] : first_span["end"]] == first_span["evidence"]
    assert any(span["dimension"] == "template_action_reuse" for span in payload["span_findings"])
    assert payload["risk_clusters"][0]["count"] >= 1
    assert payload["recommended_next_action"]["action"] == "open_deepdesk_patch"

    session.expire_all()
    assert session.get(SceneRunState, "LQ400_SC01").scene_status == "archived"
