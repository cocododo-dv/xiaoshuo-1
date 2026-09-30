import { apiGet, apiPost } from "./lib/client.js";

/* ==========================================================
   起草任务控制条的两个请求（2026-09-29 从 lib/client.js 搬来：只有起草台用）
   ----------------------------------------------------------
   latest：这一场最近的一个起草任务（没有时 404）；cancel：请求取消，同一次取消的重试沿用
   同一个幂等键、AbortSignal 转给 fetch（都是 apiGet / apiPost 的契约，见 lib/client.test.js）。
   单独成一个叶子模块：单测整块 mock 它，而不必对 ws-scene-api.js 做半截 mock。ESM 模块，不写 window。
   ========================================================== */

export function getLatestSceneRunJob(sceneId, options) {
  return apiGet(`/api/v1/scenes/${encodeURIComponent(sceneId)}/run/jobs/latest`, options);
}

export function cancelRunJob(jobId, options) {
  return apiPost(`/api/v1/run-jobs/${encodeURIComponent(jobId)}/cancel`, {}, options);
}
