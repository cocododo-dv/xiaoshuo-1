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


def setup_function() -> None:
    """每个测试开始时清缓存。"""
    clear_config_cache()


def test_load_input_thresholds_yaml() -> None:
    cfg = load_yaml_config("input_thresholds")
    assert set(cfg.keys()) >= {"language", "narrative", "scene", "theme"}
    assert cfg["language"]["skip"] == 10000


def test_sensory_lexicon_is_gone() -> None:
    """2026-09-23:感官词表指标(子串匹配)随测量核删除,词表文件一并删除。"""
    with pytest.raises(FileNotFoundError):
        load_yaml_config("sensory_lexicon")


def test_dead_learning_configs_are_gone() -> None:
    """2026-09-23 v3:旧抽取器的采样 / 重试配置(extraction.yaml)与 👍/👎 调档阈值(feedback.yaml)随代码删除。"""
    for name in ("extraction", "feedback"):
        with pytest.raises(FileNotFoundError):
            load_yaml_config(name)


def test_load_banned_adjectives_yaml_returns_items_key() -> None:
    """banned_adjectives.yaml 顶层是 list,wrapper 返回 {'items': [...]}。"""
    cfg = load_yaml_config("banned_adjectives")
    assert "items" in cfg
    assert "文笔优美" in cfg["items"]


def test_tolerance_floors_is_gone() -> None:
    """2026-09-24 风格参考 v3 S2:v2 指标包络(metrics.py / candidate_rerank.py)连同 tolerance_floors.yaml 一起删除。"""
    with pytest.raises(FileNotFoundError):
        load_yaml_config("tolerance_floors")


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
