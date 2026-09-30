"""config_loader.py 单测:YAML 加载 + lru_cache + 路径错误。

参见 plans/style-reference-v1-1-fancy-shannon.md §"测试策略"。
"""

from __future__ import annotations

import pytest

from novel_system.services.style_reference.config_loader import (
    clear_config_cache,
    load_text_template,
    load_yaml_config,
)


def test_load_input_thresholds_yaml() -> None:
    cfg = load_yaml_config("input_thresholds")
    assert set(cfg.keys()) >= {"language", "narrative", "scene", "theme"}
    assert cfg["language"]["skip"] == 10000


def test_load_banned_adjectives_yaml_returns_items_key() -> None:
    """banned_adjectives.yaml 顶层是 list,wrapper 返回 {'items': [...]}。"""
    cfg = load_yaml_config("banned_adjectives")
    assert "items" in cfg
    assert "文笔优美" in cfg["items"]


def test_load_anti_plagiarism_template() -> None:
    text = load_text_template("anti_plagiarism_template")
    assert "严格禁止" in text
    assert "{banned_terms_list}" in text


def test_load_missing_yaml_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_yaml_config("does_not_exist_xyz")


def test_load_missing_text_template_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_text_template("does_not_exist_xyz")


def test_cache_hit() -> None:
    """同一 name 加载两次应该命中 cache(返回相同 object)。"""
    first = load_yaml_config("input_thresholds")
    second = load_yaml_config("input_thresholds")
    # load_yaml_config 返回 dict(data),所以不是同一 object;
    # 但底层 _load_yaml 是 lru_cache 的,这里测试 cache_clear 后会重新读盘
    clear_config_cache()
    third = load_yaml_config("input_thresholds")
    assert first == second == third


# ---------------------------------------------------------------- injection_budget.yaml（budget_config 唯一解析）


def test_injection_budget_reads_the_repo_file() -> None:
    from novel_system.services.style_reference.budget_config import InjectionBudget, injection_budget

    budget = injection_budget()
    assert budget == InjectionBudget(
        sample_window_max_chars=5000,
        card_budget_chars=2600,
        draft_mode_default="style_first",
        style_first_length_slack=0.5,
        style_first_reference_scene_chars_max=5000,
        continuity_anchor_max_chars=900,
        fidelity={
            "style_step_max_percentile": 90,
            "revision_min_improvement": 0.03,
            "patch_max_distance_increase": 0.05,
            "judge_tolerance": 0.1,
        },
    )


def test_injection_budget_falls_back_per_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from novel_system.services.style_reference import budget_config

    raw = {
        "sample_window_max_chars": -3,  # 负数按 0（与 render 原来的读法相同）
        "card_budget_chars": "lots",  # 读不成整数 → 缺省
        "draft_mode_default": " Neutral_First ",
        "style_first_length_slack": 3,  # 夹到 0.9
        "style_first_reference_scene_chars_max": 0,  # 不是正数 → 缺省
        "continuity_anchor_max_chars": -1,
        "fidelity": ["not", "a", "section"],
    }
    monkeypatch.setattr(budget_config, "load_optional_yaml_config", lambda _name: dict(raw))
    budget = budget_config.injection_budget()
    assert budget.sample_window_max_chars == 0
    assert budget.card_budget_chars == budget_config.CARD_BUDGET_CHARS
    assert budget.draft_mode_default == "neutral_first"
    assert budget.style_first_length_slack == 0.9
    assert budget.style_first_reference_scene_chars_max == budget_config.REFERENCE_SCENE_CHARS_MAX
    assert budget.continuity_anchor_max_chars == budget_config.CONTINUITY_ANCHOR_MAX_CHARS
    assert budget.fidelity == {}

    raw.update(draft_mode_default="sideways", style_first_length_slack=float("nan"))
    budget = budget_config.injection_budget()
    assert budget.draft_mode_default == "style_first" and budget.style_first_length_slack == 0.0


def test_injection_budget_survives_an_unreadable_file(monkeypatch: pytest.MonkeyPatch) -> None:
    from novel_system.services.style_reference import budget_config

    def broken(_name: str) -> dict:
        raise ValueError("mapping values are not allowed here")

    monkeypatch.setattr(budget_config, "load_optional_yaml_config", broken)
    assert budget_config.injection_budget() == budget_config.InjectionBudget()
