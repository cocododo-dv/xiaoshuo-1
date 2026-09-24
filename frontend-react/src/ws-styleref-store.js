import { apiDelete, apiGet, apiPatch, apiPost } from "./lib/client.js";
import { srActivityActive, srNormalizeConfig, srSpineColor } from "./ws-styleref-model.js";

/* ==========================================================
   风格参考 · store（2026-09-23 v3 重建）
   ----------------------------------------------------------
   模块级缓存 + window 事件广播，界面订阅事件重渲：
   · 书库 SR_BOOKS（摘要：状态、段落类型来源、最近的分类 / 学习作业、画像摘要、用在了哪些作品上）
     → sr:books-changed
   · 按需读的详情：一本书（含 stats_json）、学习信息（作业 + 估算）、重新分类的估算、文风画像、一部作品的生效绑定
     → sr:detail-changed
   · 参考书活动：作业表条目（段落分类 / 学习文风 / 对照检查，key 以 job: 开头）+ 一个 /activity 轮询；
     别的条目不收 → sr:activity-changed；某条从进行中走到终态 → sr:activity-finished；全量清单里消失的在跑条目
     收尾拿掉（删书），书库摘要里在跑的作业补登进来，读失败退避重试
   · 导入成功 → sr:book-imported（页面据此切到新书）
   写操作都是「先改界面、再等服务端；失败回滚并把错误抛给调用方」（✓ / ✗、改绑定、解除、批量删除、用于作品），
   说法由界面按 ws-styleref-model 的 srErrorInfo 给。所有请求都经 lib/client.js（上传也是：FormData）。
   只 import lib/client.js 与纯派生 ws-styleref-model.js；不写 window（事件用 window.dispatchEvent 广播）。
   当前作品由界面层在加载时经 srConfigureHost 接上（ws-styleref-ui.jsx）；没接上时当没有作品。
   ========================================================== */

const API = "/api/v2/style-reference";

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

/* ---------- 事件 ---------- */
const SR_EVENTS = {
  books: "sr:books-changed",
  detail: "sr:detail-changed",
  activity: "sr:activity-changed",
  finished: "sr:activity-finished",
  imported: "sr:book-imported",
};

function srEmit(channel, detail) {
  window.dispatchEvent(detail === undefined ? new CustomEvent(SR_EVENTS[channel]) : new CustomEvent(SR_EVENTS[channel], { detail }));
}

/* 订阅若干频道：返回 (listener) => 退订函数（lib/store-utils 的 useStoreTick 要的形状） */
export function srSubscribe(...channels) {
  return (listener) => {
    const names = channels.map((c) => SR_EVENTS[c] || c);
    names.forEach((n) => window.addEventListener(n, listener));
    return () => names.forEach((n) => window.removeEventListener(n, listener));
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

export function srMapBook(raw) {
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

export async function srSyncBooks() {
  let rows;
  try {
    rows = ((await apiGet(`${API}/books`)) || {}).books || [];
  } catch (e) {
    SR_BOOKS_STATE = { phase: "error", error: (e && e.message) || String(e), code: (e && e.code) || "" };
    srEmit("books");
    return SR_BOOKS;
  }
  SR_BOOKS = rows.map(srMapBook).filter((b) => !SR_DELETED_BOOKS.has(b.id));
  SR_BOOKS_STATE = { phase: "ready", error: null, code: "" };
  srEmit("books");
  srActivitySeedFromBooks();
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
      const data = await apiGet(`${API}/runtime`);
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

function srCacheGet(key) { return SR_CACHE.get(key) || null; }

function srCacheSet(key, value) {
  SR_CACHE.set(key, value);
  srEmit("detail");
}

async function srCacheLoad(key, fetcher, { force = false } = {}) {
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

function srCacheDrop(prefix) {
  for (const key of Array.from(SR_CACHE.keys())) if (key.startsWith(prefix)) SR_CACHE.delete(key);
}

/* 一本书的完整载荷（含 stats_json：段落类型分布、语料评估……） */
export function srBookDetail(bookId) { return srCacheGet(`book:${bookId}`); }
export function srLoadBookDetail(bookId, opts) {
  return srCacheLoad(`book:${bookId}`, async () => ((await apiGet(`${API}/books/${encodeURIComponent(bookId)}`)) || {}).book || null, opts);
}

/* 学习信息：最近一次学习作业 + 学一次的估算 + 学习节点的路由 */
export function srLearnInfo(bookId) { return srCacheGet(`learn:${bookId}`); }
export function srLoadLearn(bookId, opts) {
  return srCacheLoad(`learn:${bookId}`, async () => (await apiGet(`${API}/books/${encodeURIComponent(bookId)}/learn`)) || null, opts);
}

/* 用模型重新分类（就地）的费用估算 */
export function srClassifyEstimate(bookId) { return srCacheGet(`estimate:${bookId}`); }
export function srLoadClassifyEstimate(bookId, opts) {
  return srCacheLoad(`estimate:${bookId}`, async () => ((await apiGet(`${API}/books/${encodeURIComponent(bookId)}/classification/estimate`)) || {}).estimate || null, opts);
}

/* 文风画像详情 */
export function srProfileDetail(profileId) { return srCacheGet(`profile:${profileId}`); }
export function srLoadProfile(profileId, opts) {
  return srCacheLoad(`profile:${profileId}`, async () => ((await apiGet(`${API}/profiles/${encodeURIComponent(profileId)}`)) || {}).profile || null, opts);
}

/* 一部作品现在实际生效的绑定 */
export function srProjectBinding(projectId) { return srCacheGet(`project:${projectId}`); }
export function srLoadProjectBinding(projectId, opts) {
  return srCacheLoad(`project:${projectId}`, async () => (await apiGet(`${API}/projects/${encodeURIComponent(projectId)}/style-binding`)) || null, opts);
}

/* 一份画像的全部绑定（含场景级 / 角色级、已停用的） */
export function srProfileBindings(profileId) { return srCacheGet(`bindings:${profileId}`); }
export function srLoadProfileBindings(profileId, opts) {
  return srCacheLoad(`bindings:${profileId}`, async () => ((await apiGet(`${API}/profiles/${encodeURIComponent(profileId)}/bindings`)) || {}).bindings || [], opts);
}

/* 绑定变了（解除、删书）：读过的作品的生效绑定全部重读（旧版的全局绑定挂在每一部没有自己应用的作品上，
   解除它影响的不只一部），再加上 extra 与当前作品 */
function srReloadProjectBindings(...extra) {
  const ids = new Set(extra.filter(Boolean));
  const active = srActiveWorkId();
  if (active) ids.add(active);
  for (const key of SR_CACHE.keys()) if (key.startsWith("project:")) ids.add(key.slice("project:".length));
  ids.forEach((id) => { srLoadProjectBinding(id, { force: true }); });
}

/* 读过的「一份画像的全部绑定」重读（界面上「这份文风还用在」读它；删掉缓存不重读，那一块就消失了） */
function srReloadBindingLists(profileIds = null) {
  const only = profileIds ? new Set(profileIds.filter(Boolean)) : null;
  for (const key of Array.from(SR_CACHE.keys())) {
    if (!key.startsWith("bindings:")) continue;
    const profileId = key.slice("bindings:".length);
    if (!only || only.has(profileId)) srLoadProfileBindings(profileId, { force: true });
  }
}

/* ==========================================================
   文风卡一句的 ✓ / ✗（先改界面，失败回滚）
   ========================================================== */
function srWithLineState(profile, lineId, state) {
  const states = { ...(profile.card_line_states || {}) };
  if (state) states[lineId] = state; else delete states[lineId];
  return {
    ...profile,
    card_line_states: states,
    dimensions: (profile.dimensions || []).map((dim) => (
      (dim.lines || []).some((line) => line.line_id === lineId)
        ? { ...dim, lines: dim.lines.map((line) => (line.line_id === lineId ? { ...line, state: state || null } : line)) }
        : dim
    )),
  };
}

export async function srSetCardLineState(profileId, lineId, state) {
  const key = `profile:${profileId}`;
  const entry = SR_CACHE.get(key);
  const before = entry && entry.data ? entry.data : null;
  if (before) srCacheSet(key, { ...entry, data: srWithLineState(before, lineId, state) });
  try {
    const result = await apiPost(`${API}/profiles/${encodeURIComponent(profileId)}/card-lines/${encodeURIComponent(lineId)}`, { state: state || null });
    const now = SR_CACHE.get(key);
    if (now && now.data && result && result.card_line_states) {
      const reconciled = { ...now.data, card_line_states: result.card_line_states };
      reconciled.dimensions = (reconciled.dimensions || []).map((dim) => ({
        ...dim,
        lines: (dim.lines || []).map((line) => ({ ...line, state: result.card_line_states[line.line_id] || null })),
      }));
      srCacheSet(key, { ...now, data: reconciled });
    }
    return result;
  } catch (e) {
    if (before) srCacheSet(key, { ...(SR_CACHE.get(key) || entry), data: before });
    throw e;
  }
}

/* ==========================================================
   用于作品：直接绑定 / 改配置 / 解除（先改界面，失败回滚）
   ========================================================== */

/* 与后端 merge_config 同一口径：顶层三键覆盖，维度状态按维合并 */
export function srMergeConfig(base, patch) {
  const current = srNormalizeConfig(base);
  const p = patch || {};
  const next = { ...current };
  ["reference_mode", "sample_windows", "draft_mode"].forEach((k) => { if (p[k] != null) next[k] = p[k]; });
  if (p.dimension_states) next.dimension_states = { ...current.dimension_states, ...p.dimension_states };
  return srNormalizeConfig(next);
}

function srPatchBooksApplied(mutator) {
  let changed = false;
  SR_BOOKS = SR_BOOKS.map((book) => {
    const next = mutator(book);
    if (next !== book) changed = true;
    return next;
  });
  if (changed) srEmit("books");
}

function srBookOfProfile(profileId) {
  return SR_BOOKS.find((b) => b.profile && b.profile.profile_id === profileId) || null;
}

/* 把画像用于作品。先把这部作品的生效绑定改成「在用这份」（pending），成功后换成服务端的结果并刷新书库；
   失败回滚。返回 { binding, created, changed, replaced }。
   baseConfig：这份画像在这部作品上已有的（停用的）绑定的配置——换回这本书时后端在它上面合并，乐观值也照它算。 */
export async function srApplyProfile(profileId, { projectId, config = {}, baseConfig = null } = {}) {
  if (!profileId || !projectId) throw Object.assign(new Error("还没有打开作品"), { code: "SR_NO_WORK" });
  const key = `project:${projectId}`;
  const entry = SR_CACHE.get(key);
  const before = entry ? entry.data : null;
  const booksBefore = SR_BOOKS;
  const book = srBookOfProfile(profileId);
  const sameOwn = !!(before && before.binding && before.binding.profile_id === profileId
    && before.binding.scope === "project" && before.binding.scope_ref_id === projectId);
  const optimistic = {
    ...(before || { project_id: projectId }),
    binding: {
      binding_id: sameOwn ? before.binding.binding_id : null,
      profile_id: profileId,
      scope: "project",
      scope_ref_id: projectId,
      status: "active",
      config: srMergeConfig(sameOwn ? before.binding.config : baseConfig, config),
      pending: true,
    },
    profile: book ? book.profile : (before ? before.profile : null),
    book: book ? { book_id: book.id, title: book.title, cloud_policy: book.cloudPolicy } : (before ? before.book : null),
  };
  srCacheSet(key, { phase: "ready", data: optimistic, error: null });
  srPatchBooksApplied((b) => {
    const others = (b.appliedProjects || []).filter((item) => item.project_id !== projectId);
    if (b.profile && b.profile.profile_id === profileId) {
      return { ...b, appliedProjects: [...others, { project_id: projectId, profile_id: profileId, binding_id: null, config: optimistic.binding.config }] };
    }
    return others.length !== (b.appliedProjects || []).length ? { ...b, appliedProjects: others } : b;
  });
  let result;
  try {
    result = await apiPost(`${API}/profiles/${encodeURIComponent(profileId)}/apply`, { scope: "project", scope_ref_id: projectId, config });
  } catch (e) {
    srCacheSet(key, entry || { phase: "idle", data: null, error: null });
    SR_BOOKS = booksBefore;
    srEmit("books");
    throw e;
  }
  const current = SR_CACHE.get(key);
  srCacheSet(key, { phase: "ready", data: { ...(current ? current.data : optimistic), binding: result.binding }, error: null });
  /* 「这份文风还用在」读这份画像的绑定清单：重读，不是删掉（删掉没人重读，那一块就消失了）；被这次换下来的
     别的画像的绑定停用了，读过它们清单的也重读 */
  srLoadProfileBindings(profileId, { force: true });
  srReloadBindingLists(((result && result.replaced) || []).map((r) => r && r.profile_id).filter((pid) => pid && pid !== profileId));
  srLoadProjectBinding(projectId, { force: true });
  srSyncBooks();
  return result;
}

/* 改一条绑定的配置（维度状态按维合并）。projectId 给了就先改那部作品缓存里的配置，失败回滚。 */
export async function srUpdateBinding(bindingId, patch, { projectId = null } = {}) {
  const key = projectId ? `project:${projectId}` : null;
  const entry = key ? SR_CACHE.get(key) : null;
  const before = entry ? entry.data : null;
  const booksBefore = SR_BOOKS;
  if (before && before.binding && before.binding.binding_id === bindingId) {
    const nextConfig = srMergeConfig(before.binding.config, patch);
    srCacheSet(key, { ...entry, data: { ...before, binding: { ...before.binding, config: nextConfig } } });
    srPatchBooksApplied((b) => (
      (b.appliedProjects || []).some((item) => item.binding_id === bindingId)
        ? { ...b, appliedProjects: b.appliedProjects.map((item) => (item.binding_id === bindingId ? { ...item, config: nextConfig } : item)) }
        : b
    ));
  }
  try {
    const result = await apiPatch(`${API}/bindings/${encodeURIComponent(bindingId)}`, { config: patch });
    const now = key ? SR_CACHE.get(key) : null;
    if (now && now.data && now.data.binding && now.data.binding.binding_id === bindingId && result && result.binding) {
      srCacheSet(key, { ...now, data: { ...now.data, binding: result.binding } });
    }
    return result;
  } catch (e) {
    if (key && entry) srCacheSet(key, entry);
    SR_BOOKS = booksBefore;
    srEmit("books");
    throw e;
  }
}

/* 文风画像页的「重点 / 正常 / 不学」：写在当前作品的绑定上 */
export function srSetDimensionState(projectId, bindingId, dimension, state) {
  return srUpdateBinding(bindingId, { dimension_states: { [dimension]: state } }, { projectId });
}

/* 解除一条绑定。先把作品缓存与书库里的「在用」去掉，失败回滚。
   缓存里凡是生效绑定就是这一条的作品都先改成「没有在用」（旧版的全局绑定挂在每一部没有自己应用的作品上）；
   成功后这些作品、当前作品与 projectId 的生效绑定一律重读——解除的不是作品自己那条时，作品页也不会还说「在用」。 */
export async function srUnbind(bindingId, { projectId = null, profileId = null } = {}) {
  const booksBefore = SR_BOOKS;
  const touched = [];
  for (const [key, entry] of Array.from(SR_CACHE.entries())) {
    if (!key.startsWith("project:")) continue;
    if (entry && entry.data && entry.data.binding && entry.data.binding.binding_id === bindingId) {
      touched.push([key, entry]);
      SR_CACHE.set(key, { ...entry, data: { ...entry.data, binding: null } });
    }
  }
  if (touched.length) srEmit("detail");
  srPatchBooksApplied((b) => (
    (b.appliedProjects || []).some((item) => item.binding_id === bindingId)
      ? { ...b, appliedProjects: b.appliedProjects.filter((item) => item.binding_id !== bindingId) }
      : b
  ));
  try {
    const result = await apiDelete(`${API}/bindings/${encodeURIComponent(bindingId)}`);
    if (profileId) srLoadProfileBindings(profileId, { force: true });
    srReloadProjectBindings(projectId);
    srSyncBooks();
    return result;
  } catch (e) {
    touched.forEach(([key, entry]) => SR_CACHE.set(key, entry));
    if (touched.length) srEmit("detail");
    SR_BOOKS = booksBefore;
    srEmit("books");
    throw e;
  }
}

/* ==========================================================
   删书（单本 / 多本都走批量删除）：先从书库里拿掉，服务端没删成的放回来
   ========================================================== */
export async function srDeleteBooks(bookIds) {
  const ids = Array.from(new Set((bookIds || []).filter(Boolean)));
  if (!ids.length) return { results: [], deleted_count: 0, failed_count: 0 };
  const booksBefore = SR_BOOKS;
  SR_BOOKS = SR_BOOKS.filter((b) => !ids.includes(b.id));
  srEmit("books");
  let result;
  try {
    result = await apiPost(`${API}/books/bulk-delete`, { book_ids: ids });
  } catch (e) {
    SR_BOOKS = booksBefore;
    srEmit("books");
    throw e;
  }
  const failed = new Set(
    (result.results || [])
      .filter((item) => !item.deleted && !(item.error && item.error.code === "STYLE_REFERENCE_BOOK_NOT_FOUND"))
      .map((item) => item.book_id),
  );
  if (failed.size) {
    SR_BOOKS = [...SR_BOOKS, ...booksBefore.filter((b) => failed.has(b.id))];
    srEmit("books");
  }
  const deleted = ids.filter((id) => !failed.has(id));
  const deletedProfiles = new Set();
  deleted.forEach((id) => {
    SR_DELETED_BOOKS.add(id);
    ["book:", "learn:", "estimate:"].forEach((prefix) => SR_CACHE.delete(`${prefix}${id}`));
    const was = booksBefore.find((b) => b.id === id);
    if (was && was.profile && was.profile.profile_id) deletedProfiles.add(was.profile.profile_id);
    /* 这本书的活动条目全部拿掉——在跑的也是：作业行随书一起删了，服务端不会再列出它们，留着就是一条永远
       「进行中」的幽灵条目，轮询也因此永远停不下来 */
    for (const e of Array.from(SR_ACTIVITY.values())) if (e.book_id === id) SR_ACTIVITY.delete(e.key);
  });
  if (deleted.length) {
    for (const [key, entry] of SR_CACHE) {
      if (key.startsWith("profile:") && entry && entry.data && deleted.includes(entry.data.book_id)) deletedProfiles.add(key.slice("profile:".length));
    }
    deletedProfiles.forEach((profileId) => {
      SR_CACHE.delete(`profile:${profileId}`);
      SR_CACHE.delete(`bindings:${profileId}`);
    });
    /* 删书连同它的绑定一起删了：用着它的作品先改成「没有在用」，再把读过的作品生效绑定与各画像的绑定清单
       都重读，哪一页都不再说它「在用」 */
    for (const [key, entry] of Array.from(SR_CACHE.entries())) {
      if (!key.startsWith("project:") || !entry || !entry.data) continue;
      const bound = entry.data.binding;
      const bookOf = entry.data.book && entry.data.book.book_id;
      if ((bound && deletedProfiles.has(bound.profile_id)) || (bookOf && deleted.includes(bookOf))) {
        SR_CACHE.set(key, { ...entry, data: { ...entry.data, binding: null, profile: null, book: null } });
      }
    }
    srReloadProjectBindings();
    srReloadBindingLists();
  }
  srEmit("detail");
  srEmit("activity");
  srSyncBooks();
  return result;
}

/* ==========================================================
   导入：multipart 经 lib/client 的 apiPost（FormData），同一个幂等键重试时后端重放
   成功：把分类作业登记进活动表、刷新书库、广播 sr:book-imported；失败原样抛给导入对话框
   ========================================================== */
export async function srRunImport({ file, title, authorLabel = null, cloudPolicy, rightsDeclaration = null, importKey = null }) {
  const form = new FormData();
  form.append("file", file, file.name);
  form.append("title", title);
  if (authorLabel && String(authorLabel).trim()) form.append("author_label", String(authorLabel).trim());
  form.append("cloud_policy", cloudPolicy);
  if (rightsDeclaration) form.append("rights_declaration", JSON.stringify(rightsDeclaration));
  const key = importKey || `sr-import-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  const data = (await apiPost(`${API}/books/import-upload`, form, { idempotencyKey: key })) || {};
  const book = data.book || {};
  if (data.job_id) srActivityTrack(data.job_id, { kind: "classify", mode: "import", book_id: book.book_id, title: book.title || title });
  await srSyncBooks();
  srEmit("imported", { bookId: book.book_id || null, jobId: data.job_id || null });
  return data;
}

/* ==========================================================
   段落分类：用模型重新分类（就地重标类型，保留画像与绑定）/ 继续 / 取消
   ========================================================== */
export async function srRetype(bookId) {
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/reclassify`, { mode: "retype" });
  if (data && data.job_id) srActivityTrack(data.job_id, { kind: "classify", mode: "retype", book_id: bookId, title: srBookTitle(bookId) });
  SR_CACHE.delete(`estimate:${bookId}`);
  srSyncBooks();
  return data;
}

export async function srResumeClassification(bookId) {
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/reclassify`, { resume: true });
  if (data && data.job_id) srActivityTrack(data.job_id, { kind: "classify", mode: data.mode, book_id: bookId, title: srBookTitle(bookId) });
  srSyncBooks();
  return data;
}

export async function srCancelClassification(bookId) {
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/classification/cancel`, {});
  srActivityPoke();
  return data;
}

/* ==========================================================
   学习文风：开始 / 继续 / 取消
   ========================================================== */
/* resume：从断点续跑；force：正文很短也学（作业因 input_too_small 失败、或建作业时 409 STYLE_REFERENCE_INPUT_TOO_SMALL 之后）；
   retag：全书窗口的标签都重打（缺省只补标签版本旧了的窗） */
export async function srStartLearn(bookId, { resume = false, force = false, retag = false } = {}) {
  const body = {};
  if (resume) body.resume = true;
  if (force) body.force = true;
  if (retag) body.retag = true;
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/learn`, body);
  if (data && data.job_id) srActivityTrack(data.job_id, { kind: "learn", book_id: bookId, title: srBookTitle(bookId) });
  srLoadLearn(bookId, { force: true });
  srSyncBooks();
  return data;
}

export async function srCancelLearn(bookId) {
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/learn/cancel`, {});
  srActivityPoke();
  srLoadLearn(bookId, { force: true });
  return data;
}

/* ==========================================================
   本场预览（只读，不缓存：设置一变就重算）与当前作品的场景
   ========================================================== */
export async function srScenePreview(profileId, { sceneId = null, projectId = null, config = {} } = {}) {
  const c = srNormalizeConfig(config);
  const body = {
    reference_mode: c.reference_mode,
    sample_windows: c.sample_windows,
    dimension_states: c.dimension_states,
    draft_mode: c.draft_mode,
  };
  if (sceneId) body.scene_id = sceneId;
  if (projectId) body.project_id = projectId;
  return apiPost(`${API}/profiles/${encodeURIComponent(profileId)}/injection-preview`, body);
}

/* 当前作品的章与场（本场预览选场用）：[{ chapterId, no, title, scenes: [{ sceneId, title }] }] */
export async function srLoadWorkScenes(projectId) {
  const data = await apiGet(`/api/v2/projects/${encodeURIComponent(projectId)}/catalog`);
  return ((data && data.chapters) || []).map((c, index) => ({
    chapterId: c.chapter_id || c.slug || `ch${index + 1}`,
    no: Number(c.no || c.n || index + 1),
    title: c.title || "",
    scenes: (c.scenes || []).filter((s) => s && s.scene_id).map((s) => ({ sceneId: s.scene_id, title: s.title || "" })),
  }));
}

/* 参考书原文的一段范围（本场预览里展开一个样例窗；只在本机给作者看）：闭区间，一次至多 80 段 */
export async function srLoadParagraphs(bookId, start, end) {
  const qs = new URLSearchParams({ start: String(start), end: String(end) });
  const data = await apiGet(`${API}/books/${encodeURIComponent(bookId)}/paragraphs?${qs.toString()}`);
  return { paragraphs: (data && data.paragraphs) || [], capped: !!(data && data.capped), end: data ? data.end : end };
}

/* ==========================================================
   禁用词（本书专名由学习作业识别；作者自己加的「起草时不许出现」的词）
   ========================================================== */
export async function srLoadBannedTerms(profileId) {
  return ((await apiGet(`${API}/profiles/${encodeURIComponent(profileId)}/banned-terms`)) || {}).terms || [];
}

export async function srAddBannedTerm(profileId, term) {
  return apiPost(`${API}/profiles/${encodeURIComponent(profileId)}/banned-terms`, { term, scope: "generation" });
}

export async function srRemoveBannedTerm(termId) {
  return apiDelete(`${API}/banned-terms/${encodeURIComponent(termId)}`);
}

/* ==========================================================
   参考书活动：作业表条目（段落分类 / 学习文风 / 对照检查）+ /activity 轮询
   · 只收作业表条目（key 以 job: 开头），别的不收；
   · 条目到终态时按 kind 刷新对应缓存，再广播 sr:activity-finished；
   · 作者关掉的终态条目记下来，别让服务端清单（10 分钟内结束的也会回来）每轮把它们复活；
   · 一次成功的 /activity 就是服务端的全量清单：本地还「进行中」、登记已超过几秒、清单里却没有了的条目
     （书删了，作业行随书删了；或别处清掉了）收尾拿掉——不然它永远「进行中」，轮询也永远停不下来；
   · 书库摘要里说在排队 / 在跑的分类、学习作业，活动表里没有就补登（第一次 /activity 失败、刷新了页面……），
     并开始轮询；/activity 读失败时退避重试（页面挂着、或还有在跑的条目时）。
   ========================================================== */
const SR_ACTIVITY = new Map();
const SR_ACTIVITY_DISMISSED = new Set();
/* 在全量清单里消失、已经收尾拿掉的作业：书库摘要（可能比活动清单旧）不再把它们补登回来 */
const SR_ACTIVITY_VANISHED = new Set();
const SR_ACTIVITY_POLL_MS = 1500;
/* 登记之后这么久还没出现在全量清单里，就当它已经不在了 */
export const SR_ACTIVITY_VANISH_GRACE_MS = 5000;
const SR_ACTIVITY_BACKOFF_MAX_MS = 15000;

export function srActivityEntries() {
  return Array.from(SR_ACTIVITY.values()).sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0));
}

/* 某本书正在排队 / 运行的某类作业（kind：classify / learn / check；不给就任意一类） */
export function srActivityFor(bookId, kind = null) {
  if (!bookId) return null;
  for (const e of SR_ACTIVITY.values()) {
    if (srActivityActive(e) && e.book_id === bookId && (!kind || e.kind === kind)) return e;
  }
  return null;
}

function srBookTitle(bookId) {
  const book = srBookById(bookId);
  return book ? book.title : null;
}

/* 发起作业后先登记一条本地条目（服务端快照到了再覆盖）。
   续跑（「继续分类 / 继续学习」）沿用同一个作业 id：旧条目是上一次的终态（失败 / 取消），这次的排队必须盖过它，
   作者关掉过它的记号也一并清掉——不然界面上看不到在跑，跑完了结果还被当成「关掉过」丢掉。 */
export function srActivityTrack(jobId, { kind, mode = null, book_id = null, title = null } = {}) {
  if (!jobId) return;
  const key = `job:${jobId}`;
  SR_ACTIVITY_DISMISSED.delete(key);
  SR_ACTIVITY_VANISHED.delete(key);
  const current = SR_ACTIVITY.get(key) || null;
  const live = current && srActivityActive(current) ? current : null;
  const now = Date.now();
  SR_ACTIVITY.set(key, {
    ...(live || {}),
    key,
    job_id: jobId,
    kind: kind || (current && current.kind) || null,
    mode: mode || (current && current.mode) || null,
    book_id: book_id || (current && current.book_id) || null,
    title: title || (current && current.title) || null,
    status: live ? live.status : "queued",
    phase_label: live ? live.phase_label : "排队中",
    percent: live && live.percent != null ? live.percent : 0,
    startedAt: (live && live.startedAt) || now,
    trackedAt: now,
    seenTerminal: false,
  });
  srEmit("activity");
  srActivityPoke();
}

/* 书库摘要里说在排队 / 在跑、活动表里却没有的分类 / 学习作业：补登一条，开始轮询 */
function srActivitySeedFromBooks() {
  let seeded = false;
  const now = Date.now();
  for (const book of SR_BOOKS) {
    for (const [kind, job] of [["classify", book.classification], ["learn", book.learn]]) {
      if (!job || !job.job_id || (job.state !== "queued" && job.state !== "running")) continue;
      const key = `job:${job.job_id}`;
      if (SR_ACTIVITY.has(key) || SR_ACTIVITY_DISMISSED.has(key) || SR_ACTIVITY_VANISHED.has(key)) continue;
      const done = Number(kind === "classify" ? job.batches_done : job.done) || 0;
      const total = Number(kind === "classify" ? job.batches_total : job.total) || 0;
      SR_ACTIVITY.set(key, {
        key,
        job_id: job.job_id,
        kind,
        mode: kind === "classify" ? job.mode || null : null,
        book_id: book.id,
        title: book.title,
        status: job.state,
        phase_label: job.phase_label || (job.state === "queued" ? "排队中" : "进行中"),
        percent: total > 0 ? Math.round((1000 * done) / total) / 10 : null,
        steps: total > 0 ? { done, total } : null,
        cancel_requested: !!job.cancel_requested,
        stalled: !!job.stalled,
        startedAt: Date.parse(job.started_at || job.created_at || "") || now,
        trackedAt: now,
        seenTerminal: false,
      });
      seeded = true;
    }
  }
  if (!seeded) return;
  srEmit("activity");
  srActivityStart();
}

async function srActivityFinished(entry) {
  const bookId = entry.book_id;
  try {
    if (entry.kind === "classify" || entry.kind === "learn") await srSyncBooks();
    if (bookId) {
      srLoadBookDetail(bookId, { force: true });
      srLoadLearn(bookId, { force: true });
      SR_CACHE.delete(`estimate:${bookId}`);
    }
    if (entry.kind === "learn") {
      const profileId = entry.profile_id || (entry.result && entry.result.profile_id);
      if (profileId) srLoadProfile(profileId, { force: true });
    }
  } catch (e) { /* 刷新失败不影响面板 */ }
  srEmit("finished", entry);
}

/* 在全量清单里消失的条目收尾：书还在就把它的书库摘要与详情重读（结果以那里为准）；不广播 finished（不知道
   它是怎么结束的，不能说「完成」） */
function srActivityVanished(entry) {
  const bookId = entry.book_id;
  if (!bookId || SR_DELETED_BOOKS.has(bookId)) return;
  srSyncBooks();
  srLoadBookDetail(bookId, { force: true });
  srLoadLearn(bookId, { force: true });
  SR_CACHE.delete(`estimate:${bookId}`);
}

/* 把服务端的活动条目并进活动表。
   complete：这是一次成功的 GET /activity（全量清单）——本地还「进行中」、登记已超过 SR_ACTIVITY_VANISH_GRACE_MS、
   清单里却没有的作业条目收尾拿掉。sentAt：这次请求发出的时刻——请求发出之后才登记（刚发起 / 续跑）的条目，
   旧快照里的终态不算数，也不按它判消失。 */
export function srActivityApply(items, { complete = false, sentAt = null } = {}) {
  const finished = [];
  const listed = new Set();
  let changed = false;
  for (const item of items || []) {
    if (!item || !item.key || !String(item.key).startsWith("job:")) continue;
    listed.add(item.key);
    // 删掉的书：作业行随书删了，删书之前发出的请求晚到时它们不能复活
    if (item.book_id && SR_DELETED_BOOKS.has(item.book_id)) continue;
    const local = SR_ACTIVITY.get(item.key) || null;
    const active = srActivityActive(item);
    if (!local && !active && SR_ACTIVITY_DISMISSED.has(item.key)) continue;
    if (local && srActivityActive(local) && !active && sentAt != null && (local.trackedAt || 0) > sentAt) continue;
    const startedAt = (local && local.startedAt) || (item.started_at ? Date.parse(item.started_at) : NaN) || Date.now();
    const next = {
      ...item,
      title: item.title || (local && local.title) || null,
      book_id: item.book_id || (local && local.book_id) || null,
      mode: item.mode || (local && local.mode) || null,
      startedAt,
      trackedAt: (local && local.trackedAt) || Date.now(),
      seenTerminal: local ? !!local.seenTerminal : !active,
    };
    SR_ACTIVITY.set(item.key, next);
    SR_ACTIVITY_VANISHED.delete(item.key);
    changed = true;
    if (local && srActivityActive(local) && !active) finished.push(next);
  }
  const vanished = [];
  if (complete) {
    const cutoff = (sentAt != null ? sentAt : Date.now()) - SR_ACTIVITY_VANISH_GRACE_MS;
    for (const [key, entry] of Array.from(SR_ACTIVITY.entries())) {
      if (!key.startsWith("job:") || listed.has(key) || !srActivityActive(entry)) continue;
      if ((entry.trackedAt || 0) > cutoff) continue;
      SR_ACTIVITY.delete(key);
      SR_ACTIVITY_VANISHED.add(key);
      vanished.push(entry);
      changed = true;
    }
  }
  if (changed) srEmit("activity");
  finished.forEach((entry) => { srActivityFinished(entry); });
  vanished.forEach((entry) => { srActivityVanished(entry); });
  return finished;
}

export function srActivityDismiss(key) {
  if (!SR_ACTIVITY.delete(key)) return;
  SR_ACTIVITY_DISMISSED.add(key);
  srEmit("activity");
}

export function srActivityClearFinished() {
  let changed = false;
  for (const [key, e] of Array.from(SR_ACTIVITY.entries())) {
    if (!srActivityActive(e)) { SR_ACTIVITY.delete(key); SR_ACTIVITY_DISMISSED.add(key); changed = true; }
  }
  if (changed) srEmit("activity");
}

function srActivityAnyActive() {
  for (const e of SR_ACTIVITY.values()) if (srActivityActive(e)) return true;
  return false;
}

let srActivityTimer = null;
let srActivityBusy = false;
let srActivityFailures = 0;

function srActivitySchedule(ms = SR_ACTIVITY_POLL_MS) {
  clearTimeout(srActivityTimer);
  srActivityTimer = setTimeout(srActivityTick, ms);
}

async function srActivityTick() {
  srActivityTimer = null;
  if (srActivityBusy) { srActivitySchedule(); return; }
  srActivityBusy = true;
  let ok = false;
  try {
    const sentAt = Date.now();
    const data = await apiGet(`${API}/activity`);
    ok = true;
    const items = data && Array.isArray(data.items) ? data.items : null;
    srActivityApply(items || [], { complete: !!items, sentAt });
  } catch (e) { /* 网络抖动 / 后端在重启：退避后再试 */ }
  finally { srActivityBusy = false; }
  if (ok) {
    const recovered = srActivityFailures > 0;
    srActivityFailures = 0;
    // 后端重启回来了：进页面时没读到的书库顺手补读（书库摘要里在跑的作业会补登进活动表）
    if (recovered && SR_BOOKS_STATE.phase === "error") srSyncBooks();
    if (srActivityAnyActive()) srActivitySchedule();
    return;
  }
  srActivityFailures += 1;
  // 读失败：还有在跑的条目，或页面挂着（第一次就没读到，说不定有在跑的作业）——退避后再试，不就此停下
  if (srActivityAnyActive() || SR_VIEW_MOUNTED) {
    srActivitySchedule(Math.min(SR_ACTIVITY_BACKOFF_MAX_MS, SR_ACTIVITY_POLL_MS * 2 ** Math.min(srActivityFailures, 4)));
  }
}

/* 页面挂载 / 发起作业时调用：立刻拉一次，有在跑的就持续轮询，空了自动停 */
export function srActivityStart() {
  if (srActivityTimer != null) return;
  srActivitySchedule(0);
}

/* 发起 / 取消了一项作业：马上拉一次活动清单（界面在别的 store 里取消了对照检查时也用） */
export function srActivityPoke() {
  clearTimeout(srActivityTimer);
  srActivityTimer = null;
  srActivitySchedule(0);
}

export function srActivityStop() {
  clearTimeout(srActivityTimer);
  srActivityTimer = null;
}

/* 单测用：清空模块级状态 */
export function srResetForTests() {
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
  SR_ACTIVITY.clear();
  SR_ACTIVITY_DISMISSED.clear();
  SR_ACTIVITY_VANISHED.clear();
  SR_SESSION_UI.clear();
  srActivityFailures = 0;
  srActivityStop();
  srCacheDrop("");
}
