"""故事记忆两个 LLM 节点（章摘要 ``chapter_digest``、编辑通读 ``editorial_review``）共用的两件小事：取模板、读回 JSON。"""

from __future__ import annotations

import json
from typing import Any


def load_template(name: str) -> Any:
    """``config/prompts.yaml`` 里的模板；缺了就是部署错误（``LookupError``，不翻成作者能处理的错）。"""
    from novel_system.services.prompt_builder import load_prompt_templates

    template = load_prompt_templates().get(name)
    if template is None:
        raise LookupError(f"prompt template {name!r} is missing from config/prompts.yaml")
    return template


def parse_json_object(response: Any) -> dict[str, Any] | None:
    """响应里的 JSON 对象：优先结构化输出，其次正文（去掉 ``` 围栏）；不是对象 → ``None``。"""
    structured = getattr(response, "structured_output", None)
    if isinstance(structured, dict) and structured:
        return structured
    raw = str(getattr(response, "text", "") or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.endswith("```"):
            raw = raw[:-3]
    try:
        parsed = json.loads(raw.strip())
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
