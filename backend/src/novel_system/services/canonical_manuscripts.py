from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    FinalScene,
    OperationLog,
    SceneBundle,
    SceneCard,
    SceneMemory,
    SceneRunState,
)
from novel_system.services.aggregator import Aggregator
from novel_system.services.archive_effects_plan import aggregate_volume_after_chapter
from novel_system.services.archiver import Archiver
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.chapter_approval import require_chapter_mutation_allowed
from novel_system.services.errors import DomainError
from novel_system.services.final_text_gate import FinalTextGateService
from novel_system.services.hash_engine import sha256_text
from novel_system.services.scene_lookup import scene_project_id
from novel_system.services.scene_text import final_chapter_memory

_LOGGER = logging.getLogger(__name__)

_MAX_CANONICAL_CHARS = 1_000_000
_BLOCK_TAGS = {"address", "article", "blockquote", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "pre", "section"}
_DROP_CONTENT_TAGS = {"script", "style", "template", "noscript"}


class _CanonicalTextParser(HTMLParser):
    """Extract safe plain manuscript text from the rich-text author draft."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._drop_depth = 0

    def _newline(self) -> None:
        if not self.parts or self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        if normalized in _DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if normalized == "br" or normalized in _BLOCK_TAGS:
            self._newline()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self._drop_depth and tag.lower() == "br":
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in _DROP_CONTENT_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if not self._drop_depth and normalized in _BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if not self._drop_depth:
            self.parts.append(data)


def canonicalize_author_text(content: str) -> str:
    parser = _CanonicalTextParser()
    parser.feed(content or "")
    parser.close()
    text = unicodedata.normalize("NFC", "".join(parser.parts)).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[\t\f\v ]+", " ", line).strip() for line in text.split("\n")]
    compact: list[str] = []
    for line in lines:
        if not line:
            if compact and compact[-1] != "":
                compact.append("")
            continue
        compact.append(line)
    while compact and compact[-1] == "":
        compact.pop()
    return "\n".join(compact)


def canonical_content_hash(content: str) -> str:
    return sha256_text(content)


class CanonicalSceneService:
    """Promote a scene AuthorDraft into the immutable canonical FinalScene chain.

    Text publication and fact authority are separate. ``requires_reconcile``
    publishes the exact author revision but leaves its continuity ledger pending;
    ``facts_unchanged`` may carry a previously committed ledger forward. Silently
    retaining unreviewed narrative events is forbidden.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.lifecycle = AuthorLifecycleService(session)

    def promote_author_draft(
        self,
        draft_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
        fidelity_source: str = "archive",
    ) -> dict[str, Any]:
        """作者稿提升为权威正文（成稿中心 source=archive；起草台「采用」的精确作者稿 source=adopt——
        归档时记的「像不像」读数带这个来源）。

        步骤都在同一个事务里，检查与写入的先后固定（B08-11）：请求校验 → 读基线（场景、当前权威正文、
        修订号）→ 规范正文 → 同一修订已经发布且这一场的派生齐全就原样返回 → 成稿门 → 草稿 / 指针双 CAS 发布新版 →
        归档、事实延续、章汇总（尽力而为：章汇总只是派生缓存，章级读者读各场终稿现拼，重建不成只记日志——R13）→ 审计。
        """
        draft = self.session.get(AuthorDraft, draft_id)
        if draft is None:
            raise DomainError("AUTHOR_DRAFT_NOT_FOUND", "author draft not found", status_code=404)
        if draft.status != "current":
            raise DomainError("AUTHOR_DRAFT_NOT_CURRENT", "author draft is not current", status_code=409)
        if draft.object_type != "scene":
            raise DomainError(
                "AUTHOR_DRAFT_PROMOTION_SCOPE_UNSUPPORTED",
                "canonical promotion currently supports scene author drafts only",
                status_code=409,
                details={"object_type": draft.object_type, "object_id": draft.object_id},
            )
        request = _PromotionRequest.parse(payload or {}, draft)
        base = self._load_base(draft, request)
        canonical_text = self._canonical_text(draft)
        content_hash = canonical_content_hash(canonical_text)

        # 相同文本不等于相同发布：生成稿第一次被作者确认时仍需创建带作者
        # provenance 的新版本。只有当前版本已绑定同一草稿修订才可能幂等。
        current_final = base.current_final
        same_author_revision = bool(
            current_final is not None
            and current_final.source_kind == "author_draft"
            and current_final.source_author_draft_id == draft.draft_id
            and current_final.source_author_draft_revision_no == request.base_revision_no
            and canonical_content_hash(current_final.content or "") == content_hash
        )
        if same_author_revision and current_final is not None and current_final.content_hash == content_hash:
            unchanged = self._already_current_result(
                draft, request, base, canonical_text=canonical_text, content_hash=content_hash, actor_ref=actor_ref
            )
            if unchanged is not None:
                return unchanged
        return self._publish(
            draft,
            request,
            base,
            canonical_text=canonical_text,
            content_hash=content_hash,
            same_author_revision=same_author_revision,
            actor_ref=actor_ref,
            fidelity_source=fidelity_source,
        )

    def _load_base(self, draft: AuthorDraft, request: "_PromotionRequest") -> "_PromotionBase":
        """这一场、它所在的章与作品、运行状态行（没有就准备新建），以及当前权威正文——并确认它们就是请求所基于的那一版。"""
        scene = self.lifecycle.require_active_scene(draft.object_id)
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        project_id = scene_project_id(self.session, scene)

        state = self.session.get(SceneRunState, scene.scene_id)
        state_is_new = False
        if state is None:
            if request.expected_final_id is not None:
                raise self._canonical_base_conflict(scene.scene_id, request.expected_final_id, None)
            state = SceneRunState(scene_id=scene.scene_id, scene_status="ready")
            state_is_new = True
        current_final_id = state.current_final_scene_row_id
        if current_final_id != request.expected_final_id:
            raise self._canonical_base_conflict(scene.scene_id, request.expected_final_id, current_final_id)
        current_final = self.session.get(FinalScene, current_final_id) if current_final_id else None
        if current_final is not None and (
            current_final.scene_id != scene.scene_id or current_final.chapter_id != scene.chapter_id
        ):
            raise DomainError(
                "CANONICAL_BASE_DETACHED",
                "current FinalScene is detached from the author draft target",
                status_code=409,
                details={"scene_id": scene.scene_id, "final_scene_row_id": current_final.row_id},
            )
        if int(draft.revision_no) != request.base_revision_no:
            raise DomainError(
                "AUTHOR_DRAFT_CONFLICT",
                "author draft has changed; refresh before canonical promotion",
                status_code=409,
                details={"current_revision_no": draft.revision_no},
            )
        return _PromotionBase(
            scene=scene,
            chapter=chapter,
            project_id=project_id,
            state=state,
            state_is_new=state_is_new,
            current_final=current_final,
            current_final_id=current_final_id,
        )

    @staticmethod
    def _canonical_text(draft: AuthorDraft) -> str:
        canonical_text = canonicalize_author_text(draft.content or "")
        if not canonical_text:
            raise DomainError("AUTHOR_DRAFT_EMPTY", "empty author draft cannot become canonical", status_code=409)
        if len(canonical_text) > _MAX_CANONICAL_CHARS:
            raise DomainError(
                "AUTHOR_DRAFT_TOO_LARGE",
                "author draft exceeds the canonical manuscript size limit",
                status_code=413,
                details={"char_count": len(canonical_text), "max_char_count": _MAX_CANONICAL_CHARS},
            )
        return canonical_text

    def _already_current_result(
        self,
        draft: AuthorDraft,
        request: "_PromotionRequest",
        base: "_PromotionBase",
        *,
        canonical_text: str,
        content_hash: str,
        actor_ref: str,
    ) -> dict[str, Any] | None:
        """同一份作者稿修订已经是权威正文、这一场的派生（场景记忆 / 审计）也都对得上：不再写任何东西，原样回报。
        派生不齐（返回 None）就照常走发布。章汇总不看（派生缓存，见 :meth:`_complete_derivation`）。"""
        current_final = base.current_final
        assert current_final is not None
        existing_derivation = self._complete_derivation(draft=draft, state=base.state, final=current_final)
        if existing_derivation is None:
            return None
        state = base.state
        canon_continuity: dict[str, Any] = {
            "status": "unavailable",
            "complete": False,
            "reason": "projectless_legacy_scene",
        }
        if base.project_id:
            canon_service = CanonContinuityService(self.session)
            canon_continuity = canon_service.scene_status(base.project_id, base.scene.scene_id)
            if request.narrative_effect == "facts_unchanged" and not canon_continuity["complete"]:
                canon_continuity = canon_service.carry_forward_facts_unchanged(
                    current_final.row_id,
                    source_final_scene_row_id=current_final.parent_final_scene_row_id,
                    actor_ref=actor_ref or "operator",
                    note="作者确认本次正文修订不改变既有叙事事实",
                )
            elif not canon_continuity["complete"]:
                state.narrative_sync_status = str(canon_continuity["status"])
                state.narrative_sync_final_scene_row_id = current_final.row_id
        # Different idempotency keys may reach this branch. It is a true
        # publication no-op: no CAS UPDATE, archive, aggregate, or log.
        return {
            "draft_id": draft.draft_id,
            "draft_revision_no": request.base_revision_no,
            "previous_final_scene_row_id": current_final.row_id,
            "final_scene_row_id": current_final.row_id,
            "content_hash": content_hash,
            "already_current": True,
            "derivation_reused": True,
            "scene_status": state.scene_status,
            "safe_to_archive": bool(
                existing_derivation["final_text_gate"].get(
                    "safe_to_archive",
                    existing_derivation["final_text_gate"].get("archivable", True),
                )
            ),
            "literary_warnings_unresolved": False,
            "author_confirmed_final": True,
            "finality": {
                "safe_to_archive": True,
                "literary_warnings_unresolved": False,
                "author_confirmed_final": True,
            },
            "scene_memory_row_id": existing_derivation["scene_memory_row_id"],
            "chapter_memory_row_id": existing_derivation["chapter_memory_row_id"],
            "narrative_sync_status": state.narrative_sync_status,
            "canonical_dirty": False,
            "canon_continuity": canon_continuity,
            "validation": {
                "canonical_char_count": len(canonical_text),
                "source_safety_scan": existing_derivation["source_safety_scan"],
                "final_text_gate": existing_derivation["final_text_gate"],
                "accepted_warning_codes": request.accepted_warning_codes,
                "reused_existing_validation": True,
            },
        }

    def _publish(
        self,
        draft: AuthorDraft,
        request: "_PromotionRequest",
        base: "_PromotionBase",
        *,
        canonical_text: str,
        content_hash: str,
        same_author_revision: bool,
        actor_ref: str,
        fidelity_source: str,
    ) -> dict[str, Any]:
        scene, state, current_final, current_final_id = base.scene, base.state, base.current_final, base.current_final_id
        if base.chapter is not None:
            require_chapter_mutation_allowed(
                self.session,
                base.chapter,
                changed_fields=["canonical_final_scene"],
                operation="canonical_manuscript.promote_author_draft",
            )

        source_bundle = self._source_bundle(state, current_final)
        final_text_gate = FinalTextGateService(self.session).evaluate(
            scene_id=scene.scene_id,
            content=canonical_text,
            source_bundle_id=source_bundle.bundle_id if source_bundle is not None else None,
            author_confirmed_final=True,
            accepted_warning_codes=request.accepted_warning_codes,
        )
        gate_content_hash = str(final_text_gate.get("content_hash") or "")
        if gate_content_hash != content_hash:
            raise DomainError(
                "FINAL_TEXT_GATE_HASH_MISMATCH",
                "final-text gate evaluated a different manuscript payload",
                status_code=500,
                details={
                    "scene_id": scene.scene_id,
                    "canonical_content_hash": content_hash,
                    "gate_content_hash": gate_content_hash,
                },
            )
        FinalTextGateService.raise_if_not_archivable(final_text_gate, scene_id=scene.scene_id)
        content_hash = gate_content_hash
        safety_scan = final_text_gate.get("source_safety") or {"safe": True}

        final = self._write_final(
            draft,
            request,
            base,
            canonical_text=canonical_text,
            content_hash=content_hash,
            same_author_revision=same_author_revision,
            source_bundle=source_bundle,
            actor_ref=actor_ref,
        )

        carry_notes = [
            {
                "kind": "author_canonical_promotion",
                "actor_ref": actor_ref or "operator",
                "draft_id": draft.draft_id,
                "draft_revision_no": request.base_revision_no,
                "narrative_effect": request.narrative_effect,
            }
        ]
        if same_author_revision:
            existing_memory = self.session.execute(
                select(SceneMemory).where(
                    SceneMemory.scene_id == scene.scene_id,
                    SceneMemory.final_scene_row_id == final.row_id,
                )
            ).scalars().first()
            if existing_memory is not None:
                carry_notes = list(existing_memory.carry_notes_json or [])
        archive_result = Archiver(self.session).archive_final_scene(
            scene.scene_id,
            final.row_id,
            carry_notes_json=carry_notes,
            author_confirmed_final=True,
            accepted_warning_codes=request.accepted_warning_codes,
            fidelity_source=fidelity_source,
        )
        # Project-less rows only exist in the legacy compatibility surface. They
        # cannot own a CanonCommit (the new ledger deliberately requires a real
        # StoryProject), but adopting their exact author revision must keep the
        # pre-existing archive behaviour. Archiver already returns an explicit
        # unavailable marker for this case; never invent a project merely to make
        # the continuity status look complete.
        canon_continuity = dict(archive_result.get("canon_continuity") or {})
        if (
            base.project_id
            and request.narrative_effect == "facts_unchanged"
            and not canon_continuity.get("complete")
        ):
            canon_continuity = CanonContinuityService(
                self.session
            ).carry_forward_facts_unchanged(
                final.row_id,
                source_final_scene_row_id=current_final_id,
                actor_ref=actor_ref or "operator",
                note="作者确认本次正文修订不改变既有叙事事实",
            )
        # 章汇总照旧重建，但它只是派生缓存（章级读者读各场终稿现拼，R13）：拼不出来（同一章里位置对不上的旧行）
        # 只记日志、不挡发布——以前这里 409，章里一场进了回收站，别的场就再也晋升不了
        aggregate_result = Aggregator(self.session).run_final_aggregate(scene.chapter_id) or {}
        if aggregate_result.get("status") == "created":
            # 与流水线同样的确定性收尾（[批准#22]，三条归档路径的差别见 archive_effects_plan 的说明）：章汇总之后接着卷汇总
            volume_result = aggregate_volume_after_chapter(self.session, scene.chapter_id)
        else:
            _LOGGER.warning(
                "chapter aggregate not rebuilt after promoting scene %s (chapter %s): %s %s %s",
                scene.scene_id,
                scene.chapter_id,
                aggregate_result.get("status"),
                aggregate_result.get("reason"),
                aggregate_result.get("scene_ids") or "",
            )
            # 卷汇总从各章的章汇总卷起：这一章的没重建，就不拿旧的那份去卷
            volume_result = {"status": "skipped", "reason": "chapter_aggregate_not_created"}

        self.session.add(
            OperationLog(
                event_type="author_draft_promoted_canonical",
                object_type="scene",
                object_ref=scene.scene_id,
                payload_json={
                    "project_id": base.project_id,
                    "chapter_id": scene.chapter_id,
                    "draft_id": draft.draft_id,
                    "draft_revision_no": request.base_revision_no,
                    "previous_final_scene_row_id": current_final_id,
                    "final_scene_row_id": final.row_id,
                    "content_hash": content_hash,
                    "already_current": same_author_revision,
                    "narrative_effect": request.narrative_effect,
                    "narrative_events_preserved": request.narrative_effect == "facts_unchanged",
                    "narrative_sync_status": state.narrative_sync_status,
                    "accepted_warning_codes": request.accepted_warning_codes,
                    "source_safety_scan": safety_scan,
                    "final_text_gate": final_text_gate,
                    "chapter_aggregate": aggregate_result.get("status"),
                    "volume_aggregate": volume_result.get("status"),
                    "actor_ref": actor_ref or "operator",
                },
            )
        )
        self.session.flush()
        return {
            "draft_id": draft.draft_id,
            "draft_revision_no": request.base_revision_no,
            "previous_final_scene_row_id": current_final_id,
            "final_scene_row_id": final.row_id,
            "content_hash": content_hash,
            "already_current": same_author_revision,
            "scene_status": state.scene_status,
            "safe_to_archive": archive_result["safe_to_archive"],
            "literary_warnings_unresolved": archive_result[
                "literary_warnings_unresolved"
            ],
            "author_confirmed_final": archive_result["author_confirmed_final"],
            "finality": archive_result["finality"],
            "scene_memory_row_id": archive_result["scene_memory_row_id"],
            "chapter_memory_row_id": aggregate_result.get("chapter_memory_row_id"),
            "narrative_sync_status": state.narrative_sync_status,
            "canonical_dirty": False,
            "canon_continuity": canon_continuity,
            "validation": {
                "canonical_char_count": len(canonical_text),
                "source_safety_scan": safety_scan,
                "final_text_gate": final_text_gate,
                "accepted_warning_codes": request.accepted_warning_codes,
            },
        }

    def _write_final(
        self,
        draft: AuthorDraft,
        request: "_PromotionRequest",
        base: "_PromotionBase",
        *,
        canonical_text: str,
        content_hash: str,
        same_author_revision: bool,
        source_bundle: SceneBundle | None,
        actor_ref: str,
    ) -> FinalScene:
        """发布：草稿修订 CAS（顺带记下晋升证据）→ 新一版 FinalScene → 运行状态指针 CAS → 旧一版标为被取代。"""
        scene, state, current_final = base.scene, base.state, base.current_final
        if base.state_is_new:
            # Creating missing runtime state is authoritative; keep it behind the
            # exact-text preflight just like every other publication write.
            self.session.add(state)
            self.session.flush()

        new_final_id = current_final.row_id if same_author_revision and current_final is not None else (
            f"final_scene_{scene.scene_id}_author_{uuid.uuid4().hex[:12]}"
        )

        # Draft CAS doubles as durable publication metadata. A concurrent PATCH
        # changes revision_no and makes this update affect zero rows.
        draft_cas = self.session.execute(
            update(AuthorDraft)
            .where(
                AuthorDraft.draft_id == draft.draft_id,
                AuthorDraft.status == "current",
                AuthorDraft.revision_no == request.base_revision_no,
            )
            .values(
                last_promoted_revision_no=request.base_revision_no,
                last_promoted_final_scene_row_id=new_final_id,
            )
            .execution_options(synchronize_session=False)
        )
        if draft_cas.rowcount != 1:
            raise DomainError(
                "AUTHOR_DRAFT_CONFLICT",
                "author draft changed during canonical promotion",
                status_code=409,
                details={"base_revision_no": request.base_revision_no},
            )

        final = current_final
        if not same_author_revision:
            source_bundle_id = (
                current_final.source_bundle_id
                if current_final is not None
                else (source_bundle.bundle_id if source_bundle is not None else "author_first")
            )
            source_bundle_hash = (
                current_final.source_bundle_hash
                if current_final is not None
                else (
                    source_bundle.bundle_snapshot_hash
                    if source_bundle is not None
                    else canonical_content_hash(f"author_first:{scene.scene_id}")
                )
            )
            final = FinalScene(
                row_id=new_final_id,
                scene_id=scene.scene_id,
                chapter_id=scene.chapter_id,
                content=canonical_text,
                content_hash=content_hash,
                status="archived",
                source_bundle_id=source_bundle_id,
                source_bundle_hash=source_bundle_hash,
                source_kind="author_draft",
                source_author_draft_id=draft.draft_id,
                source_author_draft_revision_no=request.base_revision_no,
                parent_final_scene_row_id=current_final.row_id if current_final is not None else None,
                created_by=actor_ref or "operator",
            )
            self.session.add(final)
            self.session.flush()

        target_sync_status = (
            "synced" if request.narrative_effect == "facts_unchanged" else "pending_extraction"
        )
        state_condition = SceneRunState.current_final_scene_row_id.is_(None)
        if request.expected_final_id is not None:
            state_condition = SceneRunState.current_final_scene_row_id == request.expected_final_id
        state_cas = self.session.execute(
            update(SceneRunState)
            .where(SceneRunState.scene_id == scene.scene_id, state_condition)
            .values(
                current_final_scene_row_id=new_final_id,
                narrative_sync_status=target_sync_status,
                narrative_sync_final_scene_row_id=new_final_id,
            )
            .execution_options(synchronize_session=False)
        )
        if state_cas.rowcount != 1:
            current_pointer = self.session.get(SceneRunState, scene.scene_id)
            raise self._canonical_base_conflict(
                scene.scene_id,
                request.expected_final_id,
                current_pointer.current_final_scene_row_id if current_pointer is not None else None,
            )

        draft.last_promoted_revision_no = request.base_revision_no
        draft.last_promoted_final_scene_row_id = new_final_id
        state.current_final_scene_row_id = new_final_id
        state.narrative_sync_status = target_sync_status
        state.narrative_sync_final_scene_row_id = new_final_id
        if current_final is not None and not same_author_revision:
            current_final.status = "superseded"
            current_final.superseded_by_final_scene_row_id = new_final_id
        assert final is not None
        return final

    def _complete_derivation(
        self,
        *,
        draft: AuthorDraft,
        state: SceneRunState,
        final: FinalScene,
    ) -> dict[str, Any] | None:
        """Return the current immutable derivation only when every pointer agrees.

        这一场自己的派生（指针、场景记忆、晋升审计）齐全就算。章汇总不在里面（R13）：它只是派生缓存，同一章别的场
        归档、重排、进回收站都会让它落后——以前这里要求它逐字对得上，同一修订的重放就跟着重新发布一遍。
        ``chapter_memory_row_id`` 只是顺带报告这一章当前存着的那份汇总（没有就 None）。
        """

        if (
            draft.last_promoted_revision_no != draft.revision_no
            or draft.last_promoted_final_scene_row_id != final.row_id
            or state.current_final_scene_row_id != final.row_id
            or state.narrative_sync_status != "synced"
            or state.narrative_sync_final_scene_row_id != final.row_id
            or final.content_hash != canonical_content_hash(final.content or "")
        ):
            return None

        target_memories = list(
            self.session.execute(
                select(SceneMemory).where(
                    SceneMemory.scene_id == final.scene_id,
                    SceneMemory.active_flag == 1,
                )
            ).scalars().all()
        )
        if len(target_memories) != 1:
            return None
        target_memory = target_memories[0]
        if (
            target_memory.chapter_id != final.chapter_id
            or target_memory.final_scene_row_id != final.row_id
            or target_memory.content != final.content
            or target_memory.source_bundle_id != final.source_bundle_id
            or target_memory.runtime_eligible != 1
        ):
            return None

        matching_log: OperationLog | None = None
        logs = self.session.execute(
            select(OperationLog)
            .where(
                OperationLog.event_type == "author_draft_promoted_canonical",
                OperationLog.object_type == "scene",
                OperationLog.object_ref == final.scene_id,
            )
            .order_by(OperationLog.operation_id.desc())
        ).scalars().all()
        for log in logs:
            payload = log.payload_json or {}
            if (
                payload.get("draft_id") == draft.draft_id
                and payload.get("draft_revision_no") == draft.revision_no
                and payload.get("final_scene_row_id") == final.row_id
                and payload.get("content_hash") == canonical_content_hash(final.content or "")
            ):
                matching_log = log
                break
        if matching_log is None:
            return None
        source_safety_scan = (matching_log.payload_json or {}).get("source_safety_scan")
        if not isinstance(source_safety_scan, dict):
            source_safety_scan = {"safe": True, "status": "reused_existing_canonical"}
        final_text_gate = (matching_log.payload_json or {}).get("final_text_gate")
        if not isinstance(final_text_gate, dict):
            final_text_gate = {
                "content_hash": canonical_content_hash(final.content or ""),
                "archivable": True,
                "safe_to_archive": True,
                "literary_warnings_unresolved": False,
                "author_confirmed_final": True,
                "status": "reused_existing_canonical",
            }
        chapter_memory = final_chapter_memory(self.session, final.chapter_id)
        return {
            "scene_memory_row_id": target_memory.row_id,
            "chapter_memory_row_id": chapter_memory.row_id if chapter_memory is not None else None,
            "source_safety_scan": source_safety_scan,
            "final_text_gate": final_text_gate,
        }

    def _source_bundle(self, state: SceneRunState, current_final: FinalScene | None) -> SceneBundle | None:
        candidate_ids = [
            state.current_bundle_id,
            current_final.source_bundle_id if current_final is not None else None,
        ]
        for bundle_id in candidate_ids:
            if not bundle_id:
                continue
            bundle = self.session.get(SceneBundle, bundle_id)
            if bundle is not None:
                return bundle
        return None

    @staticmethod
    def _canonical_base_conflict(
        scene_id: str,
        expected_final_scene_row_id: str | None,
        current_final_scene_row_id: str | None,
    ) -> DomainError:
        return DomainError(
            "CANONICAL_BASE_CONFLICT",
            "canonical scene changed; refresh and compare before promotion",
            status_code=409,
            details={
                "scene_id": scene_id,
                "expected_current_final_scene_row_id": expected_final_scene_row_id,
                "current_final_scene_row_id": current_final_scene_row_id,
            },
        )


@dataclass(frozen=True)
class _PromotionRequest:
    """晋升请求：叙事影响、基于的草稿修订号、期望的当前权威正文、已确认的提示码（校验顺序与报错不变）。"""

    narrative_effect: str
    base_revision_no: int
    expected_final_id: str | None
    accepted_warning_codes: list[str]

    @classmethod
    def parse(cls, body: dict[str, Any], draft: AuthorDraft) -> "_PromotionRequest":
        narrative_effect = str(body.get("narrative_effect") or "requires_reconcile").strip()
        if narrative_effect not in {"requires_reconcile", "facts_unchanged"}:
            raise DomainError(
                "CANONICAL_NARRATIVE_EFFECT_INVALID",
                "narrative_effect must be requires_reconcile or facts_unchanged",
                status_code=400,
                details={
                    "draft_id": draft.draft_id,
                    "scene_id": draft.object_id,
                    "narrative_effect": narrative_effect,
                    "supported_effects": ["requires_reconcile", "facts_unchanged"],
                },
            )

        base_revision_no = body.get("base_revision_no")
        if isinstance(base_revision_no, bool) or not isinstance(base_revision_no, int) or base_revision_no < 1:
            raise DomainError(
                "AUTHOR_DRAFT_PROMOTION_INVALID",
                "base_revision_no must be a positive integer",
                status_code=400,
            )
        if "expected_current_final_scene_row_id" not in body:
            raise DomainError(
                "AUTHOR_DRAFT_PROMOTION_INVALID",
                "expected_current_final_scene_row_id is required (use null when no canonical scene exists)",
                status_code=400,
            )
        expected_final_id = body.get("expected_current_final_scene_row_id")
        if expected_final_id is not None and (not isinstance(expected_final_id, str) or not expected_final_id.strip()):
            raise DomainError(
                "AUTHOR_DRAFT_PROMOTION_INVALID",
                "expected_current_final_scene_row_id must be a non-empty string or null",
                status_code=400,
            )
        expected_final_id = expected_final_id.strip() if isinstance(expected_final_id, str) else None

        accepted_warning_codes = body.get("accepted_warning_codes", [])
        if not isinstance(accepted_warning_codes, list) or any(not isinstance(code, str) for code in accepted_warning_codes):
            raise DomainError(
                "AUTHOR_DRAFT_PROMOTION_INVALID",
                "accepted_warning_codes must be a list of strings",
                status_code=400,
            )
        return cls(
            narrative_effect=narrative_effect,
            base_revision_no=base_revision_no,
            expected_final_id=expected_final_id,
            accepted_warning_codes=list(dict.fromkeys(code.strip() for code in accepted_warning_codes if code.strip())),
        )


@dataclass
class _PromotionBase:
    """晋升所基于的现状：这一场、所在的章与作品、运行状态行（``state_is_new`` = 还没落库）、当前权威正文。"""

    scene: SceneCard
    chapter: ChapterGoal | None
    project_id: str | None
    state: SceneRunState
    state_is_new: bool
    current_final: FinalScene | None
    current_final_id: str | None
