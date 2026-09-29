"""供应商响应里的用量字段：取出、判完整、归一（叶子模块）。

客户端（组 ``LLMResponse``）和记账层（结算物理尝试）以前各抄一份同样的四组键名与整数校验；现在都从这里取。
四种写法：Responses 的 ``input/output/total_tokens``、Chat 的 ``prompt/completion/total_tokens``、
Gemini 的 ``usageMetadata``（``promptTokenCount`` …）、Ollama 顶层的 ``prompt_eval_count`` / ``eval_count``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from novel_system.services.value_coercion import usage_int

# (输入键, 输出键, 合计键)；Ollama 没有合计键。按顺序认第一组出现了输入或输出键的。
_USAGE_KEY_SETS: tuple[tuple[str, str, str | None], ...] = (
    ("input_tokens", "output_tokens", "total_tokens"),
    ("prompt_tokens", "completion_tokens", "total_tokens"),
    ("promptTokenCount", "candidatesTokenCount", "totalTokenCount"),
    ("prompt_eval_count", "eval_count", None),
)
_OLLAMA_KEYS = ("prompt_eval_count", "eval_count")
# 各家「输出 token 数」的键：OpenAI completion_tokens / Responses output_tokens / Gemini candidatesTokenCount / Ollama eval_count
_OUTPUT_TOKEN_KEYS = ("completion_tokens", "output_tokens", "candidatesTokenCount", "eval_count")


@dataclass(frozen=True, slots=True)
class NormalizedUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    usage_is_estimate: bool


def extract_raw_usage(body: Any) -> dict[str, Any] | None:
    """响应体里的原始用量对象（复制一份）；没有 → ``None``。"""
    if not isinstance(body, dict):
        return None
    for key in ("usage", "usageMetadata"):
        value = body.get(key)
        if isinstance(value, dict):
            return dict(value)
    if any(key in body for key in _OLLAMA_KEYS):
        return {key: body.get(key) for key in _OLLAMA_KEYS if key in body}
    return None


def normalize_raw_usage(raw_usage: dict[str, Any] | None) -> NormalizedUsage | None:
    """供应商实报的用量（``usage_is_estimate=False``）；输入 / 输出数缺失或不是非负整数、合计对不上 → ``None``。"""
    if raw_usage is None:
        return None
    for prompt_key, completion_key, total_key in _USAGE_KEY_SETS:
        if prompt_key not in raw_usage and completion_key not in raw_usage:
            continue
        prompt = usage_int(raw_usage.get(prompt_key))
        completion = usage_int(raw_usage.get(completion_key))
        if prompt is None or completion is None:
            return None
        expected_total = prompt + completion
        if total_key is not None and total_key in raw_usage:
            total = usage_int(raw_usage.get(total_key))
            if total is None or total != expected_total:
                return None
        return NormalizedUsage(prompt, completion, expected_total, False)
    return None


def raw_usage_is_complete(raw_usage: dict[str, Any] | None) -> bool:
    return normalize_raw_usage(raw_usage) is not None


def usage_output_tokens(raw_usage: dict[str, Any] | None) -> int | None:
    """各家用法里的「输出 token 数」；取不到返 ``None``。"""
    if not isinstance(raw_usage, dict):
        return None
    for key in _OUTPUT_TOKEN_KEYS:
        number = usage_int(raw_usage.get(key))
        if number is not None:
            return number
    return None


def _safe_usage_int(value: Any) -> int:
    parsed = usage_int(value)
    return parsed if parsed is not None else 0


def wire_usage(usage: Any) -> dict[str, int]:
    """``LLMResponse.usage`` 的宽松形状 ``{input_tokens, output_tokens, total_tokens}``：缺的、坏的记 0。

    与 :func:`normalize_raw_usage` 不同，这里从不返回 ``None``：适配器自己没给出用量时，响应上仍要有这三个数。
    """
    if not isinstance(usage, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    if "input_tokens" in usage or "output_tokens" in usage:
        input_tokens = _safe_usage_int(usage.get("input_tokens", 0))
        output_tokens = _safe_usage_int(usage.get("output_tokens", 0))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": _safe_usage_int(usage.get("total_tokens", input_tokens + output_tokens)),
        }

    if "promptTokenCount" in usage or "candidatesTokenCount" in usage:
        input_tokens = _safe_usage_int(usage.get("promptTokenCount", 0))
        output_tokens = _safe_usage_int(usage.get("candidatesTokenCount", 0))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": _safe_usage_int(usage.get("totalTokenCount", input_tokens + output_tokens)),
        }

    if "prompt_eval_count" in usage or "eval_count" in usage:
        input_tokens = _safe_usage_int(usage.get("prompt_eval_count", 0))
        output_tokens = _safe_usage_int(usage.get("eval_count", 0))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    return {
        "input_tokens": _safe_usage_int(usage.get("prompt_tokens", 0)),
        "output_tokens": _safe_usage_int(usage.get("completion_tokens", 0)),
        "total_tokens": _safe_usage_int(usage.get("total_tokens", 0)),
    }


__all__ = [
    "NormalizedUsage",
    "extract_raw_usage",
    "normalize_raw_usage",
    "raw_usage_is_complete",
    "usage_output_tokens",
    "wire_usage",
]
