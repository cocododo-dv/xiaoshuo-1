"""风格参考 v3 —「本场预览」的载荷(台账 U6 / U13 / J12):一场起草时会拿到的样例窗口与文风卡,只读。

渲染本身是 ``inject.preview.preview_render``(与起草同一套选窗、同一个块次序);这里把它的结果整理成界面要的
形状:

- ``windows``:按原书顺序的样例窗——章号、位置(章首 / 章末 / 整章 / 章中)、字数与段数、是按哪条配额选进来的
  (``slot``)、占比最大的段落类型(``paragraph_type``),以及学习作业给这一窗打的场面 / 情绪标签、这一窗最能示范的
  维度键(``dimensions``,前端按 STYLE_DIMENSION_LABELS 显示中文名;2026-09-24 O1 起取代书特有的「手法」)与一句话
  梗概(``gist``,专名已换成代称);
- ``blocks``:文风卡 / 声音 / 原文样例 / 红线四块的全文(样例是参考作者的原文,只在本机给作者看);
- ``sizes``:system 前缀、user 尾块、样例、文风卡各多少字;
- ``reference_mode``:书的云策略压过之后**真正生效**的参考方式(``requested_reference_mode`` 是配置里写的);
- ``notices``:渲染时的提示码(样例被书的云策略挡下、书被改过按当前索引选窗、没有窗口……)。

旧预览端点的字段(``fragments`` / ``prefix`` / ``user_tail`` / ``stats`` / ``window_refs``)原样保留。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard, StyleReferenceBook, StyleReferenceProfile, StyleReferenceWindow
from novel_system.services.style_reference.binding_config import normalize_binding_config
from novel_system.services.style_reference.windows import WINDOW_INDEX_VERSION, index_marker


def _dominant_type(type_mix: Any) -> str:
    """窗口里占比最大的段落类型(并列取键名靠前的,结果稳定);没有类型分布时给空串。"""
    if not isinstance(type_mix, Mapping):
        return ""
    best = ""
    best_share = 0.0
    for key in sorted(str(k) for k in type_mix):
        try:
            share = float(type_mix[key] or 0.0)
        except (TypeError, ValueError):
            continue
        if share > best_share:
            best, best_share = key, share
    return best


def window_tag_rows(session: Session, book_id: str | None, window_nos: Sequence[int]) -> dict[int, dict[str, Any]]:
    """当前窗口索引里这些窗的标签与主段落类型(按书的索引标记限定 root,旧索引的行不认)。

    本场预览与起草台的「本场参考窗口」(工作台 ``style_windows``)共用:``{window_no: {tags, paragraph_type}}``,
    ``tags`` 是学习作业打的 ``{situations, moods, dimensions, gist}``(v1 标签没有 ``dimensions``)。"""
    if not book_id or not window_nos:
        return {}
    book = session.get(StyleReferenceBook, str(book_id))
    marker = index_marker(book.stats_json if book is not None else None) or {}
    stmt = select(
        StyleReferenceWindow.window_no, StyleReferenceWindow.tags_json, StyleReferenceWindow.type_mix_json
    ).where(
        StyleReferenceWindow.book_id == str(book_id),
        StyleReferenceWindow.index_version == WINDOW_INDEX_VERSION,
        StyleReferenceWindow.window_no.in_(sorted({int(n) for n in window_nos})),
    )
    root = str(marker.get("root") or "")
    if root:
        stmt = stmt.where(StyleReferenceWindow.root_sha256 == root)
    out: dict[int, dict[str, Any]] = {}
    for window_no, tags, type_mix in session.execute(stmt):
        out[int(window_no)] = {
            "tags": dict(tags) if isinstance(tags, Mapping) else {},
            "paragraph_type": _dominant_type(type_mix),
        }
    return out


def scene_preview_payload(
    session: Session,
    profile_id: str,
    result: Mapping[str, Any],
    *,
    config: Mapping[str, Any],
    scene_id: str | None = None,
) -> dict[str, Any]:
    """``preview_render`` 的结果 → 本场预览的 v3 字段(见模块文档)。"""
    profile = session.get(StyleReferenceProfile, str(profile_id))
    book_id = str(profile.book_id) if profile is not None else None
    fragments = dict(result.get("fragments") or {})
    stats = dict(result.get("stats") or {})
    refs = [dict(item) for item in result.get("window_refs") or []]
    rows = window_tag_rows(session, book_id, [int(ref.get("window_no") or 0) for ref in refs])
    windows = []
    for ref in refs:
        row = rows.get(int(ref.get("window_no") or 0), {})
        window_tags = row.get("tags") or {}
        windows.append(
            {
                "window_no": int(ref.get("window_no") or 0),
                "chapter": int(ref.get("chapter") or 0),
                "position": str(ref.get("position") or ""),
                "start": int(ref.get("start") or 0),
                "end": int(ref.get("end") or 0),
                "chars": int(ref.get("chars") or 0),
                "paragraphs": int(ref.get("paragraphs") or 0),
                "slot": str(ref.get("slot") or ""),
                "dialogue_share": float(ref.get("dialogue_share") or 0.0),
                "situations": list(window_tags.get("situations") or ref.get("situations") or []),
                "moods": list(window_tags.get("moods") or []),
                "dimensions": list(window_tags.get("dimensions") or ref.get("dimensions") or []),
                "gist": str(window_tags.get("gist") or ""),
                "paragraph_type": str(row.get("paragraph_type") or ""),
            }
        )
    blocks = {
        "card": str(fragments.get("positive_block") or ""),
        "voice": str(fragments.get("voice_block") or ""),
        "samples": str(fragments.get("few_shot_block") or ""),
        "red_line": str(fragments.get("anti_plagiarism_block") or ""),
    }
    prefix = str(result.get("prefix") or "")
    user_tail = str(result.get("user_tail") or "")
    audit = dict(result.get("audit") or {})
    request_audit = dict(audit.get("request") or {}) if isinstance(audit.get("request"), Mapping) else {}
    normalized = normalize_binding_config(config)
    scene_payload = None
    if scene_id:
        scene = session.get(SceneCard, str(scene_id))
        scene_payload = {
            "scene_id": str(scene_id),
            "found": scene is not None,
            "chapter_id": getattr(scene, "chapter_id", None),
            "position": request_audit.get("position"),
            "situation_tags": list(request_audit.get("situation_tags") or []),
        }
    return {
        "reference_mode": result.get("reference_mode"),
        "requested_reference_mode": normalized["reference_mode"],
        "sample_windows": result.get("sample_windows"),
        "draft_mode": normalized["draft_mode"],
        "dimension_states": normalized["dimension_states"],
        "scene": scene_payload,
        "windows": windows,
        "blocks": blocks,
        "sizes": {
            "system_prefix_chars": len(prefix),
            "user_tail_chars": len(user_tail),
            "total_chars": len(prefix) + len(user_tail),
            "sample_windows": int(stats.get("few_shot_windows") or 0),
            "sample_chars": int(stats.get("few_shot_chars") or 0),
            "card_chars": len(blocks["card"]),
            "card_lines": int(stats.get("positive_lines") or 0) + int(stats.get("avoid_lines") or 0),
            "voice_lines": int(stats.get("voice_lines") or 0),
        },
        "notices": list(audit.get("notices") or []),
        "samples_blocked": audit.get("samples_blocked"),
    }


__all__ = ["scene_preview_payload", "window_tag_rows"]
