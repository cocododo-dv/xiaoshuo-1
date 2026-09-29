"""场景正文的确定性连续性检查——硬 / 软质检与成稿门共用这一份（B04-03）。

两类确定性 issue（``source="deterministic"``；分级由 ``quality_classifier`` 做）：

* ``mechanical_required_beat_listing``：必写节拍被堆在段尾当清单（Q2）；
* ``event_log_consistency_violation``：正文与叙事事件账本记下的硬事实矛盾（关键词匹配，Q1）。

事件账本读不出（库出错、场景没有作品归属……）时不猜：``unavailable_error`` 只记异常的类型名（异常原文可能夹带
库里的内容），由调用方各自报成不阻断的提示。质检引擎在这上面加 severity 与修改简报，成稿门做分级与作者豁免。
以前两边各写一份，质检那份还另开一个会话读库（读的是别的连接已提交的快照、绕开了调用方的事务）；现在一律用
调用方给的会话。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import SceneCard
from novel_system.services.character_continuity import detect_mechanical_required_beat_listing
from novel_system.services.narrative_event_log import NarrativeEventLog
from novel_system.services.scene_ownership import require_scene_project_id

REQUIRED_BEAT_LISTING_KEY = "mechanical_required_beat_listing"
EVENT_LOG_VIOLATION_KEY = "event_log_consistency_violation"
CONTINUITY_UNAVAILABLE_KEY = "continuity_validation_unavailable"


@dataclass(frozen=True)
class ContinuityCheck:
    """一次检查的结果：确定性 issue（按节拍清单、事件账本的次序），事件账本没查成时的异常类型名。"""

    issues: list[dict[str, Any]] = field(default_factory=list)
    unavailable_error: str | None = None


def deterministic_continuity_issues(session: Session, scene: SceneCard, content: str) -> ContinuityCheck:
    issues: list[dict[str, Any]] = []
    listing = detect_mechanical_required_beat_listing(content=content, must_include_text=scene.must_include_text)
    if listing is not None:
        issues.append({**listing, "source": "deterministic"})
    try:
        project_id = require_scene_project_id(session, scene)
        report = NarrativeEventLog(session).check_consistency(
            content,
            project_id,
            scene.scene_id,
            character_ids=scene.onstage_chars_json or [],
        )
    except Exception as exc:  # noqa: BLE001 — 没有确定性证据就不下 Q1 结论；原因只记类型名
        return ContinuityCheck(issues=issues, unavailable_error=type(exc).__name__)
    issues.extend(
        {
            "issue_key": EVENT_LOG_VIOLATION_KEY,
            "message": (
                f"Event log contradiction: {violation.entity_id}.{violation.fact_key} "
                f"expected '{violation.expected}' but text suggests '{violation.actual}'"
            ),
            "source": "deterministic",
            "details": {
                "entity_id": violation.entity_id,
                "fact_key": violation.fact_key,
                "expected": violation.expected,
                "actual": violation.actual,
                "evidence": violation.evidence,
                "source": violation.source,
            },
        }
        for violation in report.violations
    )
    return ContinuityCheck(issues=issues)
