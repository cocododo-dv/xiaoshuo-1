"""场景诊断（2026-09-22）：一场正文「哪里有问题」只有一份记录。

之前有四套引擎各算各的——文学质量视图的 21 维规则、写作台深改姿态里三条浏览器本地正则、
后端从没被任何界面调用过的 LLM 深评（``writer_deep_review``）、起草管线在去模板门 / 成稿门
里再跑一遍的同一批规则——三套词汇、三种严重度，页面之间只有跳转、没有数据。这里把它们
合成**一种**发现形状，写作台的深改面板是唯一的展示处：

    {signal_id, source, dimension, label, lens, severity, issue, recommendation, why,
     evidence: {excerpt, paragraph_index, start, end} | None, context,
     ignored, stale, house_taste, origin, patch, opinion}

* ``source``：``rules``（21 维规则，`literary_quality.py`）/ ``craft``（段落节奏：贴邻叠句、
  段落偏长、句首重复——从浏览器本地规则搬到服务端）/ ``review``（起草台的准定稿评审）/
  ``ai``（写作台的 AI 深评：整场一次、「AI 看这一处」的局部深评、成稿中心「AI 通读本章」
  里落到这一场的发现——``origin.kind`` 区分 ``scene`` / ``passage`` / ``chapter``）。
* ``signal_id`` 稳定：规则按「命中了什么」取 id（`literary_quality.rule_signal_id`），节奏按
  段落开头与命中文本，评审 / 深评按维度 + 证据。作者的「忽略」记在 ``SceneCard`` 的
  ``deep_review_ignored_keys_json`` 里，按 id 生效，并反向作用到文学质量视图与成稿门。
* ``evidence`` 钉到**编辑器段落**：``paragraph_index`` 是写作台 ``p, blockquote`` 的序号，
  ``start / end`` 是这一段可见文字里的偏移；正文是作者稿 HTML，这里按同一规则拆段。
* ``stale``：评审 / 深评的证据在当前正文里已找不到（多半改掉了）；``ai.status`` /
  ``review.status`` 另说整份评审是不是改前的。
* ``opinion``：「AI 看这一处」对这条发现的判断（成立 / 部分成立 / 不成立）与改法。局部深评看的是
  焦点段（一段、一段范围或几条发现所在的段）加**整场正文**（``passage_scope``：焦点段与前后段标出，
  其余段全文给到 ``PASSAGE_SCENE_FULL_CHARS`` 字，再长的远段只留开头），所以它能指出焦点段与本场
  另一段的矛盾——那样的发现带 ``related``（另一段的原话与段号）。
* 有风格绑定的场，检查按参考作者校准（``craft_calibration``）：节奏三条（段落偏长的阈值取参考书
  段长的长尾，参考作者常用的贴邻叠句 / 句首重复不再提示）+ 21 维规则的词表与维度
  （``literary_quality.RuleCalibration``：参考作者每万字用到一次以上的词表词不当毛病，在参考书一半
  以上的场级窗口上都会响的规则降为提示并带 ``calibrated``）；规则与节奏发现标 ``house_taste``。
* 计数随写回传：``scene_rollup`` / ``chapter_rollup`` 是作者稿保存、深评动作、通读的响应里带的
  ``diagnosis_rollup``——主页 / 成稿中心的角标不必再拉整本书的汇总。

叶子模块：只依赖 ``literary_quality``、``manuscript_html``、两个纯函数的段型判断、模型与风格绑定
解析；``writer_deep_review`` / ``api.routes`` 从这里取载荷。
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    FinalScene,
    PassagePatchCandidate,
    SceneCard,
    SceneDraft,
    SceneRunState,
    StoryProject,
    StyleReferenceBook,
    StyleReferenceParagraph,
    StyleReferenceProfile,
    WriterEvaluation,
)
from novel_system.services.errors import DomainError
from novel_system.services.manuscript_html import manuscript_paragraphs
from novel_system.services.literary_quality import (
    DEFAULT_RULE_CALIBRATION,
    FAULT_LEXICONS,
    RULE_ENDING_DIMENSIONS,
    SEVERITY_RANK,
    RuleCalibration,
    analyze_literary_quality,
    calibrate_lexicons,
    dimension_label,
    dimension_level,
    unify_rule_finding,
)
from novel_system.services.scene_lookup import require_chapter, require_scene
from novel_system.services.style_policy import StylePolicy, style_policy_live
from novel_system.services.style_reference.segmentation.heuristic import is_title_paragraph
from novel_system.services.style_reference.text_utils import is_scene_break_paragraph

# 评审来源的 rubric id。深评的在这里定义（writer_deep_review 从这里取）；准定稿评审的
# 与 near_final.NEAR_FINAL_RUBRIC_ID 相同——测试钉住两者相等，这里不 import near_final
# （它牵着整条管线，会把这个叶子拖进环）。
LITERARY_REVISION_RUBRIC_ID = "literary_revision_v1"
LITERARY_REVISION_PASSAGE_RUBRIC_ID = "literary_revision_passage_v1"
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
PASSAGE_VERDICTS: tuple[str, ...] = ("holds", "partly", "does_not_hold", "no_finding")
PASSAGE_VERDICT_LABELS: dict[str, str] = {
    "holds": "成立",
    "partly": "部分成立",
    "does_not_hold": "不成立",
    "no_finding": "没有要改的",
}
EVALUATION_STATUSES: tuple[str, ...] = ("not_run", "current", "stale")

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
# 参考作者的习惯：每千段里贴邻叠句 / 三句同字开头的段落数到了这个水平，就是这位作者的手法，不提示
CRAFT_ECHO_HABIT_PER_1K = 5.0
CRAFT_SAME_OPENING_HABIT_PER_1K = 10.0
# 21 维规则的校准：参考书按标题段 / 场分隔行 / 导入时记下的场界切成单元，单元内按 ~2400 字（一场的量）切窗口；
# 一般维度在最多 96 个窗口上量「响的比例」，收尾三条只在最多 96 个真实收尾（章末 / 场界，不够时补转场段之前的
# 那一段）上量；窗口 / 收尾都至少要 4 个才算数（更少的样本连 Wilson 下界也撑不起来）
RULE_CALIBRATION_WINDOW_CHARS = 2400
RULE_CALIBRATION_MAX_WINDOWS = 96
RULE_CALIBRATION_MAX_ENDINGS = 96
RULE_CALIBRATION_MIN_WINDOWS = 4
RULE_CALIBRATION_MIN_ENDINGS = 4
TRANSITION_PARAGRAPH_TYPE = "transition"
# 局部深评：整场不超过这个字数就全文给模型（焦点段与前后段标出），再长的远段只留开头
PASSAGE_SCENE_FULL_CHARS = 12000
PASSAGE_FAR_PARAGRAPH_HEAD = 40
PASSAGE_RELATION_KINDS: tuple[str, ...] = ("contradiction", "repetition", "continuity")
PASSAGE_RELATION_LABELS: dict[str, str] = {"contradiction": "矛盾", "repetition": "重复", "continuity": "承接"}
_ECHO_RE = re.compile(r"([一-龥]{2,5})([，、；]?)\1")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])")
_WS_RE = re.compile(r"\s+")
PATCH_CANDIDATE_LIMIT = 20


# ---------------------------------------------------------------------------
# 正文与定位
# ---------------------------------------------------------------------------


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


def passage_scope(
    paragraphs: list[str],
    focus: list[int],
    *,
    context: int = 1,
    full_chars: int = PASSAGE_SCENE_FULL_CHARS,
) -> dict[str, Any]:
    """局部深评看的范围：焦点段（一段、一段范围或几条发现所在的段）标 【焦点段 N】，前后各 ``context``
    段标 【上下文 N】，其余段按 【第 N 段】 全文给出——整场不超过 ``full_chars`` 字时模型看得到全场，
    跨段的矛盾就能对上；再长的远段只留开头（【第 N 段·略】），至少知道前后发生了什么。"""

    total = len(paragraphs)
    focus_set = sorted({int(index) for index in focus if 0 <= int(index) < total})
    near = {
        neighbour
        for index in focus_set
        for neighbour in range(index - context, index + context + 1)
        if 0 <= neighbour < total and neighbour not in focus_set
    }
    whole_scene = sum(len(paragraph) for paragraph in paragraphs) <= full_chars
    lines: list[str] = []
    abbreviated = 0
    for index, paragraph in enumerate(paragraphs):
        if index in focus_set:
            lines.append(f"【焦点段 {index + 1}】{paragraph}")
        elif index in near:
            lines.append(f"【上下文 {index + 1}】{paragraph}")
        elif whole_scene:
            lines.append(f"【第 {index + 1} 段】{paragraph}")
        else:
            head = paragraph[:PASSAGE_FAR_PARAGRAPH_HEAD]
            abbreviated += 1
            lines.append(f"【第 {index + 1} 段·略】{head}{'……' if len(paragraph) > len(head) else ''}")
    shown = sorted(set(focus_set) | near)
    return {
        "focus": focus_set,
        "start": shown[0] if shown else 0,
        "end": shown[-1] if shown else 0,
        "whole_scene": whole_scene,
        "abbreviated": abbreviated,
        "focus_text": "\n\n".join(paragraphs[index] for index in focus_set),
        "text": "\n\n".join(lines),
    }


# ---------------------------------------------------------------------------
# 缓存：同一场同一份字的规则 / 节奏发现（21 维规则一场约 30 ms，全书计数一次跑几十场）
# ---------------------------------------------------------------------------

_FINDINGS_CACHE: "OrderedDict[tuple[Any, ...], list[dict[str, Any]]]" = OrderedDict()
_FINDINGS_CACHE_MAX = 512
STYLE_TASK_TYPE = "scene_generation"


@dataclass(frozen=True)
class BoundProfile:
    """一场绑定的（最具体那一层的）画像——只带校准要用的三样，不加载整个 profile_json（几十万字的窗口索引）。"""

    profile_id: str
    book_id: str | None
    deliberate_repetition: bool = False


# ---------------------------------------------------------------------------
# 节奏检查的校准（参考作者）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CraftCalibration:
    source: str = "default"  # default | reference
    profile_id: str | None = None
    book_id: str | None = None
    book_title: str | None = None
    paragraphs: int = 0
    long_paragraph_chars: int = CRAFT_LONG_PARAGRAPH_CHARS
    echo_per_1k: float | None = None
    same_opening_per_1k: float | None = None
    flag_echo: bool = True
    flag_same_opening: bool = True
    deliberate_repetition: bool = False
    # 2026-09-22 第三轮：21 维规则的词表 / 维度校准也挂在这里（写作台读的是同一个 craft_calibration 载荷）
    rules: RuleCalibration = DEFAULT_RULE_CALIBRATION

    @property
    def note(self) -> str:
        if self.source != "reference":
            return ""
        title = f"《{self.book_title}》" if self.book_title else "参考书"
        parts = [f"段落超过 {self.long_paragraph_chars} 字才提示"]
        habits: list[str] = []
        if not self.flag_echo:
            habits.append("贴邻叠句")
        if not self.flag_same_opening:
            habits.append("句首重复")
        if habits:
            reason = "这位作者刻意用重复" if self.deliberate_repetition else "这位作者常这么写"
            parts.append(f"{' / '.join(habits)}不提示（{reason}）")
        if self.rules.active:
            rules = self.rules.as_dict()
            if rules["top_needles"]:
                sample = "、".join(f"{item['term']} {item['per_10k']:g}" for item in rules["top_needles"][:4])
                parts.append(f"词表词按这位作者的密度判（每万字：{sample}…），寻常用法不当毛病")
            if self.rules.habitual_dimensions:
                labels = [dimension_label(item) or item for item in sorted(self.rules.habitual_dimensions)]
                parts.append(f"「{' / '.join(labels[:4])}{'…' if len(labels) > 4 else ''}」是这位作者的常态，只作提示")
            if self.rules.common_dimensions:
                labels = [dimension_label(item) or item for item in sorted(self.rules.common_dimensions)]
                parts.append(f"「{' / '.join(labels[:4])}{'…' if len(labels) > 4 else ''}」在这位作者的场里也常见，按审美看")
            if self.rules.endings_source == "none":
                parts.append("参考书没有章节与场的分界，收尾三条没有校准")
            elif self.rules.endings_source in {"transitions", "units+transitions"}:
                parts.append(f"收尾三条按 {self.rules.endings} 个真实收尾校准（含转场段之前的那一段）")
        return f"按{title}校准：{'；'.join(parts)}。"

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "profile_id": self.profile_id,
            "book_id": self.book_id,
            "book_title": self.book_title,
            "paragraphs": self.paragraphs,
            "long_paragraph_chars": self.long_paragraph_chars,
            "echo_per_1k": self.echo_per_1k,
            "same_opening_per_1k": self.same_opening_per_1k,
            "flag_echo": self.flag_echo,
            "flag_same_opening": self.flag_same_opening,
            "deliberate_repetition": self.deliberate_repetition,
            "rules": self.rules.as_dict() if self.rules.active else None,
            "note": self.note,
        }


DEFAULT_CRAFT_CALIBRATION = CraftCalibration()
_REFERENCE_CRAFT_CACHE: dict[tuple[str, int, str], dict[str, Any]] = {}


def _evenly(items: list[str], limit: int) -> list[str]:
    if len(items) <= limit:
        return list(items)
    step = len(items) / float(limit)
    return [items[int(index * step)] for index in range(limit)]


def _needle_rates(corpus: str, chars: int) -> dict[str, float]:
    """「命中即毛病」词表里每个词在参考书里每万字的次数（一遍正则；短词加上含它的长词的命中）。"""

    needles = sorted({term for terms in FAULT_LEXICONS.values() for term in terms if term.strip()}, key=len, reverse=True)
    if not needles or chars <= 0:
        return {}
    pattern = re.compile("|".join(re.escape(term.lower()) for term in needles))
    counts: Counter[str] = Counter(match.group(0) for match in pattern.finditer(corpus.lower()))
    rates: dict[str, float] = {}
    for term in needles:
        lowered = term.lower()
        count = counts.get(lowered, 0) + sum(hits for hit, hits in counts.items() if hit != lowered and lowered in hit)
        if count:
            rates[term] = round(10000.0 * count / chars, 3)
    return rates


def _reference_units(
    paragraphs: list[str],
    *,
    paragraph_types: list[str] | None,
    scene_breaks: Iterable[int] | None,
) -> tuple[list[list[str]], int, int]:
    """参考书切成单元：标题段 / 纯符号分隔行 / 导入时记下的场界（含空行分界）都是结构分界。
    结构分界太少时（不到 ``RULE_CALIBRATION_MIN_ENDINGS`` 个真实收尾）再按转场段补：分类器标为
    ``transition`` 的段之前的那一段也算一个收尾。返回 (单元, 结构分界数, 转场补充数)。"""

    types = list(paragraph_types or [])
    break_after = {int(index) for index in (scene_breaks or ()) if isinstance(index, int) and not isinstance(index, bool)}
    kept: list[tuple[int, str, str]] = []  # (原索引, 正文, 段型)
    for index, paragraph in enumerate(paragraphs):
        body = str(paragraph or "").strip()
        if body:
            kept.append((index, body, str(types[index] if index < len(types) else "") or ""))

    def cut(use_transitions: bool) -> tuple[list[list[str]], int, int]:
        units: list[list[str]] = []
        current: list[str] = []
        structural = 0
        transitional = 0
        for position, (index, body, paragraph_type) in enumerate(kept):
            if is_title_paragraph(body) or is_scene_break_paragraph(body):
                if current:
                    units.append(current)
                    current = []
                    structural += 1
                continue
            if use_transitions and paragraph_type == TRANSITION_PARAGRAPH_TYPE and current and position > 0:
                units.append(current)
                current = []
                transitional += 1
            current.append(body)
            if index in break_after:
                units.append(current)
                current = []
                structural += 1
        if current:
            units.append(current)
        return units, structural, transitional

    units, structural, _ = cut(False)
    if structural + 1 >= RULE_CALIBRATION_MIN_ENDINGS:
        return units, structural, 0
    with_transitions, structural_again, transitional = cut(True)
    if transitional:
        return with_transitions, structural_again, transitional
    return units, structural, 0


def compute_reference_rules(
    paragraphs: list[str],
    *,
    paragraph_types: list[str] | None = None,
    scene_breaks: Iterable[int] | None = None,
) -> dict[str, Any]:
    """参考书上的 21 维规则读数：词表词的密度（每万字）与每条规则在场级窗口上「响」的次数。

    参考书按 ``_reference_units`` 切成单元，单元内按 ~2400 字切窗口；收尾三条（summary_ending /
    ending_drive / false_poetic_closure）只在真实的单元末尾上量——随手切的窗口末尾不是收尾。
    ``endings_source`` 记收尾从哪来：``units``（章末 / 场界）、``units+transitions`` / ``transitions``
    （补了转场段之前的那一段）、``none``（不够 4 个真实收尾：收尾三条不校准）。
    """

    units, structural, transitional = _reference_units(paragraphs, paragraph_types=paragraph_types, scene_breaks=scene_breaks)
    corpus = "\n".join(paragraph for unit in units for paragraph in unit)
    chars = len(_WS_RE.sub("", corpus))
    empty = {"chars": 0, "windows": 0, "endings": 0, "endings_source": "none", "needle_rates": {}, "dimension_stats": {}, "dimension_shares": {}}
    if not chars:
        return empty

    windows: list[str] = []
    endings: list[str] = []
    for unit in units:
        chunks: list[str] = []
        buffer: list[str] = []
        size = 0
        for paragraph in unit:
            buffer.append(paragraph)
            size += len(paragraph)
            if size >= RULE_CALIBRATION_WINDOW_CHARS:
                chunks.append(" ".join(buffer))
                buffer, size = [], 0
        if buffer:
            if chunks and size < RULE_CALIBRATION_WINDOW_CHARS // 4:
                chunks[-1] = chunks[-1] + " " + " ".join(buffer)
            else:
                chunks.append(" ".join(buffer))
        if not chunks:
            continue
        endings.append(chunks[-1])
        windows.extend(chunks[:-1])
    if not windows:
        windows = list(endings)
    windows = _evenly(windows, RULE_CALIBRATION_MAX_WINDOWS)
    endings = _evenly(endings, RULE_CALIBRATION_MAX_ENDINGS)
    endings_usable = len(endings) >= RULE_CALIBRATION_MIN_ENDINGS
    if not endings_usable:
        endings_source = "none"
    elif transitional and structural:
        endings_source = "units+transitions"
    elif transitional:
        endings_source = "transitions"
    else:
        endings_source = "units"

    stats: dict[str, dict[str, int]] = {}
    if len(windows) >= RULE_CALIBRATION_MIN_WINDOWS:
        fired: Counter[str] = Counter()
        for window in windows:
            _, findings = analyze_literary_quality(window)
            for dimension in {str(item.get("dimension") or "") for item in findings}:
                if dimension and dimension not in RULE_ENDING_DIMENSIONS:
                    fired[dimension] += 1
        stats.update({dimension: {"fired": count, "n": len(windows)} for dimension, count in fired.items()})
    if endings_usable:
        ending_fired: Counter[str] = Counter()
        for window in endings:
            _, findings = analyze_literary_quality(window)
            for dimension in {str(item.get("dimension") or "") for item in findings}:
                if dimension in RULE_ENDING_DIMENSIONS:
                    ending_fired[dimension] += 1
        stats.update({dimension: {"fired": count, "n": len(endings)} for dimension, count in ending_fired.items()})
    return {
        "chars": chars,
        "windows": len(windows),
        "endings": len(endings) if endings_usable else 0,
        "endings_source": endings_source,
        "needle_rates": _needle_rates(corpus, chars),
        "dimension_stats": stats,
        "dimension_shares": {dimension: round(item["fired"] / item["n"], 3) for dimension, item in stats.items() if item["n"]},
    }


def rule_calibration_from_reference(stats: dict[str, Any] | None, *, deliberate_repetition: bool = False) -> RuleCalibration:
    if not stats or not int(stats.get("chars") or 0):
        return DEFAULT_RULE_CALIBRATION
    rates = {str(key): float(value) for key, value in (stats.get("needle_rates") or {}).items()}
    dimension_stats: dict[str, dict[str, Any]] = {}
    for dimension, item in (stats.get("dimension_stats") or {}).items():
        fired = int((item or {}).get("fired") or 0)
        total = int((item or {}).get("n") or 0)
        if total <= 0:
            continue
        level, lower = dimension_level(fired, total)
        dimension_stats[str(dimension)] = {
            "fired": fired,
            "n": total,
            "share": round(fired / total, 3),
            "lower_bound": round(lower, 3),
            "level": level,
        }
    return RuleCalibration(
        source="reference",
        needle_rates=rates,
        dimension_stats=dimension_stats,
        deliberate_repetition=bool(deliberate_repetition),
        windows=int(stats.get("windows") or 0),
        endings=int(stats.get("endings") or 0),
        endings_source=str(stats.get("endings_source") or "none"),
        chars=int(stats.get("chars") or 0),
    )


def _same_opening_hit(paragraph: str) -> dict[str, Any] | None:
    sentences = [part for part in _SENTENCE_SPLIT_RE.split(paragraph) if part.strip()]
    for offset in range(len(sentences) - 2):
        heads = [sentence.strip()[:1] for sentence in sentences[offset : offset + 3]]
        if heads[0] and heads[0] == heads[1] == heads[2]:
            span_text = "".join(sentences[offset : offset + 3])
            start = paragraph.find(span_text)
            return {"head": heads[0], "span_text": span_text, "start": start}
    return None


def compute_reference_craft(paragraphs: list[str]) -> dict[str, Any]:
    """参考书段落表上的三个节奏读数：段长 p95、每千段贴邻叠句数、每千段三句同字开头数。"""

    bodies = [str(paragraph or "").strip() for paragraph in paragraphs]
    bodies = [body for body in bodies if body]
    count = len(bodies)
    if not count:
        return {"paragraphs": 0, "long_paragraph_p95": 0, "echo_per_1k": 0.0, "same_opening_per_1k": 0.0}
    lengths = sorted(len(body) for body in bodies)
    p95 = lengths[min(count - 1, int(round(0.95 * (count - 1))))]
    echo = sum(1 for body in bodies if _ECHO_RE.search(body))
    same_opening = sum(1 for body in bodies if _same_opening_hit(body) is not None)
    return {
        "paragraphs": count,
        "long_paragraph_p95": int(p95),
        "echo_per_1k": round(1000.0 * echo / count, 2),
        "same_opening_per_1k": round(1000.0 * same_opening / count, 2),
    }


def calibration_from_reference(
    *,
    profile_id: str | None,
    book_id: str | None,
    book_title: str | None,
    stats: dict[str, Any],
    deliberate_repetition: bool,
    rule_stats: dict[str, Any] | None = None,
) -> CraftCalibration:
    echo_rate = float(stats.get("echo_per_1k") or 0.0)
    opening_rate = float(stats.get("same_opening_per_1k") or 0.0)
    return CraftCalibration(
        source="reference",
        profile_id=profile_id,
        book_id=book_id,
        book_title=book_title,
        paragraphs=int(stats.get("paragraphs") or 0),
        long_paragraph_chars=max(CRAFT_LONG_PARAGRAPH_CHARS, int(stats.get("long_paragraph_p95") or 0)),
        echo_per_1k=echo_rate,
        same_opening_per_1k=opening_rate,
        flag_echo=not deliberate_repetition and echo_rate < CRAFT_ECHO_HABIT_PER_1K,
        flag_same_opening=not deliberate_repetition and opening_rate < CRAFT_SAME_OPENING_HABIT_PER_1K,
        deliberate_repetition=deliberate_repetition,
        rules=rule_calibration_from_reference(rule_stats, deliberate_repetition=deliberate_repetition),
    )


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


def rule_findings(
    text: DiagnosisText,
    *,
    house_taste: bool = False,
    calibration: RuleCalibration = DEFAULT_RULE_CALIBRATION,
) -> list[dict[str, Any]]:
    """21 维规则的发现。``calibration`` 来自参考书时：作者的常用词不再命中，作者常态的维度降为提示
    （``calibrated`` 说明它在参考书多少窗口上也会响）。"""

    _, raw = analyze_literary_quality(text.plain, calibration=calibration if calibration.active else None)
    findings: list[dict[str, Any]] = []
    for item in raw:
        unified = unify_rule_finding(item)
        evidence = _locate(
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
                "context": _compact(unified.get("evidence_excerpt") or ""),
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
        echo = _ECHO_RE.search(paragraph) if calibration.flag_echo else None
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
            opening = _same_opening_hit(paragraph)
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
        "context": _compact(evidence.get("excerpt") or "")[:160],
        "ignored": False,
        "stale": False,
        "house_taste": house_taste,
        "origin": None,
        "opinion": None,
        "patch": _patch_hint(kind, recommendation),
    }


def _evaluation_findings(
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
        excerpt = _compact(str(item.get("evidence_excerpt") or ""))
        evidence = _locate(text.paragraphs, needle=excerpt, excerpt=excerpt) if excerpt else None
        if evidence is None and origin_kind == "passage":
            # 局部深评没引到原句时，仍然钉在它看的那（第一）段上
            focus_list = [int(value) for value in (meta.get("focus_paragraphs") or []) if isinstance(value, int)]
            focus = focus_list[0] if focus_list else meta.get("paragraph_index")
            if isinstance(focus, int) and 0 <= focus < len(text.paragraphs):
                evidence = {"excerpt": text.paragraphs[focus][:80], "paragraph_index": focus, "start": None, "end": None}
        # 跨段的发现（焦点段与本场另一段矛盾 / 重复 / 承接）：另一段的原话也钉到段
        related: dict[str, Any] | None = None
        related_excerpt = _compact(str(item.get("related_excerpt") or ""))
        if related_excerpt:
            related_hit = _locate(text.paragraphs, needle=related_excerpt, excerpt=related_excerpt)
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


def _finding_counts(findings: list[dict[str, Any]]) -> dict[str, Any]:
    open_findings = [finding for finding in findings if not finding.get("ignored")]
    return {
        "total": len(findings),
        "open": len(open_findings),
        "ignored": len(findings) - len(open_findings),
        "stale": sum(1 for finding in open_findings if finding.get("stale")),
        "by_severity": {level: sum(1 for finding in open_findings if finding["severity"] == level) for level in SEVERITIES},
        "by_source": {source: sum(1 for finding in open_findings if finding["source"] == source) for source in DIAGNOSIS_SOURCES},
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


def serialize_passage_review(row: WriterEvaluation, status: str) -> dict[str, Any]:
    meta = row.contract_field_refs_json if isinstance(row.contract_field_refs_json, dict) else {}
    verdict = str(meta.get("verdict") or "no_finding")
    focus_paragraphs = [int(value) for value in (meta.get("focus_paragraphs") or []) if isinstance(value, int)]
    if not focus_paragraphs and isinstance(meta.get("paragraph_index"), int):
        focus_paragraphs = [int(meta["paragraph_index"])]
    return {
        "evaluation_id": row.evaluation_id,
        "paragraph_index": meta.get("paragraph_index"),
        "focus_paragraphs": focus_paragraphs,
        "paragraph_start": meta.get("paragraph_start", focus_paragraphs[0] if focus_paragraphs else None),
        "paragraph_end": meta.get("paragraph_end", focus_paragraphs[-1] if focus_paragraphs else None),
        "whole_scene": bool(meta.get("whole_scene", False)),
        "about_signal_id": meta.get("about_signal_id"),
        "about_signal_ids": [str(value) for value in (meta.get("about_signal_ids") or ([meta["about_signal_id"]] if meta.get("about_signal_id") else []))],
        "verdict": verdict if verdict in PASSAGE_VERDICTS else "no_finding",
        "verdict_label": PASSAGE_VERDICT_LABELS.get(verdict, PASSAGE_VERDICT_LABELS["no_finding"]),
        "assessment": str(meta.get("assessment") or ""),
        "rewrite_brief": str(meta.get("rewrite_brief") or ""),
        "question": str(meta.get("question") or ""),
        "findings_count": len([item for item in (row.findings_json or []) if isinstance(item, dict)]),
        "status": status,
        "llm_call_id": row.evaluator_llm_call_id,
        "created_at": row.created_at,
    }


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class SceneDiagnosisService:
    def __init__(self, session: Session) -> None:
        self.session = session
        # 风格参考 v3：每场一份 StylePolicy（轻量现解析，不冻结契约），一次请求内记住
        self._policy_memo: dict[str, StylePolicy] = {}

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

    def chapter_scenes(self, chapter_id: str) -> list[SceneCard]:
        return list(
            self.session.execute(
                select(SceneCard)
                .where(SceneCard.chapter_id == chapter_id, SceneCard.trashed_flag == 0)
                .order_by(SceneCard.scene_seq.asc(), SceneCard.scene_id.asc())
            ).scalars().all()
        )

    # -- 评审行 ------------------------------------------------------------

    def latest_evaluation(self, object_id: str, rubric_id: str, *, object_type: str = "scene") -> WriterEvaluation | None:
        return self.session.execute(
            select(WriterEvaluation)
            .where(
                WriterEvaluation.object_type == object_type,
                WriterEvaluation.object_id == object_id,
                WriterEvaluation.rubric_id == rubric_id,
                WriterEvaluation.parent_evaluation_id.is_(None),
                WriterEvaluation.status != "superseded",
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

    def passage_rows(self, scene_id: str) -> list[WriterEvaluation]:
        """这一场还有效的局部深评（同一段再看一次时旧的退位，见 writer_deep_review.run_passage_review）。"""

        return list(
            self.session.execute(
                select(WriterEvaluation)
                .where(
                    WriterEvaluation.object_type == "scene",
                    WriterEvaluation.object_id == scene_id,
                    WriterEvaluation.rubric_id == LITERARY_REVISION_PASSAGE_RUBRIC_ID,
                    WriterEvaluation.status != "superseded",
                )
                .order_by(WriterEvaluation.created_at.asc(), WriterEvaluation.evaluation_id.asc())
            ).scalars().all()
        )

    def _evaluation_status(self, row: WriterEvaluation | None, text: DiagnosisText) -> str:
        """``not_run`` / ``current`` / ``stale``：评审看的还是不是现在这份正文。

        评审记的是 ``source_text_ref``：``final_scene:<row>`` / ``source_draft:<row>`` 的内容不会变，
        直接比正文是否相同（作者稿常是从终稿复制出来的，同一份字就是 current）；``author_draft:<id>``
        的行会被原地改写，只能看时间——空保存不改时间戳（AuthorDraftService.save 内容未变即返回），
        所以「草稿的 updated_at 晚于评审的 created_at」就是「改过了」。
        """

        if row is None:
            return "not_run"
        if text.layer == "none":
            return "stale"
        ref = str(row.source_text_ref or "")
        frozen = None
        if ref.startswith("final_scene:"):
            frozen = self.session.get(FinalScene, ref.split(":", 1)[1])
        elif ref.startswith("source_draft:"):
            frozen = self.session.get(SceneDraft, ref.split(":", 1)[1])
        if frozen is not None:
            reviewed = " ".join(part for part in manuscript_paragraphs(frozen.content or "") if part.strip())
            return "current" if _compact(reviewed) == text.compact() else "stale"
        if ref.startswith(("final_scene:", "source_draft:")):
            return "stale"
        if ref.startswith("author_draft:"):
            if ref != str(text.ref or ""):
                return "stale"
            if text.updated_at and row.created_at and str(text.updated_at) > str(row.created_at):
                return "stale"
            return "current"
        return "stale"

    def _chapter_evaluation_status(
        self,
        row: WriterEvaluation | None,
        texts: list[DiagnosisText],
        scene_ids: list[str] | None = None,
    ) -> str:
        """章级深评看的是各场拼起来的字。通读行记了每场正文的哈希（``contract_field_refs_json.scenes``）时按
        哈希判：哪一场的字变了、多了一场有字的、少了一场，整份就是改前的；老的行没有哈希，退回时间戳
        （任何一场的作者稿在它之后改过）。"""

        if row is None:
            return "not_run"
        if not texts or all(text.layer == "none" for text in texts):
            return "stale"
        if scene_ids is not None:
            changes = self.chapter_review_changes(row, scene_ids, texts)
            if changes is not None:
                return "stale" if changes["changed_scene_ids"] or changes["removed_scene_ids"] else "current"
        for text in texts:
            if text.layer == "author_draft" and text.updated_at and row.created_at and str(text.updated_at) > str(row.created_at):
                return "stale"
        return "current"

    def _scene_view_chapter_status(self, row: WriterEvaluation | None, scene: SceneCard, text: DiagnosisText) -> str:
        """写作台里这一场看到的通读新旧：只问「通读看的是不是这一场现在的字」（别的场改没改、多没多，那是成稿中心的事）。"""

        if row is None:
            return "not_run"
        changes = self.chapter_review_changes(row, [scene.scene_id], [text])
        if changes is not None:
            return "stale" if scene.scene_id in changes["changed_scene_ids"] else "current"
        return self._chapter_evaluation_status(row, [text])

    @staticmethod
    def chapter_review_changes(
        row: WriterEvaluation | None,
        scene_ids: list[str],
        texts: list[DiagnosisText],
    ) -> dict[str, Any] | None:
        """上一次通读之后哪些场的字变了。通读行没记哈希（老的行）→ None。"""

        meta = row.contract_field_refs_json if row is not None and isinstance(row.contract_field_refs_json, dict) else {}
        recorded = meta.get("scenes")
        if not isinstance(recorded, list):
            return None
        recorded_sha = {
            str(item.get("scene_id")): str(item.get("sha256") or "")
            for item in recorded
            if isinstance(item, dict) and item.get("scene_id")
        }
        changed: list[str] = []
        unchanged: list[str] = []
        for scene_id, text in zip(scene_ids, texts):
            current_sha = text.sha256 if text.layer != "none" else ""
            if scene_id not in recorded_sha:
                (changed if current_sha else unchanged).append(scene_id)
            elif recorded_sha[scene_id] != current_sha:
                changed.append(scene_id)
            else:
                unchanged.append(scene_id)
        removed = [scene_id for scene_id in recorded_sha if scene_id not in set(scene_ids) and recorded_sha[scene_id]]
        return {"changed_scene_ids": changed, "unchanged_scene_ids": unchanged, "removed_scene_ids": removed}

    # -- 风格绑定与校准 ----------------------------------------------------

    def _bound_profile(self, profile_id: str) -> BoundProfile:
        try:
            row = self.session.execute(
                select(
                    StyleReferenceProfile.book_id,
                    func.json_extract(StyleReferenceProfile.profile_json, "$.voice_signature.deliberate_repetition"),
                ).where(StyleReferenceProfile.profile_id == profile_id)
            ).first()
            if row is not None:
                return BoundProfile(profile_id=profile_id, book_id=row[0], deliberate_repetition=bool(row[1]) and str(row[1]) not in {"0", "false"})
        except Exception:  # noqa: BLE001 — 没有 json_extract 的库：退回整行
            pass
        profile = self.session.get(StyleReferenceProfile, profile_id)
        if profile is None:
            return BoundProfile(profile_id=profile_id, book_id=None)
        voice = (profile.profile_json or {}).get("voice_signature") if isinstance(profile.profile_json, dict) else None
        return BoundProfile(
            profile_id=profile_id,
            book_id=profile.book_id,
            deliberate_repetition=bool(voice.get("deliberate_repetition")) if isinstance(voice, dict) else False,
        )

    def style_policy(self, scene: SceneCard) -> StylePolicy:
        """这一场的风格策略（风格参考 v3）：诊断没有 bundle，按当前活动绑定轻量现解析（scene > character >
        project > global，同层取最新；画像不 active 的绑定不算），不冻结契约、不加载 profile_json。

        ``bound``：按参考书校准节奏检查与 21 维规则；``defers_house_taste()``（绑定且作者手笔直起）：规则 /
        节奏发现标 ``house_taste``——与成稿门、起草管线同一个判定（此前这里把 neutral_first 的绑定也当让位）。
        """

        memo = self._policy_memo.get(scene.scene_id)
        if memo is None:
            memo = style_policy_live(self.session, scene, task_type=STYLE_TASK_TYPE, freeze_contract=False)
            self._policy_memo[scene.scene_id] = memo
        return memo

    def bound_profile_for_policy(self, policy: StylePolicy) -> BoundProfile | None:
        """策略绑定的画像（校准要用的三样）；未绑定 → None。书以策略为准（冻结契约记下的那本）。"""

        if not policy.bound or not policy.profile_id:
            return None
        profile = self._bound_profile(policy.profile_id)
        return BoundProfile(
            profile_id=profile.profile_id,
            book_id=policy.book_id or profile.book_id,
            deliberate_repetition=profile.deliberate_repetition,
        )

    def binding_profile(self, scene: SceneCard) -> tuple[bool, BoundProfile | None]:
        """这一场有没有风格绑定，以及最具体那一层的画像（见 :meth:`style_policy`）。"""

        profile = self.bound_profile_for_policy(self.style_policy(scene))
        return (True, profile) if profile is not None else (False, None)

    def style_bound(self, scene: SceneCard) -> bool:
        return self.style_policy(scene).bound

    def scene_calibration(self, scene: SceneCard) -> tuple[bool, CraftCalibration]:
        """这一场的（绑定与否，校准）：有绑定按参考书，没有就是房风默认。"""

        style_bound, profile = self.binding_profile(scene)
        return style_bound, (self.craft_calibration(profile) if style_bound else DEFAULT_CRAFT_CALIBRATION)

    def rule_calibration_for_scene(self, scene: SceneCard) -> RuleCalibration | None:
        """文学质量视图用的解析器（路由层注入 LiteraryQualityService）：有绑定给参考书的规则校准，否则 None。"""

        style_bound, calibration = self.scene_calibration(scene)
        return calibration.rules if style_bound and calibration.rules.active else None

    def rule_calibration_for_policy(self, policy: StylePolicy) -> RuleCalibration | None:
        """成稿门用的解析器（风格参考 v3 V11）：按策略绑定的书校准的 21 维规则；未绑定 / 校准不可用 → None。"""

        profile = self.bound_profile_for_policy(policy)
        if profile is None:
            return None
        rules = self.craft_calibration(profile).rules
        return rules if rules.active else None

    def craft_calibration(self, profile: BoundProfile | None) -> CraftCalibration:
        """按绑定画像的参考书校准节奏检查与 21 维规则；读数按（书、段落数、最新段落时间）缓存在进程里
        （『龙族』26k 段：节奏读数 ≈1.1 s、规则读数 ≈0.6 s，每个进程每本书算一次）。"""

        if profile is None or not profile.book_id:
            return DEFAULT_CRAFT_CALIBRATION
        try:
            count, latest = self.session.execute(
                select(func.count(StyleReferenceParagraph.paragraph_id), func.max(StyleReferenceParagraph.created_at)).where(
                    StyleReferenceParagraph.book_id == profile.book_id
                )
            ).one()
            count = int(count or 0)
            if not count:
                return DEFAULT_CRAFT_CALIBRATION
            key = (str(profile.book_id), count, str(latest or ""))
            stats = _REFERENCE_CRAFT_CACHE.get(key)
            book = self.session.get(StyleReferenceBook, profile.book_id)
            if stats is None or "rules" not in stats or "endings_source" not in (stats.get("rules") or {}):
                rows = self.session.execute(
                    select(StyleReferenceParagraph.text, StyleReferenceParagraph.paragraph_type)
                    .where(StyleReferenceParagraph.book_id == profile.book_id)
                    .order_by(StyleReferenceParagraph.paragraph_index.asc())
                ).all()
                texts = [str(row[0] or "") for row in rows]
                types = [str(row[1] or "") for row in rows]
                # 导入时记下的场界（含空行分界，段落表本身看不出来）："其后有场界" 的段落索引
                book_stats = getattr(book, "stats_json", None) if book is not None else None
                scene_breaks = (book_stats or {}).get("scene_breaks") if isinstance(book_stats, dict) else None
                stats = {
                    **compute_reference_craft(texts),
                    "rules": compute_reference_rules(texts, paragraph_types=types, scene_breaks=scene_breaks if isinstance(scene_breaks, list) else None),
                }
                _REFERENCE_CRAFT_CACHE.clear()
                _REFERENCE_CRAFT_CACHE[key] = stats
            return calibration_from_reference(
                profile_id=profile.profile_id,
                book_id=profile.book_id,
                book_title=getattr(book, "title", None),
                stats=stats,
                deliberate_repetition=bool(profile.deliberate_repetition),
                rule_stats=stats.get("rules"),
            )
        except Exception:  # noqa: BLE001 — 校准失败退回默认阈值，不让诊断失败
            return DEFAULT_CRAFT_CALIBRATION

    # -- 一场的诊断 --------------------------------------------------------

    def diagnose_scene(
        self,
        scene: SceneCard,
        *,
        chapter_row: WriterEvaluation | None = None,
        with_patches: bool = True,
        text: DiagnosisText | None = None,
    ) -> dict[str, Any]:
        text = text if text is not None else self.text_for_scene(scene)
        style_bound, calibration = self.scene_calibration(scene)
        # 规则 / 节奏发现是否标房风：只在「让位」时（绑定且作者手笔直起）——与成稿门同一个判定
        house_taste = self.style_policy(scene).defers_house_taste()
        ignored = {str(key) for key in (scene.deep_review_ignored_keys_json or []) if str(key)}

        findings: list[dict[str, Any]] = []
        waived: list[dict[str, Any]] = []
        if text.layer != "none":
            text_findings, waived = cached_text_findings(scene.scene_id, text, calibration=calibration, house_taste=house_taste)
            findings.extend(text_findings)

        review_row = self.latest_evaluation(scene.scene_id, NEAR_FINAL_RUBRIC_ID)
        ai_row = self.latest_evaluation(scene.scene_id, LITERARY_REVISION_RUBRIC_ID)
        passage_rows = self.passage_rows(scene.scene_id)
        if chapter_row is None and scene.chapter_id:
            chapter_row = self.latest_evaluation(scene.chapter_id, LITERARY_REVISION_RUBRIC_ID, object_type="chapter")
        if text.layer != "none":
            if review_row is not None:
                findings.extend(_evaluation_findings(review_row, text, source="review", label_for=REVIEW_DIMENSION_LABELS))
            if ai_row is not None:
                findings.extend(_evaluation_findings(ai_row, text, source="ai", label_for=AI_DIMENSION_LABELS))
            for row in passage_rows:
                findings.extend(_evaluation_findings(row, text, source="ai", label_for=AI_DIMENSION_LABELS, origin_kind="passage"))
            if chapter_row is not None:
                # 章级通读的发现只有钉得到这一场的才属于这一场；钉不到的留在成稿中心的章级清单里
                findings.extend(
                    item
                    for item in _evaluation_findings(chapter_row, text, source="ai", label_for=AI_DIMENSION_LABELS, origin_kind="chapter")
                    if item.get("evidence") is not None
                )

        opinions: dict[str, dict[str, Any]] = {}
        passage_reviews: list[dict[str, Any]] = []
        for row in passage_rows:
            entry = serialize_passage_review(row, self._evaluation_status(row, text))
            passage_reviews.append(entry)
            for about_id in entry["about_signal_ids"]:
                opinions[str(about_id)] = entry

        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for finding in findings:
            if finding["signal_id"] in seen:
                continue
            seen.add(finding["signal_id"])
            finding["ignored"] = finding["signal_id"] in ignored
            finding["opinion"] = opinions.get(finding["signal_id"])
            deduped.append(finding)
        deduped.sort(key=_finding_sort_key)

        ai_lenses = self.lens_rows(ai_row.evaluation_id) if ai_row is not None else []
        latest_evaluation = serialize_evaluation(ai_row)
        patch_rows = (
            self.session.execute(
                select(PassagePatchCandidate)
                .where(PassagePatchCandidate.object_type == "scene", PassagePatchCandidate.object_id == scene.scene_id)
                .order_by(PassagePatchCandidate.created_at.desc(), PassagePatchCandidate.patch_id.desc())
                .limit(PATCH_CANDIDATE_LIMIT)
            ).scalars().all()
            if with_patches
            else []
        )
        chapter_status = self._scene_view_chapter_status(chapter_row, scene, text)

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
            "house_taste_deferred": house_taste,
            # 这一稿里按参考作者的密度放过的词表词（词、次数、作者每万字次数、一场的量里的期望、这个次数的概率）
            "craft_calibration": {**calibration.as_dict(), "waived_in_scene": waived},
            "findings": deduped,
            "summary": _finding_counts(deduped),
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
            "passage_reviews": passage_reviews,
            "chapter_review": {
                "status": chapter_status,
                "evaluation_id": chapter_row.evaluation_id if chapter_row is not None else None,
                "created_at": chapter_row.created_at if chapter_row is not None else None,
                "findings_here": sum(1 for item in deduped if (item.get("origin") or {}).get("kind") == "chapter"),
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

    def payload(self, scene_id: str, *, with_rollup: bool = True) -> dict[str, Any]:
        scene = require_scene(self.session, scene_id, trashed_as_conflict=True)
        payload = self.diagnose_scene(scene)
        if with_rollup:
            payload["diagnosis_rollup"] = self.scene_rollup(scene)
        return payload

    # -- 一章的诊断（成稿中心「AI 通读本章」）-------------------------------

    def _chapter_block(self, chapter: ChapterGoal) -> dict[str, Any]:
        """一章的全部读数（一次算完，chapter_payload / project_summary / rollup 共用）：每场的诊断、落到各场的
        通读发现、钉不到任何一场的章级发现、章级通读的新旧与改过的场。"""

        scenes = self.chapter_scenes(chapter.chapter_id)
        chapter_row = self.latest_evaluation(chapter.chapter_id, LITERARY_REVISION_RUBRIC_ID, object_type="chapter")
        scene_entries: list[dict[str, Any]] = []
        texts: list[DiagnosisText] = []
        located_ids: set[str] = set()
        for scene in scenes:
            text = self.text_for_scene(scene)
            texts.append(text)
            diagnosis = self.diagnose_scene(scene, chapter_row=chapter_row, with_patches=False, text=text)
            from_chapter = [item for item in diagnosis["findings"] if (item.get("origin") or {}).get("kind") == "chapter"]
            located_ids.update(item["signal_id"] for item in from_chapter)
            scene_entries.append(
                {
                    "scene_id": scene.scene_id,
                    "scene_seq": scene.scene_seq,
                    "title": getattr(scene, "title", None) or "",
                    "text_layer": diagnosis["text"]["layer"],
                    "summary": diagnosis["summary"],
                    "ai_status": diagnosis["ai"]["status"],
                    "review_status": diagnosis["review"]["status"],
                    "findings_from_chapter": from_chapter,
                    "carried": any((item.get("origin") or {}).get("carried_from") for item in from_chapter),
                    "counts": _scene_counts_entry(chapter.chapter_id, diagnosis),
                }
            )

        chapter_findings: list[dict[str, Any]] = []
        if chapter_row is not None:
            empty = DiagnosisText(layer="chapter", ref=chapter_row.source_text_ref, content="", paragraphs=[])
            for item in _evaluation_findings(chapter_row, empty, source="ai", label_for=AI_DIMENSION_LABELS, origin_kind="chapter"):
                if item["signal_id"] in located_ids:
                    continue
                # 钉不到任何一场：章级判断（承诺 / 升级 / 兑现），或引的那句已经改掉
                item["stale"] = bool(item.get("context"))
                chapter_findings.append(item)
            chapter_findings.sort(key=_finding_sort_key)

        scene_ids = [scene.scene_id for scene in scenes]
        changes = self.chapter_review_changes(chapter_row, scene_ids, texts) if chapter_row is not None else None
        ai_status = self._chapter_evaluation_status(chapter_row, texts, scene_ids) if chapter_row is not None else "not_run"
        changed_ids = list(changes["changed_scene_ids"]) if changes else (
            [scene.scene_id for scene, text in zip(scenes, texts) if text.layer != "none"] if ai_status == "stale" else []
        )
        for entry in scene_entries:
            entry["changed_since_review"] = entry["scene_id"] in set(changed_ids)
        meta = chapter_row.contract_field_refs_json if chapter_row is not None and isinstance(chapter_row.contract_field_refs_json, dict) else {}
        chapter_level_blocking = sum(1 for item in chapter_findings if item["severity"] == "blocking")
        counts = {
            "open": sum(entry["summary"]["open"] for entry in scene_entries) + len(chapter_findings),
            "blocking": sum(entry["summary"]["by_severity"]["blocking"] for entry in scene_entries) + chapter_level_blocking,
            "chapter_level": len(chapter_findings),
            "chapter_level_blocking": chapter_level_blocking,
            "scenes": len(scene_entries),
            "scenes_with_findings": sum(1 for entry in scene_entries if entry["summary"]["open"]),
            "ai_status": ai_status,
        }
        return {
            "chapter": chapter,
            "row": chapter_row,
            "scenes": scenes,
            "texts": texts,
            "scene_entries": scene_entries,
            "chapter_findings": chapter_findings,
            "counts": counts,
            "ai": {
                "status": ai_status,
                "evaluation_id": chapter_row.evaluation_id if chapter_row is not None else None,
                "overall_score": chapter_row.overall_score if chapter_row is not None else None,
                "revision_brief": list(chapter_row.revision_brief_json or []) if chapter_row is not None else [],
                "llm_call_id": chapter_row.evaluator_llm_call_id if chapter_row is not None else None,
                "created_at": chapter_row.created_at if chapter_row is not None else None,
                "source_text_ref": chapter_row.source_text_ref if chapter_row is not None else None,
                # 2026-09-22 第三轮：这一轮通读看了哪些场（scope all / changed）、哪些场的发现是沿用上一轮的，
                # 以及通读之后又改过字的场——成稿中心据此给「只通读改过的 N 场」
                "scope": str(meta.get("scope") or ("all" if chapter_row is not None else "")),
                "reviewed_scene_ids": [str(value) for value in (meta.get("reviewed_scene_ids") or [])],
                "carried_scene_ids": [str(value) for value in (meta.get("carried_scene_ids") or [])],
                "carried_from": meta.get("carried_from"),
                "changed_scene_ids": changed_ids,
                "changed_count": len(changed_ids),
                "incremental_available": bool(changes is not None and changed_ids and changes["unchanged_scene_ids"]),
            },
        }

    def chapter_payload(self, chapter_id: str) -> dict[str, Any]:
        chapter = require_chapter(self.session, chapter_id)
        block = self._chapter_block(chapter)
        chapter_row = block["row"]
        chapter_findings = block["chapter_findings"]
        scene_entries = block["scene_entries"]
        latest_evaluation = serialize_evaluation(chapter_row)
        lens_rows = self.lens_rows(chapter_row.evaluation_id) if chapter_row is not None else []
        patch_rows = self.session.execute(
            select(PassagePatchCandidate)
            .where(PassagePatchCandidate.object_type == "chapter", PassagePatchCandidate.object_id == chapter.chapter_id)
            .order_by(PassagePatchCandidate.created_at.desc(), PassagePatchCandidate.patch_id.desc())
            .limit(PATCH_CANDIDATE_LIMIT)
        ).scalars().all()
        return {
            "chapter_id": chapter.chapter_id,
            "project_id": chapter.project_id,
            "ai": block["ai"],
            "chapter_findings": chapter_findings,
            "scenes": [{key: value for key, value in entry.items() if key != "counts"} for entry in scene_entries],
            "summary": {
                "open": block["counts"]["open"],
                "chapter_level": len(chapter_findings),
                "scenes": len(scene_entries),
                "scenes_with_findings": block["counts"]["scenes_with_findings"],
                "blocking": block["counts"]["blocking"],
            },
            "diagnosis_rollup": _rollup_from_block(block),
            "patch_candidates": [serialize_patch_candidate(row) for row in patch_rows],
            # 旧契约的键
            "status": "reviewed" if chapter_row is not None else "not_run",
            "object_type": "chapter",
            "object_id": chapter.chapter_id,
            "rubric_id": LITERARY_REVISION_RUBRIC_ID,
            "latest_evaluation": latest_evaluation,
            "latest_score": latest_evaluation["overall_score"] if latest_evaluation else None,
            "requires_human_review": bool(latest_evaluation["requires_human_review"]) if latest_evaluation else False,
            "lens_evaluations": [item for item in (serialize_evaluation(row) for row in lens_rows) if item],
        }

    # -- 一本书的计数（主页 / 成稿中心 / 起草台的角标）-----------------------

    def project_summary(self, project_id: str) -> dict[str, Any]:
        """整本书的计数——视图挂载 / 换作品时读一次；之后的变化由 ``scene_rollup`` / ``chapter_rollup``
        随写回传（同一种 ``scenes`` / ``chapters`` 条目形状，前端本地汇总 ``totals``）。"""

        project = self.session.get(StoryProject, project_id)
        if project is None:
            raise DomainError("PROJECT_NOT_FOUND", "project not found", status_code=404)
        chapters = list(
            self.session.execute(
                select(ChapterGoal)
                .where(ChapterGoal.project_id == project_id, ChapterGoal.trashed_flag == 0)
                .order_by(ChapterGoal.display_order.asc(), ChapterGoal.chapter_id.asc())
            ).scalars().all()
        )
        scenes_out: dict[str, dict[str, Any]] = {}
        chapters_out: dict[str, dict[str, Any]] = {}
        for chapter in chapters:
            block = self._chapter_block(chapter)
            for entry in block["scene_entries"]:
                scenes_out[entry["scene_id"]] = entry["counts"]
            chapters_out[chapter.chapter_id] = dict(block["counts"])
        return {"project_id": project_id, "totals": summarize_counts(scenes_out, chapters_out), "chapters": chapters_out, "scenes": scenes_out}

    # -- 随写回传的计数 --------------------------------------------------------

    def scene_rollup(self, scene: SceneCard) -> dict[str, Any]:
        """这一场所在那一章的计数（章条目 + 章里每一场的条目）：作者稿保存 / 深评动作 / 忽略之后随响应回传，
        主页与成稿中心的角标据此更新，不必再拉整本书。没有章的场只回这一场。"""

        chapter = self.session.get(ChapterGoal, scene.chapter_id) if scene.chapter_id else None
        if chapter is None:
            diagnosis = self.diagnose_scene(scene, with_patches=False)
            return {
                "project_id": getattr(scene, "project_id", None),
                "chapter_id": scene.chapter_id,
                "chapters": {},
                "scenes": {scene.scene_id: _scene_counts_entry(scene.chapter_id, diagnosis)},
            }
        return _rollup_from_block(self._chapter_block(chapter))

    def chapter_rollup(self, chapter_id: str) -> dict[str, Any]:
        chapter = require_chapter(self.session, chapter_id)
        return _rollup_from_block(self._chapter_block(chapter))


def _scene_counts_entry(chapter_id: str | None, diagnosis: dict[str, Any]) -> dict[str, Any]:
    summary = diagnosis["summary"]
    return {
        "chapter_id": chapter_id,
        "text_layer": diagnosis["text"]["layer"],
        "open": summary["open"],
        "blocking": summary["by_severity"]["blocking"],
        "revision": summary["by_severity"]["revision"],
        "taste": summary["by_severity"]["taste"],
        "info": summary["by_severity"]["info"],
        "ignored": summary["ignored"],
        "stale": summary["stale"],
        "ai_status": diagnosis["ai"]["status"],
        "review_status": diagnosis["review"]["status"],
    }


def _rollup_from_block(block: dict[str, Any]) -> dict[str, Any]:
    chapter = block["chapter"]
    return {
        "project_id": chapter.project_id,
        "chapter_id": chapter.chapter_id,
        "chapters": {chapter.chapter_id: dict(block["counts"])},
        "scenes": {entry["scene_id"]: entry["counts"] for entry in block["scene_entries"]},
    }


def summarize_counts(scenes: dict[str, dict[str, Any]], chapters: dict[str, dict[str, Any]]) -> dict[str, int]:
    """``totals`` 从场 / 章条目汇总（前端的 store 用同一条规则本地汇总，随写回传的 rollup 不必带 totals）。"""

    totals = {
        "open": 0,
        "blocking": 0,
        "revision": 0,
        "taste": 0,
        "info": 0,
        "ignored": 0,
        "stale": 0,
        "scenes": 0,
        "scenes_with_text": 0,
        "scenes_with_findings": 0,
        "ai_reviewed_scenes": 0,
        "chapters_reviewed": 0,
    }
    for entry in scenes.values():
        totals["scenes"] += 1
        if entry.get("text_layer") not in (None, "none"):
            totals["scenes_with_text"] += 1
        for key in ("open", "blocking", "revision", "taste", "info", "ignored", "stale"):
            totals[key] += int(entry.get(key) or 0)
        if int(entry.get("open") or 0):
            totals["scenes_with_findings"] += 1
        if entry.get("ai_status") and entry.get("ai_status") != "not_run":
            totals["ai_reviewed_scenes"] += 1
    for counts in chapters.values():
        if counts.get("ai_status") and counts.get("ai_status") != "not_run":
            totals["chapters_reviewed"] += 1
        # 钉不到任何一场的章级发现（承诺 / 升级 / 兑现）也算开着的
        totals["open"] += int(counts.get("chapter_level") or 0)
        totals["blocking"] += int(counts.get("chapter_level_blocking") or 0)
    return totals
