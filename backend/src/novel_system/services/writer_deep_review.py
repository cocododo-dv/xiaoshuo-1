from __future__ import annotations

import hashlib
import json
import logging
import uuid
from statistics import mean
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    AuthorPreferenceProfile,
    ChapterGoal,
    FinalScene,
    PassagePatchCandidate,
    ReviewItem,
    SceneCard,
    SceneRunState,
    StoryProject,
    WriterEvaluation,
)
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.author_preferences import merge_preference_summaries, safe_preference_summary_for_prompt
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import canonical_json
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.llm_task_runner import (
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.reference_copy_gate import (
    check_reference_copy_for_scope,
    copy_block_author_action,
)
from novel_system.services.scene_design_context import render_scene_design_context
from novel_system.services.manuscript_html import manuscript_paragraphs
from novel_system.services.scene_diagnosis import (
    LITERARY_REVISION_PASSAGE_RUBRIC_ID,
    LITERARY_REVISION_RUBRIC_ID,
    PASSAGE_VERDICTS,
    PATCH_CATEGORIES,
    SCENE_FORMS,
    PASSAGE_RELATION_KINDS,
    SceneDiagnosisService,
    candidate_category_for_dimension,
    locate_in_paragraphs,
    passage_scope,
    scene_form_from_findings,
    serialize_evaluation as _serialize_evaluation,
    serialize_passage_review,
    serialize_patch_candidate as _serialize_patch_candidate,
)
from novel_system.services.scene_lookup import require_chapter, require_scene
from novel_system.services.scene_structure_brief import render_scene_structure_brief
from novel_system.services.style_prompt_injection import (
    PLANNING_FEW_SHOT_K_CAP,
    inject_style_reference_prefix,
    resolve_style_scope,
)
from novel_system.settings import get_settings


_LOGGER = logging.getLogger(__name__)
# LITERARY_REVISION_RUBRIC_ID / SCENE_FORMS / PATCH_CATEGORIES 定义在 scene_diagnosis（这里再导出）。
__all__ = [
    "LITERARY_REVISION_RUBRIC_ID",
    "LITERARY_REVISION_PASSAGE_RUBRIC_ID",
    "LITERARY_REVISION_DIMENSIONS",
    "DEEP_REVIEW_LENSES",
    "SCENE_FORMS",
    "PATCH_CATEGORIES",
    "WriterDeepReviewOutputError",
    "WriterDeepReviewService",
]
LITERARY_REVISION_DIMENSIONS: tuple[str, ...] = (
    "character_contradiction",
    "choice_pressure",
    "relationship_tension",
    "dialogue_subtext",
    "information_rhythm",
    "voice_distinction",
    "image_necessity",
    "repetitive_expression",
    "ending_drive",
    "theme_pressure",
)
DEEP_REVIEW_LENSES: tuple[str, ...] = ("story", "character", "prose", "reader", "theme")
# 写作台工具条的自由改写（没有对应的诊断维度）用这个维度键；指令本身走 instruction 字段
AUTHOR_INSTRUCTION_DIMENSION = "author_instruction"


class WriterDeepReviewOutputError(ValueError):
    """The provider completed a call but violated a writer output contract."""


class WriterDeepReviewService:
    def __init__(self, session: Session, *, llm_client: Any | None = None, llm_runner: LLMNodeRunner | None = None) -> None:
        self.session = session
        self.prompt_builder = PromptBuilder()
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)

    def _llm_context(
        self,
        *,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        node_id: str,
        execution_step_key: str,
    ) -> LLMCallContext:
        execution_id = current_llm_execution_id()
        if object_type == "scene":
            scene = self._require_scene(scene_id or object_id)
            chapter = self._require_chapter(scene.chapter_id)
            return LLMCallContext(
                scope_type="scene",
                scope_id=scene.scene_id,
                project_id=scene.project_id or chapter.project_id,
                chapter_id=chapter.chapter_id,
                scene_id=scene.scene_id,
                node_id=node_id,
                step=node_id,
                execution_id=execution_id,
                execution_step_key=execution_step_key if execution_id is not None else None,
                provider_execution_mode=self._llm_runner.provider_execution_mode,
            )
        chapter = self._require_chapter(chapter_id or object_id)
        return LLMCallContext(
            scope_type="chapter",
            scope_id=chapter.chapter_id,
            project_id=chapter.project_id,
            chapter_id=chapter.chapter_id,
            node_id=node_id,
            step=node_id,
            execution_id=execution_id,
            execution_step_key=execution_step_key if execution_id is not None else None,
            provider_execution_mode=self._llm_runner.provider_execution_mode,
        )

    def scene_summary(self, scene_id: str) -> dict[str, Any]:
        """写作台深改面板的载荷：统一的场景诊断（规则 / 节奏 / 评审 / AI 深评），见 scene_diagnosis。"""

        return SceneDiagnosisService(self.session).payload(scene_id)

    def chapter_summary(self, chapter_id: str) -> dict[str, Any]:
        """成稿中心「AI 通读本章」的载荷：章级判断 + 各场的诊断计数 + 落到各场的通读发现（scene_diagnosis.chapter_payload）。"""

        return SceneDiagnosisService(self.session).chapter_payload(chapter_id)

    def run_scene_review(self, scene_id: str, actor_ref: str = "operator") -> dict[str, Any]:
        """「AI 深评」：对当前作者稿跑一次 writer_deep_review 节点，返回统一诊断载荷。拒绝式：无模型即 409。"""

        scene = self._require_scene(scene_id)
        source = self._scene_source(scene)
        self._create_deep_review(
            object_type="scene",
            object_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            source=source,
            actor_ref=actor_ref,
        )
        return SceneDiagnosisService(self.session).payload(scene.scene_id)

    def run_chapter_review(self, chapter_id: str, actor_ref: str = "operator", *, scope: str = "all") -> dict[str, Any]:
        """「AI 通读本章」：对整章跑一次 writer_deep_review，发现落到各场。拒绝式。

        ``scope="all"``：各场作者稿全文按场标出，整章一次。``scope="changed"``（2026-09-22 第三轮）：只把上次
        通读之后改过字的场全文送审，未改的场只给开头 / 结尾 / 上次的发现作摘要，它们的发现沿用上一轮
        （``carried_from``）；章级判断（承诺 / 升级 / 兑现）由模型对着上次的章级发现重新说一遍。通读行记下
        每场正文的哈希（``contract_field_refs_json.scenes``），新旧从此按哈希判。上次之后没有场改过 → 不调
        模型，载荷带 ``notice.code = CHAPTER_REVIEW_UP_TO_DATE``；上一轮没记哈希（老的行）→ 退回整章。
        """

        chapter = self._require_chapter(chapter_id)
        scope = scope if scope in {"all", "changed"} else "all"
        diagnosis_service = SceneDiagnosisService(self.session)
        scenes = diagnosis_service.chapter_scenes(chapter.chapter_id)
        texts = [diagnosis_service.text_for_scene(scene) for scene in scenes]
        previous = diagnosis_service.latest_evaluation(chapter.chapter_id, LITERARY_REVISION_RUBRIC_ID, object_type="chapter")
        changes = diagnosis_service.chapter_review_changes(previous, [scene.scene_id for scene in scenes], texts) if previous is not None else None
        incremental = scope == "changed" and changes is not None and bool(changes["unchanged_scene_ids"])
        if scope == "changed" and changes is not None and not changes["changed_scene_ids"] and not changes["removed_scene_ids"]:
            payload = diagnosis_service.chapter_payload(chapter.chapter_id)
            payload["notice"] = {"code": "CHAPTER_REVIEW_UP_TO_DATE", "message": "上次通读之后没有场改过字，不必再通读。"}
            return payload
        full_scene_ids = set(changes["changed_scene_ids"]) if incremental else {scene.scene_id for scene in scenes}
        carried = _carry_previous_scene_findings(previous, scenes, texts, full_scene_ids) if incremental else []
        source = self._chapter_source(
            chapter,
            scenes=scenes,
            texts=texts,
            full_scene_ids=full_scene_ids if incremental else None,
            previous=previous if incremental else None,
        )
        prompt_tail = _chapter_review_prompt_tail(scenes, texts, full_scene_ids, previous, carried) if incremental else None
        self._create_deep_review(
            object_type="chapter",
            object_id=chapter.chapter_id,
            chapter_id=chapter.chapter_id,
            scene_id=None,
            source=source,
            actor_ref=actor_ref,
            prompt_tail=prompt_tail,
            extra_findings=carried,
            meta={
                "kind": "chapter",
                "scope": "changed" if incremental else "all",
                "scenes": [
                    {
                        "scene_id": scene.scene_id,
                        "scene_seq": scene.scene_seq,
                        "sha256": text.sha256 if text.layer != "none" else "",
                        "layer": text.layer,
                        "ref": text.ref,
                    }
                    for scene, text in zip(scenes, texts)
                ],
                "reviewed_scene_ids": [scene.scene_id for scene in scenes if scene.scene_id in full_scene_ids],
                "carried_scene_ids": [scene.scene_id for scene in scenes if scene.scene_id not in full_scene_ids] if incremental else [],
                "carried_from": previous.evaluation_id if incremental and previous is not None else None,
            },
        )
        return diagnosis_service.chapter_payload(chapter.chapter_id)

    def run_passage_review(
        self,
        scene_id: str,
        *,
        signal_id: str | None = None,
        paragraph_index: int | None = None,
        paragraph_start: int | None = None,
        paragraph_end: int | None = None,
        excerpt: str | None = None,
        question: str | None = None,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        """「AI 看这一处」：局部深评——焦点是一段（``paragraph_index`` / 选中的原话）、一段范围
        （``paragraph_start``–``paragraph_end``）或一条发现所在的段（``signal_id``），模型同时看到整场正文
        （``passage_scope``），所以能指出焦点段与本场另一段的矛盾 / 重复（发现带 ``related``）。

        带 ``signal_id`` 时是对那条发现的复核（成立 / 部分成立 / 不成立）加改法；否则是对这一处的独立判断。
        结果落成一行 rubric ``literary_revision_passage_v1`` 的评审（焦点段有交集、或复核同一条发现的旧行退位），
        它的发现与意见并入统一诊断（scene_diagnosis）。拒绝式：无模型即 409。
        """

        self._require_live_llm("writer_passage_review")
        scene = self._require_scene(scene_id)
        diagnosis_service = SceneDiagnosisService(self.session)
        text = diagnosis_service.text_for_scene(scene)
        if text.layer == "none":
            raise DomainError("WRITER_PASSAGE_REVIEW_NO_TEXT", "这一场还没有正文，没有可看的段落。", status_code=409)
        about: dict[str, Any] | None = None
        focus: list[int] = []
        if signal_id:
            diagnosis = diagnosis_service.diagnose_scene(scene, with_patches=False)
            about = next((item for item in diagnosis["findings"] if item["signal_id"] == signal_id), None)
            if about is None:
                raise DomainError(
                    "WRITER_PASSAGE_REVIEW_FINDING_NOT_FOUND",
                    "这条发现不在当前作者稿的诊断里（可能已经改掉，或来自另一层文本）。",
                    status_code=404,
                    details={"signal_id": signal_id},
                )
            evidence = about.get("evidence") or {}
            if isinstance(evidence.get("paragraph_index"), int):
                focus.append(int(evidence["paragraph_index"]))
            related = about.get("related") or {}
            if isinstance(related.get("paragraph_index"), int):
                focus.append(int(related["paragraph_index"]))
            if not excerpt and evidence.get("excerpt"):
                excerpt = str(evidence["excerpt"])
        if paragraph_start is not None or paragraph_end is not None:
            start = int(paragraph_start if paragraph_start is not None else paragraph_end)
            end = int(paragraph_end if paragraph_end is not None else paragraph_start)
            if start > end:
                start, end = end, start
            focus.extend(range(start, end + 1))
        if paragraph_index is not None:
            focus.append(int(paragraph_index))
        if not focus and excerpt:
            hit = locate_in_paragraphs(text.paragraphs, excerpt)
            if hit:
                focus.append(int(hit["paragraph_index"]))
        focus = sorted({index for index in focus if 0 <= index < len(text.paragraphs)})
        if not focus:
            raise DomainError(
                "WRITER_PASSAGE_REVIEW_TARGET_INVALID",
                "要看的那一段不在正文里：给一个段落序号或范围，或一句正文里的原话。",
                status_code=400,
                details={"paragraph_count": len(text.paragraphs)},
            )
        if len(focus) > PASSAGE_MAX_FOCUS_PARAGRAPHS:
            raise DomainError(
                "WRITER_PASSAGE_REVIEW_TARGET_INVALID",
                f"一次最多看 {PASSAGE_MAX_FOCUS_PARAGRAPHS} 段；要看整场就跑 AI 深评。",
                status_code=400,
                details={"paragraph_count": len(text.paragraphs), "focus_count": len(focus)},
            )
        scope = passage_scope(text.paragraphs, focus)
        snapshot: dict[str, Any] = {
            "object_type": "scene",
            "object_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "scene_id": scene.scene_id,
            "rubric_id": LITERARY_REVISION_PASSAGE_RUBRIC_ID,
            "dimensions": list(LITERARY_REVISION_DIMENSIONS),
            "lenses": list(DEEP_REVIEW_LENSES),
            "passage": {
                "focus_paragraphs": focus,
                "start": scope["start"],
                "end": scope["end"],
                "whole_scene": scope["whole_scene"],
            },
            "about_signal_id": signal_id,
            "scene_summary": scope["text"],
        }
        # 段落范围与这一场的结构 / 设计背景都作为 inline digest 进用户消息（见 _create_deep_review_with_llm）
        snapshot["inline_digests"] = {"scene_summary": scope["text"], **self._scene_design_sections(scene.scene_id)}
        prompt = self.prompt_builder.build(snapshot, "writer_passage_review")
        user_prompt = _passage_review_user_prompt(
            prompt["user_prompt"],
            scope=scope,
            about=about,
            excerpt=excerpt,
            question=question,
        )
        prompt = self._inject_style_reference_prefix(
            prompt,
            object_type="scene",
            object_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            context_text=scope["focus_text"] or None,
            final_user_prompt=user_prompt,
        )
        execution_step_key = f"writer_passage_review:{scene.scene_id}:{focus[0]}-{focus[-1]}"
        context = self._llm_context(
            object_type="scene",
            object_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            node_id="writer_deep_review",
            execution_step_key=execution_step_key,
        )
        try:
            node_result = self._llm_runner.run(
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                bundle_id=text.ref or f"writer_passage_review:{scene.scene_id}",
                bundle_hash=hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
                node_id="writer_deep_review",
                step="writer_passage_review",
                prompt=prompt,
                user_prompt=user_prompt,
                execution_step_key=execution_step_key,
                context=context,
            )
        except LLMNodeExecutionError as exc:
            raise DomainError(
                "WRITER_DEEP_REVIEW_LLM_FAILED",
                exc.message,
                status_code=409,
                details={
                    "llm_call_id": exc.llm_call_id,
                    "node_id": "writer_deep_review",
                    "step": "writer_passage_review",
                    "error_code": exc.error_code,
                    "next_action": "configure_writer_deep_review_route_and_retry",
                    "response_summary": exc.response_summary,
                },
            ) from exc
        normalized = _normalize_passage_review_output(
            node_result.response.structured_output or {},
            has_finding=about is not None,
        )
        if not normalized["assessment"] and not normalized["findings"] and not normalized["rewrite_brief"]:
            raise DomainError(
                "WRITER_PASSAGE_REVIEW_EMPTY",
                "模型这次没有给出可用的判断。换个问法，或稍后再试。",
                status_code=502,
                details={"llm_call_id": node_result.llm_call_id, "node_id": "writer_deep_review"},
            )
        # 焦点段有交集、或复核的是同一条发现：旧的退位，面板只留最新的意见
        focus_set = set(focus)
        for row in diagnosis_service.passage_rows(scene.scene_id):
            meta = row.contract_field_refs_json if isinstance(row.contract_field_refs_json, dict) else {}
            old_focus = {int(value) for value in (meta.get("focus_paragraphs") or []) if isinstance(value, int)}
            if not old_focus and isinstance(meta.get("paragraph_index"), int):
                old_focus = {int(meta["paragraph_index"])}
            same_target = bool(old_focus & focus_set) or bool(signal_id and meta.get("about_signal_id") == signal_id)
            if same_target:
                row.status = "superseded"
        row = WriterEvaluation(
            evaluation_id=f"writer_passage_eval_{scene.scene_id}_{uuid.uuid4().hex[:10]}",
            object_type="scene",
            object_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            rubric_id=LITERARY_REVISION_PASSAGE_RUBRIC_ID,
            source_text_ref=text.ref,
            source_bundle_id=None,
            evaluator_llm_call_id=node_result.llm_call_id,
            lens="passage",
            parent_evaluation_id=None,
            evidence_spans_json=[{"paragraph_index": index, "text": text.paragraphs[index][:80]} for index in focus[:8]],
            overall_score=None,
            scores_json={},
            findings_json=[{**item, "passage_paragraph_index": focus[0]} for item in normalized["findings"]],
            revision_brief_json=(
                [{"dimension": (about or {}).get("dimension") or "passage", "classification": "revision", "action": normalized["rewrite_brief"], "priority": "medium"}]
                if normalized["rewrite_brief"]
                else []
            ),
            # 这一行「看的是什么、说了什么」：焦点段、看到的范围、复核的发现 id、判定、评语、改法、作者的问题
            contract_field_refs_json={
                "kind": "passage",
                "paragraph_index": focus[0],
                "focus_paragraphs": focus,
                "paragraph_start": scope["start"],
                "paragraph_end": scope["end"],
                "whole_scene": scope["whole_scene"],
                "about_signal_id": signal_id,
                "about_signal_ids": [signal_id] if signal_id else [],
                "verdict": normalized["verdict"],
                "assessment": normalized["assessment"],
                "rewrite_brief": normalized["rewrite_brief"],
                "question": question or "",
                "excerpt": excerpt or "",
            },
            requires_human_review=0,
            status="completed",
        )
        self.session.add(row)
        self.session.flush()
        payload = diagnosis_service.diagnose_scene(scene)
        payload["passage_review"] = serialize_passage_review(row, "current")
        payload["diagnosis_rollup"] = diagnosis_service.scene_rollup(scene)
        return payload

    def create_patch_candidate(self, payload: dict[str, Any], actor_ref: str = "operator") -> dict[str, Any]:
        """局部改写候选。

        ``issue_dimension`` 是维度键（一条诊断发现的 ``dimension``，或工具条自由改写的
        ``author_instruction``）；作者 / 诊断给的改法走 ``instruction``，发现的问题句走
        ``issue_note``，发现的 id 走 ``quality_signal_id``——修补类别、改写策略与偏好标签按
        维度推，画像学到的是「对白潜台词」而不是「润色」两个字。
        """

        source_excerpt = _required_text(payload, "source_excerpt")
        issue_dimension = _required_text(payload, "issue_dimension")
        object_type = _required_text(payload, "object_type")
        object_id = _required_text(payload, "object_id")
        if object_type not in {"scene", "chapter"}:
            raise DomainError("PASSAGE_PATCH_INVALID", "object_type must be scene or chapter", status_code=400)
        instruction = _optional_text(payload, "instruction")
        issue_note = _optional_text(payload, "issue_note")
        patch_payload = self._run_passage_patch(
            payload,
            source_excerpt=source_excerpt,
            issue_dimension=issue_dimension,
            instruction=instruction,
            issue_note=issue_note,
        )
        row = PassagePatchCandidate(
            patch_id=f"passage_patch_{object_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            object_type=object_type,
            object_id=object_id,
            chapter_id=_optional_text(payload, "chapter_id"),
            scene_id=_optional_text(payload, "scene_id"),
            source_text_ref=_optional_text(payload, "source_text_ref") or _optional_text(payload, "target_text_ref"),
            target_text_ref=_optional_text(payload, "target_text_ref"),
            source_draft_id=_optional_text(payload, "source_draft_id"),
            generation_llm_call_id=patch_payload.get("generation_llm_call_id"),
            quality_signal_id=_optional_text(payload, "quality_signal_id"),
            source_excerpt=source_excerpt,
            issue_dimension=issue_dimension,
            candidate_category=_candidate_category(payload, issue_dimension),
            target_range_json=_target_range(payload.get("target_range")),
            revision_strategy=_revision_strategy(payload, issue_dimension, instruction=instruction),
            preference_tags_json=_preference_tags(payload, issue_dimension, instruction=instruction),
            inserted_into_author_draft=0,
            replacement_options_json=patch_payload["replacement_options"],
            rationale=patch_payload.get("rationale"),
            manual_only=1,
            status="candidate",
            author_decision="pending",
            created_by=actor_ref or "writer_deep_review",
        )
        self.session.add(row)
        self.session.flush()
        return {"candidate": self.serialize_patch_candidate(row)}

    def accept_patch_candidate(self, patch_id: str, payload: dict[str, Any], actor_ref: str = "operator") -> dict[str, Any]:
        row = self._require_patch_candidate(patch_id)
        selected_option_id = _optional_text(payload, "selected_option_id")
        option_ids = {str(option.get("option_id")) for option in row.replacement_options_json or []}
        if selected_option_id and selected_option_id not in option_ids:
            raise DomainError("PASSAGE_PATCH_OPTION_NOT_FOUND", "selected replacement option not found", status_code=404)
        self._require_patch_copy_safe(row, selected_option_id)
        row.status = "accepted"
        row.author_decision = "accepted"
        row.selected_option_id = selected_option_id
        row.author_decision_note = _optional_text(payload, "note") or row.author_decision_note
        self.session.flush()
        self._refresh_author_preference_profile(actor_ref=actor_ref)
        self.session.flush()
        return {"candidate": self.serialize_patch_candidate(row)}

    def _require_patch_copy_safe(self, row: PassagePatchCandidate, selected_option_id: str | None) -> None:
        """作者采纳局部改写之前过唯一抄袭门（风格参考 v3 V1）：采纳的那个选项（没点名就查全部选项）与绑定的参考书
        连续 ≥12 字相同、或含受保护专名 → 409，候选不改状态。位置是选项文字里的第几字，不印参考原文。"""

        options = [option for option in row.replacement_options_json or [] if isinstance(option, dict)]
        if selected_option_id:
            options = [option for option in options if str(option.get("option_id")) == selected_option_id]
        text = "\n".join(str(option.get("replacement_text") or "") for option in options).strip()
        if not text:
            return
        scene_id = row.scene_id or (row.object_id if row.object_type == "scene" else None)
        chapter_id = row.chapter_id or (row.object_id if row.object_type == "chapter" else None)
        scope = resolve_style_scope(self.session, scene_id=scene_id, chapter_id=chapter_id)
        check = check_reference_copy_for_scope(self.session, text, scope=scope)
        if not check.blocked:
            return
        raise DomainError(
            "SOURCE_SAFETY_BLOCKED",
            "reference copy gate blocked adopting this passage rewrite — the author draft is unchanged",
            status_code=409,
            details={
                "patch_id": row.patch_id,
                "selected_option_id": selected_option_id,
                "reference_copy": check.audit(),
                "author_action": copy_block_author_action(
                    check,
                    target_view="writer",
                    target_ref=f"{row.object_type}:{row.object_id}",
                    subject="这条改写",
                ),
            },
        )

    def reject_patch_candidate(self, patch_id: str, payload: dict[str, Any], actor_ref: str = "operator") -> dict[str, Any]:
        row = self._require_patch_candidate(patch_id)
        row.status = "rejected"
        row.author_decision = "rejected"
        row.author_decision_note = _optional_text(payload, "note") or row.author_decision_note
        self.session.flush()
        self._refresh_author_preference_profile(actor_ref=actor_ref)
        self.session.flush()
        return {"candidate": self.serialize_patch_candidate(row)}

    def author_preference_profile(self) -> dict[str, Any]:
        profile = self._latest_preference_profile()
        if profile is None:
            return {
                "profile": {
                    "profile_id": "author_pref_global_global",
                    "scope_type": "global",
                    "scope_ref_id": "global",
                    "status": "draft",
                    "runtime_eligible": False,
                    "summary": _empty_preference_summary(),
                    "source_patch_ids": [],
                    "created_at": None,
                    "updated_at": None,
                }
            }
        return {"profile": self.serialize_preference_profile(profile)}

    @staticmethod
    def serialize_evaluation(row: WriterEvaluation | None) -> dict[str, Any] | None:
        return _serialize_evaluation(row)

    @staticmethod
    def serialize_patch_candidate(row: PassagePatchCandidate) -> dict[str, Any]:
        return _serialize_patch_candidate(row)

    @staticmethod
    def serialize_preference_profile(row: AuthorPreferenceProfile) -> dict[str, Any]:
        return {
            "profile_id": row.profile_id,
            "scope_type": row.scope_type,
            "scope_ref_id": row.scope_ref_id,
            "status": row.status,
            "runtime_eligible": bool(row.runtime_eligible),
            "summary": row.summary_json or _empty_preference_summary(),
            "source_patch_ids": row.source_patch_ids_json or [],
            "created_by": row.created_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    def _create_deep_review(
        self,
        *,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        source: dict[str, Any],
        actor_ref: str,
        prompt_tail: str | None = None,
        extra_findings: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """深评是拒绝式的 LLM 节点：没有真实模型就 409 + author_action，不再有本地词表兜底。

        （2026-09-22 之前这里有一条 ``_diagnose_by_lens``：按「保护 / 真相 / 公开 / 隐藏」这类
        写死的词给出套话——那是退役演示故事的残留，对任何真实作品都在说谎。）
        """

        self._require_live_llm("writer_deep_review")
        return self._create_deep_review_with_llm(
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            source=source,
            prompt_tail=prompt_tail,
            extra_findings=extra_findings,
            meta=meta,
        )

    @staticmethod
    def _require_live_llm(step: str) -> None:
        if get_settings().llm_enabled:
            return
        raise DomainError(
            "WRITER_DEEP_REVIEW_LLM_REQUIRED",
            "写作台的 AI 深评需要先启用真实模型。请到系统配置里配置 provider 与密钥并测试通过后重试。",
            status_code=409,
            details={
                "node_id": "writer_deep_review",
                "step": step,
                "next_action": "configure_writer_deep_review_route_and_retry",
                "author_action": llm_setup_action(llm_enabled=False, generation_mode="offline_disabled"),
            },
        )

    def _create_deep_review_with_llm(
        self,
        *,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        source: dict[str, Any],
        prompt_tail: str | None = None,
        extra_findings: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = {
            "object_type": object_type,
            "object_id": object_id,
            "chapter_id": chapter_id,
            "scene_id": scene_id,
            "rubric_id": LITERARY_REVISION_RUBRIC_ID,
            "dimensions": list(LITERARY_REVISION_DIMENSIONS),
            "lenses": list(DEEP_REVIEW_LENSES),
            "source": {**source, "content": _prompt_text(source.get("content"))},
            "scene_summary": _prompt_text(source.get("content")) if object_type == "scene" else None,
            "chapter_summary": _prompt_text(source.get("content")) if object_type == "chapter" else None,
            "review_scope": (meta or {}).get("scope") or "all",
        }
        # 提示词装配只渲染 inline_digests 里的 section（context_budget.collect_prompt_sections）——
        # 顶层的 scene_summary / chapter_summary 从来没进过用户消息：深评节点在接进面板之前从未被调用，
        # 所以这个空载荷一直没人发现。正文（可见文字）与这一场的结构 / 设计背景都从这里进。
        digests: dict[str, str] = {}
        prompt_text = _prompt_text(source.get("content"))
        if prompt_text:
            digests["scene_summary" if object_type == "scene" else "chapter_summary"] = prompt_text
        if object_type == "scene":
            # 2026-09-22：深评按作者设计的这一场判断（形态 / 三拍 / 代价 / 该藏的），不把设计好的
            # 反应场当成「压力不足」；设计背景是可压缩的 section，缺了也不影响评审本身。
            digests.update(self._scene_design_sections(scene_id or object_id))
        snapshot["inline_digests"] = digests
        prompt = self.prompt_builder.build(snapshot, "writer_deep_review")
        # 只通读改过的场时，用户消息尾部说明哪些场是全文、哪些只是摘要，并列出上次的章级发现
        user_prompt = prompt["user_prompt"] + (f"\n\n{prompt_tail}" if prompt_tail else "")
        # 2026-09-14 WP6.3：评审在参考作者的手笔下判断「复读 / 意象必要性 / 声音辨识度」
        prompt = self._inject_style_reference_prefix(
            prompt,
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            context_text=str(source.get("content") or "") or None,
            final_user_prompt=user_prompt,
        )
        execution_step_key = f"writer_deep_review:{object_type}:{object_id}"
        context = self._llm_context(
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            node_id="writer_deep_review",
            execution_step_key=execution_step_key,
        )
        try:
            node_result = self._llm_runner.run(
                scene_id=scene_id or object_id,
                chapter_id=chapter_id or object_id,
                bundle_id=source.get("source_text_ref") or f"writer_deep_review:{object_type}:{object_id}",
                bundle_hash=hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
                node_id="writer_deep_review",
                step="writer_deep_review",
                prompt=prompt,
                user_prompt=user_prompt,
                execution_step_key=execution_step_key,
                context=context,
            )
        except LLMNodeExecutionError as exc:
            raise DomainError(
                "WRITER_DEEP_REVIEW_LLM_FAILED",
                exc.message,
                status_code=409,
                details={
                    "llm_call_id": exc.llm_call_id,
                    "node_id": "writer_deep_review",
                    "error_code": exc.error_code,
                    "next_action": "configure_writer_deep_review_route_and_retry",
                    "response_summary": exc.response_summary,
                },
            ) from exc
        normalized = _normalize_deep_review_output(node_result.response.structured_output or {})
        if extra_findings:
            # 未改的场沿用上一轮通读的发现（带 carried_from）；模型这次又说到同一处的，以模型的为准
            seen = {(str(item.get("dimension")), _compact_text(str(item.get("evidence_excerpt") or ""), 200)) for item in normalized["findings"]}
            normalized["findings"] = list(normalized["findings"]) + [
                item for item in extra_findings if (str(item.get("dimension")), _compact_text(str(item.get("evidence_excerpt") or ""), 200)) not in seen
            ]
        # 模型答完了才让旧的一轮退位：被拒绝 / 失败的一次不动历史
        for row in self.session.execute(
            select(WriterEvaluation).where(
                WriterEvaluation.object_type == object_type,
                WriterEvaluation.object_id == object_id,
                WriterEvaluation.rubric_id == LITERARY_REVISION_RUBRIC_ID,
                WriterEvaluation.parent_evaluation_id.is_(None),
            )
        ).scalars().all():
            row.status = "superseded"
        parent = WriterEvaluation(
            evaluation_id=f"writer_deep_eval_{object_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            rubric_id=LITERARY_REVISION_RUBRIC_ID,
            source_text_ref=source.get("source_text_ref"),
            source_bundle_id=source.get("source_bundle_id"),
            evaluator_llm_call_id=node_result.llm_call_id,
            lens="aggregate",
            parent_evaluation_id=None,
            evidence_spans_json=_evidence_spans(source["content"], normalized["findings"]),
            overall_score=normalized["overall_score"],
            scores_json=normalized["scores"],
            findings_json=normalized["findings"],
            revision_brief_json=normalized["revision_brief"],
            contract_field_refs_json=dict(meta) if meta else None,
            requires_human_review=1 if normalized["requires_human_review"] else 0,
            status="completed",
        )
        self.session.add(parent)
        self.session.flush()

        for payload in normalized["lens_evaluations"]:
            lens = str(payload.get("lens") or "story")
            scores = _normalize_scores(payload.get("scores"))
            findings = _normalize_findings(payload.get("findings"))
            row = WriterEvaluation(
                evaluation_id=f"writer_deep_eval_{object_type}_{object_id}_{lens}_{uuid.uuid4().hex[:8]}",
                object_type=object_type,
                object_id=object_id,
                chapter_id=chapter_id,
                scene_id=scene_id,
                rubric_id=LITERARY_REVISION_RUBRIC_ID,
                source_text_ref=source.get("source_text_ref"),
                source_bundle_id=source.get("source_bundle_id"),
                evaluator_llm_call_id=node_result.llm_call_id,
                lens=lens,
                parent_evaluation_id=parent.evaluation_id,
                evidence_spans_json=_evidence_spans(source["content"], findings),
                overall_score=_optional_score(payload.get("overall_score")) or (round(mean(scores.values()), 2) if scores else None),
                scores_json=scores,
                findings_json=findings,
                revision_brief_json=_normalize_revision_brief(payload.get("revision_brief"), findings),
                requires_human_review=1 if any(item.get("severity") == "blocking" for item in findings) else 0,
                status="completed",
            )
            self.session.add(row)
        self.session.flush()
        return self._review_payload(object_type, object_id)

    def _scene_design_sections(self, scene_id: str) -> dict[str, str]:
        """这一场的结构事实段 + 设计背景段（有就给，任何一段渲染失败都只是少一段）。"""

        sections: dict[str, str] = {}
        scene = self.session.get(SceneCard, scene_id)
        if scene is None:
            return sections
        try:
            brief = render_scene_structure_brief(scene, self.session)
            if brief:
                sections["scene_structure_brief"] = brief
        except Exception:  # noqa: BLE001 — 背景段是可选增强
            _LOGGER.debug("scene structure brief unavailable for %s", scene_id, exc_info=True)
        try:
            context = render_scene_design_context(scene, self.session)
            if context:
                sections["scene_design_context"] = context
        except Exception:  # noqa: BLE001
            _LOGGER.debug("scene design context unavailable for %s", scene_id, exc_info=True)
        return sections

    def _review_payload(self, object_type: str, object_id: str) -> dict[str, Any]:
        latest = self.session.execute(
            select(WriterEvaluation)
            .where(
                WriterEvaluation.object_type == object_type,
                WriterEvaluation.object_id == object_id,
                WriterEvaluation.rubric_id == LITERARY_REVISION_RUBRIC_ID,
                WriterEvaluation.parent_evaluation_id.is_(None),
            )
            .order_by(WriterEvaluation.created_at.desc(), WriterEvaluation.evaluation_id.desc())
        ).scalars().first()
        lenses: list[dict[str, Any]] = []
        if latest is not None:
            lens_rows = self.session.execute(
                select(WriterEvaluation)
                .where(WriterEvaluation.parent_evaluation_id == latest.evaluation_id)
                .order_by(WriterEvaluation.lens.asc(), WriterEvaluation.evaluation_id.asc())
            ).scalars().all()
            lenses = [item for item in (self.serialize_evaluation(row) for row in lens_rows) if item]
        patch_rows = self.session.execute(
            select(PassagePatchCandidate)
            .where(PassagePatchCandidate.object_type == object_type, PassagePatchCandidate.object_id == object_id)
            .order_by(PassagePatchCandidate.created_at.desc(), PassagePatchCandidate.patch_id.desc())
        ).scalars().all()
        latest_payload = self.serialize_evaluation(latest)
        return {
            "status": "reviewed" if latest else "not_run",
            "object_type": object_type,
            "object_id": object_id,
            "rubric_id": LITERARY_REVISION_RUBRIC_ID,
            "latest_evaluation": latest_payload,
            "latest_score": latest_payload["overall_score"] if latest_payload else None,
            "requires_human_review": bool(latest_payload["requires_human_review"]) if latest_payload else False,
            "lens_evaluations": lenses,
            "patch_candidates": [self.serialize_patch_candidate(row) for row in patch_rows],
        }

    def _run_passage_patch(
        self,
        payload: dict[str, Any],
        *,
        source_excerpt: str,
        issue_dimension: str,
        instruction: str | None = None,
        issue_note: str | None = None,
    ) -> dict[str, Any]:
        target_text_ref = _optional_text(payload, "target_text_ref") or _optional_text(payload, "source_text_ref") or ""
        source_draft = self._source_draft(_optional_text(payload, "source_draft_id"))
        preference = self._approved_runtime_preference_profile(payload)
        snapshot = _passage_patch_snapshot(
            payload=payload,
            source_excerpt=source_excerpt,
            issue_dimension=issue_dimension,
            target_text_ref=target_text_ref,
            source_draft=source_draft,
            preference=preference,
            instruction=instruction,
            issue_note=issue_note,
        )
        prompt = self.prompt_builder.build(snapshot, "writer_passage_patch")
        object_type = _required_text(payload, "object_type")
        object_id = _required_text(payload, "object_id")
        user_prompt = _passage_patch_user_prompt(
            prompt["user_prompt"],
            source_excerpt=source_excerpt,
            issue_dimension=issue_dimension,
            target_text_ref=target_text_ref,
            source_draft=source_draft,
            preference=preference,
            instruction=instruction,
            issue_note=issue_note,
        )
        # 2026-09-14 WP6.3：局部补丁在参考作者的手笔下改句（k≤3 样例窗口，作者稿作选窗上下文）
        prompt = self._inject_style_reference_prefix(
            prompt,
            object_type=object_type,
            object_id=object_id,
            chapter_id=_optional_text(payload, "chapter_id"),
            scene_id=_optional_text(payload, "scene_id"),
            context_text=(source_draft.content if source_draft is not None else source_excerpt) or None,
            final_user_prompt=user_prompt,
        )
        execution_step_key = f"writer_passage_patch:{object_type}:{object_id}"
        context = self._llm_context(
            object_type=object_type,
            object_id=object_id,
            chapter_id=_optional_text(payload, "chapter_id"),
            scene_id=_optional_text(payload, "scene_id"),
            node_id="writer_passage_patch",
            execution_step_key=execution_step_key,
        )
        node_result = self._llm_runner.run(
            scene_id=context.scene_id,
            chapter_id=context.chapter_id,
            bundle_id=snapshot["source_version_refs"]["target_text_ref"] or "writer_passage_patch",
            bundle_hash=hashlib.sha256(canonical_json(snapshot).encode("utf-8")).hexdigest(),
            node_id="writer_passage_patch",
            step="writer_passage_patch",
            prompt=prompt,
            user_prompt=user_prompt,
            execution_step_key=execution_step_key,
            context=context,
        )
        try:
            normalized = _normalize_patch_output(
                node_result.response.structured_output,
                source_excerpt=source_excerpt,
                issue_dimension=issue_dimension,
                target_text_ref=target_text_ref,
            )
        except WriterDeepReviewOutputError as exc:
            raise DomainError(
                "WRITER_PASSAGE_PATCH_OUTPUT_INVALID",
                f"writer passage patch returned an invalid payload: {exc}",
                status_code=502,
                details={
                    "llm_call_id": node_result.llm_call_id,
                    "node_id": "writer_passage_patch",
                    "validation_error": str(exc),
                },
            ) from exc
        generation_llm_call_id = str(node_result.llm_call_id or "").strip()
        if not generation_llm_call_id:
            raise DomainError(
                "WRITER_PASSAGE_PATCH_OUTPUT_INVALID",
                "writer passage patch completed without an auditable LLM call id",
                status_code=502,
                details={"node_id": "writer_passage_patch", "validation_error": "generation_llm_call_id is required"},
            )
        normalized["generation_llm_call_id"] = generation_llm_call_id
        return normalized

    def _inject_style_reference_prefix(
        self,
        prompt: dict[str, Any],
        *,
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        context_text: str | None,
        final_user_prompt: str,
    ) -> dict[str, Any]:
        """2026-09-14 保真修补（WP6.3）：深评 / 局部补丁按项目 / 场景的 active 绑定拿到 ``[STYLE_REFERENCE]``。

        场景对象按场景作用域（窗口按场景轮换），章对象按 project + global 作用域；样例窗口封顶
        :data:`PLANNING_FEW_SHOT_K_CAP`（评审与补丁只需少量样例定标准），被评 / 被改的文本作
        选窗上下文，并按最终 user prompt 压进模板预算。无绑定 → 提示词逐字不变；解析 / 注入
        失败 → 回退基础 prompt（可选增强，绝不阻断评审或补丁）。
        """
        try:
            scope = resolve_style_scope(
                self.session,
                scene_id=scene_id or (object_id if object_type == "scene" else None),
                chapter_id=chapter_id or (object_id if object_type == "chapter" else None),
            )
            if scope is None:
                return prompt
            injected = inject_style_reference_prefix(
                self.session,
                prompt,
                scope,
                None,
                task_type="scene_generation",
                context_text=context_text,
                final_user_prompt=final_user_prompt,
                few_shot_k_cap=PLANNING_FEW_SHOT_K_CAP,
            )
            return injected if injected is not None else prompt
        except Exception:  # noqa: BLE001 — 可选增强：注入失败只记日志，不阻断评审 / 补丁
            _LOGGER.warning(
                "writer style reference prefix skipped for %s %s",
                object_type,
                object_id,
                exc_info=True,
            )
            return prompt

    def _source_draft(self, source_draft_id: str | None) -> AuthorDraft | None:
        if not source_draft_id:
            return None
        return self.session.get(AuthorDraft, source_draft_id)

    def _approved_runtime_preference_profile(self, payload: dict[str, Any]) -> AuthorPreferenceProfile | None:
        chapter_id = _optional_text(payload, "chapter_id")
        scene_id = _optional_text(payload, "scene_id")
        scene = self.session.get(SceneCard, scene_id) if scene_id else None
        chapter = self.session.get(ChapterGoal, chapter_id or (scene.chapter_id if scene else ""))
        project_id = (scene.project_id if scene else None) or (chapter.project_id if chapter else None)
        project = self.session.get(StoryProject, project_id) if project_id else None
        scopes: list[tuple[str, str]] = [("global", "global")]
        genre = " ".join(str(project.genre or "").strip().lower().split()) if project else ""
        if genre:
            scopes.append(("genre", genre[:120]))
        if project_id:
            scopes.append(("project", project_id))
        if chapter is not None:
            scopes.append(("chapter", chapter.chapter_id))
        rows: list[AuthorPreferenceProfile] = []
        for scope_type, scope_ref_id in scopes:
            rows.extend(
                self.session.execute(
                    select(AuthorPreferenceProfile)
                    .where(
                        AuthorPreferenceProfile.scope_type == scope_type,
                        AuthorPreferenceProfile.scope_ref_id == scope_ref_id,
                        AuthorPreferenceProfile.status == "approved",
                        AuthorPreferenceProfile.runtime_eligible == 1,
                    )
                    .order_by(AuthorPreferenceProfile.updated_at.asc(), AuthorPreferenceProfile.profile_id.asc())
                ).scalars().all()
            )
        if not rows:
            return None
        summary: dict[str, Any] = {}
        for row in rows:
            summary = merge_preference_summaries(summary, row.summary_json or {})
        # Keep the existing snapshot contract while avoiding mutation of any
        # persisted profile as broader scopes are merged for this target.
        return SimpleNamespace(
            profile_id=rows[-1].profile_id,
            summary_json=safe_preference_summary_for_prompt(summary),
        )

    def _scene_source(self, scene: SceneCard) -> dict[str, Any]:
        author_draft = self._current_author_draft("scene", scene.scene_id)
        if author_draft is not None:
            return {
                "content": author_draft.content or "",
                "source_text_ref": f"author_draft:{author_draft.draft_id}",
                "source_bundle_id": None,
            }
        state = self.session.get(SceneRunState, scene.scene_id)
        final_row = self.session.get(FinalScene, state.current_final_scene_row_id) if state and state.current_final_scene_row_id else None
        if final_row is None:
            final_row = self.session.execute(
                select(FinalScene)
                .where(FinalScene.scene_id == scene.scene_id)
                .order_by(FinalScene.created_at.desc(), FinalScene.row_id.desc())
            ).scalars().first()
        return {
            "content": final_row.content if final_row else "",
            "source_text_ref": f"final_scene:{final_row.row_id}" if final_row else f"scene:{scene.scene_id}",
            "source_bundle_id": final_row.source_bundle_id if final_row else (state.current_bundle_id if state else None),
        }

    def _chapter_source(
        self,
        chapter: ChapterGoal,
        *,
        scenes: list[SceneCard] | None = None,
        texts: list[Any] | None = None,
        full_scene_ids: set[str] | None = None,
        previous: WriterEvaluation | None = None,
    ) -> dict[str, Any]:
        """通读整章给模型看的字：各场的诊断正文（作者稿，其次运行终稿——与写作台看到的同一份）按场标出，
        评审能说「第 3 场」，发现的引文仍逐字来自各场正文。只通读改过的场时（``full_scene_ids`` 是子集），
        未改的场只给开头 / 结尾与上一轮对它的发现作摘要。

        2026-09-22 第三轮起不再取章级作者稿 / 章记忆：诊断把发现钉到各场的正文上，通读看的必须是同一份字。
        """

        diagnosis_service = SceneDiagnosisService(self.session)
        if scenes is None:
            scenes = diagnosis_service.chapter_scenes(chapter.chapter_id)
        if texts is None:
            texts = [diagnosis_service.text_for_scene(scene) for scene in scenes]
        full = set(full_scene_ids) if full_scene_ids is not None else {scene.scene_id for scene in scenes}
        previous_by_scene = _previous_findings_by_scene(previous, scenes, texts) if previous is not None else {}
        parts: list[str] = []
        for index, (scene, text) in enumerate(zip(scenes, texts), start=1):
            if text.layer == "none":
                continue
            paragraphs = [paragraph for paragraph in text.paragraphs if paragraph.strip()]
            if scene.scene_id in full:
                marker = f"【第 {index} 场】" if full_scene_ids is None else f"【第 {index} 场 · 本次通读】"
                parts.append(f"{marker}\n" + "\n\n".join(paragraphs))
                continue
            digest = [f"【第 {index} 场 · 未改 · 摘要】"]
            if paragraphs:
                digest.append(f"（开头）{paragraphs[0][:CHAPTER_DIGEST_EDGE_CHARS]}")
                if len(paragraphs) > 1:
                    digest.append("……")
                    digest.append(f"（结尾）{paragraphs[-1][-CHAPTER_DIGEST_EDGE_CHARS:]}")
            previous_items = previous_by_scene.get(scene.scene_id) or []
            if previous_items:
                digest.append("上次通读对这一场的发现：")
                digest.extend(
                    f"- [{item.get('dimension')}] {item.get('issue')}（改法：{item.get('recommendation')}）"
                    for item in previous_items[:CHAPTER_DIGEST_MAX_FINDINGS]
                )
            parts.append("\n".join(digest))
        return {
            "content": "\n\n".join(parts),
            "source_text_ref": f"chapter_assembled:{chapter.chapter_id}",
            "source_bundle_id": None,
        }

    def _current_author_draft(self, object_type: str, object_id: str) -> AuthorDraft | None:
        return self.session.execute(
            select(AuthorDraft)
            .where(
                AuthorDraft.object_type == object_type,
                AuthorDraft.object_id == object_id,
                AuthorDraft.status == "current",
            )
            .order_by(AuthorDraft.updated_at.desc(), AuthorDraft.draft_id.desc())
        ).scalars().first()

    def _require_scene(self, scene_id: str) -> SceneCard:
        return require_scene(self.session, scene_id)

    def _require_chapter(self, chapter_id: str) -> ChapterGoal:
        return require_chapter(self.session, chapter_id)

    def _require_patch_candidate(self, patch_id: str) -> PassagePatchCandidate:
        row = self.session.get(PassagePatchCandidate, patch_id)
        if row is None:
            raise DomainError("PASSAGE_PATCH_NOT_FOUND", "passage patch candidate not found", status_code=404)
        return row

    def _latest_preference_profile(self) -> AuthorPreferenceProfile | None:
        return self.session.execute(
            select(AuthorPreferenceProfile)
            .where(AuthorPreferenceProfile.scope_type == "global", AuthorPreferenceProfile.scope_ref_id == "global")
            .order_by(AuthorPreferenceProfile.created_at.desc(), AuthorPreferenceProfile.profile_id.desc())
        ).scalars().first()

    def _refresh_author_preference_profile(self, *, actor_ref: str) -> AuthorPreferenceProfile:
        decided = self.session.execute(
            select(PassagePatchCandidate)
            .where(PassagePatchCandidate.author_decision.in_(("accepted", "rejected")))
            .order_by(PassagePatchCandidate.created_at.asc(), PassagePatchCandidate.patch_id.asc())
        ).scalars().all()
        summary = _preference_summary(decided)
        profile = self._latest_preference_profile()
        if profile is None:
            profile = AuthorPreferenceProfile(
                profile_id="author_pref_global_global",
                scope_type="global",
                scope_ref_id="global",
                status="draft",
                runtime_eligible=0,
                summary_json=summary,
                source_patch_ids_json=[row.patch_id for row in decided],
                created_by=actor_ref or "writer_deep_review",
            )
            self.session.add(profile)
        else:
            profile.status = "draft"
            profile.runtime_eligible = 0
            profile.summary_json = summary
            profile.source_patch_ids_json = [row.patch_id for row in decided]
            profile.created_by = actor_ref or profile.created_by
        self._upsert_author_preference_review(profile, actor_ref=actor_ref)
        return profile

    def _upsert_author_preference_review(self, profile: AuthorPreferenceProfile, *, actor_ref: str) -> ReviewItem:
        review_id = f"review_{profile.profile_id}"
        summary = profile.summary_json or _empty_preference_summary()
        source_patch_ids = profile.source_patch_ids_json or []
        candidate_payload = {
            "profile_id": profile.profile_id,
            "scope_type": profile.scope_type,
            "scope_ref_id": profile.scope_ref_id,
            "summary": summary,
            "source_patch_ids": source_patch_ids,
        }
        review = self.session.get(ReviewItem, review_id)
        if review is None:
            review = ReviewItem(
                review_id=review_id,
                item_type="author_preference_profile",
                status="pending",
                candidate_text=json.dumps(summary, ensure_ascii=False, sort_keys=True),
                candidate_payload_json=candidate_payload,
                active_on_approve=1,
                materialize_status="pending",
            )
            self.session.add(review)
            return review
        review.item_type = "author_preference_profile"
        review.status = "pending"
        review.candidate_text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        review.candidate_payload_json = candidate_payload
        review.active_on_approve = 1
        review.materialize_status = "pending"
        review.approved_item_row_id = None
        review.approved_item_id = None
        return review


def _prompt_text(content: Any) -> str:
    """作者稿是 HTML：给模型看的是可见文字（段落之间空一行），否则它会把 <p> 引进证据里。"""

    text = str(content or "")
    if "<" not in text:
        return text
    return "\n\n".join(part for part in manuscript_paragraphs(text) if part.strip())


PASSAGE_MAX_FOCUS_PARAGRAPHS = 40
CHAPTER_DIGEST_EDGE_CHARS = 200
CHAPTER_DIGEST_MAX_FINDINGS = 6


def _passage_review_user_prompt(
    base_prompt: str,
    *,
    scope: dict[str, Any],
    about: dict[str, Any] | None,
    excerpt: str | None,
    question: str | None,
) -> str:
    focus = [int(index) + 1 for index in scope["focus"]]
    focus_label = f"{focus[0]}" if len(focus) == 1 else (f"{focus[0]}–{focus[-1]}" if focus == list(range(focus[0], focus[-1] + 1)) else ", ".join(str(index) for index in focus))
    coverage = (
        "the whole scene is shown: focus paragraphs are marked 【焦点段 N】, their neighbours 【上下文 N】, every other paragraph 【第 N 段】"
        if scope.get("whole_scene")
        else f"focus paragraphs are marked 【焦点段 N】, their neighbours 【上下文 N】; {int(scope.get('abbreviated') or 0)} far paragraphs are abbreviated to their opening and marked 【第 N 段·略】"
    )
    lines = [
        base_prompt,
        "",
        "## Passage Under Review",
        f"Focus paragraph{'s' if len(focus) > 1 else ''}: {focus_label} ({coverage}).",
    ]
    if excerpt:
        lines.append(f"Selected text: {excerpt}")
    if about is not None:
        lines.extend(
            [
                "",
                "## Finding To Verify",
                f"Source: {about.get('source')} · Dimension: {about.get('dimension')} ({about.get('label')}) · Severity: {about.get('severity')}",
                f"Issue: {about.get('issue')}",
                f"Suggested fix: {about.get('recommendation')}",
            ]
        )
        related = about.get("related") or {}
        if related.get("excerpt"):
            lines.append(f"Related passage (paragraph {int(related['paragraph_index']) + 1 if isinstance(related.get('paragraph_index'), int) else '?'}): {related['excerpt']}")
    else:
        lines.extend(["", "## Finding To Verify", "(none — judge the focus paragraphs on their own; verdict is no_finding unless you find something)"])
    lines.extend(
        [
            "",
            "## Cross-Paragraph Check",
            "Check the focus paragraphs against every other paragraph shown: a fact, object, time, place, injury, or who-knows-what that contradicts another paragraph; a beat, image or sentence the focus repeats from elsewhere; a setup elsewhere that the focus fails to pick up. Report such a finding with evidence_excerpt copied verbatim from a focus paragraph, related_excerpt copied verbatim (at most 80 characters) from the other paragraph, related_paragraph_index as the number in that paragraph's marker, and relation = contradiction | repetition | continuity. Findings that concern only the focus paragraphs leave these fields empty.",
        ]
    )
    if question:
        lines.extend(["", "## Author's Question", str(question)])
    return "\n".join(lines)


def _normalize_passage_review_output(payload: Any, *, has_finding: bool) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict not in PASSAGE_VERDICTS:
        verdict = "partly" if has_finding else "no_finding"
    if not has_finding and verdict in {"holds", "partly", "does_not_hold"}:
        verdict = "no_finding"
    findings = _normalize_findings(payload.get("findings"))
    for finding in findings:
        # 跨段发现：另一段的原话 + 标记里的段号（模型看到的是 1 起的序号，存 0 起的段索引）
        related_excerpt = str(finding.get("related_excerpt") or "").strip()
        finding["related_excerpt"] = related_excerpt
        raw_index = finding.get("related_paragraph_index")
        related_index: int | None = None
        if related_excerpt and raw_index not in (None, ""):
            try:
                related_index = int(raw_index) - 1
            except (TypeError, ValueError):
                related_index = None
        finding["related_paragraph_index"] = related_index if related_index is not None and related_index >= 0 else None
        relation = str(finding.get("relation") or "").strip().lower()
        finding["relation"] = relation if relation in PASSAGE_RELATION_KINDS else ("contradiction" if related_excerpt else "")
    return {
        "verdict": verdict,
        "assessment": str(payload.get("assessment") or "").strip(),
        "findings": findings,
        "rewrite_brief": str(payload.get("rewrite_brief") or "").strip(),
    }


def _previous_findings_by_scene(
    previous: WriterEvaluation | None,
    scenes: list[SceneCard],
    texts: list[Any],
) -> dict[str, list[dict[str, Any]]]:
    """上一轮通读的发现按「引文钉在哪一场」分组（钉不到任何一场的是章级发现，不在这里）。"""

    grouped: dict[str, list[dict[str, Any]]] = {}
    if previous is None:
        return grouped
    for item in previous.findings_json or []:
        if not isinstance(item, dict):
            continue
        excerpt = _compact_text(str(item.get("evidence_excerpt") or ""), 400)
        if not excerpt:
            continue
        for scene, text in zip(scenes, texts):
            if text.layer != "none" and locate_in_paragraphs(text.paragraphs, excerpt) is not None:
                grouped.setdefault(scene.scene_id, []).append(item)
                break
    return grouped


def _carry_previous_scene_findings(
    previous: WriterEvaluation | None,
    scenes: list[SceneCard],
    texts: list[Any],
    full_scene_ids: set[str],
) -> list[dict[str, Any]]:
    """只通读改过的场时，未改的场沿用上一轮的发现（带 ``carried_from``）；改过的场由模型重判，章级发现由模型重说。"""

    if previous is None:
        return []
    carried: list[dict[str, Any]] = []
    for scene_id, items in _previous_findings_by_scene(previous, scenes, texts).items():
        if scene_id in full_scene_ids:
            continue
        for item in items:
            carried.append({**{key: value for key, value in item.items() if key != "carried_from"}, "carried_from": str(item.get("carried_from") or previous.evaluation_id)})
    return carried


def _chapter_review_prompt_tail(
    scenes: list[SceneCard],
    texts: list[Any],
    full_scene_ids: set[str],
    previous: WriterEvaluation | None,
    carried: list[dict[str, Any]],
) -> str:
    full_numbers = [str(index) for index, scene in enumerate(scenes, start=1) if scene.scene_id in full_scene_ids]
    digest_numbers = [str(index) for index, (scene, text) in enumerate(zip(scenes, texts), start=1) if scene.scene_id not in full_scene_ids and text.layer != "none"]
    lines = [
        "## Read-Through Scope",
        f"This is an incremental read-through. Scenes {', '.join(full_numbers)} changed since the previous read-through and are shown in full under 【第 N 场 · 本次通读】: judge them completely. "
        f"Scenes {', '.join(digest_numbers) or '—'} did not change and appear only as 【第 N 场 · 未改 · 摘要】 (opening, ending, the previous read-through's findings on them); their findings are kept automatically — do not restate them, and report a finding on an unchanged scene only when a changed scene now contradicts or undercuts it (quote the changed scene as evidence). "
        "Judge the chapter as a whole again (promise, escalation, payoff, ending) with the changed scenes in place.",
    ]
    if previous is not None:
        located = {(str(item.get("dimension")), _compact_text(str(item.get("evidence_excerpt") or ""), 200)) for item in carried}
        chapter_level = [
            item
            for item in (previous.findings_json or [])
            if isinstance(item, dict)
            and (str(item.get("dimension")), _compact_text(str(item.get("evidence_excerpt") or ""), 200)) not in located
            and not _compact_text(str(item.get("evidence_excerpt") or ""), 200)
        ]
        if chapter_level:
            lines.extend(
                [
                    "",
                    "### Previous Chapter-Level Findings",
                    "These chapter-level findings came from the previous read-through. Re-issue each one that still holds (the wording may stay), drop the ones the changes resolved, add new ones:",
                ]
            )
            lines.extend(f"- [{item.get('dimension')}] {item.get('issue')}（改法：{item.get('recommendation')}）" for item in chapter_level[:CHAPTER_DIGEST_MAX_FINDINGS])
    return "\n".join(lines)


def _passage_patch_snapshot(
    *,
    payload: dict[str, Any],
    source_excerpt: str,
    issue_dimension: str,
    target_text_ref: str,
    source_draft: AuthorDraft | None,
    preference: AuthorPreferenceProfile | None,
    instruction: str | None = None,
    issue_note: str | None = None,
) -> dict[str, Any]:
    preference_summary = preference.summary_json if preference is not None else {}
    inline_digests = {
        "scene_summary": json.dumps(
            {
                "object_type": payload.get("object_type"),
                "object_id": payload.get("object_id"),
                "target_text_ref": target_text_ref,
                "source_excerpt": source_excerpt,
                "issue_dimension": issue_dimension,
                "instruction": instruction or "",
                "issue_note": issue_note or "",
                "source_draft_id": source_draft.draft_id if source_draft is not None else None,
                "source_draft_context": _compact_text(source_draft.content if source_draft is not None else source_excerpt, 1200),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    }
    if preference is not None:
        inline_digests["style_profile"] = json.dumps(
            {
                "profile_id": preference.profile_id,
                "kind": "approved_author_preference_profile",
                "summary": preference_summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    return {
        "contract_version": "WRITER_PASSAGE_PATCH_SOURCE_v1",
        "stage_allowlist_name": "writer_passage_patch",
        "scene_id": _optional_text(payload, "scene_id") or "",
        "chapter_id": _optional_text(payload, "chapter_id") or "",
        "source_version_refs": {
            "target_text_ref": target_text_ref,
            "source_draft_id": source_draft.draft_id if source_draft is not None else None,
            "author_preference_profile_id": preference.profile_id if preference is not None else None,
        },
        "resolved_ref_ids": {},
        "ordered_injections": [
            {"slot": "passage_patch_target", "ref_id": target_text_ref, "digest_key": "scene_summary"},
            {
                "slot": "author_preference_profile",
                "ref_id": preference.profile_id if preference is not None else "",
                "digest_key": "style_profile",
            },
        ],
        "inline_digests": inline_digests,
    }


def _passage_patch_user_prompt(
    base_prompt: str,
    *,
    source_excerpt: str,
    issue_dimension: str,
    target_text_ref: str,
    source_draft: AuthorDraft | None,
    preference: AuthorPreferenceProfile | None,
    instruction: str | None = None,
    issue_note: str | None = None,
) -> str:
    preference_summary = preference.summary_json if preference is not None else {}
    target_lines = [
        "## Passage Patch Target",
        f"Target Text Ref: {target_text_ref}",
        f"Issue Dimension: {issue_dimension}",
    ]
    if issue_note:
        target_lines.append(f"Diagnosed Issue: {issue_note}")
    if instruction:
        target_lines.append(f"Author Instruction: {instruction}")
    return "\n".join(
        [
            base_prompt,
            "",
            *target_lines,
            "Source Excerpt:",
            source_excerpt,
            "",
            "## Current Author Draft Context",
            _compact_text(source_draft.content if source_draft is not None else "", 1400),
            "",
            "## Approved Author Preference Profile",
            json.dumps(preference_summary, ensure_ascii=False, sort_keys=True) if preference is not None else "{}",
        ]
    )


def _normalize_patch_output(
    payload: Any,
    *,
    source_excerpt: str,
    issue_dimension: str,
    target_text_ref: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "replacement_options": _replacement_options(source_excerpt, issue_dimension),
            "rationale": "fallback because patch response was not an object",
        }
    patches = payload.get("patches")
    if not isinstance(patches, list) or not patches:
        return {
            "replacement_options": _replacement_options(source_excerpt, issue_dimension),
            "rationale": str(payload.get("rationale") or "fallback because patch list was empty"),
        }
    options: list[dict[str, Any]] = []
    for index, patch in enumerate(patches[:3], start=1):
        if not isinstance(patch, dict):
            continue
        replacement_text = patch.get("replacement_text")
        if not isinstance(replacement_text, str) or not replacement_text.strip():
            continue
        changed_dimensions = patch.get("changed_dimensions") if isinstance(patch.get("changed_dimensions"), list) else []
        dimensions = [str(item) for item in changed_dimensions if isinstance(item, str) and item.strip()]
        tone = str(patch.get("tone") or (dimensions[0] if dimensions else issue_dimension))
        options.append(
            {
                "option_id": f"option_llm_{index}",
                "tone": tone,
                "label": str(patch.get("label") or f"版本 {index}"),
                "replacement_text": replacement_text.strip(),
                "changed_dimensions": dimensions or [issue_dimension],
                "why_it_helps": str(patch.get("why_it_helps") or patch.get("reason") or ""),
                "target_text_ref": str(patch.get("target_text_ref") or target_text_ref),
                "source_excerpt": str(patch.get("source_excerpt") or source_excerpt),
                "patch_type": str(patch.get("patch_type") or "replace_excerpt"),
            }
        )
    if not options:
        # 模型完全没给可用候选 → 确定性兜底(3 个)，沿用既有语义（离线测试覆盖）
        options = _replacement_options(source_excerpt, issue_dimension)
    elif len(options) < 2:
        # Fix B：模型仅回 1 个合法候选时，用确定性变体补足到 ≥2，保留「多选改写」UX 契约。
        # 诚实纪律：补足项 option_id 带 topup 前缀 + is_fallback_topup 标记可区分、不冒充模型产物；
        # 且补足时 rationale 不得整串落入前端 /offline deterministic/i 正则
        # （否则 ws-writer.jsx 会把整次真实改写误判为「模型不可用」而整体丢弃）。
        existing = {opt["replacement_text"].strip() for opt in options}
        for variant in _replacement_options(source_excerpt, issue_dimension):
            if len(options) >= 2:
                break
            text = str(variant.get("replacement_text") or "").strip()
            if not text or text in existing:
                continue
            options.append(
                {
                    "option_id": f"option_topup_{variant['option_id']}",
                    "tone": str(variant.get("tone") or issue_dimension),
                    "label": f"{variant.get('label') or '备选'}（确定性补足）",
                    "replacement_text": text,
                    "changed_dimensions": [*(variant.get("changed_dimensions") or []), "deterministic_topup"],
                    "why_it_helps": str(variant.get("why_it_helps") or ""),
                    "target_text_ref": target_text_ref,
                    "source_excerpt": source_excerpt,
                    "patch_type": "replace_excerpt",
                    "is_fallback_topup": True,
                }
            )
            existing.add(text)

    rationale = str(payload.get("rationale") or "")
    if any(opt.get("is_fallback_topup") for opt in options):
        rationale = (rationale + "（模型仅返回单个候选，已用确定性变体补足候选数；标注「确定性补足」的选项为非模型产物。）").strip()
    return {
        "replacement_options": options,
        "rationale": rationale,
    }


def _replacement_options(source_excerpt: str, issue_dimension: str) -> list[dict[str, Any]]:
    compressed = source_excerpt.strip().rstrip("。！？")
    return [
        {
            "option_id": "option_shorter",
            "tone": "shorter",
            "label": "更短",
            "replacement_text": f"{compressed}。",
            "changed_dimensions": [issue_dimension, "information_rhythm"],
            "why_it_helps": "压掉解释余量，让动作和停顿自己承担压力。",
        },
        {
            "option_id": "option_sharper",
            "tone": "sharper",
            "label": "更狠",
            "replacement_text": f"{compressed}。她没有补充理由，只把证据袋按进掌心。",
            "changed_dimensions": [issue_dimension, "relationship_tension"],
            "why_it_helps": "让角色拒绝解释，把锋利感放进动作后果。",
        },
        {
            "option_id": "option_subtler",
            "tone": "subtler",
            "label": "更含蓄",
            "replacement_text": f"{compressed}。话音落下后，她先看了一眼门缝。",
            "changed_dimensions": [issue_dimension, "dialogue_subtext"],
            "why_it_helps": "把明说转为观察和回避，保留读者自行判断的空间。",
        },
    ]


def _compact_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    head = max(0, limit // 2)
    tail = max(0, limit - head)
    return f"{text[:head]}\n...\n{text[-tail:]}"


def _candidate_category(payload: dict[str, Any], issue_dimension: str) -> str:
    explicit = _optional_text(payload, "candidate_category")
    if explicit in PATCH_CATEGORIES:
        return explicit
    return candidate_category_for_dimension(issue_dimension)


def _target_range(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("start", "end"):
        if isinstance(value.get(key), int):
            result[key] = value[key]
    unit = value.get("unit")
    if isinstance(unit, str) and unit.strip():
        result["unit"] = unit.strip()
    return result or None


def _revision_strategy(payload: dict[str, Any], issue_dimension: str, *, instruction: str | None = None) -> str:
    explicit = _optional_text(payload, "revision_strategy")
    if explicit:
        return explicit
    if instruction:
        return instruction
    category = _candidate_category(payload, issue_dimension)
    return {
        "dialogue_rewrite": "用反问、截断或沉默替代解释性对白。",
        "action_replace": "用物件移动、身体位置或关系后果替代模板动作。",
        "ending_pressure": "把结尾改成推动下一场的硬动作或视觉钩子。",
        "information_reorder": "让信息通过行动分段释放，避免一次性说明。",
        "de_model_voice": "删掉抽象总结和泛化比喻，保留具体动作压力。",
    }.get(category, f"围绕 {issue_dimension} 做局部深改。")


def _preference_tags(payload: dict[str, Any], issue_dimension: str, *, instruction: str | None = None) -> list[str]:
    raw = payload.get("preference_tags")
    if isinstance(raw, list):
        tags = [str(item).strip() for item in raw if str(item).strip()]
        if tags:
            return _dedupe(tags)[:8]
    category = _candidate_category(payload, issue_dimension)
    defaults = {
        "dialogue_rewrite": ["少解释", "对白更短"],
        "action_replace": ["动作承压", "少模板手势"],
        "ending_pressure": ["结尾硬钩子"],
        "information_reorder": ["信息分段释放"],
        "de_model_voice": ["去模型腔", "少抽象总结"],
    }
    if category in defaults:
        return defaults[category][:8]
    # 自由改写：偏好画像记作者说的那句话（不是维度键）
    if instruction and issue_dimension == AUTHOR_INSTRUCTION_DIMENSION:
        return [instruction[:40]]
    return [issue_dimension][:8]


# 供旧调用方 / 测试按名字取：场景形态推断现在只看模型给的 scene_form（scene_diagnosis）
_scene_form_from_findings = scene_form_from_findings


def _normalize_deep_review_output(payload: dict[str, Any]) -> dict[str, Any]:
    findings = _normalize_findings(payload.get("findings"))
    scores = _normalize_scores(payload.get("scores"))
    overall_score = _optional_score(payload.get("overall_score"))
    if overall_score is None:
        overall_score = round(mean(scores.values()), 2) if scores else None
    revision_brief = _normalize_revision_brief(payload.get("revision_brief"), findings)
    normalized_lenses = _normalize_lens_evaluations(payload.get("lens_evaluations"), findings)
    requires_human_review = bool(payload.get("requires_human_review"))
    if any(finding.get("severity") == "blocking" for finding in findings):
        requires_human_review = True
    return {
        "overall_score": overall_score,
        "scores": scores,
        "findings": findings,
        "revision_brief": revision_brief,
        "requires_human_review": requires_human_review,
        "lens_evaluations": normalized_lenses,
    }


def _normalize_lens_evaluations(value: Any, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 模型直出的分组是权威来源（校验后采用）;缺失的镜头再从顶层 findings 的
    # lens 标签重建——不把顶层 findings 并入模型已给出的条目,否则同一条发现
    # 会在两处同时出现时被重复计入。
    findings_by_lens: dict[str, list[dict[str, Any]]] = {lens: [] for lens in DEEP_REVIEW_LENSES}
    for finding in findings:
        findings_by_lens[_coerce_lens(finding.get("lens")) or "story"].append(finding)
    by_lens: dict[str, dict[str, Any]] = {}
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        lens = _coerce_lens(item.get("lens"))
        if lens is None:
            continue
        lens_findings = _normalize_findings(item.get("findings"))
        for finding in lens_findings:
            finding["lens"] = lens
        entry = by_lens.get(lens)
        if entry is None:
            by_lens[lens] = {
                "lens": lens,
                "overall_score": _optional_score(item.get("overall_score")),
                "scores": _normalize_scores(item.get("scores")),
                "findings": lens_findings,
                "revision_brief": _normalize_revision_brief(item.get("revision_brief"), lens_findings),
            }
        else:
            entry["findings"].extend(lens_findings)
            entry["revision_brief"].extend(_revision_brief_from_findings(lens_findings))
    for lens in DEEP_REVIEW_LENSES:
        lens_findings = findings_by_lens[lens]
        if lens in by_lens or not lens_findings:
            continue
        by_lens[lens] = {
            "lens": lens,
            "scores": _scores_for_findings(lens_findings),
            "findings": lens_findings,
            "revision_brief": _revision_brief_from_findings(lens_findings),
        }
    return [by_lens[lens] for lens in DEEP_REVIEW_LENSES if lens in by_lens]


def _coerce_lens(value: Any) -> str | None:
    lens = str(value or "").strip().lower()
    return lens if lens in DEEP_REVIEW_LENSES else None


def _normalize_scores(value: Any) -> dict[str, float]:
    scores = {dimension: 0.78 for dimension in LITERARY_REVISION_DIMENSIONS}
    if isinstance(value, dict):
        for dimension, raw_score in value.items():
            if dimension not in scores:
                continue
            score = _optional_score(raw_score)
            if score is not None:
                scores[dimension] = score
    return scores


def _optional_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, round(score, 2)))


def _normalize_findings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    findings: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        finding = dict(item)
        severity = str(finding.get("severity") or finding.get("classification") or "revision")
        if severity not in {"blocking", "revision", "taste", "ignore_ok"}:
            severity = "revision"
        finding["severity"] = severity
        finding["classification"] = str(finding.get("classification") or severity)
        finding["lens"] = _coerce_lens(finding.get("lens")) or "story"
        finding["dimension"] = str(finding.get("dimension") or "choice_pressure")
        finding["issue"] = str(finding.get("issue") or "")
        finding["recommendation"] = str(finding.get("recommendation") or "")
        finding["evidence_excerpt"] = str(finding.get("evidence_excerpt") or "")
        finding["evidence_location"] = str(finding.get("evidence_location") or "source text")
        finding["why_it_matters"] = str(finding.get("why_it_matters") or "")
        finding["scene_form"] = str(finding.get("scene_form") or "plot_scene")
        findings.append(finding)
    return findings


def _normalize_revision_brief(value: Any, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items = [dict(item) for item in value if isinstance(item, dict)]
        if items:
            return items
    return _revision_brief_from_findings(findings)


def _scores_for_findings(findings: list[dict[str, Any]]) -> dict[str, float]:
    scores = {dimension: 0.78 for dimension in LITERARY_REVISION_DIMENSIONS}
    for finding in findings:
        dimension = finding.get("dimension")
        if dimension not in scores:
            continue
        if finding.get("severity") == "blocking":
            scores[dimension] = min(scores[dimension], 0.42)
        elif finding.get("severity") == "revision":
            scores[dimension] = min(scores[dimension], 0.58)
        elif finding.get("severity") == "taste":
            scores[dimension] = min(scores[dimension], 0.72)
    return scores


def _revision_brief_from_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    brief: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("severity") == "ignore_ok":
            priority = "optional"
        elif finding.get("severity") == "taste":
            priority = "low"
        elif finding.get("severity") == "blocking":
            priority = "high"
        else:
            priority = "medium"
        brief.append(
            {
                "dimension": finding.get("dimension"),
                "classification": finding.get("severity"),
                "action": finding.get("recommendation"),
                "priority": priority,
                "evidence_excerpt": finding.get("evidence_excerpt", ""),
            }
        )
    return brief


def _evidence_spans(content: str, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for finding in findings:
        excerpt = str(finding.get("evidence_excerpt") or "")
        if not excerpt:
            continue
        start = content.find(excerpt)
        if start < 0:
            continue
        spans.append({"text": excerpt, "start": start, "end": start + len(excerpt)})
        if len(spans) >= 8:
            break
    return spans


def _preference_summary(rows: list[PassagePatchCandidate]) -> dict[str, list[str]]:
    preferred: list[str] = []
    rejected: list[str] = []
    ai_traces: list[str] = []
    preferred_categories: list[str] = []
    rejected_categories: list[str] = []
    preference_tags: list[str] = []
    for row in rows:
        category_label = _category_label(row.candidate_category)
        if row.author_decision == "accepted":
            selected = _selected_option(row)
            tone = selected.get("tone") if selected else ""
            preferred_categories.append(category_label)
            preference_tags.extend(str(item) for item in (row.preference_tags_json or []) if str(item).strip())
            if tone == "sharper":
                preferred.append("偏好更锋利的局部改写，让动作代替解释。")
            elif tone == "subtler":
                preferred.append("偏好更含蓄的局部改写，保留读者判断空间。")
            elif tone == "shorter":
                preferred.append("偏好更短的句段，压缩解释余量。")
            else:
                preferred.append(f"偏好{category_label}：{row.revision_strategy or row.issue_dimension}。")
        elif row.author_decision == "rejected":
            rejected_categories.append(category_label)
            # Free-form author notes are audit evidence, not prompt instructions.
            # Convert the decision into a controlled label before publication.
            rejected.append(f"保留作者原句；拒绝自动应用{category_label}。")
        ai_traces.extend(term for term in _repeated_ai_trace_terms(row.source_excerpt) if term not in ai_traces)
    return {
        "preferred_revision_moves": _dedupe(preferred),
        "rejected_revision_moves": _dedupe(rejected),
        "preferred_patch_categories": _dedupe(preferred_categories),
        "rejected_patch_categories": _dedupe(rejected_categories),
        "preference_tags": _dedupe(preference_tags),
        "ai_trace_terms_to_watch": _dedupe(ai_traces),
        "runtime_policy": ["偏好摘要保持 draft；审核批准前不得进入运行 bundle。"],
    }


def _selected_option(row: PassagePatchCandidate) -> dict[str, Any] | None:
    for option in row.replacement_options_json or []:
        if option.get("option_id") == row.selected_option_id:
            return option
    return None


def _empty_preference_summary() -> dict[str, list[str]]:
    return {
        "preferred_revision_moves": [],
        "rejected_revision_moves": [],
        "preferred_patch_categories": [],
        "rejected_patch_categories": [],
        "preference_tags": [],
        "ai_trace_terms_to_watch": [],
        "runtime_policy": ["偏好摘要保持 draft；审核批准前不得进入运行 bundle。"],
    }


def _category_label(value: str | None) -> str:
    return {
        "dialogue_rewrite": "对白改写",
        "action_replace": "动作替换",
        "ending_pressure": "结尾重压",
        "information_reorder": "信息释放重排",
        "de_model_voice": "去模型腔",
        "local_patch": "局部深改",
    }.get(value or "", "局部深改")


def _repeated_ai_trace_terms(text: str) -> list[str]:
    watched = ("手指", "停顿", "幽蓝", "冷光", "低声", "盐霜", "泛着")
    return [term for term in watched if text.count(term) >= 2 or (term in {"幽蓝", "冷光", "盐霜"} and term in text)]


def _first_match(text: str, terms: tuple[str, ...]) -> str:
    for term in terms:
        index = text.find(term)
        if index >= 0:
            return text[max(0, index - 10) : index + len(term) + 16]
    return text[:40]


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("PASSAGE_PATCH_INVALID", f"{key} is required", status_code=400)
    return value.strip()


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
