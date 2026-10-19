"""风格参考 — 按场面的写法（画像 ``profile_json["situation_card"]``，2026-10-19；叶子模块）。

文风卡是全书一张；同一位作者写打斗、写日常、写回忆时手法不一样，压成一张卡会把这些差别抹平。通读全书后，学习作业
（``learn_situations``）按窗口的场面标签找出依据集中落在某类场面里的发现，每类常见场面写 2–4 句「写这类场面时
他尤其这样写」，存成：

``{"version", "situations": [{"situation": 场面, "windows": 几窗, "lines": [{"line_id", "text", "finding_ids",
"evidence_quote_ids", "source"}]}]}``

起草时这一场的场面标签（蓝图给的，或由场景设计推）对上了，就把那几句放进文风卡块的「这一场的场面」一节
（``card.plan_card_block``，与近期常见偏差同一档，排在各维的第一句之前——它是专对这一场的提醒）。本模块只管读：
形状校验、按场面挑句。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SITUATION_CARD_VERSION = "situation_card_v1"
SITUATION_CARD_KEY = "situation_card"
# 起草时一场至多带几类场面、每类几句
RENDER_MAX_SITUATIONS = 2
RENDER_MAX_LINES = 3


class SituationLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    line_id: str
    text: str = Field(min_length=1)
    finding_ids: list[str] = Field(default_factory=list)
    evidence_quote_ids: list[str] = Field(default_factory=list)
    source: str = "synthesis"


class SituationEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    situation: str
    windows: int = 0
    lines: list[SituationLine] = Field(default_factory=list)


def situation_entries(profile_json: Mapping[str, Any] | None) -> list[SituationEntry]:
    """画像里的按场面写法（形状不对的条目跳过；没有 → 空）。"""
    raw = (profile_json or {}).get(SITUATION_CARD_KEY) if isinstance(profile_json, Mapping) else None
    if not isinstance(raw, Mapping):
        return []
    out: list[SituationEntry] = []
    for item in raw.get("situations") or []:
        if not isinstance(item, Mapping):
            continue
        try:
            entry = SituationEntry.model_validate(item)
        except Exception:  # noqa: BLE001 — 坏条目跳过
            continue
        if entry.lines:
            out.append(entry)
    return out


def situation_lines_for(
    profile_json: Mapping[str, Any] | None,
    situation_tags: Sequence[str],
    *,
    max_situations: int = RENDER_MAX_SITUATIONS,
    max_lines: int = RENDER_MAX_LINES,
) -> list[tuple[str, str]]:
    """这一场的场面标签（按标签顺序）→ ``[(场面, 句子), …]``：至多 ``max_situations`` 类、每类前 ``max_lines`` 句。"""
    entries = {entry.situation: entry for entry in situation_entries(profile_json)}
    out: list[tuple[str, str]] = []
    used = 0
    for tag in dict.fromkeys(str(t) for t in situation_tags if str(t or "").strip()):
        entry = entries.get(tag)
        if entry is None:
            continue
        out.extend((tag, line.text) for line in entry.lines[:max_lines])
        used += 1
        if used >= max_situations:
            break
    return out


def previous_situation_lines(profile_json: Mapping[str, Any] | None) -> dict[str, dict[str, str]]:
    """上一版的按场面写法：``{场面: {line_id: 句子}}``（重新学习时在上一版上修订）。"""
    return {entry.situation: {line.line_id: line.text for line in entry.lines} for entry in situation_entries(profile_json)}


__all__ = [
    "RENDER_MAX_LINES",
    "RENDER_MAX_SITUATIONS",
    "SITUATION_CARD_KEY",
    "SITUATION_CARD_VERSION",
    "SituationEntry",
    "SituationLine",
    "previous_situation_lines",
    "situation_entries",
    "situation_lines_for",
]
