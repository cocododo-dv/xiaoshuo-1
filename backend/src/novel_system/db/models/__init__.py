"""ORM 模型的唯一导入面：``from novel_system.db.models import <模型类> | utcnow | Base``。

全部表按领域分在子模块里（B12-09）；子模块只引 ``db.base`` 与 ``_common``，从不回头引这个包，所以没有导入环。
外键查找索引（``_indexes``）最后建——它要全部表都已登记。新加的模型类要在这里导出（守卫
``tests/test_models_package.py``：``__all__`` 覆盖全部映射类）。
"""

from __future__ import annotations

from novel_system.db.base import Base
from novel_system.db.models._common import utcnow
from novel_system.db.models.project import (
    OutlinePlan,
    ProjectWritingStats,
    StoryProject,
)
from novel_system.db.models.snowflake import (
    SnowflakeAssistantTurn,
    SnowflakeChapterPlan,
    SnowflakeCharacterPlan,
    SnowflakeDirectionBrief,
    SnowflakeScenePlan,
    SnowflakeSceneTriageItem,
    SnowflakeStepRun,
)
from novel_system.db.models.library import (
    LibraryEntity,
    LibraryRelation,
    StoryCharacter,
    TimelineEvent,
)
from novel_system.db.models.catalog import (
    ChapterGoal,
    SceneCard,
)
from novel_system.db.models.run import (
    AttemptTracker,
    BackgroundRecoveryLease,
    ChapterRunJob,
    ChapterState,
    GenerationPlanningArtifact,
    QcReport,
    SceneBlueprint,
    SceneBundle,
    SceneDraft,
    SceneExecutionContract,
    SceneRunState,
)
from novel_system.db.models.llm_ledger import (
    LlmCall,
    LlmCallAttempt,
)
from novel_system.db.models.review import (
    HumanReviewEvent,
    PassagePatchCandidate,
    RevisionCandidate,
    WriterEvaluation,
)
from novel_system.db.models.author_drafts import (
    AuthorDraft,
    AuthorDraftEvent,
    AuthorDraftProposal,
    AuthorDraftRevision,
)
from novel_system.db.models.canon import (
    CanonCommit,
    ChapterDigest,
    ChapterMemory,
    ChapterRollingNote,
    ContinuitySnapshot,
    EditorialGuidance,
    EditorialReview,
    FactCandidate,
    FinalScene,
    NarrativeEvent,
    SceneMemory,
    StoryThreadMark,
)
from novel_system.db.models.infra import (
    IdempotencyKey,
    OperationLog,
    SystemConfigSnapshot,
    SystemSecret,
)
from novel_system.db.models.style_reference import (
    StyleFidelityReading,
    StyleReferenceBannedTerm,
    StyleReferenceBook,
    StyleReferenceEvidence,
    StyleReferenceExtraction,
    StyleReferenceFinding,
    StyleReferenceInjectionBinding,
    StyleReferenceJob,
    StyleReferenceMetricEvent,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    StyleReferenceQuote,
    StyleReferenceRun,
    StyleReferenceSceneWindows,
    StyleReferenceWindow,
)
from novel_system.db.models import _indexes  # noqa: F401,E402 — 外键查找索引（要在全部表登记之后）

__all__ = [
    "AttemptTracker",
    "AuthorDraft",
    "AuthorDraftEvent",
    "AuthorDraftProposal",
    "AuthorDraftRevision",
    "BackgroundRecoveryLease",
    "Base",
    "CanonCommit",
    "ChapterDigest",
    "ChapterGoal",
    "ChapterMemory",
    "ChapterRollingNote",
    "ChapterRunJob",
    "ChapterState",
    "ContinuitySnapshot",
    "EditorialGuidance",
    "EditorialReview",
    "FactCandidate",
    "FinalScene",
    "GenerationPlanningArtifact",
    "HumanReviewEvent",
    "IdempotencyKey",
    "LibraryEntity",
    "LibraryRelation",
    "LlmCall",
    "LlmCallAttempt",
    "NarrativeEvent",
    "OperationLog",
    "OutlinePlan",
    "PassagePatchCandidate",
    "ProjectWritingStats",
    "QcReport",
    "RevisionCandidate",
    "SceneBlueprint",
    "SceneBundle",
    "SceneCard",
    "SceneDraft",
    "SceneExecutionContract",
    "SceneMemory",
    "SceneRunState",
    "SnowflakeAssistantTurn",
    "SnowflakeChapterPlan",
    "SnowflakeCharacterPlan",
    "SnowflakeDirectionBrief",
    "SnowflakeScenePlan",
    "SnowflakeSceneTriageItem",
    "SnowflakeStepRun",
    "StoryCharacter",
    "StoryProject",
    "StoryThreadMark",
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
    "SystemConfigSnapshot",
    "SystemSecret",
    "TimelineEvent",
    "WriterEvaluation",
    "utcnow",
]
