"""ProfileSynthesizer — 16 sub_dim findings → StyleProfile。

参见《风格参考模块重构执行手册 v1.1》§6.1(`style_ref_synthesize_profile`)与
plans/style-reference-v1-1-fancy-shannon.md §"ProfileSynthesizer 流程"。

profile_json 结构:
  - reference_basis(当前参考语料的动态来源契约，不含固定作者枚举)
  - narrative_summary(可复算精确统计，仅供内部审计/RAG)
  - qualitative_summary(LLM 产出并经过原文重合过滤，供生成提示使用)
  - metrics_baseline(从 book.stats_json.metrics 直读)
  - scene_samples_index({paragraph_type: [quote_id, ...]} 按 quotes 分桶)
  - sub_dimensions({sub_dim_path: {confidence, observation_count, ...}})
  - style_features / narrative_patterns / banned_replication_rules /
    calibration_guidance(LLM 产出,materialization 时分发到 4 集合)
  - narrative_guidance(确定性派生:narrative_patterns ∪ narrative.* forbidden
    statements,forbidden 陈述带「避免：」极性标记,去重、过滤原文重合,≤8 行;
    供规划/初稿阶段的叙事机制注入)
  - voice_signature(W3 确定性声音签名 + habits,≤12 行;模块未就绪时缺省)
  - structure_card / planning_guidance(2026-09-12 结构跟随:确定性结构画像——章 / 场尺度、
    开合方式、段型比重、章首章尾样例——与 scene.* / theme.* 观察陈述 ≤10 行;
    见 style_reference/structure.py;计算失败时缺省,下游按旧画像优雅退化)
  - anchor_quotes_used(审计:最终送入合成模型的锚引文条数)
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from typing import Mapping, TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from novel_system.services.context_budget import estimate_tokens
from novel_system.services.errors import DomainError
from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.prompt_builder import PromptTemplate, load_prompt_templates
from novel_system.services.style_reference._llm_helper import LLMNodeError, call_llm_node
from novel_system.services.style_reference.errors import LLMRequiredError, SynthesizeError
from novel_system.services.style_reference.narrative_guidance import (
    mark_forbidden_narrative_statement,
)
from novel_system.services.style_reference.import_progress import (
    ImportProgressReporter,
    NullImportProgress,
)
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
from novel_system.services.style_reference.profile_fields import REFERENCE_BASIS_VERSION
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import (
    ProfileStatus,
    SynthesizedProfile,
)
from novel_system.services.style_reference.structure import (
    compute_structure_card,
    derive_planning_guidance,
)
from novel_system.services.style_reference.untrusted_data import (
    UntrustedPayload,
    render_untrusted_system_prompt,
    render_untrusted_user_prompt,
)
from novel_system.services.style_reference.validation.plagiarism import (
    CorpusOverlapIndex,
    check_plagiarism,
    normalize_text_for_matching,
)

if TYPE_CHECKING:
    from novel_system.db.models import (
        StyleReferenceEvidence,
        StyleReferenceFinding,
        StyleReferenceProfile,
        StyleReferenceQuote,
    )

logger = logging.getLogger(__name__)

SYNTHESIZE_NODE_ID = "style_ref_synthesize_profile"
_PROFILE_SOURCE_OVERLAP_THRESHOLD = 8
_SYNTHESIS_SCHEMA_SAFETY_TOKENS = 128
_SYNTHESIS_TOKENIZER_SAFETY_MULTIPLIER = 1.25
_SYNTHESIS_STATEMENT_MAX_CHARS = 120
# 预算降级第 ③ 级:全体 statement 统一压到的字数梯度(先 80 再 56)。
_SYNTHESIS_STATEMENT_LIMIT_LADDER: tuple[int, ...] = (80, 56)
# 锚引文:每子维 ≤2 条、每条 ≤60 字(来自本 run 真实 paragraph_quote 证据)。
_ANCHOR_QUOTES_PER_DIMENSION = 2
_ANCHOR_QUOTE_MAX_CHARS = 60
_VOICE_HABITS_MAX_LINES = 12
_NARRATIVE_GUIDANCE_MAX_LINES = 8
_NARRATIVE_SUB_DIMENSION_PREFIX = "narrative."
_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}
_STATUS_RANK = {"approved": 1, "pending": 0}
_SYNTHESIS_METRIC_PRIORITY: tuple[str, ...] = (
    "avg_sentence_length",
    "sentence_length_std",
    "short_sentence_ratio",
    "long_sentence_ratio",
    "punctuation_density_per_1k",
    "dash_em_density_per_1k",
    "ellipsis_density_per_1k",
    "semicolon_density_per_1k",
    "question_density_per_1k",
    "classical_word_ratio",
    "colloquial_marker_ratio",
    "dialogue_ratio",
    "psychology_ratio",
    "description_env_ratio",
    "description_char_ratio",
    "action_ratio",
    "narration_ratio",
    "paragraph_mean_chars",
    "paragraph_length_std_chars",
    "paragraphs_per_1k",
    "single_sentence_paragraph_ratio",
    "quote_led_paragraph_ratio",
)
# 预算降级第 ② 级只丢"非必需"指标;这里的必需项与 injection._METRIC_REQUIRED_ORDER
# (生成提示里的量化锚)保持一致,由 tests/test_style_reference_synthesizer.py 钉住对齐。
_SYNTHESIS_REQUIRED_METRICS: tuple[str, ...] = (
    "paragraph_mean_chars",
    "paragraphs_per_1k",
    "avg_sentence_length",
    "punctuation_density_per_1k",
    "classical_word_ratio",
    "colloquial_marker_ratio",
)
# 有界业务重试两次都失败时,把内部失败标记映射到对外 reason_code。
_VALIDATION_FAILURE_REASON_CODES: dict[str, str] = {
    "invalid_or_empty_profile": "empty_profile",
    "profile_text_integrity_invalid": "text_integrity",
    "source_overlap_removed_required_content": "source_overlap",
}

__all__ = [
    "ProfileSynthesizer",
    "ProfileTextIntegrityError",
    "SYNTHESIZE_NODE_ID",
    "SynthesizeError",
]


class ProfileTextIntegrityError(SynthesizeError):
    """画像文本含编码替换符或控制字符，不能进入运行时提示。"""

    def __init__(self, violations: list[str]) -> None:
        self.violations = list(violations)
        super().__init__(
            "profile text integrity validation failed",
            reason_code="text_integrity",
            details={"violations": self.violations[:12]},
        )


class ProfileSynthesizer:
    """聚合 16 sub_dim findings → StyleReferenceProfile。"""

    def __init__(
        self,
        session: Session,
        *,
        llm_client: Any | None = None,
        llm_enabled: bool | None = None,
        progress: ImportProgressReporter | NullImportProgress | None = None,
    ) -> None:
        self.session = session
        self.repo = StyleReferenceRepository(session)
        self._llm_client = llm_client
        if llm_enabled is None:
            from novel_system.settings import get_settings

            llm_enabled = bool(get_settings().llm_enabled)
        self._llm_enabled = llm_enabled
        # 合成进度(2026-09-15):kind=synthesize 的登记簿句柄;无消费者时用空实现。
        self._progress = progress if progress is not None else NullImportProgress()

    def synthesize(self, book_id: str, run_id: str) -> "StyleReferenceProfile":
        if not self._llm_enabled or self._llm_client is None:
            raise LLMRequiredError(operation="synthesize_profile")
        self._progress.phase("collect")

        book = self.repo.get_book(book_id)
        if book is None:
            # run 存在但书不存在是数据不一致,不是合成失败;按 404 资源缺失暴露。
            raise DomainError(
                "STYLE_REFERENCE_BOOK_NOT_FOUND",
                f"book {book_id!r} not found",
                status_code=404,
                details={"book_id": book_id},
            )
        # 附录 B — local_only 的书禁止把 finding/quote 派生内容送往云端 LLM
        ensure_cloud_llm_allowed(book, operation="synthesize_profile")

        findings = self.repo.list_findings(book_id=book_id, run_id=run_id)
        # PR-23 — 被驳回的 finding 不进聚合 payload / source_finding_ids_json;
        # pending + approved 保留(审阅是可选环节,与 review 端点三态语义一致)
        findings = [f for f in findings if f.status != "rejected"]
        # 2026-07 勘误:quotes 原为 list_quotes(book_id) 全书跨 run——同书重复抽取
        # (未 reclassify)时,旧 run 的引文会混进本 profile 的 scene_samples_index /
        # quote_count / few-shot 池。改为 **run-scoped**:仅取本 run findings 经
        # evidence 关联的 quotes(与 source_finding_ids_json 同一物料来源)。
        finding_ids = [f.finding_id for f in findings]
        evidences = self.repo.list_evidences_for_findings(finding_ids)
        evidences.sort(key=lambda ev: (ev.created_at or "", ev.evidence_id))
        quotes = self._run_scoped_quotes(findings, evidences=evidences)

        sub_dim_summaries = _aggregate_sub_dim_stats(findings, quotes)
        book_stats = book.stats_json or {}
        metrics_baseline = {
            **(book_stats.get("metrics", {}) or {}),
            **(book_stats.get("prose_shape_metrics", {}) or {}),
        }
        paragraphs = self.repo.list_paragraphs(book_id)
        paragraph_types = {
            p.paragraph_id: p.paragraph_type
            for p in paragraphs
        }
        scene_samples_index = _build_scene_samples_index(quotes, paragraph_types)
        finding_summaries = _build_finding_summaries_payload(findings, evidences)
        corpus_texts = [str(p.text or "") for p in paragraphs if str(p.text or "")]
        # 2026-09-15:源文重合过滤要对上百行逐行判定,先把语料建成 8-gram 索引(一次几秒),
        # 之后每行微秒级;判定与逐行 check_plagiarism 完全等价,见 CorpusOverlapIndex。
        overlap_index = CorpusOverlapIndex(
            corpus_texts, threshold_chars=_PROFILE_SOURCE_OVERLAP_THRESHOLD
        )
        self._progress.set_totals(
            chars_total=int(book.total_chars or 0) or None,
            paragraphs_total=len(paragraphs),
            title=book.title,
        )
        # W3 确定性声音签名:未就绪 / 失败都不阻断合成,只是画像没有 voice_signature。
        self._progress.phase("voice")
        voice_signature = _compute_voice_signature_block(corpus_texts, overlap=overlap_index)
        voice_habits = list(voice_signature.get("habits") or []) if voice_signature else []
        # 锚引文只作机制锚点送入合成模型(cloud policy 已由 ensure_cloud_llm_allowed 把关,
        # 与抽取阶段送段落同一权限面);输出侧仍经 _contains_source_overlap 过滤。
        anchor_quotes = _build_anchor_quotes_payload(findings, quotes, evidences)

        raw_payload = {
            "book_title": book.title,
            "sub_dimensions": sub_dim_summaries,
            "metrics_baseline": _prune_metrics_for_prompt(metrics_baseline),
            "voice_habits": voice_habits,
            "anchor_quotes": anchor_quotes,
            "finding_summaries": finding_summaries,
        }
        template = load_prompt_templates()[SYNTHESIZE_NODE_ID]
        payload, input_budget_audit = _fit_synthesis_payload_to_budget(
            raw_payload,
            template,
        )

        (
            synthesized,
            safe_profile,
            overlap_audit,
            synthesis_attempt_audit,
        ) = self._synthesize_validated_profile(
            payload,
            template=template,
            corpus_texts=overlap_index,
            book_id=book_id,
            run_id=run_id,
        )
        self._progress.phase("filter")
        safe_forbidden_findings = [
            {
                "finding_id": str(f.finding_id),
                "sub_dimension": str(f.sub_dimension or ""),
                "statement": str(f.statement or "").strip(),
                "status": str(f.status or ""),
            }
            for f in findings
            if f.finding_kind == "forbidden_pattern"
            and str(f.statement or "").strip()
            and not _contains_source_overlap(str(f.statement), overlap_index)
        ]
        overlap_audit["dropped_forbidden_finding_count"] = sum(
            1 for f in findings if f.finding_kind == "forbidden_pattern"
        ) - len(safe_forbidden_findings)

        narrative_guidance = _derive_narrative_guidance(
            safe_profile["narrative_patterns"],
            safe_forbidden_findings,
            overlap_index,
        )
        # 2026-09-12 结构跟随(Track B):结构画像与规划层指引都是确定性派生,与
        # voice_signature 同一原则——任何失败只让画像缺键,绝不拖垮合成。
        self._progress.phase("derive")
        structure_card = _compute_structure_card_block(paragraphs, book_stats)
        planning_guidance = _derive_planning_guidance_block(findings, overlap_index)
        # 2026-09-14 保真修补(WP3):全书样例窗口索引——渲染期在整本书里选窗,不再只能以
        # 抽取证据引文为中心。确定性派生,失败只让画像缺键(渲染期会惰性复算)。
        exemplar_windows = _compute_exemplar_index_block(
            paragraphs,
            voice_signature=voice_signature,
            metrics_baseline=metrics_baseline,
            scene_breaks=book_stats.get("scene_breaks") if isinstance(book_stats, Mapping) else None,
        )

        metric_summary = _deterministic_metric_summary(metrics_baseline)
        profile_json: dict[str, Any] = {
            # 生产画像始终由当前用户导入的参考语料派生；作者名不是路由键，
            # 也不存在任何固定作者 allow-list。内置作者样本仅属于隔离基准。
            "reference_basis": {
                "version": REFERENCE_BASIS_VERSION,
                "mode": "reference_derived",
                "scope": "work_or_collection",
                "fixed_author_allowlist": False,
                "book_id": str(book.book_id),
                "source_kind": str(book.source_kind),
                "text_checksum": str(book.text_checksum),
                "source_char_count": int(book.total_chars or 0),
                "paragraph_count": len(paragraphs),
            },
            # 稳定、可复算的量化摘要留作内部审计/RAG；LLM 的安全定性概述
            # 单独保存给生成提示，并会在注入侧剔除频率/配额断言，避免同一
            # 语料两次合成出的漂移标签覆盖软分布真源。
            "narrative_summary": metric_summary
            or safe_profile["narrative_summary"],
            "qualitative_summary": safe_profile["narrative_summary"],
            "metrics_baseline": metrics_baseline,
            "scene_samples_index": scene_samples_index,
            "sub_dimensions": sub_dim_summaries,
            "style_features": safe_profile["style_features"],
            "narrative_patterns": safe_profile["narrative_patterns"],
            "banned_replication_rules": safe_profile["banned_replication_rules"],
            "calibration_guidance": safe_profile["calibration_guidance"],
            "narrative_guidance": narrative_guidance,
            "generation_safe_forbidden_findings": safe_forbidden_findings,
            "source_overlap_filter": overlap_audit,
            "synthesis_input_budget": input_budget_audit,
            "synthesis_attempts": synthesis_attempt_audit,
            # 审计:最终成功那次调用实际随 payload 送出的锚引文条数(重试可能再降级)。
            "anchor_quotes_used": int(synthesis_attempt_audit.get("anchor_quotes_used") or 0),
        }
        if voice_signature is not None:
            profile_json["voice_signature"] = voice_signature
        if structure_card is not None:
            profile_json["structure_card"] = structure_card
        if planning_guidance is not None:
            profile_json["planning_guidance"] = planning_guidance
        if exemplar_windows is not None:
            # 不进冻结键:契约冻结的是段落根哈希,根哈希一致时索引可按同一算法复算。
            profile_json["exemplar_windows"] = exemplar_windows

        self._progress.phase("persist")
        profile = self.repo.create_profile(
            profile_id=f"sr_profile_{uuid.uuid4().hex[:12]}",
            book_id=book_id,
            run_id=run_id,
            title=synthesized.profile_title,
            status=ProfileStatus.DRAFT.value,
            profile_json=profile_json,
            coverage_json={
                "sub_dim_count": len(sub_dim_summaries),
                "findings_count": len(findings),
                "quotes_count": len(quotes),
            },
            source_finding_ids_json=[f.finding_id for f in findings],
        )
        # 立项 C — profile 就绪即建三粒度 RAG 索引(Strategy C 召回的数据底座)。
        # 容错:向量后端不可用(如 Windows 原生 chroma)或失败均不阻断 synthesize。
        self._progress.phase("index")
        try:
            from novel_system.services.style_reference.rag import build_rag_index

            build_rag_index(
                self.session,
                profile,
                book_id=book_id,
                progress=_rag_progress_adapter(self._progress),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "rag index build failed for profile %s", profile.profile_id, exc_info=True
            )
        return profile

    def _run_scoped_quotes(
        self,
        findings: list["StyleReferenceFinding"],
        *,
        evidences: list["StyleReferenceEvidence"] | None = None,
    ) -> list["StyleReferenceQuote"]:
        """本 run findings 经 evidence 关联的 quotes(排序确定:created_at, quote_id)。"""
        finding_ids = [f.finding_id for f in findings]
        if evidences is None:
            evidences = self.repo.list_evidences_for_findings(finding_ids)
            evidences.sort(key=lambda ev: (ev.created_at or "", ev.evidence_id))
        quote_ids: list[str] = []
        seen: set[str] = set()
        for ev in evidences:
            if ev.quote_id and ev.quote_id not in seen:
                seen.add(ev.quote_id)
                quote_ids.append(ev.quote_id)
        quotes = self.repo.list_quotes_by_ids(quote_ids)
        quotes.sort(key=lambda q: (q.created_at or "", q.quote_id))
        return quotes

    # ------------------------------------------------------------------ LLM

    def _call_llm(
        self,
        node_id: str,
        payload: dict,
        *,
        book_id: str,
        run_id: str,
        attempt_no: int = 1,
    ) -> dict[str, Any]:
        # PR-8 §"_call_llm 统一" — 复用 _llm_helper.call_llm_node
        self._progress.llm_call(node_id)
        try:
            return call_llm_node(
                node_id,
                UntrustedPayload(payload),
                self._llm_client,
                session=self.session,
                context=LLMCallContext(
                    scope_type="style_reference_book",
                    scope_id=book_id,
                    node_id=node_id,
                    step=(
                        f"synthesize:{run_id}"
                        if attempt_no == 1
                        else f"synthesize:{run_id}:validation_retry"
                    ),
                ),
            )
        except LLMNodeError as exc:
            raise SynthesizeError(str(exc), reason_code="llm_failed") from exc

    def _synthesize_validated_profile(
        self,
        payload: dict[str, Any],
        *,
        template: PromptTemplate,
        corpus_texts: list[str] | CorpusOverlapIndex,
        book_id: str,
        run_id: str,
    ) -> tuple[SynthesizedProfile, dict[str, Any], dict[str, Any], dict[str, Any]]:
        """校验聚合结果；只对结构/安全内容失败做一次有界业务重试。"""

        attempt_payload = payload
        first_failure: dict[str, Any] | None = None
        retry_budget_audit: dict[str, Any] | None = None
        for attempt_no in (1, 2):
            self._progress.phase(
                "llm", detail=f"第 {attempt_no} 次" if attempt_no > 1 else None
            )
            structured = self._call_llm(
                SYNTHESIZE_NODE_ID,
                attempt_payload,
                book_id=book_id,
                run_id=run_id,
                attempt_no=attempt_no,
            )
            try:
                synthesized = SynthesizedProfile.model_validate(structured)
                safe_profile, overlap_audit = _sanitize_synthesized_profile(
                    synthesized,
                    corpus_texts,
                )
            except ValidationError as exc:
                failure = _profile_validation_failure(exc)
                failure_message = f"LLM response failed Pydantic validation: {exc}"
            except ProfileTextIntegrityError as exc:
                failure = {
                    "reason_code": "profile_text_integrity_invalid",
                    "violations": exc.violations[:12],
                }
                failure_message = str(exc)
            except SynthesizeError as exc:
                failure = {
                    "reason_code": "source_overlap_removed_required_content",
                    "violations": ["style_features_or_narrative_patterns_not_generation_safe"],
                }
                failure_message = str(exc)
            else:
                return (
                    synthesized,
                    safe_profile,
                    overlap_audit,
                    {
                        "attempt_count": attempt_no,
                        "retried": attempt_no > 1,
                        "first_failure": first_failure,
                        "retry_input_budget": retry_budget_audit,
                        "anchor_quotes_used": len(
                            attempt_payload.get("anchor_quotes") or []
                        ),
                    },
                )

            if attempt_no == 2:
                raise SynthesizeError(
                    failure_message,
                    reason_code=_VALIDATION_FAILURE_REASON_CODES.get(
                        str(failure.get("reason_code") or ""), "empty_profile"
                    ),
                    details={
                        "attempt_count": attempt_no,
                        "first_failure": first_failure,
                        "violations": list(failure.get("violations") or [])[:12],
                    },
                )

            first_failure = failure
            logger.warning(
                "profile synthesis validation failed; retrying once: %s",
                failure["reason_code"],
            )
            retry_payload = {
                **payload,
                "validation_retry": {
                    **failure,
                    "attempt": 2,
                    "required_action": (
                        "重新聚合；profile_title、narrative_summary、style_features、"
                        "narrative_patterns 必须非空，输出必须是有效 Unicode，"
                        "且只写不复用原文字词的抽象机制"
                    ),
                },
            }
            attempt_payload, retry_budget_audit = _fit_synthesis_payload_to_budget(
                retry_payload,
                template,
            )

        raise AssertionError("unreachable")


def _profile_validation_failure(exc: ValidationError) -> dict[str, Any]:
    violations = [
        f"{'.'.join(str(part) for part in error.get('loc', ())) or 'profile'}:"
        f"{error.get('type', 'invalid')}"
        for error in exc.errors(include_url=False, include_input=False)
    ]
    return {
        "reason_code": "invalid_or_empty_profile",
        "violations": violations[:12],
    }


def _estimate_synthesis_input_tokens(
    template: PromptTemplate,
    payload: dict[str, Any],
) -> int:
    system_prompt = render_untrusted_system_prompt(template.system_prompt)
    user_prompt = render_untrusted_user_prompt(
        template.task_prompt,
        UntrustedPayload(payload),
        kind=SYNTHESIZE_NODE_ID,
    )
    schema_text = json.dumps(
        template.structured_schema,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    deterministic_estimate = (
        estimate_tokens(system_prompt)
        + estimate_tokens(user_prompt)
        + estimate_tokens(schema_text)
    )
    return (
        math.ceil(deterministic_estimate * _SYNTHESIS_TOKENIZER_SAFETY_MULTIPLIER)
        + _SYNTHESIS_SCHEMA_SAFETY_TOKENS
    )


def _fit_synthesis_payload_to_budget(
    payload: dict[str, Any],
    template: PromptTemplate,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把画像聚合输入真正压进模板预算——全局分级降级,而不是逐条挤压。

    级别(前一级整体应用后仍装不下才进入下一级;最终落在的那一级内取最小改动):
      ① 全量;
      ② 去掉非必需指标(保留 ``_SYNTHESIS_REQUIRED_METRICS``,与注入侧量化锚对齐);
      ③ 全体 statement 统一压到 80、再 56 字;
      ④ 锚引文每子维 2→1→0;
      ⑤ 按 status / confidence / evidence_count 轮转丢弃多余 finding,但每子维保留
         ≥1 observation 且 ≥1 forbidden_pattern(若存在);
    仍装不下才失败(``reason_code=budget_unfit``)。审计字段向后兼容并新增降级级别。
    """

    target = int(template.input_token_budget)
    before = _estimate_synthesis_input_tokens(template, payload)
    rows = [
        dict(row)
        for row in (payload.get("finding_summaries") or [])
        if isinstance(row, dict)
    ]
    metrics_before = dict(payload.get("metrics_baseline") or {})
    anchors_before = [
        dict(item)
        for item in (payload.get("anchor_quotes") or [])
        if isinstance(item, dict)
    ]
    protected, drop_order = _synthesis_finding_drop_plan(rows)
    drop_rank = {index: position for position, index in enumerate(drop_order)}
    presentation_order = _synthesis_presentation_order(rows)

    def build(
        *,
        metrics: dict[str, Any],
        statement_limit: int,
        anchor_limit: int,
        dropped_count: int,
    ) -> dict[str, Any]:
        kept_rows = [
            _compact_finding_summary(rows[index], statement_limit=statement_limit)
            for index in presentation_order
            if drop_rank.get(index, len(drop_order)) >= dropped_count
        ]
        anchors = _limit_anchor_quotes(anchors_before, anchor_limit)
        fitted: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "metrics_baseline":
                fitted[key] = metrics
            elif key == "finding_summaries":
                fitted[key] = kept_rows
            elif key == "anchor_quotes":
                fitted[key] = anchors
            else:
                fitted[key] = value
        fitted.setdefault("metrics_baseline", metrics)
        fitted.setdefault("finding_summaries", kept_rows)
        return fitted

    def fits(candidate: dict[str, Any]) -> bool:
        return _estimate_synthesis_input_tokens(template, candidate) <= target

    stage = "full"
    metrics = dict(metrics_before)
    statement_limit = _SYNTHESIS_STATEMENT_MAX_CHARS
    anchor_limit = _ANCHOR_QUOTES_PER_DIMENSION
    dropped_count = 0

    def current() -> dict[str, Any]:
        return build(
            metrics=metrics,
            statement_limit=statement_limit,
            anchor_limit=anchor_limit,
            dropped_count=dropped_count,
        )

    fitted = current()
    if not fits(fitted):
        # ② 非必需指标:先试"全部去掉"能否装下,能则二分出最少需要去掉的个数。
        stage = "drop_optional_metrics"
        droppable = [
            name
            for name in _synthesis_metric_drop_order(metrics_before)
            if name not in _SYNTHESIS_REQUIRED_METRICS
        ]

        def without_metrics(count: int) -> dict[str, Any]:
            removed = set(droppable[:count])
            return {name: value for name, value in metrics_before.items() if name not in removed}

        minimal = _smallest_fitting(
            len(droppable),
            lambda count: fits(
                build(
                    metrics=without_metrics(count),
                    statement_limit=statement_limit,
                    anchor_limit=anchor_limit,
                    dropped_count=dropped_count,
                )
            ),
        )
        metrics = without_metrics(minimal if minimal is not None else len(droppable))
        fitted = current()

    if not fits(fitted):
        # ③ 全体 statement 统一压缩(80 → 56 字)。
        stage = "compress_statements"
        for limit in _SYNTHESIS_STATEMENT_LIMIT_LADDER:
            statement_limit = limit
            fitted = current()
            if fits(fitted):
                break

    if not fits(fitted):
        # ④ 锚引文 2 → 1 → 0。
        stage = "trim_anchor_quotes"
        for limit in range(_ANCHOR_QUOTES_PER_DIMENSION - 1, -1, -1):
            anchor_limit = limit
            fitted = current()
            if fits(fitted):
                break

    if not fits(fitted):
        # ⑤ 轮转丢弃多余 finding;受保护的每子维 obs/fp 永不丢弃。
        stage = "drop_findings"
        minimal = _smallest_fitting(
            len(drop_order),
            lambda count: fits(
                build(
                    metrics=metrics,
                    statement_limit=statement_limit,
                    anchor_limit=anchor_limit,
                    dropped_count=count,
                )
            ),
        )
        if minimal is None:
            floor_payload = build(
                metrics=metrics,
                statement_limit=statement_limit,
                anchor_limit=anchor_limit,
                dropped_count=len(drop_order),
            )
            floor_estimate = _estimate_synthesis_input_tokens(template, floor_payload)
            raise SynthesizeError(
                "style profile synthesis input cannot fit input_token_budget even after "
                f"keeping one observation and one forbidden pattern per sub-dimension: "
                f"{floor_estimate}>{target}",
                reason_code="budget_unfit",
                details={
                    "target_input_tokens": target,
                    "estimated_before": before,
                    "estimated_floor": floor_estimate,
                    "protected_finding_count": len(protected),
                },
            )
        dropped_count = minimal
        fitted = current()

    after = _estimate_synthesis_input_tokens(template, fitted)
    if after > target:  # pragma: no cover - 上面的分级已保证;保留兜底
        raise SynthesizeError(
            f"style profile synthesis input budget fit failed: {after}>{target}",
            reason_code="budget_unfit",
            details={"target_input_tokens": target, "estimated_after": after},
        )

    selected = fitted.get("finding_summaries") or []
    selected_by_dimension = Counter(
        str(row.get("sub_dimension") or "unknown") for row in selected
    )
    audit = {
        "applied": before > after,
        "target_input_tokens": target,
        "estimated_before": before,
        "estimated_after": after,
        "schema_safety_tokens": _SYNTHESIS_SCHEMA_SAFETY_TOKENS,
        "tokenizer_safety_multiplier": _SYNTHESIS_TOKENIZER_SAFETY_MULTIPLIER,
        "degradation_stage": stage,
        "statement_max_chars": statement_limit,
        "finding_count_before": len(rows),
        "finding_count_after": len(selected),
        "protected_finding_count": len(protected),
        "metric_count_before": len(metrics_before),
        "metric_count_after": len(metrics),
        "anchor_quote_count_before": len(anchors_before),
        "anchor_quote_count_after": len(fitted.get("anchor_quotes") or []),
        "covered_sub_dimensions": sorted(selected_by_dimension),
        "selected_by_dimension": dict(sorted(selected_by_dimension.items())),
    }
    return fitted, audit


def _smallest_fitting(count: int, fits_at: Callable[[int], bool]) -> int | None:
    """在单调的 ``fits_at`` 上二分:返回 [1, count] 中最小的 k;全部不满足返回 None。"""

    if count <= 0 or not fits_at(count):
        return None
    low, high = 1, count
    while low < high:
        middle = (low + high) // 2
        if fits_at(middle):
            high = middle
        else:
            low = middle + 1
    return low


def _finding_rank(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int, str, int]:
    """越小越强:approved > pending,high > medium > low,证据越多越强;末位用原序保证确定。"""

    index, row = item
    return (
        -_STATUS_RANK.get(str(row.get("status") or "pending"), 0),
        -_CONFIDENCE_RANK.get(str(row.get("confidence") or "medium"), 1),
        -int(row.get("evidence_count") or 0),
        str(row.get("statement") or ""),
        index,
    )


def _group_rows_by_dimension(
    rows: list[dict[str, Any]],
) -> dict[str, list[tuple[int, dict[str, Any]]]]:
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for item in enumerate(rows):
        grouped[str(item[1].get("sub_dimension") or "unknown")].append(item)
    return grouped


def _synthesis_presentation_order(rows: list[dict[str, Any]]) -> list[int]:
    """送给模型的顺序:按子维分组、组内强证据在前,便于模型逐维读完再下笔。"""

    grouped = _group_rows_by_dimension(rows)
    order: list[int] = []
    for sub_dimension in sorted(grouped):
        order.extend(index for index, _ in sorted(grouped[sub_dimension], key=_finding_rank))
    return order


def _synthesis_finding_drop_plan(
    rows: list[dict[str, Any]],
) -> tuple[set[int], list[int]]:
    """返回 (受保护 finding 下标, 丢弃顺序)。

    每子维保护最强的 1 条 observation 与 1 条 forbidden_pattern(若存在;两类都没有时
    保护最强的 1 条)。丢弃顺序按轮转生成:每一轮从每个子维各丢 1 条最弱的,剩余
    finding 多的子维先丢,保证子维之间的保留数最多相差 1。
    """

    grouped = _group_rows_by_dimension(rows)
    protected: set[int] = set()
    droppable_by_dimension: dict[str, list[int]] = {}
    for sub_dimension in sorted(grouped):
        candidates = sorted(grouped[sub_dimension], key=_finding_rank)
        observations = [
            index
            for index, row in candidates
            if str(row.get("finding_kind")) == "observation"
        ]
        forbidden = [
            index
            for index, row in candidates
            if str(row.get("finding_kind")) == "forbidden_pattern"
        ]
        if observations:
            protected.add(observations[0])
        if forbidden:
            protected.add(forbidden[0])
        if not observations and not forbidden:
            protected.add(candidates[0][0])
        droppable_by_dimension[sub_dimension] = [
            index for index, _ in reversed(candidates) if index not in protected
        ]

    drop_order: list[int] = []
    while any(droppable_by_dimension.values()):
        round_dimensions = sorted(
            droppable_by_dimension,
            key=lambda name: (-len(droppable_by_dimension[name]), name),
        )
        for sub_dimension in round_dimensions:
            remaining = droppable_by_dimension[sub_dimension]
            if remaining:
                drop_order.append(remaining.pop(0))
    return protected, drop_order


def _limit_anchor_quotes(
    anchors: list[dict[str, Any]],
    per_dimension: int,
) -> list[dict[str, Any]]:
    """每子维只保留前 ``per_dimension`` 条(锚引文列表已按子维强证据优先排好)。"""

    if per_dimension <= 0:
        return []
    kept: list[dict[str, Any]] = []
    seen: Counter[str] = Counter()
    for item in anchors:
        sub_dimension = str(item.get("sub_dimension") or "unknown")
        if seen[sub_dimension] >= per_dimension:
            continue
        seen[sub_dimension] += 1
        kept.append(item)
    return kept


def _compact_finding_summary(
    row: dict[str, Any],
    *,
    statement_limit: int,
) -> dict[str, Any]:
    try:
        evidence_count = max(0, int(row.get("evidence_count") or 0))
    except (TypeError, ValueError):
        evidence_count = 0
    return {
        "sub_dimension": str(row.get("sub_dimension") or "unknown"),
        "finding_kind": str(row.get("finding_kind") or "observation"),
        "statement": str(row.get("statement") or "").strip()[:statement_limit],
        # 这些字段参与 coverage-first 排序，也必须真正送到合成模型。
        # 旧实现排序后把它们丢掉，模型无法区分“2 条 pending 证据”和
        # “多条 approved/high 证据”，容易把偶发样例夸成高频规律。
        "confidence": str(row.get("confidence") or "medium"),
        "status": str(row.get("status") or "pending"),
        "evidence_count": evidence_count,
    }


def _synthesis_metric_drop_order(metrics: dict[str, Any]) -> list[str]:
    priority = [name for name in _SYNTHESIS_METRIC_PRIORITY if name in metrics]
    nonpriority = sorted(name for name in metrics if name not in priority)
    return [*nonpriority, *reversed(priority)]


def _contains_source_overlap(
    text: str, corpus_texts: list[str] | CorpusOverlapIndex
) -> bool:
    if not text.strip() or not corpus_texts:
        return False
    if isinstance(corpus_texts, CorpusOverlapIndex):
        return corpus_texts.contains_overlap(text, ngram_size=6)
    return not check_plagiarism(
        text,
        corpus_texts,
        ngram_size=6,
        threshold_chars=_PROFILE_SOURCE_OVERLAP_THRESHOLD,
    ).passed


def _sanitize_synthesized_profile(
    synthesized: SynthesizedProfile,
    corpus_texts: list[str] | CorpusOverlapIndex,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """阻止聚合模型把参考原句伪装成抽象风格指令带入生成提示。"""

    integrity_violations = _profile_text_integrity_violations(synthesized)
    if integrity_violations:
        raise ProfileTextIntegrityError(integrity_violations)

    dropped_counts: dict[str, int] = {}

    def safe_items(field: str, items: list[str]) -> list[str]:
        safe = [
            str(item).strip()
            for item in items
            if str(item).strip()
            and not _contains_source_overlap(str(item), corpus_texts)
        ]
        dropped_counts[field] = len(items) - len(safe)
        return safe

    style_features = safe_items("style_features", synthesized.style_features)
    narrative_patterns = safe_items(
        "narrative_patterns", synthesized.narrative_patterns
    )
    banned_rules = safe_items(
        "banned_replication_rules", synthesized.banned_replication_rules
    )
    calibration = safe_items(
        "calibration_guidance", synthesized.calibration_guidance
    )
    if not style_features or not narrative_patterns:
        raise SynthesizeError(
            "profile source-overlap filter removed every required style feature or narrative pattern",
            reason_code="source_overlap",
        )

    summary = synthesized.narrative_summary.strip()
    summary_replaced = _contains_source_overlap(summary, corpus_texts)
    if summary_replaced:
        summary = "；".join([*style_features[:2], *narrative_patterns[:2]])[:200]

    return (
        {
            "narrative_summary": summary,
            "style_features": style_features,
            "narrative_patterns": narrative_patterns,
            "banned_replication_rules": banned_rules,
            "calibration_guidance": calibration,
        },
        {
            "applied": True,
            "threshold_chars": _PROFILE_SOURCE_OVERLAP_THRESHOLD,
            "summary_replaced": summary_replaced,
            "dropped_counts": dropped_counts,
        },
    )


def _deterministic_metric_summary(metrics_baseline: dict[str, Any]) -> str:
    """把冻结基线渲染成稳定概述；不使用作者名、主题词或 LLM 判断。"""

    def mean(name: str) -> float | None:
        stats = metrics_baseline.get(name)
        if not isinstance(stats, dict):
            return None
        try:
            value = float(stats.get("mean"))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    parts: list[str] = []
    sentence = mean("avg_sentence_length")
    short = mean("short_sentence_ratio")
    long = mean("long_sentence_ratio")
    if sentence is not None:
        sentence_part = f"句均约{sentence:.1f}字"
        if short is not None:
            sentence_part += f"、短句约{short * 100:.0f}%"
        if long is not None:
            sentence_part += f"、长句约{long * 100:.0f}%"
        parts.append(sentence_part)

    paragraph_mean = mean("paragraph_mean_chars")
    paragraph_rate = mean("paragraphs_per_1k")
    if paragraph_mean is not None or paragraph_rate is not None:
        paragraph_parts: list[str] = []
        if paragraph_mean is not None:
            paragraph_parts.append(f"段均约{paragraph_mean:.1f}字")
        if paragraph_rate is not None:
            paragraph_parts.append(f"每千字约{paragraph_rate:.1f}段")
        parts.append("、".join(paragraph_parts))

    punctuation = mean("punctuation_density_per_1k")
    semicolon = mean("semicolon_density_per_1k")
    ellipsis = mean("ellipsis_density_per_1k")
    if punctuation is not None or semicolon is not None or ellipsis is not None:
        punctuation_parts: list[str] = []
        if punctuation is not None:
            punctuation_parts.append(f"每千字标点约{punctuation:.1f}")
        if semicolon is not None:
            punctuation_parts.append(f"分号约{semicolon:.1f}")
        if ellipsis is not None:
            punctuation_parts.append(f"省略号约{ellipsis:.1f}")
        parts.append("、".join(punctuation_parts))

    if not parts:
        return ""
    return "量化基线（与定性描述冲突时以此为准）：" + "；".join(parts) + "。"


_PROFILE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _profile_text_integrity_violations(
    synthesized: SynthesizedProfile,
) -> list[str]:
    """只返回字段/标记，不把可能含参考内容的画像正文写入审计。"""

    violations: list[str] = []
    fields: dict[str, list[str]] = {
        "profile_title": [synthesized.profile_title],
        "narrative_summary": [synthesized.narrative_summary],
        "style_features": list(synthesized.style_features),
        "narrative_patterns": list(synthesized.narrative_patterns),
        "banned_replication_rules": list(synthesized.banned_replication_rules),
        "calibration_guidance": list(synthesized.calibration_guidance),
    }
    for field, values in fields.items():
        for value in values:
            markers: list[str] = []
            if "\ufffd" in str(value):
                markers.append("replacement_character")
            if _PROFILE_CONTROL_RE.search(str(value)):
                markers.append("control_character")
            for marker in markers:
                violation = f"{field}:{marker}"
                if violation not in violations:
                    violations.append(violation)
    return violations


# ---------------------------------------------------------------------------
# 聚合辅助函数
# ---------------------------------------------------------------------------


def _aggregate_sub_dim_stats(
    findings: list["StyleReferenceFinding"],
    quotes: list["StyleReferenceQuote"],
) -> dict[str, dict[str, Any]]:
    """按 sub_dimension 分桶,统计 obs / forbid / quote 数量与置信度概要。"""
    by_sub_dim: dict[str, dict[str, int]] = defaultdict(
        lambda: {"observation_count": 0, "forbidden_pattern_count": 0, "quote_count": 0}
    )
    confidence_counter: dict[str, Counter] = defaultdict(Counter)

    for f in findings:
        key = f.sub_dimension
        if f.finding_kind == "observation":
            by_sub_dim[key]["observation_count"] += 1
        elif f.finding_kind == "forbidden_pattern":
            by_sub_dim[key]["forbidden_pattern_count"] += 1
        confidence_counter[key][f.confidence or "medium"] += 1

    # quote_count 按 illustrates_dims 分桶
    for q in quotes:
        for dim in q.illustrates_dims or []:
            if dim in by_sub_dim:
                by_sub_dim[dim]["quote_count"] += 1

    result: dict[str, dict[str, Any]] = {}
    for sub_dim, counts in by_sub_dim.items():
        conf = confidence_counter[sub_dim].most_common(1)
        result[sub_dim] = {
            **counts,
            "confidence": conf[0][0] if conf else "medium",
        }
    return result


def _build_scene_samples_index(
    quotes: list["StyleReferenceQuote"],
    paragraph_types: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """按 paragraph_type 把 quote_id 分桶。

    `quote.paragraph_id` 关联回 paragraph,这里只用 quote_id 作为索引值;
    Few-shot 调用方按 paragraph_type 拉对应 quote_id list 再 fetch quote_text。

    只收 anchor_kind=paragraph_quote 的真实原文引文:counter_example 是与原作
    风格**相悖**的合成反例,author_avoidance 是统计说明文本,二者进样例索引会被
    few-shot 当作风格范例注入。段落类型优先用段落表实测(`paragraph_types`),
    其次 quote 落库时冗余的 extracted_features.paragraph_type,最后回退 "narration"。
    """
    paragraph_types = paragraph_types or {}
    index: dict[str, list[str]] = defaultdict(list)
    for q in quotes:
        feats = q.extracted_features or {}
        if feats.get("anchor_kind", "paragraph_quote") != "paragraph_quote":
            continue
        if not q.paragraph_id:
            continue
        ptype = (
            paragraph_types.get(q.paragraph_id)
            or feats.get("paragraph_type")
            or "narration"
        )
        index[ptype].append(q.quote_id)
    return dict(index)


def _build_finding_summaries_payload(
    findings: list["StyleReferenceFinding"],
    evidences: list["StyleReferenceEvidence"],
) -> list[dict[str, Any]]:
    """画像聚合只消费已验证的抽象 finding，不重复发送参考原文。"""

    evidence_counts = Counter(evidence.finding_id for evidence in evidences)
    return [
        {
            "sub_dimension": str(finding.sub_dimension or ""),
            "finding_kind": str(finding.finding_kind or "observation"),
            "statement": str(finding.statement or "").strip()[
                :_SYNTHESIS_STATEMENT_MAX_CHARS
            ],
            "confidence": str(finding.confidence or "medium"),
            "status": str(finding.status or "pending"),
            "evidence_count": int(evidence_counts.get(finding.finding_id, 0)),
        }
        for finding in findings
        if str(finding.statement or "").strip()
    ]


def _build_sample_quotes_payload(
    findings: list["StyleReferenceFinding"],
    quotes: list["StyleReferenceQuote"],
    evidences: list["StyleReferenceEvidence"],
) -> list[dict[str, str]]:
    """每条 finding 配自己的 evidence quote，而不是同维度的任意 quote。

    evidence 优先真实 paragraph_quote，再取其它真实证据，最后才取合成证据。
    同一 quote 可以合法支撑多个 finding，不因全局去重而改配无关引文。控制
    prompt token 在合理范围(每 finding < 400 字)。
    """
    quote_by_id = {q.quote_id: q for q in quotes}
    evidence_by_finding: dict[str, list["StyleReferenceEvidence"]] = defaultdict(list)
    for evidence in evidences:
        evidence_by_finding[evidence.finding_id].append(evidence)
    for rows in evidence_by_finding.values():
        rows.sort(
            key=lambda ev: (
                bool(ev.is_synthetic),
                ev.anchor_kind != "paragraph_quote",
                ev.created_at or "",
                ev.evidence_id,
            )
        )

    payload: list[dict[str, str]] = []
    for f in findings:
        repr_quote: str = ""
        for evidence in evidence_by_finding.get(f.finding_id, []):
            q = quote_by_id.get(evidence.quote_id)
            if q is not None and (q.quote_text or "").strip():
                repr_quote = (q.quote_text or "")[:200]
                break
        payload.append(
            {
                "sub_dimension": f.sub_dimension,
                "finding_kind": f.finding_kind,
                "statement": (f.statement or "")[:120],
                "representative_quote": repr_quote,
            }
        )
    return payload


_ANCHOR_SENTENCE_BOUNDARY_RE = re.compile(r"[。！？；!?;]")


def _trim_anchor_quote(text: str, max_chars: int = _ANCHOR_QUOTE_MAX_CHARS) -> str:
    """规整空白并截到 ≤max_chars 字;截断时尽量落在句末标点,避免半截句误导模型。"""

    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= max_chars:
        return collapsed
    window = collapsed[:max_chars]
    boundaries = [match.end() for match in _ANCHOR_SENTENCE_BOUNDARY_RE.finditer(window)]
    # 至少保留一半长度,否则宁可硬截也不要只剩一个短句。
    usable = [end for end in boundaries if end >= max_chars // 2]
    return window[: usable[-1]] if usable else window


def _build_anchor_quotes_payload(
    findings: list["StyleReferenceFinding"],
    quotes: list["StyleReferenceQuote"],
    evidences: list["StyleReferenceEvidence"],
    *,
    per_dimension: int = _ANCHOR_QUOTES_PER_DIMENSION,
    max_chars: int = _ANCHOR_QUOTE_MAX_CHARS,
) -> list[dict[str, str]]:
    """每子维 ≤per_dimension 条、每条 ≤max_chars 字的代表锚引文(run-scoped)。

    只取真实、非合成的 paragraph_quote 证据(counter_example / author_avoidance 不是
    原文,不能当机制锚点);同一子维内按 finding 强弱依次各取 1 条,避免两条锚都
    来自同一 finding。提示词明令锚引文只作机制锚点、不得复述进任何输出字段,输出
    仍由 _contains_source_overlap 兜底过滤。
    """

    if per_dimension <= 0:
        return []
    quote_by_id = {q.quote_id: q for q in quotes}
    evidence_by_finding: dict[str, list["StyleReferenceEvidence"]] = defaultdict(list)
    for evidence in evidences:
        evidence_by_finding[evidence.finding_id].append(evidence)
    for evidence_rows in evidence_by_finding.values():
        evidence_rows.sort(key=lambda ev: (ev.created_at or "", ev.evidence_id))

    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    finding_by_index: dict[int, "StyleReferenceFinding"] = {}
    for index, finding in enumerate(findings):
        finding_by_index[index] = finding
        grouped[str(finding.sub_dimension or "unknown")].append(
            (
                index,
                {
                    "status": finding.status,
                    "confidence": finding.confidence,
                    "evidence_count": len(evidence_by_finding.get(finding.finding_id, [])),
                    "statement": finding.statement,
                },
            )
        )

    payload: list[dict[str, str]] = []
    for sub_dimension in sorted(grouped):
        picked = 0
        seen: set[str] = set()
        for index, _ in sorted(grouped[sub_dimension], key=_finding_rank):
            finding = finding_by_index[index]
            for evidence in evidence_by_finding.get(finding.finding_id, []):
                if bool(evidence.is_synthetic) or evidence.anchor_kind != "paragraph_quote":
                    continue
                quote = quote_by_id.get(evidence.quote_id)
                if quote is None:
                    continue
                features = quote.extracted_features or {}
                if features.get("anchor_kind", "paragraph_quote") != "paragraph_quote":
                    continue
                text = _trim_anchor_quote(quote.quote_text or "", max_chars)
                if not text or text in seen:
                    continue
                seen.add(text)
                payload.append({"sub_dimension": sub_dimension, "quote": text})
                picked += 1
                break
            if picked >= per_dimension:
                break
    return payload


def _derive_narrative_guidance(
    narrative_patterns: list[str],
    safe_forbidden_findings: list[dict[str, Any]],
    corpus_texts: list[str] | CorpusOverlapIndex,
    *,
    max_lines: int = _NARRATIVE_GUIDANCE_MAX_LINES,
) -> list[str]:
    """确定性派生叙事机制指引:narrative_patterns ∪ narrative.* forbidden statements。

    forbidden 陈述按抽取约定命名的是「作者明确不用的模式」本身,平铺进同一列表时
    极性会丢——这里给每条加 ``避免：`` 标记(已以否定词起头的原样保留,见
    :func:`mark_forbidden_narrative_statement`),渲染到 "Narrative Mechanisms" 后
    每行自带方向。原文重合过滤作用于陈述原文;去重按加标记后的规范化文本,
    ≤max_lines 行。供规划 / 初稿阶段注入(W5),不含任何原文引文。
    """

    candidates: list[tuple[str, bool]] = [(str(item), False) for item in narrative_patterns]
    candidates.extend(
        (str(finding.get("statement") or ""), True)
        for finding in safe_forbidden_findings
        if str(finding.get("sub_dimension") or "").startswith(_NARRATIVE_SUB_DIMENSION_PREFIX)
    )
    guidance: list[str] = []
    seen: set[str] = set()
    for candidate, is_forbidden in candidates:
        text = " ".join(candidate.split()).strip()
        if not text:
            continue
        if _contains_source_overlap(text, corpus_texts):
            continue
        line = mark_forbidden_narrative_statement(text) if is_forbidden else text
        key = normalize_text_for_matching(line)
        if not key or key in seen:
            continue
        seen.add(key)
        guidance.append(line)
        if len(guidance) >= max_lines:
            break
    return guidance


def _json_scalar_fallback(value: Any) -> float | str:
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _rag_progress_adapter(progress: Any) -> Any:
    """``build_rag_index`` 的 ``progress(stage, done, total)`` 回调 → 登记簿步骤。"""

    def report(stage: str, done: int, total: int) -> None:
        label = "段" if stage == "signatures" else "粒度"
        try:
            progress.set_steps(int(done), int(total), label=label)
        except Exception:  # noqa: BLE001 — 进度汇报绝不打断建索引
            logger.debug("rag progress report failed", exc_info=True)

    return report


def _compute_voice_signature_block(
    paragraph_texts: list[str],
    *,
    overlap: CorpusOverlapIndex | None = None,
) -> dict[str, Any] | None:
    """调用 W3 的确定性声音签名;模块未就绪(ImportError)或任何异常都不阻断合成。

    返回 ``{**signature, "habits": [...]}``(habits ≤12 行,已过原文重合过滤),
    或 None(此时 profile_json 不写 voice_signature,下游按旧画像优雅退化)。
    """

    try:
        from novel_system.services.style_reference.voice_signature import (
            compute_voice_signature,
            render_voice_habits,
        )
    except ImportError:
        logger.warning("voice_signature module unavailable; profile omits voice_signature")
        return None
    except Exception:  # noqa: BLE001 - 模块加载期任何错误(含 SyntaxError)都不能拖垮合成
        logger.warning(
            "voice_signature module failed to load; profile omits voice_signature",
            exc_info=True,
        )
        return None
    try:
        signature = compute_voice_signature(list(paragraph_texts))
        if not isinstance(signature, Mapping):
            raise TypeError("compute_voice_signature must return a mapping")
        if not isinstance(signature.get("features"), Mapping):
            raise TypeError("voice signature is missing its features mapping")
        # 传整份签名:渲染器可用 top_words / stats / block_count 给出具体词与聚合尺度。
        habits = render_voice_habits(dict(signature))
        # profile_json 是 JSON 列:把签名规整成纯 JSON 值(numpy 标量等一律转 float/str),
        # 否则落库时才炸、还会把整次合成拖垮。
        signature = json.loads(
            json.dumps(dict(signature), ensure_ascii=False, default=_json_scalar_fallback)
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "voice signature computation failed; profile omits voice_signature",
            exc_info=True,
        )
        return None
    habit_lines: list[str] = []
    for habit in habits or []:
        line = " ".join(str(habit).split()).strip()
        if not line or line in habit_lines:
            continue
        if _contains_source_overlap(line, overlap if overlap is not None else paragraph_texts):
            continue
        habit_lines.append(line)
        if len(habit_lines) >= _VOICE_HABITS_MAX_LINES:
            break
    return {**dict(signature), "habits": habit_lines}


def _compute_exemplar_index_block(
    paragraphs: list[Any],
    *,
    voice_signature: Mapping[str, Any] | None,
    metrics_baseline: Mapping[str, Any] | None,
    scene_breaks: Any = None,
) -> dict[str, Any] | None:
    """全书样例窗口索引(2026-09-14 WP3);任何失败返回 None,合成不受影响。"""
    try:
        from novel_system.services.style_reference.exemplar_index import (
            build_exemplar_window_index,
        )
        from novel_system.services.style_reference.injection import (
            _WindowAffinityScorer,
            exemplar_index_window_config,
        )

        scorer = _WindowAffinityScorer(voice_signature, dict(metrics_baseline or {}))
        index = build_exemplar_window_index(
            paragraphs,
            scorer=scorer,
            scene_breaks=[int(i) for i in scene_breaks if isinstance(i, int)] if isinstance(scene_breaks, list) else None,
            **exemplar_index_window_config(),
        )
        return index if index.get("windows") else None
    except Exception:  # noqa: BLE001 — 确定性派生失败只让画像缺键
        logger.warning("exemplar window index computation failed", exc_info=True)
        return None


def _compute_structure_card_block(
    paragraphs: list[Any],
    book_stats: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """2026-09-12 结构跟随:从段落表 + 导入期声音签名确定性算结构画像;失败 → None(缺键)。"""

    try:
        voice_signature = (
            book_stats.get("voice_signature") if isinstance(book_stats, Mapping) else None
        )
        raw_breaks = book_stats.get("scene_breaks") if isinstance(book_stats, Mapping) else None
        card = compute_structure_card(
            paragraphs,
            voice_signature=voice_signature if isinstance(voice_signature, Mapping) else None,
            # 2026-09-14(WP5):导入期记录的场界 → 每章场数 / 场长
            scene_breaks=[int(i) for i in raw_breaks if isinstance(i, int)] if isinstance(raw_breaks, list) else None,
        )
        # 与 voice_signature 同理:profile_json 是 JSON 列,先规整成纯 JSON 值。
        return json.loads(json.dumps(card, ensure_ascii=False, default=_json_scalar_fallback))
    except Exception:  # noqa: BLE001
        logger.warning(
            "structure card computation failed; profile omits structure_card", exc_info=True
        )
        return None


def _derive_planning_guidance_block(
    findings: list[Any],
    corpus_texts: list[str] | CorpusOverlapIndex,
) -> list[str] | None:
    """2026-09-12 结构跟随:scene.* / theme.* 观察陈述 → ≤10 行规划层指引;失败 → None(缺键)。"""

    try:
        return derive_planning_guidance(
            findings,
            overlap_filter=lambda text: _contains_source_overlap(text, corpus_texts),
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "planning guidance derivation failed; profile omits planning_guidance", exc_info=True
        )
        return None


def _prune_metrics_for_prompt(metrics: dict[str, Any]) -> dict[str, dict[str, float]]:
    """只取每项 metric 的 mean / std(去掉 sample_count 等),控制 prompt 体积。"""
    pruned: dict[str, dict[str, float]] = {}
    for name, val in (metrics or {}).items():
        if isinstance(val, dict):
            pruned[name] = {
                "mean": float(val.get("mean", 0.0)),
                "std": float(val.get("std", 0.0)),
            }
    return pruned
