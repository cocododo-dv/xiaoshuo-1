"""场景生成的契约（叶子）：结果类型、生成后处理失败、稿行 id 格式，以及各流程共用的来源标签 / 谱系标记。

结果对象由编排器消费（检查点、产品回调、终选门）；行 id 格式与这些标记写进 scene_drafts / attempt_trackers，
续跑与工作台据此读回——改名或改格式都会让已有的检查点对不上。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from sqlalchemy.orm import Session

from novel_system.services.hash_engine import sha256_json_normalized
from novel_system.services.style_reference.runtime_contract import DRAFT_MODE_NEUTRAL_FIRST


class SceneGenerationPostprocessError(ValueError):
    """Stable typed failure emitted after a provider call settled successfully."""

    def __init__(self, *, llm_call_id: str | None, message: str) -> None:
        super().__init__(message)
        self.llm_call_id = llm_call_id
        self.code = "SCENE_GENERATION_RESPONSE_INVALID"
        self.error_code = self.code


@dataclass(slots=True)
class NeutralGenerationResult:
    row_id: str
    content: str
    llm_call_id: str
    bundle_id: str
    bundle_hash: str
    execution_step_key: str | None = None
    artifact_execution_id: str | None = None
    # 2026-09-12 风格直起:本步位实际的起草方式与首稿 notices / 门裁决(neutral_first 下为空)。
    draft_mode: str = DRAFT_MODE_NEUTRAL_FIRST
    notices: list[dict[str, Any]] = field(default_factory=list)
    styled_draft_gate: dict[str, Any] | None = None


@dataclass(slots=True)
class StyleGenerationResult:
    row_id: str
    content: str
    llm_call_id: str
    bundle_id: str
    bundle_hash: str
    execution_step_key: str | None = None
    artifact_execution_id: str | None = None
    ranking_audit: RankingAudit | None = None
    # 2026-09 风格模仿 v2（W5，规格 §2.W5.6）：风格链路的非静默提示。每项
    # ``{"code", "message", "severity", ...}``；code 见 STYLE_NOTICE_*。同一份也写进
    # AttemptTracker.details_json["notices"]，供场景运行 / 工作台响应回读。
    notices: list[dict[str, Any]] = field(default_factory=list)
    # 生成侧 styled-draft gate 的诊断字典（qc_engine.run_styled_draft_style_gate 的返回；
    # 无绑定时 None）。orchestrator 据此对 near_final_rewrite 的抄袭裁决采取行动。
    styled_draft_gate: dict[str, Any] | None = None
    # 风格参考 v3（P5b）：「首稿即风格稿」（读数在作者范围内，风格步不调模型）的产品沿用首稿的调用谱系——
    # llm_call_id / execution_step_key 是首稿那次调用的；检查点校验据此认它（见 orchestrator）。
    lineage: str | None = None
    # 风格步的决定（读数、要改的维、采用 / 保留首稿的原因），同一份也写进 AttemptTracker.details_json.style_step。
    style_step: dict[str, Any] | None = None


class ProductMetadata(TypedDict, total=False):
    """产品回调的元数据（编排器写进风格稿工作项、续跑时逐项核对；键名即检查点契约）。"""

    slot_order: int  # 槽位序：子游标 = 槽位序 × 2 + 是否成稿
    source_neutral_draft_row_id: str  # 这一槽位的来源稿（首稿 / 已批准的中性稿）
    gate_decision: dict[str, Any] | None  # 成稿过 styled-draft gate 的裁决（底稿为 None）
    source_base_row_id: str | None  # 成稿所依的底稿行（底稿为 None）
    de_template_outcome: dict[str, Any]  # 去模板化的结局（只有成稿带）


# 产品回调（编排器据此写检查点）：(槽位键, "base" | "final", 结果, 元数据)。
ProductCallback = Callable[[str, str, "StyleGenerationResult", ProductMetadata], None]
# 步位对账（编排器的 _reconcile_execution_step）：调模型之前按步位键对一次账本
StepReconciler = Callable[[str], None]


class RankingAudit(TypedDict, total=False):
    """``StyleGenerationResult.ranking_audit``：候选排序的审计（编排器据此装配 ``style_candidates``，并存进检查点）。"""

    row_id: str
    rank: int
    selected: bool
    selection_reason: str
    slot_index: int
    quality_score: float
    style_score: float | None
    style_confidence: float | None
    fidelity_distance: float | None
    fidelity_percentile: float | None
    within_range: bool | None
    duplicate_of_row_id: str | None
    plagiarism_checked: bool
    plagiarism_passed: bool | None
    rerank: dict[str, Any]


class GenerationHost(Protocol):
    """各流程模块（first_draft / style_first / neutral_style / best_of_n）从门面借用的东西——
    :class:`~novel_system.services.scene_generation.SceneGenerationService` 就是它。

    流程是模块函数、第一个参数收门面：会话、模型节点执行器、模板构建器、参考注入与风格门都按实例查找，
    测试替换门面上的这几个方法照旧生效。
    """

    session: Session
    _llm_runner: Any  # LLMNodeRunner（叶子模块不 import 它）

    def _prompt_builder(self) -> Any: ...

    def _inject_style_reference(
        self, prompt: dict[str, Any] | None, scene: Any, **kwargs: Any
    ) -> dict[str, Any] | None: ...

    def _styled_draft_style_gate(
        self,
        scene: Any,
        style_content: str,
        *,
        bundle: dict[str, Any] | None = None,
        stage: str = "style_draft",
    ) -> dict[str, Any] | None: ...

    def generate_style_draft(
        self, scene_id: str, bundle: dict[str, Any], **kwargs: Any
    ) -> StyleGenerationResult: ...


# 来源稿标签:作者手笔直起的定向修改看到首稿,neutral_first 的风格稿看到已批准的中性稿。
FIRST_DRAFT_SOURCE_LABEL = "First Draft (already in the reference author's hand)"
NEUTRAL_DRAFT_SOURCE_LABEL = "Approved Neutral Draft"
# AttemptTracker.details_json.content_source 标记:首稿直起。
STYLE_FIRST_DRAFT_CONTENT_SOURCE = "style_first_draft"
# 风格参考 v3（P5b）：风格步「首稿即风格稿」产品的谱系标记（StyleGenerationResult.lineage / 检查点描述符）。
LINEAGE_FIRST_DRAFT_ACCEPTED = "first_draft_accepted"
STYLE_STEP_VERSION = "style_step_v1"
# 定向修改稿的抄袭门没有查成（书已删 / 策略降级 / 检查出错）：与「查出新带进的重合」（copy_gate_blocked）分开说。
REASON_COPY_UNCHECKED = "copy_gate_unavailable"


def versioned_scene_artifact_id(
    prefix: str, scene_id: str, bundle: dict[str, Any]
) -> str:
    bundle_id = str(bundle.get("bundle_id") or "")
    bundle_prefix = f"bundle_{scene_id}_"
    if bundle_id.startswith(bundle_prefix):
        return f"{prefix}_{scene_id}_{bundle_id[len(bundle_prefix):]}"
    if bundle_id == f"bundle_{scene_id}":
        return f"{prefix}_{scene_id}"
    bundle_hash = str(bundle.get("bundle_snapshot_hash") or "")
    suffix = (
        bundle_hash[:12]
        if bundle_hash
        else sha256_json_normalized(bundle)[:12]
    )
    return f"{prefix}_{scene_id}_{suffix}"
