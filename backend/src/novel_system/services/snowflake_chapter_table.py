"""构思侧章表（``snowflake_chapter_plans`` 行）的唯一读写处（B07-05，2026-09-30）。叶子模块：只依赖 ORM 与别的叶子。

- 读：:func:`live_chapter_plans`（按章序）、:func:`live_chapter_plans_by_catalog_id`（目录章 id → 钉着它的章计划）；
- 写：:func:`upsert_chapter_rows` + :func:`soft_delete_unlisted_chapters`——分章面板确认（``save(replace_chapters=true)``）
  与 07 章表同步（``_sync_chapter_plans``）共用这一套：认得的 row_uid 就地改、新章建行、没列出来的章软删并把挂在上面的场
  退回「未分章」。两条路只差在「认不得的 row_uid 怎么办」：面板里只有 ``new:N`` 才建新章（别的 404），07 按它自己的
  row_uid 收下（被软删过的章原样取回、同一张表里重号的另起一行）；
- 身份：:func:`mint_chapter_row_uid`、目录章号 :func:`catalog_chapter_id`（阶段 Y 的序列号，铸一次、钉住、不复用）；
- 章名规则 :func:`is_auto_chapter_title`（``chapter_title_sync`` 再导出给目录服务）；
- 07 草稿里的镜像：:func:`mirror_chapters_into_long_synopsis`。

放在分章包外面：目录服务（``chapter_title_sync`` / ``chapter_structure_ownership``）也要引它，而导入分章包的任何子模块
都会先跑包的 ``__init__``——它引着分章服务，分章服务引着回收站 / 作品服务，再绕回目录，就闭环了。
"""

from __future__ import annotations

import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from novel_system.db.models import ChapterGoal, OperationLog, SnowflakeChapterPlan, SnowflakeScenePlan, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.snowflake_queries import latest_step_run

#: 三幕结构的三个铰链（Ingermanson 的三个灾难）。章与场上只认这三个标记。
SPINE_MARKS = ("灾一", "灾二", "灾三")
#: 分章面板里还没落库的新章的临时身份前缀（确认时由 ``upsert_chapter_rows`` 铸成真正的 row_uid）。
NEW_CHAPTER_PREFIX = "new:"
# 「未命名」= 章节编排里把章名清空后落下的「未命名章节」：同样不是作者起的名字
PLACEHOLDER_TITLE_MARKERS = ("待补", "TODO", "todo", "TBD", "tbd", "占位", "未命名")
#: 系统起的占位章名「第 N 章」——它跟着章序走，不是作者的命名
AUTO_TITLE_PATTERN = re.compile(r"^第\s*\d+\s*章$")


def is_auto_chapter_title(title: Any) -> bool:
    """章名是不是系统起的占位（空、「第 N 章」、「（待补）」一类）——AI 起章名只碰这些，作者起的名字不碰。"""
    text = str(title or "").strip()
    return (
        not text
        or bool(AUTO_TITLE_PATTERN.match(text))
        or any(marker in text for marker in PLACEHOLDER_TITLE_MARKERS)
    )


def is_placeholder_chapter(chapter: Any) -> bool:
    """07 编辑器「添加章节」点出来、还没写任何东西的章行（章名是系统起的占位——空 / 「（待补）」/「第 N 章」，
    与 :func:`is_auto_chapter_title` 同一条规则——摘要、章目标、脊柱全空）。

    这种章表不是作者的分章决定：把场均摊进几个「（待补）」只会得到一份没有意义的结构，
    面板应该当它不存在、直接按场景列表提议。
    """
    blank_title = is_auto_chapter_title(getattr(chapter, "title", ""))
    has_content = any(
        str(getattr(chapter, field_name, "") or "").strip() for field_name in ("summary", "chapter_goal", "spine")
    )
    return blank_title and not has_content


def mint_chapter_row_uid() -> str:
    return f"chrow_{uuid.uuid4().hex}"


def chapter_target_id(project_id: str, serial: int) -> str:
    """第 ``serial`` 个被铸出来的雪花章在目录里的 id。

    阶段 Y（2026-09-20）：这个数字是**序列号，不是章序**。过去物化目标按章序算（第 3 章 = ``…_CH03``），
    重新分章之后同一个 id 指着另一组场：目录里那一行的章级状态（章状态、字数目标、戏剧卡、运行任务、终审）
    留在「第 3 个位置」上，从拆点往后的每张场景卡都要换章，场景运行时表上冗余的 chapter_id 成片过期。
    现在 id 钉在章计划行上（``SnowflakeChapterPlan.catalog_chapter_id``，见 :func:`catalog_chapter_id`）：
    第一次物化按章序铸出 CH01…CHnn（与从前一样），之后新出现的章拿下一个没用过的号，已有的章永远是它自己的
    那个号；显示的章序只由 ``display_order`` 决定。
    """
    return f"{project_id}_CH{serial:02d}"


def coerce_act(value: Any, fallback: int | None = 1) -> int:
    try:
        act = int(value)
    except (TypeError, ValueError):
        return int(fallback or 1)
    return act if act in (1, 2, 3) else int(fallback or 1)


def parse_outline_chapters(draft: dict[str, Any] | None) -> list[dict[str, Any]]:
    """07 长篇大纲草稿里的结构化章表 ``chapters`` → 章行字段（章序按数组顺序、幕 1–3、脊柱只认三个标记）。

    2026-09-30（B07-22）：不再从 ``paragraphs`` 里解析「NN 章名：一句话（灾一）」行——那是 2026-09-13 阶段 D 之前
    章表镜像进 07 的格式；库里每一部确认过 07 的作品都已经有章计划行（性能库只读核对）。五段展开是散文，没有
    ``chapters`` 的草稿就是没有章表，绝不从散文里造章。
    """
    payload = draft if isinstance(draft, dict) else {}
    chapters: list[dict[str, Any]] = []
    for index, item in enumerate((entry for entry in (payload.get("chapters") or []) if isinstance(entry, dict)), start=1):
        spine = str(item.get("spine") or "").strip()
        chapters.append(
            {
                "row_uid": str(item.get("row_uid") or "").strip(),
                "chapter_seq": index,
                "act": coerce_act(item.get("act"), 1),
                "title": str(item.get("title") or "").strip(),
                "summary": str(item.get("summary") or "").strip(),
                "spine": spine if spine in SPINE_MARKS else "",
                "chapter_goal": str(item.get("chapter_goal") or "").strip(),
            }
        )
    return chapters


# ------------------------------------------------------------------ 读


def live_chapter_plans(session: Session, project_id: str) -> list[SnowflakeChapterPlan]:
    """没被软删的章计划，按章序（同序按 row_uid）。"""
    return list(
        session.execute(
            select(SnowflakeChapterPlan)
            .where(SnowflakeChapterPlan.project_id == project_id, SnowflakeChapterPlan.removed_at.is_(None))
            .order_by(SnowflakeChapterPlan.chapter_seq.asc(), SnowflakeChapterPlan.row_uid.asc())
        ).scalars()
    )


def live_chapter_plans_by_catalog_id(session: Session, project_id: str | None) -> dict[str, SnowflakeChapterPlan]:
    """目录章 id → 钉着它的那一行章计划（没被软删的）。一个目录章至多被一行钉住（铸号规则保证）。"""
    if not project_id:
        return {}
    pinned: dict[str, SnowflakeChapterPlan] = {}
    for row in live_chapter_plans(session, project_id):
        chapter_id = str(row.catalog_chapter_id or "").strip()
        if chapter_id and chapter_id not in pinned:
            pinned[chapter_id] = row
    return pinned


# ------------------------------------------------------ 章在目录里的 id（阶段 Y）


def catalog_chapter_id(session: Session, chapter: SnowflakeChapterPlan, *, mint: bool = True) -> str:
    """这一章在目录里的 id。钉过就用钉的；没钉过且 ``mint`` 时铸下一个序列号并钉上。

    预览里的瞬态章（``new:N``，不在 session 里）从不铸号：确认之前什么都不落库。
    """
    pinned = str(chapter.catalog_chapter_id or "").strip()
    if pinned or not mint or not chapter.chapter_plan_id:
        return pinned
    chapter.catalog_chapter_id = chapter_target_id(chapter.project_id, next_catalog_serial(session, chapter.project_id))
    session.flush()
    return chapter.catalog_chapter_id


def is_pinnable_chapter_id(session: Session, project_id: str, chapter_id: str) -> bool:
    """目录里真有这一章（属于本作品），或者是本作品序列号的形状——才钉。

    场景行上的章戳可能是规划器 / 模型随手写的字符串（``ch1``、别的作品的章号）：钉上去就成了物化目标，
    批准时要么建出一个怪 id 的章，要么撞上 ``CHAPTER_ALREADY_OWNED``。不钉的留空，保存分章时照常铸号。
    """
    row = session.get(ChapterGoal, chapter_id)
    if row is not None:
        return row.project_id == project_id
    return re.fullmatch(rf"{re.escape(project_id)}_CH\d+", chapter_id) is not None


def next_catalog_serial(session: Session, project_id: str) -> int:
    """下一个没用过的序列号：目录里现有的章（含回收站）与所有钉过的号（含已软删的章计划）都算占用。"""
    pattern = re.compile(rf"^{re.escape(project_id)}_CH(\d+)$")
    used = [
        *session.execute(select(ChapterGoal.chapter_id).where(ChapterGoal.project_id == project_id)).scalars(),
        *session.execute(
            select(SnowflakeChapterPlan.catalog_chapter_id).where(
                SnowflakeChapterPlan.project_id == project_id,
                SnowflakeChapterPlan.catalog_chapter_id.is_not(None),
            )
        ).scalars(),
    ]
    # 场景行上的章戳不算占用：前端那一路给所有场盖的是退化默认值 ``…_CH01``，那不是一个真的章。
    serials = [int(match.group(1)) for value in used if (match := pattern.match(str(value or "")))]
    return max(serials, default=0) + 1


# ------------------------------------------------------------------ 写


def create_chapter_plan(session: Session, project_id: str, item: dict[str, Any]) -> SnowflakeChapterPlan:
    """建一行章计划（``row_uid`` 缺省时铸新的）。字段取自 ``item``，状态 draft。"""
    row_uid = str(item.get("row_uid") or "").strip() or mint_chapter_row_uid()
    row = SnowflakeChapterPlan(
        chapter_plan_id=f"snowflake_chapter_plan_{project_id}_{row_uid}",
        project_id=project_id,
        row_uid=row_uid,
        chapter_seq=int(item.get("chapter_seq") or 1),
        act=coerce_act(item.get("act"), 1),
        title=str(item.get("title") or "").strip(),
        summary=str(item.get("summary") or "").strip(),
        spine=str(item.get("spine") or "").strip(),
        chapter_goal=str(item.get("chapter_goal") or "").strip(),
        status="draft",
    )
    session.add(row)
    return row


@dataclass
class ChapterRowsUpsert:
    """一次整表写入的结果。"""

    #: 本次写入之后认得的每一行（含新建与取回的），按 row_uid
    by_uid: dict[str, SnowflakeChapterPlan] = field(default_factory=dict)
    #: 载荷里的章键（row_uid，或新章的 ``new:N``）→ 那一行——面板的场景归属按它对章
    alias: dict[str, SnowflakeChapterPlan] = field(default_factory=dict)
    #: 载荷列到的章（row_uid）；整表替换时其余的章软删
    listed: set[str] = field(default_factory=set)
    created: list[SnowflakeChapterPlan] = field(default_factory=list)
    #: 载荷里有章换了身份（新铸 / 重号另起）或是新建的——07 同步据此把 row_uid 写回它的草稿
    rewritten: bool = False


def upsert_chapter_rows(
    session: Session,
    project_id: str,
    items: list[dict[str, Any]],
    *,
    chapters: list[SnowflakeChapterPlan] | None = None,
    create_new: bool = True,
    adopt_unknown: bool = False,
    status: str | None = None,
    source_step_run_id: str | None = None,
) -> ChapterRowsUpsert:
    """按载荷的顺序写章行：章序 = 载荷里的位置；载荷**带着的**字段（标题 / 幕 / 脊柱 / 章目标 / 摘要）才改。

    认不得的 row_uid：
    - 面板（``adopt_unknown=False``）：``create_new`` 时空的与 ``new:N`` 新建一章，其余 404
      ``SNOWFLAKE_CHAPTER_PLAN_NOT_FOUND``（不带整表替换标记的旧契约只改认得的章）；
    - 07 同步（``adopt_unknown=True``）：按它的 row_uid 收下——软删过的章原样取回（身份、目录章号都还在），
      同一张表里重号的另起一行，空的铸新号；铸好的号写回 ``item["row_uid"]``。

    ``chapters`` 是面板对着的那份现状（缺省读活的章计划）。写完 flush：场景行的 ``chapter_plan_id`` 是外键，
    而 ORM 没有声明关系来排依赖顺序。
    """
    if adopt_unknown:
        known = {
            row.row_uid: row
            for row in session.execute(select(SnowflakeChapterPlan).where(SnowflakeChapterPlan.project_id == project_id)).scalars()
        }
    else:
        known = {row.row_uid: row for row in (chapters if chapters is not None else live_chapter_plans(session, project_id))}
    result = ChapterRowsUpsert(by_uid=known)
    for index, item in enumerate(items, start=1):
        row_uid = str(item.get("row_uid") or "").strip()
        if adopt_unknown:
            if row_uid in result.listed:
                row_uid = ""  # 同一张表里重号 → 当作新章
            row = known.get(row_uid) if row_uid else None
            if row is None:
                row = create_chapter_plan(session, project_id, {"row_uid": row_uid, "chapter_seq": index, "act": item.get("act")})
                known[row.row_uid] = row
                result.created.append(row)
                result.rewritten = True
            elif row.removed_at:
                row.removed_at = None
                row.removed_by = None
            if item.get("row_uid") != row.row_uid:
                item["row_uid"] = row.row_uid
                result.rewritten = True
        else:
            row = known.get(row_uid)
            if row is None and create_new and (not row_uid or row_uid.startswith(NEW_CHAPTER_PREFIX)):
                row = create_chapter_plan(session, project_id, {"chapter_seq": index, "act": item.get("act")})
                known[row.row_uid] = row
                result.created.append(row)
            if row is None:
                raise DomainError(
                    "SNOWFLAKE_CHAPTER_PLAN_NOT_FOUND",
                    f"未找到章「{item.get('title') or row_uid}」。",
                    status_code=404,
                )
            result.alias[row_uid or f"{NEW_CHAPTER_PREFIX}{index}"] = row
        result.listed.add(row.row_uid)
        row.chapter_seq = index
        _apply_chapter_fields(row, item)
        if status is not None:
            row.status = status
        if source_step_run_id is not None:
            row.source_step_run_id = source_step_run_id
    session.flush()
    return result


def _apply_chapter_fields(row: SnowflakeChapterPlan, item: dict[str, Any]) -> None:
    if "title" in item:
        row.title = str(item.get("title") or "").strip()
    if "act" in item:
        row.act = coerce_act(item.get("act"), row.act)
    if "spine" in item:
        spine = str(item.get("spine") or "").strip()
        row.spine = spine if spine in SPINE_MARKS else ""
    if "chapter_goal" in item:
        row.chapter_goal = str(item.get("chapter_goal") or "").strip()
    if "summary" in item:
        row.summary = str(item.get("summary") or "").strip()


@dataclass
class RemovedChapter:
    row: SnowflakeChapterPlan
    #: 挂在这一章上、因此退回「未分章」的活场数
    unbound_scene_count: int


def soft_delete_unlisted_chapters(
    session: Session,
    project_id: str,
    rows: Iterable[SnowflakeChapterPlan],
    listed: set[str],
    scene_plans: Iterable[SnowflakeScenePlan],
    *,
    actor_ref: str = "operator",
    reason: str | None = None,
) -> list[RemovedChapter]:
    """整表替换：``rows`` 里没列出来（不在 ``listed``）的活章软删，挂在上面的场退回「未分章」（不静默塞进别的章）。

    ``scene_plans`` 在内存里解绑——调用方可能刚改过归属还没 flush（会话不自动 flush）。
    """
    removed_at = utcnow()
    plans = list(scene_plans)
    removed: list[RemovedChapter] = []
    for row in rows:
        if row.row_uid in listed or row.removed_at:
            continue
        row.removed_at = removed_at
        row.removed_by = actor_ref or "operator"
        payload: dict[str, Any] = {
            "project_id": project_id,
            "row_uid": row.row_uid,
            "title": row.title or "",
            "removed_at": removed_at,
        }
        if reason:
            payload["reason"] = reason
        session.add(
            OperationLog(
                event_type="snowflake_chapter_plan_removed",
                object_type="snowflake_chapter_plan",
                object_ref=row.chapter_plan_id,
                payload_json=payload,
            )
        )
        unbound = 0
        for plan in plans:
            if plan.chapter_plan_id == row.chapter_plan_id:
                plan.chapter_plan_id = None
                if plan.removed_at is None:
                    unbound += 1
        removed.append(RemovedChapter(row=row, unbound_scene_count=unbound))
    return removed


# ------------------------------------------------------------------ 07 草稿里的镜像
#
# 2026-09-30（R11，批准 #18a）起 07 的章表是分章结果的**只读镜像**：分章面板（与章节编排改名）是唯一改章的地方，
# 07 只显示。前端不再上行章表；07 保存不带章表时沿用存着的那一份（``carry_stored_chapters``），07 重新生成时只要
# 有章表行就保留现表、不收模型的章（``keep_live_chapter_table``）。API 调用方显式给的章表照旧同步成行。


def chapter_table_rows(chapters: Iterable[SnowflakeChapterPlan]) -> list[dict[str, Any]]:
    """章表行 → 07 草稿里 ``chapters`` 的形状（按章序）。"""
    ordered = sorted(chapters, key=lambda row: (int(row.chapter_seq or 0), row.row_uid))
    return [
        {
            "row_uid": row.row_uid,
            "chapter_seq": row.chapter_seq,
            "act": row.act,
            "title": row.title or "",
            "summary": row.summary or "",
            "spine": row.spine or "",
            "chapter_goal": row.chapter_goal or "",
        }
        for row in ordered
    ]


def mirror_chapters_into_long_synopsis(session: Session, project_id: str, chapters: list[SnowflakeChapterPlan]) -> None:
    """把章表写回 07 最新草稿的 ``chapters``（带 row_uid），07 显示的与章表行才是同一份。

    草稿里的 ``fe_scaffold.chapters`` 曾是前端的第二份章表（水合时还优先于规范字段——新浏览器看到的是旧章表，下一次
    07 上行又把它当作者的章表同步回来，把刚确认的分章冲掉）。R11 起前端从规范的 ``chapters`` 读章表、不再上行它，
    这里也不再维护那一份：还留着的旧副本去掉，免得哪个没刷新的页面把它当章表读。
    """
    run = latest_step_run(session, project_id, "long_synopsis")
    if run is None:
        return
    draft = dict(run.draft_json or {})
    draft["chapters"] = chapter_table_rows(chapters)
    scaffold = draft.get("fe_scaffold")
    if isinstance(scaffold, dict) and "chapters" in scaffold:
        draft["fe_scaffold"] = {key: value for key, value in scaffold.items() if key != "chapters"}
    run.draft_json = draft
    flag_modified(run, "draft_json")


def carry_stored_chapters(draft: dict[str, Any], sent_draft: Any, stored_run: Any) -> bool:
    """07 保存没带章表（前端不再上行它；空表也算没带）：把存着的那一份原样放回 ``draft``，返回 True。

    必须在「这一版和已确认的是不是同一个故事」的比较之前做：``merge_step_draft`` 从步骤默认值起，缺席的章表会变成
    空表，已确认的 07 就被当成改过、打回待审。沿用时调用方也不去同步章表行——章表没动。显式带了章表（API 调用方）
    返回 False，照旧同步。
    """
    sent = sent_draft.get("chapters") if isinstance(sent_draft, dict) else None
    if isinstance(sent, list) and any(isinstance(item, dict) for item in sent):
        return False
    stored = (getattr(stored_run, "draft_json", None) or {}).get("chapters") if stored_run is not None else None
    draft["chapters"] = deepcopy(stored) if isinstance(stored, list) else []
    return True


def keep_live_chapter_table(session: Session, project_id: str, draft: dict[str, Any]) -> bool:
    """07 重新生成：已经有章表行就保留现表（模型给的章表不收——拆章 / 并章 / 改章名都在分章面板），返回 True。"""
    live = live_chapter_plans(session, project_id)
    if not live:
        return False
    draft["chapters"] = chapter_table_rows(live)
    return True
