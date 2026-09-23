"""参考书来源安全的只读 / 运维入口（风格参考 v3 起建立在唯一抄袭门之上）。

v3 之前这里有一条「来源安全画像」链路：``POST …/books/{id}/safety-profile/extract`` 把按启发式挑的专名 / 特征句 /
场景桥写进 ``profile_json.source_safety``，运行时扫描再读这个键——可现有画像没有一份带着它，扫描从来没拿绑定的
书比对过（参考书连续 60 字照抄也判「安全」）。那条链路与读取路径都已删除；进正文的每条路径改走
``services/reference_copy_gate.check_reference_copy``（12 字 n-gram 对绑定的书 + 受保护专名）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBannedTerm, StyleReferenceProfile
from novel_system.services.reference_copy_gate import check_reference_copy


class ReferenceSafetyService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def overview(self) -> dict[str, Any]:
        """每份画像的受保护专名（生成期禁用词，含 P3 的 protected_auto）计数——抄袭门拿它们比对。"""
        profiles = self.session.execute(
            select(StyleReferenceProfile).order_by(
                StyleReferenceProfile.created_at.desc(),
                StyleReferenceProfile.profile_id.desc(),
            )
        ).scalars().all()
        counts = {
            str(profile_id): int(count or 0)
            for profile_id, count in self.session.execute(
                select(StyleReferenceBannedTerm.profile_id, func.count(StyleReferenceBannedTerm.term_id))
                .where(StyleReferenceBannedTerm.scope == "generation")
                .group_by(StyleReferenceBannedTerm.profile_id)
            ).all()
        }
        items = [
            {
                "profile_id": profile.profile_id,
                "book_id": profile.book_id,
                "run_id": profile.run_id,
                "title": profile.title,
                "status": profile.status,
                "protected_term_count": counts.get(str(profile.profile_id), 0),
                "created_at": profile.created_at,
                "updated_at": profile.updated_at,
            }
            for profile in profiles
        ]
        return {
            "summary": {
                "profile_count": len(items),
                "ready_profile_count": sum(1 for item in items if item["status"] in {"ready", "active"}),
                "profile_with_protected_terms_count": sum(1 for item in items if item["protected_term_count"]),
            },
            "items": items,
        }

    def scan_text(
        self,
        *,
        text: str,
        source_profile_ids: list[str] | None = None,
        object_ref: str | None = None,
    ) -> dict[str, Any]:
        """按指定画像的书与受保护专名跑一次抄袭门（没指定画像时只查环境变量里的全局专名）。"""
        profile_ids = [item for item in (source_profile_ids or []) if isinstance(item, str) and item.strip()]
        book_ids: list[str] = []
        if profile_ids:
            book_ids = [
                str(book_id)
                for book_id in self.session.execute(
                    select(StyleReferenceProfile.book_id).where(StyleReferenceProfile.profile_id.in_(profile_ids))
                ).scalars().all()
                if book_id
            ]
        check = check_reference_copy(
            self.session, text, book_ids=list(dict.fromkeys(book_ids)), profile_ids=profile_ids
        )
        result = check.audit()
        result["object_ref"] = object_ref
        result["profile_count"] = len(profile_ids)
        return result
