"""作品的「像不像」汇总（``readings.project_fidelity_summary``）改成几条定向查询之后（B10-20）：

- 与原来「把作品的全部读数读进来、在内存里按时间顺序逐行覆盖」的做法逐字节相同——走势、每场最新终稿（连键序）、
  按维平均（连求和次序）、读数总数；随机数据对照原实现；
- 不再把全部读数整行读进来：一场有几百条首稿读数时，读进内存的只有走势那 ``trend_limit`` 条与每场最新的几条。
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from novel_system.db.models import StyleFidelityReading
from novel_system.services.style_reference import readings as R
from novel_system.services.value_coercion import finite_or_none

_DIMS = ("language.vocabulary", "language.punctuation", "rhythm.sentence_length", "dialogue.tags", "made_up.dim")


def _oracle_summary(session: Session, project_id: str, *, profile_id: str | None, trend_limit: int) -> dict[str, Any]:
    """改写之前的实现（原样）：全部读数按时间升序读进来，逐行覆盖。"""
    stmt = select(StyleFidelityReading).where(StyleFidelityReading.project_id == str(project_id))
    if profile_id:
        stmt = stmt.where(StyleFidelityReading.profile_id == str(profile_id))
    rows = list(
        session.scalars(stmt.order_by(StyleFidelityReading.created_at.asc(), StyleFidelityReading.reading_id.asc()))
    )
    trend_rows = [row for row in rows if row.stage in (R.STAGE_FIRST_DRAFT, R.STAGE_FINAL)]
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
            "max_percentile": finite_or_none((row.reading_json or {}).get("max_percentile")),
            "created_at": row.created_at,
        }
        for row in trend_rows[-max(0, int(trend_limit)) :]
    ]
    latest_final: dict[str, StyleFidelityReading] = {}
    for row in rows:
        if row.stage == R.STAGE_FINAL and row.scene_id:
            latest_final[str(row.scene_id)] = row
    deterministic: dict[str, list[float]] = {}
    for row in latest_final.values():
        for dim, score in dict((row.reading_json or {}).get("dimension_scores") or {}).items():
            number = finite_or_none(score)
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
            number = finite_or_none(entry.get("score") if isinstance(entry, Mapping) else entry)
            if number is not None:
                judged.setdefault(str(dim), []).append(number)
    labels = list(R.DIMENSION_LABELS)
    dims = sorted(set(deterministic) | set(judged), key=lambda dim: labels.index(dim) if dim in labels else 99)
    gap_details = R.recent_gap_details(session, project_id=project_id, profile_id=profile_id)
    return {
        "trend": trend,
        "recent_gaps": [entry["phrase"] for entry in gap_details],
        "recent_gap_details": gap_details,
        "scene_finals": {scene_id: R._scene_final_brief(row) for scene_id, row in latest_final.items()},
        "dimension_averages": {
            dim: {
                "label": R.DIMENSION_LABELS.get(dim, dim),
                "deterministic": R._average(deterministic.get(dim, [])),
                "judge": R._average(judged.get(dim, [])),
                "scenes": len(deterministic.get(dim, [])),
                "judged": len(judged.get(dim, [])),
            }
            for dim in dims
        },
        "reading_count": len(rows),
        "final_scene_count": len(latest_final),
    }


_UNSET = object()


def _judge(rng: random.Random) -> Any:
    roll = rng.random()
    if roll < 0.3:
        return _UNSET  # 列没写（SQL NULL）
    if roll < 0.4:
        return None  # JSON 的 null
    if roll < 0.5:
        return [1, 2]  # 不是对象
    if roll < 0.55:
        return {}
    dims = rng.sample(_DIMS, rng.randint(1, 3))
    return {
        "dimensions": {
            dim: ({"score": round(rng.uniform(0, 10), 3)} if rng.random() < 0.8 else round(rng.uniform(0, 10), 3))
            for dim in dims
        }
    }


def _reading(rng: random.Random, index: int) -> StyleFidelityReading:
    percentile = round(rng.uniform(0, 100), 4)
    data: dict[str, Any] = {"within_range": rng.random() < 0.6, "window_count": 40}
    if rng.random() < 0.7:
        data["reliable"] = rng.random() < 0.8
    if rng.random() < 0.5:
        data["max_percentile"] = rng.choice([85.0, 90.0, "not-a-number", None])
    if rng.random() < 0.7:
        data["dimension_scores"] = {dim: round(rng.uniform(0, 10), 5) for dim in rng.sample(_DIMS, rng.randint(1, 4))}
    if rng.random() < 0.3:
        data["out_of_band"] = [{"feature": "fw_modal_per_1k", "z": -2.4, "direction": "low", "dimension": "language.vocabulary"}]
    kwargs: dict[str, Any] = {
        # 读数 id 与时间都打乱：同一时刻的几条按 id 定先后
        "reading_id": f"rd_{rng.randrange(10**6):06d}_{index}",
        "project_id": rng.choice(["P_Q1", "P_Q1", "P_Q2"]),
        "scene_id": rng.choice([None, "", "SC_1", "SC_2", "SC_3", "SC_4"]),
        "profile_id": rng.choice(["pf_a", "pf_a", "pf_b"]),
        "source": rng.choice(R.SOURCES),
        "stage": rng.choice(R.STAGES),
        "draft_ref": None,
        "text_sha256": f"sha_{index}",
        "char_count": 1800,
        "percentile": percentile,
        "distance": round(rng.uniform(0, 2), 4),
        "reading_json": data,
        "created_at": f"2026-09-2{rng.randint(0, 3)}T0{rng.randint(0, 2)}:00:00",
    }
    judge = _judge(rng)
    if judge is not _UNSET:
        kwargs["judge_json"] = judge
    return StyleFidelityReading(**kwargs)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)  # 不排序：键序也要一样


def test_summary_matches_the_row_by_row_implementation_on_random_readings(session: Session) -> None:
    rng = random.Random(20260930)
    index = 0
    for trial in range(30):
        session.query(StyleFidelityReading).delete()
        rows = []
        for _ in range(rng.randint(0, 45)):
            index += 1
            rows.append(_reading(rng, index))
        session.add_all(rows)
        session.commit()
        for project_id in ("P_Q1", "P_Q2", "P_NONE"):
            for profile_id in (None, "pf_a", "pf_b"):
                for trend_limit in (R.TREND_LIMIT, 3, 0):
                    expected = _oracle_summary(session, project_id, profile_id=profile_id, trend_limit=trend_limit)
                    actual = R.project_fidelity_summary(
                        session, project_id, profile_id=profile_id, trend_limit=trend_limit
                    )
                    assert _dump(actual) == _dump(expected), (trial, project_id, profile_id, trend_limit)


def test_summary_reads_only_the_trend_window_and_the_latest_rows(session: Session) -> None:
    rows = [
        StyleFidelityReading(
            reading_id=f"fd_{i:04d}",
            project_id="P_BIG",
            scene_id=f"SC_{i % 3}",
            profile_id="pf_a",
            source=R.SOURCE_PIPELINE,
            stage=R.STAGE_FIRST_DRAFT if i % 10 else R.STAGE_FINAL,
            draft_ref=None,
            text_sha256=f"sha_{i}",
            char_count=1800,
            percentile=40.0,
            distance=0.5,
            reading_json={"within_range": True, "reliable": True, "dimension_scores": {"language.vocabulary": 6.0}},
            judge_json={"dimensions": {"language.vocabulary": {"score": 7.0}}} if i % 7 == 0 else None,
            created_at=f"2026-09-23T{i // 60:02d}:{i % 60:02d}:00",
        )
        for i in range(300)
    ]
    session.add_all(rows)
    session.commit()
    session.expunge_all()

    loaded: list[str] = []

    def _count(target: StyleFidelityReading, _context: Any) -> None:
        loaded.append(target.reading_id)

    event.listen(StyleFidelityReading, "load", _count)
    try:
        summary = R.project_fidelity_summary(session, "P_BIG", profile_id="pf_a")
    finally:
        event.remove(StyleFidelityReading, "load", _count)
    assert summary["reading_count"] == 300
    assert len(summary["trend"]) == R.TREND_LIMIT
    assert summary["final_scene_count"] == 3
    # 走势 60 条 + 每场最新终稿 3 条 + 每场最新评审 3 条（有重叠也按次数算）；原来是 300 条整行
    assert len(loaded) <= R.TREND_LIMIT + 3 + 3
