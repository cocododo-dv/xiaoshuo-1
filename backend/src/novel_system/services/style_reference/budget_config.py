"""风格参考 · ``config/style_reference/injection_budget.yaml`` 的唯一解析处（叶子：只依赖 ``config_loader`` 与
``binding_config``）。

每个键的缺省与合法范围只在这里写一次；文件不在、读不出来（记一条警告）、键缺失或值不合法 → 那个键的缺省。
读它的地方：样例窗与文风卡的预算（``inject/render``）、起草方式的缺省（``runtime_contract.resolve_draft_mode``）、
风格步与软补丁的阈值段（``style_step.fidelity_thresholds`` 校验 ``fidelity``）、风格直起的参考场长上限
（``bundle_builder``）与跨场景连续性锚（``bundle_continuity``）；风格直起的长度放宽（``style_first_length_slack``）
也在这里有一份，场景管线那一处改读它之前仍自己解析同一个键。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from novel_system.services.style_reference.binding_config import DRAFT_MODE_STYLE_FIRST, DRAFT_MODES
from novel_system.services.style_reference.config_loader import load_optional_yaml_config

logger = logging.getLogger(__name__)

# 单窗正文上限（短尾窗并入时可放宽 25%；超长的单段窗在句边界截断）
SAMPLE_WINDOW_MAX_CHARS = 5000
# 文风卡的总预算（整行截断，永不截半句）
CARD_BUDGET_CHARS = 2600
# 风格直起下场景卡数字长度带两侧各放宽的比例，合法范围 [0, 0.9]
STYLE_FIRST_LENGTH_SLACK = 0.5
STYLE_FIRST_LENGTH_SLACK_MAX = 0.9
# 「这位作者的一场多长」推算值的上限（字）
REFERENCE_SCENE_CHARS_MAX = 5000
# 上一场成稿尾部作 [前文声音锚] 的字数上限
CONTINUITY_ANCHOR_MAX_CHARS = 900


@dataclass(frozen=True)
class InjectionBudget:
    sample_window_max_chars: int = SAMPLE_WINDOW_MAX_CHARS
    card_budget_chars: int = CARD_BUDGET_CHARS
    draft_mode_default: str = DRAFT_MODE_STYLE_FIRST
    style_first_length_slack: float = STYLE_FIRST_LENGTH_SLACK
    style_first_reference_scene_chars_max: int = REFERENCE_SCENE_CHARS_MAX
    continuity_anchor_max_chars: int = CONTINUITY_ANCHOR_MAX_CHARS
    # ``fidelity:`` 段原样（键的范围由 ``style_step.fidelity_thresholds`` 按 ``FidelityThresholds`` 校验）
    fidelity: Mapping[str, Any] = field(default_factory=dict)


def _non_negative_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    """整数，负数按 0；读不成整数 → 缺省。"""
    try:
        return max(0, int(raw.get(key, default)))
    except (TypeError, ValueError):
        return default


def _positive_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    """正整数；0、负数、空值或读不成整数 → 缺省。"""
    try:
        value = int(raw.get(key, default) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _slack(raw: Mapping[str, Any]) -> float:
    """放宽比例夹进 [0, 0.9]（与场景管线原来的读法逐位相同：NaN 夹成 0）。"""
    try:
        value = float(raw.get("style_first_length_slack", STYLE_FIRST_LENGTH_SLACK))
    except (TypeError, ValueError):
        return STYLE_FIRST_LENGTH_SLACK
    return max(0.0, min(value, STYLE_FIRST_LENGTH_SLACK_MAX))


def _draft_mode(raw: Mapping[str, Any]) -> str:
    value = str(raw.get("draft_mode_default") or "").strip().lower()
    return value if value in DRAFT_MODES else DRAFT_MODE_STYLE_FIRST


def injection_budget() -> InjectionBudget:
    """当前的注入预算（yaml 由 ``config_loader`` 缓存，这里每次只做键的校验）。"""
    try:
        raw = load_optional_yaml_config("injection_budget")
    except Exception:  # noqa: BLE001 — 坏配置不应让注入 / bundle 构建失败：全部按缺省
        logger.warning("injection_budget.yaml unreadable; using defaults", exc_info=True)
        return InjectionBudget()
    fidelity = raw.get("fidelity")
    return InjectionBudget(
        sample_window_max_chars=_non_negative_int(raw, "sample_window_max_chars", SAMPLE_WINDOW_MAX_CHARS),
        card_budget_chars=_non_negative_int(raw, "card_budget_chars", CARD_BUDGET_CHARS),
        draft_mode_default=_draft_mode(raw),
        style_first_length_slack=_slack(raw),
        style_first_reference_scene_chars_max=_positive_int(
            raw, "style_first_reference_scene_chars_max", REFERENCE_SCENE_CHARS_MAX
        ),
        continuity_anchor_max_chars=_positive_int(raw, "continuity_anchor_max_chars", CONTINUITY_ANCHOR_MAX_CHARS),
        fidelity=dict(fidelity) if isinstance(fidelity, Mapping) else {},
    )


__all__ = [
    "CARD_BUDGET_CHARS",
    "CONTINUITY_ANCHOR_MAX_CHARS",
    "InjectionBudget",
    "REFERENCE_SCENE_CHARS_MAX",
    "SAMPLE_WINDOW_MAX_CHARS",
    "STYLE_FIRST_LENGTH_SLACK",
    "STYLE_FIRST_LENGTH_SLACK_MAX",
    "injection_budget",
]
