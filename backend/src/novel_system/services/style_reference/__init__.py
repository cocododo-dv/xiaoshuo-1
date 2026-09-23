"""Style Reference (style_reference) service package.

现行说明见 ``docs/style-reference.md``（v3 重构的设计与台账见 ``docs/style-reference-v3-2026-09-23.md``）。学习链路是一个持久作业
（``learn_job``：窗口 → 选窗 → 分层抽取 → 文风卡 → 受保护专名 → 窗口标签 → 画像），处理器在模块导入时注册
（``register_job_handler("learn", ...)``），这里不导入它，免得包导入就拉起整条学习链路。

**包的 ``__init__`` 只导入包内不依赖包外服务的模块。** 包内任何模块（例如 ``binding_config``）第一次被导入时
都会先跑这个文件；这里若导入了会回头依赖 ``services.style_policy`` 的模块（``binding_apply``），``style_policy``
→ ``binding_config`` → 本文件 → ``binding_apply`` → ``style_policy``（还没初始化完）就在新解释器里 ImportError（M1，
``tests/test_style_reference_import_order.py`` 守着）。直接绑定请从 ``style_reference.binding_apply`` 导入。
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
from novel_system.services.style_reference.validation import check_plagiarism
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
    PlagiarismHit,
    PlagiarismReport,
    InjectionPreviewRequest,
    InjectionPreviewResponse,
    SystemPromptFragments,
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
    "BannedTermScope",
    "CloudPolicy",
    "StyleReferenceError",
    "DuplicateBookError",
    "EmptyBookError",
    "LLMRequiredError",
    "ExtractionEvidenceInput",
    "PlagiarismHit",
    "PlagiarismReport",
    "InjectionPreviewRequest",
    "InjectionPreviewResponse",
    "SystemPromptFragments",
    "IngestService",
    "IngestResult",
    "assess_input_size",
    "InjectionService",
    "check_plagiarism",
    "MetricsEngine",
    "ParagraphRecord",
    "MetricName",
    "METRIC_NAMES",
    "classify_paragraphs",
    "ParagraphClassification",
    "SegmentationResult",
    "SegmentationLLMError",
]
