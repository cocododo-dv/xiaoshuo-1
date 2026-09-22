import { apiGet, apiPost } from "./lib/client.js";
import { useStoreTick } from "./lib/store-utils.js";
import { WsWorks } from "./ws-works.jsx";

/* ==========================================================
   文学质量的 store（从 ws-quality.jsx 拆出，2026-09-22）——轻量模块级缓存 + 自定义事件。
   三个端点都不强制 project_id，故无 __loading__ 等待逻辑；视图挂载时拉取，
   失败只记在 error（errorScope 说明是哪一步），由视图就地显示，不弹浏览器对话框。
   ========================================================== */
let qState = { overview: null, analyze: null, review: null, loading: false, analyzing: false, reviewing: false, error: null, errorScope: null };

export function qSnapshot() { return qState; }
function qEmit() { try { window.dispatchEvent(new CustomEvent("ws:quality-changed")); } catch (e) {} }

/* 自建查询串：只 import apiGet/apiPost，避免单测 mock 掉 buildQueryPath */
function qBuildPath(base, filters) {
  const p = new URLSearchParams();
  Object.entries(filters || {}).forEach(([k, v]) => {
    if (v === null || v === undefined || v === "") return;
    p.set(k, v);
  });
  const q = p.toString();
  return q ? `${base}?${q}` : base;
}

/* 把当前作品 id 注入 overview 过滤 —— 作者只看自己作品。
   作品列表还没到（__loading__）或没有作品时返回原 filters → 退回全局。 */
export function qScopeFilters(filters) {
  const pid = WsWorks.activeId();
  return pid && pid !== "__loading__" ? { ...filters, project_id: pid } : filters;
}

export async function qLoadOverview(filters = {}) {
  qState = { ...qState, loading: true, error: null, errorScope: null };
  qEmit();
  try {
    const data = await apiGet(qBuildPath("/api/v1/literary-quality/overview", filters));
    qState = { ...qState, overview: data || null, loading: false };
    qEmit();
    return data;
  } catch (e) {
    qState = { ...qState, loading: false, error: (e && e.message) || "巡检失败。", errorScope: "overview" };
    qEmit();
    return null;
  }
}

export async function qAnalyzeText(content) {
  const text = (content || "").trim();
  if (!text) return null;
  qState = { ...qState, analyzing: true, error: null, errorScope: null };
  qEmit();
  try {
    const data = await apiPost("/api/v1/literary-quality/analyze-text", { content: text });
    qState = { ...qState, analyze: data || null, analyzing: false };
    qEmit();
    return data;
  } catch (e) {
    qState = { ...qState, analyzing: false, error: (e && e.message) || "扫描失败。", errorScope: "analyze" };
    qEmit();
    return null;
  }
}

export async function qChapterSetReview({ chapter_ids, protected_terms, text_layer } = {}) {
  const ids = (chapter_ids || []).filter(Boolean);
  if (!ids.length) return null;
  qState = { ...qState, reviewing: true, error: null, errorScope: null };
  qEmit();
  try {
    const data = await apiPost("/api/v1/literary-quality/chapter-set-review", {
      chapter_ids: ids,
      protected_terms: (protected_terms || []).filter(Boolean),
      text_layer: text_layer || "author_draft_preferred",
    });
    qState = { ...qState, review: data || null, reviewing: false };
    qEmit();
    return data;
  } catch (e) {
    qState = { ...qState, reviewing: false, error: (e && e.message) || "章组复审失败。", errorScope: "review" };
    qEmit();
    return null;
  }
}

export function useQualityState() {
  useStoreTick((bump) => {
    window.addEventListener("ws:quality-changed", bump);
    return () => window.removeEventListener("ws:quality-changed", bump);
  });
  return qSnapshot();
}
