"""风格参考 v3 — 文风卡（dimension card）：16 维各自「代替模型默认写法，这位作者这样写」。

文风卡是画像的核心显式表示（``profile_json["dimension_card"]``）：
- 由学习作业的合成步写出（每维 1–3 条可执行句，允许 ≤11 字的作者原话作例子，带证据引文 id；外加
  「作者不这么写」的 avoid 句、这一维的一句概括、通用模型在这一维的默认写法、选窗用的手法名）；
- 由注入渲染成 ``[文风卡]`` 块（气质与必须体现在前，按作品的维度状态排序 / 取舍，预算内整行截断）；
- 由读数与定向修改按维使用（``measurable_features``：这一维可以用哪些测量核特征确定性地测）。

作者在矩阵里对某一句的 ✓ / ✗ 记在 ``profile_json["card_line_states"]``（``pinned`` 永远带上 / ``excluded``
不再用），不改卡本身、不让画像失效；作品层面的「重点 / 正常 / 不学」在绑定配置的 ``dimension_states``。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from novel_system.services.style_reference.binding_config import (
    ALL_DIMENSIONS,
    DIMENSION_EMPHASIZE,
    DIMENSION_EXCLUDE,
    normalize_dimension_states,
)

DIMENSION_CARD_VERSION = "dimension_card_v1"
PROFILE_VERSION_V3 = "style_profile_v3"

# 与前端 ws-styleref-model.js 的 SR_LAYERS 同名（一张标签表，别处不再各写一份）
DIMENSION_LABELS: dict[str, str] = {
    "language.sentence_structure": "句式结构",
    "language.vocabulary": "词汇选择",
    "language.rhetoric": "修辞手法",
    "language.punctuation": "标点节奏",
    "narrative.perspective": "叙事视角",
    "narrative.pacing": "节奏控制",
    "narrative.time_handling": "时间处理",
    "narrative.information_density": "信息密度",
    "scene.environment": "环境描写",
    "scene.character_portrayal": "人物刻画",
    "scene.dialogue": "对话写法",
    "scene.sensory_priority": "感官优先",
    "theme.emotional_tone": "情感基调",
    "theme.values": "价值取向",
    "theme.motifs": "母题意象",
    "theme.narrative_philosophy": "叙事哲学",
}
LINE_STATE_PINNED = "pinned"
LINE_STATE_EXCLUDED = "excluded"
LINE_STATES = (LINE_STATE_PINNED, LINE_STATE_EXCLUDED)

CARD_LINE_MAX_CHARS = 90
DEFAULT_CARD_BUDGET_CHARS = 2600
_LINES_PER_DIMENSION = {DIMENSION_EMPHASIZE: 3, "normal": 2}


class CardLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    line_id: str = ""
    text: str = Field(min_length=1)
    kind: Literal["do", "avoid"] = "do"
    source: str = "synthesis"
    evidence_quote_ids: list[str] = Field(default_factory=list)
    # 这一句依据的抽取发现（矩阵据此把卡片行连到发现与证据；P3 学习作业写）
    finding_ids: list[str] = Field(default_factory=list)
    distinctiveness: float = 0.5
    mandatory: bool = False


class DimensionEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    dimension: str
    label: str = ""
    summary: str = ""
    model_default: str = ""
    lines: list[CardLine] = Field(default_factory=list)
    devices: list[str] = Field(default_factory=list)
    measurable_features: list[str] = Field(default_factory=list)
    distinctiveness: float = 0.5


class DimensionCard(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str = DIMENSION_CARD_VERSION
    dimensions: list[DimensionEntry] = Field(default_factory=list)
    temperament: list[str] = Field(default_factory=list)
    generated_at: str = ""

    def entry(self, dimension: str) -> DimensionEntry | None:
        for item in self.dimensions:
            if item.dimension == dimension:
                return item
        return None

    def all_lines(self) -> list[tuple[str, CardLine]]:
        return [(entry.dimension, line) for entry in self.dimensions for line in entry.lines]


def line_id_for(dimension: str, text: str) -> str:
    digest = hashlib.sha256(f"{dimension}\x1f{text.strip()}".encode("utf-8")).hexdigest()
    return f"cl_{digest[:12]}"


def normalize_card(raw: Mapping[str, Any] | DimensionCard | None) -> DimensionCard | None:
    """任意来源 → 规范的 16 维卡（补 label / line_id、截断超长句、按辨识度排序、未知维丢弃）。"""
    if raw is None:
        return None
    card = raw if isinstance(raw, DimensionCard) else DimensionCard.model_validate(dict(raw))
    by_dim: dict[str, DimensionEntry] = {}
    for entry in card.dimensions:
        if entry.dimension not in DIMENSION_LABELS or entry.dimension in by_dim:
            continue
        seen: set[str] = set()
        lines: list[CardLine] = []
        for line in entry.lines:
            text = " ".join(str(line.text or "").split())[:CARD_LINE_MAX_CHARS].strip()
            if not text or text in seen:
                continue
            seen.add(text)
            lines.append(
                line.model_copy(
                    update={
                        "text": text,
                        "line_id": line.line_id or line_id_for(entry.dimension, text),
                        "distinctiveness": max(0.0, min(1.0, float(line.distinctiveness))),
                    }
                )
            )
        by_dim[entry.dimension] = entry.model_copy(
            update={
                "label": entry.label or DIMENSION_LABELS[entry.dimension],
                "lines": lines,
                "distinctiveness": max(0.0, min(1.0, float(entry.distinctiveness))),
            }
        )
    for dim in ALL_DIMENSIONS:
        by_dim.setdefault(dim, DimensionEntry(dimension=dim, label=DIMENSION_LABELS[dim]))
    ordered = sorted(by_dim.values(), key=lambda item: (-item.distinctiveness, ALL_DIMENSIONS.index(item.dimension)))
    temperament = [" ".join(str(t or "").split())[:CARD_LINE_MAX_CHARS] for t in card.temperament if str(t or "").strip()]
    return card.model_copy(update={"dimensions": ordered, "temperament": temperament[:4]})


def card_from_profile_json(profile_json: Mapping[str, Any] | None) -> DimensionCard | None:
    raw = (profile_json or {}).get("dimension_card") if isinstance(profile_json, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    try:
        return normalize_card(raw)
    except Exception:  # noqa: BLE001 — 坏卡按没有卡处理（渲染端退回样例 + 声音）
        return None


def line_states_from_profile_json(profile_json: Mapping[str, Any] | None) -> dict[str, str]:
    raw = (profile_json or {}).get("card_line_states") if isinstance(profile_json, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items() if str(v) in LINE_STATES}


def _usable_lines(
    entry: DimensionEntry, *, state: str, line_states: Mapping[str, str], kind: str
) -> list[CardLine]:
    lines = [
        line
        for line in entry.lines
        if line.kind == kind and line_states.get(line.line_id) != LINE_STATE_EXCLUDED
    ]
    pinned = [line for line in lines if line_states.get(line.line_id) == LINE_STATE_PINNED]
    rest = sorted(
        (line for line in lines if line_states.get(line.line_id) != LINE_STATE_PINNED),
        key=lambda line: (not line.mandatory, -line.distinctiveness),
    )
    limit = _LINES_PER_DIMENSION.get(state, 2) if kind == "do" else 1
    chosen = pinned + rest
    return chosen[: max(limit, len(pinned))]


def render_card_block(
    card: DimensionCard | None,
    *,
    dimension_states: Mapping[str, str] | None = None,
    line_states: Mapping[str, str] | None = None,
    recent_gaps: Sequence[str] | None = None,
    budget_chars: int = DEFAULT_CARD_BUDGET_CHARS,
    role: str = "draft",
) -> str:
    """``[文风卡]`` 块（system 前缀里的抽象部分；样例在 user 尾部）。

    顺序：气质（必须体现）→ 重点维 → 其余维（按辨识度）→ 「作者不这么写」→ 近期常见偏差。
    ``exclude`` 的维整维不出现；预算内整行截断，永不截半句。``role`` 只改标题口径
    （draft：写这一场时照着做；review：评审时逐维对照）。
    """
    if card is None:
        return ""
    states = normalize_dimension_states(dimension_states)
    line_state_map = dict(line_states or {})
    header = (
        "[文风卡](这位作者与通用写法不同的地方，逐维写明；样例是权威，这里帮你不漏掉。"
        "标「必须」的每场都要体现，设计文字的调性不改变这里的写法)"
        if role != "review"
        else "[文风卡](评审时逐维对照：草稿在这些维度上像不像这位作者；样例是权威)"
    )
    main_lines: list[str] = []
    if card.temperament:
        main_lines.append("气质（必须）：" + "；".join(card.temperament))
    ordered = sorted(
        (entry for entry in card.dimensions if states.get(entry.dimension) != DIMENSION_EXCLUDE),
        key=lambda entry: (states.get(entry.dimension) != DIMENSION_EMPHASIZE, -entry.distinctiveness),
    )
    avoid_lines: list[str] = []
    for entry in ordered:
        state = states.get(entry.dimension, "normal")
        do_lines = _usable_lines(entry, state=state, line_states=line_state_map, kind="do")
        if do_lines:
            mark = "【重点】" if state == DIMENSION_EMPHASIZE else ""
            body = "；".join(("（必须）" if line.mandatory else "") + line.text for line in do_lines)
            main_lines.append(f"- {mark}{entry.label}：{body}")
        for line in _usable_lines(entry, state=state, line_states=line_state_map, kind="avoid"):
            avoid_lines.append(f"- {entry.label}：{line.text}")
    gaps = [f"- {str(g).strip()}" for g in (recent_gaps or []) if str(g).strip()][:3]
    sections: list[tuple[str | None, list[str]]] = [
        (None, main_lines),
        ("[近期常见偏差](前几场草稿里反复出现的不像之处，这一场特别注意)", gaps),
        ("[作者不这么写]", avoid_lines),
    ]
    out: list[str] = [header]
    used = len(header) + 1
    for title, items in sections:
        kept: list[str] = []
        section_used = len(title) + 1 if title else 0
        for item in items:
            cost = len(item) + 1
            if used + section_used + cost > budget_chars:
                continue  # 整行截断，永不截半句
            kept.append(item)
            section_used += cost
        if kept:
            if title:
                out.append(title)
            out.extend(kept)
            used += section_used
    return "\n".join(out) if len(out) > 1 else ""


__all__ = [
    "CARD_LINE_MAX_CHARS",
    "CardLine",
    "DEFAULT_CARD_BUDGET_CHARS",
    "DIMENSION_CARD_VERSION",
    "DIMENSION_LABELS",
    "DimensionCard",
    "DimensionEntry",
    "LINE_STATES",
    "LINE_STATE_EXCLUDED",
    "LINE_STATE_PINNED",
    "PROFILE_VERSION_V3",
    "card_from_profile_json",
    "line_id_for",
    "line_states_from_profile_json",
    "normalize_card",
    "render_card_block",
]
