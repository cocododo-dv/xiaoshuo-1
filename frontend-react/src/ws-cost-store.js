import { apiGet } from "./lib/client.js";
import { createStore } from "./lib/store-kit.js";

/* ==========================================================
   成本看板的 store（从 ws-cost.jsx 拆出，2026-09-22）
   一份 createStore 状态；视图用 useCostState() 订阅（过去是广播 ws:cost-changed、再由本模块自己去听）。
   只读：项目级一读聚合 cost-dashboard，章节 / 场景下钻 cost-summary。
   请求序号（审计 F05-03）：每次 costLoad / costBack 都让之前发出、还没回来的请求作废——迟到的响应（换了作品、
   连点统计窗口、下钻途中点了「返回全书」）一律丢掉，不盖掉新的状态；换作品时先清掉上一部的看板，
   新作品的数到之前不把旧数摆在新作品名下。
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

/* 最近一次发出的请求的序号；回来时序号已经变了（发了新请求或返回全书）就作废 */
let csSerial = 0;

export async function costLoad(projectId, opts = {}) {
  if (!projectId) return null;
  const serial = ++csSerial;
  const days = Number(opts.days) > 0 ? Math.floor(Number(opts.days)) : csStore.get().days;
  const level = opts.sceneId ? "scene" : opts.chapterId ? "chapter" : "project";
  const switched = csStore.get().projectId !== projectId;
  csStore.set({
    projectId, level, days, loading: true, error: null,
    // 换了作品：上一部的看板、汇总与用量读数都不留；下钻：数到之前不把全书的汇总当成这一章 / 场的
    ...(switched ? { dashboard: null, quota: null } : {}),
    ...(switched || level !== "project" ? { summary: null } : {}),
  });
  const current = () => serial === csSerial;
  try {
    const base = `/api/v2/projects/${encodeURIComponent(projectId)}`;
    let data;
    if (level === "project") {
      data = await apiGet(`${base}/cost-dashboard?days=${days}`);
      if (!current()) return null;
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
      if (!current()) return null;
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
    if (current()) csStore.set({ loading: false, error: (e && e.message) || "成本加载失败。" });
    return null;
  }
}

/* 下钻返回：dashboard 还在缓存里就地还原，不重发请求；还在飞的下钻请求作废 */
export function costBack() {
  csSerial += 1;
  const state = csStore.get();
  if (state.dashboard) {
    csStore.set({ level: "project", summary: state.dashboard.summary || null, error: null, loading: false });
    return;
  }
  if (state.projectId) costLoad(state.projectId);
}

export function useCostState() {
  return csStore.useStore();
}
