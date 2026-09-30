"""一次请求里一组场的诊断原料（审计 B05-03 / X01-12）：批量取好，逐场只读内存。

以前每诊断一场要查六七次库（当前作者稿、运行终稿、准定稿评审行、AI 深评行、局部深评行、章级通读行、各镜头行、
评审看的冻结正文），全书计数 17 场两百多条语句；取场景载荷时还把这一场诊断两遍（先单独一遍，再在整章计数里
一遍）。这里对一组场（一场、一章或整本书）一次取齐：

* 正文：当前作者稿与运行终稿各一次批量查询（``scene_text`` 的批量版本，取行的规则与单行查询逐条相同）；
* 评审行：这组场与它们的章在两种评审口径下各自最新的一行（没被退位的顶层行），一次查询；
* 局部深评行（没被退位的，按时间）与 AI 深评的各镜头行各一次；
* 评审看的冻结正文（``final_scene:`` / ``source_draft:`` 引用）各一次。

取出来的行都是会话里的对象；载荷只读它们。上下文不跨写入复用：写了新评审行之后另取一份。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import FinalScene, SceneCard, SceneDraft, WriterEvaluation
from novel_system.services.manuscript_html import manuscript_paragraphs, plain_manuscript_text
from novel_system.services.scene_diagnosis.text import DiagnosisText
from novel_system.services.scene_diagnosis.vocabulary import (
    LITERARY_REVISION_PASSAGE_RUBRIC_ID,
    LITERARY_REVISION_RUBRIC_ID,
    NEAR_FINAL_RUBRIC_ID,
)
from novel_system.services.scene_text import current_author_drafts, pointed_final_scenes
from novel_system.services.style_reference.text_utils import compact_ws

# 诊断读的两种顶层评审口径（场：准定稿评审 + AI 深评；章：AI 通读）
_TOP_LEVEL_RUBRICS = (NEAR_FINAL_RUBRIC_ID, LITERARY_REVISION_RUBRIC_ID)
_MISSING = object()


def diagnosis_text(draft: Any | None, final: FinalScene | None) -> DiagnosisText:
    """诊断的正文 = 写作台看到的那份：当前作者稿，其次运行终稿（有字的），否则没有正文。

    「有字」按可见文字算，不按存的内容：写作台一打开没有终稿的场就建一份空白作者稿（``""``，编辑器存回来是
    ``<p></p>`` / ``<p><br></p>``），写作台上是一张白纸——这也是没有正文（``layer="none"``）：规则不诊断它（缺什么
    的规则对白纸全会响），深评 / 局部深评 / 章级通读都不拿它去调模型（复核 P02b-R1），前端照 ``none`` 说「这一场还
    没有正文」。作者稿在就以作者稿为准，不退回终稿——写作台上看到的就是这张白纸。"""

    if draft is not None:
        content = draft.content or ""
        paragraphs = manuscript_paragraphs(content)
        if not _has_visible_text(paragraphs):
            return _no_text()
        return DiagnosisText(
            layer="author_draft",
            ref=f"author_draft:{draft.draft_id}",
            content=content,
            paragraphs=paragraphs,
            updated_at=draft.updated_at,
        )
    if final is not None:
        content = final.content or ""
        paragraphs = manuscript_paragraphs(content)
        if _has_visible_text(paragraphs):
            return DiagnosisText(
                layer="runtime_final_scene",
                ref=f"final_scene:{final.row_id}",
                content=content,
                paragraphs=paragraphs,
                updated_at=final.created_at,
            )
    return _no_text()


def _has_visible_text(paragraphs: list[str]) -> bool:
    return any(paragraph.strip() for paragraph in paragraphs)


def _no_text() -> DiagnosisText:
    return DiagnosisText(layer="none", ref=None, content="", paragraphs=[], updated_at=None)


def _frozen_ref(ref: str | None) -> tuple[str, str] | None:
    value = str(ref or "")
    for prefix in ("final_scene:", "source_draft:"):
        if value.startswith(prefix):
            return prefix, value.split(":", 1)[1]
    return None


@dataclass
class DiagnosisContext:
    texts: dict[str, DiagnosisText] = field(default_factory=dict)
    latest: dict[tuple[str, str, str], WriterEvaluation] = field(default_factory=dict)
    passages: dict[str, list[WriterEvaluation]] = field(default_factory=dict)
    lenses: dict[str, list[WriterEvaluation]] = field(default_factory=dict)
    frozen: dict[str, Any] = field(default_factory=dict)
    _reviewed: dict[str, str | None] = field(default_factory=dict)

    @classmethod
    def load(cls, session: Session, scenes: Sequence[SceneCard], *, chapter_ids: Iterable[str | None] = ()) -> "DiagnosisContext":
        scene_ids = list(dict.fromkeys(scene.scene_id for scene in scenes))
        chapters = list(dict.fromkeys(chapter_id for chapter_id in chapter_ids if chapter_id))
        context = cls()
        if scene_ids:
            drafts = current_author_drafts(session, "scene", scene_ids)
            finals = pointed_final_scenes(session, [scene_id for scene_id in scene_ids if scene_id not in drafts])
            context.texts = {scene_id: diagnosis_text(drafts.get(scene_id), finals.get(scene_id)) for scene_id in scene_ids}
        objects = scene_ids + chapters
        if objects:
            for row in session.execute(
                select(WriterEvaluation)
                .where(
                    WriterEvaluation.object_type.in_(("scene", "chapter")),
                    WriterEvaluation.object_id.in_(objects),
                    WriterEvaluation.rubric_id.in_(_TOP_LEVEL_RUBRICS),
                    WriterEvaluation.parent_evaluation_id.is_(None),
                    WriterEvaluation.status != "superseded",
                )
                .order_by(
                    WriterEvaluation.object_type,
                    WriterEvaluation.object_id,
                    WriterEvaluation.rubric_id,
                    WriterEvaluation.created_at.desc(),
                    WriterEvaluation.evaluation_id.desc(),
                )
            ).scalars():
                context.latest.setdefault((row.object_type, row.object_id, row.rubric_id), row)
        if scene_ids:
            for row in session.execute(
                select(WriterEvaluation)
                .where(
                    WriterEvaluation.object_type == "scene",
                    WriterEvaluation.object_id.in_(scene_ids),
                    WriterEvaluation.rubric_id == LITERARY_REVISION_PASSAGE_RUBRIC_ID,
                    WriterEvaluation.status != "superseded",
                )
                .order_by(WriterEvaluation.created_at.asc(), WriterEvaluation.evaluation_id.asc())
            ).scalars():
                context.passages.setdefault(row.object_id, []).append(row)
        parents = [
            row.evaluation_id
            for (object_type, _object_id, rubric_id), row in context.latest.items()
            if object_type == "scene" and rubric_id == LITERARY_REVISION_RUBRIC_ID
        ]
        if parents:
            for row in session.execute(
                select(WriterEvaluation)
                .where(WriterEvaluation.parent_evaluation_id.in_(parents))
                .order_by(WriterEvaluation.lens.asc(), WriterEvaluation.evaluation_id.asc())
            ).scalars():
                context.lenses.setdefault(str(row.parent_evaluation_id), []).append(row)
        context._load_frozen(session)
        return context

    def _load_frozen(self, session: Session) -> None:
        """评审记的冻结正文（``final_scene:`` / ``source_draft:``）：评审是不是对着现在这份字，按它判。"""

        rows = [*self.latest.values(), *(row for rows in self.passages.values() for row in rows)]
        wanted: dict[str, set[str]] = {"final_scene:": set(), "source_draft:": set()}
        for row in rows:
            parsed = _frozen_ref(row.source_text_ref)
            if parsed is not None:
                wanted[parsed[0]].add(parsed[1])
        if wanted["final_scene:"]:
            for final in session.execute(select(FinalScene).where(FinalScene.row_id.in_(wanted["final_scene:"]))).scalars():
                self.frozen[f"final_scene:{final.row_id}"] = final
        if wanted["source_draft:"]:
            for draft in session.execute(select(SceneDraft).where(SceneDraft.row_id.in_(wanted["source_draft:"]))).scalars():
                self.frozen[f"source_draft:{draft.row_id}"] = draft

    # -- 读 ----------------------------------------------------------------

    def text(self, scene: SceneCard) -> DiagnosisText:
        return self.texts[scene.scene_id]

    def latest_evaluation(self, object_id: str, rubric_id: str, *, object_type: str = "scene") -> WriterEvaluation | None:
        return self.latest.get((object_type, object_id, rubric_id))

    def passage_rows(self, scene_id: str) -> list[WriterEvaluation]:
        return list(self.passages.get(scene_id, []))

    def lens_rows(self, parent_id: str) -> list[WriterEvaluation]:
        return list(self.lenses.get(parent_id, []))

    def reviewed_text(self, ref: str | None) -> str | None:
        """评审看的冻结正文（压缩空白后的可见文字）；引用不是冻结正文、或那一行已经没了 → None。"""

        key = str(ref or "")
        cached = self._reviewed.get(key, _MISSING)
        if cached is not _MISSING:
            return cached  # type: ignore[return-value]
        frozen = self.frozen.get(key)
        value = None
        if frozen is not None:
            value = compact_ws(plain_manuscript_text(frozen.content))
        self._reviewed[key] = value
        return value


__all__ = ["DiagnosisContext", "diagnosis_text"]
