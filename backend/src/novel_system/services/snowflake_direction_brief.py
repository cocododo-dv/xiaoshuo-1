"""作者意图要点（direction brief）：教练对话 → 作者可编辑的本步意图 → 生成 / 候选 / 分诊的受保护输入。

2026-09-16 阶段 T。此前雪花模块的三个 LLM 入口互不知情：教练每轮是单发调用（连自己上一轮都看不到），
三候选与整步生成拿不到任何对话内容，唯一的桥「应用补丁」带的是内容不是意图。现在：

- 教练在同一次调用里重述作者此刻对本步的意图（``brief_update.lines``：决定 / 否决 / 约束 / 待定，
  本步 / 全书），服务端按 ``line_id`` 求差（新增 / 改写 / 撤下）落到 ``snowflake_direction_briefs``；
  探索性提问只能记为「待定」，教练不替作者拍板。
- 作者能看到并编辑每一条（撤下 / 改写 / 加条 / 切换范围 / 恢复）；作者写过或改过的条目归作者，
  教练不能再改写或撤下；作者撤下的条目教练不能复活。
- 整步生成、三候选、分诊读入当前活动条目（加上游各步的「全书」级条目，作者可关掉继承），作为
  ``snowflake_prompt_budget.PROTECTED_KEYS`` 里的 ``author_direction_brief``——条目数与单条长度都有硬上限，
  受保护也不会失控。
- 生成出的版本在 ``health_json.direction_brief`` 记录消费了哪一版（revision / sha / line_ids），前端据此
  提示「本稿未采用最新要点」。

本模块是叶子：只依赖 db.models / hash_engine / snowflake_steps。``snowflake_workspace_llm`` 与
``snowflake_workspace`` 都可以导入它而不成环（``tests/test_service_architecture.py`` 守着）。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from novel_system.db.models import SnowflakeDirectionBrief, utcnow
from novel_system.services.hash_engine import canonical_json
from novel_system.services.snowflake_steps import STEP_ORDER, list_step_definitions

KINDS = ("decision", "rejection", "constraint", "pending")
SCOPES = ("step", "book")
ORIGINS = ("coach", "author")
STATUSES = ("active", "dismissed")
KIND_LABELS = {"decision": "决定", "rejection": "否决", "constraint": "约束", "pending": "待定"}
SCOPE_LABELS = {"step": "本步", "book": "全书"}
# 中文标签 / 常见近义词也能对上——作者在界面上、模型在输出里都不必背英文枚举。
_KIND_ALIASES = {
    "决定": "decision", "已决定": "decision", "decided": "decision",
    "否决": "rejection", "不要": "rejection", "reject": "rejection", "rejected": "rejection",
    "约束": "constraint", "constraints": "constraint", "限制": "constraint",
    "待定": "pending", "open": "pending", "exploring": "pending", "question": "pending", "探索": "pending",
}
_SCOPE_ALIASES = {
    "本步": "step", "this_step": "step", "local": "step",
    "全书": "book", "novel": "book", "global": "book", "project": "book", "whole_book": "book",
}

MAX_ACTIVE_LINES = 16          # 一步最多同时活动的条目（超出时先撤教练提出的「待定」）
MAX_LINE_CHARS = 160           # 单条上限：要点不是段落
MAX_INHERITED_LINES = 24       # 上游全书级条目带入下游的上限（上游在前，最基本的先保）
MAX_COACH_PROPOSALS = 12       # 教练一轮最多重述的条目数
MAX_WITHDRAWN_SHOWN = 8        # 给教练看的「作者撤下的条目」上限（免得它再提）
RECENT_TURNS = 6               # 教练看得到本步最近几轮
MAX_SOURCE_TURN_IDS = 50
_TURN_MESSAGE_CHARS = 600
_TURN_REPLY_CHARS = 800
_TURN_SUGGESTION_CHARS = 120
_TURN_SUGGESTIONS = 3

AUTHOR_DIRECTION_BRIEF_HOW_TO_USE = (
    "这是作者在驻场教练对话里定下、并亲手核过的本步意图要点。效力等同作者自己的草稿："
    "「决定」与「约束」必须落实，「否决」的方向不得出现，「待定」是作者仍在探索的问题——不要替作者拍板，"
    "两可处宁可留白或按上游材料处理。它压过 pressure_rubric 的通用建议和你自己的偏好，"
    "但不压过 upstream_steps 里 confirmed: true 的事实；与未确认的上游草稿冲突时按要点写，并让输出自身保持一致。"
    "inherited 是上游各步定下的全书级要点（基调、叙事立场、结局类型、题材承诺等），同样有效。"
)
CONVERSATION_HOW_TO_USE = (
    "conversation 是你与作者在本步的既有对话。brief.lines 是当前的意图要点（含 line_id）：origin=author 的条目是"
    "作者亲手写或改过的，你只能引用，不能改写或撤下；origin=coach 的是你此前提出的。withdrawn 是作者撤下的条目，"
    "不要再提。recent_turns 是本步最近几轮问答（按时间顺序），回答追问时以此为上下文。"
    "每轮都要在 brief_update.lines 里重述完整的、当前仍成立的 coach 条目：沿用的原样带 line_id 回传，"
    "改写的带同一 line_id 与新文字，本轮被推翻的不再列出（即撤下），新的直接加（不带 line_id）。"
    "只记作者说出或接受的东西：作者的探索性提问（如果……会怎样）记为 pending，绝不记为 decision；"
    "语气、立场、篇幅、必须出现的元素记为 constraint；作者明确不要的记为 rejection。"
    "scope=book 只给统摄全书的意图（基调、叙事立场、结局类型、题材承诺），其余一律 step。"
    "每条不超过 60 个汉字，不写写作建议（建议放在 suggestions），用作者的语言。"
)

_WS_RE = re.compile(r"\s+")
_KEY_STRIP_RE = re.compile(r"[\s,.;:!?，。；：！？、「」『』“”\"'()（）\[\]【】·—\-…]+")


def _mint_line_id() -> str:
    return f"dl_{uuid.uuid4().hex[:10]}"


def _clean_text(value: Any) -> str:
    text = _WS_RE.sub(" ", str(value or "")).strip()
    return text[:MAX_LINE_CHARS]


def text_key(value: Any) -> str:
    """去标点 / 空白 / 大小写后的比对键——模型丢了 line_id 但原样复述的条目仍能对上。"""
    return _KEY_STRIP_RE.sub("", str(value or "")).lower()


def coerce_kind(value: Any, *, default: str = "pending") -> str:
    raw = str(value or "").strip()
    key = raw.lower()
    if key in KINDS:
        return key
    return _KIND_ALIASES.get(raw, _KIND_ALIASES.get(key, default))


def coerce_scope(value: Any, *, default: str = "step") -> str:
    raw = str(value or "").strip()
    key = raw.lower()
    if key in SCOPES:
        return key
    return _SCOPE_ALIASES.get(raw, _SCOPE_ALIASES.get(key, default))


def normalize_line(
    raw: Any,
    *,
    origin: str,
    source_turn_id: str | None = None,
    now: str | None = None,
    line_id: str | None = None,
    status: str = "active",
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    text = _clean_text(raw.get("text"))
    if not text:
        return None
    stamp = now or utcnow()
    return {
        "line_id": line_id or str(raw.get("line_id") or "").strip() or _mint_line_id(),
        "kind": coerce_kind(raw.get("kind")),
        "scope": coerce_scope(raw.get("scope")),
        "text": text,
        "origin": origin if origin in ORIGINS else "coach",
        "status": status if status in STATUSES else "active",
        "dismissed_by": None,
        "source_turn_id": source_turn_id or None,
        "created_at": stamp,
        "updated_at": stamp,
    }


def coerce_brief_update(raw: Any) -> dict[str, Any] | None:
    """模型输出的 ``brief_update`` → ``{"lines": [...]}``。

    不是对象、或没有 ``lines`` 列表 → ``None``（本轮不动要点：旧提示词快照 / 模型漏字段时不会误撤）。
    ``{"lines": []}`` 是明确的「此前的 coach 条目都不再成立」。
    """
    if not isinstance(raw, dict):
        return None
    lines = raw.get("lines")
    if not isinstance(lines, list):
        return None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in lines:
        if not isinstance(item, dict):
            continue
        text = _clean_text(item.get("text"))
        if not text:
            continue
        key = text_key(text)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "line_id": str(item.get("line_id") or "").strip() or None,
                "kind": coerce_kind(item.get("kind")),
                "scope": coerce_scope(item.get("scope")),
                "text": text,
            }
        )
        if len(out) >= MAX_COACH_PROPOSALS:
            break
    return {"lines": out}


def _empty_delta() -> dict[str, Any]:
    return {"added": [], "updated": [], "superseded": [], "kept": 0}


def delta_changed(delta: dict[str, Any] | None) -> bool:
    if not delta:
        return False
    return bool(delta.get("added") or delta.get("updated") or delta.get("superseded"))


def _enforce_active_cap(lines: list[dict[str, Any]], stamp: str, delta: dict[str, Any]) -> None:
    """活动条目超上限时先撤教练提出的「待定」（最老的先），再撤其余教练条目；作者的条目永不自动撤。"""
    def _active_count() -> int:
        return sum(1 for line in lines if line.get("status") == "active")

    if _active_count() <= MAX_ACTIVE_LINES:
        return
    for kind_pass in ("pending", None):
        for line in lines:
            if _active_count() <= MAX_ACTIVE_LINES:
                return
            if line.get("status") != "active" or line.get("origin") != "coach":
                continue
            if kind_pass is not None and line.get("kind") != kind_pass:
                continue
            line["status"] = "dismissed"
            line["dismissed_by"] = "coach"
            line["updated_at"] = stamp
            delta["superseded"].append(line["line_id"])


def apply_coach_restatement(
    lines: list[dict[str, Any]] | None,
    update: dict[str, Any] | None,
    *,
    turn_id: str,
    now: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把教练本轮的完整重述并进既有条目，返回 (新条目列表, 差异)。

    - 带 line_id（或文本对得上）的既有 coach 条目：文字 / 类型 / 范围变了记 updated，否则 kept；
    - 不带 line_id 的新条目：记 added；
    - 既有的活动 coach 条目本轮没被重述：撤下（superseded）；
    - origin=author 的条目只可引用：不改写、不撤下；作者撤下的条目不复活。
    ``update is None`` 表示本轮没有重述，原样返回。
    """
    stamp = now or utcnow()
    current = [deepcopy(line) for line in (lines or []) if isinstance(line, dict)]
    delta = _empty_delta()
    if update is None:
        return current, delta
    by_id = {str(line.get("line_id") or ""): line for line in current}
    by_key = {text_key(line.get("text")): line for line in current}
    mentioned: set[str] = set()
    for proposal in update.get("lines") or []:
        line_id = str(proposal.get("line_id") or "")
        target = by_id.get(line_id) if line_id else None
        if target is None:
            target = by_key.get(text_key(proposal.get("text")))
        if target is not None:
            mentioned.add(str(target.get("line_id")))
            if target.get("origin") == "author":
                delta["kept"] += 1
                continue
            if target.get("status") != "active" and target.get("dismissed_by") == "author":
                # 作者撤下的条目，教练无权复活
                continue
            changed = False
            if target.get("status") != "active":
                target["status"] = "active"
                target["dismissed_by"] = None
                changed = True
            for field in ("kind", "scope", "text"):
                value = proposal.get(field)
                if not value or value == target.get(field):
                    continue
                if field == "text" and text_key(value) == text_key(target.get("text")):
                    continue  # 只差标点 / 空白的复述不算改写
                target[field] = value
                changed = True
            if changed:
                target["updated_at"] = stamp
                target["source_turn_id"] = turn_id
                delta["updated"].append(str(target.get("line_id")))
            else:
                delta["kept"] += 1
            continue
        line = normalize_line(
            {"kind": proposal.get("kind"), "scope": proposal.get("scope"), "text": proposal.get("text")},
            origin="coach",
            source_turn_id=turn_id,
            now=stamp,
            line_id=_mint_line_id(),
        )
        if line is None:
            continue
        current.append(line)
        by_id[line["line_id"]] = line
        by_key[text_key(line["text"])] = line
        mentioned.add(line["line_id"])
        delta["added"].append(line["line_id"])
    for line in current:
        if line.get("origin") == "coach" and line.get("status") == "active" and str(line.get("line_id")) not in mentioned:
            line["status"] = "dismissed"
            line["dismissed_by"] = "coach"
            line["updated_at"] = stamp
            delta["superseded"].append(str(line.get("line_id")))
    _enforce_active_cap(current, stamp, delta)
    return current, delta


def apply_author_edit(
    lines: list[dict[str, Any]] | None,
    requested: list[dict[str, Any]] | None,
    *,
    now: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """作者的整份编辑：请求里的列表就是作者要的列表。

    带 line_id 的对上既有条目（文字 / 类型 / 范围改了即归作者，教练此后不能再动）；不带的为新条目
    （origin=author）；既有的活动条目没出现在请求里 = 作者撤下；``status`` 显式给 dismissed / active 也认
    （恢复一条已撤的条目就是带着它的 line_id 和 status=active 回传）。
    """
    stamp = now or utcnow()
    current = [deepcopy(line) for line in (lines or []) if isinstance(line, dict)]
    by_id = {str(line.get("line_id") or ""): line for line in current}
    changed = False
    seen: set[str] = set()
    for raw in requested or []:
        if not isinstance(raw, dict):
            continue
        requested_status = str(raw.get("status") or "active").strip().lower()
        if requested_status not in STATUSES:
            requested_status = "active"
        line_id = str(raw.get("line_id") or "").strip()
        target = by_id.get(line_id) if line_id else None
        text = _clean_text(raw.get("text"))
        if target is None:
            if not text:
                continue
            line = normalize_line(raw, origin="author", now=stamp, line_id=_mint_line_id(), status=requested_status)
            if line is None:
                continue
            current.append(line)
            by_id[line["line_id"]] = line
            seen.add(line["line_id"])
            changed = True
            continue
        seen.add(str(target.get("line_id")))
        if not text:
            text = str(target.get("text") or "")
        kind = coerce_kind(raw.get("kind"), default=str(target.get("kind") or "pending"))
        scope = coerce_scope(raw.get("scope"), default=str(target.get("scope") or "step"))
        if text != target.get("text") or kind != target.get("kind") or scope != target.get("scope"):
            target.update({"text": text, "kind": kind, "scope": scope, "origin": "author", "updated_at": stamp})
            changed = True
        if requested_status != target.get("status"):
            target["status"] = requested_status
            target["dismissed_by"] = "author" if requested_status == "dismissed" else None
            target["updated_at"] = stamp
            changed = True
    for line in current:
        if str(line.get("line_id")) not in seen and line.get("status") == "active":
            line["status"] = "dismissed"
            line["dismissed_by"] = "author"
            line["updated_at"] = stamp
            changed = True
    return current, changed


def active_lines(lines: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [line for line in (lines or []) if isinstance(line, dict) and line.get("status") == "active" and line.get("text")]


_PUBLIC_LINE_KEYS = (
    "line_id", "kind", "scope", "text", "origin", "status", "dismissed_by", "source_turn_id", "created_at", "updated_at",
)


def line_public(line: dict[str, Any]) -> dict[str, Any]:
    return {key: line.get(key) for key in _PUBLIC_LINE_KEYS}


def brief_fingerprint(row: SnowflakeDirectionBrief | None, inherited: list[dict[str, Any]]) -> dict[str, Any]:
    """生成出的版本消费了哪一版要点：revision + 内容哈希 + 条目 id（不存正文，正文在要点表里）。"""
    own = active_lines(row.lines_json) if row is not None else []
    material = {
        "lines": [[line.get("kind"), line.get("scope"), line.get("text")] for line in own],
        "inherited": [[item.get("step_key"), item.get("line_id"), item.get("text")] for item in inherited],
    }
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()[:16]
    return {
        "used": True,
        "revision": int(row.revision) if row is not None else 0,
        "sha": digest,
        "line_ids": [str(line.get("line_id")) for line in own],
        "inherited_line_ids": [str(item.get("line_id")) for item in inherited],
        "inherit_upstream": bool(row.inherit_upstream) if row is not None else True,
    }


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class DirectionBriefStore:
    """要点表的读写面。生成 / 候选 / 分诊只用 ``prompt_payload_for`` 与 ``fingerprint_for``，
    教练用 ``conversation_payload`` 与 ``record_coach_restatement``，作者编辑走 ``save_author_edit``。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._labels = {
            str(definition["step_key"]): str(definition.get("label") or definition["step_key"])
            for definition in list_step_definitions()
        }

    # ---------- 读 ----------
    def rows(self, project_id: str) -> dict[str, SnowflakeDirectionBrief]:
        rows = self.session.execute(
            select(SnowflakeDirectionBrief).where(SnowflakeDirectionBrief.project_id == project_id)
        ).scalars().all()
        return {row.step_key: row for row in rows}

    def get(self, project_id: str, step_key: str) -> SnowflakeDirectionBrief | None:
        return self.session.execute(
            select(SnowflakeDirectionBrief).where(
                SnowflakeDirectionBrief.project_id == project_id,
                SnowflakeDirectionBrief.step_key == step_key,
            )
        ).scalars().first()

    def inherited_for(self, step_key: str, rows: dict[str, SnowflakeDirectionBrief]) -> list[dict[str, Any]]:
        """上游各步的全书级活动条目，按雪花顺序，最上游的在前；超上限时保留最上游的（最基本的意图）。"""
        limit = STEP_ORDER.get(str(step_key or ""), len(STEP_ORDER))
        out: list[dict[str, Any]] = []
        for key in sorted(rows, key=lambda item: STEP_ORDER.get(item, len(STEP_ORDER))):
            if STEP_ORDER.get(key, len(STEP_ORDER)) >= limit:
                continue
            for line in active_lines(rows[key].lines_json):
                if line.get("scope") != "book":
                    continue
                out.append(
                    {
                        "step_key": key,
                        "step_label": self._labels.get(key, key),
                        "line_id": str(line.get("line_id")),
                        "kind": str(line.get("kind") or "pending"),
                        "text": str(line.get("text") or ""),
                    }
                )
        return out[:MAX_INHERITED_LINES]

    def payload_for(
        self,
        project_id: str,
        step_key: str,
        rows: dict[str, SnowflakeDirectionBrief] | None = None,
    ) -> dict[str, Any] | None:
        rows = rows if rows is not None else self.rows(project_id)
        row = rows.get(step_key)
        inherited = self.inherited_for(step_key, rows)
        if row is None and not inherited:
            return None
        lines = list(row.lines_json or []) if row is not None else []
        return {
            "step_key": step_key,
            "revision": int(row.revision) if row is not None else 0,
            "inherit_upstream": bool(row.inherit_upstream) if row is not None else True,
            "lines": [line_public(line) for line in lines if isinstance(line, dict)],
            "active_count": len(active_lines(lines)),
            "inherited": inherited,
            "source_turn_ids": list(row.source_turn_ids_json or []) if row is not None else [],
            "author_edited_at": row.author_edited_at if row is not None else None,
            "updated_at": row.updated_at if row is not None else None,
        }

    def all_payloads(self, project_id: str) -> dict[str, dict[str, Any]]:
        rows = self.rows(project_id)
        out: dict[str, dict[str, Any]] = {}
        for definition in list_step_definitions():
            key = str(definition["step_key"])
            payload = self.payload_for(project_id, key, rows)
            if payload is not None:
                out[key] = payload
        return out

    def prompt_payload_for(
        self,
        project_id: str,
        step_key: str,
        rows: dict[str, SnowflakeDirectionBrief] | None = None,
    ) -> dict[str, Any] | None:
        """给生成 / 候选 / 分诊的形状：中文标签、只带活动条目；没有任何条目时返回 None（键不出现）。"""
        rows = rows if rows is not None else self.rows(project_id)
        row = rows.get(step_key)
        own = active_lines(row.lines_json) if row is not None else []
        inherit = bool(row.inherit_upstream) if row is not None else True
        inherited = self.inherited_for(step_key, rows) if inherit else []
        if not own and not inherited:
            return None
        return {
            "lines": [
                {
                    "kind": KIND_LABELS.get(str(line.get("kind")), "待定"),
                    "scope": SCOPE_LABELS.get(str(line.get("scope")), "本步"),
                    "text": str(line.get("text") or ""),
                }
                for line in own
            ],
            "inherited": [
                {"step": item["step_label"], "kind": KIND_LABELS.get(item["kind"], "待定"), "text": item["text"]}
                for item in inherited
            ],
            "how_to_use": AUTHOR_DIRECTION_BRIEF_HOW_TO_USE,
        }

    def fingerprint_for(
        self,
        project_id: str,
        step_key: str,
        *,
        used: bool,
        rows: dict[str, SnowflakeDirectionBrief] | None = None,
    ) -> dict[str, Any] | None:
        """落到 health_json.direction_brief 的出处：没有任何条目 → None（键不出现）；作者关掉带入 → used=False。"""
        rows = rows if rows is not None else self.rows(project_id)
        row = rows.get(step_key)
        inherit = bool(row.inherit_upstream) if row is not None else True
        inherited = self.inherited_for(step_key, rows) if inherit else []
        own = active_lines(row.lines_json) if row is not None else []
        if not own and not inherited:
            return None
        if not used:
            return {"used": False, "revision": int(row.revision) if row is not None else 0}
        return brief_fingerprint(row, inherited)

    def conversation_payload(
        self,
        project_id: str,
        step_key: str,
        *,
        turns: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """教练每轮看到的对话上下文：当前要点（含 line_id 与 origin）、作者撤下的条目、继承的全书级要点、
        本步最近几轮 LLM 问答（历史上的规则回退轮次不算对话）。"""
        rows = self.rows(project_id)
        row = rows.get(step_key)
        lines = list(row.lines_json or []) if row is not None else []
        inherit = bool(row.inherit_upstream) if row is not None else True
        withdrawn = [
            str(line.get("text") or "")
            for line in lines
            if isinstance(line, dict) and line.get("status") == "dismissed" and line.get("dismissed_by") == "author"
        ][-MAX_WITHDRAWN_SHOWN:]
        step_turns = [
            turn
            for turn in turns
            if isinstance(turn, dict) and turn.get("step_key") == step_key and turn.get("source") == "llm"
        ][-RECENT_TURNS:]
        return {
            "brief": {
                "lines": [
                    {
                        "line_id": str(line.get("line_id")),
                        "kind": str(line.get("kind") or "pending"),
                        "scope": str(line.get("scope") or "step"),
                        "origin": str(line.get("origin") or "coach"),
                        "text": str(line.get("text") or ""),
                    }
                    for line in active_lines(lines)
                ],
                "withdrawn": withdrawn,
            },
            "inherited_brief": [
                {"step": item["step_label"], "kind": item["kind"], "text": item["text"]}
                for item in (self.inherited_for(step_key, rows) if inherit else [])
            ],
            "recent_turns": [
                {
                    "message": _truncate(turn.get("message"), _TURN_MESSAGE_CHARS),
                    "reply": _truncate(turn.get("reply"), _TURN_REPLY_CHARS),
                    "suggestions": [
                        _truncate(item, _TURN_SUGGESTION_CHARS)
                        for item in list(turn.get("suggestions") or [])[:_TURN_SUGGESTIONS]
                    ],
                    "candidate_label": str(turn.get("candidate_label") or ""),
                }
                for turn in step_turns
            ],
            "how_to_use": CONVERSATION_HOW_TO_USE,
        }

    # ---------- 写 ----------
    def _get_or_create(self, project_id: str, step_key: str) -> SnowflakeDirectionBrief:
        row = self.get(project_id, step_key)
        if row is not None:
            return row
        row = SnowflakeDirectionBrief(
            brief_id=f"snowflake_direction_brief_{uuid.uuid4().hex[:12]}",
            project_id=project_id,
            step_key=step_key,
            lines_json=[],
            inherit_upstream=1,
            revision=0,
            source_turn_ids_json=[],
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_coach_restatement(
        self,
        project_id: str,
        step_key: str,
        update: dict[str, Any] | None,
        *,
        turn_id: str,
    ) -> tuple[SnowflakeDirectionBrief | None, dict[str, Any]]:
        if update is None:
            return self.get(project_id, step_key), _empty_delta()
        existing = self.get(project_id, step_key)
        if existing is None and not (update.get("lines") or []):
            # 什么都没提、也没有既有条目：不为一张空表建行
            return None, _empty_delta()
        row = existing or self._get_or_create(project_id, step_key)
        lines, delta = apply_coach_restatement(row.lines_json, update, turn_id=turn_id)
        if not delta_changed(delta):
            return row, delta
        row.lines_json = lines
        flag_modified(row, "lines_json")
        row.revision = int(row.revision or 0) + 1
        row.source_turn_ids_json = (list(row.source_turn_ids_json or []) + [turn_id])[-MAX_SOURCE_TURN_IDS:]
        flag_modified(row, "source_turn_ids_json")
        row.updated_at = utcnow()
        self.session.flush()
        return row, delta

    def save_author_edit(
        self,
        project_id: str,
        step_key: str,
        *,
        lines: list[dict[str, Any]] | None = None,
        inherit_upstream: bool | None = None,
    ) -> SnowflakeDirectionBrief:
        row = self._get_or_create(project_id, step_key)
        changed = False
        if lines is not None:
            new_lines, lines_changed = apply_author_edit(row.lines_json, lines)
            if lines_changed:
                row.lines_json = new_lines
                flag_modified(row, "lines_json")
                changed = True
        if inherit_upstream is not None and bool(row.inherit_upstream) != bool(inherit_upstream):
            row.inherit_upstream = 1 if inherit_upstream else 0
            changed = True
        if changed:
            row.revision = int(row.revision or 0) + 1
            row.author_edited_at = utcnow()
            row.updated_at = row.author_edited_at
            self.session.flush()
        return row
