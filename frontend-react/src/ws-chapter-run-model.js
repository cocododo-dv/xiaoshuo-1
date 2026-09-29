/* ==========================================================
   章节编排「运行本章」的纯数据部分（2026-09-29 从 ws-chapter-run.jsx 拆出）
   ----------------------------------------------------------
   run-status 回包 → 运行记录（normalizeRun）、状态文案、按钮文字。纯函数，不读 store、不写 window。
   ========================================================== */

export const ACTIVE_STATUSES = new Set(["submitting", "pending", "running"]);
export const TERMINAL_STATUSES = new Set(["blocked", "failed", "completed"]);

export const EMPTY_RUN = Object.freeze({
  status: "idle",
  jobId: null,
  progressPct: 0,
  sceneCount: 0,
  completedCount: 0,
  currentSceneId: null,
  errorCode: null,
  message: "",
  refreshWarning: "",
});

export const STATUS_COPY = {
  submitting: { label: "正在启动", hint: "正在把本章交给运行队列。" },
  pending: { label: "已排队", hint: "任务已创建，正在等待执行。" },
  running: { label: "正在运行", hint: "场景会按章节顺序逐一生成与校验。" },
  blocked: { label: "运行受阻", hint: "处理阻塞项后，可以从这里继续运行。" },
  failed: { label: "运行失败", hint: "本次运行没有完成，请按提示处理后重试。" },
  completed: { label: "本章已完成", hint: "目录已刷新，可以去成稿中心通读与审阅。" },
};

/* 打开这一章时读到的「上次运行」——安静的一枚小标签，点开才看细节 */
export const LAST_RUN_LABEL = { completed: "已完成", blocked: "受阻", failed: "失败" };

export function clampPercent(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(0, Math.min(100, Math.round(parsed)));
}

export function runErrorMessage(error) {
  if (error && error.code === "LLM_DISABLED_FOR_CHAPTER_RUN") {
    return "当前未配置可用模型，请配置模型后再运行本章。";
  }
  return (error && error.message) || "章节运行请求失败，请稍后重试。";
}

export function normalizeRun(payload) {
  if (!payload || typeof payload !== "object") return null;
  const status = payload.status === "queued" ? "pending" : payload.status;
  if (!["idle", "pending", "running", "blocked", "failed", "completed"].includes(status)) return null;
  const latestError = payload.latest_error && typeof payload.latest_error === "object" ? payload.latest_error : null;
  const authorAction = latestError && latestError.author_action && typeof latestError.author_action === "object"
    ? latestError.author_action
    : null;
  return {
    status,
    jobId: payload.job_id || null,
    progressPct: clampPercent(payload.progress_pct),
    sceneCount: Math.max(0, Number(payload.scene_count) || 0),
    completedCount: Math.max(0, Number(payload.completed_count) || 0),
    currentSceneId: payload.current_scene_id || null,
    errorCode: (latestError && latestError.code) || null,
    message: (authorAction && authorAction.message) || (latestError && latestError.message) || "",
    refreshWarning: "",
  };
}

export function buttonLabel(run) {
  if (run.status === "submitting") return "启动中…";
  if (run.status === "pending") return "等待运行";
  if (run.status === "running") return `运行中 ${run.progressPct}%`;
  if (run.status === "completed") return "本章已完成";
  if (run.status === "blocked" || run.status === "failed") return "重新运行";
  return "运行本章";
}
