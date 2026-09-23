"""Style Reference (style_reference) service package.

风格参考 v3（2026-09-23）的现行契约见 ``docs/style-reference-v3-2026-09-23.md``。学习链路是一个持久作业
（``learn_job``：窗口 → 选窗 → 分层抽取 → 文风卡 → 受保护专名 → 窗口标签 → 画像），处理器在模块导入时注册
（``register_job_handler("learn", ...)``），这里不导入它，免得包导入就拉起整条学习链路。
"""

from __future__ import annotations

from novel_system.services.style_reference.dimensions import (
    LAYER_TO_SUB_DIMS,
    Layer,
    SubDimension,
)
from novel_system.services.style_reference.errors import (
    DuplicateBookError,
    EmptyBookError,
    LLMRequiredError,
    StyleReferenceError,
)
from novel_system.services.style_reference.ingest import IngestResult, IngestService, assess_input_size
from novel_system.services.style_reference.injection import InjectionService
from novel_system.services.style_reference.materialization import (
    MaterializationService,
    MaterializeResult,
)
from novel_system.services.style_reference.preview import PreviewService
from novel_system.services.style_reference.validation import (
    ValidationOrchestrator,
    check_forbidden_local,
    check_forbidden_semantic,
    check_plagiarism,
    check_quantitative,
    check_semantic,
    run_sync_validate,
)
from novel_system.services.style_reference.metrics import (
    METRIC_NAMES,
    MetricName,
    MetricsEngine,
    ParagraphRecord,
)
from novel_system.services.style_reference.segmentation import (
    ParagraphClassification,
    SegmentationLLMError,
    SegmentationResult,
    classify_paragraphs,
)
from novel_system.services.style_reference.schemas import (
    AnchorKind,
    BannedTermScope,
    BindingScope,
    BindingStatus,
    CloudPolicy,
    ExtractionEvidenceInput,
    ExtractionPurpose,
    FindingKind,
    InjectionStrategy,
    ParagraphType,
    ProfileStatus,
    RunPhase,
    RunStatus,
    TaskType,
    ValidationMode,
    ValidationTargetKind,
    ValidationVerdict,
    PlagiarismHit,
    PlagiarismReport,
    ForbiddenHit,
    ValidationReport,
    PreviewGeneratedSample,
    PreviewSampleResult,
    InjectionPreviewRequest,
    InjectionPreviewResponse,
    QuantitativeReportItem,
    SemanticReportItem,
    SystemPromptFragments,
    ValidateRequest,
    ValidateResponse,
)

__all__ = [
    "Layer",
    "SubDimension",
    "LAYER_TO_SUB_DIMS",
    "ParagraphType",
    "FindingKind",
    "AnchorKind",
    "ExtractionPurpose",
    "RunStatus",
    "RunPhase",
    "ProfileStatus",
    "BindingScope",
    "BindingStatus",
    "InjectionStrategy",
    "TaskType",
    "ValidationVerdict",
    "ValidationMode",
    "ValidationTargetKind",
    "BannedTermScope",
    "CloudPolicy",
    "StyleReferenceError",
    "DuplicateBookError",
    "EmptyBookError",
    "LLMRequiredError",
    "ExtractionEvidenceInput",
    "PlagiarismHit",
    "PlagiarismReport",
    "ForbiddenHit",
    "ValidationReport",
    "PreviewGeneratedSample",
    "PreviewSampleResult",
    "InjectionPreviewRequest",
    "InjectionPreviewResponse",
    "QuantitativeReportItem",
    "SemanticReportItem",
    "SystemPromptFragments",
    "ValidateRequest",
    "ValidateResponse",
    "IngestService",
    "IngestResult",
    "assess_input_size",
    "InjectionService",
    "MaterializationService",
    "MaterializeResult",
    "PreviewService",
    "ValidationOrchestrator",
    "run_sync_validate",
    "check_plagiarism",
    "check_forbidden_local",
    "check_quantitative",
    "check_semantic",
    "check_forbidden_semantic",
    "MetricsEngine",
    "ParagraphRecord",
    "MetricName",
    "METRIC_NAMES",
    "classify_paragraphs",
    "ParagraphClassification",
    "SegmentationResult",
    "SegmentationLLMError",
]
