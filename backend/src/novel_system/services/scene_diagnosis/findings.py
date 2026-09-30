"""各来源 → 统一发现：规则维度（``literary_quality``）、段落节奏（原写作台本地规则）、评审 / 深评行里的发现；
计数与排序；同一场同一份字的规则 / 节奏发现在进程里缓存（规则维度一场约 30 ms，全书计数一次跑几十场）。"""

from __future__ import annotations

import copy
import hashlib
from collections import OrderedDict
from typing import Any

from novel_system.cache_registry import register_cache_reset
from novel_system.db.models import WriterEvaluation
from novel_system.services.literary_quality import (
    DEFAULT_RULE_CALIBRATION,
    SEVERITY_RANK,
    RuleCalibration,
    analyze_literary_quality,
    calibrate_lexicons,
    dimension_label,
    unify_rule_finding,
)
from novel_system.services.scene_diagnosis.calibration import (
    CRAFT_LONG_PARAGRAPH_CHARS,
    DEFAULT_CRAFT_CALIBRATION,
    ECHO_RE,
    CraftCalibration,
    same_opening_hit,
)
from novel_system.services.scene_diagnosis.text import DiagnosisText, locate
from novel_system.services.scene_diagnosis.vocabulary import (
    AI_DIMENSION_LABELS,
    CRAFT_LABELS,
    DIAGNOSIS_SOURCES,
    LENS_LABELS,
    PASSAGE_RELATION_KINDS,
    PASSAGE_RELATION_LABELS,
    REVIEW_DIMENSION_LABELS,
    SEVERITIES,
    SOURCE_LABELS,
    candidate_category_for_dimension,
)
from novel_system.services.style_reference.text_utils import compact_ws

_FINDINGS_CACHE: "OrderedDict[tuple[Any, ...], dict[str, list[dict[str, Any]]]]" = OrderedDict()
_FINDINGS_CACHE_MAX = 512
register_cache_reset("scene_diagnosis.findings", _FINDINGS_CACHE.clear)


def _digest(value: str) -> str:
    return hashlib.sha1(compact_ws(value).encode("utf-8")).hexdigest()[:8]


def _severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "ignore_ok":
        return "info"
    return text if text in SEVERITIES else "revision"


def _patch_hint(dimension: str, recommendation: str) -> dict[str, str]:
    return {
        "candidate_category": candidate_category_for_dimension(dimension),
        "revision_strategy": recommendation,
    }


def rule_findings(
    text: DiagnosisText,
    *,
    house_taste: bool = False,
    calibration: RuleCalibration = DEFAULT_RULE_CALIBRATION,
) -> list[dict[str, Any]]:
    """规则维度的发现。``calibration`` 来自参考书时：作者的常用词不再命中，作者常态的维度降为提示
    （``calibrated`` 说明它在参考书多少窗口上也会响）。"""

    _, raw = analyze_literary_quality(text.plain, calibration=calibration if calibration.active else None)
    findings: list[dict[str, Any]] = []
    for item in raw:
        unified = unify_rule_finding(item)
        evidence = locate(
            text.paragraphs,
            needle=unified.get("needle") or "",
            excerpt=unified.get("evidence_excerpt") or "",
            anchor=unified.get("anchor") or "text",
        )
        calibrated = unified.get("calibrated") if isinstance(unified.get("calibrated"), dict) else None
        why = ""
        if calibrated is not None:
            share = int(round(100 * float(calibrated.get("share") or 0.0)))
            if calibrated.get("kind") == "profile_deliberate_repetition":
                why = "画像标了这位作者刻意用重复，只作提示。"
            elif calibrated.get("level") == "habit":
                why = f"参考作者的场里约 {share}% 也是这样（{int(calibrated.get('n') or 0)} 个窗口），只作提示。"
            else:
                why = f"参考作者的场里约 {share}% 也是这样（{int(calibrated.get('n') or 0)} 个窗口），按审美看。"
        findings.append(
            {
                "signal_id": unified["signal_id"],
                "quality_signal_id": unified["signal_id"],
                "source": "rules",
                "dimension": unified["dimension"],
                "label": unified["label"],
                "lens": None,
                "severity": _severity(unified.get("severity")),
                "issue": unified["issue"],
                "recommendation": unified["recommendation"],
                "why": why,
                "evidence": evidence,
                "context": compact_ws(unified.get("evidence_excerpt") or ""),
                "anchor": unified.get("anchor") or "text",
                "ignored": False,
                "stale": False,
                "house_taste": house_taste,
                "calibrated": calibrated,
                "origin": None,
                "opinion": None,
                "patch": _patch_hint(unified["dimension"], unified["recommendation"]),
            }
        )
    return findings


def craft_findings(
    text: DiagnosisText,
    *,
    calibration: CraftCalibration = DEFAULT_CRAFT_CALIBRATION,
    house_taste: bool = False,
) -> list[dict[str, Any]]:
    """段落节奏检查（原写作台本地规则）：贴邻叠句、段落偏长、连续三句同字开头。

    ``calibration`` 来自参考作者时：段落偏长的阈值是参考书段长的长尾（p95，至少 170 字），
    参考作者常用的贴邻叠句 / 句首重复不再提示（那是这位作者的手法，不是毛病）。
    """

    findings: list[dict[str, Any]] = []
    limit = int(calibration.long_paragraph_chars or CRAFT_LONG_PARAGRAPH_CHARS)
    for index, paragraph in enumerate(text.paragraphs):
        body = paragraph.strip()
        if not body:
            continue
        head = body[:24]
        echo = ECHO_RE.search(paragraph) if calibration.flag_echo else None
        if echo:
            hit = echo.group(0)
            findings.append(
                _craft_finding(
                    "adjacent_echo",
                    f"craft:adjacent_echo:{_digest(hit)}",
                    "taste",
                    f"「{hit}」贴邻重复。",
                    "短语回响节奏偏刻意，考虑改换连接或删一处。",
                    {"excerpt": hit, "paragraph_index": index, "start": echo.start(), "end": echo.end()},
                    house_taste,
                )
            )
        if len(body) > limit:
            reason = f"，超过参考作者段落的长尾 {limit} 字" if calibration.source == "reference" else ""
            findings.append(
                _craft_finding(
                    "long_paragraph",
                    f"craft:long_paragraph:{_digest(head)}",
                    "info",
                    f"第 {index + 1} 段偏长（{len(body)} 字{reason}）。",
                    "单段信息密度偏高，考虑拆段或删减一件物事。",
                    {"excerpt": paragraph, "paragraph_index": index, "start": 0, "end": len(paragraph)},
                    house_taste,
                )
            )
        if calibration.flag_same_opening:
            opening = same_opening_hit(paragraph)
            if opening is not None:
                start = opening["start"]
                span_text = opening["span_text"]
                findings.append(
                    _craft_finding(
                        "same_opening",
                        f"craft:same_opening:{_digest(head + opening['head'])}",
                        "info",
                        f"连续三句以「{opening['head']}」开头。",
                        "句首重复读起来平，考虑改写其中一句的主语或语序。",
                        {
                            "excerpt": span_text,
                            "paragraph_index": index,
                            "start": start if start >= 0 else None,
                            "end": start + len(span_text) if start >= 0 else None,
                        },
                        house_taste,
                    )
                )
    return findings


def cached_text_findings(
    scene_id: str,
    text: DiagnosisText,
    *,
    calibration: CraftCalibration,
    house_taste: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """规则 + 节奏发现，以及这一稿里按参考作者放过的词表词；按（场、正文哈希、校准、绑定）缓存在进程里；
    返回的是副本，调用方随便改。"""

    key = (
        str(scene_id),
        text.sha256,
        calibration.source,
        calibration.profile_id,
        calibration.long_paragraph_chars,
        calibration.flag_echo,
        calibration.flag_same_opening,
        calibration.rules.signature,
        bool(house_taste),
    )
    cached = _FINDINGS_CACHE.get(key)
    if cached is not None:
        _FINDINGS_CACHE.move_to_end(key)
        return copy.deepcopy(cached["findings"]), copy.deepcopy(cached["waived"])
    findings = rule_findings(text, house_taste=house_taste, calibration=calibration.rules) + craft_findings(
        text, calibration=calibration, house_taste=house_taste
    )
    waived = calibrate_lexicons(calibration.rules, text.plain)[1] if calibration.rules.active else []
    _FINDINGS_CACHE[key] = {"findings": copy.deepcopy(findings), "waived": copy.deepcopy(waived)}
    while len(_FINDINGS_CACHE) > _FINDINGS_CACHE_MAX:
        _FINDINGS_CACHE.popitem(last=False)
    return findings, waived


def _craft_finding(
    kind: str,
    signal_id: str,
    severity: str,
    issue: str,
    recommendation: str,
    evidence: dict[str, Any],
    house_taste: bool,
) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "quality_signal_id": signal_id,
        "source": "craft",
        "dimension": kind,
        "label": CRAFT_LABELS[kind],
        "lens": None,
        "severity": severity,
        "issue": issue,
        "recommendation": recommendation,
        "why": "",
        "evidence": evidence,
        "context": compact_ws(evidence.get("excerpt") or "")[:160],
        "ignored": False,
        "stale": False,
        "house_taste": house_taste,
        "origin": None,
        "opinion": None,
        "patch": _patch_hint(kind, recommendation),
    }


def evaluation_findings(
    row: WriterEvaluation,
    text: DiagnosisText,
    *,
    source: str,
    label_for: dict[str, str],
    origin_kind: str = "scene",
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    meta = row.contract_field_refs_json if isinstance(row.contract_field_refs_json, dict) else {}
    for index, item in enumerate(row.findings_json or []):
        if not isinstance(item, dict):
            continue
        issue_text = str(item.get("issue") or "").strip()
        recommendation = str(item.get("recommendation") or "").strip()
        if not issue_text and not recommendation:
            # 真实安装上见过的空成员（schema-enforcing 中转把没声明 properties 的成员解码成 {}）：
            # 没有一个字可给作者看，不当作一条「unknown」发现列出来
            continue
        dimension = str(item.get("dimension") or item.get("category") or item.get("kind") or "").strip() or "review_note"
        excerpt = compact_ws(str(item.get("evidence_excerpt") or ""))
        evidence = locate(text.paragraphs, needle=excerpt, excerpt=excerpt) if excerpt else None
        if evidence is None and origin_kind == "passage":
            # 局部深评没引到原句时，仍然钉在它看的那（第一）段上
            focus_list = [int(value) for value in (meta.get("focus_paragraphs") or []) if isinstance(value, int)]
            focus = focus_list[0] if focus_list else meta.get("paragraph_index")
            if isinstance(focus, int) and 0 <= focus < len(text.paragraphs):
                evidence = {"excerpt": text.paragraphs[focus][:80], "paragraph_index": focus, "start": None, "end": None}
        # 跨段的发现（焦点段与本场另一段矛盾 / 重复 / 承接）：另一段的原话也钉到段
        related: dict[str, Any] | None = None
        related_excerpt = compact_ws(str(item.get("related_excerpt") or ""))
        if related_excerpt:
            related_hit = locate(text.paragraphs, needle=related_excerpt, excerpt=related_excerpt)
            related_kind = str(item.get("relation") or "contradiction").strip().lower()
            related = {
                "excerpt": related_excerpt[:160],
                "paragraph_index": related_hit["paragraph_index"] if related_hit else (
                    int(item["related_paragraph_index"]) if isinstance(item.get("related_paragraph_index"), int) else None
                ),
                "start": related_hit["start"] if related_hit else None,
                "end": related_hit["end"] if related_hit else None,
                "kind": related_kind if related_kind in PASSAGE_RELATION_KINDS else "contradiction",
                "stale": related_hit is None,
            }
            related["label"] = PASSAGE_RELATION_LABELS[related["kind"]]
        seed = excerpt or str(item.get("issue") or "") or str(index)
        signal_id = f"{source}:{dimension}:{_digest(seed)}"
        lens = str(item.get("lens") or "") or None
        origin: dict[str, Any] = {
            "kind": origin_kind,
            "evaluation_id": row.evaluation_id,
            "created_at": row.created_at,
            "rubric_id": row.rubric_id,
        }
        if origin_kind == "passage":
            origin["paragraph_index"] = meta.get("paragraph_index")
            origin["focus_paragraphs"] = list(meta.get("focus_paragraphs") or ([meta["paragraph_index"]] if isinstance(meta.get("paragraph_index"), int) else []))
            origin["about_signal_id"] = meta.get("about_signal_id")
        if origin_kind == "chapter" and item.get("carried_from"):
            # 只通读改过的场时，未改的场沿用上一次通读的发现：记下它原来的那一轮
            origin["carried_from"] = str(item.get("carried_from"))
        findings.append(
            {
                "signal_id": signal_id,
                "quality_signal_id": signal_id,
                "source": source,
                "dimension": dimension,
                "label": label_for.get(dimension) or dimension_label(dimension) or AI_DIMENSION_LABELS.get(dimension) or REVIEW_DIMENSION_LABELS.get(dimension) or SOURCE_LABELS.get(source, dimension),
                "lens": lens if lens in LENS_LABELS else None,
                "severity": _severity(item.get("severity") or item.get("classification")),
                "issue": issue_text,
                "recommendation": recommendation,
                "why": str(item.get("why_it_matters") or ""),
                "evidence": evidence,
                "context": excerpt[:160],
                "ignored": False,
                # 有证据却在当前正文里找不到：多半已经改掉了
                "stale": bool(excerpt) and evidence is None,
                "house_taste": False,
                "related": related,
                "origin": origin,
                "opinion": None,
                "patch": _patch_hint(dimension, recommendation),
            }
        )
    return findings


def finding_counts(findings: list[dict[str, Any]]) -> dict[str, Any]:
    open_findings = [finding for finding in findings if not finding.get("ignored")]
    return {
        "total": len(findings),
        "open": len(open_findings),
        "ignored": len(findings) - len(open_findings),
        "stale": sum(1 for finding in open_findings if finding.get("stale")),
        "by_severity": {level: sum(1 for finding in open_findings if finding["severity"] == level) for level in SEVERITIES},
        "by_source": {source: sum(1 for finding in open_findings if finding["source"] == source) for source in DIAGNOSIS_SOURCES},
    }


def finding_sort_key(finding: dict[str, Any]) -> tuple[int, int, int, str]:
    evidence = finding.get("evidence") or {}
    paragraph = evidence.get("paragraph_index")
    return (
        SEVERITY_RANK.get(str(finding.get("severity")), 99),
        paragraph if isinstance(paragraph, int) else 1_000_000,
        DIAGNOSIS_SOURCES.index(finding["source"]) if finding.get("source") in DIAGNOSIS_SOURCES else 99,
        str(finding.get("signal_id") or ""),
    )
