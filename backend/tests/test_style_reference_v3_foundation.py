"""风格参考 v3 地基：绑定配置的唯一解释、文风卡结构与渲染、StylePolicy。"""

from __future__ import annotations

from novel_system.services.style_policy import (
    MODE_ABSENT,
    MODE_DEGRADED,
    MODE_FROZEN,
    UNBOUND,
    policy_from_contract,
    reset_style_policy_cache,
    style_policy_for_bundle,
)
from novel_system.services.style_reference.binding_config import (
    ALL_DIMENSIONS,
    DEFAULT_SAMPLE_WINDOWS,
    REFERENCE_MODE_CARD_ONLY,
    REFERENCE_MODE_FULL,
    effective_reference_mode,
    normalize_binding_config,
)
from novel_system.services.style_reference.card import (
    DIMENSION_LABELS,
    CardLine,
    DimensionCard,
    DimensionEntry,
    LINE_STATE_EXCLUDED,
    LINE_STATE_PINNED,
    line_id_for,
    normalize_card,
    render_card_block,
)


def test_missing_binding_keys_take_v3_defaults_and_legacy_keys_are_ignored() -> None:
    """2026-09-24：旧键（strategy / intensity / sub_dimensions / include_*）由迁移 0092 一次回填，代码不再解释——
    缺键取默认（full / 12 窗 / 全 normal / style_first），旧键与未知键丢弃。"""
    defaults = normalize_binding_config({})
    assert defaults["reference_mode"] == REFERENCE_MODE_FULL and defaults["sample_windows"] == DEFAULT_SAMPLE_WINDOWS
    assert set(defaults["dimension_states"].values()) == {"normal"} and defaults["draft_mode"] == "style_first"
    assert normalize_binding_config(None) == defaults
    stale = normalize_binding_config({"intensity": 50, "sub_dimensions": ["language.vocabulary"], "include_metric": True})
    assert stale == defaults
    assert set(normalize_binding_config({"reference_mode": "card_only", "bogus": 1})) == {
        "reference_mode", "sample_windows", "dimension_states", "draft_mode"
    }
    assert normalize_binding_config({"reference_mode": "card_only"})["reference_mode"] == REFERENCE_MODE_CARD_ONLY


def test_v3_binding_keys_win_and_are_clamped() -> None:
    config = normalize_binding_config(
        {
            "reference_mode": "samples_only",
            "sample_windows": 99,
            "dimension_states": {"language.rhetoric": "emphasize", "theme.values": "exclude", "bogus": "exclude", "scene.dialogue": "?"},
            "draft_mode": "neutral_first",
        },
    )
    assert config["reference_mode"] == "samples_only"
    assert config["sample_windows"] == 16
    assert config["dimension_states"]["language.rhetoric"] == "emphasize"
    assert config["dimension_states"]["theme.values"] == "exclude"
    assert config["dimension_states"]["scene.dialogue"] == "normal"
    assert set(config["dimension_states"]) == set(ALL_DIMENSIONS)
    assert config["draft_mode"] == "neutral_first"


def test_segments_only_books_never_send_windows() -> None:
    assert effective_reference_mode("full", cloud_policy="segments_only") == REFERENCE_MODE_CARD_ONLY
    assert effective_reference_mode("samples_only", cloud_policy="segments_only") == REFERENCE_MODE_CARD_ONLY
    assert effective_reference_mode("full", cloud_policy="allow_full_cloud") == REFERENCE_MODE_FULL


def _card() -> DimensionCard:
    return DimensionCard(
        dimensions=[
            DimensionEntry(
                dimension="language.rhetoric",
                distinctiveness=0.9,
                lines=[
                    CardLine(text="紧张处拿日常小物件打夸张的比方，比如把对峙说成排队买奶茶", mandatory=True, distinctiveness=0.9),
                    CardLine(text="借游戏、电影的套路打比方", distinctiveness=0.8),
                    CardLine(text="比喻多，一段里常有两三个", distinctiveness=0.3),
                    CardLine(text="不用让天气替人伤心的拟人", kind="avoid"),
                ],
            ),
            DimensionEntry(
                dimension="theme.values",
                distinctiveness=0.2,
                lines=[CardLine(text="平凡日子的温情压过宏大使命")],
            ),
            DimensionEntry(dimension="unknown.dimension", lines=[CardLine(text="丢掉")]),
        ],
        temperament=["危急关头用自嘲和吐槽冲淡严肃"],
    )


def test_normalize_card_fills_all_16_dims_labels_and_stable_line_ids() -> None:
    card = normalize_card(_card())
    assert [entry.dimension for entry in card.dimensions][0] == "language.rhetoric"
    assert {entry.dimension for entry in card.dimensions} == set(ALL_DIMENSIONS)
    rhetoric = card.entry("language.rhetoric")
    assert rhetoric.label == DIMENSION_LABELS["language.rhetoric"]
    assert rhetoric.lines[0].line_id == line_id_for("language.rhetoric", rhetoric.lines[0].text)
    assert card.entry("unknown.dimension") is None


def test_render_orders_temperament_emphasis_and_respects_states() -> None:
    card = normalize_card(_card())
    text = render_card_block(card)
    assert text.startswith("[文风卡]")
    lines = text.splitlines()
    assert lines[1].startswith("气质（必须）：危急关头用自嘲和吐槽")
    assert "（必须）紧张处拿日常小物件" in text
    # 正常维最多两条 do：第三条（辨识度最低）不出现
    assert "一段里常有两三个" not in text
    assert "[作者不这么写]" in text and "不用让天气替人伤心的拟人" in text
    # 重点维排在最前、多一条；不学的维整维消失
    emphasized = render_card_block(
        card,
        dimension_states={"theme.values": "emphasize", "language.rhetoric": "exclude"},
    )
    assert "【重点】价值取向" in emphasized.splitlines()[2]
    assert "修辞手法" not in emphasized
    # ✗ 的句不再用；✓ 的句永远带上（哪怕超出每维条数）
    rhetoric = card.entry("language.rhetoric")
    excluded = render_card_block(card, line_states={rhetoric.lines[0].line_id: LINE_STATE_EXCLUDED})
    assert "紧张处拿日常小物件" not in excluded
    pinned = render_card_block(card, line_states={rhetoric.lines[2].line_id: LINE_STATE_PINNED})
    assert "一段里常有两三个" in pinned


def test_render_budget_cuts_whole_lines_and_drops_empty_section_titles() -> None:
    card = normalize_card(_card())
    tiny = render_card_block(card, budget_chars=120, recent_gaps=["句末语气词太少"])
    assert all(not line.endswith("，") for line in tiny.splitlines())
    assert "[作者不这么写]" not in tiny or tiny.splitlines()[-1] != "[作者不这么写]"
    assert render_card_block(None) == ""


def _contract(**overrides) -> dict:
    layer = {
        "order": 0,
        "binding": {
            "binding_id": "sr_bind_x",
            "profile_id": "sr_profile_x",
            "scope": "project",
            "scope_ref_id": "PRJ",
            "task_type": "scene_generation",
            "strategy": "mixed",
            "status": "active",
            "config_json": {"intensity": 100, "dimension_states": {"scene.dialogue": "emphasize"}},
        },
        "profile": {"profile_id": "sr_profile_x", "book_id": "sr_book_x"},
        "book": {"book_id": "sr_book_x", "cloud_policy": "allow_full_cloud"},
    }
    contract = {"layers": [layer], "draft_mode": "style_first", "contract_hash": "h" * 64}
    contract.update(overrides)
    return contract


def test_policy_from_contract_reads_the_most_specific_layer() -> None:
    policy = policy_from_contract(_contract(), mode=MODE_FROZEN)
    assert policy.bound and policy.style_first and policy.defers_house_taste()
    assert policy.profile_id == "sr_profile_x" and policy.binding_id == "sr_bind_x" and policy.book_id == "sr_book_x"
    assert policy.reference_mode == "full" and policy.sample_windows == 12
    assert policy.dimension_states["scene.dialogue"] == "emphasize"
    assert policy.sends_samples and policy.sends_card
    neutral = policy_from_contract(_contract(draft_mode="neutral_first"), mode=MODE_FROZEN)
    assert neutral.bound and not neutral.style_first and not neutral.defers_house_taste()
    legacy = policy_from_contract(_contract(draft_mode=None), mode=MODE_FROZEN)
    assert not legacy.style_first  # v1 契约缺键 → 先中性（与旧 effective_draft_mode 一致）


def test_policy_for_bundle_states() -> None:
    reset_style_policy_cache()
    assert style_policy_for_bundle(None) == UNBOUND
    absent = style_policy_for_bundle(
        {"source_version_refs": {"style_reference_runtime_contract_status": "absent"}, "inline_digests": {}}
    )
    assert absent.mode == MODE_ABSENT and not absent.bound
    broken = style_policy_for_bundle(
        {
            "source_version_refs": {"style_reference_runtime_contract_status": "frozen"},
            "inline_digests": {"_style_reference_runtime_contract": "{not json"},
        }
    )
    assert broken.mode == MODE_DEGRADED and not broken.bound and broken.error_code
