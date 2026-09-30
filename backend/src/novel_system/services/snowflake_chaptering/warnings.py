"""分章预览的提醒：未分配 / 空章 / 过长章、章序冲突、节奏体检、目录里对不上的章、回收站里的卡、孤儿场。

只有孤儿场是 blocker（它等作者做一个决定）；其余都是提醒，从不阻断、也绝不替作者删东西。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard, SnowflakeScenePlan
from novel_system.services.catalog_placeholders import pristine_placeholder_chapters
from novel_system.services.catalog_trash_cascade import split_trashed_planned_cards
from novel_system.services.scene_design_ownership import is_snowflake_origin
from novel_system.services.snowflake_chaptering.algorithms import clip
from novel_system.services.snowflake_chaptering.contiguity import misplaced_in_preview

# 一章分到的场数超过均值这么多倍时给个提醒（不阻断——长章是合法的作者选择）
_OVERSIZED_RATIO = 3.0


def _chapter_label(item: dict[str, Any]) -> str:
    return item["title"] or "第 " + str(item["chapter_seq"]) + " 章"


def preview_warnings(
    session: Session,
    project_id: str,
    chapter_payloads: list[dict[str, Any]],
    unassigned: list[SnowflakeScenePlan],
    *,
    scenes: list[SnowflakeScenePlan],
    rhythm: dict[str, Any],
    orphaned: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if unassigned:
        titles = "、".join((scene.title or scene.summary or scene.scene_id) for scene in unassigned[:3])
        more = f" 等 {len(unassigned)} 场" if len(unassigned) > 3 else ""
        warnings.append(
            {
                "kind": "unassigned_scenes",
                "severity": "warning",
                "message": f"{len(unassigned)} 场没有分到章：{titles}{more}。确认前请指派，否则它们不会进入章节目录。",
            }
        )
    for item in (item for item in chapter_payloads if not item["scene_count"]):
        warnings.append(
            {
                "kind": "empty_chapter",
                "severity": "warning",
                "message": f"「{_chapter_label(item)}」没有分到任何场，物化后会是一章空壳。",
            }
        )
    counts = [item["scene_count"] for item in chapter_payloads if item["scene_count"]]
    if counts:
        mean = sum(counts) / len(counts)
        for item in chapter_payloads:
            if mean > 0 and item["scene_count"] >= max(2, mean * _OVERSIZED_RATIO):
                warnings.append(
                    {
                        "kind": "oversized_chapter",
                        "severity": "warning",
                        "message": (
                            f"「{_chapter_label(item)}」分到 {item['scene_count']} 场，"
                            f"约是平均值（{mean:.1f}）的 {item['scene_count'] / mean:.1f} 倍。"
                        ),
                    }
                )
    # 章必须是故事序上连续的一段：章内顺序永远等于 09 场景列表的顺序，所以一场如果分在了比故事序上排在它前面的
    # 场更靠前的章，目录里读到的顺序就和场景列表不一样了（常见成因：分章之后又在 09 里拖过行）。
    misplaced = misplaced_in_preview(chapter_payloads)
    if misplaced:
        chapter, scene = misplaced[0]
        more = f" 等 {len(misplaced)} 场" if len(misplaced) > 1 else ""
        warnings.append(
            {
                "kind": "chapter_order_conflict",
                "severity": "advisory",
                "message": (
                    f"「{clip(scene['title'])}」{more}在场景列表里排在后面几章的场之后，却分在"
                    f"《{_chapter_label(chapter)}》——章是场景列表上连续的一段，"
                    "目录里读到的顺序会和场景列表不一致。把它移回相邻的章，或者按场景重新分章。"
                ),
                "scene_plan_ids": [entry[1]["scene_plan_id"] for entry in misplaced],
            }
        )

    warnings.extend(catalog_warnings(session, project_id, chapter_payloads, scenes=scenes))

    # 节奏体检的提示（P3）：只提醒、从不阻断 —— 作者故意把灾二后置是合法选择。
    for item in rhythm["spine_placement"]:
        if not item.get("placed"):
            warnings.append(
                {
                    "kind": "spine_not_placed",
                    "severity": "advisory",
                    "message": f"没有任何一章标着「{item['spine']}」—— 三个灾难是幕与幕的铰链，缺一个结构就会塌。",
                }
            )
        elif not item.get("on_hinge"):
            where = "本幕最后一章" if item["spine"] != "灾二" else "第二幕"
            warnings.append(
                {
                    "kind": "spine_off_hinge",
                    "severity": "advisory",
                    "message": (
                        f"「{item['spine']}」落在第 {item['act']} 幕的《{item['chapter_title']}》，"
                        f"通常它应该在第 {item['expected_act']} 幕的{where}。故意为之就忽略这条。"
                    ),
                }
            )

    for item in orphaned:
        warnings.append(
            {
                "kind": "orphaned_scene",
                "severity": "blocker",
                "message": (
                    f"「{item['title']}」已经从场景列表删除，但章节目录里已有它的场景卡"
                    "（可能已经写了正文）。请先决定是一并删除还是保留。"
                ),
                "scene_plan_id": item["scene_plan_id"],
            }
        )
    return warnings


def catalog_warnings(
    session: Session,
    project_id: str,
    chapter_payloads: list[dict[str, Any]],
    *,
    scenes: list[SnowflakeScenePlan],
) -> list[dict[str, Any]]:
    """章节目录里和这次分章对不上的章——只提醒，不阻断，也绝不替作者删。

    - 作者在章节编排里手建的章（不是雪花整理出来的）：原样保留——雪花的章按章表的顺序排，手建的章
      原来跟在哪一章后面现在还跟在它后面（``_CatalogPlacement.settle_chapter_order``）；
    - 上一版分章留下、这一版已经没有场的章：场景卡搬走之后空了的进回收站，还留着东西的原样保留。
    """
    rows = list(
        session.execute(
            select(ChapterGoal).where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
        ).scalars()
    )
    trash_warnings = trashed_card_warnings(session, project_id, chapter_payloads)
    if not rows:
        return trash_warnings
    targets = {item["chapter_id"] for item in chapter_payloads if item["scene_count"] and item["chapter_id"]}

    # 阶段 X：手建的章分两种——作者真写过东西的（原样保留，新章接在后面）与空白占位章
    # （「第 1 章 / 开场」，一个字没写：确认写入时移入回收站，雪花的章从第 1 章排起）。
    # 阶段 Y：哪一章是雪花整理出来的，看它的来源，不看 id 长什么样（章号不再有位置含义）。
    placeholder_ids = {chapter.chapter_id for chapter, _cards in pristine_placeholder_chapters(session, project_id)}
    placeholders = [row for row in rows if row.chapter_id in placeholder_ids]
    hand_made = [
        row for row in rows if not is_snowflake_origin(row.writer_brief_json) and row.chapter_id not in placeholder_ids
    ]
    leftover = [row for row in rows if is_snowflake_origin(row.writer_brief_json) and row.chapter_id not in targets]
    warnings: list[dict[str, Any]] = list(trash_warnings)

    def names(items: list[ChapterGoal]) -> str:
        labels = [
            str((item.narrative_json or {}).get("title") or (item.writer_brief_json or {}).get("chapter_title") or item.chapter_id)
            for item in items[:3]
        ]
        return "、".join(f"「{label}」" for label in labels) + (f" 等 {len(items)} 章" if len(items) > 3 else "")

    if placeholders:
        warnings.append(
            {
                "kind": "catalog_placeholder_chapters",
                "severity": "advisory",
                "message": (
                    f"章节目录里的 {names(placeholders)} 是还没动过笔的空白占位章——确认写入时会移入回收站"
                    "（可在回收站取回），这一版的章从第 1 章排起。"
                ),
            }
        )
    if hand_made:
        warnings.append(
            {
                "kind": "catalog_hand_made_chapters",
                "severity": "advisory",
                "message": (
                    f"章节目录里已有 {len(hand_made)} 章不是雪花整理出来的（{names(hand_made)}）。"
                    "它们原样保留、不会被覆盖：原来排在最前面的还在最前面，原来跟在哪一章后面的还跟在那一章后面；"
                    "不要的可以到章节编排里删。"
                ),
            }
        )
    if leftover:
        # 确认写入之后这一章还剩不剩东西。计划内的卡搬走，作者手加的场**跟着它的锚点场一起走**（阶段 Y，
        # 见 ``_CatalogPlacement``）；留得下来的只有回收站里的卡，和整章一张计划内的活跃卡都没有（没有锚点
        # 可跟）的章里的卡。什么都不剩的章 → 确认写入时移入回收站。
        plan_scene_ids = {plan.scene_id for plan in scenes}
        leftover_ids = {row.chapter_id for row in leftover}
        leftover_cards = list(
            session.execute(
                select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.chapter_id.in_(sorted(leftover_ids)))
            ).scalars()
        )
        anchored = {
            card.chapter_id for card in leftover_cards
            if card.scene_id in plan_scene_ids and int(card.trashed_flag or 0) == 0
        }
        keeps_something = {
            card.chapter_id for card in leftover_cards
            if int(card.trashed_flag or 0) == 1 or card.chapter_id not in anchored
        }
        emptied = [row for row in leftover if row.chapter_id not in keeps_something]
        kept = [row for row in leftover if row.chapter_id in keeps_something]
        if emptied:
            warnings.append(
                {
                    "kind": "catalog_leftover_chapters",
                    "severity": "advisory",
                    "message": (
                        f"上一版分章留在目录里的 {names(emptied)} 在这一版里没有场了——确认写入后这些空章会移入回收站"
                        "（可在回收站取回）。"
                    ),
                }
            )
        if kept:
            warnings.append(
                {
                    "kind": "catalog_leftover_chapters_kept",
                    "severity": "advisory",
                    "message": (
                        f"{names(kept)} 在这一版里没有场了，但里面还留着东西（回收站里的场，或整章只有你手加的场）"
                        "——这几章原样保留，可到章节编排里处理。"
                    ),
                }
            )
    return warnings


def trashed_card_warnings(
    session: Session, project_id: str, chapter_payloads: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """这一版章表里的场，场景卡此刻却在回收站里——确认写入之前说清楚哪些会回来、哪些不会。

    随旧章一起删的（作者先在章节编排里删了旧章，再回来重新分章）确认写入时取回；作者单独删掉的那几场
    是对那一场的裁定，不替作者取回（判定见 ``catalog_trash_cascade``）。
    """
    scene_ids = [
        str(scene.get("scene_id") or "")
        for item in chapter_payloads
        for scene in item.get("scenes") or []
        if not scene.get("excluded") and scene.get("rendering_mode") != "skip"
    ]
    cascade, individual = split_trashed_planned_cards(
        session,
        project_id,
        scene_ids,
        target_chapter_ids=[str(item.get("chapter_id") or "") for item in chapter_payloads],
    )
    warnings: list[dict[str, Any]] = []
    if cascade:
        warnings.append(
            {
                "kind": "catalog_trashed_scenes_return",
                "severity": "advisory",
                "scene_ids": [card.scene_id for card in cascade],
                "message": (
                    f"这一版里有 {len(cascade)} 场的场景卡是随着旧章一起进回收站的——确认写入时会取回，"
                    "放进这一版的章里（卡上的正文与运行记录都在）。"
                ),
            }
        )
    if individual:
        warnings.append(
            {
                "kind": "catalog_trashed_scenes_kept",
                "severity": "advisory",
                "scene_ids": [card.scene_id for card in individual],
                "message": (
                    f"这一版里有 {len(individual)} 场的场景卡是你单独删掉的（在回收站里）——确认写入不会替你取回，"
                    "目录里仍然看不到这几场：要写就到回收站恢复；不要这一场，就在构思第 10 步把它裁定为「待删」。"
                ),
            }
        )
    return warnings
