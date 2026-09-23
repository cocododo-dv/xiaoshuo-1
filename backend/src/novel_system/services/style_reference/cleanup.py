"""StyleReference 运行时 cleanup:删书(单本 / 批量共用 ``delete_reference_book``)/ 破坏式重新分类时清派生数据,
遥测留存清理。"""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from novel_system.db.models import (
    ReviewItem,
    StyleReferenceBannedTerm,
    StyleReferenceEvidence,
    StyleReferenceExtraction,
    StyleReferenceFinding,
    StyleReferenceFindingFeedback,
    StyleReferenceInjectionBinding,
    StyleReferenceJob,
    StyleReferenceProfile,
    StyleReferenceQuote,
    StyleReferenceRun,
    StyleReferenceValidationReport,
    StyleReferenceWindow,
)


_LOGGER = logging.getLogger(__name__)


def purge_derived_data(session: Session, book_id: str) -> dict[str, int]:
    """删除一本书的全部派生数据,保留段落表与书本身(``delete_book`` 与破坏式重新分类共用)。

    按 FK 反向顺序清 12 张派生表:回测报告 → 绑定 → 禁用词(每个画像)→ 画像 → 发现反馈 →
    证据 → 发现 → 抽取 → 引文 → 抽取 run → 作业(``style_reference_jobs``,该书的分类 / 学习 /
    检查作业;还在跑的工人的条件写随之落空)→ 窗口索引(``style_reference_windows``);外加
    相关 ReviewItem(``review_style_ref_finding_*`` 按发现、``review_style_ref_apply_*`` /
    ``review_style_ref_calib_*`` 按画像的遗留待办行)与每个画像的 RAG 向量索引(向量后端,独立于
    DB 事务,失败只记警告)。

    刻意不删:``style_reference_metric_events``(纯运营遥测,无 book 列,由 ``cleanup_metric_events``
    的 90 天留存独立清理);``style_reference_scene_windows`` / ``style_fidelity_readings``(按场景 /
    画像键,没有书的外键,属于作品侧的历史)。

    flush 但不 commit。返回 {表名: 删除行数} 摘要。
    """
    from novel_system.services.style_reference.repository import (
        StyleReferenceRepository,
    )

    repo = StyleReferenceRepository(session)
    profile_ids = [p.profile_id for p in repo.list_profiles(book_id=book_id)]
    findings = repo.list_findings(book_id=book_id)
    finding_ids = [f.finding_id for f in findings]

    # 立项 C — 删除每个 profile 的三粒度 RAG 向量索引(向量后端,独立于 DB 事务)。
    # delete_rag_index 内部已容错;此处再兜底 import 失败,清理不阻断派生数据删除。
    try:
        from novel_system.services.style_reference.rag import delete_rag_index

        for _pid in profile_ids:
            delete_rag_index(_pid)
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "Style-reference RAG cleanup degraded book_id=%s",
            book_id,
            exc_info=True,
        )

    counts: dict[str, int] = {}

    def _exec(stmt, key: str) -> None:
        result = session.execute(stmt)
        counts[key] = counts.get(key, 0) + int(result.rowcount or 0)

    # 相关 ReviewItem:finding review id 是确定性的(finding_id 后 12 位),
    # apply / calib 按 profile_id 后 12 位做前缀匹配(见 materialization.py)。
    review_ids = {f"review_style_ref_finding_{fid[-12:]}" for fid in finding_ids}
    review_ids.update(f.review_id for f in findings if f.review_id)
    if review_ids:
        _exec(
            delete(ReviewItem).where(ReviewItem.review_id.in_(sorted(review_ids))),
            "review_items",
        )
    for pid in profile_ids:
        suffix = pid[-12:] if len(pid) > 12 else pid
        for prefix in ("review_style_ref_apply_", "review_style_ref_calib_"):
            pattern = f"{prefix}{suffix}_"
            _exec(
                delete(ReviewItem).where(
                    ReviewItem.review_id.startswith(pattern, autoescape=True)
                ),
                "review_items",
            )

    for pid in profile_ids:
        _exec(
            delete(StyleReferenceValidationReport).where(
                StyleReferenceValidationReport.profile_id == pid
            ),
            "validation_reports",
        )
        _exec(
            delete(StyleReferenceInjectionBinding).where(
                StyleReferenceInjectionBinding.profile_id == pid
            ),
            "bindings",
        )
        _exec(
            delete(StyleReferenceBannedTerm).where(
                StyleReferenceBannedTerm.profile_id == pid
            ),
            "banned_terms",
        )
    _exec(
        delete(StyleReferenceProfile).where(StyleReferenceProfile.book_id == book_id),
        "profiles",
    )
    if finding_ids:
        # 立项 B — 删 finding 前先删其用户反馈(FK 未在 SQLite 强制,需显式清理防孤儿)
        _exec(
            delete(StyleReferenceFindingFeedback).where(
                StyleReferenceFindingFeedback.finding_id.in_(finding_ids)
            ),
            "finding_feedback",
        )
        _exec(
            delete(StyleReferenceEvidence).where(
                StyleReferenceEvidence.finding_id.in_(finding_ids)
            ),
            "evidences",
        )
    _exec(
        delete(StyleReferenceFinding).where(StyleReferenceFinding.book_id == book_id),
        "findings",
    )
    _exec(
        delete(StyleReferenceExtraction).where(
            StyleReferenceExtraction.book_id == book_id
        ),
        "extractions",
    )
    _exec(
        delete(StyleReferenceQuote).where(StyleReferenceQuote.book_id == book_id),
        "quotes",
    )
    _exec(
        delete(StyleReferenceRun).where(StyleReferenceRun.book_id == book_id),
        "runs",
    )
    _exec(
        delete(StyleReferenceJob).where(StyleReferenceJob.book_id == book_id),
        "jobs",
    )
    _exec(
        delete(StyleReferenceWindow).where(StyleReferenceWindow.book_id == book_id),
        "windows",
    )
    session.flush()
    # 进程内抄袭语料缓存按 (book_id, checksum) 键;删书/重分类后清一次,
    # 避免陈旧条目滞留(命中键含 checksum,无正确性风险,纯内存卫生)
    try:
        from novel_system.services.style_reference.validation import (
            clear_plagiarism_corpus_cache,
        )

        clear_plagiarism_corpus_cache()
    except Exception:  # noqa: BLE001 — 缓存清理失败不阻断删除
        _LOGGER.warning(
            "Style-reference plagiarism cache cleanup degraded book_id=%s",
            book_id,
            exc_info=True,
        )
    return counts


def delete_reference_book(session: Session, book_id: str) -> dict[str, Any]:
    """删一本参考书(单本删除与书库批量删除共用;flush 但不 commit)。

    同一事务里依次:这本书的活动作业收尾为 cancelled(旧工人的条件写全部落空、不再调模型——删书后同一份
    文本重新导入,旧线程也不会接着把正文发出去,v3 I2)→ 它的生效绑定所在范围内按这本参考做的规划产物作废
    (与解除绑定同一口径)→ :func:`purge_derived_data` 清派生数据 → 段落 → 书。书不存在 404。
    """
    from novel_system.db.models import StyleReferenceBook, StyleReferenceParagraph
    from novel_system.services.errors import DomainError
    from novel_system.services.scene_planning_staleness import supersede_for_binding_scope
    from novel_system.services.style_reference.jobs import StyleJobService

    book = session.get(StyleReferenceBook, str(book_id))
    if book is None:
        raise DomainError(
            "STYLE_REFERENCE_BOOK_NOT_FOUND",
            f"book {book_id!r} not found",
            status_code=404,
        )
    title = book.title
    cancelled = StyleJobService(session).cancel_all_for_book(book_id)
    profile_ids = [
        str(pid)
        for pid in session.scalars(
            select(StyleReferenceProfile.profile_id).where(StyleReferenceProfile.book_id == book_id)
        )
    ]
    unbound: list[dict[str, Any]] = []
    if profile_ids:
        for binding in session.scalars(
            select(StyleReferenceInjectionBinding)
            .where(
                StyleReferenceInjectionBinding.profile_id.in_(profile_ids),
                StyleReferenceInjectionBinding.status == "active",
            )
            .order_by(StyleReferenceInjectionBinding.created_at, StyleReferenceInjectionBinding.binding_id)
        ):
            supersede_for_binding_scope(
                session,
                scope=str(binding.scope),
                scope_ref_id=binding.scope_ref_id,
                reason=f"style_reference_book_deleted:{book_id}",
            )
            unbound.append(
                {"binding_id": binding.binding_id, "scope": binding.scope, "scope_ref_id": binding.scope_ref_id}
            )
    counts = dict(purge_derived_data(session, book_id))
    counts["paragraphs"] = int(
        session.execute(
            delete(StyleReferenceParagraph).where(StyleReferenceParagraph.book_id == book_id)
        ).rowcount
        or 0
    )
    counts["books"] = int(
        session.execute(delete(StyleReferenceBook).where(StyleReferenceBook.book_id == book_id)).rowcount or 0
    )
    session.flush()
    return {
        "book_id": book_id,
        "title": title,
        "deleted": True,
        "cancelled_jobs": list(cancelled),
        "unbound": unbound,
        "counts": counts,
    }


def cleanup_metric_events(
    session: Session,
    *,
    days_threshold: int = 90,
    dry_run: bool = True,
) -> dict[str, Any]:
    """删除 ``style_reference_metric_events`` 中超过 ``days_threshold`` 天的事件。

    ``dry_run=True``(默认)只统计,不执行 DELETE;``dry_run=False`` 真删。
    返回执行摘要 dict,包含 ``deleted_count`` / ``oldest_kept_at`` /
    ``dry_run`` / ``days_threshold`` / ``cutoff`` / ``executed_at``。

    本函数 flush 但不 commit;由调用方(CLI / 测试)负责事务提交。
    """
    from datetime import timedelta

    now = datetime.now(UTC)
    cutoff_dt = now - timedelta(days=int(days_threshold))
    cutoff = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    count_row = session.execute(
        text(
            "SELECT COUNT(*) FROM style_reference_metric_events "
            "WHERE created_at < :cutoff"
        ),
        {"cutoff": cutoff},
    ).scalar()
    deleted_count = int(count_row or 0)

    oldest_kept = session.execute(
        text(
            "SELECT MIN(created_at) FROM style_reference_metric_events "
            "WHERE created_at >= :cutoff"
        ),
        {"cutoff": cutoff},
    ).scalar()

    if not dry_run and deleted_count > 0:
        session.execute(
            text(
                "DELETE FROM style_reference_metric_events WHERE created_at < :cutoff"
            ),
            {"cutoff": cutoff},
        )
        session.flush()

    return {
        "deleted_count": deleted_count,
        "oldest_kept_at": oldest_kept,
        "dry_run": dry_run,
        "days_threshold": int(days_threshold),
        "cutoff": cutoff,
        "executed_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
