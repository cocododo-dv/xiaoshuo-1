"""风格参考：书 / 段落 / 学习血缘（run · 抽取 · 引文 · 发现 · 证据）/ 画像 / 绑定 / 禁用词 / 遥测，
以及 v3（迁移 0090）的作业 / 窗口索引 / 读数 / 每场冻结选窗。现行说明见 docs/style-reference.md；
旧回测报告表、发现反馈表与 base_confidence 列由迁移 0091 删除。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    JSON,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow


class StyleReferenceBook(Base):
    __tablename__ = "style_reference_books"
    __table_args__ = (
        UniqueConstraint("text_checksum", name="uq_style_reference_books_text_checksum"),
        Index("ix_style_reference_books_status_updated_at", "status", "updated_at"),
    )

    book_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    author_label: Mapped[str | None] = mapped_column(String, nullable=True)
    source_kind: Mapped[str] = mapped_column(String)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    cloud_policy: Mapped[str] = mapped_column(String)
    text_checksum: Mapped[str] = mapped_column(String)
    total_chars: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="pending")
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceParagraph(Base):
    __tablename__ = "style_reference_paragraphs"
    __table_args__ = (
        Index(
            "ix_style_reference_paragraphs_book_type",
            "book_id",
            "paragraph_type",
        ),
        Index(
            "ix_style_reference_paragraphs_book_index",
            "book_id",
            "paragraph_index",
        ),
    )

    paragraph_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    paragraph_index: Mapped[int] = mapped_column(Integer)
    paragraph_type: Mapped[str] = mapped_column(String)
    start_offset: Mapped[int] = mapped_column(Integer)
    end_offset: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)
    classifier_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class StyleReferenceRun(Base):
    __tablename__ = "style_reference_runs"
    __table_args__ = (
        Index("ix_style_reference_runs_book_status", "book_id", "status"),
        Index("ix_style_reference_runs_dispatch_state", "dispatch_state"),
    )

    run_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    status: Mapped[str] = mapped_column(String, default="pending")
    phase: Mapped[str] = mapped_column(String, default="ingest")
    # ``status`` describes the domain run while ``dispatch_state`` describes
    # durable background ownership.  Keeping them separate lets startup
    # recovery re-dispatch work that never started without pretending that a
    # partially executed extraction can be resumed safely.
    dispatch_state: Mapped[str] = mapped_column(String, default="completed")
    requested_layers_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    coverage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    heartbeat_at: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceExtraction(Base):
    __tablename__ = "style_reference_extractions"
    __table_args__ = (
        Index(
            "ix_style_reference_extractions_book_layer_sub",
            "book_id",
            "layer",
            "sub_dimension",
        ),
        Index(
            "ix_style_reference_extractions_run_status",
            "run_id",
            "status",
        ),
    )

    extraction_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("style_reference_runs.run_id"))
    layer: Mapped[str] = mapped_column(String)
    sub_dimension: Mapped[str] = mapped_column(String)
    llm_call_id: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="pending")
    validation_errors_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    purpose: Mapped[str] = mapped_column(String, default="extract")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceQuote(Base):
    __tablename__ = "style_reference_quotes"
    __table_args__ = (
        Index("ix_style_reference_quotes_book", "book_id"),
    )

    quote_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    # paragraph_id 可空:支持 anchor_kind=counter_example 的合成 quote 不指向真实段落
    paragraph_id: Mapped[str | None] = mapped_column(
        ForeignKey("style_reference_paragraphs.paragraph_id"), nullable=True
    )
    span_start: Mapped[int] = mapped_column(Integer)
    span_end: Mapped[int] = mapped_column(Integer)
    quote_text: Mapped[str] = mapped_column(Text)
    illustrates_dims: Mapped[list[str]] = mapped_column(JSON, default=list)
    extracted_features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class StyleReferenceFinding(Base):
    __tablename__ = "style_reference_findings"
    __table_args__ = (
        Index(
            "ix_style_reference_findings_book_sub_kind",
            "book_id",
            "sub_dimension",
            "finding_kind",
        ),
        UniqueConstraint("review_id", name="uq_style_reference_findings_review_id"),
        # PR-3 hotfix 0038:UNIQUE 复合 4 列(原 3 列与 §6.5 0-8 条 obs 输出矛盾)
        # 详见 plans/style-reference-v1-1-fancy-shannon.md §"v1.2 文档修订清单 #8"
        UniqueConstraint(
            "extraction_id",
            "sub_dimension",
            "finding_kind",
            "statement_hash",
            name="uq_style_reference_findings_extract_sub_kind_hash",
        ),
    )

    finding_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("style_reference_runs.run_id"))
    extraction_id: Mapped[str] = mapped_column(
        ForeignKey("style_reference_extractions.extraction_id")
    )
    sub_dimension: Mapped[str] = mapped_column(String)
    finding_kind: Mapped[str] = mapped_column(String)
    statement: Mapped[str] = mapped_column(Text)
    # PR-3 hotfix 0038:statement 的 SHA256[:16],用于 UNIQUE 复合;应用层 / repository
    # 在 create_finding 时自动填充
    statement_hash: Mapped[str] = mapped_column(String)
    confidence: Mapped[str] = mapped_column(String, default="medium")
    status: Mapped[str] = mapped_column(String, default="pending")
    review_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceEvidence(Base):
    __tablename__ = "style_reference_evidences"
    __table_args__ = (
        UniqueConstraint(
            "finding_id",
            "quote_id",
            name="uq_style_reference_evidences_finding_quote",
        ),
    )

    evidence_id: Mapped[str] = mapped_column(String, primary_key=True)
    finding_id: Mapped[str] = mapped_column(ForeignKey("style_reference_findings.finding_id"))
    quote_id: Mapped[str] = mapped_column(ForeignKey("style_reference_quotes.quote_id"))
    anchor_kind: Mapped[str] = mapped_column(String)
    is_synthetic: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class StyleReferenceProfile(Base):
    __tablename__ = "style_reference_profiles"
    __table_args__ = (
        Index(
            "ix_style_reference_profiles_book_status",
            "book_id",
            "status",
        ),
        Index("ix_style_reference_profiles_version_tag", "version_tag"),
    )

    profile_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("style_reference_runs.run_id"))
    title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="draft")
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    coverage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_finding_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    version_tag: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceInjectionBinding(Base):
    __tablename__ = "style_reference_injection_bindings"
    __table_args__ = (
        Index(
            "ix_style_reference_injection_bindings_profile_scope_ref",
            "profile_id",
            "scope",
            "scope_ref_id",
        ),
        Index(
            "ix_style_reference_injection_bindings_task_type",
            "task_type",
        ),
        # 并发 apply 的「先查后建」竞态兜底:同 (profile, scope, scope_ref, task)
        # 不允许重复 binding(否则注入选取顺序不确定)
        UniqueConstraint(
            "profile_id",
            "scope",
            "scope_ref_id",
            "task_type",
            name="uq_style_reference_injection_bindings_target",
        ),
    )

    binding_id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("style_reference_profiles.profile_id"))
    scope: Mapped[str] = mapped_column(String)
    scope_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    task_type: Mapped[str] = mapped_column(String)
    strategy: Mapped[str] = mapped_column(String)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceBannedTerm(Base):
    __tablename__ = "style_reference_banned_terms"
    __table_args__ = (
        UniqueConstraint(
            "profile_id",
            "term",
            "scope",
            name="uq_style_reference_banned_terms_profile_term_scope",
        ),
    )

    term_id: Mapped[str] = mapped_column(String, primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("style_reference_profiles.profile_id"))
    term: Mapped[str] = mapped_column(String)
    replacement_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(String, default="generation")
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleReferenceMetricEvent(Base):
    """风格参考的审计事件流(append-only,无 FK;写入口 ``metrics_recorder.MetricsRecorder``)。

    现在只有 qc_engine 的两道门在写:``styled_draft_gate_decided``(风格稿门:抄袭门 + 生成禁用词)与
    ``qc_gate_decided``(中性步位稿的原文重合门)。没有聚合端点,这些行只作审计,由
    ``cleanup.cleanup_metric_events`` 按 90 天留存清理;旧库里残留的 ``injection_invoked`` /
    ``validation_executed`` / ``style_drift_observed`` 等旧种类不再被写,也不再被读。
    """

    __tablename__ = "style_reference_metric_events"
    __table_args__ = (
        Index("ix_sr_metric_events_kind_created", "event_kind", "created_at"),
        Index("ix_sr_metric_events_profile_created", "profile_id", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_kind: Mapped[str] = mapped_column(String)
    target_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    target_ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    binding_id: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class StyleReferenceJob(Base):
    """风格参考 v3（2026-09-23）— 统一的持久作业表（迁移 0090）。

    ``kind`` ∈ classify / learn / check。一个作业一行；工人认领时 ``attempt`` +1 并换一枚新的
    ``owner_token``，此后的每一次写（心跳 / 进度 / 游标 / 结束）都以「owner_token 仍是自己」为条件——
    被清扫重排、被取消、被删书的作业，旧工人的写全部落空，自然停下。心跳过期的 running 作业由常驻
    清扫线程放回 queued，重启或 ``--reload`` 之后不需要人工介入。
    """

    __tablename__ = "style_reference_jobs"
    __table_args__ = (
        Index("ix_style_reference_jobs_book_kind_state", "book_id", "kind", "state"),
        Index("ix_style_reference_jobs_state_heartbeat", "state", "heartbeat_at"),
    )

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String)
    book_id: Mapped[str | None] = mapped_column(
        ForeignKey("style_reference_books.book_id"), nullable=True
    )
    profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    op_key: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, default="queued")
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    owner_token: Mapped[str | None] = mapped_column(String, nullable=True)
    heartbeat_at: Mapped[str | None] = mapped_column(String, nullable=True)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cursor_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    progress_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)
    started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String, nullable=True)


class StyleReferenceWindow(Base):
    """风格参考 v3 — 持久化的全书样例窗口索引（迁移 0090）。

    一本书在一个索引版本 + 一个段落根哈希下的一组连续窗口；``features_json`` 是测量核在这一窗上的
    特征（「像不像」读数的参照分布就是作者自己这些窗口的分布），``tags_json`` 是学习作业里模型打的
    场面 / 情绪 / 手法标签（按本场挑样例用）。段落表变了（根哈希变了）就整组重建。
    """

    __tablename__ = "style_reference_windows"
    __table_args__ = (
        UniqueConstraint(
            "book_id",
            "index_version",
            "window_no",
            name="uq_style_reference_windows_book_version_no",
        ),
        Index("ix_style_reference_windows_book_version_chapter", "book_id", "index_version", "chapter_no"),
    )

    window_id: Mapped[str] = mapped_column(String, primary_key=True)
    book_id: Mapped[str] = mapped_column(ForeignKey("style_reference_books.book_id"))
    index_version: Mapped[str] = mapped_column(String)
    root_sha256: Mapped[str] = mapped_column(String)
    window_no: Mapped[int] = mapped_column(Integer)
    start_index: Mapped[int] = mapped_column(Integer)
    end_index: Mapped[int] = mapped_column(Integer)
    chapter_no: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[str] = mapped_column(String, default="middle")
    chars: Mapped[int] = mapped_column(Integer, default=0)
    paragraph_count: Mapped[int] = mapped_column(Integer, default=0)
    type_mix_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dialogue_share: Mapped[float] = mapped_column(Float, default=0.0)
    typicality: Mapped[float] = mapped_column(Float, default=0.0)
    features_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tags_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow, onupdate=utcnow)


class StyleFidelityReading(Base):
    """风格参考 v3 — 一份文字「像不像参考」的读数（迁移 0090）。

    ``percentile`` / ``distance`` 是对作者自己窗口分布的确定性读数；``reading_json`` 带越界特征
    （特征 → 维度 → 白话短语）与按维确定性分；``judge_json`` 是参考评审（软 QC / 对照检查）按 16 维
    给的分。刻意不存 chapter_id：场景改章时读数不必跟着搬（场景 → 章从 SceneCard 现查）。
    """

    __tablename__ = "style_fidelity_readings"
    __table_args__ = (
        Index("ix_style_fidelity_readings_scene_created", "scene_id", "created_at"),
        Index("ix_style_fidelity_readings_project_created", "project_id", "created_at"),
        Index("ix_style_fidelity_readings_profile_created", "profile_id", "created_at"),
    )

    reading_id: Mapped[str] = mapped_column(String, primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_id: Mapped[str | None] = mapped_column(String, nullable=True)
    binding_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String)
    stage: Mapped[str] = mapped_column(String)
    draft_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    text_sha256: Mapped[str] = mapped_column(String)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance: Mapped[float | None] = mapped_column(Float, nullable=True)
    reading_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    judge_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    copy_check_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class StyleReferenceSceneWindows(Base):
    """风格参考 v3 — 一场冻结的选窗（迁移 0090）。

    ``selection_key`` = sha256(bundle_id | 契约哈希 | scene_id | 选窗参数版本)；同一场的首稿、修改、
    评审、补丁读同一行，所以同一场的所有工序看到同一组窗（评审 / 规划节点取其中前几窗）。
    """

    __tablename__ = "style_reference_scene_windows"
    __table_args__ = (
        UniqueConstraint("selection_key", name="uq_style_reference_scene_windows_key"),
        Index("ix_style_reference_scene_windows_scene", "scene_id", "created_at"),
    )

    selection_id: Mapped[str] = mapped_column(String, primary_key=True)
    selection_key: Mapped[str] = mapped_column(String)
    scene_id: Mapped[str | None] = mapped_column(String, nullable=True)
    bundle_id: Mapped[str | None] = mapped_column(String, nullable=True)
    contract_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    window_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


__all__ = [
    "StyleFidelityReading",
    "StyleReferenceBannedTerm",
    "StyleReferenceBook",
    "StyleReferenceEvidence",
    "StyleReferenceExtraction",
    "StyleReferenceFinding",
    "StyleReferenceInjectionBinding",
    "StyleReferenceJob",
    "StyleReferenceMetricEvent",
    "StyleReferenceParagraph",
    "StyleReferenceProfile",
    "StyleReferenceQuote",
    "StyleReferenceRun",
    "StyleReferenceSceneWindows",
    "StyleReferenceWindow",
]
