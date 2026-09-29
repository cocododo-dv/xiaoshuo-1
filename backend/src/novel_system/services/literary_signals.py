"""规则文学分析（``literary_quality.analyze_literary_quality``，不带校准）的进程内小缓存。

一次场景运行里同一份稿子会被分析好几遍：风格稿的去模板门、去模板改写的验收、自动批判的规则一遍、LLM 批判里
又一遍——每遍几十毫秒（三千多字约 60 ms）。这里按正文记最近 64 份；返回深拷贝，调用方改了结果也不会污染缓存。
带参考书校准或外部信号的分析不走这里。
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Any

from novel_system.cache_registry import register_cache_reset
from novel_system.services.literary_quality import analyze_literary_quality

_CACHE_SIZE = 64


@lru_cache(maxsize=_CACHE_SIZE)
def _analysis(text: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    return analyze_literary_quality(text)


def rule_analysis(text: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """``analyze_literary_quality(text)`` 的 ``(signals, findings)``：同一份正文只算一次。"""
    signals, findings = _analysis(text)
    return deepcopy(signals), deepcopy(findings)


register_cache_reset("literary_signals.rule_analysis", _analysis.cache_clear)
