"""构思侧分章：07 章表 × 09 场景列表 → 章节结构（Phase 2）。

历史上「整理成章节结构」没有任何一方真正在分章：09/10 步没有章字段，提示词让
LLM 把 chapter_id 留空说「server assigns」，而服务端的起始值就是 ``{project_id}_CH01``
且把作者传入的 chapter_id 当系统身份丢弃 —— 于是全书落进一章。唯一编了章的 07 长篇
大纲在后端只是四段自由文本，物化时完全不读。

这个模块把分章变成一次**可预览、可调整、可确认**的显式动作：
- ``preview`` 是只读推演（分章方案不写库），每种策略都确定性可复算；
- ``save`` 把作者在面板里确认的归属落到 ``SnowflakeScenePlan.chapter_plan_id`` / ``chapter_id``；
- 物化改读章表分组，章标题/章目标来自章表，而不是章 id 字符串。

脊柱锚点算法取自原前端 ``s2MaterializePreview``（那条降级路径反而是唯一分对章的），
搬到后端成为唯一实现，前端不再持有第二套。

2026-09-18（阶段 V）起的纪律——对应一次真实的「整理出来乱七八糟」：
- **章是故事序上连续的一段。** 故事序只有一个来源：09 场景列表草稿的行序（``snowflake_scene_order``）；
  ``scene_seq`` 只表示章内位置、只由 ``renumber_scene_seq`` 写。这里读场景永远按故事序，
  ``save`` 不看载荷里的章内先后。
- **章在场景之后**（阶段 K）：``from_scenes`` 策略按场景列表提议章表——三个灾难各自收束一章，每章场数的
  来历写在回包的 ``scale`` 里。它和别的策略一样是预览；确认时 ``save(replace_chapters=true)`` 才建章
  （``new:N`` → 真 row_uid）、软删没列出来的旧章、镜像回 07。``auto`` 由服务端按现状挑策略。
- 灾难标记既认场上的 ``spine`` 列，也认功能标签里的「灾难一 / 二 / 三」（``spine_from_role``）。
"""

from __future__ import annotations

import math
import re
import uuid
from bisect import bisect_right
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    OperationLog,
    SceneCard,
    SnowflakeChapterPlan,
    SnowflakeScenePlan,
    SnowflakeStepRun,
    StoryProject,
    utcnow,
)
from novel_system.services.catalog_placeholders import pristine_placeholder_chapters
from novel_system.services.catalog_trash_cascade import split_trashed_planned_cards
from novel_system.services.chapter_title_sync import (  # noqa: F401  (is_auto_chapter_title 从这里再导出)
    AUTO_TITLE_PATTERN,
    PLACEHOLDER_TITLE_MARKERS,
    is_auto_chapter_title,
    mirror_chapters_into_long_synopsis,
)
from novel_system.services.errors import DomainError
from novel_system.services.scene_design_ownership import is_snowflake_origin
from novel_system.services.snowflake_scene_order import renumber_scene_seq, sort_in_story_order
from novel_system.services.snowflake_steps import effective_rendering_mode
from novel_system.services.snowflake_triage import excluded_scene_plan_ids

#: ``from_scenes`` = 按场景列表推一份章表（不落库的预览，确认时才建章）；``auto`` = 由服务端按现状挑：
#: 已有归属 → keep_current，07 里有作者写的章表 → spine_anchor，否则 → from_scenes。
STRATEGIES = ("spine_anchor", "even", "keep_current", "from_scenes")
SPINE_MARKS = ("灾一", "灾二", "灾三")
_SPINE_PATTERN = re.compile(r"灾[一二三]")
# chapter_role 里的灾难标记。提示词给模型的范例就是「灾难一·一幕高潮」——只认「灾一」的旧正则
# 连自己文档里的例子都匹配不上，于是模型生成的场景表永远没有锚，三幕铰链从不生效
# （2026-09-18 真实数据：17 场里三场标着 灾难一 / 灾难二 / 灾难三，分章却报「没有任何一章标着灾一」）。
_SPINE_ROLE_PATTERN = re.compile(
    r"灾难?\s*([一二三123])"
    r"|第\s*([一二三123])\s*(?:个|次|场|重)?\s*灾难?"
    r"|disaster\s*#?\s*([123])",
    re.IGNORECASE,
)
_SPINE_BY_ORDINAL = {"一": "灾一", "1": "灾一", "二": "灾二", "2": "灾二", "三": "灾三", "3": "灾三"}
#: 提议章表时，新章在面板里的临时身份前缀（确认时由 ``save`` 铸成真正的 row_uid）。
NEW_CHAPTER_PREFIX = "new:"
# 占位章名的判定（「第 N 章」/「（待补）」/「未命名章节」）与章表在 07 草稿里的镜像搬到了叶子模块
# chapter_title_sync（阶段 Z）：目录服务也要用，而它不能反过来引用本模块。
_PLACEHOLDER_TITLE_MARKERS = PLACEHOLDER_TITLE_MARKERS
_AUTO_TITLE_PATTERN = AUTO_TITLE_PATTERN
# NN 章名：一句话（灾一）—— 2026-09-13 阶段 D 之前提示词 snowflake_generate_long_synopsis 与前端 07
# 脚手架把章表镜像进 paragraphs 时用的行格式。现在 paragraphs 是五段展开的散文，章表只在 chapters 里；
# 这个正则只为没有 chapters 的历史草稿服务。
_OUTLINE_LINE = re.compile(r"^(\d+)\s+([^：:]+)[：:]?(.*)$")

# 一章分到的场数超过均值这么多倍时给个提醒（不阻断——长章是合法的作者选择）
_OVERSIZED_RATIO = 3.0


def mint_chapter_row_uid() -> str:
    return f"chrow_{uuid.uuid4().hex}"


def chapter_target_id(project_id: str, serial: int) -> str:
    """第 ``serial`` 个被铸出来的雪花章在目录里的 id。

    阶段 Y（2026-09-20）：这个数字是**序列号，不是章序**。过去物化目标按章序算（第 3 章 = ``…_CH03``），
    重新分章之后同一个 id 指着另一组场：目录里那一行的章级状态（章状态、字数目标、戏剧卡、运行任务、终审）
    留在「第 3 个位置」上，从拆点往后的每张场景卡都要换章，场景运行时表上冗余的 chapter_id 成片过期。
    现在 id 钉在章计划行上（``SnowflakeChapterPlan.catalog_chapter_id``，见
    :meth:`SnowflakeChapteringService.catalog_chapter_id`）：第一次物化按章序铸出 CH01…CHnn（与从前一样），
    之后新出现的章拿下一个没用过的号，已有的章永远是它自己的那个号；显示的章序只由 ``display_order`` 决定。
    """
    return f"{project_id}_CH{serial:02d}"


def scene_spine(plan: SnowflakeScenePlan) -> str:
    """场景的脊柱标记：作者显式标注优先，其次从 chapter_role 里认灾难标记。

    提示词要求 LLM 「mark the three disaster scenes in chapter_role」（例如
    「灾难一·一幕高潮」），所以 LLM 生成的场景表没有显式 spine 也能锚定。
    """
    explicit = str(getattr(plan, "spine", "") or "").strip()
    if explicit:
        return explicit if explicit in SPINE_MARKS else ""
    return spine_from_role(plan.chapter_role)


def spine_from_role(chapter_role: Any) -> str:
    """功能标签里的灾难标记：灾一 / 灾难一 / 灾难 1 / 第一个灾难 / Disaster 1 → ``灾一``。"""
    match = _SPINE_ROLE_PATTERN.search(str(chapter_role or ""))
    if not match:
        return ""
    ordinal = next((group for group in match.groups() if group), "")
    return _SPINE_BY_ORDINAL.get(ordinal, "")


def spine_positions(scenes: list[SnowflakeScenePlan]) -> dict[str, int]:
    """每个灾难标记落在第几场（按传入顺序，-1 = 没有）。

    作者显式标的（``spine`` 列）压过从功能标签里认出来的；同一个标记出现多次时取**最后**一场——
    灾难是它所在章的收束点，一个两场连打的灾难要在第二场之后才断章。
    """
    positions: dict[str, int] = {}
    for mark in SPINE_MARKS:
        explicit = [
            index for index, scene in enumerate(scenes) if str(getattr(scene, "spine", "") or "").strip() == mark
        ]
        inferred = [index for index, scene in enumerate(scenes) if scene_spine(scene) == mark]
        hits = explicit or inferred
        positions[mark] = hits[-1] if hits else -1
    return positions


def is_placeholder_chapter(chapter: Any) -> bool:
    """07 编辑器「添加章节」点出来、还没写任何东西的章行（标题空 / 「（待补）」，摘要、章目标、脊柱全空）。

    这种章表不是作者的分章决定：把场均摊进几个「（待补）」只会得到一份没有意义的结构，
    面板应该当它不存在、直接按场景列表提议。
    """
    title = str(getattr(chapter, "title", "") or "").strip()
    blank_title = not title or any(marker in title for marker in _PLACEHOLDER_TITLE_MARKERS)
    has_content = any(
        str(getattr(chapter, field, "") or "").strip() for field in ("summary", "chapter_goal", "spine")
    )
    return blank_title and not has_content


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

    # ------------------------------------------------------ 章在目录里的 id（阶段 Y）

    def catalog_chapter_id(self, chapter: SnowflakeChapterPlan, *, mint: bool = True) -> str:
        """这一章在目录里的 id。钉过就用钉的；没钉过且 ``mint`` 时铸下一个序列号并钉上。

        预览里的瞬态章（``new:N``，不在 session 里）从不铸号：确认之前什么都不落库。
        """
        pinned = str(chapter.catalog_chapter_id or "").strip()
        if pinned or not mint or not chapter.chapter_plan_id:
            return pinned
        chapter.catalog_chapter_id = chapter_target_id(chapter.project_id, self._next_catalog_serial(chapter.project_id))
        self.session.flush()
        return chapter.catalog_chapter_id

    def _is_pinnable_chapter_id(self, project_id: str, chapter_id: str) -> bool:
        """目录里真有这一章（属于本作品），或者是本作品序列号的形状——才钉。

        场景行上的章戳可能是规划器 / 模型随手写的字符串（``ch1``、别的作品的章号）：钉上去就成了物化目标，
        批准时要么建出一个怪 id 的章，要么撞上 ``CHAPTER_ALREADY_OWNED``。不钉的留空，保存分章时照常铸号。
        """
        row = self.session.get(ChapterGoal, chapter_id)
        if row is not None:
            return row.project_id == project_id
        return re.fullmatch(rf"{re.escape(project_id)}_CH\d+", chapter_id) is not None

    def _next_catalog_serial(self, project_id: str) -> int:
        """下一个没用过的序列号：目录里现有的章（含回收站）与所有钉过的号（含已软删的章计划）都算占用。"""
        pattern = re.compile(rf"^{re.escape(project_id)}_CH(\d+)$")
        used = [
            *self.session.execute(select(ChapterGoal.chapter_id).where(ChapterGoal.project_id == project_id)).scalars(),
            *self.session.execute(
                select(SnowflakeChapterPlan.catalog_chapter_id).where(
                    SnowflakeChapterPlan.project_id == project_id,
                    SnowflakeChapterPlan.catalog_chapter_id.is_not(None),
                )
            ).scalars(),
        ]
        # 场景行上的章戳不算占用：前端那一路给所有场盖的是退化默认值 ``…_CH01``，那不是一个真的章。
        serials = [int(match.group(1)) for value in used if (match := pattern.match(str(value or "")))]
        return max(serials, default=0) + 1

    def scene_plans(self, project_id: str) -> list[SnowflakeScenePlan]:
        """活跃场景计划，按**故事序**（09 场景列表的行序）。

        曾按 ``(scene_seq, scene_id)`` 排——``scene_seq`` 在第一次分章落库后就是章内序，于是第二次
        分章读到的是各章的场交错洗在一起的顺序（1、10、2、11……）。故事序的唯一来源见
        ``snowflake_scene_order``。
        """
        rows = self.session.execute(
            select(SnowflakeScenePlan).where(
                SnowflakeScenePlan.project_id == project_id,
                SnowflakeScenePlan.removed_at.is_(None),
            )
        ).scalars().all()
        return sort_in_story_order(self.session, project_id, rows)

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
        for item, row in zip(derived, created):
            # 从目录 / 场景行的章戳反推出来的章：那个章号就是它在目录里的身份，钉住
            source = str(item.get("source_chapter_id") or "").strip()
            if source and self._is_pinnable_chapter_id(project_id, source):
                row.catalog_chapter_id = source
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
        # 只认**雪花物化出来的章**——章里有场景计划对应的场景卡。作者在章节编排里手建的章
        # （比如新建作品后随手点出来的「第 1 章 / 开场」）不是构思侧的分章决定：把它当章表，
        # 面板就会显示「一章 + 全书的场都未分配」，而真正要整理的章一章都没有。
        plan_scene_ids = {plan.scene_id for plan in self.scene_plans(project_id)}
        materialized_chapter_ids = {
            card.chapter_id
            for card in self.session.execute(
                select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
            ).scalars()
            if card.scene_id in plan_scene_ids
        }
        rows = [chapter for chapter in rows if chapter.chapter_id in materialized_chapter_ids]
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

    # -------------------------------------------------- 阶段 K：章在场景之后——按场景列表提议章表

    def propose_from_scenes(
        self,
        project_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        """按已经列好的场景提议一份章表并落库（Ingermanson：章是列完场之后的包装决定）。

        - 三个灾难是幕的铰链：带 灾一 / 灾二 / 灾三 的场必须是它所在章的最后一场；
        - 章的尺度见 :meth:`chapter_scale`（载荷的章数 / 每章场数 → 作品设置 → 参考书章长 → 每章 3 场）；
        - 每幕至少一章；章标题给占位「第 N 章」，摘要取本章**最后一场**的一句话（这一章把局面推到哪），作者随后改；
        - 已有章表时必须显式 ``replace=true`` 才覆盖（旧章软删，归属重排）；
        - 章表同时镜像进 07 草稿的 ``chapters``，前端表格能看到。

        面板不再走这条路——它用 ``preview(strategy="from_scenes")`` 拿到同一份提议的**预览**，
        作者确认时才由 ``save(replace_chapters=true)`` 落库；这个端点留给脚本 / API 调用方。
        """
        body = payload or {}
        scenes = self.scene_plans(project_id)
        if not scenes:
            raise DomainError(
                "SNOWFLAKE_SCENES_REQUIRED",
                "09 场景列表还没有场景，无法按场景提议章表。",
                status_code=409,
                details={"step_key": "scene_list"},
            )
        existing = self.chapter_plans(project_id)
        if existing and not body.get("replace"):
            raise DomainError(
                "SNOWFLAKE_CHAPTER_PLAN_EXISTS",
                "已经有章表了；要按场景重新提议，请带 replace=true（旧章会被替换，场景归属重排）。",
                status_code=409,
                details={"chapter_count": len(existing)},
            )
        scale = self.chapter_scale(project_id, body, scenes)
        chunks = propose_chapter_chunks(
            scenes,
            target_chapter_count=scale["target_chapter_count"],
            scenes_per_chapter=scale["scenes_per_chapter"],
        )
        proposed_at = utcnow()
        # 阶段 Y：与面板同一套身份沿用——场至少一半相同的章还是那一章（目录里同一行），其余才是新章
        matches = match_chunks_to_chapters(existing, scenes, chunks)
        scene_summaries = {str(scene.summary or "").strip() for scene in scenes if str(scene.summary or "").strip()}
        uid_of = {
            index: (matches[index - 1][0].row_uid if index - 1 in matches else f"{NEW_CHAPTER_PREFIX}{index}")
            for index in range(1, len(chunks) + 1)
        }
        self.save(
            project_id,
            {
                "replace_chapters": True,
                "chapters": [
                    {"row_uid": uid_of[index], **_proposed_chapter_fields(index, chunk, matches.get(index - 1), scene_summaries)}
                    for index, chunk in enumerate(chunks, start=1)
                ],
                "assignments": [
                    {"scene_plan_id": scene.scene_plan_id, "chapter_row_uid": uid_of[index]}
                    for index, chunk in enumerate(chunks, start=1)
                    for scene in chunk["scenes"]
                ],
            },
            actor_ref=actor_ref,
        )
        self.session.add(
            OperationLog(
                event_type="snowflake_chapter_plan_proposed",
                object_type="story_project",
                object_ref=project_id,
                payload_json={
                    "project_id": project_id,
                    "chapter_count": len(chunks),
                    "scene_count": len(scenes),
                    "replaced_chapter_count": len(existing),
                    "target_chapter_count": scale["target_chapter_count"],
                    "scenes_per_chapter": scale["scenes_per_chapter"],
                    "scale_source": scale["source"],
                    "reference_hint": scale["reference_hint"],
                    "actor_ref": actor_ref or "operator",
                    "proposed_at": proposed_at,
                },
            )
        )
        self.session.flush()
        preview = self.preview(project_id, {"strategy": "keep_current"})
        preview["created_chapter_count"] = len(chunks)
        preview["replaced_chapter_count"] = len(existing)
        preview["scale"] = scale
        return preview

    def chapter_scale(
        self,
        project_id: str,
        body: dict[str, Any] | None,
        scenes: list[SnowflakeScenePlan],
    ) -> dict[str, Any]:
        """一章大约装几场——以及这个数是从哪来的（面板要向作者解释，不能是个黑盒）。

        优先级：作者这一次指名的章数 → 这一次指名的每章场数 → 作品设置的目标章数 →
        参考书的章长（结构画像 chapter_chars 中位 ÷ 场长中位，无显式场界按中等场 1500 字）→ 每章 3 场。
        作者在面板里填「每章约 N 场」必须压过作品设置里的目标章数——那是建项目时随手填的数，
        不该让面板上的输入框形同虚设。铰链优先于这一切：三个灾难各自收束一章，见 ``hinge_min_chapters``。
        """
        payload = body or {}
        total = len(scenes)
        requested_target = _as_int(payload.get("target_chapter_count"))
        requested_per = _as_int(payload.get("scenes_per_chapter"))
        project = self.session.get(StoryProject, project_id)
        project_target = int(getattr(project, "target_chapter_count", 0) or 0)
        reference_hint = None
        target = 0
        if requested_target > 0:
            source, target = "request_target", requested_target
            per_chapter = max(1, math.ceil(total / requested_target)) if total else 1
        elif requested_per > 0:
            source, per_chapter = "request_per_chapter", requested_per
        elif project_target > 0:
            source, target = "project_target", project_target
            per_chapter = max(1, math.ceil(total / project_target)) if total else 1
        else:
            # 2026-09-14 风格保真修补(WP5)：作者什么都没定时，按参考作者的章长推每章场数。
            reference_hint = _reference_chapter_scale_hint(self.session, project_id)
            if reference_hint:
                source, per_chapter = "reference", int(reference_hint["scenes_per_chapter"])
            else:
                source, per_chapter = "default", 3
        return {
            "source": source,
            "target_chapter_count": target,
            "scenes_per_chapter": max(1, per_chapter),
            "scene_count": total,
            "hinge_min_chapters": hinge_min_chapters(scenes),
            "reference_hint": reference_hint,
        }

    def _mirror_chapters_into_long_synopsis(self, project_id: str, chapters: list[SnowflakeChapterPlan]) -> None:
        """把章表写回 07 最新草稿的 ``chapters`` 与 ``fe_scaffold.chapters``（见 ``chapter_title_sync``）。"""
        mirror_chapters_into_long_synopsis(self.session, project_id, chapters)

    # -------------------------------------------------------------- 预览

    def preview(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = payload or {}
        strategy = str(body.get("strategy") or "spine_anchor").strip()
        healed_from_saved = False
        if strategy == "auto":
            strategy = self._auto_strategy(project_id)
            healed_from_saved = strategy == "from_scenes" and bool(
                misplaced_scene_plan_ids(self.chapter_plans(project_id), self.scene_plans(project_id))
            )
        if strategy not in STRATEGIES:
            raise DomainError(
                "SNOWFLAKE_CHAPTER_STRATEGY_INVALID",
                f"分章策略只能是 {'、'.join((*STRATEGIES, 'auto'))} 之一。",
                status_code=400,
            )
        if strategy == "from_scenes":
            shaped = self._preview_from_scenes(project_id, body)
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
        healed_ids: list[str] = []
        if strategy == "keep_current":
            # 面板摆出来的必须就是「确认写入」会落库的那一版：保存时非连续的归属会被并回相邻的章，这里先并给作者看。
            fixed = heal_assignment(assignment, chapters, scenes)
            healed_ids = [key for key, value in fixed.items() if value != assignment.get(key)]
            assignment = fixed
        shaped = self._shape_preview(project_id, strategy, chapters, scenes, assignment)
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
        shaped["scale"] = self.chapter_scale(project_id, body, scenes)
        shaped["chapter_table"] = self._chapter_table_info(project_id)
        return shaped

    def _chapter_table_info(self, project_id: str) -> dict[str, Any]:
        """落了库的章表现在是什么状态——面板据此决定哪几种分法点得动。

        ``authored``：至少有一章不是「（待补）」占位（作者在 07 写的，或上一次确认留下的）；
        ``saved``：已经有场分进了章（「已保存的分章」才有东西可摆）。
        """
        chapters = self.chapter_plans(project_id)
        valid = {chapter.chapter_plan_id for chapter in chapters}
        return {
            "count": len(chapters),
            "authored": any(not is_placeholder_chapter(chapter) for chapter in chapters),
            "saved": any(plan.chapter_plan_id in valid for plan in self.scene_plans(project_id)),
        }

    def _auto_strategy(self, project_id: str) -> str:
        """面板打开时该给作者看什么（不替作者做决定，只挑「现状」最诚实的那一种）。

        - 已经有场分进了章 → ``keep_current``：作者上次确认 / 调整过的结果原样摆出来，新加的场跟着
          故事序上的前一场走。以前面板一打开就按脊柱锚点**重算**一遍，作者手调过的归属每次都被抹平。
        - 一场都没分、但 07 里有作者真的写过的章表 → ``spine_anchor``：把场倒进作者的章。
        - 没有章表，或者章表只是几行「（待补）」占位 → ``from_scenes``：章是列完场之后的包装决定，
          直接按场景列表提议（阶段 K）。
        """
        chapters = self.chapter_plans(project_id) or self.ensure_chapter_plans(project_id)
        if not chapters:
            return "from_scenes"
        valid = {chapter.chapter_plan_id for chapter in chapters}
        scenes = self.scene_plans(project_id)
        if any(plan.chapter_plan_id in valid for plan in scenes):
            # 阶段 X：已保存的分章如果不是故事序上的连续切片（阶段 V 之前交错洗过的归属，被面板的
            # 「从这里另起一章」继续切下去——2026-09-19 真实项目：第 4 章 = 第 4、10–13 场），就不再把它
            # 原样摆出来让作者用只会挪章界的工具去修一个修不好的东西：直接按场景列表重新提议。
            return "keep_current" if not misplaced_scene_plan_ids(chapters, scenes) else "from_scenes"
        if all(is_placeholder_chapter(chapter) for chapter in chapters):
            return "from_scenes"
        return "spine_anchor"

    def _preview_from_scenes(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """按场景列表提议章表——**只读预览**，章行此刻并不存在。

        回包里的章带临时身份 ``new:N``；作者确认时面板把整张章表连同 ``replace_chapters=true``
        交回来，``save`` 才铸 row_uid、软删旧章。和另外几种策略一样：确认之前什么都不落库。
        """
        scenes = self.scene_plans(project_id)
        if not scenes:
            raise DomainError(
                "SNOWFLAKE_SCENES_REQUIRED",
                "09 场景列表还没有场景，无法按场景提议章表。",
                status_code=409,
                details={"step_key": "scene_list"},
            )
        scale = self.chapter_scale(project_id, body, scenes)
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
        live = self.chapter_plans(project_id)
        matches = match_chunks_to_chapters(live, scenes, chunks)
        scene_summaries = {str(scene.summary or "").strip() for scene in scenes if str(scene.summary or "").strip()}
        chapters: list[SnowflakeChapterPlan] = []
        assignment: dict[str, str | None] = {}
        reused: set[str] = set()
        for index, chunk in enumerate(chunks, start=1):
            same = matches[index - 1][0] if index - 1 in matches else None
            fields = _proposed_chapter_fields(index, chunk, matches.get(index - 1), scene_summaries)
            if same is not None:
                reused.add(same.row_uid)
                row_uid = same.row_uid
            else:
                row_uid = f"{NEW_CHAPTER_PREFIX}{index}"
            # 不进 session 的瞬态行：只为了复用 _shape_preview 的同一套成形逻辑
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
        shaped = self._shape_preview(project_id, "from_scenes", chapters, scenes, assignment)
        shaped["scale"] = scale
        shaped["chapter_table"] = self._chapter_table_info(project_id)
        shaped["replaces_chapter_count"] = len(live) - len(reused)
        return shaped

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
        excluded = self._excluded_scene_plan_ids(project_id)
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
                    "chapter_id": self.catalog_chapter_id(chapter, mint=False),
                    "act": int(chapter.act or 1),
                    "title": chapter.title or "",
                    "summary": chapter.summary or "",
                    "spine": chapter.spine or "",
                    # 章目标原样给（不拿摘要顶替）：面板会把它原样交回来，顶替过的值一存就成了「作者写的章目标」，
                    # 之后拆章 / 并章它就一直描述着一场已经搬走的戏。物化时章目标缺席自会退回摘要。
                    "chapter_goal": chapter.chapter_goal or "",
                    "scene_count": len(members),
                    "scenes": [
                        {
                            "scene_plan_id": scene.scene_plan_id,
                            "row_uid": scene.row_uid or "",
                            "scene_id": scene.scene_id,
                            "scene_seq": seq,
                            "story_index": story_index.get(scene.scene_plan_id, 0),
                            "title": scene.title or scene.summary or scene.scene_id,
                            # 09 的「功能」栏（起疑 / 取证 / 灾难一·一幕高潮…）：一场在故事里的活儿，
                            # 比一整句事件摘要好扫读
                            "function": scene.chapter_role or "",
                            "summary": scene.summary or "",
                            "primary_form": scene.scene_type or "proactive",
                            "spine": scene_spine(scene),
                            "anchored": bool(scene_spine(scene)) and scene_spine(scene) == (chapter.spine or ""),
                            "planned": bool((scene.goal or scene.reaction or "").strip()),
                            # 阶段 C / N：概述场在节奏体检里按半场计（两种形态都可以概述）
                            "rendering_mode": effective_rendering_mode(scene.scene_type, scene.rendering_mode),
                            # 阶段 N：作者裁定该重写 / 待删——不物化，节奏按 0 计
                            "excluded": scene.scene_plan_id in excluded,
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
                    "story_index": story_index.get(scene.scene_plan_id, 0),
                    "title": scene.title or scene.summary or scene.scene_id,
                    "function": scene.chapter_role or "",
                    "primary_form": scene.scene_type or "proactive",
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
                    "message": f"「{item['title'] or '第 ' + str(item['chapter_seq']) + ' 章'}」没有分到任何场，物化后会是一章空壳。",
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
                                f"「{item['title'] or '第 ' + str(item['chapter_seq']) + ' 章'}」分到 {item['scene_count']} 场，"
                                f"约是平均值（{mean:.1f}）的 {item['scene_count'] / mean:.1f} 倍。"
                            ),
                        }
                    )
        # 章必须是故事序上连续的一段：章内顺序永远等于 09 场景列表的顺序，所以一场如果排在它前面
        # 那些章的场之前，目录里读到的顺序就和场景列表不一样了（常见成因：分章之后又在 09 里拖过行）。
        running_max = 0
        misplaced: list[dict[str, Any]] = []
        for item in chapter_payloads:
            indices = [int(scene.get("story_index") or 0) for scene in item["scenes"]]
            for scene in item["scenes"]:
                if 0 < int(scene.get("story_index") or 0) < running_max:
                    misplaced.append({"chapter": item, "scene": scene})
            if indices:
                running_max = max(running_max, max(indices))
        if misplaced:
            first = misplaced[0]
            more = f" 等 {len(misplaced)} 场" if len(misplaced) > 1 else ""
            warnings.append(
                {
                    "kind": "chapter_order_conflict",
                    "severity": "advisory",
                    "message": (
                        f"「{_clip(first['scene']['title'])}」{more}在场景列表里排在前面几章的场之前，却分在"
                        f"《{first['chapter']['title'] or '第 ' + str(first['chapter']['chapter_seq']) + ' 章'}》——章是场景列表上连续的一段，"
                        "目录里读到的顺序会和场景列表不一致。把它移回相邻的章，或者按场景重新分章。"
                    ),
                    "scene_plan_ids": [entry["scene"]["scene_plan_id"] for entry in misplaced],
                }
            )

        warnings.extend(self._catalog_warnings(project_id, chapter_payloads))

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

    def _catalog_warnings(self, project_id: str, chapter_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """章节目录里和这次分章对不上的章——只提醒，不阻断，也绝不替作者删。

        - 作者在章节编排里手建的章（不是雪花整理出来的）：原样保留——雪花的章按章表的顺序排，手建的章
          原来跟在哪一章后面现在还跟在它后面（``_CatalogPlacement.settle_chapter_order``）；
        - 上一版分章留下、这一版已经没有场的章：场景卡搬走之后空了的进回收站，还留着东西的原样保留。
        """
        rows = list(
            self.session.execute(
                select(ChapterGoal).where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
            ).scalars()
        )
        trash_warnings = self._trashed_card_warnings(project_id, chapter_payloads)
        if not rows:
            return trash_warnings
        targets = {item["chapter_id"] for item in chapter_payloads if item["scene_count"] and item["chapter_id"]}

        # 阶段 Y：哪一章是雪花整理出来的，看它的来源，不看 id 长什么样（章号不再有位置含义）
        def made_by_snowflake(row: ChapterGoal) -> bool:
            return is_snowflake_origin(row.writer_brief_json)

        # 阶段 X：手建的章分两种——作者真写过东西的（原样保留，新章接在后面）与空白占位章
        # （「第 1 章 / 开场」，一个字没写：确认写入时移入回收站，雪花的章从第 1 章排起）。
        placeholder_ids = {chapter.chapter_id for chapter, _cards in pristine_placeholder_chapters(self.session, project_id)}
        placeholders = [row for row in rows if row.chapter_id in placeholder_ids]
        hand_made = [row for row in rows if not made_by_snowflake(row) and row.chapter_id not in placeholder_ids]
        leftover = [row for row in rows if made_by_snowflake(row) and row.chapter_id not in targets]
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
            plan_scene_ids = {plan.scene_id for plan in self.scene_plans(project_id)}
            leftover_ids = {row.chapter_id for row in leftover}
            leftover_cards = list(
                self.session.execute(
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

    def _trashed_card_warnings(self, project_id: str, chapter_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            self.session,
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

    # -------------------------------------------------------------- 落库

    def save(
        self,
        project_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
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
        chapters = self.chapter_plans(project_id) if replace_chapters else self.ensure_chapter_plans(project_id)
        by_row_uid = {chapter.row_uid: chapter for chapter in chapters}
        scenes = {plan.scene_plan_id: plan for plan in self.scene_plans(project_id)}

        # 章的元数据（标题/幕/脊柱/章目标/顺序）——作者可以在面板里直接改
        incoming_chapters = [item for item in (body.get("chapters") or []) if isinstance(item, dict)]
        if replace_chapters and not incoming_chapters:
            raise DomainError(
                "SNOWFLAKE_CHAPTER_PLAN_PAYLOAD_EMPTY",
                "整张替换章表时必须带上新的章表；空章表不会被当成「删掉所有章」。",
                status_code=400,
            )
        alias: dict[str, SnowflakeChapterPlan] = {}
        listed: set[str] = set()
        for index, item in enumerate(incoming_chapters, start=1):
            row_uid = str(item.get("row_uid") or "").strip()
            chapter = by_row_uid.get(row_uid)
            if chapter is None and replace_chapters and (not row_uid or row_uid.startswith(NEW_CHAPTER_PREFIX)):
                chapter = self._create_chapter_plan(project_id, {"chapter_seq": index, "act": item.get("act")})
                by_row_uid[chapter.row_uid] = chapter
            if chapter is None:
                raise DomainError(
                    "SNOWFLAKE_CHAPTER_PLAN_NOT_FOUND",
                    f"未找到章「{item.get('title') or row_uid}」。",
                    status_code=404,
                )
            alias[row_uid or f"{NEW_CHAPTER_PREFIX}{index}"] = chapter
            listed.add(chapter.row_uid)
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
        # 新建的章必须先落库：场景行的 chapter_plan_id 是外键，而 ORM 没有声明关系来排依赖顺序
        self.session.flush()
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
            chapter = alias.get(row_uid) or by_row_uid.get(row_uid)
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
        removed: list[SnowflakeChapterPlan] = []
        if replace_chapters:
            removed_at = utcnow()
            for chapter in chapters:
                if chapter.row_uid in listed:
                    continue
                chapter.removed_at = removed_at
                chapter.removed_by = actor_ref or "operator"
                removed.append(chapter)
                for plan in scenes.values():
                    if plan.chapter_plan_id == chapter.chapter_plan_id:
                        plan.chapter_plan_id = None
                self.session.add(
                    OperationLog(
                        event_type="snowflake_chapter_plan_removed",
                        object_type="snowflake_chapter_plan",
                        object_ref=chapter.chapter_plan_id,
                        payload_json={
                            "project_id": project_id,
                            "row_uid": chapter.row_uid,
                            "title": chapter.title or "",
                            "removed_at": removed_at,
                            "reason": "chapter_plan_replaced",
                        },
                    )
                )

        # 章序可能整体变了（拆章 / 并章 / 重排），所以给**每一场**重盖章戳，而不只是这次点名的场——
        # 否则没被点名的场还带着旧的物化目标章号，回流会把场景卡搬进错的章。
        live = {chapter.chapter_plan_id: chapter for chapter in by_row_uid.values() if not chapter.removed_at}
        # 阶段 X：「章是故事序上连续的一段」由服务端守住，不再信面板。面板只提供挪章界 / 拆章 / 并章，
        # 从一版连续的分章出发不会越界；可它手里的那一版如果本来就是交错的（旧数据、API 调用方），
        # 再怎么拆也是交错的——落库前过一遍 heal_assignment：保住章序的最长不降子序列，离群的场并入故事序上前一场的章。
        ordered_chapters = sorted(live.values(), key=lambda chapter: (int(chapter.chapter_seq or 0), chapter.chapter_plan_id))
        by_uid = {chapter.row_uid: chapter for chapter in ordered_chapters}
        by_plan_id = {chapter.chapter_plan_id: chapter for chapter in ordered_chapters}
        story_scenes = list(scenes.values())  # scene_plans() 已按故事序
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
            self._refresh_auto_chapter_fields(list(live.values()), list(scenes.values()))
        # 阶段 Y：每章在目录里的 id 钉在章计划行上——按章序铸号（第一次物化仍是 CH01…CHnn），
        # 已经钉过的章不管现在排第几都还是它自己的号：拆章 / 并章 / 重排只动真的换了章的场。
        for chapter in ordered_chapters:
            self.catalog_chapter_id(chapter)
        for plan in scenes.values():
            chapter = live.get(plan.chapter_plan_id or "")
            if chapter is None:
                continue
            plan.chapter_id = chapter.catalog_chapter_id
            # 不退回场景行上的旧值：那是它上一个章的标题 / 章目标，回流还会把它写进场景卡的简报
            plan.chapter_title = chapter.title or ""
            plan.chapter_goal = chapter.chapter_goal or chapter.summary or ""
        self.session.flush()
        renumber_scene_seq(self.session, project_id)
        self._mirror_chapters_into_long_synopsis(project_id, list(live.values()))

        self.session.add(
            OperationLog(
                event_type="snowflake_chapter_plan_saved",
                object_type="story_project",
                object_ref=project_id,
                payload_json={
                    "project_id": project_id,
                    "chapter_count": len(live),
                    "assigned_scene_count": len(touched),
                    "created_chapter_count": sum(1 for chapter in live.values() if chapter not in chapters),
                    "removed_chapter_count": len(removed),
                    "healed_scene_plan_ids": healed,
                    "actor_ref": actor_ref or "operator",
                    "saved_at": utcnow(),
                },
            )
        )
        self.session.flush()
        return {"assigned_scene_count": len(touched), "healed_scene_plan_ids": healed}

    @staticmethod
    def _refresh_auto_chapter_fields(chapters: list[SnowflakeChapterPlan], scenes: list[SnowflakeScenePlan]) -> None:
        """整张章表落库时，把**系统起的**章名与章摘要按新的结构重算；作者写的一个字都不动。

        - 章名空着、或是占位「第 N 章」→ 按现在的章序重编（拆章 / 并章之后「第 3 章」不能排在第 4 位，
          空章名物化进目录会变成章 id 字符串）；
        - 章摘要空着、或与某一场的摘要一字不差（= 提议时从场上抄来的）→ 取这一章现在的最后一场。
          作者自己写的摘要不会和某一场的摘要逐字相同。
        """
        scene_summaries = {str(scene.summary or "").strip() for scene in scenes if str(scene.summary or "").strip()}
        members: dict[str, list[SnowflakeScenePlan]] = {}
        for scene in scenes:  # scenes 已按故事序
            members.setdefault(scene.chapter_plan_id or "", []).append(scene)
        for chapter in chapters:
            title = str(chapter.title or "").strip()
            if not title or _AUTO_TITLE_PATTERN.match(title):
                chapter.title = f"第 {int(chapter.chapter_seq or 1)} 章"
            summary = str(chapter.summary or "").strip()
            mine = members.get(chapter.chapter_plan_id) or []
            if mine and (not summary or summary in scene_summaries):
                last = mine[-1]
                chapter.summary = str(last.summary or last.title or "").strip()

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
        assignment = _enforce_contiguity(assignment, chapters, scenes)
        shaped = self._shape_preview(project_id, "llm_suggested", chapters, scenes, assignment)
        shaped["chapter_table"] = self._chapter_table_info(project_id)
        shaped["rationale"] = result.payload.get("rationale") or ""
        shaped["source"] = result.source
        shaped["llm_call_id"] = result.llm_call_id
        shaped["kept_from_deterministic"] = sorted(result.payload.get("missing_scene_plan_ids") or [])
        return shaped

    # ------------------------------------------------------------ 阶段 W：AI 起章名

    #: 一次 LLM 调用起几章的名字 / 一次请求最多几批。整本书的章名 + 章摘要一次要不完
    #: （思考型中转的推理 token 也算在输出里），分批还让后面的批看得见前面已经起好的名字，口径一致。
    TITLE_BATCH_SIZE = 12
    TITLE_MAX_BATCHES = 6

    def suggest_titles(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """AI 起章名（**只读**——不落库，名字回到面板里由作者改、由作者确认）。

        载荷的 ``chapters`` 是面板此刻的章表（含还没落库的 ``new:N`` 章、作者手调过的归属）：
        ``[{row_uid, title, act, spine, scene_plan_ids}]``；不带就按已保存的分章起。
        只给**系统起的占位名**（空 / 「第 N 章」/「（待补）」）起名，作者自己起的名字不碰——
        ``rename_all=true`` 才全部重起。fail-closed：模型没配好就 409，不拿规则拼的名字冒充。
        """
        from novel_system.services.snowflake_workspace_llm import SnowflakeWorkspaceLLMService

        body = payload or {}
        scenes = self.scene_plans(project_id)
        order = {scene.scene_plan_id: index for index, scene in enumerate(scenes, start=1)}
        by_id = {scene.scene_plan_id: scene for scene in scenes}
        incoming = [item for item in (body.get("chapters") or []) if isinstance(item, dict)]
        if not incoming:
            saved = self.preview(project_id, {"strategy": "keep_current"})
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
                    "act": _coerce_act(item.get("act"), 1),
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

        llm = SnowflakeWorkspaceLLMService(self.session)
        project = self._project_payload(project_id)
        book = self._book_context(project_id)
        # 2026-09-22 结构跟随参考书:章名照参考作家起题名的方式起(题名样例 + 形态);无绑定 → None
        reference_titles = _reference_chapter_titles(self.session, project_id)
        named = [{"position": chapter["position"], "title": chapter["title"]} for chapter in authored]
        batches = [
            targets[start : start + self.TITLE_BATCH_SIZE] for start in range(0, len(targets), self.TITLE_BATCH_SIZE)
        ]
        for batch_index, batch in enumerate(batches[: self.TITLE_MAX_BATCHES]):
            try:
                outcome = llm.chapter_title_suggestions(
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
                                    "summary": _clip(by_id[scene_plan_id].summary or by_id[scene_plan_id].title, 160),
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

    def _book_context(self, project_id: str) -> dict[str, Any]:
        """章名要带着全书的调子：已确认的一句话与五句脊柱（未确认的草稿不算事实，不给）。"""
        context: dict[str, Any] = {}
        for step_key, field, target in (
            ("one_sentence_summary", "summary", "logline"),
            ("one_paragraph_summary", "sentences", "five_sentence_spine"),
            ("one_paragraph_summary", "moral_premise", "moral_premise"),
        ):
            run = self.session.execute(
                select(SnowflakeStepRun)
                .where(
                    SnowflakeStepRun.project_id == project_id,
                    SnowflakeStepRun.step_key == step_key,
                    SnowflakeStepRun.status.in_(["approved", "stale"]),
                )
                .order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.created_at.desc())
            ).scalars().first()
            value = (run.draft_json or {}).get(field) if run is not None else None
            if value:
                context[target] = value
        return context

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

    def _excluded_scene_plan_ids(self, project_id: str) -> set[str]:
        # 阶段 N：作者裁定该重写 / 待删的场不物化——节奏体检按 0 计（口径在 snowflake_triage）。
        return excluded_scene_plan_ids(self.session, project_id)

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
    # 阶段 I：页面上略过的反应场不占篇幅，按 0 计。
    # 阶段 N：作者裁定该重写 / 待删的场（excluded）不物化，同样按 0 计。
    _weight = {"summary": 0.5, "skip": 0.0}
    weighted = [
        sum(
            0.0 if scene.get("excluded") else _weight.get(str(scene.get("rendering_mode") or "full"), 1.0)
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
    skipped_scene_count = sum(
        1
        for item in chapter_payloads
        for scene in (item.get("scenes") or [])
        if str(scene.get("rendering_mode") or "full") == "skip"
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
        "skipped_scene_count": skipped_scene_count,
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
    （历史草稿与旧 LLM 输出曾用 ``NN 章名：一句话（灾一）`` 这个格式）。
    2026-09-13 阶段 D 起 ``paragraphs`` 是五段展开的散文（书里的第 6 步），不再是章行镜像，
    所以回退解析只对历史草稿有意义——散文段落里解析不出章行就得到空表，绝不造假章。
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


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# 参考作者无显式场界时,推每章场数所用的「中等场」字数(与场景卡 medium 长度带同量级)
_REFERENCE_DEFAULT_SCENE_CHARS = 1500
_REFERENCE_MAX_SCENES_PER_CHAPTER = 12


def _reference_chapter_titles(session: Any, project_id: str) -> dict[str, Any] | None:
    """参考作家的章题画像(``planning_context.reference_titles_payload``);任何异常 → None(可选增强)。"""
    try:
        from novel_system.services.style_reference.planning_context import reference_titles_payload

        return reference_titles_payload(session, project_id)
    except Exception:  # noqa: BLE001 — 可选增强
        return None


def _reference_chapter_scale_hint(session: Any, project_id: str) -> dict[str, Any] | None:
    """项目 / 全局作用域绑定的参考画像 → {chapter_chars_median, scene_chars_median, scenes_per_chapter}。

    绑定只在 ``style_policy_live`` 解析(2026-09-24 S3;轻量路径——这里只要画像 id,不冻结契约);任何异常都
    返回 None(可选增强,分章提议不能因参考失败而失败)。
    """
    try:
        from types import SimpleNamespace

        from novel_system.db.models import StyleReferenceProfile
        from novel_system.services.style_policy import style_policy_live

        scope = SimpleNamespace(project_id=str(project_id), scene_id=None, pov_character_id=None, onstage_chars_json=[])
        policy = style_policy_live(session, scope, task_type="scene_generation", freeze_contract=False)
        if not policy.bound or not policy.profile_id:
            return None
        profile = session.get(StyleReferenceProfile, str(policy.profile_id))
        card = (getattr(profile, "profile_json", None) or {}).get("structure_card") if profile is not None else None
        if not isinstance(card, dict) or int(card.get("chapter_count") or 0) <= 1:
            return None
        chapter_median = int((card.get("chapter_chars") or {}).get("median") or 0)
        if chapter_median <= 0:
            return None
        scene_median = 0
        if str(card.get("scene_break_style") or "") == "explicit":
            scene_median = int((card.get("scene_chars") or {}).get("median") or 0)
        typical_scene = scene_median or _REFERENCE_DEFAULT_SCENE_CHARS
        scenes_per_chapter = max(1, min(_REFERENCE_MAX_SCENES_PER_CHAPTER, int(round(chapter_median / typical_scene))))
        return {
            "profile_id": str(getattr(profile, "profile_id", "") or ""),
            "chapter_chars_median": chapter_median,
            "scene_chars_median": scene_median or None,
            "scenes_per_chapter": scenes_per_chapter,
        }
    except Exception:  # noqa: BLE001 — 可选增强
        return None


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


def _chunk_chapter_fields(index: int, chunk: dict[str, Any]) -> dict[str, Any]:
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


def _clip(text: Any, limit: int = 24) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else f"{value[:limit]}…"


def _proposed_chapter_fields(
    index: int,
    chunk: dict[str, Any],
    match: tuple[SnowflakeChapterPlan, bool] | None,
    scene_summaries: set[str],
) -> dict[str, Any]:
    """按场景提议出来的一章的字段；对上了已有的章就沿用作者写过的东西。

    章名：作者 / AI 起过的留着（占位名「第 N 章」照新的章序重编）；章目标：原样留着；
    章摘要：场完全没变、或是作者自己写的（不与任何一场的摘要逐字相同）才留，否则取新的末场。
    """
    fields = _chunk_chapter_fields(index, chunk)
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


def misplaced_scene_plan_ids(
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
) -> list[str]:
    """已保存的分章里，哪些场分在了故事序之外的章（沿故事序走，章序出现回退的那些场）。空 = 每章都是连续的一段。"""
    order = {chapter.chapter_plan_id: index for index, chapter in enumerate(chapters)}
    misplaced: list[str] = []
    reached = -1
    for scene in scenes:
        index = order.get(scene.chapter_plan_id or "", -1)
        if index < 0:
            continue
        if index < reached:
            misplaced.append(scene.scene_plan_id)
        else:
            reached = index
    return misplaced


def heal_assignment(
    assignment: dict[str, str | None],
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
) -> dict[str, str | None]:
    """把一份归属修成「每章都是故事序上连续的一段」，**改动的场尽量少**。

    沿故事序看每场的章序：保住最长的那条不降子序列（这些场的归属本来就是对的），其余的场是离群的——
    作者在 09 里把一场拖到了别的章的范围里、或者旧数据里两章的场交错着——各自并入故事序上前一场所在的章
    （排在全书最前面的离群场并入后一场的章）。只拖了一场，就只有这一场换章；不会像「章序只许不降」的
    简单拉平那样，一场跳到前面，后面整本书都被拽进它原来的那一章。没有归属的场原样留着。
    """
    order = {chapter.row_uid: index for index, chapter in enumerate(chapters)}
    indexed = [
        (position, order[assignment[scene.scene_plan_id]])
        for position, scene in enumerate(scenes)
        if assignment.get(scene.scene_plan_id) in order
    ]
    if not indexed:
        return dict(assignment)
    # 最长不降子序列（patience）：tails[k] = 长度 k+1 的子序列的最小结尾值，links 记前驱以便回溯
    tails: list[int] = []
    tail_at: list[int] = []
    links: list[int] = [-1] * len(indexed)
    for i, (_position, value) in enumerate(indexed):
        k = bisect_right(tails, value)
        if k == len(tails):
            tails.append(value)
            tail_at.append(i)
        else:
            tails[k] = value
            tail_at[k] = i
        links[i] = tail_at[k - 1] if k > 0 else -1
    keep: set[int] = set()
    cursor = tail_at[-1]
    while cursor >= 0:
        keep.add(indexed[cursor][0])
        cursor = links[cursor]

    fixed = dict(assignment)
    kept_positions = sorted(keep)
    first_kept = kept_positions[0]
    previous_uid: str | None = None
    for position, scene in enumerate(scenes):
        uid = assignment.get(scene.scene_plan_id)
        if uid not in order:
            continue
        if position in keep:
            previous_uid = uid
            continue
        fixed[scene.scene_plan_id] = previous_uid or assignment[scenes[first_kept].scene_plan_id]
    return fixed


def _enforce_contiguity(
    assignment: dict[str, str | None],
    chapters: list[SnowflakeChapterPlan],
    scenes: list[SnowflakeScenePlan],
) -> dict[str, str | None]:
    """章是故事序上连续的一段：沿故事序走，章序只许不降。

    模型给的分章建议可能把第 9 场放回第 2 章——章内顺序永远等于故事序，那样目录里读到的顺序就和
    场景列表分家了。回退的归属被拉平到它前面已经到达的那一章；没有归属的场原样留着（由面板报未分配）。
    """
    order = {chapter.row_uid: index for index, chapter in enumerate(chapters)}
    fixed: dict[str, str | None] = {}
    reached = -1
    for scene in scenes:
        target = assignment.get(scene.scene_plan_id)
        index = order.get(target or "", -1)
        if index < 0:
            fixed[scene.scene_plan_id] = target if target in order else None
            continue
        reached = max(reached, index)
        fixed[scene.scene_plan_id] = chapters[reached].row_uid
    return fixed


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
