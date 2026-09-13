"""构思侧分章：07 章表 × 09 场景列表 → 章节结构（Phase 2）。

历史上「整理成章节结构」没有任何一方真正在分章：09/10 步没有章字段，提示词让
LLM 把 chapter_id 留空说「server assigns」，而服务端的起始值就是 ``{project_id}_CH01``
且把作者传入的 chapter_id 当系统身份丢弃 —— 于是全书落进一章。唯一编了章的 07 长篇
大纲在后端只是四段自由文本，物化时完全不读。

这个模块把分章变成一次**可预览、可调整、可确认**的显式动作：
- ``preview`` 是纯函数式的只读推演（不写库），三种策略都确定性可复算；
- ``save`` 把作者在面板里确认的归属落到 ``SnowflakeScenePlan.chapter_plan_id`` /
  ``chapter_id`` / ``scene_seq``；
- 物化改读章表分组，章标题/章目标来自作者在 07 写的东西，而不是章 id 字符串。

脊柱锚点算法取自原前端 ``s2MaterializePreview``（那条降级路径反而是唯一分对章的），
搬到后端成为唯一实现，前端不再持有第二套。
"""

from __future__ import annotations

import math
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    OperationLog,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    SnowflakeStepRun,
    utcnow,
)
from novel_system.services.errors import DomainError

STRATEGIES = ("spine_anchor", "even", "keep_current")
SPINE_MARKS = ("灾一", "灾二", "灾三")
_SPINE_PATTERN = re.compile(r"灾[一二三]")
# NN 章名：一句话（灾一）—— 提示词 snowflake_generate_long_synopsis 要求的行格式，
# 也是前端 07 脚手架折成 paragraphs 时用的格式。
_OUTLINE_LINE = re.compile(r"^(\d+)\s+([^：:]+)[：:]?(.*)$")

# 一章分到的场数超过均值这么多倍时给个提醒（不阻断——长章是合法的作者选择）
_OVERSIZED_RATIO = 3.0


def mint_chapter_row_uid() -> str:
    return f"chrow_{uuid.uuid4().hex}"


def chapter_target_id(project_id: str, chapter_seq: int) -> str:
    """章的物化目标 id。与 ``display_order`` 同源，保证目录里的章序稳定。"""
    return f"{project_id}_CH{chapter_seq:02d}"


def scene_spine(plan: SnowflakeScenePlan) -> str:
    """场景的脊柱标记：作者显式标注优先，其次从 chapter_role 里认灾难标记。

    提示词要求 LLM 「mark the three disaster scenes in chapter_role」（例如
    「灾难一·一幕高潮」），所以 LLM 生成的场景表没有显式 spine 也能锚定。
    """
    explicit = str(getattr(plan, "spine", "") or "").strip()
    if explicit:
        return explicit if explicit in SPINE_MARKS else ""
    match = _SPINE_PATTERN.search(str(plan.chapter_role or ""))
    return match.group(0) if match else ""


class SnowflakeChapteringService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ 读

    def chapter_plans(self, project_id: str) -> list[SnowflakeChapterPlan]:
        return list(
            self.session.execute(
                select(SnowflakeChapterPlan)
                .where(
                    SnowflakeChapterPlan.project_id == project_id,
                    SnowflakeChapterPlan.removed_at.is_(None),
                )
                .order_by(SnowflakeChapterPlan.chapter_seq.asc(), SnowflakeChapterPlan.row_uid.asc())
            ).scalars()
        )

    def scene_plans(self, project_id: str) -> list[SnowflakeScenePlan]:
        return list(
            self.session.execute(
                select(SnowflakeScenePlan)
                .where(
                    SnowflakeScenePlan.project_id == project_id,
                    SnowflakeScenePlan.removed_at.is_(None),
                )
                .order_by(SnowflakeScenePlan.scene_seq.asc(), SnowflakeScenePlan.scene_id.asc())
            ).scalars()
        )

    # ------------------------------------------------------ 章表惰性派生

    def ensure_chapter_plans(self, project_id: str) -> list[SnowflakeChapterPlan]:
        """保证项目有一份章表可用（幂等）。

        章表本来由 07 步保存时同步（``_sync_chapter_plans``），但在这个功能存在之前
        就确认过 07 的项目没有章表行，所以这里按优先级补：

        1. 已经有章表 → 直接用（作者的分章成果，绝不覆盖）。
        2. 已物化过（有 ``ChapterGoal``）→ 从目录反推，让「打开分章面板」看到的是现状，
           而不是一份把已有章节结构抹平的空白重排。
        3. 否则 → 解析 07 长篇大纲草稿。

        放在服务层而不是迁移里：解析自由文本、章序缺号、已物化项目要保持现状，
        这些都是有真实边界情况的业务逻辑，需要可单测、可重算、出错可重试。
        """
        existing = self.chapter_plans(project_id)
        if existing:
            return existing
        derived = (
            self._derive_from_catalog(project_id)
            or self._derive_from_scene_chapter_ids(project_id)
            or self._derive_from_long_synopsis(project_id)
        )
        if not derived:
            return []
        created = [self._create_chapter_plan(project_id, item) for item in derived]
        # 来源自带归属的（目录 / 场景行上的 chapter_id）：归属是既成事实，不是待决策项，
        # 建完章顺手绑上，作者不必为「系统已经知道的事」再点一次确认。
        by_source_chapter_id = {
            str(item.get("source_chapter_id") or ""): row
            for item, row in zip(derived, created)
            if item.get("source_chapter_id")
        }
        if by_source_chapter_id:
            for plan in self.scene_plans(project_id):
                if plan.chapter_plan_id:
                    continue
                target = by_source_chapter_id.get(plan.chapter_id or "")
                if target is not None:
                    plan.chapter_plan_id = target.chapter_plan_id
        self.session.flush()
        return self.chapter_plans(project_id)

    def _derive_from_scene_chapter_ids(self, project_id: str) -> list[dict[str, Any]]:
        """场景行自带的章归属（规划器骨架、以及任何回填了 chapter_id 的 LLM 输出）。

        **只在出现两个及以上不同章号时才认**：前端 ``canonFromFE("scenes")`` 不发
        chapter_id，服务端于是给所有场同一个 ``{project_id}_CH01`` —— 那是退化默认值，
        不是作者的分章决定。把它当成「已分章」正是这次要修的「全书落进一章」。
        """
        scenes = self.scene_plans(project_id)
        ordered_chapter_ids: list[str] = []
        for plan in scenes:
            chapter_id = str(plan.chapter_id or "").strip()
            if chapter_id and chapter_id not in ordered_chapter_ids:
                ordered_chapter_ids.append(chapter_id)
        if len(ordered_chapter_ids) < 2:
            return []
        first_by_chapter: dict[str, SnowflakeScenePlan] = {}
        for plan in scenes:
            first_by_chapter.setdefault(str(plan.chapter_id or "").strip(), plan)
        derived: list[dict[str, Any]] = []
        for index, chapter_id in enumerate(ordered_chapter_ids, start=1):
            sample = first_by_chapter[chapter_id]
            title = str(sample.chapter_title or "").strip()
            derived.append(
                {
                    "row_uid": "",
                    "chapter_seq": index,
                    "act": 1,
                    "title": title if title and title != chapter_id else f"第 {index} 章",
                    "summary": str(sample.chapter_goal or "").strip(),
                    "spine": scene_spine(sample),
                    "chapter_goal": str(sample.chapter_goal or "").strip(),
                    "source_chapter_id": chapter_id,
                }
            )
        return derived

    def _create_chapter_plan(self, project_id: str, item: dict[str, Any]) -> SnowflakeChapterPlan:
        row_uid = str(item.get("row_uid") or "").strip() or mint_chapter_row_uid()
        row = SnowflakeChapterPlan(
            chapter_plan_id=f"snowflake_chapter_plan_{project_id}_{row_uid}",
            project_id=project_id,
            row_uid=row_uid,
            chapter_seq=int(item.get("chapter_seq") or 1),
            act=_coerce_act(item.get("act"), 1),
            title=str(item.get("title") or "").strip(),
            summary=str(item.get("summary") or "").strip(),
            spine=str(item.get("spine") or "").strip(),
            chapter_goal=str(item.get("chapter_goal") or "").strip(),
            status="draft",
        )
        self.session.add(row)
        return row

    def _derive_from_catalog(self, project_id: str) -> list[dict[str, Any]]:
        rows = list(
            self.session.execute(
                select(ChapterGoal).where(
                    ChapterGoal.project_id == project_id,
                    ChapterGoal.trashed_flag == 0,
                )
            ).scalars()
        )
        if not rows:
            return []
        rows.sort(key=lambda chapter: (chapter.display_order is None, chapter.display_order or 0, chapter.chapter_id))
        derived: list[dict[str, Any]] = []
        for index, chapter in enumerate(rows, start=1):
            narrative = dict(chapter.narrative_json or {})
            brief = dict(chapter.writer_brief_json or {})
            goal = str(chapter.chapter_goal or "").strip()
            title = (
                str(narrative.get("title") or "").strip()
                or str(brief.get("chapter_title") or "").strip()
                or (goal.splitlines()[0][:24] if goal else "")
                or chapter.chapter_id
            )
            derived.append(
                {
                    "row_uid": "",
                    "chapter_seq": index,
                    "act": _coerce_act(narrative.get("act"), 1),
                    "title": title,
                    "summary": goal,
                    "spine": str(narrative.get("spine") or "").strip(),
                    "chapter_goal": goal,
                    # 已物化项目里场景行的 chapter_id 就等于 ChapterGoal 的主键，
                    # 所以归属可以直接按它对上，不需要作者重新指派。
                    "source_chapter_id": chapter.chapter_id,
                }
            )
        return derived

    def _derive_from_long_synopsis(self, project_id: str) -> list[dict[str, Any]]:
        run = self.session.execute(
            select(SnowflakeStepRun)
            .where(
                SnowflakeStepRun.project_id == project_id,
                SnowflakeStepRun.step_key == "long_synopsis",
                SnowflakeStepRun.status != "superseded",
            )
            .order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.created_at.desc())
        ).scalars().first()
        if run is None:
            return []
        return parse_outline_chapters(run.draft_json or {})

    # -------------------------------------------------------------- 预览

    def preview(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = payload or {}
        strategy = str(body.get("strategy") or "spine_anchor").strip()
        if strategy not in STRATEGIES:
            raise DomainError(
                "SNOWFLAKE_CHAPTER_STRATEGY_INVALID",
                f"分章策略只能是 {'、'.join(STRATEGIES)} 之一。",
                status_code=400,
            )
        chapters = self.ensure_chapter_plans(project_id)
        scenes = self.scene_plans(project_id)
        if not chapters:
            raise DomainError(
                "SNOWFLAKE_CHAPTER_PLAN_EMPTY",
                "07 长篇大纲还没有可用章节，先去把章列出来再分章。",
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

        assignment = _assign(strategy, chapters, scenes)
        return self._shape_preview(project_id, strategy, chapters, scenes, assignment)

    def _shape_preview(
        self,
        project_id: str,
        strategy: str,
        chapters: list[SnowflakeChapterPlan],
        scenes: list[SnowflakeScenePlan],
        assignment: dict[str, str | None],
    ) -> dict[str, Any]:
        by_chapter: dict[str, list[SnowflakeScenePlan]] = {chapter.row_uid: [] for chapter in chapters}
        unassigned: list[SnowflakeScenePlan] = []
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
                    "chapter_id": chapter_target_id(project_id, index),
                    "act": int(chapter.act or 1),
                    "title": chapter.title or "",
                    "summary": chapter.summary or "",
                    "spine": chapter.spine or "",
                    "chapter_goal": chapter.chapter_goal or chapter.summary or "",
                    "scene_count": len(members),
                    "scenes": [
                        {
                            "scene_plan_id": scene.scene_plan_id,
                            "row_uid": scene.row_uid or "",
                            "scene_id": scene.scene_id,
                            "scene_seq": seq,
                            "title": scene.title or scene.summary or scene.scene_id,
                            "primary_form": scene.scene_type or "proactive",
                            "spine": scene_spine(scene),
                            "anchored": bool(scene_spine(scene)) and scene_spine(scene) == (chapter.spine or ""),
                            "planned": bool((scene.goal or scene.reaction or "").strip()),
                            # 阶段 C：概述两段的反应场在节奏体检里按半场计
                            "rendering_mode": (scene.rendering_mode or "full") if (scene.scene_type or "proactive") == "reactive" else "full",
                        }
                        for seq, scene in enumerate(members, start=1)
                    ],
                }
            )

        warnings = self._warnings(project_id, chapter_payloads, unassigned)
        return {
            "strategy": strategy,
            "rhythm": _rhythm_report(chapter_payloads),
            "chapters": chapter_payloads,
            "unassigned": [
                {
                    "scene_plan_id": scene.scene_plan_id,
                    "scene_id": scene.scene_id,
                    "title": scene.title or scene.summary or scene.scene_id,
                    "reason": "no_anchor_segment",
                }
                for scene in unassigned
            ],
            "removed_scenes": self._orphaned_payload(project_id),
            "warnings": warnings,
            "totals": {
                "chapter_count": len(chapter_payloads),
                "scene_count": sum(item["scene_count"] for item in chapter_payloads),
                "unassigned_count": len(unassigned),
            },
        }

    def _orphaned_payload(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(SnowflakeScenePlan).where(
                SnowflakeScenePlan.project_id == project_id,
                SnowflakeScenePlan.orphaned_flag == 1,
                SnowflakeScenePlan.removed_at.is_(None),
            )
        ).scalars().all()
        return [
            {
                "scene_plan_id": plan.scene_plan_id,
                "scene_id": plan.scene_id,
                "title": plan.title or plan.summary or plan.scene_id,
                "orphaned": True,
            }
            for plan in rows
        ]

    #: 孤儿场的两个合法去向。blocker 文案「请先决定是一并删除还是保留」承诺的就是这两个。
    ORPHAN_RESOLUTIONS = ("discard", "keep")

    def resolve_orphan(
        self,
        project_id: str,
        scene_plan_id: str,
        *,
        action: str,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        """处置一个孤儿场——作者从 09 删掉了它，但目录里的场景卡可能已经有正文。

        没有这个动作时 ``orphaned_flag`` 是只写字段：blocker 永久挂在分章面板上，
        「确认分章」再也点不动，而提示语还在说「请先决定」——一个没有对应动作的决定。

        - ``discard``：正文也不要了。场景卡进回收站（可恢复，不是物理删除——那上面
          可能有作者写了几千字的稿子），计划行软删。
        - ``keep``：正文留在目录里，只是不再属于构思侧的场景列表。计划行软删，场景卡
          原样不动。

        两条路都软删计划行，孤儿警告因此**持久**消失（``_orphaned_payload`` 过滤
        ``removed_at``）；只清 ``orphaned_flag`` 不行，下一次 PATCH 场景列表会立刻
        把它重新标成孤儿，作者陷在同一个循环里。
        """
        if action not in self.ORPHAN_RESOLUTIONS:
            raise DomainError(
                "SNOWFLAKE_ORPHAN_ACTION_INVALID",
                f"孤儿场的处置只能是 {' / '.join(self.ORPHAN_RESOLUTIONS)}。",
                status_code=400,
            )
        plan = self.session.get(SnowflakeScenePlan, scene_plan_id)
        if plan is None or plan.project_id != project_id:
            raise DomainError("SNOWFLAKE_SCENE_PLAN_NOT_FOUND", "找不到这一场。", status_code=404)
        if not plan.orphaned_flag or plan.removed_at:
            raise DomainError(
                "SNOWFLAKE_SCENE_NOT_ORPHANED",
                "这一场不是待处置的孤儿场——可能已经处置过，或者它又回到了场景列表里。",
                status_code=409,
            )

        trashed_scene = False
        if action == "discard":
            from novel_system.services.trash import TrashService

            # 场景卡走回收站而不是物理删除：作者随时可以在回收站里反悔。
            TrashService(self.session).trash_scene_in_project(
                project_id, plan.scene_id, actor_ref=actor_ref
            )
            trashed_scene = True

        plan.orphaned_flag = 0
        plan.removed_at = utcnow()
        plan.removed_by = actor_ref
        plan.chapter_plan_id = None
        self.session.add(
            OperationLog(
                event_type="snowflake_scene_plan_orphan_resolved",
                object_type="snowflake_scene_plan",
                object_ref=plan.scene_plan_id,
                payload_json={
                    "project_id": project_id,
                    "scene_id": plan.scene_id,
                    "action": action,
                    "trashed_scene_card": trashed_scene,
                    "actor_ref": actor_ref,
                },
            )
        )
        self.session.flush()
        return {
            "scene_plan_id": plan.scene_plan_id,
            "scene_id": plan.scene_id,
            "action": action,
            "trashed_scene_card": trashed_scene,
            "orphaned_remaining": len(self._orphaned_payload(project_id)),
        }

    def _warnings(
        self,
        project_id: str,
        chapter_payloads: list[dict[str, Any]],
        unassigned: list[SnowflakeScenePlan],
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
        empty = [item for item in chapter_payloads if not item["scene_count"]]
        for item in empty:
            warnings.append(
                {
                    "kind": "empty_chapter",
                    "severity": "warning",
                    "message": f"「{item['title'] or item['chapter_id']}」没有分到任何场，物化后会是一章空壳。",
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
                                f"「{item['title'] or item['chapter_id']}」分到 {item['scene_count']} 场，"
                                f"约是平均值（{mean:.1f}）的 {item['scene_count'] / mean:.1f} 倍。"
                            ),
                        }
                    )
        # 节奏体检的提示（P3）：只提醒、从不阻断 —— 作者故意把灾二后置是合法选择。
        for item in _rhythm_report(chapter_payloads)["spine_placement"]:
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

        for item in self._orphaned_payload(project_id):
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

    # -------------------------------------------------------------- 落库

    def save(
        self,
        project_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        body = payload or {}
        chapters = self.ensure_chapter_plans(project_id)
        by_row_uid = {chapter.row_uid: chapter for chapter in chapters}
        scenes = {plan.scene_plan_id: plan for plan in self.scene_plans(project_id)}

        # 章的元数据（标题/幕/脊柱/章目标/顺序）——作者可以在面板里直接改
        incoming_chapters = [item for item in (body.get("chapters") or []) if isinstance(item, dict)]
        for index, item in enumerate(incoming_chapters, start=1):
            row_uid = str(item.get("row_uid") or "").strip()
            chapter = by_row_uid.get(row_uid)
            if chapter is None:
                raise DomainError(
                    "SNOWFLAKE_CHAPTER_PLAN_NOT_FOUND",
                    f"未找到章「{item.get('title') or row_uid}」。",
                    status_code=404,
                )
            chapter.chapter_seq = index
            if "title" in item:
                chapter.title = str(item.get("title") or "").strip()
            if "act" in item:
                chapter.act = _coerce_act(item.get("act"), chapter.act)
            if "spine" in item:
                spine = str(item.get("spine") or "").strip()
                chapter.spine = spine if spine in SPINE_MARKS else ""
            if "chapter_goal" in item:
                chapter.chapter_goal = str(item.get("chapter_goal") or "").strip()
            if "summary" in item:
                chapter.summary = str(item.get("summary") or "").strip()

        # 场景归属
        assignments = [item for item in (body.get("assignments") or []) if isinstance(item, dict)]
        if not assignments:
            raise DomainError(
                "SNOWFLAKE_CHAPTER_ASSIGNMENTS_REQUIRED",
                "没有收到任何场景归属，无法保存分章。",
                status_code=400,
            )
        seq_by_chapter: dict[str, int] = {}
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
            chapter = by_row_uid.get(row_uid)
            if chapter is None:
                raise DomainError(
                    "SNOWFLAKE_CHAPTER_PLAN_NOT_FOUND",
                    "分章里引用了不存在的章。",
                    status_code=404,
                    details={"chapter_row_uid": row_uid},
                )
            next_seq = seq_by_chapter.get(row_uid, 0) + 1
            seq_by_chapter[row_uid] = next_seq
            plan.chapter_plan_id = chapter.chapter_plan_id
            plan.chapter_id = chapter_target_id(project_id, chapter.chapter_seq)
            plan.chapter_title = chapter.title or plan.chapter_title
            plan.chapter_goal = chapter.chapter_goal or chapter.summary or plan.chapter_goal
            plan.scene_seq = next_seq
            touched.append(plan)

        self.session.add(
            OperationLog(
                event_type="snowflake_chapter_plan_saved",
                object_type="story_project",
                object_ref=project_id,
                payload_json={
                    "project_id": project_id,
                    "chapter_count": len(chapters),
                    "assigned_scene_count": len(touched),
                    "actor_ref": actor_ref or "operator",
                    "saved_at": utcnow(),
                },
            )
        )
        self.session.flush()
        return {"assigned_scene_count": len(touched)}

    def suggest(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """让 LLM 给一份分章建议（P3，只读 —— 不落库）。

        回包的形状和 ``preview`` 一致（chapters/unassigned/warnings/rhythm），面板可以
        直接把它当成另一份「候选预览」渲染，作者采纳或丢弃都只是本地状态。多出一个
        ``rationale``：模型自己说哪几个断章它最没把握，作者知道先看哪里。
        """
        from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService

        base = self.preview(project_id, {"strategy": (payload or {}).get("base_strategy") or "spine_anchor"})
        chapters = self.ensure_chapter_plans(project_id)
        scenes = self.scene_plans(project_id)
        result = SnowflakeWorkspaceLLMService(self.session).chapter_plan_suggestions(
            project=self._project_payload(project_id),
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
            scenes=[
                {
                    "scene_plan_id": plan.scene_plan_id,
                    "scene_seq": plan.scene_seq,
                    "title": plan.title or plan.summary or plan.scene_id,
                    "summary": plan.summary or "",
                    "primary_form": plan.scene_type or "proactive",
                    "spine": scene_spine(plan),
                }
                for plan in scenes
            ],
            current_assignment=[
                {"scene_plan_id": scene["scene_plan_id"], "chapter_row_uid": chapter["row_uid"]}
                for chapter in base["chapters"]
                for scene in chapter["scenes"]
            ],
            approved_context=[],
        )
        suggested = {
            item["scene_plan_id"]: item["chapter_row_uid"] for item in result.payload.get("assignments") or []
        }
        # 模型没提到的场保留确定性提案的归属 —— 建议是叠加，不是全量替换
        assignment = {
            scene.scene_plan_id: suggested.get(
                scene.scene_plan_id,
                next(
                    (c["row_uid"] for c in base["chapters"] for s in c["scenes"] if s["scene_plan_id"] == scene.scene_plan_id),
                    None,
                ),
            )
            for scene in scenes
        }
        shaped = self._shape_preview(project_id, "llm_suggested", chapters, scenes, assignment)
        shaped["rationale"] = result.payload.get("rationale") or ""
        shaped["source"] = result.source
        shaped["llm_call_id"] = result.llm_call_id
        shaped["kept_from_deterministic"] = sorted(result.payload.get("missing_scene_plan_ids") or [])
        return shaped

    def _project_payload(self, project_id: str) -> dict[str, Any]:
        from novel_system.services.projects import ProjectService, project_payload

        return project_payload(ProjectService(self.session).require_project(project_id))

    def autoassign(
        self,
        project_id: str,
        strategy: str,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        """按指定策略直接落一版分章（不经预览面板）。

        给脚本 / API 调用方用：策略由调用方显式指名，结果与 ``preview`` 同一套算法，
        所以「预览看到什么就是什么」的承诺不会因为走了这条路而失效。
        """
        preview = self.preview(project_id, {"strategy": strategy})
        payload = {
            "chapters": [
                {
                    "row_uid": chapter["row_uid"],
                    "title": chapter["title"],
                    "act": chapter["act"],
                    "spine": chapter["spine"],
                    "chapter_goal": chapter["chapter_goal"],
                }
                for chapter in preview["chapters"]
            ],
            "assignments": [
                {
                    "scene_plan_id": scene["scene_plan_id"],
                    "chapter_row_uid": chapter["row_uid"],
                    "scene_seq": scene["scene_seq"],
                }
                for chapter in preview["chapters"]
                for scene in chapter["scenes"]
            ],
        }
        if not payload["assignments"]:
            raise DomainError(
                "SNOWFLAKE_CHAPTER_ASSIGNMENTS_REQUIRED",
                f"策略「{strategy}」没有分配出任何场景归属。",
                status_code=409,
                details={"warnings": preview["warnings"]},
            )
        return self.save(project_id, payload, actor_ref=actor_ref)

    # ------------------------------------------------------- 物化前置检查

    def status(self, project_id: str, scene_plans: list[SnowflakeScenePlan]) -> dict[str, Any]:
        """分章现状（**只读**，不建行、不绑定）。

        必须和 ``materialize`` 看到的是同一个真相：物化会先 ``ensure_chapter_plans``，
        把目录 / 场景行上已经存在的章归属派生并绑定，所以这里也要把「还没落库、但一
        物化就会自动绑上」的那部分算作已分章 —— 否则工作台报 blocked、实际却能物化，
        作者对着一个假闸门发懵。
        """
        chapters = self.chapter_plans(project_id)
        if chapters:
            valid = {chapter.chapter_plan_id for chapter in chapters}
            unassigned = [
                plan for plan in scene_plans if not plan.chapter_plan_id or plan.chapter_plan_id not in valid
            ]
            chapter_count = len(chapters)
        else:
            # 派生优先级必须与 ensure_chapter_plans 逐字一致，包括 07 长篇大纲那一级。
            # 漏掉它的后果是诊断撒谎：作者在 07 里明明编了章表，闸门却报「还没有分章：
            # 章节结构要先决定每一场归哪一章」，把他支回 07 去做一件已经做完的事。
            # 真相是章有了、只是还没有一场绑上去——那是另一句话、另一个动作。
            derived = (
                self._derive_from_catalog(project_id)
                or self._derive_from_scene_chapter_ids(project_id)
                or self._derive_from_long_synopsis(project_id)
            )
            # 只有目录 / 场景行那两级自带既成归属；07 章表派生出来的章还没有任何场绑定，
            # 所以 bindable 为空、全部场算未分章——这正是此时的真相，不是缺陷。
            bindable = {str(item.get("source_chapter_id") or "") for item in derived if item.get("source_chapter_id")}
            unassigned = [plan for plan in scene_plans if str(plan.chapter_id or "") not in bindable]
            chapter_count = len(derived)
        return {
            "chapter_count": chapter_count,
            "assigned_scene_count": len(scene_plans) - len(unassigned),
            "unassigned_scene_count": len(unassigned),
            "unassigned_scenes": [
                {
                    "scene_plan_id": plan.scene_plan_id,
                    "scene_id": plan.scene_id,
                    "title": plan.title or plan.summary or plan.scene_id,
                }
                for plan in unassigned[:20]
            ],
            "chaptered": bool(chapter_count) and not unassigned,
        }

    def unassigned_scene_plans(self, project_id: str) -> list[SnowflakeScenePlan]:
        """还没有分章的活跃场。物化前置闸门用它判断能不能整理。"""
        valid_chapter_ids = {chapter.chapter_plan_id for chapter in self.chapter_plans(project_id)}
        return [
            plan
            for plan in self.scene_plans(project_id)
            if not plan.chapter_plan_id or plan.chapter_plan_id not in valid_chapter_ids
        ]


# ------------------------------------------------------------------ 节奏体检

#: 三幕结构里三个灾难该落在哪一幕（Ingermanson：灾一收一幕、灾二是中点、灾三送进三幕）。
#: 这是**建议**不是规则——作者故意把灾二后置是合法选择，所以体检只提示、从不阻断。
_SPINE_EXPECTED_ACT = {"灾一": 1, "灾二": 2, "灾三": 2}


def _rhythm_report(chapter_payloads: list[dict[str, Any]]) -> dict[str, Any]:
    """分章的节奏体检（P3，纯确定性，不调 LLM）。

    只报三件能从结构本身看出来、且作者一眼能行动的事：每章场数的分布、三幕的章场
    配比、三个灾难落点是否还在它该在的幕。不做「张力评分」那种需要读正文才谈得上的
    判断——这里还没有正文。
    """
    counts = [item["scene_count"] for item in chapter_payloads]
    # 阶段 C：概述两段的反应场只有一两百字，按半场计入均值——它不该把一章「撑」成长章。
    weighted = [
        sum(
            0.5 if str(scene.get("rendering_mode") or "full") == "summary" else 1.0
            for scene in (item.get("scenes") or [])
        )
        for item in chapter_payloads
    ]
    summary_scene_count = sum(
        1
        for item in chapter_payloads
        for scene in (item.get("scenes") or [])
        if str(scene.get("rendering_mode") or "full") == "summary"
    )
    non_empty = [count for count in weighted if count]
    mean = (sum(non_empty) / len(non_empty)) if non_empty else 0.0

    acts: dict[int, dict[str, int]] = {}
    for item in chapter_payloads:
        bucket = acts.setdefault(int(item.get("act") or 1), {"chapter_count": 0, "scene_count": 0})
        bucket["chapter_count"] += 1
        bucket["scene_count"] += item["scene_count"]

    spine_placement = []
    for mark, expected_act in _SPINE_EXPECTED_ACT.items():
        hit = next((item for item in chapter_payloads if (item.get("spine") or "") == mark), None)
        if hit is None:
            spine_placement.append({"spine": mark, "placed": False, "expected_act": expected_act})
            continue
        act = int(hit.get("act") or 1)
        index = chapter_payloads.index(hit)
        is_last_of_act = not any(
            other for other in chapter_payloads[index + 1 :] if int(other.get("act") or 1) == act
        )
        spine_placement.append(
            {
                "spine": mark,
                "placed": True,
                "chapter_seq": hit["chapter_seq"],
                "chapter_title": hit["title"],
                "act": act,
                "expected_act": expected_act,
                # 灾一/灾三 是幕的收束点，落在本幕最后一章才算「在铰链上」；灾二是中点，只看幕。
                "on_hinge": (act == expected_act) and (mark == "灾二" or is_last_of_act),
            }
        )

    raw_non_empty = [count for count in counts if count]
    return {
        "scene_counts": counts,
        "weighted_scene_counts": weighted,
        "summary_scene_count": summary_scene_count,
        "mean_scenes_per_chapter": round(mean, 2),
        "min_scenes": min(raw_non_empty) if raw_non_empty else 0,
        "max_scenes": max(raw_non_empty) if raw_non_empty else 0,
        "empty_chapter_count": sum(1 for count in counts if not count),
        "acts": [
            {"act": act, **acts[act]}
            for act in sorted(acts)
        ],
        "spine_placement": spine_placement,
    }


# ------------------------------------------------------------------ 分章算法


def _coerce_act(value: Any, fallback: int | None = 1) -> int:
    try:
        act = int(value)
    except (TypeError, ValueError):
        return int(fallback or 1)
    return act if act in (1, 2, 3) else int(fallback or 1)


def parse_outline_chapters(draft: dict[str, Any] | None) -> list[dict[str, Any]]:
    """07 长篇大纲草稿 → 结构化章表。

    优先读结构化 ``chapters``（新契约）；缺席时回退解析 ``paragraphs`` 的文本行
    （历史草稿与旧 LLM 输出——提示词至今仍要求 ``NN 章名：一句话（灾一）`` 这个格式）。
    回退解析对不合格式的行也给出一章：宁可让作者在分章面板里改标题，也不要静默丢章。
    """
    payload = draft if isinstance(draft, dict) else {}
    structured = [item for item in (payload.get("chapters") or []) if isinstance(item, dict)]
    if structured:
        chapters: list[dict[str, Any]] = []
        for index, item in enumerate(structured, start=1):
            spine = str(item.get("spine") or "").strip()
            chapters.append(
                {
                    "row_uid": str(item.get("row_uid") or "").strip(),
                    "chapter_seq": index,
                    "act": _coerce_act(item.get("act"), 1),
                    "title": str(item.get("title") or "").strip(),
                    "summary": str(item.get("summary") or "").strip(),
                    "spine": spine if spine in SPINE_MARKS else "",
                    "chapter_goal": str(item.get("chapter_goal") or "").strip(),
                }
            )
        return chapters

    # 回退解析**只认真正的章行**（``NN 章名：…``）。曾经把任意非空行都当成一章，
    # 结果规划器骨架那种「四段散文」的 07 草稿会被编造成一堆假章，还抢在真正的章归属
    # 之前落库。宁可解析不出章（作者去 07 把章列出来 / 走分章面板），也不要造假章。
    chapters = []
    for act_index, paragraph in enumerate(payload.get("paragraphs") or [], start=1):
        for line in str(paragraph or "").splitlines():
            text = line.strip()
            if not text:
                continue
            match = _OUTLINE_LINE.match(text)
            if not match:
                continue
            spine_match = _SPINE_PATTERN.search(text)
            title = match.group(2).strip()
            summary = re.sub(r"（.*?）$", "", match.group(3)).strip()
            chapters.append(
                {
                    "row_uid": "",
                    "chapter_seq": len(chapters) + 1,
                    "act": min(act_index, 3),
                    "title": title,
                    "summary": summary,
                    "spine": spine_match.group(0) if spine_match else "",
                    "chapter_goal": "",
                }
            )
    return chapters


def _assign(
    strategy: str,
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
) -> dict[str, str | None]:
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
    for mark in SPINE_MARKS:
        scene_index = next((i for i, scene in enumerate(scenes) if scene_spine(scene) == mark), -1)
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
