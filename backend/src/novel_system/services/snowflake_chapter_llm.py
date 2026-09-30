"""分章的两个 AI 顾问调用：分章建议（P3）与 AI 起章名（阶段 W）——提示载荷、白名单归一、章名清洗。

2026-09-30 从雪花工作区 LLM 服务里搬出（B07-17）。调用经传进来的雪花 LLM 服务的 ``_run_structured_task``：
与整步生成同一条计量 / 审计 / 输入预算 / fail-closed 路径（``SNOWFLAKE_LLM_NOT_CONFIGURED``），也同一个节点路由
``snowflake_chapter_plan``。``SnowflakeWorkspaceLLMService.chapter_plan_suggestions`` / ``chapter_title_suggestions``
是转到这里的同名入口。放在分章包外面：雪花 LLM 服务要在模块顶层引它，而分章包的服务又引着雪花 LLM 服务。

两个调用都 **fail-closed**（不给 ``fallback_payload``）：作者点的是「让 AI 建议」，LLM 没配好时返回一份规则算出来
的东西并称之为建议就是撒谎；规则分章本来就以几种策略明明白白摆在面板上。
"""

from __future__ import annotations

import re
from typing import Any

#: 章名的硬上限（字符）。提示词要的是 2–10 个字；超过这个数的不是章名，是一句话。
CHAPTER_TITLE_MAX_CHARS = 24
CHAPTER_SUMMARY_MAX_CHARS = 120
_TITLE_WRAPPERS = "《》〈〉「」『』“”\"'‘’【】[]（）()"
_TITLE_NUMBER_PREFIX = re.compile(
    r"^\s*(?:第\s*[0-9０-９一二三四五六七八九十百千零〇两]+\s*[章回节幕卷]|chapter\s*[0-9ivxlc]+)\s*[:：·\-—.、\s]*",
    re.IGNORECASE,
)
_GENERIC_TITLES = frozenset({"序幕", "开端", "发展", "高潮", "转折", "结局", "风波", "真相", "危机", "尾声", "开始", "结束"})
#: 两个调用共用的节点路由（同一个面板、同一类顾问调用；另立节点的话，已保存模型快照的安装还得先点「一键补齐」）
CHAPTER_PLAN_NODE = "snowflake_chapter_plan"


def chapter_plan_suggestions(
    llm: Any,
    *,
    project: dict[str, Any],
    chapters: list[dict[str, Any]],
    scenes: list[dict[str, Any]],
    current_assignment: list[dict[str, Any]],
) -> Any:
    """分章建议（P3，顾问通道）。``scenes`` 带全书故事序号 ``story_index``（B07-13）。"""
    prompt_payload = {
        "project": project,
        "chapters": chapters,
        "scenes": scenes,
        "current_assignment": current_assignment,
    }
    allowed_scene_ids = {str(item.get("scene_plan_id") or "") for item in scenes}
    allowed_chapter_uids = {str(item.get("row_uid") or "") for item in chapters}
    return llm._run_structured_task(
        task_key=CHAPTER_PLAN_NODE,
        template_name="snowflake_chapter_plan_suggest",
        project_id=str(project.get("project_id") or ""),
        step_ref="long_synopsis",
        prompt_payload=prompt_payload,
        normalize_output=lambda output: normalize_chapter_plan_output(output, allowed_scene_ids, allowed_chapter_uids),
    )


def chapter_title_suggestions(
    llm: Any,
    *,
    project: dict[str, Any],
    book: dict[str, Any],
    chapters: list[dict[str, Any]],
    named_chapters: list[dict[str, Any]],
    reference_titles: dict[str, Any] | None = None,
) -> Any:
    """AI 起章名（阶段 W，顾问通道）：给 ``chapters`` 里每一章一个章名和一句章摘要。

    2026-09-22 结构跟随参考书：``reference_titles``（参考作家的题名样例与形态）进载荷；样例本身
    当作已占用的名字，模型照抄一条就当重复丢掉。
    """
    prompt_payload = {
        "project": project,
        "book": book,
        "named_chapters": named_chapters,
        "chapters": chapters,
    }
    if reference_titles:
        prompt_payload["reference_titles"] = dict(reference_titles)
    allowed_row_uids = {str(item.get("row_uid") or "") for item in chapters}
    taken_titles = {str(item.get("title") or "").strip() for item in named_chapters}
    taken_titles |= {
        str(item or "").strip()
        for item in ((reference_titles or {}).get("samples") or [])
        if str(item or "").strip()
    }
    return llm._run_structured_task(
        task_key=CHAPTER_PLAN_NODE,
        template_name="snowflake_chapter_titles_suggest",
        project_id=str(project.get("project_id") or ""),
        step_ref="long_synopsis",
        prompt_payload=prompt_payload,
        normalize_output=lambda output: normalize_chapter_titles_output(output, allowed_row_uids, taken_titles),
    )


def normalize_chapter_plan_output(
    output: dict[str, Any],
    allowed_scene_ids: set[str],
    allowed_chapter_uids: set[str],
) -> dict[str, Any]:
    """把模型给的分章建议约束回一份可安全展示的提案。

    服务端硬约束（模型违约时是过滤掉，不是报错——建议本来就允许不完美，但绝不能
    因为它编了一个不存在的 id 就把作者的场丢掉或绑到不存在的章上）：
    - 只保留 id 在白名单里的条目；
    - 一个场只认第一次出现（重复分配会让场在两章里各出现一次）；
    - 没被提到的场不在这里补——调用方按「缺哪些」如实展示。
    """
    raw = output.get("assignments") if isinstance(output, dict) else None
    assignments: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        scene_plan_id = str(item.get("scene_plan_id") or "").strip()
        chapter_row_uid = str(item.get("chapter_row_uid") or "").strip()
        if scene_plan_id not in allowed_scene_ids or chapter_row_uid not in allowed_chapter_uids:
            continue
        if scene_plan_id in seen:
            continue
        seen.add(scene_plan_id)
        assignments.append({"scene_plan_id": scene_plan_id, "chapter_row_uid": chapter_row_uid})
    rationale = str((output or {}).get("rationale") or "").strip()[:600]
    return {
        "assignments": assignments,
        "rationale": rationale,
        "missing_scene_plan_ids": sorted(allowed_scene_ids - seen),
    }


def clean_chapter_title(value: Any) -> str:
    """模型给的章名 → 可以直接落进章表的章名；不合格返回空串（那一章就留着等作者起名）。

    去掉包裹的引号 / 书名号、模型自己加的「第三章：」前缀（章号由系统编）、句末标点；
    空的、超长的（一句话不是章名）、光秃秃的结构标签（高潮 / 结局…）一律不要。
    """
    title = str(value or "").strip()
    title = _TITLE_NUMBER_PREFIX.sub("", title).strip()
    while title and title[0] in _TITLE_WRAPPERS:
        title = title[1:].strip()
    while title and title[-1] in _TITLE_WRAPPERS + "。．.！!？?，,；;：:、":
        title = title[:-1].strip()
    if not title or len(title) > CHAPTER_TITLE_MAX_CHARS or title in _GENERIC_TITLES:
        return ""
    return title


def normalize_chapter_titles_output(
    output: dict[str, Any],
    allowed_row_uids: set[str],
    taken_titles: set[str],
) -> dict[str, Any]:
    """把模型起的章名约束回一份可以安全展示的提案（违约的条目过滤掉，不报错——建议允许不完美）。

    - 只认白名单里的 ``row_uid``，一章只认第一次出现；
    - 章名过 ``clean_chapter_title``；与已有章名、与本批前面的章名重复的不要（全书章名互不相同）；
    - 章摘要截到上限；章名不合格时整条不要（只有摘要的条目没有意义）。
    """
    raw = output.get("titles") if isinstance(output, dict) else None
    titles: list[dict[str, str]] = []
    seen_uids: set[str] = set()
    used = {title for title in taken_titles if title}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        row_uid = str(item.get("row_uid") or "").strip()
        if row_uid not in allowed_row_uids or row_uid in seen_uids:
            continue
        title = clean_chapter_title(item.get("title"))
        if not title or title in used:
            continue
        seen_uids.add(row_uid)
        used.add(title)
        summary = " ".join(str(item.get("summary") or "").split())[:CHAPTER_SUMMARY_MAX_CHARS]
        titles.append({"row_uid": row_uid, "title": title, "summary": summary})
    return {"titles": titles, "missing_row_uids": sorted(allowed_row_uids - seen_uids)}
