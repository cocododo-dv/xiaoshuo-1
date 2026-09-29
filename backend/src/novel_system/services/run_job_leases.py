"""场景 / 章节运行任务的共用词表与租约内核（叶子模块：只依赖 ``db.models`` 与 SQLAlchemy）。

两种任务都是 ``chapter_run_jobs`` 表里的一行（``job_type`` 区分），用同一套租约字段：
``worker_id`` / ``attempt_no`` / ``lease_expires_at``。这里放它们共用的词：任务类型、任务状态、
场景任务对作者展示的步位词表（``current_step``）。
"""

from __future__ import annotations

# ---------------------------------------------------------------------- 任务类型
JOB_TYPE_SCENE_FULL = "scene_run_full"
JOB_TYPE_CHAPTER_FULL = "chapter_run_full"

# ---------------------------------------------------------------------- 场景任务的步位词表（B03-03）
# ``current_step`` 对作者只说一套词：管线阶段（进行中）+ 几个停点。前端 ``ws-scene-derive.js`` 的
# RUN_JOB_STEP_LABELS 按这套词给中文标签；检查点节点名（budget_ready …）不出现在任务视图里。
SCENE_RUN_STAGE_ORDER: tuple[str, ...] = (
    "planning_running",
    "bundle_built",
    "neutral_running",
    "hard_qc_running",
    "style_running",
    "soft_qc_running",
    "rewrite_running",
    "acceptance_review_running",
    "near_final",
    "archived",
)
SCENE_STEP_CLAIMED = "planning_running"

# 检查点节点（scene_run_checkpoint.RUN_CHECKPOINT_ORDER）→ 存下这个节点之后正在进行的阶段。
# 风格稿的逐稿进度存在 hard_qc_ready 的 sub_index 上；归档尾巴的 8 个子步都存在 near_final_ready 上。
_CHECKPOINT_STAGES: dict[str, str] = {
    "budget_ready": "planning_running",
    "planning_ready": "bundle_built",
    "bundle_ready": "neutral_running",
    "neutral_ready": "hard_qc_running",
    "hard_qc_ready": "style_running",
    "style_ready": "soft_qc_running",
    "selection_wait": "awaiting_candidate_selection",
    "soft_qc_ready": "acceptance_review_running",
    "near_final_ready": "near_final",
    "archived": "archived",
}


def scene_job_step(token: str) -> str:
    """任务里记下的步位 → 作者词表：检查点节点换成进行中的阶段，其余（阶段词、停点、状态词）原样。"""
    return _CHECKPOINT_STAGES.get(token, token)
