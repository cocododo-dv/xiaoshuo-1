"""分章的纯算法：按场景切章（阶段 K）、三种把场倒进已有章表的分法、重新提议时章的身份沿用（阶段 Y）。

不碰数据库：进来的是按故事序排好的场景计划行与章计划行（或预览里的瞬态章），出去的是归属与切片。
"""

from __future__ import annotations

import math
from typing import Any

from novel_system.db.models import SnowflakeChapterPlan, SnowflakeScenePlan
from novel_system.services.snowflake_chapter_table import SPINE_MARKS, is_auto_chapter_title
from novel_system.services.snowflake_chaptering.spine import scene_spine, spine_positions

#: ``from_scenes`` = 按场景列表推一份章表（不落库的预览，确认时才建章）；``auto`` = 由服务端按现状挑：
#: 已有归属 → keep_current，07 里有作者写的章表 → spine_anchor，否则 → from_scenes。
STRATEGIES = ("spine_anchor", "even", "keep_current", "from_scenes")


def propose_chapter_chunks(
    scenes: list[SnowflakeScenePlan],
    *,
    target_chapter_count: int = 0,
    scenes_per_chapter: int = 3,
) -> list[dict[str, Any]]:
    """把有序的场景切成章（纯函数）。三个灾难场是幕的铰链，各自收束所在的章；
    章数按目标章数（或每章场数）在各幕之间按场数比例分配，每幕至少一章。"""
    ordered = list(scenes)
    if not ordered:
        return []
    marks = spine_positions(ordered)
    # 幕的边界：灾一收束第一幕，灾三收束第二幕；灾二是第二幕内部的铰链（也收束它所在的章）
    boundaries: list[int] = []
    for mark in ("灾一", "灾三"):
        index = marks.get(mark, -1)
        if index >= 0 and (not boundaries or index > boundaries[-1]):
            boundaries.append(index)
    acts: list[tuple[int, list[SnowflakeScenePlan]]] = []
    start = 0
    for act_number, boundary in enumerate(boundaries, start=1):
        acts.append((act_number, ordered[start : boundary + 1]))
        start = boundary + 1
    if start < len(ordered) or not acts:
        acts.append((len(acts) + 1, ordered[start:]))
    acts = [(number, members) for number, members in acts if members]

    total = len(ordered)
    wanted = int(target_chapter_count or 0) or max(1, math.ceil(total / max(1, scenes_per_chapter)))
    # 铰链优先于章数：每幕至少一章；灾二在一幕中间时，那一幕至少两章——它后面的场还要再起一章，
    # 灾二才能收束自己的章。下限按幕给，而不是只抬总数：以前多出来的那一章可能被分给别的幕，
    # 灾二和灾三于是挤在同一章里，章上只剩一个脊柱标记。
    hinge = marks.get("灾二", -1)
    minimums = [
        2 if (0 <= hinge < total and ordered[hinge] in members and members[-1] is not ordered[hinge]) else 1
        for _number, members in acts
    ]
    wanted = max(sum(minimums), min(wanted, total))
    # 余下的章一章一章发给「此刻每章场数最多」的那一幕，保证每章至少一场
    quotas = list(minimums)
    while sum(quotas) < wanted:
        growable = [i for i in range(len(acts)) if quotas[i] < len(acts[i][1])]
        if not growable:
            break
        fullest = max(growable, key=lambda i: (len(acts[i][1]) / quotas[i], -i))
        quotas[fullest] += 1

    chunks: list[dict[str, Any]] = []
    for (act_number, members), quota in zip(acts, quotas):
        pieces = _split_act(members, quota, hinge=hinge, ordered=ordered)
        for piece in pieces:
            # 一章收束在哪个灾难上：取章内**最后**一个标记（灾难是章的收束点）
            spine = next((scene_spine(scene) for scene in reversed(piece) if scene_spine(scene)), "")
            chunks.append({"act": min(3, act_number), "spine": spine, "scenes": piece})
    return chunks


def hinge_min_chapters(scenes: list[SnowflakeScenePlan]) -> int:
    """三个灾难各自收束一章时，最少要几章（面板向作者解释「为什么不是你填的那个数」时用）。"""
    return len(propose_chapter_chunks(scenes, target_chapter_count=1)) if scenes else 0


def _split_act(
    members: list[SnowflakeScenePlan],
    quota: int,
    *,
    hinge: int,
    ordered: list[SnowflakeScenePlan],
) -> list[list[SnowflakeScenePlan]]:
    """把一幕的场均匀切成 quota 章；灾二（若在本幕）必须是它所在章的最后一场。"""
    quota = max(1, min(quota, len(members)))
    hinge_scene = ordered[hinge] if 0 <= hinge < len(ordered) else None
    if hinge_scene is not None and hinge_scene in members and quota >= 2:
        cut = members.index(hinge_scene) + 1
        left, right = members[:cut], members[cut:]
        if right:
            left_quota = max(1, min(len(left), round(quota * len(left) / len(members))))
            right_quota = max(1, min(len(right), quota - left_quota))
            return _even_pieces(left, left_quota) + _even_pieces(right, right_quota)
    return _even_pieces(members, quota)


def _even_pieces(members: list[SnowflakeScenePlan], quota: int) -> list[list[SnowflakeScenePlan]]:
    quota = max(1, min(quota, len(members)))
    pieces: list[list[SnowflakeScenePlan]] = []
    for index in range(quota):
        start = math.floor(index * len(members) / quota)
        end = math.floor((index + 1) * len(members) / quota)
        piece = members[start:end]
        if piece:
            pieces.append(piece)
    return pieces


# ------------------------------------------------------------------ 把场倒进已有章表


def assign(
    strategy: str,
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
) -> dict[str, str | None]:
    """``spine_anchor`` / ``even`` / ``keep_current``：场 → 章的 row_uid（没分到的是 None）。"""
    if strategy == "keep_current":
        return _assign_keep_current(chapters, scenes)
    if strategy == "even":
        return _assign_even(chapters, scenes)
    return _assign_spine_anchor(chapters, scenes)


def _assign_even(
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
) -> dict[str, str | None]:
    """忽略锚点，按顺序均分。章数多于场数时尾部章会空着（预览里会给 empty_chapter 提醒）。"""
    result: dict[str, str | None] = {}
    per = max(1, math.ceil(len(scenes) / max(1, len(chapters))))
    for index, scene in enumerate(scenes):
        result[scene.scene_plan_id] = chapters[min(index // per, len(chapters) - 1)].row_uid
    return result


def _assign_keep_current(
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
) -> dict[str, str | None]:
    """保持已有归属，只给还没分章的场找位置（跟随上一场；没有上一场则归首章）。"""
    by_plan_id = {chapter.chapter_plan_id: chapter.row_uid for chapter in chapters}
    result: dict[str, str | None] = {}
    previous = chapters[0].row_uid if chapters else None
    for scene in scenes:
        current = by_plan_id.get(scene.chapter_plan_id or "")
        target = current or previous
        result[scene.scene_plan_id] = target
        if target:
            previous = target
    return result


def _assign_spine_anchor(
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
) -> dict[str, str | None]:
    """三个灾难是结构铰链：带同一脊柱标记的场与章互相锁定，锚点之间的场均匀铺开。

    只保留场序与章序**同时**单调递增的锚（否则铺展区间会反向，产生乱序的章）。
    """
    anchors: list[tuple[int, int]] = []
    scene_marks = spine_positions(scenes)
    for mark in SPINE_MARKS:
        scene_index = scene_marks.get(mark, -1)
        chapter_index = next((j for j, chapter in enumerate(chapters) if (chapter.spine or "") == mark), -1)
        if scene_index >= 0 and chapter_index >= 0:
            anchors.append((scene_index, chapter_index))
    anchors.sort()
    monotonic: list[tuple[int, int]] = []
    for scene_index, chapter_index in anchors:
        if not monotonic or (scene_index > monotonic[-1][0] and chapter_index > monotonic[-1][1]):
            monotonic.append((scene_index, chapter_index))

    assigned: list[int] = [-1] * len(scenes)
    for scene_index, chapter_index in monotonic:
        assigned[scene_index] = chapter_index

    segments = [(-1, -1), *monotonic, (len(scenes), len(chapters))]
    for start, end in zip(segments, segments[1:]):
        scene_slots = list(range(start[0] + 1, end[0]))
        if not scene_slots:
            continue
        chapter_slots = list(range(start[1] + 1, end[1]))
        if not chapter_slots:
            fallback = start[1] if start[1] >= 0 else min(end[1], len(chapters) - 1)
            for slot in scene_slots:
                assigned[slot] = max(0, fallback)
            continue
        for offset, slot in enumerate(scene_slots):
            position = math.floor(offset * len(chapter_slots) / len(scene_slots))
            assigned[slot] = chapter_slots[min(len(chapter_slots) - 1, position)]

    return {
        scene.scene_plan_id: (chapters[assigned[index]].row_uid if assigned[index] >= 0 else None)
        for index, scene in enumerate(scenes)
    }


# ------------------------------------------------------------------ 重新提议时章的身份（阶段 Y）


def chunk_chapter_fields(index: int, chunk: dict[str, Any]) -> dict[str, Any]:
    """按场景提议出来的一章的默认字段。

    摘要取本章**最后一场**的一句话：07 章表里这一栏问的是「这一章把局面推到哪」，那是章末的事；
    以前取第一场，整章的摘要 / 章目标于是只描述了开头（物化时它还会变成目录里的章目标）。
    标题只给占位「第 N 章」——章名是作者的事，面板里可以直接改。
    """
    last = chunk["scenes"][-1]
    return {
        "title": f"第 {index} 章",
        "act": int(chunk["act"]),
        "spine": str(chunk["spine"] or ""),
        "summary": str(last.summary or last.title or "").strip(),
        "chapter_goal": "",
    }


def proposed_chapter_fields(
    index: int,
    chunk: dict[str, Any],
    match: tuple[SnowflakeChapterPlan, bool] | None,
    scene_summaries: set[str],
) -> dict[str, Any]:
    """按场景提议出来的一章的字段；对上了已有的章就沿用作者写过的东西。

    章名：作者 / AI 起过的留着（占位名「第 N 章」照新的章序重编）；章目标：原样留着；
    章摘要：场完全没变、或是作者自己写的（不与任何一场的摘要逐字相同）才留，否则取新的末场。
    """
    fields = chunk_chapter_fields(index, chunk)
    if match is None:
        return fields
    same, identical = match
    if not is_auto_chapter_title(same.title):
        fields["title"] = same.title
    old_summary = (same.summary or "").strip()
    if old_summary and (identical or old_summary not in scene_summaries):
        fields["summary"] = old_summary
    fields["chapter_goal"] = (same.chapter_goal or "").strip()
    return fields


def match_chunks_to_chapters(
    live: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
    chunks: list[dict[str, Any]],
) -> dict[int, tuple[SnowflakeChapterPlan, bool]]:
    """按场景重新提议的每一段，是不是已有的某一章？返回 ``{段下标: (那一章, 场是否完全相同)}``。

    章的身份 = 章计划这一行 = 它钉着的目录章（章名、书签、章级状态、戏剧卡都挂在那一行上）。两条规则，
    都和面板上的手势同一个口径（「从这里另起一章」是前半截留着原章，「并入上一章」是上一章留着）：

    1. **内容过半**：这一段与那一章的公共场不少于两边各自的一半 → 同一章。恰好对半时（一章从正中拆开、
       两章等长地并成一章）归故事序上靠前的那一个。沿故事序逐段认领，一段取公共场最多的候选，并列取靠前的章。
    2. **整拆 / 整并**（第 1 条没说话的时候才用）：一章被整个拆成几小段、或几章整个并成一段，谁都不过半——
       身份归**开头对得上**的那一个：这一段的第一场就是那一章的第一场，并且一方整个包在另一方里。
       不这样的话，一章 7 场拆成 2 / 2 / 3，原来那一行会整个进回收站，作者起的章名和戏剧卡跟着不见了。

    一章只被认领一次。``live`` 按章序、``scenes`` / ``chunks`` 按故事序给：同一份章表、同一个故事序，
    永远得出同一种对法。
    """
    members: dict[str, set[str]] = {}
    first_scene_of: dict[str, str] = {}
    owner_of: dict[str, str] = {}
    for scene in scenes:
        if scene.chapter_plan_id:
            members.setdefault(scene.chapter_plan_id, set()).add(scene.scene_plan_id)
            first_scene_of.setdefault(scene.chapter_plan_id, scene.scene_plan_id)
            owner_of[scene.scene_plan_id] = scene.chapter_plan_id
    by_id = {chapter.chapter_plan_id: chapter for chapter in live}
    claimed: set[str] = set()
    matched: dict[int, tuple[SnowflakeChapterPlan, bool]] = {}
    for index, chunk in enumerate(chunks):
        chunk_ids = {scene.scene_plan_id for scene in chunk["scenes"]}
        best: tuple[SnowflakeChapterPlan, int, bool] | None = None
        for chapter in live:
            if chapter.chapter_plan_id in claimed:
                continue
            old_ids = members.get(chapter.chapter_plan_id) or set()
            shared = len(old_ids & chunk_ids)
            if not shared or shared * 2 < len(old_ids) or shared * 2 < len(chunk_ids):
                continue
            if best is None or shared > best[1]:
                best = (chapter, shared, old_ids == chunk_ids)
        if best is not None:
            claimed.add(best[0].chapter_plan_id)
            matched[index] = (best[0], best[2])
    for index, chunk in enumerate(chunks):
        if index in matched or not chunk["scenes"]:
            continue
        opening = chunk["scenes"][0].scene_plan_id
        owner = by_id.get(owner_of.get(opening, ""))
        if owner is None or owner.chapter_plan_id in claimed or first_scene_of.get(owner.chapter_plan_id) != opening:
            continue
        chunk_ids = {scene.scene_plan_id for scene in chunk["scenes"]}
        old_ids = members[owner.chapter_plan_id]
        if chunk_ids <= old_ids or old_ids <= chunk_ids:
            claimed.add(owner.chapter_plan_id)
            matched[index] = (owner, False)
    return matched


def clip(text: Any, limit: int = 24) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else f"{value[:limit]}…"
