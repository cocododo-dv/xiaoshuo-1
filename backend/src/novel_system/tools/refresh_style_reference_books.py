"""刷新已导入参考书的派生统计(2026-09-14 风格保真修补之后的数据维护;2026-09-23 v3 修正)。

不删书、不动画像与绑定。对每本**已就绪**(``status == ready``)的书:

1. 剥离副文本段(脚注 / 站点声明 / 网址短行,``text_utils.is_paratext_paragraph``)——它们此前进过
   指标、声音签名、样例窗口与结构画像的章首 / 章尾样例;引用被剥段落的引文只解除父段关联
   (引文按 paragraph_id 引段,重编号不影响其余引文与证据);
2. 剥离后把 ``paragraph_index`` **重编号**为连续的 0..n-1(窗口、相邻段展开都按「遇缺口即停」读段落表,
   留洞会把样例窗切碎);
3. **保留**导入期记下的场界(含只有重新导入才能恢复的空行型场界):按新编号搬运,落在被剥段之后的
   挪到前一个保留段;单独一行的省略号(「……」)不再算场界;再并上纯符号分隔行;
4. 重算 ``stats_json`` 的 ``metrics`` / ``prose_shape_metrics`` / ``paragraph_type_distribution`` /
   ``voice_signature`` / ``paratext_dropped``,写 ``refresh`` 审计;段落行有变化时 ``pop`` 掉
   ``paragraph_root_sha256`` / ``paragraph_count``(契约 §3.1:写段落表的人负责,窗口索引据此重建)。

没有就绪的书(分类中 / 失败)与有排队 / 运行中作业(分类 / 就地重标 / 学习文风)的书一律跳过——就地重标时
书一直是 ready,得看作业表。``--execute`` 时每本书一个事务:先拿写锁(``BEGIN IMMEDIATE``),在锁里重新
核对作业、重新算计划、写库、提交——算计划与写库之间没有别的写者能插进来;``stats_json`` 只合并本工具管的键
(``json_set`` / ``json_remove``),不整列覆盖。必须用 ``--book ID``(可重复)或显式 ``--all`` 选书;默认干跑。

用法(backend 目录下):
    python -m novel_system.tools.refresh_style_reference_books --all                # 干跑,全部就绪的书
    python -m novel_system.tools.refresh_style_reference_books --book sr_book_xxx --execute
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, update

from novel_system.db.models import StyleReferenceBook
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.jobs import BOOK_EXCLUSIVE_KINDS, StyleJobService
from novel_system.services.style_reference.paragraph_root import COUNT_KEY, ROOT_KEY, patch_book_stats
from novel_system.services.style_reference.metrics import (
    MetricsEngine,
    ParagraphRecord,
    compute_prose_shape_with_variance,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.text_utils import (
    is_paratext_paragraph,
    is_scene_break_paragraph,
    remap_scene_breaks,
)
from novel_system.services.style_reference.voice_signature import compute_voice_signature

TOOL_VERSION = "refresh_style_reference_books_v2"

_ELLIPSIS_ONLY = frozenset("…⋯.。 \t")


def _is_ellipsis_line(text: str) -> bool:
    stripped = str(text or "").strip()
    return bool(stripped) and all(ch in _ELLIPSIS_ONLY for ch in stripped)


def plan_book_refresh(session, book_id: str) -> dict[str, Any] | None:
    """算出一本书的刷新计划与新统计(纯读;``apply_book_refresh`` 才写)。书不存在返回 None;
    书没有就绪时返回带 ``skipped`` 原因的计划(``apply_book_refresh`` 对它什么都不做)。"""
    repo = StyleReferenceRepository(session)
    book = repo.get_book(book_id)
    if book is None:
        return None
    if str(book.status or "") != "ready":
        return {"book": book, "title": book.title, "skipped": f"状态是 {book.status},不是 ready"}
    busy = [
        job for job in StyleJobService(session).active_for_book(book_id) if job.kind in BOOK_EXCLUSIVE_KINDS
    ]
    if busy:
        return {
            "book": book,
            "title": book.title,
            "skipped": f"有{busy[0].kind}作业在排队 / 运行({busy[0].job_id}):等它完成或取消后再刷新",
        }
    paragraphs = repo.list_paragraphs(book_id)
    paratext = [p for p in paragraphs if is_paratext_paragraph(p.text or "")]
    removed_ids = {p.paragraph_id for p in paratext}
    kept = [p for p in paragraphs if p.paragraph_id not in removed_ids]
    old_to_new = {int(p.paragraph_index): new for new, p in enumerate(kept)}
    renumber = [(p, new) for new, p in enumerate(kept) if int(p.paragraph_index) != new]
    kept_texts = [str(p.text or "") for p in kept if str(p.text or "").strip()]
    records = [ParagraphRecord(text=str(p.text or ""), paragraph_type=p.paragraph_type) for p in kept]
    sample_count = len(records)
    metrics_block = {
        name: {"mean": float(mean), "std": float(std), "sample_count": sample_count}
        for name, (mean, std) in MetricsEngine().compute_with_variance(records).items()
    }
    prose_shape_block = {
        name: {"mean": float(mean), "std": float(std), "sample_count": sample_count}
        for name, (mean, std) in compute_prose_shape_with_variance(records).items()
    }
    type_counter = Counter(p.paragraph_type for p in kept)
    type_distribution = (
        {ptype: round(count / sample_count, 4) for ptype, count in type_counter.items()}
        if sample_count
        else {}
    )
    old_stats = dict(book.stats_json or {})
    text_by_old_index = {int(p.paragraph_index): str(p.text or "") for p in paragraphs}
    # 导入期记下的场界:按新编号搬运;旧规则把单独一行的「……」也记成了场界(记在那一行自己的编号上),去掉
    recorded = [
        int(index)
        for index in (old_stats.get("scene_breaks") or [])
        if isinstance(index, int) and not _is_ellipsis_line(text_by_old_index.get(int(index), ""))
    ]
    kept_breaks = remap_scene_breaks(recorded, old_to_new)
    symbol_breaks = [new for new, p in enumerate(kept) if is_scene_break_paragraph(str(p.text or ""))]
    scene_breaks = sorted(set(kept_breaks) | set(symbol_breaks))
    voice_signature = compute_voice_signature(kept_texts)
    quotes_to_detach = [
        q for q in repo.list_quotes(book_id) if q.paragraph_id and q.paragraph_id in removed_ids
    ]
    old_voice = (old_stats.get("voice_signature") or {}).get("features") or {}
    return {
        "book": book,
        "title": book.title,
        "skipped": None,
        "paragraph_count": len(paragraphs),
        "paratext": paratext,
        "kept_count": len(kept),
        "renumber": renumber,
        "rows_changed": bool(paratext or renumber),
        "quotes_to_detach": quotes_to_detach,
        "scene_breaks": scene_breaks,
        "scene_breaks_before": len(old_stats.get("scene_breaks") or []),
        "stats_update": {
            "metrics": metrics_block,
            "prose_shape_metrics": prose_shape_block,
            "paragraph_type_distribution": type_distribution,
            "voice_signature": voice_signature,
            "scene_breaks": scene_breaks,
            "paratext_dropped": int(old_stats.get("paratext_dropped") or 0) + len(paratext),
        },
        "before": {
            "person_third_share": old_voice.get("person_third_share"),
            "classical_word_ratio": ((old_stats.get("metrics") or {}).get("classical_word_ratio") or {}).get("mean"),
        },
        "after": {
            "person_third_share": (voice_signature.get("features") or {}).get("person_third_share"),
            "classical_word_ratio": (metrics_block.get("classical_word_ratio") or {}).get("mean"),
        },
    }


def apply_book_refresh(session, plan: dict[str, Any]) -> None:
    """按计划写库:解除引文父段、删副文本段、重编号、更新 stats_json(调用方 commit)。"""
    if plan.get("skipped"):
        return
    book = plan["book"]
    for quote in plan["quotes_to_detach"]:
        quote.paragraph_id = None
    for paragraph in plan["paratext"]:
        session.delete(paragraph)
    session.flush()
    for paragraph, new_index in plan["renumber"]:
        paragraph.paragraph_index = new_index
    session.flush()
    values = dict(plan["stats_update"])
    values["refresh"] = {
        "tool": TOOL_VERSION,
        "at": datetime.now(timezone.utc).isoformat(),
        "paragraphs_removed": len(plan["paratext"]),
        "paragraphs_renumbered": len(plan["renumber"]),
        "quotes_detached": len(plan["quotes_to_detach"]),
        "scene_breaks": len(plan["scene_breaks"]),
    }
    # 只合并本工具管的键(json_set),别的写者写进 stats_json 的键原样保留
    patch_book_stats(session, book.book_id, values)
    if plan["rows_changed"]:
        # 契约 §3.1:改动段落行的写入者负责作废根哈希;窗口索引发现缺失时现算并重建。
        session.execute(
            update(StyleReferenceBook)
            .where(StyleReferenceBook.book_id == book.book_id)
            .values(stats_json=func.json_remove(StyleReferenceBook.stats_json, f"$.{ROOT_KEY}", f"$.{COUNT_KEY}"))
            .execution_options(synchronize_session=False)
        )
        session.expire(book, ["stats_json"])
    session.flush()


def _begin_immediate(session) -> None:
    if session.get_bind().dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _fmt(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def _describe(plan: dict[str, Any]) -> str:
    if plan.get("skipped"):
        return f"{plan['book'].book_id}  《{plan['title']}》  {plan['skipped']},跳过"
    lines = [
        f"{plan['book'].book_id}  《{plan['title']}》  段落 {plan['paragraph_count']} → {plan['kept_count']}"
        f"（副文本 {len(plan['paratext'])} 段，重编号 {len(plan['renumber'])} 段，"
        f"解除引文 {len(plan['quotes_to_detach'])} 条，场界 {plan['scene_breaks_before']} → {len(plan['scene_breaks'])} 处）",
        f"  人称·第三人称占比 {_fmt(plan['before']['person_third_share'])} → {_fmt(plan['after']['person_third_share'])}；"
        f"文言比例 {_fmt(plan['before']['classical_word_ratio'])} → {_fmt(plan['after']['classical_word_ratio'])}",
    ]
    for paragraph in plan["paratext"][:3]:
        lines.append(f"  剥离 #{paragraph.paragraph_index}: {str(paragraph.text or '')[:48]}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--book", action="append", default=None, help="刷新这本书(可重复)")
    selector.add_argument("--all", action="store_true", help="刷新全部就绪的书(必须显式给出)")
    parser.add_argument("--execute", action="store_true", help="真正写库(默认只干跑)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with SessionLocal() as session:
        repo = StyleReferenceRepository(session)
        if args.book:
            book_ids = list(dict.fromkeys(args.book))
        else:
            book_ids = [book.book_id for book in repo.list_books()]
        if not book_ids:
            print("没有参考书需要刷新。")
            return 0
        plans = []
        for book_id in book_ids:
            plan = plan_book_refresh(session, book_id)
            if plan is None:
                print(f"{book_id}: 不存在，跳过")
                continue
            print(_describe(plan))
            if not plan.get("skipped"):
                plans.append(plan)
        if not args.execute:
            print(f"\n干跑：{len(plans)} 本书未写库；加 --execute 执行。")
            return 0
        refreshed = 0
        for plan in plans:
            book_id = plan["book"].book_id
            # 每本书一个事务:先拿写锁,在锁里重新核对作业、重新算计划(干跑的计划可能已经过时),写库、提交
            session.commit()
            _begin_immediate(session)
            locked = plan_book_refresh(session, book_id)
            if locked is None or locked.get("skipped"):
                session.rollback()
                print(f"{book_id}: {(locked or {}).get('skipped') or '已不存在'},跳过")
                continue
            apply_book_refresh(session, locked)
            session.commit()
            refreshed += 1
        print(f"\n已刷新 {refreshed} 本书。有画像的书请重新学习文风以更新文风卡与声音特征。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
