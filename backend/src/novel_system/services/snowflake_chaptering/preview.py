"""分章预览：只读推演（分章方案不写库），每种策略都确定性可复算；面板打开时由 ``auto`` 挑最诚实的那一种。

一次预览只读一遍场景计划与章计划，往下传（B07-19：以前 ``_auto_strategy``、自愈检查、成形、提醒、章表现状
各查一遍，节奏体检算两遍）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import SnowflakeChapterPlan, SnowflakeScenePlan
from novel_system.services.errors import DomainError
from novel_system.services.snowflake_chapter_table import (
    NEW_CHAPTER_PREFIX,
    catalog_chapter_id,
    is_placeholder_chapter,
    live_chapter_plans,
)
from novel_system.services.snowflake_chaptering.algorithms import (
    STRATEGIES,
    assign,
    match_chunks_to_chapters,
    propose_chapter_chunks,
    proposed_chapter_fields,
)
from novel_system.services.snowflake_chaptering.contiguity import heal_assignment, misplaced_scene_plan_ids
from novel_system.services.snowflake_chaptering.derive import ensure_chapter_plans
from novel_system.services.snowflake_chaptering.orphans import orphaned_payload
from novel_system.services.snowflake_chaptering.rhythm import rhythm_report
from novel_system.services.snowflake_chaptering.scale import chapter_scale
from novel_system.services.snowflake_chaptering.spine import scene_spine
from novel_system.services.snowflake_chaptering.warnings import preview_warnings
from novel_system.services.snowflake_scene_order import live_scene_plans_in_story_order
from novel_system.services.snowflake_steps import effective_rendering_mode
from novel_system.services.snowflake_triage import excluded_scene_plan_ids

_SCENES_REQUIRED_MESSAGE = "09 场景列表还没有场景，无法按场景提议章表。"


def preview(session: Session, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = payload or {}
    strategy = str(body.get("strategy") or "spine_anchor").strip()
    scenes = live_scene_plans_in_story_order(session, project_id)
    healed_from_saved = False
    if strategy == "auto":
        chapters = ensure_chapter_plans(session, project_id)
        strategy = auto_strategy(chapters, scenes)
        healed_from_saved = strategy == "from_scenes" and bool(misplaced_scene_plan_ids(chapters, scenes))
    if strategy not in STRATEGIES:
        raise DomainError(
            "SNOWFLAKE_CHAPTER_STRATEGY_INVALID",
            f"分章策略只能是 {'、'.join((*STRATEGIES, 'auto'))} 之一。",
            status_code=400,
        )
    if strategy == "from_scenes":
        shaped = preview_from_scenes(session, project_id, body, scenes)
        if healed_from_saved:
            shaped.setdefault("warnings", []).insert(
                0,
                {
                    "kind": "chapter_order_healed",
                    "severity": "advisory",
                    "message": (
                        "上一次保存的分章里，有的章装着故事序上不相邻的场（章必须是场景列表上连续的一段）。"
                        "这里已经按场景列表重新提议了一版；确认写入后替换旧的分章，目录里的场景卡会跟着搬到新章。"
                    ),
                },
            )
        return shaped
    chapters = ensure_chapter_plans(session, project_id)
    if not chapters:
        raise DomainError(
            "SNOWFLAKE_CHAPTER_PLAN_EMPTY",
            "还没有章表：先按场景提议一版分章（章是列完场之后的包装决定）。",
            status_code=409,
            details={"step_key": "long_synopsis"},
        )
    if not scenes:
        raise DomainError(
            "SNOWFLAKE_SCENES_REQUIRED",
            "09 场景列表还没有场景，无法分章。",
            status_code=409,
            details={"step_key": "scene_list"},
        )

    assignment = assign(strategy, chapters, scenes)
    healed_ids: list[str] = []
    if strategy == "keep_current":
        # 面板摆出来的必须就是「确认写入」会落库的那一版：保存时非连续的归属会被并回相邻的章，这里先并给作者看。
        fixed = heal_assignment(assignment, chapters, scenes)
        healed_ids = [key for key, value in fixed.items() if value != assignment.get(key)]
        assignment = fixed
    shaped = shape_preview(session, project_id, strategy, chapters, scenes, assignment)
    if healed_ids:
        shaped.setdefault("warnings", []).insert(
            0,
            {
                "kind": "chapter_order_healed",
                "severity": "advisory",
                "message": (
                    f"已保存的分章里有 {len(healed_ids)} 场分在了故事序之外的章；章必须是场景列表上连续的一段，"
                    "这里已把它们并回相邻的章。想重新来过，用「按场景重新分章」。"
                ),
                "scene_plan_ids": healed_ids,
            },
        )
    shaped["scale"] = chapter_scale(session, project_id, body, scenes)
    shaped["chapter_table"] = chapter_table_info(chapters, scenes)
    return shaped


def chapter_table_info(chapters: list[SnowflakeChapterPlan], scenes: list[SnowflakeScenePlan]) -> dict[str, Any]:
    """落了库的章表现在是什么状态——面板据此决定哪几种分法点得动。

    ``authored``：至少有一章不是「（待补）」占位（作者在 07 写的，或上一次确认留下的）；
    ``saved``：已经有场分进了章（「已保存的分章」才有东西可摆）。``chapters`` 是活的章计划行。
    """
    valid = {chapter.chapter_plan_id for chapter in chapters}
    return {
        "count": len(chapters),
        "authored": any(not is_placeholder_chapter(chapter) for chapter in chapters),
        "saved": any(plan.chapter_plan_id in valid for plan in scenes),
    }


def auto_strategy(chapters: list[SnowflakeChapterPlan], scenes: list[SnowflakeScenePlan]) -> str:
    """面板打开时该给作者看什么（不替作者做决定，只挑「现状」最诚实的那一种）。

    - 已经有场分进了章 → ``keep_current``：作者上次确认 / 调整过的结果原样摆出来，新加的场跟着
      故事序上的前一场走。以前面板一打开就按脊柱锚点**重算**一遍，作者手调过的归属每次都被抹平。
    - 一场都没分、但有作者真的写过的章表 → ``spine_anchor``：把场倒进作者的章。
    - 没有章表，或者章表只是几行「（待补）」占位 → ``from_scenes``：章是列完场之后的包装决定，
      直接按场景列表提议（阶段 K）。
    """
    if not chapters:
        return "from_scenes"
    valid = {chapter.chapter_plan_id for chapter in chapters}
    if any(plan.chapter_plan_id in valid for plan in scenes):
        # 阶段 X：已保存的分章如果不是故事序上的连续切片（阶段 V 之前交错洗过的归属，被面板的
        # 「从这里另起一章」继续切下去——2026-09-19 真实项目：第 4 章 = 第 4、10–13 场），就不再把它
        # 原样摆出来让作者用只会挪章界的工具去修一个修不好的东西：直接按场景列表重新提议。
        return "keep_current" if not misplaced_scene_plan_ids(chapters, scenes) else "from_scenes"
    if all(is_placeholder_chapter(chapter) for chapter in chapters):
        return "from_scenes"
    return "spine_anchor"


def preview_from_scenes(
    session: Session,
    project_id: str,
    body: dict[str, Any],
    scenes: list[SnowflakeScenePlan],
) -> dict[str, Any]:
    """按场景列表提议章表——**只读预览**，章行此刻并不存在。

    回包里的章带临时身份 ``new:N``；作者确认时面板把整张章表连同 ``replace_chapters=true``
    交回来，``save`` 才铸 row_uid、软删旧章。和另外几种策略一样：确认之前什么都不落库。
    """
    if not scenes:
        raise DomainError(
            "SNOWFLAKE_SCENES_REQUIRED",
            _SCENES_REQUIRED_MESSAGE,
            status_code=409,
            details={"step_key": "scene_list"},
        )
    scale = chapter_scale(session, project_id, body, scenes)
    chunks = propose_chapter_chunks(
        scenes,
        target_chapter_count=scale["target_chapter_count"],
        scenes_per_chapter=scale["scenes_per_chapter"],
    )
    # 重新按场景分章（换一个每章场数、09 加了一场）不该把所有章都当成新章：
    # - 场**完全相同**的章就是同一章：沿用它的身份（row_uid）、作者 / AI 起的章名、章摘要与章目标；
    # - 阶段 Y：场**至少一半相同**的章也还是那一章（新旧两边都不少于一半；恰好对半时归靠前的那一个），
    #   整拆 / 整并而谁都不过半时归开头对得上的那一个（见 match_chunks_to_chapters）——身份沿用，于是它在目录里
    #   还是同一行（章状态 / 字数目标 / 戏剧卡 / 运行任务不丢）；作者起过的章名与章目标留着，章摘要按新的末场
    #   重算（除非是作者自己写的）。
    live = live_chapter_plans(session, project_id)
    matches = match_chunks_to_chapters(live, scenes, chunks)
    scene_summaries = {str(scene.summary or "").strip() for scene in scenes if str(scene.summary or "").strip()}
    chapters: list[SnowflakeChapterPlan] = []
    assignment: dict[str, str | None] = {}
    reused: set[str] = set()
    for index, chunk in enumerate(chunks, start=1):
        same = matches[index - 1][0] if index - 1 in matches else None
        fields = proposed_chapter_fields(index, chunk, matches.get(index - 1), scene_summaries)
        if same is not None:
            reused.add(same.row_uid)
            row_uid = same.row_uid
        else:
            row_uid = f"{NEW_CHAPTER_PREFIX}{index}"
        # 不进 session 的瞬态行：只为了复用 shape_preview 的同一套成形逻辑
        chapters.append(
            SnowflakeChapterPlan(
                chapter_plan_id=same.chapter_plan_id if same is not None else "",
                project_id=project_id,
                row_uid=row_uid,
                catalog_chapter_id=same.catalog_chapter_id if same is not None else None,
                chapter_seq=index,
                act=fields["act"],
                title=fields["title"],
                summary=fields["summary"],
                spine=fields["spine"],
                chapter_goal=fields["chapter_goal"],
                status="draft",
            )
        )
        for scene in chunk["scenes"]:
            assignment[scene.scene_plan_id] = row_uid
    shaped = shape_preview(session, project_id, "from_scenes", chapters, scenes, assignment)
    shaped["scale"] = scale
    shaped["chapter_table"] = chapter_table_info(live, scenes)
    shaped["replaces_chapter_count"] = len(live) - len(reused)
    return shaped


def shape_preview(
    session: Session,
    project_id: str,
    strategy: str,
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
    assignment: dict[str, str | None],
) -> dict[str, Any]:
    """一份归属 → 面板要的形状：每章的场（带故事序号、功能标签、脊柱、呈现方式）、未分配的场、提醒、节奏体检。"""
    by_chapter: dict[str, list[SnowflakeScenePlan]] = {chapter.row_uid: [] for chapter in chapters}
    unassigned: list[SnowflakeScenePlan] = []
    excluded = excluded_scene_plan_ids(session, project_id)
    # 故事序号（09 场景列表里的第几场）：面板靠它让作者一眼看出「章是不是故事序上连续的一段」
    story_index = {scene.scene_plan_id: index for index, scene in enumerate(scenes, start=1)}
    for scene in scenes:
        target = assignment.get(scene.scene_plan_id)
        if target and target in by_chapter:
            by_chapter[target].append(scene)
        else:
            unassigned.append(scene)

    chapter_payloads = []
    for index, chapter in enumerate(chapters, start=1):
        members = by_chapter[chapter.row_uid]
        chapter_payloads.append(
            {
                "row_uid": chapter.row_uid,
                "chapter_plan_id": chapter.chapter_plan_id,
                "chapter_seq": index,
                # 钉过的目录章号；还没物化过 / 预览里的新章是空串（确认写入时才铸号）
                "chapter_id": catalog_chapter_id(session, chapter, mint=False),
                "act": int(chapter.act or 1),
                "title": chapter.title or "",
                "summary": chapter.summary or "",
                "spine": chapter.spine or "",
                # 章目标原样给（不拿摘要顶替）：面板会把它原样交回来，顶替过的值一存就成了「作者写的章目标」，
                # 之后拆章 / 并章它就一直描述着一场已经搬走的戏。物化时章目标缺席自会退回摘要。
                "chapter_goal": chapter.chapter_goal or "",
                "scene_count": len(members),
                "scenes": [_scene_payload(scene, seq, chapter, story_index, excluded) for seq, scene in enumerate(members, start=1)],
            }
        )

    rhythm = rhythm_report(chapter_payloads)
    orphaned = orphaned_payload(session, project_id)
    return {
        "strategy": strategy,
        "rhythm": rhythm,
        "chapters": chapter_payloads,
        "unassigned": [
            {
                "scene_plan_id": scene.scene_plan_id,
                "scene_id": scene.scene_id,
                "story_index": story_index.get(scene.scene_plan_id, 0),
                "title": scene.title or scene.summary or scene.scene_id,
                "function": scene.chapter_role or "",
                "primary_form": scene.scene_type or "proactive",
                "reason": "no_anchor_segment",
            }
            for scene in unassigned
        ],
        "removed_scenes": orphaned,
        "warnings": preview_warnings(
            session, project_id, chapter_payloads, unassigned, scenes=scenes, rhythm=rhythm, orphaned=orphaned
        ),
        "totals": {
            "chapter_count": len(chapter_payloads),
            "scene_count": sum(item["scene_count"] for item in chapter_payloads),
            "unassigned_count": len(unassigned),
        },
    }


def _scene_payload(
    scene: SnowflakeScenePlan,
    seq: int,
    chapter: SnowflakeChapterPlan,
    story_index: dict[str, int],
    excluded: set[str],
) -> dict[str, Any]:
    spine = scene_spine(scene)
    return {
        "scene_plan_id": scene.scene_plan_id,
        "row_uid": scene.row_uid or "",
        "scene_id": scene.scene_id,
        "scene_seq": seq,
        "story_index": story_index.get(scene.scene_plan_id, 0),
        "title": scene.title or scene.summary or scene.scene_id,
        # 09 的「功能」栏（起疑 / 取证 / 灾难一·一幕高潮…）：一场在故事里的活儿，比一整句事件摘要好扫读
        "function": scene.chapter_role or "",
        "summary": scene.summary or "",
        "primary_form": scene.scene_type or "proactive",
        "spine": spine,
        "anchored": bool(spine) and spine == (chapter.spine or ""),
        "planned": bool((scene.goal or scene.reaction or "").strip()),
        # 阶段 C / N：概述场在节奏体检里按半场计（两种形态都可以概述）
        "rendering_mode": effective_rendering_mode(scene.scene_type, scene.rendering_mode),
        # 阶段 N：作者裁定该重写 / 待删——不物化，节奏按 0 计
        "excluded": scene.scene_plan_id in excluded,
    }
