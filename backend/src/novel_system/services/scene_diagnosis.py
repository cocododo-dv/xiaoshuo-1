"""场景诊断（2026-09-22）：一场正文「哪里有问题」只有一份记录。

之前有四套引擎各算各的——文学质量视图的 21 维规则、写作台深改姿态里三条浏览器本地正则、
后端从没被任何界面调用过的 LLM 深评（``writer_deep_review``）、起草管线在去模板门 / 成稿门
里再跑一遍的同一批规则——三套词汇、三种严重度，页面之间只有跳转、没有数据。这里把它们
合成**一种**发现形状，写作台的深改面板是唯一的展示处：

    {signal_id, source, dimension, label, lens, severity, issue, recommendation, why,
     evidence: {excerpt, paragraph_index, start, end} | None, context,
     ignored, stale, house_taste, origin, patch}

* ``source``：``rules``（21 维规则，`literary_quality.py`）/ ``craft``（段落节奏：贴邻叠句、
  段落偏长、句首重复——从浏览器本地规则搬到服务端）/ ``review``（起草台的准定稿评审）/
  ``ai``（写作台的 AI 深评）。
* ``signal_id`` 稳定：规则按「命中了什么」取 id（`literary_quality.rule_signal_id`），节奏按
  段落开头与命中文本，评审 / 深评按维度 + 证据。作者的「忽略」记在 ``SceneCard`` 的
  ``deep_review_ignored_keys_json`` 里，按 id 生效，并反向作用到文学质量视图与成稿门。
* ``evidence`` 钉到**编辑器段落**：``paragraph_index`` 是写作台 ``p, blockquote`` 的序号，
  ``start / end`` 是这一段可见文字里的偏移；正文是作者稿 HTML，这里按同一规则拆段。
* ``stale``：评审 / 深评的证据在当前正文里找不到了（多半已改掉）；``ai.status`` /
  ``review.status`` 另说整份评审是不是改前的。
* 有风格绑定的场，规则与节奏发现标 ``house_taste``（房风词表的意见，与参考作者的做法冲突
  时以样例为准），「段落偏长」在有绑定时不出——参考作者的段落尺度说了算。

叶子模块：只依赖 ``literary_quality``、模型与风格绑定解析；``writer_deep_review`` /
``api.routes`` 从这里取载荷。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    FinalScene,
    PassagePatchCandidate,
    SceneCard,
    SceneRunState,
    WriterEvaluation,
)
from novel_system.services.manuscript_html import manuscript_paragraphs
from novel_system.services.literary_quality import (
    SEVERITY_RANK,
    analyze_literary_quality,
    dimension_label,
    unify_rule_finding,
)
from novel_system.services.scene_lookup import require_scene

# 两个评审来源的 rubric id。深评的在这里定义（writer_deep_review 从这里取）；准定稿评审的
# 与 near_final.NEAR_FINAL_RUBRIC_ID 相同——测试钉住两者相等，这里不 import near_final
# （它牵着整条管线，会把这个叶子拖进环）。
LITERARY_REVISION_RUBRIC_ID = "literary_revision_v1"
NEAR_FINAL_RUBRIC_ID = "near_final_acceptance_v1"

DIAGNOSIS_SOURCES: tuple[str, ...] = ("rules", "craft", "review", "ai")
SOURCE_LABELS: dict[str, str] = {
    "rules": "规则体检",
    "craft": "节奏",
    "review": "起草评审",
    "ai": "AI 深评",
}
SEVERITIES: tuple[str, ...] = ("blocking", "revision", "taste", "info")
SEVERITY_LABELS: dict[str, str] = {"blocking": "阻断", "revision": "修订", "taste": "审美", "info": "提示"}

# 深评（literary_revision_v1）的十维与五个镜头
AI_DIMENSION_LABELS: dict[str, str] = {
    "character_contradiction": "人物自相矛盾",
    "choice_pressure": "抉择压力",
    "relationship_tension": "关系张力",
    "dialogue_subtext": "对白潜台词",
    "information_rhythm": "信息节奏",
    "voice_distinction": "声音辨识度",
    "image_necessity": "意象必要性",
    "repetitive_expression": "表达重复",
    "ending_drive": "收束驱动",
    "theme_pressure": "主题压力",
}
LENS_LABELS: dict[str, str] = {"story": "故事", "character": "人物", "prose": "文字", "reader": "读者", "theme": "主题"}
# 准定稿评审（near_final）里确定性门产出的维度
REVIEW_DIMENSION_LABELS: dict[str, str] = {
    "model_voice_risk": "模型腔",
    "forced_choice": "被逼的选择",
    "price_paid": "付出的代价",
    "ending_action": "收尾动作",
    "structure": "结构",
    "scene_form": "场景形态",
    "source_text": "正文",
}
CRAFT_LABELS: dict[str, str] = {
    "adjacent_echo": "贴邻叠句",
    "long_paragraph": "段落偏长",
    "same_opening": "句首重复",
}
SCENE_FORMS: tuple[str, ...] = (
    "plot_scene",
    "atmosphere_scene",
    "relationship_scene",
    "revelation_scene",
    "transition_scene",
)
PATCH_CATEGORIES: tuple[str, ...] = (
    "dialogue_rewrite",
    "action_replace",
    "ending_pressure",
    "information_reorder",
    "de_model_voice",
    "local_patch",
)

CRAFT_LONG_PARAGRAPH_CHARS = 170
_ECHO_RE = re.compile(r"([一-龥]{2,5})([，、；]?)\1")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])")
_WS_RE = re.compile(r"\s+")
PATCH_CANDIDATE_LIMIT = 20


@dataclass
class DiagnosisText:
    layer: str
    ref: str | None
    content: str
    paragraphs: list[str] = field(default_factory=list)
    updated_at: str | None = None

    @property
    def plain(self) -> str:
        return " ".join(paragraph for paragraph in self.paragraphs if paragraph.strip())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.plain.encode("utf-8")).hexdigest()

    @property
    def chars(self) -> int:
        return len(_WS_RE.sub("", self.plain))

    def compact(self) -> str:
        return _compact(self.plain)


def _compact(value: str) -> str:
    return _WS_RE.sub(" ", str(value or "")).strip()


def locate_in_paragraphs(paragraphs: list[str], needle: str) -> dict[str, Any] | None:
    """在段落列表里钉住 ``needle``：先原样找（偏移可用），再按压缩空白找（只到段）。"""

    exact = str(needle or "").strip()
    if not exact:
        return None
    for index, paragraph in enumerate(paragraphs):
        at = paragraph.find(exact)
        if at >= 0:
            return {"excerpt": exact, "paragraph_index": index, "start": at, "end": at + len(exact)}
    compact_needle = _compact(exact)
    if not compact_needle:
        return None
    for index, paragraph in enumerate(paragraphs):
        if compact_needle in _compact(paragraph):
            return {"excerpt": exact, "paragraph_index": index, "start": None, "end": None}
    return None


def _fragments(evidence: str) -> list[str]:
    raw = str(evidence or "").strip()
    if not raw:
        return []
    parts = [raw, *[part.strip() for part in raw.split(" / ") if part.strip()]]
    fragments: list[str] = []
    for part in parts:
        for candidate in (part, part.strip(" .。!?！？,，;；")):
            if candidate and candidate not in fragments:
                fragments.append(candidate)
    return sorted(fragments, key=len, reverse=True)


def _locate(paragraphs: list[str], *, needle: str = "", excerpt: str = "", anchor: str = "text") -> dict[str, Any] | None:
    if anchor == "scene":
        return None
    if anchor == "ending" and paragraphs:
        last = len(paragraphs) - 1
        hit = locate_in_paragraphs([paragraphs[last]], needle) if needle else None
        if hit:
            hit["paragraph_index"] = last
            return hit
        return {"excerpt": paragraphs[last][-60:], "paragraph_index": last, "start": None, "end": None}
    hit = locate_in_paragraphs(paragraphs, needle) if needle else None
    if hit:
        return hit
    for fragment in _fragments(excerpt):
        hit = locate_in_paragraphs(paragraphs, fragment)
        if hit:
            return hit
        # 证据窗口跨了段：取它前 24 个字再钉一次
        head = fragment[:24]
        if len(head) >= 8:
            hit = locate_in_paragraphs(paragraphs, head)
            if hit:
                return hit
    return None


# ---------------------------------------------------------------------------
# 各来源 → 统一发现
# ---------------------------------------------------------------------------


def _digest(value: str) -> str:
    return hashlib.sha1(_compact(value).encode("utf-8")).hexdigest()[:8]


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


def candidate_category_for_dimension(dimension: str) -> str:
    """一条发现的维度 → 局部修补的类别（偏好画像按类别学）。规则 21 维与深评十维都认。"""

    value = str(dimension or "")
    if value in {"dialogue_subtext", "dialogue_edge", "relationship_tension", "expository_dialogue", "dialogue_as_report"}:
        return "dialogue_rewrite"
    if value in {"image_necessity", "repetitive_expression", "template_action_reuse", "repetitive_action", "self_repetition", "adjacent_echo", "same_opening"}:
        return "action_replace"
    if value in {"ending_drive", "summary_ending", "false_poetic_closure", "ending_action"}:
        return "ending_pressure"
    if value in {"information_rhythm", "false_clarity", "over_explained_motive", "long_paragraph"}:
        return "information_reorder"
    if value in {"model_voice", "prose_model_voice", "model_voice_risk", "image_homogeneity", "syntax_monotony", "decorative_imagery", "image_field_reuse", "perception_filter", "voice_distinction"}:
        return "de_model_voice"
    return "local_patch"


def rule_findings(text: DiagnosisText, *, house_taste: bool = False) -> list[dict[str, Any]]:
    _, raw = analyze_literary_quality(text.plain)
    findings: list[dict[str, Any]] = []
    for item in raw:
        unified = unify_rule_finding(item)
        evidence = _locate(
            text.paragraphs,
            needle=unified.get("needle") or "",
            excerpt=unified.get("evidence_excerpt") or "",
            anchor=unified.get("anchor") or "text",
        )
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
                "why": "",
                "evidence": evidence,
                "context": _compact(unified.get("evidence_excerpt") or ""),
                "anchor": unified.get("anchor") or "text",
                "ignored": False,
                "stale": False,
                "house_taste": house_taste,
                "origin": None,
                "patch": _patch_hint(unified["dimension"], unified["recommendation"]),
            }
        )
    return findings


def craft_findings(text: DiagnosisText, *, house_taste: bool = False) -> list[dict[str, Any]]:
    """段落节奏检查（原写作台本地规则）：贴邻叠句、段落偏长、连续三句同字开头。"""

    findings: list[dict[str, Any]] = []
    for index, paragraph in enumerate(text.paragraphs):
        body = paragraph.strip()
        if not body:
            continue
        head = body[:24]
        echo = _ECHO_RE.search(paragraph)
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
        if len(body) > CRAFT_LONG_PARAGRAPH_CHARS and not house_taste:
            findings.append(
                _craft_finding(
                    "long_paragraph",
                    f"craft:long_paragraph:{_digest(head)}",
                    "info",
                    f"第 {index + 1} 段偏长（{len(body)} 字）。",
                    "单段信息密度偏高，考虑拆段或删减一件物事。",
                    {"excerpt": paragraph, "paragraph_index": index, "start": 0, "end": len(paragraph)},
                    False,
                )
            )
        sentences = [part for part in _SENTENCE_SPLIT_RE.split(paragraph) if part.strip()]
        for offset in range(len(sentences) - 2):
            heads = [sentence.strip()[:1] for sentence in sentences[offset : offset + 3]]
            if heads[0] and heads[0] == heads[1] == heads[2]:
                span_text = "".join(sentences[offset : offset + 3])
                start = paragraph.find(span_text)
                findings.append(
                    _craft_finding(
                        "same_opening",
                        f"craft:same_opening:{_digest(head + heads[0])}",
                        "info",
                        f"连续三句以「{heads[0]}」开头。",
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
                break
    return findings


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
        "context": _compact(evidence.get("excerpt") or "")[:160],
        "ignored": False,
        "stale": False,
        "house_taste": house_taste,
        "origin": None,
        "patch": _patch_hint(kind, recommendation),
    }


def _evaluation_findings(
    row: WriterEvaluation,
    text: DiagnosisText,
    *,
    source: str,
    label_for: dict[str, str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for index, item in enumerate(row.findings_json or []):
        if not isinstance(item, dict):
            continue
        dimension = str(item.get("dimension") or "unknown")
        excerpt = _compact(str(item.get("evidence_excerpt") or ""))
        recommendation = str(item.get("recommendation") or "")
        evidence = _locate(text.paragraphs, needle=excerpt, excerpt=excerpt) if excerpt else None
        seed = excerpt or str(item.get("issue") or "") or str(index)
        signal_id = f"{source}:{dimension}:{_digest(seed)}"
        lens = str(item.get("lens") or "") or None
        findings.append(
            {
                "signal_id": signal_id,
                "quality_signal_id": signal_id,
                "source": source,
                "dimension": dimension,
                "label": label_for.get(dimension) or dimension_label(dimension) or AI_DIMENSION_LABELS.get(dimension) or REVIEW_DIMENSION_LABELS.get(dimension) or dimension,
                "lens": lens if lens in LENS_LABELS else None,
                "severity": _severity(item.get("severity") or item.get("classification")),
                "issue": str(item.get("issue") or ""),
                "recommendation": recommendation,
                "why": str(item.get("why_it_matters") or ""),
                "evidence": evidence,
                "context": excerpt[:160],
                "ignored": False,
                # 有证据却在当前正文里找不到：多半已经改掉了
                "stale": bool(excerpt) and evidence is None,
                "house_taste": False,
                "origin": {"evaluation_id": row.evaluation_id, "created_at": row.created_at, "rubric_id": row.rubric_id},
                "patch": _patch_hint(dimension, recommendation),
            }
        )
    return findings


# ---------------------------------------------------------------------------
# 序列化（writer_deep_review 从这里取，避免两份）
# ---------------------------------------------------------------------------


def scene_form_from_findings(findings: list[dict[str, Any]], object_type: str | None = None) -> str | None:
    if object_type != "scene":
        return None
    for finding in findings:
        scene_form = str(finding.get("scene_form") or "")
        if scene_form in SCENE_FORMS:
            return scene_form
    return "plot_scene"


def serialize_evaluation(row: WriterEvaluation | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "evaluation_id": row.evaluation_id,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "chapter_id": row.chapter_id,
        "scene_id": row.scene_id,
        "rubric_id": row.rubric_id,
        "source_text_ref": row.source_text_ref,
        "source_bundle_id": row.source_bundle_id,
        "evaluator_llm_call_id": row.evaluator_llm_call_id,
        "lens": row.lens or "aggregate",
        "parent_evaluation_id": row.parent_evaluation_id,
        "evidence_spans": row.evidence_spans_json or [],
        "overall_score": row.overall_score,
        "scores": row.scores_json or {},
        "findings": row.findings_json or [],
        "failure_class": row.failure_class,
        "auto_rewrite_eligible": bool(row.auto_rewrite_eligible) if row.auto_rewrite_eligible is not None else None,
        "contract_field_refs": row.contract_field_refs_json or {},
        "promotion_blockers": row.promotion_blockers_json or [],
        "scene_form": scene_form_from_findings(row.findings_json or [], row.object_type),
        "revision_brief": row.revision_brief_json or [],
        "requires_human_review": bool(row.requires_human_review),
        "status": row.status,
        "created_at": row.created_at,
    }


def serialize_patch_candidate(row: PassagePatchCandidate) -> dict[str, Any]:
    return {
        "patch_id": row.patch_id,
        "object_type": row.object_type,
        "object_id": row.object_id,
        "chapter_id": row.chapter_id,
        "scene_id": row.scene_id,
        "source_text_ref": row.source_text_ref,
        "target_text_ref": row.target_text_ref,
        "source_draft_id": row.source_draft_id,
        "generation_llm_call_id": row.generation_llm_call_id,
        "quality_signal_id": row.quality_signal_id,
        "source_excerpt": row.source_excerpt,
        "issue_dimension": row.issue_dimension,
        "candidate_category": row.candidate_category,
        "target_range": row.target_range_json or None,
        "revision_strategy": row.revision_strategy,
        "preference_tags": row.preference_tags_json or [],
        "inserted_into_author_draft": bool(row.inserted_into_author_draft),
        "replacement_options": row.replacement_options_json or [],
        "rationale": row.rationale,
        "manual_only": bool(row.manual_only),
        "status": row.status,
        "author_decision": row.author_decision,
        "selected_option_id": row.selected_option_id,
        "author_decision_note": row.author_decision_note,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class SceneDiagnosisService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- 正文 --------------------------------------------------------------

    def text_for_scene(self, scene: SceneCard) -> DiagnosisText:
        """诊断的正文 = 写作台看到的那份：当前作者稿，其次运行终稿，否则没有正文。"""

        draft = self._current_author_draft(scene.scene_id)
        if draft is not None:
            content = draft.content or ""
            return DiagnosisText(
                layer="author_draft",
                ref=f"author_draft:{draft.draft_id}",
                content=content,
                paragraphs=manuscript_paragraphs(content),
                updated_at=draft.updated_at,
            )
        final = self._final_scene(scene.scene_id)
        if final is not None and (final.content or "").strip():
            content = final.content or ""
            return DiagnosisText(
                layer="runtime_final_scene",
                ref=f"final_scene:{final.row_id}",
                content=content,
                paragraphs=manuscript_paragraphs(content),
                updated_at=final.created_at,
            )
        return DiagnosisText(layer="none", ref=None, content="", paragraphs=[], updated_at=None)

    def _current_author_draft(self, scene_id: str) -> AuthorDraft | None:
        return self.session.execute(
            select(AuthorDraft)
            .where(
                AuthorDraft.object_type == "scene",
                AuthorDraft.object_id == scene_id,
                AuthorDraft.status == "current",
            )
            .order_by(AuthorDraft.updated_at.desc(), AuthorDraft.draft_id.desc())
        ).scalars().first()

    def _final_scene(self, scene_id: str) -> FinalScene | None:
        state = self.session.get(SceneRunState, scene_id)
        if state is not None and state.current_final_scene_row_id:
            pointed = self.session.get(FinalScene, state.current_final_scene_row_id)
            if pointed is not None and pointed.scene_id == scene_id:
                return pointed
        return self.session.execute(
            select(FinalScene)
            .where(FinalScene.scene_id == scene_id)
            .order_by(FinalScene.created_at.desc(), FinalScene.row_id.desc())
        ).scalars().first()

    # -- 评审行 ------------------------------------------------------------

    def latest_evaluation(self, scene_id: str, rubric_id: str) -> WriterEvaluation | None:
        return self.session.execute(
            select(WriterEvaluation)
            .where(
                WriterEvaluation.object_type == "scene",
                WriterEvaluation.object_id == scene_id,
                WriterEvaluation.rubric_id == rubric_id,
                WriterEvaluation.parent_evaluation_id.is_(None),
            )
            .order_by(WriterEvaluation.created_at.desc(), WriterEvaluation.evaluation_id.desc())
        ).scalars().first()

    def lens_rows(self, parent_id: str) -> list[WriterEvaluation]:
        return list(
            self.session.execute(
                select(WriterEvaluation)
                .where(WriterEvaluation.parent_evaluation_id == parent_id)
                .order_by(WriterEvaluation.lens.asc(), WriterEvaluation.evaluation_id.asc())
            ).scalars().all()
        )

    def _evaluation_status(self, row: WriterEvaluation | None, text: DiagnosisText) -> str:
        """``not_run`` / ``current`` / ``stale``：评审看的还是不是现在这份正文。

        评审记的是 ``source_text_ref``：``final_scene:<row>`` 的内容不会变，直接比正文是否相同
        （作者稿常是从终稿复制出来的，同一份字就是 current）；``author_draft:<id>`` 的行会被
        原地改写，只能看时间——空保存不改时间戳（AuthorDraftService.save 内容未变即返回），
        所以「草稿的 updated_at 晚于评审的 created_at」就是「改过了」。
        """

        if row is None:
            return "not_run"
        if text.layer == "none":
            return "stale"
        ref = str(row.source_text_ref or "")
        if ref.startswith("final_scene:"):
            final = self.session.get(FinalScene, ref.split(":", 1)[1])
            if final is None:
                return "stale"
            reviewed = " ".join(part for part in manuscript_paragraphs(final.content or "") if part.strip())
            return "current" if _compact(reviewed) == text.compact() else "stale"
        if ref.startswith("author_draft:"):
            if ref != str(text.ref or ""):
                return "stale"
            if text.updated_at and row.created_at and str(text.updated_at) > str(row.created_at):
                return "stale"
            return "current"
        return "stale"

    # -- 风格绑定 ----------------------------------------------------------

    def style_bound(self, scene: SceneCard) -> bool:
        project_id = getattr(scene, "project_id", None)
        if not project_id:
            return False
        try:
            from novel_system.services.style_reference.injection import InjectionService

            layers = InjectionService(self.session).resolve_binding_layers(
                str(project_id),
                "scene_generation",
                character_ids=[],
                scene_id=scene.scene_id,
            )
            return bool(layers)
        except Exception:  # noqa: BLE001 — 绑定解析失败按无绑定处理（房风规则照常给）
            return False

    # -- 载荷 --------------------------------------------------------------

    def payload(self, scene_id: str) -> dict[str, Any]:
        scene = require_scene(self.session, scene_id, trashed_as_conflict=True)
        text = self.text_for_scene(scene)
        style_bound = self.style_bound(scene)
        ignored = {str(key) for key in (scene.deep_review_ignored_keys_json or []) if str(key)}

        findings: list[dict[str, Any]] = []
        if text.layer != "none":
            findings.extend(rule_findings(text, house_taste=style_bound))
            findings.extend(craft_findings(text, house_taste=style_bound))

        review_row = self.latest_evaluation(scene.scene_id, NEAR_FINAL_RUBRIC_ID)
        if review_row is not None and text.layer != "none":
            findings.extend(_evaluation_findings(review_row, text, source="review", label_for=REVIEW_DIMENSION_LABELS))

        ai_row = self.latest_evaluation(scene.scene_id, LITERARY_REVISION_RUBRIC_ID)
        if ai_row is not None and text.layer != "none":
            findings.extend(_evaluation_findings(ai_row, text, source="ai", label_for=AI_DIMENSION_LABELS))

        # 同一处同一维度只留一条（评审与深评常指向同一句）：按来源顺序先到先得
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for finding in findings:
            if finding["signal_id"] in seen:
                continue
            seen.add(finding["signal_id"])
            finding["ignored"] = finding["signal_id"] in ignored
            deduped.append(finding)
        deduped.sort(key=_finding_sort_key)

        open_findings = [finding for finding in deduped if not finding["ignored"]]
        summary = {
            "total": len(deduped),
            "open": len(open_findings),
            "ignored": len(deduped) - len(open_findings),
            "stale": sum(1 for finding in open_findings if finding.get("stale")),
            "by_severity": {level: sum(1 for finding in open_findings if finding["severity"] == level) for level in SEVERITIES},
            "by_source": {source: sum(1 for finding in open_findings if finding["source"] == source) for source in DIAGNOSIS_SOURCES},
        }

        ai_lenses = self.lens_rows(ai_row.evaluation_id) if ai_row is not None else []
        latest_evaluation = serialize_evaluation(ai_row)
        patch_rows = self.session.execute(
            select(PassagePatchCandidate)
            .where(PassagePatchCandidate.object_type == "scene", PassagePatchCandidate.object_id == scene.scene_id)
            .order_by(PassagePatchCandidate.created_at.desc(), PassagePatchCandidate.patch_id.desc())
            .limit(PATCH_CANDIDATE_LIMIT)
        ).scalars().all()

        return {
            "scene_id": scene.scene_id,
            "chapter_id": scene.chapter_id,
            "project_id": getattr(scene, "project_id", None),
            "text": {
                "layer": text.layer,
                "ref": text.ref,
                "sha256": text.sha256 if text.layer != "none" else None,
                "paragraph_count": len(text.paragraphs),
                "chars": text.chars,
            },
            "style_bound": style_bound,
            "findings": deduped,
            "summary": summary,
            "ai": {
                "status": self._evaluation_status(ai_row, text),
                "evaluation_id": ai_row.evaluation_id if ai_row is not None else None,
                "overall_score": ai_row.overall_score if ai_row is not None else None,
                "revision_brief": list(ai_row.revision_brief_json or []) if ai_row is not None else [],
                "lenses": [
                    {"lens": row.lens, "label": LENS_LABELS.get(str(row.lens or ""), str(row.lens or "")), "overall_score": row.overall_score}
                    for row in ai_lenses
                ],
                "llm_call_id": ai_row.evaluator_llm_call_id if ai_row is not None else None,
                "created_at": ai_row.created_at if ai_row is not None else None,
                "source_text_ref": ai_row.source_text_ref if ai_row is not None else None,
            },
            "review": {
                "status": self._evaluation_status(review_row, text),
                "evaluation_id": review_row.evaluation_id if review_row is not None else None,
                "overall_score": review_row.overall_score if review_row is not None else None,
                "failure_class": review_row.failure_class if review_row is not None else None,
                "revision_brief": list(review_row.revision_brief_json or []) if review_row is not None else [],
                "created_at": review_row.created_at if review_row is not None else None,
                "source_text_ref": review_row.source_text_ref if review_row is not None else None,
            },
            "preferences": {
                "revision_no": int(scene.deep_review_preferences_revision_no or 0),
                "decision_log": list(scene.deep_review_decision_log_json or []),
                "ignored_issue_keys": sorted(ignored),
            },
            "patch_candidates": [serialize_patch_candidate(row) for row in patch_rows],
            # 旧契约的键（写作台以外的调用方 / 测试仍读它们）
            "status": "reviewed" if ai_row is not None else "not_run",
            "object_type": "scene",
            "object_id": scene.scene_id,
            "rubric_id": LITERARY_REVISION_RUBRIC_ID,
            "latest_evaluation": latest_evaluation,
            "latest_score": latest_evaluation["overall_score"] if latest_evaluation else None,
            "requires_human_review": bool(latest_evaluation["requires_human_review"]) if latest_evaluation else False,
            "lens_evaluations": [item for item in (serialize_evaluation(row) for row in ai_lenses) if item],
        }


def _finding_sort_key(finding: dict[str, Any]) -> tuple[int, int, int, str]:
    evidence = finding.get("evidence") or {}
    paragraph = evidence.get("paragraph_index")
    return (
        SEVERITY_RANK.get(str(finding.get("severity")), 99),
        paragraph if isinstance(paragraph, int) else 1_000_000,
        DIAGNOSIS_SOURCES.index(finding["source"]) if finding.get("source") in DIAGNOSIS_SOURCES else 99,
        str(finding.get("signal_id") or ""),
    )

