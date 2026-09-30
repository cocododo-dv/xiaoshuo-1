"""一次场景运行在各阶段之间传的值：管线的 ``SceneRunContext`` 与交给归档尾段的 ``ArchiveInputs``。

纯数据，mixin 模块都可以导入（mixin 之间不互相导入）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from novel_system.db.models import FinalScene, SceneCard, SceneRunState
from novel_system.services.qc_engine import HardQcDecision
from novel_system.services.scene_generation import NeutralGenerationResult, StyleGenerationResult


@dataclass
class SceneRunContext:
    """一次 ``_run_scene_pipeline`` 在各 ``_phase_*`` 之间传的东西（阶段按顺序往里填）。"""

    scene: SceneCard
    state: SceneRunState
    contract: Any
    author_note: str | None
    run_policy: str
    planning: dict[str, Any] | None = None
    bundle: dict[str, Any] | None = None
    criticality: Any = None
    neutral_generation: NeutralGenerationResult | None = None
    hard_qc: HardQcDecision | None = None
    n_candidates: int = 1
    candidates: list[StyleGenerationResult] = field(default_factory=list)
    style_generation: StyleGenerationResult | None = None
    candidate_summaries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scene_id(self) -> str:
        return self.scene.scene_id


@dataclass(frozen=True)
class ArchiveInputs:
    """全新的一次运行交给归档尾段的、同一进程刚做完的输入（续跑时从检查点读回并复验）。"""

    soft_qc: Any
    final_scene: FinalScene
    near_final_payload: dict[str, Any]
