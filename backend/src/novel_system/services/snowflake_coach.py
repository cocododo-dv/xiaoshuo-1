"""雪花驻场教练与「先看 3 个方向」：教练回合、方向回合、作者意图要点的编辑，以及回合的前端口径。

教练与方向都 fail-closed（LLM 未启用即 409）；每轮教练重述的要点按 line_id 求差落表（阶段 T），方向回合被采纳时
记 adoption（阶段 U）。2026-09-30 从 ``SnowflakeWorkspaceService`` 拆出（B06-07）。
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import select

from novel_system.db.models import OperationLog, SnowflakeAssistantTurn
from novel_system.services.errors import DomainError
from novel_system.services.snowflake_character_ids import present_draft
from novel_system.services.snowflake_direction_brief import delta_changed
from novel_system.services.value_coercion import coerce_string_list

# 阶段 U：「先看 3 个方向」没带作者要求时，回合里的「我」这一行写这句
CANDIDATES_DEFAULT_ASK = "给我 3 个不同方向"


class SnowflakeCoachMixin:
    """见模块说明。与其它 ``snowflake_*`` 混入类一起组成 ``SnowflakeWorkspaceService``（B06-07）：
    方法之间照旧经 ``self`` 互相调用，名字与签名一个不改（测试与分章包依赖它们）。"""

    @staticmethod
    def _present_turn(project_id: str, turn: dict[str, Any]) -> dict[str, Any]:
        if not turn.get("candidate_patch"):
            return turn
        return {**turn, "candidate_patch": present_draft(project_id, turn["candidate_patch"])}

    # 阶段 U（2026-09-17）：「先看 3 个方向」是教练日志里的一种回合，不再是独立的「候选」页签。
    # fail-closed（LLM 未启用即 409，与教练同一条路——以前 source="fallback" + 空列表让前端自己猜）；
    # 底稿与 generate / assistant 同源（draft_override 盖在存档上），作者的要求（ask）与第 10 步的
    # 聚焦场进提示；结果落成 turn_kind=candidates 的回合，回包带整条教练历史，教练下一轮就看得到
    # 作者看过哪些方向、选了哪个。
    def fe_step_candidates(self, project_id: str, step_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        self._require_step(step_key)
        body = payload or {}
        try:
            target_chars = max(40, min(int(body.get("target_chars") or 120), 400))
        except (TypeError, ValueError):
            target_chars = 120
        use_brief = body.get("use_direction_brief")
        use_brief = True if use_brief is None else bool(use_brief)
        ask = str(body.get("ask") or "").strip()[:600]
        focus_scene_id = str(body.get("focus_scene_id") or "").strip() or None
        if step_key != "scene_details":
            focus_scene_id = None
        latest_by_step = self._latest_by_step(project.project_id)
        llm_result = self._llm.step_candidates(
            project=project,
            step_key=step_key,
            target_chars=target_chars,
            latest_by_step=latest_by_step,
            draft_override=self._merged_draft_override(
                project.project_id,
                latest_by_step,
                step_key,
                body.get("draft_override"),
                roster=self._character_roster(project.project_id, latest_by_step),
            ),
            author_ask=ask or None,
            focus_scene_id=focus_scene_id,
            author_direction_brief=(
                self._briefs.prompt_payload_for(project.project_id, step_key) if use_brief else None
            ),
        )
        candidates = list((llm_result.payload or {}).get("candidates") or [])
        if not candidates:
            raise DomainError(
                "SNOWFLAKE_CANDIDATES_EMPTY",
                "模型这次没有给出可用的方向，请再试一次（可以在输入框里把要求说得更具体）。",
                status_code=502,
                details={"node_id": "snowflake_step_candidates", "step_key": step_key, "llm_call_id": llm_result.llm_call_id},
            )
        turn = self._record_assistant_turn(
            project.project_id,
            step_key=step_key,
            message=ask or CANDIDATES_DEFAULT_ASK,
            focus_scene_id=focus_scene_id,
            result={"reply": "", "suggestions": [], "source": llm_result.source, "llm_call_id": llm_result.llm_call_id},
            turn_kind="candidates",
            candidates={"items": candidates, "target_chars": target_chars},
        )
        return {
            "source": llm_result.source,
            "llm_call_id": llm_result.llm_call_id,
            "candidates": candidates,
            "turn_id": turn.turn_id,
            "turn": self._present_turn(project.project_id, self._assistant_turn_payload(turn)),
            "assistant_history": self._presented_history(project.project_id),
        }

    def request_assistant(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        project = self._require_snowflake_project(project_id)
        body = payload or {}
        latest_by_step = self._latest_by_step(project.project_id)
        workspace = self._workspace_payload(project.project_id, lean=True)
        step_key = str(body.get("step_key") or workspace.get("current_step_key") or "book_brief").strip() or "book_brief"
        step = self._step_from_workspace(workspace, step_key)
        step = self._step_with_override(
            project.project_id,
            step,
            body.get("draft_override"),
            latest_by_step=latest_by_step,
            roster=self._character_roster(project.project_id, latest_by_step),
        )
        approved_context = self._approved_context(workspace)
        focus_scene_id = str(body.get("focus_scene_id") or "").strip() or None
        # 阶段 T：教练有记忆——当前要点（含作者撤下的）、继承的全书级要点、本步最近几轮问答
        conversation = self._briefs.conversation_payload(
            project.project_id,
            step_key,
            turns=workspace.get("assistant_history") or [],
        )
        llm_result = self._llm.assistant_reply(
            project=workspace["project"],
            step=step,
            message=str(body.get("message") or ""),
            approved_context=approved_context,
            latest_by_step=latest_by_step,
            focus_scene_id=focus_scene_id,
            conversation=conversation,
        )
        brief_update = llm_result.payload.get("brief_update")
        result = {
            **{key: value for key, value in llm_result.payload.items() if key != "brief_update"},
            "step_key": step_key,
            "source": llm_result.source,
            "llm_call_id": llm_result.llm_call_id,
        }
        turn = self._record_assistant_turn(
            project.project_id,
            step_key=step_key,
            message=str(body.get("message") or ""),
            focus_scene_id=focus_scene_id,
            result=result,
        )
        # 教练本轮对作者意图的完整重述 → 按 line_id 求差落到要点表（作者的条目不归教练管）
        _row, brief_delta = self._briefs.record_coach_restatement(
            project.project_id, step_key, brief_update, turn_id=turn.turn_id
        )
        if delta_changed(brief_delta):
            # 阶段 U：差异随回合落表——日志里每一轮自己说「要点 +1 / 改 1 / 撤 1」，不靠一闪而过的提示
            turn.brief_delta_json = deepcopy(brief_delta)
            self.session.add(
                OperationLog(
                    event_type="snowflake_direction_brief_restated",
                    object_type="snowflake_direction_brief",
                    object_ref=f"{project.project_id}:{step_key}",
                    payload_json={"project_id": project.project_id, "step_key": step_key, "turn_id": turn.turn_id, **brief_delta},
                )
            )
        history = self._presented_history(project.project_id)
        if result.get("candidate_patch"):
            result = {**result, "candidate_patch": present_draft(project.project_id, result["candidate_patch"])}
        return {
            **result,
            "turn_id": turn.turn_id,
            "created_at": turn.created_at,
            "assistant_history": history,
            "direction_brief": self._briefs.payload_for(project.project_id, step_key),
            "brief_delta": brief_delta,
        }

    def update_direction_brief(self, project_id: str, step_key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """阶段 T：作者编辑本步意图要点（撤下 / 改写 / 加条 / 切换范围 / 恢复 / 是否继承上游）。
        请求里的 lines 就是作者要的列表；作者改过的条目归作者，教练此后不能再改写或撤下。"""
        project = self._require_snowflake_project(project_id)
        self._require_step(step_key)
        body = payload or {}
        lines = body.get("lines")
        if lines is not None and not isinstance(lines, list):
            raise DomainError("SNOWFLAKE_DIRECTION_BRIEF_INVALID", "要点列表必须是数组。", status_code=400)
        inherit = body.get("inherit_upstream")
        row = self._briefs.save_author_edit(
            project.project_id,
            step_key,
            lines=[item for item in (lines or []) if isinstance(item, dict)] if lines is not None else None,
            inherit_upstream=bool(inherit) if inherit is not None else None,
        )
        self.session.add(
            OperationLog(
                event_type="snowflake_direction_brief_edited",
                object_type="snowflake_direction_brief",
                object_ref=row.brief_id,
                payload_json={
                    "project_id": project.project_id,
                    "step_key": step_key,
                    "revision": row.revision,
                    "active_count": sum(1 for line in (row.lines_json or []) if isinstance(line, dict) and line.get("status") == "active"),
                    "inherit_upstream": bool(row.inherit_upstream),
                },
            )
        )
        self.session.flush()
        return {"direction_brief": self._briefs.payload_for(project.project_id, step_key)}

    def _presented_history(self, project_id: str) -> list[dict[str, Any]]:
        return [self._present_turn(project_id, turn) for turn in self._assistant_history(project_id)]

    def _assistant_history(self, project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.session.execute(
            select(SnowflakeAssistantTurn)
            .where(SnowflakeAssistantTurn.project_id == project_id)
            .order_by(SnowflakeAssistantTurn.created_at.desc(), SnowflakeAssistantTurn.turn_id.desc())
            .limit(max(1, min(int(limit or 50), 200)))
        ).scalars().all()
        return [self._assistant_turn_payload(row) for row in reversed(rows)]

    def _record_assistant_turn(
        self,
        project_id: str,
        *,
        step_key: str,
        message: str,
        focus_scene_id: str | None,
        result: dict[str, Any],
        turn_kind: str = "chat",
        candidates: dict[str, Any] | None = None,
    ) -> SnowflakeAssistantTurn:
        turn = SnowflakeAssistantTurn(
            turn_id=f"snowflake_assistant_turn_{project_id}_{uuid.uuid4().hex[:10]}",
            project_id=project_id,
            step_key=step_key,
            focus_scene_id=focus_scene_id,
            user_message=str(message or "").strip(),
            reply=str(result.get("reply") or "").strip(),
            suggestions_json=coerce_string_list(result.get("suggestions")),
            candidate_label=str(result.get("candidate_label") or "").strip() or None,
            candidate_patch_json=deepcopy(result.get("candidate_patch") or {}) or None,
            source=str(result.get("source") or "fallback").strip() or "fallback",
            llm_call_id=str(result.get("llm_call_id") or "").strip() or None,
            turn_kind="candidates" if turn_kind == "candidates" else "chat",
            candidates_json=deepcopy(candidates) if candidates else None,
        )
        self.session.add(turn)
        self.session.flush()
        return turn

    def _resolve_direction_turn(
        self,
        project_id: str,
        body: dict[str, Any],
        *,
        direction_text: str,
    ) -> tuple[SnowflakeAssistantTurn | None, int | None, str | None]:
        """阶段 U：方向来源的教练回合。返回 (回合, 方向回合里的第几条, 那一条的标签)；没指回合 → (None, None, None)。"""
        turn_id = str(body.get("direction_turn_id") or "").strip()
        if not turn_id:
            return None, None, None
        if not direction_text:
            raise DomainError(
                "SNOWFLAKE_DIRECTION_TEXT_REQUIRED",
                "指明了方向来源的回合，却没有带方向正文。",
                status_code=400,
                details={"direction_turn_id": turn_id},
            )
        turn = self.session.get(SnowflakeAssistantTurn, turn_id)
        if turn is None or turn.project_id != project_id:
            raise DomainError(
                "SNOWFLAKE_DIRECTION_TURN_NOT_FOUND",
                "方向来源的教练回合不存在（可能已被清理），请重新让教练给方向。",
                status_code=404,
                details={"direction_turn_id": turn_id},
            )
        if turn.turn_kind != "candidates":
            return turn, None, None
        items = list((turn.candidates_json or {}).get("items") or [])
        raw_index = body.get("direction_index")
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = -1
        if index < 0 or index >= len(items):
            raise DomainError(
                "SNOWFLAKE_DIRECTION_INDEX_INVALID",
                "要采纳的方向编号不在这一组方向里。",
                status_code=400,
                details={"direction_turn_id": turn_id, "direction_index": raw_index, "count": len(items)},
            )
        item = items[index] if isinstance(items[index], dict) else {}
        return turn, index, (str(item.get("label") or "").strip() or None)

    @staticmethod
    def _assistant_turn_payload(row: SnowflakeAssistantTurn) -> dict[str, Any]:
        turn_kind = "candidates" if (row.turn_kind or "chat") == "candidates" else "chat"
        return {
            "turn_id": row.turn_id,
            "project_id": row.project_id,
            "step_key": row.step_key,
            "focus_scene_id": row.focus_scene_id or None,
            "message": row.user_message or "",
            "reply": row.reply or "",
            "suggestions": list(row.suggestions_json or []),
            "candidate_label": row.candidate_label or None,
            "candidate_patch": deepcopy(row.candidate_patch_json or {}) or None,
            "source": row.source or "fallback",
            "llm_call_id": row.llm_call_id,
            "created_at": row.created_at,
            # 阶段 U：回合种类（chat / candidates）、方向回合的几条方向、本轮要点差异、被哪一版生成采纳过
            "turn_kind": turn_kind,
            "candidates": (
                [deepcopy(item) for item in ((row.candidates_json or {}).get("items") or []) if isinstance(item, dict)]
                if turn_kind == "candidates"
                else []
            ),
            "brief_delta": deepcopy(row.brief_delta_json) if row.brief_delta_json else None,
            "adoption": deepcopy(row.adoption_json) if row.adoption_json else None,
        }
