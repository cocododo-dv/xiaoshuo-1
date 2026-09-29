"""场景生成：首稿 → 风格步 / 风格稿 → 软补丁 → 准终稿改写，以及 Best-of-N 候选。

``SceneGenerationService`` 是编排器用的门面；流程是子模块里的模块函数（第一个参数收门面，见
``contracts.GenerationHost``）：

- :mod:`.first_draft` 首稿（中性步位，两种起草方式）；
- :mod:`.style_first` 作者手笔直起时的风格步（按读数决定，越界才定向修改）；
- :mod:`.neutral_style` 风格通道的一遍（neutral_first 的风格稿、软补丁、准终稿改写）；
- :mod:`.best_of_n` 候选（默认关）。

共用的零件：:mod:`.contracts`（结果类型、标签、行 id）、:mod:`.notices`（风格提示）、:mod:`.length_policy`
（长度带）、:mod:`.text_gates`（确定性文本门与约束快照）、:mod:`.briefs`（修复 / 改写简报）、
:mod:`.segment_patch`（分段补丁）、:mod:`.ledger`（稿行 / 尝试 / 失败落库）、:mod:`.fidelity_probe`（读数探针）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard, SceneRunState
from novel_system.services import qc_engine
from novel_system.services.llm_task_runner import LLMNodeRunner
from novel_system.services.prompt_builder import PromptBuilder
from novel_system.services.scene_generation import (
    best_of_n,
    fidelity_probe,
    first_draft,
    neutral_style,
    style_first,
    text_gates,
)
from novel_system.services.scene_generation.briefs import (
    _NEUTRAL_STYLE_INSTRUCTION,
    _author_note_instruction_for_bundle,
    _neutral_repair_brief,
    author_note_instruction,
    build_style_user_prompt,
)
from novel_system.services.scene_generation.contracts import (
    FIRST_DRAFT_SOURCE_LABEL,
    LINEAGE_FIRST_DRAFT_ACCEPTED,
    NEUTRAL_DRAFT_SOURCE_LABEL,
    REASON_COPY_UNCHECKED,
    STYLE_FIRST_DRAFT_CONTENT_SOURCE,
    STYLE_STEP_VERSION,
    NeutralGenerationResult,
    ProductCallback,
    SceneGenerationPostprocessError,
    StyleGenerationResult,
    versioned_scene_artifact_id,
)
from novel_system.services.scene_generation.length_policy import (
    LengthPolicy,
    _neutral_length_instruction,
    _parse_numeric_length_band,
    _reference_scale_sentence,
    _reference_scene_scale_from_bundle,
    _style_first_length_instruction,
    _style_first_length_slack,
    _style_length_instruction,
    _style_repair_length_instruction,
)
from novel_system.services.scene_generation.segment_patch import (
    _apply_style_length_patch,
    _apply_style_salvage_patch,
)
from novel_system.services.scene_generation.text_gates import (
    ConstraintSnapshot,
    _anti_template_quality_gate,
    _assess_de_template_rewrite,
    _assess_neutral_draft,
    _assess_style_base_rewrite,
    _extract_scene_text,
    _scene_text_integrity_markers,
    _visible_char_count,
    assess_rewrite_regressions,
)
from novel_system.services.scene_generation.notices import (
    STYLE_NOTICE_ATTEMPT_STEPS,
    STYLE_NOTICE_BANNED_TERM_HIT,
    STYLE_NOTICE_CODES,
    STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL,
    STYLE_NOTICE_FIRST_DRAFT,
    STYLE_NOTICE_FIRST_DRAFT_ACCEPTED,
    STYLE_NOTICE_GATE_UNAVAILABLE,
    STYLE_NOTICE_INJECTION_DEGRADED,
    STYLE_NOTICE_INJECTION_MISS,
    STYLE_NOTICE_PATCH_REVERTED,
    STYLE_NOTICE_PLAGIARISM_HIT,
    STYLE_NOTICE_REFERENCE_BOOK_CHANGED,
    STYLE_NOTICE_REFERENCE_BOOK_MISSING,
    STYLE_NOTICE_REFERENCE_NO_WINDOWS,
    STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED,
    STYLE_NOTICE_REVISION_REJECTED,
    STYLE_PATCH_KEEP_STEP,
    _styled_draft_gate_notices,
    latest_style_notices,
    render_audit_notices,
    style_injection_notices,
    style_notice,
)
from novel_system.services.style_policy import style_policy_for_bundle
from novel_system.services.style_prompt_injection import ROLE_REVISE, inject_style_reference_prefix
from novel_system.services.style_reference import style_step

# 门面：编排器、路由与测试从这里取名字；子模块才是它们的家——流程在 first_draft / style_first / neutral_style /
# best_of_n（模块函数，第一个参数收本服务，见 contracts.GenerationHost）。下划线开头的是测试直接调用的内部助手——要替换
# （monkeypatch）某个助手，替换它所在的子模块，不是这里。
__all__ = [
    "FIRST_DRAFT_SOURCE_LABEL",
    "LengthPolicy",
    "LINEAGE_FIRST_DRAFT_ACCEPTED",
    "NEUTRAL_DRAFT_SOURCE_LABEL",
    "REASON_COPY_UNCHECKED",
    "STYLE_FIRST_DRAFT_CONTENT_SOURCE",
    "STYLE_NOTICE_ATTEMPT_STEPS",
    "STYLE_NOTICE_BANNED_TERM_HIT",
    "STYLE_NOTICE_CODES",
    "STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL",
    "STYLE_NOTICE_FIRST_DRAFT",
    "STYLE_NOTICE_FIRST_DRAFT_ACCEPTED",
    "STYLE_NOTICE_GATE_UNAVAILABLE",
    "STYLE_NOTICE_INJECTION_DEGRADED",
    "STYLE_NOTICE_INJECTION_MISS",
    "STYLE_NOTICE_PATCH_REVERTED",
    "STYLE_NOTICE_PLAGIARISM_HIT",
    "STYLE_NOTICE_REFERENCE_BOOK_CHANGED",
    "STYLE_NOTICE_REFERENCE_BOOK_MISSING",
    "STYLE_NOTICE_REFERENCE_NO_WINDOWS",
    "STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED",
    "STYLE_NOTICE_REVISION_REJECTED",
    "STYLE_PATCH_KEEP_STEP",
    "STYLE_STEP_VERSION",
    "NeutralGenerationResult",
    "ProductCallback",
    "SceneGenerationPostprocessError",
    "ConstraintSnapshot",
    "SceneGenerationService",
    "StyleGenerationResult",
    "_anti_template_quality_gate",
    "_apply_style_length_patch",
    "_apply_style_salvage_patch",
    "_assess_de_template_rewrite",
    "_assess_neutral_draft",
    "_assess_style_base_rewrite",
    "_extract_scene_text",
    "_neutral_length_instruction",
    "_neutral_repair_brief",
    "_parse_numeric_length_band",
    "_reference_scale_sentence",
    "_reference_scene_scale_from_bundle",
    "_style_first_length_instruction",
    "_style_first_length_slack",
    "_style_length_instruction",
    "_style_repair_length_instruction",
    "_visible_char_count",
    "assess_rewrite_regressions",
    "best_of_n",
    "author_note_instruction",
    "fidelity_probe",
    "first_draft",
    "_scene_text_integrity_markers",
    "_styled_draft_gate_notices",
    "inject_style_reference_prefix",
    "latest_style_notices",
    "neutral_style",
    "render_audit_notices",
    "style_injection_notices",
    "style_first",
    "style_notice",
    "style_step",
    "text_gates",
    "versioned_scene_artifact_id",
]

_LOGGER = logging.getLogger(__name__)


class SceneGenerationService:
    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        llm_runner: LLMNodeRunner | None = None,
    ) -> None:
        self.session = session
        self._llm_runner = llm_runner or LLMNodeRunner(session, llm_client=llm_client)
        self._prompt_builder_instance: PromptBuilder | None = None

    def generate_neutral_draft(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        author_note: str | None = None,
    ) -> NeutralGenerationResult:
        """中性步位(``neutral_ready`` 检查点)的起草。

        2026-09-12 风格直起(Step 2):bundle 冻结契约的 ``draft_mode`` 决定这一步位写什么——
        ``neutral_first``:中性稿(现状,阅读对照组);``style_first``:直接以参考作者手笔从
        bundle 写首稿(``style_first_draft`` 模板 + ``[STYLE_REFERENCE]`` 前缀,走 style_draft
        节点路由)。步位、``stage="neutral_draft"`` 行、attempt step、指针、账本字段全部不变。
        """
        return first_draft.generate_first_draft(self, scene_id, bundle, author_note=author_note)

    def generate_style_draft(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        neutral_draft_row_id: str,
        neutral_content: str,
        author_note: str | None = None,
        resume_base: StyleGenerationResult | None = None,
        product_callback: (
            Callable[[str, str, StyleGenerationResult, dict[str, Any]], None] | None
        ) = None,
        step_reconciler: Callable[[str], None] | None = None,
    ) -> StyleGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        policy = style_policy_for_bundle(bundle)
        if policy.style_first:
            # 风格参考 v3（P5b）：作者手笔直起时风格步按读数决定（在范围内不调模型；越界定向修改；不更像保留首稿）。
            # neutral_first（阅读对照组）与未绑定仍走下面原来的「重组 / 复读」，逐字不变。
            return style_first.style_first_step(
                self,
                scene=scene,
                state=state,
                bundle=bundle,
                policy=policy,
                first_row_id=neutral_draft_row_id,
                first_content=neutral_content,
                author_note=author_note,
                row_id=versioned_scene_artifact_id("draft_style", scene_id, bundle),
                slot_key="initial:0",
                slot_order=0,
                execution_step_key="style_draft:0",
                resume_base=resume_base,
                product_callback=product_callback,
                step_reconciler=None,
                attempt_details_extra={"source_neutral_draft_row_id": neutral_draft_row_id},
            )
        return neutral_style.run_style_generation(
            self,
            scene=scene,
            state=state,
            bundle=bundle,
            row_id=versioned_scene_artifact_id("draft_style", scene_id, bundle),
            stage="style_draft",
            llm_step="style_draft",
            neutral_content=neutral_content,
            source_label=NEUTRAL_DRAFT_SOURCE_LABEL,
            source_row_id=neutral_draft_row_id,
            extra_instruction=(
                _NEUTRAL_STYLE_INSTRUCTION
                + _author_note_instruction_for_bundle(bundle, author_note)
            ),
            source_draft_row_id=neutral_draft_row_id,
            source_draft_content=neutral_content,
            client_kind="style",
            execution_step_key="style_draft:0",
            attempt_details_extra={"source_neutral_draft_row_id": neutral_draft_row_id},
            product_slot_key="initial:0",
            product_slot_order=0,
            resume_base=resume_base,
            product_callback=product_callback,
            step_reconciler=step_reconciler,
        )

    def generate_style_draft_candidates(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        neutral_draft_row_id: str,
        neutral_content: str,
        author_note: str | None = None,
        n_candidates: int = 3,
        # 编排器还在传补候选的上限（P01c 删掉这个传参后一起删）；多稿不再补候选，用不上它
        max_candidates: int | None = None,
        step_reconciler: Callable[[str], None] | None = None,
        resume_bases: dict[str, StyleGenerationResult] | None = None,
        resume_products: dict[str, StyleGenerationResult] | None = None,
        product_callback: ProductCallback | None = None,
    ) -> list[StyleGenerationResult]:
        """Best-of-N 风格候选（``NOVEL_SYSTEM_SCENE_BEST_OF_N_ENABLED``）：只有作者手笔直起才出多稿，见 :mod:`.best_of_n`。"""
        return best_of_n.generate_candidates(
            self,
            scene_id,
            bundle,
            neutral_draft_row_id=neutral_draft_row_id,
            neutral_content=neutral_content,
            author_note=author_note,
            n_candidates=n_candidates,
            step_reconciler=step_reconciler,
            resume_bases=resume_bases,
            resume_products=resume_products,
            product_callback=product_callback,
        )

    def generate_style_patch(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        source_style_draft_row_id: str,
        source_style_content: str,
        rewrite_brief: list[str],
        source_qc_report_id: str,
        execution_step_key: str = "soft_patch:0",
    ) -> StyleGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        # 2026-09-14 风格保真修补:有绑定时软补丁低温、未点名的句子逐字保留——补丁的 schema 仍要求
        # 返回整篇 scene_text,温度 0.8 会把整场措辞重掷一遍。neutral_first 不变。
        style_first = style_policy_for_bundle(bundle).defers_house_taste()
        result = neutral_style.run_style_generation(
            self,
            scene=scene,
            state=state,
            bundle=bundle,
            row_id=versioned_scene_artifact_id("draft_style_patch", scene_id, bundle),
            stage="style_patch",
            llm_step="soft_patch",
            neutral_content=source_style_content,
            source_label="Current Style Draft",
            source_row_id=source_style_draft_row_id,
            extra_instruction=(
                "Apply only the controlled patch brief; do not rewrite the full scene."
                + (
                    " Keep every sentence the brief does not name verbatim; for the sentences you do change, "
                    "the [STYLE_REFERENCE] block is the style authority."
                    if style_first
                    else ""
                )
            ),
            temperature_override=0.3 if style_first else None,
            patch_brief=rewrite_brief,
            source_draft_row_id=source_style_draft_row_id,
            source_draft_content=source_style_content,
            client_kind="patch",
            execution_step_key=execution_step_key,
            attempt_details_extra={
                "source_qc_report_id": source_qc_report_id,
                "source_style_draft_row_id": source_style_draft_row_id,
                "rewrite_brief": rewrite_brief,
            },
            # 风格参考 v3（P5b）：作者手笔直起时软补丁按改稿口径渲染（「只改不像的地方，已经像的原样留下」）
            render_role=ROLE_REVISE if style_first else None,
        )
        state.soft_patch_count += 1
        return result

    def generate_near_final_rewrite(
        self,
        scene_id: str,
        bundle: dict[str, Any],
        *,
        source_draft_row_id: str,
        source_content: str,
        revision_brief: list[str],
        source_evaluation_id: str,
        execution_step_key: str = "near_final_rewrite:0",
    ) -> StyleGenerationResult:
        scene = self.session.get(SceneCard, scene_id)
        state = self.session.get(SceneRunState, scene_id)
        return neutral_style.run_style_generation(
            self,
            scene=scene,
            state=state,
            bundle=bundle,
            row_id=versioned_scene_artifact_id(
                "draft_near_final_rewrite", scene_id, bundle
            ),
            stage="near_final_rewrite",
            llm_step="scene_literary_rewrite",
            neutral_content=source_content,
            source_label="Near-Final Draft Under Review",
            source_row_id=source_draft_row_id,
            extra_instruction=(
                "Rewrite the full scene under the same facts. Treat the brief below as a literary rewrite brief, "
                "not a local patch request."
            ),
            patch_brief=revision_brief,
            source_draft_row_id=source_draft_row_id,
            source_draft_content=source_content,
            client_kind="style",
            execution_step_key=execution_step_key,
            attempt_details_extra={
                "source_evaluation_id": source_evaluation_id,
                "source_style_draft_row_id": source_draft_row_id,
                "rewrite_brief": revision_brief,
            },
        )

    @staticmethod
    def _build_style_user_prompt(
        base_prompt: str,
        *,
        neutral_content: str,
        source_label: str,
        source_row_id: str,
        extra_instruction: str,
        patch_brief: list[str] | None = None,
        patch_heading: str = "Patch Brief",
    ) -> str:
        return build_style_user_prompt(
            base_prompt,
            neutral_content=neutral_content,
            source_label=source_label,
            source_row_id=source_row_id,
            extra_instruction=extra_instruction,
            patch_brief=patch_brief,
            patch_heading=patch_heading,
        )

    def _prompt_builder(self) -> PromptBuilder:
        if self._prompt_builder_instance is None:
            self._prompt_builder_instance = PromptBuilder()
        return self._prompt_builder_instance

    def _inject_style_reference(
        self,
        prompt: dict[str, Any] | None,
        scene: SceneCard | None,
        *,
        task_type: str = "scene_generation",
        bundle: dict[str, Any] | None = None,
        context_text: str | None = None,
        final_user_prompt: str | None = None,
        placement: str = "system",
        role: str | None = None,
        situation_tags: Sequence[str] | None = None,
        revise_dimensions: Sequence[str] | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any] | None:
        """PR-8 §5.1 — 把 active StyleProfile 注入到 prompt["system_prompt"] 头部。

        v2（W5）：核心逻辑抽成模块级 ``inject_style_reference_prefix``，qc_engine 在
        soft_qc 阶段复用同一前缀；本方法只做委派，契约不变。
        2026-09-22 风格参考优先:起草通道传 ``placement=PLACEMENT_USER_TAIL``——样例块落到
        user 消息末尾,调用方随后用 :func:`apply_style_user_tail` 接上。
        风格参考 v3（P5b）：调用方显式给角色（首稿 ``draft`` + bundle 冻结的场面标签；定向修改 / 软补丁 ``revise``
        + 要改的维）；不给时适配器按落点推断（与旧行为相同）。
        """
        extra: dict[str, Any] = {}
        if role is not None:
            extra["role"] = role
        if situation_tags is not None:
            extra["situation_tags"] = situation_tags
        if revise_dimensions:
            extra["revise_dimensions"] = revise_dimensions
        if node_id:
            # 云策略按这一遍实际派发的节点判（H1）：调用方知道是 style_draft 还是 style_patch，不让适配器按模板从严猜
            extra["node_id"] = node_id
        return inject_style_reference_prefix(
            self.session,
            prompt,
            scene,
            bundle,
            task_type=task_type,
            context_text=context_text,
            final_user_prompt=final_user_prompt,
            placement=placement,
            **extra,
        )

    def _styled_draft_style_gate(
        self,
        scene: SceneCard,
        style_content: str,
        *,
        bundle: dict[str, Any] | None = None,
        stage: str = "style_draft",
    ) -> dict[str, Any] | None:
        """v2（规格 §2.W5.5）styled-draft gate：对已落库的风格化输出跑抄袭 + 生成禁用词。

        契约取自本次生成用的 bundle（与注入前缀同一冻结契约）；返回
        qc_engine.run_styled_draft_style_gate 的诊断字典；无绑定 → None；gate 自身失败 →
        ``verdict="unavailable"``（失败已 WARNING 落日志，不阻断生成，但必须可见——调用方
        据此发 STYLE_GATE_UNAVAILABLE notice）。``stage`` 为 style_draft 时升级到人工复核由
        soft_qc 阶段的同一 gate 完成；near_final_rewrite 由 orchestrator 直接处置。
        """
        try:
            return qc_engine.run_styled_draft_style_gate(
                self.session,
                scene,
                style_content,
                stage=stage,
                bundle=bundle,
            )
        except Exception as exc:  # noqa: BLE001 — gate 自身故障不阻断生成，但必须可见
            _LOGGER.warning(
                "styled-draft style gate failed for scene %s (stage=%s)",
                getattr(scene, "scene_id", None),
                stage,
                exc_info=True,
            )
            return qc_engine.styled_gate_unavailable_result(
                stage=stage, error=type(exc).__name__
            )

