"""落一版分章：面板确认（``save``）、脚本指名策略（``autoassign``）、按场景提议并落库（``propose_from_scenes``）。

三条路最后都是 ``save``：章行经 ``snowflake_chapter_table`` 的同一个 upsert 写，场的章内顺序永远等于故事序
（``renumber_scene_seq``），「章是故事序上连续的一段」由服务端守住（``heal_assignment``）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import OperationLog, SnowflakeChapterPlan, SnowflakeScenePlan, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.snowflake_chapter_table import (
    catalog_chapter_id,
    is_auto_chapter_title,
    live_chapter_plans,
    mirror_chapters_into_long_synopsis,
    soft_delete_unlisted_chapters,
    upsert_chapter_rows,
)
from novel_system.services.snowflake_chaptering.contiguity import heal_assignment
from novel_system.services.snowflake_chaptering.derive import ensure_chapter_plans
from novel_system.services.snowflake_chaptering.preview import preview
from novel_system.services.snowflake_scene_order import live_scene_plans_in_story_order, renumber_scene_seq


def save(session: Session, project_id: str, payload: dict[str, Any] | None = None, *, actor_ref: str = "operator") -> dict[str, Any]:
    """落一版分章。

    ``replace_chapters=true``（面板确认时总是带）= 载荷里的 ``chapters`` 是**整张章表**：
    - 认得的 ``row_uid`` → 改标题 / 幕 / 脊柱 / 章目标 / 顺序；
    - ``new:N``（或空）→ 新建一章（按场景提议的章、面板里「从这里另起一章」拆出来的章）；
    - 没列出来的章 → 软删（并入上一章之后空掉的章、被整张替换的旧章表）。
    不带这个标记时是旧契约：只更新列出来的章，认不得的 row_uid 报 404。

    场的章内顺序不看载荷里的先后——它永远等于故事序（09 场景列表的行序），由
    ``renumber_scene_seq`` 统一重算。章是故事序上连续的一段，面板也只提供挪章界、拆章、并章。
    """
    body = payload or {}
    replace_chapters = bool(body.get("replace_chapters"))
    chapters = live_chapter_plans(session, project_id) if replace_chapters else ensure_chapter_plans(session, project_id)
    story_scenes = live_scene_plans_in_story_order(session, project_id)
    scenes = {plan.scene_plan_id: plan for plan in story_scenes}

    # 章的元数据（标题/幕/脊柱/章目标/顺序）——作者可以在面板里直接改
    incoming_chapters = [item for item in (body.get("chapters") or []) if isinstance(item, dict)]
    if replace_chapters and not incoming_chapters:
        raise DomainError(
            "SNOWFLAKE_CHAPTER_PLAN_PAYLOAD_EMPTY",
            "整张替换章表时必须带上新的章表；空章表不会被当成「删掉所有章」。",
            status_code=400,
        )
    table = upsert_chapter_rows(session, project_id, incoming_chapters, chapters=chapters, create_new=replace_chapters)

    # 场景归属
    assignments = [item for item in (body.get("assignments") or []) if isinstance(item, dict)]
    if not assignments:
        raise DomainError(
            "SNOWFLAKE_CHAPTER_ASSIGNMENTS_REQUIRED",
            "没有收到任何场景归属，无法保存分章。",
            status_code=400,
        )
    touched: list[SnowflakeScenePlan] = []
    for item in assignments:
        scene_plan_id = str(item.get("scene_plan_id") or "").strip()
        plan = scenes.get(scene_plan_id)
        if plan is None:
            raise DomainError(
                "SNOWFLAKE_SCENE_PLAN_NOT_FOUND",
                "分章里引用了不存在或已删除的场景计划。",
                status_code=404,
                details={"scene_plan_id": scene_plan_id},
            )
        row_uid = str(item.get("chapter_row_uid") or "").strip()
        chapter = table.alias.get(row_uid) or table.by_uid.get(row_uid)
        if chapter is None:
            raise DomainError(
                "SNOWFLAKE_CHAPTER_PLAN_NOT_FOUND",
                "分章里引用了不存在的章。",
                status_code=404,
                details={"chapter_row_uid": row_uid},
            )
        plan.chapter_plan_id = chapter.chapter_plan_id
        touched.append(plan)

    # 整张替换：没列出来的章软删，还挂在上面的场退回「未分章」
    removed = (
        soft_delete_unlisted_chapters(
            session,
            project_id,
            chapters,
            table.listed,
            story_scenes,
            actor_ref=actor_ref,
            reason="chapter_plan_replaced",
        )
        if replace_chapters
        else []
    )

    # 章序可能整体变了（拆章 / 并章 / 重排），所以给**每一场**重盖章戳，而不只是这次点名的场——
    # 否则没被点名的场还带着旧的物化目标章号，回流会把场景卡搬进错的章。
    live = {chapter.chapter_plan_id: chapter for chapter in table.by_uid.values() if not chapter.removed_at}
    # 阶段 X：「章是故事序上连续的一段」由服务端守住，不再信面板。面板只提供挪章界 / 拆章 / 并章，
    # 从一版连续的分章出发不会越界；可它手里的那一版如果本来就是交错的（旧数据、API 调用方），
    # 再怎么拆也是交错的——落库前过一遍 heal_assignment：保住章序的最长不降子序列，离群的场并入故事序上前一场的章。
    ordered_chapters = sorted(live.values(), key=lambda chapter: (int(chapter.chapter_seq or 0), chapter.chapter_plan_id))
    by_uid = {chapter.row_uid: chapter for chapter in ordered_chapters}
    by_plan_id = {chapter.chapter_plan_id: chapter for chapter in ordered_chapters}
    current = {
        plan.scene_plan_id: by_plan_id[plan.chapter_plan_id].row_uid if plan.chapter_plan_id in by_plan_id else None
        for plan in story_scenes
    }
    fixed = heal_assignment(current, ordered_chapters, story_scenes)
    healed: list[str] = []
    for plan in story_scenes:
        target = fixed.get(plan.scene_plan_id)
        if target and target != current.get(plan.scene_plan_id):
            plan.chapter_plan_id = by_uid[target].chapter_plan_id
            healed.append(plan.scene_plan_id)
    if replace_chapters:
        refresh_auto_chapter_fields(list(live.values()), story_scenes)
    # 阶段 Y：每章在目录里的 id 钉在章计划行上——按章序铸号（第一次物化仍是 CH01…CHnn），
    # 已经钉过的章不管现在排第几都还是它自己的号：拆章 / 并章 / 重排只动真的换了章的场。
    for chapter in ordered_chapters:
        catalog_chapter_id(session, chapter)
    for plan in story_scenes:
        chapter = live.get(plan.chapter_plan_id or "")
        if chapter is None:
            continue
        plan.chapter_id = chapter.catalog_chapter_id
        # 不退回场景行上的旧值：那是它上一个章的标题 / 章目标，回流还会把它写进场景卡的简报
        plan.chapter_title = chapter.title or ""
        plan.chapter_goal = chapter.chapter_goal or chapter.summary or ""
    session.flush()
    renumber_scene_seq(session, project_id)
    mirror_chapters_into_long_synopsis(session, project_id, list(live.values()))

    session.add(
        OperationLog(
            event_type="snowflake_chapter_plan_saved",
            object_type="story_project",
            object_ref=project_id,
            payload_json={
                "project_id": project_id,
                "chapter_count": len(live),
                "assigned_scene_count": len(touched),
                "created_chapter_count": len(table.created),
                "removed_chapter_count": len(removed),
                "healed_scene_plan_ids": healed,
                "actor_ref": actor_ref or "operator",
                "saved_at": utcnow(),
            },
        )
    )
    session.flush()
    return {"assigned_scene_count": len(touched), "healed_scene_plan_ids": healed}


def refresh_auto_chapter_fields(chapters: list[SnowflakeChapterPlan], scenes: list[SnowflakeScenePlan]) -> None:
    """整张章表落库时，把**系统起的**章名与章摘要按新的结构重算；作者写的一个字都不动。

    - 章名空着、或是系统起的占位（「第 N 章」「（待补）」「未命名章节」，``is_auto_chapter_title``，与 AI 起章名
      同一条规则，B07-14）→ 按现在的章序重编（拆章 / 并章之后「第 3 章」不能排在第 4 位，空章名物化进目录会变成
      章 id 字符串，「（待补）」原样进目录也不是一个章名）；
    - 章摘要空着、或与某一场的摘要一字不差（= 提议时从场上抄来的）→ 取这一章现在的最后一场。
      作者自己写的摘要不会和某一场的摘要逐字相同。
    """
    scene_summaries = {str(scene.summary or "").strip() for scene in scenes if str(scene.summary or "").strip()}
    members: dict[str, list[SnowflakeScenePlan]] = {}
    for scene in scenes:  # scenes 已按故事序
        members.setdefault(scene.chapter_plan_id or "", []).append(scene)
    for chapter in chapters:
        if is_auto_chapter_title(chapter.title):
            chapter.title = f"第 {int(chapter.chapter_seq or 1)} 章"
        summary = str(chapter.summary or "").strip()
        mine = members.get(chapter.chapter_plan_id) or []
        if mine and (not summary or summary in scene_summaries):
            last = mine[-1]
            chapter.summary = str(last.summary or last.title or "").strip()


def autoassign(session: Session, project_id: str, strategy: str, *, actor_ref: str = "operator") -> dict[str, Any]:
    """按指定策略直接落一版分章（不经预览面板）。

    给脚本 / API 调用方用：策略由调用方显式指名，结果与 ``preview`` 同一套算法，
    所以「预览看到什么就是什么」的承诺不会因为走了这条路而失效。
    """
    shaped = preview(session, project_id, {"strategy": strategy})
    payload = _payload_from_preview(shaped, replace_chapters=False)
    if not payload["assignments"]:
        raise DomainError(
            "SNOWFLAKE_CHAPTER_ASSIGNMENTS_REQUIRED",
            f"策略「{strategy}」没有分配出任何场景归属。",
            status_code=409,
            details={"warnings": shaped["warnings"]},
        )
    return save(session, project_id, payload, actor_ref=actor_ref)


def propose_from_scenes(
    session: Session,
    project_id: str,
    payload: dict[str, Any] | None = None,
    *,
    actor_ref: str = "operator",
) -> dict[str, Any]:
    """按已经列好的场景提议一份章表并落库（Ingermanson：章是列完场之后的包装决定）。

    - 三个灾难是幕的铰链：带 灾一 / 灾二 / 灾三 的场必须是它所在章的最后一场；
    - 章的尺度见 ``chapter_scale``（载荷的章数 / 每章场数 → 作品设置 → 参考书章长 → 每章 3 场）；
    - 每幕至少一章；章标题给占位「第 N 章」，摘要取本章**最后一场**的一句话（这一章把局面推到哪），作者随后改；
    - 已有章表时必须显式 ``replace=true`` 才覆盖（旧章软删，归属重排）；
    - 章表同时镜像进 07 草稿的 ``chapters``，前端表格能看到。

    面板不走这条路——它用 ``preview(strategy="from_scenes")`` 拿到同一份提议的**预览**，作者确认时才由
    ``save(replace_chapters=true)`` 落库；这个端点留给脚本 / API 调用方。B07-08：它就是那两步本身
    （``from_scenes`` 预览 → 整张保存），不再自己再推一遍身份沿用，所以不会和面板漂开。
    """
    body = payload or {}
    scenes = live_scene_plans_in_story_order(session, project_id)
    if not scenes:
        raise DomainError(
            "SNOWFLAKE_SCENES_REQUIRED",
            "09 场景列表还没有场景，无法按场景提议章表。",
            status_code=409,
            details={"step_key": "scene_list"},
        )
    existing = live_chapter_plans(session, project_id)
    if existing and not body.get("replace"):
        raise DomainError(
            "SNOWFLAKE_CHAPTER_PLAN_EXISTS",
            "已经有章表了；要按场景重新提议，请带 replace=true（旧章会被替换，场景归属重排）。",
            status_code=409,
            details={"chapter_count": len(existing)},
        )
    proposal = preview(
        session,
        project_id,
        {
            "strategy": "from_scenes",
            "target_chapter_count": body.get("target_chapter_count"),
            "scenes_per_chapter": body.get("scenes_per_chapter"),
        },
    )
    scale = proposal["scale"]
    save(session, project_id, _payload_from_preview(proposal, replace_chapters=True), actor_ref=actor_ref)
    session.add(
        OperationLog(
            event_type="snowflake_chapter_plan_proposed",
            object_type="story_project",
            object_ref=project_id,
            payload_json={
                "project_id": project_id,
                "chapter_count": len(proposal["chapters"]),
                "scene_count": len(scenes),
                "replaced_chapter_count": len(existing),
                "target_chapter_count": scale["target_chapter_count"],
                "scenes_per_chapter": scale["scenes_per_chapter"],
                "scale_source": scale["source"],
                "reference_hint": scale["reference_hint"],
                "actor_ref": actor_ref or "operator",
                "proposed_at": utcnow(),
            },
        )
    )
    session.flush()
    result = preview(session, project_id, {"strategy": "keep_current"})
    result["created_chapter_count"] = len(proposal["chapters"])
    result["replaced_chapter_count"] = len(existing)
    result["scale"] = scale
    return result


def _payload_from_preview(shaped: dict[str, Any], *, replace_chapters: bool) -> dict[str, Any]:
    """一份预览 → ``save`` 的载荷（面板确认时交回来的就是这个形状）。"""
    payload: dict[str, Any] = {
        "chapters": [
            {
                "row_uid": chapter["row_uid"],
                "title": chapter["title"],
                "act": chapter["act"],
                "spine": chapter["spine"],
                "chapter_goal": chapter["chapter_goal"],
                **({"summary": chapter["summary"]} if replace_chapters else {}),
            }
            for chapter in shaped["chapters"]
        ],
        "assignments": [
            {"scene_plan_id": scene["scene_plan_id"], "chapter_row_uid": chapter["row_uid"]}
            for chapter in shaped["chapters"]
            for scene in chapter["scenes"]
        ],
    }
    if replace_chapters:
        payload["replace_chapters"] = True
    return payload
