"""StyleReference 错误体系。

PR-1 落地完整错误层级,后续 PR 直接 raise。
设计依据:《风格参考模块重构执行手册 v1.1》§6.7 / §4.1。
"""

from __future__ import annotations

from typing import Any

from novel_system.services.errors import DomainError


class StyleReferenceError(Exception):
    """所有 style_reference 模块异常的基类。"""


class BannedAdjectiveError(StyleReferenceError):
    """抽取器产出的 finding.statement 命中禁用空泛形容词词表。"""

    def __init__(self, statement: str, matched: list[str]) -> None:
        super().__init__(
            f"finding.statement 命中空泛形容词 {matched!r}: {statement!r}",
        )
        self.statement = statement
        self.matched = list(matched)


class EvidenceShortError(StyleReferenceError):
    """抽取器产出的某条 finding 不满足 ≥2 evidence 强约束;触发两级重试机制。"""

    def __init__(self, finding_ref: str, evidence_count: int) -> None:
        super().__init__(
            f"finding {finding_ref!r} 仅有 {evidence_count} 条 evidence (要求 ≥2)",
        )
        self.finding_ref = finding_ref
        self.evidence_count = evidence_count


class EvidenceSpanError(StyleReferenceError):
    """evidence.quote 不在所引用 paragraph_id 对应文本的指定 span 内。"""

    def __init__(self, paragraph_id: str, span: tuple[int, int], quote_excerpt: str) -> None:
        super().__init__(
            f"paragraph={paragraph_id!r} span={span!r} 与 quote={quote_excerpt[:32]!r}... 不匹配",
        )
        self.paragraph_id = paragraph_id
        self.span = span
        self.quote_excerpt = quote_excerpt


class LegacyBackupMissingError(StyleReferenceError):
    """drop 旧 reference_learning 表前要求至少存在一个 backups/style_reference_legacy_*.json。"""

    def __init__(self, backup_dir: str) -> None:
        super().__init__(
            "drop 旧 reference_learning 表前必须存在 backups/style_reference_legacy_*.json。"
            f"未在 {backup_dir!r} 找到任何 JSON 备份;请先准备历史备份文件后再执行相关迁移。",
        )
        self.backup_dir = backup_dir


class DuplicateBookError(StyleReferenceError):
    """同 text_checksum 的 book 已存在(避免重复导入)。"""

    def __init__(self, book_id: str, checksum: str) -> None:
        super().__init__(
            f"book with checksum {checksum[:12]!r} already exists as {book_id!r}",
        )
        self.book_id = book_id
        self.checksum = checksum


class EmptyBookError(StyleReferenceError):
    """书籍文本在清洗 / 段落切分后为空。"""

    def __init__(self, stage: str) -> None:
        super().__init__(f"book text became empty after {stage}")
        self.stage = stage


class LLMRequiredError(StyleReferenceError, DomainError):
    """操作需要启用 LLM 但当前 NOVEL_SYSTEM_LLM_ENABLED=false。

    extractor / synthesize 等语义抽取操作必须有 LLM。同时继承 DomainError,
    使 API 层自动映射为 409 + author_action 引导(而非通用 500),前端可据此
    跳转 SystemConfig 启用 LLM provider。
    """

    def __init__(self, operation: str) -> None:
        DomainError.__init__(
            self,
            "STYLE_REFERENCE_LLM_REQUIRED",
            f"operation {operation!r} requires NOVEL_SYSTEM_LLM_ENABLED=true; "
            "see SystemConfig to enable LLM provider",
            status_code=409,
            details={
                "operation": operation,
                "author_action": {
                    "action": "enable_llm_provider_in_system_config",
                    "view": "systemConfig",
                    "label": "前往系统配置启用 LLM",
                },
            },
        )
        self.operation = operation
        self.next_action = "enable_llm_provider_in_system_config"


class ClassificationFailedError(StyleReferenceError, DomainError):
    """段落分类的 LLM 调用失败(2026-09-15 严格 LLM:不再降级到启发式)。

    502 + retryable + author_action:检查模型接入后重试;导入任务会把它记在书的
    ``stats_json.classification.error`` 上,作者在「参考书活动」里看到原因后可「继续分类」。
    """

    def __init__(self, *, code: str, message: str, book_id: str | None = None) -> None:
        DomainError.__init__(
            self,
            "STYLE_REFERENCE_CLASSIFICATION_FAILED",
            f"paragraph classification failed ({code}): {message}",
            status_code=502,
            details={
                "reason_code": code,
                "book_id": book_id,
                "retryable": True,
                "author_action": {
                    "action": "check_llm_provider_then_retry",
                    "view": "systemConfig",
                    "label": "段落分类的模型调用失败：检查模型接入后重试",
                },
            },
        )
        self.reason_code = code
        self.book_id = book_id


class CloudPolicyBlockedError(StyleReferenceError, DomainError):
    """书籍 cloud_policy=local_only 时禁止任何把书籍内容送往云端 LLM 的操作。

    与 LLMRequiredError 同理继承 DomainError,API 层映射 409 + author_action。
    """

    def __init__(
        self,
        *,
        book_id: str,
        operation: str,
        provider: str | None = None,
        base_url: str | None = None,
    ) -> None:
        # 2026-09-15 严格 LLM:「仅本机」的书没有启发式兜底了,它的段落只能交给本地模型;
        # 运行时模型是云端接入时拒绝,并告诉作者两条路(本地模型 / 换策略重导)。
        details: dict[str, Any] = {
            "book_id": book_id,
            "operation": operation,
            "cloud_policy": "local_only",
            "author_action": {
                "action": "configure_local_llm_or_change_cloud_policy",
                "view": "systemConfig",
                "label": "该参考书为「仅本机」策略：需要本地模型（如 Ollama）才能处理，或改用送云策略重新导入",
            },
        }
        if provider is not None:
            details["provider"] = provider
        if base_url is not None:
            details["base_url"] = base_url
        DomainError.__init__(
            self,
            "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED",
            f"book {book_id!r} has cloud_policy=local_only; "
            f"operation {operation!r} needs a local LLM and the configured provider is not local",
            status_code=409,
            details=details,
        )
        self.book_id = book_id
        self.operation = operation


class CloudSendRightsBlockedError(StyleReferenceError, DomainError):
    """非本地策略缺少严格、显式的云端发送权声明。"""

    def __init__(
        self,
        *,
        book_id: str,
        operation: str,
        cloud_policy: str,
    ) -> None:
        DomainError.__init__(
            self,
            "STYLE_REFERENCE_SEND_RIGHTS_REQUIRED",
            f"book {book_id!r} has cloud_policy={cloud_policy!r} but lacks an explicit "
            "declared=true and send_rights=true declaration; "
            f"operation {operation!r} would send book content to a cloud LLM and is blocked",
            status_code=409,
            details={
                "book_id": book_id,
                "operation": operation,
                "cloud_policy": cloud_policy,
                "author_action": {
                    "action": "redeclare_send_rights",
                    "view": "styleref",
                    "label": "请重新声明参考书的云端发送权",
                },
            },
        )
        self.book_id = book_id
        self.operation = operation
        self.cloud_policy = cloud_policy


class CloudPolicyInvalidError(StyleReferenceError, DomainError):
    """持久化 cloud_policy 不属于受支持策略时拒绝云端发送。"""

    def __init__(
        self,
        *,
        book_id: str,
        operation: str,
        cloud_policy: str,
    ) -> None:
        DomainError.__init__(
            self,
            "STYLE_REFERENCE_CLOUD_POLICY_INVALID",
            f"book {book_id!r} has unsupported cloud_policy={cloud_policy!r}; "
            f"operation {operation!r} would send book content to a cloud LLM and is blocked",
            status_code=409,
            details={
                "book_id": book_id,
                "operation": operation,
                "cloud_policy": cloud_policy,
                "author_action": {
                    "action": "review_cloud_policy",
                    "view": "styleref",
                    "label": "请检查并重新选择参考书的云端策略",
                },
            },
        )
        self.book_id = book_id
        self.operation = operation
        self.cloud_policy = cloud_policy


SYNTHESIZE_REASON_CODES: frozenset[str] = frozenset(
    {"budget_unfit", "empty_profile", "source_overlap", "text_integrity", "llm_failed"}
)
_SYNTHESIZE_AUTHOR_ACTIONS: dict[str, dict[str, str]] = {
    # 预算装不下：抽取结果本身太多/太长，回到维度矩阵审阅、驳回冗余 finding 再合成。
    "budget_unfit": {
        "action": "review_dimension_matrix_then_synthesize",
        "view": "styleref",
        "label": "抽取结果超出合成预算，请回到维度矩阵精简 finding 后重试合成",
    },
    # 其余四类都是单次 LLM 产出问题，重试合成即可。
    "empty_profile": {
        "action": "retry_synthesize",
        "view": "styleref",
        "label": "模型返回的画像为空或无效，请重试合成",
    },
    "source_overlap": {
        "action": "retry_synthesize",
        "view": "styleref",
        "label": "模型输出与参考原文重合被过滤，请重试合成",
    },
    "text_integrity": {
        "action": "retry_synthesize",
        "view": "styleref",
        "label": "模型输出含损坏字符，请重试合成",
    },
    "llm_failed": {
        "action": "retry_synthesize",
        "view": "styleref",
        "label": "画像合成调用失败，请检查 LLM 配置后重试合成",
    },
}


class SynthesizeError(StyleReferenceError, DomainError):
    """ProfileSynthesizer 失败。

    同时继承 DomainError:API 层自动映射为 409 + ``STYLE_REFERENCE_SYNTHESIZE_FAILED``,
    ``details.reason_code`` 取 :data:`SYNTHESIZE_REASON_CODES` 之一,并附 ``author_action``
    (回到维度矩阵 / 重试合成)。子类(如 ProfileTextIntegrityError)通过 ``reason_code``
    参数复用同一映射。
    """

    code = "STYLE_REFERENCE_SYNTHESIZE_FAILED"

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "llm_failed",
        details: dict | None = None,
    ) -> None:
        if reason_code not in SYNTHESIZE_REASON_CODES:
            raise ValueError(f"unknown synthesize reason_code {reason_code!r}")
        merged_details: dict = {
            **(details or {}),
            "reason_code": reason_code,
            "author_action": dict(_SYNTHESIZE_AUTHOR_ACTIONS[reason_code]),
        }
        DomainError.__init__(
            self,
            self.code,
            message,
            status_code=409,
            details=merged_details,
        )
        self.reason_code = reason_code
