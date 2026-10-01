"""新鲜度预算：防本系统复读自己已写成的前几场（B03-19 从 bundle_builder 拆出）。

有绑定且让位（``StylePolicy.defers_house_taste``）时只剩逐字层的近场重复 n-gram；构式 / 语义层清单与房风词表
一概不发。只由虚词与标点组成的 ``avoid_*`` / ``vary_*`` 条目剔除（那是作者的节拍，不是内容模板）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard
from novel_system.services.house_taste_lexicons import FALSE_CLARITY_PHRASES, SUMMARY_ENDING_PHRASES
from novel_system.services.literary_quality import fingerprint_literary_quality
from novel_system.services.planning_queries import current_final_scenes
from novel_system.services.style_policy import UNBOUND, StylePolicy
from novel_system.services.style_reference.style_continuity import contract_deliberate_repetition

_LOGGER = logging.getLogger(__name__)

_FRESHNESS_PRUNE_KEY_PREFIXES: tuple[str, ...] = ("avoid_", "vary_")
_FW_CORE_RE = re.compile(r"[^A-Za-z0-9㐀-鿿]+")


def _function_word_inventory() -> tuple[frozenset[str], int]:
    """(voice_signature 词表全部组的并集, 最长词长)；词表不可用时为空集（不剔除任何条目）。"""
    try:
        from novel_system.services.style_reference.voice_signature import load_voice_lexicon

        words = frozenset(
            word for group in load_voice_lexicon().group_words.values() for word in group if word
        )
    except Exception:  # noqa: BLE001 — 词表损坏不应让 bundle 构建失败
        _LOGGER.warning("function word lexicon unavailable; freshness exemption disabled", exc_info=True)
        return frozenset(), 0
    return words, max((len(word) for word in words), default=0)


def _is_function_word_only(text: str, words: frozenset[str], max_len: int) -> bool:
    """去掉标点 / 空白后能被虚词表完全切分（或什么都不剩）→ True。"""
    core = _FW_CORE_RE.sub("", str(text or ""))
    if not core:
        return True
    if not words:
        return False
    reachable = [False] * (len(core) + 1)
    reachable[0] = True
    for start in range(len(core)):
        if not reachable[start]:
            continue
        for length in range(1, min(max_len, len(core) - start) + 1):
            if core[start : start + length] in words:
                reachable[start + length] = True
    return reachable[len(core)]


def _prune_function_word_only_entries(budget: dict[str, Any]) -> list[str]:
    """就地剔除 ``avoid_*`` / ``vary_*`` 列表里仅由虚词与标点组成的条目；返回被剔除的条目。"""
    words, max_len = _function_word_inventory()
    removed: list[str] = []
    for key, value in list(budget.items()):
        if not isinstance(value, list) or not str(key).startswith(_FRESHNESS_PRUNE_KEY_PREFIXES):
            continue
        kept: list[Any] = []
        for item in value:
            if isinstance(item, str) and _is_function_word_only(item, words, max_len):
                removed.append(item)
                continue
            kept.append(item)
        if len(kept) != len(value):
            budget[key] = kept
    return removed


def literary_freshness_budget(
    session: Session,
    scene: SceneCard,
    policy: StylePolicy = UNBOUND,
    *,
    on_degraded: Callable[[str], None],
) -> dict[str, Any] | None:
    """新鲜度预算：防本系统复读自己已写成的前几场。

    风格参考 v3（L8）：让位（``policy.defers_house_taste()``）时只保留逐字层的防复读（近场重复 n-gram）；
    构式 / 语义层清单（动作模板、意象场、句形、语义复读、全书已用表达）一律不发——这些手法反复出现
    正是参考作者的风格（真实案例：「劣质 + 材质名词」这类像参考作者的比喻被当成「章内已禁用」）。
    策略由 bundle 构建时建好的那份契约给出，不再为新鲜度预算另建一次契约。
    """
    # 同章更早各场的当前正文（重跑过的场只取当前那一版，B03-04）
    earlier_scene_ids = list(
        session.execute(
            select(SceneCard.scene_id)
            .where(
                SceneCard.chapter_id == scene.chapter_id,
                SceneCard.trashed_flag == 0,
                SceneCard.scene_seq < scene.scene_seq,
            )
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars()
    )
    current_finals = current_final_scenes(session, earlier_scene_ids)
    rows = [current_finals[scene_id] for scene_id in earlier_scene_ids if scene_id in current_finals]
    if not rows:
        return None

    source_rows = rows[-3:]
    combined_text = "\n".join(row.content or "" for row in source_rows)
    fingerprint = fingerprint_literary_quality(combined_text)
    action_templates = [
        row["value"]
        for row in fingerprint.get("action_templates", [])
        if int(row.get("count") or 0) >= 2
    ]
    image_fields = [
        row["value"]
        for row in fingerprint.get("image_fields", [])
        if int(row.get("count") or 0) >= 2
    ]
    syntax_shapes = [
        row["value"]
        for row in fingerprint.get("syntax_shapes", [])
        if int(row.get("count") or 0) >= 3
    ]
    style_bound = policy.defers_house_taste()
    preserve_repetition = bool(
        policy.bound and policy.contract is not None and contract_deliberate_repetition(policy.contract)
    )
    budget: dict[str, Any] = {
        "schema_version": "literary_freshness_budget_v1",
        "source_scene_ids": [row.scene_id for row in source_rows],
    }
    if style_bound:
        # 2026-09-14 风格保真修补:动作模板 / 意象场 / 句形三张表是从本系统自己已按作者手笔写成的
        # 前几场里挖出来的——有绑定时它们就是作者的声音,不再当作要避开的东西发给起草。
        # 风格参考 v3(L8):语义复读与全书已用表达(比喻 / 意象 / 开头方式 / 动作口癖 / 情绪惯用语)
        # 也是构式层清单,同样让位;只剩逐字层的近场重复 n-gram。
        budget["house_taste_lists"] = "deferred_to_reference"
        budget["voice_lists"] = "deferred_to_reference"
        budget["construction_lists"] = "deferred_to_reference"
        budget["instruction"] = (
            "Use this as a freshness budget against copying your own earlier scenes word for word only: "
            "do not reuse the recent n-grams listed here verbatim. The reference author's habits, devices, "
            "comparisons, cadence, syntax shapes, image fields, and closing moves are never repetition to avoid."
        )
    else:
        budget["avoid_action_templates"] = action_templates
        budget["avoid_image_fields"] = image_fields[:6]
        budget["vary_syntax_shapes"] = syntax_shapes[:5]
        budget["avoid_false_clarity"] = list(FALSE_CLARITY_PHRASES)
        budget["avoid_summary_endings"] = list(SUMMARY_ENDING_PHRASES)
        budget["instruction"] = (
            "Use this as a freshness budget: do not repeat high-frequency action templates, "
            "rotate image fields, and end on a hard action instead of explanation."
        )
    try:
        from novel_system.services.self_repetition import (
            SelfRepetitionDetector,
            format_semantic_repetition_guidance,
        )

        detector = SelfRepetitionDetector(session)
        repeated_ngrams = detector.top_repeated_ngrams(
            scene.chapter_id, lookback_scenes=6, top_n=8
        )
        if repeated_ngrams:
            budget["avoid_recent_ngrams"] = repeated_ngrams
        corpus_texts, corpus_ids = (
            ([], [])
            if style_bound
            else detector.recent_corpus(scene.scene_id, scene.chapter_id, lookback_scenes=6)
        )
        if corpus_texts:
            from novel_system.services.self_repetition import (
                check_semantic_repetition,
            )

            sem_hits = check_semantic_repetition(
                scene.scene_goal or scene.hook or "",
                corpus_texts,
                corpus_ids,
            )
            if sem_hits:
                budget["semantic_repetition_alert"] = (
                    format_semantic_repetition_guidance(sem_hits)
                )
        # §9 blueprint: whole-book banned expression list (LifetimeExpressionRegistry)
        # 风格参考 v3(L8):让位时整张表不发(它把作者反复用的比喻 / 口头禅当成滥用)。
        if not style_bound:
            from novel_system.services.self_repetition import LifetimeExpressionRegistry

            lifetime_reg = LifetimeExpressionRegistry(session)
            lifetime_guidance = lifetime_reg.get_lifetime_avoidance_guidance(
                scene.project_id
            )
            if lifetime_guidance:
                budget["lifetime_banned_expressions"] = lifetime_guidance
    except Exception:
        on_degraded("literary_freshness_enrichment")
    # v2（规格 §2.W6.3）新鲜度豁免：
    # (a) 仅由虚词（function_words.yaml 全部组）与标点 / 空白组成的 avoid_* / vary_* 条目剔除——
    #     它们是作者节拍而非内容模板，压掉会抹平参考的声音；
    # (b) 任一层画像标记 deliberate_repetition 时加 preserve_reference_repetition 布尔键，
    #     style_draft 模板据此保留参考的刻意复沓。无契约时预算不变。
    _prune_function_word_only_entries(budget)
    if preserve_repetition:
        # 只放布尔标记：这份预算 neutral_draft 也会看到，而「保留参考的刻意复沓」
        # 是语言层的目标文风指令，规格 §1.3 只允许 style_draft 模板解释它
        # （config/prompts.yaml ``style_draft`` 已有对应措辞），不能把措辞写进 instruction。
        budget["preserve_reference_repetition"] = True
    return {
        "source_final_scene_ids": [row.row_id for row in source_rows],
        "budget": budget,
    }
