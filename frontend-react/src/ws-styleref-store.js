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
   · 参考书活动：作业表条目（段落分类 / 学习文风 / 对照检查）+ 一个 /activity 轮询；给旧前端的别名条目
     （compat_alias_of）不收 → sr:activity-changed；某条从进行中走到终态 → sr:activity-finished
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
   回来后第一次读必须重读。 */
const SR_FRESH = new Set();
export function srSetViewMounted(mounted) {
  if (mounted) SR_FRESH.clear();
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

export async function srSyncBooks() {
  let rows;
  try {
    rows = ((await apiGet(`${API}/books`)) || {}).books || [];
  } catch (e) {
    SR_BOOKS_STATE = { phase: "error", error: (e && e.message) || String(e), code: (e && e.code) || "" };
    srEmit("books");
    return SR_BOOKS;
  }
  SR_BOOKS = rows.map(srMapBook);
  SR_BOOKS_STATE = { phase: "ready", error: null, code: "" };
  srEmit("books");
  return SR_BOOKS;
}

/* ==========================================================
   导入对话框的默认值（GET /runtime）
   ========================================================== */
let SR_RUNTIME = { phase: "idle", data: null };
export function srRuntime() { return SR_RUNTIME; }

export async function srLoadRuntime({ force = false } = {}) {
  if (!force && SR_RUNTIME.phase === "ready") return SR_RUNTIME.data;
  SR_RUNTIME = { ...SR_RUNTIME, phase: "loading" };
  try {
    const data = await apiGet(`${API}/runtime`);
    SR_RUNTIME = { phase: "ready", data: data || null };
  } catch (e) {
    SR_RUNTIME = { phase: "error", data: null, error: (e && e.message) || String(e) };
  }
  srEmit("detail");
  return SR_RUNTIME.data;
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
  if (SR_INFLIGHT.has(key)) return SR_INFLIGHT.get(key);
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
   失败回滚。返回 { binding, created, changed, replaced }。 */
export async function srApplyProfile(profileId, { projectId, config = {} } = {}) {
  if (!profileId || !projectId) throw Object.assign(new Error("还没有打开作品"), { code: "SR_NO_WORK" });
  const key = `project:${projectId}`;
  const entry = SR_CACHE.get(key);
  const before = entry ? entry.data : null;
  const booksBefore = SR_BOOKS;
  const book = srBookOfProfile(profileId);
  const optimistic = {
    ...(before || { project_id: projectId }),
    binding: {
      binding_id: before && before.binding && before.binding.profile_id === profileId ? before.binding.binding_id : null,
      profile_id: profileId,
      scope: "project",
      scope_ref_id: projectId,
      status: "active",
      config: srMergeConfig(before && before.binding && before.binding.profile_id === profileId ? before.binding.config : null, config),
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
  SR_CACHE.delete(`bindings:${profileId}`);
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

/* 解除一条绑定。先把作品缓存与书库里的「在用」去掉，失败回滚。 */
export async function srUnbind(bindingId, { projectId = null, profileId = null } = {}) {
  const key = projectId ? `project:${projectId}` : null;
  const entry = key ? SR_CACHE.get(key) : null;
  const booksBefore = SR_BOOKS;
  if (entry && entry.data && entry.data.binding && entry.data.binding.binding_id === bindingId) {
    srCacheSet(key, { ...entry, data: { ...entry.data, binding: null } });
  }
  srPatchBooksApplied((b) => (
    (b.appliedProjects || []).some((item) => item.binding_id === bindingId)
      ? { ...b, appliedProjects: b.appliedProjects.filter((item) => item.binding_id !== bindingId) }
      : b
  ));
  try {
    const result = await apiDelete(`${API}/bindings/${encodeURIComponent(bindingId)}`);
    if (profileId) SR_CACHE.delete(`bindings:${profileId}`);
    if (projectId) srLoadProjectBinding(projectId, { force: true });
    srSyncBooks();
    return result;
  } catch (e) {
    if (key && entry) srCacheSet(key, entry);
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
  ids.filter((id) => !failed.has(id)).forEach((id) => {
    ["book:", "learn:", "estimate:"].forEach((prefix) => SR_CACHE.delete(`${prefix}${id}`));
    for (const e of Array.from(SR_ACTIVITY.values())) if (e.book_id === id && !srActivityActive(e)) SR_ACTIVITY.delete(e.key);
  });
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
export async function srStartLearn(bookId, { resume = false } = {}) {
  const data = await apiPost(`${API}/books/${encodeURIComponent(bookId)}/learn`, resume ? { resume: true } : {});
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
   · 给旧前端的别名条目（compat_alias_of）不收；
   · 条目到终态时按 kind 刷新对应缓存，再广播 sr:activity-finished；
   · 作者关掉的终态条目记下来，别让服务端清单（10 分钟内结束的也会回来）每轮把它们复活。
   ========================================================== */
const SR_ACTIVITY = new Map();
const SR_ACTIVITY_DISMISSED = new Set();
const SR_ACTIVITY_POLL_MS = 1500;

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

/* 发起作业后先登记一条本地条目（服务端快照到了再覆盖） */
export function srActivityTrack(jobId, { kind, mode = null, book_id = null, title = null } = {}) {
  if (!jobId) return;
  const key = `job:${jobId}`;
  SR_ACTIVITY_DISMISSED.delete(key);
  const current = SR_ACTIVITY.get(key);
  SR_ACTIVITY.set(key, {
    key, job_id: jobId, kind, mode, book_id, title, status: "queued", phase_label: "排队中",
    percent: 0, startedAt: Date.now(), ...(current || {}),
  });
  srEmit("activity");
  srActivityPoke();
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

export function srActivityApply(items) {
  const finished = [];
  let changed = false;
  for (const item of items || []) {
    if (!item || !item.key || item.compat_alias_of) continue;
    const local = SR_ACTIVITY.get(item.key) || null;
    const active = srActivityActive(item);
    if (!local && !active && SR_ACTIVITY_DISMISSED.has(item.key)) continue;
    const startedAt = (local && local.startedAt) || (item.started_at ? Date.parse(item.started_at) : NaN) || Date.now();
    const next = {
      ...item,
      title: item.title || (local && local.title) || null,
      book_id: item.book_id || (local && local.book_id) || null,
      mode: item.mode || (local && local.mode) || null,
      startedAt,
      seenTerminal: local ? !!local.seenTerminal : !active,
    };
    SR_ACTIVITY.set(item.key, next);
    changed = true;
    if (local && srActivityActive(local) && !active) finished.push(next);
  }
  if (changed) srEmit("activity");
  finished.forEach((entry) => { srActivityFinished(entry); });
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

function srActivitySchedule(ms = SR_ACTIVITY_POLL_MS) {
  clearTimeout(srActivityTimer);
  srActivityTimer = setTimeout(srActivityTick, ms);
}

async function srActivityTick() {
  srActivityTimer = null;
  if (srActivityBusy) { srActivitySchedule(); return; }
  srActivityBusy = true;
  try {
    const data = await apiGet(`${API}/activity`);
    srActivityApply(data && Array.isArray(data.items) ? data.items : []);
  } catch (e) { /* 网络抖动：下一轮再试 */ }
  finally { srActivityBusy = false; }
  if (srActivityAnyActive()) srActivitySchedule();
}

/* 页面挂载 / 发起作业时调用：立刻拉一次，有在跑的就持续轮询，空了自动停 */
export function srActivityStart() {
  if (srActivityTimer != null) return;
  srActivitySchedule(0);
}

function srActivityPoke() {
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
  SR_CACHE.clear();
  SR_INFLIGHT.clear();
  SR_FRESH.clear();
  SR_ACTIVITY.clear();
  SR_ACTIVITY_DISMISSED.clear();
  SR_SESSION_UI.clear();
  srActivityStop();
  srCacheDrop("");
}
