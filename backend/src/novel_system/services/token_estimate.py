"""提示词 token 估算（叶子：只依赖 ``hash_engine`` 的字符串归一化）。

所有输入预算用同一把尺：场景 / 规划 / 章节提示词的分段降载（:mod:`.context_budget`）、雪花工作台的 JSON 降载
阶梯（:mod:`.snowflake_prompt_budget`）、风格样例装箱（``style_reference.inject.fit``）与记账的预留上限
（:mod:`.llm_accounting`）。它是预算上的保守上界，不是哪家 provider 的真实分词；调用结束后以 provider 报的 usage
记账。``TOKEN_ESTIMATOR_VERSION`` 写进每次请求的 ``token_budget`` 审计，改算法就要改版本号。
"""

from __future__ import annotations

import math
import unicodedata

from novel_system.services.hash_engine import normalize_string

TOKEN_ESTIMATOR_VERSION = "cjk_aware_conservative_v1"


def estimate_tokens(text: str) -> int:
    """Return a conservative, deterministic prompt-token estimate.

    The previous ``len(text) / 4`` rule is a reasonable rough estimate for
    English, but it under-counts Chinese/Japanese/Korean text by roughly four
    times.  East-Asian wide characters (including CJK punctuation and most
    emoji) are therefore charged as one token each, while the remaining text
    keeps the established four-characters-per-token approximation.

    This is deliberately a budgeting upper bound rather than a claim about a
    provider's exact tokenizer.  Provider-reported usage remains authoritative
    for accounting after the request completes.
    """
    normalized_text = normalize_string(text)
    if not normalized_text:
        return 0
    wide_count = sum(1 for char in normalized_text if is_wide_token_char(char))
    compact_count = len(normalized_text) - wide_count
    return max(1, wide_count + math.ceil(compact_count / 4))


def is_wide_token_char(char: str) -> bool:
    """Whether a character should be budgeted as an approximately whole token."""
    if not char or char.isspace():
        return False
    return unicodedata.east_asian_width(char) in {"W", "F"}
