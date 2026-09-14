from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.contracts.bundle import BundleSnapshotHashProjection
from novel_system.db.models import (
    AttemptTracker,
    AuthorPreferenceProfile,
    ChapterGoal,
    FinalScene,
    GenerationPlanningArtifact,
    SceneBlueprint,
    SceneBundle,
    SceneCard,
    SceneDraft,
    SceneMemory,
    SceneRunState,
    StoryProject,
    StoryCharacter,
    VolumeSummary,
)
from novel_system.services.errors import DomainError
from novel_system.services.hash_engine import compute_bundle_hash_projection
from novel_system.services.literary_quality import fingerprint_literary_quality
from novel_system.services.resolver import Resolver
from novel_system.services.character_continuity import (
    CHARACTER_CONTRACT_VERSION,
    build_character_contract_digest,
)
from novel_system.services.scene_design_context import (
    SCENE_DESIGN_SECTION_KEY,
    build_scene_design_context,
)
from novel_system.services.scene_digest import scene_card_digest
from novel_system.services.scene_ownership import require_scene_project_id
from novel_system.services.scene_structure_brief import (
    SCENE_STRUCTURE_SECTION_KEY,
    render_scene_structure_brief,
)
from novel_system.services.style_reference.config_loader import (
    load_optional_yaml_config,
)
from novel_system.services.style_reference.injection import InjectionService
from novel_system.services.style_reference.narrative_guidance import (
    NARRATIVE_GUIDANCE_SECTION_KEY,
    collect_narrative_guidance,
    render_narrative_section,
)
from novel_system.services.style_reference.runtime_contract import (
    STYLE_RUNTIME_CONTRACT_VERSION,
    build_style_runtime_contract,
)
from novel_system.services.style_reference.style_continuity import (
    contract_deliberate_repetition,
    latest_drift_calibration,
    latest_drift_event,
)
from novel_system.services.writer_briefs import (
    normalize_chapter_writer_brief,
    normalize_scene_writer_brief,
    writer_brief_has_content,
)
from novel_system.services.author_preferences import (
    merge_preference_summaries,
    safe_preference_summary_for_prompt,
)
from novel_system.services.author_instructions import normalize_author_note


_LOGGER = logging.getLogger(__name__)

# 2026-09 风格模仿 v2（W5，规格 §1.3）——三个新 section 的登记名。
VOICE_ANCHOR_SECTION_KEY = "previous_scene_voice_anchor"
DRIFT_CALIBRATION_SECTION_KEY = "style_drift_calibration"
# 「前文声音锚」取上一场最新的**已风格化**稿：style_draft 本体、反模板重写、软补丁、
# 安全挽救稿都算；中性稿 / rejected 行不算（前者无目标文风，后者是被否决的文本）。
STYLED_DRAFT_STAGES: tuple[str, ...] = (
    "style_draft",
    "de_template",
    "style_patch",
    "style_salvage",
)
_CONTINUITY_BUDGET_DEFAULTS: dict[str, int] = {
    "continuity_anchor_max_chars": 900,
    "drift_calibration_max_lines": 3,
}
_SENTENCE_END_RE = re.compile(r"[。！？!?…]+[”’」』）)]*")


def load_continuity_budget() -> dict[str, int]:
    """读 ``config/style_reference/injection_budget.yaml`` 的跨场景连续性预算键。

    W4 负责把 ``continuity_anchor_max_chars`` / ``drift_calibration_max_lines`` 写进
    yaml；文件或键缺失时回到规格 §1.4 的默认值（900 字 / 3 行）。
    """
    budget = dict(_CONTINUITY_BUDGET_DEFAULTS)
    try:
        raw = load_optional_yaml_config("injection_budget")
    except Exception:  # noqa: BLE001 — 配置损坏不应让 bundle 构建失败
        _LOGGER.warning("injection_budget.yaml unreadable; using continuity defaults", exc_info=True)
        return budget
    for key, default in _CONTINUITY_BUDGET_DEFAULTS.items():
        value = raw.get(key, default)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        budget[key] = parsed if parsed > 0 else default
    return budget


def tail_at_sentence_boundary(text: str, max_chars: int) -> str:
    """取 ``text`` 尾部 ≤``max_chars`` 字，并让片段从一个完整句子起头。"""
    normalized = str(text or "").strip()
    if not normalized or max_chars <= 0:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    tail = normalized[-max_chars:]
    match = _SENTENCE_END_RE.search(tail)
    if match is not None and match.end() < len(tail):
        tail = tail[match.end():]
    return tail.strip()


def resolve_scene_style_runtime_contract(
    session: Session,
    scene: SceneCard,
    *,
    task_type: str = "scene_generation",
) -> dict[str, Any] | None:
    """按场景解析 active 绑定层并冻结成运行时契约；无绑定 → ``None``。

    供 ``BundleBuilder`` 之外的规划节点（scene_blueprint 等）复用同一套
    scene > character > project > global 作用域解析。解析 / 冻结失败时抛出，
    由调用方决定如何降级（bundle 内部的降级槽记录不在这里做）。
    """
    style_injection = InjectionService(session)
    character_ids = list(
        dict.fromkeys(
            value
            for value in [
                getattr(scene, "pov_character_id", None),
                *(getattr(scene, "onstage_chars_json", None) or []),
            ]
            if value
        )
    )
    layers = style_injection.resolve_binding_layers(
        getattr(scene, "project_id", None),
        task_type,
        character_ids=character_ids,
        scene_id=getattr(scene, "scene_id", None),
    )
    if not layers:
        return None
    return build_style_runtime_contract(
        style_injection.repo,
        layers,
        task_type=task_type,
    )


# scene_generation 在风格稿未过确定性安全门时，把**已批准的中性稿**原文写成主
# ``style_draft`` 行（provider 原稿另存为 ``style_rejected``），并在 AttemptTracker
# ``details_json.content_source`` 上打这个标记；行本身没有字段能区分。
NEUTRAL_FALLBACK_CONTENT_SOURCE = "approved_neutral_fallback"
# 2026-09-12 风格直起:style_first 下中性步位的首稿已按参考作者手笔写成——它的正文**是**
# 目标文风,既不该被当成「中性稿」排除,也可以直接作前文声音锚(无更晚的风格稿时)。
STYLE_FIRST_DRAFT_CONTENT_SOURCE = "style_first_draft"


def _normalized_draft_text(content: str | None) -> str:
    return str(content or "").strip()


def _neutral_fallback_styled_row_ids(session: Session, scene_id: str) -> set[str]:
    """该场景 AttemptTracker 标记为「回退到中性稿」的 styled 行 ``row_id`` 集合。"""
    row_ids: set[str] = set()
    for details in (
        session.execute(select(AttemptTracker.details_json).where(AttemptTracker.scene_id == scene_id)).scalars().all()
    ):
        if not isinstance(details, dict):
            continue
        if details.get("content_source") != NEUTRAL_FALLBACK_CONTENT_SOURCE:
            continue
        row_id = details.get("row_id")
        if isinstance(row_id, str) and row_id:
            row_ids.add(row_id)
    return row_ids


def _style_first_neutral_row_ids(session: Session, scene_id: str) -> set[str]:
    """该场景 AttemptTracker 标记为「首稿直起」的 ``neutral_draft`` 行 ``row_id`` 集合。"""
    row_ids: set[str] = set()
    for details in (
        session.execute(
            select(AttemptTracker.details_json).where(
                AttemptTracker.scene_id == scene_id,
                AttemptTracker.step == "neutral_draft",
            )
        )
        .scalars()
        .all()
    ):
        if not isinstance(details, dict):
            continue
        if details.get("content_source") != STYLE_FIRST_DRAFT_CONTENT_SOURCE:
            continue
        row_id = details.get("row_id")
        if isinstance(row_id, str) and row_id:
            row_ids.add(row_id)
    return row_ids


def _neutral_draft_texts(session: Session, scene_id: str) -> set[str]:
    """该场景所有**真正中性**的 ``neutral_draft`` 行正文（规范化后）集合。

    style_first 的首稿虽然落在 ``neutral_draft`` 行,却已是目标文风,不算中性正文。
    """
    style_first_rows = _style_first_neutral_row_ids(session, scene_id)
    return {
        _normalized_draft_text(content)
        for row_id, content in session.execute(
            select(SceneDraft.row_id, SceneDraft.content).where(
                SceneDraft.scene_id == scene_id,
                SceneDraft.stage == "neutral_draft",
            )
        ).all()
        if row_id not in style_first_rows
    }


def _latest_style_first_draft(session: Session, scene_id: str) -> SceneDraft | None:
    """最新一条首稿直起的 ``neutral_draft`` 行(未被否决);没有 → ``None``。"""
    row_ids = _style_first_neutral_row_ids(session, scene_id)
    if not row_ids:
        return None
    return (
        session.execute(
            select(SceneDraft)
            .where(
                SceneDraft.scene_id == scene_id,
                SceneDraft.stage == "neutral_draft",
                SceneDraft.status != "rejected",
                SceneDraft.row_id.in_(sorted(row_ids)),
            )
            .order_by(SceneDraft.created_at.desc(), SceneDraft.row_id.desc())
        )
        .scalars()
        .first()
    )


def latest_styled_draft_for_scene(session: Session, scene_id: str) -> SceneDraft | None:
    """某场景最新一条**真正**已风格化、未被否决的 ``SceneDraft``。

    stage 在 ``STYLED_DRAFT_STAGES`` 内且未被否决只是必要条件：风格稿未过安全门时
    scene_generation 会把已批准的中性稿原文写成主 ``style_draft`` 行
    （``STYLE_DRAFT_FALLBACK_NEUTRAL``），这种行承载的是无目标文风的中性散文，
    作为「前文声音锚」会把下一场钉在中性稿上。因此再跳过两类行：
    AttemptTracker 标记 ``content_source == approved_neutral_fallback`` 的行，
    以及正文与同场景任一 ``neutral_draft`` 行完全相同的行。都被跳过 → ``None``。
    """
    rows = (
        session.execute(
            select(SceneDraft)
            .where(
                SceneDraft.scene_id == scene_id,
                SceneDraft.stage.in_(STYLED_DRAFT_STAGES),
                SceneDraft.status != "rejected",
            )
            .order_by(SceneDraft.created_at.desc(), SceneDraft.row_id.desc())
        )
        .scalars()
        .all()
    )
    fallback_row_ids = _neutral_fallback_styled_row_ids(session, scene_id)
    neutral_texts = _neutral_draft_texts(session, scene_id)
    for row in rows:
        if row.row_id in fallback_row_ids:
            continue
        if _normalized_draft_text(row.content) in neutral_texts:
            continue
        return row
    # 2026-09-12 风格直起:还没有合格的风格稿时,首稿直起的中性步位行本身就是目标文风。
    return _latest_style_first_draft(session, scene_id)


_FRESHNESS_PRUNE_KEY_PREFIXES: tuple[str, ...] = ("avoid_", "vary_")
_FW_CORE_RE = re.compile(r"[^A-Za-z0-9㐀-鿿]+")


def _function_word_inventory() -> tuple[frozenset[str], int]:
    """(voice_signature 词表全部组的并集, 最长词长)；词表不可用时为空集（不剔除任何条目）。"""
    try:
        from novel_system.services.style_reference.voice_signature import load_voice_lexicon

        words = frozenset(
            word for group in load_voice_lexicon().group_words.values() for word in group if word
        )
    except Exception:  # noqa: BLE001 — 词表损坏不应让 bundle 构建失败
        _LOGGER.warning("function word lexicon unavailable; freshness exemption disabled", exc_info=True)
        return frozenset(), 0
    return words, max((len(word) for word in words), default=0)


def _is_function_word_only(text: str, words: frozenset[str], max_len: int) -> bool:
    """去掉标点 / 空白后能被虚词表完全切分（或什么都不剩）→ True。"""
    core = _FW_CORE_RE.sub("", str(text or ""))
    if not core:
        return True
    if not words:
        return False
    reachable = [False] * (len(core) + 1)
    reachable[0] = True
    for start in range(len(core)):
        if not reachable[start]:
            continue
        for length in range(1, min(max_len, len(core) - start) + 1):
            if core[start : start + length] in words:
                reachable[start + length] = True
    return reachable[len(core)]


def _prune_function_word_only_entries(budget: dict[str, Any]) -> list[str]:
    """就地剔除 ``avoid_*`` / ``vary_*`` 列表里仅由虚词与标点组成的条目；返回被剔除的条目。"""
    words, max_len = _function_word_inventory()
    removed: list[str] = []
    for key, value in list(budget.items()):
        if not isinstance(value, list) or not str(key).startswith(_FRESHNESS_PRUNE_KEY_PREFIXES):
            continue
        kept: list[Any] = []
        for item in value:
            if isinstance(item, str) and _is_function_word_only(item, words, max_len):
                removed.append(item)
                continue
            kept.append(item)
        if len(kept) != len(value):
            budget[key] = kept
    return removed


def _previous_chapter(session: Session, scene: SceneCard) -> ChapterGoal | None:
    current = session.get(ChapterGoal, scene.chapter_id)
    if current is None:
        return None
    if current.display_order is not None:
        stmt = (
            select(ChapterGoal)
            .where(
                ChapterGoal.project_id == scene.project_id,
                ChapterGoal.trashed_flag == 0,
                ChapterGoal.display_order < current.display_order,
            )
            .order_by(ChapterGoal.display_order.desc())
        )
    else:
        stmt = (
            select(ChapterGoal)
            .where(
                ChapterGoal.project_id == scene.project_id,
                ChapterGoal.trashed_flag == 0,
                ChapterGoal.chapter_id < scene.chapter_id,
            )
            .order_by(ChapterGoal.chapter_id.desc())
        )
    return session.execute(stmt).scalars().first()


def previous_scene_voice_anchor(
    session: Session,
    scene: SceneCard,
    *,
    max_chars: int | None = None,
) -> dict[str, Any] | None:
    """规格 §1.3「前文声音锚」：同章上一场（按 scene_seq 最近）的最新已风格化稿尾部。

    同章没有已风格化前文时退到上一章最后一场；都没有 → ``None``（不登记）。
    返回 ``{"text", "source_scene_id", "source_draft_row_id", "source_stage",
    "max_chars"}``。
    """
    limit = (
        int(max_chars)
        if max_chars is not None
        else load_continuity_budget()["continuity_anchor_max_chars"]
    )
    if limit <= 0:
        return None
    candidates = list(
        session.execute(
            select(SceneCard)
            .where(
                SceneCard.chapter_id == scene.chapter_id,
                SceneCard.trashed_flag == 0,
                SceneCard.scene_seq < scene.scene_seq,
            )
            .order_by(SceneCard.scene_seq.desc())
        )
        .scalars()
        .all()
    )
    if not candidates:
        previous_chapter = _previous_chapter(session, scene)
        if previous_chapter is not None:
            candidates = list(
                session.execute(
                    select(SceneCard)
                    .where(
                        SceneCard.chapter_id == previous_chapter.chapter_id,
                        SceneCard.trashed_flag == 0,
                    )
                    .order_by(SceneCard.scene_seq.desc())
                )
                .scalars()
                .all()
            )
    for candidate in candidates:
        draft = latest_styled_draft_for_scene(session, candidate.scene_id)
        if draft is None:
            continue
        text = tail_at_sentence_boundary(draft.content or "", limit)
        if not text:
            continue
        return {
            "text": text,
            "source_scene_id": candidate.scene_id,
            "source_draft_row_id": draft.row_id,
            "source_stage": draft.stage,
            "max_chars": limit,
        }
    return None


class BundleBuilder:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.resolver = Resolver()
        # 审计 P-11：可选注入槽的降级不允许静默——WARNING 落日志并随快照暴露。
        self._degraded_slots: set[str] = set()

    def _slot_degraded(self, slot: str, scene: SceneCard | None = None) -> None:
        """记录一个可选注入槽的降级（在 except 块内调用，exc_info 取当前异常）。"""
        self._degraded_slots.add(slot)
        _LOGGER.warning(
            "bundle slot %s degraded for scene %s",
            slot,
            getattr(scene, "scene_id", "?"),
            exc_info=True,
        )

    @staticmethod
    def _single_or_list(values: list[str]) -> str | list[str]:
        return values[0] if len(values) == 1 else values

    @staticmethod
    def _combined_text(rows: list[Any], text_field: str) -> str:
        return "\n\n".join(
            str(getattr(row, text_field))
            for row in rows
            if getattr(row, text_field, None)
        )

    def _next_bundle_id(self, scene_id: str, state: SceneRunState) -> tuple[str, int]:
        build_no = (state.bundle_build_count or 0) + 1
        while True:
            bundle_id = f"bundle_{scene_id}_v{build_no}"
            if self.session.get(SceneBundle, bundle_id) is None:
                return bundle_id, build_no
            build_no += 1

    def build(
        self,
        scene_id: str,
        execution_mode: str = "P2",
        force_rebuild: bool = False,
        *,
        author_note: str | None = None,
    ) -> dict[str, Any]:
        self._degraded_slots = set()
        scene = self.session.get(SceneCard, scene_id)
        if scene is None:
            raise DomainError("SCENE_NOT_FOUND", "scene not found", status_code=404)
        chapter = self.session.get(ChapterGoal, scene.chapter_id)
        if chapter is None:
            raise DomainError("CHAPTER_NOT_FOUND", "chapter not found", status_code=404)
        state = self.session.get(SceneRunState, scene_id)
        previous_memory = (
            self.session.execute(
                select(SceneMemory)
                .join(SceneCard, SceneCard.scene_id == SceneMemory.scene_id)
                .where(
                    SceneMemory.chapter_id == scene.chapter_id,
                    SceneMemory.active_flag == 1,
                    SceneMemory.runtime_eligible == 1,
                    SceneCard.trashed_flag == 0,
                    SceneCard.scene_seq < scene.scene_seq,
                )
                .order_by(SceneCard.scene_seq.desc(), SceneMemory.created_at.desc())
            )
            .scalars()
            .first()
        )

        source_version_refs = {
            "chapter_goal": chapter.chapter_id,
            "scene_card": scene.scene_id,
        }
        style_injection = InjectionService(self.session)
        style_character_ids = list(
            dict.fromkeys(
                value
                for value in [
                    scene.pov_character_id,
                    *(scene.onstage_chars_json or []),
                ]
                if value
            )
        )
        reference_resolution_degraded = False
        try:
            reference_layers = style_injection.resolve_binding_layers(
                scene.project_id,
                "scene_generation",
                character_ids=style_character_ids,
                scene_id=scene.scene_id,
            )
        except Exception:  # noqa: BLE001 — optional style layer degrades visibly
            reference_layers = []
            reference_resolution_degraded = True
            self._slot_degraded("style_reference_binding_resolution", scene)
        reference_profile_ids = list(
            dict.fromkeys(layer.profile_id for layer in reference_layers)
        )
        if reference_profile_ids:
            # 来源画像必须进入冻结 bundle 的版本引用：归档/回放时据此加载动态
            # protected_terms / scene_bridges，不能只在 prompt 注入侧短暂可见。
            source_version_refs["reference_profile_ids"] = reference_profile_ids
        ordered_injections = [
            {
                "slot": "chapter_goal",
                "ref_id": chapter.chapter_id,
                "digest_key": "chapter_goal",
            },
            {
                "slot": "scene_card",
                "ref_id": scene.scene_id,
                "digest_key": "scene_card",
            },
        ]
        inline_digests = {
            "chapter_goal": chapter.chapter_goal,
            "scene_card": scene_card_digest(scene),
        }
        # 2026-09-13 阶段 A：雪花 / 章节编排写下的场景结构（形态、坩埚、三拍、代价）直读
        # 原始键进入 bundle，作为与 scene_card 同级的事实 section。此前它只经 v2 简报的
        # 归一化通道到达写作，而那条通道会把这些键全部丢掉——起草模型从未见过作者的三拍。
        structure_brief = render_scene_structure_brief(scene, self.session)
        if structure_brief:
            source_version_refs[SCENE_STRUCTURE_SECTION_KEY] = scene.scene_id
            ordered_injections.append(
                {
                    "slot": SCENE_STRUCTURE_SECTION_KEY,
                    "ref_id": scene.scene_id,
                    "digest_key": SCENE_STRUCTURE_SECTION_KEY,
                }
            )
            inline_digests[SCENE_STRUCTURE_SECTION_KEY] = structure_brief
        # 2026-09-13 阶段 F：已确认的雪花设计背景（02 / 03 / 04 / 06 与章表、相邻两场）紧随结构简报。
        # 它是背景不是事实：预算紧时被压缩 / 省略，硬 QC 不看；引用的步骤版本进 source_version_refs，
        # 设计一改，bundle 哈希就变。
        design_context = build_scene_design_context(scene, self.session)
        if design_context is not None:
            source_version_refs[SCENE_DESIGN_SECTION_KEY] = list(design_context.step_run_ids)
            ordered_injections.append(
                {
                    "slot": SCENE_DESIGN_SECTION_KEY,
                    "ref_id": scene.scene_id,
                    "digest_key": SCENE_DESIGN_SECTION_KEY,
                }
            )
            inline_digests[SCENE_DESIGN_SECTION_KEY] = design_context.text
        source_version_refs["style_reference_runtime_contract_version"] = (
            STYLE_RUNTIME_CONTRACT_VERSION
        )
        source_version_refs["style_reference_runtime_contract_status"] = (
            "degraded"
            if reference_resolution_degraded
            else ("frozen" if reference_layers else "absent")
        )
        if reference_layers:
            try:
                style_runtime_contract = build_style_runtime_contract(
                    style_injection.repo,
                    reference_layers,
                    task_type="scene_generation",
                )
                if style_runtime_contract is not None:
                    source_version_refs["style_reference_runtime_contract_hash"] = (
                        style_runtime_contract["contract_hash"]
                    )
                    source_version_refs["reference_binding_ids"] = (
                        style_runtime_contract["binding_ids"]
                    )
                    inline_digests["_style_reference_runtime_contract"] = json.dumps(
                        style_runtime_contract,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    # v2（规格 §1.3）：叙事机制指引——neutral_draft 与 style_draft 都可见。
                    narrative_lines = collect_narrative_guidance(style_runtime_contract)
                    narrative_text = render_narrative_section(narrative_lines)
                    if narrative_text:
                        source_version_refs["style_narrative_guidance_contract_hash"] = (
                            style_runtime_contract["contract_hash"]
                        )
                        source_version_refs["style_narrative_guidance_line_count"] = len(
                            narrative_lines
                        )
                        ordered_injections.append(
                            {
                                "slot": NARRATIVE_GUIDANCE_SECTION_KEY,
                                "ref_id": style_runtime_contract["contract_hash"],
                                "digest_key": NARRATIVE_GUIDANCE_SECTION_KEY,
                            }
                        )
                        inline_digests[NARRATIVE_GUIDANCE_SECTION_KEY] = narrative_text
            except Exception:  # noqa: BLE001 — optional style layer degrades visibly
                source_version_refs["style_reference_runtime_contract_status"] = (
                    "degraded"
                )
                self._slot_degraded("style_reference_runtime_contract", scene)
        normalized_author_note = normalize_author_note(author_note)
        if normalized_author_note:
            instruction_hash = hashlib.sha256(
                normalized_author_note.encode("utf-8")
            ).hexdigest()
            source_version_refs["author_instruction_hash"] = instruction_hash
            ordered_injections.append(
                {
                    "slot": "author_instruction",
                    "ref_id": f"author_instruction:{instruction_hash}",
                    "digest_key": "author_instruction",
                }
            )
            inline_digests["author_instruction"] = normalized_author_note
        chapter_writer_brief = normalize_chapter_writer_brief(chapter.writer_brief_json)
        if writer_brief_has_content(chapter_writer_brief):
            source_version_refs["chapter_writer_brief"] = chapter.chapter_id
            ordered_injections.append(
                {
                    "slot": "chapter_writer_brief",
                    "ref_id": chapter.chapter_id,
                    "digest_key": "chapter_writer_brief",
                }
            )
            inline_digests["chapter_writer_brief"] = json.dumps(
                chapter_writer_brief,
                ensure_ascii=False,
                sort_keys=True,
            )
        scene_writer_brief = normalize_scene_writer_brief(scene.writer_brief_json)
        if writer_brief_has_content(scene_writer_brief):
            source_version_refs["scene_writer_brief"] = scene.scene_id
            ordered_injections.append(
                {
                    "slot": "scene_writer_brief",
                    "ref_id": scene.scene_id,
                    "digest_key": "scene_writer_brief",
                }
            )
            inline_digests["scene_writer_brief"] = json.dumps(
                scene_writer_brief,
                ensure_ascii=False,
                sort_keys=True,
            )

        scene_blueprint = (
            self.session.execute(
                select(SceneBlueprint)
                .where(
                    SceneBlueprint.scene_id == scene.scene_id,
                    SceneBlueprint.status.in_(("accepted", "draft")),
                )
                .order_by(
                    SceneBlueprint.created_at.desc(), SceneBlueprint.row_id.desc()
                )
            )
            .scalars()
            .first()
        )
        if scene_blueprint is not None:
            source_version_refs["scene_blueprint_row_id"] = scene_blueprint.row_id
            ordered_injections.append(
                {
                    "slot": "scene_blueprint",
                    "ref_id": scene_blueprint.row_id,
                    "digest_key": "scene_blueprint",
                }
            )
            inline_digests["scene_blueprint"] = json.dumps(
                scene_blueprint.blueprint_json or {},
                ensure_ascii=False,
                sort_keys=True,
            )

        character_pressure = self._latest_planning_artifact(
            artifact_type="character_pressure_blueprint",
            object_type="scene",
            object_id=scene.scene_id,
        )
        if character_pressure is not None:
            source_version_refs["character_pressure_artifact_row_id"] = (
                character_pressure.row_id
            )
            ordered_injections.append(
                {
                    "slot": "character_pressure",
                    "ref_id": character_pressure.row_id,
                    "digest_key": "character_pressure",
                }
            )
            inline_digests["character_pressure"] = json.dumps(
                character_pressure.payload_json or {},
                ensure_ascii=False,
                sort_keys=True,
            )

        chapter_architecture = self._latest_planning_artifact(
            artifact_type="chapter_story_architecture",
            object_type="chapter",
            object_id=scene.chapter_id,
        )
        if chapter_architecture is not None:
            source_version_refs["chapter_story_architecture_artifact_row_id"] = (
                chapter_architecture.row_id
            )
            ordered_injections.append(
                {
                    "slot": "chapter_story_architecture",
                    "ref_id": chapter_architecture.row_id,
                    "digest_key": "chapter_story_architecture",
                }
            )
            inline_digests["chapter_story_architecture"] = json.dumps(
                chapter_architecture.payload_json or {},
                ensure_ascii=False,
                sort_keys=True,
            )

        voice_profile_id = self.resolver.resolve_voice_profile_id(scene)
        voice_profile = self.resolver.resolve_active_voice_profile(self.session, scene)
        if voice_profile_id and voice_profile is None:
            raise DomainError(
                "BUNDLE_SOURCE_MISSING",
                f"active voice profile missing for {voice_profile_id}",
                status_code=409,
            )
        if voice_profile:
            source_version_refs["voice_profile_id"] = voice_profile.voice_profile_id
            source_version_refs["voice_profile_row_id"] = voice_profile.row_id
            source_version_refs["voice_profile_version"] = voice_profile.version
            ordered_injections.append(
                {
                    "slot": "pov_voice",
                    "ref_id": voice_profile.voice_profile_id,
                    "digest_key": "voice_card",
                }
            )
            inline_digests["voice_card"] = voice_profile.content

        relation_profile_id = self.resolver.resolve_relation_profile_id(scene)
        relation_profile = self.resolver.resolve_active_relation_profile(
            self.session, scene
        )
        if relation_profile_id and relation_profile is None:
            raise DomainError(
                "BUNDLE_SOURCE_MISSING",
                f"active relation profile missing for {relation_profile_id}",
                status_code=409,
            )
        if relation_profile:
            source_version_refs["relation_profile_id"] = (
                relation_profile.relation_profile_id
            )
            source_version_refs["relation_profile_row_id"] = relation_profile.row_id
            source_version_refs["relation_profile_version"] = relation_profile.version
            ordered_injections.append(
                {
                    "slot": "relation",
                    "ref_id": relation_profile.relation_profile_id,
                    "digest_key": "relation_card",
                }
            )
            inline_digests["relation_card"] = relation_profile.content

        # 解析 pov/onstage 的权威 display_name（StoryCharacter），避免裸 id 进提示词当人名
        contract_char_ids = [
            cid
            for cid in [scene.pov_character_id, *(scene.onstage_chars_json or [])]
            if cid
        ]
        character_display_names: dict[str, str] = {}
        if contract_char_ids:
            for row in (
                self.session.execute(
                    select(StoryCharacter).where(
                        StoryCharacter.character_id.in_(contract_char_ids)
                    )
                )
                .scalars()
                .all()
            ):
                if row.display_name:
                    character_display_names[row.character_id] = row.display_name
        character_contract = build_character_contract_digest(
            pov_character_id=scene.pov_character_id,
            onstage_character_ids=scene.onstage_chars_json,
            voice_profile_content=voice_profile.content if voice_profile else None,
            relation_profile_content=(
                relation_profile.content if relation_profile else None
            ),
            display_names=character_display_names,
        )
        if character_contract:
            source_version_refs["character_contract"] = CHARACTER_CONTRACT_VERSION
            ordered_injections.append(
                {
                    "slot": "character_contract",
                    "ref_id": CHARACTER_CONTRACT_VERSION,
                    "digest_key": "character_contract",
                }
            )
            inline_digests["character_contract"] = character_contract

        narrative_state = self._narrative_state_digest(scene)
        if narrative_state:
            inline_digests["narrative_state"] = narrative_state

        info_asymmetry = self._information_asymmetry_digest(scene)
        if info_asymmetry:
            inline_digests["information_asymmetry"] = info_asymmetry

        chapter_transition = self._chapter_transition_buffer(scene)
        if chapter_transition:
            inline_digests["chapter_transition_buffer"] = chapter_transition

        # v2（规格 §1.3）：前文声音锚 / 漂移校准——只对 style_draft 可见
        # （context_budget.NEUTRAL_DRAFT_STYLE_SECTIONS 让中性稿看不到）。
        voice_anchor = self._previous_scene_voice_anchor(scene)
        if voice_anchor is not None:
            source_version_refs["previous_scene_voice_anchor_scene_id"] = voice_anchor[
                "source_scene_id"
            ]
            source_version_refs["previous_scene_voice_anchor_draft_row_id"] = (
                voice_anchor["source_draft_row_id"]
            )
            source_version_refs["previous_scene_voice_anchor_stage"] = voice_anchor[
                "source_stage"
            ]
            ordered_injections.append(
                {
                    "slot": VOICE_ANCHOR_SECTION_KEY,
                    "ref_id": voice_anchor["source_draft_row_id"],
                    "digest_key": VOICE_ANCHOR_SECTION_KEY,
                }
            )
            inline_digests[VOICE_ANCHOR_SECTION_KEY] = voice_anchor["text"]

        drift_lines = self._style_drift_calibration(
            scene,
            source_version_refs=source_version_refs,
            inline_digests=inline_digests,
        )
        if drift_lines:
            source_version_refs["style_drift_calibration_line_count"] = len(drift_lines)
            source_version_refs["style_drift_calibration_before_scene_seq"] = (
                scene.scene_seq
            )
            ordered_injections.append(
                {
                    "slot": DRIFT_CALIBRATION_SECTION_KEY,
                    "ref_id": f"{scene.chapter_id}:before_seq_{scene.scene_seq}",
                    "digest_key": DRIFT_CALIBRATION_SECTION_KEY,
                }
            )
            inline_digests[DRIFT_CALIBRATION_SECTION_KEY] = "\n".join(
                f"- {line}" for line in drift_lines
            )

        similar_scenes = self._similar_scene_context(scene)
        if similar_scenes:
            inline_digests["similar_scene"] = similar_scenes

        if previous_memory:
            source_version_refs["scene_memory_prev"] = previous_memory.scene_id
            ordered_injections.append(
                {
                    "slot": "prev_scene_memory",
                    "ref_id": previous_memory.scene_id,
                    "digest_key": "scene_memory",
                }
            )
            inline_digests["scene_memory"] = previous_memory.content

        freshness_budget = self._literary_freshness_budget(scene)
        if freshness_budget is not None:
            source_version_refs["literary_freshness_source_final_scene_ids"] = (
                freshness_budget["source_final_scene_ids"]
            )
            ordered_injections.append(
                {
                    "slot": "literary_freshness_budget",
                    "ref_id": scene.chapter_id,
                    "digest_key": "literary_freshness_budget",
                }
            )
            inline_digests["literary_freshness_budget"] = json.dumps(
                freshness_budget["budget"],
                ensure_ascii=False,
                sort_keys=True,
            )

        author_preference_profiles = self._approved_runtime_author_preference_profiles(
            scene, chapter
        )
        if author_preference_profiles:
            author_preference_profile = author_preference_profiles[-1]
            merged_preference: dict[str, Any] = {}
            for row in author_preference_profiles:
                merged_preference = merge_preference_summaries(
                    merged_preference, row.summary_json or {}
                )
            runtime_preference = safe_preference_summary_for_prompt(merged_preference)
            profile_ids = [row.profile_id for row in author_preference_profiles]
            source_version_refs["author_preference_profile_id"] = (
                author_preference_profile.profile_id
            )
            source_version_refs["author_preference_profile_ids"] = profile_ids
            source_version_refs["author_preference_profile_updated_at"] = (
                author_preference_profile.updated_at
            )
            ordered_injections.append(
                {
                    "slot": "author_preference_profile",
                    "ref_id": author_preference_profile.profile_id,
                    "digest_key": "author_preference_profile",
                }
            )
            inline_digests["author_preference_profile"] = json.dumps(
                {
                    "profile_id": author_preference_profile.profile_id,
                    "profile_ids": profile_ids,
                    "kind": "approved_author_preference_profile",
                    "summary": runtime_preference,
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        scene_summary = self.resolver.resolve_scene_summary(self.session, scene)
        if scene_summary:
            source_version_refs["scene_summary_id"] = scene_summary.scene_id
            ordered_injections.append(
                {
                    "slot": "scene_summary",
                    "ref_id": scene_summary.scene_id,
                    "digest_key": "scene_summary",
                }
            )
            inline_digests["scene_summary"] = scene_summary.content

        chapter_summary = self.resolver.resolve_chapter_summary(self.session, scene)
        if chapter_summary:
            source_version_refs["chapter_summary_id"] = chapter_summary.chapter_id
            ordered_injections.append(
                {
                    "slot": "chapter_summary",
                    "ref_id": chapter_summary.chapter_id,
                    "digest_key": "chapter_summary",
                }
            )
            inline_digests["chapter_summary"] = chapter_summary.content

        # §2 summary tower: far-horizon volume atmosphere (read-only, NOT a fact source)
        volume_summary = self._latest_volume_summary(scene)
        if volume_summary is not None:
            source_version_refs["volume_summary_row_id"] = volume_summary.row_id
            ordered_injections.append(
                {
                    "slot": "volume_summary",
                    "ref_id": volume_summary.row_id,
                    "digest_key": "volume_summary",
                }
            )
            inline_digests["volume_summary"] = (
                "【卷级远景氛围 — 仅供语气/基调延续，严禁当作事实来源；事实一律以权威状态为准】\n"
                + (volume_summary.atmosphere_summary or "")
            )

        projection = BundleSnapshotHashProjection(
            contract_version="BSHASH_v1",
            stage_allowlist_name="bundle_build_allowlist_v1",
            source_version_refs=source_version_refs,
            resolved_ref_ids={
                "relation_ids": (
                    [relation_profile.relation_profile_id] if relation_profile else []
                ),
            },
            ordered_injections=ordered_injections,
            inline_digests=inline_digests,
        )
        bundle_hash = compute_bundle_hash_projection(projection)
        bundle_id, build_count = self._next_bundle_id(scene.scene_id, state)
        snapshot = projection.model_dump(mode="json")
        snapshot["scene_id"] = scene.scene_id
        snapshot["chapter_id"] = scene.chapter_id
        # 审计 P-11：降级槽位随快照可见（hash 之后追加——不参与 bundle_snapshot_hash，
        # 与 scene_id/chapter_id 同一约定）。
        if self._degraded_slots:
            snapshot["degraded_slots"] = sorted(self._degraded_slots)

        bundle = SceneBundle(
            bundle_id=bundle_id,
            scene_id=scene.scene_id,
            chapter_id=scene.chapter_id,
            execution_mode=execution_mode,
            bundle_snapshot_hash=bundle_hash,
            frozen_snapshot_json=snapshot,
        )
        self.session.add(bundle)

        state.current_bundle_id = bundle_id
        state.current_bundle_hash = bundle_hash
        state.bundle_build_count = build_count
        state.scene_status = "bundle_built"
        self.session.flush()

        return {
            "bundle_id": bundle_id,
            "bundle_snapshot_hash": bundle_hash,
            "snapshot": snapshot,
        }

    def _latest_planning_artifact(
        self,
        *,
        artifact_type: str,
        object_type: str,
        object_id: str,
    ) -> GenerationPlanningArtifact | None:
        return (
            self.session.execute(
                select(GenerationPlanningArtifact)
                .where(
                    GenerationPlanningArtifact.artifact_type == artifact_type,
                    GenerationPlanningArtifact.object_type == object_type,
                    GenerationPlanningArtifact.object_id == object_id,
                    GenerationPlanningArtifact.status == "active",
                )
                .order_by(
                    GenerationPlanningArtifact.created_at.desc(),
                    GenerationPlanningArtifact.row_id.desc(),
                )
            )
            .scalars()
            .first()
        )


    def _previous_scene_voice_anchor(self, scene: SceneCard) -> dict[str, Any] | None:
        """规格 §1.3「前文声音锚」；失败只记降级槽，不阻断 bundle。"""
        try:
            return previous_scene_voice_anchor(self.session, scene)
        except Exception:  # noqa: BLE001 — optional continuity aid degrades visibly
            self._slot_degraded(VOICE_ANCHOR_SECTION_KEY, scene)
            return None

    def _style_drift_calibration(
        self,
        scene: SceneCard,
        *,
        source_version_refs: dict[str, Any] | None = None,
        inline_digests: dict[str, str] | None = None,
    ) -> list[str]:
        """规格 §1.3「漂移校准」：读 W6 的 ``style_drift_observed`` 事件。

        主路径 ``latest_drift_event``（本章更早场景最近一次；同章没有退到上一章）——拿到
        校准行、``event_id`` 与 few-shot 段型优先级；事件缺失时退到 W5 约定的
        ``latest_drift_calibration``（list[str] 读取端，供外部替换 / 测试注入）。有行时把
        ``style_drift_calibration_event_id`` / ``…_source_scene_id`` / ``…_source_scope`` /
        ``…_ptype_priority`` 记进 ``source_version_refs``，并把段型优先级以 JSON 字符串写进
        ``inline_digests["_drift_ptype_priority"]``（bundle hash 投影要求 digest 值为 str，
        scene_generation 消费该键）。返回的行已去重并按 ``drift_calibration_max_lines`` 截断。
        """
        event: dict[str, Any] | None = None
        try:
            event = latest_drift_event(self.session, scene.chapter_id, scene.scene_seq)
            if isinstance(event, dict):
                raw_lines = event.get("calibration_lines")
            else:
                event = None
                raw_lines = latest_drift_calibration(
                    self.session, scene.chapter_id, scene.scene_seq
                )
        except Exception:  # noqa: BLE001 — optional continuity aid degrades visibly
            self._slot_degraded(DRIFT_CALIBRATION_SECTION_KEY, scene)
            return []
        if not isinstance(raw_lines, (list, tuple)):
            return []
        max_lines = load_continuity_budget()["drift_calibration_max_lines"]
        lines: list[str] = []
        for item in raw_lines:
            text = " ".join(str(item or "").split())
            if text and text not in lines:
                lines.append(text)
            if len(lines) >= max_lines:
                break
        if lines and event is not None:
            self._record_drift_event_refs(
                event,
                source_version_refs=source_version_refs,
                inline_digests=inline_digests,
            )
        return lines

    @staticmethod
    def _record_drift_event_refs(
        event: dict[str, Any],
        *,
        source_version_refs: dict[str, Any] | None,
        inline_digests: dict[str, str] | None,
    ) -> None:
        """把漂移事件的审计引用与段型优先级写进快照（``_style_drift_calibration`` 的直接 helper）。"""
        event_id = str(event.get("event_id") or "")
        priority = [
            str(ptype)
            for ptype in (event.get("drift_ptype_priority") or [])
            if str(ptype or "").strip()
        ]
        if source_version_refs is not None:
            if event_id:
                source_version_refs["style_drift_calibration_event_id"] = event_id
            source_scene_id = event.get("scene_id")
            if source_scene_id:
                source_version_refs["style_drift_calibration_source_scene_id"] = str(source_scene_id)
            source_scope = event.get("source_scope")
            if source_scope:
                source_version_refs["style_drift_calibration_source_scope"] = str(source_scope)
            if priority:
                source_version_refs["style_drift_calibration_ptype_priority"] = list(priority)
        if inline_digests is not None and priority:
            inline_digests["_drift_ptype_priority"] = json.dumps(priority, ensure_ascii=False)

    def _narrative_state_digest(self, scene: SceneCard) -> str | None:
        """Inject authoritative character state from event log into the prompt."""
        try:
            from novel_system.services.canon_continuity import CanonContinuityService
            from novel_system.services.narrative_event_log import NarrativeEventLog

            log = NarrativeEventLog(self.session)
            project_id = require_scene_project_id(self.session, scene)
            # Wave 4（§5.6）：传 pov_character_id → format_state_for_prompt 委派
            # PovKnowledgeProjection 做减法投影，隐藏非 POV 秘密内容（硬 QC 仍读全量）。
            text = log.format_state_for_prompt(
                project_id,
                None,
                scene_id=scene.scene_id,
                pov_character_id=scene.pov_character_id,
                onstage_character_ids=scene.onstage_chars_json,
            )
            checkpoint = CanonContinuityService(
                self.session
            ).format_recent_checkpoint_for_prompt(
                project_id,
                scene.scene_id,
                pov_character_id=scene.pov_character_id,
            )
            parts = [part for part in (text, checkpoint) if part]
            return "\n\n".join(parts) if parts else None
        except Exception:
            self._slot_degraded("narrative_state", scene)
            return None

    def _literary_freshness_budget(self, scene: SceneCard) -> dict[str, Any] | None:
        rows = (
            self.session.execute(
                select(FinalScene)
                .join(SceneCard, SceneCard.scene_id == FinalScene.scene_id)
                .where(
                    FinalScene.chapter_id == scene.chapter_id,
                    # Wave 1 词表统一：archived 是归档事务写入的权威成稿态，必须与旧值并列
                    FinalScene.status.in_(("approved", "near_final_ready", "archived")),
                    SceneCard.trashed_flag == 0,
                    SceneCard.scene_seq < scene.scene_seq,
                )
                .order_by(
                    SceneCard.scene_seq.asc(),
                    FinalScene.created_at.asc(),
                    FinalScene.row_id.asc(),
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None

        source_rows = rows[-3:]
        combined_text = "\n".join(row.content or "" for row in source_rows)
        fingerprint = fingerprint_literary_quality(combined_text)
        action_templates = [
            row["value"]
            for row in fingerprint.get("action_templates", [])
            if int(row.get("count") or 0) >= 2
        ]
        image_fields = [
            row["value"]
            for row in fingerprint.get("image_fields", [])
            if int(row.get("count") or 0) >= 2
        ]
        syntax_shapes = [
            row["value"]
            for row in fingerprint.get("syntax_shapes", [])
            if int(row.get("count") or 0) >= 3
        ]
        # 2026-09-12 风格直起:契约先解析——style_first 下写死的两张房风词表与「以动作而非
        # 解释收尾」子句整体让位;跨场景动作模板 / 意象场 / 句形复用(防系统自我复读)保留。
        try:
            contract = resolve_scene_style_runtime_contract(self.session, scene)
        except Exception:  # noqa: BLE001 — 豁免标记是可选增强，解析失败按无契约处理
            _LOGGER.debug("freshness budget contract lookup degraded", exc_info=True)
            contract = None
        style_bound = (
            contract is not None and str(contract.get("draft_mode") or "") == "style_first"
        )
        preserve_repetition = contract is not None and contract_deliberate_repetition(contract)
        budget: dict[str, Any] = {
            "schema_version": "literary_freshness_budget_v1",
            "source_scene_ids": [row.scene_id for row in source_rows],
        }
        if style_bound:
            # 2026-09-14 风格保真修补:动作模板 / 意象场 / 句形三张表是从本系统自己已按作者手笔写成的
            # 前几场里挖出来的——有绑定时它们就是作者的声音,不再当作要避开的东西发给起草;只保留
            # 内容级复读检查(近场 n-gram、语义复读、全书禁用表达)。
            budget["house_taste_lists"] = "deferred_to_reference"
            budget["voice_lists"] = "deferred_to_reference"
            budget["instruction"] = (
                "Use this as a freshness budget against repeating your own earlier scenes' content only: "
                "do not reuse the recent n-grams or the semantic beats listed here. The reference "
                "author's habits, cadence, syntax shapes, image fields, and closing moves are never repetition to avoid."
            )
        else:
            budget["avoid_action_templates"] = action_templates
            budget["avoid_image_fields"] = image_fields[:6]
            budget["vary_syntax_shapes"] = syntax_shapes[:5]
            budget["avoid_false_clarity"] = ["她知道", "他知道", "忽然意识到", "突然意识到"]
            budget["avoid_summary_endings"] = [
                "这意味着",
                "一切都变了",
                "事情从此不同",
                "解释了一切",
            ]
            budget["instruction"] = (
                "Use this as a freshness budget: do not repeat high-frequency action templates, "
                "rotate image fields, and end on a hard action instead of explanation."
            )
        try:
            from novel_system.services.self_repetition import (
                SelfRepetitionDetector,
                format_semantic_repetition_guidance,
            )

            detector = SelfRepetitionDetector(self.session)
            repeated_ngrams = detector.top_repeated_ngrams(
                scene.chapter_id, lookback_scenes=6, top_n=8
            )
            if repeated_ngrams:
                budget["avoid_recent_ngrams"] = repeated_ngrams
            corpus_texts, corpus_ids = detector._load_corpus(
                scene.scene_id, scene.chapter_id, lookback_scenes=6
            )
            if corpus_texts:
                from novel_system.services.self_repetition import (
                    check_semantic_repetition,
                )

                sem_hits = check_semantic_repetition(
                    scene.scene_goal or scene.hook or "",
                    corpus_texts,
                    corpus_ids,
                )
                if sem_hits:
                    budget["semantic_repetition_alert"] = (
                        format_semantic_repetition_guidance(sem_hits)
                    )
            # §9 blueprint: whole-book banned expression list (LifetimeExpressionRegistry)
            from novel_system.services.self_repetition import LifetimeExpressionRegistry

            lifetime_reg = LifetimeExpressionRegistry(self.session)
            lifetime_guidance = lifetime_reg.get_lifetime_avoidance_guidance(
                scene.project_id
            )
            # style_first 且参考刻意复沓时,全书禁用表达表也让位(它会把作者的口头禅当成滥用)。
            if lifetime_guidance and not (style_bound and preserve_repetition):
                budget["lifetime_banned_expressions"] = lifetime_guidance
        except Exception:
            self._slot_degraded("literary_freshness_enrichment", scene)
        # v2（规格 §2.W6.3）新鲜度豁免：
        # (a) 仅由虚词（function_words.yaml 全部组）与标点 / 空白组成的 avoid_* / vary_* 条目剔除——
        #     它们是作者节拍而非内容模板，压掉会抹平参考的声音；
        # (b) 任一层画像标记 deliberate_repetition 时加 preserve_reference_repetition 布尔键，
        #     style_draft 模板据此保留参考的刻意复沓。无契约时预算不变。
        _prune_function_word_only_entries(budget)
        if preserve_repetition:
            # 只放布尔标记：这份预算 neutral_draft 也会看到，而「保留参考的刻意复沓」
            # 是语言层的目标文风指令，规格 §1.3 只允许 style_draft 模板解释它
            # （config/prompts.yaml ``style_draft`` 已有对应措辞），不能把措辞写进 instruction。
            budget["preserve_reference_repetition"] = True
        return {
            "source_final_scene_ids": [row.row_id for row in source_rows],
            "budget": budget,
        }

    def _latest_volume_summary(self, scene: SceneCard) -> VolumeSummary | None:
        """§2: most recent active volume atmosphere summary for the scene's project."""
        project_id = scene.project_id
        if not project_id:
            return None
        return (
            self.session.execute(
                select(VolumeSummary)
                .where(
                    VolumeSummary.project_id == project_id,
                    VolumeSummary.active_flag == 1,
                    VolumeSummary.runtime_eligible == 1,
                )
                .order_by(VolumeSummary.volume_seq.desc(), VolumeSummary.row_id.desc())
            )
            .scalars()
            .first()
        )

    def _approved_runtime_author_preference_profiles(
        self,
        scene: SceneCard,
        chapter: ChapterGoal,
    ) -> list[AuthorPreferenceProfile]:
        project_id = scene.project_id or chapter.project_id
        project = self.session.get(StoryProject, project_id) if project_id else None
        scopes: list[tuple[str, str]] = [("global", "global")]
        genre = (
            " ".join(str(project.genre or "").strip().lower().split())
            if project
            else ""
        )
        if genre:
            scopes.append(("genre", genre[:120]))
        if project_id:
            scopes.append(("project", project_id))
        scopes.append(("chapter", chapter.chapter_id))
        rows: list[AuthorPreferenceProfile] = []
        for scope_type, scope_ref_id in scopes:
            rows.extend(
                self.session.execute(
                    select(AuthorPreferenceProfile)
                    .where(
                        AuthorPreferenceProfile.scope_type == scope_type,
                        AuthorPreferenceProfile.scope_ref_id == scope_ref_id,
                        AuthorPreferenceProfile.status == "approved",
                        AuthorPreferenceProfile.runtime_eligible == 1,
                    )
                    .order_by(
                        AuthorPreferenceProfile.updated_at.asc(),
                        AuthorPreferenceProfile.profile_id.asc(),
                    )
                )
                .scalars()
                .all()
            )
        return rows


    def _chapter_transition_buffer(self, scene: SceneCard) -> str | None:
        """Blueprint §3: inject last 500-1000 chars of previous chapter as continuity anchor.

        Only fires for the first scene of a chapter (scene_seq == 1).
        Uses ChapterGoal.display_order (or chapter_id alphabetical) to find the previous chapter.
        """
        try:
            if scene.scene_seq and scene.scene_seq > 1:
                return None
            current_chapter = self.session.get(ChapterGoal, scene.chapter_id)
            if current_chapter is None:
                return None
            current_order = current_chapter.display_order
            if current_order is not None:
                prev_chapter = (
                    self.session.execute(
                        select(ChapterGoal)
                        .where(
                            ChapterGoal.project_id == scene.project_id,
                            ChapterGoal.display_order < current_order,
                        )
                        .order_by(ChapterGoal.display_order.desc())
                    )
                    .scalars()
                    .first()
                )
            else:
                prev_chapter = (
                    self.session.execute(
                        select(ChapterGoal)
                        .where(
                            ChapterGoal.project_id == scene.project_id,
                            ChapterGoal.chapter_id < scene.chapter_id,
                        )
                        .order_by(ChapterGoal.chapter_id.desc())
                    )
                    .scalars()
                    .first()
                )
            if prev_chapter is None:
                return None
            last_final = (
                self.session.execute(
                    select(FinalScene)
                    .join(SceneCard, SceneCard.scene_id == FinalScene.scene_id)
                    .where(
                        FinalScene.chapter_id == prev_chapter.chapter_id,
                        FinalScene.status.in_(
                            ("approved", "near_final_ready", "archived")
                        ),
                        SceneCard.trashed_flag == 0,
                    )
                    .order_by(SceneCard.scene_seq.desc(), FinalScene.created_at.desc())
                )
                .scalars()
                .first()
            )
            if last_final and last_final.content:
                tail = last_final.content[-800:]
                return f"## Chapter Transition Buffer (previous chapter ending — maintain tone continuity)\n\n{tail}"
            return None
        except Exception:
            self._slot_degraded("chapter_transition_buffer", scene)
            return None

    def _similar_scene_context(self, scene: SceneCard) -> str | None:
        """Blueprint §3 Track 3: semantic retrieval for atmosphere/echo material."""
        try:
            # 审计 P-7 关联：统一走 get_vector_store()（memory=进程级单例 / chroma=持久化）。
            # 行为保持"每次由 DB 重建集合再查询"——自包含且结果始终新鲜。
            from novel_system.services.vector_store import get_vector_store

            project_id = require_scene_project_id(self.session, scene)
            collection_name = f"scenes_{project_id}"
            store = get_vector_store()
            approved_scenes = (
                self.session.execute(
                    select(FinalScene)
                    .join(SceneCard, SceneCard.scene_id == FinalScene.scene_id)
                    .where(
                        FinalScene.status.in_(
                            ("approved", "near_final_ready", "archived")
                        ),
                        SceneCard.trashed_flag == 0,
                        SceneCard.scene_id != scene.scene_id,
                        SceneCard.project_id == project_id,
                    )
                    .order_by(SceneCard.scene_seq.asc())
                )
                .scalars()
                .all()
            )
            if not approved_scenes or len(approved_scenes) < 2:
                return None
            documents = [
                {"id": fs.scene_id, "text": (fs.content or "")[:600]}
                for fs in approved_scenes
                if fs.content
            ]
            if not documents:
                return None
            store.write_collection(collection_name, documents)
            query_text = scene.scene_goal or ""
            if scene.location:
                query_text += f" {scene.location}"
            results = store.query(collection_name, query_text, top_k=2)
            if not results:
                return None
            lines = [
                "## Similar Scene Context (§3 Track 3 — inspiration only, NOT fact-authoritative)",
                "These excerpts are for atmosphere/echo reference. They may be imprecise.",
                "Do NOT copy facts, character states, or plot points from them.",
                "Use them only for tonal resonance, imagery contrast, or emotional echoing.",
            ]
            for item in results:
                lines.append(
                    f"\n[scene {item.get('id', '?')}]\n{item.get('text', '')[:400]}"
                )
            return "\n".join(lines)
        except Exception:
            self._slot_degraded("similar_scene", scene)
            return None


    def _information_asymmetry_digest(self, scene: SceneCard) -> str | None:
        """Blueprint §2/§11: inject information gaps between onstage characters."""
        try:
            from novel_system.services.narrative_event_log import NarrativeEventLog

            log = NarrativeEventLog(self.session)
            project_id = require_scene_project_id(self.session, scene)
            onstage = scene.onstage_chars_json or []
            if len(onstage) < 2:
                return None
            # Wave 4（§5.6）：写作提示词走 POV 减法投影——传 pov 后，他人秘密/错误信念
            # 内容被抑制，只保留 POV 独有认知与内容无关的盲区提示。
            text = log.information_asymmetry_digest(
                project_id,
                None,
                onstage,
                scene_id=scene.scene_id,
                pov_character_id=scene.pov_character_id,
            )
            return text if text else None
        except Exception:
            self._slot_degraded("information_asymmetry", scene)
            return None
