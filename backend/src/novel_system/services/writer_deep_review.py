"""写作台的 LLM 评审节点（拒绝式：没有真实模型就 409 + author_action）：整场「AI 深评」、「AI 看这一处」的局部深评、
成稿中心「AI 通读本章」（整章或只通读改过的场），以及局部改写候选（``writer_deep_review_patches``）。评审结果落成
``WriterEvaluation`` 行，统一诊断（``scene_diagnosis``）把它们并进一场的发现。

拆分（审计 B05-18）：模型输出的归一在 ``writer_deep_review_output``，送审材料与提示词尾在 ``writer_deep_review_prompts``，
局部改写在 ``writer_deep_review_patches``（mixin）。这里保留服务、LLM 运行器与参考书注入——测试在这个模块上给
``LLMNodeRunner`` / ``inject_style_reference_prefix`` 打桩；测试与调用方从这里取的名字原样再导出。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ChapterGoal,
    FinalScene,
    PassagePatchCandidate,
    SceneCard,
    WriterEvaluation,
)
from novel_system.services.author_actions import llm_setup_action
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_json_normalized
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.llm_fail_closed import raise_llm_domain_error
from novel_system.services.llm_task_runner import (
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_design_context import render_scene_design_context
from novel_system.services.scene_diagnosis import (
    DiagnosisText,
    LITERARY_REVISION_PASSAGE_RUBRIC_ID,
    LITERARY_REVISION_RUBRIC_ID,
    PATCH_CATEGORIES,
    SceneDiagnosisService,
    locate_in_paragraphs,
    passage_scope,
    serialize_passage_review,
    serialize_patch_candidate as _serialize_patch_candidate,
)
from novel_system.services.scene_lookup import require_chapter, require_scene
from novel_system.services.scene_structure_brief import render_scene_structure_brief
from novel_system.services.style_prompt_injection import (
    inject_style_reference_prefix,
    resolve_style_scope,
)
from novel_system.services.style_reference.inject.request import PLAN_K
from novel_system.services.style_reference.policy import STYLE_REFERENCE_FAIL_CLOSED_ERRORS
from novel_system.services.writer_deep_review_output import (
    DEEP_REVIEW_LENSES,
    LITERARY_REVISION_DIMENSIONS,
    _lens_overall_score,
    _normalize_deep_review_output,
    _normalize_findings,
    _normalize_passage_review_output,
    _normalize_patch_output,
    _normalize_revision_brief,
    _normalize_scores,
    _optional_score,
)
from novel_system.services.writer_deep_review_patches import AUTHOR_INSTRUCTION_DIMENSION, PassagePatchMixin
from novel_system.services.writer_deep_review_prompts import (
    CHAPTER_DIGEST_EDGE_CHARS,
    CHAPTER_DIGEST_MAX_FINDINGS,
    PASSAGE_MAX_FOCUS_PARAGRAPHS,
    _carry_previous_scene_findings,
    _chapter_review_prompt_tail,
    _clip_middle,
    _passage_review_user_prompt,
    _previous_findings_by_scene,
    _prompt_text,
)
from novel_system.settings import get_settings


_LOGGER = logging.getLogger(__name__)
# LITERARY_REVISION_RUBRIC_ID / PATCH_CATEGORIES 定义在 scene_diagnosis，维度与镜头在
# writer_deep_review_output（这里再导出）；测试还从这里取 _normalize_deep_review_output / _normalize_patch_output /
# _optional_score。
__all__ = [
    "AUTHOR_INSTRUCTION_DIMENSION",
    "LITERARY_REVISION_RUBRIC_ID",
    "LITERARY_REVISION_PASSAGE_RUBRIC_ID",
    "LITERARY_REVISION_DIMENSIONS",
    "DEEP_REVIEW_LENSES",
    "PATCH_CATEGORIES",
    "WriterDeepReviewService",
    "_normalize_deep_review_output",
    "_normalize_patch_output",
    "_optional_score",
]


@dataclass(frozen=True)
class _WriterNode:
    """一个写作台 LLM 节点失败时回给前端的码（前端按码分支，不按状态码）。"""

    capability_code: str
    failure_code: str
    operation: str
    next_action: str


# 整场深评、局部深评与通读本章走同一个节点路由（writer_deep_review）；局部改写走自己的 writer_passage_patch
_WRITER_NODES: dict[str, _WriterNode] = {
    "writer_deep_review": _WriterNode(
        capability_code="WRITER_DEEP_REVIEW_LLM_REQUIRED",
        failure_code="WRITER_DEEP_REVIEW_LLM_FAILED",
        operation="writer deep review",
        next_action="configure_writer_deep_review_route_and_retry",
    ),
    "writer_passage_patch": _WriterNode(
        capability_code="WRITER_PASSAGE_PATCH_LLM_REQUIRED",
        failure_code="WRITER_PASSAGE_PATCH_LLM_FAILED",
        operation="writer passage patch",
        next_action="configure_writer_passage_patch_route_and_retry",
    ),
}


def _no_text_error(object_type: str, object_id: str) -> DomainError:
    subject = "这一场" if object_type == "scene" else "这一章的各场"
    return DomainError(
        "WRITER_DEEP_REVIEW_NO_TEXT",
        f"{subject}还没有正文，没有可评的字。先写一段（或起草一稿）再跑 AI 深评。",
        status_code=409,
        details={"object_type": object_type, "object_id": object_id},
    )


class WriterDeepReviewService(PassagePatchMixin):
    def __init__(self, session: Session, *, llm_client: Any | None = None, llm_runner: LLMNodeRunner | None = None) -> None:
        self.session = session
        self._llm_client = llm_client
        if llm_runner is not None:
            self._llm_runner = llm_runner

    # 提示词装配与 LLM 运行器第一次用到时才建：只读路径（工作台摘要、最新一版读取）一次都用不到，
    # 不该为它们读提示词与运行时配置。测试照旧可以直接给实例的这两个属性赋值。
    @cached_property
    def prompt_builder(self) -> PromptBuilder:
        return PromptBuilder()

    @cached_property
    def _llm_runner(self) -> LLMNodeRunner:
        return LLMNodeRunner(self.session, llm_client=self._llm_client)

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
            scene = require_scene(self.session, scene_id or object_id)
            chapter = require_chapter(self.session, scene.chapter_id)
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
        chapter = require_chapter(self.session, chapter_id or object_id)
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

    def run_scene_review(self, scene_id: str, actor_ref: str = "operator") -> dict[str, Any]:
        """「AI 深评」：对写作台看到的这一场正文跑一次 writer_deep_review 节点，返回统一诊断载荷。拒绝式：无模型即
        409；这一场还没有正文也 409（不拿空提示词去花钱，模型给的「发现」只能是编的）——写作台打开就建的空白作者稿
        也算没有正文（``diagnosis_text`` 按可见文字判）。"""

        scene = require_scene(self.session, scene_id)
        self._require_live_llm("writer_deep_review")
        text = SceneDiagnosisService(self.session).text_for_scene(scene)
        if text.layer == "none":
            raise _no_text_error("scene", scene.scene_id)
        self._create_deep_review_with_llm(
            object_type="scene",
            object_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            source=self._scene_source(text),
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

        chapter = require_chapter(self.session, chapter_id)
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
        self._require_live_llm("writer_deep_review")
        if all(text.layer == "none" for text in texts):
            raise _no_text_error("chapter", chapter.chapter_id)
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
        self._create_deep_review_with_llm(
            object_type="chapter",
            object_id=chapter.chapter_id,
            chapter_id=chapter.chapter_id,
            scene_id=None,
            source=source,
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
        scene = require_scene(self.session, scene_id)
        diagnosis_service = SceneDiagnosisService(self.session)
        text = diagnosis_service.text_for_scene(scene)
        if text.layer == "none":
            # 空白作者稿也在这里（diagnosis_text 按可见文字判）：不拿空段落去调模型
            raise DomainError("WRITER_PASSAGE_REVIEW_NO_TEXT", "这一场还没有正文，没有可看的段落。", status_code=409)
        about: dict[str, Any] | None = None
        focus: list[int] = []
        if signal_id:
            diagnosis = diagnosis_service.diagnose_scene(scene)
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
        if not any(text.paragraphs[index].strip() for index in focus):
            # 焦点段全是空段（编辑器里的空行）：没有可看的字，模型给的判断只能是编的（复核 P02b-R1）
            raise DomainError(
                "WRITER_PASSAGE_REVIEW_NO_TEXT",
                "要看的那一段是空的，没有可看的字。",
                status_code=409,
                details={"paragraph_count": len(text.paragraphs), "focus_paragraphs": focus},
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
        node_result = self._run_writer_node(
            "writer_deep_review",
            step="writer_passage_review",
            template="writer_passage_review",
            snapshot=snapshot,
            finish_user_prompt=lambda base: _passage_review_user_prompt(
                base,
                scope=scope,
                about=about,
                excerpt=excerpt,
                question=question,
            ),
            object_type="scene",
            object_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            scene_id=scene.scene_id,
            context_text=scope["focus_text"] or None,
            bundle_id=text.ref or f"writer_passage_review:{scene.scene_id}",
            run_scene_id=scene.scene_id,
            run_chapter_id=scene.chapter_id,
            execution_step_key=f"writer_passage_review:{scene.scene_id}:{focus[0]}-{focus[-1]}",
        )
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
            status="completed",
        )
        self.session.add(row)
        self.session.flush()
        payload = diagnosis_service.scene_payload(scene)
        payload["passage_review"] = serialize_passage_review(row, "current")
        return payload


    @staticmethod
    def serialize_patch_candidate(row: PassagePatchCandidate) -> dict[str, Any]:
        return _serialize_patch_candidate(row)

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
    ) -> None:
        """跑节点、写评审行（一行 aggregate + 各镜头行）；调用方随后按统一诊断回载荷，这里不另拼一份。

        深评是拒绝式的 LLM 节点：入口先查过有没有真实模型与正文，不再有本地词表兜底。（2026-09-22 之前这里有一条
        ``_diagnose_by_lens``：按「保护 / 真相 / 公开 / 隐藏」这类写死的词给出套话——那是退役演示故事的残留，
        对任何真实作品都在说谎。）
        """

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
        node_result = self._run_writer_node(
            "writer_deep_review",
            step="writer_deep_review",
            template="writer_deep_review",
            snapshot=snapshot,
            # 只通读改过的场时，用户消息尾部说明哪些场是全文、哪些只是摘要，并列出上次的章级发现
            finish_user_prompt=lambda base: base + (f"\n\n{prompt_tail}" if prompt_tail else ""),
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            # 2026-09-14 WP6.3：评审在参考作者的手笔下判断「复读 / 意象必要性 / 声音辨识度」
            context_text=str(source.get("content") or "") or None,
            bundle_id=source.get("source_text_ref") or f"writer_deep_review:{object_type}:{object_id}",
            run_scene_id=scene_id or object_id,
            run_chapter_id=chapter_id or object_id,
            execution_step_key=f"writer_deep_review:{object_type}:{object_id}",
        )
        normalized = _normalize_deep_review_output(node_result.response.structured_output or {})
        if extra_findings:
            # 未改的场沿用上一轮通读的发现（带 carried_from）；模型这次又说到同一处的，以模型的为准
            seen = {(str(item.get("dimension")), _clip_middle(str(item.get("evidence_excerpt") or ""), 200)) for item in normalized["findings"]}
            normalized["findings"] = list(normalized["findings"]) + [
                item for item in extra_findings if (str(item.get("dimension")), _clip_middle(str(item.get("evidence_excerpt") or ""), 200)) not in seen
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
            overall_score=normalized["overall_score"],
            scores_json=normalized["scores"],
            findings_json=normalized["findings"],
            revision_brief_json=normalized["revision_brief"],
            contract_field_refs_json=dict(meta) if meta else None,
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
                overall_score=_lens_overall_score(payload.get("overall_score")),
                scores_json=scores,
                findings_json=findings,
                revision_brief_json=_normalize_revision_brief(payload.get("revision_brief"), findings),
                status="completed",
            )
            self.session.add(row)
        self.session.flush()

    def _run_writer_node(
        self,
        node_id: str,
        *,
        step: str,
        template: str,
        snapshot: dict[str, Any],
        finish_user_prompt: Callable[[str], str],
        object_type: str,
        object_id: str,
        chapter_id: str | None,
        scene_id: str | None,
        context_text: str | None,
        bundle_id: str,
        execution_step_key: str,
        run_scene_id: str | None = None,
        run_chapter_id: str | None = None,
        ids_from_context: bool = False,
        style_role: str = "review",
        adjust_schema: Callable[[dict[str, Any]], None] | None = None,
    ) -> Any:
        """写作台三个 LLM 流程共用的一次节点调用（审计 B05-10）：装配模板 → 用户消息尾 → 参考书注入（按接收节点的
        路由判云策略，「仅本机」的书遇云端路由整次 409）→ 计费上下文 → 运行器；运行器的失败只在这里翻译一次
        （``llm_fail_closed``：缺模型能力 409 + ``capability_code``，上游模型失败 502 + ``failure_code``）。
        运行器记账用的场 / 章 id 各流程照旧：深评与局部深评显式给出，局部改写（``ids_from_context``）取计费上下文里的。"""

        prompt = self.prompt_builder.build(snapshot, template)
        if adjust_schema is not None:
            # 发出去的 schema 按这一次请求收紧（build 给的是模板 schema 的副本）
            adjust_schema(prompt["structured_schema"])
        user_prompt = finish_user_prompt(prompt["user_prompt"])
        prompt = self._inject_style_reference_prefix(
            prompt,
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            context_text=context_text,
            final_user_prompt=user_prompt,
            role=style_role,
        )
        context = self._llm_context(
            object_type=object_type,
            object_id=object_id,
            chapter_id=chapter_id,
            scene_id=scene_id,
            node_id=node_id,
            execution_step_key=execution_step_key,
        )
        try:
            return self._llm_runner.run(
                scene_id=context.scene_id if ids_from_context else run_scene_id,
                chapter_id=context.chapter_id if ids_from_context else run_chapter_id,
                bundle_id=bundle_id,
                bundle_hash=sha256_json_normalized(snapshot),
                node_id=node_id,
                step=step,
                prompt=prompt,
                user_prompt=user_prompt,
                execution_step_key=execution_step_key,
                context=context,
            )
        except LLMNodeExecutionError as exc:
            node = _WRITER_NODES[node_id]
            raise_llm_domain_error(
                exc,
                capability_code=node.capability_code,
                failure_code=node.failure_code,
                operation=node.operation,
                node_id=node_id,
                next_action=node.next_action,
                # 局部深评与整场深评是同一个节点，失败详情里只有 step 分得开是哪一个流程（B09-24）
                extra_details={"step": step},
            )

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
        role: str = "review",
    ) -> dict[str, Any]:
        """2026-09-14 保真修补（WP6.3）：深评 / 局部补丁按项目 / 场景的 active 绑定拿到 ``[STYLE_REFERENCE]``。

        2026-09-23 风格参考 v3：显式角色（深评 / 局部深评 ``review``、局部补丁 ``revise``），样例与文风卡按
        评审 / 改稿的口径渲染，不再拿起草口径的标题（J16）；窗数上限不变。

        场景对象按场景作用域（窗口按场景轮换），章对象按 project + global 作用域；样例窗口封顶
        :data:`~novel_system.services.style_reference.inject.request.PLAN_K`（评审与补丁只需少量样例定标准），被评 / 被改的文本作
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
                few_shot_k_cap=PLAN_K,
                role=role,
            )
            return injected if injected is not None else prompt
        except STYLE_REFERENCE_FAIL_CLOSED_ERRORS:
            # 云策略不许把这本书派生的任何东西送给这个节点：整次调用 409（带 author_action），不降级成没有参考的提示
            raise
        except Exception:  # noqa: BLE001 — 可选增强：注入失败只记日志，不阻断评审 / 补丁
            _LOGGER.warning(
                "writer style reference prefix skipped for %s %s",
                object_type,
                object_id,
                exc_info=True,
            )
            return prompt


    def _scene_source(self, text: DiagnosisText) -> dict[str, Any]:
        """整场深评送审的正文 = 诊断看的那一份（当前作者稿，其次运行终稿）；终稿记下它的 bundle。"""

        bundle_id = None
        if text.layer == "runtime_final_scene" and text.ref:
            final = self.session.get(FinalScene, text.ref.split(":", 1)[1])
            bundle_id = final.source_bundle_id if final is not None else None
        return {"content": text.content, "source_text_ref": text.ref, "source_bundle_id": bundle_id}

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



