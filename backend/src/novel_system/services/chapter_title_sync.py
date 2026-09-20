"""章名只有一个（阶段 Z，2026-09-20）。叶子模块：只依赖 ORM——目录服务、分章服务、雪花工作台都引用它。

一章的名字有三扇门：07 章节表、「整理章节结构」面板、章节编排。过去章节编排改的是目录里的**另一份**：
分章面板和 09 的章头还挂着旧名，「AI 起章名」会给作者已经起过名的章再起一遍；反过来 07 里改的章名要等
下一次「确认写入」才到得了目录。现在三扇门改的是同一个名字：

- 章节编排改名 → :func:`adopt_catalog_title`（目录 PATCH 的同一事务里写穿章计划行）；
- 07 保存章表 → :func:`follow_plan_titles`（目录里还是上次播下去的名字就跟着走）；
- 分章面板确认 → ``SnowflakeChapteringService.save`` + 物化（阶段 W 的「目录章名跟随章表」）。

目录那一行的 ``writer_brief_json["chapter_title"]`` 记着「上一次由章表播下去的名字」；两边一致时它就等于
当前章名，之后任何一扇门再改都还跟得上。章表在 07 草稿里的镜像（``chapters`` 与前端写穿缓存
``fe_scaffold.chapters``）由 :func:`mirror_chapters_into_long_synopsis` 统一维护。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from novel_system.db.models import (
    ChapterGoal,
    OperationLog,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    SnowflakeStepRun,
)

# 「未命名」= 章节编排里把章名清空后落下的「未命名章节」：同样不是作者起的名字
PLACEHOLDER_TITLE_MARKERS = ("待补", "TODO", "todo", "TBD", "tbd", "占位", "未命名")
#: 系统起的占位章名「第 N 章」——它跟着章序走，不是作者的命名
AUTO_TITLE_PATTERN = re.compile(r"^第\s*\d+\s*章$")


def is_auto_chapter_title(title: Any) -> bool:
    """章名是不是系统起的占位（空、「第 N 章」、「（待补）」一类）——AI 起章名只碰这些，作者起的名字不碰。"""
    text = str(title or "").strip()
    return (
        not text
        or bool(AUTO_TITLE_PATTERN.match(text))
        or any(marker in text for marker in PLACEHOLDER_TITLE_MARKERS)
    )


def live_chapter_plans(session: Session, project_id: str) -> list[SnowflakeChapterPlan]:
    return list(
        session.execute(
            select(SnowflakeChapterPlan)
            .where(SnowflakeChapterPlan.project_id == project_id, SnowflakeChapterPlan.removed_at.is_(None))
            .order_by(SnowflakeChapterPlan.chapter_seq.asc(), SnowflakeChapterPlan.row_uid.asc())
        ).scalars()
    )


def mirror_chapters_into_long_synopsis(session: Session, project_id: str, chapters: list[SnowflakeChapterPlan]) -> None:
    """把章表写回 07 最新草稿的 ``chapters``（带 row_uid），前端 07 表格与章表行才是同一份。

    草稿里的 ``fe_scaffold.chapters`` 是前端写穿缓存，水合时**优先于**规范字段——只改 ``chapters``
    的话，新浏览器看到的仍是旧章表（真实故障里是两行「（待补）」），下一次 07 上行还会把它们
    当成作者的章表同步回来、把刚确认的分章冲掉。两处一起写。
    """
    run = session.execute(
        select(SnowflakeStepRun)
        .where(
            SnowflakeStepRun.project_id == project_id,
            SnowflakeStepRun.step_key == "long_synopsis",
            SnowflakeStepRun.status != "superseded",
        )
        .order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.created_at.desc())
    ).scalars().first()
    if run is None:
        return
    ordered = sorted(chapters, key=lambda row: (int(row.chapter_seq or 0), row.row_uid))
    draft = dict(run.draft_json or {})
    draft["chapters"] = [
        {
            "row_uid": row.row_uid,
            "chapter_seq": row.chapter_seq,
            "act": row.act,
            "title": row.title or "",
            "summary": row.summary or "",
            "spine": row.spine or "",
            "chapter_goal": row.chapter_goal or "",
        }
        for row in ordered
    ]
    scaffold = draft.get("fe_scaffold")
    if isinstance(scaffold, dict):
        draft["fe_scaffold"] = {
            **scaffold,
            "chapters": [
                {
                    "row_uid": row.row_uid,
                    "id": f"{index:02d}",
                    "act": min(max(int(row.act or 1), 1), 3),
                    "title": row.title or "",
                    "summary": row.summary or "",
                    "spine": row.spine or "",
                    "goal": row.chapter_goal or "",
                }
                for index, row in enumerate(ordered, start=1)
            ],
        }
    run.draft_json = draft
    flag_modified(run, "draft_json")


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
