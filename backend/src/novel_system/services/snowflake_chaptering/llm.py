"""分章面板上的两个 AI 按钮：「AI 建议分章」（只读，不落库）与「AI 起章名」（只读，不落库）。

模型调用本身在 ``snowflake_chapter_llm``（经雪花 LLM 服务的计量 / fail-closed 路径）；这里组载荷、把建议叠回
确定性的分章上（批准 #17c：放错先后的场只挪它自己）、分批起名。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.errors import DomainError
from novel_system.services.projects import project_payload
from novel_system.services.scene_lookup import require_project
from novel_system.services.snowflake_chapter_llm import chapter_plan_suggestions, chapter_title_suggestions
from novel_system.services.snowflake_chapter_table import NEW_CHAPTER_PREFIX, coerce_act, is_auto_chapter_title
from novel_system.services.snowflake_chaptering.algorithms import clip
from novel_system.services.snowflake_chaptering.contiguity import heal_assignment
from novel_system.services.snowflake_chaptering.derive import ensure_chapter_plans
from novel_system.services.snowflake_chaptering.preview import chapter_table_info, preview, shape_preview
from novel_system.services.snowflake_chaptering.scale import reference_chapter_titles
from novel_system.services.snowflake_chaptering.spine import scene_spine
from novel_system.services.snowflake_queries import latest_step_run
from novel_system.services.snowflake_scene_order import live_scene_plans_in_story_order
from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService


def suggest(session: Session, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """让 LLM 给一份分章建议（P3，只读 —— 不落库）。

    回包的形状和 ``preview`` 一致（chapters/unassigned/warnings/rhythm），面板可以
    直接把它当成另一份「候选预览」渲染，作者采纳或丢弃都只是本地状态。多出一个
    ``rationale``：模型自己说哪几个断章它最没把握，作者知道先看哪里。
    """
    base = preview(session, project_id, {"strategy": (payload or {}).get("base_strategy") or "spine_anchor"})
    chapters = ensure_chapter_plans(session, project_id)
    scenes = live_scene_plans_in_story_order(session, project_id)
    current = {
        scene["scene_plan_id"]: chapter["row_uid"] for chapter in base["chapters"] for scene in chapter["scenes"]
    }
    result = chapter_plan_suggestions(
        SnowflakeWorkspaceLLMService(session),
        project=_project_payload(session, project_id),
        chapters=[
            {
                "row_uid": chapter.row_uid,
                "chapter_seq": chapter.chapter_seq,
                "act": chapter.act,
                "title": chapter.title or "",
                "summary": chapter.summary or "",
                "spine": chapter.spine or "",
            }
            for chapter in chapters
        ],
        # B07-13：给模型的是全书的故事序号——scene_seq 在分过一次章之后是章内序（1、2、1、2……）
        scenes=[
            {
                "scene_plan_id": plan.scene_plan_id,
                "story_index": story_index,
                "title": plan.title or plan.summary or plan.scene_id,
                "summary": plan.summary or "",
                "primary_form": plan.scene_type or "proactive",
                "spine": scene_spine(plan),
            }
            for story_index, plan in enumerate(scenes, start=1)
        ],
        current_assignment=[
            {"scene_plan_id": scene_plan_id, "chapter_row_uid": row_uid} for scene_plan_id, row_uid in current.items()
        ],
    )
    suggested = {item["scene_plan_id"]: item["chapter_row_uid"] for item in result.payload.get("assignments") or []}
    # 模型没提到的场保留确定性提案的归属 —— 建议是叠加，不是全量替换
    assignment = {scene.scene_plan_id: suggested.get(scene.scene_plan_id, current.get(scene.scene_plan_id)) for scene in scenes}
    # 批准 #17c：模型放错先后的场只挪它自己（与 save 同一条连续性规则），面板看到的就是确认后落库的那一版
    assignment = heal_assignment(assignment, chapters, scenes)
    shaped = shape_preview(session, project_id, "llm_suggested", chapters, scenes, assignment)
    shaped["chapter_table"] = chapter_table_info(chapters, scenes)
    shaped["rationale"] = result.payload.get("rationale") or ""
    shaped["source"] = result.source
    shaped["llm_call_id"] = result.llm_call_id
    shaped["kept_from_deterministic"] = sorted(result.payload.get("missing_scene_plan_ids") or [])
    return shaped


def suggest_titles(
    session: Session,
    project_id: str,
    payload: dict[str, Any] | None = None,
    *,
    batch_size: int,
    max_batches: int,
) -> dict[str, Any]:
    """AI 起章名（**只读**——不落库，名字回到面板里由作者改、由作者确认）。

    载荷的 ``chapters`` 是面板此刻的章表（含还没落库的 ``new:N`` 章、作者手调过的归属）：
    ``[{row_uid, title, act, spine, scene_plan_ids}]``；不带就按已保存的分章起。
    只给**系统起的占位名**（空 / 「第 N 章」/「（待补）」）起名，作者自己起的名字不碰——
    ``rename_all=true`` 才全部重起。fail-closed：模型没配好就 409，不拿规则拼的名字冒充。
    一次调用起 ``batch_size`` 章、一次请求最多 ``max_batches`` 批（分批还让后面的批看得见前面起好的名字）。
    """
    body = payload or {}
    scenes = live_scene_plans_in_story_order(session, project_id)
    order = {scene.scene_plan_id: index for index, scene in enumerate(scenes, start=1)}
    by_id = {scene.scene_plan_id: scene for scene in scenes}
    incoming = [item for item in (body.get("chapters") or []) if isinstance(item, dict)]
    if not incoming:
        saved = preview(session, project_id, {"strategy": "keep_current"})
        incoming = [
            {
                "row_uid": chapter["row_uid"],
                "title": chapter["title"],
                "act": chapter["act"],
                "spine": chapter["spine"],
                "scene_plan_ids": [scene["scene_plan_id"] for scene in chapter["scenes"]],
            }
            for chapter in saved["chapters"]
        ]

    rename_all = bool(body.get("rename_all"))
    chapters: list[dict[str, Any]] = []
    for position, item in enumerate(incoming, start=1):
        row_uid = str(item.get("row_uid") or "").strip() or f"{NEW_CHAPTER_PREFIX}{position}"
        title = str(item.get("title") or "").strip()
        members = sorted(
            {str(value or "").strip() for value in (item.get("scene_plan_ids") or [])} & set(by_id),
            key=lambda scene_plan_id: order[scene_plan_id],
        )
        chapters.append(
            {
                "row_uid": row_uid,
                "position": position,
                "title": title,
                "auto": rename_all or is_auto_chapter_title(title),
                "act": coerce_act(item.get("act"), 1),
                "spine": str(item.get("spine") or "").strip(),
                "scene_plan_ids": members,
            }
        )
    targets = [chapter for chapter in chapters if chapter["auto"] and chapter["scene_plan_ids"]]
    authored = [chapter for chapter in chapters if not chapter["auto"]]
    result: dict[str, Any] = {
        "titles": [],
        "requested_count": len(targets),
        "named_count": 0,
        "remaining_count": len(targets),
        "skipped_authored_count": len(authored),
        "notice": None,
        "source": "llm",
        "llm_call_ids": [],
    }
    if not targets:
        result["notice"] = {
            "code": "CHAPTER_TITLES_NOTHING_TO_NAME",
            "severity": "info",
            "message": "每一章都已经有你起的名字了；想让 AI 重起某一章，先把它的章名清空。",
        }
        return result

    llm = SnowflakeWorkspaceLLMService(session)
    project = _project_payload(session, project_id)
    book = book_context(session, project_id)
    # 2026-09-22 结构跟随参考书:章名照参考作家起题名的方式起(题名样例 + 形态);无绑定 → None
    reference_titles = reference_chapter_titles(session, project_id)
    named = [{"position": chapter["position"], "title": chapter["title"]} for chapter in authored]
    batches = [targets[start : start + batch_size] for start in range(0, len(targets), batch_size)]
    for batch_index, batch in enumerate(batches[:max_batches]):
        try:
            outcome = chapter_title_suggestions(
                llm,
                project=project,
                book=book,
                named_chapters=sorted(named, key=lambda item: item["position"]),
                reference_titles=reference_titles,
                chapters=[
                    {
                        "row_uid": chapter["row_uid"],
                        "position": chapter["position"],
                        "of": len(chapters),
                        "act": chapter["act"],
                        "spine": chapter["spine"],
                        "scenes": [
                            {
                                "story_index": order[scene_plan_id],
                                "function": by_id[scene_plan_id].chapter_role or "",
                                "spine": scene_spine(by_id[scene_plan_id]),
                                "form": by_id[scene_plan_id].scene_type or "proactive",
                                "summary": clip(by_id[scene_plan_id].summary or by_id[scene_plan_id].title, 160),
                            }
                            for scene_plan_id in chapter["scene_plan_ids"]
                        ],
                    }
                    for chapter in batch
                ],
            )
        except DomainError as exc:
            if not result["titles"]:
                raise
            # 前面几批已经起好了名字：如实交回去，剩下的说清楚为什么没起成
            result["notice"] = {
                "code": "CHAPTER_TITLES_PARTIAL",
                "severity": "warning",
                "message": f"起到第 {batch_index} 批时模型调用失败（{exc.message}）；已经起好的章名先给你，其余的可以再点一次。",
            }
            break
        if outcome.llm_call_id:
            result["llm_call_ids"].append(outcome.llm_call_id)
        position_of = {chapter["row_uid"]: chapter["position"] for chapter in batch}
        for item in outcome.payload.get("titles") or []:
            result["titles"].append(item)
            named.append({"position": position_of[item["row_uid"]], "title": item["title"]})

    if not result["titles"]:
        raise DomainError(
            "SNOWFLAKE_CHAPTER_TITLES_EMPTY",
            "模型这一次没有给出可用的章名（空的、重复的或只是「高潮」「结局」这类标签都不算）。可以再点一次。",
            status_code=502,
            details={"llm_call_ids": result["llm_call_ids"]},
        )
    result["named_count"] = len(result["titles"])
    result["remaining_count"] = len(targets) - len(result["titles"])
    if result["remaining_count"] and result["notice"] is None:
        result["notice"] = {
            "code": "CHAPTER_TITLES_PARTIAL",
            "severity": "info",
            "message": f"这一次起了 {result['named_count']} 章的名字，还有 {result['remaining_count']} 章没起成；再点一次接着起。",
        }
    return result


def book_context(session: Session, project_id: str) -> dict[str, Any]:
    """章名要带着全书的调子：已确认的一句话与五句脊柱（未确认的草稿不算事实，不给）。"""
    context: dict[str, Any] = {}
    for step_key, field_name, target in (
        ("one_sentence_summary", "summary", "logline"),
        ("one_paragraph_summary", "sentences", "five_sentence_spine"),
        ("one_paragraph_summary", "moral_premise", "moral_premise"),
    ):
        run = latest_step_run(session, project_id, step_key, statuses=("approved", "stale"))
        value = (run.draft_json or {}).get(field_name) if run is not None else None
        if value:
            context[target] = value
    return context


def _project_payload(session: Session, project_id: str) -> dict[str, Any]:
    return project_payload(require_project(session, project_id))
