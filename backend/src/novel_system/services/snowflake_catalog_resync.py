"""雪花构思 → 目录的回流：已物化的场景卡跟上构思（显式回流、确认即同步、待同步清单），章内顺序的两阶段落位。

场景卡的补丁与差异（``_scene_card_resync_patch`` / ``_scene_card_diff``）、按故事序的章内顺序漂移、
停靠再落位、运行时行跟着场走（``rehome_scenes``）、搬空的章进回收站都在这里。2026-09-30 从
``SnowflakeWorkspaceService`` 拆出（B06-07）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from novel_system.db.models import (
    AuthorDraft,
    ChapterGoal,
    FinalScene,
    LlmCall,
    OperationLog,
    SceneCard,
    SceneRunState,
    SnowflakeScenePlan,
)
from novel_system.services.catalog_ordering import PARK_GAP
from novel_system.services.errors import DomainError
from novel_system.services.projects import trash_emptied_snowflake_chapters
from novel_system.services.scene_rehome import rehome_scenes
from novel_system.services.snowflake_scene_brief import followed_scene_title, real_scene_title, scene_card_beats, scene_card_goal
from novel_system.services.snowflake_scene_rows import scene_plan_payload
from novel_system.services.snowflake_step_catalog import SUMMARY_LENGTH_BAND, effective_rendering_mode
from novel_system.services.story_slots import planned_beats, without_retired_chapter_goal

#: 一条 ``IN`` 查询最多带多少个 id（远低于 SQLite 的变量上限）
_IN_CHUNK = 500


class SnowflakeCatalogResyncMixin:
    """见模块说明。与其它 ``snowflake_*`` 混入类一起组成 ``SnowflakeWorkspaceService``（B06-07）：
    方法之间照旧经 ``self`` 互相调用，名字与签名一个不改（测试与分章包依赖它们）。"""

    def _auto_sync_catalog(self, project_id: str, *, actor_ref: str = "operator") -> dict[str, Any]:
        plans = self._scene_plans(project_id)
        excluded = self._excluded_scene_plan_ids(project_id)
        order_drift = self._scene_card_order_drift(project_id, plans)
        cards = self._scene_cards_by_id(project_id, [plan.scene_id for plan in plans])
        eligible: list[str] = []
        held: list[dict[str, Any]] = []
        for plan in plans:
            scene = cards.get(plan.scene_id)
            if scene is None:
                continue  # 还没物化的场：进目录走「整理为章节结构」
            patch = self._scene_card_resync_patch(
                plan,
                scene,
                excluded=plan.scene_plan_id in excluded,
                leaving=self._chapter_left_behind(project_id, plan, scene),
            )
            diff = self._scene_card_diff(scene, patch)
            if scene.scene_id in order_drift:
                diff["scene_order"] = order_drift[scene.scene_id]
            if not diff:
                continue
            reason = self._auto_sync_hold_reason(plan, scene, patch)
            if reason:
                held.append(
                    {
                        "scene_plan_id": plan.scene_plan_id,
                        "scene_id": plan.scene_id,
                        "title": plan.title or plan.summary or plan.scene_id,
                        "reason": reason,
                    }
                )
            else:
                eligible.append(plan.scene_plan_id)
        synced: list[str] = []
        trashed_empty_chapters: list[dict[str, Any]] = []
        notice: dict[str, Any] | None = None
        if eligible:
            outcome = self.resync_materialized_scenes(
                project_id,
                {"scene_plan_ids": eligible},
                actor_ref=f"auto_sync:{actor_ref or 'operator'}",
                include_workspace=False,
            )
            synced = [item["scene_id"] for item in outcome.get("results") or [] if item.get("synced")]
            trashed_empty_chapters = list(outcome.get("trashed_empty_chapters") or [])
            notice = outcome.get("notice")
        return {
            "synced_count": len(synced),
            "synced_scene_ids": synced,
            "held_count": len(held),
            "held": held,
            "trashed_empty_chapters": trashed_empty_chapters,
            **({"notice": notice} if notice else {}),
        }

    def _auto_sync_hold_reason(self, plan: SnowflakeScenePlan, scene: SceneCard, patch: dict[str, Any]) -> str:
        if str(plan.status or "") != "approved":
            return "plan_not_confirmed"
        if int(patch.get("trashed_flag") or 0) == 1 and self._scene_card_has_work(scene):
            return "would_trash_written_scene"
        return ""

    def _scene_card_has_work(self, scene: SceneCard) -> bool:
        if int(scene.words_current or 0) > 0:
            return True
        state = self.session.get(SceneRunState, scene.scene_id)
        if state is not None and (state.current_final_scene_row_id or str(state.scene_status or "ready") != "ready"):
            return True
        return (
            self.session.execute(select(FinalScene.row_id).where(FinalScene.scene_id == scene.scene_id).limit(1)).first()
            is not None
        )

    def resync_status(self, project_id: str) -> dict[str, Any]:
        """台子用的轻量读口：哪几场的场景卡落后于构思（不必为此拉整个工作台）。

        写作台 / AI 起草台对每一部作品都会问这一句，包括不是雪花法的作品——那不是错误：
        如实回答「这部作品没有构思侧可同步」（``supported: false``），而不是 409（浏览器会把它记成一条控制台报错）。
        """
        project = self._projects.require_project(project_id)
        if str(getattr(project, "planning_mode", "") or "") != "snowflake":
            return {"supported": False, "pending_count": 0, "pending_scene_plan_ids": [], "pending_scenes": []}
        return {"supported": True, **self._resync_status(project.project_id, self._scene_plans(project.project_id))}

    def resync_materialized_scenes(
        self,
        project_id: str,
        payload: dict[str, Any] | None = None,
        *,
        actor_ref: str = "operator",
        include_workspace: bool = True,
    ) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        dry_run = bool(body.get("dry_run", False))
        requested_plan_ids = {
            str(item or "").strip()
            for item in body.get("scene_plan_ids") or []
            if str(item or "").strip()
        }
        requested_scene_ids = {
            str(item or "").strip()
            for item in body.get("scene_ids") or []
            if str(item or "").strip()
        }
        all_plans = self._scene_plans(project.project_id)
        plans = all_plans
        if requested_plan_ids:
            plans = [plan for plan in plans if plan.scene_plan_id in requested_plan_ids]
        if requested_scene_ids:
            plans = [plan for plan in plans if plan.scene_id in requested_scene_ids]
        if not plans:
            raise DomainError("SNOWFLAKE_RESYNC_SCENES_NOT_FOUND", "未找到匹配的场景计划。", status_code=404)

        results: list[dict[str, Any]] = []
        affected_scene_ids: list[str] = []
        pending_moves: list[dict[str, Any]] = []
        touched_chapter_ids: set[str] = set()
        moved_scenes: dict[str, tuple[str, str]] = {}  # 阶段 Y：换了章的场，运行时行要跟着走
        excluded = self._excluded_scene_plan_ids(project.project_id)
        order_drift = self._scene_card_order_drift(project.project_id, all_plans)
        cards = self._scene_cards_by_id(project.project_id, [plan.scene_id for plan in plans])
        # 第一遍：只算补丁（不动库）。场景卡的位置 = 章 + 章内序，两样都受唯一索引
        # (chapter_id, scene_seq) 约束；一张一张地改会撞上还没搬走的那一张，所以位置留到最后
        # 由 _settle_scene_card_order 两阶段统一落位，补丁里不再带 scene_seq。
        prepared: list[tuple[SnowflakeScenePlan, SceneCard | None, dict[str, Any], dict[str, Any] | None]] = []
        for plan in plans:
            scene = cards.get(plan.scene_id)
            if scene is None:
                prepared.append((plan, None, {}, None))
                continue
            scene_patch = self._scene_card_resync_patch(
                plan,
                scene,
                excluded=plan.scene_plan_id in excluded,
                leaving=self._chapter_left_behind(project.project_id, plan, scene),
            )
            blocked_move = self._unmaterialized_chapter_move(project.project_id, scene, scene_patch)
            if blocked_move:
                # 搬不动就别搬：``SceneCard.chapter_id`` 是指向 chapter_goals 的外键，
                # 写一个目录里还不存在的章号 = FOREIGN KEY constraint failed，整次回流
                # 500「database operation failed」，连能同步的内容改动一起赔进去。
                # 作者重新分了章但还没「整理为章节结构」时这就是常态，不是异常。
                scene_patch.pop("chapter_id", None)
            prepared.append((plan, scene, scene_patch, blocked_move))

        if not dry_run:
            repositioned = {
                str(chapter_id)
                for _plan, scene, scene_patch, _blocked in prepared
                if scene is not None
                for chapter_id in (
                    (scene.chapter_id, scene_patch.get("chapter_id"))
                    if (
                        (scene_patch.get("chapter_id") and scene_patch.get("chapter_id") != scene.chapter_id)
                        or scene.scene_id in order_drift
                        or "trashed_flag" in scene_patch
                    )
                    else ()
                )
                if chapter_id
            }
            settle = self._park_scene_cards(project.project_id, repositioned)
        else:
            settle = None

        for plan, scene, scene_patch, blocked_move in prepared:
            if scene is None:
                results.append(
                    {
                        "scene_plan_id": plan.scene_plan_id,
                        "scene_id": plan.scene_id,
                        "synced": False,
                        "reason": "scene_not_materialized",
                        "diff": {},
                    }
                )
                continue
            source_chapter_id = str(scene.chapter_id or "")
            diff = self._scene_card_diff(scene, scene_patch)
            if scene.scene_id in order_drift:
                diff["scene_order"] = order_drift[scene.scene_id]
            if diff:
                affected_scene_ids.append(scene.scene_id)
            if not dry_run and diff:
                self._apply_scene_card_resync(scene, scene_patch)
                if source_chapter_id and scene.chapter_id and scene.chapter_id != source_chapter_id:
                    moved_scenes[scene.scene_id] = (source_chapter_id, str(scene.chapter_id))
                # 阶段 L：章目标写到场**搬进去之后**所在的章（以前先取旧章再搬，跨章移动会把目标章的
                # 章目标盖到旧章上）；两头的章都记下，最后重算 is_chapter_last。
                touched_chapter_ids.update({source_chapter_id, str(scene.chapter_id or "")})
                chapter = self.session.get(ChapterGoal, scene.chapter_id)
                if chapter is not None:
                    chapter.writer_brief_json = {
                        **dict(chapter.writer_brief_json or {}),
                        "source": "snowflake_resync",
                        "project_id": project.project_id,
                        "chapter_id": plan.chapter_id,
                        "chapter_goal": plan.chapter_goal or chapter.chapter_goal,
                    }
                    if plan.chapter_goal:
                        chapter.chapter_goal = plan.chapter_goal
                self.session.add(
                    OperationLog(
                        event_type="snowflake_scene_resynced",
                        object_type="scene_card",
                        object_ref=scene.scene_id,
                        payload_json={
                            "project_id": project.project_id,
                            "scene_id": scene.scene_id,
                            "scene_plan_id": plan.scene_plan_id,
                            "dry_run": False,
                            "diff_fields": sorted(diff.keys()),
                            "actor_ref": actor_ref or "operator",
                        },
                    )
                )
            entry = {
                "scene_plan_id": plan.scene_plan_id,
                "scene_id": scene.scene_id,
                "synced": bool(diff) and not dry_run,
                "reason": "changed" if diff else "already_current",
                "diff": diff,
            }
            if blocked_move:
                entry["blocked_chapter_move"] = blocked_move
                pending_moves.append({"scene_id": scene.scene_id, **blocked_move})
            results.append(entry)
        trashed_empty_chapters: list[dict[str, Any]] = []
        if not dry_run:
            self.session.flush()
            if settle is not None:
                self._settle_scene_card_order(project.project_id, settle)
            if moved_scenes:
                rehome_scenes(self.session, project.project_id, moved_scenes)
            remaining = touched_chapter_ids - set((settle or {}).get("chapter_ids") or ())
            if remaining:
                self._recompute_chapter_last(project.project_id, remaining)
            if settle is not None:
                # 场景卡跨章搬完之后，雪花建的旧章如果一张卡都不剩、这一版分章也不再用它，就移入回收站
                # （与「确认写入」同一条规则；章还被某个场景计划指着时绝不动它）。
                trashed_empty_chapters = trash_emptied_snowflake_chapters(
                    self.session,
                    project.project_id,
                    keep_chapter_ids={str(plan.chapter_id or "") for plan in self._scene_plans(project.project_id)},
                )

        if not dry_run:
            self.session.flush()
        affected_runtime = self._affected_runtime_summary(project.project_id, affected_scene_ids)
        result: dict[str, Any] = {
            "dry_run": dry_run,
            "results": results,
            "affected_runtime": affected_runtime,
            "trashed_empty_chapters": trashed_empty_chapters,
        }
        if include_workspace:
            # 确认即同步（approve_step）自己会在最后取一次工作台，不必在这里再算一遍
            result["workspace"] = self.mutation_workspace(project.project_id)
        if pending_moves:
            # 静默跳过等于撒谎：作者以为回流做完了，目录其实还停在上一版章节结构。
            targets = sorted({item["target_chapter_id"] for item in pending_moves})
            result["notice"] = {
                "code": "CHAPTER_MOVE_NEEDS_MATERIALIZE",
                "severity": "warning",
                "message": (
                    f"有 {len(pending_moves)} 场要搬到目录里还不存在的章"
                    f"（{'、'.join(targets[:3])}{'…' if len(targets) > 3 else ''}），"
                    "这一部分没有回流。请先「整理为章节结构」把新的章写进目录，再回流一次。"
                ),
                "pending_moves": pending_moves,
            }
        return result

    def _active_cards_by_chapter(self, project_id: str, chapter_ids: set[str] | None = None) -> dict[str, list[SceneCard]]:
        query = select(SceneCard).where(SceneCard.project_id == project_id, SceneCard.trashed_flag == 0)
        if chapter_ids is not None:
            query = query.where(SceneCard.chapter_id.in_(sorted(chapter_ids)))
        grouped: dict[str, list[SceneCard]] = {}
        for card in self.session.execute(query).scalars():
            grouped.setdefault(str(card.chapter_id), []).append(card)
        for cards in grouped.values():
            cards.sort(key=lambda card: (int(card.scene_seq or 0), str(card.scene_id)))
        return grouped

    def _scene_card_order_drift(
        self,
        project_id: str,
        scene_plans: list[SnowflakeScenePlan] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """目录里章内顺序和故事序对不上的场景卡：``scene_id → {before, after}``（都是章内第几场）。

        只比有场景计划的卡彼此之间的先后——计划外的卡（作者手加的场）和被略过 / 待删的场不占位，
        所以它们的存在不会让整章被误报成「待同步」。
        """
        plans = scene_plans if scene_plans is not None else self._scene_plans(project_id)
        rank = {plan.scene_id: index for index, plan in enumerate(plans)}
        drift: dict[str, dict[str, Any]] = {}
        for cards in self._active_cards_by_chapter(project_id).values():
            planned = [card for card in cards if card.scene_id in rank]
            wanted = sorted(planned, key=lambda card: rank[card.scene_id])
            for index, card in enumerate(planned):
                if wanted[index] is not card:
                    drift[card.scene_id] = {"before": index + 1, "after": wanted.index(card) + 1}
        return drift

    def _park_scene_cards(self, project_id: str, chapter_ids: set[str]) -> dict[str, Any] | None:
        """两阶段落位的第一阶段：记下这些章此刻的顺序，把它们的活跃卡停到不会冲突的高位序号上。"""
        if not chapter_ids:
            return None
        grouped = self._active_cards_by_chapter(project_id, chapter_ids)
        cards = [card for members in grouped.values() for card in members]
        old_order = {chapter_id: [card.scene_id for card in members] for chapter_id, members in grouped.items()}
        old_seq = {card.scene_id: int(card.scene_seq or 1) for card in cards}
        if cards:
            # 停靠位要高过这些章里**所有**卡的序号——包括回收站里的：这次回流可能正要把其中一张取回来
            # （改回「略过」/「待删」的裁定），它带着自己的旧序号变回活跃，不能和停靠位撞上。
            # 所以上限按库里查（catalog_ordering.park 只看交给它的行），间距用同一个 PARK_GAP（B08-15），
            # 与 catalog_trash_cascade 的取回同一个做法。
            highest = self.session.execute(
                select(func.max(SceneCard.scene_seq)).where(
                    SceneCard.project_id == project_id, SceneCard.chapter_id.in_(sorted(chapter_ids))
                )
            ).scalar()
            park_base = int(highest or 0) + PARK_GAP
            for offset, card in enumerate(cards):
                card.scene_seq = park_base + offset
            self.session.flush()
        return {"chapter_ids": set(chapter_ids), "old_order": old_order, "old_seq": old_seq}

    def _settle_scene_card_order(self, project_id: str, parked: dict[str, Any]) -> None:
        """第二阶段：每一章按「计划内的卡跟故事序、计划外的卡跟着它原来的前一张」重新编号。"""
        rank = {plan.scene_id: index for index, plan in enumerate(self._scene_plans(project_id))}
        # 计划外的卡原来前面依次是哪些计划内的卡（近的在前）：最近的那张若搬去了别的章，就跟再前面那张
        preceding_of: dict[str, list[str]] = {}
        for members in (parked.get("old_order") or {}).values():
            seen: list[str] = []
            for scene_id in members:
                if scene_id in rank:
                    seen.insert(0, scene_id)
                else:
                    preceding_of[scene_id] = list(seen)
        grouped = self._active_cards_by_chapter(project_id, set(parked["chapter_ids"]))
        for members in grouped.values():
            planned = sorted((card for card in members if card.scene_id in rank), key=lambda card: rank[card.scene_id])
            staying = {card.scene_id for card in planned}
            trailing: dict[str | None, list[SceneCard]] = {}
            for card in members:
                if card.scene_id in rank:
                    continue
                anchor = next((item for item in preceding_of.get(card.scene_id, []) if item in staying), None)
                trailing.setdefault(anchor, []).append(card)
            ordered = list(trailing.get(None, []))
            for card in planned:
                ordered.append(card)
                ordered.extend(trailing.get(card.scene_id, []))
            for index, card in enumerate(ordered, start=1):
                card.scene_seq = index
                card.is_chapter_last = 1 if index == len(ordered) else 0
        # 这次回流送进回收站的卡：把停靠前的序号还给它（回收站里的卡不受唯一索引约束）——
        # 作者从回收站手动恢复时，目录按这个序号把它插回原来的位置。
        old_seq = parked.get("old_seq") or {}
        if old_seq:
            for card in self.session.execute(
                select(SceneCard).where(SceneCard.scene_id.in_(sorted(old_seq)), SceneCard.trashed_flag == 1)
            ).scalars():
                card.scene_seq = old_seq[card.scene_id]
        self.session.flush()

    def _unmaterialized_chapter_move(
        self,
        project_id: str,
        scene: SceneCard,
        patch: dict[str, Any],
    ) -> dict[str, Any] | None:
        """这条补丁想把场景卡搬进一个目录里还不存在的章吗？

        重新分章只写构思侧（``SnowflakeScenePlan.chapter_id`` = 那一章钉住的目录章号，新章是刚铸的号），
        目录里的 ``ChapterGoal`` 要等「整理为章节结构」才建。两者之间的窗口里，构思侧
        指向的章号可以完全没有对应的目录行——而 ``SceneCard.chapter_id`` 是外键。
        """
        target = str(patch.get("chapter_id") or "").strip()
        if not target or target == scene.chapter_id:
            return None
        chapter = self.session.get(ChapterGoal, target)
        if chapter is not None and chapter.project_id == project_id and not chapter.trashed_flag:
            return None
        return {
            "target_chapter_id": target,
            "current_chapter_id": scene.chapter_id,
            "reason": "chapter_not_in_catalog",
        }

    def _chapter_left_behind(self, project_id: str, plan: SnowflakeScenePlan, scene: SceneCard) -> ChapterGoal | None:
        """回流会把这张卡搬进别的章（目录里已经有那一章）时，它离开的那一章；不搬、或搬不动（目标章还没物化，
        见 :meth:`_unmaterialized_chapter_move`）返回 None。回流、待同步清单与确认即同步按同一个判定算补丁。"""
        target = str(plan.chapter_id or "").strip()
        if not target or target == scene.chapter_id:
            return None
        if self._unmaterialized_chapter_move(project_id, scene, {"chapter_id": target}) is not None:
            return None
        return self.session.get(ChapterGoal, scene.chapter_id)

    def _resync_status(
        self,
        project_id: str,
        scene_plans: list[SnowflakeScenePlan],
        *,
        excluded: set[str] | None = None,
    ) -> dict[str, Any]:
        pending: list[dict[str, Any]] = []
        if excluded is None:
            excluded = self._excluded_scene_plan_ids(project_id)
        order_drift = self._scene_card_order_drift(project_id, scene_plans)
        cards = self._scene_cards_by_id(project_id, [plan.scene_id for plan in scene_plans])
        for plan in scene_plans:
            scene = cards.get(plan.scene_id)
            if scene is None:
                continue
            patch = self._scene_card_resync_patch(
                plan,
                scene,
                excluded=plan.scene_plan_id in excluded,
                leaving=self._chapter_left_behind(project_id, plan, scene),
            )
            diff = self._scene_card_diff(scene, patch)
            if scene.scene_id in order_drift:
                diff["scene_order"] = order_drift[scene.scene_id]
            if not diff:
                continue
            pending.append(
                {
                    "scene_plan_id": plan.scene_plan_id,
                    "scene_id": plan.scene_id,
                    "title": plan.title or plan.summary or plan.scene_id,
                    "changed_fields": sorted(diff.keys()),
                    # 阶段 X：台子只提示**已确认**的规划落后于目录；还在改的草稿不算「待同步」
                    "plan_status": str(plan.status or ""),
                }
            )
        return {
            "pending_count": len(pending),
            "pending_scene_plan_ids": [item["scene_plan_id"] for item in pending],
            "pending_scenes": pending,
        }

    def _scene_cards_by_id(self, project_id: str, scene_ids: list[str]) -> dict[str, SceneCard]:
        """这些场景计划已经物化出的场景卡（含回收站里的）：一次 ``IN`` 查询，代替逐场 ``session.get``（B06-04 / 06）。"""
        wanted = sorted({scene_id for scene_id in scene_ids if scene_id})
        cards: dict[str, SceneCard] = {}
        for start in range(0, len(wanted), _IN_CHUNK):
            chunk = wanted[start : start + _IN_CHUNK]
            for card in self.session.execute(select(SceneCard).where(SceneCard.scene_id.in_(chunk))).scalars():
                if card.project_id == project_id:
                    cards[card.scene_id] = card
        return cards

    def _recompute_chapter_last(self, project_id: str, chapter_ids: set[str]) -> None:
        """回流搬过场之后重算每章的章末标记（scene_criticality 把章末当高潮位）——物化时算过一次，
        搬动后旧章的末场变了、新章的末场也变了，以前都没有重算。"""
        for chapter_id in sorted(cid for cid in chapter_ids if cid):
            cards = self.session.execute(
                select(SceneCard).where(
                    SceneCard.project_id == project_id,
                    SceneCard.chapter_id == chapter_id,
                    SceneCard.trashed_flag == 0,
                )
            ).scalars().all()
            if not cards:
                continue
            last = max(cards, key=lambda card: (int(card.scene_seq or 0), str(card.scene_id)))
            for card in cards:
                card.is_chapter_last = 1 if card is last else 0

    @staticmethod
    def _scene_card_resync_patch(
        plan: SnowflakeScenePlan,
        scene: SceneCard,
        *,
        excluded: bool = False,
        leaving: ChapterGoal | None = None,
    ) -> dict[str, Any]:
        """``leaving``：这张卡要搬出去的那一章（:meth:`_chapter_left_behind`），不搬时为 None。"""
        # 阶段 C：呈现方式与篇幅带同物化一个口径——summary 场回流也拿数值带。
        rendering_mode = effective_rendering_mode(plan.scene_type, plan.rendering_mode)
        # 阶段 N：作者裁定该重写 / 待删的场与「略过」同路——卡进回收站，改回裁定时取回。
        skipped = rendering_mode == "skip" or bool(excluded)
        if rendering_mode == "summary":
            target_length_band = SUMMARY_LENGTH_BAND
        else:
            # 从概述改回完整场：规划行多半没有显式篇幅带（前端不上行它），不能让场景卡
            # 继续挂着 200-500 的概述带——回到默认 medium。
            current_band = scene.target_length_band if scene.target_length_band != SUMMARY_LENGTH_BAND else None
            target_length_band = plan.target_length_band or current_band or "medium"
        previous_brief = dict(scene.writer_brief_json or {})
        carried = {key: value for key, value in previous_brief.items() if key not in {"title", "seeded_title", "desk_edited_at"}}
        brief = {
            **carried,
            # 阶段 X：题名跟构思走，除非作者在台子上改过（``desk_edited_at`` 是阶段 X 留下的旧记号，回流时顺手清掉）
            **followed_scene_title(previous_brief, real_scene_title(plan.title, plan.summary)),
            "source": "snowflake_resync",
            "scene_plan_id": plan.scene_plan_id,
            "project_id": plan.project_id,
            "chapter_id": plan.chapter_id,
            "scene_id": plan.scene_id,
            "chapter_goal": plan.chapter_goal,
            "scene_crucible": plan.scene_crucible,
            "goal": plan.goal,
            "conflict": plan.conflict,
            "setback": plan.setback,
            "reaction": plan.reaction,
            "dilemma": plan.dilemma,
            "decision": plan.decision,
            "cost_requirement": plan.cost_requirement,
            "expected_reader_emotion": plan.expected_reader_emotion or "",
            "story_time": plan.story_time or "",
            "exception_reason": plan.exception_reason or "",
            "primary_form": plan.scene_type,
            "rendering_mode": rendering_mode,
            "timebox": target_length_band or "medium",
            # 阶段 I：规划里改成「略过」的已物化场，回流把场景卡送进回收站（可恢复）；改回来时再取回。
            # 作者自己扔进回收站的卡不带这个标记，回流不碰它。阶段 N：该重写 / 待删的裁定走同一条路。
            "skipped_by_plan": skipped,
            "excluded_by_triage": bool(excluded),
        }
        # 与物化同一配方（scene_card_beats）：两个写入方各算一套，刚物化完的每一场
        # 都会因 beats_json 不同被报成「待同步」，横幅在物化当刻就喊 N 场。
        detail = scene_plan_payload(plan)
        beats = scene_card_beats(str(detail.get("scene_type") or "proactive"), detail)
        trash_patch: dict[str, Any] = {}
        if skipped:
            trash_patch["trashed_flag"] = 1
        elif int(scene.trashed_flag or 0) and bool((scene.writer_brief_json or {}).get("skipped_by_plan")):
            trash_patch["trashed_flag"] = 0
        # 规划行没有目标 / 节拍可给时沿用卡上的旧值。旧物化给没写摘要的场补的「推进本章：<章名>」只按所在章的
        # 名字认（story_slots）：卡搬去别的章之后就认不出来、又被当成作者写的目标——搬走时先去掉（S2 1）。
        # 不搬的卡原样沿用：那句话照旧认得出来，也不因此多报一场「待同步」。
        stored_goal = scene.scene_goal if leaving is None else without_retired_chapter_goal(scene.scene_goal, leaving)
        stored_beats = list(scene.beats_json or []) if leaving is None else planned_beats(scene.beats_json, leaving)
        return {
            **trash_patch,
            # 与物化同一配方（scene_card_goal）：事件 → 目标 → 题名，构思这一侧什么都给不出时沿用卡上的值
            "scene_goal": scene_card_goal(detail, fallback=stored_goal),
            "beats_json": beats or stored_beats,
            "must_include_text": plan.must_include_text or scene.must_include_text,
            "exit_change": plan.exit_change or plan.setback or plan.decision or scene.exit_change,
            "hook": plan.hook or scene.hook,
            "target_length_band": target_length_band,
            # P2：重新分章后，回流要把场景卡也搬到新章去，否则目录停留在上一版结构。
            # 章内顺序不在补丁里：它由 _settle_scene_card_order 按故事序两阶段落位，
            # 待同步判定走 _scene_card_order_drift（只比计划内的卡彼此的先后）。
            "chapter_id": plan.chapter_id or scene.chapter_id,
            "scene_type": plan.scene_type or scene.scene_type,
            "pov_character_id": plan.pov_character_id or scene.pov_character_id,
            "onstage_chars_json": list(plan.onstage_chars_json or scene.onstage_chars_json or []),
            "location": plan.location or scene.location,
            "writer_brief_json": brief,
        }

    # writer_brief_json 里承载作者内容的戏剧键：pending 检测只看它们。
    # 其余键要么是出处/标识（source、scene_plan_id/outline_plan_id、chapter_id、scene_id）、
    # 要么是 resync 补丁才回填的富化键（primary_form 与物化写的 scene_form 同义、
    # chapter_goal 汇总）——物化与 resync 两个写入方对这些键的写法天生不同，
    # 拿去整体 != 会让刚物化完的每一场都被报成待同步（纯假阳性）。
    # 场卡其余内容（scene_goal/beats/hook/location/POV/scene_type…）由顶层列对比兜底。
    _BRIEF_CONTENT_KEYS = (
        "scene_crucible", "goal", "conflict", "setback", "reaction", "dilemma", "decision", "cost_requirement",
        "expected_reader_emotion", "story_time", "exception_reason",
    )

    @staticmethod
    def _writer_brief_comparable(value: Any) -> Any:
        """writer_brief_json 的可比形态：只取戏剧内容键、剥空值（空串与缺席等价）。"""
        if not isinstance(value, dict):
            return value
        comparable: dict[str, Any] = {}
        for key in SnowflakeCatalogResyncMixin._BRIEF_CONTENT_KEYS:
            item = value.get(key)
            if item is None or (isinstance(item, str) and not item.strip()):
                continue
            comparable[key] = item
        # 阶段 C：呈现方式只在非默认（summary）时参与比较——阶段 C 之前物化的场景卡没有这个键，
        # 把「缺席」当成 full，才不会让全书在升级当刻集体报「待同步」。
        rendering_mode = str(value.get("rendering_mode") or "").strip().lower()
        if rendering_mode and rendering_mode != "full":
            comparable["rendering_mode"] = rendering_mode
        return comparable

    @staticmethod
    def _scene_card_diff(scene: SceneCard, patch: dict[str, Any]) -> dict[str, dict[str, Any]]:
        diff: dict[str, dict[str, Any]] = {}
        for field, after in patch.items():
            before = getattr(scene, field)
            if field == "writer_brief_json":
                before_cmp = SnowflakeCatalogResyncMixin._writer_brief_comparable(before)
                after_cmp = SnowflakeCatalogResyncMixin._writer_brief_comparable(after)
                # 阶段 X：构思里的题名改了也算待同步——但只对播过题名的卡比（``seeded_title`` 键在）。
                # 阶段 X 之前物化的卡没有这个键；拿「缺席」去比，全书会在升级当刻集体报「待同步」。
                if isinstance(before, dict) and "seeded_title" in before and isinstance(before_cmp, dict):
                    before_cmp["seeded_title"] = str(before.get("seeded_title") or "")
                    after_cmp["seeded_title"] = str((after or {}).get("seeded_title") or "")
                if before_cmp == after_cmp:
                    continue
            if before != after:
                diff[field] = {"before": before, "after": after}
        return diff

    @staticmethod
    def _apply_scene_card_resync(scene: SceneCard, patch: dict[str, Any]) -> None:
        for field, value in patch.items():
            setattr(scene, field, value)

    def _affected_runtime_summary(self, project_id: str, scene_ids: list[str]) -> dict[str, int]:
        unique_scene_ids = list(dict.fromkeys(scene_ids))
        if not unique_scene_ids:
            return {"final_scene_count": 0, "author_draft_count": 0, "llm_call_count": 0}
        final_scene_count = self.session.query(FinalScene).filter(FinalScene.scene_id.in_(unique_scene_ids)).count()
        author_draft_count = self.session.query(AuthorDraft).filter(
            AuthorDraft.object_type == "scene",
            AuthorDraft.object_id.in_(unique_scene_ids),
        ).count()
        llm_call_count = self.session.query(LlmCall).filter(
            LlmCall.project_id == project_id,
            LlmCall.scene_id.in_(unique_scene_ids),
        ).count()
        return {
            "final_scene_count": final_scene_count,
            "author_draft_count": author_draft_count,
            "llm_call_count": llm_call_count,
        }
