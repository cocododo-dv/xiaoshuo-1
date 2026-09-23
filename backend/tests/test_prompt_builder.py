from __future__ import annotations

from copy import deepcopy

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    SceneCard,
    SceneMemory,
    SceneRunState,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
    StyleReferenceRun,
)
from novel_system.services.bundle_builder import BundleBuilder
from novel_system.services.prompt_builder import (
    PromptBuilder,
    PromptConfigurationError,
    load_prompt_templates,
)


def _bundle_snapshot() -> dict:
    return {
        "contract_version": "BSHASH_v1",
        "stage_allowlist_name": "bundle_build_allowlist_v1",
        "scene_id": "CH001_SC01",
        "chapter_id": "CH001",
        "source_version_refs": {
            "chapter_goal": "CH001",
            "scene_card": "CH001_SC01",
            "style_observation_ids": ["STY_SCENE_01", "STY_SCENE_02"],
        },
        "resolved_ref_ids": {
            "relation_ids": ["REL_A_B"],
            "world_rule_ids": ["WR_GLOBAL_014"],
            "open_foreshadow_ids": ["F014"],
        },
        "ordered_injections": [
            {"slot": "chapter_goal", "ref_id": "CH001", "digest_key": "chapter_goal"},
            {"slot": "scene_card", "ref_id": "CH001_SC01", "digest_key": "scene_card"},
            {
                "slot": "style_observations",
                "ref_id": "STY_SCENE_01",
                "digest_key": "style_observation",
            },
        ],
        "inline_digests": {
            "chapter_goal": "Close the reunion chapter with a traceable reveal.",
            "scene_card": "Reunite the leads and turn the old letter into immediate action.",
            "character_contract": (
                '{"contract_version":"CHARACTER_CONTRACT_v1","characters":'
                '[{"character_id":"CHAR_A","display_name":"Mira","pronouns":["she"],'
                '"role":"archivist","aliases":["M"]}]}'
            ),
            "voice_card": "Short clipped lines; pressure makes the tone harder.",
            "style_rule": "Keep emotion in gesture and pause.",
            "banned_rule": "Do not explain the whole backstory at reunion time.",
            "style_observation": (
                "Gesture before explanation. Let silence carry accusation. "
                "End paragraphs on pressure, not exposition. Keep the emotional turn tactile."
            ),
            "calibration_line": "The door closed like a sentence left unfinished.",
            "relation_card": "Reunion tension; B knows slightly more than A.",
            "world_rule": "Public spellcasting inside the city is forbidden.",
            "foreshadow": "The old letter sender clue is now in play.",
            "scene_memory": "Previous scene memory digest about the hidden sender.",
            "scene_summary": "Current scene summary digest about the reunion beat.",
            "chapter_summary": "Chapter summary digest about guarded trust replacing suspicion.",
            "similar_scene": (
                "Similar-scene reference: another gate reunion leaned too heavily on explanation "
                "and lost pressure halfway through."
            ),
        },
    }


def test_bundle_snapshot_carries_active_reference_profile_provenance(session) -> None:
    session.add(
        StoryProject(
            project_id="PROJ_REF_PROV",
            title="Reference provenance",
            outline_text="",
            planning_mode="snowflake",
        )
    )
    session.add(
        ChapterGoal(
            chapter_id="CH_REF_PROV",
            project_id="PROJ_REF_PROV",
            planned_scene_count=1,
            chapter_goal="Keep reference provenance in the frozen bundle.",
        )
    )
    session.add(
        SceneCard(
            scene_id="CH_REF_PROV_SC01",
            chapter_id="CH_REF_PROV",
            project_id="PROJ_REF_PROV",
            scene_seq=1,
            onstage_chars_json=[],
            scene_goal="Draft an original scene with auditable reference provenance.",
        )
    )
    session.add(SceneRunState(scene_id="CH_REF_PROV_SC01"))
    session.add(
        StyleReferenceBook(
            book_id="sr_book_ref_prov",
            title="Public domain source",
            source_kind="path",
            cloud_policy="segments_only",
            text_checksum="checksum-ref-prov",
            stats_json={"rights_declaration": {"declared": True, "send_rights": True}},
        )
    )
    session.add(
        StyleReferenceRun(
            run_id="sr_run_ref_prov", book_id="sr_book_ref_prov", status="done"
        )
    )
    session.add(
        StyleReferenceProfile(
            profile_id="sr_profile_ref_prov",
            book_id="sr_book_ref_prov",
            run_id="sr_run_ref_prov",
            title="Audited profile",
            status="active",
            profile_json={"style_features": ["abstract craft only"]},
        )
    )
    session.add(
        StyleReferenceInjectionBinding(
            binding_id="sr_bind_ref_prov",
            profile_id="sr_profile_ref_prov",
            scope="project",
            scope_ref_id="PROJ_REF_PROV",
            task_type="scene_generation",
            strategy="A",
            status="active",
        )
    )
    session.commit()

    snapshot = BundleBuilder(session).build("CH_REF_PROV_SC01")["snapshot"]

    assert snapshot["source_version_refs"]["reference_profile_ids"] == [
        "sr_profile_ref_prov"
    ]
    from novel_system.services.style_reference.runtime_contract import (
        style_runtime_contract_from_bundle,
    )

    contract = style_runtime_contract_from_bundle(snapshot)
    assert contract is not None
    assert contract["profile_ids"] == ["sr_profile_ref_prov"]
    assert contract["binding_ids"] == ["sr_bind_ref_prov"]
    assert (
        snapshot["source_version_refs"]["style_reference_runtime_contract_hash"]
        == contract["contract_hash"]
    )
    assert (
        snapshot["source_version_refs"]["style_reference_runtime_contract_status"]
        == "frozen"
    )


def test_prompt_builder_hash_changes_only_for_relevant_inputs() -> None:
    builder = PromptBuilder()
    baseline_snapshot = _bundle_snapshot()
    irrelevant_change = deepcopy(baseline_snapshot)
    relevant_change = deepcopy(baseline_snapshot)

    irrelevant_change["source_version_refs"]["debug_timestamp"] = "2026-04-14T21:00:00Z"
    relevant_change["inline_digests"][
        "scene_card"
    ] = "The leads reunite, but the letter clue stays buried."

    baseline = builder.build(baseline_snapshot, "neutral_draft")
    same_hash = builder.build(irrelevant_change, "neutral_draft")
    changed_hash = builder.build(relevant_change, "neutral_draft")

    assert baseline["prompt_hash"] == same_hash["prompt_hash"]
    assert baseline["prompt_hash"] != changed_hash["prompt_hash"]


def test_prompt_builder_enforces_budget_using_rendered_prompt_shape() -> None:
    builder = PromptBuilder()
    snapshot = _bundle_snapshot()

    baseline = builder.build(snapshot, "neutral_draft")
    threshold = baseline["token_budget"]["estimated_input_tokens"] - 1

    payload = builder.build(snapshot, "neutral_draft", max_input_tokens=threshold)

    assert payload["token_budget"]["estimated_input_tokens"] <= threshold
    assert (
        payload["token_budget"]["section_status"]["similar_scene_context"]["status"]
        == "omitted"
    )


def test_prompt_builder_returns_isolated_schema_copies() -> None:
    builder = PromptBuilder()
    snapshot = _bundle_snapshot()

    original = builder.build(snapshot, "neutral_draft")
    original_hash = original["prompt_hash"]
    original["structured_schema"]["required"].append("mutated_field")
    original["structured_schema"]["properties"]["scene_text"]["type"] = "array"

    repeated = builder.build(snapshot, "neutral_draft")

    assert repeated["prompt_hash"] == original_hash
    assert repeated["structured_schema"]["required"] == ["scene_text"]
    assert repeated["structured_schema"]["properties"]["scene_text"]["type"] == "string"


def test_prompt_builder_injects_literary_freshness_budget() -> None:
    builder = PromptBuilder()
    snapshot = _bundle_snapshot()
    snapshot["inline_digests"][
        "literary_freshness_budget"
    ] = """
{
  "schema_version": "literary_freshness_budget_v1",
  "avoid_action_templates": ["pronoun_looked_at_object_then_silence"],
  "avoid_image_fields": ["钥匙"],
  "avoid_summary_endings": ["一切都变了"]
}
""".strip()

    payload = builder.build(snapshot, "style_draft")

    assert "## Literary Freshness Budget" in payload["user_prompt"]
    assert "pronoun_looked_at_object_then_silence" in payload["user_prompt"]
    assert "avoid_summary_endings" in payload["user_prompt"]
    assert "literary_freshness_budget" in payload["token_budget"]["included_sections"]


def test_chapter_summary_schema_requires_carry_forward() -> None:
    payload = PromptBuilder().build(_bundle_snapshot(), "chapter_summary")

    assert payload["structured_schema"]["required"] == ["summary", "carry_forward"]


def test_writer_passage_patch_schema_is_manual_only_and_targeted() -> None:
    payload = PromptBuilder().build(_bundle_snapshot(), "writer_passage_patch")

    assert payload["structured_schema"]["required"] == [
        "patches",
        "rationale",
        "manual_only",
    ]
    patch_schema = payload["structured_schema"]["properties"]["patches"]["items"]
    assert patch_schema["required"] == [
        "target_text_ref",
        "source_excerpt",
        "replacement_text",
        "patch_type",
        "changed_dimensions",
        "why_it_helps",
    ]
    assert (
        "Required top-level JSON keys: patches, rationale, manual_only"
        in payload["user_prompt"]
    )


def test_hard_qc_uses_runtime_minimum_budget_for_default_runs(tmp_path) -> None:
    prompt_path = tmp_path / "prompts.yaml"
    prompt_path.write_text(
        """
templates:
  hard_qc:
    version: "test"
    input_token_budget: 60
    system_prompt: "system"
    task_prompt: "task"
    structured_schema:
      type: object
      additionalProperties: false
      required:
        - resolution_code
        - pass_flag
        - next_action
        - issues
      properties:
        resolution_code:
          type: string
        pass_flag:
          type: boolean
        next_action:
          type: string
        issues:
          type: array
          items:
            type: object
        rewrite_brief:
          type: array
          items:
            type: string
""".strip(),
        encoding="utf-8",
    )
    builder = PromptBuilder(prompt_path)

    default_payload = builder.build(_bundle_snapshot(), "hard_qc")
    explicit_payload = builder.build(_bundle_snapshot(), "hard_qc", max_input_tokens=60)

    assert default_payload["token_budget"]["target_input_tokens"] >= 3200
    assert explicit_payload["token_budget"]["target_input_tokens"] == 60


def test_prompt_builder_passes_template_task_kind_to_context_budget() -> None:
    builder = PromptBuilder()

    hard_qc = builder.build(_bundle_snapshot(), "hard_qc", max_input_tokens=120)
    drafting = builder.build(_bundle_snapshot(), "style_draft", max_input_tokens=120)
    chapter_review = builder.build(
        _bundle_snapshot(), "chapter_summary", max_input_tokens=120
    )

    assert hard_qc["token_budget"]["task_kind"] == "hard_qc"
    assert (
        "drop_style_context_before_fact_context"
        in hard_qc["token_budget"]["continuity_policy"]
    )
    assert drafting["token_budget"]["task_kind"] == "drafting"
    assert (
        "preserve_style_profile_author_preference_and_calibration"
        in drafting["token_budget"]["continuity_policy"]
    )
    assert chapter_review["token_budget"]["task_kind"] == "chapter_review"
    assert (
        "preserve_chapter_promise_payoff_and_memory"
        in chapter_review["token_budget"]["continuity_policy"]
    )


def test_hard_qc_schema_requires_rewrite_brief_for_runtime_validator() -> None:
    payload = PromptBuilder().build(_bundle_snapshot(), "hard_qc")

    assert payload["structured_schema"]["required"] == [
        "resolution_code",
        "pass_flag",
        "next_action",
        "issues",
        "rewrite_brief",
    ]
    assert (
        "Required top-level JSON keys: resolution_code, pass_flag, next_action, issues, rewrite_brief"
        in payload["user_prompt"]
    )


def test_load_prompt_templates_rejects_invalid_config(tmp_path) -> None:
    missing_field_path = tmp_path / "prompts_missing.yaml"
    missing_field_path.write_text(
        """
templates:
  neutral_draft:
    version: "2026-04-14.v1"
    input_token_budget: 2600
    system_prompt: "system"
    structured_schema: {}
""".strip(),
        encoding="utf-8",
    )

    wrong_type_path = tmp_path / "prompts_wrong_type.yaml"
    wrong_type_path.write_text(
        """
templates:
  neutral_draft:
    version: 20260414
    input_token_budget: "2600"
    system_prompt: "system"
    task_prompt: "task"
    structured_schema: {}
""".strip(),
        encoding="utf-8",
    )

    try:
        load_prompt_templates(missing_field_path)
    except PromptConfigurationError as exc:
        assert (
            str(exc) == "template neutral_draft is missing required fields: task_prompt"
        )
    else:
        raise AssertionError("expected missing-field prompt config to be rejected")

    try:
        load_prompt_templates(wrong_type_path)
    except PromptConfigurationError as exc:
        assert str(exc) == "template neutral_draft.version must be a string"
    else:
        raise AssertionError("expected wrong-type prompt config to be rejected")


def test_load_prompt_templates_rejects_invalid_structured_schema_shape(
    tmp_path,
) -> None:
    invalid_schema_path = tmp_path / "prompts_invalid_schema.yaml"
    invalid_schema_path.write_text(
        """
templates:
  neutral_draft:
    version: "2026-04-14.v1"
    input_token_budget: 2600
    system_prompt: "system"
    task_prompt: "task"
    structured_schema:
      type: array
      properties: []
      required: scene_text
""".strip(),
        encoding="utf-8",
    )

    try:
        load_prompt_templates(invalid_schema_path)
    except PromptConfigurationError as exc:
        assert (
            str(exc) == "template neutral_draft.structured_schema.type must be 'object'"
        )
    else:
        raise AssertionError("expected invalid structured_schema shape to be rejected")


def test_load_prompt_templates_rejects_unsupported_structured_schema_type(
    tmp_path,
) -> None:
    invalid_schema_path = tmp_path / "prompts_invalid_schema_type.yaml"
    invalid_schema_path.write_text(
        """
templates:
  neutral_draft:
    version: "2026-04-14.v1"
    input_token_budget: 2600
    system_prompt: "system"
    task_prompt: "task"
    structured_schema:
      type: object
      additionalProperties: false
      required:
        - scene_text
      properties:
        scene_text:
          type: dictionary
""".strip(),
        encoding="utf-8",
    )

    try:
        load_prompt_templates(invalid_schema_path)
    except PromptConfigurationError as exc:
        assert str(exc) == (
            "template neutral_draft.structured_schema.properties.scene_text.type "
            "has unsupported value dictionary"
        )
    else:
        raise AssertionError(
            "expected unsupported structured_schema type to be rejected"
        )


def test_load_prompt_templates_rejects_required_fields_missing_from_properties_when_closed(
    tmp_path,
) -> None:
    invalid_schema_path = tmp_path / "prompts_invalid_required.yaml"
    invalid_schema_path.write_text(
        """
templates:
  neutral_draft:
    version: "2026-04-14.v1"
    input_token_budget: 2600
    system_prompt: "system"
    task_prompt: "task"
    structured_schema:
      type: object
      additionalProperties: false
      required:
        - scene_text
        - continuity_notes
      properties:
        scene_text:
          type: string
""".strip(),
        encoding="utf-8",
    )

    try:
        load_prompt_templates(invalid_schema_path)
    except PromptConfigurationError as exc:
        assert str(exc) == (
            "template neutral_draft.structured_schema.required contains entries not declared in properties: "
            "continuity_notes"
        )
    else:
        raise AssertionError(
            "expected closed-schema required/property mismatch to be rejected"
        )


def test_bundle_builder_scene_digest_includes_operational_scene_constraints(
    session,
) -> None:
    session.add(
        ChapterGoal(
            chapter_id="CH901",
            planned_scene_count=1,
            chapter_goal="Open the trial with a visible cost.",
        )
    )
    session.add(
        SceneCard(
            scene_id="CH901_SC01",
            chapter_id="CH901",
            scene_seq=1,
            location="Moon bridge",
            scene_goal="Test the initiate without copying source material.",
            beats_json=["arrival", "seal wakes", "choice under pressure"],
            must_include_text="the spirit seal glows like cold jade",
            forbidden_text="Do not use source names.",
            exit_change="The mountain gate answers.",
            hook="continue",
            target_length_band="short",
            scene_type="cultivation_trial",
        )
    )
    session.add(SceneRunState(scene_id="CH901_SC01"))
    session.commit()

    snapshot = BundleBuilder(session).build("CH901_SC01")["snapshot"]
    scene_digest = snapshot["inline_digests"]["scene_card"]

    assert "Goal: Test the initiate without copying source material." in scene_digest
    assert "Location: Moon bridge" in scene_digest
    assert "Beats: arrival; seal wakes; choice under pressure" in scene_digest
    assert (
        "Required beats to weave naturally: the spirit seal glows like cold jade"
        in scene_digest
    )
    assert "Forbidden text: Do not use source names." in scene_digest
    assert "Exit change: The mountain gate answers." in scene_digest
    assert "Hook: continue" in scene_digest
    assert "Target length: short" in scene_digest


def test_bundle_builder_uses_only_prior_scene_memory(session) -> None:
    session.add(
        ChapterGoal(
            chapter_id="CH902",
            planned_scene_count=2,
            chapter_goal="Move from first sign to second choice.",
        )
    )
    session.add_all(
        [
            SceneCard(
                scene_id="CH902_SC01",
                chapter_id="CH902",
                scene_seq=1,
                onstage_chars_json=[],
                scene_goal="Open the chapter.",
            ),
            SceneCard(
                scene_id="CH902_SC02",
                chapter_id="CH902",
                scene_seq=2,
                onstage_chars_json=[],
                scene_goal="Continue after the first result.",
            ),
            SceneRunState(scene_id="CH902_SC01"),
            SceneRunState(scene_id="CH902_SC02"),
            SceneMemory(
                row_id="scene_memory_CH902_SC01_v1",
                scene_id="CH902_SC01",
                chapter_id="CH902",
                content="prior scene memory",
                source_bundle_id="bundle_CH902_SC01_v1",
                final_scene_row_id="final_scene_CH902_SC01_v1",
                active_flag=1,
                created_at="2026-04-20T00:00:00+00:00",
            ),
            SceneMemory(
                row_id="scene_memory_CH902_SC02_v1",
                scene_id="CH902_SC02",
                chapter_id="CH902",
                content="current scene stale memory",
                source_bundle_id="bundle_CH902_SC02_v1",
                final_scene_row_id="final_scene_CH902_SC02_v1",
                active_flag=1,
                created_at="2026-04-20T01:00:00+00:00",
            ),
        ]
    )
    session.commit()

    first_snapshot = BundleBuilder(session).build("CH902_SC01")["snapshot"]
    second_snapshot = BundleBuilder(session).build("CH902_SC02")["snapshot"]

    assert "scene_memory" not in first_snapshot["inline_digests"]
    assert second_snapshot["source_version_refs"]["scene_memory_prev"] == "CH902_SC01"
    assert second_snapshot["inline_digests"]["scene_memory"] == "prior scene memory"


def test_bundle_builder_adds_literary_freshness_budget_from_prior_final_scenes(
    session,
) -> None:
    session.add(
        ChapterGoal(
            chapter_id="CH903",
            planned_scene_count=3,
            chapter_goal="Protect the witness without draining the chapter rhythm.",
        )
    )
    session.add_all(
        [
            SceneCard(
                scene_id="CH903_SC01",
                chapter_id="CH903",
                scene_seq=1,
                scene_goal="First exchange.",
            ),
            SceneCard(
                scene_id="CH903_SC02",
                chapter_id="CH903",
                scene_seq=2,
                scene_goal="Second exchange.",
            ),
            SceneCard(
                scene_id="CH903_SC03",
                chapter_id="CH903",
                scene_seq=3,
                scene_goal="Break the pattern.",
            ),
            SceneRunState(scene_id="CH903_SC01"),
            SceneRunState(scene_id="CH903_SC02"),
            SceneRunState(scene_id="CH903_SC03"),
            FinalScene(
                row_id="final_scene_CH903_SC01_v1",
                scene_id="CH903_SC01",
                chapter_id="CH903",
                content="林岑低头看着钥匙，沉默了片刻。雨敲着门。她必须选择公开。",
                source_bundle_id="bundle_CH903_SC01_v1",
                source_bundle_hash="hash_CH903_SC01_v1",
            ),
            FinalScene(
                row_id="final_scene_CH903_SC02_v1",
                scene_id="CH903_SC02",
                chapter_id="CH903",
                content="许望低头看着证据，沉默了片刻。雨又敲着门。他必须选择隐瞒。",
                source_bundle_id="bundle_CH903_SC02_v1",
                source_bundle_hash="hash_CH903_SC02_v1",
            ),
        ]
    )
    session.commit()

    snapshot = BundleBuilder(session).build("CH903_SC03")["snapshot"]

    budget = snapshot["inline_digests"]["literary_freshness_budget"]
    assert "literary_freshness_budget_v1" in budget
    assert "pronoun_looked_at_object_then_silence" in budget
    assert "avoid_summary_endings" in budget
    assert snapshot["source_version_refs"][
        "literary_freshness_source_final_scene_ids"
    ] == [
        "final_scene_CH903_SC01_v1",
        "final_scene_CH903_SC02_v1",
    ]


# ---------------------------------------------------------------------------
# 2026-09 风格模仿 v2（W5，规格 §1.3 / §2.W5）：三个新 section 的可见性与 bundle 登记
# ---------------------------------------------------------------------------


def _v2_style_snapshot() -> dict:
    snapshot = _bundle_snapshot()
    snapshot["inline_digests"].update(
        {
            "style_narrative_guidance": (
                "以下是参考作品的叙事取舍机制：\n- 关键信息放段首一次给出\n- 结尾不解释动机"
            ),
            "previous_scene_voice_anchor": "他把杯子放回桌上，没有看她。窗外的雨声更紧了些。",
            # 风格参考 v3 之前的 bundle 残留：漂移校准段已删，任何模板都不再渲染它
            "style_drift_calibration": "- 逗号再密一点\n- 少用然而",
        }
    )
    return snapshot


def test_neutral_draft_prompt_sees_narrative_mechanisms_only() -> None:
    """neutral_draft：叙事机制块可见；前文声音锚 / 漂移校准 / 语言层块与样例都不可见。"""
    builder = PromptBuilder()
    payload = builder.build(_v2_style_snapshot(), "neutral_draft")
    user_prompt = payload["user_prompt"]
    assert "## Style Reference — Narrative Mechanisms" in user_prompt
    assert "关键信息放段首一次给出" in user_prompt
    assert "Previous Scene Voice Anchor" not in user_prompt
    assert "Style Drift Calibration" not in user_prompt
    # 语言层：style_observations / calibration_lines 仍被中性稿屏蔽
    assert "Gesture before explanation" not in user_prompt
    assert "The door closed like a sentence" not in user_prompt
    assert "[STYLE_REFERENCE]" not in payload["system_prompt"]
    assert "[风格样例]" not in payload["system_prompt"]
    assert "If a Style Reference — Narrative Mechanisms section is present" in user_prompt
    assert "keep diction neutral" in user_prompt
    omitted = set(payload["token_budget"]["omitted_sections"])
    assert "previous_scene_voice_anchor" in omitted
    assert "style_narrative_guidance" not in omitted
    assert "逗号再密一点" not in user_prompt


def test_style_draft_prompt_sees_v2_sections_and_new_contract_wording() -> None:
    builder = PromptBuilder()
    payload = builder.build(_v2_style_snapshot(), "style_draft")
    user_prompt = payload["user_prompt"]
    assert "## Style Reference — Narrative Mechanisms" in user_prompt
    assert "## Previous Scene Voice Anchor (own prose; keep the same voice)" in user_prompt
    # 风格参考 v3：漂移校准段已删，旧 bundle 里的残留摘要不渲染（style_draft 模板里那句条件式的
    # 「treat Style Drift Calibration lines as…」留给 P5b 改写风格步时一起删）
    assert "## Style Drift Calibration" not in user_prompt
    assert "逗号再密一点" not in user_prompt
    assert "他把杯子放回桌上" in user_prompt
    system_prompt = payload["system_prompt"]
    # 七维契约与已移除层的引用不再出现；真实块名与消费顺序出现
    assert "Style Feature Contract" not in system_prompt
    assert "Longform Structure Guidance" not in user_prompt
    assert "Chapter Story Architecture" not in user_prompt
    for block in ("[禁忌模式]", "[声音特征]", "[正向风格特征]", "[风格分布指导]", "[风格样例]"):
        assert block in system_prompt
    assert "[禁止复刻]" not in system_prompt  # 2026-09-22:幽灵标签删除,真实块名是 [禁忌模式]
    assert "[风格样例] block at the end of the user message" in system_prompt
    assert "If no [STYLE_REFERENCE] block is present" in system_prompt
    # 骨架约束放宽 + 新鲜度预算复沓豁免的预留句
    assert "sentence order, pause placement, information-release order, and paragraph selection may be rearranged" in user_prompt
    assert "Do not add new events" in user_prompt
    assert "preserve_reference_repetition" in user_prompt
    # 「不可变骨架」只限定事实层（what happened），不再禁止重排句序 / 段落取舍（2026-09-22 v12 措辞）
    assert "Keep every fact, causal step, ending function, must-include item, character identity, and POV" in user_prompt
    assert "rebuild the prose from a blank page" in user_prompt
    assert "do not preserve its sentence shapes" not in user_prompt


def test_soft_qc_template_drops_seven_dimension_scoring() -> None:
    builder = PromptBuilder()
    payload = builder.build(_v2_style_snapshot(), "soft_qc")
    assert "Score style adherence from 0 to 1" not in payload["user_prompt"]
    assert "[声音特征]" in payload["system_prompt"]
    assert "[正向风格特征]" in payload["user_prompt"]
    # outcome 元组与 literary risks 项保留
    assert "soft_block_human / false / human_review_required" in payload["user_prompt"]
    assert "model_voice, image_homogeneity, expository_dialogue" in payload["user_prompt"]


def _seed_v2_work(session, *, project_id: str, chapters: int = 1, scenes_per_chapter: int = 3) -> None:
    session.add(
        StoryProject(project_id=project_id, title="v2 continuity", outline_text="", planning_mode="snowflake")
    )
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
                )
            )
            session.add(SceneRunState(scene_id=scene_id))
    session.commit()


def _add_draft(session, *, scene_id: str, stage: str, content: str, created_at: str, status: str = "active") -> str:
    from novel_system.db.models import SceneDraft

    chapter_id = scene_id.rsplit("_SC", 1)[0]
    row_id = f"{stage}_{scene_id}_{created_at.replace(':', '').replace('-', '')}"
    session.add(
        SceneDraft(
            row_id=row_id,
            scene_id=scene_id,
            chapter_id=chapter_id,
            stage=stage,
            status=status,
            content=content,
            source_bundle_id=f"bundle_{scene_id}",
            source_bundle_hash="h",
            created_at=created_at,
        )
    )
    session.commit()
    return row_id


def _seed_v2_binding(session, *, project_id: str, seed: str, profile_json: dict) -> None:
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
            scope="project",
            scope_ref_id=project_id,
            task_type="scene_generation",
            strategy="A",
            status="active",
        )
    )
    session.commit()


def test_bundle_builder_registers_narrative_guidance_from_frozen_contract(session) -> None:
    _seed_v2_work(session, project_id="P_V2_NG", scenes_per_chapter=1)
    _seed_v2_binding(
        session,
        project_id="P_V2_NG",
        seed="v2ng",
        profile_json={
            "style_features": ["短句克制"],
            "narrative_guidance": ["关键信息放段首一次给出", "结尾以动作收束，不解释动机"],
        },
    )
    snapshot = BundleBuilder(session).build("P_V2_NG_CH01_SC01")["snapshot"]
    digest = snapshot["inline_digests"]["style_narrative_guidance"]
    assert "- 关键信息放段首一次给出" in digest and "- 结尾以动作收束，不解释动机" in digest
    assert "短句克制" not in digest
    slot = next(item for item in snapshot["ordered_injections"] if item["slot"] == "style_narrative_guidance")
    assert slot["digest_key"] == "style_narrative_guidance"
    assert slot["ref_id"] == snapshot["source_version_refs"]["style_reference_runtime_contract_hash"]
    assert snapshot["source_version_refs"]["style_narrative_guidance_line_count"] == 2
    # 中性稿看到叙事机制块，看不到语言层
    payload = PromptBuilder().build(snapshot, "neutral_draft")
    assert "## Style Reference — Narrative Mechanisms" in payload["user_prompt"]
    assert "关键信息放段首一次给出" in payload["user_prompt"]
    assert "[STYLE_REFERENCE]" not in payload["system_prompt"]


def test_bundle_builder_skips_narrative_guidance_for_legacy_profiles(session) -> None:
    _seed_v2_work(session, project_id="P_V2_LEG", scenes_per_chapter=1)
    _seed_v2_binding(session, project_id="P_V2_LEG", seed="v2leg", profile_json={"style_features": ["短句"]})
    snapshot = BundleBuilder(session).build("P_V2_LEG_CH01_SC01")["snapshot"]
    assert "style_narrative_guidance" not in snapshot["inline_digests"]
    assert all(item["slot"] != "style_narrative_guidance" for item in snapshot["ordered_injections"])
    assert snapshot["source_version_refs"]["style_reference_runtime_contract_status"] == "frozen"


def test_bundle_builder_voice_anchor_uses_latest_styled_draft_of_previous_scene(session) -> None:
    from novel_system.services.bundle_builder import load_continuity_budget

    _seed_v2_work(session, project_id="P_V2_VA", scenes_per_chapter=3)
    long_styled = "".join(
        f"第{i}句他把杯子放回桌上，没有看她，窗外的雨声更紧了些。" for i in range(40)
    )
    styled_row = _add_draft(
        session, scene_id="P_V2_VA_CH01_SC01", stage="style_draft", content=long_styled,
        created_at="2026-09-05T10:00:00",
    )
    # 更新的中性稿 / 被否决稿都不算「已风格化前文」
    _add_draft(
        session, scene_id="P_V2_VA_CH01_SC01", stage="neutral_draft", content="中性稿不该被当作声音锚。",
        created_at="2026-09-05T11:00:00",
    )
    _add_draft(
        session, scene_id="P_V2_VA_CH01_SC01", stage="style_rejected", content="被拒稿不该被当作声音锚。",
        created_at="2026-09-05T12:00:00", status="rejected",
    )
    builder = BundleBuilder(session)

    # 第一场：没有前文 → 不登记
    first = builder.build("P_V2_VA_CH01_SC01")["snapshot"]
    assert "previous_scene_voice_anchor" not in first["inline_digests"]
    assert all(item["slot"] != "previous_scene_voice_anchor" for item in first["ordered_injections"])

    # 第二场：取第一场最新已风格化稿的尾部，≤ continuity_anchor_max_chars 且从句边界起头
    second = builder.build("P_V2_VA_CH01_SC02")["snapshot"]
    anchor = second["inline_digests"]["previous_scene_voice_anchor"]
    max_chars = load_continuity_budget()["continuity_anchor_max_chars"]
    assert 0 < len(anchor) <= max_chars
    assert long_styled.endswith(anchor)
    assert long_styled[len(long_styled) - len(anchor) - 1] == "。"
    assert "中性稿" not in anchor and "被拒稿" not in anchor
    refs = second["source_version_refs"]
    assert refs["previous_scene_voice_anchor_scene_id"] == "P_V2_VA_CH01_SC01"
    assert refs["previous_scene_voice_anchor_draft_row_id"] == styled_row
    assert refs["previous_scene_voice_anchor_stage"] == "style_draft"
    slot = next(item for item in second["ordered_injections"] if item["slot"] == "previous_scene_voice_anchor")
    assert slot["ref_id"] == styled_row and slot["digest_key"] == "previous_scene_voice_anchor"

    # 第三场：第二场没有稿 → 回退到同章更早的第一场
    third = builder.build("P_V2_VA_CH01_SC03")["snapshot"]
    assert third["source_version_refs"]["previous_scene_voice_anchor_scene_id"] == "P_V2_VA_CH01_SC01"

    # 可见性：style_draft 看到，neutral_draft 看不到
    style_payload = PromptBuilder().build(second, "style_draft")
    assert "## Previous Scene Voice Anchor (own prose; keep the same voice)" in style_payload["user_prompt"]
    neutral_payload = PromptBuilder().build(second, "neutral_draft")
    assert "Previous Scene Voice Anchor" not in neutral_payload["user_prompt"]


def test_bundle_builder_voice_anchor_falls_back_to_previous_chapter_last_scene(session) -> None:
    _seed_v2_work(session, project_id="P_V2_CH", chapters=2, scenes_per_chapter=2)
    _add_draft(
        session, scene_id="P_V2_CH_CH01_SC01", stage="style_draft", content="第一章第一场的风格稿。",
        created_at="2026-09-05T10:00:00",
    )
    _add_draft(
        session, scene_id="P_V2_CH_CH01_SC02", stage="de_template", content="第一章最后一场的去模板稿。",
        created_at="2026-09-05T10:30:00",
    )
    snapshot = BundleBuilder(session).build("P_V2_CH_CH02_SC01")["snapshot"]
    assert snapshot["inline_digests"]["previous_scene_voice_anchor"] == "第一章最后一场的去模板稿。"
    refs = snapshot["source_version_refs"]
    assert refs["previous_scene_voice_anchor_scene_id"] == "P_V2_CH_CH01_SC02"
    assert refs["previous_scene_voice_anchor_stage"] == "de_template"


_NEUTRAL_FALLBACK_TEXT = "他把杯子放回桌上。他没有看她。窗外在下雨。雨声比刚才大了一些。"


def test_bundle_builder_voice_anchor_skips_style_draft_that_carries_neutral_fallback_text(session) -> None:
    """风格稿未过安全门时 scene_generation 把已批准的中性稿原文写成主 style_draft 行
    （STYLE_DRAFT_FALLBACK_NEUTRAL）：这一行不算「已风格化前文」，声音锚退到更早的真风格稿。"""
    from novel_system.services.bundle_builder import latest_styled_draft_for_scene

    _seed_v2_work(session, project_id="P_V2_NF", scenes_per_chapter=2)
    genuine_row = _add_draft(
        session,
        scene_id="P_V2_NF_CH01_SC01",
        stage="de_template",
        content="真正带文风的去模板稿。",
        created_at="2026-09-05T10:00:00",
    )
    _add_draft(
        session,
        scene_id="P_V2_NF_CH01_SC01",
        stage="neutral_draft",
        content=_NEUTRAL_FALLBACK_TEXT,
        created_at="2026-09-05T11:00:00",
    )
    _add_draft(
        session,
        scene_id="P_V2_NF_CH01_SC01",
        stage="style_rejected",
        content="被安全门否决的 provider 风格稿。",
        created_at="2026-09-05T12:00:00",
        status="rejected",
    )
    # 主 style_draft 行：status 仍是默认 active，正文却是中性稿原文
    fallback_row = _add_draft(
        session,
        scene_id="P_V2_NF_CH01_SC01",
        stage="style_draft",
        content=_NEUTRAL_FALLBACK_TEXT,
        created_at="2026-09-05T12:00:00",
    )

    latest = latest_styled_draft_for_scene(session, "P_V2_NF_CH01_SC01")
    assert latest is not None and latest.row_id == genuine_row and latest.row_id != fallback_row

    snapshot = BundleBuilder(session).build("P_V2_NF_CH01_SC02")["snapshot"]
    assert snapshot["inline_digests"]["previous_scene_voice_anchor"] == "真正带文风的去模板稿。"
    refs = snapshot["source_version_refs"]
    assert refs["previous_scene_voice_anchor_draft_row_id"] == genuine_row
    assert refs["previous_scene_voice_anchor_stage"] == "de_template"
    style_payload = PromptBuilder().build(snapshot, "style_draft")
    assert _NEUTRAL_FALLBACK_TEXT not in style_payload["user_prompt"]


def test_bundle_builder_voice_anchor_absent_when_only_styled_row_is_neutral_fallback(session) -> None:
    """同章只有一条「中性稿原文」的 style_draft 行、又没有上一章 → 不登记声音锚，而不是钉在中性稿上。"""
    from novel_system.services.bundle_builder import latest_styled_draft_for_scene, previous_scene_voice_anchor

    _seed_v2_work(session, project_id="P_V2_NFO", scenes_per_chapter=2)
    _add_draft(
        session,
        scene_id="P_V2_NFO_CH01_SC01",
        stage="neutral_draft",
        content=_NEUTRAL_FALLBACK_TEXT,
        created_at="2026-09-05T11:00:00",
    )
    _add_draft(
        session,
        scene_id="P_V2_NFO_CH01_SC01",
        stage="style_draft",
        content=_NEUTRAL_FALLBACK_TEXT + "\n",
        created_at="2026-09-05T12:00:00",
    )
    assert latest_styled_draft_for_scene(session, "P_V2_NFO_CH01_SC01") is None
    scene2 = session.get(SceneCard, "P_V2_NFO_CH01_SC02")
    assert previous_scene_voice_anchor(session, scene2) is None

    snapshot = BundleBuilder(session).build("P_V2_NFO_CH01_SC02")["snapshot"]
    assert "previous_scene_voice_anchor" not in snapshot["inline_digests"]
    assert all(item["slot"] != "previous_scene_voice_anchor" for item in snapshot["ordered_injections"])
    assert "previous_scene_voice_anchor_scene_id" not in snapshot["source_version_refs"]


def test_bundle_builder_voice_anchor_honours_attempt_tracker_neutral_fallback_marker(session) -> None:
    """AttemptTracker.details_json.content_source == approved_neutral_fallback 的 styled 行同样被跳过——
    即使该场景的 neutral_draft 行已不在（正文比对无从下手）。"""
    from novel_system.db.models import AttemptTracker
    from novel_system.services.bundle_builder import latest_styled_draft_for_scene

    _seed_v2_work(session, project_id="P_V2_NFM", scenes_per_chapter=2)
    genuine_row = _add_draft(
        session,
        scene_id="P_V2_NFM_CH01_SC01",
        stage="style_patch",
        content="带文风的软补丁稿。",
        created_at="2026-09-05T10:00:00",
    )
    marked_row = _add_draft(
        session,
        scene_id="P_V2_NFM_CH01_SC01",
        stage="style_draft",
        content=_NEUTRAL_FALLBACK_TEXT,
        created_at="2026-09-05T12:00:00",
    )
    session.add(
        AttemptTracker(
            scene_id="P_V2_NFM_CH01_SC01",
            chapter_id="P_V2_NFM_CH01",
            step="style_draft",
            status="completed",
            source_bundle_id="bundle_P_V2_NFM_CH01_SC01",
            details_json={"row_id": marked_row, "content_source": "approved_neutral_fallback"},
        )
    )
    session.commit()

    latest = latest_styled_draft_for_scene(session, "P_V2_NFM_CH01_SC01")
    assert latest is not None and latest.row_id == genuine_row
    snapshot = BundleBuilder(session).build("P_V2_NFM_CH01_SC02")["snapshot"]
    assert snapshot["inline_digests"]["previous_scene_voice_anchor"] == "带文风的软补丁稿。"
    assert snapshot["source_version_refs"]["previous_scene_voice_anchor_stage"] == "style_patch"


def test_bundle_builder_never_carries_drift_calibration(session) -> None:
    """风格参考 v3：漂移驾驶已删——库里残留的 style_drift_observed 事件不再进 bundle（没有校准段、
    没有漂移优先选窗的 ``_drift_ptype_priority``、没有 style_drift_calibration_* 引用）。"""
    from novel_system.db.models import StyleReferenceMetricEvent

    _seed_v2_work(session, project_id="P_V2_DC", scenes_per_chapter=2)
    session.add(
        StyleReferenceMetricEvent(
            event_id="sr_metric_legacy_drift",
            event_kind="style_drift_observed",
            target_kind="scene",
            target_ref_id="P_V2_DC_CH01_SC01",
            outcome="observed",
            context_json={
                "chapter_id": "P_V2_DC_CH01",
                "scene_seq": 1,
                "calibration_lines": ["逗号再密一点"],
                "drift_ptype_priority": ["dialogue"],
            },
        )
    )
    session.commit()

    snapshot = BundleBuilder(session).build("P_V2_DC_CH01_SC02")["snapshot"]
    assert "style_drift_calibration" not in snapshot["inline_digests"]
    assert "_drift_ptype_priority" not in snapshot["inline_digests"]
    assert not any(key.startswith("style_drift_calibration") for key in snapshot["source_version_refs"])
    assert not any(item["slot"] == "style_drift_calibration" for item in snapshot["ordered_injections"])
    assert "逗号再密一点" not in PromptBuilder().build(snapshot, "style_draft")["user_prompt"]
