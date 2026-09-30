"""写作台「AI 续写」：三条走向不同的续写候选——模型生成、风格参考前缀、唯一抄袭门、新一组替换旧候选（批准 #7）。

测试替换模型调用时 patch 本模块的 ``LLMNodeRunner``；风格前缀的注入 / 作用域解析也在本模块里解析
（``inject_style_reference_prefix`` / ``resolve_style_scope``）。
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import update

from novel_system.db.models import AuthorDraft, AuthorDraftProposal
from novel_system.services.author_drafts.store import _optional_text
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_json_normalized, sha256_text
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.llm_fail_closed import raise_llm_domain_error
from novel_system.services.llm_task_runner import (
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.manuscript_html import plain_manuscript_text
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.reference_copy_gate import (
    check_reference_copy_for_scope,
    copy_block_author_action,
    introduced_copy,
)
from novel_system.services.style_prompt_injection import (
    PLACEMENT_USER_TAIL,
    apply_style_user_tail,
    inject_style_reference_prefix,
    resolve_style_scope,
)
from novel_system.services.style_reference.policy import STYLE_REFERENCE_FAIL_CLOSED_ERRORS

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_LOGGER = logging.getLogger("novel_system.services.author_drafts")
#: 写作台「AI 续写」：一次生成三条走向不同的续写候选（同一个幂等意图）。这是作者稿建议唯一还在用的生成方式——
#: 采纳 / 放弃 / 对比 / 单条生成 / 列表这几个接口界面从没调过，已删（批准 #7）；候选由前端插进正文、走普通保存。
CONTINUATION_VARIANTS_MODE = "continuation_variants"
CONTINUATION_VARIANT_DIRECTIONS = (
    ("action", "候选方向：优先用动作推进下一拍，避免解释和总结。"),
    ("relationship", "候选方向：优先增加人物关系压力，用反应、停顿或选择推进。"),
    ("suspense", "候选方向：优先释放一个新信息或悬念钩子，但不要越过下一拍。"),
)


class AuthorDraftProposalsMixin:
    session: "Session"

    def _llm_context_for_target(
        self,
        runner: LLMNodeRunner,
        target: dict[str, Any],
        *,
        node_id: str,
        step: str,
        execution_step_key: str,
    ) -> LLMCallContext:
        execution_id = current_llm_execution_id()
        common = {
            "scope_id": target["object_id"],
            "project_id": target["project_id"],
            "node_id": node_id,
            "step": step,
            "execution_id": execution_id,
            "execution_step_key": execution_step_key if execution_id is not None else None,
            "provider_execution_mode": runner.provider_execution_mode,
        }
        return LLMCallContext(
            scope_type="scene",
            scene_id=target["scene_id"],
            chapter_id=target["chapter_id"],
            **common,
        )

    def generate_proposal_set(
        self,
        draft_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
    ) -> dict[str, Any]:
        """写作台「AI 续写」：三条走向不同的续写候选（动作 / 关系 / 悬念），同一个幂等意图里一次生成。

        照抄参考书的那几条在生成时就筛掉、从不落库；一条都不剩才报错（写作台在浏览器里插入候选，没有采纳端点可拦）。
        这一组至少留下一条之后，同一份作者稿上一组还开着的候选标成「已替换」（批准 #7）——只在全部模型调用结束之后改：
        记账会在两次调用之间提交会话，提前改的话，后面的调用失败时上一组也回不来了。
        """
        draft = self._require_draft(draft_id)
        request_payload = payload or {}
        mode = _optional_text(request_payload, "mode") or CONTINUATION_VARIANTS_MODE
        if mode != CONTINUATION_VARIANTS_MODE:
            raise DomainError(
                "AUTHOR_DRAFT_PROPOSAL_MODE_UNSUPPORTED",
                "only continuation_variants proposals are generated",
                status_code=400,
                details={"mode": mode, "supported_modes": [CONTINUATION_VARIANTS_MODE]},
            )
        instruction = _optional_text(request_payload, "instruction")
        target = self._target_payload(draft.object_type, draft.object_id)
        proposals: list[AuthorDraftProposal | _CopyBlockedProposal] = []
        for slot, direction in CONTINUATION_VARIANT_DIRECTIONS:
            proposals.append(self._create_proposal(
                draft,
                target=target,
                proposal_type="continuation",
                instruction="\n".join(part for part in (instruction, direction) if part),
                proposal_source=f"writer_room_continuation_variants:{slot}",
                proposal_kind="continuation",
                actor_ref=actor_ref,
            ))
        blocked_checks = [item.check for item in proposals if isinstance(item, _CopyBlockedProposal)]
        kept = [item for item in proposals if not isinstance(item, _CopyBlockedProposal)]
        if not kept and blocked_checks:
            self._raise_proposal_copy_blocked(draft, blocked_checks[0], proposal_ids=[])
        self.session.flush()
        self._supersede_open_proposals(draft, keep_ids=[row.proposal_id for row in kept])
        response = {"draft_id": draft.draft_id, "mode": mode, "proposals": [self.serialize_proposal(row) for row in kept]}
        if blocked_checks:
            response["reference_copy_blocked_count"] = len(blocked_checks)
        return response

    def _supersede_open_proposals(self, draft: AuthorDraft, *, keep_ids: list[str]) -> None:
        """同一份作者稿上还开着的旧候选标成「已替换」（刚生成的这一组除外）。"""
        if not keep_ids:
            return
        self.session.execute(
            update(AuthorDraftProposal)
            .where(
                AuthorDraftProposal.draft_id == draft.draft_id,
                AuthorDraftProposal.status == "candidate",
                AuthorDraftProposal.proposal_id.not_in(keep_ids),
            )
            .values(status="superseded", merge_status="superseded")
            .execution_options(synchronize_session=False)
        )

    def _create_proposal(
        self,
        draft: AuthorDraft,
        *,
        target: dict[str, Any],
        proposal_type: str,
        instruction: str | None,
        proposal_source: str,
        proposal_kind: str,
        actor_ref: str,
    ) -> "AuthorDraftProposal | _CopyBlockedProposal":
        """建一条建议。模型写的建议先过唯一抄袭门：照抄参考书 → 返回 :class:`_CopyBlockedProposal`，不加进会话
        （LLM 记账会在调用之间提交调用方的会话，加进去再 expunge 撤不回已提交的行）。"""
        generated = self._generate_proposal_content(
            draft,
            target=target,
            proposal_type=proposal_type,
            instruction=instruction,
        )
        proposal = AuthorDraftProposal(
            proposal_id=f"author_draft_proposal_{draft.object_type}_{draft.object_id}_{uuid.uuid4().hex[:10]}",
            draft_id=draft.draft_id,
            object_type=draft.object_type,
            object_id=draft.object_id,
            proposal_type=proposal_type,
            proposal_source=proposal_source,
            content=generated["content"],
            rationale=generated.get("rationale") or _proposal_rationale(target=target, instruction=instruction),
            source_llm_call_id=generated.get("source_llm_call_id"),
            before_text_hash=sha256_text(draft.content or ""),
            proposal_kind=proposal_kind,
            merge_status="pending",
            status="candidate",
            created_by=actor_ref or "author_draft_proposal",
        )
        copy = self._proposal_copy_check(draft, proposal)
        if copy is not None:
            return _CopyBlockedProposal(copy)
        self.session.add(proposal)
        return proposal

    def _generate_proposal_content(
        self,
        draft: AuthorDraft,
        *,
        target: dict[str, Any],
        proposal_type: str,
        instruction: str | None,
    ) -> dict[str, str | None]:
        target_for_prompt = {
            **target,
            "proposal_instruction": instruction or "",
        }
        snapshot = _proposal_generate_snapshot(draft, target=target_for_prompt, proposal_type=proposal_type)
        prompt = PromptBuilder().build(snapshot, "author_proposal_generate")
        user_prompt = _proposal_generate_user_prompt(prompt["user_prompt"], draft=draft, target=target_for_prompt)
        # 续写是正文：有风格绑定就拿到完整 k 的 [STYLE_REFERENCE]（2026-09-14 WP6.3），样例放到 user 消息末尾（2026-09-22）
        prompt = self._inject_style_reference_prefix(
            prompt,
            target,
            context_text=draft.content or None,
            final_user_prompt=user_prompt,
        )
        user_prompt = apply_style_user_tail(prompt, user_prompt)
        bundle_hash = sha256_json_normalized(snapshot)
        runner = LLMNodeRunner(self.session)
        execution_step_key = f"author_proposal_generate:{draft.draft_id}:{proposal_type}"
        context = self._llm_context_for_target(
            runner,
            target,
            node_id="author_proposal_generate",
            step="author_proposal_generate",
            execution_step_key=execution_step_key,
        )
        try:
            node_result = runner.run(
                scene_id=target.get("scene_id") or target.get("project_id") or draft.object_id,
                chapter_id=target.get("chapter_id") or target.get("project_id") or draft.object_id,
                bundle_id=f"author_draft:{draft.draft_id}:proposal:{proposal_type}",
                bundle_hash=bundle_hash,
                node_id="author_proposal_generate",
                step="author_proposal_generate",
                prompt=prompt,
                user_prompt=user_prompt,
                source_draft_row_id=draft.draft_id,
                source_draft_content=draft.content,
                execution_step_key=execution_step_key,
                context=context,
            )
        except LLMNodeExecutionError as exc:
            raise_llm_domain_error(
                exc,
                capability_code="AUTHOR_PROPOSAL_LLM_NOT_CONFIGURED",
                failure_code="AUTHOR_PROPOSAL_GENERATE_FAILED",
                operation="author proposal generation",
                node_id="author_proposal_generate",
                next_action="configure_author_proposal_route_and_retry",
            )
        normalized = _normalize_proposal_payload(
            node_result.response.structured_output,
            draft=draft,
            target=target,
            proposal_type=proposal_type,
            instruction=instruction,
        )
        normalized["source_llm_call_id"] = node_result.llm_call_id
        return normalized

    def _inject_style_reference_prefix(
        self,
        prompt: dict[str, Any],
        target: dict[str, Any],
        *,
        context_text: str | None,
        final_user_prompt: str,
    ) -> dict[str, Any]:
        """2026-09-14 保真修补（WP6.3）：作者稿建议按项目 / 场景的 active 绑定拿到 ``[STYLE_REFERENCE]``。

        场景稿按场景作用域（scene > character > project > global，窗口按场景轮换），整章稿 /
        项目稿按 project + global 作用域；完整 k 的样例窗口（这是要写正文的节点），作者的当前
        稿作为选窗上下文，并按最终 user prompt 压进模板预算。无绑定 → 提示词逐字不变；解析 /
        注入失败 → 回退基础 prompt（可选增强，绝不阻断建议生成）。
        """
        try:
            scope = resolve_style_scope(
                self.session,
                scene_id=target.get("scene_id"),
                chapter_id=target.get("chapter_id"),
                project_id=target.get("project_id"),
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
                placement=PLACEMENT_USER_TAIL,
            )
            return injected if injected is not None else prompt
        except STYLE_REFERENCE_FAIL_CLOSED_ERRORS:
            # 云策略不许把这本书派生的任何东西送给这个节点：整次调用 409（带 author_action），不降级成没有参考的提示
            raise
        except Exception:  # noqa: BLE001 — 可选增强：注入失败只记日志，不阻断建议生成
            _LOGGER.warning(
                "author proposal style reference prefix skipped for %s %s",
                target.get("object_type"),
                target.get("object_id"),
                exc_info=True,
            )
            return prompt

    def _draft_copy_scope(self, draft: AuthorDraft):
        return resolve_style_scope(self.session, scene_id=draft.object_id)

    def _proposal_copy_check(self, draft: AuthorDraft, proposal: AuthorDraftProposal):
        """模型写的建议文字过唯一抄袭门；拦下 → 返回检查结果，干净 → None。

        只算建议新带进来的命中（:func:`introduced_copy`）：整稿建议原样保留作者稿里已有的字不算。"""

        text = plain_manuscript_text(proposal.content or "")
        if not text.strip():
            return None
        check = check_reference_copy_for_scope(self.session, text, scope=self._draft_copy_scope(draft))
        check = introduced_copy(check, text, plain_manuscript_text(draft.content or ""))
        return check if check.blocked else None

    def _raise_proposal_copy_blocked(self, draft: AuthorDraft, check, *, proposal_ids: list[str]) -> None:
        raise DomainError(
            "SOURCE_SAFETY_BLOCKED",
            "the generated proposal copies the bound reference book and was discarded — generate again",
            status_code=409,
            details={
                "draft_id": draft.draft_id,
                "proposal_ids": proposal_ids,
                "reference_copy": check.audit(),
                "author_action": copy_block_author_action(
                    check,
                    target_view="writer",
                    target_ref=f"{draft.object_type}:{draft.object_id}",
                    subject="这条 AI 建议",
                ),
            },
        )


    @staticmethod
    def serialize_proposal(row: AuthorDraftProposal) -> dict[str, Any]:
        return {
            "proposal_id": row.proposal_id,
            "draft_id": row.draft_id,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "proposal_type": row.proposal_type,
            "proposal_source": row.proposal_source,
            "content": row.content,
            "rationale": row.rationale,
            "source_llm_call_id": row.source_llm_call_id,
            "target_range": row.target_range_json or None,
            "before_text_hash": row.before_text_hash,
            "replacement_text": row.replacement_text,
            "proposal_kind": row.proposal_kind,
            "source_evaluation_id": row.source_evaluation_id,
            "merge_status": row.merge_status or "pending",
            "status": row.status,
            "author_decision_note": row.author_decision_note,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


@dataclass(frozen=True)
class _CopyBlockedProposal:
    """生成时被唯一抄袭门拦下的建议（没落库，只带检查结果）。"""

    check: Any


def _proposal_generate_snapshot(
    draft: AuthorDraft,
    *,
    target: dict[str, Any],
    proposal_type: str,
) -> dict[str, Any]:
    """PromptBuilder 的输入：只供 user 消息开头的场景 / 章 / 契约 / 阶段几行，以及审计记的 bundle_hash。

    不带 inline 摘要：PromptBuilder 只渲染 ``context_budget.SECTION_SPECS`` 里的摘要，续写要的正文、
    元数据与这次的指令由 :func:`_proposal_generate_user_prompt` 另附在 user 消息后面。过去这里还备着
    author_draft / target_metadata / proposal_request 三份摘要，模型从来看不到（R6 复核补充 (2)）。
    """
    return {
        "contract_version": "AUTHOR_PROPOSAL_GENERATE_SOURCE_v1",
        "stage_allowlist_name": "author_proposal_generate",
        "scene_id": target.get("scene_id") or "",
        "chapter_id": target.get("chapter_id") or "",
        "source_version_refs": {
            "source_draft_id": draft.draft_id,
            "object_type": draft.object_type,
            "object_id": draft.object_id,
            "proposal_type": proposal_type,
        },
        "resolved_ref_ids": {},
    }


def _proposal_generate_user_prompt(base_prompt: str, *, draft: AuthorDraft, target: dict[str, Any]) -> str:
    return "\n".join(
        [
            base_prompt,
            "",
            "## Author Draft Target",
            f"Object Type: {draft.object_type}",
            f"Object ID: {draft.object_id}",
            f"Project ID: {target.get('project_id') or ''}",
            f"Chapter ID: {target.get('chapter_id') or ''}",
            f"Scene ID: {target.get('scene_id') or ''}",
            "",
            "## Current Author Draft",
            draft.content or "",
            "",
            "## Current Metadata",
            json.dumps(target, ensure_ascii=False, sort_keys=True),
        ]
    )


def _normalize_proposal_payload(
    payload: Any,
    *,
    draft: AuthorDraft,
    target: dict[str, Any],
    proposal_type: str,
    instruction: str | None,
) -> dict[str, str | None]:
    if not isinstance(payload, dict):
        raise DomainError(
            "AUTHOR_PROPOSAL_OUTPUT_INVALID",
            "author proposal response must be a JSON object",
            status_code=502,
            details={"node_id": "author_proposal_generate", "proposal_type": proposal_type},
        )
    content = str(payload.get("content") or payload.get("proposal") or "").strip()
    if not content:
        # 假生成已退役：模型没给出正文时不再用模板拼占位稿冒充提案。
        raise DomainError(
            "AUTHOR_PROPOSAL_OUTPUT_INVALID",
            "author proposal response is missing non-empty content",
            status_code=502,
            details={"node_id": "author_proposal_generate", "proposal_type": proposal_type},
        )
    rationale = str(payload.get("rationale") or "").strip()
    if not rationale:
        rationale = _proposal_rationale(target=target, instruction=instruction)
    return {"content": content, "rationale": rationale, "source_llm_call_id": None}


def _proposal_rationale(*, target: dict[str, Any], instruction: str | None) -> str:
    focus = instruction or target.get("chapter_goal") or "writer-facing drafting target"
    return f"续写候选：只推进下一拍，不改写作者现有正文。依据：{focus}"
