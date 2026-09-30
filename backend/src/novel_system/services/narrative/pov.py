"""Wave 4 — POV 减法投影（设计 §5.6 / §7.11 / 不变量 11）。

问题：全知的状态摘要与信息差摘要向**写作提示词**注入全量权威状态，包括非 POV 角色的
`secret_held_by` / `believes_false` 正文——模型从输入层就看见了 POV 不该知道的秘密（G-05）。

这里对**写作提示词**做减法投影：只保留 POV 应知的信息，抑制他人秘密内容。
硬 QC 仍读全量权威状态（`check_consistency` 不受影响）——"机器守下限用全量，写作上限受 POV 约束"。

6 个知识级别（§5.6）由现有事件结构派生，无需 schema 迁移：

- ``public``       —— 非信息不对称键的事实（在场角色可观察），照旧注入
- ``secret_owner`` —— ``secret_held_by`` 事实：持有者知道、他人不知
- ``known``        —— POV 自身事实 ∪ POV 的 ``character_learns`` ∪ POV 持有/已被揭示的秘密
                       ∪ POV 在场场景断言的公共事实（回填启发式）
- ``believed_false``—— POV 的 ``believes_false``（POV 据此行动，注入）
- ``suspected``    —— POV 的 ``character_learns`` 且 ``payload_json.knowledge_status=='suspected'``
- ``unknown``      —— 其余，不注入

退化性质：只对信息不对称键做 POV 过滤，公共事实照旧。项目若无任何秘密/错误信念，
投影输出与全量注入等价——"无显式秘密标注 → 等价全量"是逐事实过滤的自然结果（§5.6）。

投影是 ``ProjectionSnapshot`` 上的纯过滤（B11-08）：模块函数只读快照，不查库、不回调事件日志；
``PovKnowledgeProjection`` 是给编排器与测试用的入口，每次调用建一份快照。``pov_character_id=None``
表示全知视角（无单一受限 POV 可保护），直接走 ``digests`` 的全量摘要，逐字节不变。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from novel_system.db.models import NarrativeEvent, SceneCard
from novel_system.services.character_names import display_name_of, labelled_name_of
from novel_system.services.narrative import digests
from novel_system.services.narrative.replay import (
    NarrativeEventStore,
    ProjectionSnapshot,
    require_scene_boundary,
    snapshot_before,
)
from novel_system.services.narrative.taxonomy import (
    INFORMATION_ASYMMETRY_FACT_KEYS,
    SECRET_CONTENT_KEYS,
)
from novel_system.services.narrative_position import NarrativePositionService


# ---------------------------------------------------------------------------
# 知识归属（纯函数，只读快照）
# ---------------------------------------------------------------------------


def is_suspected(event: NarrativeEvent) -> bool:
    payload = event.payload_json or {}
    return str(payload.get("knowledge_status") or "").strip().lower() == "suspected"


def pov_learned_values(snapshot: ProjectionSnapshot, pov_character_id: str) -> set[str]:
    return {event.fact_value for event in snapshot.learns_events(pov_character_id)}


def secret_known_to_pov(
    snapshot: ProjectionSnapshot,
    secret_value: str,
    owner_id: str,
    pov_character_id: str,
) -> bool:
    if owner_id == pov_character_id:
        return True
    # 秘密已显式向 POV 揭示？
    if pov_character_id in snapshot.fact_values(owner_id, "revealed_to"):
        return True
    # POV 是否已获知该秘密内容（character_learns）？
    return secret_value in pov_learned_values(snapshot, pov_character_id)


def onstage_public_values(
    snapshot: ProjectionSnapshot,
    pov_character_id: str,
    scenes_before: list[SceneCard],
) -> set[str]:
    """回填启发式：POV 在场场景断言的公共事实 → POV 已知（防饿死上下文，§5.6）。

    只取公共事实（非秘密键）；秘密不因"在场"默认已知（保守策略）。
    """
    onstage_scene_ids = {
        scene.scene_id
        for scene in scenes_before
        if isinstance(scene.onstage_chars_json, list) and pov_character_id in scene.onstage_chars_json
    }
    if not onstage_scene_ids:
        return set()
    return {
        event.fact_value
        for event in snapshot.events
        if event.scene_id in onstage_scene_ids and event.fact_key not in INFORMATION_ASYMMETRY_FACT_KEYS
    }


def pov_known_fact_values(
    snapshot: ProjectionSnapshot,
    pov_character_id: str,
    scenes_before: list[SceneCard],
) -> set[str]:
    """POV 已知的全部事实值集合——供脱敏与信息盲区判定。"""
    values = {projected.fact_value for projected in snapshot.character_state(pov_character_id).facts.values()}
    values |= pov_learned_values(snapshot, pov_character_id)
    values |= onstage_public_values(snapshot, pov_character_id, scenes_before)
    return values


def suppressed_secret_values(
    snapshot: ProjectionSnapshot,
    pov_character_id: str,
    onstage_character_ids: list[str] | None = None,
) -> set[str]:
    """非 POV 角色持有、且 POV 不知的秘密/错误信念内容集合。"""
    chars = onstage_character_ids or snapshot.characters()
    values: set[str] = set()
    for char_id in chars:
        if char_id == pov_character_id:
            continue
        state = snapshot.character_state(char_id)
        for key in SECRET_CONTENT_KEYS:
            projected = state.facts.get(key)
            if projected and not secret_known_to_pov(snapshot, projected.fact_value, char_id, pov_character_id):
                values.add(projected.fact_value)
    return values


# ---------------------------------------------------------------------------
# 写作提示词投影
# ---------------------------------------------------------------------------


def format_pov_state(
    snapshot: ProjectionSnapshot,
    pov_character_id: str,
    onstage_character_ids: list[str] | None = None,
) -> str:
    """POV 过滤的权威状态摘要（写作提示词用）。"""
    chars = onstage_character_ids or snapshot.characters()
    names = snapshot.names()
    pov_name = display_name_of(names, pov_character_id)
    lines: list[str] = [digests.CHARACTER_STATE_HEADER]
    suppressed_owners: list[str] = []

    for char_id in chars:
        state = snapshot.character_state(char_id)
        if not state.facts:
            continue
        visible: list[tuple[str, str]] = []
        for key, value in sorted(state.as_dict().items()):
            if key in INFORMATION_ASYMMETRY_FACT_KEYS:
                if char_id == pov_character_id or secret_known_to_pov(snapshot, value, char_id, pov_character_id):
                    visible.append((key, value))
                elif key in SECRET_CONTENT_KEYS and char_id not in suppressed_owners:
                    suppressed_owners.append(char_id)
                # 非 POV 秘密内容：抑制（不注入）
            else:
                visible.append((key, value))  # 公共事实
        if not visible:
            continue
        lines.append(f"\n### {labelled_name_of(names, char_id)}")
        for key, value in visible:
            lines.append(f"- {key}: {value}")

    # POV 已知 / 怀疑（character_learns 分流）
    known_regular: list[tuple[str, str]] = []
    suspected: list[tuple[str, str]] = []
    for event in snapshot.learns_events(pov_character_id):
        (suspected if is_suspected(event) else known_regular).append((event.fact_key, event.fact_value))
    if known_regular:
        lines.append(f"\n### POV知识边界 ({pov_name} 已知信息)")
        for key, value in known_regular:
            lines.append(f"- {key}: {value}")
    if suspected:
        lines.append(f"\n### POV怀疑 ({pov_name} 尚未确证，勿写成既定事实)")
        for key, value in suspected:
            lines.append(f"- {key}: {value}（尚未确证/suspected）")

    # 信息差写作约束——内容无关（§5.6.4）
    if suppressed_owners:
        lines.append("\n## 写作约束（信息差·勿泄漏内容）")
        for owner in suppressed_owners:
            lines.append(
                f"- 角色 {display_name_of(names, owner)} 掌握 {pov_name} 未知的信息；"
                f"勿在 {pov_name} 视角泄漏其内容。"
            )

    # 地点 / 物品状态（公共，与全知摘要逐字节相同）
    lines.extend(digests.entity_state_lines(snapshot))
    return "\n".join(lines) if len(lines) > 1 else ""


def format_pov_asymmetry(
    snapshot: ProjectionSnapshot,
    pov_character_id: str,
    onstage_character_ids: list[str],
) -> str:
    """POV 视角的信息不对称摘要——只显示 POV 独有认知，他人独有内容仅给盲区提示。"""
    if len(onstage_character_ids) < 2:
        return ""
    names = snapshot.names()
    pov_name = display_name_of(names, pov_character_id)
    lines: list[str] = ["## Information Asymmetry (POV-filtered, do NOT leak hidden content)"]

    pov_knows = {f"{fact.fact_key}:{fact.fact_value}" for fact in snapshot.known_facts(pov_character_id)}
    exclusive_lines: list[str] = []
    blind_owners: list[str] = []
    for other in onstage_character_ids:
        if other == pov_character_id:
            continue
        other_knows = {f"{fact.fact_key}:{fact.fact_value}" for fact in snapshot.known_facts(other)}
        for fact in sorted(pov_knows - other_knows):
            exclusive_lines.append(f"  - {fact}")
        other_state = snapshot.character_state(other)
        has_secret = any(other_state.facts.get(key) for key in SECRET_CONTENT_KEYS)
        if (other_knows - pov_knows) or has_secret:
            if other not in blind_owners:
                blind_owners.append(other)

    if exclusive_lines:
        lines.append(f"\n### {pov_name} 独有认知（可据此行动）")
        # 去重保序
        seen: set[str] = set()
        for line in exclusive_lines:
            if line not in seen:
                seen.add(line)
                lines.append(line)
    if blind_owners:
        lines.append("\n### 信息盲区（勿在 POV 视角泄漏内容）")
        for owner in blind_owners:
            lines.append(f"  - 角色 {display_name_of(names, owner)} 掌握 {pov_name} 未知的信息")

    pov_state = snapshot.character_state(pov_character_id)
    own_secrets = [pov_state.facts[key].fact_value for key in SECRET_CONTENT_KEYS if pov_state.facts.get(key)]
    if own_secrets:
        lines.append(f"\n### {pov_name} 自身秘密/信念")
        for secret in own_secrets:
            lines.append(f"  - {secret}")

    return "\n".join(lines) if len(lines) > 1 else ""


# ---------------------------------------------------------------------------
# finding 证据脱敏（§7.11 / 不变量 11）
# ---------------------------------------------------------------------------


def finding_blob(finding: Any) -> str:
    if isinstance(finding, str):
        return finding
    if isinstance(finding, dict):
        parts: list[str] = []
        for key in (
            "authority_ref", "expected", "actual", "evidence", "message",
            "instruction", "recommended_action",
        ):
            val = finding.get(key)
            if val:
                parts.append(str(val))
        details = finding.get("details")
        if isinstance(details, dict):
            parts.append(" ".join(str(v) for v in details.values()))
        spans = finding.get("evidence_spans")
        if isinstance(spans, list):
            parts.append(" ".join(str(s) for s in spans))
        return " ".join(parts)
    return str(finding)


def split_findings(findings: list[Any], suppressed: set[str]) -> tuple[list[Any], list[Any]]:
    safe: list[Any] = []
    redacted: list[Any] = []
    for finding in findings:
        blob = finding_blob(finding)
        if any(value and value in blob for value in suppressed):
            if isinstance(finding, dict):
                redacted.append({
                    **finding,
                    "author_confirmation_only": True,
                    "desensitized_reason": "references_non_pov_secret",
                })
            else:
                redacted.append(finding)
        else:
            safe.append(finding)
    return safe, redacted


class PovKnowledgeProjection:
    """POV 视角的写作提示词投影器（编排器的补丁简报脱敏与测试用的入口，每次调用建一份快照）。"""

    def __init__(self, session: Session, *, event_log: NarrativeEventStore | None = None) -> None:
        self.session = session
        self.log = event_log if event_log is not None else NarrativeEventStore(session)
        self.positions = NarrativePositionService(session)

    def _snapshot(self, project_id: str, scene_id: str | None) -> ProjectionSnapshot:
        return snapshot_before(self.log, project_id, require_scene_boundary(scene_id))

    def pov_known_fact_values(
        self,
        project_id: str,
        pov_character_id: str,
        *,
        scene_id: str,
    ) -> set[str]:
        """POV 已知的全部事实值集合——供脱敏与信息盲区判定。"""
        snapshot = self._snapshot(project_id, scene_id)
        return pov_known_fact_values(
            snapshot,
            pov_character_id,
            self.positions.scenes_before(project_id, snapshot.before_scene_id),
        )

    def suppressed_secret_values(
        self, project_id: str, pov_character_id: str,
        onstage_character_ids: list[str] | None = None,
        *,
        scene_id: str,
    ) -> set[str]:
        """非 POV 角色持有、且 POV 不知的秘密/错误信念内容集合。"""
        snapshot = self._snapshot(project_id, scene_id)
        return suppressed_secret_values(snapshot, pov_character_id, onstage_character_ids)

    def format_state_for_prompt(
        self,
        project_id: str,
        *,
        scene_id: str,
        pov_character_id: str | None = None,
        onstage_character_ids: list[str] | None = None,
    ) -> str:
        """POV 过滤的权威状态摘要（写作提示词用）；pov=None → 全知摘要（逐字节不变）。"""
        snapshot = self._snapshot(project_id, scene_id)
        if not pov_character_id:
            return digests.format_state(snapshot, onstage_character_ids=onstage_character_ids)
        return format_pov_state(snapshot, pov_character_id, onstage_character_ids)

    def information_asymmetry_digest(
        self,
        project_id: str,
        *,
        scene_id: str,
        onstage_character_ids: list[str] | None = None,
        pov_character_id: str | None = None,
    ) -> str:
        """POV 视角的信息不对称摘要；pov=None → 全知摘要（逐字节不变）。"""
        scene_id = require_scene_boundary(scene_id)
        onstage = list(onstage_character_ids or [])
        if len(onstage) < 2:
            return ""
        snapshot = snapshot_before(self.log, project_id, scene_id)
        if not pov_character_id:
            return digests.format_asymmetry(snapshot, onstage)
        return format_pov_asymmetry(snapshot, pov_character_id, onstage)

    def desensitize_findings(
        self,
        findings: list[Any],
        project_id: str,
        *,
        scene_id: str | None = None,
        pov_character_id: str | None = None,
        onstage_character_ids: list[str] | None = None,
    ) -> tuple[list[Any], list[Any]]:
        """把回灌自动补丁的 finding 拆成 (safe, redacted)。

        引用了非 POV 已知秘密的 finding 不得进入自动补丁提示词，改标
        ``author_confirmation_only`` 走作者确认修订（不变量 11）。pov=None 或无秘密
        时全部放行。硬 QC 自身不经此路径（始终读全量）。
        """
        if not findings or not pov_character_id:
            return list(findings), []
        suppressed = self.suppressed_secret_values(
            project_id,
            pov_character_id,
            onstage_character_ids,
            scene_id=scene_id,
        )
        if not suppressed:
            return list(findings), []
        return split_findings(findings, suppressed)

    def redact_brief(
        self,
        brief_lines: list[str],
        project_id: str,
        *,
        scene_id: str | None = None,
        pov_character_id: str | None = None,
        onstage_character_ids: list[str] | None = None,
    ) -> list[str]:
        """从自动补丁 brief（``list[str]`` 指令）中剔除引用非 POV 秘密的条目。"""
        safe, _redacted = self.desensitize_findings(
            list(brief_lines), project_id,
            scene_id=scene_id,
            pov_character_id=pov_character_id,
            onstage_character_ids=onstage_character_ids,
        )
        return [line for line in safe if isinstance(line, str)]
