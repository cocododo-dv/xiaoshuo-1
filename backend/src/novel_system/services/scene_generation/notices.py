"""风格链路的非静默提示（notices）：码表、构造、注入 / styled-draft gate 结果的翻译，以及 API 回读。

每条 ``{"code", "message", "severity", ...}``；同一份写进 AttemptTracker.details_json["notices"]，工作台与场景运行的
响应按 bundle 回读（:func:`latest_style_notices`）。码表是封闭集合，前端按码映射文案。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import AttemptTracker
from novel_system.services.style_prompt_injection import STYLE_USER_TAIL_KEY, STYLED_GATE_UNAVAILABLE_VERDICT
from novel_system.services.style_reference import style_step


# ---------------------------------------------------------------------------
# 2026-09 风格模仿 v2（W5）：风格链路 notices
# ---------------------------------------------------------------------------
STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL = "STYLE_DRAFT_FALLBACK_NEUTRAL"
# 2026-09-12 风格直起:首稿已按参考作者手笔直接起草(信息级,不是警告)。
STYLE_NOTICE_FIRST_DRAFT = "STYLE_FIRST_DRAFT"
STYLE_NOTICE_INJECTION_MISS = "STYLE_INJECTION_MISS"
STYLE_NOTICE_INJECTION_DEGRADED = "STYLE_INJECTION_DEGRADED"
STYLE_NOTICE_PLAGIARISM_HIT = "STYLE_PLAGIARISM_HIT"
STYLE_NOTICE_BANNED_TERM_HIT = "STYLE_BANNED_TERM_HIT"
# styled-draft gate 自身没跑成（校验异常 / 契约损坏 / 参考书已删）：抄袭 / 禁用词检查
# 没有执行过，不能与「无绑定」混为一谈。
STYLE_NOTICE_GATE_UNAVAILABLE = "STYLE_GATE_UNAVAILABLE"
# 风格参考 v3（P5b）：风格步按读数决定——首稿在作者范围内不调模型（信息级）；定向修改不更像 / 没过抄袭门时保留首稿
# （信息级）；软补丁让稿子离作者更远时退回补丁前的稿子（信息级，STYLE_PATCH_REVERTED 由编排器写）。
STYLE_NOTICE_FIRST_DRAFT_ACCEPTED = "STYLE_FIRST_DRAFT_ACCEPTED"
STYLE_NOTICE_REVISION_REJECTED = "STYLE_REVISION_REJECTED"
STYLE_NOTICE_PATCH_REVERTED = "STYLE_PATCH_REVERTED"
# 注入适配器审计里的提示（inject.render / inject.selection 的 notices）原样用它们的码翻成风格链路 notice：
# 书在冻结后改过（按当前索引挑样例）/ 云策略不让发原文 / 书不在了 / 书还没有样例窗口。
STYLE_NOTICE_REFERENCE_BOOK_CHANGED = "STYLE_REFERENCE_BOOK_CHANGED"
STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED = "STYLE_REFERENCE_SAMPLES_BLOCKED"
STYLE_NOTICE_REFERENCE_BOOK_MISSING = "STYLE_REFERENCE_BOOK_MISSING"
STYLE_NOTICE_REFERENCE_NO_WINDOWS = "STYLE_REFERENCE_NO_WINDOWS"
_RENDER_AUDIT_NOTICE_MESSAGES: dict[str, str] = {
    STYLE_NOTICE_REFERENCE_BOOK_CHANGED: "参考书的段落在冻结之后改过，本场按当前的窗口索引挑了样例。",
    STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED: "这本参考书的云端策略不允许把原文发给当前模型，本场只用了文风卡与声音特征，没有原文样例。",
    STYLE_NOTICE_REFERENCE_BOOK_MISSING: "绑定的参考书已不在书库里，本场没有原文样例。",
    STYLE_NOTICE_REFERENCE_NO_WINDOWS: "参考书还没有可用的样例窗口（段落分类未完成或正文太少），本场没有原文样例。",
}
STYLE_NOTICE_CODES: frozenset[str] = frozenset(
    {
        STYLE_NOTICE_DRAFT_FALLBACK_NEUTRAL,
        STYLE_NOTICE_INJECTION_MISS,
        STYLE_NOTICE_INJECTION_DEGRADED,
        STYLE_NOTICE_PLAGIARISM_HIT,
        STYLE_NOTICE_BANNED_TERM_HIT,
        STYLE_NOTICE_GATE_UNAVAILABLE,
        STYLE_NOTICE_FIRST_DRAFT,
        STYLE_NOTICE_FIRST_DRAFT_ACCEPTED,
        STYLE_NOTICE_REVISION_REJECTED,
        STYLE_NOTICE_PATCH_REVERTED,
        *_RENDER_AUDIT_NOTICE_MESSAGES,
    }
)


# 风格链路 notices 落在哪些 AttemptTracker.step 上（API 回读按 bundle 合并这几步的最近
# 一次 completed 尝试）：style_draft 与 near_final_rewrite（step=scene_literary_rewrite）。
# 2026-09-12 风格直起:style_first 下中性步位的首稿也带 notices(首稿直起 / 注入未命中 /
# 抄袭或禁用词命中);neutral_first 下该步没有 notices,合并时自然为空。
# 风格参考 v3（P5b）：软补丁的去留（保留 / 退回）记在 step=style_patch_keep 的尝试上。
STYLE_PATCH_KEEP_STEP = style_step.STYLE_PATCH_KEEP_STEP
STYLE_NOTICE_ATTEMPT_STEPS: tuple[str, ...] = (
    "neutral_draft",
    "style_draft",
    STYLE_PATCH_KEEP_STEP,
    "scene_literary_rewrite",
)


_STYLE_NOTICE_SEVERITIES = ("info", "warning", "error", "blocking")


def _prompt_carries_style_reference(prompt: Mapping[str, Any] | None) -> bool:
    if not isinstance(prompt, Mapping):
        return False
    audit = prompt.get("_style_reference_runtime_audit")
    if isinstance(audit, Mapping) and str(audit.get("outcome") or "") in {"degraded", "degraded_budget", "miss"}:
        return False
    if isinstance(audit, Mapping) and str(audit.get("outcome") or "") == "injected":
        return True
    if str(prompt.get(STYLE_USER_TAIL_KEY) or "").strip():
        return True
    # 注入器把 [STYLE_REFERENCE] 块接在 system 提示最前面；只认开头——风格通道模板的正文自己也提到这个块名
    return str(prompt.get("system_prompt") or "").lstrip().startswith("[STYLE_REFERENCE]")


def style_notice(
    code: str,
    message: str,
    *,
    severity: str = "warning",
    **details: Any,
) -> dict[str, Any]:
    """构造一条风格链路 notice（JSON 友好，供 API 直通）。"""
    if code not in STYLE_NOTICE_CODES:
        raise ValueError(f"unknown style notice code: {code}")
    if severity not in _STYLE_NOTICE_SEVERITIES:
        raise ValueError(f"unknown style notice severity: {severity}")
    notice: dict[str, Any] = {"code": code, "message": message, "severity": severity}
    for key, value in details.items():
        if value is not None:
            notice[key] = value
    return notice


def style_injection_notices(prompt: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """把 ``prompt["_style_reference_runtime_audit"]`` 的注入结果翻译成 notices。

    - 无审计键：没有可用的参考绑定（严格 no-op）→ 无 notice（无参考不是故障）；
    - ``outcome == "miss"``：契约已冻结但没渲染出任何块 → STYLE_INJECTION_MISS；
    - ``outcome in {"degraded", "degraded_budget"}`` → STYLE_INJECTION_DEGRADED。
    """
    if not isinstance(prompt, Mapping):
        return []
    audit = prompt.get("_style_reference_runtime_audit")
    if not isinstance(audit, Mapping):
        return []
    outcome = str(audit.get("outcome") or "")
    if outcome == "miss":
        return [
            style_notice(
                STYLE_NOTICE_INJECTION_MISS,
                "风格参考已绑定，但本次没有渲染出任何可注入的风格块；本稿未受参考风格约束。",
                severity="warning",
                contract_hash=audit.get("contract_hash"),
                profile_ids=list(audit.get("profile_ids") or []),
            )
        ]
    if outcome == "degraded":
        return [
            style_notice(
                STYLE_NOTICE_INJECTION_DEGRADED,
                "风格参考注入失败，已回退到无风格前缀的基础提示；本稿未受参考风格约束。",
                severity="error",
                error_code=audit.get("error_code"),
                runtime_contract_status=audit.get("runtime_contract_status"),
            )
        ]
    if outcome == "degraded_budget":
        return [
            style_notice(
                STYLE_NOTICE_INJECTION_DEGRADED,
                "输入预算不足，风格参考前缀被整体裁掉；本稿未受参考风格约束。",
                severity="warning",
                budget_fit=deepcopy(audit.get("budget_fit")),
            )
        ]
    return render_audit_notices(audit)


def render_audit_notices(audit: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """注入适配器审计里的 ``notices``（书改过 / 原文被云策略挡下 / 书不在 / 没有样例窗口）→ 风格链路 notices。"""
    if not isinstance(audit, Mapping):
        return []
    notices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for code in audit.get("notices") or []:
        code = str(code or "")
        message = _RENDER_AUDIT_NOTICE_MESSAGES.get(code)
        if message is None or code in seen:
            continue
        seen.add(code)
        notices.append(
            style_notice(
                code,
                message,
                severity="warning",
                samples_blocked=audit.get("samples_blocked") if code == STYLE_NOTICE_REFERENCE_SAMPLES_BLOCKED else None,
            )
        )
    return notices


_STYLED_GATE_STAGE_LABEL = {
    "neutral_draft": "首稿",
    "style_draft": "风格稿",
    "near_final_rewrite": "准终稿重写稿",
}
_STYLED_GATE_STAGE_CONSEQUENCE = {
    "neutral_draft": "hard_qc 阶段的同一门将升级为人工复核。",
    "style_draft": "该稿不得直接成稿，soft_qc 阶段将升级为人工复核。",
    "near_final_rewrite": "该重写稿已被丢弃，终稿回退为重写前已过 gate 的风格稿。",
}


def _styled_draft_gate_notices(gate: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """styled-draft gate 结果 → notices。

    plagiarism 阻断级；生成禁用词命中 error 级；gate 自身未能执行（verdict
    ``unavailable``）error 级 STYLE_GATE_UNAVAILABLE。命中计数取 gate 的真实总数
    （``plagiarism_hit_count`` / ``forbidden_hit_count``），不是被截断到 8 条的证据列表长度。
    """
    if not isinstance(gate, Mapping):
        return []
    verdict = str(gate.get("verdict") or "")
    stage = str(gate.get("stage") or "style_draft")
    label = _STYLED_GATE_STAGE_LABEL.get(stage, "风格稿")
    consequence = _STYLED_GATE_STAGE_CONSEQUENCE.get(
        stage, _STYLED_GATE_STAGE_CONSEQUENCE["style_draft"]
    )
    notices: list[dict[str, Any]] = []
    if verdict == STYLED_GATE_UNAVAILABLE_VERDICT:
        notices.append(
            style_notice(
                STYLE_NOTICE_GATE_UNAVAILABLE,
                f"{label}的抄袭 / 生成禁用词检查未能执行；本稿未经参考来源安全核对，"
                "soft_qc 阶段将要求人工复核。",
                severity="error",
                stage=stage,
                error=gate.get("error"),
                error_code=gate.get("error_code"),
                profile_id=gate.get("profile_id"),
                runtime_contract_hash=gate.get("runtime_contract_hash"),
            )
        )
        return notices
    if verdict == "plagiarism" or gate.get("plagiarism_passed") is False:
        plagiarism_hits = gate.get("plagiarism_hits") or []
        notices.append(
            style_notice(
                STYLE_NOTICE_PLAGIARISM_HIT,
                f"{label}与参考作品原文存在确定性 n-gram 重叠（抄袭红线命中）；{consequence}",
                severity="blocking",
                stage=stage,
                hit_count=int(gate.get("plagiarism_hit_count") or len(plagiarism_hits)),
                profile_id=gate.get("profile_id"),
                runtime_contract_hash=gate.get("runtime_contract_hash"),
            )
        )
    forbidden_hits = gate.get("forbidden_hits") or []
    if forbidden_hits:
        notices.append(
            style_notice(
                STYLE_NOTICE_BANNED_TERM_HIT,
                f"{label}用了参考画像的生成禁用词 / 受保护专名；soft_qc 阶段会请你复核（可以接受），归档不因此被拦。",
                severity="error",
                stage=stage,
                hit_count=int(gate.get("forbidden_hit_count") or len(forbidden_hits)),
                terms=[
                    str(hit.get("matched_excerpt") or hit.get("pattern_statement") or "")
                    for hit in forbidden_hits
                    if isinstance(hit, Mapping)
                ][:8],
                profile_id=gate.get("profile_id"),
            )
        )
    return notices


def latest_style_notices(
    session: Session,
    scene_id: str,
    *,
    bundle_id: str | None = None,
) -> list[dict[str, Any]]:
    """回读风格链路写进 AttemptTracker 的 notices（API 直通用）。

    对 ``STYLE_NOTICE_ATTEMPT_STEPS`` 的每一步取最近一次 completed 尝试，按步序合并去重：
    style_draft 的 notices 在前，near_final_rewrite（step=scene_literary_rewrite）在后。
    传 ``bundle_id`` 时只看该 bundle（API 层必须传——一次运行的响应不能带上别的运行的
    notices）；不传则取场景最近一次，仅供直接调用方使用。

    风格参考 v3（L7）：Best-of-N 的一次运行有好几份风格稿尝试，``style_draft`` 这一步取**选中**的那一份候选的
    notices（:func:`~novel_system.services.style_fidelity_view.selected_style_row_id`），不是最后一个槽位的。
    """
    from novel_system.services.style_fidelity_view import selected_style_row_id

    selected_row = selected_style_row_id(session, scene_id, bundle_id) if bundle_id else None
    merged: list[dict[str, Any]] = []
    for step in STYLE_NOTICE_ATTEMPT_STEPS:
        stmt = (
            select(AttemptTracker)
            .where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == step,
                AttemptTracker.status == "completed",
            )
            .order_by(AttemptTracker.attempt_id.desc())
        )
        if bundle_id:
            stmt = stmt.where(AttemptTracker.source_bundle_id == bundle_id)
        rows = list(session.execute(stmt).scalars())
        row = rows[0] if rows else None
        if step == "style_draft" and selected_row:
            row = next(
                (item for item in rows if str((item.details_json or {}).get("row_id") or "") == selected_row),
                row,
            )
        if row is None:
            continue
        raw = (row.details_json or {}).get("notices")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict) and item.get("code") and item not in merged:
                merged.append(deepcopy(item))
    return merged
