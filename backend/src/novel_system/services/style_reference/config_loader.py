"""轻量 YAML 配置加载,缓存到内存。

`config/style_reference/<name>.yaml` 都是静态文件,不入 DB(与 SystemConfigSnapshot
机制独立)。本模块用 `@lru_cache` 缓存解析结果,测试可用 `_load_yaml.cache_clear()`
重置。

支持的 name:input_thresholds / banned_adjectives / injection_budget,测量核与声音签名另加
function_words / voice_baseline(感官词表 sensory_lexicon 已随感官指标一起删除,旧抽取器的 extraction.yaml
已随代码删除,2026-09-23;v2 指标包络的 tolerance_floors.yaml 随 metrics.py / candidate_rerank.py 删除,
2026-09-24)。`anti_plagiarism_template.txt` 不是 YAML,用 `load_text_template` 加载。

`load_optional_yaml_config` 用于「缺文件即优雅退化」的配置(如 voice_baseline.yaml):
文件不存在时返回空 dict 而不是抛错。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def _config_dir() -> Path:
    """仓库根 / config / style_reference。

    `__file__` 位于 `backend/src/novel_system/services/style_reference/config_loader.py`,
    parents[5] = 仓库根。
    """
    return Path(__file__).resolve().parents[5] / "config" / "style_reference"


@lru_cache(maxsize=16)
def _load_yaml(name: str) -> Any:
    path = _config_dir() / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"style_reference config not found: {path}"
            f" (expected one of: input_thresholds / banned_adjectives / "
            f"injection_budget / function_words / voice_baseline)"
        )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_yaml_config(name: str) -> dict[str, Any]:
    """加载 `config/style_reference/<name>.yaml` 为 dict。

    yaml 顶层若是 list(如 banned_adjectives.yaml),返回 `{"items": [...]}` 兼容
    dict 接口的调用方。
    """
    data = _load_yaml(name)
    if isinstance(data, list):
        return {"items": data}
    return dict(data or {})


def load_optional_yaml_config(name: str) -> dict[str, Any]:
    """同 `load_yaml_config`,但文件缺失时返回 `{}`(供可选配置优雅退化)。"""
    try:
        return load_yaml_config(name)
    except FileNotFoundError:
        return {}


@lru_cache(maxsize=4)
def load_text_template(name: str) -> str:
    """加载 `config/style_reference/<name>.txt` 为字符串(供 anti_plagiarism 模板用)。"""
    path = _config_dir() / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"style_reference text template not found: {path}")
    return path.read_text(encoding="utf-8")


def clear_config_cache() -> None:
    """清空所有 yaml 与 text 模板缓存,供测试用。"""
    _load_yaml.cache_clear()
    load_text_template.cache_clear()
