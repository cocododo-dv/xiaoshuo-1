"""风格参考 v3 — 文风卡（dimension card）：16 维各自「代替模型默认写法，这位作者这样写」。

文风卡是画像的核心显式表示（``profile_json["dimension_card"]``）：
- 由学习作业的合成步写出（每维 1–3 条可执行句，允许 ≤11 字的作者原话作例子，带证据引文 id；外加
  「作者不这么写」的 avoid 句、这一维的一句概括、通用模型在这一维的默认写法、选窗用的手法名）；
- 由注入渲染成 ``[文风卡]`` 块（气质与必须体现在前，按作品的维度状态排序 / 取舍，预算内整句取舍）；
- 由读数与定向修改按维使用（``measurable_features``：这一维可以用哪些测量核特征确定性地测）。

作者在矩阵里对某一句的 ✓ / ✗ 记在 ``profile_json["card_line_states"]``（``pinned`` 永远带上 / ``excluded``
不再用），不改卡本身、不让画像失效；作品层面的「重点 / 正常 / 不学」在绑定配置的 ``dimension_states``。

**预算（M2）**：卡的总预算（``injection_budget.yaml`` 的 ``card_budget_chars``，默认 2600 字）按固定次序分：
气质与 ✓ 钉住 / 标「必须」的句先占（永不因预算去掉——要去只能整张卡不发）→「作者不这么写」有自己的保底
份额（:data:`CARD_AVOID_SHARE`）→ 近期常见偏差 → 每一维的第一句（先让每一维都在）→ 卡句后面的原话例子 →
每一维的第二、三句 → 保底之外的「作者不这么写」。装不下的整句不带，永不截半句；外层预算拟合
（``inject.fit``）按 :attr:`CardPlan.priority` 倒过来去：先去例子与多出来的句，最后才让整维消失。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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

# 与前端 ws-labels.js 的 STYLE_DIMENSION_LABELS 逐字相同（frontend-react/src/ws-labels.test.js 核对）
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
# 「作者不这么写」在卡预算里的保底份额：排在其余卡句之前取，免得总被挤掉（M2）
CARD_AVOID_SHARE = 0.25
CARD_HEADER_DRAFT = (
    "[文风卡](这位作者与通用写法不同的地方，逐维写明；样例是权威，这里帮你不漏掉。"
    "标「必须」的每场都要体现，设计文字的调性不改变这里的写法)"
)
CARD_HEADER_REVIEW = "[文风卡](评审时逐维对照：草稿在这些维度上像不像这位作者；样例是权威)"
CARD_AVOID_TITLE = "[作者不这么写]"
CARD_GAPS_TITLE = "[近期常见偏差](前几场草稿里反复出现的不像之处，这一场特别注意)"
CARD_MAX_GAPS = 3
_LINES_PER_DIMENSION = {DIMENSION_EMPHASIZE: 3, "normal": 2}

# 取舍单元的类别（预算紧时的保留次序见模块说明；``mandatory`` 永不因预算去掉）
UNIT_MANDATORY = "mandatory"
UNIT_AVOID = "avoid"
UNIT_GAP = "gap"
UNIT_PRIMARY = "primary"
UNIT_EXAMPLE = "example"
UNIT_EXTRA = "extra"
UNIT_AVOID_EXTRA = "avoid_extra"
UNIT_TEMPERAMENT = "temperament"
EXAMPLE_UNIT_PREFIX = "example:"
GAP_UNIT_PREFIX = "gap:"


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


@dataclass(frozen=True)
class CardUnit:
    """卡里一个可取舍的单元：一句（``line_id``）、一句后面的例子（``example:<line_id>``）、一条近期偏差
    （``gap:<i>``）或气质（``temperament``）。"""

    unit_id: str
    kind: str


@dataclass(frozen=True)
class CardPlan:
    """一次渲染的结果与取舍：``text`` 是卡块；``mandatory`` 是永远带上的单元；``priority`` 是其余单元按
    「先保留谁」排好的次序（外层拟合倒过来去）；``kept`` / ``dropped`` 是这一次预算里带上 / 没带上的。"""

    text: str
    mandatory: tuple[CardUnit, ...] = ()
    priority: tuple[CardUnit, ...] = ()
    kept: frozenset[str] = frozenset()
    dropped: tuple[CardUnit, ...] = ()
    example_count: int = 0


def _clean_example(value: Any) -> str:
    return " ".join(str(value or "").split())


def plan_card_block(
    card: DimensionCard | None,
    *,
    dimension_states: Mapping[str, str] | None = None,
    line_states: Mapping[str, str] | None = None,
    recent_gaps: Sequence[str] | None = None,
    budget_chars: int = DEFAULT_CARD_BUDGET_CHARS,
    role: str = "draft",
    examples: Mapping[str, str] | None = None,
    avoid_share: float = CARD_AVOID_SHARE,
) -> CardPlan:
    """``[文风卡]`` 块与它的取舍（见模块说明的预算次序）。

    输出顺序：标题 → 气质（必须）→ 各维（重点维在前，其余按辨识度；一维一行，这一维带上的句用「；」连，
    钉住 / 标「必须」的句在前）→ ``[作者不这么写]`` → ``[近期常见偏差]``（卡的末尾）。``exclude`` 的维整维
    不出现（钉住的句也不出现——不学就是不学）；``examples``（``line_id`` → 作者原话）只挂在 do 句后面。
    """
    if card is None:
        return CardPlan(text="")
    budget = max(0, int(budget_chars))
    states = normalize_dimension_states(dimension_states)
    line_state_map = dict(line_states or {})
    example_map = {str(k): _clean_example(v) for k, v in (examples or {}).items() if _clean_example(v)}
    header = CARD_HEADER_REVIEW if role == "review" else CARD_HEADER_DRAFT
    temperament = "气质（必须）：" + "；".join(card.temperament) if card.temperament else ""
    gaps = [str(g).strip() for g in (recent_gaps or []) if str(g or "").strip()][:CARD_MAX_GAPS]
    entries = sorted(
        (entry for entry in card.dimensions if states.get(entry.dimension) != DIMENSION_EXCLUDE),
        key=lambda entry: (states.get(entry.dimension) != DIMENSION_EMPHASIZE, -entry.distinctiveness),
    )

    def _anchored(line: CardLine) -> bool:
        return line.mandatory or line_state_map.get(line.line_id) == LINE_STATE_PINNED

    # (entry, 这一维的 do 句, 这一维的 avoid 句)，句子按 _usable_lines 的次序（钉住的在前）
    dims: list[tuple[DimensionEntry, list[CardLine], list[CardLine]]] = []
    avoid_cost: dict[str, int] = {}
    for entry in entries:
        state = states.get(entry.dimension, "normal")
        do = _usable_lines(entry, state=state, line_states=line_state_map, kind="do")
        avoid = _usable_lines(entry, state=state, line_states=line_state_map, kind="avoid")
        dims.append((entry, do, avoid))
        for line in avoid:
            avoid_cost.setdefault(line.line_id, len(f"- {entry.label}：{line.text}") + 1)

    mandatory: list[CardUnit] = []
    if temperament:
        mandatory.append(CardUnit(UNIT_TEMPERAMENT, UNIT_TEMPERAMENT))
    avoid_units: list[CardUnit] = []
    ranked_do: list[tuple[int, int, CardUnit]] = []  # (第几句, 维序, 单元)
    for order, (_entry, do, avoid) in enumerate(dims):
        anchored = [line for line in do if _anchored(line)]
        mandatory.extend(CardUnit(line.line_id, UNIT_MANDATORY) for line in anchored)
        rest = [line for line in do if not _anchored(line)]
        offset = 1 if anchored else 0
        for index, line in enumerate(rest):
            rank = index + offset
            ranked_do.append((rank, order, CardUnit(line.line_id, UNIT_PRIMARY if rank == 0 else UNIT_EXTRA)))
        for line in avoid:
            if _anchored(line):
                mandatory.append(CardUnit(line.line_id, UNIT_MANDATORY))
            else:
                avoid_units.append(CardUnit(line.line_id, UNIT_AVOID))
    ranked_do.sort(key=lambda item: (item[0], item[1]))
    primary = [unit for rank, _order, unit in ranked_do if rank == 0]
    extra = [unit for rank, _order, unit in ranked_do if rank > 0]
    gap_units = [CardUnit(f"{GAP_UNIT_PREFIX}{index}", UNIT_GAP) for index in range(len(gaps))]

    kept: set[str] = {unit.unit_id for unit in mandatory}

    def _render(keep: set[str]) -> str:
        out: list[str] = [header]
        if temperament and UNIT_TEMPERAMENT in keep:
            out.append(temperament)
        for entry, do, _avoid in dims:
            chosen = [line for line in do if line.line_id in keep]
            if not chosen:
                continue
            state = states.get(entry.dimension, "normal")
            mark = "【重点】" if state == DIMENSION_EMPHASIZE else ""
            parts = []
            for line in chosen:
                text = ("（必须）" if line.mandatory else "") + line.text
                if f"{EXAMPLE_UNIT_PREFIX}{line.line_id}" in keep and line.line_id in example_map:
                    text += f"（例：「{example_map[line.line_id]}」）"
                parts.append(text)
            out.append(f"- {mark}{entry.label}：{'；'.join(parts)}")
        avoid_out = [
            f"- {entry.label}：{line.text}" for entry, _do, avoid in dims for line in avoid if line.line_id in keep
        ]
        if avoid_out:
            out.append(CARD_AVOID_TITLE)
            out.extend(avoid_out)
        gap_out = [f"- {gap}" for index, gap in enumerate(gaps) if f"{GAP_UNIT_PREFIX}{index}" in keep]
        if gap_out:
            out.append(CARD_GAPS_TITLE)
            out.extend(gap_out)
        return "\n".join(out) if len(out) > 1 else ""

    def _fits(unit_id: str) -> bool:
        candidate = set(kept)
        candidate.add(unit_id)
        if len(_render(candidate)) > budget:
            return False
        kept.add(unit_id)
        return True

    dropped: list[CardUnit] = []
    priority: list[CardUnit] = []

    # 1.「作者不这么写」的保底份额
    share_chars = int(budget * max(0.0, min(1.0, float(avoid_share))))
    avoid_used = 0
    leftover_avoid: list[CardUnit] = []
    for unit in avoid_units:
        cost = avoid_cost.get(unit.unit_id, 0) + (0 if avoid_used else len(CARD_AVOID_TITLE) + 1)
        if avoid_used + cost <= share_chars and _fits(unit.unit_id):
            avoid_used += cost
            priority.append(unit)
        else:
            leftover_avoid.append(CardUnit(unit.unit_id, UNIT_AVOID_EXTRA))
    # 2. 近期常见偏差
    for unit in gap_units:
        priority.append(unit)
        if not _fits(unit.unit_id):
            dropped.append(unit)
    # 3. 每一维的第一句（先让每一维都在）
    for unit in primary:
        priority.append(unit)
        if not _fits(unit.unit_id):
            dropped.append(unit)

    def _examples_for(units: Sequence[CardUnit]) -> None:
        for unit in units:
            if unit.unit_id not in kept or unit.unit_id not in example_map:
                continue
            example = CardUnit(f"{EXAMPLE_UNIT_PREFIX}{unit.unit_id}", UNIT_EXAMPLE)
            priority.append(example)
            if not _fits(example.unit_id):
                dropped.append(example)

    # 4. 已带上的句（必须 / 钉住的与每维第一句）后面的原话例子
    _examples_for([unit for unit in mandatory if unit.kind == UNIT_MANDATORY] + primary)
    # 5. 每一维的第二、三句，再给它们挂例子
    for unit in extra:
        priority.append(unit)
        if not _fits(unit.unit_id):
            dropped.append(unit)
    _examples_for(extra)
    # 6. 保底之外的「作者不这么写」
    for unit in leftover_avoid:
        priority.append(unit)
        if not _fits(unit.unit_id):
            dropped.append(unit)

    text = _render(kept)
    example_count = sum(1 for unit_id in kept if unit_id.startswith(EXAMPLE_UNIT_PREFIX))
    return CardPlan(
        text=text,
        mandatory=tuple(mandatory),
        priority=tuple(priority),
        kept=frozenset(kept),
        dropped=tuple(dropped),
        example_count=example_count,
    )


def render_card_block(
    card: DimensionCard | None,
    *,
    dimension_states: Mapping[str, str] | None = None,
    line_states: Mapping[str, str] | None = None,
    recent_gaps: Sequence[str] | None = None,
    budget_chars: int = DEFAULT_CARD_BUDGET_CHARS,
    role: str = "draft",
    examples: Mapping[str, str] | None = None,
) -> str:
    """``[文风卡]`` 块（system 前缀里的抽象部分；样例在 user 尾部）——:func:`plan_card_block` 的正文。

    顺序：气质（必须体现）→ 重点维 → 其余维（按辨识度）→ 「作者不这么写」→ 近期常见偏差。
    ``exclude`` 的维整维不出现；预算内整句取舍，永不截半句；钉住 / 标「必须」的句永不因预算去掉。
    ``role`` 只改标题口径（draft：写这一场时照着做；review：评审时逐维对照）。
    """
    return plan_card_block(
        card,
        dimension_states=dimension_states,
        line_states=line_states,
        recent_gaps=recent_gaps,
        budget_chars=budget_chars,
        role=role,
        examples=examples,
    ).text


__all__ = [
    "CARD_AVOID_SHARE",
    "CARD_AVOID_TITLE",
    "CARD_GAPS_TITLE",
    "CARD_HEADER_DRAFT",
    "CARD_HEADER_REVIEW",
    "CARD_LINE_MAX_CHARS",
    "CARD_MAX_GAPS",
    "CardLine",
    "CardPlan",
    "CardUnit",
    "DEFAULT_CARD_BUDGET_CHARS",
    "DIMENSION_CARD_VERSION",
    "DIMENSION_LABELS",
    "DimensionCard",
    "DimensionEntry",
    "EXAMPLE_UNIT_PREFIX",
    "GAP_UNIT_PREFIX",
    "LINE_STATES",
    "LINE_STATE_EXCLUDED",
    "LINE_STATE_PINNED",
    "PROFILE_VERSION_V3",
    "UNIT_AVOID",
    "UNIT_AVOID_EXTRA",
    "UNIT_EXAMPLE",
    "UNIT_EXTRA",
    "UNIT_GAP",
    "UNIT_MANDATORY",
    "UNIT_PRIMARY",
    "UNIT_TEMPERAMENT",
    "card_from_profile_json",
    "line_id_for",
    "line_states_from_profile_json",
    "normalize_card",
    "plan_card_block",
    "render_card_block",
]
