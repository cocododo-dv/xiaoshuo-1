"""风格参考的枚举与请求 / 响应契约（Pydantic）。

2026-09-23（v3 P3）：删掉从未被使用的 11 个 Row 模型、未用的枚举（FeedbackVote / BookStatus / ExtractionStatus /
FindingStatus / InputAssessmentLevel）与旧学习链路的契约（ExtractionFindingInput / ExtractionOutput /
SupplementEvidenceOutput / SynthesizedProfile / ProfileSubDimensionSummary）；学习作业的输出校验在
``learn_extract`` / ``learn_card`` 里。
"""

from __future__ import annotations

# Runtime truth: `SystemPromptFragments` is the public injection payload.
# `InjectionBundle` survives only as a historical design term in docs; the
# current HTTP contract is the `injection-preview` response below.

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 全枚举清单(PR-1 落齐,作为后续 PR 命名约束)
# ---------------------------------------------------------------------------


class ParagraphType(str, Enum):
    """8 类段落类型,LLM 分类器输出值域。来源:§4.3 stats_json / §6.5。"""

    DIALOGUE = "dialogue"
    NARRATION = "narration"
    PSYCHOLOGY = "psychology"
    DESCRIPTION_ENV = "description_env"
    DESCRIPTION_CHAR = "description_char"
    ACTION = "action"
    TRANSITION = "transition"
    FLASHBACK = "flashback"


class FindingKind(str, Enum):
    """正向观察 / 反向禁忌。来源:§4.3 findings.finding_kind / §6.5。"""

    OBSERVATION = "observation"
    FORBIDDEN_PATTERN = "forbidden_pattern"


class AnchorKind(str, Enum):
    """evidence 锚点类型。来源:§4.3 evidences.anchor_kind / §6.5。"""

    PARAGRAPH_QUOTE = "paragraph_quote"
    AUTHOR_AVOIDANCE = "author_avoidance"
    COUNTER_EXAMPLE = "counter_example"


class ExtractionPurpose(str, Enum):
    """两级重试链路追溯。来源:§4.3 extractions.purpose / §6.6。"""

    EXTRACT = "extract"
    SUPPLEMENT_EVIDENCE = "supplement_evidence"
    FULL_RETRY = "full_retry"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunPhase(str, Enum):
    INGEST = "ingest"
    EXTRACT = "extract"
    SYNTHESIZE = "synthesize"
    DONE = "done"


class ProfileStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class BindingScope(str, Enum):
    """注入绑定的目标范围。来源:§4.2 injection_bindings.scope / §8.1 ProfileApplyDialog。"""

    PROJECT = "project"
    SCENE = "scene"
    CHARACTER = "character"


class BindingStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class InjectionStrategy(str, Enum):
    """A=System Prompt / B=Few-shot / C=RAG / mixed。来源:§5.1 / §6 注入策略。"""

    A = "A"
    B = "B"
    C = "C"
    MIXED = "mixed"


class TaskType(str, Enum):
    """注入策略默认表的 key。来源:§5.1 TaskType。"""

    PROJECT_INIT = "project_init"
    SCENE_GENERATION = "scene_generation"
    FINE_TUNING = "fine_tuning"
    # deprecated——「长文续写」生产路径已下线(2026-08,产品拍板不接线)。
    # 值必须保留:存量 DB 的 StyleReferenceInjectionBinding.task_type 可能仍是
    # 'long_form_continuation',读取/校验路径(TaskType(...) 与 Pydantic 字段)
    # 收窄枚举会让存量行直接炸掉。仅从 UI 选项与任务卡片列表中移除。
    LONG_FORM_CONTINUATION = "long_form_continuation"
    KEY_CHAPTER = "key_chapter"


class ValidationVerdict(str, Enum):
    """来源:§7.5 _compute_full_verdict。"""

    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"
    PLAGIARISM = "plagiarism"


class ValidationMode(str, Enum):
    """来源:§4.3 validation_reports.mode_executed / §5.2 ValidateRequest.mode。"""

    SYNC_ONLY = "sync_only"
    ASYNC_FULL = "async_full"


class ValidationTargetKind(str, Enum):
    """来源:§5.2 ValidateRequest.target_kind。"""

    SCENE = "scene"
    CHAPTER = "chapter"
    MANUAL = "manual"


class BannedTermScope(str, Enum):
    """来源:§4.3 banned_terms.scope。"""

    GENERATION = "generation"
    EXTRACTION = "extraction"


class CloudPolicy(str, Enum):
    """沿用 services/reference_learning.py:36 SUPPORTED_CLOUD_POLICIES 的三档,
    与旧路由、旧前端、跨模块 mock 测试字面值一致。

    Hotfix(PR-1 回归):v1.1 设计文档 §附录 B 写的 (local_only / hybrid / cloud_full)
    与代码事实不符;按全局纪律 A 以代码事实为准,登记到 v1.2 修订清单第 6 条。
    """

    ALLOW_FULL_CLOUD = "allow_full_cloud"
    SEGMENTS_ONLY = "segments_only"
    LOCAL_ONLY = "local_only"


# ---------------------------------------------------------------------------
# 证据引文(学习作业的逐字核对:``evidence.align_*`` 的输入输出)
# ---------------------------------------------------------------------------


class ExtractionEvidenceInput(BaseModel):
    """一条证据引文:段落 id + 原文坐标 + 逐字引文(学习作业只产出 ``paragraph_quote``)。"""

    model_config = ConfigDict(extra="forbid")

    paragraph_id: str | None = None
    span: tuple[int, int] | None = None
    quote: str = Field(min_length=1)
    illustrates_dims: list[str] = Field(default_factory=list)
    anchor_kind: AnchorKind = AnchorKind.PARAGRAPH_QUOTE
    note: str | None = None
    is_synthetic: int = 0


# ---------------------------------------------------------------------------
# PR-4 契约:validation 简化版 / preview
# ---------------------------------------------------------------------------


# --- Validation 简化版(PR-4 范围;PR-7 加完整 quantitative / semantic)


class PlagiarismHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matched_text: str
    position: int  # generated_text 中匹配起点
    matched_length: int


class PlagiarismReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    hits: list[PlagiarismHit] = Field(default_factory=list)
    ngram_size: int = 8
    threshold_chars: int = 12


class ForbiddenHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern_statement: str
    matched_excerpt: str
    severity: str = "error"


class ValidationReport(BaseModel):
    """sync_only 简化版 ValidationReport(PR-4)。

    PR-7 加完整字段:quantitative / semantic / auto_rewrite 等。
    """

    model_config = ConfigDict(extra="forbid")

    verdict: ValidationVerdict
    mode_executed: ValidationMode = ValidationMode.SYNC_ONLY
    quantitative_json: list[dict[str, Any]] = Field(default_factory=list)
    semantic_json: list[dict[str, Any]] = Field(default_factory=list)
    plagiarism_json: dict[str, Any] = Field(default_factory=dict)
    forbidden_hits_json: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# PR-7 契约:validate 完整三路 + 双路径
# ---------------------------------------------------------------------------


class QuantitativeReportItem(BaseModel):
    """单 metric 量化对照(PR-7 §7.2)。"""

    model_config = ConfigDict(extra="forbid")

    dimension: str  # 与 SubDimension.value 对应,或 "language" / "narrative" 等粗粒度
    metric: str  # MetricName(metrics.py 26 项之一)
    target_mean: float
    target_std: float
    actual: float
    tolerance: float
    passed: bool
    deviation_ratio: float  # |actual - mean| / tolerance


class SemanticReportItem(BaseModel):
    """单 dimension 语义评分(PR-7 §7;critic LLM)。"""

    model_config = ConfigDict(extra="forbid")

    dimension: str
    score: float = Field(ge=0.0, le=10.0)
    explanation: str
    quotes_found: bool


class ValidateRequest(BaseModel):
    """`POST /profiles/{profile_id}/validate` body 形态(profile_id 在 path)。"""

    model_config = ConfigDict(extra="forbid")

    generated_text: str = Field(min_length=1, max_length=2_000_000)
    target_kind: ValidationTargetKind = ValidationTargetKind.MANUAL
    target_ref_id: str | None = Field(default=None, max_length=255)
    mode: ValidationMode = ValidationMode.ASYNC_FULL


class ValidateResponse(BaseModel):
    """`POST /profiles/{profile_id}/validate` 返回结构。

    sync_only 时 sync_result 填完整 ValidationReport;polling_url 为 None。
    async_full 时 polling_url 指向 GET /reports/{id},sync_result 为 None。
    """

    model_config = ConfigDict(extra="forbid")

    report_id: str
    mode_executed: ValidationMode
    sync_result: ValidationReport | None = None
    polling_url: str | None = None


# ---------------------------------------------------------------------------
# PR-8 契约:injection 接入(系统提示拼接片段)
# ---------------------------------------------------------------------------


# 2026-09-22 风格参考优先:样例块进 user 消息末尾时,system 前缀开头的一句指路——模型在 system 里看到
# 抽象块与红线,在 user 消息末尾看到样例本身。
FEW_SHOT_IN_USER_MESSAGE_NOTE = (
    "[风格样例](参考作者的原文样例在 user 消息的末尾,紧挨着你要写的正文;"
    "它是本场唯一的文风权威,下面的抽象特征只用来自检)"
)
# 样例块之后的收口指令:离输出最近的一段话,把「照这个手笔写」说成最后一句。
# 最后一句单独成常量:章首 / 章末场的开章 / 收章补充(style_prompt_injection.chapter_position_mandate)
# 插在它前面——「篇幅 / 只返回 JSON」永远是离输出最近的一句。
FEW_SHOT_CLOSING_MANDATE_FINAL = "篇幅按前文的长度要求；输出仍只返回前文要求的 JSON。"
FEW_SHOT_CLOSING_MANDATE = (
    "以上 [风格样例] 是本场唯一的文风权威。现在按前文的场景卡与场景结构写这一场，"
    "用这位作者的手笔来写：他的用词与口头禅、意象取向、句式长短与停顿、叙述姿态与旁白的口吻、"
    "对白的写法与换段，都照样例来，敢于用他会用的词和他会打的比方；人物、地名、事件与专名一律用本书的，"
    "不用样例里的；不整句照搬样例（连续 12 字以上与样例相同即视为照搬）。"
    + FEW_SHOT_CLOSING_MANDATE_FINAL
)


class SystemPromptFragments(BaseModel):
    """注入到 LLM system_prompt 头部的 4 块文本 + strategy 回填(PR-8 §5.1)。

    InjectionService.fragments_for() 返回此结构;scene_generation 调
    `to_system_prompt_prefix()` 拿到最终拼接字符串后 prepend 到
    messages[0]["content"]。风格 block 默认 empty,允许任一为空。

    ``anti_plagiarism_block`` 是 §A.5 抄袭事前预防红线段(设计 §11 风险 11):
    只要任一风格 block 非空(即确实在注入参考风格),红线段**必须**一并注入,
    且**永不参与预算截断**。三个风格 block 全空时整体 no-op,红线段也不输出。
    """

    model_config = ConfigDict(extra="forbid")

    positive_block: str = ""
    forbidden_block: str = ""
    metric_anchor_block: str = ""
    # 2026-09 风格模仿 v2(W4):确定性声音签名渲染的 `[声音特征]` 块
    # (来源 profile_json.voice_signature.habits;旧画像缺该键时恒为空串)。
    voice_block: str = ""
    few_shot_block: str = ""
    # 立项 C — Strategy C(RAG)按当前上下文检索的参考风格片段块;与 few_shot_block
    # 同性质(引用原文),非空时调用方保证红线段必随注。
    rag_block: str = ""
    anti_plagiarism_block: str = ""
    strategy: InjectionStrategy = InjectionStrategy.A

    def to_system_prompt_prefix(self, *, include_few_shot: bool = True) -> str:
        # 顺序(2026-09-09 样例优先):few_shot → rag → voice → positive → forbidden →
        # metric → anti_plagiarism。原文样例是主信号,排最前;抽象块作校核;量化分布最末;
        # 红线段永远最后、永不截断。(v2 §1.2 的旧顺序把样例排在抽象块之后。)
        # 2026-09-22 风格参考优先:起草通道把样例块放到 user 消息末尾(:meth:`to_user_prompt_tail`),
        # 此时 ``include_few_shot=False``——system 前缀只剩抽象块与红线,并留一句指路。
        blocks = [
            block
            for block in (
                self.few_shot_block if include_few_shot else "",
                self.rag_block,
                self.voice_block,
                self.positive_block,
                self.forbidden_block,
                self.metric_anchor_block,
            )
            if block.strip()
        ]
        if not include_few_shot and self.few_shot_block.strip():
            blocks.insert(0, FEW_SHOT_IN_USER_MESSAGE_NOTE)
        if not blocks:
            return ""
        if self.anti_plagiarism_block.strip():
            blocks.append(self.anti_plagiarism_block)
        return "[STYLE_REFERENCE]\n" + "\n\n".join(blocks) + "\n[/STYLE_REFERENCE]\n\n"

    def to_user_prompt_tail(self) -> str:
        """2026-09-22 风格参考优先:样例块作为 user 消息的**末尾**——离输出最近的位置。

        样例之后紧跟一段收口指令(``FEW_SHOT_CLOSING_MANDATE``):以样例手笔写前文定下的这一场、
        人物地名事件用本书的、不整句照搬、篇幅与 JSON 仍按前文。样例为空时返回空串。
        """
        if not self.few_shot_block.strip():
            return ""
        return "\n\n" + self.few_shot_block.rstrip() + "\n\n" + FEW_SHOT_CLOSING_MANDATE + "\n"


# ---------------------------------------------------------------------------
# PR-9 契约:injection-preview 端点(dryrun + 已落盘 binding)
# ---------------------------------------------------------------------------


class InjectionPreviewRequest(BaseModel):
    """`POST /profiles/{id}/injection-preview` body — dryrun 模式入参。

    用户在 ApplyDialog 内调整 strategy / intensity / sub_dimensions 时,
    前端 debounce 拉这个端点,**不写盘** binding。
    """

    model_config = ConfigDict(extra="forbid")

    strategy: InjectionStrategy | None = None
    task_type: TaskType = TaskType.SCENE_GENERATION
    intensity: int = Field(default=100, ge=0, le=100)
    sub_dimensions: list[str] = Field(default_factory=list, max_length=128)
    include_positive: bool = True
    include_forbidden: bool = True
    include_metric: bool | None = None
    # 2026-09-14 保真修补(WP4.3):按场景预览——同一轮换种子与场景位置提示,作者看到的就是
    # 这一场实际拿到的窗口;不传则为无种子的通用预览。
    scene_id: str | None = Field(default=None, max_length=128)
    # 2026-09-23 风格参考 v3:绑定的四个旋钮(不传时由旧 strategy / intensity 映射,见 binding_config);
    # project_id 给了就带上这部作品的近期常见偏差。旧 sub_dimensions / include_* 不再改变渲染。
    reference_mode: Literal["full", "samples_only", "card_only"] | None = None
    sample_windows: int | None = Field(default=None, ge=0, le=16)
    dimension_states: dict[str, Literal["emphasize", "normal", "exclude"]] | None = None
    draft_mode: Literal["style_first", "neutral_first"] | None = None
    project_id: str | None = Field(default=None, max_length=128)


class InjectionPreviewStats(BaseModel):
    """preview 端点的真实读数(v2 §2.W4.8;前端强度滑块读数只消费这里,不再算虚构公式)。

    行数 = 各块中以 `- ` 起头的条目行;`few_shot_windows` 是注入的连续段落窗口数,
    `few_shot_chars` 是窗口原文总字数(封装边界前);`intensity_effective_total_chars`
    是本次 intensity 对应的抽象四块总额;`few_shot_k` 是 k(i)。
    """

    model_config = ConfigDict(extra="forbid")

    positive_lines: int = 0
    forbidden_lines: int = 0
    metric_lines: int = 0
    voice_lines: int = 0
    few_shot_windows: int = 0
    few_shot_chars: int = 0
    rag_snippets: int = 0
    total_prefix_chars: int = 0
    intensity_effective_total_chars: int = 0
    few_shot_k: int = 0


class InjectionPreviewResponse(BaseModel):
    """preview 端点统一返回结构。"""

    model_config = ConfigDict(extra="forbid")

    fragments: SystemPromptFragments
    prefix: str
    stats: InjectionPreviewStats | None = None
    # 2026-09-14 保真修补(WP4.1):本次渲染实际选中的样例窗口(起止段 / 章 / 位置 / 字数 / 选窗配额),
    # 按原书顺序;不含原文(原文在 fragments.few_shot_block)。
    window_refs: list[dict[str, Any]] = Field(default_factory=list)
    # 2026-09-23 风格参考 v3:与起草提示同序——prefix 是 system 前缀,样例与收口在 user 尾块
    user_tail: str = ""
    reference_mode: str | None = None
    sample_windows: int | None = None
