"""刷新已导入参考书的派生统计（2026-09-14 风格保真修补之后的一次性数据维护）。

不删书、不动画像与绑定。对每本书：

1. 剥离副文本段（脚注 / 站点声明 / 网址短行，``text_utils.is_paratext_paragraph``）——它们此前进过
   指标、声音签名、样例窗口与结构画像的章首 / 章尾样例；引用被剥段落的引文只解除父段关联；
2. 重算 ``stats_json`` 的 ``metrics`` / ``prose_shape_metrics`` / ``paragraph_type_distribution``（文言比例
   只认文言用法）、``voice_signature``（人称只数叙述）、``scene_breaks``（纯符号分隔行；空行型场界只有
   重新导入原文才能恢复）、``paratext_dropped``，并写 ``refresh`` 审计。

段落被剥离后整本书的段落根哈希改变：已冻结进 bundle 的契约退回引文兜底路径（设计如此）；
画像里的 ``structure_card`` / ``voice_signature`` / ``exemplar_windows`` 要重新合成才会更新
（渲染期的窗口索引会按新段落表惰性复算）。默认干跑，``--execute`` 才写库。

用法（backend 目录下）:
    python -m novel_system.tools.refresh_style_reference_books            # 干跑，全部书
    python -m novel_system.tools.refresh_style_reference_books --book sr_book_xxx --execute
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from novel_system.db.models import StyleReferenceQuote
from novel_system.db.session import SessionLocal
from novel_system.services.style_reference.metrics import (
    MetricsEngine,
    ParagraphRecord,
    compute_prose_shape_with_variance,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.text_utils import (
    is_paratext_paragraph,
    is_scene_break_paragraph,
)
from novel_system.services.style_reference.voice_signature import compute_voice_signature

TOOL_VERSION = "refresh_style_reference_books_v1"


def plan_book_refresh(session, book_id: str) -> dict[str, Any] | None:
    """算出一本书的刷新计划与新统计（纯读；``apply_book_refresh`` 才写）。"""
    repo = StyleReferenceRepository(session)
    book = repo.get_book(book_id)
    if book is None:
        return None
    paragraphs = repo.list_paragraphs(book_id)
    paratext = [p for p in paragraphs if is_paratext_paragraph(p.text or "")]
    removed_ids = {p.paragraph_id for p in paratext}
    kept = [p for p in paragraphs if p.paragraph_id not in removed_ids]
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
    scene_breaks = sorted(
        int(p.paragraph_index) for p in kept if is_scene_break_paragraph(str(p.text or ""))
    )
    voice_signature = compute_voice_signature(kept_texts)
    quotes_to_detach = [
        q for q in repo.list_quotes(book_id) if q.paragraph_id and q.paragraph_id in removed_ids
    ]
    old_stats = dict(book.stats_json or {})
    old_voice = (old_stats.get("voice_signature") or {}).get("features") or {}
    return {
        "book": book,
        "title": book.title,
        "paragraph_count": len(paragraphs),
        "paratext": paratext,
        "kept_count": len(kept),
        "quotes_to_detach": quotes_to_detach,
        "scene_breaks": scene_breaks,
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
    """按计划写库：解除引文父段、删副文本段、更新 stats_json（调用方 commit）。"""
    book = plan["book"]
    for quote in plan["quotes_to_detach"]:
        quote.paragraph_id = None
    for paragraph in plan["paratext"]:
        session.delete(paragraph)
    session.flush()
    stats = dict(book.stats_json or {})
    stats.update(plan["stats_update"])
    stats["refresh"] = {
        "tool": TOOL_VERSION,
        "at": datetime.now(timezone.utc).isoformat(),
        "paragraphs_removed": len(plan["paratext"]),
        "quotes_detached": len(plan["quotes_to_detach"]),
    }
    book.stats_json = stats
    flag_modified(book, "stats_json")
    session.flush()


def _fmt(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def _describe(plan: dict[str, Any]) -> str:
    lines = [
        f"{plan['book'].book_id}  《{plan['title']}》  段落 {plan['paragraph_count']} → {plan['kept_count']}"
        f"（副文本 {len(plan['paratext'])} 段，解除引文 {len(plan['quotes_to_detach'])} 条，"
        f"符号场界 {len(plan['scene_breaks'])} 处）",
        f"  人称·第三人称占比 {_fmt(plan['before']['person_third_share'])} → {_fmt(plan['after']['person_third_share'])}；"
        f"文言比例 {_fmt(plan['before']['classical_word_ratio'])} → {_fmt(plan['after']['classical_word_ratio'])}",
    ]
    for paragraph in plan["paratext"][:3]:
        lines.append(f"  剥离 #{paragraph.paragraph_index}: {str(paragraph.text or '')[:48]}")
    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--book", action="append", default=None, help="只刷新这本书（可重复；默认全部）")
    parser.add_argument("--execute", action="store_true", help="真正写库（默认只干跑）")
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
            plans.append(plan)
            print(_describe(plan))
        if not args.execute:
            print(f"\n干跑：{len(plans)} 本书未写库；加 --execute 执行。")
            return 0
        for plan in plans:
            apply_book_refresh(session, plan)
        session.commit()
        print(f"\n已刷新 {len(plans)} 本书。有画像的书请重新合成以更新 structure_card / voice_signature / exemplar_windows。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
