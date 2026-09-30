from __future__ import annotations

from novel_system.contracts.qc import HardQCOutput, SoftQCOutput


class QCValidationError(ValueError):
    pass


VALID_HARD_COMBOS = {
    ("hard_pass", True, "pass"),
    ("hard_fail_partial", False, "partial_rewrite"),
    ("hard_fail_full", False, "full_rewrite"),
    ("hard_block_human", False, "human_review_required"),
}

VALID_SOFT_COMBOS = {
    ("soft_pass", True, "pass"),
    ("soft_patch", False, "patch"),
    ("soft_waive", True, "pass_with_notes"),
    ("soft_block_human", False, "human_review_required"),
}

ALLOWED_QC_KEYS = {
    "hard_qc": set(HardQCOutput.model_fields),
    "soft_qc": set(SoftQCOutput.model_fields),
}


def validate_qc_report(qc_type: str, payload: dict):
    normalized_payload = _normalize_local_model_payload(qc_type, payload)
    if qc_type == "hard_qc":
        report = HardQCOutput.model_validate(normalized_payload)
        if (report.resolution_code, report.pass_flag, report.next_action) not in VALID_HARD_COMBOS:
            raise QCValidationError("illegal hard qc combo")
        return report

    if qc_type == "soft_qc":
        report = SoftQCOutput.model_validate(normalized_payload)
        if (report.resolution_code, report.pass_flag, report.next_action) not in VALID_SOFT_COMBOS:
            raise QCValidationError("illegal soft qc combo")
        if report.resolution_code == "soft_patch" and not report.rewrite_brief:
            raise QCValidationError("soft patch requires rewrite brief")
        if report.resolution_code == "soft_waive":
            if not report.carry_forward_note or report.note_scope is None or not report.carry_note_text:
                raise QCValidationError("soft waive requires carry note")
        return report

    raise QCValidationError(f"unknown qc type: {qc_type}")


def _normalize_local_model_payload(qc_type: str, payload: dict) -> dict:
    normalized = dict(payload)
    rewrite_brief = normalized.get("rewrite_brief")
    if isinstance(rewrite_brief, str):
        rewrite_brief = rewrite_brief.strip()
        normalized["rewrite_brief"] = [rewrite_brief] if rewrite_brief else []
    issues = normalized.get("issues")
    if isinstance(issues, dict):
        normalized["issues"] = [_normalize_issue_pair(key, value) for key, value in issues.items()]
    elif isinstance(issues, list):
        normalized["issues"] = [_normalize_issue(issue) for issue in issues]
    flattened_style_deviation = _pop_flattened_style_deviation(normalized)
    if flattened_style_deviation and not normalized.get("style_deviations"):
        normalized["style_deviations"] = flattened_style_deviation
    style_deviations = normalized.get("style_deviations")
    if isinstance(style_deviations, dict):
        normalized["style_deviations"] = [_normalize_style_deviation(style_deviations)]
    elif isinstance(style_deviations, list):
        normalized["style_deviations"] = [_normalize_style_deviation(item) for item in style_deviations]
    _derive_pass_flag(qc_type, normalized)
    if qc_type == "soft_qc":
        apply_style_scores_alias(normalized)
        _normalize_soft_waive_note(normalized)
    _drop_unknown_contract_keys(qc_type, normalized)
    return normalized


def _derive_pass_flag(qc_type: str, payload: dict) -> None:
    resolution_code = payload.get("resolution_code")
    next_action = payload.get("next_action")
    combos = VALID_HARD_COMBOS if qc_type == "hard_qc" else VALID_SOFT_COMBOS if qc_type == "soft_qc" else set()
    for combo_resolution, combo_pass_flag, combo_next_action in combos:
        if resolution_code == combo_resolution and next_action == combo_next_action:
            payload["pass_flag"] = combo_pass_flag
            return


def _pop_flattened_style_deviation(payload: dict) -> dict[str, object]:
    flattened: dict[str, object] = {}
    for key in list(payload):
        if not isinstance(key, str) or not key.startswith("style_deviations."):
            continue
        flattened[key.split(".", 1)[1]] = payload.pop(key)
    return flattened


def _normalize_issue(issue: object) -> object:
    if not isinstance(issue, dict):
        return {
            "issue_key": "local_model_issue",
            "message": str(issue).strip() if issue is not None else "",
        }
    issue_key = issue.get("issue_key")
    message = issue.get("message")
    alias_key = issue.get("type")
    alias_message = issue.get("description")
    normalized: dict[str, str] = {}
    if not isinstance(issue_key, str) or not issue_key.strip():
        normalized["issue_key"] = str(alias_key).strip() if alias_key is not None and str(alias_key).strip() else "local_model_issue"
    else:
        normalized["issue_key"] = issue_key.strip()
    if not isinstance(message, str) or not message.strip():
        normalized["message"] = (
            str(alias_message).strip() if alias_message is not None and str(alias_message).strip() else ""
        )
    else:
        normalized["message"] = message.strip()
    return normalized


def _normalize_issue_pair(key: object, value: object) -> object:
    issue_key = str(key).strip() if key is not None and str(key).strip() else "local_model_issue"
    if isinstance(value, dict):
        item = dict(value)
        item.setdefault("issue_key", issue_key)
        return _normalize_issue(item)
    return {
        "issue_key": issue_key,
        "message": str(value).strip() if value is not None else "",
    }


def apply_style_scores_alias(payload: dict) -> None:
    """旧的 ``style_scores`` 别名（本地模型给的 ``{维: 分}``）→ ``style_dimensions`` / ``style_score``（就地改）。

    软 QC 的节点出口先调它、再按模板声明的刻度换算分数（``qc_engine._normalize_soft_qc_scores``）；这里的校验
    只收 0–1——别名要是等到校验时才摊开，0–10 的分数已经错过了换算。"""
    raw_scores = payload.pop("style_scores", None)
    if not isinstance(raw_scores, dict):
        return
    dimensions: list[dict[str, object]] = []
    numeric_scores: list[float] = []
    for name, score in raw_scores.items():
        try:
            numeric_score = float(score)
        except (TypeError, ValueError):
            continue
        dimensions.append({"name": str(name).strip() or "style", "score": numeric_score, "evidence": ""})
        numeric_scores.append(numeric_score)
    if dimensions and not payload.get("style_dimensions"):
        payload["style_dimensions"] = dimensions
    if numeric_scores and payload.get("style_score") is None:
        payload["style_score"] = sum(numeric_scores) / len(numeric_scores)


def _drop_unknown_contract_keys(qc_type: str, payload: dict) -> None:
    allowed = ALLOWED_QC_KEYS.get(qc_type)
    if not allowed:
        return
    for key in list(payload):
        if key not in allowed:
            payload.pop(key)


def _normalize_soft_waive_note(payload: dict) -> None:
    if payload.get("resolution_code") != "soft_waive" or payload.get("next_action") != "pass_with_notes":
        return
    payload["carry_forward_note"] = True
    if not payload.get("note_scope"):
        payload["note_scope"] = "scene_memory"
    if payload.get("carry_note_text"):
        return
    rewrite_brief = payload.get("rewrite_brief")
    if isinstance(rewrite_brief, list):
        note = "; ".join(str(item).strip() for item in rewrite_brief if str(item).strip())
        if note:
            payload["carry_note_text"] = note
            return
    issues = payload.get("issues")
    if isinstance(issues, list):
        messages: list[str] = []
        for issue in issues:
            if isinstance(issue, dict) and issue.get("message"):
                messages.append(str(issue["message"]).strip())
        note = "; ".join(message for message in messages if message)
        if note:
            payload["carry_note_text"] = note
            return


def _normalize_style_deviation(item: object) -> object:
    if not isinstance(item, dict):
        return item
    dimension = item.get("dimension") or item.get("type") or item.get("name") or item.get("issue_key") or "style"
    patch_brief = item.get("patch_brief") or item.get("rewrite_brief") or item.get("description") or item.get("message") or ""
    evidence = item.get("evidence")
    if evidence is None and item.get("description") and item.get("description") != patch_brief:
        evidence = item.get("description")
    return {
        "dimension": str(dimension).strip() or "style",
        "severity": str(item.get("severity") or "").strip(),
        "patch_brief": str(patch_brief).strip(),
        "evidence": str(evidence).strip() if evidence is not None and str(evidence).strip() else None,
    }
