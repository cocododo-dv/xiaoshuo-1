"""场景卡硬约束（必写 / 禁用）的字面读法：起草的验收门、运行前预检、分类器复核与成稿门同一处。

一场要按字面查的禁用词有两处来源（:func:`literal_forbidden_term_sources`）：场景卡 ``forbidden_text``，和本章戏剧卡的
「禁止包含」（``chapter_drama.chapter_forbidden_text``，round 2 · B2-5——卡片一直说它会拦，以前却没有哪一道检查读它）。
两处各按各的契约拆：场景卡是「顿号 / 逗号分隔的禁用词」（:func:`forbidden_terms`），戏剧卡是「一行一条」
（:func:`chapter_forbidden_terms`——「别让林昭和她和好，暂时」是一条，不是两个词）；两边都认 ``A|B`` 备选写法、
不到两个字的不算、防抄袭政策句剔掉。戏剧卡那一格是空的时候，各处的读数与只看场景卡时逐字相同。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import inspect as sa_inspect

from novel_system.db.models import ChapterGoal
from novel_system.services.chapter_drama import chapter_forbidden_text


def constraint_terms(text: str) -> list[str]:
    return [term.strip() for term in re.split(r"[,，、;；\n]+", text) if len(term.strip()) >= 2]


#: 防抄袭**政策句**。雪花物化 / v1 规划器曾把它写进每一张场景卡的 ``forbidden_text``——可那个字段的契约是
#: 「要按字面查的禁用词，顿号 / 逗号分隔」：这句话于是被拆成 ``不得复制参考书原文表达`` / ``人物`` /
#: ``设定或桥段。`` 三个「禁用词」，正文里只要出现「人物」两个字（这号人物、可疑人物、大人物……）就是一条
#: 已证实的 Q1 硬伤，不能归档（2026-09-20 在真实作品的 17 张卡上查实）。防抄袭由专门的机制负责
#: （参考书 n-gram 查重、受保护专名、风格注入里的红线段），不靠这个字段。物化不再写它；已经带着它的卡，
#: 所有按字面读这个字段的地方都先把它剔掉。
REFERENCE_POLICY_SENTENCES: tuple[str, ...] = (
    "不得复制参考书原文表达、人物、设定或桥段。",
    "不得复制参考书原文表达、人物、设定或桥段",
)


def strip_reference_policy(text: Any) -> str:
    """``forbidden_text`` 去掉防抄袭政策句之后剩下的、作者真正写的禁用词；没有就是空串。"""
    if not isinstance(text, str):
        return ""
    for sentence in REFERENCE_POLICY_SENTENCES:
        text = text.replace(sentence, " ")
    return text.strip()


def forbidden_terms(text: Any) -> list[str]:
    """场景卡 ``forbidden_text`` → 要按字面查的禁用词（政策句不是禁用词表）。"""
    return constraint_terms(strip_reference_policy(text))


def chapter_forbidden_terms(text: Any) -> list[str]:
    """戏剧卡「禁止包含」→ 要按字面查的写法：一行一条（整行就是一条，不再按逗号 / 顿号拆），不到两个字的行不算，
    防抄袭政策句剔掉；一行里的 ``A|B`` 照旧是备选写法（见 :func:`forbidden_term_hits`）。"""
    return [line.strip() for line in strip_reference_policy(text).splitlines() if len(line.strip()) >= 2]


#: 禁用词的两处来源（预检冲突、成稿门证据按它归因）
SCENE_FORBIDDEN_SOURCE = "scene_card.forbidden_text"
CHAPTER_FORBIDDEN_SOURCE = "chapter.drama.forbidden"


def _attached_chapter(scene: Any) -> Any:
    """场所在会话里的本章（按 ``scene.chapter_id``）；手搭的、不在会话里的卡没有。"""
    chapter_id = getattr(scene, "chapter_id", None)
    state = sa_inspect(scene, raiseerr=False)
    session = getattr(state, "session", None) if state is not None else None
    if session is None or not chapter_id:
        return None
    with session.no_autoflush:  # 闸门读数不该顺手把会话里没写完的改动冲进库
        return session.get(ChapterGoal, chapter_id)


def literal_forbidden_sources(scene: Any, chapter: Any = None) -> list[tuple[str, str]]:
    """这一场要按字面查的禁用词从哪来：``[(来源, 原文)]``——场景卡 ``forbidden_text``（防抄袭政策句剔掉）在前，
    本章戏剧卡的「禁止包含」在后；空的不列。``chapter`` 不给时按场所在的会话找本章。"""
    sources: list[tuple[str, str]] = []
    own = strip_reference_policy(getattr(scene, "forbidden_text", None))
    if own:
        sources.append((SCENE_FORBIDDEN_SOURCE, own))
    chapter_text = chapter_forbidden_text(chapter if chapter is not None else _attached_chapter(scene))
    if chapter_text:
        sources.append((CHAPTER_FORBIDDEN_SOURCE, chapter_text))
    return sources


def literal_forbidden_term_sources(scene: Any, chapter: Any = None) -> list[tuple[str, list[str]]]:
    """``[(来源, 禁用词表)]``：每处来源按它自己的契约拆（场景卡 :func:`forbidden_terms`，戏剧卡
    :func:`chapter_forbidden_terms`）；戏剧卡里与场景卡重复的写法不再记一次，拆不出词的来源不列。
    词表、命中与预检冲突都从这里读。"""
    sources: list[tuple[str, list[str]]] = []
    seen: list[str] = []
    for source, text in literal_forbidden_sources(scene, chapter):
        terms = forbidden_terms(text) if source == SCENE_FORBIDDEN_SOURCE else chapter_forbidden_terms(text)
        if seen:
            terms = [term for term in terms if term not in seen]
        if terms:
            sources.append((source, terms))
            seen.extend(terms)
    return sources


def literal_forbidden_terms(scene: Any, chapter: Any = None) -> list[str]:
    """两处来源合起来的禁用词表（按来源次序）。"""
    return [term for _source, terms in literal_forbidden_term_sources(scene, chapter) for term in terms]


def literal_forbidden_hits(scene: Any, content: str, chapter: Any = None) -> list[tuple[str, str]]:
    """``content`` 里按字面出现的禁用写法：``[(来源, 写法)]``（按来源次序、去重；``A|B`` 报出现的那一个）。"""
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, terms in literal_forbidden_term_sources(scene, chapter):
        for term in terms:
            for alternative in forbidden_term_hits(term, content):
                if alternative not in seen:
                    seen.add(alternative)
                    hits.append((source, alternative))
    return hits


def forbidden_authority_ref(scene: Any, source: str) -> str:
    """禁用词证据指向的那一处：场景卡的 ``forbidden_text``，或本章戏剧卡的「禁止包含」。"""
    if source == CHAPTER_FORBIDDEN_SOURCE:
        return f"chapter:{getattr(scene, 'chapter_id', '')}.drama.forbidden"
    return f"scene_card:{getattr(scene, 'scene_id', '')}.forbidden_text"


def constraint_alternatives(term: str) -> list[str]:
    """拆分一个约束的等价写法；``A|B`` 表示满足任意一项即可。"""
    return [
        item.strip()
        for item in re.split(r"[|｜]+", str(term or ""))
        if len(item.strip()) >= 2
    ]


def named_scene_card_sources(
    scene: Any, fields: tuple[str, ...]
) -> list[tuple[str, str]]:
    """按给定字段顺序枚举场景卡的非空文本源，`beats_json` 恒附于末尾。

    字段顺序即归因优先级（调用方取首个命中源），故各调用方自带字段元组，
    不得在此固化某一侧的顺序。
    """
    sources: list[tuple[str, str]] = []
    for field in fields:
        value = getattr(scene, field, None)
        if isinstance(value, str) and value.strip():
            sources.append((f"scene_card.{field}", value))
    beats = getattr(scene, "beats_json", None)
    for index, beat in enumerate(beats if isinstance(beats, list) else []):
        if isinstance(beat, str) and beat.strip():
            sources.append((f"scene_card.beats_json[{index}]", beat))
    return sources


def forbidden_term_hits(term: str, content: str) -> list[str]:
    """一条禁用写法在 ``content`` 里按字面出现的写法：``A|B`` 报真正出现的那一个（或几个），不是整条。"""
    return [alternative for alternative in constraint_alternatives(term) or [term] if alternative in content]


def forbidden_term_present(term: str, content: str) -> bool:
    """一条（已经拆好的）禁用写法有没有出现在 ``content`` 里——不再拆它，戏剧卡的整行照整行查。"""
    return bool(forbidden_term_hits(term, content))


def forbidden_hits(forbidden_text: Any, content: str) -> list[str]:
    """``forbidden_text`` 里在 ``content`` 中按字面出现的禁用写法（按词表顺序、去重）。

    ``A|B`` 报的是真正出现的那个写法（``雨伞``），不是整条 ``黑伞|雨伞``——证据要能在正文里找到。
    防抄袭政策句不是禁用词表（见 :func:`forbidden_terms`）。"""
    if not isinstance(forbidden_text, str) or not forbidden_text.strip():
        return []
    hits: list[str] = []
    for term in forbidden_terms(forbidden_text):
        for alternative in forbidden_term_hits(term, content):
            if alternative not in hits:
                hits.append(alternative)
    return hits


def significant_fragments(text: str) -> list[str]:
    fragments: list[str] = []
    fragments.extend(match.lower() for match in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", text))
    for sequence in re.findall(r"[\u4e00-\u9fff]{3,}", text):
        fragments.extend(sequence[index : index + 3] for index in range(0, max(len(sequence) - 2, 0)))
    seen: set[str] = set()
    unique: list[str] = []
    for fragment in fragments:
        if fragment in seen:
            continue
        seen.add(fragment)
        unique.append(fragment)
    return unique


def source_field_satisfied(source_text: str, content: str) -> bool:
    alternatives = constraint_alternatives(source_text) or [source_text.strip()]
    for alternative in alternatives:
        if alternative in content:
            return True
        fragments = significant_fragments(alternative)
        if not fragments:
            continue
        matched = [fragment for fragment in fragments if fragment in content]
        if len(matched) >= min(2, len(fragments)):
            return True
    return False


def required_groups(must_include_text: Any) -> list[str]:
    """``must_include_text`` 分出来的必写组（:func:`constraint_terms`）；一整段分不出 ≥2 字的组时整段算一组。"""
    if not isinstance(must_include_text, str) or not must_include_text.strip():
        return []
    return constraint_terms(must_include_text) or [must_include_text.strip()]


def required_groups_missing(must_include_text: Any, content: str) -> list[str]:
    """``must_include_text`` 里在 ``content`` 中没有落实的组（:func:`required_groups`，组内 ``A|B`` 任一即可、
    容忍改写——同 :func:`source_field_satisfied`）。

    起草、硬质检、分类器复核与成稿门都按组查（批准#11）：整段只要沾上两个三字片段就算满足的旧口径，会放过一整组
    没写的正文（「主角交出钥匙，门外传来警笛」只写了前一半也算满足）。"""
    return [group for group in required_groups(must_include_text) if not source_field_satisfied(group, content)]


def issue_mentions_source(issue_blob: str, source_text: str) -> bool:
    source_text = source_text.strip()
    if source_text and source_text in issue_blob:
        return True
    return any(fragment in issue_blob for fragment in significant_fragments(source_text))
