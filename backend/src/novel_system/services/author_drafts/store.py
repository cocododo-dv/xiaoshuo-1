"""作者稿的存取：建稿（ensure）、读当前稿、保存（乐观锁 + 字数 / 诊断回传）、回包形状、场景目标与起步正文。

作者稿只有场景稿（B08-22）：写作台只建场景稿，章稿 / 作品稿的分支已删；已有的旧行仍可按 id 保存。
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import update

from novel_system.db.models import (
    AuthorDraft,
    AuthorDraftEvent,
    FinalScene,
    SceneCard,
    SceneRunState,
)
from novel_system.services.chapter_approval import require_author_target_mutation_allowed
from novel_system.services.errors import DomainError
from novel_system.services.manuscript_html import sanitize_manuscript_html
from novel_system.services.scene_lookup import scene_project_id
from novel_system.services.scene_text import current_author_draft
from novel_system.services.story_slots import planned_beats, planned_chapter_goal
from novel_system.services.writer_briefs import (
    normalize_chapter_writer_brief,
    normalize_scene_writer_brief,
)
from novel_system.services.writing_stats import WritingStatsService, count_words

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from novel_system.services.author_lifecycle import AuthorLifecycleService

_RUNTIME_FINAL_UNAVAILABLE = object()
_LOGGER = logging.getLogger("novel_system.services.author_drafts")


class AuthorDraftStoreMixin:
    session: "Session"
    lifecycle: "AuthorLifecycleService"

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
        """场景稿归哪部作品（见 ``scene_lookup.scene_project_id``）；不是场景稿给 None。"""
        if object_type != "scene":
            return None
        scene = self.session.get(SceneCard, object_id)
        return scene_project_id(self.session, scene) if scene is not None else None

    def save(
        self,
        draft_id: str,
        payload: dict[str, Any],
        *,
        actor_ref: str = "operator",
        snapshot_origin: str = "edited",
    ) -> dict[str, Any]:
        """保存作者稿（修订号 CAS）。``snapshot_origin`` 是这一版修订快照的来源，只在服务端内部传、不是请求字段：
        写作台的自动保存是 ``edited``（按 5 分钟时段合并，批准 #8）；采纳并归档（``scene_adoption``）传 ``adopted``，
        永远单独留一行，不吃掉同一时段里被它替换的手写稿（终审 A-1，见 ``_snapshot_revision``）。"""
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
        self._snapshot_revision(draft, actor_ref=actor_ref, origin=snapshot_origin)
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
            # 续写提示读的目标只给作者规划过的（旧物化补的「推进本章：<章名>」不算，S2 1）
            "chapter_goal": planned_chapter_goal(chapter.chapter_goal, chapter),
            "chapter_writer_brief": normalize_chapter_writer_brief(chapter.writer_brief_json),
            "scene_card": {
                "scene_goal": planned_chapter_goal(scene.scene_goal, chapter),
                "beats": planned_beats(scene.beats_json, chapter),
                "location": scene.location or "",
                "exit_change": scene.exit_change or "",
                "hook": scene.hook or "",
            },
            "current_writer_brief": normalize_scene_writer_brief(scene.writer_brief_json),
        }


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
