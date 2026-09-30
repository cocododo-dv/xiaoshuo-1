"""准定稿评审结果的归一化与房风门（纯函数；B03-21 从 near_final 拆出）。

评审模型的回答在这里变成统一的结果形状（``_normalize_acceptance_payload``：分数按模板声明的刻度换算、失败类
归一、场景三问）；没有绑定参考书时再过房风门（``_apply_scene_near_final_gates``：场景机制 + 模型腔，词表在
``house_taste_lexicons``）。评审自身执行失败时给 ``_execution_failure_payload``——正文照常交付。
"""

from __future__ import annotations

import re
from typing import Any

from novel_system.services.house_taste_lexicons import (
    CHOICE_MARKERS,
    COST_MARKERS,
    ENDING_ACTION_MARKERS,
    EXPLAINED_ENDING_SUFFIXES,
    MODEL_VOICE_PHRASES,
)
from novel_system.services.review_scores import normalize_score, response_score_scale

NEAR_FINAL_RUBRIC_ID = "near_final_acceptance_v1"
NEAR_FINAL_REWRITE_TYPE = "near_final_scene_rewrite"

SCENE_FAILURE_CLASSES = {
    "fact_blocker",
    "scene_structure_failure",
    "character_flatness",
    "prose_model_voice",
    "ending_weakness",
    "chapter_payoff_gap",
    "reference_safety",
}
AUTOMATED_REWRITE_FAILURE_CLASSES = {
    "scene_structure_failure",
    "character_flatness",
    "prose_model_voice",
    "ending_weakness",
    "chapter_payoff_gap",
}


SCENE_ACCEPTANCE_SCORE_KEYS = frozenset(
    {
        "story_necessity",
        "character_pressure",
        "forced_choice_pressure",
        "dialogue_edge",
        "information_release",
        "prose_freshness",
        "ending_drive",
        "continuity",
        "author_voice_match",
        "model_voice_risk",
        "reference_safety",
    }
)
CHAPTER_ACCEPTANCE_SCORE_KEYS = frozenset(
    {
        "chapter_promise",
        "escalation",
        "payoff_integrity",
        "character_shift",
        "ending_drive",
        "continuity",
    }
)


def should_rewrite(payload: dict[str, Any]) -> bool:
    """评审结果授权一次自动整场重写：没通过、不要人工、没被拦下、失败类属于可自动重写的那几类。"""
    if payload.get("pass_flag") or payload.get("requires_human_review"):
        return False
    if payload.get("auto_rewrite_blocked"):
        return False
    return str(payload.get("failure_class") or "") in AUTOMATED_REWRITE_FAILURE_CLASSES


def _promotion_blockers_from_acceptance(payload: dict[str, Any]) -> list[str]:
    if payload.get("pass_flag"):
        return []
    if payload.get("requires_human_review"):
        return ["human_review_required"]
    if str(payload.get("failure_class") or "") not in AUTOMATED_REWRITE_FAILURE_CLASSES:
        return [str(payload.get("failure_class") or payload.get("near_final_status") or "auto_rewrite_not_eligible")]
    return []


SCENE_STORY_CHECK_VERDICTS = ("yes", "no", "maybe")


def _normalize_scene_story_check(value: Any) -> dict[str, Any] | None:
    """2026-09-13 阶段 D：成稿后的场景三问（Ingermanson 的 Yes / No / Maybe 分诊）。

    评审在结构判断之外单独回答：坩埚在正文里认得出来吗、设计的三拍落地了吗，
    以及一句总判——Yes（这一场成立）/ No（不成立，重写或删）/ Maybe（能修）。
    只做归一，不改变通过与否：它是给作者的分诊提示，不是闸门。
    """
    if not isinstance(value, dict):
        return None

    def _flag(raw: Any) -> bool | None:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return bool(raw)
        text = str(raw or "").strip().lower()
        if text in {"true", "yes", "y", "是", "有", "1"}:
            return True
        if text in {"false", "no", "n", "否", "无", "0"}:
            return False
        return None

    crucible = _flag(value.get("crucible_identified"))
    shape = _flag(value.get("shape_landed"))
    verdict = str(value.get("verdict") or "").strip().lower()
    if verdict not in SCENE_STORY_CHECK_VERDICTS:
        if crucible is True and shape is True:
            verdict = "yes"
        elif crucible is False and shape is False:
            verdict = "no"
        elif crucible is None and shape is None:
            verdict = ""
        else:
            verdict = "maybe"
    note = _scalar_text(value.get("note")) or ""
    if crucible is None and shape is None and not verdict and not note:
        return None
    return {
        "crucible_identified": crucible,
        "shape_landed": shape,
        "verdict": verdict or None,
        "note": note,
    }


ACCEPTANCE_SCORE_FIELDS: tuple[str, ...] = ("overall_score", "scores")


def _normalize_acceptance_payload(payload: Any, *, schema: Any = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _execution_failure_payload("near-final reviewer returned an invalid payload")
    # 风格参考 v3（V7 / L5）：分数按模板声明的刻度（``schema`` = 这一次调用的 structured_schema，overall_score / scores
    # 的 maximum）逐个换算到 0–1，越界的丢掉——此前直接夹到 [0, 1]，0–10 的回答全部饱和成 1.0（实库 3/3 次
    # overall_score = 1.0）；按一次回答的最大分猜量级又会把全在 1 以下的回答读成满分。模板没声明（旧快照）才按回答推断。
    raw_scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    scale, _source = response_score_scale(
        schema, ACCEPTANCE_SCORE_FIELDS, [payload.get("overall_score"), *raw_scores.values()]
    )
    scores = {
        str(key): _score(value, scale) for key, value in raw_scores.items() if _score(value, scale) is not None
    }
    findings = [item for item in payload.get("findings", []) if isinstance(item, dict)] if isinstance(payload.get("findings"), list) else []
    revision_brief = _revision_brief_list(payload.get("revision_brief"))
    requires_human_review = bool(payload.get("requires_human_review"))
    pass_flag = bool(payload.get("pass_flag"))
    status = _scalar_text(payload.get("near_final_status"))
    if requires_human_review:
        status = "human_review_required"
        pass_flag = False
    elif status not in {"near_final_ready", "revision_required", "human_review_required"}:
        status = "near_final_ready" if pass_flag else "revision_required"
    failure_class = _scalar_text(payload.get("failure_class"))
    failure_class_coerced = False
    if pass_flag:
        failure_class = None
    elif failure_class not in SCENE_FAILURE_CLASSES:
        # 评审没给出合法失败类时归到 prose_model_voice(可自动重写类);2026-09-14 起把这次
        # 强转记下来——有绑定时它不再授权一次房风整场重写(见 _apply_style_bound_rewrite_policy)。
        failure_class_coerced = True
        failure_class = "prose_model_voice"
    overall_score = _score(payload.get("overall_score"), scale)
    return {
        "near_final_status": status,
        "pass_flag": pass_flag and status == "near_final_ready",
        "overall_score": overall_score,
        "scores": scores,
        "findings": findings,
        "revision_brief": revision_brief,
        "failure_class": failure_class,
        "failure_class_coerced": failure_class_coerced,
        "requires_human_review": requires_human_review or status == "human_review_required",
        "scene_story_check": _normalize_scene_story_check(payload.get("scene_story_check")),
    }


def _apply_style_bound_rewrite_policy(payload: dict[str, Any]) -> dict[str, Any]:
    """2026-09-14 风格保真修补:有绑定时自动重写只认评审自己的判断。

    近终稿重写是温度 0.55、允许重排 / 合并 / 拆段的整场改写,且改写稿直接成为终稿;
    有绑定时它只能由评审明确给出的失败类 **和** 修改简报触发——被强转的失败类
    (评审返回了未知类)或空简报(否则 orchestrator 会用房风默认简报)一律 ``auto_rewrite_blocked``,
    停在 revision_required 交给作者。neutral_first / 无绑定行为不变。
    """
    if payload.get("pass_flag") or payload.get("requires_human_review"):
        return payload
    reasons: list[str] = []
    if payload.get("failure_class_coerced"):
        reasons.append("failure_class_coerced")
    if not payload.get("revision_brief"):
        reasons.append("no_reviewer_brief")
    if not reasons:
        return payload
    return {**payload, "auto_rewrite_blocked": "style_bound:" + ",".join(reasons)}


def _bundle_style_bound(bundle: Any) -> bool:
    """房风门(词表门、收尾动作启发式、默认整场重写简报)是否让位给参考:只看 bundle 的 StylePolicy。"""
    try:
        from novel_system.services.style_policy import style_policy_for_bundle

        return style_policy_for_bundle(bundle).defers_house_taste()
    except Exception:  # noqa: BLE001 — 让位判定失败按无绑定处理(现状行为)
        return False


def _apply_scene_near_final_gates(
    payload: dict[str, Any],
    source_content: str,
    *,
    style_bound: bool = False,
) -> dict[str, Any]:
    if style_bound:
        # 2026-09-12 风格直起:词表门(她知道 / 忽然意识到 / 解释了一切…)与「结尾必须是动作」
        # 启发式都是房风;有绑定时整体让位,由带样例的验收评审(LLM)判断。
        return payload
    missing = _missing_scene_machinery(source_content)
    if not missing:
        model_voice_findings = _model_voice_gate_findings(source_content)
        if not model_voice_findings:
            return payload
        findings = [*model_voice_findings, *(payload.get("findings") or [])]
        revision_brief = list(payload.get("revision_brief") or [])
        revision_brief.insert(
            0,
            {
                "dimension": "model_voice_risk",
                "action": "删掉抽象总结、解释性因果和万能情绪句；把判断改成物件移动、沉默、反问或不可撤回动作。",
                "priority": "high",
            },
        )
        scores = dict(payload.get("scores") or {})
        scores["model_voice_risk"] = min(float(scores.get("model_voice_risk", 0.4) or 0.4), 0.4)
        scores["author_voice_match"] = min(float(scores.get("author_voice_match", 0.55) or 0.55), 0.55)
        overall_score = payload.get("overall_score")
        if payload.get("pass_flag"):
            overall_score = min(float(overall_score or 0.56), 0.56)
        return {
            **payload,
            "near_final_status": "revision_required",
            "pass_flag": False,
            "overall_score": overall_score,
            "scores": scores,
            "failure_class": "prose_model_voice",
            "requires_human_review": False,
            "findings": findings,
            "revision_brief": revision_brief,
        }
    scores = dict(payload.get("scores") or {})
    scores.setdefault("choice_pressure", 0.3)
    scores.setdefault("ending_drive", 0.3)
    findings = list(payload.get("findings") or [])
    findings.insert(
        0,
        {
            "dimension": "story_necessity",
            "severity": "blocker",
            "issue": "场景缺少可见选择、已支付代价或结尾动作。",
            "recommendation": "补足人物必须二选一的动作、选择带来的具体损失，以及能推动下一场的结尾动作。",
            "evidence_excerpt": _compact_text(source_content, limit=120),
            "evidence_location": "scene body",
            "why_it_matters": "准定稿不能只说明事情重要，必须让读者看见人物在压力下改变局面。",
            "missing_machinery": missing,
        },
    )
    revision_brief = list(payload.get("revision_brief") or [])
    if not revision_brief:
        revision_brief = _default_structure_revision_brief()
    overall_score = payload.get("overall_score")
    if payload.get("pass_flag"):
        overall_score = min(float(overall_score or 0.55), 0.54)
    return {
        **payload,
        "near_final_status": "revision_required",
        "pass_flag": False,
        "overall_score": overall_score,
        "scores": scores,
        "failure_class": "scene_structure_failure",
        "requires_human_review": False,
        "findings": findings,
        "revision_brief": revision_brief,
    }


def _missing_scene_machinery(content: str) -> list[str]:
    text = content or ""
    missing: list[str] = []
    if not _has_choice(text):
        missing.append("forced_choice")
    if not _has_cost(text):
        missing.append("price_paid")
    if not _has_ending_action(text):
        missing.append("ending_action")
    return missing


def _model_voice_gate_findings(content: str) -> list[dict[str, Any]]:
    text = content or ""
    terms = [term for term in MODEL_VOICE_PHRASES if term in text]
    if not terms:
        return []
    return [
        {
            "dimension": "model_voice_risk",
            "severity": "revision",
            "issue": f"准终稿仍保留模型腔或解释性总结：{'、'.join(terms[:4])}。",
            "recommendation": "把概括性判断改成角色必须承担的动作、物件转移、沉默或反问。",
            "evidence_excerpt": _compact_text(_first_term_window(text, terms[0]), limit=120),
            "evidence_location": "scene body",
            "why_it_matters": "强情节准终稿需要让读者自行从压力中推断意义，不能由叙述替读者总结。",
        }
    ]


def _has_choice(text: str) -> bool:
    return _contains_any(text, CHOICE_MARKERS)


def _has_cost(text: str) -> bool:
    return _contains_any(text, COST_MARKERS)


def _has_ending_action(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text or "")
    if not stripped:
        return False
    if stripped.endswith(EXPLAINED_ENDING_SUFFIXES):
        return False
    tail = stripped[-80:]
    return _contains_any(tail, ENDING_ACTION_MARKERS)


def _execution_failure_payload(message: str) -> dict[str, Any]:
    # Wave 2（治理 §5.4/§7.7）：评审自身执行失败不是正文的错——不再返回
    # human_review_required 硬语义；fail + 非自动重写 failure_class，由编排层
    # 按 Q2 警告随稿交付（QC 超时/模型不可用不撤销已有正文）。
    return {
        "near_final_status": "revision_required",
        "pass_flag": False,
        "overall_score": None,
        "scores": {},
        "findings": [
            {
                "dimension": "near_final_payload",
                "severity": "revision",
                "issue": message,
                "recommendation": "准定稿验收未能执行；正文照常交付，可修复模型输出后重跑验收。",
                "evidence_excerpt": "",
                "evidence_location": "review execution",
                "why_it_matters": "无效验收不能作为准定稿依据，但也不能撤销已有正文。",
            }
        ],
        "revision_brief": [],
        "failure_class": "fact_blocker",
        "requires_human_review": False,
    }


def _default_structure_revision_brief() -> list[dict[str, str]]:
    return [
        {
            "dimension": "story_necessity",
            "action": "补足人物选择、已支付代价、关系位移和结尾动作，不要用总结句替代场景推进。",
            "priority": "high",
        }
    ]


def _revision_brief_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            items.append(item)
        elif isinstance(item, str) and item.strip():
            items.append({"dimension": "near_final", "action": item.strip(), "priority": "medium"})
    return items


def _score(value: Any, scale: float = 1.0) -> float | None:
    """一个分数按刻度换算到 0–1（见 ``review_scores``）；非数值 / NaN / 越界 → None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return normalize_score(value, scale)


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    return ""


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term.lower() in lowered for term in terms)


def _first_term_window(text: str, term: str) -> str:
    index = text.find(term)
    if index < 0:
        return text[:120]
    return text[max(0, index - 36) : index + len(term) + 64]


def _compact_text(text: str, limit: int = 1600) -> str:
    stripped = (text or "").strip()
    if len(stripped) <= limit:
        return stripped
    return f"{stripped[:limit].rstrip()}\n...[truncated]..."
