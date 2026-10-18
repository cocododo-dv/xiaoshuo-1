"""终稿逐条复核（2026-10-18）：准终稿评审对照这一场的场景结构，逐条说计划落地了没有。

硬 QC 逐条核计划，但只核**首稿**（``scene_run/pipeline.py`` 记的 ``hard_qc_source_draft_row_id``）；之后正文还会被整场
改写——风格稿、按维修改、准终稿重写（实库 SC05：交给作者的那一稿与硬 QC 核过的那一稿相似度只有 0.64）。准终稿评审看的
是最终那一稿，以前只给 11 个分数和「坩埚可辨 / 三拍落地 / 成不成立」三问。这里把它拆细（思路借自 QMAI 的
chapter-plan-compliance 与人格核验的「每条规则恰好回一条」）：

- **条目**（:func:`plan_items`）：从 bundle 里冻结的 Scene Structure 段（``scene_structure_brief`` 渲染的那几行）认出
  视角、坩埚、三拍、后续拍子、代价、读者应感到、必须揭开、先别揭、出场变化、钩子——写了才列，「未规划」不列。
  读冻结的文字而不是场景卡现在的样子：评审对照的就是起草时交给模型的那一份计划。
- **问法**：准终稿评审的 user 消息末尾列出条目（:func:`plan_check_prompt`），结构化输出按这一场的条目**动态**加一个
  ``plan_check`` 对象——每个条目 id 一个必填成员（:func:`apply_plan_check_schema`）。按 schema 约束解码的中转（实库
  那条）因此不会漏条；不约束的模型漏了，归一时记进 ``missing``、``complete = False``，不重问、不拦。
- **等级**：``kept`` 照办 / ``mostly`` 基本照办 / ``partly`` 部分偏离 / ``missed`` 明显偏离；「先别揭」照办 = 正文没有揭开。
  证据是正文里的原句（在稿子里找得到才算钉住，``located``）。
- **去处**：结果随准终稿评审的尝试记录存（``AttemptTracker.details_json.plan_check``，与场景三问同一处），写前笔记与
  工作台读它；部分偏离 / 明显偏离另作为发现进评审记录（:func:`plan_check_findings`，维度 ``plan_adherence``），
  诊断抽屉按段落钉住、可按 signal id 忽略。**不改变放行**：通过与否、自动重写照旧只看评审的分数与失败类。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PLAN_CHECK_VERSION = "plan_check_v1"
PLAN_CHECK_KEY = "plan_check"
PLAN_DIMENSION = "plan_adherence"

GRADES: tuple[str, ...] = ("kept", "mostly", "partly", "missed")
GRADE_LABELS: dict[str, str] = {"kept": "照办", "mostly": "基本照办", "partly": "部分偏离", "missed": "明显偏离"}
# 进诊断抽屉的两级与它们的严重度（明显偏离要改；部分偏离给作者看一眼）
FINDING_SEVERITY: dict[str, str] = {"missed": "revision", "partly": "info"}

# (条目 id, 行首, 给作者看的名字)——行首与 ``scene_structure_brief.render_scene_structure_brief`` 写出的完全一致
# （``tests/test_plan_check.py`` 渲染一份全字段的简报再读回来，两边漂了就失败）。
ITEM_LINES: tuple[tuple[str, str, str], ...] = (
    ("pov", "POV character: ", "视角"),
    ("crucible", "Scene crucible (坩埚): ", "坩埚"),
    ("goal", "Goal (目标): ", "目标"),
    ("conflict", "Conflict (冲突): ", "冲突"),
    ("setback", "Setback (挫折): ", "挫折"),
    ("reaction", "Reaction (反应): ", "反应"),
    ("dilemma", "Dilemma (两难): ", "两难"),
    ("decision", "Decision (决定): ", "决定"),
    ("follow_up", "Follow-up beats (after the primary trio, in this order): ", "后续拍子"),
    ("cost", "Cost paid (代价): ", "代价"),
    ("reader_feel", "Reader should feel (读者应感到): ", "读者应感到"),
    ("must_reveal", "Must reveal: ", "必须揭开"),
    ("must_withhold", "Must withhold: ", "先别揭"),
    ("exit_change", "Exit change: ", "出场变化"),
    ("hook", "Next-scene pull (钩子): ", "钩子"),
)
ITEM_LABELS: dict[str, str] = {item_id: label for item_id, _prefix, label in ITEM_LINES}
_UNPLANNED = "未规划"
# 评审给的证据原句最多留这么长（给作者看，也拿去正文里找）
EVIDENCE_MAX_CHARS = 160

_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["grade", "evidence_excerpt", "note"],
    "properties": {
        "grade": {"type": "string", "enum": list(GRADES)},
        "evidence_excerpt": {"type": "string"},
        "note": {"type": "string"},
    },
}


@dataclass(frozen=True)
class PlanItem:
    id: str
    label: str
    planned: str

    def to_json(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "planned": self.planned}


def plan_items(brief_text: str | None) -> list[PlanItem]:
    """冻结的 Scene Structure 段 → 条目（按段里的先后；没有该行 / 写的是「未规划」→ 不列）。"""
    items: list[PlanItem] = []
    seen: set[str] = set()
    for raw in str(brief_text or "").splitlines():
        line = raw.strip()
        for item_id, prefix, label in ITEM_LINES:
            if item_id in seen or not line.startswith(prefix):
                continue
            planned = line[len(prefix) :].strip()
            if planned and planned != _UNPLANNED:
                items.append(PlanItem(id=item_id, label=label, planned=planned))
                seen.add(item_id)
            break
    return items


def plan_items_from_bundle(bundle: Mapping[str, Any] | None) -> list[PlanItem]:
    snapshot = bundle.get("snapshot") if isinstance(bundle, Mapping) else None
    digests = snapshot.get("inline_digests") if isinstance(snapshot, Mapping) else None
    brief = digests.get("scene_structure_brief") if isinstance(digests, Mapping) else None
    return plan_items(brief if isinstance(brief, str) else None)


def plan_check_prompt(items: Sequence[PlanItem]) -> str:
    """附在准终稿评审 user 消息末尾的条目清单（怎么判写在模板里）。"""
    lines = ["## Plan Items (fill plan_check with exactly one entry for every id below)"]
    lines.extend(f"- {item.id} — {item.label}: {item.planned}" for item in items)
    return "\n".join(lines)


def apply_plan_check_schema(schema: Mapping[str, Any] | None, items: Sequence[PlanItem]) -> dict[str, Any] | None:
    """这一次调用的 schema：``plan_check`` 换成「每个条目 id 一个必填成员」的对象并设为必填；没有条目 → 去掉它。"""
    if not isinstance(schema, Mapping):
        return None
    out = copy.deepcopy(dict(schema))
    properties = dict(out.get("properties") or {})
    required = [key for key in out.get("required") or [] if key != PLAN_CHECK_KEY]
    if items:
        properties[PLAN_CHECK_KEY] = {
            "type": "object",
            "additionalProperties": False,
            "required": [item.id for item in items],
            "properties": {item.id: copy.deepcopy(_ITEM_SCHEMA) for item in items},
        }
        required.append(PLAN_CHECK_KEY)
    else:
        properties.pop(PLAN_CHECK_KEY, None)
    out["properties"] = properties
    out["required"] = required
    return out


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _grade(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in GRADES:
        return text
    by_label = {label: grade for grade, label in GRADE_LABELS.items()}
    return by_label.get(str(value or "").strip())


def normalize_plan_check(raw: Any, items: Sequence[PlanItem], source_text: str) -> dict[str, Any] | None:
    """评审回的 ``plan_check`` → 存下来的形状；没有条目 → None。等级认不出的条目算没回（``missing``）。"""
    if not items:
        return None
    answers = raw if isinstance(raw, Mapping) else {}
    compact_source = _compact(source_text)
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    counts = {grade: 0 for grade in GRADES}
    for item in items:
        answer = answers.get(item.id)
        grade = _grade(answer.get("grade")) if isinstance(answer, Mapping) else None
        if grade is None:
            missing.append(item.id)
            rows.append({**item.to_json(), "grade": None, "grade_label": "没回", "evidence_excerpt": "", "located": False, "note": ""})
            continue
        excerpt = " ".join(str(answer.get("evidence_excerpt") or "").split())[:EVIDENCE_MAX_CHARS]
        counts[grade] += 1
        rows.append(
            {
                **item.to_json(),
                "grade": grade,
                "grade_label": GRADE_LABELS[grade],
                "evidence_excerpt": excerpt,
                "located": bool(excerpt) and _compact(excerpt) in compact_source,
                "note": " ".join(str(answer.get("note") or "").split()),
            }
        )
    return {
        "version": PLAN_CHECK_VERSION,
        "items": rows,
        "counts": counts,
        "missing": missing,
        "complete": not missing,
    }


def plan_check_findings(plan_check: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """部分偏离 / 明显偏离 → 评审记录里的发现（诊断抽屉的「起草评审」来源读它们）。"""
    findings: list[dict[str, Any]] = []
    for row in (plan_check or {}).get("items") or ():
        grade = row.get("grade") if isinstance(row, Mapping) else None
        if grade not in FINDING_SEVERITY:
            continue
        note = str(row.get("note") or "").strip()
        issue = f"计划里的「{row['label']}」{GRADE_LABELS[grade]}：{row['planned']}"
        findings.append(
            {
                "dimension": PLAN_DIMENSION,
                "plan_item": row["id"],
                "severity": FINDING_SEVERITY[grade],
                "issue": issue + (f"——{note}" if note else ""),
                "recommendation": (
                    "这是否是你要的偏离？要照计划就按这一条改这一处；偏离得好就忽略这一条。"
                    if grade == "partly"
                    else "照计划改这一处，或者回到写前笔记 / 第 10 步改计划。"
                ),
                "evidence_excerpt": str(row.get("evidence_excerpt") or ""),
            }
        )
    return findings


__all__ = [
    "EVIDENCE_MAX_CHARS",
    "FINDING_SEVERITY",
    "GRADES",
    "GRADE_LABELS",
    "ITEM_LABELS",
    "ITEM_LINES",
    "PLAN_CHECK_KEY",
    "PLAN_CHECK_VERSION",
    "PLAN_DIMENSION",
    "PlanItem",
    "apply_plan_check_schema",
    "normalize_plan_check",
    "plan_check_findings",
    "plan_check_prompt",
    "plan_items",
    "plan_items_from_bundle",
]
