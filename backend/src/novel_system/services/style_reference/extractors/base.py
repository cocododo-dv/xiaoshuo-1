"""BaseExtractor — 一层(language / narrative / scene / theme)的抽象抽取器。

§6.6 两级重试 + §6.5 双 finding_kind + §6.7 校验装饰器(BannedAdjective /
EvidenceSpan)+ metrics_anchor 注入。

子类只需声明 layer / sub_dimensions / extract_node_id;run_orchestrator 负责调度。
LLM 调用经共享 helper 进入 execute_accounted_call，不依赖 LLMTaskRunner。
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from novel_system.services.llm_accounting import LLMCallContext
from novel_system.services.style_reference._llm_helper import LLMNodeError, call_llm_node
from novel_system.services.style_reference.banned_adjective import assert_no_banned_adjective
from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.dimensions import (
    LAYER_TO_SUB_DIMS,
    Layer,
    SubDimension,
)
from novel_system.services.style_reference.errors import (
    BannedAdjectiveError,
    EvidenceShortError,
    StyleReferenceError,
)
from novel_system.services.style_reference.evidence import (
    align_evidence_to_paragraph_lookup,
)
from novel_system.services.style_reference.metrics import (
    METRIC_NAMES,
    MetricsEngine,
    ParagraphRecord,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.sampling import (
    derive_extraction_rng,
    group_consecutive_windows,
    proportional_stratified_sample,
    sample_windows,
    scale_sample_target,
    window_position,
)
from novel_system.services.style_reference.schemas import (
    AnchorKind,
    ExtractionEvidenceInput,
    ExtractionFindingInput,
    ExtractionOutput,
    ExtractionPurpose,
    FindingKind,
    SupplementEvidenceOutput,
)
from novel_system.services.style_reference.text_utils import compact_ws
from novel_system.services.style_reference.untrusted_data import UntrustedPayload

if TYPE_CHECKING:
    from novel_system.db.models import (
        StyleReferenceFinding,
        StyleReferenceParagraph,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ExtractionRetryPolicy:
    """两级重试控制(§6.6)。"""

    max_targeted_retries: int = 2
    max_targeted_findings_per_batch: int = 2
    max_full_retries: int = 1


@dataclass
class ExtractionRunResult:
    """单 sub_dim 抽取的最终结果。"""

    sub_dimension: SubDimension
    findings: list["StyleReferenceFinding"] = field(default_factory=list)
    extractions_created: int = 0  # 关联的 extraction 行数(初次 + 重试)


class _ExtractLLMError(StyleReferenceError):
    """LLM 调用 / 解析失败的内部信号。"""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------------------
# 采样默认值(extraction.yaml `observations.*` 缺键时的兜底)
# ---------------------------------------------------------------------------


_DEFAULT_SAMPLE_SCALING: tuple[dict[str, Any], ...] = (
    {"min_chars": 200_000, "multiplier": 2.0},
    {"min_chars": 50_000, "multiplier": 1.5},
)
_DEFAULT_WINDOW_SIZE_RANGE: tuple[int, int] = (3, 6)
_DEFAULT_WINDOW_LAYERS: tuple[str, ...] = (Layer.NARRATIVE.value, Layer.THEME.value)


# ---------------------------------------------------------------------------
# 每 sub_dim 关心的 metric 子集 — 用于 metrics_anchor 注入(避免 prompt 过大)
# ---------------------------------------------------------------------------


_SUB_DIM_METRIC_SUBSET: dict[SubDimension, tuple[str, ...]] = {
    SubDimension.LANGUAGE_SENTENCE_STRUCTURE: (
        "avg_sentence_length",
        "sentence_length_std",
        "short_sentence_ratio",
        "long_sentence_ratio",
    ),
    SubDimension.LANGUAGE_VOCABULARY: (
        "classical_word_ratio",
        "colloquial_marker_ratio",
    ),
    SubDimension.LANGUAGE_RHETORIC: (
        "metaphor_density_per_1k",
        "personification_density_per_1k",
    ),
    SubDimension.LANGUAGE_PUNCTUATION: (
        "punctuation_density_per_1k",
        "dash_em_density_per_1k",
        "ellipsis_density_per_1k",
        "semicolon_density_per_1k",
        "question_density_per_1k",
    ),
    SubDimension.NARRATIVE_PERSPECTIVE: (
        "dialogue_ratio",
        "psychology_ratio",
        "narration_ratio",
    ),
    SubDimension.NARRATIVE_PACING: (
        "dialogue_ratio",
        "action_ratio",
        "narration_ratio",
        "transition_ratio",
    ),
    SubDimension.NARRATIVE_TIME_HANDLING: (
        "transition_ratio",
        "flashback_ratio",
    ),
    # PR-6:scene 层 metric 锚(强 — 五感/对话/描写比例)
    SubDimension.SCENE_ENVIRONMENT: (
        "description_env_ratio",
        "sensory_visual_per_1k",
        "sensory_olfactory_per_1k",
        "sensory_tactile_per_1k",
    ),
    SubDimension.SCENE_CHARACTER_PORTRAYAL: (
        "description_char_ratio",
        "sensory_visual_per_1k",
        "metaphor_density_per_1k",
    ),
    SubDimension.SCENE_DIALOGUE: (
        "dialogue_ratio",
        "question_density_per_1k",
        "colloquial_marker_ratio",
        "avg_sentence_length",
    ),
    SubDimension.SCENE_SENSORY_PRIORITY: (
        "sensory_visual_per_1k",
        "sensory_auditory_per_1k",
        "sensory_olfactory_per_1k",
        "sensory_tactile_per_1k",
        "sensory_gustatory_per_1k",
    ),
    # PR-6:theme 层 metric 锚(较弱 — 主题抽象,fallback 到语言/叙事密度类)
    SubDimension.THEME_EMOTIONAL_TONE: (
        "punctuation_density_per_1k",
        "ellipsis_density_per_1k",
        "colloquial_marker_ratio",
        "metaphor_density_per_1k",
    ),
    SubDimension.THEME_VALUES: (
        "narration_ratio",
        "psychology_ratio",
        "classical_word_ratio",
    ),
    SubDimension.THEME_MOTIFS: (
        "metaphor_density_per_1k",
        "personification_density_per_1k",
        "sensory_visual_per_1k",
    ),
    SubDimension.THEME_NARRATIVE_PHILOSOPHY: (
        "narration_ratio",
        "avg_sentence_length",
        "classical_word_ratio",
    ),
    SubDimension.NARRATIVE_INFORMATION_DENSITY: (
        "avg_sentence_length",
        "description_env_ratio",
        "description_char_ratio",
    ),
}


# ---------------------------------------------------------------------------
# BaseExtractor
# ---------------------------------------------------------------------------


class BaseExtractor:
    """抽象抽取器。子类必须声明 layer + extract_node_id。"""

    layer: Layer
    extract_node_id: str
    supplement_node_id: str = "style_ref_supplement_evidence"

    def __init__(
        self,
        session: Session,
        llm_client: Any,
        *,
        run_id: str,
        book_id: str,
        metrics_engine: MetricsEngine | None = None,
        retry_policy: ExtractionRetryPolicy | None = None,
        rng: random.Random | None = None,
        checkpoint: Any | None = None,
    ) -> None:
        self.session = session
        self.llm_client = llm_client
        self.run_id = run_id
        self.book_id = book_id
        self.repo = StyleReferenceRepository(session)
        self.metrics_engine = metrics_engine or MetricsEngine()
        self.retry_policy = retry_policy or ExtractionRetryPolicy()
        self._config = load_yaml_config("extraction")
        self._ptype_by_pid: dict[str, str] | None = None
        self._book: Any | None = None
        self._book_loaded = False
        self._book_metrics: dict[str, dict[str, Any]] | None = None
        # rng 默认以 sha256(text_checksum + run_id) 定种:同一 run 的采样可复现
        # (resume 重放采样即回到同一位置);测试可显式注入 rng。
        self.rng = rng if rng is not None else derive_extraction_rng(
            getattr(self._get_book(), "text_checksum", None), run_id
        )
        # 后台模式的事务边界:每个 sub_dim 落库后调用(session.commit),否则
        # 一层 4 个 sub_dim 的写事务会跨着多次分钟级 LLM 调用持有 SQLite 写锁,
        # 期间任何 UI 写操作等满 busy_timeout 后报 "database is busy"。
        self._checkpoint = checkpoint

    @property
    def sub_dimensions(self) -> list[SubDimension]:
        return LAYER_TO_SUB_DIMS[self.layer]

    # ------------------------------------------------------------------ public

    def extract_all_sub_dimensions(
        self,
        *,
        skip_sub_dimensions: set[SubDimension] | None = None,
    ) -> list[ExtractionRunResult]:
        """对 layer 下 4 个 sub_dim 各跑一遍 extract_with_retry。"""
        results: list[ExtractionRunResult] = []
        skipped = skip_sub_dimensions or set()
        for sub_dim in self.sub_dimensions:
            if sub_dim in skipped:
                # 恢复中断 run 时仍重放一次确定性采样，以推进共享 RNG 到与
                # 未中断执行相同的位置；不发 LLM、不重复落库。
                self._sample_paragraphs(sub_dim)
                continue
            result = self._extract_with_retry(sub_dim)
            results.append(result)
            if self._checkpoint is not None:
                self._checkpoint()
        return results

    # ------------------------------------------------------------------ retry

    def _extract_with_retry(self, sub_dim: SubDimension) -> ExtractionRunResult:
        result = ExtractionRunResult(sub_dimension=sub_dim)

        paragraphs = self._sample_paragraphs(sub_dim)
        metrics_anchor = self._build_metrics_anchor(paragraphs, sub_dim)
        paragraph_lookup = {p.paragraph_id: p.text for p in paragraphs}

        # Step 1: 初次抽取
        try:
            findings = self._extract_once(
                sub_dim,
                paragraphs,
                metrics_anchor,
                paragraph_lookup,
                purpose=ExtractionPurpose.EXTRACT,
            )
            failed = []
        except _PartialResult as partial:
            findings = partial.findings
            failed = partial.failed
        except _ExtractLLMError as exc:
            # 单个 sub_dim 的供应商截断/空正文不应拖垮整本 Profile。把它视为
            # 一次“空的初抽”，复用下方受 max_full_retries 限制的整维重试；
            # 重试仍失败时落一个 0 finding 终态，覆盖报告会如实显示缺口。
            logger.warning(
                "Initial extraction failed for %s; retrying whole sub-dimension: %s",
                sub_dim.value,
                exc,
            )
            findings = []
            failed = []
        result.extractions_created += 1

        if not failed:
            if not findings and self.retry_policy.max_full_retries > 0:
                # 空 observations/forbidden_patterns 是合法 schema 输出,不会进
                # 任何既有重试路径 —— 弱模型"产出薄"会让该 sub_dim 静默落空且
                # 永不补抽。按 full_retry 预算原 payload 重抽一次。
                logger.warning(
                    "Empty extraction for %s, retrying once", sub_dim.value
                )
                try:
                    findings = self._extract_once(
                        sub_dim,
                        paragraphs,
                        metrics_anchor,
                        paragraph_lookup,
                        purpose=ExtractionPurpose.FULL_RETRY,
                    )
                except _PartialResult as partial:
                    findings = [f for f in partial.findings if len(f.evidence) >= 2]
                except _ExtractLLMError as exc:
                    logger.warning("empty-result full retry failed: %s", exc)
                    findings = []
                result.extractions_created += 1
                self._persist_findings(sub_dim, findings, ExtractionPurpose.FULL_RETRY)
                result.findings = findings
                return result
            self._persist_findings(sub_dim, findings, ExtractionPurpose.EXTRACT)
            result.findings = findings
            return result

        # Step 2: 第一级 — 单 obs 定向补抽
        promoted: list[ExtractionFindingInput] = []
        targeted_retry_rounds = self.retry_policy.max_targeted_retries
        if len(failed) > self.retry_policy.max_targeted_findings_per_batch:
            logger.warning(
                "Skipping per-finding supplements for %s: %d failed findings exceed batch limit %d",
                sub_dim.value,
                len(failed),
                self.retry_policy.max_targeted_findings_per_batch,
            )
            targeted_retry_rounds = 0
        for _ in range(targeted_retry_rounds):
            still_failed: list[ExtractionFindingInput] = []
            for finding in list(failed):
                try:
                    extras = self._supplement_evidence_for(
                        finding, sub_dim, paragraphs, paragraph_lookup
                    )
                except _ExtractLLMError as exc:
                    logger.warning("supplement_evidence failed: %s", exc)
                    still_failed.append(finding)
                    continue
                # 2026-07 勘误:shim 的原始 evidence(首轮 LLM 产出)因 Pydantic 在
                # ≥2 条校验处提前失败,从未过 span 校验——伪造引文可借补证晋升入库
                # (再经 few-shot 注入)。合并前按与补证同一套规则过滤原始条目。
                original_valid = _span_valid_evidence(
                    finding.evidence, paragraph_lookup
                )
                merged = _span_valid_evidence(
                    original_valid + list(extras), paragraph_lookup
                )
                if len(merged) >= 2:
                    finding.evidence = merged
                    promoted.append(finding)
                else:
                    still_failed.append(finding)
            failed = still_failed
            if not failed:
                break

        if promoted:
            # 升级后的 finding 进入 findings(成功列表),供 persist 使用
            findings = list(findings) + promoted

        if not failed:
            # 第一级重试通过:写一个 SUPPLEMENT_EVIDENCE 占位 extraction + 落 findings
            self._record_purpose_extraction(sub_dim, ExtractionPurpose.SUPPLEMENT_EVIDENCE)
            result.extractions_created += 1
            self._persist_findings(sub_dim, findings, ExtractionPurpose.SUPPLEMENT_EVIDENCE)
            result.findings = findings
            return result

        # Step 3: 第二级 — 整 sub_dim 重抽
        if self.retry_policy.max_full_retries > 0:
            try:
                retry_findings = self._extract_once(
                    sub_dim,
                    paragraphs,
                    metrics_anchor,
                    paragraph_lookup,
                    purpose=ExtractionPurpose.FULL_RETRY,
                )
                self._persist_findings(sub_dim, retry_findings, ExtractionPurpose.FULL_RETRY)
                result.extractions_created += 1
                result.findings = retry_findings
                return result
            except _PartialResult as partial:
                retry_findings = partial.findings
                # full_retry 后只保留 ≥2 evidence 的
                kept = [f for f in retry_findings if len(f.evidence) >= 2]
                dropped = len(retry_findings) - len(kept)
                if dropped:
                    logger.warning(
                        "Dropped %d findings after full_retry for %s",
                        dropped,
                        sub_dim.value,
                    )
                self._persist_findings(sub_dim, kept, ExtractionPurpose.FULL_RETRY)
                result.extractions_created += 1
                result.findings = kept
                return result
            except _ExtractLLMError as exc:
                # 重抽本身失败(供应商截断 / 空正文 / 网络):保留首抽 + 定向补证
                # 已通过校验的 findings,而不是让整个 run FAILED。
                kept = [f for f in findings if len(f.evidence) >= 2]
                logger.warning(
                    "full_retry LLM call failed for %s; keeping %d validated findings "
                    "from the initial extraction and dropping %d unresolved: %s",
                    sub_dim.value,
                    len(kept),
                    len(failed),
                    exc,
                )
                self._persist_findings(sub_dim, kept, ExtractionPurpose.EXTRACT)
                result.extractions_created += 1
                result.findings = kept
                return result

        # Step 4: 全部失败,丢弃失效 + warning
        kept = [f for f in findings if len(f.evidence) >= 2]
        logger.warning(
            "Dropped %d findings after exhausting retries for %s",
            len(failed),
            sub_dim.value,
        )
        self._persist_findings(sub_dim, kept, ExtractionPurpose.EXTRACT)
        result.findings = kept
        return result

    # ------------------------------------------------------------------ once

    def _extract_once(
        self,
        sub_dim: SubDimension,
        paragraphs: list["StyleReferenceParagraph"],
        metrics_anchor: dict[str, Any],
        paragraph_lookup: dict[str, str],
        *,
        purpose: ExtractionPurpose,
    ) -> list[ExtractionFindingInput]:
        """一次 LLM 抽取调用 + Pydantic 校验 + banned_adjective + evidence span 校验。

        返回 list[ExtractionFindingInput](observations + forbidden_patterns 合并)。
        若部分 finding evidence<2 通过 _PartialResult 异常带出 failed list,让重试机制处理。
        """
        payload = {
            "sub_dimension": sub_dim.value,
            "metrics_anchor": metrics_anchor,
            "paragraphs": self._paragraph_payload_items(paragraphs),
        }
        try:
            structured = self._call_llm(self.extract_node_id, payload)
        except _ExtractLLMError as exc:
            logger.warning("extract LLM failed: %s", exc)
            raise

        findings, failed = self._parse_extraction_response(
            structured, sub_dim, paragraph_lookup
        )
        if failed:
            raise _PartialResult(findings=findings, failed=failed)
        return findings

    def _parse_extraction_response(
        self,
        structured: dict[str, Any],
        sub_dim: SubDimension,
        paragraph_lookup: dict[str, str],
    ) -> tuple[list[ExtractionFindingInput], list[ExtractionFindingInput]]:
        """把 LLM structured_output 转 list[ExtractionFindingInput]。

        校验链:Pydantic ≥2 evidence(model_validator) + BannedAdjective + EvidenceSpan。
        EvidenceShortError 触发的 finding 被收入 `failed` 列表;BannedAdjective /
        EvidenceSpan 触发的 finding 直接丢弃(不可补救)。
        """
        # 校准后的数量上限(ExtractionOutput 契约 obs ≤6 / fp ≤2):此前只写在
        # schema 注释里从未执行,LLM 超发时全部入库。超出部分截断。
        observations = (structured.get("observations") or [])[:6]
        forbidden = (structured.get("forbidden_patterns") or [])[:2]

        findings: list[ExtractionFindingInput] = []
        failed: list[ExtractionFindingInput] = []

        for item, kind in [
            *((it, FindingKind.OBSERVATION) for it in observations),
            *((it, FindingKind.FORBIDDEN_PATTERN) for it in forbidden),
        ]:
            if not isinstance(item, dict):
                continue
            item = _normalize_finding_item(dict(item))
            raw_evidence = item.get("evidence")
            raw_evidence_count = (
                len(raw_evidence) if isinstance(raw_evidence, list) else 0
            )
            item["evidence"] = _salvage_parseable_evidence(raw_evidence)
            if len(item["evidence"]) < raw_evidence_count:
                logger.warning(
                    "Dropped %d structurally invalid evidence items before finding validation",
                    raw_evidence_count - len(item["evidence"]),
                )
            item.setdefault("finding_kind", kind.value)
            item.setdefault("sub_dimension", sub_dim.value)

            # 先 banned_adjective(快路径)
            statement = str(item.get("statement", "")).strip()
            if not statement:
                continue
            try:
                assert_no_banned_adjective(statement)
            except BannedAdjectiveError as exc:
                logger.warning("BannedAdjective hit, dropping finding: %s", exc.matched)
                continue

            try:
                finding = ExtractionFindingInput.model_validate(item)
            except ValidationError as exc:
                # 检查是否是 EvidenceShortError(允许重试)
                if _is_evidence_short_error(exc):
                    short = _build_short_finding(item, sub_dim, kind)
                    failed.append(short)
                else:
                    logger.warning("ExtractionFinding validate failed (non-retry): %s", exc)
                continue
            except EvidenceShortError:
                short = _build_short_finding(item, sub_dim, kind)
                failed.append(short)
                continue

            # 引文逐条对齐，而不是“一条坏引文拖垮整条 finding”。保留合法、
            # 去重后的证据；不足 2 条时进入既有定向补证链。伪造/拼接引文本身
            # 仍然 fail-closed，绝不会落库。
            original_count = len(finding.evidence)
            valid_evidence = _span_valid_evidence(
                finding.evidence, paragraph_lookup
            )
            finding.evidence = valid_evidence
            if len(valid_evidence) < 2:
                logger.warning(
                    "Evidence alignment kept %d/%d items; supplementing finding: %s",
                    len(valid_evidence),
                    original_count,
                    statement[:80],
                )
                failed.append(finding)
                continue

            findings.append(finding)

        return findings, failed

    def _supplement_evidence_for(
        self,
        finding: ExtractionFindingInput,
        sub_dim: SubDimension,
        paragraphs: list["StyleReferenceParagraph"],
        paragraph_lookup: dict[str, str],
    ) -> list[ExtractionEvidenceInput]:
        """对单条 finding 调 supplement_evidence LLM 节点。返回新 evidence 列表(可能空)。"""
        payload = {
            "finding_statement": finding.statement,
            "finding_kind": finding.finding_kind.value,
            "sub_dimension": sub_dim.value,
            "existing_evidence_count": len(finding.evidence),
            "paragraphs": [
                {
                    "paragraph_id": p.paragraph_id,
                    "text": compact_ws(p.text)[:600],
                }
                for p in paragraphs
            ],
        }
        structured = self._call_llm(self.supplement_node_id, payload)
        try:
            parsed = SupplementEvidenceOutput.model_validate(structured or {})
        except ValidationError as exc:
            logger.warning("supplement_evidence parse failed: %s", exc)
            return []
        validated = _span_valid_evidence(parsed.additional_evidence, paragraph_lookup)
        # 调用方应记录 SUPPLEMENT_EVIDENCE purpose
        self._record_purpose_extraction(sub_dim, ExtractionPurpose.SUPPLEMENT_EVIDENCE)
        return validated

    # ------------------------------------------------------------------ LLM

    def _call_llm(self, node_id: str, payload: dict) -> dict[str, Any]:
        # PR-8 §"_call_llm 统一" — 复用 _llm_helper.call_llm_node,统一 LLM 调用入口
        try:
            return call_llm_node(
                node_id,
                UntrustedPayload(payload),
                self.llm_client,
                session=self.session,
                context=LLMCallContext(
                    scope_type="style_reference_book",
                    scope_id=self.book_id,
                    node_id=node_id,
                    step=f"extraction:{self.run_id}",
                ),
            )
        except LLMNodeError as exc:
            raise _ExtractLLMError(str(exc)) from exc

    # --------------------------------------------------------------- sampling

    def _sample_paragraphs(self, sub_dim: SubDimension) -> list["StyleReferenceParagraph"]:
        """为一个 sub_dim 采样段落(返回按 paragraph_index 排序的扁平列表)。

        - 样本量 = 层基准值 × 全书字数分档(`scale_sample_target`);
        - language / scene:按段型真实分布比例分配名额(每型下限 1);
        - narrative / theme:抽 3–6 个相邻段的连续窗口,窗口边界由
          `group_consecutive_windows` 从扁平列表还原(payload 带 window_id / window_position)。
        """
        observations_cfg = self._config.get("observations", {}) or {}
        samples_map = observations_cfg.get("samples_per_sub_dimension", {}) or {}
        layer_name = self.layer.value
        base_n = int(samples_map.get(layer_name, 20))
        target_n = scale_sample_target(
            base_n,
            getattr(self._get_book(), "total_chars", 0),
            scaling=observations_cfg.get("sample_scaling") or list(_DEFAULT_SAMPLE_SCALING),
            max_n=int(observations_cfg.get("max_samples_per_sub_dimension", 60) or 60),
        )
        min_per_type = int(observations_cfg.get("min_samples_per_type", 1))

        paragraphs = self.repo.list_paragraphs(self.book_id)
        # extraction 域禁用词(用户在任一 profile 上登记):含此词的段落不进抽取
        # 样本池——典型用途是把源书专名/标志性意象隔离在 finding/quote 之外
        banned = [
            t.term
            for t in self.repo.list_banned_terms_for_book(self.book_id, scope="extraction")
            if (t.term or "").strip()
        ]
        if banned:
            paragraphs = [
                p for p in paragraphs
                if not any(term in (p.text or "") for term in banned)
            ]
        if self._uses_windows():
            size_range = observations_cfg.get("window_size_range") or list(_DEFAULT_WINDOW_SIZE_RANGE)
            windows = sample_windows(
                paragraphs,
                target_n,
                (int(size_range[0]), int(size_range[1])),
                self.rng,
            )
            return [p for window in windows for p in window]
        return proportional_stratified_sample(
            paragraphs,
            target_n=target_n,
            min_per_type=min_per_type,
            rng=self.rng,
        )

    def _uses_windows(self) -> bool:
        observations_cfg = self._config.get("observations", {}) or {}
        window_layers = observations_cfg.get("window_layers")
        if window_layers is None:
            window_layers = list(_DEFAULT_WINDOW_LAYERS)
        return self.layer.value in {str(v) for v in window_layers}

    def _paragraph_payload_items(
        self,
        paragraphs: list["StyleReferenceParagraph"],
    ) -> list[dict[str, Any]]:
        """抽取 payload 的段落条目:始终带 paragraph_index;窗口层再带
        window_id / window_position(first / middle / last),让 LLM 看到顺序与窗口边界。"""
        items: list[dict[str, Any]] = []
        if self._uses_windows():
            for w_idx, window in enumerate(group_consecutive_windows(paragraphs), start=1):
                window_id = f"w{w_idx:02d}"
                for pos, p in enumerate(window):
                    items.append(
                        {
                            "paragraph_id": p.paragraph_id,
                            "paragraph_index": int(getattr(p, "paragraph_index", 0) or 0),
                            "paragraph_type": p.paragraph_type,
                            "window_id": window_id,
                            "window_position": window_position(pos, len(window)),
                            "text": compact_ws(p.text)[:600],
                        }
                    )
            return items
        for p in paragraphs:
            items.append(
                {
                    "paragraph_id": p.paragraph_id,
                    "paragraph_index": int(getattr(p, "paragraph_index", 0) or 0),
                    "paragraph_type": p.paragraph_type,
                    "text": compact_ws(p.text)[:600],
                }
            )
        return items

    def _get_book(self) -> Any | None:
        if not self._book_loaded:
            self._book = self.repo.get_book(self.book_id)
            self._book_loaded = True
        return self._book

    # ---------------------------------------------------------- metrics_anchor

    def _build_metrics_anchor(
        self,
        paragraphs: list["StyleReferenceParagraph"],
        sub_dim: SubDimension,
    ) -> dict[str, Any]:
        """注入当前 sub_dim 关心的硬指标子集(≤8 项,避免 prompt 过大)。

        锚点取 **全书真值**:ingest 已把 `MetricsEngine.compute_with_variance` /
        `compute_prose_shape_with_variance` 的结果写进 `book.stats_json.metrics` /
        `prose_shape_metrics`;这里按 `_SUB_DIM_METRIC_SUBSET` 取子集并标
        ``anchor_scope="book"``。全书值缺失(旧书 / 测试直接建表)时回退到对
        当前样本计算,标 ``anchor_scope="sample"``。
        """
        subset_names = _SUB_DIM_METRIC_SUBSET.get(sub_dim, tuple(METRIC_NAMES[:6]))
        book_metrics = self._book_level_metrics()
        anchor: dict[str, Any] = {}
        if book_metrics and all(name in book_metrics for name in subset_names):
            for name in subset_names:
                entry = book_metrics[name]
                anchor[name] = {
                    "mean": float(entry.get("mean", 0.0) or 0.0),
                    "std": float(entry.get("std", 0.0) or 0.0),
                }
            anchor["anchor_scope"] = "book"
            return anchor
        records = [
            ParagraphRecord(text=p.text, paragraph_type=p.paragraph_type)
            for p in paragraphs
        ]
        all_metrics = self.metrics_engine.compute_with_variance(records)
        for name in subset_names:
            if name in all_metrics:
                anchor[name] = {
                    "mean": float(all_metrics[name][0]),
                    "std": float(all_metrics[name][1]),
                }
        anchor["anchor_scope"] = "sample"
        return anchor

    def _book_level_metrics(self) -> dict[str, dict[str, Any]]:
        """`book.stats_json.metrics` ∪ `prose_shape_metrics`(每项 {mean, std, ...}),lazy 读一次。"""
        if self._book_metrics is None:
            stats = getattr(self._get_book(), "stats_json", None) or {}
            merged: dict[str, dict[str, Any]] = {}
            for key in ("metrics", "prose_shape_metrics"):
                block = stats.get(key) if isinstance(stats, dict) else None
                if not isinstance(block, dict):
                    continue
                for name, entry in block.items():
                    if isinstance(entry, dict) and "mean" in entry:
                        merged[str(name)] = entry
            self._book_metrics = merged
        return self._book_metrics

    # --------------------------------------------------------------- persist

    def _persist_findings(
        self,
        sub_dim: SubDimension,
        findings: list[ExtractionFindingInput],
        purpose: ExtractionPurpose,
    ) -> None:
        """落 extraction + finding + quote + evidence 4 表。"""
        extraction_id = f"sr_ext_{uuid.uuid4().hex[:12]}"
        self.repo.create_extraction(
            extraction_id=extraction_id,
            book_id=self.book_id,
            run_id=self.run_id,
            layer=self.layer.value,
            sub_dimension=sub_dim.value,
            llm_call_id=None,
            raw_payload_json={"findings_count": len(findings)},
            status="done",
            validation_errors_json=[],
            purpose=purpose.value,
        )
        for f_idx, finding in enumerate(findings):
            finding_id = f"sr_find_{uuid.uuid4().hex[:12]}"
            # 创建 finding
            self.repo.create_finding(
                finding_id=finding_id,
                book_id=self.book_id,
                run_id=self.run_id,
                extraction_id=extraction_id,
                sub_dimension=sub_dim.value,
                finding_kind=finding.finding_kind.value,
                statement=finding.statement,
                confidence=finding.confidence.value,
                status="pending",
            )
            # 落 quotes 与 evidence
            for ev_idx, evidence in enumerate(finding.evidence):
                quote_id = f"sr_quote_{uuid.uuid4().hex[:12]}"
                span_start = evidence.span[0] if evidence.span else 0
                span_end = evidence.span[1] if evidence.span else len(evidence.quote)
                # 冗余段落类型:scene_samples_index / few-shot 按段型分桶时不再
                # 回退默认 narration(合成侧还有段落表兜底,双保险)
                features: dict[str, Any] = {"anchor_kind": evidence.anchor_kind.value}
                ptype = self._paragraph_type_of(evidence.paragraph_id)
                if ptype:
                    features["paragraph_type"] = ptype
                self.repo.create_quote(
                    quote_id=quote_id,
                    book_id=self.book_id,
                    paragraph_id=evidence.paragraph_id,
                    span_start=span_start,
                    span_end=span_end,
                    quote_text=evidence.quote,
                    illustrates_dims=list(evidence.illustrates_dims),
                    extracted_features=features,
                )
                self.repo.create_evidence(
                    evidence_id=f"sr_ev_{uuid.uuid4().hex[:12]}",
                    finding_id=finding_id,
                    quote_id=quote_id,
                    anchor_kind=evidence.anchor_kind.value,
                    is_synthetic=int(evidence.is_synthetic),
                )

    def _paragraph_type_of(self, paragraph_id: str | None) -> str | None:
        """段落类型 lazy 全书查表(每 extractor 实例最多查一次)。"""
        if not paragraph_id:
            return None
        if self._ptype_by_pid is None:
            self._ptype_by_pid = {
                p.paragraph_id: p.paragraph_type
                for p in self.repo.list_paragraphs(self.book_id)
            }
        return self._ptype_by_pid.get(paragraph_id)

    def _record_purpose_extraction(self, sub_dim: SubDimension, purpose: ExtractionPurpose) -> None:
        """在两级重试中,即使没有新 finding 也需要落一个 extraction 行表明 purpose 调用过。

        但避免在 _persist_findings 已经创建 extraction 行时重复;这里只做"占位"
        extraction:findings_count=0 + status=done。
        """
        extraction_id = f"sr_ext_{uuid.uuid4().hex[:12]}"
        self.repo.create_extraction(
            extraction_id=extraction_id,
            book_id=self.book_id,
            run_id=self.run_id,
            layer=self.layer.value,
            sub_dimension=sub_dim.value,
            llm_call_id=None,
            raw_payload_json={"purpose_only": True},
            status="done",
            validation_errors_json=[],
            purpose=purpose.value,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _PartialResult(StyleReferenceError):
    """携带部分成功 findings 与部分失败(evidence<2)的内部异常。"""

    def __init__(
        self,
        *,
        findings: list[ExtractionFindingInput],
        failed: list[ExtractionFindingInput],
    ) -> None:
        super().__init__(f"partial:ok={len(findings)},failed={len(failed)}")
        self.findings = findings
        self.failed = failed


_FINDING_KEYS = {"statement", "confidence", "finding_kind", "evidence", "sub_dimension"}
_STATEMENT_ALIASES = ("description", "observation", "pattern", "summary")
_EVIDENCE_KEYS = {
    "paragraph_id", "span", "quote", "illustrates_dims",
    "anchor_kind", "note", "is_synthetic",
}


def _normalize_finding_item(item: dict) -> dict:
    """结构化输出降级(schema 不上线)时对近似输出做容错归一。

    实测中转模型在裸 json_object 模式下会把 statement 写成 description、
    span 写成原文字符串——不归一会在 Pydantic(extra=forbid)全灭。只做键级
    纠偏,内容校验(引文原文、≥2 证据、禁用形容词)照旧。
    """
    if "statement" not in item:
        for alias in _STATEMENT_ALIASES:
            value = item.get(alias)
            if isinstance(value, str) and value.strip():
                item["statement"] = value
                break
    evidence = item.get("evidence")
    if isinstance(evidence, list):
        for ev in evidence:
            if not isinstance(ev, dict):
                continue
            span = ev.get("span")
            span_ok = (
                isinstance(span, (list, tuple))
                and len(span) == 2
                and all(isinstance(x, int) for x in span)
            )
            if span is not None and not span_ok:
                ev["span"] = None
            for key in list(ev.keys()):
                if key not in _EVIDENCE_KEYS:
                    ev.pop(key)
    for key in list(item.keys()):
        if key not in _FINDING_KEYS:
            item.pop(key)
    return item


def _salvage_parseable_evidence(raw: Any) -> list[dict[str, Any]]:
    """逐条过滤坏 evidence，避免一个空 quote 拖垮整条可补救 finding。

    这里只放宽容器/结构容错；通过后的条目仍会在 `_span_valid_evidence`
    中逐条核对原段落，伪造或拼接引文不会因此落库。
    """
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        candidates = []
    salvaged: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        normalized = dict(candidate)
        quote = normalized.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            continue
        normalized["quote"] = quote.strip()
        try:
            parsed = ExtractionEvidenceInput.model_validate(normalized)
        except ValidationError:
            continue
        salvaged.append(parsed.model_dump(mode="python"))
    return salvaged


def _span_valid_evidence(
    evidence_list: list[ExtractionEvidenceInput],
    paragraph_lookup: dict[str, str],
) -> list[ExtractionEvidenceInput]:
    """按 evidence span 规则过滤:paragraph_quote 锚必须 quote ∈ 对应段落文本;
    counter_example / author_avoidance(合成锚)直通;缺 paragraph_id 的引用锚丢弃。

    与 `_supplement_evidence_for` 的补证校验为同一套规则;两级重试的 shim 原始
    evidence 合并前也走这里(2026-07 勘误:此前原始条目未经校验即可晋升入库)。
    """
    validated: list[ExtractionEvidenceInput] = []
    seen: set[tuple[Any, ...]] = set()
    for ev in evidence_list:
        if ev.anchor_kind in (AnchorKind.COUNTER_EXAMPLE, AnchorKind.AUTHOR_AVOIDANCE):
            aligned = ev
        else:
            aligned = align_evidence_to_paragraph_lookup(ev, paragraph_lookup)
            if aligned is None:
                continue
        identity = (
            aligned.anchor_kind.value,
            aligned.paragraph_id,
            aligned.span,
            aligned.quote,
        )
        if identity in seen:
            continue
        seen.add(identity)
        validated.append(aligned)
    return validated


def _is_evidence_short_error(exc: ValidationError) -> bool:
    for err in exc.errors():
        ctx = err.get("ctx", {})
        if isinstance(ctx.get("error"), EvidenceShortError):
            return True
        if "EvidenceShortError" in str(err):
            return True
    return False


def _build_short_finding(
    item: dict, sub_dim: SubDimension, kind: FindingKind
) -> ExtractionFindingInput:
    """构造一个允许 evidence < 2 的 finding(供重试机制使用)。

    绕过 model_validator:直接用 __pydantic_validator__ 跳过验证不够干净;
    最简方案是构造一个新 BaseModel 派生类(无 validator)。这里简化为手工
    构造 dataclass-like 对象。
    """
    return _ShortFindingShim(
        statement=str(item.get("statement", "")),
        finding_kind=kind,
        sub_dimension=sub_dim.value,
        confidence_raw=str(item.get("confidence", "medium")),
        evidence_raw=list(item.get("evidence", []) or []),
    )


from novel_system.services.style_reference.schemas import ConfidenceLevel  # noqa: E402


@dataclass
class _ShortFindingShim:
    """evidence<2 的 finding 临时持有结构,只供重试链路使用,不参与 ExtractionFindingInput 校验。"""

    statement: str
    finding_kind: FindingKind
    sub_dimension: str
    confidence_raw: str = "medium"
    evidence_raw: list[Any] = field(default_factory=list)

    @property
    def confidence(self) -> ConfidenceLevel:
        try:
            return ConfidenceLevel(self.confidence_raw)
        except ValueError:
            return ConfidenceLevel.MEDIUM

    @property
    def evidence(self) -> list[ExtractionEvidenceInput]:
        out: list[ExtractionEvidenceInput] = []
        for ev in self.evidence_raw:
            if not isinstance(ev, dict):
                continue
            try:
                out.append(ExtractionEvidenceInput.model_validate(ev))
            except ValidationError:
                continue
        return out

    @evidence.setter
    def evidence(self, value: list[ExtractionEvidenceInput]) -> None:
        self.evidence_raw = [
            (
                v.model_dump()
                if isinstance(v, ExtractionEvidenceInput)
                else v
            )
            for v in value
        ]
