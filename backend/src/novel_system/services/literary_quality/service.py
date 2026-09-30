"""文学质量视图（``/api/v1/literary-quality/*``）：按文本层取正文（作者稿 / 终稿 / 章节汇总 / 拼接）、逐条分析与三个入口。

章的正文读时现拼（重评 R13，[批准#21]）：默认层（作者稿优先）与 ``runtime`` 层拼各场当前终稿，不读存下来的章汇总
（它可能落后于逐场终稿）；显式挑「章记忆终稿」这一层时按 :func:`aggregator.derive_chapter_aggregate` 现拼这一章
归档过的各场记忆。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import AuthorDraft, ChapterGoal, FinalScene, SceneCard
from novel_system.services.aggregator import Aggregator, ChapterAggregateDerivation
from novel_system.services.errors import DomainError
from novel_system.services.literary_quality.calibration import RuleCalibration
from novel_system.services.literary_quality.calibration_source import PolicyRuleCalibrations
from novel_system.services.literary_quality.chapter_set import (
    _chapter_set_payoff_reveal_checks,
    _chapter_set_repeated_patterns,
    _chapter_set_scores,
    _reference_safety_findings,
)
from novel_system.services.literary_quality.dimensions import (
    DIMENSION_LABELS,
    QUALITY_DIMENSIONS,
    QUALITY_TEXT_LAYERS,
)
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
from novel_system.services.manuscript_html import plain_manuscript_text, visible_paragraphs
from novel_system.services.scene_text import current_author_drafts, pointed_final_scenes
from novel_system.services.style_policy import live_policies_without_contract, style_policy_live
from novel_system.services.value_coercion import optional_text


_LOGGER = logging.getLogger(__name__)


RuleCalibrationResolver = Callable[[SceneCard], "RuleCalibration | None"]
# 按场取风格策略的任务类型（与写作台深改面板同一个：scene_diagnosis.STYLE_TASK_TYPE）
_STYLE_TASK_TYPE = "scene_generation"


@dataclass(frozen=True)
class _TextRows:
    """一批章 / 场按文本层要读的正文行，几次批量查询查齐（B04-14：以前逐章逐场各查两三次，巡检一次看全书就是
    上百条查询）。取行的规则与单行查询逐条相同（``scene_text`` 的批量版本）：作者稿取最新的 current 一份；终稿取
    运行状态指着的那一份、指错了取最新一份。章记忆读时现拼（:meth:`Aggregator.derive_final_aggregates`）。"""

    chapter_drafts: Mapping[str, AuthorDraft]
    scene_drafts: Mapping[str, AuthorDraft]
    chapter_memories: Mapping[str, ChapterAggregateDerivation]
    finals: Mapping[str, FinalScene]
    chapter_scenes: Mapping[str, list[SceneCard]]  # 拼整章用：每章未删的场，按 scene_seq、scene_id


class LiteraryQualityService:
    def __init__(self, session: Session, *, rule_calibration_resolver: RuleCalibrationResolver | None = None) -> None:
        self.session = session
        # 2026-09-22 第三轮：文学质量视图与写作台深改面板、成稿门用同一份参考书校准。读数在本包的
        # calibration_source（B04-21）：没给解析器时按这一场当前的风格绑定现取；解析器参数留给测试注入
        self._rule_calibration_resolver = rule_calibration_resolver
        self._policy_calibrations = PolicyRuleCalibrations(session)
        self._scene_policies: dict[str, Any] = {}

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

        chapters = self._chapters(project_id=project_id, chapter_id=chapter_id)
        scenes = self._scenes(project_id=project_id, chapter_id=chapter_id)
        rows = self._text_rows(text_layer, chapter_ids=[chapter.chapter_id for chapter in chapters], scenes=scenes)
        items: list[dict[str, Any]] = []
        for chapter in chapters:
            source = self._chapter_source(chapter.chapter_id, text_layer=text_layer, rows=rows)
            if source is not None:
                items.append(self._analyze_item("chapter", chapter.chapter_id, chapter.chapter_id, None, source))
        scene_sources = [
            (scene, source)
            for scene in scenes
            if (source := self._scene_source(scene.scene_id, text_layer=text_layer, rows=rows)) is not None
        ]
        self._prime_scene_policies([scene for scene, _source in scene_sources])
        for scene, source in scene_sources:
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
            # 每条的指纹就在条目的 fingerprint 里（以前顶层还有一份一模一样的列表，没人读）
            "cross_scene_reuse": cross_scene_reuse,
            "recommended_next_action": _top_recommended_action(items),
            # 规则维度与中文名（筛选项读服务端这一份，前端不必再维护一张对照表，B04-31）
            "dimensions": [
                {"dimension": dimension, "label": DIMENSION_LABELS.get(dimension, dimension)}
                for dimension in QUALITY_DIMENSIONS
            ],
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

        chapter_scenes = self._chapter_scenes([chapter.chapter_id for chapter in chapters])
        rows = self._text_rows(
            text_layer,
            chapter_ids=[chapter.chapter_id for chapter in chapters],
            scenes=[scene for members in chapter_scenes.values() for scene in members],
            chapter_scenes=chapter_scenes,
        )
        chapter_items: list[dict[str, Any]] = []
        scene_items: list[dict[str, Any]] = []
        source_rows: list[dict[str, Any]] = []
        self._prime_scene_policies([scene for members in chapter_scenes.values() for scene in members])
        for chapter in chapters:
            chapter_source = self._chapter_source(chapter.chapter_id, text_layer=text_layer, rows=rows)
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
            for scene in chapter_scenes.get(chapter.chapter_id, []):
                scene_source = self._scene_source(scene.scene_id, text_layer=text_layer, rows=rows)
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
        if scene is not None:
            try:
                calibration = self._rule_calibration(scene)
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

    def _prime_scene_policies(self, scenes: list[SceneCard]) -> None:
        """逐场取校准之前，一次把这些场的风格策略解析好（``live_policies_without_contract``：查询数不随场数增长）。"""
        if self._rule_calibration_resolver is not None:
            return
        pending = [scene for scene in scenes if scene.scene_id not in self._scene_policies]
        if pending:
            policies = live_policies_without_contract(self.session, pending, task_type=_STYLE_TASK_TYPE)
            self._scene_policies.update(zip((scene.scene_id for scene in pending), policies))

    def _rule_calibration(self, scene: SceneCard) -> RuleCalibration | None:
        """这一场按绑定的参考书校准的规则维度；未绑定 / 校准不可用 → None。策略与写作台深改面板同一个解析
        （``style_policy_live``：按当前活动绑定轻量现解析，不冻结契约）。"""
        if self._rule_calibration_resolver is not None:
            return self._rule_calibration_resolver(scene)
        policy = self._scene_policies.get(scene.scene_id)
        if policy is None:
            policy = style_policy_live(self.session, scene, task_type=_STYLE_TASK_TYPE, freeze_contract=False)
            self._scene_policies[scene.scene_id] = policy
        return self._policy_calibrations.for_policy(policy)

    @staticmethod
    def _scene_ignored_keys(scene: SceneCard | None) -> list[str]:
        if scene is None:
            return []
        return [str(key) for key in (scene.deep_review_ignored_keys_json or []) if str(key)]

    def _chapters(self, *, project_id: str | None, chapter_id: str | None) -> list[ChapterGoal]:
        stmt = select(ChapterGoal).where(ChapterGoal.trashed_flag == 0)
        if project_id:
            stmt = stmt.where(ChapterGoal.project_id == project_id)
        if chapter_id:
            stmt = stmt.where(ChapterGoal.chapter_id == chapter_id)
        return self.session.execute(stmt.order_by(ChapterGoal.chapter_id.asc())).scalars().all()

    def _scenes(self, *, project_id: str | None, chapter_id: str | None) -> list[SceneCard]:
        stmt = select(SceneCard).where(SceneCard.trashed_flag == 0)
        if project_id:
            stmt = stmt.where(SceneCard.project_id == project_id)
        if chapter_id:
            stmt = stmt.where(SceneCard.chapter_id == chapter_id)
        return self.session.execute(
            stmt.order_by(SceneCard.chapter_id.asc(), SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars().all()

    def _chapter_scenes(self, chapter_ids: list[str]) -> dict[str, list[SceneCard]]:
        """每章未删的场，按 scene_seq、scene_id（拼整章与章组复审都按这个次序）。"""
        grouped: dict[str, list[SceneCard]] = {chapter_id: [] for chapter_id in chapter_ids}
        if not chapter_ids:
            return grouped
        for scene in self.session.execute(
            select(SceneCard)
            .where(SceneCard.chapter_id.in_(chapter_ids), SceneCard.trashed_flag == 0)
            .order_by(SceneCard.chapter_id.asc(), SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
        ).scalars():
            grouped.setdefault(scene.chapter_id, []).append(scene)
        return grouped

    def _text_rows(
        self,
        text_layer: str,
        *,
        chapter_ids: list[str],
        scenes: list[SceneCard],
        chapter_scenes: Mapping[str, list[SceneCard]] | None = None,
    ) -> _TextRows:
        """这一批章 / 场按 ``text_layer`` 会读到的正文行（用不到的那几种不查）。"""
        with_drafts = text_layer == "author_draft_preferred"
        with_memories = text_layer == "chapter_memory_final"
        with_assembled = text_layer in {"author_draft_preferred", "runtime", "chapter_assembled"}
        with_scene_finals = text_layer in {"author_draft_preferred", "runtime", "runtime_final_scene"}
        if not with_assembled:
            chapter_scenes = {}
        elif chapter_scenes is None:
            chapter_scenes = self._chapter_scenes(chapter_ids)
        scene_ids = [scene.scene_id for scene in scenes]
        final_ids = [
            *(scene_ids if with_scene_finals else []),
            *(scene.scene_id for members in chapter_scenes.values() for scene in members),
        ]
        return _TextRows(
            chapter_drafts=current_author_drafts(self.session, "chapter", chapter_ids) if with_drafts else {},
            scene_drafts=current_author_drafts(self.session, "scene", scene_ids) if with_drafts else {},
            chapter_memories=Aggregator(self.session).derive_final_aggregates(chapter_ids) if with_memories else {},
            finals=pointed_final_scenes(self.session, final_ids),
            chapter_scenes=chapter_scenes,
        )

    @staticmethod
    def _chapter_source(chapter_id: str, *, text_layer: str, rows: _TextRows) -> dict[str, str] | None:
        if text_layer == "author_draft_preferred":
            draft = rows.chapter_drafts.get(chapter_id)
            if draft is not None and _has_visible_text(draft.content):
                return {
                    "text_layer": "author_draft",
                    "source_ref": f"author_draft:{draft.draft_id}",
                    "content": draft.content or "",
                }
        if text_layer == "runtime_final_scene":
            return None

        if text_layer == "chapter_memory_final":
            # 章记忆读时现拼：这一章此刻归档过的各场记忆按场序（位置对不上、拼不出来的章不列）
            derivation = rows.chapter_memories.get(chapter_id)
            if derivation is None or derivation.status != "derived" or not _has_visible_text(derivation.content):
                return None
            return {
                "text_layer": "chapter_memory_final",
                "source_ref": f"chapter_memory:{chapter_id}",
                "content": derivation.content,
            }

        # 默认层与 runtime 层：各场当前终稿现拼（以前先读存下来的章汇总，它可能漏场、还是旧场序——R13）
        if text_layer in {"author_draft_preferred", "runtime", "chapter_assembled"}:
            parts: list[str] = []
            for scene in rows.chapter_scenes.get(chapter_id, []):
                final_scene = rows.finals.get(scene.scene_id)
                if final_scene is not None and _has_visible_text(final_scene.content):
                    parts.append(final_scene.content)
            assembled = "\n\n".join(parts)
            if assembled:
                return {
                    "text_layer": "chapter_assembled",
                    "source_ref": f"chapter_assembled:{chapter_id}",
                    "content": assembled,
                }
        return None

    @staticmethod
    def _scene_source(scene_id: str, *, text_layer: str, rows: _TextRows) -> dict[str, str] | None:
        if text_layer == "author_draft_preferred":
            draft = rows.scene_drafts.get(scene_id)
            if draft is not None and _has_visible_text(draft.content):
                return {
                    "text_layer": "author_draft",
                    "source_ref": f"author_draft:{draft.draft_id}",
                    "content": draft.content or "",
                }
        if text_layer not in {"author_draft_preferred", "runtime", "runtime_final_scene"}:
            return None

        final_scene = rows.finals.get(scene_id)
        if final_scene is None or not _has_visible_text(final_scene.content):
            return None
        return {
            "text_layer": "runtime_final_scene",
            "source_ref": f"final_scene:{final_scene.row_id}",
            "content": final_scene.content or "",
        }


def _has_visible_text(content: str | None) -> bool:
    """看得见的字才算正文——与写作台深改面板同一条规则（``scene_diagnosis.context.diagnosis_text``）：写作台一打开
    没有终稿的场就建一份空白作者稿（``""`` / ``<p></p>`` / ``<p><br></p>``），那是一张白纸，不是正文；拿它去分析，
    「缺什么」的规则对白纸全会响（S1 17，与复核 P02b-R1 同类）。空白的作者稿往下一层落（终稿 / 各场终稿现拼），
    都没有字就不列。"""
    return bool(visible_paragraphs(content))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in values:
            values.append(item.strip())
    return values
