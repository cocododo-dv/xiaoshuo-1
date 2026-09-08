"""Style Reference v2 — 叙事机制指引（W5，规格 §1.3 / §2.W5.1）。

合成期（W1）把 ``profile_json.narrative_guidance``（≤8 行、确定性派生、已过原文
重合过滤）写进画像，并随 ``runtime_contract._FROZEN_PROFILE_JSON_KEYS`` 冻结进
每个 SceneBundle。本模块只做两件事：

- ``collect_narrative_guidance(contract)``：遍历冻结契约 ``layers[*].profile.
  profile_json.narrative_guidance``，按层序（泛 → 具体）拼接、去重、截到 8 行；
- ``render_narrative_section(lines)``：渲染成 bundle section
  ``style_narrative_guidance`` 的正文（每行 ``- `` 开头，带一句用途前缀）。

这是 **neutral_draft 唯一可见的风格参考块**：它只决定「先说什么、何处停顿、透露
多少、时间怎么推进」，不含任何语言层特征或原文样例，因此不与 “Keep the prose
neutral” 冲突。旧画像没有该键时返回空 → 调用方不登记 section（优雅退化）。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

NARRATIVE_GUIDANCE_MAX_LINES = 8
NARRATIVE_GUIDANCE_SECTION_KEY = "style_narrative_guidance"
NARRATIVE_GUIDANCE_SECTION_LABEL = "Style Reference — Narrative Mechanisms"
_NARRATIVE_SECTION_PREFIX = (
    "以下是参考作品的叙事取舍机制（只决定先说什么、何处停顿、透露多少、时间如何"
    "推进；不决定用词，也不是需要复述的文本）："
)
# 合成期给 narrative.* forbidden_pattern 陈述加的极性标记。规格 §2.W1.4 只说「并集」，
# 但 forbidden 陈述按抽取约定直接命名模式本身（极性原本由注入侧 `[禁忌模式]` 标题
# 承载）；平铺进 narrative_guidance 后极性必须随行携带，否则 neutral_draft /
# scene_blueprint 会把「作者明确不用的模式」当成要采用的机制。
NARRATIVE_AVOID_MARKER = "避免："
# 已是否定 / 回避措辞的陈述不再叠加标记（避免「避免：不用 …」式双重否定）。
# 只认完整的否定 / 回避词：单字「不 / 无 / 别 / 莫 / 忌」会把「不断插入旁白」「不同视角
# 交替」「无数细节堆叠」「别出心裁地倒叙」「莫名其妙地切换」这类正面陈述误判为已否定。
_NEGATED_STATEMENT_PREFIXES: tuple[str, ...] = (
    "不用",
    "不使用",
    "不采用",
    "不采取",
    "不会",
    "不要",
    "不能",
    "不可",
    "不得",
    "不宜",
    "不以",
    "不把",
    "不让",
    "不做",
    "不写",
    "不作",
    "不去",
    "不再",
    "不必",
    "不曾",
    "不肯",
    "不直接",
    "不靠",
    "不加",
    "不设",
    "不依赖",
    "不借",
    "不轻易",
    "不刻意",
    "不随意",
    "不动辄",
    "不逐",
    "无需",
    "无须",
    "毋",
    "勿",
    "莫用",
    "莫要",
    "别用",
    "别让",
    "别把",
    "忌用",
    "切忌",
    "忌讳",
    "避免",
    "避开",
    "回避",
    "禁止",
    "禁用",
    "杜绝",
    "拒绝",
    "从不",
    "从来不",
    "从未",
    "绝不",
    "决不",
    "切勿",
    "少用",
    "慎用",
    "摒弃",
    "舍弃",
    "省去",
    "没有",
)


def mark_forbidden_narrative_statement(statement: Any) -> str:
    """给 forbidden 陈述加 ``避免：`` 前缀，让极性随行进入 ``narrative_guidance``。

    已以否定 / 回避词起头（不 / 无 / 勿 / 避免 / 禁止 / 从不 …）的陈述原样返回；
    空白 → ``""``。只规整空白，不改动陈述正文。
    """
    text = " ".join(str(statement or "").split()).strip()
    if not text or text.startswith(_NEGATED_STATEMENT_PREFIXES):
        return text
    return f"{NARRATIVE_AVOID_MARKER}{text}"


def _normalize_line(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    # 合成期已保证纯文本；这里只剥掉偶发的列表前缀，避免渲染成 “- - 行”。
    while text[:1] in {"-", "•", "·"}:
        text = text[1:].lstrip()
    return text


def _layer_lines(layer: Any) -> list[str]:
    if not isinstance(layer, Mapping):
        return []
    profile = layer.get("profile")
    if not isinstance(profile, Mapping):
        return []
    profile_json = profile.get("profile_json")
    if not isinstance(profile_json, Mapping):
        return []
    raw = profile_json.get("narrative_guidance")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, bytearray)):
        return []
    return [line for line in (_normalize_line(item) for item in raw) if line]


def collect_narrative_guidance(
    contract: Mapping[str, Any] | None,
    *,
    max_lines: int = NARRATIVE_GUIDANCE_MAX_LINES,
) -> list[str]:
    """合并冻结契约各层的 ``narrative_guidance``：泛 → 具体、去重、≤``max_lines``。

    契约缺失 / 形状不对 / 所有层都没有该键 → ``[]``。绝不抛异常：这是可选增强。
    """
    if not isinstance(contract, Mapping):
        return []
    layers = contract.get("layers")
    if not isinstance(layers, Sequence) or isinstance(layers, (str, bytes, bytearray)):
        return []
    ordered: list[Any] = sorted(
        (layer for layer in layers if isinstance(layer, Mapping)),
        key=lambda layer: (
            layer.get("order") if isinstance(layer.get("order"), int) else 0
        ),
    )
    collected: list[str] = []
    seen: set[str] = set()
    for layer in ordered:
        for line in _layer_lines(layer):
            key = line.casefold()
            if key in seen:
                continue
            seen.add(key)
            collected.append(line)
            if len(collected) >= max(0, int(max_lines)):
                return collected
    return collected


def render_narrative_section(lines: Sequence[str]) -> str:
    """把指引行渲染成 bundle section 正文；无行 → 空串（调用方据此不登记）。"""
    cleaned = [line for line in (_normalize_line(item) for item in lines) if line]
    if not cleaned:
        return ""
    return "\n".join([_NARRATIVE_SECTION_PREFIX, *(f"- {line}" for line in cleaned)])


# 规格 §2.W5.1 用的短名；保留别名以便两种称呼都可用。
render_section = render_narrative_section


__all__ = [
    "NARRATIVE_AVOID_MARKER",
    "NARRATIVE_GUIDANCE_MAX_LINES",
    "NARRATIVE_GUIDANCE_SECTION_KEY",
    "NARRATIVE_GUIDANCE_SECTION_LABEL",
    "collect_narrative_guidance",
    "mark_forbidden_narrative_statement",
    "render_narrative_section",
    "render_section",
]
