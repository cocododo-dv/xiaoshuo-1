"""跨章检查（2026-10-20，成稿中心「跨章」页签）：把全书已写的章放在一起看，一章一章看不出来的问题。

不用模型，按各场现在的正文（写作台上那份）现算，读不写：

- **反复出现的写法**：全书逐字反复出现的说法（``scene_diagnosis.repeats``）——按短语列出在哪几章哪几场；每一处同时是
  写作台深改里那一场的一条「跨场重复」发现（同一个 ``signal_id``），在这里点哪一场就到写作台那一场的那一条；
  作者在某一场忽略了，这里那一处标「已忽略」。
- **章与章的形状**：写完的章（每一个活场都有正文）连着 :data:`STREAK_MIN_CHAPTERS` 章以上停在同一种收尾（问句、省略号、
  一句对白、感叹）或以同一种开头（一句对白）起；篇幅比写完的章的中位数短一半以上或长一倍以上的章（至少三章写完才算）。
- **线索**：线索台账（``story_memory.thread_ledger_overview``，章摘要里模型记下的埋线与兑现）里到了计划兑现的章还没兑现、
  好几章没碰的线——这里只报数和几条，标记与处理在成稿中心「记忆」页签。
"""

from __future__ import annotations

from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from novel_system.services.scene_diagnosis import SceneDiagnosisService
from novel_system.services.scene_lookup import require_project
from novel_system.services.story_memory_digest import chapter_display_title

# 连着这么多章停在同一种收尾 / 以同一种开头起，才说一声
STREAK_MIN_CHAPTERS = 3
# 篇幅：至少这么多章写完才比；比中位数短一半以上或长一倍以上
LENGTH_MIN_CHAPTERS = 3
LENGTH_SHORT_RATIO = 0.5
LENGTH_LONG_RATIO = 2.0
# 线索：最多列几条
THREAD_ROWS = 5

_QUOTE_OPEN = "“「『\"‘"
_QUOTE_CLOSE = "”」』\"’"
EDGE_KIND_LABELS = {
    "question": "问句",
    "ellipsis": "省略号",
    "dialogue": "一句对白",
    "exclaim": "感叹",
    "narration": "叙述",
}
# 连着出现才值得说的那几种（叙述收尾 / 开头是常态）
_STREAK_KINDS = {"ending": ("question", "ellipsis", "dialogue", "exclaim"), "opening": ("dialogue",)}


def paragraph_edge_kind(paragraph: str, edge: str = "ending") -> str:
    """一段是哪种。章的最后一段（``ending``）：问句 / 省略号 / 一句对白 / 感叹 / 叙述；章的第一段（``opening``）只分
    一句对白（以引号起）与叙述——开头是一句问话的对白，算对白。"""
    text = str(paragraph or "").strip()
    if not text:
        return "narration"
    if edge == "opening":
        return "dialogue" if text[0] in _QUOTE_OPEN else "narration"
    core = text.rstrip(_QUOTE_CLOSE + " ")
    if core.endswith(("？", "?")):
        return "question"
    if core.endswith(("……", "…")):
        return "ellipsis"
    if text[0] in _QUOTE_OPEN or text[-1] in _QUOTE_CLOSE:
        return "dialogue"
    if core.endswith(("！", "!")):
        return "exclaim"
    return "narration"


def _streaks(chapters: list[dict[str, Any]], edge: str) -> list[dict[str, Any]]:
    """写完的章里，连着 :data:`STREAK_MIN_CHAPTERS` 章以上同一种收尾 / 开头（中间夹着没写完的章就断开）。"""
    key = f"{edge}_kind"
    out: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []

    def close() -> None:
        if len(run) >= STREAK_MIN_CHAPTERS and run[0][key] in _STREAK_KINDS[edge]:
            out.append(
                {
                    "edge": edge,
                    "kind": run[0][key],
                    "label": EDGE_KIND_LABELS[run[0][key]],
                    "chapter_ids": [item["chapter_id"] for item in run],
                    "chapter_numbers": [item["number"] for item in run],
                }
            )

    for chapter in chapters:
        if not chapter["complete"]:
            close()
            run = []
            continue
        if run and run[-1][key] == chapter[key]:
            run.append(chapter)
            continue
        close()
        run = [chapter]
    close()
    return out


def _length_outliers(chapters: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [chapter for chapter in chapters if chapter["complete"]]
    if len(complete) < LENGTH_MIN_CHAPTERS:
        return {"median_chars": None, "items": [], "enough": False}
    middle = median(chapter["chars"] for chapter in complete)
    items = []
    for chapter in complete:
        ratio = chapter["chars"] / middle if middle else 1.0
        if ratio < LENGTH_SHORT_RATIO or ratio > LENGTH_LONG_RATIO:
            items.append(
                {
                    "chapter_id": chapter["chapter_id"],
                    "number": chapter["number"],
                    "chars": chapter["chars"],
                    "ratio": round(ratio, 2),
                    "kind": "short" if ratio < LENGTH_SHORT_RATIO else "long",
                }
            )
    return {"median_chars": int(middle), "items": items, "enough": True}


class CrossChapterService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def report(self, project_id: str) -> dict[str, Any]:
        require_project(self.session, project_id)
        diagnosis = SceneDiagnosisService(self.session)
        rows = diagnosis.project_texts(project_id)
        repeats = diagnosis.project_repeats(project_id)
        ignored_by_scene = {
            scene.scene_id: {str(key) for key in (scene.deep_review_ignored_keys_json or []) if str(key)}
            for _chapter, _cn, scene, _sn, _text in rows
        }

        phrases = []
        for item in repeats.phrases if repeats is not None else ():
            occurrences = [
                {
                    "chapter_id": occurrence.chapter_id,
                    "chapter_number": occurrence.chapter_number,
                    "scene_id": occurrence.scene_id,
                    "scene_number": occurrence.scene_number,
                    "paragraph_index": occurrence.paragraph_index,
                    "excerpt": occurrence.excerpt,
                    "ignored": item.signal_id in ignored_by_scene.get(occurrence.scene_id, set()),
                }
                for occurrence in item.occurrences
            ]
            phrases.append(
                {
                    "phrase": item.phrase,
                    "signal_id": item.signal_id,
                    "count": item.count,
                    "scene_count": len(item.scene_ids),
                    "chapter_count": len(item.chapter_ids),
                    "reference_count": item.reference_count,
                    "habitual": item.habitual,
                    "ignored": all(entry["ignored"] for entry in occurrences),
                    "occurrences": occurrences,
                }
            )

        chapters: list[dict[str, Any]] = []
        texts = {scene.scene_id: (scene, scene_number, text) for _chapter, _cn, scene, scene_number, text in rows}
        # 按全书的章序逐章列（还没有场的章也在：它不算写完，连着几章的判断在它这儿断开）
        for number, (chapter, scenes) in enumerate(diagnosis.project_scenes_in_order(project_id), start=1):
            members = [texts[scene.scene_id] for scene in scenes if scene.scene_id in texts]
            written = [(scene, scene_number, text) for scene, scene_number, text in members if text.layer != "none"]
            paragraphs = [paragraph for _scene, _n, text in written for paragraph in text.paragraphs if paragraph.strip()]
            complete = bool(members) and len(written) == len(members)
            chapters.append(
                {
                    "chapter_id": chapter.chapter_id,
                    "number": number,
                    "title": chapter_display_title(chapter),
                    "scenes": len(members),
                    "scenes_with_text": len(written),
                    "complete": complete,
                    "chars": sum(text.chars for _scene, _n, text in written),
                    "opening_kind": paragraph_edge_kind(paragraphs[0], "opening") if paragraphs else None,
                    "ending_kind": paragraph_edge_kind(paragraphs[-1], "ending") if paragraphs else None,
                }
            )

        return {
            "project_id": project_id,
            "scenes_with_text": repeats.scenes_with_text if repeats is not None else 0,
            "chapters_with_text": sum(1 for chapter in chapters if chapter["scenes_with_text"]),
            "chapters_complete": sum(1 for chapter in chapters if chapter["complete"]),
            "chars": repeats.chars if repeats is not None else 0,
            "reference_bound": bool(repeats is not None and repeats.reference_bound),
            "repeats": phrases,
            "shapes": {
                "chapters": chapters,
                "streaks": [*_streaks(chapters, "ending"), *_streaks(chapters, "opening")],
                "lengths": _length_outliers(chapters),
                "streak_min_chapters": STREAK_MIN_CHAPTERS,
                "length_min_chapters": LENGTH_MIN_CHAPTERS,
            },
            "threads": self._threads(project_id),
            "edge_kind_labels": dict(EDGE_KIND_LABELS),
        }

    def _threads(self, project_id: str) -> dict[str, Any]:
        """线索台账里有提醒的线（标记与处理在「记忆」页签）。"""
        from novel_system.services.story_memory import thread_ledger_overview

        ledger = thread_ledger_overview(self.session, project_id)
        alerts = [row for row in ledger.get("open") or [] if row.get("alert")]
        return {
            "current_number": ledger.get("current_number") or 0,
            "open_count": len(ledger.get("open") or []),
            "payoff_missed": sum(1 for row in alerts if row["alert"] == "payoff_missed"),
            "cold": sum(1 for row in alerts if row["alert"] == "cold"),
            "items": [
                {
                    "thread_id": row.get("thread_id"),
                    "thread": row.get("thread"),
                    "alert": row.get("alert"),
                    "opened_in": row.get("opened_in"),
                    "last_touched_in": row.get("last_touched_in"),
                    "planned_payoff_in": row.get("planned_payoff_in"),
                }
                for row in alerts[:THREAD_ROWS]
            ],
        }


__all__ = ["EDGE_KIND_LABELS", "CrossChapterService", "paragraph_edge_kind"]
