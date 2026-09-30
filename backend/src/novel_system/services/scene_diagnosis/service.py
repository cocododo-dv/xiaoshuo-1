"""``SceneDiagnosisService``：一场 / 一章 / 一本书的诊断载荷与随写回传的计数（见包说明）。"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import ChapterGoal, SceneCard, WriterEvaluation
from novel_system.services.literary_quality import DEFAULT_RULE_CALIBRATION, RuleCalibration
from novel_system.services.literary_quality.calibration_source import (
    BoundProfile,
    bound_profile_for_policy,
    rule_calibration_for_book,
)
from novel_system.services.scene_diagnosis.calibration import (
    DEFAULT_CRAFT_CALIBRATION,
    CraftCalibration,
    craft_calibration_for,
)
from novel_system.services.scene_diagnosis.context import DiagnosisContext, diagnosis_text
from novel_system.services.scene_diagnosis.findings import (
    cached_text_findings,
    evaluation_findings,
    finding_counts,
    finding_sort_key,
)
from novel_system.services.scene_diagnosis.serialize import serialize_passage_review
from novel_system.services.scene_diagnosis.text import DiagnosisText
from novel_system.services.scene_diagnosis.vocabulary import (
    AI_DIMENSION_LABELS,
    LENS_LABELS,
    LITERARY_REVISION_PASSAGE_RUBRIC_ID,
    LITERARY_REVISION_RUBRIC_ID,
    NEAR_FINAL_RUBRIC_ID,
    REVIEW_DIMENSION_LABELS,
)
from novel_system.services.scene_lookup import active_chapter_scenes, require_chapter, require_project, require_scene
from novel_system.services.scene_text import current_author_draft, pointed_final_scene
from novel_system.services.style_policy import StylePolicy, style_policy_live

_LOGGER = logging.getLogger(__name__)
STYLE_TASK_TYPE = "scene_generation"


class SceneDiagnosisService:
    def __init__(self, session: Session) -> None:
        self.session = session
        # 风格参考 v3：每场一份 StylePolicy（轻量现解析，不冻结契约），一次请求内记住
        self._policy_memo: dict[str, StylePolicy] = {}
        # 每份绑定的画像一份校准（书的版本只查一次库，而不是逐场一次；X01-03 / X01-09）
        self._calibration_memo: dict[tuple[str, str | None, bool], CraftCalibration] = {}
        self._rule_memo: dict[tuple[str, str | None, bool], RuleCalibration] = {}
        self._profile_memo: dict[tuple[str, str | None], BoundProfile | None] = {}

    # -- 正文 --------------------------------------------------------------

    def text_for_scene(self, scene: SceneCard) -> DiagnosisText:
        """诊断的正文 = 写作台看到的那份：当前作者稿，其次运行终稿，否则没有正文（一场一查；一组场见
        :class:`~novel_system.services.scene_diagnosis.context.DiagnosisContext`）。"""

        draft = current_author_draft(self.session, "scene", scene.scene_id)
        final = pointed_final_scene(self.session, scene.scene_id) if draft is None else None
        return diagnosis_text(draft, final)

    def context_for(self, scenes: list[SceneCard]) -> DiagnosisContext:
        """一组场（与它们所在的章）的诊断原料，一次取齐。"""

        return DiagnosisContext.load(self.session, scenes, chapter_ids=[scene.chapter_id for scene in scenes])

    def chapter_scenes(self, chapter_id: str) -> list[SceneCard]:
        return active_chapter_scenes(self.session, chapter_id)

    # -- 评审行 ------------------------------------------------------------

    def latest_evaluation(self, object_id: str, rubric_id: str, *, object_type: str = "scene") -> WriterEvaluation | None:
        return self.session.execute(
            select(WriterEvaluation)
            .where(
                WriterEvaluation.object_type == object_type,
                WriterEvaluation.object_id == object_id,
                WriterEvaluation.rubric_id == rubric_id,
                WriterEvaluation.parent_evaluation_id.is_(None),
                WriterEvaluation.status != "superseded",
            )
            .order_by(WriterEvaluation.created_at.desc(), WriterEvaluation.evaluation_id.desc())
        ).scalars().first()

    def passage_rows(self, scene_id: str) -> list[WriterEvaluation]:
        """这一场还有效的局部深评（同一段再看一次时旧的退位，见 writer_deep_review.run_passage_review）。"""

        return list(
            self.session.execute(
                select(WriterEvaluation)
                .where(
                    WriterEvaluation.object_type == "scene",
                    WriterEvaluation.object_id == scene_id,
                    WriterEvaluation.rubric_id == LITERARY_REVISION_PASSAGE_RUBRIC_ID,
                    WriterEvaluation.status != "superseded",
                )
                .order_by(WriterEvaluation.created_at.asc(), WriterEvaluation.evaluation_id.asc())
            ).scalars().all()
        )

    def _evaluation_status(self, row: WriterEvaluation | None, text: DiagnosisText, context: DiagnosisContext) -> str:
        """``not_run`` / ``current`` / ``stale``：评审看的还是不是现在这份正文。

        评审记的是 ``source_text_ref``：``final_scene:<row>`` / ``source_draft:<row>`` 的内容不会变，
        直接比正文是否相同（作者稿常是从终稿复制出来的，同一份字就是 current）；``author_draft:<id>``
        的行会被原地改写，只能看时间——空保存不改时间戳（AuthorDraftService.save 内容未变即返回），
        所以「草稿的 updated_at 晚于评审的 created_at」就是「改过了」。
        """

        if row is None:
            return "not_run"
        if text.layer == "none":
            return "stale"
        ref = str(row.source_text_ref or "")
        if ref.startswith(("final_scene:", "source_draft:")):
            reviewed = context.reviewed_text(ref)
            return "current" if reviewed is not None and reviewed == text.compact() else "stale"
        if ref.startswith("author_draft:"):
            if ref != str(text.ref or ""):
                return "stale"
            if text.updated_at and row.created_at and str(text.updated_at) > str(row.created_at):
                return "stale"
            return "current"
        return "stale"

    def _chapter_evaluation_status(
        self,
        row: WriterEvaluation | None,
        texts: list[DiagnosisText],
        scene_ids: list[str] | None = None,
    ) -> str:
        """章级深评看的是各场拼起来的字。通读行记了每场正文的哈希（``contract_field_refs_json.scenes``）时按
        哈希判：哪一场的字变了、多了一场有字的、少了一场，整份就是改前的；老的行没有哈希，退回时间戳
        （任何一场的作者稿在它之后改过）。"""

        if row is None:
            return "not_run"
        if not texts or all(text.layer == "none" for text in texts):
            return "stale"
        if scene_ids is not None:
            changes = self.chapter_review_changes(row, scene_ids, texts)
            if changes is not None:
                return "stale" if changes["changed_scene_ids"] or changes["removed_scene_ids"] else "current"
        for text in texts:
            if text.layer == "author_draft" and text.updated_at and row.created_at and str(text.updated_at) > str(row.created_at):
                return "stale"
        return "current"

    @staticmethod
    def chapter_review_changes(
        row: WriterEvaluation | None,
        scene_ids: list[str],
        texts: list[DiagnosisText],
    ) -> dict[str, Any] | None:
        """上一次通读之后哪些场的字变了。通读行没记哈希（老的行）→ None。"""

        meta = row.contract_field_refs_json if row is not None and isinstance(row.contract_field_refs_json, dict) else {}
        recorded = meta.get("scenes")
        if not isinstance(recorded, list):
            return None
        recorded_sha = {
            str(item.get("scene_id")): str(item.get("sha256") or "")
            for item in recorded
            if isinstance(item, dict) and item.get("scene_id")
        }
        changed: list[str] = []
        unchanged: list[str] = []
        for scene_id, text in zip(scene_ids, texts):
            current_sha = text.sha256 if text.layer != "none" else ""
            if scene_id not in recorded_sha:
                (changed if current_sha else unchanged).append(scene_id)
            elif recorded_sha[scene_id] != current_sha:
                changed.append(scene_id)
            else:
                unchanged.append(scene_id)
        removed = [scene_id for scene_id in recorded_sha if scene_id not in set(scene_ids) and recorded_sha[scene_id]]
        return {"changed_scene_ids": changed, "unchanged_scene_ids": unchanged, "removed_scene_ids": removed}

    # -- 风格绑定与校准 ----------------------------------------------------

    def style_policy(self, scene: SceneCard) -> StylePolicy:
        """这一场的风格策略（风格参考 v3）：诊断没有 bundle，按当前活动绑定轻量现解析（scene > character >
        project > global，同层取最新；画像不 active 的绑定不算），不冻结契约、不加载 profile_json。

        ``bound``：按参考书校准节奏检查与规则维度；``defers_house_taste()``（绑定且作者手笔直起）：规则 /
        节奏发现标 ``house_taste``——与成稿门、起草管线同一个判定（此前这里把 neutral_first 的绑定也当让位）。
        """

        memo = self._policy_memo.get(scene.scene_id)
        if memo is None:
            memo = style_policy_live(self.session, scene, task_type=STYLE_TASK_TYPE, freeze_contract=False)
            self._policy_memo[scene.scene_id] = memo
        return memo

    def bound_profile_for_policy(self, policy: StylePolicy) -> BoundProfile | None:
        """策略绑定的画像（校准要用的三样）；未绑定 → None。书以策略为准（冻结契约记下的那本）。同一个实例里
        同一份画像只查一次库。"""

        if not policy.bound or not policy.profile_id:
            return None
        key = (str(policy.profile_id), policy.book_id)
        if key not in self._profile_memo:
            self._profile_memo[key] = bound_profile_for_policy(self.session, policy)
        return self._profile_memo[key]

    def binding_profile(self, scene: SceneCard) -> tuple[bool, BoundProfile | None]:
        """这一场有没有风格绑定，以及最具体那一层的画像（见 :meth:`style_policy`）。"""

        profile = self.bound_profile_for_policy(self.style_policy(scene))
        return (True, profile) if profile is not None else (False, None)

    def style_bound(self, scene: SceneCard) -> bool:
        return self.style_policy(scene).bound

    def scene_calibration(self, scene: SceneCard) -> tuple[bool, CraftCalibration]:
        """这一场的（绑定与否，校准）：有绑定按参考书，没有就是房风默认。"""

        style_bound, profile = self.binding_profile(scene)
        return style_bound, (self.craft_calibration(profile) if style_bound else DEFAULT_CRAFT_CALIBRATION)

    def rule_calibration_for_scene(self, scene: SceneCard) -> RuleCalibration | None:
        """文学质量视图用的解析器（路由层注入 LiteraryQualityService）：有绑定给参考书的规则校准，否则 None。"""

        return self.rule_calibration_for_policy(self.style_policy(scene))

    def rule_calibration_for_policy(self, policy: StylePolicy) -> RuleCalibration | None:
        """成稿门用的解析器（风格参考 v3 V11）：按策略绑定的书校准的规则维度；未绑定 / 校准不可用 → None。
        只算规则那一半（成稿门与文学质量视图用不到节奏读数）。"""

        profile = self.bound_profile_for_policy(policy)
        if profile is None:
            return None
        rules = self._rule_calibration(profile)
        return rules if rules.active else None

    def _rule_calibration(self, profile: BoundProfile) -> RuleCalibration:
        key = (profile.profile_id, profile.book_id, bool(profile.deliberate_repetition))
        craft = self._calibration_memo.get(key)
        if craft is not None:
            return craft.rules
        rules = self._rule_memo.get(key)
        if rules is None:
            try:
                rules = rule_calibration_for_book(
                    self.session, profile.book_id, deliberate_repetition=bool(profile.deliberate_repetition)
                )
            except Exception:  # noqa: BLE001 — 校准读不出：按未校准处理，不让调用方失败
                _LOGGER.warning("rule calibration unavailable for book %s", profile.book_id, exc_info=True)
                rules = DEFAULT_RULE_CALIBRATION
            self._rule_memo[key] = rules
        return rules

    def craft_calibration(self, profile: BoundProfile | None) -> CraftCalibration:
        """按绑定画像的参考书校准节奏检查与规则维度（``calibration.craft_calibration_for``：读数按书的版本缓存在
        进程里）；同一个实例里同一份画像只算一次。"""

        if profile is None or not profile.book_id:
            return DEFAULT_CRAFT_CALIBRATION
        key = (profile.profile_id, profile.book_id, bool(profile.deliberate_repetition))
        calibration = self._calibration_memo.get(key)
        if calibration is None:
            calibration = craft_calibration_for(self.session, profile)
            self._calibration_memo[key] = calibration
        return calibration

    # -- 一场的诊断 --------------------------------------------------------

    def diagnose_scene(
        self,
        scene: SceneCard,
        *,
        chapter_row: WriterEvaluation | None = None,
        text: DiagnosisText | None = None,
        context: DiagnosisContext | None = None,
    ) -> dict[str, Any]:
        context = context if context is not None else self.context_for([scene])
        text = text if text is not None else context.text(scene)
        style_bound, calibration = self.scene_calibration(scene)
        # 规则 / 节奏发现是否标房风：只在「让位」时（绑定且作者手笔直起）——与成稿门同一个判定
        house_taste = self.style_policy(scene).defers_house_taste()
        ignored = {str(key) for key in (scene.deep_review_ignored_keys_json or []) if str(key)}

        findings: list[dict[str, Any]] = []
        waived: list[dict[str, Any]] = []
        if text.layer != "none":
            text_findings, waived = cached_text_findings(scene.scene_id, text, calibration=calibration, house_taste=house_taste)
            findings.extend(text_findings)

        review_row = context.latest_evaluation(scene.scene_id, NEAR_FINAL_RUBRIC_ID)
        ai_row = context.latest_evaluation(scene.scene_id, LITERARY_REVISION_RUBRIC_ID)
        passage_rows = context.passage_rows(scene.scene_id)
        if chapter_row is None and scene.chapter_id:
            chapter_row = context.latest_evaluation(scene.chapter_id, LITERARY_REVISION_RUBRIC_ID, object_type="chapter")
        if text.layer != "none":
            if review_row is not None:
                findings.extend(evaluation_findings(review_row, text, source="review", label_for=REVIEW_DIMENSION_LABELS))
            if ai_row is not None:
                findings.extend(evaluation_findings(ai_row, text, source="ai", label_for=AI_DIMENSION_LABELS))
            for row in passage_rows:
                findings.extend(evaluation_findings(row, text, source="ai", label_for=AI_DIMENSION_LABELS, origin_kind="passage"))
            if chapter_row is not None:
                # 章级通读的发现只有钉得到这一场的才属于这一场；钉不到的留在成稿中心的章级清单里
                findings.extend(
                    item
                    for item in evaluation_findings(chapter_row, text, source="ai", label_for=AI_DIMENSION_LABELS, origin_kind="chapter")
                    if item.get("evidence") is not None
                )

        opinions: dict[str, dict[str, Any]] = {}
        for row in passage_rows:
            entry = serialize_passage_review(row, self._evaluation_status(row, text, context))
            for about_id in entry["about_signal_ids"]:
                opinions[str(about_id)] = entry

        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for finding in findings:
            if finding["signal_id"] in seen:
                continue
            seen.add(finding["signal_id"])
            finding["ignored"] = finding["signal_id"] in ignored
            finding["opinion"] = opinions.get(finding["signal_id"])
            deduped.append(finding)
        deduped.sort(key=finding_sort_key)

        ai_lenses = context.lens_rows(ai_row.evaluation_id) if ai_row is not None else []

        return {
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "project_id": getattr(scene, "project_id", None),
            "text": {
                "layer": text.layer,
                "ref": text.ref,
                "sha256": text.sha256 if text.layer != "none" else None,
                "paragraph_count": len(text.paragraphs),
                "chars": text.chars,
            },
            "style_bound": style_bound,
            # 这一稿里按参考作者的密度放过的词表词（词、次数、作者每万字次数、一场的量里的期望、这个次数的概率）
            "craft_calibration": {**calibration.as_dict(), "waived_in_scene": waived},
            "findings": deduped,
            "summary": finding_counts(deduped),
            "ai": {
                "status": self._evaluation_status(ai_row, text, context),
                "evaluation_id": ai_row.evaluation_id if ai_row is not None else None,
                "overall_score": ai_row.overall_score if ai_row is not None else None,
                "revision_brief": list(ai_row.revision_brief_json or []) if ai_row is not None else [],
                "lenses": [
                    {"lens": row.lens, "label": LENS_LABELS.get(str(row.lens or ""), str(row.lens or "")), "overall_score": row.overall_score}
                    for row in ai_lenses
                ],
                "llm_call_id": ai_row.evaluator_llm_call_id if ai_row is not None else None,
                "created_at": ai_row.created_at if ai_row is not None else None,
                "source_text_ref": ai_row.source_text_ref if ai_row is not None else None,
            },
            "review": {
                "status": self._evaluation_status(review_row, text, context),
                "evaluation_id": review_row.evaluation_id if review_row is not None else None,
                "overall_score": review_row.overall_score if review_row is not None else None,
                "failure_class": review_row.failure_class if review_row is not None else None,
                "revision_brief": list(review_row.revision_brief_json or []) if review_row is not None else [],
                "created_at": review_row.created_at if review_row is not None else None,
                "source_text_ref": review_row.source_text_ref if review_row is not None else None,
            },
            "preferences": {
                "revision_no": int(scene.deep_review_preferences_revision_no or 0),
                "decision_log": list(scene.deep_review_decision_log_json or []),
                "ignored_issue_keys": sorted(ignored),
            },
        }

    def payload(self, scene_id: str) -> dict[str, Any]:
        scene = require_scene(self.session, scene_id, trashed_as_conflict=True)
        return self.scene_payload(scene)

    def scene_payload(self, scene: SceneCard) -> dict[str, Any]:
        """写作台深改面板的载荷：这一场的诊断 + 它所在那一章的计数。整章一起诊断一遍，这一场的载荷就从里面取
        （以前先单独诊断一遍、算计数时又在整章里诊断一遍）。"""

        diagnosis, rollup = self._diagnosis_and_rollup(scene)
        diagnosis["diagnosis_rollup"] = rollup
        return diagnosis

    def _diagnosis_and_rollup(self, scene: SceneCard) -> tuple[dict[str, Any], dict[str, Any]]:
        chapter = self.session.get(ChapterGoal, scene.chapter_id) if scene.chapter_id else None
        if chapter is not None:
            block = self._chapter_block(chapter)
            diagnosis = block["diagnoses"].get(scene.scene_id)
            if diagnosis is not None:
                return diagnosis, _rollup_from_block(block)
        diagnosis = self.diagnose_scene(scene)
        if chapter is not None:
            return diagnosis, _rollup_from_block(block)
        return diagnosis, {
            "project_id": getattr(scene, "project_id", None),
            "chapter_id": scene.chapter_id,
            "chapters": {},
            "scenes": {scene.scene_id: _scene_counts_entry(scene.chapter_id, diagnosis)},
        }

    # -- 一章的诊断（成稿中心「AI 通读本章」）-------------------------------

    def _chapter_block(
        self,
        chapter: ChapterGoal,
        *,
        scenes: list[SceneCard] | None = None,
        context: DiagnosisContext | None = None,
    ) -> dict[str, Any]:
        """一章的全部读数（一次算完，chapter_payload / project_summary / rollup / 场景载荷共用）：每场的诊断、落到
        各场的通读发现、钉不到任何一场的章级发现、章级通读的新旧与改过的场。原料一次取齐（``context``：整本书
        计数时各章共用一份）。"""

        scenes = scenes if scenes is not None else self.chapter_scenes(chapter.chapter_id)
        if context is None:
            context = DiagnosisContext.load(self.session, scenes, chapter_ids=[chapter.chapter_id])
        chapter_row = context.latest_evaluation(chapter.chapter_id, LITERARY_REVISION_RUBRIC_ID, object_type="chapter")
        scene_entries: list[dict[str, Any]] = []
        diagnoses: dict[str, dict[str, Any]] = {}
        texts: list[DiagnosisText] = []
        located_ids: set[str] = set()
        for scene in scenes:
            text = context.text(scene)
            texts.append(text)
            diagnosis = self.diagnose_scene(scene, chapter_row=chapter_row, text=text, context=context)
            diagnoses[scene.scene_id] = diagnosis
            from_chapter = [item for item in diagnosis["findings"] if (item.get("origin") or {}).get("kind") == "chapter"]
            located_ids.update(item["signal_id"] for item in from_chapter)
            scene_entries.append(
                {
                    "scene_id": scene.scene_id,
                    "scene_seq": scene.scene_seq,
                    "title": getattr(scene, "title", None) or "",
                    "text_layer": diagnosis["text"]["layer"],
                    "summary": diagnosis["summary"],
                    "ai_status": diagnosis["ai"]["status"],
                    "review_status": diagnosis["review"]["status"],
                    "findings_from_chapter": from_chapter,
                    "carried": any((item.get("origin") or {}).get("carried_from") for item in from_chapter),
                    "counts": _scene_counts_entry(chapter.chapter_id, diagnosis),
                }
            )

        chapter_findings: list[dict[str, Any]] = []
        if chapter_row is not None:
            empty = DiagnosisText(layer="chapter", ref=chapter_row.source_text_ref, content="", paragraphs=[])
            for item in evaluation_findings(chapter_row, empty, source="ai", label_for=AI_DIMENSION_LABELS, origin_kind="chapter"):
                if item["signal_id"] in located_ids:
                    continue
                # 钉不到任何一场：章级判断（承诺 / 升级 / 兑现），或引的那句已经改掉
                item["stale"] = bool(item.get("context"))
                chapter_findings.append(item)
            chapter_findings.sort(key=finding_sort_key)

        scene_ids = [scene.scene_id for scene in scenes]
        changes = self.chapter_review_changes(chapter_row, scene_ids, texts) if chapter_row is not None else None
        ai_status = self._chapter_evaluation_status(chapter_row, texts, scene_ids) if chapter_row is not None else "not_run"
        changed_ids = list(changes["changed_scene_ids"]) if changes else (
            [scene.scene_id for scene, text in zip(scenes, texts) if text.layer != "none"] if ai_status == "stale" else []
        )
        for entry in scene_entries:
            entry["changed_since_review"] = entry["scene_id"] in set(changed_ids)
        meta = chapter_row.contract_field_refs_json if chapter_row is not None and isinstance(chapter_row.contract_field_refs_json, dict) else {}
        chapter_level_blocking = sum(1 for item in chapter_findings if item["severity"] == "blocking")
        counts = {
            "open": sum(entry["summary"]["open"] for entry in scene_entries) + len(chapter_findings),
            "blocking": sum(entry["summary"]["by_severity"]["blocking"] for entry in scene_entries) + chapter_level_blocking,
            "chapter_level": len(chapter_findings),
            "chapter_level_blocking": chapter_level_blocking,
            "scenes": len(scene_entries),
            "scenes_with_findings": sum(1 for entry in scene_entries if entry["summary"]["open"]),
            "ai_status": ai_status,
        }
        return {
            "chapter": chapter,
            "row": chapter_row,
            "scenes": scenes,
            "texts": texts,
            "diagnoses": diagnoses,
            "scene_entries": scene_entries,
            "chapter_findings": chapter_findings,
            "counts": counts,
            "ai": {
                "status": ai_status,
                "evaluation_id": chapter_row.evaluation_id if chapter_row is not None else None,
                "overall_score": chapter_row.overall_score if chapter_row is not None else None,
                "revision_brief": list(chapter_row.revision_brief_json or []) if chapter_row is not None else [],
                "llm_call_id": chapter_row.evaluator_llm_call_id if chapter_row is not None else None,
                "created_at": chapter_row.created_at if chapter_row is not None else None,
                "source_text_ref": chapter_row.source_text_ref if chapter_row is not None else None,
                # 2026-09-22 第三轮：这一轮通读看了哪些场（scope all / changed）、哪些场的发现是沿用上一轮的，
                # 以及通读之后又改过字的场——成稿中心据此给「只通读改过的 N 场」
                "scope": str(meta.get("scope") or ("all" if chapter_row is not None else "")),
                "reviewed_scene_ids": [str(value) for value in (meta.get("reviewed_scene_ids") or [])],
                "carried_scene_ids": [str(value) for value in (meta.get("carried_scene_ids") or [])],
                "carried_from": meta.get("carried_from"),
                "changed_scene_ids": changed_ids,
                "changed_count": len(changed_ids),
                "incremental_available": bool(changes is not None and changed_ids and changes["unchanged_scene_ids"]),
            },
        }

    def chapter_payload(self, chapter_id: str) -> dict[str, Any]:
        chapter = require_chapter(self.session, chapter_id)
        block = self._chapter_block(chapter)
        chapter_findings = block["chapter_findings"]
        scene_entries = block["scene_entries"]
        return {
            "chapter_id": chapter.chapter_id,
            "project_id": chapter.project_id,
            "ai": block["ai"],
            "chapter_findings": chapter_findings,
            "scenes": [{key: value for key, value in entry.items() if key != "counts"} for entry in scene_entries],
            "summary": {
                "open": block["counts"]["open"],
                "chapter_level": len(chapter_findings),
                "scenes": len(scene_entries),
                "scenes_with_findings": block["counts"]["scenes_with_findings"],
                "blocking": block["counts"]["blocking"],
            },
            "diagnosis_rollup": _rollup_from_block(block),
        }

    # -- 一本书的计数（主页 / 成稿中心 / 起草台的角标）-----------------------

    def project_summary(self, project_id: str) -> dict[str, Any]:
        """整本书的计数——视图挂载 / 换作品时读一次；之后的变化由 ``scene_rollup`` / ``chapter_rollup``
        随写回传（同一种 ``scenes`` / ``chapters`` 条目形状，前端本地汇总 ``totals``）。"""

        require_project(self.session, project_id)
        chapters = list(
            self.session.execute(
                select(ChapterGoal)
                .where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
                .order_by(ChapterGoal.display_order.asc(), ChapterGoal.chapter_id.asc())
            ).scalars().all()
        )
        scenes_by_chapter: dict[str, list[SceneCard]] = {chapter.chapter_id: [] for chapter in chapters}
        if chapters:
            # 与 active_chapter_scenes 同一个口径（未删的场，按 scene_seq、scene_id），各章一次取齐
            for scene in self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id.in_(list(scenes_by_chapter)), SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
            ).scalars():
                scenes_by_chapter[scene.chapter_id].append(scene)
        context = DiagnosisContext.load(
            self.session,
            [scene for scenes in scenes_by_chapter.values() for scene in scenes],
            chapter_ids=list(scenes_by_chapter),
        )
        scenes_out: dict[str, dict[str, Any]] = {}
        chapters_out: dict[str, dict[str, Any]] = {}
        for chapter in chapters:
            block = self._chapter_block(chapter, scenes=scenes_by_chapter[chapter.chapter_id], context=context)
            for entry in block["scene_entries"]:
                scenes_out[entry["scene_id"]] = entry["counts"]
            chapters_out[chapter.chapter_id] = dict(block["counts"])
        return {"project_id": project_id, "totals": summarize_counts(scenes_out, chapters_out), "chapters": chapters_out, "scenes": scenes_out}

    # -- 随写回传的计数 --------------------------------------------------------

    def scene_rollup(self, scene: SceneCard) -> dict[str, Any]:
        """这一场所在那一章的计数（章条目 + 章里每一场的条目）：作者稿保存 / 深评动作 / 忽略之后随响应回传，
        主页与成稿中心的角标据此更新，不必再拉整本书。没有章的场只回这一场。"""

        return self._diagnosis_and_rollup(scene)[1]

    def chapter_rollup(self, chapter_id: str) -> dict[str, Any]:
        chapter = require_chapter(self.session, chapter_id)
        return _rollup_from_block(self._chapter_block(chapter))


def _scene_counts_entry(chapter_id: str | None, diagnosis: dict[str, Any]) -> dict[str, Any]:
    summary = diagnosis["summary"]
    return {
        "chapter_id": chapter_id,
        "text_layer": diagnosis["text"]["layer"],
        "open": summary["open"],
        "blocking": summary["by_severity"]["blocking"],
        "revision": summary["by_severity"]["revision"],
        "taste": summary["by_severity"]["taste"],
        "info": summary["by_severity"]["info"],
        "ignored": summary["ignored"],
        "stale": summary["stale"],
        "ai_status": diagnosis["ai"]["status"],
        "review_status": diagnosis["review"]["status"],
    }


def _rollup_from_block(block: dict[str, Any]) -> dict[str, Any]:
    chapter = block["chapter"]
    return {
        "project_id": chapter.project_id,
        "chapter_id": chapter.chapter_id,
        "chapters": {chapter.chapter_id: dict(block["counts"])},
        "scenes": {entry["scene_id"]: entry["counts"] for entry in block["scene_entries"]},
    }


def summarize_counts(scenes: dict[str, dict[str, Any]], chapters: dict[str, dict[str, Any]]) -> dict[str, int]:
    """``totals`` 从场 / 章条目汇总（前端的 store 用同一条规则本地汇总，随写回传的 rollup 不必带 totals）。"""

    totals = {
        "open": 0,
        "blocking": 0,
        "revision": 0,
        "taste": 0,
        "info": 0,
        "ignored": 0,
        "stale": 0,
        "scenes": 0,
        "scenes_with_text": 0,
        "scenes_with_findings": 0,
        "ai_reviewed_scenes": 0,
        "chapters_reviewed": 0,
    }
    for entry in scenes.values():
        totals["scenes"] += 1
        if entry.get("text_layer") not in (None, "none"):
            totals["scenes_with_text"] += 1
        for key in ("open", "blocking", "revision", "taste", "info", "ignored", "stale"):
            totals[key] += int(entry.get(key) or 0)
        if int(entry.get("open") or 0):
            totals["scenes_with_findings"] += 1
        if entry.get("ai_status") and entry.get("ai_status") != "not_run":
            totals["ai_reviewed_scenes"] += 1
    for counts in chapters.values():
        if counts.get("ai_status") and counts.get("ai_status") != "not_run":
            totals["chapters_reviewed"] += 1
        # 钉不到任何一场的章级发现（承诺 / 升级 / 兑现）也算开着的
        totals["open"] += int(counts.get("chapter_level") or 0)
        totals["blocking"] += int(counts.get("chapter_level_blocking") or 0)
    return totals
