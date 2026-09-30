"""场景正文的确定性连续性检查——硬 / 软质检与成稿门共用这一份（B04-03）。

两类确定性 issue（``source="deterministic"``；分级由 ``quality_classifier`` 做）：

* ``mechanical_required_beat_listing``：必写节拍被堆在段尾当清单（Q2）；
* ``event_log_consistency_violation``：正文与叙事事件账本记下的硬事实矛盾（关键词匹配，Q1）。

事件账本读不出（库出错、场景没有作品归属……）时不猜：``unavailable_error`` 只记异常的类型名（异常原文可能夹带
库里的内容），由调用方各自报成不阻断的提示。质检引擎在这上面加 severity 与修改简报，成稿门做分级与作者豁免。
以前两边各写一份，质检那份还另开一个会话读库（读的是别的连接已提交的快照、绕开了调用方的事务）；现在一律用
调用方给的会话——查询前的 autoflush 把同一事务里刚记下、还没提交的事件也算进来（测试
``test_continuity_extended_facts`` 钉着这一条）。
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


def event_log_violation_message(entity: str, fact_key: str, expected: str, actual: str) -> str:
    """事件账本矛盾那条 issue 的说明。``entity`` 给作者看的是人物名（没有名字才是账本里的 id）。"""
    return f"Event log contradiction: {entity}.{fact_key} expected '{expected}' but text suggests '{actual}'"


def reference_message(issue: dict[str, Any]) -> str | None:
    """事件账本矛盾的说明按账本 id 写的那一份；别的 issue → None。

    质检从 issue 的说明里取词（证据位置、与场景卡的冲突、去重）；说明里的人物名是在指这个人物，不是质检要改的词——
    取词仍按 id 算，与说明里印 id 的时候一样（名字多半写在场景卡上，按名字取词会凭空多出「场景卡冲突」，
    把一条确定性的事实矛盾升级成人工复核，I1-R1(a)）。"""
    if issue.get("issue_key") != EVENT_LOG_VIOLATION_KEY or issue.get("source") != "deterministic":
        return None
    details = issue.get("details")
    if not isinstance(details, dict) or not details.get("entity_id"):
        return None
    return event_log_violation_message(
        str(details["entity_id"]),
        str(details.get("fact_key") or ""),
        str(details.get("expected") or ""),
        str(details.get("actual") or ""),
    )


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
            # 说明印人物名（批准#14：正史事实按人物名找、摘要显示名字）；账本 id 留在 details 里（分类器按它拼
            # authority_ref）
            "message": event_log_violation_message(
                violation.entity_name or violation.entity_id,
                violation.fact_key,
                violation.expected,
                violation.actual,
            ),
            "source": "deterministic",
            "details": {
                "entity_id": violation.entity_id,
                "entity_name": violation.entity_name or violation.entity_id,
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
