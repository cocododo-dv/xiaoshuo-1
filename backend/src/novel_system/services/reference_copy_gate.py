"""风格参考 v3（2026-09-23）— 唯一抄袭门（台账 V1 / E7 / V8）。

凡是要进正文（终稿、归档、成稿中心提升、写作台采纳 AI 建议 / 局部改写）的文字都过这一道门：

1. **原文重合**（硬门，``blocked``）：规范化（去空白 / 标点 / 符号、小写）后与绑定的参考书段落连续
   ``THRESHOLD_CHARS``（12）字以上相同即命中——判定与 ``style_reference.validation.plagiarism.check_plagiarism(
   ngram_size=8, threshold_chars=12)`` 等价（命中区间 = 所有命中 12 字元的并集）。每本书在进程里建一次 12 字元
   哈希索引（按书与段落指纹缓存），之后一次检查是毫秒级；哈希命中再用规范化全文逐字复核，碰撞不会误拦。
   这是唯一能拦下正文的一条：每条路径上都是 Q0，没有豁免。
2. **受保护专名**（只提示，``protected_hits``）：画像**现行**的生成期禁用词（``style_reference_banned_terms``，
   ``scope="generation"``；学习作业写的 ``source="protected_auto"`` 专名也在里面）加上环境变量
   ``NOVEL_SYSTEM_PROTECTED_SOURCE_TERMS_JSON`` 的全局词。专名表是模型认的，难免把日常词收进去；命中**从不**拦下
   归档 / 采纳 / 提升——成稿门把它报成不拦的警告（``source_safety:protected_term``），管线在软 QC 里要作者复核
   （作者可以接受）。只比对现读的表：作者删掉一个误收的词，所有检查立刻不再认它；冻结契约里的禁用词只用来渲染
   提示词的红线（注入包），不参与任何门的判定。

书查不到（绑定的书已删）或策略解析降级时，这一边**没有查成**：``unavailable`` 为真、``missing_books`` /
``unavailable_reasons`` 说明缘故，调用方按「检查没跑」处理（管线挂 Q2 复核，成稿门报不拦的
``source_safety:unavailable`` 警告），不能把它当成「查过、没问题」。

命中只记哈希与位置（位置指向被检查的这段文字，即作者自己的正文），**从不记参考原文**。同一段文字对同一组
书与词只扫一次（按文本 sha256 缓存），一次运行里硬 QC、风格稿、软 QC、准定稿、归档反复检查同一稿不再各扫一遍。

以前的「来源安全」扫描（``ReferenceSafetyService.scan_runtime_text``）读的是画像里一个现有画像都没有的
``profile_json.source_safety`` 键，从来没拿绑定的书比对过——参考书连续 60 字照抄也判「安全」。
"""

from __future__ import annotations

import array
import bisect
import dataclasses
import hashlib
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceBannedTerm,
    StyleReferenceBook,
    StyleReferenceParagraph,
    StyleReferenceProfile,
)
from novel_system.services.author_actions import author_action
from novel_system.services.source_safety import (
    configured_protected_source_terms,
    find_protected_term_spans,
    normalize_for_term_match,
)
from novel_system.services.style_reference.validation.plagiarism import (
    normalize_text_for_matching,
    normalize_with_offsets,
)

COPY_GATE_VERSION = "reference_copy_gate_v1"
THRESHOLD_CHARS = 12
MAX_REPORTED_HITS = 20
MAX_WARNING_TERMS = 8
BLOCK_ISSUE_KEY = "reference_copy"
ENV_TERM_SOURCE = "environment"
# 成稿门的两条不拦警告（issue_key）：用了受保护专名 / 有一边没查成（书已删、策略降级）
PROTECTED_TERM_WARNING_KEY = "source_safety:protected_term"
UNAVAILABLE_WARNING_KEY = "source_safety:unavailable"

_INDEX_CACHE_MAX = 4
_RESULT_CACHE_MAX = 256
_INDEX_CACHE: "OrderedDict[tuple[str, tuple[Any, ...]], _BookCopyIndex]" = OrderedDict()
_RESULT_CACHE: "OrderedDict[tuple[Any, ...], CopyCheck]" = OrderedDict()
_LOCK = threading.Lock()


@dataclass(frozen=True)
class CopyHit:
    """与参考书原文连续相同的一段（``start`` / ``end`` 是被检查文字里的下标，end 不含）。"""

    start: int
    end: int
    matched_chars: int
    sha256: str
    book_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "matched_chars": self.matched_chars,
            "sha256": self.sha256,
            "book_id": self.book_id,
        }


@dataclass(frozen=True)
class ProtectedHit:
    """受保护专名在被检查文字里的一处出现。

    ``term`` 是专名表里的那个词（成稿门的警告要说出是哪几个词——它们是作者自己正文里的字、也列在画像的禁用词表里
    给作者看，不是参考原文）；检查记录（:meth:`as_dict` / :meth:`CopyCheck.audit`）照旧只记它的哈希与位置。"""

    start: int
    end: int
    term_sha256: str
    source: str
    term: str = field(default="", compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "term_sha256": self.term_sha256, "source": self.source}


@dataclass(frozen=True)
class CopyCheck:
    """一次检查的结果。``blocked`` 只看原文重合（硬门）；受保护专名只在 ``protected_hits`` 里报（不拦）；
    ``unavailable`` 为真时有一边没有查成（书已删 / 策略降级），调用方不得当作「查过、没问题」。"""

    blocked: bool
    hits: tuple[CopyHit, ...] = ()
    protected_hits: tuple[ProtectedHit, ...] = ()
    checked_books: tuple[str, ...] = ()
    profile_ids: tuple[str, ...] = ()
    text_sha256: str = ""
    terms_checked: int = 0
    policy_mode: str | None = None
    version: str = COPY_GATE_VERSION
    extra: Mapping[str, Any] = field(default_factory=dict)
    missing_books: tuple[str, ...] = ()
    unavailable_reasons: tuple[str, ...] = ()

    @property
    def safe(self) -> bool:
        return not self.blocked

    @property
    def unavailable(self) -> bool:
        return bool(self.missing_books or self.unavailable_reasons)

    def protected_terms(self) -> list[str]:
        """命中的受保护专名（去重、按第一次出现的位置）。"""
        return list(dict.fromkeys(hit.term for hit in self.protected_hits if hit.term))

    def audit(self) -> dict[str, Any]:
        """JSON 友好的检查记录（哈希与位置，无原文、不写专名本身）；``safe`` 键沿用旧扫描载荷的口径（只看原文重合）。"""
        return {
            "version": self.version,
            "safe": not self.blocked,
            "blocked": self.blocked,
            "text_sha256": self.text_sha256,
            "threshold_chars": THRESHOLD_CHARS,
            "checked_books": list(self.checked_books),
            "profile_ids": list(self.profile_ids),
            "policy_mode": self.policy_mode,
            "terms_checked": self.terms_checked,
            "hit_count": len(self.hits),
            "hits": [hit.as_dict() for hit in self.hits[:MAX_REPORTED_HITS]],
            "protected_hit_count": len(self.protected_hits),
            "protected_hits": [hit.as_dict() for hit in self.protected_hits[:MAX_REPORTED_HITS]],
            # 受保护专名只提示、不拦（见模块说明）
            "protected_terms_block": False,
            "unavailable": self.unavailable,
            "unavailable_reasons": list(self.unavailable_reasons),
            "missing_books": list(self.missing_books),
            **dict(self.extra),
        }


def _sha(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


class _BookCopyIndex:
    """一本书段落的 12 字元哈希索引（有序数组 + 二分）+ 规范化全文（逐字复核用）。"""

    __slots__ = ("book_id", "hashes", "joined", "paragraph_count")

    def __init__(self, book_id: str, texts: Iterable[str]) -> None:
        self.book_id = book_id
        norms = [normalize_text_for_matching(text) for text in texts if text]
        width = THRESHOLD_CHARS
        raw = array.array("q")
        append = raw.append
        for norm in norms:
            for index in range(len(norm) - width + 1):
                append(hash(norm[index : index + width]))
        self.hashes = array.array("q", sorted(raw))
        # 段落之间用换行分隔：规范化文本里不会有空白，逐字复核不会跨段拼出假命中
        self.joined = "\n".join(norms)
        self.paragraph_count = len(norms)

    def contains(self, gram: str) -> bool:
        value = hash(gram)
        position = bisect.bisect_left(self.hashes, value)
        if position >= len(self.hashes) or self.hashes[position] != value:
            return False
        return gram in self.joined


def _book_fingerprint(session: Session, book_id: str) -> tuple[Any, ...] | None:
    """段落表的廉价指纹：统计里记着的根哈希（有就用）+ 段数 / 总字数 / 最新段落时间 + 书的校验和与建书时间
    （同一个书号删了重导入也认得出来）。"""
    book = session.get(StyleReferenceBook, book_id)
    if book is None:
        return None
    stats = book.stats_json if isinstance(book.stats_json, dict) else {}
    count, total, latest = session.execute(
        select(
            func.count(StyleReferenceParagraph.paragraph_id),
            func.coalesce(func.sum(func.length(StyleReferenceParagraph.text)), 0),
            func.max(StyleReferenceParagraph.created_at),
        ).where(StyleReferenceParagraph.book_id == book_id)
    ).one()
    return (
        str(stats.get("paragraph_root_sha256") or ""),
        int(count or 0),
        int(total or 0),
        str(latest or ""),
        str(book.text_checksum or ""),
        str(book.created_at or ""),
    )


def _book_index(session: Session, book_id: str, fingerprint: tuple[Any, ...]) -> _BookCopyIndex:
    key = (book_id, fingerprint)
    with _LOCK:
        cached = _INDEX_CACHE.get(key)
        if cached is not None:
            _INDEX_CACHE.move_to_end(key)
            return cached
    texts = session.execute(
        select(StyleReferenceParagraph.text)
        .where(StyleReferenceParagraph.book_id == book_id)
        .order_by(StyleReferenceParagraph.paragraph_index.asc())
    ).scalars().all()
    index = _BookCopyIndex(book_id, (str(text or "") for text in texts))
    with _LOCK:
        for stale in [existing for existing in _INDEX_CACHE if existing[0] == book_id]:
            _INDEX_CACHE.pop(stale, None)
        _INDEX_CACHE[key] = index
        while len(_INDEX_CACHE) > _INDEX_CACHE_MAX:
            _INDEX_CACHE.popitem(last=False)
    return index


def _copy_hits(text: str, indexes: list[_BookCopyIndex]) -> list[CopyHit]:
    """被检查文字里所有与某本书连续 ≥12（规范化）字相同的区间，按书合并、映射回原文下标。"""
    normalized, offsets = normalize_with_offsets(text)
    width = THRESHOLD_CHARS
    if len(normalized) < width or not indexes:
        return []
    hits: list[CopyHit] = []
    for index in indexes:
        intervals: list[list[int]] = []
        for start in range(len(normalized) - width + 1):
            if not index.contains(normalized[start : start + width]):
                continue
            if intervals and start <= intervals[-1][1]:
                intervals[-1][1] = start + width
            else:
                intervals.append([start, start + width])
        for norm_start, norm_end in intervals:
            hits.append(
                CopyHit(
                    start=offsets[norm_start],
                    end=offsets[norm_end - 1] + 1,
                    matched_chars=norm_end - norm_start,
                    sha256=_sha(normalized[norm_start:norm_end]),
                    book_id=index.book_id,
                )
            )
    hits.sort(key=lambda hit: (hit.start, hit.end, hit.book_id))
    return hits


def _policy_books_and_profiles(policy: Any) -> tuple[list[str], list[str]]:
    books: list[str] = []
    profiles: list[str] = []
    contract = getattr(policy, "contract", None)
    layers = contract.get("layers") if isinstance(contract, Mapping) else None
    for layer in layers if isinstance(layers, list) else []:
        if not isinstance(layer, Mapping):
            continue
        book = layer.get("book") if isinstance(layer.get("book"), Mapping) else {}
        profile = layer.get("profile") if isinstance(layer.get("profile"), Mapping) else {}
        book_id = str(book.get("book_id") or profile.get("book_id") or "")
        profile_id = str(profile.get("profile_id") or "")
        if book_id and book_id not in books:
            books.append(book_id)
        if profile_id and profile_id not in profiles:
            profiles.append(profile_id)
    book_id = str(getattr(policy, "book_id", None) or "")
    profile_id = str(getattr(policy, "profile_id", None) or "")
    if book_id and book_id not in books:
        books.append(book_id)
    if profile_id and profile_id not in profiles:
        profiles.append(profile_id)
    return books, profiles


def _policy_unavailable_reason(policy: Any) -> str | None:
    """策略解析降级（契约损坏、现解析失败）：这一边的绑定没法查——报原因，不当作没有绑定。"""
    if policy is None or getattr(policy, "bound", False):
        return None
    if str(getattr(policy, "mode", "") or "") != "degraded":
        return None
    return str(getattr(policy, "error_code", None) or "style_policy_degraded")


def _protected_terms(session: Session, profile_ids: list[str], book_ids: list[str]) -> list[tuple[str, str]]:
    """(词, 来源)：画像**现行**的生成期禁用词（含学习作业写的 protected_auto）+ 环境变量全局词。

    只读现在的表：冻结契约里记下的禁用词不参与判定（它们只给提示词渲染红线用），作者删掉一个误收的词，
    所有检查立刻不再认它。"""
    terms: dict[str, str] = {}
    rows: list[Any] = []
    if profile_ids:
        rows = list(
            session.execute(
                select(StyleReferenceBannedTerm.term, StyleReferenceBannedTerm.source).where(
                    StyleReferenceBannedTerm.profile_id.in_(profile_ids),
                    StyleReferenceBannedTerm.scope == "generation",
                )
            ).all()
        )
    elif book_ids:
        rows = list(
            session.execute(
                select(StyleReferenceBannedTerm.term, StyleReferenceBannedTerm.source)
                .join(StyleReferenceProfile, StyleReferenceProfile.profile_id == StyleReferenceBannedTerm.profile_id)
                .where(
                    StyleReferenceProfile.book_id.in_(book_ids),
                    StyleReferenceBannedTerm.scope == "generation",
                )
            ).all()
        )
    for term, source in rows:
        text = str(term or "").strip()
        if text:
            terms.setdefault(text, str(source or "manual"))
    for text in configured_protected_source_terms():
        terms.setdefault(str(text).strip(), ENV_TERM_SOURCE)
    return sorted((term, source) for term, source in terms.items() if term)


def check_reference_copy(
    session: Session,
    text: str,
    *,
    policy: Any = None,
    book_ids: Iterable[str] | None = None,
    extra_policies: Iterable[Any] = (),
    profile_ids: Iterable[str] | None = None,
) -> CopyCheck:
    """``text`` 能不能进正文：与绑定的书连续 ≥12 字相同 → ``blocked``（唯一的硬门）；受保护专名只报在
    ``protected_hits`` 里（不拦，见模块说明）。

    书的来源：显式 ``book_ids``；否则 ``policy``（:class:`~novel_system.services.style_policy.StylePolicy`）
    与 ``extra_policies`` 绑定的书的并集（多层旧契约取每层的书）。受保护专名：显式 ``profile_ids`` 的，否则同上
    各策略画像的（都没有画像时取这些书全部画像的），一律读现行的禁用词表。都没有时只查环境变量里的全局专名。
    书已删、策略降级（``mode="degraded"``）→ 结果的 ``unavailable`` 为真（这一边没有查成）。检查失败（库读不出等）
    直接抛出——调用方按自己的语义 fail-closed。
    """
    content = str(text or "")
    text_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    books: list[str] = []
    profiles: list[str] = []
    unavailable_reasons: list[str] = []
    for item in [policy, *list(extra_policies or ())]:
        if item is None:
            continue
        reason = _policy_unavailable_reason(item)
        if reason is not None:
            unavailable_reasons.append(reason)
            continue
        item_books, item_profiles = _policy_books_and_profiles(item)
        books.extend(item_books)
        profiles.extend(item_profiles)
    if book_ids is not None:
        books = [str(book_id) for book_id in book_ids if str(book_id or "").strip()]
    if profile_ids is not None:
        profiles = [str(profile_id) for profile_id in profile_ids if str(profile_id or "").strip()]
    books = list(dict.fromkeys(books))
    profiles = list(dict.fromkeys(profiles))
    unavailable_reasons = list(dict.fromkeys(unavailable_reasons))
    terms = _protected_terms(session, profiles, books)
    fingerprints: list[tuple[str, tuple[Any, ...]]] = []
    missing_books: list[str] = []
    for book_id in books:
        fingerprint = _book_fingerprint(session, book_id)
        if fingerprint is not None:
            fingerprints.append((book_id, fingerprint))
        else:
            # 绑定的书已不在书库：这一本没有查成（不是「查过、没重合」）
            missing_books.append(book_id)
    policy_mode = getattr(policy, "mode", None) if policy is not None else None
    terms_digest = _sha("\x1f".join(f"{term}\x1e{source}" for term, source in terms), 32)
    cache_key = (
        tuple(fingerprints),
        terms_digest,
        text_sha256,
        tuple(profiles),
        policy_mode,
        tuple(missing_books),
        tuple(unavailable_reasons),
    )
    with _LOCK:
        cached = _RESULT_CACHE.get(cache_key)
        if cached is not None:
            _RESULT_CACHE.move_to_end(cache_key)
            return cached

    hits: list[CopyHit] = []
    if content.strip() and fingerprints:
        indexes = [_book_index(session, book_id, fingerprint) for book_id, fingerprint in fingerprints]
        hits = _copy_hits(content, indexes)
    protected: list[ProtectedHit] = []
    if content.strip() and terms:
        sources = dict(terms)
        for term, start, end in find_protected_term_spans(content, [term for term, _source in terms]):
            protected.append(
                ProtectedHit(
                    start=start,
                    end=end,
                    term_sha256=_sha(term),
                    source=sources.get(term, "manual"),
                    term=term,
                )
            )
    result = CopyCheck(
        blocked=bool(hits),
        hits=tuple(hits),
        protected_hits=tuple(protected),
        checked_books=tuple(book_id for book_id, _fingerprint in fingerprints),
        profile_ids=tuple(profiles),
        text_sha256=text_sha256,
        terms_checked=len(terms),
        policy_mode=policy_mode,
        missing_books=tuple(missing_books),
        unavailable_reasons=tuple(unavailable_reasons),
    )
    with _LOCK:
        _RESULT_CACHE[cache_key] = result
        while len(_RESULT_CACHE) > _RESULT_CACHE_MAX:
            _RESULT_CACHE.popitem(last=False)
    return result


def introduced_copy(check: CopyCheck, text: str, baseline: str | None) -> CopyCheck:
    """``check``（对 ``text`` 的检查结果）里只留 ``baseline`` 里本来没有的命中。

    AI 建议 / 局部改写 / 定向修改常常把原稿里已有的字原样带回来（整稿建议、改写保留原句、修改稿保留首稿的句子）：
    那些字——哪怕是作者自己粘进来的参考原文——不是这一版带进来的，由成稿门在定稿时对全文把关，这里不该以「这一版
    照抄了参考书」拦下它。命中区间规范化后是 ``baseline``（同样规范化）的子串即视为原有；把原有照抄扩写长了的
    命中不是子串，照样拦。受保护专名同理只留新带进来的（它们本来就不拦，只影响提示）。
    """
    if not (check.hits or check.protected_hits) or not str(baseline or "").strip():
        return check
    base_copy = normalize_text_for_matching(str(baseline))
    base_terms = normalize_for_term_match(str(baseline))
    hits = tuple(
        hit for hit in check.hits if normalize_text_for_matching(text[hit.start : hit.end]) not in base_copy
    )
    protected = tuple(
        hit
        for hit in check.protected_hits
        if normalize_for_term_match(text[hit.start : hit.end]) not in base_terms
    )
    if len(hits) == len(check.hits) and len(protected) == len(check.protected_hits):
        return check
    return dataclasses.replace(
        check,
        blocked=bool(hits),
        hits=hits,
        protected_hits=protected,
        extra={
            **dict(check.extra),
            "baseline_sha256": hashlib.sha256(str(baseline).encode("utf-8")).hexdigest(),
            "preexisting_hit_count": len(check.hits) - len(hits),
            "preexisting_protected_hit_count": len(check.protected_hits) - len(protected),
        },
    )


def copy_gate_policies(
    session: Session,
    *,
    scope: Any = None,
    bundle_snapshot: Mapping[str, Any] | None = None,
) -> list[Any]:
    """抄袭门要比对的绑定：bundle 冻结的那份（绑定时）+ 作用域当前的活动绑定（轻量现解析，不冻结契约）。

    两边都查：采纳作者稿、成稿中心提升等路径上，正文可能在冻结之后才粘进参考原文，冻结时没绑定或换了书，
    今天绑着的书照样要拦。哪一边解析降级（契约损坏、现解析失败）就把那份降级策略也带上——
    :func:`check_reference_copy` 据此把结果标成 ``unavailable``（那一边没有查成），而不是悄悄少查一边。
    """
    from novel_system.services.style_policy import style_policy_for_bundle, style_policy_live

    policies: list[Any] = []
    if isinstance(bundle_snapshot, Mapping):
        frozen = style_policy_for_bundle(bundle_snapshot)
        if frozen.bound or _policy_unavailable_reason(frozen) is not None:
            policies.append(frozen)
    if scope is not None:
        live = style_policy_live(session, scope, freeze_contract=False)
        if live.bound or _policy_unavailable_reason(live) is not None:
            policies.append(live)
    return policies


def check_reference_copy_for_scope(
    session: Session,
    text: str,
    *,
    scope: Any = None,
    bundle_snapshot: Mapping[str, Any] | None = None,
) -> CopyCheck:
    """:func:`copy_gate_policies` + :func:`check_reference_copy` 的便捷组合（进正文的各条路径用）。"""
    policies = copy_gate_policies(session, scope=scope, bundle_snapshot=bundle_snapshot)
    return check_reference_copy(
        session,
        text,
        policy=policies[0] if policies else None,
        extra_policies=policies[1:],
    )


def copy_block_author_action(
    check: CopyCheck | Mapping[str, Any],
    *,
    target_view: str = "writer",
    target_ref: str = "",
    subject: str = "这段文字",
) -> dict[str, Any] | None:
    """抄袭门拦下时给作者的动作：说清是第几字到第几字（``subject`` 里的位置——作者自己的正文或这条 AI 建议），
    不印参考原文。只有原文重合会拦；同一段里的受保护专名只顺带提一句（不拦）。"""
    audit = check.audit() if isinstance(check, CopyCheck) else dict(check or {})
    hits = [item for item in audit.get("hits") or [] if isinstance(item, Mapping)]
    if not audit.get("blocked") or not hits:
        return None
    protected = [item for item in audit.get("protected_hits") or [] if isinstance(item, Mapping)]
    where = "、".join(f"第 {int(item['start']) + 1}–{int(item['end'])} 字" for item in hits[:5])
    more = f"等 {int(audit.get('hit_count') or len(hits))} 处" if int(audit.get("hit_count") or 0) > 5 else ""
    message = (
        f"{subject}的{where}{more}与参考书原文连续 {THRESHOLD_CHARS} 字以上相同。"
        "把这些位置改写成你自己的句子后再定稿；正文已保留，未被改动。"
    )
    if protected:
        count = int(audit.get("protected_hit_count") or len(protected))
        message += f"另有 {count} 处用了参考书的专名——这一项不拦，定稿前可以换成你自己的。"
    evidence = [
        f"copy:{int(item['start'])}-{int(item['end'])}:len={int(item.get('matched_chars') or 0)}:sha={item.get('sha256')}"
        for item in hits[:MAX_REPORTED_HITS]
    ] + [
        f"protected:{int(item['start'])}-{int(item['end'])}:source={item.get('source')}:sha={item.get('term_sha256')}"
        for item in protected[:MAX_REPORTED_HITS]
    ]
    return author_action(
        f"{subject}里有参考书的原文，不能进正文",
        message,
        target_view=target_view,
        target_ref=target_ref,
        primary_button_label="去改写这些位置",
        evidence_summary=evidence,
    )


def protected_term_warning(check: CopyCheck, *, subject: str = "正文") -> dict[str, Any] | None:
    """受保护专名命中 → 成稿门的**不拦**警告（``source_safety:protected_term``）。

    带上命中的词本身（它们是作者正文里的字、也在画像的禁用词表里给作者看过，不是参考原文）与次数；专名表是模型
    认的，可能收进日常词，所以话里同时告诉作者怎么把误收的词删掉。没有命中 → ``None``。
    """
    terms = check.protected_terms() if isinstance(check, CopyCheck) else []
    if not terms:
        return None
    count = len(check.protected_hits)
    shown = "、".join(f"「{term}」" for term in terms[:MAX_WARNING_TERMS])
    more = f"等 {len(terms)} 个词" if len(terms) > MAX_WARNING_TERMS else ""
    return {
        "issue_key": PROTECTED_TERM_WARNING_KEY,
        "quality_level": "Q2",
        "blocking": False,
        "message": (
            f"{subject}里用了参考书的专名 {shown}{more}（共 {count} 处）。这一项不拦归档；如果它们是参考书里的人名、"
            "地名或设定名，建议换成你自己的——若只是日常用词被误收进了专名表，可以到文风画像的禁用词里删掉它。"
        ),
        "terms": terms[:MAX_REPORTED_HITS],
        "hit_count": count,
        "recommended_action": "author_review_optional_fix",
        "verified_by": "reference_copy_gate",
    }


def unavailable_warning(check: CopyCheck | None = None, *, error_type: str | None = None) -> dict[str, Any]:
    """抄袭门有一边没有查成（绑定的书已删 / 风格策略解析降级）→ 成稿门的**不拦**警告（``source_safety:unavailable``）。"""
    missing = list(check.missing_books) if isinstance(check, CopyCheck) else []
    reasons = list(check.unavailable_reasons) if isinstance(check, CopyCheck) else []
    if missing:
        detail = "绑定的参考书已不在书库里"
    else:
        detail = "这一场的风格绑定解析失败"
    return {
        "issue_key": UNAVAILABLE_WARNING_KEY,
        "quality_level": "Q2",
        "blocking": False,
        "message": f"{detail}，这一边的原文重合检查没有做成；正文照常归档，如需核对请恢复绑定后再做一次对照检查。",
        "missing_books": missing,
        "reasons": reasons + ([error_type] if error_type else []),
        "recommended_action": "author_review_optional_fix",
        "verified_by": None,
    }


def reset_reference_copy_gate_cache() -> None:
    with _LOCK:
        _INDEX_CACHE.clear()
        _RESULT_CACHE.clear()


__all__ = [
    "BLOCK_ISSUE_KEY",
    "COPY_GATE_VERSION",
    "CopyCheck",
    "CopyHit",
    "PROTECTED_TERM_WARNING_KEY",
    "ProtectedHit",
    "THRESHOLD_CHARS",
    "UNAVAILABLE_WARNING_KEY",
    "check_reference_copy",
    "check_reference_copy_for_scope",
    "copy_block_author_action",
    "copy_gate_policies",
    "introduced_copy",
    "protected_term_warning",
    "reset_reference_copy_gate_cache",
    "unavailable_warning",
]
