"""文学质量视图的报告层：统一形状的发现、在原文里定位、风险簇、跨场复用、推荐的下一步，与「整维已忽略」的判定。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from novel_system.services.errors import DomainError
from novel_system.services.literary_quality.dimensions import (
    QUALITY_DIMENSIONS,
    SEVERITY_RANK,
    dimension_label,
    rule_signal_id,
    unify_rule_finding,
)
from novel_system.services.literary_quality.rules import analyze_literary_quality
from novel_system.services.literary_quality.text import _compact_ws


def ignored_dimensions_from_findings(findings: Iterable[Mapping[str, Any]], ignored_keys: Iterable[str]) -> set[str]:
    """Dimensions whose every finding in ``findings`` the author has ignored in the writer.

    The ignore list holds signal ids (``rule_signal_id``); a dimension counts as the author's
    decision only when all of its findings were ignored. The final-text gate drops its Q3
    warnings for those dimensions: a finding dismissed in the deep drawer must not come back
    as a warning in 成稿中心.
    """

    ignored = {str(key) for key in ignored_keys or [] if str(key)}
    if not ignored:
        return set()
    by_dimension: dict[str, list[str]] = {}
    for finding in findings:
        by_dimension.setdefault(str(finding.get("dimension") or ""), []).append(rule_signal_id(finding))
    return {
        dimension
        for dimension, ids in by_dimension.items()
        if dimension and ids and all(signal_id in ignored for signal_id in ids)
    }


def ignored_rule_dimensions(text: str, ignored_keys: Iterable[str]) -> set[str]:
    """:func:`ignored_dimensions_from_findings` over the house-rule findings of ``text``."""

    ignored = [str(key) for key in ignored_keys or [] if str(key)]
    if not ignored:
        return set()
    _, findings = analyze_literary_quality(text)
    return ignored_dimensions_from_findings(findings, ignored)


def _validate_quality_filters(*, risk_type: str | None, min_severity: str | None) -> None:
    if risk_type and risk_type not in QUALITY_DIMENSIONS:
        raise DomainError("LITERARY_QUALITY_RISK_TYPE_INVALID", "unsupported literary quality risk_type", status_code=400)
    if min_severity and min_severity not in SEVERITY_RANK:
        raise DomainError("LITERARY_QUALITY_SEVERITY_INVALID", "unsupported literary quality min_severity", status_code=400)


def _filter_quality_items(
    items: list[dict[str, Any]],
    *,
    risk_type: str | None,
    min_severity: str | None,
) -> list[dict[str, Any]]:
    threshold = SEVERITY_RANK[min_severity] if min_severity else None
    filtered: list[dict[str, Any]] = []
    for item in items:
        if risk_type and not item.get("signals", {}).get(risk_type, {}).get("risk"):
            continue
        if risk_type:
            focused_findings = [finding for finding in item.get("findings") or [] if finding.get("dimension") == risk_type]
            item = {
                **item,
                "recommended_next_action": _recommended_next_action(focused_findings, item.get("signals") or {}),
            }
        if threshold is not None and not any(
            SEVERITY_RANK.get(finding.get("severity"), 99) <= threshold
            for finding in item.get("findings") or []
        ):
            continue
        filtered.append(item)
    return filtered


def _enrich_findings(
    findings: list[dict[str, str]],
    *,
    object_type: str,
    object_id: str,
    chapter_id: str,
    scene_id: str | None,
    source_ref: str,
    ignored_keys: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Findings in the unified shape (Chinese text, stable ``signal_id``), tagged with the object.

    ``quality_signal_id`` is kept as an alias of ``signal_id`` — it is the column name a
    passage patch candidate records for the handoff. ``ignored`` marks the findings the
    author dismissed in the writer's deep drawer (the scene's ignore list holds ids).
    """

    ignored = {str(key) for key in ignored_keys or [] if str(key)}
    enriched: list[dict[str, Any]] = []
    for finding in findings:
        unified = unify_rule_finding(finding)
        enriched.append(
            {
                **unified,
                "quality_signal_id": unified["signal_id"],
                "object_type": object_type,
                "object_id": object_id,
                "chapter_id": chapter_id,
                "scene_id": scene_id,
                "source_ref": source_ref,
                "target_text_ref": source_ref,
                "ignored": unified["signal_id"] in ignored,
            }
        )
    return enriched


def _span_findings(content: str, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for finding in findings:
        span = _locate_finding_span(
            content,
            str(finding.get("evidence_excerpt") or ""),
            needle=str(finding.get("needle") or ""),
        )
        if span is None:
            continue
        start, end, evidence = span
        spans.append(
            {
                "dimension": finding.get("dimension") or "unknown",
                "label": finding.get("label") or dimension_label(str(finding.get("dimension") or "")),
                "severity": finding.get("severity") or "revision",
                "start": start,
                "end": end,
                "evidence": evidence,
                "issue": finding.get("issue") or "",
                "recommended_action": finding.get("recommendation") or "",
                "quality_signal_id": finding.get("quality_signal_id"),
                "signal_id": finding.get("signal_id") or finding.get("quality_signal_id"),
                "ignored": bool(finding.get("ignored")),
            }
        )
    return spans


def _locate_finding_span(content: str, evidence: str, *, needle: str = "") -> tuple[int, int, str] | None:
    source = str(content or "")
    if not source:
        return None
    # 命中的词 / 句本身是最准的锚点：先钉它，再退到证据窗口
    exact = str(needle or "").strip()
    if exact:
        index = source.find(exact)
        if index >= 0:
            return index, index + len(exact), exact
    for fragment in _evidence_fragments(evidence):
        index = source.find(fragment)
        if index >= 0:
            return index, index + len(fragment), fragment
    compact_source = _compact_ws(source)
    if compact_source and compact_source in source:
        index = source.find(compact_source)
        return index, index + len(compact_source), compact_source
    fallback = source.strip()
    if not fallback:
        return None
    index = source.find(fallback)
    fragment = fallback[: min(len(fallback), 120)]
    return index, index + len(fragment), fragment


def _evidence_fragments(evidence: str) -> list[str]:
    raw = str(evidence or "").strip()
    candidates: list[str] = []
    if raw:
        candidates.append(raw)
        candidates.extend(part.strip() for part in raw.split(" / ") if part.strip())
    fragments: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in fragments:
            fragments.append(candidate)
        trimmed = candidate.strip(" .。!?！？,，;；")
        if trimmed and trimmed not in fragments:
            fragments.append(trimmed)
    return sorted(fragments, key=len, reverse=True)


def _recommended_next_action(findings: list[dict[str, Any]], signals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    open_findings = [finding for finding in findings if not finding.get("ignored")]
    if not open_findings:
        reason = "未发现明显文学质量风险。" if not findings else "剩下的发现都已在写作台忽略。"
        return {"action": "none", "label": "暂无动作", "reason": reason}
    priority = sorted(open_findings, key=lambda item: (SEVERITY_RANK.get(item.get("severity"), 99), item.get("dimension", "")))[0]
    dimension = priority.get("dimension") or "quality"
    # 动作名保留旧值（前端与测试都认它）；落点是写作台的深改姿态，带着这条发现的 signal_id 过去
    action = "open_deepdesk_patch"
    label = "去写作台处理这一处"
    signal_id = priority.get("signal_id") or priority.get("quality_signal_id")
    return {
        "action": action,
        "label": label,
        "risk_type": dimension,
        "signal_id": signal_id,
        "quality_signal_id": signal_id,
        "target_text_ref": priority.get("target_text_ref"),
        "source_excerpt": priority.get("evidence_excerpt") or signals.get(dimension, {}).get("evidence") or "",
        "reason": priority.get("issue") or "",
        "recommendation": priority.get("recommendation") or "",
    }


def _top_recommended_action(items: list[dict[str, Any]]) -> dict[str, Any]:
    actions = [item.get("recommended_next_action") for item in items if item.get("recommended_next_action", {}).get("action") != "none"]
    if not actions:
        return {"action": "none", "label": "暂无动作", "reason": "当前列表没有明显文学质量风险。"}
    return actions[0]


def _risk_clusters(
    items: list[dict[str, Any]],
    *,
    risk_type: str | None = None,
    min_severity: str | None = None,
) -> list[dict[str, Any]]:
    threshold = SEVERITY_RANK[min_severity] if min_severity else None
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        for finding in item.get("findings") or []:
            dimension = finding.get("dimension") or "unknown"
            if risk_type and dimension != risk_type:
                continue
            if threshold is not None and SEVERITY_RANK.get(finding.get("severity"), 99) > threshold:
                continue
            cluster = grouped.setdefault(
                dimension,
                {
                    "dimension": dimension,
                    "severity": finding.get("severity") or "revision",
                    "count": 0,
                    "object_refs": [],
                    "quality_signal_ids": [],
                    "representative_evidence": finding.get("evidence_excerpt") or "",
                    "recommendation": finding.get("recommendation") or "",
                },
            )
            cluster["count"] += 1
            ref = {
                "object_type": item["object_type"],
                "object_id": item["object_id"],
                "chapter_id": item["chapter_id"],
                "scene_id": item["scene_id"],
                "source_ref": item["source_ref"],
            }
            if ref not in cluster["object_refs"]:
                cluster["object_refs"].append(ref)
            signal_id = finding.get("quality_signal_id")
            if signal_id and signal_id not in cluster["quality_signal_ids"]:
                cluster["quality_signal_ids"].append(signal_id)
            if SEVERITY_RANK.get(finding.get("severity"), 99) < SEVERITY_RANK.get(cluster["severity"], 99):
                cluster["severity"] = finding.get("severity")
    return sorted(grouped.values(), key=lambda row: (-row["count"], SEVERITY_RANK.get(row["severity"], 99), row["dimension"]))


def _cross_scene_reuse(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        if item.get("object_type") != "scene":
            continue
        chapter_id = item.get("chapter_id") or ""
        fingerprint = item.get("fingerprint") or {}
        for cluster_type, key in (
            ("action_template", "action_templates"),
            ("image_field", "image_fields"),
            ("syntax_shape", "syntax_shapes"),
        ):
            for token_row in fingerprint.get(key) or []:
                token = token_row.get("value")
                if not token:
                    continue
                group = grouped.setdefault(
                    (chapter_id, cluster_type, token),
                    {
                        "chapter_id": chapter_id,
                        "cluster_type": cluster_type,
                        "token": token,
                        "count": 0,
                        "object_ids": [],
                        "source_refs": [],
                        "severity": "revision" if cluster_type != "image_field" else "taste",
                    },
                )
                group["count"] += int(token_row.get("count") or 0)
                if item["object_id"] not in group["object_ids"]:
                    group["object_ids"].append(item["object_id"])
                if item["source_ref"] not in group["source_refs"]:
                    group["source_refs"].append(item["source_ref"])
    rows = [row for row in grouped.values() if len(row["object_ids"]) >= 2]
    return sorted(rows, key=lambda row: (-row["count"], row["chapter_id"], row["cluster_type"], row["token"]))
