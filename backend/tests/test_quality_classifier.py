"""Wave 2 · 统一质量分级分类器（治理 §5.4/§6.1）。

纪律断言（先红后绿）：
- blocking 由 quality_level 派生并强制一致（Q2/Q3 恒 false，自由组合被纠正）。
- 单个 LLM 判断不能直接生成 Q0/Q1：升级必须经确定性复核（verified_by 必填）。
- 无法给出确定证据时自动降 Q2，并记录 downgraded_from。
- 未知 issue_key 默认 Q2（保守不阻断）。
"""

from __future__ import annotations

from novel_system.db.models import SceneCard
from novel_system.services.quality_classifier import (
    BLOCKING_LEVELS,
    ISSUE_KEY_POLICY,
    blocking_issues,
    classify_issue,
    classify_issues,
    has_blocking,
)


def _scene(**overrides) -> SceneCard:
    fields = {
        "scene_id": "CHX_SC01",
        "chapter_id": "CHX",
        "scene_seq": 1,
        "must_include_text": "A red envelope changes hands.",
        "forbidden_text": "",
    }
    fields.update(overrides)
    return SceneCard(**fields)


# ---------- blocking 派生一致性 ----------

def test_blocking_is_derived_from_level_and_free_combination_is_corrected() -> None:
    issue = classify_issue(
        {"issue_key": "theme_relevance_warning", "message": "theme unclear", "blocking": True},
        scene=_scene(),
        content="正文",
    )
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False  # Q2 恒不阻断，自由组合被纠正


def test_q3_style_and_tension_prefixes_never_block() -> None:
    for key in ("style_compliance", "style_rule_violation", "style_profile_drift", "tension_monotony", "tension_adjacent_tag_repeat"):
        issue = classify_issue({"issue_key": key, "message": "m"}, scene=_scene(), content="正文")
        assert issue["quality_level"] == "Q3", key
        assert issue["blocking"] is False, key


def test_unknown_issue_key_defaults_to_q2_non_blocking() -> None:
    issue = classify_issue({"issue_key": "totally_new_key", "message": "m"}, scene=_scene(), content="正文")
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert issue["verified_by"] is None


def test_execution_failure_keys_are_q2_warnings_not_blocks() -> None:
    for key in (
        "hard_qc_execution_failed",
        "soft_qc_execution_failed",
        "invalid_hard_qc_payload",
        "invalid_soft_qc_payload",
        "continuity_budget_exceeded",
    ):
        issue = classify_issue({"issue_key": key, "message": "m"}, scene=_scene(), content="正文")
        assert issue["quality_level"] == "Q2", key
        assert issue["blocking"] is False, key


# ---------- LLM 提案不得单独 Q0/Q1 ----------

def test_llm_wordlist_blocking_keys_downgrade_without_deterministic_evidence() -> None:
    # 旧 BLOCKING_QC_ISSUE_KEYS 中无确定性生产者的键：LLM 单独提案 → 降 Q2
    for key in ("scene_conflict_missing", "instruction_residue"):
        issue = classify_issue({"issue_key": key, "message": "m"}, scene=_scene(), content="正文")
        assert issue["quality_level"] == "Q2", key
        assert issue["blocking"] is False, key


def test_llm_source_leak_claim_downgrades_when_scan_is_clean() -> None:
    issue = classify_issue(
        {"issue_key": "source_leak_risk", "message": "可能泄漏"},
        scene=_scene(),
        content="一段完全原创、不含任何保护词的正文。",
    )
    assert issue["quality_level"] == "Q2"
    assert issue["blocking"] is False
    assert issue["downgraded_from"] == "Q0"
    assert issue["downgrade_reason"] == "no_deterministic_verification"


def test_llm_pronoun_drift_claim_downgrades_without_deterministic_detector() -> None:
    issue = classify_issue(
        {"issue_key": "character_pronoun_drift", "message": "代词漂移", "source": "llm_advisory"},
        scene=_scene(),
        content="正文",
    )
    assert issue["quality_level"] == "Q2"
    assert issue["downgraded_from"] == "Q1"


# ---------- 确定性复核通过 → Q0/Q1 + verified_by ----------

def test_source_leak_verified_by_deterministic_scan_blocks(monkeypatch) -> None:
    monkeypatch.setenv("NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON", '["路明非"]')
    issue = classify_issue(
        {"issue_key": "source_leak_risk", "message": "命中保护词"},
        scene=_scene(),
        content="他想起路明非说过的话。",
    )
    assert issue["quality_level"] == "Q0"
    assert issue["blocking"] is True
    assert issue["verified_by"] == "source_safety_scan"
    assert issue["authority_ref"]


def test_deterministic_pronoun_drift_is_verified_q1() -> None:
    issue = classify_issue(
        {"issue_key": "character_pronoun_drift", "message": "代词漂移", "source": "deterministic"},
        scene=_scene(),
        content="正文",
    )
    assert issue["quality_level"] == "Q1"
    assert issue["blocking"] is True
    assert issue["verified_by"]
    assert issue["source"] == "deterministic"


def test_missing_required_text_verified_only_when_truly_missing() -> None:
    scene = _scene()
    missing = classify_issue(
        {"issue_key": "missing_required_text", "message": "缺少 red envelope 线索"},
        scene=scene,
        content="正文里没有那个必备元素。",
    )
    assert missing["quality_level"] == "Q1"
    assert missing["blocking"] is True
    assert missing["verified_by"] == "scene_card_required_text"

    satisfied = classify_issue(
        {"issue_key": "missing_required_text", "message": "缺少 red envelope 线索"},
        scene=scene,
        content="A red envelope changes hands. 其余正文。",
    )
    assert satisfied["quality_level"] == "Q2"
    assert satisfied["downgraded_from"] == "Q1"


def test_forbidden_text_verified_only_when_term_present() -> None:
    scene = _scene(forbidden_text="青花瓷")
    hit = classify_issue(
        {"issue_key": "forbidden_text", "message": "出现禁用词"},
        scene=scene,
        content="桌上摆着一只青花瓷瓶。",
    )
    assert hit["quality_level"] == "Q1"
    assert hit["verified_by"] == "scene_card_forbidden_term"

    clean = classify_issue(
        {"issue_key": "forbidden_text", "message": "出现禁用词"},
        scene=scene,
        content="桌上什么都没有。",
    )
    assert clean["quality_level"] == "Q2"
    assert clean["downgraded_from"] == "Q1"


def test_the_legacy_reference_policy_sentence_never_verifies_a_forbidden_text_issue() -> None:
    """旧卡的 forbidden_text 是物化写下的防抄袭政策句：正文里出现「人物」不是已证实的硬伤。"""
    scene = _scene(forbidden_text="不得复制参考书原文表达、人物、设定或桥段。")
    issue = classify_issue(
        {"issue_key": "forbidden_text", "message": "出现禁用词：人物"},
        scene=scene,
        content="这号人物他见得多了。",
    )
    assert issue["quality_level"] == "Q2"
    assert issue["downgraded_from"] == "Q1"
    assert issue["blocking"] is False

    # 同一张卡上作者另外写了真正的禁用词：照常是已证实的 Q1
    scene = _scene(forbidden_text="不得复制参考书原文表达、人物、设定或桥段。青花瓷")
    hit = classify_issue(
        {"issue_key": "forbidden_text", "message": "出现禁用词"},
        scene=scene,
        content="这号人物端着一只青花瓷瓶。",
    )
    assert hit["quality_level"] == "Q1"
    assert hit["evidence_spans"] == [{"text": "青花瓷"}]


def test_constraint_conflict_annotation_is_verified_q1() -> None:
    issue = classify_issue(
        {
            "issue_key": "unsupported_event",
            "message": "建议删除旧信线索",
            "conflicts_with": [{"term": "旧信", "constraint_source": "scene_card.must_include_text"}],
        },
        scene=_scene(),
        content="正文",
    )
    assert issue["quality_level"] == "Q1"
    assert issue["verified_by"] == "scene_card_constraint_conflict"


def test_event_log_keyword_violation_from_deterministic_source_is_q1() -> None:
    issue = classify_issue(
        {
            "issue_key": "event_log_consistency_violation",
            "message": "Event log contradiction",
            "source": "deterministic",
            "details": {"entity_id": "CHAR_A", "fact_key": "location"},
        },
        scene=_scene(),
        content="正文",
    )
    assert issue["quality_level"] == "Q1"
    assert issue["blocking"] is True
    assert issue["authority_ref"] == "event:CHAR_A.location"


# ---------- §6.1 契约字段齐备 ----------

def test_classified_issue_carries_contract_fields() -> None:
    issue = classify_issue({"issue_key": "cadence_flat", "message": "m"}, scene=_scene(), content="正文")
    for field in (
        "issue_key",
        "quality_level",
        "blocking",
        "authority_ref",
        "evidence_spans",
        "confidence",
        "recommended_action",
        "source",
        "verified_by",
    ):
        assert field in issue, field
    assert issue["source"] == "llm_advisory"


def test_registry_blocking_levels_always_declare_verifier() -> None:
    for key, policy in ISSUE_KEY_POLICY.items():
        if policy.verified_level in BLOCKING_LEVELS:
            assert policy.verifier_id, f"{key} 声明了 {policy.verified_level} 但没有确定性复核器"
        assert policy.fallback_level not in BLOCKING_LEVELS, f"{key} 的未复核回退级别不得阻断"


# ---------- 集合助手 ----------

def test_helpers_split_blocking_and_warning_sets() -> None:
    scene = _scene()
    classified = classify_issues(
        [
            {"issue_key": "character_pronoun_drift", "message": "drift", "source": "deterministic"},
            {"issue_key": "scene_conflict_missing", "message": "conflict"},
            {"issue_key": "style_compliance", "message": "style"},
        ],
        scene=scene,
        content="正文",
    )
    assert has_blocking(classified) is True
    assert [i["issue_key"] for i in blocking_issues(classified)] == ["character_pronoun_drift"]
