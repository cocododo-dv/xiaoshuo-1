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


class DuplicateBookError(StyleReferenceError, DomainError):
    """同 text_checksum 的书已存在(同一份文本不重复导入):409 + 已有书的 id / 标题 / 状态 + 打开动作。"""

    def __init__(
        self,
        book_id: str,
        checksum: str,
        *,
        title: str | None = None,
        status: str | None = None,
    ) -> None:
        shown = f"《{title}》" if title else "这本书"
        DomainError.__init__(
            self,
            "STYLE_REFERENCE_BOOK_DUPLICATE",
            f"书库里已经有同一份文本:{shown}(内容完全相同,不重复导入)。",
            status_code=409,
            details={
                "book_id": book_id,
                "title": title,
                "status": status,
                "author_action": {
                    "action": "open_existing_reference_book",
                    "view": "styleref",
                    "book_id": book_id,
                    "label": "打开书库里的这本书",
                },
            },
        )
        self.book_id = book_id
        self.checksum = checksum


class EmptyBookError(StyleReferenceError, DomainError):
    """书籍文本在清洗 / 段落切分后为空:400。"""

    def __init__(self, stage: str) -> None:
        DomainError.__init__(
            self,
            "STYLE_REFERENCE_BOOK_EMPTY",
            "这个文件里没有可以当参考的正文(清洗、切段或剥掉站点声明 / 脚注之后什么也没剩下)。",
            status_code=400,
            details={"stage": stage},
        )
        self.stage = stage


class LLMRequiredError(StyleReferenceError, DomainError):
    """操作需要启用 LLM 但运行时没有可用的模型(严格 LLM:没有启发式兜底)。

    同时继承 DomainError,API 层自动映射为 409 + author_action 引导(而非通用 500),
    前端可据此跳转系统配置启用模型。
    """

    def __init__(self, operation: str) -> None:
        DomainError.__init__(
            self,
            "STYLE_REFERENCE_LLM_REQUIRED",
            "这一步要用模型,但还没有接入可用的模型:请到「设置 → 模型与接入」配置并开启后重试。",
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
    """段落分类的 LLM 调用失败(2026-09-15 严格 LLM:不降级到启发式)。

    502 + retryable + author_action:检查模型接入后「继续分类」。分类作业把它记在作业行的
    ``error_json`` 上(``details`` 带失败的阶段 / 批号 / 节点 / 重试次数 / 输出问题),作业游标保留。
    """

    retryable = True

    def __init__(
        self,
        *,
        code: str,
        message: str,
        book_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {
            **(details or {}),
            "reason_code": code,
            "book_id": book_id,
            "retryable": True,
            "author_action": {
                "action": "check_llm_provider_then_retry",
                "view": "systemConfig",
                "label": "段落分类的模型调用失败：检查模型接入后「继续分类」",
            },
        }
        DomainError.__init__(
            self,
            "STYLE_REFERENCE_CLASSIFICATION_FAILED",
            f"段落分类失败({code}):{message}",
            status_code=502,
            details=merged,
        )
        self.reason_code = code
        self.book_id = book_id


class CloudPolicyBlockedError(StyleReferenceError, DomainError):
    """书籍 cloud_policy=local_only 时,这一步要调用的模型不是本机模型:409 + 中文原因 + author_action。

    「仅本机」= 只有本机模型能看到正文(严格 LLM:没有启发式兜底)。告诉作者两条路:
    把这个节点换成本机模型,或改用送云策略重新导入。
    """

    def __init__(
        self,
        *,
        book_id: str,
        operation: str,
        provider: str | None = None,
        base_url: str | None = None,
        node_id: str | None = None,
        model: str | None = None,
    ) -> None:
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
        if node_id is not None:
            details["node_id"] = node_id
        if model:
            details["model"] = model
        where = f"节点 {node_id} 当前" if node_id else "当前模型"
        target = provider or "云端接入"
        DomainError.__init__(
            self,
            "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED",
            f"这本参考书设为「仅本机」:只有本机模型能读它的正文,但{where}走的是 {target}(不是本机)。"
            "请在「设置 → 模型与接入」把它换成本机模型(如 Ollama),或改用送云策略重新导入。",
            status_code=409,
            details=details,
        )
        self.book_id = book_id
        self.operation = operation


class CloudSendRightsBlockedError(StyleReferenceError, DomainError):
    """非本地策略缺少严格、显式的云端发送权声明:409(书的状态不允许,不是请求本身不合法)。"""

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
            "这本参考书没有声明云端发送权,不能把它的正文发给云端模型:"
            "请重新导入并确认发送权声明,或改用「仅本机」策略。",
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
            f"这本参考书的云端策略({cloud_policy or '空'})无法识别,不能把它的正文发给模型:"
            "请重新导入并选择「仅本机」「只送片段」或「允许全文上云」之一。",
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
