"""FE-ALIGN Phase 5: 实时派生待办（原型 ws-review rvDerived 的后端化）。

纯读函数：每次 GET 时从工作台真相（雪花步骤 / 目录 / 起草状态）现算，
不落库。三条语义（简报 ⚠️）：
① live=True，端点拒绝无动作 resolve；
② 源头修好，下次 GET 自动消失；
③ id 带内容指纹 —— 状况变化即新 id，即使旧指纹曾 snooze 也重新浮现
   （snooze 记录按指纹存 review_derived_snoozes）。

首批三类：雪花步骤空缺/不完整、目录结构异常（空章/成稿无字）、产出待办
（审阅中章待批准 / 全场成稿可送审 —— 对齐原型第 5 类派生）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard, SceneRunState, SnowflakeStepRun, StoryProject
from novel_system.services.catalog import CatalogService
from novel_system.services.snowflake_steps import list_step_definitions
from novel_system.services.hash_engine import sha256_text
from novel_system.services.snowflake_queries import step_gate_satisfied


def _fingerprint(*parts: Any) -> str:
    return sha256_text("|".join(str(p) for p in parts))[:10]


def derive_cards(session: Session, project_id: str) -> list[dict[str, Any]]:
    project = session.get(StoryProject, project_id)
    if project is None:
        return []
    # 目录载荷整本书拼一次，目录异常与管线停稿两类卡共用（以前各拼一遍，一次收件箱刷新拼好几遍全书）
    chapters = _catalog_chapter_payloads(session, project)
    cards: list[dict[str, Any]] = []
    cards.extend(_snowflake_gaps(session, project_id))
    cards.extend(_catalog_anomalies(chapters))
    cards.extend(_pipeline_blocked(session, project, chapters))
    return cards


def _catalog_chapter_payloads(session: Session, project: StoryProject) -> list[dict[str, Any]]:
    """整本书每一章的目录载荷（与章节编排同一份：场景 slug、标题、状态、字数），按章序。"""

    catalog = CatalogService(session)
    chapter_rows = catalog.chapter_rows(project.project_id)
    context = catalog.read_context(project.project_id, [chapter.chapter_id for chapter in chapter_rows])
    return [
        {"chapter": chapter, "payload": catalog.chapter_payload(project, chapter, index, context=context)}
        for index, chapter in enumerate(chapter_rows)
    ]


def _snowflake_gaps(session: Session, project_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        select(SnowflakeStepRun)
        .where(SnowflakeStepRun.project_id == project_id)
        .order_by(SnowflakeStepRun.version.asc(), SnowflakeStepRun.created_at.asc())
    ).scalars().all()
    latest: dict[str, SnowflakeStepRun] = {}
    for row in rows:
        if row.status != "superseded":
            latest[row.step_key] = row
    if not latest:
        return []  # 还没开始雪花：不催
    steps = list_step_definitions()
    cards: list[dict[str, Any]] = []
    max_satisfied_index = max(
        (index for index, step in enumerate(steps) if step_gate_satisfied(latest.get(step["step_key"]))),
        default=-1,
    )
    for index, step in enumerate(steps):
        run = latest.get(step["step_key"])
        if step_gate_satisfied(run):
            continue
        drafted = run is not None and run.status == "pending_review"
        gap_behind = index < max_satisfied_index  # 后续步骤已确认，本步却是空缺/未确认
        if not drafted and not gap_behind:
            continue  # 顺序推进中的「下一步」不算异常
        status_sig = run.status if run is not None else "missing"
        cards.append(
            {
                "id": f"derived:snowflake:{step['step_key']}:{_fingerprint(status_sig)}",
                "live": True,
                "kind": "idea",
                "priority": 1 if gap_behind else 2,
                "title": (
                    f"雪花「{step['label']}」{'已被跳过遗留空缺' if status_sig == 'missing' else '已起草未确认'}"
                ),
                "where": f"构思 · {step['label']}",
                "source": "雪花构思",
                "time": "实时",
                "detail": (
                    f"「{step['label']}」"
                    + ("缺失，而它之后的步骤已确认——补全后这条会自动消失。" if status_sig == "missing"
                       else "停在草稿态。回去确认（或显式跳过）后这条会自动消失。")
                ),
                "actions": [
                    {"label": "去补全", "intent": "primary", "op": "nav", "nav_to": "snowflake", "nav_step": step["step_key"]},
                    {"label": "稍后再说", "intent": "quiet", "op": "snooze"},
                ],
            }
        )
    return cards


# 管线停稿的阻塞态（scene_run_states.scene_status）→ 卡片文案（标签、说明、说明之后的「怎样算处理完」）。
# 只收「等人拍板/处理」的终局态；in-flight（bundle_built 等）与通过态不投递。
_PIPELINE_BLOCKED_STATUSES: dict[str, tuple[str, str]] = {
    "human_review_required": (
        "人工审阅",
        "管线把这稿停在人工审阅闸门——草稿已生成，等你在起草台裁决采纳或重跑。",
    ),
    # 关键场景的匿名候选终选（批准 #2，重评 R2）：多稿打开时关键场景停在这里等作者挑一份；运行本章遇到它也
    # 停下并指到这里——这张卡是作者不论从哪条路进来都找得到这一场的门
    "awaiting_candidate_selection": (
        "关键场景 · 等你终选",
        "去起草台读完候选再选一份，选完自动续跑。",
    ),
    "hard_qc_partial_rewrite_required": (
        "硬质检局部重写",
        "硬质检要求局部重写——可在起草台带改写指令重跑这一场。",
    ),
    "hard_qc_full_rewrite_required": (
        "硬质检整场重写",
        "硬质检要求整场重写——可在起草台带改写指令重跑这一场。",
    ),
    "soft_qc_patch_required": (
        "软质检补丁",
        "软质检要求补丁——去起草台处理后再归档。",
    ),
    "needs_replan": (
        "上游已变更",
        "上游构思/设定的变更让这稿需要重新规划——确认场景卡后在起草台重跑。",
    ),
}


def _pipeline_blocked(session: Session, project: StoryProject, chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """贯通轮遗留 ③：管线 blocked 的稿子投递进待办收件箱。

    真相源 = SceneRunState.scene_status（run job 的 blocked 终态落在这里）。
    目录场已 done（作者已在起草台采纳归档）的不投递——作者主权优先；
    指纹 = scene_status，状态变化即新卡（旧指纹的 snooze 不再遮它）。
    """
    rows = session.execute(
        select(SceneRunState, SceneCard)
        .join(SceneCard, SceneCard.scene_id == SceneRunState.scene_id)
        .where(SceneCard.project_id == project.project_id, SceneCard.trashed_flag == 0)
    ).all()
    blocked = [
        (state, card)
        for state, card in rows
        if state.scene_status in _PIPELINE_BLOCKED_STATUSES and str(card.state or "") != "done"
    ]
    if not blocked:
        return []
    slug_map: dict[str, tuple[str, str, str]] = {}
    for entry in chapters:
        payload = entry["payload"]
        for scene in payload["scenes"]:
            slug_map[scene["scene_id"]] = (scene["slug"], scene["title"], payload["no"])
    cards: list[dict[str, Any]] = []
    for state, card in blocked:
        slug, title, no = slug_map.get(card.scene_id, ("", "", ""))
        if not slug:
            continue
        label, detail = _PIPELINE_BLOCKED_STATUSES[state.scene_status]
        settle = (
            " 选完这条会自动消失。"
            if state.scene_status == "awaiting_candidate_selection"
            else " 处理（采纳归档 / 重跑 / 改场景卡）后这条会自动消失。"
        )
        cards.append(
            {
                "id": f"derived:pipeline:{card.scene_id}:{_fingerprint(state.scene_status)}",
                "live": True,
                "kind": "decision",
                "priority": 1,
                "title": f"第 {no} 章《{title}》的 AI 稿被管线停下 · {label}",
                "where": f"AI 起草台 · 第 {no} 章",
                "source": "起草管线",
                "time": "实时",
                "detail": detail + settle,
                "actions": [
                    {"label": "去起草台裁决", "intent": "primary", "op": "nav", "nav_to": "scene", "nav_scene": slug},
                    {"label": "稍后再说", "intent": "quiet", "op": "snooze"},
                ],
            }
        )
    return cards


def _catalog_anomalies(chapters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for entry in chapters:
        chapter = entry["chapter"]
        payload = entry["payload"]
        scenes = payload["scenes"]
        no = payload["no"]
        title = payload["title"]
        if not scenes:
            cards.append(
                {
                    "id": f"derived:catalog:empty:{chapter.chapter_id}:{_fingerprint(title)}",
                    "live": True,
                    "kind": "qc",
                    "priority": 2,
                    "title": f"第 {no} 章《{title}》还没有任何场景",
                    "where": f"章节编排 · 第 {no} 章",
                    "source": "章节编排",
                    "time": "实时",
                    "detail": "空章无法进入起草与成稿流程。去编排台加场景，或删除这一章。加上场景后这条会自动消失。",
                    "actions": [
                        {"label": "去编排", "intent": "primary", "op": "nav", "nav_to": "author"},
                        {"label": "稍后再说", "intent": "quiet", "op": "snooze"},
                    ],
                }
            )
            continue
        empty_done = [s for s in scenes if s["state"] == "done" and not s["words"]]
        if empty_done:
            sig = _fingerprint(*(s["scene_id"] for s in empty_done))
            cards.append(
                {
                    "id": f"derived:catalog:hollow:{chapter.chapter_id}:{sig}",
                    "live": True,
                    "kind": "qc",
                    "priority": 2,
                    "title": f"第 {no} 章有 {len(empty_done)} 场标了成稿却没有正文",
                    "where": f"章节编排 · 第 {no} 章",
                    "source": "起草队列",
                    "time": "实时",
                    "detail": "场景状态是 done 但字数为 0——要么正文没保存，要么状态标错了。修正后这条会自动消失。",
                    "actions": [
                        {"label": "去写作器看", "intent": "primary", "op": "nav", "nav_to": "writer"},
                        {"label": "稍后再说", "intent": "quiet", "op": "snooze"},
                    ],
                }
            )
        if payload["state"] == "review":
            cards.append(
                {
                    "id": f"derived:catalog:review:{chapter.chapter_id}:{_fingerprint(payload['words']['cur'])}",
                    "live": True,
                    "kind": "decision",
                    "priority": 1,
                    "title": f"第 {no} 章《{title}》待你批准",
                    "where": f"成稿中心 · 第 {no} 章",
                    "source": "成稿中心",
                    "time": "实时",
                    "detail": f"本章 {len(scenes)} 场、{payload['words']['cur']:,} 字，正在审阅中。批准为终稿，或退回小修。",
                    "actions": [
                        {"label": "去审阅", "intent": "primary", "op": "nav", "nav_to": "manuscripts"},
                        {"label": "稍后再说", "intent": "quiet", "op": "snooze"},
                    ],
                }
            )
        elif payload["state"] == "writing" and scenes and all(s["state"] == "done" for s in scenes):
            cards.append(
                {
                    "id": f"derived:catalog:submit:{chapter.chapter_id}:{_fingerprint(len(scenes))}",
                    "live": True,
                    "kind": "qc",
                    "priority": 2,
                    "title": f"第 {no} 章全部场景已成稿 · 可送审",
                    "where": f"成稿中心 · 第 {no} 章",
                    "source": "成稿中心",
                    "time": "实时",
                    "detail": f"《{title}》的 {len(scenes)} 场全部完成。在成稿中心送入审阅，进入批准流程。",
                    "actions": [
                        {"label": "去送审", "intent": "primary", "op": "nav", "nav_to": "manuscripts"},
                        {"label": "稍后再说", "intent": "quiet", "op": "snooze"},
                    ],
                }
            )
    return cards
