"""style_reference_* 表的 ORM CRUD(只留有调用方的方法;2026-09-23 v3 清掉了无人调用的删改方法)。
所有方法操作 session 但不 commit;由调用方在 service / route 层统一提交。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _compute_statement_hash(statement: str) -> str:
    """统一 statement → SHA256[:16] 计算,供 UNIQUE 复合 4 列约束使用。

    PR-3 hotfix 0038:同 (extraction_id, sub_dim, finding_kind, statement_hash)
    唯一,允许同 sub_dim 同 kind 多条不同 statement 的 finding。
    """
    return hashlib.sha256((statement or "").strip().encode("utf-8")).hexdigest()[:16]

from novel_system.db.models import (
    StyleReferenceBannedTerm,
    StyleReferenceBook,
    StyleReferenceEvidence,
    StyleReferenceExtraction,
    StyleReferenceFinding,
    StyleReferenceFindingFeedback,
    StyleReferenceInjectionBinding,
    StyleReferenceMetricEvent,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceQuote,
    StyleReferenceRun,
    StyleReferenceValidationReport,
)


class StyleReferenceRepository:
    """11 张 style_reference_* 表的统一仓储。

    用法::

        repo = StyleReferenceRepository(session)
        book = repo.create_book(book_id="sr_book_1", title="夜雨样章集", ...)
        repo.session.commit()
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ books
    def create_book(self, **kwargs: Any) -> StyleReferenceBook:
        row = StyleReferenceBook(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def get_book(self, book_id: str) -> StyleReferenceBook | None:
        return self.session.get(StyleReferenceBook, book_id)

    def list_books(self, *, status: str | None = None) -> list[StyleReferenceBook]:
        stmt = select(StyleReferenceBook)
        if status is not None:
            stmt = stmt.where(StyleReferenceBook.status == status)
        return list(self.session.scalars(stmt).all())

    def delete_book(self, book_id: str) -> int:
        result = self.session.execute(
            delete(StyleReferenceBook).where(StyleReferenceBook.book_id == book_id)
        )
        return int(result.rowcount or 0)

    # ------------------------------------------------------------- paragraphs
    def create_paragraph(self, **kwargs: Any) -> StyleReferenceParagraph:
        row = StyleReferenceParagraph(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def get_paragraph(self, paragraph_id: str) -> StyleReferenceParagraph | None:
        return self.session.get(StyleReferenceParagraph, paragraph_id)

    def list_paragraphs(
        self,
        book_id: str,
        *,
        paragraph_type: str | None = None,
    ) -> list[StyleReferenceParagraph]:
        stmt = (
            select(StyleReferenceParagraph)
            .where(StyleReferenceParagraph.book_id == book_id)
            .order_by(StyleReferenceParagraph.paragraph_index)
        )
        if paragraph_type is not None:
            stmt = stmt.where(StyleReferenceParagraph.paragraph_type == paragraph_type)
        return list(self.session.scalars(stmt).all())

    def delete_paragraphs_for_book(self, book_id: str) -> int:
        result = self.session.execute(
            delete(StyleReferenceParagraph).where(StyleReferenceParagraph.book_id == book_id)
        )
        return int(result.rowcount or 0)

    # ----------------------------------------------------------------- runs
    def create_run(self, **kwargs: Any) -> StyleReferenceRun:
        row = StyleReferenceRun(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def get_run(self, run_id: str) -> StyleReferenceRun | None:
        return self.session.get(StyleReferenceRun, run_id)

    def list_runs(
        self,
        *,
        book_id: str | None = None,
        status: str | None = None,
    ) -> list[StyleReferenceRun]:
        stmt = select(StyleReferenceRun)
        if book_id is not None:
            stmt = stmt.where(StyleReferenceRun.book_id == book_id)
        if status is not None:
            stmt = stmt.where(StyleReferenceRun.status == status)
        return list(self.session.scalars(stmt).all())

    def update_run(self, run_id: str, **updates: Any) -> StyleReferenceRun | None:
        run = self.get_run(run_id)
        if run is None:
            return None
        for key, value in updates.items():
            setattr(run, key, value)
        self.session.flush()
        return run

    # ----------------------------------------------------------- extractions
    def create_extraction(self, **kwargs: Any) -> StyleReferenceExtraction:
        row = StyleReferenceExtraction(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def list_extractions(
        self,
        *,
        book_id: str | None = None,
        run_id: str | None = None,
        layer: str | None = None,
        sub_dimension: str | None = None,
    ) -> list[StyleReferenceExtraction]:
        stmt = select(StyleReferenceExtraction)
        if book_id is not None:
            stmt = stmt.where(StyleReferenceExtraction.book_id == book_id)
        if run_id is not None:
            stmt = stmt.where(StyleReferenceExtraction.run_id == run_id)
        if layer is not None:
            stmt = stmt.where(StyleReferenceExtraction.layer == layer)
        if sub_dimension is not None:
            stmt = stmt.where(StyleReferenceExtraction.sub_dimension == sub_dimension)
        return list(self.session.scalars(stmt).all())

    # --------------------------------------------------------------- quotes
    def create_quote(self, **kwargs: Any) -> StyleReferenceQuote:
        row = StyleReferenceQuote(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def get_quote(self, quote_id: str) -> StyleReferenceQuote | None:
        return self.session.get(StyleReferenceQuote, quote_id)

    def list_quotes(self, book_id: str) -> list[StyleReferenceQuote]:
        stmt = select(StyleReferenceQuote).where(StyleReferenceQuote.book_id == book_id)
        return list(self.session.scalars(stmt).all())

    def list_quotes_by_ids(self, quote_ids: list[str]) -> list[StyleReferenceQuote]:
        """批量 IN 查询(PR-23 evidence 读路径,避免逐条 get)。"""
        if not quote_ids:
            return []
        stmt = select(StyleReferenceQuote).where(
            StyleReferenceQuote.quote_id.in_(quote_ids)
        )
        return list(self.session.scalars(stmt).all())

    # ------------------------------------------------------------ evidences
    def create_evidence(self, **kwargs: Any) -> StyleReferenceEvidence:
        row = StyleReferenceEvidence(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def list_evidences_for_findings(
        self, finding_ids: list[str]
    ) -> list[StyleReferenceEvidence]:
        """批量 IN 查询(PR-23 evidence 读路径,避免逐条 get)。"""
        if not finding_ids:
            return []
        stmt = select(StyleReferenceEvidence).where(
            StyleReferenceEvidence.finding_id.in_(finding_ids)
        )
        return list(self.session.scalars(stmt).all())

    # ------------------------------------------------------------- findings
    def create_finding(self, **kwargs: Any) -> StyleReferenceFinding:
        """创建 finding 行。

        若 caller 未传 statement_hash,自动从 statement 计算 SHA256[:16] 填入;
        若 caller 显式传值则尊重 caller 选择(便于测试构造冲突场景)。
        """
        if "statement_hash" not in kwargs:
            statement = kwargs.get("statement", "")
            kwargs["statement_hash"] = _compute_statement_hash(statement)
        row = StyleReferenceFinding(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def get_finding(self, finding_id: str) -> StyleReferenceFinding | None:
        return self.session.get(StyleReferenceFinding, finding_id)

    def list_findings(
        self,
        *,
        book_id: str | None = None,
        run_id: str | None = None,
        sub_dimension: str | None = None,
        finding_kind: str | None = None,
        status: str | None = None,
    ) -> list[StyleReferenceFinding]:
        stmt = select(StyleReferenceFinding)
        if book_id is not None:
            stmt = stmt.where(StyleReferenceFinding.book_id == book_id)
        if run_id is not None:
            stmt = stmt.where(StyleReferenceFinding.run_id == run_id)
        if sub_dimension is not None:
            stmt = stmt.where(StyleReferenceFinding.sub_dimension == sub_dimension)
        if finding_kind is not None:
            stmt = stmt.where(StyleReferenceFinding.finding_kind == finding_kind)
        if status is not None:
            stmt = stmt.where(StyleReferenceFinding.status == status)
        return list(self.session.scalars(stmt).all())

    def update_finding(self, finding_id: str, **updates: Any) -> StyleReferenceFinding | None:
        row = self.get_finding(finding_id)
        if row is None:
            return None
        for key, value in updates.items():
            setattr(row, key, value)
        self.session.flush()
        return row

    # ------------------------------------------------- finding feedback(立项 B)
    def upsert_finding_feedback(
        self, finding_id: str, operator_ref: str, vote: str
    ) -> StyleReferenceFindingFeedback:
        """一人一票:同 (finding_id, operator_ref) 存在则更新 vote(改向),否则新建。

        feedback_id 由 (finding_id, operator_ref) 确定性派生(SHA256[:16]),幂等可重入。
        """
        existing = self.session.scalars(
            select(StyleReferenceFindingFeedback).where(
                StyleReferenceFindingFeedback.finding_id == finding_id,
                StyleReferenceFindingFeedback.operator_ref == operator_ref,
            )
        ).first()
        if existing is not None:
            existing.vote = vote
            self.session.flush()
            return existing
        fid = "srfb_" + hashlib.sha256(
            f"{finding_id}::{operator_ref}".encode("utf-8")
        ).hexdigest()[:16]
        row = StyleReferenceFindingFeedback(
            feedback_id=fid, finding_id=finding_id, operator_ref=operator_ref, vote=vote
        )
        # SAVEPOINT 兜底并发竞态:select 落空后两请求同时 insert 同 (finding,operator)
        # → 第二个命中 uq/PK,回退该 savepoint(外层事务不受损)→ 退化为更新既有行。
        try:
            with self.session.begin_nested():
                self.session.add(row)
            return row
        except IntegrityError:
            existing = self.session.scalars(
                select(StyleReferenceFindingFeedback).where(
                    StyleReferenceFindingFeedback.finding_id == finding_id,
                    StyleReferenceFindingFeedback.operator_ref == operator_ref,
                )
            ).first()
            if existing is not None:
                existing.vote = vote
                self.session.flush()
                return existing
            raise

    def list_finding_feedback(self, finding_id: str) -> list[StyleReferenceFindingFeedback]:
        return list(
            self.session.scalars(
                select(StyleReferenceFindingFeedback).where(
                    StyleReferenceFindingFeedback.finding_id == finding_id
                )
            ).all()
        )

    def aggregate_finding_feedback(self, finding_id: str) -> dict[str, int]:
        """聚合 net = #up − #down(每行已是去重用户的最新一票,由 uq 约束保证)。"""
        rows = self.list_finding_feedback(finding_id)
        up = sum(1 for r in rows if r.vote == "up")
        down = sum(1 for r in rows if r.vote == "down")
        return {"net": up - down, "up": up, "down": down}

    def operator_votes_for_findings(
        self, finding_ids: list[str], operator_ref: str
    ) -> dict[str, str]:
        """批量取某 operator 对一组 finding 的当前票 → {finding_id: vote}(单查询)。
        供深层 findings 序列化回显「该用户已投的票」,使投票高亮跨刷新持久。"""
        if not finding_ids:
            return {}
        rows = self.session.scalars(
            select(StyleReferenceFindingFeedback).where(
                StyleReferenceFindingFeedback.finding_id.in_(finding_ids),
                StyleReferenceFindingFeedback.operator_ref == operator_ref,
            )
        ).all()
        return {r.finding_id: r.vote for r in rows}

    # ------------------------------------------------------------- profiles
    def create_profile(self, **kwargs: Any) -> StyleReferenceProfile:
        row = StyleReferenceProfile(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def get_profile(self, profile_id: str) -> StyleReferenceProfile | None:
        return self.session.get(StyleReferenceProfile, profile_id)

    def list_profiles(
        self,
        *,
        book_id: str | None = None,
        status: str | None = None,
    ) -> list[StyleReferenceProfile]:
        stmt = select(StyleReferenceProfile)
        if book_id is not None:
            stmt = stmt.where(StyleReferenceProfile.book_id == book_id)
        if status is not None:
            stmt = stmt.where(StyleReferenceProfile.status == status)
        return list(self.session.scalars(stmt).all())

    def update_profile(self, profile_id: str, **updates: Any) -> StyleReferenceProfile | None:
        row = self.get_profile(profile_id)
        if row is None:
            return None
        for key, value in updates.items():
            setattr(row, key, value)
        self.session.flush()
        return row

    # ---------------------------------------------------- injection bindings
    def create_binding(self, **kwargs: Any) -> StyleReferenceInjectionBinding:
        row = StyleReferenceInjectionBinding(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def get_binding(self, binding_id: str) -> StyleReferenceInjectionBinding | None:
        return self.session.get(StyleReferenceInjectionBinding, binding_id)

    def list_bindings(
        self,
        *,
        profile_id: str | None = None,
        task_type: str | None = None,
    ) -> list[StyleReferenceInjectionBinding]:
        stmt = select(StyleReferenceInjectionBinding)
        if profile_id is not None:
            stmt = stmt.where(StyleReferenceInjectionBinding.profile_id == profile_id)
        if task_type is not None:
            stmt = stmt.where(StyleReferenceInjectionBinding.task_type == task_type)
        return list(self.session.scalars(stmt).all())

    def delete_binding(self, binding_id: str) -> int:
        result = self.session.execute(
            delete(StyleReferenceInjectionBinding).where(
                StyleReferenceInjectionBinding.binding_id == binding_id
            )
        )
        return int(result.rowcount or 0)

    # -------------------------------------------------- validation reports
    def create_validation_report(self, **kwargs: Any) -> StyleReferenceValidationReport:
        row = StyleReferenceValidationReport(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def get_validation_report(self, report_id: str) -> StyleReferenceValidationReport | None:
        return self.session.get(StyleReferenceValidationReport, report_id)

    def list_validation_reports(
        self,
        *,
        profile_id: str | None = None,
        verdict: str | None = None,
    ) -> list[StyleReferenceValidationReport]:
        stmt = select(StyleReferenceValidationReport)
        if profile_id is not None:
            stmt = stmt.where(StyleReferenceValidationReport.profile_id == profile_id)
        if verdict is not None:
            stmt = stmt.where(StyleReferenceValidationReport.verdict == verdict)
        return list(self.session.scalars(stmt).all())

    # ---------------------------------------------------------- banned terms
    def create_banned_term(self, **kwargs: Any) -> StyleReferenceBannedTerm:
        row = StyleReferenceBannedTerm(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def list_banned_terms(
        self,
        profile_id: str,
        *,
        scope: str | None = None,
    ) -> list[StyleReferenceBannedTerm]:
        stmt = select(StyleReferenceBannedTerm).where(
            StyleReferenceBannedTerm.profile_id == profile_id
        )
        if scope is not None:
            stmt = stmt.where(StyleReferenceBannedTerm.scope == scope)
        return list(self.session.scalars(stmt).all())

    def get_banned_term(self, term_id: str) -> StyleReferenceBannedTerm | None:
        return self.session.get(StyleReferenceBannedTerm, term_id)

    def find_banned_term(
        self, profile_id: str, term: str, scope: str
    ) -> StyleReferenceBannedTerm | None:
        """按唯一约束 (profile_id, term, scope) 查重,用于幂等创建。"""
        stmt = select(StyleReferenceBannedTerm).where(
            StyleReferenceBannedTerm.profile_id == profile_id,
            StyleReferenceBannedTerm.term == term,
            StyleReferenceBannedTerm.scope == scope,
        )
        return self.session.scalars(stmt).first()

    def list_banned_terms_for_book(
        self,
        book_id: str,
        *,
        scope: str | None = None,
    ) -> list[StyleReferenceBannedTerm]:
        """一本书全部 profile 的禁用词并集(extraction 域抽取过滤用)。"""
        stmt = (
            select(StyleReferenceBannedTerm)
            .join(
                StyleReferenceProfile,
                StyleReferenceBannedTerm.profile_id == StyleReferenceProfile.profile_id,
            )
            .where(StyleReferenceProfile.book_id == book_id)
        )
        if scope is not None:
            stmt = stmt.where(StyleReferenceBannedTerm.scope == scope)
        return list(self.session.scalars(stmt).all())

    def delete_banned_term(self, term_id: str) -> int:
        result = self.session.execute(
            delete(StyleReferenceBannedTerm).where(StyleReferenceBannedTerm.term_id == term_id)
        )
        return int(result.rowcount or 0)

    # -------------------------------------------------- PR-10 metric events
    def create_metric_event(self, **kwargs: Any) -> StyleReferenceMetricEvent:
        row = StyleReferenceMetricEvent(**kwargs)
        self.session.add(row)
        self.session.flush()
        return row

    def list_metric_events(
        self,
        *,
        event_kind: str | None = None,
        profile_id: str | None = None,
        since_ts: str | None = None,
        limit: int | None = None,
    ) -> list[StyleReferenceMetricEvent]:
        stmt = select(StyleReferenceMetricEvent)
        if event_kind is not None:
            stmt = stmt.where(StyleReferenceMetricEvent.event_kind == event_kind)
        if profile_id is not None:
            stmt = stmt.where(StyleReferenceMetricEvent.profile_id == profile_id)
        if since_ts is not None:
            stmt = stmt.where(StyleReferenceMetricEvent.created_at >= since_ts)
        stmt = stmt.order_by(StyleReferenceMetricEvent.created_at.desc())
        if limit is not None:
            stmt = stmt.limit(int(limit))
        return list(self.session.scalars(stmt).all())
