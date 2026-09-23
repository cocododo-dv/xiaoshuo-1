"""规划层的风格参考解析（2026-09-12 结构跟随，Step 2 Track B）。

场景运行有 ``bundle_builder.resolve_scene_style_runtime_contract``（scene > character >
project > global）；规划一章 / 排一张场表时还没有具体的场，只能按 **project + global**
作用域解析。本模块把这条路径收口成一个函数，并把冻结契约里最具体一层的
``structure_card`` / ``planning_guidance`` 渲染成规划节点可直接注入的中文块：

- ``resolve_project_style_reference(session, project_id)`` → 见 :func:`render_planning_reference`
  的返回值；无绑定 / 旧画像无键 / 解析异常 → ``None``（只记 debug 日志，规划永不因此阻断）。
- ``render_planning_reference(contract, session=...)`` → ``{"contract_hash", "profile_id",
  "structure_card", "structure_samples", "planning_guidance"}``。样例块单独成键：雪花提示词
  预算可以先卸样例、再卸整张画像（``snowflake_prompt_budget``）。

云策略按**接收这份规划提示的节点**的实际路由判（H1，与起草注入同一个判定
``policy.decide_reference_route``）：调用方用 ``node_ids=`` 说是哪个节点；不说时按这个函数的全部消费节点
（:data:`PROJECT_PLANNING_NODE_IDS` / :data:`SCENE_PLANNING_NODE_IDS` / :data:`CHAPTER_TITLE_NODE_IDS`）判，要求
每一个都满足——

- 「仅本机」的书：这些节点都走本机模型才给参考（含章首 / 章尾原文样例）；有一个走云端就**什么都不给**
  （返回 ``None``——规划是可选增强，不报错，但由这本书派生的结构画像、场景手法、叙事机制、章题一个字都不送）；
- 送云策略的书：章首 / 章尾样例与章题样例是参考原文，只有该书**冻结时**与**现在**都允许送云端（严格发送权声明）
  才渲染；否则只给带数字的画像。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceParagraph
from novel_system.services.style_reference.binding_config import (
    DIMENSION_EMPHASIZE,
    DIMENSION_EXCLUDE,
    normalize_binding_config,
)
from novel_system.services.style_reference.card import (
    LINE_STATE_EXCLUDED,
    LINE_STATE_PINNED,
    card_from_profile_json,
    line_states_from_profile_json,
)
from novel_system.services.style_reference.inject.bindings import resolve_binding_layers
from novel_system.services.style_reference.narrative_guidance import (
    NARRATIVE_GUIDANCE_SECTION_KEY,
    collect_narrative_guidance,
    render_narrative_section,
)
from novel_system.services.style_reference.policy import ReferenceRouteDecision, decide_reference_route
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import build_style_runtime_contract, contract_layer
from novel_system.services.style_reference.segmentation.heuristic import is_title_paragraph
from novel_system.services.style_reference.structure import (
    PLANNING_GUIDANCE_HEADER,
    PLANNING_GUIDANCE_MAX_LINES,
    STRUCTURE_TITLE_MAX_CHARS,
    chapter_titles_summary,
    render_planning_guidance,
    render_structure_card_parts,
)

logger = logging.getLogger(__name__)

# 2026-09-12 结构跟随（Step 2 Track B）/ 2026-09-14 保真修补（WP6.1）：场景蓝图与近终稿规划
# （章架构、人物压力）的来源快照共用这三个摘要键。``style_narrative_guidance`` 在
# ``context_budget.SECTION_SPECS`` 里（PromptBuilder 渲染成 Narrative Mechanisms section）；
# 结构画像 / 场景手法不在那张表里（它属于场景 bundle），由调用方通过
# :func:`style_reference_prompt_blocks` 直接渲染进 user prompt（体量由渲染器封顶：画像
# ≤1,500 字 + ≤6 条 ≤150 字样例 + ≤10 行手法）。
STYLE_STRUCTURE_CARD_KEY = "style_structure_card"
STYLE_PLANNING_GUIDANCE_KEY = "style_planning_guidance"
STYLE_REFERENCE_DIGEST_KEYS: tuple[str, ...] = (
    NARRATIVE_GUIDANCE_SECTION_KEY,
    STYLE_STRUCTURE_CARD_KEY,
    STYLE_PLANNING_GUIDANCE_KEY,
)
STRUCTURE_CARD_PROMPT_HEADING = "## Style Reference — Structure Card"
PLANNING_GUIDANCE_PROMPT_HEADING = "## Style Reference — Scene Craft"

PLANNING_REFERENCE_TASK_TYPE = "scene_generation"
# 2026-09-22 结构跟随参考书 / 简报气质让位:场景手法里的情绪基调、价值取向、叙事观也是这位作家的
# 气质——每一场的读者情绪与钩子按他到达它们的方式设想;project_scale 给出的每场字数直接进长度带。
STRUCTURE_REFERENCE_HOW_TO_USE = (
    "这是作者绑定的参考作家的结构画像与场景手法：按其章 / 场尺度（每章场数与字数、"
    "开章与收章方式、对白与叙述比重、何处用概述）规划，沿用其惯用的场景类型与收场方式；"
    "[场景手法] 里的情绪基调、价值取向与叙事观也是这位作家的气质——每一场的读者情绪、钩子与收场"
    "都按这位作家到达它们的方式来设想（他会用什么样的对冲、幽默、反讽或留白），不要替他换成另一种"
    "文学气质；project_scale 给出按参考章长与本书每章场数推算的每场字数时，target_length_band 取"
    "它附近的数字区间；只学结构与手法，绝不复用样例里的人物、地点、事件与句子。"
)
REFERENCE_TITLES_HOW_TO_USE = (
    "这是作者绑定的参考作家给自己各章起的题名（编号已去掉，编号由系统另加）：照这位作家起题名的"
    "方式来起——题名长短按 name_chars、是具体物象 / 人名 / 地名还是一句话、直白还是含蓄、有没有副题；"
    "样例本身一个也不能用，也不能改一两个字冒充。"
)
# 旧画像(structure_card_v1)没有 chapter_titles:按段落表惰性补算,按 (book_id, 段落数) 缓存。
_CHAPTER_TITLES_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
_CHAPTER_TITLES_CACHE_MAX = 64


def chapter_titles_for_book(session: Session | None, book_id: str | None) -> dict[str, Any] | None:
    """从段落表算出章题画像(``structure.chapter_titles_summary``);读库失败 → ``None``。

    只扫短段(≤ ``STRUCTURE_TITLE_MAX_CHARS`` 字)再过标题启发式,190 万字的书也只是几万行的
    一次 SQL;同一 (book, 段落数) 只算一次。
    """
    if session is None or not book_id:
        return None
    try:
        count = int(
            session.execute(
                select(func.count())
                .select_from(StyleReferenceParagraph)
                .where(StyleReferenceParagraph.book_id == str(book_id))
            ).scalar()
            or 0
        )
        key = (str(book_id), count)
        cached = _CHAPTER_TITLES_CACHE.get(key)
        if cached is not None:
            return dict(cached)
        rows = session.execute(
            select(StyleReferenceParagraph.text)
            .where(
                StyleReferenceParagraph.book_id == str(book_id),
                StyleReferenceParagraph.char_count <= STRUCTURE_TITLE_MAX_CHARS,
            )
            .order_by(StyleReferenceParagraph.paragraph_index)
        ).scalars().all()
    except Exception:  # noqa: BLE001 — 可选增强
        logger.debug("chapter titles unavailable for book %s", book_id, exc_info=True)
        return None
    titles = [str(text or "") for text in rows if is_title_paragraph(str(text or ""))]
    summary = chapter_titles_summary(titles)
    if len(_CHAPTER_TITLES_CACHE) >= _CHAPTER_TITLES_CACHE_MAX:
        _CHAPTER_TITLES_CACHE.clear()
    _CHAPTER_TITLES_CACHE[key] = summary
    return dict(summary)


# 各入口的消费节点（调用方不说是哪个节点时按全部消费节点判，每一个都要满足）
# resolve_project_style_reference：雪花 09 / 10 场景表（snowflake_step_generate）、分章起章名（snowflake_chapter_plan）、
# 章规划的四个节点（chapter_planning_context 的 style_reference 槽）
PROJECT_PLANNING_NODE_IDS: tuple[str, ...] = (
    "snowflake_step_generate",
    "snowflake_chapter_plan",
    "chapter_story_architecture",
    "chapter_scene_plan_candidates",
    "chapter_scene_plan_fill",
    "chapter_plan_review",
)
# build_planning_style_reference：场景蓝图的来源快照、准定稿规划（章架构 / 人物压力）的来源快照
SCENE_PLANNING_NODE_IDS: tuple[str, ...] = (
    "scene_blueprint",
    "chapter_story_architecture",
    "character_pressure_blueprint",
)
# reference_titles_payload：「AI 起章名」
CHAPTER_TITLE_NODE_IDS: tuple[str, ...] = ("snowflake_chapter_plan",)


def _route_decision(
    layer: Mapping[str, Any],
    session: Session | None,
    node_ids: Sequence[str],
) -> ReferenceRouteDecision:
    """这一层参考能不能进这些节点的提示（H1）。没有会话（纯函数调用）→ 只按冻结的书快照判。"""
    book = layer.get("book") if isinstance(layer.get("book"), Mapping) else {}
    if session is None:
        return decide_reference_route(
            None, node_ids=node_ids, frozen_book=book, book_missing=False, operation="style_reference_planning"
        )
    book_id = str(book.get("book_id") or "")
    row = StyleReferenceRepository(session).get_book(book_id) if book_id else None
    return decide_reference_route(row, node_ids=node_ids, frozen_book=book, operation="style_reference_planning")


CARD_PLANNING_PREFIXES = ("scene.", "theme.")
CARD_PLANNING_HEADER = (
    f"{PLANNING_GUIDANCE_HEADER}（参考作者在场景与主题层面的写法与气质；规划时按这位作者的方式设想每一场的场面、"
    "读者情绪、钩子与收场，设计文字的调性不改变这里的写法；不复用其内容）"
)


def render_card_planning_guidance(
    profile_json: Mapping[str, Any] | None,
    dimension_states: Mapping[str, str] | None = None,
) -> str:
    """文风卡 → 规划层的 ``[场景手法]``：气质（必须）+ 场景层 / 主题层各维的句子（重点维在前、多一条；
    不学的维与作者划掉的句不出现；钉住的句永远带上）；≤ ``PLANNING_GUIDANCE_MAX_LINES`` 行。没有卡 → ``""``。"""
    card = card_from_profile_json(profile_json)
    if card is None:
        return ""
    states = dict(dimension_states or {})
    line_states = line_states_from_profile_json(profile_json)
    lines: list[str] = []
    if card.temperament:
        lines.append("- 气质（必须）：" + "；".join(card.temperament))
    entries = sorted(
        (
            entry
            for entry in card.dimensions
            if entry.dimension.startswith(CARD_PLANNING_PREFIXES) and states.get(entry.dimension) != DIMENSION_EXCLUDE
        ),
        key=lambda entry: (states.get(entry.dimension) != DIMENSION_EMPHASIZE, -entry.distinctiveness),
    )
    for entry in entries:
        if len(lines) >= PLANNING_GUIDANCE_MAX_LINES:
            break
        usable = [
            line
            for line in entry.lines
            if line.kind == "do" and line_states.get(line.line_id) != LINE_STATE_EXCLUDED
        ]
        usable.sort(
            key=lambda line: (
                line_states.get(line.line_id) != LINE_STATE_PINNED,
                not line.mandatory,
                -line.distinctiveness,
            )
        )
        pinned = sum(1 for line in usable if line_states.get(line.line_id) == LINE_STATE_PINNED)
        limit = max(3 if states.get(entry.dimension) == DIMENSION_EMPHASIZE else 2, pinned)
        chosen = usable[:limit]
        if not chosen:
            continue
        mark = "【重点】" if states.get(entry.dimension) == DIMENSION_EMPHASIZE else ""
        body = "；".join(("（必须）" if line.mandatory else "") + line.text for line in chosen)
        lines.append(f"- {mark}{entry.label}：{body}")
    if not lines:
        return ""
    return "\n".join([CARD_PLANNING_HEADER, *lines])


def render_planning_reference(
    contract: Mapping[str, Any] | None,
    *,
    session: Session | None = None,
    node_ids: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """从冻结契约最具体的一层渲染规划层参考块；没有可渲染内容 → ``None``。

    ``node_ids``：接收规划提示的节点（缺省 :data:`PROJECT_PLANNING_NODE_IDS`，全部要满足）。这本书的云策略不许
    送给它们（「仅本机」遇云端节点……）→ ``None``，由这本书派生的东西一个字都不给（H1）。"""
    if not isinstance(contract, Mapping):
        return None
    layer = contract_layer(contract)
    if not layer:
        return None
    route = _route_decision(layer, session, tuple(node_ids) if node_ids else PROJECT_PLANNING_NODE_IDS)
    if not route.send_book:
        logger.debug("planning style reference withheld (%s): %s", route.reason, route.audit())
        return None
    samples_allowed = route.send_samples
    profile = layer.get("profile") if isinstance(layer.get("profile"), Mapping) else {}
    profile_json = profile.get("profile_json") if isinstance(profile.get("profile_json"), Mapping) else {}
    card = profile_json.get("structure_card") if isinstance(profile_json.get("structure_card"), Mapping) else None
    # 2026-09-22:v1 画像没有章题——按冻结层的书惰性补算(章题不是内容,不必等重新合成)
    chapter_titles = card.get("chapter_titles") if card is not None else None
    if card is not None and not isinstance(chapter_titles, Mapping):
        book = layer.get("book") if isinstance(layer.get("book"), Mapping) else {}
        chapter_titles = chapter_titles_for_book(session, str(book.get("book_id") or "") or None)
    structure_card, structure_samples = render_structure_card_parts(
        profile_json,
        include_samples=samples_allowed,
        chapter_titles=chapter_titles if isinstance(chapter_titles, Mapping) else None,
    )
    binding = layer.get("binding") if isinstance(layer.get("binding"), Mapping) else {}
    dimension_states = normalize_binding_config(
        str(binding.get("strategy") or "mixed"),
        binding.get("config_json") if isinstance(binding.get("config_json"), Mapping) else {},
    )["dimension_states"]
    # 2026-09-23 风格参考 v3（L5，全学）：有文风卡的画像给规划节点卡里场景层 / 主题层的句子与气质，
    # 不再给旧的 [场景手法] 观察陈述；旧画像照旧。
    planning_guidance = render_card_planning_guidance(profile_json, dimension_states) or render_planning_guidance(
        profile_json
    )
    if not structure_card and not planning_guidance:
        return None
    return {
        "contract_hash": str(contract.get("contract_hash") or ""),
        "profile_id": str(profile.get("profile_id") or ""),
        "structure_card": structure_card,
        "structure_samples": structure_samples,
        "planning_guidance": planning_guidance,
        # 原始画像与章题画像:分章面板起章名、规划期推场长用;提示词载荷只挑上面三块
        "card": dict(card) if card is not None else None,
        "chapter_titles": dict(chapter_titles) if isinstance(chapter_titles, Mapping) else None,
        "samples_allowed": samples_allowed,
    }


def reference_titles_payload(
    session: Session,
    project_id: str | None,
    *,
    node_ids: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """AI 起章名的参考载荷:参考作家的章题形态与题名样例(project + global 绑定);无绑定 / 无章题 /
    这本书不许送给起章名的节点(缺省 :data:`CHAPTER_TITLE_NODE_IDS`) → ``None``。样例是原文题名,与章首 /
    章尾样例同一送云端口径。"""
    reference = resolve_project_style_reference(
        session, project_id, node_ids=tuple(node_ids) if node_ids else CHAPTER_TITLE_NODE_IDS
    )
    if not reference:
        return None
    titles = reference.get("chapter_titles")
    if not isinstance(titles, Mapping) or int(titles.get("count") or 0) <= 0:
        return None
    samples = [str(item) for item in (titles.get("samples") or []) if str(item or "").strip()]
    if not reference.get("samples_allowed", True):
        samples = []
    return {
        "profile_id": str(reference.get("profile_id") or ""),
        "count": int(titles.get("count") or 0),
        "named_count": int(titles.get("named_count") or 0),
        "marker_style": titles.get("marker_style"),
        "name_chars": dict(titles.get("name_chars") or {}),
        "samples": samples,
        "how_to_use": REFERENCE_TITLES_HOW_TO_USE,
    }


def structure_card_text(reference: Mapping[str, Any] | None) -> str:
    """画像 + 样例合成一段（scene_blueprint 摘要 / 章规划 slot 用）。"""
    if not isinstance(reference, Mapping):
        return ""
    return "\n".join(
        part
        for part in (
            str(reference.get("structure_card") or ""),
            str(reference.get("structure_samples") or ""),
        )
        if part
    )


def resolve_project_style_reference(
    session: Session,
    project_id: str | None,
    *,
    task_type: str = PLANNING_REFERENCE_TASK_TYPE,
    node_ids: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """按 project + global 作用域解析 active 绑定、冻结契约、渲染规划层参考块。

    ``node_ids``：接收这份规划提示的节点（缺省 :data:`PROJECT_PLANNING_NODE_IDS`，每一个都要满足书的云策略）。
    任何异常都吞掉并返回 ``None``：这是规划节点的可选增强，缺参考不能让规划失败。
    """
    if not project_id:
        return None
    try:
        layers = resolve_binding_layers(session, str(project_id), task_type, character_ids=[], scene_id=None)
        if not layers:
            return None
        contract = build_style_runtime_contract(StyleReferenceRepository(session), layers, task_type=task_type)
        if not contract:
            return None
        return render_planning_reference(contract, session=session, node_ids=node_ids)
    except Exception:  # noqa: BLE001 — 可选增强：解析失败只记日志
        logger.debug(
            "project style reference unavailable for project %s", project_id, exc_info=True
        )
        return None


@dataclass(frozen=True)
class PlanningStyleReference:
    """一份规划快照要登记的风格参考：契约哈希、按固定次序的摘要块、来源引用字段。"""

    contract_hash: str
    digests: dict[str, str]
    refs: dict[str, Any]


def build_planning_style_reference(
    contract: Mapping[str, Any] | None,
    *,
    session: Session | None = None,
    node_ids: Sequence[str] | None = None,
) -> PlanningStyleReference | None:
    """从（按场景作用域解析出的）契约生成规划层三块摘要。

    叙事机制（``narrative_guidance``，无语言层特征）、结构画像（+ 允许送云端时的章首 / 章尾
    样例）、场景手法。无绑定 / 旧画像无键 → ``None``：调用方连契约哈希也不登记（与旧画像行为
    一致）。``refs`` 里 ``style_narrative_guidance_line_count`` 只要有任一块就记（可为 0），
    ``style_structure_card_chars`` / ``style_planning_guidance_line_count`` 只在对应块存在时记。
    ``node_ids``：接收规划提示的节点（缺省 :data:`SCENE_PLANNING_NODE_IDS`）；这本书不许送给它们 → ``None``（H1）。
    """
    if not isinstance(contract, Mapping):
        return None
    layer = contract_layer(contract)
    nodes = tuple(node_ids) if node_ids else SCENE_PLANNING_NODE_IDS
    if layer and not _route_decision(layer, session, nodes).send_book:
        return None
    contract_hash = str(contract.get("contract_hash") or "")
    lines = collect_narrative_guidance(contract)
    reference = render_planning_reference(contract, session=session, node_ids=nodes)
    digests: dict[str, str] = {}
    if lines:
        digests[NARRATIVE_GUIDANCE_SECTION_KEY] = render_narrative_section(lines)
    if reference is not None:
        card = structure_card_text(reference)
        if card:
            digests[STYLE_STRUCTURE_CARD_KEY] = card
        guidance = str(reference.get("planning_guidance") or "")
        if guidance:
            digests[STYLE_PLANNING_GUIDANCE_KEY] = guidance
    if not digests:
        return None
    refs: dict[str, Any] = {
        "style_reference_runtime_contract_hash": contract_hash,
        "style_narrative_guidance_line_count": len(lines),
    }
    if STYLE_STRUCTURE_CARD_KEY in digests:
        refs["style_structure_card_chars"] = len(digests[STYLE_STRUCTURE_CARD_KEY])
    if STYLE_PLANNING_GUIDANCE_KEY in digests:
        refs["style_planning_guidance_line_count"] = sum(
            1 for line in digests[STYLE_PLANNING_GUIDANCE_KEY].splitlines() if line.startswith("- ")
        )
    return PlanningStyleReference(
        contract_hash=contract_hash,
        digests={key: digests[key] for key in STYLE_REFERENCE_DIGEST_KEYS if key in digests},
        refs=refs,
    )


def register_planning_style_reference(snapshot: dict[str, Any], reference: PlanningStyleReference) -> None:
    """把摘要块登记进来源快照（``source_version_refs`` / ``ordered_injections`` / ``inline_digests``）。

    次序固定为 :data:`STYLE_REFERENCE_DIGEST_KEYS`，``ref_id`` 是契约哈希，``digest_key`` 与
    slot 同名——快照哈希因此随参考设计变化。
    """
    refs = snapshot.setdefault("source_version_refs", {})
    injections = snapshot.setdefault("ordered_injections", [])
    digests = snapshot.setdefault("inline_digests", {})
    refs.update(reference.refs)
    for key, text in reference.digests.items():
        injections.append({"slot": key, "ref_id": reference.contract_hash, "digest_key": key})
        digests[key] = text


def snapshot_has_style_reference(snapshot: Any) -> bool:
    """来源快照是否带任一风格参考块（叙事机制 / 结构画像 / 场景手法）。"""
    if not isinstance(snapshot, Mapping):
        return False
    digests = snapshot.get("inline_digests")
    if not isinstance(digests, Mapping):
        return False
    return any(str(digests.get(key) or "").strip() for key in STYLE_REFERENCE_DIGEST_KEYS)


def style_reference_prompt_blocks(source: Mapping[str, Any] | None) -> list[str]:
    """结构画像 / 场景手法不在 SECTION_SPECS 里，直接渲染进 user prompt（体量已由渲染器封顶）。

    接受 ``{"snapshot": {...}}`` 形式的来源字典，也接受快照本身。
    """
    if not isinstance(source, Mapping):
        return []
    snapshot = source.get("snapshot") if isinstance(source.get("snapshot"), Mapping) else source
    digests = snapshot.get("inline_digests") if isinstance(snapshot, Mapping) else None
    if not isinstance(digests, Mapping):
        return []
    blocks: list[str] = []
    card = str(digests.get(STYLE_STRUCTURE_CARD_KEY) or "").strip()
    if card:
        blocks.extend(["", STRUCTURE_CARD_PROMPT_HEADING, card])
    guidance = str(digests.get(STYLE_PLANNING_GUIDANCE_KEY) or "").strip()
    if guidance:
        blocks.extend(["", PLANNING_GUIDANCE_PROMPT_HEADING, guidance])
    return blocks


__all__ = [
    "CARD_PLANNING_HEADER",
    "CARD_PLANNING_PREFIXES",
    "CHAPTER_TITLE_NODE_IDS",
    "PLANNING_GUIDANCE_PROMPT_HEADING",
    "PLANNING_REFERENCE_TASK_TYPE",
    "PROJECT_PLANNING_NODE_IDS",
    "PlanningStyleReference",
    "SCENE_PLANNING_NODE_IDS",
    "REFERENCE_TITLES_HOW_TO_USE",
    "STRUCTURE_CARD_PROMPT_HEADING",
    "STRUCTURE_REFERENCE_HOW_TO_USE",
    "chapter_titles_for_book",
    "reference_titles_payload",
    "STYLE_PLANNING_GUIDANCE_KEY",
    "STYLE_REFERENCE_DIGEST_KEYS",
    "STYLE_STRUCTURE_CARD_KEY",
    "build_planning_style_reference",
    "register_planning_style_reference",
    "render_card_planning_guidance",
    "render_planning_reference",
    "resolve_project_style_reference",
    "snapshot_has_style_reference",
    "structure_card_text",
    "style_reference_prompt_blocks",
]
