"""作者稿的修订快照：自动保存按 5 分钟时段合并、晋升的那一版与建稿那一版永远留着（批准 #8），版本对比的列表与单版正文。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from novel_system.db.models import AuthorDraft, AuthorDraftRevision, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.manuscript_html import sanitize_manuscript_html
from novel_system.services.pagination import paginate_select, resolve_pagination_request
from novel_system.services.writing_stats import count_words

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

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


class AuthorDraftRevisionsMixin:
    session: "Session"

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
