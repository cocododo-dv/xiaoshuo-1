"""目录的旧版整批导入（B08-24 从 ``catalog.py`` 拆出）：只往空目录里导，按章序建章与场景卡。

原本是浏览器里一次性 localStorage 迁移的落点；那条迁移与它的接口已经删掉（批准 #25），现在只给测试夹具
（``tests/fixture_works.py`` 的 work-a / work-b）播种用——``CatalogService.import_catalog`` 照旧委托到这里。
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard
from novel_system.services.catalog_labels import CHAPTER_STATES, SCENE_STATES, parse_scene_kind
from novel_system.services.catalog_reader import CatalogReader
from novel_system.services.errors import DomainError
from novel_system.services.scene_lookup import require_project


def import_catalog(session: Session, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """一次性迁移入口（admin 保护）：localStorage 旧目录 → 后端行。仅允许空目录导入。"""
    project = require_project(session, project_id)
    if CatalogReader(session).chapter_rows(project_id):
        raise DomainError(
            "CATALOG_NOT_EMPTY",
            "catalog import is only allowed into an empty catalog",
            status_code=409,
        )
    chapters = list((payload or {}).get("chapters") or [])
    if not chapters:
        raise DomainError("CATALOG_IMPORT_EMPTY", "chapters are required", status_code=400)
    normalized_states = [
        state if state in CHAPTER_STATES else "planned"
        for state in (str(item.get("state") or "planned") for item in chapters)
    ]
    requested_current_indexes = [index for index, item in enumerate(chapters) if bool(item.get("current"))]
    if len(requested_current_indexes) > 1:
        raise DomainError(
            "CATALOG_IMPORT_CURRENT_INVALID",
            "catalog import can contain at most one current chapter",
            status_code=400,
        )
    if requested_current_indexes:
        expected_current_index = requested_current_indexes[0]
        if normalized_states[expected_current_index] == "approved":
            raise DomainError(
                "CATALOG_IMPORT_CURRENT_INVALID",
                "current chapter cannot already be approved",
                status_code=400,
            )
        if any(state == "approved" for state in normalized_states[expected_current_index + 1:]):
            raise DomainError(
                "CATALOG_IMPORT_APPROVAL_ORDER_INVALID",
                "approved chapters must precede the current chapter",
                status_code=400,
            )
        # A controlled legacy import may carry intermediate review/draft
        # labels before its explicit current chapter.  The project flow is
        # linear, so canonicalize that historical prefix as approved.
        normalized_states[:expected_current_index] = ["approved"] * expected_current_index
        approved_prefix_length = expected_current_index
    else:
        approved_prefix_length = 0
        for state in normalized_states:
            if state != "approved":
                break
            approved_prefix_length += 1
        if any(state == "approved" for state in normalized_states[approved_prefix_length:]):
            raise DomainError(
                "CATALOG_IMPORT_APPROVAL_ORDER_INVALID",
                "approved chapters must form a contiguous prefix in catalog order",
                status_code=400,
            )
        expected_current_index = approved_prefix_length if approved_prefix_length < len(chapters) else None
    created_scenes = 0
    created_chapter_ids: list[str] = []
    for order, item in enumerate(chapters, start=1):
        title = str(item.get("title") or f"第 {order} 章").strip()
        state = normalized_states[order - 1]
        chapter = ChapterGoal(
            chapter_id=f"{project_id}_CH_{uuid.uuid4().hex[:8]}",
            project_id=project_id,
            planned_scene_count=len(item.get("scenes") or []),
            chapter_goal=title,
            state=state,
            words_target=int((item.get("words") or {}).get("target") or 0) or None,
            display_order=order,
            # 章级的张力 / 视角 / 时间 / 地点 / 入口 / 出口 / 衔接 / 线索已退役（批准 #17a）：旧目录里带着也不导
            narrative_json={
                "title": title,
                "act": item.get("act"),
                "promise": item.get("promise"),
                "drama": dict(item.get("drama") or {}),
            },
            writer_brief_json={"source": "catalog_import", "title": title},
        )
        session.add(chapter)
        session.flush()
        created_chapter_ids.append(chapter.chapter_id)
        scenes_in = list(item.get("scenes") or [])
        # 旧目录的字数挂在章级（words.cur），场景级缺失时把差额摊给零字数场景，
        # 保证 rollup（章字数 = Σ场景字数）不丢数据。
        chapter_cur = int((item.get("words") or {}).get("cur") or 0)
        scene_words = [int(sc.get("words") or 0) for sc in scenes_in]
        shortfall = chapter_cur - sum(scene_words)
        zero_slots = [i for i, w in enumerate(scene_words) if w == 0]
        if scenes_in and shortfall > 0:
            slots = zero_slots or [len(scenes_in) - 1]
            base, remainder = divmod(shortfall, len(slots))
            for j, i in enumerate(slots):
                scene_words[i] += base + (remainder if j == len(slots) - 1 else 0)
        for seq, sc in enumerate(scenes_in, start=1):
            kind = parse_scene_kind(sc.get("kind"))
            s_state = str(sc.get("state") or "todo")
            scene = SceneCard(
                scene_id=f"{chapter.chapter_id}_SC{seq:02d}",
                chapter_id=chapter.chapter_id,
                project_id=project_id,
                scene_seq=seq,
                scene_goal=str(sc.get("title") or "").strip() or f"场景 {seq}",
                scene_type=kind,
                state=s_state if s_state in SCENE_STATES else ("writing" if s_state == "active" else "todo"),
                words_current=scene_words[seq - 1],
                is_chapter_last=1 if seq == len(scenes_in) else 0,
                writer_brief_json={
                    "source": "catalog_import",
                    "title": str(sc.get("title") or "").strip(),
                    "primary_form": kind,
                    **(
                        {"goal": str(sc.get("goal") or ""), "conflict": str(sc.get("obstacle") or ""), "setback": str(sc.get("turn") or "")}
                        if kind == "proactive"
                        else {"reaction": str(sc.get("goal") or ""), "dilemma": str(sc.get("obstacle") or ""), "decision": str(sc.get("turn") or "")}
                    ),
                },
            )
            session.add(scene)
            # 导入路径不在这里补建 SceneRunState：测试夹具（fixture_works）经由本路径
            # 播种并自行管理状态行；运行本章对缺行场景会惰性补建（chapter_runner）。
            created_scenes += 1
    project.approved_chapter_ids_json = created_chapter_ids[:approved_prefix_length]
    if expected_current_index is None:
        project.current_chapter_id = None
        project.status = "completed"
    else:
        project.current_chapter_id = created_chapter_ids[expected_current_index]
        project.status = "chapter_ready"
    session.flush()
    return {"created_chapter_count": len(chapters), "created_scene_count": created_scenes}
