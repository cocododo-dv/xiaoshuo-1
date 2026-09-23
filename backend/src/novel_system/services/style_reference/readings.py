"""风格参考 v3（P5b，N5）—「像不像」读数入库的唯一入口，以及读数的查询与 API 形状。

读数本身是纯函数（``fidelity.read_fidelity``：一段文字对作者自己窗口分布的确定性读数）；这里负责：

- :func:`reading_for_text`：按一份 :class:`~novel_system.services.style_policy.StylePolicy` 读一段文字（不入库）；
  未绑定 / 书没有可用的参照分布 → ``None``；
- :func:`record_fidelity_reading`：**唯一**的入库入口（契约 §3.1）——管线各步（首稿 / 定向修改 / 补丁 / 归档）、
  起草台采纳、成稿中心、写作台采纳 AI 建议、对照检查都经它写 ``style_fidelity_readings``。按场景 id 记，
  **不记章 id**（场景改章不用搬）；抄袭门的结果只记计数与旗标，永不记原文；
- :func:`latest_scene_readings` / :func:`project_fidelity_summary` / :func:`reading_payload`：界面与接口读。

``source`` ∈ pipeline / adopt / archive / manual_check / author_draft；``stage`` ∈ first_draft / revision / patched /
final / manual。
"""

from __future__ import annotations

import hashlib
import logging
import math
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleFidelityReading, utcnow
from novel_system.services.style_reference.card import DIMENSION_LABELS
from novel_system.services.style_reference.fidelity import (
    DEFAULT_MAX_PERCENTILE,
    MIN_REFERENCE_WINDOWS,
    MIN_RELIABLE_CHARS,
    FidelityReading,
    read_fidelity,
    recent_gap_entries,
    recent_gap_phrases,
    reference_distribution_for_book,
    within_author_range,
)

logger = logging.getLogger(__name__)

SOURCE_PIPELINE = "pipeline"
SOURCE_ADOPT = "adopt"
SOURCE_ARCHIVE = "archive"
SOURCE_MANUAL_CHECK = "manual_check"
SOURCE_AUTHOR_DRAFT = "author_draft"
SOURCES: tuple[str, ...] = (
    SOURCE_PIPELINE,
    SOURCE_ADOPT,
    SOURCE_ARCHIVE,
    SOURCE_MANUAL_CHECK,
    SOURCE_AUTHOR_DRAFT,
)

STAGE_FIRST_DRAFT = "first_draft"
STAGE_REVISION = "revision"
STAGE_PATCHED = "patched"
STAGE_FINAL = "final"
STAGE_MANUAL = "manual"
STAGES: tuple[str, ...] = (STAGE_FIRST_DRAFT, STAGE_REVISION, STAGE_PATCHED, STAGE_FINAL, STAGE_MANUAL)

# 近期常见偏差只看首稿读数（「前几场草稿里反复出现的不像之处」）：同一场的修改 / 补丁 / 终稿读数不重复计票。
GAP_SOURCE_STAGES: tuple[tuple[str, str], ...] = ((SOURCE_PIPELINE, STAGE_FIRST_DRAFT),)
RECENT_READINGS_WINDOW = 5
RECENT_GAP_MIN_HITS = 3
TREND_LIMIT = 60


# ---------------------------------------------------------------------------
# 读（不入库）
# ---------------------------------------------------------------------------


def reading_for_text(session: Session, policy: Any, text: str) -> FidelityReading | None:
    """按策略读一段文字（未绑定 / 没有书 / 空文字 / 书没有窗口 → ``None``）。"""
    if policy is None or not getattr(policy, "bound", False):
        return None
    book_id = str(getattr(policy, "book_id", "") or "")
    content = str(text or "")
    if not book_id or not content.strip():
        return None
    dist = reference_distribution_for_book(session, book_id)
    if dist is None:
        return None
    return read_fidelity(content, dist, dimension_states=getattr(policy, "dimension_states", None))


def text_sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def scene_project_id(session: Session, scene: Any) -> str | None:
    """场景所属作品（旧场景行没有 project_id 时取它所在章的）；定不出 → ``None``（读数照记，只是不进作品汇总）。"""
    project_id = getattr(scene, "project_id", None)
    if project_id:
        return str(project_id)
    chapter_id = getattr(scene, "chapter_id", None)
    if not chapter_id:
        return None
    from novel_system.db.models import ChapterGoal

    chapter = session.get(ChapterGoal, chapter_id)
    return str(chapter.project_id) if chapter is not None and chapter.project_id else None


def policy_essentials(policy: Any) -> dict[str, Any]:
    """读数里记的策略要点（不含契约正文）。"""
    audit = policy.audit() if hasattr(policy, "audit") else {}
    return {
        "mode": audit.get("mode"),
        "contract_hash": audit.get("contract_hash"),
        "profile_id": audit.get("profile_id"),
        "binding_id": audit.get("binding_id"),
        "book_id": getattr(policy, "book_id", None),
        "reference_mode": audit.get("reference_mode"),
        "draft_mode": audit.get("draft_mode"),
        "sample_windows": audit.get("sample_windows"),
        "dimension_states": {
            dim: state
            for dim, state in dict(getattr(policy, "dimension_states", None) or {}).items()
            if state != "normal"
        },
    }


def copy_check_summary(copy_check: Any) -> dict[str, Any] | None:
    """抄袭门结果 → 只有旗标与计数（不记位置之外的任何东西，也不记位置）。"""
    if copy_check is None:
        return None
    if isinstance(copy_check, Mapping):
        audit = dict(copy_check)
        hits = audit.get("hit_count", len(audit.get("hits") or []) if isinstance(audit.get("hits"), list) else audit.get("hits"))
        protected = audit.get(
            "protected_hit_count",
            len(audit.get("protected_hits") or []) if isinstance(audit.get("protected_hits"), list) else audit.get("protected_hits"),
        )
        return {
            "blocked": bool(audit.get("blocked")),
            "hits": int(hits or 0),
            "protected_hits": int(protected or 0),
            "terms_checked": int(audit.get("terms_checked") or 0),
            "version": audit.get("version"),
        }
    return {
        "blocked": bool(getattr(copy_check, "blocked", False)),
        "hits": len(getattr(copy_check, "hits", ()) or ()),
        "protected_hits": len(getattr(copy_check, "protected_hits", ()) or ()),
        "terms_checked": int(getattr(copy_check, "terms_checked", 0) or 0),
        "version": getattr(copy_check, "version", None),
    }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_judge(judge: Any, *, source: str | None = None) -> dict[str, Any] | None:
    """评审分 → ``{overall, dimensions: {dim: {score, note}}, source, summary}``（10 分制，一位小数）。

    接受：对照检查的评审输出（``{overall, dimensions: {dim: {score, note}}}``）、软 QC 的参考评审记录
    （``{kind: reference_judge, style_score, dimension_scores: {dim: score}}``），或已规整过的本形状。分数必须
    已经是 10 分制（量级换算在调用方用 ``review_scores`` 做，这里只截到 0–10，不再猜量级）。没有任何分数 → ``None``。
    """
    if not isinstance(judge, Mapping):
        return None
    raw_dims: dict[str, tuple[Any, str]] = {}
    dims = judge.get("dimensions")
    if isinstance(dims, Mapping):
        for key, value in dims.items():
            if str(key) not in DIMENSION_LABELS:
                continue
            if isinstance(value, Mapping):
                raw_dims[str(key)] = (value.get("score"), str(value.get("note") or "").strip())
            else:
                raw_dims[str(key)] = (value, "")
    scores = judge.get("dimension_scores")
    if isinstance(scores, Mapping):
        for key, value in scores.items():
            if str(key) in DIMENSION_LABELS and str(key) not in raw_dims:
                raw_dims[str(key)] = (value, "")
    overall_raw = judge.get("overall", judge.get("style_score"))
    dimensions: dict[str, dict[str, Any]] = {}
    for dim, (score, note) in raw_dims.items():
        number = _finite(score)
        if number is None:
            continue
        dimensions[dim] = {"score": round(max(0.0, min(10.0, number)), 1), "note": note[:200]}
    overall_number = _finite(overall_raw)
    if overall_number is None and dimensions:
        overall_number = sum(item["score"] for item in dimensions.values()) / len(dimensions)
    if overall_number is None and not dimensions:
        return None
    payload: dict[str, Any] = {
        "overall": round(max(0.0, min(10.0, overall_number)), 1) if overall_number is not None else None,
        "dimensions": dimensions,
        "source": str(source or judge.get("source") or judge.get("kind") or "judge"),
    }
    summary = str(judge.get("summary") or "").strip()
    if summary:
        payload["summary"] = summary[:500]
    return payload


# ---------------------------------------------------------------------------
# 入库（唯一入口）
# ---------------------------------------------------------------------------


def _existing(
    session: Session,
    *,
    scene_id: str | None,
    source: str,
    stage: str,
    draft_ref: str,
    sha: str,
    profile_id: str | None,
) -> StyleFidelityReading | None:
    stmt = select(StyleFidelityReading).where(
        StyleFidelityReading.source == source,
        StyleFidelityReading.stage == stage,
        StyleFidelityReading.draft_ref == draft_ref,
        StyleFidelityReading.text_sha256 == sha,
    )
    stmt = stmt.where(
        StyleFidelityReading.scene_id == scene_id if scene_id else StyleFidelityReading.scene_id.is_(None)
    )
    stmt = stmt.where(
        StyleFidelityReading.profile_id == profile_id if profile_id else StyleFidelityReading.profile_id.is_(None)
    )
    return session.scalars(stmt.order_by(StyleFidelityReading.created_at.desc()).limit(1)).first()


def record_fidelity_reading(
    session: Session,
    *,
    policy: Any,
    text: str,
    source: str,
    stage: str,
    scene_id: str | None = None,
    project_id: str | None = None,
    draft_ref: str | None = None,
    judge: Any = None,
    copy_check: Any = None,
    reading: FidelityReading | None = None,
    max_percentile: float | None = None,
    strict: bool = False,
) -> StyleFidelityReading | None:
    """记一条读数；未绑定 / 书没有可用的参照分布 / 文字为空 → ``None``（什么也不写）。

    - ``reading``：调用方已经读过同一段文字时传进来，省一次测量；
    - ``draft_ref`` 给了时幂等：同一场、同一来源 / 阶段 / 稿行、同一段文字、同一画像已有读数就直接返回那一条
      （检查点续跑、重确认不会重复记）；对照检查（``manual_check``）例外——每次检查都记一条（作业本身只跑一次）；
    - ``judge``：评审模型的按维打分（对照检查 / 软 QC 参考评审），规整成 10 分制；
    - ``copy_check``：抄袭门结果，只记旗标与计数；
    - ``strict=False``（管线默认）：写在保存点里，任何异常只记日志、返回 ``None``——读数是观察，永不阻断管线；
      对照检查传 ``strict=True``，失败照抛。
    """
    if source not in SOURCES:
        raise ValueError(f"unknown fidelity reading source: {source!r}")
    if stage not in STAGES:
        raise ValueError(f"unknown fidelity reading stage: {stage!r}")
    if not strict:
        try:
            with session.begin_nested():
                return _record(
                    session,
                    policy=policy,
                    text=text,
                    source=source,
                    stage=stage,
                    scene_id=scene_id,
                    project_id=project_id,
                    draft_ref=draft_ref,
                    judge=judge,
                    copy_check=copy_check,
                    reading=reading,
                    max_percentile=max_percentile,
                )
        except Exception:  # noqa: BLE001 — 读数是观察：失败只记日志
            logger.warning(
                "fidelity reading not recorded (scene=%s source=%s stage=%s)", scene_id, source, stage, exc_info=True
            )
            return None
    return _record(
        session,
        policy=policy,
        text=text,
        source=source,
        stage=stage,
        scene_id=scene_id,
        project_id=project_id,
        draft_ref=draft_ref,
        judge=judge,
        copy_check=copy_check,
        reading=reading,
        max_percentile=max_percentile,
    )


def _record(
    session: Session,
    *,
    policy: Any,
    text: str,
    source: str,
    stage: str,
    scene_id: str | None,
    project_id: str | None,
    draft_ref: str | None,
    judge: Any,
    copy_check: Any,
    reading: FidelityReading | None,
    max_percentile: float | None,
) -> StyleFidelityReading | None:
    if policy is None or not getattr(policy, "bound", False):
        return None
    content = str(text or "")
    if not content.strip():
        return None
    sha = text_sha256(content)
    profile_id = str(getattr(policy, "profile_id", "") or "") or None
    # 对照检查每做一次就是一条新读数（作者重查同一稿要看到这一次的评审）；管线 / 采纳 / 归档按稿行幂等
    if draft_ref and source != SOURCE_MANUAL_CHECK:
        existing = _existing(
            session,
            scene_id=scene_id,
            source=source,
            stage=stage,
            draft_ref=str(draft_ref),
            sha=sha,
            profile_id=profile_id,
        )
        if existing is not None:
            return existing
    reading = reading if reading is not None else reading_for_text(session, policy, content)
    if reading is None:
        return None
    threshold = float(max_percentile if max_percentile is not None else _default_max_percentile())
    reading_json = {
        **reading.to_json(),
        "within_range": within_author_range(reading, max_percentile=threshold),
        "max_percentile": threshold,
        "policy": policy_essentials(policy),
    }
    row = StyleFidelityReading(
        reading_id=f"sfr_{uuid.uuid4().hex[:16]}",
        project_id=str(project_id) if project_id else None,
        scene_id=str(scene_id) if scene_id else None,
        profile_id=profile_id,
        binding_id=str(getattr(policy, "binding_id", "") or "") or None,
        source=source,
        stage=stage,
        draft_ref=str(draft_ref) if draft_ref else None,
        text_sha256=sha,
        char_count=int(reading.char_count),
        percentile=float(reading.percentile),
        distance=float(reading.distance),
        reading_json=reading_json,
        judge_json=normalize_judge(judge),
        copy_check_json=copy_check_summary(copy_check),
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def _default_max_percentile() -> float:
    try:
        from novel_system.services.style_reference.style_step import fidelity_thresholds

        return float(fidelity_thresholds().style_step_max_percentile)
    except Exception:  # noqa: BLE001 — 阈值文件读不到按临时默认
        return float(DEFAULT_MAX_PERCENTILE)


def record_author_draft_reading(
    session: Session,
    *,
    scene_id: str | None,
    text: str,
    stage: str,
    draft_ref: str,
) -> StyleFidelityReading | None:
    """写作台采纳 AI 建议 / 局部改写之后的作者稿读数（source=author_draft）。

    策略按场景**当前**的活动绑定轻量现解析（作者此刻对着的那本书），不冻结契约；场景不存在 / 未绑定 → ``None``。
    读数是观察：任何失败只记日志（保存点里写，不影响采纳本身）。
    """
    if not scene_id or not str(text or "").strip():
        return None
    try:
        from novel_system.db.models import SceneCard
        from novel_system.services.style_policy import style_policy_live

        scene = session.get(SceneCard, str(scene_id))
        if scene is None:
            return None
        policy = style_policy_live(session, scene, freeze_contract=False)
    except Exception:  # noqa: BLE001 — 读数是观察
        logger.warning("author-draft fidelity policy unavailable for scene %s", scene_id, exc_info=True)
        return None
    return record_fidelity_reading(
        session,
        policy=policy,
        text=text,
        source=SOURCE_AUTHOR_DRAFT,
        stage=stage,
        scene_id=str(scene_id),
        project_id=scene_project_id(session, scene),
        draft_ref=draft_ref,
    )


def attach_judge(session: Session, reading_id: str | None, judge: Any) -> StyleFidelityReading | None:
    """给一条已入库的读数补上评审分（软 QC 在读数之后才评完的那一轮）。"""
    if not reading_id:
        return None
    row = session.get(StyleFidelityReading, str(reading_id))
    normalized = normalize_judge(judge)
    if row is None or normalized is None:
        return row
    row.judge_json = normalized
    session.flush()
    return row


# ---------------------------------------------------------------------------
# 查询 / API 形状
# ---------------------------------------------------------------------------


def reading_payload(row: StyleFidelityReading | None) -> dict[str, Any] | None:
    """一条读数的 API 形状（越界特征带维度的中文名；评审 / 抄袭门只有分数与计数）。"""
    if row is None:
        return None
    data = dict(row.reading_json or {})
    out_of_band = []
    for item in data.get("out_of_band") or []:
        if not isinstance(item, Mapping):
            continue
        dimension = item.get("dimension")
        out_of_band.append(
            {
                "feature": item.get("feature"),
                "dimension": dimension,
                "dimension_label": DIMENSION_LABELS.get(str(dimension or ""), "") or None,
                "direction": item.get("direction"),
                "phrase": item.get("phrase"),
                "z": item.get("z"),
            }
        )
    threshold = _finite(data.get("max_percentile"))
    if threshold is None:
        threshold = _default_max_percentile()
    within = data.get("within_range")
    if not isinstance(within, bool):
        within = within_author_range(data, max_percentile=threshold)
    copy = dict(row.copy_check_json) if isinstance(row.copy_check_json, Mapping) else None
    reliable = bool(data.get("reliable", False))
    char_count = int(row.char_count or 0)
    window_count = int(_finite(data.get("window_count")) or 0)
    return {
        "reading_id": row.reading_id,
        "scene_id": row.scene_id,
        "project_id": row.project_id,
        "profile_id": row.profile_id,
        "source": row.source,
        "stage": row.stage,
        "draft_ref": row.draft_ref,
        "percentile": row.percentile,
        "distance": row.distance,
        "within_range": bool(within),
        # 「正常范围」的百分位上限（入库时的阈值）与重点维：界面据此说清「前 N 位算正常」「重点维越界不算正常」
        "max_percentile": threshold,
        "emphasized_dimensions": [str(dim) for dim in data.get("emphasized_dimensions") or []],
        "excluded_dimensions": [str(dim) for dim in data.get("excluded_dimensions") or []],
        "reliable": reliable,
        # 读数为什么只能参考：文字太短（too_short）/ 参考书的窗口太少（few_windows）；可靠时为 None
        "unreliable_reason": (
            None if reliable else ("too_short" if char_count < MIN_RELIABLE_CHARS else "few_windows")
        ),
        "window_count": window_count,
        "min_reliable_chars": MIN_RELIABLE_CHARS,
        "min_reference_windows": MIN_REFERENCE_WINDOWS,
        "char_count": char_count,
        "out_of_band": out_of_band,
        "dimension_scores": {
            str(dim): score for dim, score in dict(data.get("dimension_scores") or {}).items()
        },
        "judge": dict(row.judge_json) if isinstance(row.judge_json, Mapping) else None,
        "copy_check": (
            {
                "blocked": bool(copy.get("blocked")),
                "hits": int(copy.get("hits") or 0),
                "protected_hits": int(copy.get("protected_hits") or 0),
            }
            if copy is not None
            else None
        ),
        "created_at": row.created_at,
    }


def latest_scene_readings(
    session: Session,
    scene_id: str,
    *,
    profile_id: str | None = None,
) -> dict[str, StyleFidelityReading]:
    """这一场每个阶段最新的一条读数（``{stage: row}``；给了画像只看这份画像的读数）。"""
    stmt = select(StyleFidelityReading).where(StyleFidelityReading.scene_id == str(scene_id))
    if profile_id:
        stmt = stmt.where(StyleFidelityReading.profile_id == str(profile_id))
    latest: dict[str, StyleFidelityReading] = {}
    for row in session.scalars(
        stmt.order_by(StyleFidelityReading.created_at.desc(), StyleFidelityReading.reading_id.desc())
    ):
        latest.setdefault(str(row.stage), row)
    return latest


def recent_gap_readings(
    session: Session,
    *,
    project_id: str | None,
    profile_id: str | None,
    window: int = RECENT_READINGS_WINDOW,
) -> list[StyleFidelityReading]:
    """近期常见偏差的票源：这部作品（同一画像）最近 ``window`` 次首稿读数，新 → 旧。"""
    if not project_id:
        return []
    stmt = select(StyleFidelityReading).where(StyleFidelityReading.project_id == str(project_id))
    if profile_id:
        stmt = stmt.where(StyleFidelityReading.profile_id == str(profile_id))
    conditions = [
        (StyleFidelityReading.source == source) & (StyleFidelityReading.stage == stage)
        for source, stage in GAP_SOURCE_STAGES
    ]
    condition = conditions[0]
    for extra in conditions[1:]:
        condition = condition | extra
    stmt = stmt.where(condition)
    return list(
        session.scalars(
            stmt.order_by(StyleFidelityReading.created_at.desc(), StyleFidelityReading.reading_id.desc()).limit(
                max(0, int(window))
            )
        ).all()
    )


def recent_gaps(
    session: Session,
    *,
    project_id: str | None,
    profile_id: str | None,
    limit: int | None = 3,
) -> list[str]:
    """最近 5 次首稿读数里越界 ≥3 次的偏差（白话短语，至多 ``limit`` 条）。"""
    rows = recent_gap_readings(session, project_id=project_id, profile_id=profile_id)
    if not rows:
        return []
    return recent_gap_phrases(rows, min_hits=RECENT_GAP_MIN_HITS, window=RECENT_READINGS_WINDOW, limit=limit)


def recent_gap_details(
    session: Session,
    *,
    project_id: str | None,
    profile_id: str | None,
    limit: int | None = 3,
) -> list[dict[str, Any]]:
    """同一份近期常见偏差的结构化形状（界面按维标出）：``{phrase, feature, direction, dimension, dimension_label,
    hits, window}``——短语与 :func:`recent_gaps` 逐条相同、同序（首稿补充强调的正是这几条）。"""
    rows = recent_gap_readings(session, project_id=project_id, profile_id=profile_id)
    if not rows:
        return []
    return [
        {**entry, "dimension_label": DIMENSION_LABELS.get(str(entry.get("dimension") or ""), "") or None}
        for entry in recent_gap_entries(
            rows, min_hits=RECENT_GAP_MIN_HITS, window=RECENT_READINGS_WINDOW, limit=limit
        )
    ]


def _scene_final_brief(row: StyleFidelityReading) -> dict[str, Any]:
    data = row.reading_json if isinstance(row.reading_json, Mapping) else {}
    return {
        "reading_id": row.reading_id,
        "source": row.source,
        "percentile": row.percentile,
        "within_range": bool(data.get("within_range")),
        "reliable": bool(data.get("reliable", False)),
        "created_at": row.created_at,
    }


def _average(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def project_fidelity_summary(
    session: Session,
    project_id: str,
    *,
    profile_id: str | None = None,
    trend_limit: int = TREND_LIMIT,
) -> dict[str, Any]:
    """一部作品的读数走势：终稿 / 首稿读数的趋势、近期常见偏差、按维平均（确定性分与评审分分开）。

    按维平均：确定性分只数每一场最新的一条终稿读数（每场一票）；评审分每一场取最新一条带评审分的读数（对照检查 /
    软 QC 参考评审，每场一票——同一份评审挂在补丁与终稿两条读数上不重复计票），不在场景上的文字检查各算一票。
    """
    stmt = select(StyleFidelityReading).where(StyleFidelityReading.project_id == str(project_id))
    if profile_id:
        stmt = stmt.where(StyleFidelityReading.profile_id == str(profile_id))
    rows = list(
        session.scalars(stmt.order_by(StyleFidelityReading.created_at.asc(), StyleFidelityReading.reading_id.asc()))
    )
    trend_rows = [row for row in rows if row.stage in (STAGE_FIRST_DRAFT, STAGE_FINAL)]
    trend = [
        {
            "reading_id": row.reading_id,
            "scene_id": row.scene_id,
            "source": row.source,
            "stage": row.stage,
            "percentile": row.percentile,
            "distance": row.distance,
            "within_range": bool((row.reading_json or {}).get("within_range")),
            "reliable": bool((row.reading_json or {}).get("reliable", False)),
            "max_percentile": _finite((row.reading_json or {}).get("max_percentile")),
            "created_at": row.created_at,
        }
        for row in trend_rows[-max(0, int(trend_limit)) :]
    ]
    latest_final: dict[str, StyleFidelityReading] = {}
    for row in rows:
        if row.stage == STAGE_FINAL and row.scene_id:
            latest_final[str(row.scene_id)] = row
    deterministic: dict[str, list[float]] = {}
    for row in latest_final.values():
        for dim, score in dict((row.reading_json or {}).get("dimension_scores") or {}).items():
            number = _finite(score)
            if number is not None:
                deterministic.setdefault(str(dim), []).append(number)
    latest_judged: dict[str, StyleFidelityReading] = {}
    unscoped_judged: list[StyleFidelityReading] = []
    for row in rows:
        if not isinstance(row.judge_json, Mapping):
            continue
        if row.scene_id:
            latest_judged[str(row.scene_id)] = row
        else:
            unscoped_judged.append(row)
    judged: dict[str, list[float]] = {}
    for row in [*latest_judged.values(), *unscoped_judged]:
        judge = row.judge_json if isinstance(row.judge_json, Mapping) else None
        for dim, entry in dict((judge or {}).get("dimensions") or {}).items():
            number = _finite(entry.get("score") if isinstance(entry, Mapping) else entry)
            if number is not None:
                judged.setdefault(str(dim), []).append(number)
    dims = sorted(set(deterministic) | set(judged), key=lambda dim: list(DIMENSION_LABELS).index(dim) if dim in DIMENSION_LABELS else 99)
    gap_details = recent_gap_details(session, project_id=project_id, profile_id=profile_id)
    return {
        "trend": trend,
        "recent_gaps": [entry["phrase"] for entry in gap_details],
        "recent_gap_details": gap_details,
        # 每一场最新的终稿读数（成稿中心按场的「像不像」角标）
        "scene_finals": {scene_id: _scene_final_brief(row) for scene_id, row in latest_final.items()},
        "dimension_averages": {
            dim: {
                "label": DIMENSION_LABELS.get(dim, dim),
                "deterministic": _average(deterministic.get(dim, [])),
                "judge": _average(judged.get(dim, [])),
                "scenes": len(deterministic.get(dim, [])),
                "judged": len(judged.get(dim, [])),
            }
            for dim in dims
        },
        "reading_count": len(rows),
        "final_scene_count": len(latest_final),
    }


__all__ = [
    "GAP_SOURCE_STAGES",
    "RECENT_GAP_MIN_HITS",
    "RECENT_READINGS_WINDOW",
    "SOURCES",
    "SOURCE_ADOPT",
    "SOURCE_ARCHIVE",
    "SOURCE_AUTHOR_DRAFT",
    "SOURCE_MANUAL_CHECK",
    "SOURCE_PIPELINE",
    "STAGES",
    "STAGE_FINAL",
    "STAGE_FIRST_DRAFT",
    "STAGE_MANUAL",
    "STAGE_PATCHED",
    "STAGE_REVISION",
    "attach_judge",
    "copy_check_summary",
    "latest_scene_readings",
    "normalize_judge",
    "policy_essentials",
    "project_fidelity_summary",
    "reading_for_text",
    "record_author_draft_reading",
    "reading_payload",
    "recent_gap_details",
    "recent_gap_readings",
    "recent_gaps",
    "record_fidelity_reading",
    "scene_project_id",
    "text_sha256",
]
