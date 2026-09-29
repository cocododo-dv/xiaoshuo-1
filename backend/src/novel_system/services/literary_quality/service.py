"""文学质量视图（``/api/v1/literary-quality/*``）：按文本层取正文（作者稿 / 终稿 / 章节汇总 / 拼接）、逐条分析与三个入口。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import AuthorDraft, ChapterGoal, ChapterMemory, FinalScene, SceneCard
from novel_system.services.errors import DomainError
from novel_system.services.literary_quality.calibration import RuleCalibration
from novel_system.services.literary_quality.chapter_set import (
    _chapter_set_payoff_reveal_checks,
    _chapter_set_repeated_patterns,
    _chapter_set_scores,
    _reference_safety_findings,
)
from novel_system.services.literary_quality.dimensions import QUALITY_DIMENSIONS, QUALITY_TEXT_LAYERS
from novel_system.services.literary_quality.fingerprint import fingerprint_literary_quality
from novel_system.services.literary_quality.report import (
    _cross_scene_reuse,
    _enrich_findings,
    _filter_quality_items,
    _recommended_next_action,
    _risk_clusters,
    _span_findings,
    _top_recommended_action,
    _validate_quality_filters,
)
from novel_system.services.literary_quality.rules import analyze_literary_quality
from novel_system.services.literary_quality.scoring import automated_diagnostic_assessment, weighted_score
from novel_system.services.manuscript_html import plain_manuscript_text
from novel_system.services.scene_text import current_author_draft, final_chapter_memory, pointed_final_scene
from novel_system.services.value_coercion import optional_text


_LOGGER = logging.getLogger(__name__)


RuleCalibrationResolver = Callable[[SceneCard], "RuleCalibration | None"]


class LiteraryQualityService:
    def __init__(self, session: Session, *, rule_calibration_resolver: RuleCalibrationResolver | None = None) -> None:
        self.session = session
        # 2026-09-22 第三轮：文学质量视图与写作台深改面板用同一份参考书校准（路由层注入 scene_diagnosis 的解析器；
        # 这里不能 import scene_diagnosis——它在本模块之上）
        self._rule_calibration_resolver = rule_calibration_resolver

    def overview(
        self,
        *,
        text_layer: str = "author_draft_preferred",
        chapter_id: str | None = None,
        risk_type: str | None = None,
        min_severity: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if text_layer not in QUALITY_TEXT_LAYERS:
            raise DomainError("LITERARY_QUALITY_LAYER_INVALID", "unsupported literary quality text layer", status_code=400)
        _validate_quality_filters(risk_type=risk_type, min_severity=min_severity)

        items: list[dict[str, Any]] = []
        for chapter in self._chapters():
            if project_id and chapter.project_id != project_id:
                continue
            if chapter_id and chapter.chapter_id != chapter_id:
                continue
            source = self._chapter_source(chapter, text_layer=text_layer)
            if source is not None:
                items.append(self._analyze_item("chapter", chapter.chapter_id, chapter.chapter_id, None, source))
        for scene in self._scenes():
            if project_id and scene.project_id != project_id:
                continue
            if chapter_id and scene.chapter_id != chapter_id:
                continue
            source = self._scene_source(scene, text_layer=text_layer)
            if source is not None:
                items.append(
                    self._analyze_item(
                        "scene",
                        scene.scene_id,
                        scene.chapter_id,
                        scene.scene_id,
                        source,
                        ignored_keys=self._scene_ignored_keys(scene),
                        scene=scene,
                    )
                )

        items = _filter_quality_items(items, risk_type=risk_type, min_severity=min_severity)
        mean_score = round(sum(item["score"] for item in items) / len(items), 4) if items else None
        risk_clusters = _risk_clusters(items, risk_type=risk_type, min_severity=min_severity)
        cross_scene_reuse = _cross_scene_reuse(items)
        return {
            "filters": {
                "text_layer": text_layer,
                "chapter_id": chapter_id,
                "risk_type": risk_type,
                "min_severity": min_severity,
                "project_id": project_id,
            },
            "summary": {
                "object_count": len(items),
                "mean_score": mean_score,
                "high_risk_count": sum(1 for item in items if item["score"] < 0.72),
                "model_voice_count": sum(1 for item in items if item["signals"]["model_voice"]["risk"]),
                "risk_cluster_count": len(risk_clusters),
                "cross_scene_reuse_count": len(cross_scene_reuse),
            },
            "items": items,
            "risk_clusters": risk_clusters,
            "fingerprints": [
                {
                    "object_type": item["object_type"],
                    "object_id": item["object_id"],
                    "chapter_id": item["chapter_id"],
                    "scene_id": item["scene_id"],
                    "source_ref": item["source_ref"],
                    "fingerprint": item["fingerprint"],
                }
                for item in items
            ],
            "cross_scene_reuse": cross_scene_reuse,
            "recommended_next_action": _top_recommended_action(items),
        }

    def analyze_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DomainError("LITERARY_QUALITY_TEXT_REQUIRED", "content is required", status_code=400)
        object_type = optional_text(payload.get("object_type")) or "ad_hoc"
        object_id = optional_text(payload.get("object_id")) or "scratch"
        chapter_id = optional_text(payload.get("chapter_id")) or object_id
        scene_id = optional_text(payload.get("scene_id"))
        scene = self.session.get(SceneCard, scene_id) if scene_id else None
        item = self._analyze_item(
            object_type,
            object_id,
            chapter_id,
            scene_id,
            {
                "text_layer": "ad_hoc",
                "source_ref": optional_text(payload.get("source_ref")) or f"ad_hoc:{object_id}",
                "content": content,
            },
            ignored_keys=self._scene_ignored_keys(scene),
            scene=scene,
        )
        return {
            **item,
            "content": content,
            "span_findings": _span_findings(content, item["findings"]),
            "risk_clusters": _risk_clusters([item]),
            "recommended_next_action": item["recommended_next_action"],
        }

    def chapter_set_review(self, payload: dict[str, Any]) -> dict[str, Any]:
        chapter_ids = _string_list(payload.get("chapter_ids"))
        if not chapter_ids:
            raise DomainError("LITERARY_QUALITY_CHAPTER_SET_REQUIRED", "chapter_ids are required", status_code=400)
        protected_terms = _string_list(payload.get("protected_terms"))
        text_layer = optional_text(payload.get("text_layer")) or "author_draft_preferred"
        if text_layer not in QUALITY_TEXT_LAYERS:
            raise DomainError("LITERARY_QUALITY_LAYER_INVALID", "unsupported literary quality text layer", status_code=400)

        chapters: list[ChapterGoal] = []
        for chapter_id in chapter_ids:
            chapter = self.session.get(ChapterGoal, chapter_id)
            if chapter is None or chapter.trashed_flag:
                raise DomainError("LITERARY_QUALITY_CHAPTER_NOT_FOUND", f"chapter {chapter_id} not found", status_code=404)
            chapters.append(chapter)

        chapter_items: list[dict[str, Any]] = []
        scene_items: list[dict[str, Any]] = []
        source_rows: list[dict[str, Any]] = []
        for chapter in chapters:
            chapter_source = self._chapter_source(chapter, text_layer=text_layer)
            if chapter_source is not None:
                chapter_items.append(self._analyze_item("chapter", chapter.chapter_id, chapter.chapter_id, None, chapter_source))
                source_rows.append(
                    {
                        "object_type": "chapter",
                        "object_id": chapter.chapter_id,
                        "chapter_id": chapter.chapter_id,
                        "scene_id": None,
                        "source_ref": chapter_source["source_ref"],
                        "text_layer": chapter_source["text_layer"],
                        "content": chapter_source["content"],
                    }
                )
            for scene in self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id == chapter.chapter_id, SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
            ).scalars().all():
                scene_source = self._scene_source(scene, text_layer=text_layer)
                if scene_source is None:
                    continue
                scene_items.append(
                    self._analyze_item(
                        "scene",
                        scene.scene_id,
                        chapter.chapter_id,
                        scene.scene_id,
                        scene_source,
                        ignored_keys=self._scene_ignored_keys(scene),
                        # 与巡检、写作台深改面板同一份参考书校准（批准#13a）
                        scene=scene,
                    )
                )
                source_rows.append(
                    {
                        "object_type": "scene",
                        "object_id": scene.scene_id,
                        "chapter_id": chapter.chapter_id,
                        "scene_id": scene.scene_id,
                        "source_ref": scene_source["source_ref"],
                        "text_layer": scene_source["text_layer"],
                        "content": scene_source["content"],
                    }
                )

        all_items = [*chapter_items, *scene_items]
        mean_score = round(sum(item["score"] for item in all_items) / len(all_items), 4) if all_items else None
        payoff_reveal_checks = _chapter_set_payoff_reveal_checks(chapters, source_rows)
        safety_findings = _reference_safety_findings(source_rows, protected_terms)
        repeated_patterns = _chapter_set_repeated_patterns(source_rows, scene_items)
        scores = _chapter_set_scores(
            mean_score, payoff_reveal_checks, safety_findings, len(chapters),
            source_rows=source_rows, chapters=chapters, protected_terms=protected_terms,
        )
        return {
            "chapter_ids": chapter_ids,
            "summary": {
                "chapter_count": len(chapters),
                "scene_count": len(scene_items),
                "mean_score": mean_score,
                "high_risk_count": sum(1 for item in all_items if item["score"] < 0.72),
                "repeated_pattern_count": len(repeated_patterns),
                "reference_safety_finding_count": len(safety_findings),
            },
            "scores": scores,
            "chapters": chapter_items,
            "scenes": scene_items,
            "risk_clusters": _risk_clusters(all_items),
            "repeated_patterns": repeated_patterns,
            "payoff_reveal_checks": payoff_reveal_checks,
            "reference_safety_findings": safety_findings,
            "recommended_next_action": _top_recommended_action(all_items),
        }

    def _analyze_item(
        self,
        object_type: str,
        object_id: str,
        chapter_id: str,
        scene_id: str | None,
        source: dict[str, str],
        *,
        ignored_keys: Iterable[str] = (),
        scene: SceneCard | None = None,
    ) -> dict[str, Any]:
        # 作者稿是 HTML：按可见文字算，否则「第一句」里带着 <p>，同一条发现在写作台和这里 id 不同
        text = plain_manuscript_text(source["content"] or "")
        calibration: RuleCalibration | None = None
        if scene is not None and self._rule_calibration_resolver is not None:
            try:
                calibration = self._rule_calibration_resolver(scene)
            except Exception:  # noqa: BLE001 — 校准失败按房风词表看
                _LOGGER.debug("rule calibration unavailable for %s", scene.scene_id, exc_info=True)
                calibration = None
        signals, raw_findings = analyze_literary_quality(text, calibration=calibration)
        all_findings = _enrich_findings(
            raw_findings,
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            source_ref=source["source_ref"],
            ignored_keys=ignored_keys,
        )
        # 作者在写作台忽略过的发现不再列出（分数照算——分数是文本的事实，忽略是作者的决定）
        findings = [finding for finding in all_findings if not finding.get("ignored")]
        ignored_findings = [finding for finding in all_findings if finding.get("ignored")]
        fingerprint = fingerprint_literary_quality(text)
        raw_score = weighted_score(signals, QUALITY_DIMENSIONS)
        automated_assessment = automated_diagnostic_assessment(
            text,
            raw_diagnostic_score=raw_score,
        )
        return {
            "object_type": object_type,
            "object_id": object_id,
            "chapter_id": chapter_id,
            "scene_id": scene_id,
            "text_layer": source["text_layer"],
            "source_ref": source["source_ref"],
            "score": automated_assessment["score"],
            "automated_assessment": automated_assessment,
            "signals": signals,
            "findings": findings,
            "ignored_findings": [
                {"signal_id": finding["signal_id"], "dimension": finding["dimension"], "label": finding["label"]}
                for finding in ignored_findings
            ],
            "ignored_count": len(ignored_findings),
            "open_dimensions": list(dict.fromkeys(str(finding.get("dimension") or "") for finding in findings)),
            "rule_calibration": calibration.as_dict() if calibration is not None and calibration.active else None,
            "fingerprint": fingerprint,
            "recommended_next_action": _recommended_next_action(findings, signals),
        }

    @staticmethod
    def _scene_ignored_keys(scene: SceneCard | None) -> list[str]:
        if scene is None:
            return []
        return [str(key) for key in (scene.deep_review_ignored_keys_json or []) if str(key)]

    def _chapters(self) -> list[ChapterGoal]:
        return self.session.execute(
            select(ChapterGoal)
            .where(ChapterGoal.trashed_flag == 0)
            .order_by(ChapterGoal.chapter_id.asc())
        ).scalars().all()

    def _scenes(self) -> list[SceneCard]:
        return self.session.execute(
            select(SceneCard)
            .where(SceneCard.trashed_flag == 0)
            .order_by(SceneCard.chapter_id.asc(), SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()

    def _chapter_source(self, chapter: ChapterGoal, *, text_layer: str) -> dict[str, str] | None:
        if text_layer == "author_draft_preferred":
            draft = self._current_author_draft("chapter", chapter.chapter_id)
            if draft is not None:
                return {
                    "text_layer": "author_draft",
                    "source_ref": f"author_draft:{draft.draft_id}",
                    "content": draft.content or "",
                }
        if text_layer == "runtime_final_scene":
            return None

        if text_layer in {"author_draft_preferred", "runtime", "chapter_memory_final"}:
            memory = self._final_chapter_memory(chapter.chapter_id)
            if memory is not None:
                return {
                    "text_layer": "chapter_memory_final",
                    "source_ref": f"chapter_memory:{memory.row_id}",
                    "content": memory.content or "",
                }
            if text_layer == "chapter_memory_final":
                return None

        if text_layer in {"author_draft_preferred", "runtime", "chapter_assembled"}:
            assembled = self._assembled_chapter_text(chapter.chapter_id)
            if assembled:
                return {
                    "text_layer": "chapter_assembled",
                    "source_ref": f"chapter_assembled:{chapter.chapter_id}",
                    "content": assembled,
                }
        return None

    def _scene_source(self, scene: SceneCard, *, text_layer: str) -> dict[str, str] | None:
        if text_layer == "author_draft_preferred":
            draft = self._current_author_draft("scene", scene.scene_id)
            if draft is not None:
                return {
                    "text_layer": "author_draft",
                    "source_ref": f"author_draft:{draft.draft_id}",
                    "content": draft.content or "",
                }
        if text_layer not in {"author_draft_preferred", "runtime", "runtime_final_scene"}:
            return None

        final_scene = self._final_scene(scene.scene_id)
        if final_scene is None:
            return None
        return {
            "text_layer": "runtime_final_scene",
            "source_ref": f"final_scene:{final_scene.row_id}",
            "content": final_scene.content or "",
        }

    def _current_author_draft(self, object_type: str, object_id: str) -> AuthorDraft | None:
        return current_author_draft(self.session, object_type, object_id)

    def _final_scene(self, scene_id: str) -> FinalScene | None:
        return pointed_final_scene(self.session, scene_id)

    def _final_chapter_memory(self, chapter_id: str) -> ChapterMemory | None:
        return final_chapter_memory(self.session, chapter_id)

    def _assembled_chapter_text(self, chapter_id: str) -> str:
        parts: list[str] = []
        scenes = self.session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
            .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()
        for scene in scenes:
            final_scene = self._final_scene(scene.scene_id)
            if final_scene is not None and final_scene.content:
                parts.append(final_scene.content)
        return "\n\n".join(parts)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in values:
            values.append(item.strip())
    return values
