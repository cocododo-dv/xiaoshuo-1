"""立项 C — Strategy C(RAG)三粒度向量召回。

设计依据:``docs/style_reference_module_design_v1.1.md`` §10 Phase 3(三粒度
sentence/paragraph/scene 索引 + rerank)、§12 防漂移(续写按最新上下文重召回)、
§6.1(``style_ref_rag_rerank`` 路由)与 ``docs/style-reference-phase3-backlog.md``
立项 C。

三粒度索引建立在 ``services/vector_store.py`` 抽象之上(``memory`` 后端为
Windows 确定性后端;``chroma`` 走 WSL 集成测试)。索引在 profile **synthesize
之后**构建(profile 就绪即建),在 ``cleanup.purge_derived_data`` 时随派生数据清理。

设计硬约束:
- **inject 热路径无 LLM**(§11 风险 6:inject < 50ms,库内拼装)。因此召回用
  内容克制的风格签名近邻 + **确定性风格距离 rerank**,而非 LLM rerank。
  ``style_ref_rag_rerank`` LLM 节点仅作为离线/预览增强 hook 落地,不在热路径调用。
- **内容独立**:原文与 query 题材词不进入正向检索键。索引 document 只保存
  句段形状、标点节奏、虚词/代词与文字系统比例的量化签名;原文仅作为最终 payload。
  内容二元组重叠只可施加负向安全惩罚,绝不增加召回分。
- **反抄袭**(§A.5):RAG 注入参考书原文片段,与 few-shot(B)同性质——调用方
  (``injection._render``)在 rag_block 非空时强制随注红线段;此处再按 budget 截断,
  避免注入大段原文。
- **确定性**:切句 / scene 聚合 / rerank 全为纯函数,Windows ``memory`` 后端下可
  确定性断言。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from novel_system.services.errors import DomainError
from novel_system.services.style_reference.config_loader import load_yaml_config
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.style_signature import (
    FEATURE_NAMES,
    STYLE_SIGNATURE_VERSION,
    StyleSignature,
    content_shingle_overlap,
    extract_style_signature,
    parse_style_signature,
    query_view_for_granularity,
    style_signature_similarity,
)
from novel_system.services.vector_store import VectorStore, get_vector_store

logger = logging.getLogger(__name__)

# ``build_rag_index`` 的进度回调:``(stage, done, total)``,stage ∈ {"signatures", "write"}。
RagProgress = Callable[[str, int, int], None]
RAG_PROGRESS_EVERY_PARAGRAPHS = 250

# 同一画像的索引构建串行化:apply 的后台构建与生成期 ``_render_rag`` 的惰性 ensure 同时到达时,
# 后者等前者建完直接读到 ready,而不是各建一份互相覆盖。
_BUILD_LOCKS: dict[str, threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()

# apply 后台建索引的执行器(单线程;索引是 CPU 工作,串行避免两本大书同时吃满 CPU)。
_RAG_EXECUTOR: ThreadPoolExecutor | None = None
_RAG_EXECUTOR_LOCK = threading.Lock()


def _profile_build_lock(profile_id: str) -> threading.Lock:
    with _BUILD_LOCKS_GUARD:
        lock = _BUILD_LOCKS.get(profile_id)
        if lock is None:
            lock = threading.Lock()
            _BUILD_LOCKS[profile_id] = lock
        return lock

# 三粒度(顺序即 "由细到粗",用于稳定排序的次序权重)
GRANULARITIES: tuple[str, ...] = ("sentence", "paragraph", "scene")
# 分数相等时的粒度优先级(由细到粗;细粒度更聚焦,优先)。用于跨粒度 rerank 的
# 确定性 tiebreaker,避免退化为粒度字符串字母序(scene<sentence 之类反直觉)。
_GRAN_RANK: dict[str, int] = {g: i for i, g in enumerate(GRANULARITIES)}

# rag_rerank LLM 节点 id(§6.1;仅离线/预览增强 hook,inject 热路径不调用)
RAG_RERANK_NODE_ID = "style_ref_rag_rerank"

# 配置兜底:``config/style_reference/injection_budget.yaml`` 缺 rag.* 键时使用。
_DEFAULT_RAG: dict[str, Any] = {
    "rag_top_k": 3,  # 每粒度向量召回条数
    "rag_inject_max": 5,  # 注入到 prompt 的合并后总条数上限
    "rag_quote_max_chars": 100,  # 单条样例截断
    "rag_block_max_chars": 600,  # 整个 [风格检索样例] block 上限
    "rag_scene_target_chars": 600,  # scene 聚合的目标块长(连续段落累加阈值)
    "rag_min_sentence_chars": 8,  # 过短句(标点/语气词)不入 sentence 索引
    "rag_min_paragraph_chars": 8,  # 过短段不入 paragraph 索引
    "rag_context_query_max_chars": 2000,  # 续写防漂移 query 取最近 N 字
    "rag_signature_candidate_pool_min": 24,  # ANN 只作 shortlist;随后精确风格距离重排
    "rag_signature_candidate_pool_multiplier": 8,
    "rag_paragraph_query_chars": 400,
    "rag_style_min_score": 0.45,
    "rag_content_overlap_penalty_threshold": 0.18,
    "rag_content_overlap_penalty_max": 0.25,
    # 粒度权重:签名特征已归一,权重仅微调 "更完整肌理优先"。
    "rag_weight_sentence": 1.0,
    "rag_weight_paragraph": 1.1,
    "rag_weight_scene": 1.15,
}

# 句末标点(含中英文 + 省略号);在标点后、且下一字符不是右引号/右括号处断句。
_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？!?…])(?=[^」』”’)）】　\s]|$)")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def load_rag_config() -> dict[str, Any]:
    """读取 rag.* 预算(并入 injection_budget.yaml),缺失键回退默认。"""
    try:
        raw = load_yaml_config("injection_budget")
    except FileNotFoundError:
        return dict(_DEFAULT_RAG)
    merged = dict(_DEFAULT_RAG)
    for key in _DEFAULT_RAG:
        if isinstance(raw, dict) and key in raw:
            merged[key] = raw[key]
    return merged


def _granularity_weights(config: dict[str, Any]) -> dict[str, float]:
    return {
        "sentence": float(config.get("rag_weight_sentence", 1.0)),
        "paragraph": float(config.get("rag_weight_paragraph", 1.1)),
        "scene": float(config.get("rag_weight_scene", 1.15)),
    }


# ---------------------------------------------------------------------------
# 文本切分(自包含、确定性)
# ---------------------------------------------------------------------------


def split_sentences(text: str, *, min_chars: int = 8) -> list[str]:
    """中文优先的确定性切句:按句末标点边界切,保留标点;过滤过短片段。"""
    if not text or not text.strip():
        return []
    raw = _SENTENCE_BOUNDARY.split(text)
    out: list[str] = []
    for piece in raw:
        s = piece.strip()
        if len(s) >= min_chars:
            out.append(s)
    return out


def aggregate_scenes(
    paragraphs: list[Any],
    *,
    target_chars: int = 600,
) -> list[dict[str, Any]]:
    """把连续段落按字数窗口聚合为 "scene 级" 块。

    纯启发式(无标注 scene 边界):顺序累加段落正文,累计长度达到
    ``target_chars`` 即封一块;块的 ``paragraph_type`` 取块内出现最多的段类型
    (并列时取首个出现),用于注入时标注与 ptype 命中加权。
    """
    blocks: list[dict[str, Any]] = []
    buf_texts: list[str] = []
    buf_types: list[str] = []
    buf_first_index: int | None = None
    buf_len = 0

    def _flush() -> None:
        nonlocal buf_texts, buf_types, buf_first_index, buf_len
        if not buf_texts:
            return
        text = "\n".join(buf_texts).strip()
        ptype = _dominant(buf_types)
        blocks.append(
            {
                "text": text,
                "paragraph_type": ptype,
                "paragraph_index": (
                    buf_first_index if buf_first_index is not None else 0
                ),
            }
        )
        buf_texts, buf_types, buf_first_index, buf_len = [], [], None, 0

    for para in paragraphs:
        ptext = (getattr(para, "text", "") or "").strip()
        if not ptext:
            continue
        if buf_first_index is None:
            buf_first_index = getattr(para, "paragraph_index", 0)
        buf_texts.append(ptext)
        buf_types.append(getattr(para, "paragraph_type", "") or "")
        buf_len += len(ptext)
        if buf_len >= target_chars:
            _flush()
    _flush()
    return blocks


def _dominant(values: list[str]) -> str | None:
    counts: dict[str, int] = {}
    order: list[str] = []
    for v in values:
        if not v:
            continue
        if v not in counts:
            order.append(v)
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    # 出现次数降序;并列时按首次出现顺序(确定性)
    return max(order, key=lambda v: (counts[v], -order.index(v)))


# ---------------------------------------------------------------------------
# collection 命名
# ---------------------------------------------------------------------------


def rag_collection_name(profile_id: str, granularity: str) -> str:
    # v2 collection 与旧的原文向量索引物理隔离,避免升级后误把内容相似当风格相似。
    return f"style_ref_rag_v2_{profile_id}_{granularity}"


def rag_manifest_collection_name(profile_id: str) -> str:
    return f"style_ref_rag_v2_{profile_id}_manifest"


# ---------------------------------------------------------------------------
# 索引构建 / 删除(生命周期)
# ---------------------------------------------------------------------------


def _resolve_store(vector_store: VectorStore | None) -> VectorStore | None:
    """拿向量后端;Windows 原生 chroma 不可用等情况返回 None(调用方降级)。"""
    if vector_store is not None:
        return vector_store
    try:
        return get_vector_store()
    except DomainError as exc:  # CHROMA_RUNTIME_UNSUPPORTED / 依赖缺失
        logger.info("rag vector store unavailable, skipping: %s", exc)
        return None


def build_rag_index(
    session: Any,
    profile: Any,
    *,
    vector_store: VectorStore | None = None,
    book_id: str | None = None,
    progress: RagProgress | None = None,
) -> dict[str, Any]:
    """为 profile 构建三粒度向量索引。

    数据源:本书全部段落(``StyleReferenceParagraph``)。三粒度文档:
    - sentence:每段切句,逐句一文档;
    - paragraph:每段一文档;
    - scene:连续段落按字数窗口聚合,逐块一文档。

    向量 document 是无题材词的风格签名搜索码;原文放在 ``source_text`` metadata,
    仅在召回完成后用于注入。幂等:``write_collection`` 内部先 reset 同名
    collection,重复构建覆盖旧索引。
    向量后端不可用时返回 ``{"skipped": <reason>}``(不抛,调用方 synthesize 不阻断)。
    """
    store = _resolve_store(vector_store)
    if store is None:
        return {"skipped": "vector_store_unavailable"}

    book = book_id or profile.book_id
    profile_id = profile.profile_id
    config = load_rag_config()
    repo = StyleReferenceRepository(session)
    paragraphs = repo.list_paragraphs(book)

    # Remove the completion marker before any destructive reset. A failed write
    # can therefore never leave a partial index looking healthy on the next apply.
    store.delete_collection(rag_manifest_collection_name(profile_id))

    min_sent = int(config["rag_min_sentence_chars"])
    min_para = int(config["rag_min_paragraph_chars"])

    sentence_docs: list[dict[str, Any]] = []
    paragraph_docs: list[dict[str, Any]] = []
    total_paragraphs = len(paragraphs)
    if progress is not None:
        progress("signatures", 0, total_paragraphs)
    for index, para in enumerate(paragraphs, start=1):
        if progress is not None and (
            index % RAG_PROGRESS_EVERY_PARAGRAPHS == 0 or index == total_paragraphs
        ):
            progress("signatures", index, total_paragraphs)
        ptext = (getattr(para, "text", "") or "").strip()
        if not ptext:
            continue
        pid = getattr(para, "paragraph_id", "")
        ptype = getattr(para, "paragraph_type", None)
        pindex = getattr(para, "paragraph_index", 0)
        if len(ptext) >= min_para:
            paragraph_docs.append(
                _style_index_document(
                    doc_id=f"p_{pid}",
                    source_text=ptext,
                    granularity="paragraph",
                    paragraph_type=ptype,
                    paragraph_index=pindex,
                )
            )
        for si, sent in enumerate(split_sentences(ptext, min_chars=min_sent)):
            sentence_docs.append(
                _style_index_document(
                    doc_id=f"s_{pid}_{si}",
                    source_text=sent,
                    granularity="sentence",
                    paragraph_type=ptype,
                    paragraph_index=pindex,
                )
            )

    scene_blocks = aggregate_scenes(
        paragraphs, target_chars=int(config["rag_scene_target_chars"])
    )
    scene_docs: list[dict[str, Any]] = [
        _style_index_document(
            doc_id=f"sc_{book}_{blk['paragraph_index']}_{i}",
            source_text=blk["text"],
            granularity="scene",
            paragraph_type=blk["paragraph_type"],
            paragraph_index=blk["paragraph_index"],
        )
        for i, blk in enumerate(scene_blocks)
    ]

    docs_by_gran = {
        "sentence": sentence_docs,
        "paragraph": paragraph_docs,
        "scene": scene_docs,
    }
    counts: dict[str, Any] = {}
    if progress is not None:
        progress("write", 0, len(docs_by_gran))
    for written, (gran, docs) in enumerate(docs_by_gran.items(), start=1):
        store.write_collection(rag_collection_name(profile_id, gran), docs)
        counts[gran] = len(docs)
        if progress is not None:
            progress("write", written, len(docs_by_gran))
    counts["profile_id"] = profile_id
    counts["signature_version"] = STYLE_SIGNATURE_VERSION
    store.write_collection(
        rag_manifest_collection_name(profile_id),
        [
            {
                "id": "manifest",
                "text": STYLE_SIGNATURE_VERSION,
                "profile_id": profile_id,
                "signature_version": STYLE_SIGNATURE_VERSION,
                "counts_json": json.dumps(counts, ensure_ascii=False, sort_keys=True),
            }
        ],
    )
    return counts


def ensure_rag_index(
    session: Any,
    profile: Any,
    *,
    book_id: str | None = None,
    vector_store: VectorStore | None = None,
    progress: RagProgress | None = None,
) -> dict[str, Any]:
    """Ensure a complete v2 index exists without rebuilding healthy collections.

    The version is part of the collection name and a completion marker is written
    only after all three collections succeed. Missing or partial collections
    trigger one idempotent full rebuild. Builds of the same profile are serialized
    (2026-09-15): a caller arriving while the background build runs waits for it
    and then reads ``ready`` instead of starting a second build.
    """
    store = _resolve_store(vector_store)
    if store is None:
        return {"skipped": "vector_store_unavailable"}
    profile_id = str(profile.profile_id)
    with _profile_build_lock(profile_id):
        return _ensure_rag_index_locked(
            session,
            profile,
            store=store,
            profile_id=profile_id,
            book_id=book_id,
            progress=progress,
        )


def _ensure_rag_index_locked(
    session: Any,
    profile: Any,
    *,
    store: VectorStore,
    profile_id: str,
    book_id: str | None,
    progress: RagProgress | None,
) -> dict[str, Any]:
    try:
        collections_ready = all(
            store.collection_exists(rag_collection_name(profile_id, granularity))
            for granularity in GRANULARITIES
        )
        manifest_rows = store.load_collection(rag_manifest_collection_name(profile_id))
        manifest_ready = bool(
            manifest_rows
            and manifest_rows[0].get("profile_id") == profile_id
            and manifest_rows[0].get("signature_version") == STYLE_SIGNATURE_VERSION
        )
        ready = collections_ready and manifest_ready
    except Exception:  # noqa: BLE001 — readiness probe must not block apply
        logger.warning(
            "rag index readiness probe failed for profile %s",
            profile_id,
            exc_info=True,
        )
        ready = False
    if ready:
        return {
            "profile_id": profile_id,
            "status": "ready",
            "signature_version": STYLE_SIGNATURE_VERSION,
        }
    rebuilt = build_rag_index(
        session,
        profile,
        book_id=book_id,
        vector_store=store,
        progress=progress,
    )
    return {**rebuilt, "status": "rebuilt"}


# ---------------------------------------------------------------------------
# apply 后台建索引(2026-09-15):索引构建不再占着 apply / 收件箱 resolve 的写事务
# ---------------------------------------------------------------------------


def rag_index_operation_key(profile_id: str) -> str:
    return f"rag_index:{profile_id}"


def start_style_reference_rag_index_worker(
    *,
    profile_id: str,
    book_id: str | None,
) -> None:
    """提交一次后台 ``ensure_rag_index``(幂等;已就绪的索引立刻返回 ready)。"""

    global _RAG_EXECUTOR
    with _RAG_EXECUTOR_LOCK:
        if _RAG_EXECUTOR is None:
            _RAG_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sr_rag_index")
        _RAG_EXECUTOR.submit(_rag_index_worker, profile_id=str(profile_id), book_id=book_id)


def shutdown_style_reference_rag_index_executor(*, wait: bool = False) -> None:
    global _RAG_EXECUTOR
    with _RAG_EXECUTOR_LOCK:
        executor = _RAG_EXECUTOR
        _RAG_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=wait, cancel_futures=False)


def _rag_index_worker(*, profile_id: str, book_id: str | None) -> None:
    from novel_system.db.session import SessionLocal
    from novel_system.services.style_reference.import_progress import start_import_progress

    progress = None
    try:
        with SessionLocal() as session:
            repo = StyleReferenceRepository(session)
            profile = repo.get_profile(profile_id)
            if profile is None:
                logger.warning("rag index worker: profile %s disappeared", profile_id)
                return
            resolved_book_id = book_id or profile.book_id
            book = repo.get_book(resolved_book_id) if resolved_book_id else None
            progress = start_import_progress(
                rag_index_operation_key(profile_id),
                kind="rag_index",
                title=book.title if book is not None else None,
                source="apply",
                book_id=resolved_book_id,
                target_id=profile_id,
            )
            progress.phase("signatures")

            def report(stage: str, done: int, total: int) -> None:
                if stage == "write":
                    progress.phase("write")
                progress.set_steps(int(done), int(total), label="段" if stage == "signatures" else "粒度")

            result = ensure_rag_index(
                session,
                profile,
                book_id=resolved_book_id,
                progress=report,
            )
            progress.succeed(result={k: v for k, v in result.items() if k != "profile_id"})
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("background rag index build failed for profile %s", profile_id)
        if progress is not None:
            progress.fail(
                code=str(getattr(exc, "code", None) or exc.__class__.__name__),
                message=str(exc),
            )


def build_query_signatures(
    paragraph_texts: Sequence[str],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, StyleSignature]:
    """画像代表签名:对一组代表段落按三粒度取 :func:`extract_style_signature` 的**均值**。

    v2(W4.7)Strategy C 的默认 query 视图——中性稿不代表目标风格,用画像自己的样例
    段落(或冻结引用的段落)的签名均值做 query,再由 :class:`RagRetriever` 以
    ``query_signatures`` 消费。sentence 粒度对每段切句后逐句取签名;paragraph 逐段;
    scene 按 ``rag_scene_target_chars`` 聚合连续段落。无可用文本时返回空 dict。
    纯函数、无 LLM、确定性。
    """
    cfg = dict(config) if config is not None else load_rag_config()
    texts = [str(text or "").strip() for text in paragraph_texts]
    texts = [text for text in texts if text]
    if not texts:
        return {}
    min_sent = int(cfg.get("rag_min_sentence_chars", 8))
    units: dict[str, list[str]] = {
        "sentence": [
            sentence
            for text in texts
            for sentence in split_sentences(text, min_chars=min_sent)
        ],
        "paragraph": list(texts),
        "scene": [
            str(block["text"])
            for block in aggregate_scenes(
                [
                    SimpleNamespace(text=text, paragraph_type="", paragraph_index=index)
                    for index, text in enumerate(texts)
                ],
                target_chars=int(cfg.get("rag_scene_target_chars", 600)),
            )
            if str(block.get("text") or "").strip()
        ],
    }
    out: dict[str, StyleSignature] = {}
    for gran in GRANULARITIES:
        samples = units.get(gran) or []
        if not samples:
            # 该粒度切不出单位(如全是短段切不出句)时退回整段
            samples = list(texts)
        signatures = [extract_style_signature(sample, granularity=gran) for sample in samples]
        if not signatures:
            continue
        features = {
            name: _clip01(
                sum(float(sig.features.get(name, 0.0)) for sig in signatures)
                / len(signatures)
            )
            for name in FEATURE_NAMES
        }
        out[gran] = StyleSignature(granularity=gran, features=features)
    return out


def _clip01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(1.0, max(0.0, value))


def _style_index_document(
    *,
    doc_id: str,
    source_text: str,
    granularity: str,
    paragraph_type: str | None,
    paragraph_index: int,
) -> dict[str, Any]:
    signature = extract_style_signature(source_text, granularity=granularity)
    return {
        "id": doc_id,
        # Vector backends index only the encoded style bins.  Topic/content words
        # are held separately and cannot contribute positive retrieval similarity.
        "text": signature.search_text(),
        "source_text": source_text,
        "style_signature_json": signature.to_json(),
        "signature_version": STYLE_SIGNATURE_VERSION,
        "granularity": granularity,
        "paragraph_type": paragraph_type,
        "paragraph_index": paragraph_index,
    }


def delete_rag_index(
    profile_id: str,
    *,
    vector_store: VectorStore | None = None,
) -> dict[str, Any]:
    """删除 profile 的三粒度 collection(purge_derived_data 调用)。容错。"""
    store = _resolve_store(vector_store)
    if store is None:
        return {"skipped": "vector_store_unavailable"}
    deleted = []
    for gran in GRANULARITIES:
        names = (
            rag_collection_name(profile_id, gran),
            f"style_ref_rag_{profile_id}_{gran}",  # v1 原文向量索引清理
        )
        removed = False
        for name in names:
            try:
                existed = store.collection_exists(name)
                store.delete_collection(name)
                removed = removed or existed
            except Exception:  # noqa: BLE001 — 删除是尽力而为
                logger.warning("failed deleting rag collection %s", name)
        if removed:
            deleted.append(gran)
    try:
        store.delete_collection(rag_manifest_collection_name(profile_id))
    except Exception:  # noqa: BLE001 — 删除是尽力而为
        logger.warning("failed deleting rag manifest for %s", profile_id)
    return {"deleted": deleted, "profile_id": profile_id}


# ---------------------------------------------------------------------------
# 召回 + 确定性 rerank
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RagSnippet:
    snippet_id: str
    text: str
    granularity: str
    paragraph_type: str | None
    score: float
    style_score: float = 0.0
    content_overlap: float = 0.0
    retrieval_version: str = STYLE_SIGNATURE_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.snippet_id,
            "text": self.text,
            "granularity": self.granularity,
            "paragraph_type": self.paragraph_type,
            "score": round(self.score, 6),
            "style_score": round(self.style_score, 6),
            "content_overlap": round(self.content_overlap, 6),
            "retrieval_version": self.retrieval_version,
        }


def legacy_content_coverage_score(snippet_text: str, query_chars: set[str]) -> float:
    """query 字符覆盖率 ∈ [0,1]:|set(query) ∩ set(snippet)| / |set(query)|。

    对长文本做了天然归一(分母只与 query 有关),避免 scene 块仅因更长而霸榜;
    跨粒度可比,纯函数确定性。
    """
    if not query_chars:
        return 0.0
    snippet_chars = set(snippet_text)
    return len(query_chars & snippet_chars) / len(query_chars)


def content_restrained_style_score(
    query_text: str,
    snippet_text: str,
    *,
    granularity: str,
    config: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Pure treatment score used by production retrieval and frozen A/B evaluation."""
    cfg = config or load_rag_config()
    query_signature = extract_style_signature(query_text, granularity=granularity)
    snippet_signature = extract_style_signature(snippet_text, granularity=granularity)
    style_score = style_signature_similarity(query_signature, snippet_signature)
    overlap = content_shingle_overlap(query_text, snippet_text)
    final_score = _apply_content_penalty(
        style_score,
        overlap,
        granularity=granularity,
        config=cfg,
    )
    return {
        "style_score": style_score,
        "content_overlap": overlap,
        "final_score": final_score,
    }


def _apply_content_penalty(
    style_score: float,
    content_overlap: float,
    *,
    granularity: str,
    config: dict[str, Any],
) -> float:
    threshold = float(config["rag_content_overlap_penalty_threshold"])
    max_penalty = float(config["rag_content_overlap_penalty_max"])
    if content_overlap <= threshold:
        penalty = 0.0
    else:
        normalized = (content_overlap - threshold) / max(1e-9, 1.0 - threshold)
        penalty = min(max_penalty, max_penalty * normalized)
    # Granularity is only a small completeness preference; raw content overlap
    # can reduce a score but can never increase it.
    weights = _granularity_weights(config)
    weighted = style_score * weights.get(granularity, 1.0) * (1.0 - penalty)
    return min(1.0, max(0.0, weighted))


class RagRetriever:
    """三粒度内容克制风格召回 + 确定性 rerank。无 LLM。

    v2(W4.7)query 构造:``query_signatures``(粒度 → :class:`StyleSignature`,通常是
    :func:`build_query_signatures` 算出的画像代表签名均值)显式给出时,该粒度不再从
    ``query_text`` 抽签名——``query_text`` 可以为空,内容重叠惩罚也随之为 0;
    ``preferred_paragraph_types`` 是场景段型**软过滤**:命中段型的片段排在前面,没有
    命中时不丢弃任何片段。二者都走构造函数,``retrieve(profile_id, query_text)`` 的
    位置参数契约不变。
    """

    def __init__(
        self,
        session: Any,
        *,
        vector_store: VectorStore | None = None,
        query_signatures: Mapping[str, StyleSignature] | None = None,
        preferred_paragraph_types: Iterable[str] | None = None,
    ):
        self.session = session
        self._store = vector_store
        self._config = load_rag_config()
        self._query_signatures: dict[str, StyleSignature] = {
            str(gran): sig
            for gran, sig in dict(query_signatures or {}).items()
            if isinstance(sig, StyleSignature)
        }
        self._preferred_types: frozenset[str] = frozenset(
            str(ptype) for ptype in (preferred_paragraph_types or []) if str(ptype)
        )

    def _prefer(self, snippets: list["RagSnippet"]) -> list["RagSnippet"]:
        """场景段型软过滤:命中 preferred 段型的片段稳定前置;无命中 / 无偏好时原序。"""
        if not self._preferred_types:
            return snippets
        matched = [
            s for s in snippets if str(s.paragraph_type or "") in self._preferred_types
        ]
        if not matched:
            return snippets
        others = [
            s for s in snippets if str(s.paragraph_type or "") not in self._preferred_types
        ]
        return [*matched, *others]

    # -- 评测/调试:按粒度分别返回(供 hit@k 独立测量) --------------------
    def retrieve_per_granularity(
        self, profile_id: str, query_text: str, *, top_k: int | None = None
    ) -> dict[str, list[RagSnippet]]:
        store = _resolve_store(self._store)
        has_text = bool((query_text or "").strip())
        if store is None or (not has_text and not self._query_signatures):
            return {gran: [] for gran in GRANULARITIES}
        k = int(top_k if top_k is not None else self._config["rag_top_k"])
        out: dict[str, list[RagSnippet]] = {}
        for gran in GRANULARITIES:
            name = rag_collection_name(profile_id, gran)
            query_view = (
                query_view_for_granularity(
                    query_text,
                    gran,
                    paragraph_chars=int(self._config["rag_paragraph_query_chars"]),
                    scene_chars=int(self._config["rag_scene_target_chars"]),
                )
                if has_text
                else ""
            )
            preset = self._query_signatures.get(gran)
            if preset is not None:
                # 画像代表签名:query 视图只用于内容重叠惩罚(无文本时惩罚为 0)
                query_signature = preset
            elif has_text:
                query_signature = extract_style_signature(query_view, granularity=gran)
            else:
                out[gran] = []
                continue
            pool_size = max(
                k * int(self._config["rag_signature_candidate_pool_multiplier"]),
                int(self._config["rag_signature_candidate_pool_min"]),
            )
            hits = self._query_collection(
                store, name, query_signature.search_text(), pool_size
            )
            snippets: list[RagSnippet] = []
            for hit in hits:
                signature = parse_style_signature(hit.get("style_signature_json", ""))
                source_text = str(hit.get("source_text", "") or "")
                if (
                    signature is None
                    or signature.granularity != gran
                    or not source_text
                ):
                    # v1/raw-text documents are deliberately not a fallback: using
                    # them would silently reintroduce topic matching after upgrade.
                    continue
                style_score = style_signature_similarity(query_signature, signature)
                overlap = content_shingle_overlap(query_view, source_text)
                final_score = _apply_content_penalty(
                    style_score,
                    overlap,
                    granularity=gran,
                    config=self._config,
                )
                if final_score < float(self._config["rag_style_min_score"]):
                    continue
                snippets.append(
                    RagSnippet(
                        snippet_id=str(hit.get("id", "")),
                        text=source_text,
                        granularity=gran,
                        paragraph_type=hit.get("paragraph_type"),
                        score=final_score,
                        style_score=style_score,
                        content_overlap=overlap,
                    )
                )
            snippets.sort(key=lambda s: (-s.score, s.snippet_id))
            out[gran] = self._prefer(snippets)[: max(0, k)]
        return out

    # -- inject 热路径:合并三粒度 → 全局 rerank → 截断 --------------------
    def retrieve(
        self, profile_id: str, query_text: str, *, inject_max: int | None = None
    ) -> list[RagSnippet]:
        per_gran = self.retrieve_per_granularity(profile_id, query_text)
        merged: list[RagSnippet] = [s for snips in per_gran.values() for s in snips]
        # 去重(同一段落可能在 sentence/paragraph/scene 重复命中):按文本去重,保留高分。
        # tiebreaker 用粒度 rank(由细到粗)而非粒度字符串字母序,再 snippet_id 兜底确定性。
        merged.sort(
            key=lambda s: (-s.score, _GRAN_RANK.get(s.granularity, 99), s.snippet_id)
        )
        seen_texts: set[str] = set()
        deduped: list[RagSnippet] = []
        for s in merged:
            key = s.text.strip()
            if not key or key in seen_texts:
                continue
            seen_texts.add(key)
            deduped.append(s)
        limit = int(
            inject_max if inject_max is not None else self._config["rag_inject_max"]
        )
        return self._prefer(deduped)[: max(0, limit)]

    def _query_collection(
        self, store: VectorStore, name: str, query_text: str, top_k: int
    ) -> list[dict[str, Any]]:
        try:
            if not store.collection_exists(name):
                return []
            return store.query(name, query_text, top_k=top_k)
        except Exception:  # noqa: BLE001 — 召回失败不阻断生成
            logger.warning("rag query failed for collection %s", name, exc_info=True)
            return []


# ---------------------------------------------------------------------------
# 注入 block 渲染(由 injection._render C 分支调用)
# ---------------------------------------------------------------------------

_GRAN_LABEL = {"sentence": "句", "paragraph": "段", "scene": "景"}


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1] + "…"


def render_rag_block(
    snippets: list[RagSnippet], *, config: dict[str, Any] | None = None
) -> str:
    """把召回样例渲染为 [风格检索样例] block(供 system_prompt 注入)。

    样例引用参考书原文,故调用方保证红线段必随注;此处按 budget 截断每条与整块,
    避免注入大段原文(反抄袭事前预防)。无样例返回空串(C 分支据此优雅退化)。
    """
    if not snippets:
        return ""
    cfg = config or load_rag_config()
    quote_max = int(cfg["rag_quote_max_chars"])
    block_max = int(cfg["rag_block_max_chars"])
    lines = [
        "[风格检索样例](按当前上下文检索的参考风格片段;体会句式与肌理;严禁照抄或微改其中任何句子)"
    ]
    for s in snippets:
        label = _GRAN_LABEL.get(s.granularity, "")
        text = _truncate(s.text.strip(), quote_max)
        if not text:
            continue
        lines.append(f"-（{label}）「{text}」")
    if len(lines) == 1:
        return ""
    block = "\n".join(lines)
    if len(block) <= block_max:
        return block
    # 行边界截断:保头部完整行
    cut = block.rfind("\n", 0, block_max)
    return block[:cut] if cut > block_max // 2 else _truncate(block, block_max)
