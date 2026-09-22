import React from "react";
import { apiGet } from "./lib/client.js";

/* ==========================================================
   成本看板的 store（从 ws-cost.jsx 拆出，2026-09-22）
   模块级快照 csState + ws:cost-changed 广播；视图用 useCostState() 订阅。
   只读：项目级一读聚合 cost-dashboard，章节 / 场景下钻 cost-summary。
   ========================================================== */

let csState = {
  projectId: null,
  level: "project",   // project | chapter | scene
  days: 30,
  dashboard: null,    // 项目级一读聚合（含 summary/trend/by_*/top_calls）
  summary: null,      // 当前层 summary（project 时 = dashboard.summary；下钻时为下钻结果）
  quota: null,
  loading: false,
  error: null,
};

export function csSnapshot() { return csState; }
function csEmit() { try { window.dispatchEvent(new CustomEvent("ws:cost-changed")); } catch (e) {} }

export const PHASE_LABEL = {
  candidate_generation: "候选生成",
  quality_check: "质检",
  revision: "修订",
  review: "评审",
  other: "其他",
};
export const COST_WINDOWS = [7, 30, 90];

export async function costLoad(projectId, opts = {}) {
  if (!projectId) return null;
  const days = Number(opts.days) > 0 ? Math.floor(Number(opts.days)) : csState.days;
  const level = opts.sceneId ? "scene" : opts.chapterId ? "chapter" : "project";
  csState = { ...csState, projectId, level, days, loading: true, error: null };
  csEmit();
  try {
    const base = `/api/v2/projects/${encodeURIComponent(projectId)}`;
    let data;
    if (level === "project") {
      data = await apiGet(`${base}/cost-dashboard?days=${days}`);
      csState = {
        ...csState,
        dashboard: data || null,
        summary: (data && data.summary) || null,
        quota: (data && data.quota) || null,
        loading: false,
      };
    } else {
      const qs = opts.sceneId
        ? `scene_id=${encodeURIComponent(opts.sceneId)}`
        : `chapter_id=${encodeURIComponent(opts.chapterId)}`;
      data = await apiGet(`${base}/cost-summary?${qs}`);
      csState = {
        ...csState,
        level: (data && data.level) || level,
        summary: (data && data.summary) || null,
        quota: (data && data.quota) || csState.quota,
        loading: false,
      };
    }
    csEmit();
    return data;
  } catch (e) {
    csState = { ...csState, loading: false, error: (e && e.message) || "成本加载失败。" };
    csEmit();
    return null;
  }
}

/* 下钻返回：dashboard 还在缓存里就地还原，不重发请求 */
export function costBack() {
  if (csState.dashboard) {
    csState = { ...csState, level: "project", summary: csState.dashboard.summary || null, error: null };
    csEmit();
    return;
  }
  if (csState.projectId) costLoad(csState.projectId);
}

export function useCostState() {
  const [, force] = React.useReducer((x) => x + 1, 0);
  React.useEffect(() => {
    const h = () => force();
    window.addEventListener("ws:cost-changed", h);
    return () => window.removeEventListener("ws:cost-changed", h);
  }, []);
  return csSnapshot();
}
