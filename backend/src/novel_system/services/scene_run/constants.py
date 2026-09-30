"""场景运行的跳过原因与阶段名：写进检查点（``near_completion`` / ``soft_completion``）与 near_final payload 的字面值。

改名就是改检查点——库里停在半路的运行续跑时按这些值核对。
"""

from __future__ import annotations

# 2026-09 风格模仿 v2（W5）：near_final_rewrite 带同一 [STYLE_REFERENCE] 前缀（含参考原文
# 样例窗口）重写整场，输出直接成为终稿。它的 styled-draft gate 判定抄袭（Q0）时重写稿永远
# 不能落成 FinalScene：回退到重写前、已过 soft_qc gate 的来源稿，skip_reason 记在
# 检查点 / near_completion 里，警告随 near_final payload 走。
NEAR_FINAL_REWRITE_REJECTED_SKIP_REASON = "rewrite_rejected_style_plagiarism"
NEAR_FINAL_REWRITE_GATE_STAGE = "near_final_rewrite"
# 风格参考 v3 复核（A/B 实测：定稿改写把整场挤成一段、读数从 92.7 退到 98.3 百分位）：定稿改写稿进 eval1 之前
# 再过两道确定性门——相对来源稿的基础安全回退（必写事实、禁用内容、文本完整性含整场挤成一段、长度），以及作者手笔
# 直起时「离作者更远超过容差」。没过 = 与抄袭被拒同一条路径：终稿回到来源稿，eval0 的意见随稿留痕。
NEAR_FINAL_REWRITE_BASE_SAFETY_SKIP_REASON = "rewrite_rejected_base_safety"
NEAR_FINAL_REWRITE_MOVED_AWAY_SKIP_REASON = "rewrite_rejected_moved_away"
NEAR_FINAL_REJECTION_SKIP_REASONS = {
    "style_plagiarism": NEAR_FINAL_REWRITE_REJECTED_SKIP_REASON,
    "base_safety": NEAR_FINAL_REWRITE_BASE_SAFETY_SKIP_REASON,
    "moved_away": NEAR_FINAL_REWRITE_MOVED_AWAY_SKIP_REASON,
}
# 风格参考 v3（P5b）：作者手笔直起时软补丁让稿子离作者更远 → 退回补丁前的稿子。软 QC 收尾记这个 skip_reason，
# 收尾的决定由补丁前那一轮评审派生（branch=waive、stop_reason 如下），评审的改稿意见随稿留痕。
STYLE_PATCH_REVERTED_SKIP_REASON = "style_patch_reverted"
STYLE_PATCH_REVERTED_STOP_REASON = "style_patch_reverted"
