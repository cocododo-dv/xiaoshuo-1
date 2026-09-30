"""整理为章节结构的产物：按构思侧章表分组的 OutlinePlan（``approve_outline_plan`` 把它落进目录）。

2026-09-30 从雪花工作区搬来（B07-17）；工作区的 ``_build_chaptered_outline_plan`` / ``_protagonist_hint`` 是转到这里的同名入口。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import SnowflakeCharacterPlan, SnowflakeScenePlan, StoryProject
from novel_system.services.catalog import normalize_act
from novel_system.services.snowflake_chapter_table import catalog_chapter_id
from novel_system.services.snowflake_chaptering.derive import ensure_chapter_plans
from novel_system.services.snowflake_queries import latest_step_run
from novel_system.services.snowflake_scene_brief import scene_card_beats, scene_title_seed, scene_writer_brief
from novel_system.services.snowflake_scene_rows import scene_plan_payload
from novel_system.services.snowflake_step_catalog import SUMMARY_LENGTH_BAND, effective_rendering_mode
from novel_system.services.snowflake_step_diagnosis import is_protagonist_role


def build_chaptered_outline_plan(
    session: Session,
    project: StoryProject,
    scene_plans: list[SnowflakeScenePlan],
    *,
    protagonist: dict[str, str] | None,
    excluded: set[str],
) -> dict[str, Any]:
    """按构思侧章表分组产出 OutlinePlan（P2）。

    与被它取代的 v1 规划器 ``_build_outline_plan``（2026-09-30 退役）的关键差别：章不再从
    场景行的 ``chapter_id`` 反推（那条路上所有场都写着 ``…_CH01``，于是全书一章，
    章标题就是章 id 字符串），而是读作者在分章面板里确认的章——标题、幕、脊柱、章目标都来自那里。

    ``protagonist``：全书主角（:func:`protagonist_hint`）——阶段 B（雪花评估 B5）：挫折 / 胜利以主角衡量，
    不以 POV 衡量；把角色摘要表里的主角带进每一场的简报，结构简报会渲染它。
    ``excluded``：阶段 N，作者裁定该重写 / 待删的场不物化（三拍留在构思里，回流时再补建或取回）。
    """
    chapters = ensure_chapter_plans(session, project.project_id)
    grouped: dict[str, list[SnowflakeScenePlan]] = {chapter.chapter_plan_id: [] for chapter in chapters}
    for scene in scene_plans:
        key = scene.chapter_plan_id or ""
        if key in grouped and scene.scene_plan_id not in excluded:
            grouped[key].append(scene)

    chapter_payloads: list[dict[str, Any]] = []
    for index, chapter in enumerate(chapters, start=1):
        # 阶段 I：页面上略过的反应场不物化——没有正文要写；它的三拍经下一场的设计上下文到达写手。
        # 章内顺序 = 故事序：scene_plans 进来时已经按 09 场景列表的行序排好，这里只分组、不再重排。
        members = [
            item
            for item in grouped[chapter.chapter_plan_id]
            if effective_rendering_mode(item.scene_type, item.rendering_mode) != "skip"
        ]
        if not members:
            continue  # 空章不落库：预览里已经就此告警过，作者选择保留就是不要它
        # 阶段 Y：目录里的章 id 钉在章计划行上（不再按章序算）——这一章以前物化过，就还是目录里的那一行
        chapter_id = catalog_chapter_id(session, chapter)
        goal = (chapter.chapter_goal or chapter.summary or "").strip() or f"推进本章：{chapter.title or chapter_id}"
        scenes_payload: list[dict[str, Any]] = []
        for seq, scene in enumerate(members, start=1):
            detail = scene_plan_payload(scene)
            if protagonist is not None:
                detail["protagonist_hint"] = protagonist["display_name"]
                detail["protagonist_character_id"] = protagonist["character_id"]
            scene_type = detail.get("primary_form") or "proactive"
            # 阶段 C / N：summary 场（两种形态都可以）拿到数值篇幅带（起草 / 长度补丁按数值硬约束），
            # 并把呈现方式写进简报，结构简报会渲染它。
            rendering_mode = effective_rendering_mode(scene_type, detail.get("rendering_mode"))
            detail["rendering_mode"] = rendering_mode
            detail["target_length_band"] = (
                SUMMARY_LENGTH_BAND if rendering_mode == "summary" else (detail.get("target_length_band") or "medium")
            )
            scenes_payload.append(
                {
                    "scene_id": scene.scene_id,
                    "chapter_id": chapter_id,
                    "scene_seq": seq,
                    "pov_character_id": detail.get("pov_character_id") or None,
                    "onstage_chars_json": detail.get("onstage_chars_json") or [],
                    "location": detail.get("location") or None,
                    "scene_goal": detail.get("summary") or detail.get("title") or goal,
                    "beats_json": scene_card_beats(scene_type, detail),
                    # 阶段 F：摘要不再冒充「必须包含」的硬约束（它本来就含挫折，写成硬约束会让
                    # 硬 QC 拿一句概括去卡正文）；钩子 / 离场变化没写就留空，简报只陈述作者写过的。
                    "must_include_text": detail.get("must_include_text") or "",
                    # 2026-09-20：不再把防抄袭政策句写进 forbidden_text——那个字段是「按字面查的禁用词」，
                    # 这句话会被拆出禁用词「人物」，正文里一出现就是 Q1 硬伤（见 qc_constraints）。
                    "forbidden_text": "",
                    # 与 _scene_card_resync_patch 同一配方：规划行没写离场变化就用挫折 / 决定。
                    # 两边配方不同时，刚物化完的每一场都会被报成「待同步」（纯假阳性）。
                    "exit_change": (
                        detail.get("exit_change")
                        or detail.get("setback")
                        or detail.get("decision")
                        or ""
                    ),
                    "hook": detail.get("hook") or "",
                    "target_length_band": detail["target_length_band"],
                    "primary_form": scene_type,
                    "scene_type": scene_type,
                    "is_chapter_last": 1 if seq == len(members) else 0,
                    "writer_brief_json": {
                        **scene_writer_brief(scene_type, detail),
                        # 阶段 X：构思里起过的短题名随卡进目录（整句摘要不算题名）
                        **scene_title_seed(detail),
                    },
                }
            )
        chapter_payloads.append(
            {
                "chapter_id": chapter_id,
                "chapter_plan_row_uid": chapter.row_uid,
                "title": chapter.title or chapter_id,
                "display_order": index,
                "planned_scene_count": len(scenes_payload),
                "chapter_goal": goal,
                "main_plot_push": (chapter.summary or goal).strip(),
                "emotional_target": "让人物目标、阻碍和代价在行动中显形。",
                "ending_effect": "用新的选择、代价或信息推动下一章。",
                "must_not": "不得复制参考书原文表达、人物、设定或桥段。",
                "notes": "由雪花法分章物化，需确认后进入逐章运行。",
                # 阶段 X：幕写成目录侧的口径 act1 / act2 / act3。过去写整数 1 / 2 / 3，而章节编排按
                # ``act === "act1"`` 分卷——雪花整理出来的章在编排台上一张都不显示。
                "narrative_json": {
                    "title": chapter.title or chapter_id,
                    "act": normalize_act(chapter.act),
                    "spine": chapter.spine or "",
                },
                "writer_brief_json": {
                    "source": "snowflake_method",
                    "chapter_title": chapter.title or chapter_id,
                    "chapter_act": normalize_act(chapter.act),
                    "chapter_spine": chapter.spine or "",
                },
                "scenes": scenes_payload,
            }
        )

    return {
        "source": "snowflake_method",
        "project_id": project.project_id,
        "project_title": project.title,
        "outline_text": project.outline_text,
        "reference_safety": [
            "参考书只进入抽象风格画像，不复制原文表达。",
            "不得复刻参考书人物、设定、桥段、特殊意象或标志性句式。",
            "运行时只使用节奏、句法、叙事手法、结构技巧和禁复刻规则。",
        ],
        "chapters": chapter_payloads,
    }


def protagonist_hint(session: Session, project_id: str) -> dict[str, str] | None:
    """全书主角：04 角色摘要表显式指定的 ``protagonist_character_id`` 优先（双主角作品由作者定），
    否则取定位为主角的第一人；都没有返回 None，简报不带这两个键。"""
    rows = session.execute(
        select(SnowflakeCharacterPlan)
        .where(SnowflakeCharacterPlan.project_id == project_id)
        .order_by(SnowflakeCharacterPlan.created_at.asc(), SnowflakeCharacterPlan.character_id.asc())
    ).scalars().all()
    run = latest_step_run(session, project_id, "character_sheets")
    explicit = str(((run.draft_json or {}) if run is not None else {}).get("protagonist_character_id") or "").strip()
    if explicit:
        for row in rows:
            if row.character_id == explicit:
                return {"character_id": row.character_id, "display_name": row.display_name}
    for row in rows:
        if is_protagonist_role(row.role):
            return {"character_id": row.character_id, "display_name": row.display_name}
    return None
