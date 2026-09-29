"""雪花一步的版本（``snowflake_step_runs`` 表）：最新版、下一版号、造一版、原位改写待审版、确认时让位、历史与抹空保护。

生成、保存、恢复三处以前各自拼一遍 ``SnowflakeStepRun(step_run_id=…, version=…)``，保存又自己写一遍「原位改写
待审版要清掉哪些失效留痕」。现在都经 :class:`StepRunStore`（B06-17）。叶子模块：只依赖模型与两个只读叶子。
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from novel_system.db.models import OperationLog, SnowflakeStepRun
from novel_system.services.snowflake_queries import latest_by_step, next_step_version
from novel_system.services.snowflake_staleness import semantic_payload

#: 抹空保护新起一版时记的操作日志（历史列表据此标出「保住的是哪一版」）
WIPE_PRESERVED_EVENT = "snowflake_step_wipe_preserved"

# 抹空保护：一步草稿里不算「故事文字」的键——身份、枚举与排序，空白默认稿也会带着它们（如角色步的 role=主角）
_NON_STORY_KEYS = frozenset({
    "character_id", "row_uid", "scene_id", "scene_plan_id", "chapter_id", "role", "primary_form", "scene_type",
    "rendering_mode", "target_length_band", "act", "chapter_seq", "scene_seq", "spine", "status", "version",
    "pov_character_id", "protagonist_character_id", "onstage_chars_json",
})
_WIPE_GUARD_MIN_CHARS = 8  # 两三个字的试笔被清空不值得多留一版


def _story_text_chars(value: Any, key: str | None = None) -> int:
    if key is not None and (key in _NON_STORY_KEYS or str(key).startswith("fe_")):
        return 0
    if isinstance(value, str):
        return len(value.strip())
    if isinstance(value, list):
        return sum(_story_text_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(_story_text_chars(item, str(name)) for name, item in value.items())
    return 0


def would_wipe_story(previous: Any, incoming: Any) -> bool:
    """整步抹空：旧稿有成段的故事文字，新稿一个字都没有（只剩身份 / 枚举 / fe_* 写穿键）。"""
    return (
        _story_text_chars(semantic_payload(previous if isinstance(previous, dict) else {})) >= _WIPE_GUARD_MIN_CHARS
        and _story_text_chars(semantic_payload(incoming if isinstance(incoming, dict) else {})) == 0
    )


def draft_summary(value: Any, *, limit: int = 180) -> str:
    """历史列表里一版草稿的一行摘要（前几段文字拼起来，截到 ``limit`` 字）。"""
    pieces: list[str] = []

    def visit(item: Any) -> None:
        if len(" ".join(pieces)) >= limit:
            return
        if isinstance(item, str):
            text = " ".join(item.split())
            if text:
                pieces.append(text)
            return
        if isinstance(item, list):
            for child in item[:8]:
                visit(child)
            return
        if isinstance(item, dict):
            for child in item.values():
                visit(child)

    visit(value)
    summary = " ".join(pieces)
    return summary[:limit].rstrip()


def step_run_payload(run: SnowflakeStepRun | None, *, include_diagnosis: bool = True) -> dict[str, Any] | None:
    """一版草稿的元数据。``diagnosis_json`` 是 ``health`` 的一份深拷贝，只有 GET 的工作台还带它（B06-05）。"""
    if run is None:
        return None
    payload = {
        "step_run_id": run.step_run_id,
        "artifact_id": run.step_run_id,
        "step_key": run.step_key,
        "version": run.version,
        "status": run.status,
        "diagnosis_json": deepcopy(run.health_json or {}),
        "llm_call_id": run.llm_call_id,
        "approved_at": run.approved_at,
        "stale_reason": run.stale_reason,
        "stale_accepted_at": run.stale_accepted_at,
        "stale_accepted_by": run.stale_accepted_by,
        "stale_accepted_note": run.stale_accepted_note,
        # 阶段 E：本版草稿写入时消费的上游 step_run_id（按 step_key）——前端用它对照
        # 各上游现在的 step_run_id，把「上游改了什么」拉成消费版本 vs 当前版本的 diff。
        "input_refs": deepcopy(run.input_refs_json or {}),
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }
    if not include_diagnosis:
        del payload["diagnosis_json"]
    return payload


def step_run_history_payload(run: SnowflakeStepRun, *, include_draft: bool = False) -> dict[str, Any]:
    """历史列表里的一版（草稿只在预览某一版时带）。"""
    payload = {
        "step_run_id": run.step_run_id,
        "version": run.version,
        "status": run.status,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "approved_at": run.approved_at,
        "stale_reason": run.stale_reason,
        "stale_accepted_at": run.stale_accepted_at,
        "stale_accepted_by": run.stale_accepted_by,
        "stale_accepted_note": run.stale_accepted_note,
        "generation_source": str((run.health_json or {}).get("generation_source") or ""),
        "trigger_source": str((run.health_json or {}).get("trigger_source") or ""),
        "draft_summary": draft_summary(run.draft_json or {}),
    }
    if include_draft:
        payload["draft"] = deepcopy(run.draft_json or {})
    return payload


class StepRunStore:
    """一步的版本：读最新、造新版、原位改写待审版、让位、历史（B06-17）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def latest_by_step(self, project_id: str) -> dict[str, SnowflakeStepRun]:
        """每一步最新的一版（未被取代的最高版本）。"""
        return latest_by_step(self.session, SnowflakeStepRun, project_id)

    def next_version(self, project_id: str, step_key: str) -> int:
        return next_step_version(self.session, SnowflakeStepRun, project_id, step_key)

    def latest_confirmed(self, project_id: str, step_key: str, *, exclude_step_run_id: str) -> SnowflakeStepRun | None:
        """这一步除 ``exclude_step_run_id`` 外最近确认（或略过）的一版——重新确认时拿它算下游失效范围。"""
        return self.session.execute(
            select(SnowflakeStepRun)
            .where(
                SnowflakeStepRun.project_id == project_id,
                SnowflakeStepRun.step_key == step_key,
                SnowflakeStepRun.step_run_id != exclude_step_run_id,
                SnowflakeStepRun.status.in_(["approved", "skipped"]),
            )
            .order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.created_at.desc())
        ).scalars().first()

    def new_run(
        self,
        project_id: str,
        step_key: str,
        *,
        draft: dict[str, Any],
        status: str,
        health: dict[str, Any],
        input_refs: dict[str, Any],
        llm_call_id: str | None = None,
        approved_at: str | None = None,
    ) -> SnowflakeStepRun:
        """造这一步的下一版并加进会话（版本号 = 这一步已有的最高版本 + 1）。"""
        run = SnowflakeStepRun(
            step_run_id=f"snowflake_step_run_{project_id}_{step_key}_{uuid.uuid4().hex[:10]}",
            project_id=project_id,
            step_key=step_key,
            version=self.next_version(project_id, step_key),
            status=status,
            draft_json=draft,
            health_json=health,
            input_refs_json=input_refs,
            llm_call_id=llm_call_id,
            approved_at=approved_at,
        )
        self.session.add(run)
        return run

    @staticmethod
    def rewrite_pending(
        run: SnowflakeStepRun, *, draft: dict[str, Any], health: dict[str, Any], input_refs: dict[str, Any]
    ) -> None:
        """待审版原位改写（不为每次键入造一版）：草稿、健康度、消费的上游换新，失效留痕清空。"""
        run.draft_json = draft
        run.input_refs_json = input_refs
        run.health_json = health
        run.stale_reason = None
        run.stale_accepted_at = None
        run.stale_accepted_by = None
        run.stale_accepted_note = None

    def supersede_others(self, run: SnowflakeStepRun) -> None:
        """``run`` 确认 / 略过时，这一步以前确认过的版本让位（superseded）。"""
        rows = self.session.execute(
            select(SnowflakeStepRun).where(
                SnowflakeStepRun.project_id == run.project_id,
                SnowflakeStepRun.step_key == run.step_key,
                SnowflakeStepRun.step_run_id != run.step_run_id,
                SnowflakeStepRun.status.in_(["approved", "skipped"]),
            )
        ).scalars().all()
        for row in rows:
            row.status = "superseded"

    def history(self, project_id: str, step_key: str, *, step_run_id: str | None = None) -> list[SnowflakeStepRun]:
        """这一步的版本，新的在前；给了 ``step_run_id`` 就只取那一版（取不到返回空表）。"""
        query = select(SnowflakeStepRun).where(SnowflakeStepRun.project_id == project_id, SnowflakeStepRun.step_key == step_key)
        wanted = str(step_run_id or "").strip()
        if wanted:
            query = query.where(SnowflakeStepRun.step_run_id == wanted)
        return list(
            self.session.execute(
                query.order_by(SnowflakeStepRun.version.desc(), SnowflakeStepRun.updated_at.desc(), SnowflakeStepRun.created_at.desc())
            ).scalars().all()
        )

    def wipe_guard_preservations(self, step_run_ids: list[str]) -> dict[str, str]:
        """抹空保护新起的版本 → 它保住的上一版（``snowflake_step_wipe_preserved`` 操作日志）。"""
        if not step_run_ids:
            return {}
        rows = self.session.execute(
            select(OperationLog.object_ref, OperationLog.payload_json).where(
                OperationLog.event_type == WIPE_PRESERVED_EVENT,
                OperationLog.object_type == "snowflake_step_run",
                OperationLog.object_ref.in_(step_run_ids),
            )
        ).all()
        return {
            str(object_ref): str((payload or {}).get("preserved_step_run_id") or "")
            for object_ref, payload in rows
            if (payload or {}).get("preserved_step_run_id")
        }
