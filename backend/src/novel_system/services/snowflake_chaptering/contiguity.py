"""「章是故事序上连续的一段」——一个检测、一个修法（B07-04：以前四处各写一遍，建议与落库的修法还不一样）。

- 检测 = 沿故事序走，章序出现回退的场（``misplaced_scene_plan_ids`` 看落了库的归属，``misplaced_in_preview``
  看预览里的归属——同一条规则）；
- 修法 = :func:`heal_assignment`：保住章序的最长不降子序列，离群的场并入故事序上前一场的章。
  面板预览（keep_current）、确认写入（``save``）与 AI 分章建议（批准 #17c）都用它，所以看到的就是落库的那一版。
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Any

from novel_system.db.models import SnowflakeChapterPlan, SnowflakeScenePlan


def _misplaced_positions(orders: list[int | None]) -> list[int]:
    """沿故事序走，章序比前面已经到过的章靠前的那些位置（``None`` = 没有归属，跳过）。"""
    reached = -1
    misplaced: list[int] = []
    for position, order in enumerate(orders):
        if order is None:
            continue
        if order < reached:
            misplaced.append(position)
        else:
            reached = order
    return misplaced


def misplaced_scene_plan_ids(
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
) -> list[str]:
    """已保存的分章里，哪些场分在了故事序之外的章（沿故事序走，章序出现回退的那些场）。空 = 每章都是连续的一段。"""
    order = {chapter.chapter_plan_id: index for index, chapter in enumerate(chapters)}
    positions = _misplaced_positions([order.get(scene.chapter_plan_id or "") for scene in scenes])
    return [scenes[position].scene_plan_id for position in positions]


def misplaced_in_preview(chapter_payloads: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """预览里的同一个检测：``(章, 场)``——那一场分在了比故事序上排在它前面的场更靠前的章。"""
    entries = sorted(
        (
            (int(scene.get("story_index") or 0), chapter_index, chapter, scene)
            for chapter_index, chapter in enumerate(chapter_payloads)
            for scene in chapter.get("scenes") or []
            if int(scene.get("story_index") or 0) > 0
        ),
        key=lambda entry: entry[0],
    )
    positions = _misplaced_positions([entry[1] for entry in entries])
    return [(entries[position][2], entries[position][3]) for position in positions]


def heal_assignment(
    assignment: dict[str, str | None],
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
) -> dict[str, str | None]:
    """把一份归属修成「每章都是故事序上连续的一段」，**改动的场尽量少**。

    沿故事序看每场的章序：保住最长的那条不降子序列（这些场的归属本来就是对的），其余的场是离群的——
    作者在 09 里把一场拖到了别的章的范围里、或者旧数据里两章的场交错着——各自并入故事序上前一场所在的章
    （排在全书最前面的离群场并入后一场的章）。只拖了一场，就只有这一场换章；不会像「章序只许不降」的
    简单拉平那样，一场跳到前面，后面整本书都被拽进它原来的那一章。没有归属的场原样留着。
    """
    order = {chapter.row_uid: index for index, chapter in enumerate(chapters)}
    indexed = [
        (position, order[assignment[scene.scene_plan_id]])
        for position, scene in enumerate(scenes)
        if assignment.get(scene.scene_plan_id) in order
    ]
    if not indexed:
        return dict(assignment)
    # 最长不降子序列（patience）：tails[k] = 长度 k+1 的子序列的最小结尾值，links 记前驱以便回溯
    tails: list[int] = []
    tail_at: list[int] = []
    links: list[int] = [-1] * len(indexed)
    for i, (_position, value) in enumerate(indexed):
        k = bisect_right(tails, value)
        if k == len(tails):
            tails.append(value)
            tail_at.append(i)
        else:
            tails[k] = value
            tail_at[k] = i
        links[i] = tail_at[k - 1] if k > 0 else -1
    keep: set[int] = set()
    cursor = tail_at[-1]
    while cursor >= 0:
        keep.add(indexed[cursor][0])
        cursor = links[cursor]

    fixed = dict(assignment)
    kept_positions = sorted(keep)
    first_kept = kept_positions[0]
    previous_uid: str | None = None
    for position, scene in enumerate(scenes):
        uid = assignment.get(scene.scene_plan_id)
        if uid not in order:
            continue
        if position in keep:
            previous_uid = uid
            continue
        fixed[scene.scene_plan_id] = previous_uid or assignment[scenes[first_kept].scene_plan_id]
    return fixed
