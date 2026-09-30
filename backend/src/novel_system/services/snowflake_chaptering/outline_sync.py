"""07 长篇大纲里显式给出的章表 → 构思侧章表行（P2）。

身份锚是 ``row_uid``，规则与场景计划一致：改标题、改幕、重排都不重建行，已经分好的场景归属
（``SnowflakeScenePlan.chapter_plan_id``）不会因为一次改标题就断掉；删掉的章软删，分在里面的场退回「未分章」。
写行走 ``snowflake_chapter_table`` 里与分章面板同一个 upsert（B07-05）。

2026-09-30（R11，批准 #18a）起 07 的章表是分章结果的只读镜像：07 保存不带章表时 ``update_step`` 沿用存着的那一份、
不走这里；07 重新生成时有章表行就保留现表。走到这里的只剩显式给了章表的保存——API 调用方，以及前端改成不上行章表
之前的前端 07 上行。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from novel_system.db.models import SnowflakeScenePlan, SnowflakeStepRun
from novel_system.services.chapter_title_sync import follow_plan_titles
from novel_system.services.snowflake_chapter_table import (
    parse_outline_chapters,
    soft_delete_unlisted_chapters,
    upsert_chapter_rows,
)


def sync_long_synopsis_chapters(
    session: Session,
    project_id: str,
    draft: dict[str, Any],
    run: SnowflakeStepRun,
    *,
    approved: bool,
) -> dict[str, Any] | None:
    """把 07 草稿的章表落成章表行，返回「作者必须知道」的章表收缩摘要（没有则 None）。

    章表**收缩**（新表比旧表短，尾部的章连同它的场景归属一起没了）是作者必须知道的事：返回一份摘要，
    由调用方挂进健康度 ``generation_notice``——绝不静默报「已生成」。
    """
    incoming = parse_outline_chapters(draft)
    if not incoming:
        return None  # 空草稿不收口——同 P1-3 的护栏，一次空 PATCH 不能清掉全书的章
    # 07 的章目标一栏空着 = 不改：分章面板写过的章目标不被一次空白覆盖
    rows = [{key: value for key, value in item.items() if key != "chapter_goal" or value} for item in incoming]
    table = upsert_chapter_rows(
        session,
        project_id,
        rows,
        adopt_unknown=True,
        status="approved" if approved else "draft",
        source_step_run_id=run.step_run_id,
    )
    removed = soft_delete_unlisted_chapters(
        session,
        project_id,
        list(table.by_uid.values()),
        table.listed,
        session.execute(select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)).scalars().all(),
    )

    # 把铸好的 row_uid 回写进草稿，让下一次保存和前端水合都拿到同一个锚
    for item, row in zip(incoming, rows):
        item["row_uid"] = row["row_uid"]
    if table.rewritten and isinstance(run.draft_json, dict):
        run.draft_json = {**run.draft_json, "chapters": incoming}
        flag_modified(run, "draft_json")

    # 阶段 Z「章名只有一个」：07 里改的章名不必等下一次「确认写入」——09 的章头（场景行上的章名戳）当场跟上，
    # 目录里那一章（名字还是上次由章表播下去的）跟作者起的名字；系统起的「第 N 章」等确认写入（follow_plan_titles）。
    session.flush()
    follow_plan_titles(session, project_id)

    loosened = sum(item.unbound_scene_count for item in removed)
    if not loosened:
        return None  # 没有场因此松绑 = 纯粹的章表编辑，不必打扰作者
    dropped = [
        {"title": item.row.title or item.row.row_uid, "unbound_scene_count": item.unbound_scene_count} for item in removed
    ]
    titles = "、".join(item["title"] for item in dropped if item["unbound_scene_count"])[:120]
    return {
        "code": "CHAPTER_PLAN_SHRUNK",
        "severity": "warning",
        "message": (
            f"章表变短了：{titles} 已从章表消失，其中 {loosened} 场退回「未分章」。"
            "请到分章面板重新指派，否则它们不会进入章节目录。"
        ),
        "dropped_chapters": dropped,
        "unbound_scene_count": loosened,
    }
