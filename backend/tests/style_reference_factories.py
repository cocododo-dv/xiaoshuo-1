"""风格参考测试的共享造数（风格参考 v3 台账 U17）：书 + 段落、学习血缘（run → 抽取 → 发现 → 引文 → 证据）、
画像（v3 文风卡 / 旧画像 / 空画像）、绑定、窗口索引。

各测试文件原来各写一份 ``_seed_book`` / ``_seed_profile`` / ``_ingest``，字段默认值互相漂移（校验和、权属声明、
段落 id 的格式各不相同）。这里给一套默认值，调用方只写与自己用例有关的差异：

- 只 ``flush``、不 ``commit``——提交由调用方决定（多数用例在 ``with SessionLocal() as session`` 块尾提交）；
- 全部是合成内容：段落是泛化的叙述句，文风卡的句子是泛化的写法描述，没有真实作者原文或作品人名；
- id 都由调用方给的 ``key`` / ``book_id`` 派生，同一用例里多次调用互不冲突。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import (
    StyleReferenceBook,
    StyleReferenceParagraph,
    StyleReferenceWindow,
)
from novel_system.services.style_reference.repository import StyleReferenceRepository

# 非「仅本机」的书要有发送权声明才会把原文样例送去云端模型（``policy.cloud_send_rights``）
RIGHTS_STATS: dict[str, Any] = {
    "rights_declaration": {"declared": True, "analysis_rights": True, "send_rights": True}
}

# 泛化的写法描述（与 ``style_reference_inject_helpers`` 的合成文风卡同一套说法）
VOICE_HABITS: list[str] = [
    "句子多靠「就」「也」「还」并置推进，少用「然而」「于是」",
    "对白常不加引导词，加的时候放在话后面",
    "逗号多、句号少，一口气说到底再断",
]
DEVICES: dict[str, list[str]] = {
    "language.rhetoric": ["俗物比喻", "游戏梗"],
    "narrative.pacing": ["倒计时"],
    "scene.dialogue": ["抢话"],
}


# ---------------------------------------------------------------------------
# 书 + 段落
# ---------------------------------------------------------------------------


def synthetic_paragraphs(count: int, *, prefix: str = "") -> list[str]:
    """``count`` 段泛化的叙述句（每段带序号，彼此不同）。"""
    return [f"{prefix}第{index}段：灯下的人把信折好又打开，终于没有寄出去。" for index in range(count)]


def make_book(
    session: Session,
    book_id: str,
    *,
    paragraphs: Iterable[str | Mapping[str, Any]] = (),
    title: str = "t",
    cloud_policy: str = "allow_full_cloud",
    status: str = "ready",
    stats: Mapping[str, Any] | None = None,
    paragraph_type: str = "narration",
    paragraph_id: str = "{book_id}_p{index:05d}",
    text_checksum: str | None = None,
    total_chars: int | None = None,
    source_kind: str = "upload",
) -> str:
    """一本书与它的段落（一次批量写入，段落偏移按原书顺序累加）。

    ``paragraphs`` 的元素是正文字符串，或带 ``text`` 的 dict（可再带 ``paragraph_index`` / ``paragraph_type`` /
    ``paragraph_id`` / ``classifier_confidence``，缺的按默认补）。``paragraph_id`` 是 id 的格式串（可用 ``book_id``
    与 ``index``）。``stats`` 缺省为空——需要发送权声明时传 :data:`RIGHTS_STATS`。
    """
    rows: list[dict[str, Any]] = []
    for position, item in enumerate(paragraphs):
        row = {"text": item} if isinstance(item, str) else dict(item)
        index = int(row.get("paragraph_index", position))
        text = str(row["text"])
        rows.append(
            {
                "paragraph_id": str(row.get("paragraph_id") or paragraph_id.format(book_id=book_id, index=index)),
                "paragraph_index": index,
                "paragraph_type": str(row.get("paragraph_type") or paragraph_type),
                "text": text,
                "classifier_confidence": float(row.get("classifier_confidence", 0.9)),
            }
        )
    session.add(
        StyleReferenceBook(
            book_id=book_id,
            title=title,
            source_kind=source_kind,
            cloud_policy=cloud_policy,
            text_checksum=text_checksum or hashlib.sha256(book_id.encode("utf-8")).hexdigest(),
            total_chars=total_chars if total_chars is not None else sum(len(row["text"]) for row in rows),
            status=status,
            stats_json=dict(stats or {}),
        )
    )
    session.flush()
    offset = 0
    records = []
    for row in rows:
        length = len(row["text"])
        records.append(
            StyleReferenceParagraph(
                paragraph_id=row["paragraph_id"],
                book_id=book_id,
                paragraph_index=row["paragraph_index"],
                paragraph_type=row["paragraph_type"],
                start_offset=offset,
                end_offset=offset + length,
                text=row["text"],
                char_count=length,
                classifier_confidence=row["classifier_confidence"],
            )
        )
        offset += length + 1
    if records:
        session.add_all(records)
        session.flush()
    return book_id


# ---------------------------------------------------------------------------
# 画像
# ---------------------------------------------------------------------------


def card_payload(*, with_evidence: bool = False, quote_id: str | None = None) -> dict[str, Any]:
    """一张合成文风卡（四维有句子，其余维由 ``card.normalize_card`` 补空）。"""
    evidence = [quote_id] if (with_evidence and quote_id) else []
    return {
        "version": "dimension_card_v1",
        "temperament": ["危急关头用自嘲冲淡紧张"],
        "dimensions": [
            {
                "dimension": "language.rhetoric",
                "distinctiveness": 0.95,
                "devices": DEVICES["language.rhetoric"],
                "lines": [
                    {"text": "紧张处拿日常小物件打夸张的比方", "mandatory": True, "distinctiveness": 0.9, "evidence_quote_ids": evidence},
                    {"text": "借游戏与电影的套路打比方", "distinctiveness": 0.7},
                    {"text": "一段里常叠两三个比方", "distinctiveness": 0.2},
                    {"text": "不让天气替人伤心", "kind": "avoid", "distinctiveness": 0.6},
                ],
            },
            {
                "dimension": "narrative.pacing",
                "distinctiveness": 0.8,
                "devices": DEVICES["narrative.pacing"],
                "lines": [
                    {"text": "危急时把时间切成倒计时推着走", "distinctiveness": 0.8},
                    {"text": "长句后面接极短的一句收住", "distinctiveness": 0.5},
                ],
            },
            {
                "dimension": "scene.dialogue",
                "distinctiveness": 0.6,
                "devices": DEVICES["scene.dialogue"],
                "lines": [{"text": "对白你来我往地抢话，少用长篇解释", "distinctiveness": 0.6}],
            },
            {
                "dimension": "theme.emotional_tone",
                "distinctiveness": 0.7,
                "lines": [{"text": "惊险里夹着吐槽，悲伤说得很轻", "distinctiveness": 0.7}],
            },
        ],
    }


def v3_profile_json(
    *,
    quote_id: str | None = None,
    habits: Sequence[str] = VOICE_HABITS,
    deliberate_repetition: bool = False,
    card_line_states: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """学习作业写出的 v3 画像形状：文风卡 + 行状态 + 声音（与它的 ``voice_signature`` 别名）+ 版本标记。"""
    voice = {"version": "voice_signature_v2", "habits": list(habits), "deliberate_repetition": deliberate_repetition}
    return {
        "profile_version": "style_profile_v3",
        "dimension_card": card_payload(with_evidence=quote_id is not None, quote_id=quote_id),
        "card_line_states": dict(card_line_states or {}),
        "voice": dict(voice),
        "voice_signature": dict(voice),
        "qualitative_summary": "危急关头用自嘲冲淡紧张，比方取材于日常小物件。",
    }


def make_profile(
    session: Session,
    book_id: str,
    *,
    profile_id: str,
    run_id: str | None = None,
    title: str = "t",
    status: str = "active",
    profile_json: Mapping[str, Any] | None = None,
    version_tag: str | None = None,
) -> str:
    """一份画像（连同它血缘上的 run：``run_id`` 缺省 ``run_<profile_id>``，已存在就复用）。

    ``profile_json`` 缺省为空画像；要文风卡用 :func:`v3_profile_json`。
    """
    repo = StyleReferenceRepository(session)
    run_id = run_id or f"run_{profile_id}"
    if repo.get_run(run_id) is None:
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
    repo.create_profile(
        profile_id=profile_id,
        book_id=book_id,
        run_id=run_id,
        title=title,
        status=status,
        profile_json=dict(profile_json or {}),
        coverage_json={},
        source_finding_ids_json=[],
        version_tag=version_tag,
    )
    return profile_id


def make_banned_terms(
    session: Session,
    profile_id: str,
    terms: Iterable[str | tuple[str, str]],
    *,
    source: str = "user",
    key: str | None = None,
) -> list[str]:
    """画像的禁用词：元素是词本身（生成期）或 ``(词, 作用域)``。返回 term_id 列表。"""
    repo = StyleReferenceRepository(session)
    ids: list[str] = []
    for index, item in enumerate(terms):
        term, scope = (item, "generation") if isinstance(item, str) else item
        term_id = f"term_{key or profile_id}_{index}"
        repo.create_banned_term(
            term_id=term_id,
            profile_id=profile_id,
            term=term,
            replacement_hint=None,
            source=source,
            scope=scope,
        )
        ids.append(term_id)
    return ids


# ---------------------------------------------------------------------------
# 学习血缘：run → 抽取 → 发现 → 引文 → 证据
# ---------------------------------------------------------------------------


def make_learning_lineage(
    session: Session,
    book_id: str,
    *,
    key: str,
    run_id: str | None = None,
    sub_dimension: str = "language.sentence_structure",
    statement: str = "句子短,动作接动作",
    quotes: Sequence[tuple[str | None, str]] = (),
) -> dict[str, Any]:
    """一次学习留下的血缘：run、一条抽取、一条发现与它的原文引文 / 证据（``quotes`` = [(paragraph_id, 引文)]）。

    返回 ``{"run_id", "extraction_id", "finding_id", "quote_ids", "evidence_ids"}``。
    """
    repo = StyleReferenceRepository(session)
    run_id = run_id or f"run_{key}"
    if repo.get_run(run_id) is None:
        repo.create_run(run_id=run_id, book_id=book_id, status="done", phase="done")
    extraction_id = f"ext_{key}"
    layer = sub_dimension.split(".", 1)[0]
    repo.create_extraction(
        extraction_id=extraction_id,
        book_id=book_id,
        run_id=run_id,
        layer=layer,
        sub_dimension=sub_dimension,
        raw_payload_json={},
        status="validated",
    )
    finding = repo.create_finding(
        finding_id=f"find_{key}",
        book_id=book_id,
        run_id=run_id,
        extraction_id=extraction_id,
        sub_dimension=sub_dimension,
        finding_kind="observation",
        statement=statement,
        confidence="high",
        status="active",
    )
    quote_ids: list[str] = []
    evidence_ids: list[str] = []
    for index, (paragraph_id, text) in enumerate(quotes):
        quote_id = f"quote_{key}_{index}"
        repo.create_quote(
            quote_id=quote_id,
            book_id=book_id,
            paragraph_id=paragraph_id,
            span_start=0,
            span_end=len(text),
            quote_text=text,
            illustrates_dims=[sub_dimension],
            extracted_features={},
        )
        evidence_id = f"ev_{key}_{index}"
        repo.create_evidence(
            evidence_id=evidence_id,
            finding_id=finding.finding_id,
            quote_id=quote_id,
            anchor_kind="paragraph_quote",
        )
        quote_ids.append(quote_id)
        evidence_ids.append(evidence_id)
    return {
        "run_id": run_id,
        "extraction_id": extraction_id,
        "finding_id": finding.finding_id,
        "quote_ids": quote_ids,
        "evidence_ids": evidence_ids,
    }


# ---------------------------------------------------------------------------
# 绑定与窗口
# ---------------------------------------------------------------------------


def make_binding(
    session: Session,
    profile_id: str,
    *,
    binding_id: str,
    scope: str = "project",
    scope_ref_id: str | None = None,
    config_json: Mapping[str, Any] | None = None,
    strategy: str = "mixed",
    task_type: str = "scene_generation",
    status: str = "active",
):
    """一条绑定（``config_json`` 原样写；v3 配置键见 ``binding_config.normalize_binding_config``）。"""
    return StyleReferenceRepository(session).create_binding(
        binding_id=binding_id,
        profile_id=profile_id,
        scope=scope,
        scope_ref_id=scope_ref_id,
        task_type=task_type,
        strategy=strategy,
        config_json=dict(config_json or {}),
        status=status,
    )


def make_windows(
    session: Session,
    book_id: str,
    *,
    tags: Mapping[int, Mapping[str, Any]] | None = None,
    devices: Sequence[str] = (),
) -> list[StyleReferenceWindow]:
    """建这本书的持久化窗口索引（``windows.ensure_window_index``），可选地按窗口号写场面 / 情绪 / 手法标签。"""
    from novel_system.services.style_reference.windows import (
        ensure_window_index,
        load_windows,
        set_window_tags,
    )

    ensure_window_index(session, book_id)
    if tags:
        set_window_tags(session, book_id, dict(tags), tags_version="window_tags_v1", devices=tuple(devices))
    session.flush()
    return load_windows(session, book_id)


__all__ = [
    "DEVICES",
    "RIGHTS_STATS",
    "VOICE_HABITS",
    "card_payload",
    "make_banned_terms",
    "make_binding",
    "make_book",
    "make_learning_lineage",
    "make_profile",
    "make_windows",
    "synthetic_paragraphs",
    "v3_profile_json",
]
