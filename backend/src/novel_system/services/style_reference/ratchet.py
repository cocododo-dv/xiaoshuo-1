"""风格参考 · 配对棘轮（2026-10-18）——改风格配置之前，用参考书里**留出**的原文验收「是不是更像了」。

思路借自 QMAI 的文风自进化（只借思路）。实库 2026-10-18 的读数：首稿之后的风格阶段在 5 场里有 3 场让测量核距离
变大，只有一次按维修改明显变近；分位 / 软 QC / 对照检查三把尺子又互相不一致——改模板、换模型、减样例之前，没有
一个可复现的验收办法。棘轮给的是**有标准答案**的比较：拿作者自己写过、模型没见过的一段，只把「发生什么」交给
模型，看两套配置谁写得离原文更近。内容固定，差别只剩写法。

1. **留出段**（:func:`pick_passages`）：这本书的窗口里挑 N 段（缺省 3）——够长、典型、一章至多一段
   （``judge_calibration.choose_anchor_windows``，种子不同于校准）。渲染样例时整窗跳过这几段
   （``StyleRenderRequest.exclude_windows``）；模型看得到的其余东西（文风卡引文、声音特征、别的样例窗）若覆盖了
   留出段一成以上的字（抄袭门同一把 12 字尺子，``reference_copy_gate.shared_runs``），这一段换掉。
2. **情节要点**（:func:`extract_plot`，模板 ``style_ratchet_plot_points``，``style_draft`` 路由）：只说发生什么、
   不带原文措辞、专名全换新；与原文有一处连续 ≥12 字相同就带着原因重试一次，仍不行这一段换掉。
3. **两套配置各写一遍**（:func:`draft_passage`）：真实的 ``style_first_draft`` 模板（风格先行的首稿）+ 真实的样例
   渲染，场景设计换成要点，字数给成原段的字数。配置（:class:`ArmSpec`）可以改模型、样例窗数、样例位置
   （user 末尾 / system 前缀）、提示词文件；同一套配置写过的稿子按配置签名缓存，换对手时不重写。
4. **量**（:func:`score_draft`）：每份稿子对**原段**的距离——测量核各特征之差除以作者窗口分布的尺度，按「像不像」
   读数同一套维权重平均（|z| 封顶 ``Z_CLIP``）；另附对作者全书分布的读数（百分位，与起草台同一口径）作参照。
5. **判**（:func:`decide`）：B 在每一段都更近、且平均近了 ε 以上 → B 更像；反过来 → A 更像；其余 = 分不出。
   ε 取给定值与 **A/A 实测噪声**（同一配置写两遍，每段之差的平均；记在 ``<out_dir>/noise.json``）中较大的那个——
   实库第一次 A/A（flash，2 段）每段差 0.18–0.24，噪声比直觉大得多，不量噪声就判胜负会把运气当成改进。
   确定性指标只量表面统计（句长、对白占比、虚词……），所以另出一张盲读页（原文 / A / B 打乱成甲乙丙，答案在
   ``key.json``），像不像最后由作者读了算。

棘轮不进任何管线、不改任何配置：结果是套件目录（缺省 ``.codex-run/style-ratchet/<套件>``）下的文件；模型调用照常
记账（``scope_type = style_reference_ratchet``）。入口是运维工具 ``python -m novel_system.tools.style_ratchet``。
"""

from __future__ import annotations

import dataclasses
import json
import random
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    LlmCall,
    StyleReferenceBook,
    StyleReferenceInjectionBinding,
    StyleReferenceProfile,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import sha256_json_normalized, sha256_text
from novel_system.services.llm_accounting import LLMCallContext, execute_accounted_call
from novel_system.services.llm_client import build_llm_request, load_model_routing_config, resolve_node_route
from novel_system.services.prompt_builder import PromptBuilder, load_prompt_templates
from novel_system.services.reference_copy_gate import shared_runs
from novel_system.services.style_policy import StylePolicy, policy_from_contract
from novel_system.services.style_reference.binding_config import normalize_binding_config
from novel_system.services.style_reference.fidelity import (
    Z_CLIP,
    ReferenceDistribution,
    feature_weights,
    read_fidelity,
    reference_distribution_for_book,
)
from novel_system.services.style_reference.inject.fit import fit_rendered
from novel_system.services.style_reference.inject.preview import preview_contract
from novel_system.services.style_reference.inject.render import render_style
from novel_system.services.style_reference.inject.request import (
    PLACEMENT_SYSTEM,
    PLACEMENT_USER_TAIL,
    ROLE_DRAFT,
    SCENE_POSITIONS,
    StyleRenderRequest,
)
from novel_system.services.style_reference.judge_calibration import (
    ANCHOR_MIN_CHARS,
    choose_anchor_windows,
    trim_passage,
)
from novel_system.services.style_reference.llm_nodes import accounted_call
from novel_system.services.style_reference.measure import kernel_features, measure_text, visible_length
from novel_system.services.style_reference.policy import ensure_cloud_llm_allowed
from novel_system.services.style_reference.untrusted_data import UNTRUSTED_SYSTEM_INSTRUCTION, secure_reference_block
from novel_system.services.style_reference.windows import load_windows, window_texts

RATCHET_VERSION = "style_ratchet_v1"
PLOT_TEMPLATE = "style_ratchet_plot_points"
DRAFT_TEMPLATE = "style_first_draft"
# 首稿与要点提炼都走风格稿的路由（风格先行的首稿本来就在这个节点下派发）
DRAFT_NODE_ID = "style_draft"
SCOPE_TYPE = "style_reference_ratchet"
OPERATION = "style_ratchet"
POLICY_MODE = "ratchet"
STEP_PLOT = "style_ratchet_plot_points"
STEP_DRAFT = "style_ratchet_draft"

DEFAULT_PASSAGES = 4
MIN_PASSAGES = 2
MAX_PASSAGES = 8
# 平均要近多少才算赢。实库 2026-10-18 的 A/A（flash、12 窗，同一配置写两遍）每段差 0.18–0.24：0.05 会把噪声判成胜负。
# 跑过 A/A 之后按实测噪声取（:func:`effective_epsilon`），缺省值只在还没量过噪声时用。
DEFAULT_EPSILON = 0.2
# 候选池 = 段数 × 这个数（够长、典型、一章至多一段的前几名里再按种子挑）
CANDIDATE_FACTOR = 4
# 模型看得到的东西（文风卡引文、声音特征、别的样例窗）覆盖留出段超过这个比例的字 → 这一段不能当留出段
PASSAGE_LEAK_MAX_SHARE = 0.10
# 字数要求：原段可见字数的 ±15%
LENGTH_SLACK = 0.15
# 稿子字数偏离原段超过这个比例 → 报告里标出来（字数差太多时距离里混着篇幅的差）
LENGTH_FLAG_SHARE = 0.30
NOISE_FILE = "noise.json"
MIN_BEATS = 3
SAMPLE_WINDOWS_MAX = 16
PLACEMENT_ALIASES = {"tail": PLACEMENT_USER_TAIL, "user_tail": PLACEMENT_USER_TAIL, "user": PLACEMENT_USER_TAIL, "system": PLACEMENT_SYSTEM, "prefix": PLACEMENT_SYSTEM}
PLACEMENT_LABELS = {PLACEMENT_USER_TAIL: "样例在 user 末尾", PLACEMENT_SYSTEM: "样例在 system 前缀"}
READING_LABELS = ("甲", "乙", "丙")


class RatchetError(Exception):
    """棘轮跑不下去（中文原因给操作者看）。``refused`` = 还没调用任何模型就拒跑。"""

    def __init__(self, message: str, *, refused: bool = True) -> None:
        super().__init__(message)
        self.refused = refused


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmSpec:
    """一套要比的配置：不写的项 = 现在的配置（绑定的样例窗数、``style_draft`` 路由的模型、样例在 user 末尾、
    仓库的提示词文件）。``rep`` 是同一配置的第几份（A/A 比较量噪声用：同一配置写两遍）。"""

    label: str
    model: str | None = None
    sample_windows: int | None = None
    placement: str | None = None
    prompts: str | None = None
    rep: int = 1


ARM_KEYS = ("model", "windows", "placement", "prompts", "rep")


def parse_arm(label: str, text: str | None) -> ArmSpec:
    """``"model=gemini-x,windows=8,placement=system,prompts=/path/prompts.yaml,rep=2"`` → :class:`ArmSpec`；空 = 现在的配置。"""
    values: dict[str, Any] = {}
    for part in str(text or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, value = part.partition("=")
        key, value = key.strip().lower(), value.strip()
        if not sep or key not in ARM_KEYS or not value:
            raise RatchetError(f"配置 {label} 写法不对：「{part}」。可用的项：{', '.join(ARM_KEYS)}（例：model=xxx,windows=8）。")
        values[key] = value
    windows: int | None = None
    if "windows" in values:
        try:
            windows = int(values["windows"])
        except ValueError as exc:
            raise RatchetError(f"配置 {label} 的 windows 要是 0–{SAMPLE_WINDOWS_MAX} 的整数。") from exc
        if not 0 <= windows <= SAMPLE_WINDOWS_MAX:
            raise RatchetError(f"配置 {label} 的 windows 要是 0–{SAMPLE_WINDOWS_MAX} 的整数。")
    placement: str | None = None
    if "placement" in values:
        placement = PLACEMENT_ALIASES.get(values["placement"].lower())
        if placement is None:
            raise RatchetError(f"配置 {label} 的 placement 只能是 user_tail 或 system。")
    rep = 1
    if "rep" in values:
        try:
            rep = int(values["rep"])
        except ValueError as exc:
            raise RatchetError(f"配置 {label} 的 rep 要是正整数。") from exc
        if rep < 1:
            raise RatchetError(f"配置 {label} 的 rep 要是正整数。")
    return ArmSpec(
        label=label,
        model=values.get("model"),
        sample_windows=windows,
        placement=placement,
        prompts=values.get("prompts"),
        rep=rep,
    )


@dataclass(frozen=True)
class Subject:
    """比的是哪一份文风：画像、书、策略（绑定的配置或画像的缺省配置）。"""

    profile: StyleReferenceProfile
    book: StyleReferenceBook
    policy: StylePolicy
    config: Mapping[str, Any]
    binding_id: str | None
    dist: ReferenceDistribution
    book_root: str


@dataclass(frozen=True)
class ResolvedArm:
    spec: ArmSpec
    route: Any
    model: str
    policy: StylePolicy
    placement: str
    builder: PromptBuilder
    template_version: str
    signature: Mapping[str, Any]
    key: str

    @property
    def label(self) -> str:
        return self.spec.label

    def describe(self) -> str:
        parts = [
            f"模型 {self.model or '（路由缺省）'}",
            f"样例 {self.policy.sample_windows} 窗",
            PLACEMENT_LABELS.get(self.placement, self.placement),
            f"{DRAFT_TEMPLATE} {self.template_version}" + ("（另一份提示词文件）" if self.spec.prompts else ""),
        ]
        if self.spec.rep > 1:
            parts.append(f"第 {self.spec.rep} 份")
        return " · ".join(parts)


def resolve_subject(session: Session, profile_id: str | None = None) -> Subject:
    """没给画像：用唯一一份活动绑定（它的样例窗数、维状态）；给了画像：有它的活动绑定就用绑定的配置，否则用缺省配置。"""
    bindings = list(
        session.scalars(select(StyleReferenceInjectionBinding).where(StyleReferenceInjectionBinding.status == "active"))
    )
    binding: StyleReferenceInjectionBinding | None = None
    if profile_id:
        binding = next((row for row in bindings if row.profile_id == profile_id), None)
    else:
        profiles = sorted({row.profile_id for row in bindings})
        if not profiles:
            raise RatchetError("没有活动的风格绑定：用 --profile 指定要比的画像。")
        if len(profiles) > 1:
            raise RatchetError(f"有几份活动绑定（画像 {', '.join(profiles)}）：用 --profile 指定一份。")
        binding = next(row for row in bindings if row.profile_id == profiles[0])
        profile_id = profiles[0]
    profile = session.get(StyleReferenceProfile, str(profile_id))
    if profile is None:
        raise RatchetError(f"画像 {profile_id} 不存在。")
    book = session.get(StyleReferenceBook, str(profile.book_id))
    if book is None or str(book.status or "") != "ready":
        raise RatchetError("这份画像的书不在或还没分类完：等书就绪再比。")
    config = normalize_binding_config(binding.config_json if binding is not None else {})
    contract = preview_contract(session, profile, config)
    policy = policy_from_contract(contract, mode=POLICY_MODE)
    if not policy.bound:
        raise RatchetError("这份画像造不出可用的风格契约。")
    dist = reference_distribution_for_book(session, str(book.book_id))
    if dist is None or not dist.reliable:
        raise RatchetError("这本书的样例窗口太少，量不出「像不像」。")
    windows = load_windows(session, str(book.book_id))
    root = str(windows[0].root_sha256) if windows else ""
    return Subject(
        profile=profile,
        book=book,
        policy=policy,
        config=config,
        binding_id=binding.binding_id if binding is not None else None,
        dist=dist,
        book_root=root,
    )


def _template_sha(template: Any) -> str:
    return sha256_json_normalized(
        {
            "system": str(getattr(template, "system_prompt", "") or ""),
            "task": str(getattr(template, "task_prompt", "") or ""),
            "schema": getattr(template, "structured_schema", None),
        }
    )[:16]


def resolve_arm(subject: Subject, spec: ArmSpec) -> ResolvedArm:
    """配置 → 路由、策略、提示词与签名（签名决定稿子缓存的键：任一项变了就重写）。"""
    try:
        route = resolve_node_route(load_model_routing_config(), DRAFT_NODE_ID)
    except KeyError as exc:
        raise RatchetError("「风格稿」（style_draft）节点没有模型路由：先在系统配置里补齐。") from exc
    if spec.prompts:
        path = Path(spec.prompts).expanduser()
        if not path.is_file():
            raise RatchetError(f"配置 {spec.label} 的提示词文件不存在：{path}")
        builder = PromptBuilder(path)
        templates = load_prompt_templates(path)
    else:
        builder = PromptBuilder()
        templates = load_prompt_templates()
    template = templates.get(DRAFT_TEMPLATE)
    if template is None:
        raise RatchetError(f"配置 {spec.label} 的提示词文件里没有 {DRAFT_TEMPLATE} 模板。")
    windows = spec.sample_windows if spec.sample_windows is not None else int(subject.policy.sample_windows)
    policy = dataclasses.replace(subject.policy, sample_windows=windows)
    placement = spec.placement or PLACEMENT_USER_TAIL
    model = spec.model or str(getattr(route, "model", "") or "")
    signature = {
        "ratchet": RATCHET_VERSION,
        "profile_id": str(subject.profile.profile_id),
        "profile_version": str(subject.profile.version_tag or ""),
        "book_root": subject.book_root,
        "reference_mode": policy.reference_mode,
        "dimension_states": dict(sorted((policy.dimension_states or {}).items())),
        "sample_windows": windows,
        "placement": placement,
        "provider": str(getattr(route, "provider_id", None) or getattr(route, "provider", None) or ""),
        "model": model,
        "temperature": getattr(route, "temperature", None),
        "reasoning_level": getattr(route, "reasoning_level", None),
        "max_output_tokens": getattr(route, "max_output_tokens", None),
        "template_version": str(getattr(template, "version", "") or ""),
        "template_sha": _template_sha(template),
        "rep": int(spec.rep),
    }
    return ResolvedArm(
        spec=spec,
        route=route,
        model=model,
        policy=policy,
        placement=placement,
        builder=builder,
        template_version=str(getattr(template, "version", "") or ""),
        signature=signature,
        key=sha256_json_normalized(signature)[:12],
    )


# ---------------------------------------------------------------------------
# 留出段
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Passage:
    window_no: int
    chapter_no: int | None
    position: str
    text: str

    @property
    def chars(self) -> int:
        """可见字数（汉字 / 字母 / 数字）——字数要求与报告都按它。"""
        return visible_length(self.text)

    @property
    def sha(self) -> str:
        return sha256_text(self.text)[:16]

    def meta(self) -> dict[str, Any]:
        return {
            "window_no": self.window_no,
            "chapter_no": self.chapter_no,
            "position": self.position,
            "chars": self.chars,
            "sha": self.sha,
        }


def _render_request(passage: Passage | None, *, placement: str, exclude: Iterable[int]) -> StyleRenderRequest:
    position = passage.position if passage is not None and passage.position in SCENE_POSITIONS else None
    return StyleRenderRequest(
        role=ROLE_DRAFT,
        placement=placement,
        position=position,
        node_ids=(DRAFT_NODE_ID,),
        exclude_windows=tuple(exclude),
    )


def leaked_share(passage_text: str, seen: str) -> float:
    """留出段有多大比例的字（规范化后的下标计）出现在模型看得到的文字里（连续 ≥12 字才算）。"""
    text = str(passage_text or "")
    if not text or not seen:
        return 0.0
    covered = sum(hit.end - hit.start for hit in shared_runs(text, [seen], label="seen"))
    return covered / max(1, len(text))


def pick_passages(
    session: Session,
    subject: Subject,
    arm: ResolvedArm,
    *,
    count: int,
    seed: str,
) -> list[Passage]:
    """挑留出段（见模块说明 1）。渲染只读、不冻结选窗。"""
    windows = load_windows(session, str(subject.book.book_id))
    pool = choose_anchor_windows(
        windows, exclude=set(), count=max(count * CANDIDATE_FACTOR, count), seed=f"ratchet:{subject.profile.profile_id}:{seed}"
    )
    texts = window_texts(session, pool)
    order = sorted(pool, key=lambda window: sha256_text(f"{seed}:order:{int(window.window_no)}"))
    chosen: list[Passage] = []
    for window in order:
        text = trim_passage(texts.get(int(window.window_no), ""))
        if len(text) < ANCHOR_MIN_CHARS:
            continue
        candidate = Passage(
            window_no=int(window.window_no),
            chapter_no=getattr(window, "chapter_no", None),
            position=str(getattr(window, "position", "") or "middle"),
            text=text,
        )
        exclude = [passage.window_no for passage in chosen] + [candidate.window_no]
        rendered = render_style(
            session,
            arm.policy,
            _render_request(candidate, placement=arm.placement, exclude=exclude),
            scene=None,
            persist_selection=False,
            use_cache=False,
        )
        if leaked_share(candidate.text, rendered.system_prefix + rendered.user_tail) > PASSAGE_LEAK_MAX_SHARE:
            continue
        chosen.append(candidate)
        if len(chosen) >= count:
            break
    if len(chosen) < max(MIN_PASSAGES, min(count, MIN_PASSAGES)):
        raise RatchetError(f"这本书挑不出 {count} 段合适的留出段（够长、不在样例与文风卡里）：减少段数或换个 --seed。")
    return sorted(chosen, key=lambda passage: passage.window_no)


# ---------------------------------------------------------------------------
# 情节要点
# ---------------------------------------------------------------------------


def plot_text(plot: Mapping[str, Any]) -> str:
    """要点里全部文字（查原文措辞用）。"""
    parts: list[str] = [str(plot.get(key) or "") for key in ("narrative_person", "pov", "setting", "ending")]
    parts.extend(str(beat or "") for beat in plot.get("beats") or ())
    for character in plot.get("characters") or ():
        if isinstance(character, Mapping):
            parts.extend((str(character.get("name") or ""), str(character.get("role") or "")))
    return "\n".join(part for part in parts if part)


def normalize_plot(structured: Any) -> dict[str, Any] | None:
    """模型的要点 → 规整的 dict；拍子太少 / 缺视角 → None。"""
    if not isinstance(structured, Mapping):
        return None
    beats = [" ".join(str(beat or "").split()) for beat in structured.get("beats") or () if str(beat or "").strip()]
    characters = [
        {"name": str(item.get("name") or "").strip(), "role": str(item.get("role") or "").strip()}
        for item in structured.get("characters") or ()
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    ]
    plot = {
        "narrative_person": str(structured.get("narrative_person") or "").strip(),
        "pov": str(structured.get("pov") or "").strip(),
        "setting": str(structured.get("setting") or "").strip(),
        "characters": characters,
        "beats": beats,
        "ending": str(structured.get("ending") or "").strip(),
    }
    if len(beats) < MIN_BEATS or not plot["narrative_person"]:
        return None
    return plot


def plot_user_prompt(task_prompt: str, passage: Passage, *, leaked: Sequence[str] = ()) -> str:
    lines = [
        str(task_prompt or "").strip(),
        f"The passage has about {passage.chars} Chinese characters.",
    ]
    if leaked:
        joined = "」「".join(leaked[:5])
        lines.append(
            f"Your previous outline was discarded because it reused the passage's own wording (「{joined}」): "
            "say every beat in your own words this time."
        )
    lines.extend(
        [
            "",
            "## Passage",
            secure_reference_block(passage.text, kind="style_ratchet_passage"),
            "",
            "Required top-level JSON keys: narrative_person, pov, setting, characters, beats, ending.",
            "Return only valid JSON. Do not wrap it in markdown fences.",
        ]
    )
    return "\n".join(lines)


def _context(scope_id: str, step: str) -> LLMCallContext:
    return LLMCallContext(scope_type=SCOPE_TYPE, scope_id=scope_id, node_id=DRAFT_NODE_ID, step=step)


def _wrap(message: str) -> Callable[[Exception], Exception]:
    return lambda exc: RatchetError(f"{message}（{type(exc).__name__}：{exc}）", refused=False)


def extract_plot(
    llm_client: Any,
    route: Any,
    template: Any,
    passage: Passage,
    *,
    scope_id: str,
    execute: Callable[..., Any] = execute_accounted_call,
) -> tuple[dict[str, Any] | None, list[str]]:
    """一段原文 → (要点, 调用 id)。带了原文措辞（连续 ≥12 字）或不成形 → 带着原因重试一次；两次都不行 → (None, ids)。"""
    call_ids: list[str] = []
    leaked: list[str] = []
    for _attempt in range(2):
        request = build_llm_request(
            route,
            node_id=DRAFT_NODE_ID,
            messages=[
                {"role": "system", "content": str(template.system_prompt or "").rstrip() + "\n\n" + UNTRUSTED_SYSTEM_INSTRUCTION},
                {"role": "user", "content": plot_user_prompt(template.task_prompt, passage, leaked=leaked)},
            ],
            response_schema=getattr(template, "structured_schema", None),
        )
        call_id = f"llm_style_ratchet_plot_{uuid.uuid4().hex}"
        response = accounted_call(
            llm_client,
            request,
            context=_context(scope_id, STEP_PLOT),
            llm_call_id=call_id,
            execute=execute,
            wrap_error=_wrap("提炼情节要点的模型调用失败"),
        )
        call_ids.append(str(getattr(response, "llm_call_id", None) or call_id))
        plot = normalize_plot(getattr(response, "structured_output", None))
        if plot is None:
            leaked = []
            continue
        hits = shared_runs(plot_text(plot), [passage.text])
        if not hits:
            return plot, call_ids
        text = plot_text(plot)
        leaked = [text[hit.start : hit.end] for hit in hits]
    return None, call_ids


# ---------------------------------------------------------------------------
# 起草
# ---------------------------------------------------------------------------


def length_band(passage: Passage) -> tuple[int, int]:
    chars = passage.chars
    return int(chars * (1 - LENGTH_SLACK)), int(chars * (1 + LENGTH_SLACK)) + 1


def draft_snapshot(passage: Passage, plot: Mapping[str, Any]) -> dict[str, Any]:
    """要点 → 起草模板认的两个事实段（场景卡 + 场景结构），与管线的 bundle 同一种渲染位。"""
    low, high = length_band(passage)
    card = [
        f"Location: {plot.get('setting') or '（未说明）'}",
        "Beats: " + "; ".join(str(beat) for beat in plot.get("beats") or ()),
    ]
    if plot.get("ending"):
        card.append(f"Exit change: {plot['ending']}")
    card.append(f"Target length: {low}–{high} 字")
    structure = [
        f"POV character: {plot.get('pov') or '（未说明）'}",
        f"Narrative stance: {plot.get('narrative_person')}",
    ]
    characters = [
        f"{item['name']}（{item['role']}）" if item.get("role") else str(item["name"])
        for item in plot.get("characters") or ()
        if isinstance(item, Mapping) and item.get("name")
    ]
    if characters:
        structure.append("Onstage: " + "；".join(characters))
    structure.append(
        "These beats are one continuous stretch of story: every beat happens, in this order, and nothing is added."
    )
    return {"inline_digests": {"scene_card": "\n".join(card), "scene_structure_brief": "\n".join(structure)}}


def length_instruction(passage: Passage) -> str:
    low, high = length_band(passage)
    return (
        f"\nLength: about {passage.chars} Chinese characters (between {low} and {high}, not counting punctuation); "
        "write the whole stretch, never a synopsis."
    )


@dataclass(frozen=True)
class DraftPrompt:
    messages: list[dict[str, str]]
    schema: Mapping[str, Any] | None
    style_chars: int
    sample_windows: tuple[int, ...]
    leaked: float


def build_draft_prompt(
    session: Session,
    arm: ResolvedArm,
    passage: Passage,
    plot: Mapping[str, Any],
    *,
    exclude: Iterable[int],
) -> DraftPrompt:
    """真实的 ``style_first_draft`` 模板 + 真实的样例渲染（留出段整窗跳过）→ 起草消息。"""
    prompt = arm.builder.build(draft_snapshot(passage, plot), DRAFT_TEMPLATE)
    user_prompt = prompt["user_prompt"] + length_instruction(passage)
    rendered = render_style(
        session,
        arm.policy,
        _render_request(passage, placement=arm.placement, exclude=exclude),
        scene=None,
        persist_selection=False,
        use_cache=False,
    )
    if rendered.empty:
        raise RatchetError("这份画像渲染不出任何风格参考（没有样例也没有文风卡）：先让这本书「学习文风」。")
    target = (prompt.get("token_budget") or {}).get("target_input_tokens")
    if target:
        rendered, _fit = fit_rendered(
            rendered, base_system_prompt=str(prompt["system_prompt"]), user_prompt=user_prompt, target_input_tokens=int(target)
        )
    seen = rendered.system_prefix + rendered.user_tail
    share = leaked_share(passage.text, seen)
    if share > PASSAGE_LEAK_MAX_SHARE:
        raise RatchetError(
            f"配置 {arm.label} 送给模型的样例 / 文风卡里有第 {passage.window_no} 窗的原文（{share:.0%}）：换一组留出段（--seed）。"
        )
    system = rendered.system_prefix + str(prompt["system_prompt"])
    user = user_prompt.rstrip() + rendered.user_tail if rendered.user_tail else user_prompt
    refs = tuple(int(ref["window_no"]) for ref in rendered.window_refs if ref.get("window_no") is not None)
    return DraftPrompt(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        schema=prompt.get("structured_schema"),
        style_chars=len(seen),
        sample_windows=refs,
        leaked=share,
    )


def scene_text_of(response: Any) -> str:
    structured = getattr(response, "structured_output", None)
    if isinstance(structured, Mapping) and str(structured.get("scene_text") or "").strip():
        return str(structured["scene_text"]).strip()
    try:
        parsed = json.loads(str(getattr(response, "text", "") or ""))
    except (TypeError, ValueError):
        return ""
    return str(parsed.get("scene_text") or "").strip() if isinstance(parsed, Mapping) else ""


def draft_passage(
    llm_client: Any,
    arm: ResolvedArm,
    prompt: DraftPrompt,
    *,
    scope_id: str,
    execute: Callable[..., Any] = execute_accounted_call,
) -> tuple[str, list[str]]:
    """一次起草（没写出正文再要一次）→ (正文, 调用 id)。"""
    call_ids: list[str] = []
    for _attempt in range(2):
        request = build_llm_request(arm.route, node_id=DRAFT_NODE_ID, messages=prompt.messages, response_schema=prompt.schema)
        if arm.model and request.model != arm.model:
            request = dataclasses.replace(request, model=arm.model)
        call_id = f"llm_style_ratchet_draft_{uuid.uuid4().hex}"
        response = accounted_call(
            llm_client,
            request,
            context=_context(scope_id, STEP_DRAFT),
            llm_call_id=call_id,
            execute=execute,
            wrap_error=_wrap(f"配置 {arm.label} 的起草调用失败"),
        )
        call_ids.append(str(getattr(response, "llm_call_id", None) or call_id))
        text = scene_text_of(response)
        if text:
            return text, call_ids
    raise RatchetError(f"配置 {arm.label} 两次都没写出正文（scene_text 为空）。", refused=False)


# ---------------------------------------------------------------------------
# 量与判
# ---------------------------------------------------------------------------


def _weights(dist: ReferenceDistribution, dimension_states: Mapping[str, Any] | None) -> dict[str, float]:
    weights = feature_weights(dist.features, dimension_states)
    if sum(weights.values()) <= 0:
        weights = feature_weights(dist.features, None)
    return weights


def feature_distance(
    a: Mapping[str, Any],
    b: Mapping[str, Any],
    dist: ReferenceDistribution,
    dimension_states: Mapping[str, Any] | None = None,
) -> float:
    """两段文字的测量核特征距离：各特征之差 ÷ 作者窗口分布的尺度（|z| 封顶 ``Z_CLIP``），按读数同一套维权重平均。"""
    weights = _weights(dist, dimension_states)
    total = sum(weights.values())
    distance = 0.0
    for name in dist.features:
        delta = abs(float(a.get(name, 0.0) or 0.0) - float(b.get(name, 0.0) or 0.0)) / float(dist.scale[name])
        distance += weights[name] * min(Z_CLIP, delta)
    return distance / total if total > 0 else 0.0


@dataclass(frozen=True)
class DraftScore:
    to_original: float
    distance: float
    percentile: float
    chars: int

    def to_json(self) -> dict[str, Any]:
        return {
            "to_original": round(self.to_original, 4),
            "distance": round(self.distance, 4),
            "percentile": round(self.percentile, 1),
            "chars": self.chars,
        }


def text_features(text: str) -> dict[str, float]:
    return kernel_features(measure_text(text))


def score_draft(text: str, original: Mapping[str, Any], subject: Subject) -> DraftScore:
    states = subject.policy.dimension_states
    reading = read_fidelity(text, subject.dist, dimension_states=states)
    return DraftScore(
        to_original=feature_distance(text_features(text), original, subject.dist, states),
        distance=float(reading.distance),
        percentile=float(reading.percentile),
        chars=visible_length(text),
    )


@dataclass(frozen=True)
class Verdict:
    winner: str | None
    wins_a: int
    wins_b: int
    mean_gain: float
    epsilon: float
    passages: int

    def label(self, a: str = "A", b: str = "B") -> str:
        if self.winner == "B":
            return f"{b} 更像（每段都更近，平均近 {self.mean_gain:.3f}）"
        if self.winner == "A":
            return f"{a} 更像（每段都更近，平均近 {-self.mean_gain:.3f}）"
        return (
            f"分不出（{b} 更近 {self.wins_b} 段、{a} 更近 {self.wins_a} 段，平均差 {abs(self.mean_gain):.3f}，"
            f"ε = {self.epsilon:.3f}）"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "winner": self.winner,
            "wins_a": self.wins_a,
            "wins_b": self.wins_b,
            "mean_gain": round(self.mean_gain, 4),
            "epsilon": self.epsilon,
            "passages": self.passages,
        }


def decide(pairs: Sequence[tuple[float, float]], *, epsilon: float = DEFAULT_EPSILON) -> Verdict:
    """``pairs`` = 每段的 (A 距原文, B 距原文)。B 每段都更近且平均近 ε 以上 → B；反过来 → A；其余分不出。"""
    count = len(pairs)
    wins_b = sum(1 for a, b in pairs if b < a)
    wins_a = sum(1 for a, b in pairs if a < b)
    gain = sum(a - b for a, b in pairs) / count if count else 0.0
    winner: str | None = None
    if count and wins_b == count and gain > epsilon:
        winner = "B"
    elif count and wins_a == count and -gain > epsilon:
        winner = "A"
    return Verdict(winner=winner, wins_a=wins_a, wins_b=wins_b, mean_gain=gain, epsilon=float(epsilon), passages=count)


# ---------------------------------------------------------------------------
# 噪声（A/A）
# ---------------------------------------------------------------------------


def base_key(arm: ResolvedArm) -> str:
    """去掉「第几份」的配置签名：同一配置的几份共用一条噪声记录。"""
    return sha256_json_normalized({key: value for key, value in arm.signature.items() if key != "rep"})[:12]


def record_noise(path: Path, arm: ResolvedArm, *, run_id: str, diffs: Sequence[float]) -> None:
    """A/A 一次：记下这个配置两份之间每段的差（``<out_dir>/noise.json``，按 :func:`base_key`）。"""
    noise = _read_json(path)
    noise = dict(noise) if isinstance(noise, Mapping) else {}
    entry = dict(noise.get(base_key(arm)) or {"describe": arm.describe(), "diffs": [], "runs": []})
    entry["diffs"] = [*entry.get("diffs", []), *(round(float(diff), 4) for diff in diffs)]
    entry["runs"] = [*entry.get("runs", []), run_id]
    noise[base_key(arm)] = entry
    _write_json(path, noise)


def effective_epsilon(noise: Mapping[str, Any], arms: Sequence[ResolvedArm], given: float) -> tuple[float, str]:
    """(这一次用的 ε, 来源)：两边配置量过 A/A 就取其中较大的平均差；都没量过但别的配置量过 → 用全部记录的平均差；
    什么都没量过 → 给定值。最后与给定值取大（验收闸宁严勿宽）。"""

    def mean(values: Sequence[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    own = [mean(noise[key].get("diffs") or []) for key in {base_key(arm) for arm in arms} if key in noise]
    if own:
        measured, source = max(own), "两边配置的 A/A 实测噪声"
    else:
        every = [float(diff) for entry in noise.values() if isinstance(entry, Mapping) for diff in entry.get("diffs") or ()]
        if not every:
            return float(given), "缺省值；还没跑过 A/A，先跑一次（不给 --b）再比"
        measured, source = mean(every), "别的配置的 A/A 实测噪声"
    if measured > given:
        return measured, source
    return float(given), "给定值（大于 A/A 实测噪声）"


def _chars_cell(chars: int, original: int) -> str:
    return f"{chars}*" if original and abs(chars / original - 1) > LENGTH_FLAG_SHARE else str(chars)


# ---------------------------------------------------------------------------
# 套件（留出段 + 要点 + 稿子缓存）与一次比较
# ---------------------------------------------------------------------------


def suite_id_for(subject: Subject, *, count: int, seed: str) -> str:
    plot_template = load_prompt_templates().get(PLOT_TEMPLATE)
    material = {
        "ratchet": RATCHET_VERSION,
        "profile_id": str(subject.profile.profile_id),
        "book_root": subject.book_root,
        "count": int(count),
        "seed": str(seed),
        "plot_template": str(getattr(plot_template, "version", "") or ""),
    }
    return f"{str(subject.profile.profile_id)[-8:]}-{sha256_json_normalized(material)[:10]}"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass
class Plan:
    """一次比较要做的事（空跑打印它，``--execute`` 照它跑）。"""

    subject: Subject
    arms: tuple[ResolvedArm, ResolvedArm]
    suite_id: str
    suite_dir: Path
    seed: str
    passages: list[Passage]
    plots: dict[int, dict[str, Any]]
    drafts: dict[str, dict[int, dict[str, Any]]]
    style_chars: dict[str, int] = field(default_factory=dict)

    @property
    def plots_needed(self) -> list[Passage]:
        return [passage for passage in self.passages if passage.window_no not in self.plots]

    def drafts_needed(self, arm: ResolvedArm) -> list[Passage]:
        cached = self.drafts.get(arm.key, {})
        return [passage for passage in self.passages if passage.window_no not in cached]

    @property
    def exclude(self) -> list[int]:
        return [passage.window_no for passage in self.passages]


def _plot_sha(plot: Mapping[str, Any]) -> str:
    return sha256_json_normalized(dict(plot))[:16]


def _load_suite(suite_dir: Path, session: Session, subject: Subject) -> tuple[list[Passage], dict[int, dict[str, Any]]] | None:
    meta = _read_json(suite_dir / "suite.json")
    if not isinstance(meta, Mapping) or meta.get("book_root") != subject.book_root:
        return None
    passages: list[Passage] = []
    plots: dict[int, dict[str, Any]] = {}
    for item in meta.get("passages") or ():
        stored = _read_json(suite_dir / "passages" / f"w{int(item['window_no'])}.json")
        if not isinstance(stored, Mapping) or not stored.get("text"):
            return None
        passage = Passage(
            window_no=int(item["window_no"]),
            chapter_no=item.get("chapter_no"),
            position=str(item.get("position") or "middle"),
            text=str(stored["text"]),
        )
        passages.append(passage)
        if isinstance(stored.get("plot"), Mapping):
            plots[passage.window_no] = dict(stored["plot"])
    return (passages, plots) if passages else None


def _load_drafts(suite_dir: Path, arm: ResolvedArm, passages: Sequence[Passage], plots: Mapping[int, Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    cached: dict[int, dict[str, Any]] = {}
    for passage in passages:
        stored = _read_json(suite_dir / "drafts" / arm.key / f"w{passage.window_no}.json")
        plot = plots.get(passage.window_no)
        if (
            isinstance(stored, Mapping)
            and stored.get("text")
            and plot is not None
            and stored.get("plot_sha") == _plot_sha(plot)
            and stored.get("passage_sha") == passage.sha
        ):
            cached[passage.window_no] = dict(stored)
    return cached


def plan_run(
    session: Session,
    *,
    profile_id: str | None,
    specs: tuple[ArmSpec, ArmSpec],
    passages: int = DEFAULT_PASSAGES,
    seed: str = "ratchet",
    out_dir: Path,
) -> Plan:
    """解析画像与两套配置、定留出段（已有套件就复用）、看哪些要点与稿子已经缓存。只读，不调用模型。"""
    count = max(MIN_PASSAGES, min(MAX_PASSAGES, int(passages)))
    subject = resolve_subject(session, profile_id)
    arm_a, arm_b = (resolve_arm(subject, spec) for spec in specs)
    suite_id = suite_id_for(subject, count=count, seed=seed)
    suite_dir = Path(out_dir) / suite_id
    loaded = _load_suite(suite_dir, session, subject)
    if loaded is not None:
        chosen, plots = loaded
    else:
        chosen, plots = pick_passages(session, subject, arm_a, count=count, seed=seed), {}
    plan = Plan(
        subject=subject,
        arms=(arm_a, arm_b),
        suite_id=suite_id,
        suite_dir=suite_dir,
        seed=seed,
        passages=chosen,
        plots=plots,
        drafts={arm.key: _load_drafts(suite_dir, arm, chosen, plots) for arm in (arm_a, arm_b)},
    )
    for arm in (arm_a, arm_b):
        rendered = render_style(
            session,
            arm.policy,
            _render_request(chosen[0], placement=arm.placement, exclude=plan.exclude),
            scene=None,
            persist_selection=False,
            use_cache=False,
        )
        plan.style_chars[arm.key] = len(rendered.system_prefix) + len(rendered.user_tail)
    return plan


@dataclass
class RunResult:
    run_id: str
    run_dir: Path
    verdict: Verdict
    rows: list[dict[str, Any]]
    new_call_ids: list[str]
    usage: dict[str, int]
    skipped: list[int]
    # A/A（两边只差「第几份」）：这一次量的是噪声，不判胜负
    same_config: bool = False
    epsilon_source: str = ""

    @property
    def noise(self) -> float:
        diffs = [abs(row["a"]["to_original"] - row["b"]["to_original"]) for row in self.rows]
        return sum(diffs) / len(diffs) if diffs else 0.0

    def headline(self) -> str:
        if self.same_config:
            return f"A/A：同一配置两份之间平均差 {self.noise:.3f}（这就是噪声；之后的比较 ε 至少取它）"
        return f"{self.verdict.label()} · ε = {self.verdict.epsilon:.3f}（{self.epsilon_source}）"


def _save_suite(plan: Plan) -> None:
    _write_json(
        plan.suite_dir / "suite.json",
        {
            "ratchet": RATCHET_VERSION,
            "suite_id": plan.suite_id,
            "profile_id": str(plan.subject.profile.profile_id),
            "book_id": str(plan.subject.book.book_id),
            "book_root": plan.subject.book_root,
            "seed": plan.seed,
            "passages": [passage.meta() for passage in plan.passages],
        },
    )


def _save_passage(plan: Plan, passage: Passage, plot: Mapping[str, Any] | None, call_ids: Sequence[str]) -> None:
    _write_json(
        plan.suite_dir / "passages" / f"w{passage.window_no}.json",
        {**passage.meta(), "text": passage.text, "plot": dict(plot) if plot else None, "plot_llm_call_ids": list(call_ids)},
    )


def ledger_usage(session: Session, call_ids: Sequence[str]) -> dict[str, int]:
    """这些调用在账本里的用量合计（缓存读只算供应商报了的）。"""
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_input_tokens": 0, "cache_reported_calls": 0}
    if not call_ids:
        return totals
    session.expire_all()
    for row in session.scalars(select(LlmCall).where(LlmCall.llm_call_id.in_(list(call_ids)))):
        totals["calls"] += 1
        totals["prompt_tokens"] += int(row.prompt_tokens or 0)
        totals["completion_tokens"] += int(row.completion_tokens or 0)
        summary = row.response_payload_summary if isinstance(row.response_payload_summary, Mapping) else {}
        if "cached_input_tokens" in summary:
            totals["cache_reported_calls"] += 1
            totals["cached_input_tokens"] += int(summary.get("cached_input_tokens") or 0)
    return totals


def execute_run(
    session: Session,
    plan: Plan,
    *,
    llm_client: Any,
    epsilon: float = DEFAULT_EPSILON,
    now: datetime | None = None,
    execute: Callable[..., Any] = execute_accounted_call,
    progress: Callable[[str], None] = lambda _message: None,
) -> RunResult:
    """照计划跑：缺的要点先提炼（不成的段丢掉）、缺的稿子补写、量、判、写报告与盲读页。"""
    subject = plan.subject
    try:
        ensure_cloud_llm_allowed(subject.book, operation=OPERATION, node_ids=(DRAFT_NODE_ID,), llm_client=llm_client)
    except DomainError as exc:
        # 书的云策略不许把原文送给「风格稿」路由：一次调用都没发，按拒跑报
        raise RatchetError(str(getattr(exc, "message", None) or exc)) from exc
    plot_template = load_prompt_templates().get(PLOT_TEMPLATE)
    if plot_template is None:
        raise RatchetError(f"提示词文件里没有 {PLOT_TEMPLATE} 模板。")
    started = now or datetime.now(UTC)
    new_calls: list[str] = []
    skipped: list[int] = []
    session.commit()
    _save_suite(plan)
    for passage in list(plan.plots_needed):
        progress(f"提炼情节要点：第 {passage.window_no} 窗（第 {passage.chapter_no} 章，{passage.chars} 字）")
        plot, call_ids = extract_plot(
            llm_client, plan.arms[0].route, plot_template, passage, scope_id=plan.suite_id, execute=execute
        )
        new_calls.extend(call_ids)
        _save_passage(plan, passage, plot, call_ids)
        if plot is None:
            skipped.append(passage.window_no)
            continue
        plan.plots[passage.window_no] = plot
    usable = [passage for passage in plan.passages if passage.window_no in plan.plots]
    if len(usable) < MIN_PASSAGES:
        raise RatchetError(
            f"只有 {len(usable)} 段提炼出了不带原文措辞的要点：换个 --seed 或换个起草模型再试。", refused=False
        )
    for arm in plan.arms:
        cache = plan.drafts.setdefault(arm.key, {})
        for passage in usable:
            if passage.window_no in cache:
                continue
            progress(f"配置 {arm.label} 起草：第 {passage.window_no} 窗")
            prompt = build_draft_prompt(session, arm, passage, plan.plots[passage.window_no], exclude=plan.exclude)
            session.commit()
            text, call_ids = draft_passage(llm_client, arm, prompt, scope_id=plan.suite_id, execute=execute)
            new_calls.extend(call_ids)
            record = {
                "arm": dict(arm.signature),
                "window_no": passage.window_no,
                "passage_sha": passage.sha,
                "plot_sha": _plot_sha(plan.plots[passage.window_no]),
                "text": text,
                "llm_call_ids": call_ids,
                "style_chars": prompt.style_chars,
                "sample_windows": list(prompt.sample_windows),
                "created_at": datetime.now(UTC).isoformat(),
            }
            _write_json(plan.suite_dir / "drafts" / arm.key / f"w{passage.window_no}.json", record)
            cache[passage.window_no] = record
    arm_a, arm_b = plan.arms
    rows: list[dict[str, Any]] = []
    pairs: list[tuple[float, float]] = []
    states = subject.policy.dimension_states
    for passage in usable:
        original = text_features(passage.text)
        original_reading = read_fidelity(passage.text, subject.dist, dimension_states=states)
        score_a = score_draft(plan.drafts[arm_a.key][passage.window_no]["text"], original, subject)
        score_b = score_draft(plan.drafts[arm_b.key][passage.window_no]["text"], original, subject)
        pairs.append((score_a.to_original, score_b.to_original))
        rows.append(
            {
                **passage.meta(),
                "original_percentile": round(float(original_reading.percentile), 1),
                "a": score_a.to_json(),
                "b": score_b.to_json(),
            }
        )
    same = base_key(arm_a) == base_key(arm_b)
    noise_path = plan.suite_dir.parent / NOISE_FILE
    if same:
        threshold, source = epsilon, "A/A"
    else:
        threshold, source = effective_epsilon(_read_json(noise_path) or {}, plan.arms, epsilon)
    verdict = decide(pairs, epsilon=threshold)
    run_id = f"{started.strftime('%Y%m%d-%H%M%S')}-{arm_a.key[:6]}-vs-{arm_b.key[:6]}"
    run_dir = plan.suite_dir / "runs" / run_id
    usage = ledger_usage(session, new_calls)
    result = RunResult(
        run_id=run_id,
        run_dir=run_dir,
        verdict=verdict,
        rows=rows,
        new_call_ids=new_calls,
        usage=usage,
        skipped=skipped,
        same_config=same,
        epsilon_source=source,
    )
    if same:
        record_noise(noise_path, arm_a, run_id=run_id, diffs=[abs(a - b) for a, b in pairs])
    _write_run_files(plan, result, usable, started=started)
    return result


# ---------------------------------------------------------------------------
# 报告与盲读页
# ---------------------------------------------------------------------------


def _reading_order(run_id: str, window_no: int) -> list[str]:
    order = ["original", "A", "B"]
    random.Random(f"{run_id}:{window_no}").shuffle(order)
    return order


def render_reading_sheet(plan: Plan, result: RunResult, passages: Sequence[Passage]) -> tuple[str, dict[str, Any]]:
    """盲读页（Markdown）与答案。"""
    arm_a, arm_b = plan.arms
    texts = {
        "A": {p.window_no: plan.drafts[arm_a.key][p.window_no]["text"] for p in passages},
        "B": {p.window_no: plan.drafts[arm_b.key][p.window_no]["text"] for p in passages},
    }
    lines = [
        "# 盲读：哪一份最像参考作者？",
        "",
        f"套件 {plan.suite_id} · 比较 {result.run_id}",
        "",
        "每一段是同一段情节的三份文字：一份是参考书的原文，两份是两套配置照着同样的情节要点写的。"
        "先别看答案（同目录的 key.json、report.md），每段读完记下：哪份最像原作者、哪份最不像、为什么。",
        "",
    ]
    key: dict[str, Any] = {}
    for index, passage in enumerate(passages, start=1):
        order = _reading_order(result.run_id, passage.window_no)
        plot = plan.plots[passage.window_no]
        lines.extend(
            [
                f"## 第 {index} 段（参考书第 {passage.chapter_no} 章，原文约 {passage.chars} 字）",
                "",
                "情节：" + "；".join(str(beat) for beat in plot.get("beats") or ()),
                "",
            ]
        )
        key[str(index)] = {"window_no": passage.window_no}
        for label, source in zip(READING_LABELS, order):
            body = passage.text if source == "original" else texts[source][passage.window_no]
            lines.extend([f"### {label}", "", body.strip(), ""])
            key[str(index)][label] = source
    return "\n".join(lines).rstrip() + "\n", key


def render_report(plan: Plan, result: RunResult) -> str:
    arm_a, arm_b = plan.arms
    usage = result.usage
    lines = [
        f"# 风格棘轮：A 对 B（{result.run_id}）",
        "",
        f"- 画像 {plan.subject.profile.profile_id} · 书 {plan.subject.book.book_id} · 套件 {plan.suite_id}",
        f"- 配置 A：{arm_a.describe()}",
        f"- 配置 B：{arm_b.describe()}",
        f"- 留出段 {len(result.rows)} 段" + (f"（另有 {len(result.skipped)} 段没提炼出干净的要点，没比）" if result.skipped else ""),
        "",
        f"## 结论：{result.headline()}",
        "",
        "判法：B 在每一段都比 A 离原文更近、且平均近 ε 以上才算 B 更像；反之 A 更像；其余分不出。ε 取给定值与 A/A 实测噪声"
        "（同一配置写两遍的平均差，noise.json）中较大的那个。这只是表面统计，像不像以盲读为准（reading.md）。",
        "",
        "## 逐段",
        "",
        "| 窗 | 章 | 原文字数 | A 距原文 | B 距原文 | 更近 | A 字数 | B 字数 | A 分位 | B 分位 | 原文分位 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in result.rows:
        a, b = row["a"], row["b"]
        nearer = "B" if b["to_original"] < a["to_original"] else ("A" if a["to_original"] < b["to_original"] else "=")
        lines.append(
            f"| {row['window_no']} | {row['chapter_no']} | {row['chars']} | {a['to_original']:.3f} | {b['to_original']:.3f} | {nearer} "
            f"| {_chars_cell(a['chars'], row['chars'])} | {_chars_cell(b['chars'], row['chars'])} "
            f"| {a['percentile']:.0f} | {b['percentile']:.0f} | {row['original_percentile']:.0f} |"
        )
    lines.extend(
        [
            "",
            f"- 字数后面带 * 的稿子偏离原段超过 {LENGTH_FLAG_SHARE:.0%}：它的距离里混着篇幅的差，读的时候留意。",
            "- 距原文：测量核各特征与原段之差 ÷ 作者窗口分布的尺度，按「像不像」读数的维权重平均；越小越像。",
            "- 分位：对作者全书分布的读数（与起草台同一口径），作者自己的一窗平均在 50 左右，越高越不像；原文分位是参照。",
            "",
            "## 成本（这一次新发的调用；缓存里已有的要点与稿子不再调用）",
            "",
            f"- 调用 {usage['calls']} 次：输入 {usage['prompt_tokens']:,} token，输出 {usage['completion_tokens']:,} token",
            (
                f"- 供应商报了缓存数的调用 {usage['cache_reported_calls']} 次，缓存读 {usage['cached_input_tokens']:,} token"
                if usage["cache_reported_calls"]
                else "- 供应商没有报缓存数（中转没传，或者没有命中缓存的计量）"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _write_run_files(plan: Plan, result: RunResult, passages: Sequence[Passage], *, started: datetime) -> None:
    sheet, key = render_reading_sheet(plan, result, passages)
    result.run_dir.mkdir(parents=True, exist_ok=True)
    (result.run_dir / "reading.md").write_text(sheet, encoding="utf-8")
    _write_json(result.run_dir / "key.json", key)
    (result.run_dir / "report.md").write_text(render_report(plan, result), encoding="utf-8")
    _write_json(
        result.run_dir / "result.json",
        {
            "ratchet": RATCHET_VERSION,
            "run_id": result.run_id,
            "started_at": started.isoformat(),
            "suite_id": plan.suite_id,
            "arms": {"A": dict(plan.arms[0].signature), "B": dict(plan.arms[1].signature)},
            "arm_keys": {"A": plan.arms[0].key, "B": plan.arms[1].key},
            "verdict": result.verdict.to_json(),
            "same_config": result.same_config,
            "epsilon_source": result.epsilon_source,
            "noise": round(result.noise, 4) if result.same_config else None,
            "rows": result.rows,
            "skipped": result.skipped,
            "usage": result.usage,
            "new_llm_call_ids": result.new_call_ids,
        },
    )


__all__ = [
    "ArmSpec",
    "DEFAULT_EPSILON",
    "DEFAULT_PASSAGES",
    "DRAFT_NODE_ID",
    "DRAFT_TEMPLATE",
    "DraftPrompt",
    "DraftScore",
    "PLOT_TEMPLATE",
    "Passage",
    "Plan",
    "RATCHET_VERSION",
    "RatchetError",
    "ResolvedArm",
    "RunResult",
    "SCOPE_TYPE",
    "Subject",
    "Verdict",
    "base_key",
    "build_draft_prompt",
    "decide",
    "draft_passage",
    "draft_snapshot",
    "effective_epsilon",
    "execute_run",
    "extract_plot",
    "feature_distance",
    "leaked_share",
    "ledger_usage",
    "normalize_plot",
    "parse_arm",
    "pick_passages",
    "plan_run",
    "plot_text",
    "record_noise",
    "render_reading_sheet",
    "render_report",
    "resolve_arm",
    "resolve_subject",
    "score_draft",
    "suite_id_for",
]
