"""Privacy-preserving, bounded metadata for durable LLM audit rows.

The LLM ledger is an accounting and lineage surface, not a second manuscript
store.  These helpers deliberately retain hashes, sizes, roles, field names and
bounded operational metadata while excluding prompt, draft and model prose.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


AUDIT_SCHEMA_VERSION = 2
AUDIT_SUMMARY_BYTE_CAP = 16_384
AUDIT_COLLECTION_ITEM_CAP = 32
AUDIT_FIELD_CAP = 64
AUDIT_DEPTH_CAP = 4
AUDIT_IDENTIFIER_CHAR_CAP = 256

_SAFE_ATOM = re.compile(r"^[A-Za-z0-9/][A-Za-z0-9_.:/@+\-]*$")
_SAFE_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
_HASHED_IDENTIFIER = re.compile(r"^sha256:[0-9a-f]{64};chars=[0-9]+$")
_FINGERPRINT_KINDS = frozenset({"text_fingerprint", "json_fingerprint"})

# Values under these keys are controlled protocol/configuration atoms rather
# than author prose.  They are still length- and character-bounded.
_SAFE_LITERAL_KEYS = frozenset(
    {
        "_accounting_provider_execution_mode",
        "accounting_status",
        "api_mode",
        "credential_mode",
        "dispatch_kind",
        "endpoint",
        "error_code",
        "error_type",
        "finish_reason",
        "kind",
        "model",
        "model_profile",
        "next_action",
        "node_id",
        "provider",
        "provider_id",
        "reasoning_level",
        "response_format",
        "route_node",
        "scope_type",
        "status",
        "step",
        "step_key",
        "task_key",
        "task_name",
        "template_name",
        "template_version",
        "top_level_type",
    }
)
_SAFE_LITERAL_SUFFIXES = (
    "_code",
    "_hash",
    "_id",
    "_key",
    "_mode",
    "_status",
    "_type",
    "_version",
)
# response_schema_enriched：雪花请求里按编辑器模板补全了 properties 的成员标签（"characters_items" 一类），
# 是结构标签不是内容——保持可读，「模型为什么只回了空对象」才能在审计里直接看出来。
_SAFE_LITERAL_LIST_KEYS = frozenset({"included_sections", "prompt_budget_applied", "response_schema_enriched"})
# These identifiers originate outside the application trust boundary.  They
# need deterministic correlation, but never a readable ``*_id`` fast path.
_UNTRUSTED_IDENTIFIER_KEYS = frozenset({"provider_request_id", "request_id"})
_CONTENT_KEYS = frozenset(
    {
        "content",
        "details_text",
        "draft",
        "error_text",
        "manuscript",
        "message",
        "messages",
        "outline",
        "outline_text",
        "postprocess_error",
        "prompt",
        "prompt_text",
        "raw_output",
        "response_body",
        "scene_text",
        "source_draft_content",
        "structured_output",
        "system_prompt",
        "text",
        "user_prompt",
    }
)


def text_fingerprint(value: str) -> dict[str, Any]:
    """Return stable evidence about text without retaining any text itself."""

    encoded = value.encode("utf-8")
    return {
        "kind": "text_fingerprint",
        "char_count": len(value),
        "utf8_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def json_fingerprint(value: Any) -> dict[str, Any]:
    """Return a bounded structural fingerprint for a JSON-like value."""

    canonical = _canonical_json(value)
    result: dict[str, Any] = {
        "kind": "json_fingerprint",
        "top_level_type": _json_type_name(value),
        "json_bytes": len(canonical),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }
    if isinstance(value, dict):
        keys = [_safe_field_name(key) for key in list(value)[:AUDIT_COLLECTION_ITEM_CAP]]
        result.update(
            {
                "top_level_item_count": len(value),
                "top_level_fields": keys,
                "fields_omitted": max(0, len(value) - len(keys)),
            }
        )
    elif isinstance(value, (list, tuple)):
        result["top_level_item_count"] = len(value)
    return result


def message_fingerprints(messages: Any) -> dict[str, Any]:
    """Describe prompt messages by role and text fingerprint only."""

    if not isinstance(messages, (list, tuple)):
        return {
            "count": 0,
            "invalid_payload": json_fingerprint(messages),
        }
    items: list[dict[str, Any]] = []
    for message in messages[:AUDIT_COLLECTION_ITEM_CAP]:
        if not isinstance(message, dict):
            items.append({"invalid_message": json_fingerprint(message)})
            continue
        role = bounded_identifier(message.get("role"))
        content = message.get("content")
        item: dict[str, Any] = {
            "role": role or "unknown",
            "content": text_fingerprint(str(content or "")),
        }
        if "name" in message:
            item["name"] = bounded_identifier(message.get("name"))
        items.append(item)
    return {
        "count": len(messages),
        "items": items,
        "items_omitted": max(0, len(messages) - len(items)),
    }


def bounded_identifier(value: Any) -> str | None:
    """Keep a conservative protocol identifier, otherwise retain only its hash."""

    if value is None:
        return None
    text = str(value)
    if _HASHED_IDENTIFIER.fullmatch(text):
        return text
    if (
        len(text) <= AUDIT_IDENTIFIER_CHAR_CAP
        and bool(_SAFE_ATOM.fullmatch(text))
    ):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest};chars={len(text)}"


def fingerprint_identifier(value: Any) -> str | None:
    """Hash an untrusted identifier while preserving deterministic correlation.

    Provider response identifiers are externally controlled strings.  Even a
    value that happens to match the protocol-atom grammar can be an API key,
    manuscript token, or malicious payload, so it must never use the readable
    ``bounded_identifier`` fast path.
    """

    if value is None:
        return None
    text = str(value)
    if _HASHED_IDENTIFIER.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"sha256:{digest};chars={len(text)}"


def audit_error_text(value: Any, *, error_code: Any = None) -> str | None:
    """Create a deterministic, prose-free value for ``llm_call_attempts.error_text``."""

    if value is None:
        return None
    text = str(value)
    code = bounded_identifier(error_code) or "ERROR"
    already_redacted = re.compile(
        rf"^{re.escape(code)};message_sha256=[0-9a-f]{{64}};message_chars=[0-9]+$"
    )
    if already_redacted.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{code};message_sha256={digest};message_chars={len(text)}"


def sanitize_audit_summary(payload: Any) -> dict[str, Any]:
    """Return a content-free JSON object with a strict serialized-size ceiling.

    The sanitizer is intentionally safe by default: arbitrary strings are
    fingerprinted.  Only conservative protocol/configuration atoms survive as
    literals.  Reapplying it is idempotent for fingerprints created here.
    """

    raw_fingerprint = json_fingerprint(payload)
    if isinstance(payload, dict):
        sanitized = _sanitize_dict(payload, depth=0)
    else:
        sanitized = {"value": _sanitize_value(payload, key=None, depth=0)}
    sanitized["_audit_schema_version"] = AUDIT_SCHEMA_VERSION
    if _serialized_size(sanitized) <= AUDIT_SUMMARY_BYTE_CAP:
        return sanitized

    # Preserve the handful of fields used by accounting/replay validation and
    # replace the rest with a whole-payload fingerprint.
    compact: dict[str, Any] = {
        "_audit_schema_version": AUDIT_SCHEMA_VERSION,
        "_audit_compacted": True,
        "payload": raw_fingerprint,
    }
    for key, value in sanitized.items():
        if key.startswith("_audit_"):
            continue
        if not _is_contract_key(key):
            continue
        compact[key] = value
        if len(compact) >= AUDIT_COLLECTION_ITEM_CAP:
            break
    if _serialized_size(compact) <= AUDIT_SUMMARY_BYTE_CAP:
        return compact
    # This final shape is constant-size and cannot exceed the configured cap.
    return {
        "_audit_schema_version": AUDIT_SCHEMA_VERSION,
        "_audit_compacted": True,
        "payload": raw_fingerprint,
    }


def error_audit_summary(exc: Exception, *, promote_attempt_fields: bool = False) -> dict[str, Any]:
    """Summarize an exception for durable audit rows without retaining prose.

    The shared shape for ``response_payload_summary`` on failed calls: the
    message survives only as a fingerprint, ``details`` is sanitized
    field-by-field, and the result is already passed through
    :func:`sanitize_audit_summary` (reapplying the sanitizer stays idempotent).
    ``promote_attempt_fields`` additionally lifts ``attempt_count`` /
    ``max_retries`` out of ``details`` for consumers that must not depend on
    the nested payload shape.
    """

    details = getattr(exc, "details", None)
    details = details if isinstance(details, dict) else {}
    summary: dict[str, Any] = {
        "error_type": exc.__class__.__name__,
        "error_code": getattr(exc, "code", exc.__class__.__name__),
        "message": text_fingerprint(str(exc)),
        "details": details,
        "retryable": bool(getattr(exc, "retryable", False)),
    }
    if promote_attempt_fields:
        if "attempt_count" in details:
            summary["attempt_count"] = details["attempt_count"]
        if "max_retries" in details:
            summary["max_retries"] = details["max_retries"]
    return sanitize_audit_summary(summary)


def _sanitize_dict(payload: dict[Any, Any], *, depth: int) -> dict[str, Any]:
    if _looks_like_fingerprint(payload):
        return _validated_fingerprint(payload)
    if depth >= AUDIT_DEPTH_CAP:
        return json_fingerprint(payload)

    items = list(payload.items())
    # Contract keys win the bounded field budget even for hostile, enormous maps.
    items.sort(key=lambda item: (not _is_contract_key(str(item[0])),))
    selected = items[:AUDIT_FIELD_CAP]
    result: dict[str, Any] = {}
    for raw_key, value in selected:
        key = _safe_field_name(raw_key)
        # Two adversarial keys can collapse to the same hashed field name only at
        # cryptographic probability; keep deterministic last-write behaviour.
        result[key] = _sanitize_value(value, key=str(raw_key), depth=depth + 1)
    omitted = len(items) - len(selected)
    if omitted:
        result["_omitted_field_count"] = omitted
        result["_omitted_payload"] = json_fingerprint(dict(items[AUDIT_FIELD_CAP:]))
    return result


def _sanitize_value(value: Any, *, key: str | None, depth: int) -> Any:
    normalized_key = (key or "").lower()
    if isinstance(value, dict) and _looks_like_fingerprint(value):
        return _validated_fingerprint(value)
    if normalized_key in _UNTRUSTED_IDENTIFIER_KEYS:
        return fingerprint_identifier(value)
    if normalized_key == "messages":
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return _validated_message_summary(value)
        return message_fingerprints(value)
    if normalized_key in _CONTENT_KEYS:
        return text_fingerprint(value) if isinstance(value, str) else json_fingerprint(value)
    if isinstance(value, str):
        if _is_safe_literal_key(normalized_key):
            return bounded_identifier(value)
        return text_fingerprint(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return _sanitize_dict(value, depth=depth)
    if isinstance(value, (list, tuple)):
        if depth >= AUDIT_DEPTH_CAP:
            return json_fingerprint(value)
        selected = list(value[:AUDIT_COLLECTION_ITEM_CAP])
        result = [
            _sanitize_value(
                item,
                key=normalized_key if normalized_key in _SAFE_LITERAL_LIST_KEYS else None,
                depth=depth + 1,
            )
            for item in selected
        ]
        if len(value) > len(selected):
            result.append(
                {
                    "_items_omitted": len(value) - len(selected),
                    "_omitted_payload": json_fingerprint(value[len(selected):]),
                }
            )
        return result
    return json_fingerprint(value)


def _looks_like_fingerprint(payload: dict[Any, Any]) -> bool:
    return payload.get("kind") in _FINGERPRINT_KINDS and isinstance(payload.get("sha256"), str)


def _validated_message_summary(payload: dict[Any, Any]) -> dict[str, Any]:
    raw_items = payload.get("items")
    assert isinstance(raw_items, list)
    items: list[dict[str, Any]] = []
    for raw_item in raw_items[:AUDIT_COLLECTION_ITEM_CAP]:
        if not isinstance(raw_item, dict):
            items.append({"invalid_message": json_fingerprint(raw_item)})
            continue
        content = raw_item.get("content")
        item: dict[str, Any] = {
            "role": bounded_identifier(raw_item.get("role")) or "unknown",
            "content": (
                _validated_fingerprint(content)
                if isinstance(content, dict) and _looks_like_fingerprint(content)
                else text_fingerprint(str(content or ""))
            ),
        }
        if raw_item.get("name") is not None:
            item["name"] = bounded_identifier(raw_item.get("name"))
        items.append(item)
    count = payload.get("count")
    count = count if isinstance(count, int) and count >= 0 else len(items)
    return {
        "count": count,
        "items": items,
        "items_omitted": max(0, count - len(items)),
    }


def _validated_fingerprint(payload: dict[Any, Any]) -> dict[str, Any]:
    kind = str(payload.get("kind"))
    result: dict[str, Any] = {
        "kind": kind,
        "sha256": bounded_identifier(payload.get("sha256")),
    }
    allowed_numeric = (
        "char_count",
        "utf8_bytes",
        "json_bytes",
        "top_level_item_count",
        "fields_omitted",
    )
    for key in allowed_numeric:
        value = payload.get(key)
        if isinstance(value, int) and value >= 0:
            result[key] = value
    if kind == "json_fingerprint":
        result["top_level_type"] = bounded_identifier(payload.get("top_level_type")) or "unknown"
        fields = payload.get("top_level_fields")
        if isinstance(fields, list):
            result["top_level_fields"] = [
                _safe_field_name(field)
                for field in fields[:AUDIT_COLLECTION_ITEM_CAP]
            ]
    return result


def _is_safe_literal_key(key: str) -> bool:
    return (
        key in _SAFE_LITERAL_KEYS
        or key in _SAFE_LITERAL_LIST_KEYS
        or key.endswith(_SAFE_LITERAL_SUFFIXES)
    )


def _is_contract_key(key: str) -> bool:
    lowered = key.lower()
    if _is_safe_literal_key(lowered):
        return True
    return lowered.endswith(
        (
            "_count",
            "_tokens",
            "_chars",
            "_bytes",
            "_present",
            "_retryable",
        )
    ) or lowered in {
        "attempt_count",
        "max_retries",
        "retryable",
        "token_budget",
        "continuity_warning",
    }


def _safe_field_name(value: Any) -> str:
    text = str(value)
    if len(text) <= 64 and _SAFE_FIELD.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return f"field_sha256_{digest}"


def _canonical_json(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
    except (TypeError, ValueError):
        rendered = json.dumps(
            {"type": type(value).__name__},
            sort_keys=True,
            separators=(",", ":"),
        )
    return rendered.encode("utf-8")


def _json_default(value: Any) -> dict[str, str]:
    # Never serialize repr/str here: custom objects may embed secrets in either.
    return {"unsupported_type": type(value).__name__}


def _json_type_name(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return "unsupported"


def _serialized_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
