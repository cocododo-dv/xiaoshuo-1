import { apiGet } from "./lib/client.js";
import { srSpineColor } from "./ws-styleref-model.js";

/* ==========================================================
   风格参考 · store 的底座（2026-09-30 从 ws-styleref-store.js 拆出，审计 F05-15）
   ----------------------------------------------------------
   · 当前作品（界面层在加载时经 srConfigureHost 接上；没接上时当没有作品）；
   · 订阅频道 books / detail / activity / finished / imported：模块内的订阅者集合，不再借 window 事件——
     这些频道只有本页自己在听（审计 F05-24）；
   · 页面是否挂着、本次打开应用期间停在哪本书；
   · 书库 SR_BOOKS（摘要）与「这次删掉的书」；GET /runtime；按需读的详情缓存（键 → { phase, data, error }）。
   参考书活动（作业表 + /activity 轮询）在 ws-styleref-store-activity.js，写操作与门面在 ws-styleref-store.js。
   依赖只朝一个方向：activity → core、facade → core / activity；读完书库之后「补登在跑的作业」经 srOnBooksSynced
   登记口交给活动表，本模块不 import 它。只 import lib/client.js 与纯派生的 model；不写 window。
   ========================================================== */

export const SR_API = "/api/v2/style-reference";

/* ---------- 店外依赖 ---------- */
const SR_HOST = {
  activeWorkId: () => null,
};

export function srConfigureHost(host) {
  for (const key of Object.keys(SR_HOST)) {
    if (host && typeof host[key] === "function") SR_HOST[key] = host[key];
  }
}

export function srActiveWorkId() {
  try { return SR_HOST.activeWorkId() || null; } catch (e) { return null; }
}

/* ---------- 订阅频道 ----------
   books：书库摘要；detail：详情缓存与运行时；activity：活动表；finished：某条作业走到终态（detail = 那条条目）；
   imported：导入成功（detail = { bookId, jobId }）。模块内的订阅者集合（与 lib/store-utils 的 createSubscribers
   同一个口径：逐个调用、单个出错被吞掉），通知时把 { type, detail } 交给订阅者——读法与原来的 window 事件一样。 */
const srChannels = {
  books: new Set(), detail: new Set(), activity: new Set(), finished: new Set(), imported: new Set(),
};

export function srEmit(channel, detail) {
  const subs = srChannels[channel];
  if (!subs) return;
  const event = { type: channel, detail };
  Array.from(subs).forEach((fn) => { try { fn(event); } catch (e) { /* 一个订阅者出错不打断其余的 */ } });
}

/* 订阅若干频道：返回 (listener) => 退订函数（lib/store-utils 的 useStoreTick 要的形状） */
export function srSubscribe(...channels) {
  return (listener) => {
    const added = channels
      .filter((name) => srChannels[name])
      .map((name) => {
        const fn = (event) => listener(event);
        srChannels[name].add(fn);
        return [name, fn];
      });
    return () => added.forEach(([name, fn]) => srChannels[name].delete(fn));
  };
}

/* ---------- 页面是否挂着 ----------
   每次挂载清空「本次挂载里读过的键」：详情缓存是模块级的，离开页面期间可能在别处改过绑定、跑完了作业，
   回来后第一次读必须重读。运行时（有没有模型、分类节点在不在本机）同样：作者点「去设置模型」接好模型再回来，
   页面是重新挂载的——这时必须重读，不然学习卡、参考书页、对照检查一直锁着直到刷新整页。
   挂着的时候 /activity 读失败（后端在重启）会退避重试；不挂着时只在还有在跑的条目时才接着轮询。 */
const SR_FRESH = new Set();
let SR_VIEW_MOUNTED = false;
export function srSetViewMounted(mounted) {
  SR_VIEW_MOUNTED = !!mounted;
  if (mounted) {
    SR_FRESH.clear();
    SR_RUNTIME_FRESH = false;
  }
}
export function srViewMounted() { return SR_VIEW_MOUNTED; }

/* ---------- 本次打开应用期间停在哪本书、哪一步（不跨刷新） ---------- */
const SR_SESSION_UI = new Map();
export function srSessionUi(workId) { return SR_SESSION_UI.get(workId || "") || null; }
export function srRememberSession(workId, entry) {
  if (!entry || !entry.bookId) return;
  SR_SESSION_UI.set(workId || "", { bookId: String(entry.bookId), stage: entry.stage || null });
}

/* ==========================================================
   书库
   ========================================================== */
let SR_BOOKS = [];
let SR_BOOKS_STATE = { phase: "loading", error: null, code: "" };

export function srBooks() { return SR_BOOKS; }
export function srBooksState() { return SR_BOOKS_STATE; }
export function srBookById(bookId) { return bookId ? SR_BOOKS.find((b) => b.id === bookId) || null : null; }
export function srBookTitle(bookId) {
  const book = srBookById(bookId);
  return book ? book.title : null;
}

/* 写操作先改界面上的书库、失败换回原来的（门面用；books 频道由调用方决定何时通知） */
export function srSetBooks(next) { SR_BOOKS = next; }

function srMapBook(raw) {
  const b = raw || {};
  return {
    id: b.book_id,
    title: b.title || "未命名参考书",
    author: b.author_label || "",
    chars: Number(b.total_chars || 0),
    rawStatus: b.status || null,
    cloudPolicy: b.cloud_policy || null,
    paragraphCount: b.paragraph_count ?? null,
    classification: b.classification || null,
    provenance: b.classification_provenance || null,
    typesRevision: Number(b.paragraph_types_revision || 0),
    learn: b.learn || null,
    profile: b.profile || null,
    profileCount: Number(b.profile_count || 0),
    appliedProjects: Array.isArray(b.applied_projects) ? b.applied_projects : [],
    createdAt: b.created_at || null,
    color: srSpineColor(b.book_id),
  };
}

/* 这次打开应用期间删掉的书（书 id 不会复用）：删书请求之前发出的读书库 / 读活动响应晚到时，别让它们复活 */
const SR_DELETED_BOOKS = new Set();
export function srBookDeleted(bookId) { return SR_DELETED_BOOKS.has(bookId); }
export function srMarkBookDeleted(bookId) { SR_DELETED_BOOKS.add(bookId); }

/* 读完书库之后要做的事（参考书活动登记在跑的作业）：activity 模块在这里登记，本模块不反过来 import 它 */
const srBooksSyncedHooks = new Set();
export function srOnBooksSynced(fn) {
  srBooksSyncedHooks.add(fn);
  return () => { srBooksSyncedHooks.delete(fn); };
}

export async function srSyncBooks() {
  let rows;
  try {
    rows = ((await apiGet(`${SR_API}/books`)) || {}).books || [];
  } catch (e) {
    SR_BOOKS_STATE = { phase: "error", error: (e && e.message) || String(e), code: (e && e.code) || "" };
    srEmit("books");
    return SR_BOOKS;
  }
  SR_BOOKS = rows.map(srMapBook).filter((b) => !SR_DELETED_BOOKS.has(b.id));
  SR_BOOKS_STATE = { phase: "ready", error: null, code: "" };
  srEmit("books");
  srBooksSyncedHooks.forEach((fn) => { try { fn(SR_BOOKS); } catch (e) { /* 登记口出错不打断书库 */ } });
  return SR_BOOKS;
}

/* ==========================================================
   导入对话框的默认值（GET /runtime）
   ========================================================== */
let SR_RUNTIME = { phase: "idle", data: null };
/* 这次挂载里读过没有（页面每次挂载置 false：见 srSetViewMounted） */
let SR_RUNTIME_FRESH = false;
let SR_RUNTIME_INFLIGHT = null;
export function srRuntime() { return SR_RUNTIME; }

/* 读 GET /runtime。这次挂载里读过就用缓存（force 除外）；同一时间只发一个请求。重读期间保留上一次的结果
   （锁 / 解锁不闪），读到了再换。 */
export function srLoadRuntime({ force = false } = {}) {
  if (!force && SR_RUNTIME.phase === "ready" && SR_RUNTIME_FRESH) return Promise.resolve(SR_RUNTIME.data);
  if (SR_RUNTIME_INFLIGHT) return SR_RUNTIME_INFLIGHT;
  if (SR_RUNTIME.phase !== "ready") SR_RUNTIME = { ...SR_RUNTIME, phase: "loading" };
  const promise = (async () => {
    try {
      const data = await apiGet(`${SR_API}/runtime`);
      SR_RUNTIME = { phase: "ready", data: data || null };
      SR_RUNTIME_FRESH = true;
    } catch (e) {
      SR_RUNTIME = { phase: "error", data: null, error: (e && e.message) || String(e) };
    } finally {
      SR_RUNTIME_INFLIGHT = null;
      srEmit("detail");
    }
    return SR_RUNTIME.data;
  })();
  SR_RUNTIME_INFLIGHT = promise;
  return promise;
}

/* ==========================================================
   按需读的详情：通用的「键 → { phase, data, error }」缓存
   ========================================================== */
const SR_CACHE = new Map();
const SR_INFLIGHT = new Map();

export function srCacheGet(key) { return SR_CACHE.get(key) || null; }

/* 写一格并通知 detail 频道 */
export function srCacheSet(key, value) {
  SR_CACHE.set(key, value);
  srEmit("detail");
}

/* 不通知地改 / 删一格（批量改完由调用方通知一次） */
export function srCachePut(key, value) { SR_CACHE.set(key, value); }
export function srCacheDelete(key) { SR_CACHE.delete(key); }
export function srCacheEntries() { return Array.from(SR_CACHE.entries()); }
export function srCacheKeys() { return Array.from(SR_CACHE.keys()); }

export async function srCacheLoad(key, fetcher, { force = false } = {}) {
  const current = SR_CACHE.get(key);
  if (!force && current && current.phase === "ready" && SR_FRESH.has(key)) return current.data;
  /* force 撞上在途的请求：那个请求可能是改动之前发出的——等它回来再重读一次 */
  if (SR_INFLIGHT.has(key)) {
    const inflight = SR_INFLIGHT.get(key);
    return force ? inflight.then(() => srCacheLoad(key, fetcher, { force: true })) : inflight;
  }
  if (!current || current.phase !== "ready") SR_CACHE.set(key, { phase: "loading", data: current ? current.data : null, error: null });
  const promise = (async () => {
    try {
      const data = await fetcher();
      SR_CACHE.set(key, { phase: "ready", data, error: null });
      SR_FRESH.add(key);
      return data;
    } catch (e) {
      SR_CACHE.set(key, { phase: "error", data: current ? current.data : null, error: e });
      return null;
    } finally {
      SR_INFLIGHT.delete(key);
      srEmit("detail");
    }
  })();
  SR_INFLIGHT.set(key, promise);
  return promise;
}

/* 一本书的完整载荷（含 stats_json：段落类型分布、语料评估……） */
export function srBookDetail(bookId) { return srCacheGet(`book:${bookId}`); }
export function srLoadBookDetail(bookId, opts) {
  return srCacheLoad(`book:${bookId}`, async () => ((await apiGet(`${SR_API}/books/${encodeURIComponent(bookId)}`)) || {}).book || null, opts);
}

/* 学习信息：最近一次学习作业 + 学一次的估算 + 学习节点的路由 */
export function srLearnInfo(bookId) { return srCacheGet(`learn:${bookId}`); }
export function srLoadLearn(bookId, opts) {
  return srCacheLoad(`learn:${bookId}`, async () => (await apiGet(`${SR_API}/books/${encodeURIComponent(bookId)}/learn`)) || null, opts);
}

/* 文风画像详情 */
export function srProfileDetail(profileId) { return srCacheGet(`profile:${profileId}`); }
export function srLoadProfile(profileId, opts) {
  return srCacheLoad(`profile:${profileId}`, async () => ((await apiGet(`${SR_API}/profiles/${encodeURIComponent(profileId)}`)) || {}).profile || null, opts);
}

/* 一部作品现在实际生效的绑定 */
export function srProjectBinding(projectId) { return srCacheGet(`project:${projectId}`); }
export function srLoadProjectBinding(projectId, opts) {
  return srCacheLoad(`project:${projectId}`, async () => (await apiGet(`${SR_API}/projects/${encodeURIComponent(projectId)}/style-binding`)) || null, opts);
}

/* 一份画像的全部绑定（含场景级 / 角色级、已停用的） */
export function srProfileBindings(profileId) { return srCacheGet(`bindings:${profileId}`); }
export function srLoadProfileBindings(profileId, opts) {
  return srCacheLoad(`bindings:${profileId}`, async () => ((await apiGet(`${SR_API}/profiles/${encodeURIComponent(profileId)}/bindings`)) || {}).bindings || [], opts);
}

/* 单测用：清空本模块的状态（门面的 srResetForTests 连同活动表一起清） */
export function srResetCoreForTests() {
  SR_BOOKS = [];
  SR_BOOKS_STATE = { phase: "loading", error: null, code: "" };
  SR_RUNTIME = { phase: "idle", data: null };
  SR_RUNTIME_FRESH = false;
  SR_RUNTIME_INFLIGHT = null;
  SR_VIEW_MOUNTED = false;
  SR_CACHE.clear();
  SR_INFLIGHT.clear();
  SR_FRESH.clear();
  SR_DELETED_BOOKS.clear();
  SR_SESSION_UI.clear();
}
