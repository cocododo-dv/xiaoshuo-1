from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    AuthorDraftEvent,
    AuthorDraftProposal,
    AuthorDraftRevision,
    ChapterGoal,
    FinalScene,
    SceneCard,
    SceneRunState,
    utcnow,
)
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.chapter_approval import require_author_target_mutation_allowed
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_json_normalized, sha256_text
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.llm_fail_closed import raise_llm_domain_error
from novel_system.services.llm_task_runner import (
    LLMNodeExecutionError,
    LLMNodeRunner,
    current_llm_execution_id,
)
from novel_system.services.manuscript_html import plain_manuscript_text, sanitize_manuscript_html
from novel_system.services.pagination import paginate_select, resolve_pagination_request
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.reference_copy_gate import (
    check_reference_copy_for_scope,
    copy_block_author_action,
    introduced_copy,
)
from novel_system.services.style_reference.policy import STYLE_REFERENCE_FAIL_CLOSED_ERRORS
from novel_system.services.style_prompt_injection import (
    PLACEMENT_USER_TAIL,
    apply_style_user_tail,
    inject_style_reference_prefix,
    resolve_style_scope,
)
from novel_system.services.writer_briefs import (
    normalize_chapter_writer_brief,
    normalize_scene_writer_brief,
)
from novel_system.services.writing_stats import WritingStatsService, count_words
from novel_system.services.scene_text import current_author_draft

_RUNTIME_FINAL_UNAVAILABLE = object()
_LOGGER = logging.getLogger(__name__)
#: 写作台「AI 续写」：一次生成三条走向不同的续写候选（同一个幂等意图）。这是作者稿建议唯一还在用的生成方式——
#: 采纳 / 放弃 / 对比 / 单条生成 / 列表这几个接口界面从没调过，已删（批准 #7）；候选由前端插进正文、走普通保存。
CONTINUATION_VARIANTS_MODE = "continuation_variants"
CONTINUATION_VARIANT_DIRECTIONS = (
    ("action", "候选方向：优先用动作推进下一拍，避免解释和总结。"),
    ("relationship", "候选方向：优先增加人物关系压力，用反应、停顿或选择推进。"),
    ("suspense", "候选方向：优先释放一个新信息或悬念钩子，但不要越过下一拍。"),
)


class AuthorDraftService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.lifecycle = AuthorLifecycleService(session)

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

    def current(self, object_type: str, object_id: str) -> dict[str, Any]:
        self._require_target(object_type, object_id)
        draft = self._current_row(object_type, object_id)
        if draft is None:
            return {"draft": None}
        return self._draft_response(draft)

    def ensure(self, object_type: str, object_id: str, *, actor_ref: str = "operator") -> dict[str, Any]:
        self._require_target(object_type, object_id)
        current = self._current_row(object_type, object_id)
        if current is not None:
            return self._draft_response(current)
        source = self._scene_source(object_id) or self._blank_scene_source(object_id)
        draft = self._create_draft_row(object_type, object_id, source=source, actor_ref=actor_ref)
        return self._draft_response(draft)

    def _create_draft_row(
        self,
        object_type: str,
        object_id: str,
        *,
        source: dict[str, str],
        actor_ref: str,
        event_payload: dict[str, Any] | None = None,
    ) -> AuthorDraft:
        draft = AuthorDraft(
            draft_id=f"author_draft_{object_type}_{object_id}_{uuid.uuid4().hex[:10]}",
            object_type=object_type,
            object_id=object_id,
            source_text_ref=source["source_text_ref"],
            content=sanitize_manuscript_html(source["content"]),
            revision_no=1,
            status="current",
            created_by=actor_ref or "author_draft",
            updated_by=actor_ref or "author_draft",
        )
        self.session.add(draft)
        self.session.flush()
        self._add_event(
            draft,
            event_type="created",
            actor_ref=actor_ref,
            payload={"source_text_ref": source["source_text_ref"], **(event_payload or {})},
        )
        self._snapshot_revision(draft, actor_ref=actor_ref, origin="created")
        self.session.flush()
        return draft

    def _resolve_project_id(self, object_type: str, object_id: str) -> str | None:
        """场景稿归哪部作品（场景卡自己的 project_id，旧卡没有就看它所在的章）；不是场景稿给 None。"""
        if object_type != "scene":
            return None
        scene = self.session.get(SceneCard, object_id)
        if scene is None:
            return None
        if scene.project_id:
            return scene.project_id
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        return chapter.project_id if chapter else None

    def save(self, draft_id: str, payload: dict[str, Any], *, actor_ref: str = "operator") -> dict[str, Any]:
        draft = self._require_draft(draft_id)
        previous_content = draft.content or ""
        base_revision_no = payload.get("base_revision_no")
        if int(base_revision_no or 0) != int(draft.revision_no):
            raise DomainError(
                "AUTHOR_DRAFT_CONFLICT",
                "author draft has changed; refresh before saving",
                status_code=409,
                details={"current_revision_no": draft.revision_no},
            )
        content = payload.get("content")
        if not isinstance(content, str):
            raise DomainError("AUTHOR_DRAFT_INVALID", "content must be a string", status_code=400)
        content = sanitize_manuscript_html(content)
        content_changed = content != previous_content
        require_author_target_mutation_allowed(
            self.session,
            object_type=draft.object_type,
            object_id=draft.object_id,
            changed_fields=["author_draft.content"] if content_changed else [],
            operation="author_draft.save",
        )
        if not content_changed:
            response = self._draft_response(draft)
            response["changed"] = False
            return response
        new_words = count_words(content)
        words_delta = new_words - count_words(previous_content)
        next_revision_no = int(draft.revision_no) + 1
        updated = self.session.execute(
            update(AuthorDraft)
            .where(
                AuthorDraft.draft_id == draft_id,
                AuthorDraft.revision_no == int(base_revision_no),
                AuthorDraft.status == "current",
            )
            .values(
                content=content,
                revision_no=next_revision_no,
                updated_by=actor_ref or draft.updated_by,
            )
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount != 1:
            # The pre-check above gives fast feedback in the common case; this
            # database compare-and-swap is the actual concurrency guarantee.
            self.session.rollback()
            current = self.session.get(AuthorDraft, draft_id)
            raise DomainError(
                "AUTHOR_DRAFT_CONFLICT",
                "author draft has changed; refresh before saving",
                status_code=409,
                details={
                    "current_revision_no": current.revision_no if current else None,
                    "current_status": current.status if current else None,
                },
            )
        self.session.expire(draft)
        self.session.refresh(draft)
        # FE-ALIGN P2 字数埋点（D2）：保存主路径上报 words_delta，统计按 project 聚合。
        project_id = self._resolve_project_id(draft.object_type, draft.object_id)
        if words_delta and project_id:
            WritingStatsService(self.session).record_words_delta(project_id, words_delta)
        # FE-ALIGN P3 目录 rollup：场景正文字数落 SceneCard.words_current，
        # 响应带最新 rollup（前端不再自算 delta）。
        words_rollup: dict[str, Any] | None = None
        if draft.object_type == "scene":
            scene = self.session.get(SceneCard, draft.object_id)
            if scene is not None:
                scene.words_current = new_words
                # 全书字数按库里各场求和（批准 #9）：会话不自动 flush，先把这一场的新字数写进去
                self.session.flush()
                from novel_system.services.catalog import CatalogService

                words_rollup = CatalogService(self.session).words_rollup(scene)
                if project_id:
                    # 全书字数、今日字数、连续天数一起回传：写作台不必每存一次再去问 writing-stats
                    stats = WritingStatsService(self.session).stats_payload(project_id)
                    words_rollup.update({key: stats[key] for key in ("words_total", "words_today", "streak_days")})
        self._add_event(
            draft,
            event_type="edited",
            actor_ref=actor_ref,
            patch_id=_optional_text(payload, "patch_id"),
            revision_id=_optional_text(payload, "revision_id"),
            option_id=_optional_text(payload, "option_id"),
            note=_optional_text(payload, "note"),
            payload={"base_revision_no": base_revision_no, "revision_no": draft.revision_no},
        )
        self._snapshot_revision(draft, actor_ref=actor_ref, origin="edited")
        self.session.flush()
        response = self._draft_response(draft)
        response["changed"] = True
        if words_rollup is not None:
            response["words_rollup"] = words_rollup
        if draft.object_type == "scene":
            # 2026-09-22 场景诊断第三轮：正文一存，这一场 / 这一章开着的发现数随响应回传
            # （规则 + 节奏发现按正文哈希缓存；算不出来只是少一个键，保存本身不受影响）
            try:
                from novel_system.services.scene_diagnosis import SceneDiagnosisService

                scene_row = self.session.get(SceneCard, draft.object_id)
                if scene_row is not None:
                    response["diagnosis_rollup"] = SceneDiagnosisService(self.session).scene_rollup(scene_row)
            except Exception:  # noqa: BLE001 — 角标不是闸门
                _LOGGER.debug("diagnosis rollup unavailable after draft save %s", draft_id, exc_info=True)
        return response


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
            proposal_kind=proposal_kind,
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
        proposal_kind: str,
    ) -> dict[str, str | None]:
        target_for_prompt = {
            **target,
            "proposal_instruction": instruction or "",
        }
        snapshot = _proposal_generate_snapshot(
            draft,
            target=target_for_prompt,
            proposal_type=proposal_type,
            proposal_kind=proposal_kind,
            instruction=instruction,
        )
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

    # FE-ALIGN F2 修订历史：正文的完整内容快照，支撑成稿中心版本对比。
    def _snapshot_revision(self, draft: AuthorDraft, *, actor_ref: str, origin: str) -> None:
        """记一份修订快照。自动保存（``edited``）按 5 分钟时段合并（批准 #8）：同一时段里接着上一份自动保存快照写，
        只留这一时段最新的正文——除非上一份就是晋升过权威正文的那一版（它永远留着）；建稿那一版（``created``）也永远留着。

        写作台停笔 900 毫秒就自动保存一次，过去每存一次就多一行全文快照（一晚上一场几百份），版本对比的列表一次全给。
        """
        existing = self.session.execute(
            select(AuthorDraftRevision.draft_revision_id)
            .where(AuthorDraftRevision.draft_id == draft.draft_id)
            .where(AuthorDraftRevision.revision_no == int(draft.revision_no))
        ).scalar_one_or_none()
        if existing is not None:
            return
        if origin == "edited":
            latest = self.session.execute(
                select(AuthorDraftRevision)
                .where(AuthorDraftRevision.draft_id == draft.draft_id)
                .order_by(AuthorDraftRevision.revision_no.desc())
                .limit(1)
            ).scalar_one_or_none()
            now = utcnow()
            if (
                latest is not None
                and latest.origin == "edited"
                and latest.revision_no != draft.last_promoted_revision_no
                and _same_revision_window(latest.created_at, now)
            ):
                latest.revision_no = int(draft.revision_no)
                latest.content = draft.content or ""
                latest.words = count_words(draft.content or "")
                latest.created_by = actor_ref or "author_draft"
                latest.created_at = now
                return
        self.session.add(
            AuthorDraftRevision(
                draft_revision_id=f"author_draft_rev_{uuid.uuid4().hex[:12]}",
                draft_id=draft.draft_id,
                revision_no=int(draft.revision_no),
                content=draft.content or "",
                words=count_words(draft.content or ""),
                origin=origin,
                created_by=actor_ref or "author_draft",
            )
        )

    def revisions(
        self,
        draft_id: str,
        *,
        page: int | None = None,
        page_size: int | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """版本对比的列表（新 → 旧，不带正文）。不给分页参数时照旧一次给全；给了（page / page_size 或
        cursor / limit）就分页，回包多一个 ``pagination``（批准 #8）。"""
        draft = self._require_draft(draft_id)
        statement = select(AuthorDraftRevision).where(AuthorDraftRevision.draft_id == draft.draft_id)
        pagination: dict[str, Any] | None = None
        if page is None and page_size is None and cursor is None and limit is None:
            rows = self.session.execute(statement.order_by(AuthorDraftRevision.revision_no.desc())).scalars().all()
        else:
            rows, pagination = paginate_select(
                self.session,
                statement,
                request=resolve_pagination_request(page=page, page_size=page_size, cursor=cursor, limit=limit),
                order_columns=((AuthorDraftRevision.revision_no, "desc"),),
                cursor_values=lambda row: [row.revision_no],
            )
        payload: dict[str, Any] = {
            "draft_id": draft.draft_id,
            "object_type": draft.object_type,
            "object_id": draft.object_id,
            "revision_no": draft.revision_no,
            "items": [
                {
                    "revision_no": row.revision_no,
                    "words": row.words,
                    "origin": row.origin,
                    "created_by": row.created_by,
                    "created_at": row.created_at,
                }
                for row in rows
            ],
        }
        if pagination is not None:
            payload["pagination"] = pagination
        return payload

    def revision(self, draft_id: str, revision_no: int) -> dict[str, Any]:
        draft = self._require_draft(draft_id)
        row = self.session.execute(
            select(AuthorDraftRevision)
            .where(AuthorDraftRevision.draft_id == draft.draft_id)
            .where(AuthorDraftRevision.revision_no == int(revision_no))
        ).scalar_one_or_none()
        if row is None:
            raise DomainError(
                "AUTHOR_DRAFT_REVISION_NOT_FOUND",
                "author draft revision not found",
                status_code=404,
                details={"draft_id": draft.draft_id, "revision_no": revision_no},
            )
        return {
            "revision": {
                "draft_id": draft.draft_id,
                "revision_no": row.revision_no,
                "content": sanitize_manuscript_html(row.content),
                "words": row.words,
                "origin": row.origin,
                "created_by": row.created_by,
                "created_at": row.created_at,
            }
        }

    def _draft_response(self, draft: AuthorDraft) -> dict[str, Any]:
        """作者稿接口（ensure / current / 保存）的回包：草稿本身 + 这一场当前权威正文的指针。

        写作台读的就是这两样（``draft.{draft_id, revision_no, content, canonical_dirty, last_promoted_*}``、
        ``runtime_final_ref``）。过去每次回包都附一整份「台面上下文」——开着的段落补丁、续写候选全文、偏好摘要、
        章汇总指针……前端一样不读，续写候选还随每次点击越积越多（B08-02）。
        """
        runtime_ref = self._runtime_final_ref(draft)
        runtime_final_id = runtime_ref.removeprefix("final_scene:") if runtime_ref else None
        serialized = self.serialize_draft(
            draft,
            current_final_scene_row_id=(
                runtime_final_id if draft.object_type == "scene" else _RUNTIME_FINAL_UNAVAILABLE
            ),
        )
        return {"draft": serialized, "runtime_final_ref": runtime_ref}

    def _runtime_final_ref(self, draft: AuthorDraft) -> str | None:
        """场景稿：这一场当前的权威正文（``final_scene:<row_id>``，还没有就 None）。场景进了回收站 → 409，
        与过去回包里读台面上下文时一样——保存一份回收站里的场景稿随之整笔回滚。"""
        if draft.object_type != "scene":
            return None
        self.lifecycle.require_active_scene(draft.object_id)
        source = self._scene_source(draft.object_id)
        return source["source_text_ref"] if source is not None else None

    @staticmethod
    def serialize_draft(
        row: AuthorDraft | None,
        *,
        current_final_scene_row_id: str | None | object = _RUNTIME_FINAL_UNAVAILABLE,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        canonical_dirty = row.last_promoted_revision_no != row.revision_no
        if row.object_type == "scene":
            # Without runtime state, never claim that a scene is canonical. Desk
            # responses pass the pointer explicitly and therefore remain exact.
            canonical_dirty = bool(
                canonical_dirty
                or current_final_scene_row_id is _RUNTIME_FINAL_UNAVAILABLE
                or not row.last_promoted_final_scene_row_id
                or current_final_scene_row_id != row.last_promoted_final_scene_row_id
            )
        return {
            "draft_id": row.draft_id,
            "object_type": row.object_type,
            "object_id": row.object_id,
            "source_text_ref": row.source_text_ref,
            "content": sanitize_manuscript_html(row.content),
            "revision_no": row.revision_no,
            "last_promoted_revision_no": row.last_promoted_revision_no,
            "last_promoted_final_scene_row_id": row.last_promoted_final_scene_row_id,
            "canonical_dirty": canonical_dirty,
            "status": row.status,
            "created_by": row.created_by,
            "updated_by": row.updated_by,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

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


    def _require_target(self, object_type: str, object_id: str) -> None:
        # 作者稿只有场景稿：写作台只建场景稿；章稿 / 作品稿（退役的发现稿）只剩测试在建，库里也没有（B08-22）
        if object_type != "scene":
            raise DomainError("AUTHOR_DRAFT_TARGET_INVALID", "object_type must be scene", status_code=400)
        self.lifecycle.require_active_scene(object_id)

    def _current_row(self, object_type: str, object_id: str) -> AuthorDraft | None:
        return current_author_draft(self.session, object_type, object_id)

    def _require_draft(self, draft_id: str) -> AuthorDraft:
        draft = self.session.get(AuthorDraft, draft_id)
        if draft is None:
            raise DomainError("AUTHOR_DRAFT_NOT_FOUND", "author draft not found", status_code=404)
        if draft.status != "current":
            raise DomainError("AUTHOR_DRAFT_NOT_CURRENT", "author draft is not current", status_code=409)
        return draft

    def _scene_source(self, scene_id: str) -> dict[str, str] | None:
        """这一场当前的权威正文（新建作者稿从它起步）；还没有就 None。"""
        scene = self.lifecycle.require_active_scene(scene_id)
        state = self.session.get(SceneRunState, scene.scene_id)
        final_row = self.session.get(FinalScene, state.current_final_scene_row_id) if state and state.current_final_scene_row_id else None
        if final_row is None:
            return None
        return {"source_text_ref": f"final_scene:{final_row.row_id}", "content": final_row.content or ""}

    def _blank_scene_source(self, scene_id: str) -> dict[str, str]:
        scene = self.lifecycle.require_active_scene(scene_id)
        self.lifecycle.require_active_chapter(scene.chapter_id)
        # 阶段 X：空白稿就是空白。过去这里把场景卡抄成一段「【章节目标】…【场景目标】…【节拍】…」脚手架
        # 塞进正文——那是没有随行场景卡的旧作者台留下的做法。现在设计卡常驻在正文旁边（写作台 / AI 起草台
        # 同一张），抄进正文的那份只会：算进字数、要作者先删掉才能动笔、构思改了它也不跟着变
        # （一份永远停在首次打开那一刻的旧卡），忘了删还会被一起提升成权威正文。
        return {"source_text_ref": f"scene_card:{scene.scene_id}:blank", "content": ""}

    def _add_event(
        self,
        draft: AuthorDraft,
        *,
        event_type: str,
        actor_ref: str,
        patch_id: str | None = None,
        revision_id: str | None = None,
        option_id: str | None = None,
        note: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuthorDraftEvent:
        event = AuthorDraftEvent(
            event_id=f"author_draft_event_{uuid.uuid4().hex[:12]}",
            draft_id=draft.draft_id,
            object_type=draft.object_type,
            object_id=draft.object_id,
            event_type=event_type,
            patch_id=patch_id,
            revision_id=revision_id,
            option_id=option_id,
            note=note,
            payload_json=payload or {},
            created_by=actor_ref or "author_draft",
        )
        self.session.add(event)
        return event

    def _target_payload(self, object_type: str, object_id: str) -> dict[str, Any]:
        scene = self.lifecycle.require_active_scene(object_id)
        chapter = self.lifecycle.require_active_chapter(scene.chapter_id)
        return {
            "object_type": "scene",
            "object_id": scene.scene_id,
            "project_id": scene.project_id or chapter.project_id,
            "chapter_id": scene.chapter_id,
            "scene_id": scene.scene_id,
            "chapter_goal": chapter.chapter_goal or "",
            "chapter_writer_brief": normalize_chapter_writer_brief(chapter.writer_brief_json),
            "scene_card": {
                "scene_goal": scene.scene_goal or "",
                "beats": scene.beats_json or [],
                "location": scene.location or "",
                "exit_change": scene.exit_change or "",
                "hook": scene.hook or "",
            },
            "current_writer_brief": normalize_scene_writer_brief(scene.writer_brief_json),
        }


#: 自动保存的修订快照合并的时段长度（秒）：同一个 5 分钟时段里至多留一份
REVISION_COALESCE_SECONDS = 300


def _same_revision_window(earlier: str | None, later: str) -> bool:
    """两个时刻是否落在同一个 5 分钟时段（按 UTC 时钟对齐）。解析不了就当不同时段（宁可多留一份）。"""
    try:
        earlier_at = datetime.fromisoformat(str(earlier))
        later_at = datetime.fromisoformat(str(later))
    except ValueError:
        return False
    if earlier_at.tzinfo is None or later_at.tzinfo is None:
        return False
    return int(earlier_at.timestamp()) // REVISION_COALESCE_SECONDS == int(later_at.timestamp()) // REVISION_COALESCE_SECONDS


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


@dataclass(frozen=True)
class _CopyBlockedProposal:
    """生成时被唯一抄袭门拦下的建议（没落库，只带检查结果）。"""

    check: Any


def _proposal_generate_snapshot(
    draft: AuthorDraft,
    *,
    target: dict[str, Any],
    proposal_type: str,
    proposal_kind: str,
    instruction: str | None,
) -> dict[str, Any]:
    inline_digests = {
        "author_draft": draft.content or "",
        "target_metadata": json.dumps(target, ensure_ascii=False, sort_keys=True),
        "proposal_request": json.dumps(
            {
                "proposal_type": proposal_type,
                "proposal_kind": proposal_kind,
                "instruction": instruction or "",
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    }
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
        "ordered_injections": [
            {"slot": "author_draft", "ref_id": draft.draft_id, "digest_key": "author_draft"},
            {"slot": "target_metadata", "ref_id": draft.object_id, "digest_key": "target_metadata"},
            {"slot": "proposal_request", "ref_id": proposal_type, "digest_key": "proposal_request"},
        ],
        "inline_digests": inline_digests,
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
