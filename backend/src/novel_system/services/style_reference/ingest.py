"""Style Reference 导入服务(2026-09-23 风格参考 v3)。

依据《风格参考模块重构执行手册 v1.1》§6.2 / §6.4;严格 LLM(2026-09-15);作业表(v3)。

导入的准备工作(一次请求内,几秒):
  1. 解码 + 清洗 → normalized text;SHA256 → ``book_id = "sr_book_{checksum[:12]}"``;
  2. 去重:同一份文本已在书库 → 409 ``STYLE_REFERENCE_BOOK_DUPLICATE``(带已有书的 id / 标题 / 状态);
  3. cloud_policy 校验 + 权属声明(不得默认拥有云端发送权);「仅本机」要求分类节点的实际路由是本机模型;
  4. ``source_safety`` 扫描、``assess_input_size``;
  5. 切段 → 剥副文本 → 场界(与切段同一口径,剥副文本时重映射,见 ``text_utils.scene_break_indexes``);
     清洗 / 切段后没有正文 → 400 ``STYLE_REFERENCE_BOOK_EMPTY``;
  6. **批量**落书与段落行(26,616 段:逐行 add+flush 12 s → 一次 executemany);
  7. 产品模式(``llm_enabled=True``):段落行类型 ``unclassified``、书 ``ingesting``,建一个 ``classify``
     作业(``import_job``),调用方提交后派发;整本 LLM 分类、统计、声音签名、书置 ``ready`` 都在作业里。

**离线夹具模式**(``llm_enabled=False``,只供测试与本地语料工具):启发式同步分类、书直接 ``ready``。
产品路由没有 LLM 时已经 409 ``STYLE_REFERENCE_LLM_REQUIRED``,永远不会走到这条路。

重新分类:``start_reclassify(book_id, mode="reclassify")`` 是**破坏式**的(先清掉这本书的全部派生数据——
抽取 run / 发现 / 引文 / 画像 / 绑定 / 禁用词 / 回测报告 / 作业 / 窗口索引——再从头分类,书 ``ingesting``);
``mode="retype"`` 是**就地重标段落类型**(正文不变,派生数据与绑定全部保留,书保持 ``ready``);
``resume(book_id)`` 从上次的游标继续(失败 / 取消 / 进程重启之后)。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

from novel_system.db.models import StyleReferenceBook, StyleReferenceJob, StyleReferenceParagraph, utcnow
from novel_system.services.errors import DomainError
from novel_system.services.source_safety import scan_source_safety
from novel_system.services.style_reference.classification_stats import compute_classification_stats
from novel_system.services.style_reference.cleanup import purge_derived_data
from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.errors import (
    DuplicateBookError,
    EmptyBookError,
    LLMRequiredError,
)
from novel_system.services.style_reference.metrics import MetricsEngine
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed, ensure_local_only_llm
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.schemas import CloudPolicy
from novel_system.services.style_reference.segmentation import (
    SegmentationResult,
    classify_paragraphs,
)
from novel_system.services.style_reference.segmentation.llm import CLASSIFY_NODE_IDS
from novel_system.services.style_reference.text_utils import (
    compute_text_checksum,
    decode_text,
    gap_preserving_text,
    is_paratext_paragraph,
    normalize_text,
    scene_break_indexes,
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
    不得默认拥有云端发送权。两种拒绝都是请求本身不合法:同一个 400
    ``STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED``(2026-09-23 v3 I15:
    ``STYLE_REFERENCE_SEND_RIGHTS_REQUIRED`` 只表示「已有的书缺发送权」,一律 409)。
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
            "STYLE_REFERENCE_SEND_RIGHTS_DECLARATION_REQUIRED",
            "云端策略需要用户声明发送权（send_rights=true）；未授权发送请改用 local_only。",
            status_code=400,
            details={"reason": "send_rights_false"},
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
    # 产品模式:书已落库、分类作业已建(status=ingesting),调用方提交后派发 ``job``
    classification_pending: bool = False
    job: StyleReferenceJob | None = None


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


def _book_not_found(book_id: str) -> DomainError:
    return DomainError(
        "STYLE_REFERENCE_BOOK_NOT_FOUND",
        f"book {book_id!r} not found",
        status_code=404,
    )


def bulk_insert_paragraphs(
    session: Session,
    *,
    book_id: str,
    checksum: str,
    spans: list[tuple[int, int, str]],
    types: list[tuple[str, float]],
) -> int:
    """一次 executemany 落全部段落行(v3 I13:逐行 add+flush 在 26,616 段上要 12 s)。"""
    now = utcnow()
    rows = [
        {
            "paragraph_id": f"sr_para_{checksum[:8]}_{idx:04d}",
            "book_id": book_id,
            "paragraph_index": idx,
            "paragraph_type": ptype,
            "start_offset": start,
            "end_offset": end,
            "text": body,
            "char_count": len(body),
            "classifier_confidence": float(conf),
            "created_at": now,
        }
        for idx, ((start, end, body), (ptype, conf)) in enumerate(zip(spans, types, strict=True))
    ]
    if rows:
        session.execute(insert(StyleReferenceParagraph), rows)
    return len(rows)


class IngestService:
    """Style Reference 书籍导入服务。"""

    def __init__(
        self,
        session: Session,
        *,
        llm_enabled: bool | None = None,
        op_key: str | None = None,
        llm_client: Any | None = None,
    ) -> None:
        self.session = session
        self.repo = StyleReferenceRepository(session)
        if llm_enabled is None:
            from novel_system.settings import get_settings

            llm_enabled = bool(get_settings().llm_enabled)
        # True = 产品模式(建分类作业);False = 离线夹具模式(启发式,测试 / 本地语料工具)
        self._llm_enabled = bool(llm_enabled)
        self._op_key = op_key
        # 只用来解析分类节点的实际路由(「仅本机」检查);分类本身在作业里按当时的配置取客户端。
        self._llm_client = llm_client
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

    def start_reclassify(self, book_id: str, *, mode: str = "reclassify") -> StyleReferenceJob:
        """重新分类(后台作业,调用方提交后派发)。

        - ``mode="reclassify"``(**破坏式**,旧语义):先清掉这本书的全部派生数据(抽取 run / 发现 /
          引文 / 画像 / 绑定 / 禁用词 / 回测报告 / 作业 / 窗口索引;段落与书保留),书置 ``ingesting``,
          再从头分类;
        - ``mode="retype"``(**就地重标段落类型**,v3):正文不变,只重标类型;抽取、画像、绑定全部保留,
          书保持 ``ready``。完成后 ``paragraph_types_revision`` +1(窗口索引据此重算类型分布,画像
          据此提示「段落类型已更新,建议重新学习文风」)。

        LLM 未启用 → 409 ``STYLE_REFERENCE_LLM_REQUIRED``;「仅本机」的书要求分类节点走本机模型;
        已有排队 / 运行中的分类作业 → 409 ``STYLE_REFERENCE_CLASSIFICATION_ALREADY_ACTIVE``。
        """
        from novel_system.services.style_reference.import_job import (
            MODE_RECLASSIFY,
            MODE_RETYPE,
            active_classification_job,
            count_paragraphs,
            create_classification_job,
        )

        if mode not in (MODE_RECLASSIFY, MODE_RETYPE):
            raise ValueError(f"unknown reclassify mode {mode!r}")
        book = self.repo.get_book(book_id)
        if book is None:
            raise _book_not_found(book_id)
        if not self._llm_enabled:
            raise LLMRequiredError(operation="reclassify_book")
        ensure_cloud_llm_allowed(
            book, operation="reclassify_book", node_ids=CLASSIFY_NODE_IDS, llm_client=self._llm_client
        )
        if count_paragraphs(self.session, book_id) == 0:
            raise EmptyBookError("reclassify")
        active = active_classification_job(self.session, book_id)
        if active is not None:
            # 先查再清:破坏式重分类不能把正在跑的分类作业连同派生数据一起删掉
            create_classification_job(self.session, book, mode=mode, op_key=self._op_key)
        if mode == MODE_RETYPE and str(book.status or "") != "ready":
            raise DomainError(
                "STYLE_REFERENCE_BOOK_NOT_READY",
                "这本书的段落分类还没完成:先「继续分类」,完成后才能就地重标段落类型。",
                status_code=409,
                details={
                    "book_id": book_id,
                    "status": book.status,
                    "author_action": {
                        "action": "resume_classification",
                        "view": "styleref",
                        "book_id": book_id,
                        "label": "继续分类",
                    },
                },
            )
        if mode == MODE_RECLASSIFY:
            purge_derived_data(self.session, book_id)
        job = create_classification_job(self.session, book, mode=mode, op_key=self._op_key)
        self.session.flush()
        return job

    def resume(self, book_id: str) -> StyleReferenceJob:
        """「继续分类」:从上次失败 / 取消 / 中断的游标续跑(进程重启之后也行)。"""
        from novel_system.services.style_reference.import_job import resume_classification

        book = self.repo.get_book(book_id)
        if book is None:
            raise _book_not_found(book_id)
        if not self._llm_enabled:
            raise LLMRequiredError(operation="resume_classification")
        ensure_cloud_llm_allowed(
            book, operation="resume_classification", node_ids=CLASSIFY_NODE_IDS, llm_client=self._llm_client
        )
        job = resume_classification(self.session, book, op_key=self._op_key)
        self.session.flush()
        return job

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
        decoded = decode_text(raw_bytes)
        normalized = normalize_text(decoded)
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
            raise DuplicateBookError(
                book_id=book_id, checksum=checksum, title=existing.title, status=existing.status
            )

        if self._llm_enabled and policy == CloudPolicy.LOCAL_ONLY:
            # 「仅本机」:分类节点的实际路由必须是本机模型(严格 LLM:没有启发式兜底)
            ensure_local_only_llm(
                operation="import_book",
                book_id=book_id,
                node_ids=CLASSIFY_NODE_IDS,
                llm_client=self._llm_client,
            )

        # Safety 扫描(通用 source_safety,与 reference_safety 解耦)
        safety_payload = scan_source_safety(normalized)

        # 段落切分;2026-09-14 保真修补:副文本(站点声明 / 脚注 / 网址)不是作者的文字,不入段落表——
        # 否则它们会进指标、声音签名、样例窗口与结构画像的章首 / 章尾样例。
        raw_spans = split_paragraphs(normalized)
        kept = [not is_paratext_paragraph(body) for _start, _end, body in raw_spans]
        paragraph_spans = [span for span, keep in zip(raw_spans, kept) if keep]
        paratext_dropped = len(raw_spans) - len(paragraph_spans)
        if not paragraph_spans:
            raise EmptyBookError("segmentation")
        # 场界(「其后有场界」的段落索引,按剥离副文本后的编号):与切段同一口径的空行型场界
        # (剥副文本时挪到前一个保留段上)+ 纯符号分隔行(省略号行不算)。
        scene_breaks = scene_break_indexes(
            gap_preserving_text(decoded),
            [body for _start, _end, body in raw_spans],
            kept,
        )

        stats_json: dict[str, Any] = {
            "input_assessment": assess_input_size(len(normalized)),
            "paratext_dropped": paratext_dropped,
            "scene_breaks": scene_breaks,
            "safety": safety_payload,
            "rights_declaration": rights,
        }

        if self._llm_enabled:
            from novel_system.services.style_reference.import_job import (
                MODE_IMPORT,
                UNCLASSIFIED_PARAGRAPH_TYPE,
                create_classification_job,
            )

            book = self.repo.create_book(
                book_id=book_id,
                title=title,
                author_label=author_label,
                source_kind=source_kind,
                source_path=source_path,
                cloud_policy=policy.value,
                text_checksum=checksum,
                total_chars=len(normalized),
                status="ingesting",
                stats_json=stats_json,
            )
            bulk_insert_paragraphs(
                self.session,
                book_id=book_id,
                checksum=checksum,
                spans=paragraph_spans,
                types=[(UNCLASSIFIED_PARAGRAPH_TYPE, 0.0)] * len(paragraph_spans),
            )
            job = create_classification_job(self.session, book, mode=MODE_IMPORT, op_key=self._op_key)
            self.session.flush()
            return IngestResult(
                book=book,
                paragraphs_count=len(paragraph_spans),
                safety_payload=safety_payload,
                classification_pending=True,
                job=job,
            )

        # 离线夹具模式:启发式同步分类(只供测试与本地语料工具)
        seg_result = classify_paragraphs(paragraph_spans)
        stats_json.update(self._classification_stats(paragraph_spans, seg_result))
        # v2 W3:全书确定性声音签名(闭类词 / 标点 / 引导句 / 节奏),内容安全,不依赖段型分类器。
        stats_json["voice_signature"] = compute_voice_signature(
            [body for _start, _end, body in paragraph_spans]
        )
        stats_json["paragraph_types_revision"] = 1
        stats_json["classification_provenance"] = {
            "source": "offline_heuristic",
            "llm_paragraphs": 0,
            "heuristic_paragraphs": len(paragraph_spans),
            "prompt_version": None,
            "classified_at": utcnow(),
            "mode": "import",
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
        bulk_insert_paragraphs(
            self.session,
            book_id=book_id,
            checksum=checksum,
            spans=paragraph_spans,
            types=[(c.paragraph_type, float(c.confidence)) for c in seg_result.classifications],
        )
        self.session.flush()
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
        """stats_json 中跟段落分类绑定的键(实现见叶子模块 ``classification_stats``,分类作业也用它)。"""
        return compute_classification_stats(
            paragraph_spans, seg_result, metrics_engine=self._get_metrics_engine()
        )

    def _get_metrics_engine(self) -> MetricsEngine:
        if self._metrics_engine is None:
            self._metrics_engine = MetricsEngine()
        return self._metrics_engine
