"""风格参考的枚举与请求 / 响应契约（Pydantic）。

2026-09-23（v3 P3）：删掉从未被使用的 11 个 Row 模型、未用的枚举（FeedbackVote / BookStatus / ExtractionStatus /
FindingStatus / InputAssessmentLevel）与旧学习链路的契约（ExtractionFindingInput / ExtractionOutput /
SupplementEvidenceOutput / SynthesizedProfile / ProfileSubDimensionSummary）；学习作业的输出校验在
``learn_extract`` / ``learn_card`` 里。2026-09-24（清理 S1 / S4）：``RunStatus`` / ``RunPhase`` 无读者删掉，
``InjectionStrategy`` 只剩 ``MIXED``、``TaskType`` 只剩 ``SCENE_GENERATION``（实际写 / 读的唯一值），预览契约
去掉 v2 字段（``strategy`` / ``intensity`` / ``sub_dimensions`` / ``include_*`` / ``metric_*`` / ``forbidden_*``）。
2026-09-30（B10-22）：本场预览不再回 ``fragments`` / ``prefix`` / ``user_tail`` / ``stats`` / ``window_refs``（界面一个
都不读，样例原文因此传了两遍），塑形用的 ``SystemPromptFragments`` / ``InjectionPreviewStats`` /
``InjectionPreviewResponse`` 随之删掉；预览载荷的形状见 ``scene_preview``。
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

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


class ProfileStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class BindingStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class InjectionStrategy(str, Enum):
    """绑定行的 ``strategy`` 列:v3 起恒为 ``mixed``(旧 A / B / C 由迁移 0092 统一改写;怎么送参考只看绑定配置的
    ``reference_mode``)。列本身保留,删列要重建表,另议。"""

    MIXED = "mixed"


class TaskType(str, Enum):
    """绑定行的 ``task_type``:v3 只写 / 只读 ``scene_generation``(旧 ``long_form_continuation`` 等值从没被枚举校验过,
    库里的存量行按字符串比对,不经这个枚举)。"""

    SCENE_GENERATION = "scene_generation"


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


# ---------------------------------------------------------------------------
# 抄袭检测(validation/plagiarism.py 的返回;唯一抄袭门的口径)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# PR-9 契约:injection-preview 端点(dryrun + 已落盘 binding)
# ---------------------------------------------------------------------------


class InjectionPreviewRequest(BaseModel):
    """「本场预览」``POST /profiles/{id}/injection-preview`` 的请求体——只读,**不写**绑定、不冻结选窗。

    旋钮就是 v3 的四个绑定配置键(不传的取默认,见 ``binding_config``);``scene_id`` 给了就按那一场的设计挑窗
    (与起草同一套选窗),``project_id`` 给了就带上这部作品的近期常见偏差。旧的 ``strategy`` / ``intensity`` /
    ``sub_dimensions`` / ``include_*`` 不再收(2026-09-24;React 客户端只发这四键)。
    """

    model_config = ConfigDict(extra="forbid")

    # 2026-09-14 保真修补(WP4.3):按场景预览——同一轮换种子与场景位置提示,作者看到的就是
    # 这一场实际拿到的窗口;不传则为无种子的通用预览。
    scene_id: str | None = Field(default=None, max_length=128)
    reference_mode: Literal["full", "samples_only", "card_only"] | None = None
    sample_windows: int | None = Field(default=None, ge=0, le=16)
    dimension_states: dict[str, Literal["emphasize", "normal", "exclude"]] | None = None
    draft_mode: Literal["style_first", "neutral_first"] | None = None
    project_id: str | None = Field(default=None, max_length=128)


