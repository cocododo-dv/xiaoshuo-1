"""PreviewService — apply 前的预览面板(PR-4)。

参见《风格参考模块重构执行手册 v1.1》§5 / §8 与 plans/style-reference-v1-1-fancy-shannon.md
§"Preview 流程"。

流程:
  1. 从 profile.profile_json.scene_samples_index 选 3 种 paragraph_type
     (dialogue / description_env / psychology)
  2. 各调 `style_ref_preview_generate` LLM 生成 1 段 ≤500 字示例
  3. 过唯一抄袭门(``reference_copy_gate``:与这本书连续 ≥12 字相同或含受保护专名 → verdict ``plagiarism``,
     否则 ``pass``)
  4. 返回 list[PreviewSampleResult]

2026-09-23 风格参考 v3(P5b / U5):旧校验层删除,预览不再写回测报告(「回测已完成」原来是预览顺手写的),
``report_id`` 恒为空。LLM 调用失败时,该 sample 标 `error="llm_call_failed"`,verdict 留空,不阻塞其他 sample。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from novel_system.services.llm_client import LLMRequest, load_model_routing_config
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.prompt_builder import load_prompt_templates
from novel_system.services.style_reference._llm_helper import LLMNodeError, call_llm_node
from novel_system.services.style_reference.errors import (
    LLMRequiredError,
    StyleReferenceError,
)
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
from novel_system.services.style_reference.profile_fields import generation_safe_summary
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import (
    PreviewGeneratedSample,
    PreviewSampleResult,
)
from novel_system.services.style_reference.untrusted_data import UntrustedPayload

logger = logging.getLogger(__name__)

PREVIEW_NODE_ID = "style_ref_preview_generate"

DEFAULT_TARGET_TYPES: tuple[str, ...] = ("dialogue", "description_env", "psychology")


class PreviewError(StyleReferenceError):
    """PreviewService 内部错误。"""


class PreviewService:
    """3 段示例生成 + 唯一抄袭门(不落任何报告)。"""

    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        llm_enabled: bool | None = None,
    ) -> None:
        self.session = session
        self.repo = StyleReferenceRepository(session)
        self._llm_client = llm_client
        if llm_enabled is None:
            from novel_system.settings import get_settings

            llm_enabled = bool(get_settings().llm_enabled)
        self._llm_enabled = llm_enabled

    def generate(
        self,
        profile_id: str,
        *,
        target_types: tuple[str, ...] | None = None,
    ) -> list[PreviewSampleResult]:
        if not self._llm_enabled or self._llm_client is None:
            raise LLMRequiredError(operation="generate_preview")

        profile = self.repo.get_profile(profile_id)
        if profile is None:
            raise PreviewError(f"profile {profile_id!r} not found")
        # 附录 B — local_only 的书禁止把种子引文送往云端 LLM
        ensure_cloud_llm_allowed(
            self.repo.get_book(profile.book_id), operation="generate_preview"
        )

        profile_json = profile.profile_json or {}
        samples_index: dict[str, list[str]] = profile_json.get("scene_samples_index") or {}
        narrative_summary = generation_safe_summary(profile_json)
        style_features = (profile_json.get("style_features") or [])[:5]

        target_types = target_types or DEFAULT_TARGET_TYPES

        results: list[PreviewSampleResult] = []
        for ptype in target_types:
            candidate_ids = samples_index.get(ptype, [])
            seed_quote_text = ""
            if candidate_ids:
                seed = self.repo.get_quote(candidate_ids[0])
                if seed is not None:
                    seed_quote_text = (seed.quote_text or "")[:200]

            try:
                generated = self._call_llm(
                    profile_id,
                    ptype,
                    {
                        "profile_summary": narrative_summary,
                        "paragraph_type": ptype,
                        "seed_quote": seed_quote_text,
                        "style_features": style_features,
                    }
                )
                sample = PreviewGeneratedSample.model_validate(generated)
            except (ValidationError, PreviewError) as exc:
                logger.warning("preview LLM failed for %s: %s", ptype, exc)
                results.append(
                    PreviewSampleResult(
                        paragraph_type=ptype,
                        sample_text="",
                        report_id=None,
                        verdict=None,
                        error="llm_call_failed",
                    )
                )
                continue

            sample_text = sample.sample_text
            from novel_system.services.reference_copy_gate import check_reference_copy

            copy = check_reference_copy(
                self.session,
                sample_text,
                book_ids=[str(profile.book_id)],
                profile_ids=[str(profile.profile_id)],
            )
            results.append(
                PreviewSampleResult(
                    paragraph_type=ptype,
                    sample_text=sample_text,
                    report_id=None,
                    verdict="plagiarism" if copy.blocked else "pass",
                )
            )

        return results

    # ------------------------------------------------------------------ LLM

    def _call_llm(
        self,
        profile_id: str,
        paragraph_type: str,
        payload: dict,
    ) -> dict[str, Any]:
        # PR-8 §"_call_llm 统一" — 复用 _llm_helper.call_llm_node
        try:
            return call_llm_node(
                PREVIEW_NODE_ID,
                UntrustedPayload(payload),
                self._llm_client,
                session=self.session,
                context=LLMCallContext(
                    scope_type="style_reference_profile",
                    scope_id=profile_id,
                    node_id=PREVIEW_NODE_ID,
                    step=f"preview:{paragraph_type}",
                ),
            )
        except LLMNodeError as exc:
            raise PreviewError(str(exc)) from exc
