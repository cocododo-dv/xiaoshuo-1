"""请求体里的正文输入校验（B04-32：原来的 services/text_validation.py——它是写入口的检查，不是质量闸门）。

放在 services 下，路由与接手路由逻辑的服务（v1 建章 / 建场）都能引，服务层不必反过来依赖 api。

- ``validate_user_text_payload``：粘贴进来的文字像是乱码或没解码（U+FFFD、``???`` 占位、C1 控制符、被当成
  Latin-1 读的 UTF-8）→ 400 ``TEXT_ENCODING_INVALID``；
- ``clean_backfill_markers``：旧的 ``{{backfill …}}`` 占位只留下可见文字（章节运行补写早删了，E2E 夹具里还种着）。
"""

from __future__ import annotations

import re
from typing import Any

from novel_system.services.errors import DomainError


REPLACEMENT_CHAR = "\ufffd"
C1_CONTROL_RE = re.compile(r"[\u0080-\u009f]")
MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "å", "ç", "é")


def validate_user_text_payload(payload: Any, *, field_prefix: str = "payload") -> None:
    invalid = _first_invalid_text(payload, field_prefix)
    if invalid is None:
        return
    field, reason = invalid
    raise DomainError(
        "TEXT_ENCODING_INVALID",
        "input text appears corrupted or undecoded; please paste valid UTF-8/GB18030 text",
        status_code=400,
        details={"field": field, "reason": reason},
    )


def _first_invalid_text(value: Any, field: str) -> tuple[str, str] | None:
    if isinstance(value, dict):
        for key, item in value.items():
            invalid = _first_invalid_text(item, f"{field}.{key}")
            if invalid is not None:
                return invalid
        return None
    if isinstance(value, list):
        for index, item in enumerate(value):
            invalid = _first_invalid_text(item, f"{field}[{index}]")
            if invalid is not None:
                return invalid
        return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if not text:
        return None
    if REPLACEMENT_CHAR in text:
        return field, "replacement_character"
    if "???" in text:
        return field, "question_mark_placeholder"
    if C1_CONTROL_RE.search(text):
        return field, "mojibake_control_character"
    if _looks_like_latin1_utf8_mojibake(text):
        return field, "mojibake_marker"
    return None


def _looks_like_latin1_utf8_mojibake(text: str) -> bool:
    if any(marker in text for marker in ("http://", "https://")):
        return False
    marker_count = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    if marker_count < 3:
        return False
    ascii_letters = sum(1 for char in text if "A" <= char <= "z")
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return cjk == 0 and marker_count >= max(3, ascii_letters // 8)


BACKFILL_MARKER_RE = re.compile(
    r'{{backfill\s+id=(?P<marker_id>[^\s}]+)\s+text="(?P<marker_text>[^"]+)"\s*}}'
)


def clean_backfill_markers(text: str | None) -> str | None:
    """Strip legacy ``{{backfill ...}}`` placeholders down to their visible text."""
    if text is None:
        return None
    return BACKFILL_MARKER_RE.sub(lambda match: match.group("marker_text"), text)
