import { apiGet } from "./lib/client.js";
import { createStore } from "./lib/store-kit.js";

/* ==========================================================
   成本看板的 store（从 ws-cost.jsx 拆出，2026-09-22）
   一份 createStore 状态；视图用 useCostState() 订阅（过去是广播 ws:cost-changed、再由本模块自己去听）。
   只读：项目级一读聚合 cost-dashboard，章节 / 场景下钻 cost-summary。
   ========================================================== */

const csStore = createStore({
  projectId: null,
  level: "project",   // project | chapter | scene
  days: 30,
  dashboard: null,    // 项目级一读聚合（含 summary/trend/by_*/top_calls）
  summary: null,      // 当前层 summary（project 时 = dashboard.summary；下钻时为下钻结果）
  quota: null,
  loading: false,
  error: null,
});

export function csSnapshot() { return csStore.get(); }

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
  const days = Number(opts.days) > 0 ? Math.floor(Number(opts.days)) : csStore.get().days;
  const level = opts.sceneId ? "scene" : opts.chapterId ? "chapter" : "project";
  csStore.set({ projectId, level, days, loading: true, error: null });
  try {
    const base = `/api/v2/projects/${encodeURIComponent(projectId)}`;
    let data;
    if (level === "project") {
      data = await apiGet(`${base}/cost-dashboard?days=${days}`);
      csStore.set({
        dashboard: data || null,
        summary: (data && data.summary) || null,
        quota: (data && data.quota) || null,
        loading: false,
      });
    } else {
      const qs = opts.sceneId
        ? `scene_id=${encodeURIComponent(opts.sceneId)}`
        : `chapter_id=${encodeURIComponent(opts.chapterId)}`;
      data = await apiGet(`${base}/cost-summary?${qs}`);
      csStore.set((state) => ({
        ...state,
        level: (data && data.level) || level,
        summary: (data && data.summary) || null,
        quota: (data && data.quota) || state.quota,
        loading: false,
      }));
    }
    return data;
  } catch (e) {
    csStore.set({ loading: false, error: (e && e.message) || "成本加载失败。" });
    return null;
  }
}

/* 下钻返回：dashboard 还在缓存里就地还原，不重发请求 */
export function costBack() {
  const state = csStore.get();
  if (state.dashboard) {
    csStore.set({ level: "project", summary: state.dashboard.summary || null, error: null });
    return;
  }
  if (state.projectId) costLoad(state.projectId);
}

export function useCostState() {
  return csStore.useStore();
}
