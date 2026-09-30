"""冻结一场的起草上下文（bundle）：按固定顺序登记各段，算 BSHASH_v1 哈希，存成 SceneBundle。

段落的登记顺序与存库字节由 ``bundle_sections.BundleSections`` 管；跨场景的连续性（上一章、章间过渡、前文声音锚、
上一场结尾节选）在 ``bundle_continuity``，前文声音锚读哪一稿在 ``bundle_draft_lineage``，新鲜度预算在
``bundle_freshness``（B03-19 拆出）。旧名字照旧从本模块转出。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from novel_system.contracts.bundle import BundleSnapshotHashProjection
from novel_system.db.models import (
    GenerationPlanningArtifact,
    SceneBundle,
    SceneCard,
    SceneMemory,
    SceneRunState,
    StoryCharacter,
    VolumeSummary,
    StyleReferenceBook,
)
from novel_system.services.bundle_continuity import (
    REFERENCE_FIRST_MEMORY_NOTE,
    VOICE_ANCHOR_SECTION_KEY,
    chapter_transition_text,
    load_continuity_budget,
    previous_scene_voice_anchor,
    reference_first_memory_digest,
)
from novel_system.services.bundle_draft_lineage import latest_styled_draft_for_scene
from novel_system.services.bundle_freshness import (
    _is_function_word_only,
    _prune_function_word_only_entries,
    literary_freshness_budget,
)
from novel_system.services.bundle_sections import BundleSections
from novel_system.services.hash_engine import compute_bundle_hash_projection, sha256_text
from novel_system.services.resolver import Resolver
from novel_system.services.character_continuity import (
    CHARACTER_CONTRACT_VERSION,
    build_character_contract_digest,
)
from novel_system.services.scene_digest import scene_card_digest
from novel_system.services.scene_ownership import require_scene_project_id
from novel_system.services.scene_sections import attach_scene_sections
from novel_system.services.style_reference.budget_config import injection_budget
from novel_system.services.style_reference.inject.bindings import (
    ordered_character_ids,
    resolve_binding_layers,
)
from novel_system.services.style_reference.narrative_guidance import (
    NARRATIVE_GUIDANCE_SECTION_KEY,
    collect_narrative_guidance,
    render_narrative_section,
)
from novel_system.services.style_policy import (
    MODE_FROZEN,
    UNBOUND,
    StylePolicy,
    policy_from_contract,
)
from novel_system.services.style_reference.policy import decide_reference_route
from novel_system.services.style_reference.repository import StyleReferenceRepository
from novel_system.services.style_reference.runtime_contract import (
    STYLE_RUNTIME_CONTRACT_VERSION,
    build_style_runtime_contract,
    contract_layer,
)
from novel_system.services.style_reference.structure import reference_scene_scale
from novel_system.services.style_prompt_injection import SCENE_SITUATION_TAGS_KEY
from novel_system.services.style_reference.tags import normalize_situation_tags
from novel_system.services.writer_briefs import (
    normalize_chapter_writer_brief,
    normalize_scene_writer_brief,
    writer_brief_has_content,
)
from novel_system.services.author_instructions import normalize_author_note
from novel_system.services.scene_lookup import get_chapter_or_404, get_scene_or_404
from novel_system.services.planning_queries import (
    latest_active_planning_artifact,
    latest_scene_blueprint,
)

# 调用方与测试照旧从本模块取的名字（build_style_runtime_contract：build 经本模块的全局名调用，测试替换它）
__all__ = [
    "BUNDLE_REFERENCE_NODE_IDS",
    "BundleBuilder",
    "REFERENCE_FIRST_MEMORY_NOTE",
    "SCENE_SITUATION_TAGS_KEY",
    "VOICE_ANCHOR_SECTION_KEY",
    "_is_function_word_only",
    "_prune_function_word_only_entries",
    "build_style_runtime_contract",
    "latest_styled_draft_for_scene",
    "load_continuity_budget",
    "previous_scene_voice_anchor",
    "reference_first_memory_digest",
    "resolve_scene_style_runtime_contract",
]

_LOGGER = logging.getLogger(__name__)


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
    layers = resolve_binding_layers(
        session,
        getattr(scene, "project_id", None),
        task_type,
        character_ids=ordered_character_ids(
            getattr(scene, "pov_character_id", None), getattr(scene, "onstage_chars_json", None)
        ),
        scene_id=getattr(scene, "scene_id", None),
    )
    if not layers:
        return None
    return build_style_runtime_contract(
        StyleReferenceRepository(session),
        layers,
        task_type=task_type,
    )


# 读 bundle 段的节点（起草 / 事实 QC / 参考评审 / 补丁 / 准定稿评审与改写）：bundle 里由参考书派生的段要对它们全都放行
BUNDLE_REFERENCE_NODE_IDS: tuple[str, ...] = (
    "neutral_draft",
    "style_draft",
    "style_patch",
    "hard_qc",
    "soft_qc",
    "near_final_acceptance_review",
    "scene_literary_rewrite",
)


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
        *,
        author_note: str | None = None,
    ) -> dict[str, Any]:
        self._degraded_slots = set()
        scene = get_scene_or_404(self.session, scene_id)
        chapter = get_chapter_or_404(self.session, scene.chapter_id)
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

        sections = BundleSections()
        sections.ref("chapter_goal", chapter.chapter_id)
        sections.ref("scene_card", scene.scene_id)
        style_character_ids = ordered_character_ids(scene.pov_character_id, scene.onstage_chars_json)
        reference_resolution_degraded = False
        try:
            reference_layers = resolve_binding_layers(
                self.session,
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
            sections.ref("reference_profile_ids", reference_profile_ids)
        sections.add("chapter_goal", ref_id=chapter.chapter_id, text=chapter.chapter_goal)
        sections.add("scene_card", ref_id=scene.scene_id, text=scene_card_digest(scene))
        # 2026-09-13 阶段 A：雪花 / 章节编排写下的场景结构（形态、坩埚、三拍、代价）直读
        # 原始键进入 bundle，作为与 scene_card 同级的事实 section。此前它只经 v2 简报的
        # 归一化通道到达写作，而那条通道会把这些键全部丢掉——起草模型从未见过作者的三拍。
        # 2026-09-13 阶段 F：已确认的雪花设计背景（02 / 03 / 04 / 06 与章表、相邻两场）紧随结构简报。
        # 它是背景不是事实：预算紧时被压缩 / 省略，硬 QC 不看；引用的步骤版本进 source_version_refs，
        # 设计一改，bundle 哈希就变。两段的登记次序与 sections.add 相同（来源引用 → 注入顺序 → 正文）。
        attach_scene_sections(
            scene,
            self.session,
            refs=sections.source_version_refs,
            injections=sections.ordered_injections,
            digests=sections.inline_digests,
        )
        # 2026-09-22 风格参考优先:契约写了 style_first 时,本系统自己的前文不再作为「声音」进入提示
        # (前文声音锚 / 整篇上一场正文)——第 1 场若跑偏,后面每一场都被要求接着那个腔写。
        # 风格参考 v3:契约每个 bundle 只建一次,这一次的 StylePolicy 管本 bundle 里所有让位判定(含新鲜度预算)。
        bundle_policy: StylePolicy = UNBOUND
        reference_first = False
        sections.ref("style_reference_runtime_contract_version", STYLE_RUNTIME_CONTRACT_VERSION)
        sections.ref(
            "style_reference_runtime_contract_status",
            "degraded"
            if reference_resolution_degraded
            else ("frozen" if reference_layers else "absent"),
        )
        if reference_layers:
            try:
                style_runtime_contract = build_style_runtime_contract(
                    StyleReferenceRepository(self.session),
                    reference_layers,
                    task_type="scene_generation",
                )
                if style_runtime_contract is not None:
                    bundle_policy = policy_from_contract(style_runtime_contract, mode=MODE_FROZEN)
                    reference_first = bundle_policy.defers_house_taste()
                    sections.ref("style_reference_runtime_contract_hash", style_runtime_contract["contract_hash"])
                    # 风格参考 v3 复核（H1）：bundle 里由这本书派生的段（叙事机制指引、参考尺度）会进读 bundle
                    # 的每一个节点的提示——「仅本机」的书只要其中有一个节点走云端路由，这些段一概不进 bundle
                    # （起草 / 评审节点渲染参考时会按自己的路由 409，作者看得到原因）。
                    bundle_route = self._bundle_reference_route(style_runtime_contract)
                    book_sections_allowed = bundle_route is None or bundle_route.send_book
                    if not book_sections_allowed:
                        sections.ref(
                            "style_reference_bundle_sections",
                            f"withheld:{bundle_route.reason or 'cloud_policy'}",
                        )
                    # 2026-09-22 结构跟随参考书:style_first 下按参考作者的章长与本章的场数推算
                    # 「这位作者的一场多长」,起草通道据此把硬范围上限抬到参考尺度(见
                    # scene_generation.length_policy._parse_numeric_length_band)。以 _ 开头:不进 section。
                    if reference_first and book_sections_allowed:
                        scene_scale = self._reference_scene_scale(scene, style_runtime_contract)
                        if scene_scale:
                            sections.ref("style_reference_scene_scale", int(scene_scale["derived_scene_chars"]))
                            sections.digest(
                                "_style_reference_scene_scale",
                                json.dumps(scene_scale, ensure_ascii=False, sort_keys=True),
                            )
                    sections.ref("reference_binding_ids", style_runtime_contract["binding_ids"])
                    sections.digest(
                        "_style_reference_runtime_contract",
                        json.dumps(
                            style_runtime_contract,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    )
                    # v2（规格 §1.3）：叙事机制指引——neutral_draft 与 style_draft 都可见。
                    narrative_lines = (
                        collect_narrative_guidance(style_runtime_contract) if book_sections_allowed else []
                    )
                    narrative_text = render_narrative_section(narrative_lines)
                    if narrative_text:
                        sections.add(
                            NARRATIVE_GUIDANCE_SECTION_KEY,
                            ref_id=style_runtime_contract["contract_hash"],
                            text=narrative_text,
                            refs={
                                "style_narrative_guidance_contract_hash": style_runtime_contract["contract_hash"],
                                "style_narrative_guidance_line_count": len(narrative_lines),
                            },
                        )
            except Exception:  # noqa: BLE001 — optional style layer degrades visibly
                sections.ref("style_reference_runtime_contract_status", "degraded")
                self._slot_degraded("style_reference_runtime_contract", scene)
        normalized_author_note = normalize_author_note(author_note)
        if normalized_author_note:
            instruction_hash = sha256_text(normalized_author_note)
            sections.add(
                "author_instruction",
                ref_id=f"author_instruction:{instruction_hash}",
                text=normalized_author_note,
                refs={"author_instruction_hash": instruction_hash},
            )
        chapter_writer_brief = normalize_chapter_writer_brief(chapter.writer_brief_json)
        if writer_brief_has_content(chapter_writer_brief):
            sections.add(
                "chapter_writer_brief",
                ref_id=chapter.chapter_id,
                text=json.dumps(chapter_writer_brief, ensure_ascii=False, sort_keys=True),
                refs={"chapter_writer_brief": chapter.chapter_id},
            )
        scene_writer_brief = normalize_scene_writer_brief(scene.writer_brief_json)
        if writer_brief_has_content(scene_writer_brief):
            sections.add(
                "scene_writer_brief",
                ref_id=scene.scene_id,
                text=json.dumps(scene_writer_brief, ensure_ascii=False, sort_keys=True),
                refs={"scene_writer_brief": scene.scene_id},
            )

        scene_blueprint = latest_scene_blueprint(self.session, scene.scene_id)
        if scene_blueprint is not None:
            sections.add(
                "scene_blueprint",
                ref_id=scene_blueprint.row_id,
                text=json.dumps(scene_blueprint.blueprint_json or {}, ensure_ascii=False, sort_keys=True),
                refs={"scene_blueprint_row_id": scene_blueprint.row_id},
            )
            # 风格参考 v3（N4）：事实版蓝图给这一场标的场面标签随 bundle 冻结（以 _ 开头：不进 section），
            # 选窗按它挑参考作者写同类场面的原文（同一场所有工序读同一份）。
            situation_tags = normalize_situation_tags(
                (scene_blueprint.blueprint_json or {}).get("situation_tags")
                if isinstance(scene_blueprint.blueprint_json, dict)
                else None
            )
            if situation_tags:
                sections.ref("scene_situation_tags", situation_tags)
                sections.digest(SCENE_SITUATION_TAGS_KEY, json.dumps(situation_tags, ensure_ascii=False))

        character_pressure = self._latest_planning_artifact(
            artifact_type="character_pressure_blueprint",
            object_type="scene",
            object_id=scene.scene_id,
        )
        if character_pressure is not None:
            sections.add(
                "character_pressure",
                ref_id=character_pressure.row_id,
                text=json.dumps(character_pressure.payload_json or {}, ensure_ascii=False, sort_keys=True),
                refs={"character_pressure_artifact_row_id": character_pressure.row_id},
            )

        chapter_architecture = self._latest_planning_artifact(
            artifact_type="chapter_story_architecture",
            object_type="chapter",
            object_id=scene.chapter_id,
        )
        if chapter_architecture is not None:
            sections.add(
                "chapter_story_architecture",
                ref_id=chapter_architecture.row_id,
                text=json.dumps(chapter_architecture.payload_json or {}, ensure_ascii=False, sort_keys=True),
                refs={"chapter_story_architecture_artifact_row_id": chapter_architecture.row_id},
            )

        # 声线卡 / 关系卡不再进 bundle（批准#15，重评 R8）：产品里没有任何地方能写这两类卡，实库两张表都是空的；
        # 角色的声音与关系来自构思（Scene Design Context 里的 POV 角色摘要、价值观、视角故事、同场角色）。

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
            display_names=character_display_names,
        )
        if character_contract:
            sections.add(
                "character_contract",
                ref_id=CHARACTER_CONTRACT_VERSION,
                text=character_contract,
                refs={"character_contract": CHARACTER_CONTRACT_VERSION},
            )

        narrative_state = self._narrative_state_digest(scene)
        if narrative_state:
            sections.digest("narrative_state", narrative_state)

        info_asymmetry = self._information_asymmetry_digest(scene)
        if info_asymmetry:
            sections.digest("information_asymmetry", info_asymmetry)

        chapter_transition = self._chapter_transition_buffer(scene)
        if chapter_transition:
            sections.digest("chapter_transition_buffer", chapter_transition)

        # v2（规格 §1.3）：前文声音锚——只对 style_draft 可见
        # （context_budget.NEUTRAL_DRAFT_STYLE_SECTIONS 让中性稿看不到）。
        voice_anchor = None if reference_first else self._previous_scene_voice_anchor(scene)
        if reference_first:
            sections.ref("previous_scene_voice_anchor_deferred", "reference_first")
        if voice_anchor is not None:
            sections.add(
                VOICE_ANCHOR_SECTION_KEY,
                ref_id=voice_anchor["source_draft_row_id"],
                text=voice_anchor["text"],
                refs={
                    "previous_scene_voice_anchor_scene_id": voice_anchor["source_scene_id"],
                    "previous_scene_voice_anchor_draft_row_id": voice_anchor["source_draft_row_id"],
                    "previous_scene_voice_anchor_stage": voice_anchor["source_stage"],
                },
            )

        if previous_memory:
            sections.add(
                "prev_scene_memory",
                ref_id=previous_memory.scene_id,
                digest_key="scene_memory",
                text=(
                    reference_first_memory_digest(previous_memory.content)
                    if reference_first
                    else previous_memory.content
                ),
                refs={"scene_memory_prev": previous_memory.scene_id},
            )

        freshness_budget = self._literary_freshness_budget(scene, bundle_policy)
        if freshness_budget is not None:
            sections.add(
                "literary_freshness_budget",
                ref_id=scene.chapter_id,
                text=json.dumps(freshness_budget["budget"], ensure_ascii=False, sort_keys=True),
                refs={"literary_freshness_source_final_scene_ids": freshness_budget["source_final_scene_ids"]},
            )

        scene_summary = self.resolver.resolve_scene_summary(self.session, scene)
        if scene_summary:
            sections.add(
                "scene_summary",
                ref_id=scene_summary.scene_id,
                text=scene_summary.content,
                refs={"scene_summary_id": scene_summary.scene_id},
            )

        chapter_summary = self.resolver.resolve_chapter_summary(self.session, scene)
        if chapter_summary:
            sections.add(
                "chapter_summary",
                ref_id=chapter_summary.chapter_id,
                text=chapter_summary.content,
                refs={"chapter_summary_id": chapter_summary.chapter_id},
            )

        # §2 summary tower: far-horizon volume atmosphere (read-only, NOT a fact source)
        volume_summary = self._latest_volume_summary(scene)
        if volume_summary is not None:
            sections.add(
                "volume_summary",
                ref_id=volume_summary.row_id,
                text=(
                    "【卷级远景氛围 — 仅供语气/基调延续，严禁当作事实来源；事实一律以权威状态为准】\n"
                    + (volume_summary.atmosphere_summary or "")
                ),
                refs={"volume_summary_row_id": volume_summary.row_id},
            )

        projection = BundleSnapshotHashProjection(
            contract_version="BSHASH_v1",
            stage_allowlist_name="bundle_build_allowlist_v1",
            source_version_refs=sections.source_version_refs,
            # 关系卡退役（R8）后恒为空表；键留着，没有卡的 bundle 哈希不变
            resolved_ref_ids={"relation_ids": []},
            ordered_injections=sections.ordered_injections,
            inline_digests=sections.inline_digests,
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

    def _bundle_reference_route(self, contract: Mapping[str, Any]):
        """bundle 里由参考书派生的段能不能进读 bundle 的节点：按这些节点的实际路由判（``decide_reference_route``）。

        送云策略的书与节点无关（直接放行，不去解析路由）；「仅本机」/ 未知策略的书要求每一个读 bundle 的节点都走本机
        模型。返回 ``None`` = 契约里没有书（没有可判的东西）。"""
        layer = contract_layer(contract)
        book_snapshot = layer.get("book") if isinstance(layer.get("book"), Mapping) else {}
        book_id = str(book_snapshot.get("book_id") or "")
        if not book_id:
            return None
        book = self.session.get(StyleReferenceBook, book_id)
        return decide_reference_route(
            book,
            node_ids=BUNDLE_REFERENCE_NODE_IDS,
            frozen_book=book_snapshot,
            operation="style_reference_bundle",
        )

    def _reference_scene_scale(
        self, scene: SceneCard, contract: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """最具体一层画像的结构画像 × 本章活跃场数 → ``structure.reference_scene_scale``;缺画像 → None。"""
        layers = contract.get("layers") if isinstance(contract, Mapping) else None
        if not isinstance(layers, list) or not layers or not isinstance(layers[-1], Mapping):
            return None
        profile = layers[-1].get("profile") if isinstance(layers[-1].get("profile"), Mapping) else {}
        profile_json = profile.get("profile_json") if isinstance(profile.get("profile_json"), Mapping) else {}
        card = profile_json.get("structure_card")
        if not isinstance(card, Mapping):
            return None
        scenes_in_chapter = int(
            self.session.execute(
                select(func.count())
                .select_from(SceneCard)
                .where(SceneCard.chapter_id == scene.chapter_id, SceneCard.trashed_flag == 0)
            ).scalar()
            or 0
        )
        ceiling = injection_budget().style_first_reference_scene_chars_max
        return reference_scene_scale(card, scenes_in_chapter=scenes_in_chapter, ceiling=ceiling)

    def _latest_planning_artifact(
        self,
        *,
        artifact_type: str,
        object_type: str,
        object_id: str,
    ) -> GenerationPlanningArtifact | None:
        return latest_active_planning_artifact(
            self.session, artifact_type=artifact_type, object_type=object_type, object_id=object_id
        )


    def _previous_scene_voice_anchor(self, scene: SceneCard) -> dict[str, Any] | None:
        """规格 §1.3「前文声音锚」；失败只记降级槽，不阻断 bundle。"""
        try:
            return previous_scene_voice_anchor(self.session, scene)
        except Exception:  # noqa: BLE001 — optional continuity aid degrades visibly
            self._slot_degraded(VOICE_ANCHOR_SECTION_KEY, scene)
            return None

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

    def _literary_freshness_budget(
        self, scene: SceneCard, policy: StylePolicy = UNBOUND
    ) -> dict[str, Any] | None:
        """新鲜度预算（见 ``bundle_freshness``）；补充清单读失败只记降级槽。"""
        return literary_freshness_budget(
            self.session, scene, policy, on_degraded=lambda slot: self._slot_degraded(slot, scene)
        )

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

    def _chapter_transition_buffer(self, scene: SceneCard) -> str | None:
        """章间过渡缓冲（见 ``bundle_continuity.chapter_transition_text``）；失败只记降级槽。"""
        try:
            return chapter_transition_text(self.session, scene)
        except Exception:
            self._slot_degraded("chapter_transition_buffer", scene)
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
                onstage_character_ids=onstage,
                scene_id=scene.scene_id,
                pov_character_id=scene.pov_character_id,
            )
            return text if text else None
        except Exception:
            self._slot_degraded("information_asymmetry", scene)
            return None
