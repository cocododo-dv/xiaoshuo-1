"""空泛形容词词表(``config/style_reference/banned_adjectives.yaml``)。

学习作业核对抽取结果时,陈述里出现「文笔优美」「画面感强」这类空泛评价的发现直接丢掉(``learn_extract``)。
"""

from __future__ import annotations

from novel_system.services.style_reference.config_loader import load_yaml_config


def _banned_terms() -> list[str]:
    """从 `config/style_reference/banned_adjectives.yaml` 读词表;list[str]。"""
    cfg = load_yaml_config("banned_adjectives")
    items = cfg.get("items", [])
    return [str(term) for term in items]


def check_banned_adjectives(statement: str) -> list[str]:
    """返回 statement 中命中的禁用词列表(空 list 表示通过)。"""
    matched: list[str] = []
    for term in _banned_terms():
        if term and term in statement:
            matched.append(term)
    return matched
