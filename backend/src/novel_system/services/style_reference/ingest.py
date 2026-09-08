"""Style Reference v1.1 导入服务。

依据《风格参考模块重构执行手册 v1.1》§6.2 / §6.4。

流程:
  1. 解码 + 清洗 → normalized text
  2. SHA256 checksum 计算 → book_id = "sr_book_{checksum[:12]}"
  3. 去重检测(同 checksum 已存在则 raise DuplicateBookError)
  4. cloud_policy 校验(Pydantic CloudPolicy)
  5. source_safety.scan_source_safety(text) 扫描(blocked_terms / risks)
  6. assess_input_size(total_chars) → stats_json.input_assessment
  7. split_paragraphs(text) → list of (start, end, body)
  8. segmentation.classify_paragraphs(...) → SegmentationResult
  9. MetricsEngine.compute_with_variance(records) → stats_json.metrics
     voice_signature.compute_voice_signature(paragraphs) → stats_json.voice_signature
 10. 落 style_reference_books + style_reference_paragraphs 表
 11. book.status = "ready"

`idempotency_key` 参数预留;PR-4 加 route 时由路由层包装
`execute_with_idempotency` 实际接入。本 service 不直接调用幂等机制。
"""

from __future__ import annotations

import os
import stat
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBook, StyleReferenceParagraph
from novel_system.services.errors import DomainError
from novel_system.services.source_safety import scan_source_safety
from novel_system.services.style_reference.cleanup import purge_derived_data
from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.errors import (
    DuplicateBookError,
    EmptyBookError,
    LLMRequiredError,
)
from novel_system.services.style_reference.metrics import (
    MetricsEngine,
    ParagraphRecord,
    compute_prose_shape_with_variance,
)
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import CloudPolicy
from novel_system.services.style_reference.segmentation import (
    SegmentationResult,
    classify_paragraphs,
)
from novel_system.services.style_reference.text_utils import (
    compute_text_checksum,
    decode_text,
    normalize_text,
    split_paragraphs,
)
from novel_system.services.style_reference.voice_signature import compute_voice_signature


MAX_REFERENCE_BOOK_BYTES = 10 * 1024 * 1024
_REFERENCE_BOOK_SUFFIXES = {".txt", ".md", ".markdown"}


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(os.path, "isjunction", None)
        if is_junction is not None and is_junction(path):
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        return bool(reparse_flag and attributes & reparse_flag)
    except (FileNotFoundError, OSError):
        return False


def _contains_link_or_reparse_component(path: Path) -> bool:
    current = path
    while True:
        if _is_link_or_reparse_point(current):
            return True
        if current.parent == current:
            return False
        current = current.parent


def _resolve_allowed_import_path(file_path: str | Path) -> Path:
    from novel_system.settings import get_settings

    path = Path(file_path).expanduser()
    if path.suffix.lower() not in _REFERENCE_BOOK_SUFFIXES:
        raise DomainError(
            "STYLE_REFERENCE_BOOK_FORMAT_UNSUPPORTED",
            "only .txt / .md / .markdown reference books can be imported by path",
            status_code=400,
        )

    configured_roots = get_settings(
        include_runtime_config=False
    ).style_reference_import_roots
    if not configured_roots:
        raise DomainError(
            "STYLE_REFERENCE_PATH_IMPORT_DISABLED",
            "server-side path import is disabled; use file upload or configure an allowed import root",
            status_code=403,
        )

    candidate = path if path.is_absolute() else Path.cwd() / path
    if _contains_link_or_reparse_component(candidate):
        raise DomainError(
            "STYLE_REFERENCE_BOOK_PATH_LINK_FORBIDDEN",
            "symbolic links and reparse points are not allowed for path imports",
            status_code=400,
        )
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise DomainError(
            "STYLE_REFERENCE_BOOK_PATH_NOT_FOUND",
            "reference book path does not exist",
            status_code=404,
        ) from exc
    except OSError as exc:
        raise DomainError(
            "STYLE_REFERENCE_BOOK_PATH_INVALID",
            "reference book path could not be resolved",
            status_code=400,
        ) from exc

    allowed_roots: list[Path] = []
    for configured_root in configured_roots:
        raw_root = configured_root.expanduser()
        if _contains_link_or_reparse_component(raw_root):
            continue
        try:
            root = raw_root.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        if root.is_dir():
            allowed_roots.append(root)
    if not allowed_roots:
        raise DomainError(
            "STYLE_REFERENCE_IMPORT_ROOT_INVALID",
            "no configured style-reference import root is available",
            status_code=503,
        )
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        raise DomainError(
            "STYLE_REFERENCE_BOOK_PATH_FORBIDDEN",
            "reference book path is outside the configured import roots",
            status_code=403,
        )
    if not resolved.is_file():
        raise DomainError(
            "STYLE_REFERENCE_BOOK_PATH_INVALID",
            "reference book path must point to a regular file",
            status_code=400,
        )
    return resolved


def _normalize_rights_declaration(
    declaration: dict[str, Any] | None, policy: CloudPolicy
) -> dict[str, Any]:
    """归一并校验导入权属声明（§5.9 / §11 规则 9）。

    local_only 未声明 → 记 ``{declared: False}``；非本地策略未显式声明 → 拒绝。
    声明 ``send_rights=False`` 却选了会送云端的策略（非 local_only）→ 矛盾拒绝：
    不得默认拥有云端发送权。
    """
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if declaration:
        for field in ("declared", "analysis_rights", "send_rights"):
            if field in declaration and not isinstance(declaration[field], bool):
                raise DomainError(
                    "STYLE_REFERENCE_RIGHTS_DECLARATION_INVALID",
                    f"权属声明字段 {field} 必须是布尔值。",
                    status_code=400,
                )
    if not declaration or declaration.get("declared") is False:
        if policy != CloudPolicy.LOCAL_ONLY:
            raise DomainError(
                "STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED",
                "云端策略需要用户显式声明发送权；请确认声明或改用 local_only。",
                status_code=400,
            )
        return {
            "declared": False,
            "analysis_rights": None,
            "send_rights": None,
            "declared_by": None,
            "declared_at": now,
        }
    analysis = declaration.get("analysis_rights", False)
    send = declaration.get("send_rights", False)
    if policy != CloudPolicy.LOCAL_ONLY and not send:
        raise DomainError(
            "STYLE_REFERENCE_SEND_RIGHTS_REQUIRED",
            "云端策略需要用户声明发送权（send_rights=true）；未授权发送请改用 local_only。",
            status_code=400,
        )
    return {
        "declared": True,
        "analysis_rights": analysis,
        "send_rights": send,
        "declared_by": declaration.get("declared_by"),
        "declared_at": now,
    }


@dataclass
class IngestResult:
    """ingest_path / ingest_upload 的返回结构。"""

    book: StyleReferenceBook
    paragraphs_count: int
    safety_payload: dict[str, Any]


def assess_input_size(total_chars: int) -> dict[str, str]:
    """按 input_thresholds.yaml 把总字数映射到 4 层级。

    返回 {"language": "skip"|"low"|"medium"|"high", ...} 4 个 layer。
    """
    thresholds = load_yaml_config("input_thresholds")
    result: dict[str, str] = {}
    for layer in ("language", "narrative", "scene", "theme"):
        cfg = thresholds.get(layer, {})
        skip = int(cfg.get("skip", 0))
        low = int(cfg.get("low", skip))
        high = int(cfg.get("high", low))
        if total_chars < skip:
            level = "skip"
        elif total_chars < low:
            level = "low"
        elif total_chars < high:
            level = "medium"
        else:
            level = "high"
        result[layer] = level
    return result


class IngestService:
    """Style Reference 书籍导入服务。"""

    def __init__(self, session: Session, *, llm_client: Any | None = None, llm_enabled: bool | None = None) -> None:
        self.session = session
        self.repo = StyleReferenceRepository(session)
        self._llm_client = llm_client
        if llm_enabled is None:
            from novel_system.settings import get_settings

            llm_enabled = bool(get_settings().llm_enabled)
        self._llm_enabled = llm_enabled
        self._metrics_engine: MetricsEngine | None = None

    # ------------------------------------------------------------------ public

    def ingest_path(
        self,
        file_path: str | Path,
        *,
        title: str,
        author_label: str | None,
        cloud_policy: str | CloudPolicy,
        rights_declaration: dict[str, Any] | None = None,
        idempotency_key: str | None = None,  # noqa: ARG002 (预留 PR-4 用)
    ) -> IngestResult:
        path = _resolve_allowed_import_path(file_path)
        # 服务端路径导入只允许纯文本参考书后缀。这个端点按任意路径读服务器文件,
        # 不加白名单时可把 /etc/passwd、.env、*.db 等读进 paragraphs 表并经 API
        # 回读——收窄为与 upload 相同的文本格式(且 path 模式**必须**有后缀,
        # 无后缀的系统文件一律拒绝)。
        suffix = path.suffix.lower()
        if suffix not in {".txt", ".md", ".markdown"}:
            raise DomainError(
                "STYLE_REFERENCE_BOOK_FORMAT_UNSUPPORTED",
                "only .txt / .md / .markdown reference books can be imported by path",
                status_code=400,
            )
        if not path.exists():
            raise DomainError(
                "STYLE_REFERENCE_BOOK_PATH_NOT_FOUND",
                f"reference book path does not exist: {path}",
                status_code=404,
            )
        with path.open("rb") as handle:
            raw = handle.read(MAX_REFERENCE_BOOK_BYTES + 1)
        if len(raw) > MAX_REFERENCE_BOOK_BYTES:
            raise DomainError(
                "STYLE_REFERENCE_UPLOAD_TOO_LARGE",
                f"reference book exceeds {MAX_REFERENCE_BOOK_BYTES // (1024 * 1024)}MB limit",
                status_code=413,
            )
        return self._ingest_bytes(
            raw_bytes=raw,
            source_kind="path",
            source_path=str(path),
            title=(title or "").strip() or path.stem,
            author_label=author_label,
            cloud_policy=cloud_policy,
            rights_declaration=rights_declaration,
        )

    def ingest_upload(
        self,
        raw_bytes: bytes,
        *,
        file_name: str | None,
        title: str,
        author_label: str | None,
        cloud_policy: str | CloudPolicy,
        rights_declaration: dict[str, Any] | None = None,
        idempotency_key: str | None = None,  # noqa: ARG002 (预留 PR-4 用)
    ) -> IngestResult:
        if file_name:
            suffix = Path(file_name).suffix.lower()
            if suffix and suffix not in {".txt", ".md", ".markdown"}:
                raise DomainError(
                    "STYLE_REFERENCE_BOOK_FORMAT_UNSUPPORTED",
                    "only TXT and MD reference books are supported",
                    status_code=400,
                )
        fallback_title = Path(file_name).stem if file_name else "未命名参考书"
        return self._ingest_bytes(
            raw_bytes=raw_bytes,
            source_kind="upload",
            source_path=file_name,
            title=(title or "").strip() or fallback_title,
            author_label=author_label,
            cloud_policy=cloud_policy,
            rights_declaration=rights_declaration,
        )

    def reclassify(self, book_id: str) -> int:
        """重跑段落分类器(与首次导入共用 ``classify_paragraphs`` 管线)。

        - 更新全部 paragraphs 的 ``paragraph_type`` / ``classifier_confidence``;
        - 回写 ``book.stats_json`` 的 metrics / classifier_calibration /
          paragraph_type_distribution(与 ingest 同一计算路径);
        - 级联清空派生数据(runs / findings / profiles / bindings 等;
          paragraphs 与 book 本身保留)。

        分类需要 LLM;不可用时抛 :class:`LLMRequiredError`。
        返回重分类的段落数。
        """
        book = self.repo.get_book(book_id)
        if book is None:
            raise DomainError(
                "STYLE_REFERENCE_BOOK_NOT_FOUND",
                f"book {book_id!r} not found",
                status_code=404,
            )
        if not self._llm_enabled or self._llm_client is None:
            raise LLMRequiredError(operation="reclassify_book")
        # 附录 B — local_only 的书禁止把段落送往云端 LLM 分类器
        ensure_cloud_llm_allowed(book, operation="reclassify_book")

        paragraphs = self.repo.list_paragraphs(book_id)
        if not paragraphs:
            raise EmptyBookError("reclassify")

        spans = [(p.start_offset, p.end_offset, p.text) for p in paragraphs]
        seg_result = classify_paragraphs(
            spans,
            llm_enabled=True,
            llm_client=self._llm_client,
            session=self.session,
            scope_id=book_id,
        )
        for paragraph, c in zip(paragraphs, seg_result.classifications):
            paragraph.paragraph_type = c.paragraph_type
            paragraph.classifier_confidence = float(c.confidence)

        book.stats_json = {
            **(book.stats_json or {}),
            **self._classification_stats(spans, seg_result),
        }

        purge_derived_data(self.session, book_id)
        self.session.flush()
        return len(paragraphs)

    # ----------------------------------------------------------------- private

    def _ingest_bytes(
        self,
        *,
        raw_bytes: bytes,
        source_kind: str,
        source_path: str | None,
        title: str,
        author_label: str | None,
        cloud_policy: str | CloudPolicy,
        rights_declaration: dict[str, Any] | None = None,
    ) -> IngestResult:
        normalized = normalize_text(decode_text(raw_bytes))
        if not normalized:
            raise EmptyBookError("normalize")

        # cloud_policy Pydantic 校验
        policy = CloudPolicy(cloud_policy) if isinstance(cloud_policy, str) else cloud_policy

        # Wave 7 §5.9 / §11 规则 9 — 记录导入权属声明（不得默认拥有云端发送权）
        rights = _normalize_rights_declaration(rights_declaration, policy)

        checksum = compute_text_checksum(normalized)
        book_id = f"sr_book_{checksum[:12]}"

        existing = self.repo.get_book(book_id)
        if existing is not None:
            raise DuplicateBookError(book_id=book_id, checksum=checksum)

        # Safety 扫描(通用 source_safety,与 reference_safety 解耦)
        safety_payload = scan_source_safety(normalized)

        # 段落切分
        paragraph_spans = split_paragraphs(normalized)
        if not paragraph_spans:
            raise EmptyBookError("segmentation")

        # LLM / 启发式分类(local_only 的书强制走启发式,段落不出本机)
        use_llm = self._llm_enabled and policy != CloudPolicy.LOCAL_ONLY
        seg_result = classify_paragraphs(
            paragraph_spans,
            llm_enabled=use_llm,
            llm_client=self._llm_client if use_llm else None,
            session=self.session if use_llm else None,
            scope_id=book_id if use_llm else None,
        )

        stats_json = {
            **self._classification_stats(paragraph_spans, seg_result),
            "input_assessment": assess_input_size(len(normalized)),
            "safety": safety_payload,
            "rights_declaration": rights,
            # v2 W3:全书确定性声音签名(闭类词 / 标点 / 引导句 / 节奏),内容安全,
            # 不依赖段型分类器;合成期(W1)由此写入 profile_json.voice_signature。
            "voice_signature": compute_voice_signature(
                [body for _start, _end, body in paragraph_spans]
            ),
        }

        book = self.repo.create_book(
            book_id=book_id,
            title=title,
            author_label=author_label,
            source_kind=source_kind,
            source_path=source_path,
            cloud_policy=policy.value,
            text_checksum=checksum,
            total_chars=len(normalized),
            status="ready",
            stats_json=stats_json,
        )

        # 落段落表
        for idx, ((start, end, body), c) in enumerate(zip(paragraph_spans, seg_result.classifications)):
            self.repo.create_paragraph(
                paragraph_id=f"sr_para_{checksum[:8]}_{idx:04d}",
                book_id=book_id,
                paragraph_index=idx,
                paragraph_type=c.paragraph_type,
                start_offset=start,
                end_offset=end,
                text=body,
                char_count=len(body),
                classifier_confidence=float(c.confidence),
            )

        return IngestResult(
            book=book,
            paragraphs_count=len(paragraph_spans),
            safety_payload=safety_payload,
        )

    def _classification_stats(
        self,
        paragraph_spans: list[tuple[int, int, str]],
        seg_result: SegmentationResult,
    ) -> dict[str, Any]:
        """stats_json 中跟段落分类绑定的 3 个键(ingest 与 reclassify 共用)。

        返回 ``metrics`` / ``classifier_calibration`` / ``paragraph_type_distribution``。
        """
        records = [
            ParagraphRecord(
                text=body,
                paragraph_type=c.paragraph_type,
            )
            for (_s, _e, body), c in zip(paragraph_spans, seg_result.classifications)
        ]

        engine = self._get_metrics_engine()
        metrics_with_var = engine.compute_with_variance(records)
        sample_count = len(records)
        metrics_block: dict[str, dict[str, float | int]] = {
            name: {"mean": float(mean), "std": float(std), "sample_count": sample_count}
            for name, (mean, std) in metrics_with_var.items()
        }
        prose_shape_block: dict[str, dict[str, float | int]] = {
            name: {
                "mean": float(mean),
                "std": float(std),
                "sample_count": sample_count,
            }
            for name, (mean, std) in compute_prose_shape_with_variance(
                records
            ).items()
        }

        type_counter = Counter(c.paragraph_type for c in seg_result.classifications)
        type_distribution = {
            ptype: round(count / sample_count, 4)
            for ptype, count in type_counter.items()
        } if sample_count else {}

        return {
            "metrics": metrics_block,
            "prose_shape_metrics": prose_shape_block,
            "classifier_calibration": seg_result.calibration,
            "paragraph_type_distribution": type_distribution,
        }

    def _get_metrics_engine(self) -> MetricsEngine:
        if self._metrics_engine is None:
            self._metrics_engine = MetricsEngine()
        return self._metrics_engine
