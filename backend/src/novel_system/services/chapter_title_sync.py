"""章名只有一个（阶段 Z，2026-09-20）。叶子模块：只依赖 ORM 与章表叶子——目录服务、分章服务、雪花工作台都引用它。

一章的名字有两扇门：「整理章节结构」面板、章节编排；07 的章节表是分章结果的只读镜像（R11，2026-09-30——
以前它是第三扇门）。过去章节编排改的是目录里的**另一份**：分章面板和 09 的章头还挂着旧名，「AI 起章名」会给
作者已经起过名的章再起一遍。现在两扇门改的是同一个名字：

- 章节编排改名 → :func:`adopt_catalog_title`（目录 PATCH 的同一事务里写穿章计划行）；
- 分章面板「只保存章表」→ ``SnowflakeWorkspaceService.save_chapter_plan`` → :func:`follow_plan_titles`（目录里还是
  上次播下去的名字就跟着走；API 调用方显式给 07 的章表时同一条路）；
- 分章面板确认写入 → ``SnowflakeChapteringService.save`` + 物化（阶段 W 的「目录章名跟随章表」）。

目录那一行的 ``writer_brief_json["chapter_title"]`` 记着「上一次由章表播下去的名字」；两边一致时它就等于
当前章名，之后任何一扇门再改都还跟得上。章表在 07 草稿里的镜像（``chapters``）由
:func:`mirror_chapters_into_long_synopsis`（实现在 ``snowflake_chapter_table``）统一维护。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, OperationLog, SnowflakeChapterPlan, SnowflakeScenePlan

# 章名规则、活章表与 07 镜像的实现在章表叶子 snowflake_chapter_table（B07-05）；目录服务照旧从这里 import。
from novel_system.services.snowflake_chapter_table import (  # noqa: F401
    AUTO_TITLE_PATTERN,
    PLACEHOLDER_TITLE_MARKERS,
    is_auto_chapter_title,
    live_chapter_plans,
    mirror_chapters_into_long_synopsis,
)


def adopt_catalog_title(
    session: Session,
    project_id: str,
    catalog_chapter_id: str,
    title: str,
    *,
    actor_ref: str = "operator",
) -> dict[str, Any] | None:
    """作者在章节编排里给一章改了名：构思侧的章计划接过同一个名字。没有章计划钉着这一章时返回 ``None``。"""
    chapters = live_chapter_plans(session, project_id)
    chapter = next((row for row in chapters if str(row.catalog_chapter_id or "") == catalog_chapter_id), None)
    if chapter is None:
        return None
    text = str(title or "").strip()
    previous = str(chapter.title or "").strip()
    if text != previous:
        chapter.title = text
        _restamp_chapter_title(session, project_id, chapter)
        mirror_chapters_into_long_synopsis(session, project_id, chapters)
        session.add(
            OperationLog(
                event_type="snowflake_chapter_title_adopted",
                object_type="snowflake_chapter_plan",
                object_ref=chapter.chapter_plan_id,
                payload_json={
                    "project_id": project_id,
                    "row_uid": chapter.row_uid,
                    "catalog_chapter_id": catalog_chapter_id,
                    "from": previous,
                    "to": text,
                    "source": "catalog",
                    "actor_ref": actor_ref or "operator",
                },
            )
        )
    _seed_catalog_title(session, catalog_chapter_id, text)
    session.flush()
    return {"row_uid": chapter.row_uid, "title": text, "changed": text != previous}


def follow_plan_titles(session: Session, project_id: str) -> list[str]:
    """章计划行的章名变了（07 保存章表）：绑在上面的场重盖章名戳，目录里那一章跟着改名。

    目录只在「现在的章名还是上一次由章表播下去的那个」时才跟——和重新物化同一条规矩；两边本来就一致
    （含章节编排改名写穿之后）时这永远成立。返回改了名的目录章 id。
    """
    followed: list[str] = []
    for chapter in live_chapter_plans(session, project_id):
        _restamp_chapter_title(session, project_id, chapter)
        chapter_id = str(chapter.catalog_chapter_id or "").strip()
        title = str(chapter.title or "").strip()
        row = session.get(ChapterGoal, chapter_id) if chapter_id else None
        if row is None or row.project_id != project_id or not title:
            continue
        narrative = dict(row.narrative_json or {})
        current = str(narrative.get("title") or "").strip()
        seeded = str(dict(row.writer_brief_json or {}).get("chapter_title") or "").strip()
        if current == title or (current and current != seeded):
            continue
        row.narrative_json = {**narrative, "title": title}
        _seed_catalog_title(session, chapter_id, title)
        followed.append(chapter_id)
    if followed:
        session.flush()
    return followed


def _restamp_chapter_title(session: Session, project_id: str, chapter: SnowflakeChapterPlan) -> None:
    """场景行上冗余的章名戳（09 的章头读它）跟上章计划行。"""
    title = str(chapter.title or "")
    for plan in session.execute(
        select(SnowflakeScenePlan).where(
            SnowflakeScenePlan.project_id == project_id,
            SnowflakeScenePlan.chapter_plan_id == chapter.chapter_plan_id,
            SnowflakeScenePlan.removed_at.is_(None),
        )
    ).scalars():
        if (plan.chapter_title or "") != title:
            plan.chapter_title = title


def _seed_catalog_title(session: Session, catalog_chapter_id: str, title: str) -> None:
    row = session.get(ChapterGoal, catalog_chapter_id)
    if row is None:
        return
    brief = dict(row.writer_brief_json or {})
    if brief.get("chapter_title") != title:
        row.writer_brief_json = {**brief, "chapter_title": title}
