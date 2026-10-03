"""作者稿的修订快照：自动保存按 5 分钟时段合并（整段删改另起一行）、晋升 / 采纳的那一版与建稿那一版永远留着（批准 #8），
版本对比的列表与单版正文。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from novel_system.db.models import AuthorDraft, AuthorDraftRevision, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.manuscript_html import sanitize_manuscript_html
from novel_system.services.pagination import paginate_select, resolve_pagination_request
from novel_system.services.writing_stats import compact_visible_text, count_words

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

#: 自动保存的修订快照合并的时段长度（秒）：同一个 5 分钟时段里至多留一份
REVISION_COALESCE_SECONDS = 300
#: 「整段删改」的门槛（编辑器口径的可见字数，去空白）：一次保存删掉 / 换掉上一份快照里至少
#: min(上限, max(下限, 那一份字数的一半)) 个字，就不并进这一时段的快照（终审 A-2）。打字、退格、删一句照旧并；
#: 全选删除、接受一大段改写、从同步与恢复整篇换回不并
DESTRUCTIVE_EDIT_CAP_CHARS = 200
DESTRUCTIVE_EDIT_FLOOR_CHARS = 40


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


def _shared_prefix_length(first: str, second: str, limit: int) -> int:
    """两段文字共同开头的长度（至多 ``limit``）。二分 + 切片比较：整场几万字时也不逐字走 Python 循环。"""
    low, high = 0, limit
    while low < high:
        middle = (low + high + 1) // 2
        if first[low:middle] == second[low:middle]:
            low = middle
        else:
            high = middle - 1
    return low


def _removed_chars(old: str, new: str) -> int:
    """``new`` 比 ``old`` 删掉或换掉了几个字：去掉两份共同的开头与结尾，旧稿剩下的就是这一次拿走的。只认一处连续的
    改动——两处隔开的删改按整个跨度算，宁可多算（多留一行快照）。"""
    limit = min(len(old), len(new))
    prefix = _shared_prefix_length(old, new, limit)
    suffix = _shared_prefix_length(old[::-1], new[::-1], limit - prefix)
    return len(old) - prefix - suffix


def cuts_snapshot(previous: str | None, current: str | None) -> bool:
    """这一次保存是不是「整段删改」：从上一份快照里删掉 / 换掉的可见字（编辑器口径，去空白，与快照的 ``words``
    同一条规则）够了 ``DESTRUCTIVE_EDIT_*`` 的门槛。"""
    old = compact_visible_text(previous)
    threshold = min(DESTRUCTIVE_EDIT_CAP_CHARS, max(DESTRUCTIVE_EDIT_FLOOR_CHARS, len(old) // 2))
    return _removed_chars(old, compact_visible_text(current)) >= threshold


class AuthorDraftRevisionsMixin:
    session: "Session"

    def _snapshot_revision(self, draft: AuthorDraft, *, actor_ref: str, origin: str) -> None:
        """记一份修订快照。自动保存（``edited``）按 5 分钟时段合并（批准 #8）：同一时段里接着上一份自动保存快照写，
        只留这一时段最新的正文。三种情况不并、另起一行：

        - 上一份是晋升过权威正文的那一版（它永远留着）；
        - 上一份是建稿（``created``）或采纳并归档（``adopted``）的那一版：它们永远单独一行。「自动保存 5 分钟至多一份
          + 每次采纳 / 晋升一份」是相加——采纳那一行不会被后来的自动保存改写，采纳本身也不并进这一时段被它替换的
          手写稿（采纳传的来源是 ``adopted``，终审 A-1）；
        - 这一次保存是「整段删改」（``cuts_snapshot``：全选删除、接受一大段改写……）：删改之前的那一版留在上一行，
          同一时段里接着的保存再并进新起的这一行（终审 A-2）。

        写作台停笔 900 毫秒就自动保存一次，过去每存一次就多一行全文快照（一晚上一场几百份），版本对比的列表一次全给。
        """
        existing = self.session.execute(
            select(AuthorDraftRevision.draft_revision_id)
            .where(AuthorDraftRevision.draft_id == draft.draft_id)
            .where(AuthorDraftRevision.revision_no == int(draft.revision_no))
        ).scalar_one_or_none()
        if existing is not None:
            return
        # 合并判断与新行的时间戳用同一个时刻（新行不再取列默认值）
        now = utcnow()
        if origin == "edited":
            latest = self.session.execute(
                select(AuthorDraftRevision)
                .where(AuthorDraftRevision.draft_id == draft.draft_id)
                .order_by(AuthorDraftRevision.revision_no.desc())
                .limit(1)
            ).scalar_one_or_none()
            if (
                latest is not None
                and latest.origin == "edited"
                and latest.revision_no != draft.last_promoted_revision_no
                and _same_revision_window(latest.created_at, now)
                and not cuts_snapshot(latest.content, draft.content)
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
                created_at=now,
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
