from __future__ import annotations

from sqlalchemy.orm import Session

from novel_system.db.models import (
    FinalScene,
    HumanReviewEvent,
    LlmCall,
    QcReport,
    RelationProfile,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneRunState,
    VoiceProfile,
)


SCENE_WRITER_BRIEF_V2 = {
    "schema_version": "writer_brief_v2",
    "character_desire": "CHAR_A wants the truth.",
    "obstacle": "CHAR_B holds back the key fact.",
    "stakes": "The old clue may be lost.",
    "secret_or_misunderstanding": "Both hide what they know about the letter.",
    "subtext": "Trust is being tested under the reunion.",
    "irreversible_change": "They leave knowing the secret has an observer.",
    "reader_question": "Who sent the old letter?",
    "choice_under_pressure": "CHAR_A must choose between trust and exposure.",
    "power_shift": "CHAR_B loses control of the conversation.",
    "new_information": "The letter points to a third watcher.",
    "emotional_turn": "Suspicion turns into reluctant alliance.",
    "image_anchor": "The old letter becomes a warning.",
    "reader_aftertaste": "The reunion feels useful but unsafe.",
}


def create_chapter(client, chapter_id: str = "CH910") -> None:
    response = client.post(
        "/api/v1/chapters",
        json={
            "chapter_id": chapter_id,
            "planned_scene_count": 1,
            "chapter_goal": f"goal for {chapter_id}",
            "main_plot_push": f"push for {chapter_id}",
            "emotional_target": f"emotion for {chapter_id}",
            "ending_effect": f"ending for {chapter_id}",
        },
        headers={"X-Idempotency-Key": f"chapter-{chapter_id}"},
    )
    assert response.status_code == 200


def create_scene(
    client,
    *,
    chapter_id: str = "CH910",
    scene_id: str = "CH910_SC01",
    pov_character_id: str = "CHAR_A",
    onstage_chars_json: list[str] | None = None,
    location: str = "Old city gate",
    scene_goal: str = "Reunion tension escalates",
    beats_json: list[str] | None = None,
    must_include_text: str = "Old letter clue",
) -> None:
    response = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": scene_id,
            "chapter_id": chapter_id,
            "scene_seq": 1,
            "pov_character_id": pov_character_id,
            "onstage_chars_json": ["CHAR_A", "CHAR_B"] if onstage_chars_json is None else onstage_chars_json,
            "location": location,
            "scene_goal": scene_goal,
            "beats_json": ["beat-1", "beat-2"] if beats_json is None else beats_json,
            "must_include_text": must_include_text,
            "target_length_band": "short",
            "scene_type": "reunion",
            "is_chapter_last": 0,
        },
        headers={"X-Idempotency-Key": f"scene-{scene_id}"},
    )
    assert response.status_code == 200


def seed_voice_profile(session: Session, voice_profile_id: str = "VOICE_CHAR_A") -> None:
    session.add(
        VoiceProfile(
            row_id=f"voice_profile_{voice_profile_id}_v1",
            voice_profile_id=voice_profile_id,
            version=1,
            character_id=voice_profile_id.removeprefix("VOICE_"),
            content="short clipped lines; pressure makes the tone harder",
            active_flag=1,
            source_note="test baseline",
        )
    )
    session.commit()


def seed_relation_profile(
    session: Session,
    relation_profile_id: str = "REL_CHAR_A_CHAR_B",
    *,
    left_character_id: str = "CHAR_A",
    right_character_id: str = "CHAR_B",
) -> None:
    session.add(
        RelationProfile(
            row_id=f"relation_profile_{relation_profile_id}_v1",
            relation_profile_id=relation_profile_id,
            left_character_id=left_character_id,
            right_character_id=right_character_id,
            version=1,
            content="reunion tension; B knows slightly more than A",
            active_flag=1,
            source_note="test baseline",
        )
    )
    session.commit()


def seed_literary_ready_state(session: Session, scene_id: str = "CH910_SC01", chapter_id: str = "CH910") -> None:
    scene = session.get(SceneCard, scene_id)
    scene.writer_brief_json = dict(SCENE_WRITER_BRIEF_V2)
    session.add(
        SceneBlueprint(
            row_id=f"scene_blueprint_{scene_id}_seed",
            scene_id=scene_id,
            chapter_id=chapter_id,
            source_bundle_id=f"seed_source_{scene_id}",
            source_bundle_hash=f"seed_hash_{scene_id}",
            blueprint_json={
                "character_current_desire": "CHAR_A wants the truth before CHAR_B can leave.",
                "concrete_obstacle": "CHAR_B controls the old letter and refuses a straight answer.",
                "choice_under_pressure": "CHAR_A must choose between trust and exposure.",
                "information_release": "The letter points to a third watcher.",
                "power_shift": "CHAR_B loses control of the conversation.",
                "emotional_turn": "Suspicion turns into reluctant alliance.",
                "irreversible_consequence": "The secret is no longer private.",
                "ending_reader_question": "Who sent the old letter?",
                "image_promise": "The old letter returns with a changed meaning.",
            },
            status="accepted",
        )
    )
    session.commit()


def test_workbench_preflight_is_ready_when_scene_has_required_sources_and_fields(client, session: Session) -> None:
    create_chapter(client)
    create_scene(client)
    seed_voice_profile(session)
    seed_relation_profile(session)
    seed_literary_ready_state(session)

    response = client.get("/api/v1/scenes/CH910_SC01/workbench")

    assert response.status_code == 200
    payload = response.json()["data"]
    preflight = payload["run_preflight"]
    assert preflight == {
        "can_run": True,
        "overall_status": "ready",
        "blocking_items": [],
        "warning_items": [],
        "context_items": [],
        "constraint_conflicts": [],
    }
    assert payload["generation_summary"] is None
    assert payload["hard_qc_summary"] is None
    assert payload["soft_qc_summary"] is None
    assert payload["rewrite_counters"] == {
        "hard_partial_rewrite_count": 0,
        "hard_full_rewrite_count": 0,
        "soft_patch_count": 0,
        "repeat_issue_key": None,
        "repeat_issue_count": 0,
    }
    assert payload["human_review_summary"] is None
    # 风格参考 v3：终稿读数是唯一抄袭门的检查记录（没有终稿、没有绑定：放行，什么都没比对）
    scan = payload["source_safety_scan"]
    assert scan["safe"] is True and scan["blocked"] is False
    assert scan["version"] == "reference_copy_gate_v1"
    assert scan["checked_books"] == [] and scan["hits"] == [] and scan["protected_hits"] == []
    assert scan["hit_count"] == 0 and scan["protected_hit_count"] == 0
    assert scan["threshold_chars"] == 12


def test_workbench_payload_keeps_generation_and_qc_summaries_empty_before_any_run(client, session: Session) -> None:
    create_chapter(client, "CH915")
    create_scene(client, chapter_id="CH915", scene_id="CH915_SC01")
    seed_voice_profile(session)
    seed_relation_profile(session)

    response = client.get("/api/v1/scenes/CH915_SC01/workbench")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["generation_summary"] is None
    assert data["hard_qc_summary"] is None
    assert data["soft_qc_summary"] is None
    assert data["rewrite_counters"] == {
        "hard_partial_rewrite_count": 0,
        "hard_full_rewrite_count": 0,
        "soft_patch_count": 0,
        "repeat_issue_key": None,
        "repeat_issue_count": 0,
    }
    assert data["human_review_summary"] is None


def test_workbench_payload_scans_final_scene_for_protected_source_terms(
    client,
    session: Session,
    monkeypatch,
) -> None:
    """全局受保护词（环境变量）也过唯一抄袭门：只报位置与哈希，不报词本身。"""
    monkeypatch.setenv(
        "NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON",
        '["欧文·灰港", "盐湾学院"]',
    )
    create_chapter(client, "CH921")
    create_scene(client, chapter_id="CH921", scene_id="CH921_SC01")
    seed_voice_profile(session)
    seed_relation_profile(session)
    bundle = SceneBundle(
        bundle_id="bundle_CH921_SC01_v1",
        scene_id="CH921_SC01",
        chapter_id="CH921",
        bundle_snapshot_hash="hash_CH921_SC01_v1",
        frozen_snapshot_json={
            "scene_id": "CH921_SC01",
            "chapter_id": "CH921",
            "source_version_refs": {"reference_profile_id": "refprofile_synthetic"},
        },
    )
    final = FinalScene(
        row_id="final_scene_CH921_SC01_v1",
        scene_id="CH921_SC01",
        chapter_id="CH921",
        content="这一版误把欧文·灰港和盐湾学院写进了原创场景。",
        status="approved",
        source_bundle_id=bundle.bundle_id,
        source_bundle_hash=bundle.bundle_snapshot_hash,
    )
    state = session.get(SceneRunState, "CH921_SC01")
    assert state is not None
    state.scene_status = "archived"
    state.current_bundle_id = bundle.bundle_id
    state.current_bundle_hash = bundle.bundle_snapshot_hash
    state.current_final_scene_row_id = final.row_id
    session.add_all([bundle, final])
    session.commit()

    response = client.get("/api/v1/scenes/CH921_SC01/workbench")

    assert response.status_code == 200
    scan = response.json()["data"]["source_safety_scan"]
    assert scan["safe"] is False
    assert scan["hit_count"] == 0 and scan["protected_hit_count"] == 2
    assert [(item["start"], item["end"], item["source"]) for item in scan["protected_hits"]] == [
        (5, 10, "environment"),
        (11, 15, "environment"),
    ]
    assert "欧文" not in str(scan) and "盐湾" not in str(scan)


def test_workbench_payload_scans_final_scene_against_the_bound_reference(client, session: Session) -> None:
    """风格参考 v3：工作台的终稿读数就是唯一抄袭门——绑定的书连续照抄与画像的受保护专名都报。"""
    from tests.reference_copy_fixtures import PROTECTED_NAME, REFERENCE_PASSAGE, seed_bound_reference

    create_chapter(client, "CH921D")
    create_scene(client, chapter_id="CH921D", scene_id="CH921D_SC01")
    seed_voice_profile(session)
    seed_relation_profile(session)
    refs = seed_bound_reference(
        session,
        seed="workbench",
        scope="scene",
        scope_ref_id="CH921D_SC01",
        protected_terms=(PROTECTED_NAME,),
    )
    final = FinalScene(
        row_id="final_scene_CH921D_SC01_v1",
        scene_id="CH921D_SC01",
        chapter_id="CH921D",
        content=f"{PROTECTED_NAME}低头看表。{REFERENCE_PASSAGE[:30]}",
        status="archived",
        source_bundle_id="author_adopt",
        source_bundle_hash="author_adopt",
    )
    state = session.get(SceneRunState, "CH921D_SC01")
    state.scene_status = "archived"
    state.current_final_scene_row_id = final.row_id
    session.add(final)
    session.commit()

    scan = client.get("/api/v1/scenes/CH921D_SC01/workbench").json()["data"]["source_safety_scan"]

    assert scan["safe"] is False
    assert scan["checked_books"] == [refs["book_id"]]
    assert scan["protected_hit_count"] == 1 and scan["protected_hits"][0]["source"] == "protected_auto"
    assert scan["hit_count"] == 1
    assert REFERENCE_PASSAGE[:12] not in str(scan) and PROTECTED_NAME not in str(scan)


def test_workbench_preflight_does_not_block_on_missing_voice_or_relation_cards(client) -> None:
    """2026-09-20 真实故障：声线卡 / 关系卡在产品里已经没有地方能写，却是每一场起草的硬前提——

    真实作品的每一次「开始起草」都被预检拦下（VOICE_PROFILE_MISSING / RELATION_PROFILE_MISSING），
    唯一的出路是让预检自己铸一句占位套话。缺卡不再拦起草，也不再有「铸最小卡」这个动作。
    """
    create_chapter(client, "CH911")
    create_scene(client, chapter_id="CH911", scene_id="CH911_SC01")

    response = client.get("/api/v1/scenes/CH911_SC01/workbench")

    assert response.status_code == 200
    preflight = response.json()["data"]["run_preflight"]
    assert preflight["can_run"] is True
    assert preflight["blocking_items"] == []
    assert "missing_dependencies" not in preflight
    assert "create_actions" not in preflight
    assert client.post(
        "/api/v1/scenes/CH911_SC01/preflight/create-cards", headers={"X-Idempotency-Key": "gone-create-cards"}
    ).status_code == 404


def test_bundle_builds_without_voice_or_relation_cards_and_still_injects_existing_ones(
    client, session: Session
) -> None:
    """缺卡时 bundle 照常构建（过去 409 BUNDLE_SOURCE_MISSING），只是没有这两节；库里真有卡时照旧注入。"""
    from novel_system.services.bundle_builder import BundleBuilder

    create_chapter(client, "CH912")
    create_scene(client, chapter_id="CH912", scene_id="CH912_SC01")

    bare = BundleBuilder(session).build("CH912_SC01")["snapshot"]
    assert "voice_card" not in bare["inline_digests"]
    assert "relation_card" not in bare["inline_digests"]
    assert "voice_profile_id" not in bare["source_version_refs"]
    assert "relation_profile_id" not in bare["source_version_refs"]
    # 角色身份契约不依赖这两张卡
    assert "CHAR_A" in bare["inline_digests"]["character_contract"]

    seed_voice_profile(session)
    seed_relation_profile(session)
    carded = BundleBuilder(session).build("CH912_SC01", force_rebuild=True)["snapshot"]
    assert carded["inline_digests"]["voice_card"] == "short clipped lines; pressure makes the tone harder"
    assert carded["inline_digests"]["relation_card"] == "reunion tension; B knows slightly more than A"
    assert carded["source_version_refs"]["voice_profile_id"] == "VOICE_CHAR_A"
    assert carded["source_version_refs"]["relation_profile_id"] == "REL_CHAR_A_CHAR_B"


def test_workbench_preflight_surfaces_authoring_warnings_without_blocking_run(client) -> None:
    create_chapter(client, "CH913")
    create_scene(
        client,
        chapter_id="CH913",
        scene_id="CH913_SC01",
        pov_character_id="",
        onstage_chars_json=[],
        location="",
        scene_goal="",
        beats_json=[],
        must_include_text="",
    )

    response = client.get("/api/v1/scenes/CH913_SC01/workbench")

    assert response.status_code == 200
    preflight = response.json()["data"]["run_preflight"]
    assert preflight["can_run"] is True
    assert preflight["overall_status"] == "warning"
    assert preflight["blocking_items"] == []
    assert [item["code"] for item in preflight["warning_items"]] == [
        "SCENE_GOAL_MISSING",
        "SCENE_LOCATION_MISSING",
        "SCENE_POV_MISSING",
        "SCENE_ONSTAGE_CHARACTERS_MISSING",
        "SCENE_BEATS_MISSING",
        "SCENE_BLUEPRINT_MISSING",
        "SCENE_LITERARY_INTENT_INCOMPLETE",
    ]


def test_workbench_preflight_surfaces_constraint_conflicts(client, session: Session) -> None:
    create_chapter(client, "CH919")
    create_scene(client, chapter_id="CH919", scene_id="CH919_SC01")
    seed_voice_profile(session)
    seed_relation_profile(session)
    scene = session.get(SceneCard, "CH919_SC01")
    scene.hook = "以死亡证明作为雨夜钩子。"
    scene.forbidden_text = "死亡证明"
    session.commit()

    response = client.get("/api/v1/scenes/CH919_SC01/workbench")

    assert response.status_code == 200
    preflight = response.json()["data"]["run_preflight"]
    assert preflight["can_run"] is False
    assert preflight["overall_status"] == "blocked"
    assert preflight["constraint_conflicts"] == [
        {
            "term": "死亡证明",
            "required_source": "scene_card.hook",
            "forbidden_source": "scene_card.forbidden_text",
            "severity": "blocking",
            "human_readable_reason": "场景要求使用该词，但禁用规则又禁止该词；请先选择保留或替换。",
        }
    ]


def test_create_scene_rejects_corrupted_user_text(client) -> None:
    create_chapter(client, "CH920")

    response = client.post(
        "/api/v1/scenes",
        json={
            "scene_id": "CH920_SC01",
            "chapter_id": "CH920",
            "scene_seq": 1,
            "pov_character_id": "CHAR_A",
            "onstage_chars_json": ["CHAR_A"],
            "location": "Old city gate",
            "scene_goal": "???",
            "beats_json": ["beat-1"],
            "target_length_band": "short",
            "scene_type": "reunion",
            "is_chapter_last": 0,
        },
        headers={"X-Idempotency-Key": "scene-corrupted-text"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "TEXT_ENCODING_INVALID"


def test_workbench_does_not_resurrect_stale_human_review_event_when_current_pointer_is_cleared(
    client,
    session: Session,
) -> None:
    create_chapter(client, "CH915")
    create_scene(client, chapter_id="CH915", scene_id="CH915_SC01")
    seed_voice_profile(session)
    seed_relation_profile(session)

    state = session.get(SceneRunState, "CH915_SC01")
    state.current_human_review_event_id = None
    session.add(
        HumanReviewEvent(
            event_id="human_review_stale_CH915_SC01",
            scene_id="CH915_SC01",
            chapter_id="CH915",
            object_ref="scene_draft:draft_style_old_CH915_SC01",
            event_source="scene_generation",
            priority="high",
            status="open",
            details_json={
                "trigger_reason": "soft_qc_patch_cycle_limit",
                "failure_reason": "stale blocker from a previous run",
                "recommended_action": "human_review_required",
                "linked_target_ref": "scene_draft:draft_style_old_CH915_SC01",
            },
            created_at="2026-04-15T00:00:00+00:00",
        )
    )
    session.commit()

    response = client.get("/api/v1/scenes/CH915_SC01/workbench")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["human_review_summary"] is None


def test_workbench_soft_qc_summary_only_uses_reports_from_the_active_run(client, session: Session) -> None:
    create_chapter(client, "CH916")
    create_scene(client, chapter_id="CH916", scene_id="CH916_SC01")
    seed_voice_profile(session)
    seed_relation_profile(session)

    state = session.get(SceneRunState, "CH916_SC01")
    state.current_bundle_id = "bundle_current_CH916_SC01"
    state.current_bundle_hash = "hash_current_CH916_SC01"
    state.current_qc_report_id = "qc_report_current_hard_CH916_SC01"
    session.add_all(
        [
            QcReport(
                qc_report_id="qc_report_old_soft_CH916_SC01",
                scene_id="CH916_SC01",
                chapter_id="CH916",
                qc_type="soft_qc",
                source_draft_row_id="draft_style_old_CH916_SC01",
                source_bundle_id="bundle_previous_CH916_SC01",
                resolution_code="soft_pass",
                pass_flag=1,
                next_action="pass",
                issues_json=[],
                rewrite_brief_json=[],
                created_at="2026-04-15T00:20:00+00:00",
            ),
            QcReport(
                qc_report_id="qc_report_current_hard_CH916_SC01",
                scene_id="CH916_SC01",
                chapter_id="CH916",
                qc_type="hard_qc",
                source_draft_row_id="draft_neutral_current_CH916_SC01",
                source_bundle_id="bundle_current_CH916_SC01",
                resolution_code="hard_pass",
                pass_flag=1,
                next_action="pass",
                issues_json=[],
                rewrite_brief_json=[],
                created_at="2026-04-15T00:10:00+00:00",
            ),
        ]
    )
    session.commit()

    response = client.get("/api/v1/scenes/CH916_SC01/workbench")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["hard_qc_summary"]["qc_report_id"] == "qc_report_current_hard_CH916_SC01"
    assert payload["soft_qc_summary"] is None


def test_workbench_generation_summary_stays_empty_when_current_run_has_no_generation_pointer(
    client,
    session: Session,
) -> None:
    create_chapter(client, "CH917")
    create_scene(client, chapter_id="CH917", scene_id="CH917_SC01")
    seed_voice_profile(session)
    seed_relation_profile(session)

    session.add(
        LlmCall(
            llm_call_id="llm_call_stale_CH917_SC01",
            scope_type="scene",
            scope_id="CH917_SC01",
            provider="offline_deterministic",
            model="gpt-4.1-mini",
            prompt_hash="prompt_hash_stale_CH917_SC01",
            step="style_draft",
            scene_id="CH917_SC01",
            chapter_id="CH917",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            latency_ms=12,
            finish_reason="offline_fallback",
            error_code=None,
            created_at="2026-04-15T00:30:00+00:00",
        )
    )
    session.commit()

    response = client.get("/api/v1/scenes/CH917_SC01/workbench")

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["generation_summary"] is None
