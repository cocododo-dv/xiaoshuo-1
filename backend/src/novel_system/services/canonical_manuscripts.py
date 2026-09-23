from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from html.parser import HTMLParser
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    ChapterMemory,
    ChapterState,
    FinalScene,
    OperationLog,
    SceneBundle,
    SceneCard,
    SceneMemory,
    SceneRunState,
)
from novel_system.services.aggregator import Aggregator
from novel_system.services.archiver import Archiver
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.canon_continuity import CanonContinuityService
from novel_system.services.chapter_approval import require_chapter_mutation_allowed
from novel_system.services.errors import DomainError
from novel_system.services.final_text_gate import FinalTextGateService


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
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


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
        归档时记的「像不像」读数带这个来源）。"""
        body = payload or {}
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
        accepted_warning_codes = list(dict.fromkeys(code.strip() for code in accepted_warning_codes if code.strip()))

        scene = self.lifecycle.require_active_scene(draft.object_id)
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        project_id = scene.project_id or (
            chapter.project_id if chapter is not None else None
        )

        state = self.session.get(SceneRunState, scene.scene_id)
        state_is_new = False
        if state is None:
            if expected_final_id is not None:
                raise self._canonical_base_conflict(scene.scene_id, expected_final_id, None)
            state = SceneRunState(scene_id=scene.scene_id, scene_status="ready")
            state_is_new = True
        current_final_id = state.current_final_scene_row_id
        if current_final_id != expected_final_id:
            raise self._canonical_base_conflict(scene.scene_id, expected_final_id, current_final_id)
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
        if int(draft.revision_no) != base_revision_no:
            raise DomainError(
                "AUTHOR_DRAFT_CONFLICT",
                "author draft has changed; refresh before canonical promotion",
                status_code=409,
                details={"current_revision_no": draft.revision_no},
            )

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
        content_hash = canonical_content_hash(canonical_text)

        # 相同文本不等于相同发布：生成稿第一次被作者确认时仍需创建带作者
        # provenance 的新版本。只有当前版本已绑定同一草稿修订才可能幂等。
        same_author_revision = bool(
            current_final is not None
            and current_final.source_kind == "author_draft"
            and current_final.source_author_draft_id == draft.draft_id
            and current_final.source_author_draft_revision_no == base_revision_no
            and canonical_content_hash(current_final.content or "") == content_hash
        )
        no_op_hash_matches = bool(
            same_author_revision
            and current_final is not None
            and current_final.content_hash == content_hash
        )
        if no_op_hash_matches and current_final is not None:
            existing_derivation = self._complete_derivation(
                draft=draft,
                state=state,
                final=current_final,
            )
            if existing_derivation is not None:
                canon_continuity: dict[str, Any] = {
                    "status": "unavailable",
                    "complete": False,
                    "reason": "projectless_legacy_scene",
                }
                if project_id:
                    canon_service = CanonContinuityService(self.session)
                    canon_continuity = canon_service.scene_status(
                        project_id,
                        scene.scene_id,
                    )
                    if (
                        narrative_effect == "facts_unchanged"
                        and not canon_continuity["complete"]
                    ):
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
                    "draft_revision_no": base_revision_no,
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
                        "accepted_warning_codes": accepted_warning_codes,
                        "reused_existing_validation": True,
                    },
                }

        if chapter is not None:
            require_chapter_mutation_allowed(
                self.session,
                chapter,
                changed_fields=["canonical_final_scene"],
                operation="canonical_manuscript.promote_author_draft",
            )

        source_bundle = self._source_bundle(state, current_final)
        final_text_gate = FinalTextGateService(self.session).evaluate(
            scene_id=scene.scene_id,
            content=canonical_text,
            source_bundle_id=source_bundle.bundle_id if source_bundle is not None else None,
            author_confirmed_final=True,
            accepted_warning_codes=accepted_warning_codes,
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

        if state_is_new:
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
                AuthorDraft.revision_no == base_revision_no,
            )
            .values(
                last_promoted_revision_no=base_revision_no,
                last_promoted_final_scene_row_id=new_final_id,
            )
            .execution_options(synchronize_session=False)
        )
        if draft_cas.rowcount != 1:
            raise DomainError(
                "AUTHOR_DRAFT_CONFLICT",
                "author draft changed during canonical promotion",
                status_code=409,
                details={"base_revision_no": base_revision_no},
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
                source_author_draft_revision_no=base_revision_no,
                parent_final_scene_row_id=current_final.row_id if current_final is not None else None,
                created_by=actor_ref or "operator",
            )
            self.session.add(final)
            self.session.flush()

        target_sync_status = (
            "synced" if narrative_effect == "facts_unchanged" else "pending_extraction"
        )
        state_condition = SceneRunState.current_final_scene_row_id.is_(None)
        if expected_final_id is not None:
            state_condition = SceneRunState.current_final_scene_row_id == expected_final_id
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
                expected_final_id,
                current_pointer.current_final_scene_row_id if current_pointer is not None else None,
            )

        draft.last_promoted_revision_no = base_revision_no
        draft.last_promoted_final_scene_row_id = new_final_id
        state.current_final_scene_row_id = new_final_id
        state.narrative_sync_status = target_sync_status
        state.narrative_sync_final_scene_row_id = new_final_id
        if current_final is not None and not same_author_revision:
            current_final.status = "superseded"
            current_final.superseded_by_final_scene_row_id = new_final_id

        assert final is not None
        carry_notes = [
            {
                "kind": "author_canonical_promotion",
                "actor_ref": actor_ref or "operator",
                "draft_id": draft.draft_id,
                "draft_revision_no": base_revision_no,
                "narrative_effect": narrative_effect,
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
            accepted_warning_codes=accepted_warning_codes,
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
            project_id
            and narrative_effect == "facts_unchanged"
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
        aggregate_result = Aggregator(self.session).run_final_aggregate(scene.chapter_id)
        if not aggregate_result or aggregate_result.get("status") != "created":
            raise DomainError(
                "CANONICAL_AGGREGATE_REBUILD_BLOCKED",
                "canonical scene was not published because chapter memory could not be rebuilt atomically",
                status_code=409,
                details={"scene_id": scene.scene_id, "aggregate_result": aggregate_result or {}},
            )

        self.session.add(
            OperationLog(
                event_type="author_draft_promoted_canonical",
                object_type="scene",
                object_ref=scene.scene_id,
                payload_json={
                    "project_id": project_id,
                    "chapter_id": scene.chapter_id,
                    "draft_id": draft.draft_id,
                    "draft_revision_no": base_revision_no,
                    "previous_final_scene_row_id": current_final_id,
                    "final_scene_row_id": final.row_id,
                    "content_hash": content_hash,
                    "already_current": same_author_revision,
                    "narrative_effect": narrative_effect,
                    "narrative_events_preserved": narrative_effect == "facts_unchanged",
                    "narrative_sync_status": state.narrative_sync_status,
                    "accepted_warning_codes": accepted_warning_codes,
                    "source_safety_scan": safety_scan,
                    "final_text_gate": final_text_gate,
                    "actor_ref": actor_ref or "operator",
                },
            )
        )
        self.session.flush()
        return {
            "draft_id": draft.draft_id,
            "draft_revision_no": base_revision_no,
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
            "chapter_memory_row_id": aggregate_result["chapter_memory_row_id"],
            "narrative_sync_status": state.narrative_sync_status,
            "canonical_dirty": False,
            "canon_continuity": canon_continuity,
            "validation": {
                "canonical_char_count": len(canonical_text),
                "source_safety_scan": safety_scan,
                "final_text_gate": final_text_gate,
                "accepted_warning_codes": accepted_warning_codes,
            },
        }

    def _complete_derivation(
        self,
        *,
        draft: AuthorDraft,
        state: SceneRunState,
        final: FinalScene,
    ) -> dict[str, Any] | None:
        """Return the current immutable derivation only when every pointer agrees."""

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

        active_scene_memories = list(
            self.session.execute(
                select(SceneMemory).where(
                    SceneMemory.chapter_id == final.chapter_id,
                    SceneMemory.active_flag == 1,
                )
            ).scalars().all()
        )
        scene_ids = {memory.scene_id for memory in active_scene_memories}
        if not scene_ids:
            return None
        scenes = list(
            self.session.execute(
                select(SceneCard).where(
                    SceneCard.scene_id.in_(scene_ids),
                    SceneCard.chapter_id == final.chapter_id,
                    SceneCard.trashed_flag == 0,
                )
            ).scalars().all()
        )
        scene_by_id = {scene.scene_id: scene for scene in scenes}
        if set(scene_by_id) != scene_ids:
            return None
        counts: dict[str, int] = {}
        for memory in active_scene_memories:
            counts[memory.scene_id] = counts.get(memory.scene_id, 0) + 1
        if any(count != 1 for count in counts.values()):
            return None
        ordered_memories = sorted(
            active_scene_memories,
            key=lambda memory: (
                int(scene_by_id[memory.scene_id].scene_seq or 0),
                memory.scene_id,
                memory.created_at or "",
                memory.row_id,
            ),
        )
        expected_chapter_content = "\n".join(memory.content for memory in ordered_memories)

        chapter_state = self.session.get(ChapterState, final.chapter_id)
        if (
            chapter_state is None
            or chapter_state.chapter_backfill_pending_count != 0
            or chapter_state.aggregate_block_reason != "none"
            or not chapter_state.last_final_memory_row_id
        ):
            return None
        chapter_memories = list(
            self.session.execute(
                select(ChapterMemory).where(
                    ChapterMemory.chapter_id == final.chapter_id,
                    ChapterMemory.aggregate_stage == "final",
                    ChapterMemory.active_flag == 1,
                )
            ).scalars().all()
        )
        if len(chapter_memories) != 1:
            return None
        chapter_memory = chapter_memories[0]
        if (
            chapter_memory.row_id != chapter_state.last_final_memory_row_id
            or chapter_memory.runtime_eligible != 1
            or chapter_memory.content != expected_chapter_content
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
        return {
            "scene_memory_row_id": target_memory.row_id,
            "chapter_memory_row_id": chapter_memory.row_id,
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
