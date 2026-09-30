import { apiGet, apiPost } from "./lib/client.js";
import { createSubscribers, useStoreTick } from "./lib/store-utils.js";
import { WsWorks } from "./ws-works.jsx";
import { isRealWorkId } from "./lib/work-id.js";

/* ==========================================================
   文学质量的 store（从 ws-quality.jsx 拆出，2026-09-22）——轻量模块级缓存 + 订阅（视图用 useQualityState）。
   三个端点都不强制 project_id，故无 __loading__ 等待逻辑；视图挂载时拉取，
   失败只记在 error（errorScope 说明是哪一步），由视图就地显示，不弹浏览器对话框。
   状态按作品分开存（切到另一部作品看不到上一部的巡检、章组复审、扫描结果；切回来还在），
   每一步各自记着最后发出的那一次请求：两次「重新巡检」乱序回来时只认后发的那一次；
   换作品之后才回来的旧请求落回它自己那部作品，不会显示在新作品下面。
   ========================================================== */
const EMPTY = Object.freeze({ overview: null, analyze: null, review: null, loading: false, analyzing: false, reviewing: false, error: null, errorScope: null });
const qByWork = new Map();   // 作品 id → 状态
const qLatest = new Map();   // `${作品 id}:${步骤}` → 最后发出的请求序号

function qWorkKey() {
  try { return String(WsWorks.activeId() || ""); } catch (e) { return ""; }
}
function qStateOf(key) { return qByWork.get(key) || EMPTY; }

export function qSnapshot() { return qStateOf(qWorkKey()); }
const qSubs = createSubscribers();
function qEmit() { qSubs.notify(); }

function qPatch(key, patch) {
  qByWork.set(key, { ...qStateOf(key), ...patch });
  qEmit();
}

/* 开始一步：记下序号、清掉上一步的错误；返回「这一次还是不是最新的那一次」的判断 */
function qBegin(key, scope, patch) {
  const slot = `${key}:${scope}`;
  const mine = (qLatest.get(slot) || 0) + 1;
  qLatest.set(slot, mine);
  qPatch(key, { ...patch, error: null, errorScope: null });
  return () => qLatest.get(slot) === mine;
}

/* 查询串：跳过空值 */
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
  return isRealWorkId(pid) ? { ...filters, project_id: pid } : filters;
}

export async function qLoadOverview(filters = {}) {
  const key = qWorkKey();
  const latest = qBegin(key, "overview", { loading: true });
  try {
    const data = await apiGet(qBuildPath("/api/v1/literary-quality/overview", filters));
    if (latest()) qPatch(key, { overview: data || null, loading: false });
    return data;
  } catch (e) {
    if (latest()) qPatch(key, { loading: false, error: (e && e.message) || "巡检失败。", errorScope: "overview" });
    return null;
  }
}

export async function qAnalyzeText(content) {
  const text = (content || "").trim();
  if (!text) return null;
  const key = qWorkKey();
  const latest = qBegin(key, "analyze", { analyzing: true });
  try {
    const data = await apiPost("/api/v1/literary-quality/analyze-text", { content: text });
    if (latest()) qPatch(key, { analyze: data || null, analyzing: false });
    return data;
  } catch (e) {
    if (latest()) qPatch(key, { analyzing: false, error: (e && e.message) || "扫描失败。", errorScope: "analyze" });
    return null;
  }
}

export async function qChapterSetReview({ chapter_ids, protected_terms, text_layer } = {}) {
  const ids = (chapter_ids || []).filter(Boolean);
  if (!ids.length) return null;
  const key = qWorkKey();
  const latest = qBegin(key, "review", { reviewing: true });
  try {
    const data = await apiPost("/api/v1/literary-quality/chapter-set-review", {
      chapter_ids: ids,
      protected_terms: (protected_terms || []).filter(Boolean),
      text_layer: text_layer || "author_draft_preferred",
    });
    if (latest()) qPatch(key, { review: data || null, reviewing: false });
    return data;
  } catch (e) {
    if (latest()) qPatch(key, { reviewing: false, error: (e && e.message) || "章组复审失败。", errorScope: "review" });
    return null;
  }
}

export function useQualityState() {
  useStoreTick((bump) => qSubs.subscribe(bump));
  return qSnapshot();
}
