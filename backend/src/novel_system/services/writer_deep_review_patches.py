"""写作台的局部改写候选（从 writer_deep_review 拆出）：生成（writer_passage_patch 节点）、采纳 / 放弃、唯一抄袭门、
采纳后记一条「像不像」读数。``PassagePatchMixin`` 由 ``WriterDeepReviewService`` 继承——提示词装配、LLM 运行器、
计费上下文与参考书注入都用服务上的那一份（测试照旧在 writer_deep_review 上打桩）。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from novel_system.db.models import AuthorDraft, PassagePatchCandidate
from novel_system.services.errors import DomainError
from novel_system.services.manuscript_html import plain_manuscript_text
from novel_system.services.reference_copy_gate import (
    check_reference_copy_for_scope,
    copy_block_author_action,
    introduced_copy,
)
from novel_system.services.scene_diagnosis import PATCH_CATEGORIES, candidate_category_for_dimension
from novel_system.services.style_prompt_injection import resolve_style_scope
from novel_system.services.style_reference.readings import STAGE_PATCHED, record_author_draft_reading
from novel_system.services.value_coercion import optional_text
from novel_system.services.writer_deep_review_output import WriterDeepReviewOutputError, _normalize_patch_output
from novel_system.services.writer_deep_review_prompts import _passage_patch_snapshot, _passage_patch_user_prompt

# 写作台工具条的自由改写（没有对应的诊断维度）用这个维度键；指令本身走 instruction 字段
AUTHOR_INSTRUCTION_DIMENSION = "author_instruction"


class PassagePatchMixin:
    def create_patch_candidate(self, payload: dict[str, Any], actor_ref: str = "operator") -> dict[str, Any]:
        """局部改写候选。

        ``issue_dimension`` 是维度键（一条诊断发现的 ``dimension``，或工具条自由改写的
        ``author_instruction``）；作者 / 诊断给的改法走 ``instruction``，发现的问题句走
        ``issue_note``，发现的 id 走 ``quality_signal_id``——修补类别、改写策略与标签按维度推，
        候选行记下的是「对白潜台词」而不是「润色」两个字（采纳 / 放弃只记在候选行上，不再推任何偏好画像：
        写作偏好学习已退役，批准 #6）。
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
        # 风格参考 v3（V1）：照抄参考书的改写选项不交给作者——写作台是在浏览器里把选中的改写插进正文的，
        # 采纳端点（accept）拦不住；一个选项都不剩才报错。
        patch_payload["replacement_options"] = self._copy_safe_options(
            payload, list(patch_payload.get("replacement_options") or [])
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
        self._record_accepted_patch_reading(row, selected_option_id)
        return {"candidate": self.serialize_patch_candidate(row)}

    def _record_accepted_patch_reading(self, row: PassagePatchCandidate, selected_option_id: str | None) -> None:
        """风格参考 v3（P5b）：写作台采纳局部改写之后记一条作者稿的「像不像」读数（source=author_draft，stage=patched）。

        改写是浏览器插进正文的，采纳端点到的时候作者稿可能还没保存：正文里已有这条改写 → 读作者稿本身；否则把原句
        换成改写再读；两样都对不上（作者又改了别处）→ 不记。只对场景稿；读数失败不影响采纳。"""
        scene_id = row.scene_id or (row.object_id if row.object_type == "scene" else None)
        if not scene_id:
            return
        options = [option for option in row.replacement_options_json or [] if isinstance(option, dict)]
        chosen = next(
            (option for option in options if str(option.get("option_id")) == str(selected_option_id or "")),
            options[0] if options else None,
        )
        replacement = str((chosen or {}).get("replacement_text") or "").strip()
        draft = self._source_draft(row.source_draft_id)
        if draft is None:
            draft = self.session.execute(
                select(AuthorDraft).where(
                    AuthorDraft.object_type == "scene",
                    AuthorDraft.object_id == scene_id,
                    AuthorDraft.status == "current",
                )
            ).scalars().first()
        if draft is None or not replacement:
            return
        current = plain_manuscript_text(draft.content or "")
        excerpt = str(row.source_excerpt or "").strip()
        if replacement in current:
            text = current
        elif excerpt and excerpt in current:
            text = current.replace(excerpt, replacement, 1)
        else:
            return
        record_author_draft_reading(
            self.session,
            scene_id=scene_id,
            text=text,
            stage=STAGE_PATCHED,
            draft_ref=f"passage_patch:{row.patch_id}:{selected_option_id or ''}",
        )

    def _option_copy_check(self, scope: Any, text: str, source_excerpt: str | None):
        """一个改写选项过唯一抄袭门；只算选项新带进来的命中（原句里本来就有的字不算，:func:`introduced_copy`）。"""
        if not text.strip():
            return None
        check = check_reference_copy_for_scope(self.session, text, scope=scope)
        check = introduced_copy(check, text, source_excerpt)
        return check if check.blocked else None

    def _copy_safe_options(self, payload: dict[str, Any], options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        object_type = _optional_text(payload, "object_type")
        object_id = _optional_text(payload, "object_id")
        scene_id = _optional_text(payload, "scene_id") or (object_id if object_type == "scene" else None)
        chapter_id = _optional_text(payload, "chapter_id") or (object_id if object_type == "chapter" else None)
        scope = resolve_style_scope(self.session, scene_id=scene_id, chapter_id=chapter_id)
        source_excerpt = _optional_text(payload, "source_excerpt")
        kept: list[dict[str, Any]] = []
        first_block = None
        for option in options:
            text = str(option.get("replacement_text") or "") if isinstance(option, dict) else ""
            check = self._option_copy_check(scope, text, source_excerpt)
            if check is not None:
                first_block = first_block or check
                continue
            kept.append(option)
        if not kept and first_block is not None:
            raise DomainError(
                "SOURCE_SAFETY_BLOCKED",
                "every generated rewrite copies the bound reference book and was discarded — ask again",
                status_code=409,
                details={
                    "reference_copy": first_block.audit(),
                    "author_action": copy_block_author_action(
                        first_block,
                        target_view="writer",
                        target_ref=f"{object_type}:{object_id}",
                        subject="这条改写",
                    ),
                },
            )
        return kept

    def _require_patch_copy_safe(self, row: PassagePatchCandidate, selected_option_id: str | None) -> None:
        """作者采纳局部改写之前过唯一抄袭门（风格参考 v3 V1）：采纳的那个选项（没点名就查全部选项）与绑定的参考书
        连续 ≥12 字相同、或含受保护专名 → 409，候选不改状态。位置是选项文字里的第几字，不印参考原文。"""

        options = [option for option in row.replacement_options_json or [] if isinstance(option, dict)]
        if selected_option_id:
            options = [option for option in options if str(option.get("option_id")) == selected_option_id]
        scene_id = row.scene_id or (row.object_id if row.object_type == "scene" else None)
        chapter_id = row.chapter_id or (row.object_id if row.object_type == "chapter" else None)
        scope = resolve_style_scope(self.session, scene_id=scene_id, chapter_id=chapter_id)
        # 每个选项单独查（拼在一起查，两个选项的交界处会被规范化拼出假的 12 字元）
        check = None
        for option in options:
            check = self._option_copy_check(scope, str(option.get("replacement_text") or ""), row.source_excerpt)
            if check is not None:
                break
        if check is None:
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
        return {"candidate": self.serialize_patch_candidate(row)}

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
        snapshot = _passage_patch_snapshot(
            payload=payload,
            source_excerpt=source_excerpt,
            issue_dimension=issue_dimension,
            target_text_ref=target_text_ref,
            source_draft=source_draft,
            instruction=instruction,
            issue_note=issue_note,
        )
        object_type = _required_text(payload, "object_type")
        object_id = _required_text(payload, "object_id")
        node_result = self._run_writer_node(
            "writer_passage_patch",
            step="writer_passage_patch",
            template="writer_passage_patch",
            snapshot=snapshot,
            finish_user_prompt=lambda base: _passage_patch_user_prompt(
                base,
                source_excerpt=source_excerpt,
                issue_dimension=issue_dimension,
                target_text_ref=target_text_ref,
                source_draft=source_draft,
                instruction=instruction,
                issue_note=issue_note,
            ),
            object_type=object_type,
            object_id=object_id,
            chapter_id=_optional_text(payload, "chapter_id"),
            scene_id=_optional_text(payload, "scene_id"),
            # 2026-09-14 WP6.3：局部补丁在参考作者的手笔下改句（k≤3 样例窗口，作者稿作选窗上下文）
            context_text=(source_draft.content if source_draft is not None else source_excerpt) or None,
            style_role="revise",
            bundle_id=snapshot["source_version_refs"]["target_text_ref"] or "writer_passage_patch",
            ids_from_context=True,
            execution_step_key=f"writer_passage_patch:{object_type}:{object_id}",
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

    def _source_draft(self, source_draft_id: str | None) -> AuthorDraft | None:
        if not source_draft_id:
            return None
        return self.session.get(AuthorDraft, source_draft_id)

    def _require_patch_candidate(self, patch_id: str) -> PassagePatchCandidate:
        row = self.session.get(PassagePatchCandidate, patch_id)
        if row is None:
            raise DomainError("PASSAGE_PATCH_NOT_FOUND", "passage patch candidate not found", status_code=404)
        return row


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
    # 自由改写：标签记作者说的那句话（不是维度键）
    if instruction and issue_dimension == AUTHOR_INSTRUCTION_DIMENSION:
        return [instruction[:40]]
    return [issue_dimension][:8]


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("PASSAGE_PATCH_INVALID", f"{key} is required", status_code=400)
    return value.strip()


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    return optional_text(payload.get(key))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
