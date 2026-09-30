"""请求体的共用边界：严格模型基类、空命令体、有界 JSON 对象、写作简报输入与标识符别名。"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, WithJsonSchema


# SQLite INTEGER 的上限：修订号、时长、预算这类计数的请求上限
INT64_MAX = (1 << 63) - 1

# 非空、有长度上限的标识符（章 / 场 / 书的 id 列表的元素）
BoundedIdentifier = Annotated[str, Field(min_length=1, max_length=255)]

MAX_API_OBJECT_PROPERTIES = 256
MAX_API_JSON_DEPTH = 24
MAX_API_JSON_NODES = 100_000
MAX_API_JSON_KEY_CHARS = 512
MAX_API_JSON_STRING_CHARS = 4_000_000


class StrictRequestModel(BaseModel):
    """Default boundary for fixed-shape request bodies."""

    model_config = ConfigDict(extra="forbid", strict=True)


class EmptyRequest(StrictRequestModel):
    """Explicitly empty optional body for command-style endpoints."""


def _validate_bounded_json_object(value: dict[str, Any]) -> dict[str, Any]:
    """Reject pathological JSON trees before they reach domain normalizers.

    Some versioned authoring payloads intentionally remain open dictionaries,
    but "open" must not mean unlimited depth, property count, or node count.
    The byte-level middleware provides the first ceiling; this validator adds a
    format-level ceiling and a stable OpenAPI type for those flexible payloads.
    """

    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_API_JSON_NODES:
            raise ValueError(
                f"JSON payload exceeds {MAX_API_JSON_NODES} nodes"
            )
        if depth > MAX_API_JSON_DEPTH:
            raise ValueError(
                f"JSON payload exceeds maximum depth {MAX_API_JSON_DEPTH}"
            )
        if isinstance(current, dict):
            if len(current) > MAX_API_OBJECT_PROPERTIES:
                raise ValueError(
                    "JSON object exceeds maximum property count "
                    f"{MAX_API_OBJECT_PROPERTIES}"
                )
            for key, item in current.items():
                if len(key) > MAX_API_JSON_KEY_CHARS:
                    raise ValueError(
                        f"JSON object key exceeds {MAX_API_JSON_KEY_CHARS} characters"
                    )
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str) and len(current) > MAX_API_JSON_STRING_CHARS:
            raise ValueError(
                f"JSON string exceeds {MAX_API_JSON_STRING_CHARS} characters"
            )
    return value


BoundedJsonObject = Annotated[
    dict[str, Any],
    Field(max_length=MAX_API_OBJECT_PROPERTIES),
    AfterValidator(_validate_bounded_json_object),
]


def _validate_writer_brief_resource_bounds(value: Any) -> Any:
    # Shape/type errors intentionally remain in the shared writer-brief
    # normalizers so clients keep WRITER_BRIEF_INVALID / HTTP 400.  A genuine
    # object still receives the same anti-exhaustion bounds as flexible JSON.
    if isinstance(value, dict):
        return _validate_bounded_json_object(value)
    return value


# Runtime shape validation deliberately remains in the writer-review domain
# normalizers so malformed briefs keep the stable WRITER_BRIEF_INVALID / 400
# contract.  The explicit schema prevents that implementation detail from
# advertising arbitrary JSON to OpenAPI clients.
WriterBriefJsonInput = Annotated[
    Any,
    AfterValidator(_validate_writer_brief_resource_bounds),
    WithJsonSchema(
        {
            "anyOf": [
                {
                    "type": "object",
                    "additionalProperties": True,
                    "maxProperties": MAX_API_OBJECT_PROPERTIES,
                },
                {"type": "null"},
            ]
        }
    ),
]
