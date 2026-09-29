"""雪花结构化步骤的落库同步：角色三步 → 角色计划与角色行，09 / 10 → 场景计划行（对位、铸身份、删场核对、章内序）。

一步的草稿每次保存 / 生成 / 恢复 / 确认都经 ``_sync_structured_step_data`` 落到结构化的行上；07 的章表同步
（``_sync_chapter_plans``）是分章的缝，留在 ``snowflake_workspace``（分章包之后整体搬走）。
2026-09-30 从 ``SnowflakeWorkspaceService`` 拆出（B06-07）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from novel_system.db.models import (
    OperationLog,
    SnowflakeCharacterPlan,
    SnowflakeScenePlan,
    SnowflakeStepRun,
    StoryCharacter,
    StoryProject,
    utcnow,
)
from novel_system.services.snowflake_scene_order import positions_from_rows, renumber_scene_seq
from novel_system.services.snowflake_scene_rows import (
    SCENE_LIST_OWNED_FIELDS,
    mint_row_uid,
    mint_scene_id,
    sanitize_scene_patch,
    scene_plan_content_signature,
    scene_plan_payload,
)
from novel_system.services.snowflake_step_catalog import (
    CHARACTER_STEPS,
    RENDERING_MODES,
    coerce_scene_form,
    effective_rendering_mode,
)
from novel_system.services.snowflake_step_diagnosis import diagnose_scene_detail
from novel_system.services.value_coercion import coerce_string_list, int_or_default


class SnowflakeStructuredSyncMixin:
    """见模块说明。与其它 ``snowflake_*`` 混入类一起组成 ``SnowflakeWorkspaceService``（B06-07）：
    方法之间照旧经 ``self`` 互相调用，名字与签名一个不改（测试与分章包依赖它们）。"""

    def _sync_structured_step_data(
        self,
        project: StoryProject,
        step_key: str,
        draft: dict[str, Any],
        run: SnowflakeStepRun,
        *,
        approved: bool = False,
    ) -> dict[str, Any] | None:
        """同步结构化步数据。返回「作者必须知道、但不属于草稿」的事实（目前只有章表收缩）。"""
        if step_key in CHARACTER_STEPS:
            self._sync_character_plans(project.project_id, step_key, draft.get("characters") or [], approved=approved)
        if step_key == "long_synopsis":
            return self._sync_chapter_plans(project.project_id, draft, run, approved=approved)
        if step_key in {"scene_list", "scene_details"}:
            self._sync_scene_plans(project.project_id, step_key, draft.get("scenes") or [], run, approved=approved)
        return None

    def _sync_character_plans(self, project_id: str, step_key: str, characters: list[Any], *, approved: bool) -> None:
        # 草稿已按规范口径落库（角色 id 带作品前缀、缺 id 的已铸号）；按 character_id 一次预读本作品的角色计划
        plans = {
            row.character_id: row
            for row in self.session.execute(
                select(SnowflakeCharacterPlan).where(SnowflakeCharacterPlan.project_id == project_id)
            ).scalars()
        }
        for item in characters:
            if not isinstance(item, dict):
                continue
            character_id = str(item.get("character_id") or "").strip()
            if not character_id:
                continue  # 完全空的成员不进名册（也没有铸号）
            display_name = str(item.get("display_name") or item.get("name") or character_id).strip()
            plan = plans.get(character_id)
            if plan is None:
                # 旧规则 snowflake_character_plan_{project}_{raw} 恰好等于 snowflake_character_plan_{规范 id}
                plan = SnowflakeCharacterPlan(
                    character_plan_id=f"snowflake_character_plan_{character_id}",
                    project_id=project_id,
                    character_id=character_id,
                    display_name=display_name,
                )
                self.session.add(plan)
                plans[character_id] = plan
            plan.display_name = display_name
            plan.role = item.get("role") or plan.role
            plan.source_step_key = step_key
            plan.status = "approved" if approved else "draft"
            plan.stale_reason = None
            if step_key == "character_sheets":
                plan.summary_json = item
            elif step_key == "character_synopses":
                plan.synopsis_json = item
            elif step_key == "character_bibles":
                plan.bible_json = item

            if approved and step_key in {"character_sheets", "character_bibles"}:
                self._sync_story_character(project_id, character_id, display_name, item, step_key)

    def _sync_story_character(self, project_id: str, character_id: str, display_name: str, item: dict[str, Any], step_key: str) -> None:
        row = self.session.get(StoryCharacter, character_id)
        if row is not None and row.project_id != project_id:
            # 全局主键上别的作品的人：绝不改写（B06-01 修掉的就是这个跨作品覆盖）
            return
        if row is None:
            row = StoryCharacter(
                character_id=character_id,
                project_id=project_id,
                display_name=display_name,
                role=item.get("role"),
                summary_json={},
                bible_json={},
                status="approved",
            )
            self.session.add(row)
        row.display_name = display_name
        row.role = item.get("role") or row.role
        row.status = "approved"
        if step_key == "character_sheets":
            row.summary_json = item
        elif step_key == "character_bibles":
            row.bible_json = item

    def _sync_scene_plans(
        self,
        project_id: str,
        step_key: str,
        scenes: list[Any],
        run: SnowflakeStepRun,
        *,
        approved: bool,
    ) -> None:
        current_chapter_id = f"{project_id}_CH01"
        seq_by_chapter: dict[str, int] = {}
        minted = False
        # P1-2：同一份 payload 里出现两次的 row_uid 必须拆开。前端 addScene 曾用
        # `"S" + (list.length + 1)` 编号，删掉中间一场后新增就会撞上仍然存活的那一场，
        # 于是后一条会绑到前一条的行上，把它的内容整段覆盖掉。
        seen_row_uids: set[str] = set()
        seen_scene_ids: set[str] = set()
        touched_row_uids: set[str] = set()
        # 本作品的全部场景计划（含软删的：同一 row_uid 回来要复活它）一次读进来，逐行对位查字典——
        # 以前每一行两三条查询，60 场的自动保存光对位就是一百多条语句（B06-06）。新建与认领 row_uid 时同步更新两张表。
        known_plans = list(
            self.session.execute(select(SnowflakeScenePlan).where(SnowflakeScenePlan.project_id == project_id)).scalars()
        )
        by_row_uid: dict[str, SnowflakeScenePlan] = {}
        by_scene_id: dict[str, SnowflakeScenePlan] = {}
        for known in known_plans:
            if known.row_uid:
                by_row_uid.setdefault(known.row_uid, known)
            if known.scene_id:
                by_scene_id.setdefault(known.scene_id, known)
        for index, item in enumerate(scenes, start=1):
            if not isinstance(item, dict):
                continue
            # Identity is anchored on the immutable row_uid (P1-1). Fall back to the
            # legacy scene_id lookup so step-9 drafts and pre-migration rows still
            # bind to the plan that step-8 seeded — but never trust an author's edit
            # of scene_id / chapter_id to *re-key* an existing row.
            row_uid = str(item.get("row_uid") or "").strip()
            incoming_scene_id = str(item.get("scene_id") or "").strip()
            # 本次 payload 内重号：当作一条新戏重新铸造身份，且**不再**走 scene_id 回退
            # ——否则回退会把它又认到刚被前一条占用的那一行上，等于没拆。
            duplicate_in_payload = bool(row_uid) and row_uid in seen_row_uids
            if duplicate_in_payload:
                row_uid = ""
            plan = by_row_uid.get(row_uid) if row_uid else None
            if plan is None and not duplicate_in_payload and incoming_scene_id:
                # row_uid 缺席或未命中时回退到 scene_id 查找：规划器骨架与 LLM 结构化输出
                # 只回显 scene_id（提示词明确要求 row_uid 留空），这条回退是第 10 步能绑回
                # 第 9 步建下的行、而不是每次生成都复制一份的唯一依据。
                plan = by_scene_id.get(incoming_scene_id)
            if plan is not None and plan.row_uid and plan.row_uid in seen_row_uids:
                plan = None  # 已被本轮前一条认领，不能二次绑定
            created = plan is None
            if plan is not None and plan.removed_at:
                # 作者把删掉的场又加了回来（同一 row_uid）：复活，而不是撞唯一索引。
                plan.removed_at = None
                plan.removed_by = None
            if plan is not None and plan.orphaned_flag:
                # 孤儿标记必须在这里清，不能只在上面那个「复活」分支里清：已物化的场被删时
                # 走的是**打标记不软删**那条路（removed_at 保持 NULL），所以复活分支永远
                # 摸不到它。结果是 orphaned_flag 只写不清，分章面板的 blocker 永久挂着、
                # 「确认分章」按钮再也点不动——而它自己的提示语还写着「请先决定」。
                # 场回到了场景列表里，按定义就不再是孤儿。
                plan.orphaned_flag = 0

            input_chapter_id = str(item.get("chapter_id") or current_chapter_id or f"{project_id}_CH01").strip()
            if created:
                # First time we see this row — mint its identity exactly once.
                chapter_id = input_chapter_id
            else:
                # Already exists — system identity is locked, author input is ignored.
                chapter_id = plan.chapter_id or input_chapter_id
            current_chapter_id = chapter_id

            # scene_seq = 这一场在它所在章里的位置，按草稿行序逐章计数；草稿行自带的 scene_seq 不再采信——
            # 前端 09 发的是全书序 i + 1，模型给的什么都有，混进来会让同一列有两种语义
            # （循环结束后 renumber_scene_seq 还会按故事序对**全部**活跃场统一重算一遍）。
            scene_seq = seq_by_chapter.get(chapter_id, 0) + 1
            seq_by_chapter[chapter_id] = scene_seq

            if created:
                row_uid = row_uid or mint_row_uid()
                # P1-2 铸造规则：草稿自带 scene_id 就沿用它（骨架/LLM 输出靠这个字符串
                # 在第 9→10 步之间对位；换成别的值会让第 10 步认不回第 9 步的行）。只有
                # 在它缺席或已被占用时才用 row_uid 铸——前端 canonFromFE 恰好不发
                # scene_id，所以作者手改场景表这一路始终走 row_uid 基、天然不撞号。
                scene_id = incoming_scene_id or mint_scene_id(project_id, row_uid)
                if scene_id in seen_scene_ids or scene_id in by_scene_id:
                    scene_id = mint_scene_id(project_id, row_uid)
                plan = SnowflakeScenePlan(
                    scene_plan_id=f"snowflake_scene_plan_{project_id}_{row_uid}",
                    project_id=project_id,
                    row_uid=row_uid,
                    scene_id=scene_id,
                    chapter_id=chapter_id,
                    scene_seq=scene_seq,
                )
                self.session.add(plan)
                known_plans.append(plan)
                by_row_uid.setdefault(row_uid, plan)
                by_scene_id.setdefault(scene_id, plan)
                minted = True
            else:
                scene_id = plan.scene_id
                if not plan.row_uid:
                    # Adopt a row_uid for a legacy row matched via scene_id.
                    plan.row_uid = row_uid or mint_row_uid()
                    by_row_uid.setdefault(plan.row_uid, plan)
                    minted = True
                row_uid = plan.row_uid

            before = None if created else scene_plan_content_signature(plan)
            plan.scene_seq = scene_seq
            plan.source_step_run_id = run.step_run_id
            # Discard any author-supplied scene_id / chapter_id — those are system
            # identity, not editable narrative fields.
            patch = sanitize_scene_patch(item)
            patch.pop("scene_id", None)
            patch.pop("chapter_id", None)
            if step_key == "scene_details" and not created:
                # 形态与视角归 09（场景列表）：第 10 步只深化三拍，不改已有行的这两样（F02-01 的后端兜底——
                # 前端第 10 步曾把渲染时的默认值冻进本地计划，推 10 时把 09 刚改过的形态与视角改回去）。
                for key in SCENE_LIST_OWNED_FIELDS:
                    patch.pop(key, None)
            self._apply_scene_patch(plan, patch)
            # 阶段 G：草稿同步只把**内容真的变了**（或新建）的场打回 draft；「AI 补全这一场」和
            # 一次无谓的整表 PATCH 不再把其余几十场的确认与复核留痕一起清零。批准仍然整表置 approved。
            if approved or created or before != scene_plan_content_signature(plan):
                plan.status = "approved" if approved else "draft"
                plan.stale_reason = None
                plan.stale_accepted_at = None
                plan.stale_accepted_by = None
                plan.stale_accepted_note = None
            if created and not plan.title:
                plan.title = str(item.get("title") or item.get("summary") or f"场景 {index:02d}").strip()
            if created and not plan.chapter_title:
                plan.chapter_title = str(item.get("chapter_title") or chapter_id).strip()
            plan.diagnosis_json = diagnose_scene_detail(scene_plan_payload(plan))

            seen_row_uids.add(row_uid)
            seen_scene_ids.add(scene_id)
            touched_row_uids.add(row_uid)

            # Stamp the minted identity back onto the draft row so the persisted
            # draft_json and every later re-seed carry the same stable anchor.
            if item.get("row_uid") != row_uid or item.get("scene_id") != scene_id or item.get("chapter_id") != chapter_id:
                item["row_uid"] = row_uid
                item["scene_id"] = scene_id
                item["chapter_id"] = chapter_id
                minted = True

        if step_key == "scene_list" and touched_row_uids:
            self._reconcile_removed_scene_plans(project_id, touched_row_uids, plans=known_plans)

        # 章内序统一重算（scene_seq 的唯一写入方）。故事序的来源：09 同步用**这一份**草稿的行序；
        # 第 10 步的草稿可能只带回一部分场（分批生成 / 单场补全），不能拿它当全书顺序——读最新 09 草稿。
        self.session.flush()
        renumber_scene_seq(
            self.session,
            project_id,
            positions=positions_from_rows(scenes) if step_key == "scene_list" else None,
        )

        if minted and isinstance(run.draft_json, dict):
            run.draft_json = {**run.draft_json, "scenes": scenes}
            # 行是就地改的：旧值与新值在 JSON 比较下相等，SQLAlchemy 会判「没变」而不写库——
            # 铸好的 row_uid / scene_id 于是只活在本次回包里，落库的草稿仍然没有身份。
            flag_modified(run, "draft_json")

    def _reconcile_removed_scene_plans(
        self,
        project_id: str,
        kept_row_uids: set[str],
        *,
        plans: list[SnowflakeScenePlan] | None = None,
    ) -> None:
        """P1-3 收口：把不在本次场景列表里的场标记为已删除。

        只在 ``scene_list`` 步生效 —— 「哪些场存在」是第 9 步的职责，第 10 步只负责
        深化，它的草稿如果因为 LLM 截断少返回几场，绝不能因此删掉作者的场。

        两条护栏：
        - ``kept_row_uids`` 为空（空草稿）时调用方不会进来，避免一次空 PATCH 清空全书。
        - 已经物化成 ``SceneCard`` 的场只打 ``orphaned_flag``，不软删 —— 那边可能已经
          有正文了，删不删要作者自己决定。
        """
        if plans is None:
            rows = self.session.execute(
                select(SnowflakeScenePlan).where(
                    SnowflakeScenePlan.project_id == project_id,
                    SnowflakeScenePlan.removed_at.is_(None),
                )
            ).scalars().all()
        else:
            # 调用方（``_sync_scene_plans``）已经读过本作品的全部计划、也带上了这次新建的：按同一条件在内存里筛
            rows = [plan for plan in plans if plan.project_id == project_id and plan.removed_at is None]
        leaving = [plan for plan in rows if (plan.row_uid or "") not in kept_row_uids]
        cards = self._scene_cards_by_id(project_id, [plan.scene_id for plan in leaving])
        removed_at = utcnow()
        for plan in leaving:
            materialized = cards.get(plan.scene_id)
            if materialized is not None:
                if plan.orphaned_flag:
                    continue
                plan.orphaned_flag = 1
                event_type = "snowflake_scene_plan_orphaned"
            else:
                plan.removed_at = removed_at
                plan.removed_by = "operator"
                event_type = "snowflake_scene_plan_removed"
            self.session.add(
                OperationLog(
                    event_type=event_type,
                    object_type="snowflake_scene_plan",
                    object_ref=plan.scene_plan_id,
                    payload_json={
                        "project_id": project_id,
                        "scene_id": plan.scene_id,
                        "row_uid": plan.row_uid or "",
                        "title": plan.title or plan.summary or "",
                        "removed_at": removed_at,
                    },
                )
            )

    def _apply_scene_patch(self, scene: SnowflakeScenePlan, patch: dict[str, Any]) -> None:
        if "crucible" in patch and "scene_crucible" not in patch:
            patch["scene_crucible"] = patch["crucible"]
        for key, value in patch.items():
            if key == "crucible":
                continue
            if key == "primary_form":
                scene_type = str(value or "").strip().lower()
                scene.scene_type = coerce_scene_form(scene_type)
                continue
            if not hasattr(scene, key):
                continue
            if key in {"onstage_chars_json", "beats_json"}:
                setattr(scene, key, coerce_string_list(value))
            elif key == "scene_seq":
                setattr(scene, key, int_or_default(value, scene.scene_seq or 1))
            elif key == "scene_type":
                scene_type = str(value or "").strip().lower()
                setattr(scene, key, coerce_scene_form(scene_type))
            elif key == "rendering_mode":
                mode = str(value or "").strip().lower()
                scene.rendering_mode = mode if mode in RENDERING_MODES else "full"
            else:
                setattr(scene, key, str(value or "").strip())
        # 补丁键的顺序不可依赖（SCENE_PATCH_FIELDS 是集合）：类型定下来之后再统一收口——
        # 阶段 N：概述对两种形态都合法，略过只给反应场。
        scene.rendering_mode = effective_rendering_mode(scene.scene_type, scene.rendering_mode)
