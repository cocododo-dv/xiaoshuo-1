"""场景的故事序（全书顺序）——唯一真相是 09 场景列表草稿的行序。

为什么要有这个模块（2026-09-18 真实故障：「整理成章节结构」出来的章乱七八糟）：
``SnowflakeScenePlan.scene_seq`` 曾有三个写入方、两种语义——前端 09 的 PATCH 发全书序
``i + 1``，第 10 步同步按 ``chapter_id`` 逐章计数，分章 ``save`` 写章内序；而读取方各按各的假设
排序（分章按 ``(scene_seq, scene_id)`` 当它是全书序，工作台按 ``(chapter_id, scene_seq)`` 当它是
章内序）。第一次分章落库之后 ``scene_seq`` 变成章内序，再点一次「按场景重排章表」读到的就是
1、10、2、11、3、12……两章的场交错洗在一起，整理出来的章自然是乱的。

现在的纪律：
- **故事序只有一个来源**：最新一版（未被取代的）09 草稿里 ``scenes`` 的行序——那就是作者在 09
  看板上看到、拖动的那张表。按 ``row_uid`` 对位，旧数据退回 ``scene_id``。
- ``scene_seq`` **只有一种语义**：这一场在它所在章里的位置（1 起，按故事序）。它不再参与全书排序，
  写入方只剩 :func:`renumber_scene_seq` 一个。
- 章是故事序上**连续的一段**；章内顺序永远等于故事序，不存在第二套「章内手排」。

这是叶子模块（只依赖 ORM 模型），分章、工作台、场景设计上下文都可以引用而不会闭环。
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import SnowflakeChapterPlan, SnowflakeScenePlan, SnowflakeStepRun


def story_positions(session: Session, project_id: str) -> dict[str, int]:
    """``row_uid`` / ``scene_id`` → 在最新 09 草稿里的行号（0 起）。没有 09 草稿或草稿为空时返回空表。"""
    run = session.execute(
        select(SnowflakeStepRun)
        .where(
            SnowflakeStepRun.project_id == project_id,
            SnowflakeStepRun.step_key == "scene_list",
            SnowflakeStepRun.status != "superseded",
        )
        .order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.created_at.desc())
    ).scalars().first()
    if run is None:
        return {}
    return positions_from_rows((run.draft_json or {}).get("scenes"))


def positions_from_rows(rows: Any) -> dict[str, int]:
    positions: dict[str, int] = {}
    for index, item in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(item, dict):
            continue
        for key in (str(item.get("row_uid") or "").strip(), str(item.get("scene_id") or "").strip()):
            if key and key not in positions:
                positions[key] = index
    return positions


def sort_in_story_order(
    session: Session,
    project_id: str,
    plans: Iterable[SnowflakeScenePlan],
    *,
    positions: dict[str, int] | None = None,
) -> list[SnowflakeScenePlan]:
    """按故事序排好的场景计划。

    不在 09 草稿里的行（只经第 10 步 / 旧规划器建出来的场）排在最后，彼此之间按
    （所在章的章序，chapter_id，scene_seq，scene_id）——这也是完全没有 09 草稿的项目
    （v1 规划器骨架）的排序，和过去工作台的口径一致。
    """
    rows = list(plans)
    if not rows:
        return rows
    index = positions if positions is not None else story_positions(session, project_id)
    chapter_seq = _chapter_seq_by_plan_id(session, project_id) if any(
        _position(plan, index) is None for plan in rows
    ) else {}

    def key(plan: SnowflakeScenePlan) -> tuple[int, int, int, str, int, str]:
        position = _position(plan, index)
        if position is not None:
            return (0, position, 0, "", 0, "")
        return (
            1,
            0,
            chapter_seq.get(plan.chapter_plan_id or "", 0),
            str(plan.chapter_id or ""),
            int(plan.scene_seq or 0),
            str(plan.scene_id or ""),
        )

    return sorted(rows, key=key)


def renumber_scene_seq(
    session: Session,
    project_id: str,
    *,
    positions: dict[str, int] | None = None,
) -> None:
    """把每一场的 ``scene_seq`` 重算成「章内位置」（1 起，按故事序）。``scene_seq`` 的唯一写入方。

    分组键是章归属 ``chapter_plan_id``；还没分章的场按 ``chapter_id`` 分组（前端一路全是
    ``…_CH01``，于是就是全书序——与分章之前的老数据一致）。
    """
    plans = session.execute(
        select(SnowflakeScenePlan).where(
            SnowflakeScenePlan.project_id == project_id,
            SnowflakeScenePlan.removed_at.is_(None),
        )
    ).scalars().all()
    counters: dict[str, int] = {}
    for plan in sort_in_story_order(session, project_id, plans, positions=positions):
        group = plan.chapter_plan_id or f"id:{plan.chapter_id or ''}"
        counters[group] = counters.get(group, 0) + 1
        if plan.scene_seq != counters[group]:
            plan.scene_seq = counters[group]


def _position(plan: SnowflakeScenePlan, index: dict[str, int]) -> int | None:
    for key in (str(plan.row_uid or "").strip(), str(plan.scene_id or "").strip()):
        if key and key in index:
            return index[key]
    return None


def _chapter_seq_by_plan_id(session: Session, project_id: str) -> dict[str, int]:
    return {
        row.chapter_plan_id: int(row.chapter_seq or 0)
        for row in session.execute(
            select(SnowflakeChapterPlan).where(
                SnowflakeChapterPlan.project_id == project_id,
                SnowflakeChapterPlan.removed_at.is_(None),
            )
        ).scalars()
    }
