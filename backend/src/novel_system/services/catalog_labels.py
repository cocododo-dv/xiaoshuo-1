"""目录的纯标签助手——叶子模块，只读 ORM 行的属性，不查库、不依赖任何服务。

章名 / 场景题名 / 幕 / 场景形态 / 「现在该写哪一场」在目录、回收站、章节规划、主页、雪花工作区里都要用。
过去它们住在 ``catalog.py`` 里，而 ``catalog.py`` 又导入 ``projects``——为了一个章名，回收站和雪花工作区
都得连带导入章节运行、系统配置、提示词构建（B08-23）。``catalog`` 照旧把这些名字原样再导出。
"""
from __future__ import annotations

import re
from typing import Any

from novel_system.db.models import ChapterGoal, SceneCard

SCENE_BRIEF_GCS = ("goal", "conflict", "setback")
SCENE_BRIEF_RDD = ("reaction", "dilemma", "decision")

#: 目录侧的幕（章节编排按它分卷）
CATALOG_ACTS = ("act1", "act2", "act3")
#: 目录里显示用的场景短题上限（整句摘要另有 ``summary``）
SCENE_TITLE_MAX_CHARS = 18
_ACT_DIGIT = re.compile(r"[123]")
_ACT_CN = {"一": "act1", "二": "act2", "三": "act3"}
_TITLE_CLAUSE_BREAK = re.compile(r"[，。；：！？,;:!?\n]")


def normalize_act(value: Any) -> str:
    """目录侧的幕只有 ``act1`` / ``act2`` / ``act3``。

    雪花物化曾把幕写成整数 1 / 2 / 3，而章节编排按 ``act === "act1"`` 分卷——整数幕的章在看板和
    章节序列里**一张都不显示**（2026-09-19 真实故障：目录 6 章，编排台只看得见手建的那一章）。
    读取时统一归一，写入方也已改成字符串；认不出的值落到第一幕，绝不让一章从看板上消失。
    """
    text = str(value if value is not None else "").strip().lower()
    if text in CATALOG_ACTS:
        return text
    digit = _ACT_DIGIT.search(text)
    if digit:
        return f"act{digit.group(0)}"
    for char, act in _ACT_CN.items():
        if char in text:
            return act
    return "act1"


def short_scene_title(text: Any) -> str:
    """整句摘要 → 列表里放得下的短题：够短就原样；否则取第一个分句；分句也太长就截断加省略号。"""
    value = " ".join(str(text or "").split())
    if len(value) <= SCENE_TITLE_MAX_CHARS:
        return value
    head = _TITLE_CLAUSE_BREAK.split(value, 1)[0].strip()
    if 4 <= len(head) <= SCENE_TITLE_MAX_CHARS:
        return head
    base = head if len(head) > SCENE_TITLE_MAX_CHARS else value
    return f"{base[: SCENE_TITLE_MAX_CHARS - 1].rstrip()}…"


def scene_kind(scene: SceneCard) -> str:
    brief = dict(scene.writer_brief_json or {})
    raw = str(brief.get("primary_form") or scene.scene_type or "proactive").strip().lower()
    return "reactive" if raw.startswith("react") or raw == "反应" else "proactive"


def chapter_title(chapter: ChapterGoal) -> str:
    narrative = dict(chapter.narrative_json or {})
    if str(narrative.get("title") or "").strip():
        return str(narrative["title"]).strip()
    brief = dict(chapter.writer_brief_json or {})
    for key in ("chapter_title", "title"):
        if str(brief.get(key) or "").strip():
            return str(brief[key]).strip()
    goal = str(chapter.chapter_goal or "").strip()
    return (goal.splitlines()[0][:24] if goal else "") or chapter.chapter_id


def scene_title(scene: SceneCard) -> str:
    brief = dict(scene.writer_brief_json or {})
    if str(brief.get("title") or "").strip():
        return str(brief["title"]).strip()
    return str(scene.scene_goal or "").strip() or scene.scene_id


def scene_display_title(scene: SceneCard) -> str:
    """目录载荷里的场景题名：作者 / 构思起的题名原样用；没有题名时从摘要里取一个短题。

    雪花场景卡的 ``scene_goal`` 是 09 的整句摘要（六七十字）——拿它当题名，大纲、队列、命令面板
    每一行都是一整段话。整句另以 ``summary`` 给出，:func:`scene_title` 的口径（规划上下文、回收站）不变。
    """
    brief = dict(scene.writer_brief_json or {})
    if str(brief.get("title") or "").strip():
        return str(brief["title"]).strip()
    return short_scene_title(scene.scene_goal) or scene.scene_id


def focus_scene_payload(scenes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """一章里「现在该写哪一场」：在写的那一场 → 第一场没写完的 → 最后一场。

    主页的「继续写作」、写作台的落点、AI 起草台的落点共用这一条规则（前端 ``WsCatalog.focusScene``
    是它的镜像）。过去三处各有各的规则：主页取章里最后一场，写作台取全书任何一场「在写」的场，
    于是雪花刚物化完，主页指着第 5 场、写作台却开在一张手建的空白占位场上。
    """
    if not scenes:
        return None
    for wanted in ("writing",):
        hit = next((scene for scene in scenes if scene.get("state") == wanted), None)
        if hit is not None:
            return hit
    pending = next((scene for scene in scenes if scene.get("state") != "done"), None)
    return pending if pending is not None else scenes[-1]
