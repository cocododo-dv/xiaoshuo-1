"""作者「采纳并归档」一场（``POST /api/v1/scenes/{scene_id}/adopt-current``）：唯一的服务入口（治理 §5.2）。

从路由文件搬出（B12-01 / B03-25）；幂等层仍在路由（``mutate``）。React 客户端总是带 ``exact_author_draft``：作者稿的
CAS 保存与权威正文晋升在同一个事务里完成。不带它的兼容路径（只剩测试在走）按「未归档的当前终稿 → 管线草稿 →
作者稿」取内容源。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import (
    AuthorDraft,
    FinalScene,
    SceneBundle,
    SceneDraft,
    SceneMemory,
    SceneRunState,
)
from novel_system.services.archiver import Archiver
from novel_system.services.author_drafts import AuthorDraftService
from novel_system.services.author_lifecycle import AuthorLifecycleService
from novel_system.services.author_state import compute_author_state
from novel_system.services.canonical_manuscripts import CanonicalSceneService
from novel_system.services.errors import DomainError
from novel_system.services.manuscript_html import visible_paragraphs
from novel_system.services.reference_copy_gate import (
    check_reference_copy_for_scope,
    copy_block_author_action,
)
from novel_system.services.scene_run_checkpoint import SceneRunCheckpointService


def author_draft_plain_text(html: str | None) -> str:
    """作者稿（写作台存的 HTML，``<p>`` 分段）→ 归档用的纯文本：一段一行，没有标签，实体还原成字符。

    与写作台编辑器同一条分段规则（``manuscript_html.visible_paragraphs``）。以前这里用正则剥标签，
    ``&nbsp;`` / ``&quot;`` / ``&amp;`` / ``&lt;`` 这类实体原样进了权威正文（B12-21）。
    """
    return "\n".join(visible_paragraphs(html))


def adopt_current(
    session: Session,
    scene_id: str,
    *,
    actor_ref: str,
    accepted_warning_codes: list[str],
    exact_author_draft: dict[str, Any] | None,
) -> dict[str, Any]:
    """治理 §5.2：作者采纳归档的单一服务入口。

    前端「归档/置 done」动作必须打到这里——携带 exact_author_draft 时，
    作者稿 CAS 保存与 CanonicalScene 提升在同一个幂等事务中完成，浏览器正文
    不再与 FinalScene 分裂。兼容调用未携带 exact_author_draft 时，内容源优先级
    仍为未归档 current_final_scene → 管线草稿（latest_valid > style > neutral）→
    author-draft 人工稿兜底。守卫：无任何有效稿 409 NO_VALID_DRAFT；
    确定性来源安全扫描命中 409 SOURCE_SAFETY_BLOCKED（草稿保留可重试，
    设计红线 8：来源安全未通过可保存草稿但不能标记为已安全归档）。
    """
    scene = AuthorLifecycleService(session).require_active_scene(scene_id)
    state = session.get(SceneRunState, scene_id)
    if state is None:
        state = SceneRunState(scene_id=scene_id, scene_status="ready")
        session.add(state)
        session.flush()

    # 兼容旧调用的已归档幂等返回。精确作者稿可能是在已归档版本之上的
    # 新修订，必须继续走 revision + FinalScene 双 CAS，不能在这里吞掉。
    # （如 C2 真实库中 failed@soft_qc_ready 的历史残留，作者重点一次即自愈）
    if (
        exact_author_draft is None
        and state.scene_status == "archived"
        and state.current_final_scene_row_id
    ):
        current_final = session.get(FinalScene, state.current_final_scene_row_id)
        if current_final is None or current_final.scene_id != scene_id:
            raise DomainError(
                "FINAL_SCENE_NOT_FOUND",
                "archived scene points to a missing final manuscript",
                status_code=409,
                details={"scene_id": scene_id},
            )
        current_memory = (
            session.execute(
                select(SceneMemory).where(
                    SceneMemory.scene_id == scene_id,
                    SceneMemory.final_scene_row_id == current_final.row_id,
                    SceneMemory.active_flag == 1,
                )
            )
            .scalars()
            .first()
        )
        confirmation = Archiver(session).archive_final_scene(
            scene_id,
            current_final.row_id,
            carry_notes_json=(
                list(current_memory.carry_notes_json or [])
                if current_memory is not None
                else []
            ),
            author_confirmed_final=True,
            accepted_warning_codes=accepted_warning_codes,
            fidelity_source="adopt",
        )
        residue_finalized = SceneRunCheckpointService(
            session
        ).finalize_after_author_archive(scene_id)
        return {
            "scene_id": scene_id,
            "scene_status": "archived",
            "final_scene_row_id": state.current_final_scene_row_id,
            "already_archived": True,
            "safe_to_archive": confirmation["safe_to_archive"],
            "literary_warnings_unresolved": confirmation[
                "literary_warnings_unresolved"
            ],
            "author_confirmed_final": confirmation["author_confirmed_final"],
            "finality": confirmation["finality"],
            "run_residue_finalized": residue_finalized,
            "author_state": compute_author_state(session, scene_id, state),
        }

    # Wave 2（治理 §5.3/§5.4）：只有真实 Q0/Q1 能阻断归档——投影为 hard_blocked
    # （当前 QC 报告存在 verified Q0/Q1 分级条目）时拒绝采纳，正文保留（§7.2）。
    projection = compute_author_state(session, scene_id, state)
    if projection["author_state"] == "hard_blocked":
        raise DomainError(
            "HARD_BLOCKED",
            "verified Q0/Q1 findings block adoption — resolve or revise before archiving",
            status_code=409,
            details={
                "scene_id": scene_id,
                "blocking_findings": projection["blocking_findings"],
            },
        )
    # Wave 3（§5.5 完成门）：关键场景未终选前不可归档——adopt 旁路同样封死
    if projection["author_state"] == "awaiting_author_choice":
        raise DomainError(
            "SELECTION_REQUIRED",
            "author terminal selection is required before archiving this critical scene",
            status_code=409,
            details={"scene_id": scene_id},
        )

    # 浏览器精确稿路径：先以 base_revision_no 保存请求中的确定正文，再把
    # 保存后的同一修订提升为 FinalScene。两个动作共享当前数据库事务；保存、
    # 安全门、聚合或归档任一步失败都会整体回滚。
    if exact_author_draft is not None:
        draft_id = exact_author_draft["draft_id"]
        draft = session.get(AuthorDraft, draft_id)
        if draft is None:
            raise DomainError(
                "AUTHOR_DRAFT_NOT_FOUND",
                "author draft not found",
                status_code=404,
                details={"draft_id": draft_id},
            )
        if draft.object_type != "scene" or draft.object_id != scene_id:
            raise DomainError(
                "AUTHOR_DRAFT_SCENE_MISMATCH",
                "author draft does not belong to the scene being adopted",
                status_code=409,
                details={
                    "draft_id": draft_id,
                    "draft_object_type": draft.object_type,
                    "draft_object_id": draft.object_id,
                    "scene_id": scene_id,
                },
            )
        saved = AuthorDraftService(session).save(
            draft_id,
            {
                "content": exact_author_draft["content"],
                "base_revision_no": exact_author_draft["base_revision_no"],
                "note": "atomic scene adoption",
            },
            actor_ref=actor_ref,
        )
        saved_draft = saved.get("draft") or {}
        saved_revision_no = saved_draft.get("revision_no")
        if not isinstance(saved_revision_no, int):
            raise DomainError(
                "AUTHOR_DRAFT_SAVE_INCOMPLETE",
                "saved author draft did not return a revision number",
                status_code=500,
                details={"draft_id": draft_id},
            )
        promoted = CanonicalSceneService(session).promote_author_draft(
            draft_id,
            {
                "base_revision_no": saved_revision_no,
                "expected_current_final_scene_row_id": exact_author_draft[
                    "expected_current_final_scene_row_id"
                ],
                # Saving exact author text proves which revision was chosen;
                # it does not prove that story facts stayed unchanged.
                "narrative_effect": "requires_reconcile",
                "accepted_warning_codes": accepted_warning_codes,
            },
            actor_ref=actor_ref,
            fidelity_source="adopt",
        )
        session.flush()
        session.refresh(draft)
        promoted["author_draft"] = AuthorDraftService.serialize_draft(
            draft,
            current_final_scene_row_id=promoted["final_scene_row_id"],
        )
        promoted["exact_author_draft"] = True
        promoted["author_state"] = compute_author_state(session, scene_id, state)
        return promoted

    # 1) 内容源解析
    final: FinalScene | None = None
    if state.current_final_scene_row_id:
        row = session.get(FinalScene, state.current_final_scene_row_id)
        if row is not None and (row.content or "").strip():
            final = row
    source_draft_row_id: str | None = None
    content: str | None = None
    source_bundle_id: str | None = None
    source_bundle_hash: str | None = None
    if final is None:
        for row_id in (
            state.latest_valid_draft_row_id,
            state.current_style_draft_row_id,
            state.current_neutral_draft_row_id,
        ):
            if not row_id:
                continue
            draft = session.get(SceneDraft, row_id)
            if draft is not None and (draft.content or "").strip():
                source_draft_row_id = row_id
                content = draft.content
                source_bundle_id = draft.source_bundle_id
                source_bundle_hash = draft.source_bundle_hash
                break
        if content is None:
            author_draft = (
                session.execute(
                    select(AuthorDraft).where(
                        AuthorDraft.object_type == "scene",
                        AuthorDraft.object_id == scene_id,
                        AuthorDraft.status == "current",
                    )
                )
                .scalars()
                .first()
            )
            text = (
                author_draft_plain_text(author_draft.content)
                if author_draft
                else ""
            )
            if text.strip():
                content = text
                source_bundle_id = f"author_draft:{author_draft.draft_id}"
                source_bundle_hash = f"author_draft_rev_{author_draft.revision_no}"
        if content is None:
            raise DomainError(
                "NO_VALID_DRAFT",
                "no valid draft content to adopt — generate or write the scene first",
                status_code=409,
                details={"scene_id": scene_id},
            )

    # 2) 唯一抄袭门（Q0 红线；风格参考 v3）：与绑定的参考书连续 ≥12 字相同或含受保护专名即拦。
    # 比对 bundle 冻结的绑定与这一场当前的活动绑定；归档时成稿门再过一遍同一道门（同一稿命中缓存）。
    target_content = final.content if final is not None else (content or "")
    bundle = (
        session.get(SceneBundle, state.current_bundle_id)
        if state.current_bundle_id
        else None
    )
    copy_check = check_reference_copy_for_scope(
        session,
        target_content,
        scope=scene,
        bundle_snapshot=bundle.frozen_snapshot_json if bundle else None,
    )
    scan = copy_check.audit()
    if copy_check.blocked:
        raise DomainError(
            "SOURCE_SAFETY_BLOCKED",
            "reference copy gate blocked adoption — draft is kept and can be revised",
            status_code=409,
            details={
                "scene_id": scene_id,
                "reference_copy": scan,
                "author_action": copy_block_author_action(
                    copy_check, target_view="writer", target_ref=f"scene:{scene_id}"
                ),
            },
        )

    # 3) FinalScene 建行或提升，经归档事务统一置权威态
    if final is None:
        final = FinalScene(
            row_id=f"final_scene_{scene_id}_adopt_{uuid4().hex[:10]}",
            scene_id=scene_id,
            chapter_id=scene.chapter_id,
            content=content or "",
            source_bundle_id=source_bundle_id or "author_adopt",
            source_bundle_hash=source_bundle_hash or "author_adopt",
        )
        session.add(final)
        session.flush()
    state.current_final_scene_row_id = final.row_id
    if source_draft_row_id:
        state.latest_valid_draft_row_id = source_draft_row_id

    carry_notes: list[dict[str, Any]] = [
        {"kind": "author_adoption", "actor_ref": actor_ref}
    ]
    quality_warnings = [
        item
        for item in projection.get("quality_warnings") or []
        if isinstance(item, dict)
    ]
    if quality_warnings:
        # Wave 2（Wave 2 项 7）：采纳带 Q2/Q3 警告的稿 = 作者显式接受，留审计
        carry_notes.append(
            {
                "kind": "quality_warning_acceptance",
                "actor_ref": actor_ref,
                "accepted": [
                    {
                        "issue_key": item.get("issue_key") or item.get("kind"),
                        "quality_level": item.get("quality_level"),
                    }
                    for item in quality_warnings[:10]
                ],
            }
        )
    archive_result = Archiver(session).archive_final_scene(
        scene_id,
        final.row_id,
        carry_notes_json=carry_notes,
        author_confirmed_final=True,
        accepted_warning_codes=accepted_warning_codes,
        fidelity_source="adopt",
    )
    # C2 状态一致性债务：归档后无主执行残留（failed@soft_qc_ready 等）
    # 在同一事务内收敛为 completed/archived，运维/展示不再被误导
    run_residue_finalized = SceneRunCheckpointService(
        session
    ).finalize_after_author_archive(scene_id)
    return {
        "scene_id": scene_id,
        "scene_status": archive_result["scene_status"],
        "final_scene_row_id": final.row_id,
        "scene_memory_row_id": archive_result["scene_memory_row_id"],
        "safe_to_archive": archive_result["safe_to_archive"],
        "literary_warnings_unresolved": archive_result[
            "literary_warnings_unresolved"
        ],
        "author_confirmed_final": archive_result["author_confirmed_final"],
        "finality": archive_result["finality"],
        "source_safety_scan": scan,
        "run_residue_finalized": run_residue_finalized,
        "author_state": compute_author_state(session, scene_id, state),
    }


__all__ = ["adopt_current", "author_draft_plain_text"]
