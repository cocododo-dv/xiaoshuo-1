import {
  apiDelete,
  apiGet,
  apiPost,
  buildUrl,
  getOperatorRef,
  getRemoteAccessToken,
} from "./lib/client.js";
import { SR_ACTIVITY_WHERE, SR_PARA_LABEL, srChooseProfile, srPickLatestRun, srSpineColor } from "./ws-styleref-model.js";

/* ==========================================================
   风格参考 · store（2026-09-21 从 ws-styleref.jsx 拆出）
   ----------------------------------------------------------
   模块级缓存 + window 事件广播，界面订阅事件重渲：
   · 书库 SR_BOOKS（+ 加载状态）与附加事实 SR_META（每本书的画像、哪本正用于当前作品）
     → sr:books-changed
   · 按书懒加载的深层数据 SR_DEEP（书详情 / 最新 run / findings / 画像 / 绑定 / 回测报告）
     → sr:deep-changed
   · 参考书活动表 SR_ACTIVITY（导入、分类、抽取、合成、建索引、回测、示例预览）+ 一个 /activity 轮询
     → sr:activity-changed；某条从进行中走到终态 → sr:activity-finished
   · 导入成功 → sr:book-imported（页面据此切到新书）
   所有风格参考的请求都在这里。只 import lib/client.js 与纯派生 ws-styleref-model.js，
   不 import 任何界面模块，不写 window（事件用 window.dispatchEvent 广播）。
   店外的三样东西——当前作品、提示、确认——由界面层在加载时经 srConfigureHost 接上
   （ws-styleref-ui.jsx）；没接上时提示退回 alert、确认一律当「取消」、当前作品当没有。
   ========================================================== */

/* ---------- 店外依赖 ---------- */
const SR_HOST = {
  activeWorkId: () => null,
  notify: (message) => { try { window.alert(message); } catch (e) { /* 无头环境 */ } },
  confirm: () => Promise.resolve(false),
};

export function srConfigureHost(host) {
  for (const key of Object.keys(SR_HOST)) {
    if (host && typeof host[key] === "function") SR_HOST[key] = host[key];
  }
}

/* ---------- 事件 ---------- */
const SR_EVENTS = {
  books: "sr:books-changed",
  deep: "sr:deep-changed",
  activity: "sr:activity-changed",
  finished: "sr:activity-finished",
  imported: "sr:book-imported",
};

function srEmit(channel, detail) {
  window.dispatchEvent(detail === undefined ? new CustomEvent(SR_EVENTS[channel]) : new CustomEvent(SR_EVENTS[channel], { detail }));
}

/* 订阅若干频道（books / deep / activity / finished / imported）：返回 (listener) => 退订函数，
   正好是 lib/store-utils 的 useStoreTick 要的形状。 */
export function srSubscribe(...channels) {
  return (listener) => {
    const names = channels.map((c) => SR_EVENTS[c] || c);
    names.forEach((n) => window.addEventListener(n, listener));
    return () => names.forEach((n) => window.removeEventListener(n, listener));
  };
}

/* ---------- 页面是否挂着 ----------
   页面每次挂载都清空「本次挂载里读过的书」SR_DEEP_FRESH：深层缓存是模块级的，作者离开页面期间
   可能在待办里批准了绑定、在别处跑完了回测，回来后第一次 srLoadDeep 必须重读，
   不能让页头说「当前作品在用」、步骤条却还是「注入应用 未开始」。 */
const SR_DEEP_FRESH = new Set();
export function srSetViewMounted(mounted) {
  if (mounted) SR_DEEP_FRESH.clear();
}

/* ---------- 本次打开应用期间停在哪本书、哪一步（不跨刷新，不写存储） ----------
   离开风格参考再回来接着看刚才那本；刷新或下次打开时按落点规则走（当前作品在用的书优先），
   见 ws-styleref-model.js 的 srPickLandingBook。键是作品 id（没有作品时用空串）。 */
const SR_SESSION_UI = new Map();
export function srSessionUi(workId) { return SR_SESSION_UI.get(workId || "") || null; }
export function srRememberSession(workId, entry) {
  if (!entry || !entry.bookId) return;
  SR_SESSION_UI.set(workId || "", { bookId: String(entry.bookId), stage: entry.stage || null });
}

/* ==========================================================
   书库（真相来自后端 style-reference v2；本地不内置任何样书）
   ========================================================== */
let SR_BOOKS = [];
/* 书库列表本身的加载状态：第一次加载前是 loading（左栏画骨架，不闪「书库还是空的」），
   读失败是 error（给「重试」，不装作书库是空的）。 */
let SR_BOOKS_STATE = { phase: "loading", error: null };
/* 书库的「附加事实」：每本书有哪些画像、哪几本正用于当前作品（左栏徽标、置顶、落点都用它）。
   两个请求都是尽力而为：失败时 profilesByBook / appliedBookIds 为 null，界面只少说一句，不猜。 */
let SR_META = { phase: "idle", workId: null, profilesByBook: null, appliedBookIds: null, appliedProfileIds: null };

export function srBooks() { return SR_BOOKS; }
export function srBooksState() { return SR_BOOKS_STATE; }
export function srMeta() { return SR_META; }
export function srBookById(bookId) { return bookId ? SR_BOOKS.find((b) => b.id === bookId) || null : null; }
function srBookTitle(bookId) {
  const book = srBookById(bookId);
  return book ? book.title : null;
}

/* 这本书是否正用于当前作品：true / false；还不知道（附加事实没读到）时 null */
export function srBookApplied(bookId) {
  return SR_META.appliedBookIds ? SR_META.appliedBookIds.has(bookId) : null;
}

/* 这本书的画像行（只有判断状态要用的字段）；还不知道时 null */
export function srProfilesOf(bookId) {
  return SR_META.profilesByBook ? (SR_META.profilesByBook.get(bookId) || []) : null;
}

/* 当前作品在用的是不是另一本书的画像：返回那本书的书名，不是（或不知道）返回 null。
   项目级只取最新的一份作底，批准后会换成这本。 */
export function srAppliedOtherBookTitle(bookId) {
  if (!SR_META.appliedProfileIds || !SR_META.profilesByBook) return null;
  for (const [bid, list] of SR_META.profilesByBook.entries()) {
    if (bid === bookId) continue;
    if ((list || []).some((p) => SR_META.appliedProfileIds.has(p.profile_id))) {
      const other = srBookById(bid);
      return other ? other.title : null;
    }
  }
  return null;
}

/* 作者看到的只有一句人话；错误代码（NETWORK_ERROR、STYLE_REFERENCE_*）是给排查用的，
   另存在 errorCode / code 里，界面只放进悬停提示（title），不再拼在句子后面。 */
function srActivityErrorText(e) {
  if (!e) return "未知错误";
  return e.message || String(e);
}
function srErrorCode(e) {
  return (e && e.code) || "";
}

/* 拉书库列表。第一次成功前左栏画骨架；失败如实记成 error（给「重试」）。
   列表到手后顺带（不等待）刷新附加事实 srSyncMeta。 */
export async function srSyncBooks() {
  let rows = [];
  try {
    rows = ((await apiGet("/api/v2/style-reference/books")) || {}).books || [];
  } catch (e) {
    SR_BOOKS_STATE = { phase: "error", error: srActivityErrorText(e), code: srErrorCode(e) };
    srEmit("books");
    return;
  }
  SR_BOOKS = rows.map((b) => ({
    id: b.book_id,
    title: b.title || "未命名参考书",
    author: b.author_label || "",
    chars: b.total_chars || 0,
    rawStatus: b.status,
    classification: b.classification || null,
    color: srSpineColor(b.book_id),
  }));
  SR_BOOKS_STATE = { phase: "ready", error: null, code: "" };
  srEmit("books");
  if (SR_BOOKS.length) srSyncMeta();
  else if (SR_META.phase !== "ready" || SR_META.profilesByBook) {
    SR_META = { phase: "ready", workId: null, profilesByBook: new Map(), appliedBookIds: new Set(), appliedProfileIds: new Set() };
  }
}

/* 书库的附加事实：一次 GET /profiles（每本书有哪些画像）+ 一次 GET /injection/layers?project_id=当前作品
   （哪份画像正作为这部作品的底层在用）。都是尽力而为：失败时对应字段为 null，界面不据此下结论。
   同时只跑一份；跑的时候作品换了，跑完再补一轮。 */
let SR_META_INFLIGHT = null;
let SR_META_AGAIN = false;
export function srSyncMeta() {
  if (SR_META_INFLIGHT) { SR_META_AGAIN = true; return SR_META_INFLIGHT; }
  const workId = SR_HOST.activeWorkId() || null;
  if (!(SR_META.phase === "ready" && SR_META.workId === workId)) SR_META = { ...SR_META, phase: "loading" };
  SR_META_INFLIGHT = (async () => {
    let profiles = null;
    try {
      profiles = ((await apiGet("/api/v2/style-reference/profiles")) || {}).profiles;
      if (!Array.isArray(profiles)) profiles = null;
    } catch (e) { profiles = null; }
    let appliedProfileIds = null;
    if (profiles && !profiles.length) appliedProfileIds = new Set();
    else if (profiles && workId) {
      try {
        const r = await apiGet(`/api/v2/style-reference/injection/layers?${new URLSearchParams({ project_id: workId, task_type: "scene_generation" }).toString()}`);
        appliedProfileIds = new Set(((r && r.layers) || [])
          .filter((l) => l && (l.scope === "project" || l.scope === "global"))
          .map((l) => l.profile_id));
      } catch (e) { appliedProfileIds = null; }
    }
    let profilesByBook = null;
    let appliedBookIds = null;
    if (profiles) {
      profilesByBook = new Map();
      for (const p of profiles) {
        if (!p || !p.book_id) continue;
        // 只留判断状态要用的字段：画像行里的 profile_json 可能有几十 KB
        const light = { profile_id: p.profile_id, book_id: p.book_id, run_id: p.run_id, title: p.title, status: p.status, coverage_json: { stale: !!(p.coverage_json && p.coverage_json.stale) } };
        if (!profilesByBook.has(p.book_id)) profilesByBook.set(p.book_id, []);
        profilesByBook.get(p.book_id).push(light);
      }
      if (appliedProfileIds) {
        appliedBookIds = new Set();
        for (const [bid, list] of profilesByBook.entries()) if (list.some((p) => appliedProfileIds.has(p.profile_id))) appliedBookIds.add(bid);
      }
    }
    const before = SR_META.workId === workId ? SR_META.appliedBookIds : null;
    SR_META = { phase: profiles ? "ready" : "error", workId, profilesByBook, appliedBookIds, appliedProfileIds };
    srEmit("books");
    // 「当前作品在用」翻转了的书（在待办里批准 / 在别处解绑）：它缓存里的绑定已经过时，强制重读，
    // 免得页头与步骤条、「当前应用」各说各的。
    if (before && appliedBookIds) {
      const flipped = new Set();
      for (const bid of before) if (!appliedBookIds.has(bid)) flipped.add(bid);
      for (const bid of appliedBookIds) if (!before.has(bid)) flipped.add(bid);
      flipped.forEach((bid) => { if (SR_DEEP[bid]) srLoadDeep(bid, { force: true }); });
    }
  })().finally(() => {
    SR_META_INFLIGHT = null;
    if (SR_META_AGAIN) { SR_META_AGAIN = false; srSyncMeta(); }
  });
  return SR_META_INFLIGHT;
}

/* ==========================================================
   参考书活动（2026-09-15，从「导入进度」扩成整个模块的活动表）
   所有会让人等的操作共用一张活动表 SR_ACTIVITY（key → 条目），「参考书活动」面板据此画进度条：
   - 导入：srRunImport 发请求前登记本地条目，POST 挂起期间每秒轮询
     GET …/imports/{key}/progress（404 = 服务端尚未登记或进程已换，不当失败，以 POST 结果为准）；
   - 抽取 / 合成画像 / 重新分类 / 应用画像建索引 / 回测：由 GET …/activity 一个轮询喂
     （服务端把进程内登记簿与库里的 durable 行合成一份），页面刷新后重新挂载时也能续接；
   - 示例预览：前端按段型逐张请求，本地条目记 n/3。
   条目到达终态时按 kind 刷新对应缓存（深层数据 / 书库）；完成 / 失败都只在面板里显示，不弹 alert。
   owned=true 的条目由发起它的调用方掌握终态（POST 结果为准），服务端快照只补阶段 / 百分比。
   ========================================================== */
const SR_ACTIVITY = new Map();
const SR_IMPORT_POLL_MS = 1000;
const SR_ACTIVITY_POLL_MS = 1500;
const SR_ACTIVITY_REFRESH_DEEP = new Set(["import", "extract", "synthesize", "reclassify", "rag_index", "validate"]);
/* 作者关掉的条目：服务端清单还会把「10 分钟内结束的」带回来，记住这些键，别让它们每轮轮询都复活。 */
const SR_ACTIVITY_DISMISSED = new Set();

export function srActivityEntries() {
  return Array.from(SR_ACTIVITY.values()).sort((a, b) => b.startedAt - a.startedAt);
}

function srActivityPut(key, patch) {
  const current = SR_ACTIVITY.get(key) || { key, kind: "import", status: "running", startedAt: Date.now() };
  const next = { ...current, ...patch, updatedAt: Date.now() };
  SR_ACTIVITY.set(key, next);
  return next;
}

export function srActivitySet(key, patch) {
  const next = srActivityPut(key, patch);
  srEmit("activity");
  return next;
}

export function srActivityDismiss(key) {
  if (!SR_ACTIVITY.delete(key)) return;
  SR_ACTIVITY_DISMISSED.add(key);
  srEmit("activity");
}

/* 「清除已完成」：一次关掉所有结束了的条目 */
export function srActivityClearFinished() {
  let changed = false;
  for (const [key, e] of SR_ACTIVITY.entries()) {
    if (e.status !== "running") { SR_ACTIVITY.delete(key); SR_ACTIVITY_DISMISSED.add(key); changed = true; }
  }
  if (changed) srEmit("activity");
}

/* 某本书正在跑的某类操作（头部按钮禁用、书库徽标、总览「进行中」块据此判断） */
export function srActivityFor(bookId, kind = null) {
  if (!bookId) return null;
  for (const e of SR_ACTIVITY.values()) {
    if (e.status === "running" && e.bookId === bookId && (!kind || e.kind === kind)) return e;
  }
  return null;
}

function srActivityAnyRunning() {
  for (const e of SR_ACTIVITY.values()) if (e.status === "running") return true;
  return false;
}

function srActivityKey(prefix) {
  return `${prefix}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

/* 终态副作用：按 kind 刷新对应缓存，再广播 sr:activity-finished（页面据此播报）。 */
async function srActivityFinished(entry) {
  try {
    if (SR_ACTIVITY_REFRESH_DEEP.has(entry.kind) && entry.bookId) await srLoadDeep(entry.bookId, { force: true });
    if (entry.kind === "extract" || entry.kind === "import") await srSyncBooks();
  } catch (e) { /* 刷新失败不影响面板本身 */ }
  if (entry.kind === "synthesize" || entry.kind === "rag_index" || entry.kind === "reclassify") srSyncMeta();
  srEmit("finished", entry);
}

/* 把服务端活动清单合进本地表：新条目直接收下；已有条目更新快照；从 running 到终态的那一次
   触发 srActivityFinished。owned 条目（本页发起并 await 着 POST 的导入 / 合成 / 重新分类）只在
   本地仍 running 时补服务端阶段，状态由 POST 结果决定。一轮快照只广播一次 sr:activity-changed。 */
export function srActivityApply(items) {
  const finished = [];
  let changed = false;
  for (const item of items || []) {
    if (!item || !item.key) continue;
    const local = SR_ACTIVITY.get(item.key) || null;
    const status = item.status || "running";
    if (!local && status !== "running" && SR_ACTIVITY_DISMISSED.has(item.key)) continue;
    if (local && local.owned) {
      if (local.status === "running") { srActivityPut(item.key, { server: item, phase: item.phase, percent: item.percent }); changed = true; }
      continue;
    }
    const prevStatus = local ? local.status : null;
    const startedAt = (local && local.startedAt) || (item.started_at ? Date.parse(item.started_at) : NaN) || Date.now();
    const next = srActivityPut(item.key, {
      kind: item.kind || (local && local.kind) || "import",
      title: item.title || (local && local.title) || null,
      bookId: item.book_id || (local && local.bookId) || null,
      targetId: item.target_id || (local && local.targetId) || null,
      status,
      phase: item.phase,
      percent: item.percent,
      server: item,
      error: item.error ? (item.error.message || "服务端没有给出原因") : ((local && local.error) || null),
      errorCode: item.error ? (item.error.code || "") : ((local && local.errorCode) || ""),
      startedAt,
      local: false,
      // 第一次看见就已结束（刷新页面后才看到的旧条目）：面板里折叠起来，不和眼前的操作挤在一起
      seenTerminal: local ? !!local.seenTerminal : status !== "running",
    });
    changed = true;
    if (prevStatus === "running" && status !== "running") finished.push(next);
  }
  if (changed) srEmit("activity");
  finished.forEach((entry) => { srActivityFinished(entry); });
  return finished;
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
    const data = await apiGet("/api/v2/style-reference/activity");
    srActivityApply(data && Array.isArray(data.items) ? data.items : []);
  } catch (e) { /* 网络抖动下一轮再试 */ }
  finally { srActivityBusy = false; }
  if (srActivityAnyRunning()) srActivitySchedule();
}

/* 有操作开始 / 页面挂载时调用：立刻拉一次活动清单，有在跑的就持续轮询，空了自动停。 */
export function srActivityStart() {
  if (srActivityTimer != null) return;
  srActivitySchedule(0);
}

export function srActivityStop() {
  clearTimeout(srActivityTimer);
  srActivityTimer = null;
}

/* ==========================================================
   导入（multipart 直接走 fetch：lib/client 的 apiPost 只发 JSON）
   ========================================================== */

/* 登记本地进度 → 轮询服务端进度 → POST → 终态 + 刷新书库。
   成功时广播 sr:book-imported（页面据此切到新书），失败时把原因写进面板并向调用方抛出。 */
export async function srRunImport({ file, title, authorLabel = null, cloudPolicy, rightsDeclaration = null, importKey = srActivityKey("sr-import"), pollMs = SR_IMPORT_POLL_MS }) {
  srActivitySet(importKey, { kind: "import", title, status: "running", phase: "upload", percent: 0, server: null, error: null, bookId: null, chars: null, paragraphs: null, owned: true, local: true });
  let stopped = false;
  let pollTimer = null;
  const pollTick = async () => {
    if (stopped) return;
    try {
      const data = await apiGet(`/api/v2/style-reference/imports/${encodeURIComponent(importKey)}/progress`);
      const server = data && data.progress;
      if (server && !stopped) srActivitySet(importKey, { server, phase: server.phase });
    } catch (e) { /* 404 = 尚未登记 / 进程已换；网络抖动下一轮再试 */ }
    if (!stopped) pollTimer = setTimeout(pollTick, pollMs);
  };
  pollTimer = setTimeout(pollTick, pollMs);
  const stop = () => { stopped = true; clearTimeout(pollTimer); };
  try {
    const fd = new FormData();
    fd.append("file", file, file.name);
    fd.append("title", title);
    // 作者可不填：不填就不带这个字段（后端记空，书库不显示「未署名」）
    if (authorLabel && String(authorLabel).trim()) fd.append("author_label", String(authorLabel).trim());
    // 策略必须来自作者在导入前的显式选择；默认 local_only，绝不静默放宽出域范围。
    fd.append("cloud_policy", cloudPolicy);
    // 后端以 JSON 串的 Form 字段接收声明（api/routes/style_reference.py import_book_upload）。
    if (rightsDeclaration) fd.append("rights_declaration", JSON.stringify(rightsDeclaration));
    const headers = { "X-Idempotency-Key": importKey, "X-Operator-Ref": getOperatorRef() };
    const accessToken = getRemoteAccessToken();
    if (accessToken) headers["X-Novel-Access-Token"] = accessToken;
    const res = await fetch(buildUrl("/api/v2/style-reference/books/import-upload"), { method: "POST", headers, body: fd });
    const body = await res.json();
    if (!body.ok) {
      // 原样透出后端信封里的 message（含 STYLE_REFERENCE_SEND_RIGHTS_* 的引导语）；code 挂在错误对象上
      // 便于对照日志（界面放进悬停提示），不再拼进作者读的那句话里。
      const err = (body && body.error) || {};
      const message = err.message || `导入失败（HTTP ${res.status}）`;
      throw Object.assign(new Error(message), { code: err.code || "", details: err.details || null });
    }
    stop();
    const data = body.data || {};
    const book = data.book || {};
    if (book.status === "ingesting") {
      // 2026-09-15 严格 LLM：请求只做了准备工作，整本 LLM 分类在后台任务里逐批跑（几分钟到几小时）；
      // 条目交给活动清单继续跟（同一个键），完成时 srActivityFinished 刷新书库与深层数据。
      srActivitySet(importKey, { status: "running", phase: "classify", bookId: book.book_id || null, chars: book.total_chars || null, paragraphs: data.paragraphs_count ?? null, owned: false, local: false });
      await srSyncBooks();
      srEmit("imported", { bookId: book.book_id || null, importKey });
      srActivityStart();
      return book;
    }
    srActivitySet(importKey, { status: "succeeded", phase: "done", percent: 100, bookId: book.book_id || null, chars: book.total_chars || null, paragraphs: data.paragraphs_count ?? null });
    await srSyncBooks();
    srEmit("imported", { bookId: book.book_id || null, importKey });
    return book;
  } catch (e) {
    stop();
    srActivitySet(importKey, { status: "failed", error: srActivityErrorText(e), errorCode: srErrorCode(e) });
    throw e;
  }
}

/* ==========================================================
   整本书的动作：抽取 / 重新分类 / 取消 / 继续分类 / 删除
   ========================================================== */

/* 重跑抽取 / 重新分类（重新分类前的确认在页面里，这里只管发请求）。模型未接入时给明确引导。
   两者的进度都在「参考书活动」面板：抽取是后台 run（服务端按子维推进，可取消），
   重新分类是后台分类任务（按幂等键登记阶段）；完成不弹窗。 */
export async function srBookAction(action, bookId, opts = {}) {
  const notify = SR_HOST.notify;
  try {
    if (action === "rerun") {
      const res = await apiPost(`/api/v2/style-reference/books/${bookId}/runs`, { background: true, force: !!opts.force });
      const runId = res && res.run_id;
      if (runId) srPollRun(runId, bookId);
    } else if (action === "reclassify") {
      const key = srActivityKey("sr-reclassify");
      srActivitySet(key, { kind: "reclassify", title: srBookTitle(bookId), bookId, status: "running", phase: "start", percent: 0, server: null, error: null, owned: true, local: true });
      srActivityStart();
      try {
        await apiPost(`/api/v2/style-reference/books/${bookId}/reclassify`, {}, { idempotencyKey: key });
      } catch (e) {
        srActivitySet(key, { status: "failed", error: srActivityErrorText(e), errorCode: srErrorCode(e) });
        throw e;
      }
      // 2026-09-15 严格 LLM：请求只清了派生数据并把书置 ingesting，整本 LLM 分类在后台任务里逐批跑；
      // 条目交给活动清单继续跟（同一个键），完成时 srActivityFinished 重读深层数据。
      srActivitySet(key, { phase: "classify", owned: false, local: false });
      srActivityStart();
      // 派生数据已清空 → 概览 / 矩阵 / 画像页立即重读（矩阵回到空态）
      await srLoadDeep(bookId, { force: true });
    }
  } catch (e) {
    if (e && e.code === "STYLE_REFERENCE_INPUT_TOO_SMALL" && !opts.force) {
      // §6.4 输入量门槛：全部分析层被评估为 skip。给一键强制重试（明知样本少仍要抽）
      const goOn = await SR_HOST.confirm({
        title: "这本书字数太少，仍要抽取吗？",
        body: "按语料门槛，所有分析层都被评估为「跳过」，正常情况下抽取不会执行。建议补足语料后重新导入；强制抽取也可以，但样本太少时画像不可信。",
        confirmLabel: "仍然抽取",
        tone: "danger",
      });
      if (goOn) return srBookAction("rerun", bookId, { force: true });
    } else if (e && e.code === "STYLE_REFERENCE_RUN_ALREADY_ACTIVE") {
      notify(`这本书已经在抽取了：等它完成，或先在${SR_ACTIVITY_WHERE}里取消。`, "warn");
      srActivityStart();
    } else if (e && e.code === "STYLE_REFERENCE_CLASSIFICATION_ALREADY_ACTIVE") {
      notify(`这本书正在分类：等它完成，或先在${SR_ACTIVITY_WHERE}里取消。`, "warn");
      srActivityStart();
    } else if (e && e.code === "STYLE_REFERENCE_BOOK_NOT_READY") {
      notify("这本书的段落分类还没完成：等它完成，或在概览里「继续分类」后再抽取。", "warn");
      srActivityStart();
    } else if (e && e.code === "STYLE_REFERENCE_CLOUD_POLICY_BLOCKED") {
      notify("这本书是「仅保存在本机」：需要本地模型（如 Ollama）才能处理，或用送云策略重新导入。", "warn");
    } else if (e && (e.code === "STYLE_REFERENCE_LLM_REQUIRED" || /llm/i.test(e.code || ""))) {
      notify("风格抽取需要先接入模型：到「设置 → 模型与接入」配置并开启后重试。", "warn");
    } else if (e && e.code === "REQUEST_TIMEOUT") {
      notify(`页面等不及了，但服务端还在处理；进度继续在${SR_ACTIVITY_WHERE}里显示。`, "info");
    } else {
      notify("操作失败：" + ((e && e.message) || e));
    }
  }
  await srSyncBooks();
}

/* 后台抽取：登记一条 extract 活动条目（key = run:<run_id>）并启动活动清单轮询。
   服务端按子维推进进度、30 s 心跳、可取消；条目到终态时 srActivityFinished 强制重载该书的
   深层数据并刷新书库。刷新页面后重新挂载时，在跑的 run 仍会从 GET …/activity 回来，不需要本地登记。 */
export function srPollRun(runId, bookId = null) {
  if (!runId) return;
  srActivitySet(`run:${runId}`, { kind: "extract", title: srBookTitle(bookId), bookId, targetId: runId, status: "running", phase: "start", percent: 0, server: null, error: null, local: true });
  srActivityStart();
}

/* 取消后台抽取：POST cancel，随后立刻拉一次活动清单让条目进入「已取消」。 */
export async function srCancelRun(runId) {
  await apiPost(`/api/v2/style-reference/runs/${runId}/cancel`, {});
  srActivityStart();
  srActivitySchedule(0);
  return true;
}

/* 取消后台分类（导入 / 重新分类）：worker 在下一批边界退出，书标未完成，可续跑或删书。 */
export async function srCancelClassification(bookId) {
  await apiPost(`/api/v2/style-reference/books/${bookId}/classification/cancel`, {});
  srActivityStart();
  srActivitySchedule(0);
  return true;
}

/* 继续分类：从上次的游标续跑（失败 / 取消 / 中断后），登记一条新的 reclassify 条目。 */
export async function srResumeClassification(bookId) {
  const key = srActivityKey("sr-reclassify");
  srActivitySet(key, { kind: "reclassify", title: srBookTitle(bookId), bookId, status: "running", phase: "start", percent: 0, server: null, error: null, owned: true, local: true });
  srActivityStart();
  try {
    await apiPost(`/api/v2/style-reference/books/${bookId}/reclassify`, { resume: true }, { idempotencyKey: key });
  } catch (e) {
    srActivitySet(key, { status: "failed", error: srActivityErrorText(e), errorCode: srErrorCode(e) });
    throw e;
  }
  srActivitySet(key, { phase: "classify", owned: false, local: false });
  srActivityStart();
  await srSyncBooks();
  return true;
}

export async function srDeleteBook(bookId) {
  const headers = {
    // book_id 由内容 checksum 决定（同内容重导=同 id），删除键必须带熵，
    // 否则幂等层会重放上一次的成功响应而不真正执行
    "X-Idempotency-Key": `sr-del-${bookId}-${Date.now().toString(36)}`,
    "X-Operator-Ref": getOperatorRef(),
  };
  const accessToken = getRemoteAccessToken();
  if (accessToken) headers["X-Novel-Access-Token"] = accessToken;
  const res = await fetch(buildUrl(`/api/v2/style-reference/books/${bookId}`), { method: "DELETE", headers });
  const body = await res.json();
  if (!body.ok) throw new Error((body.error && body.error.message) || "删除失败");
  // 删书级联清掉全部衍生数据：本地深层缓存必须同步失效，否则同内容重导（同 book_id）会读到旧画像
  srDropDeep(bookId);
  await srSyncBooks();
  return true;
}

/* ==========================================================
   深层数据（按书懒加载：书详情 / 最新 run / findings / 画像 / 绑定 / 回测报告）
   没有画像的书，画像 / 回测 / 应用页显示空态。内存缓存 + 懒加载 + 防重 + 事件广播。
   ========================================================== */
const SR_DEEP = {};            // bookId -> { book, run, runId, findingsByDim, dimCounts, profileId, profile, bindings, reports, lastReport, loaded, error }
const SR_DEEP_FETCHING = {};

export function srDeepFor(bookId) { return SR_DEEP[bookId] || null; }

/* 这本书的深层数据是不是本次挂载里读的（落点只按新读的数据定，免得按离开前的缓存落错步） */
export function srDeepFresh(bookId) { return !!(bookId && SR_DEEP[bookId] && SR_DEEP_FRESH.has(bookId)); }

/* 失效并广播：删书 / 书不存在时调用，订阅方回到 null 状态 */
export function srDropDeep(bookId) {
  if (!bookId) return;
  delete SR_DEEP[bookId];
  delete SR_DEEP_FETCHING[bookId];
  SR_DEEP_FRESH.delete(bookId);
  srEmit("deep");
}

/* 回测报告里最近的一份有结论的（回测页没在本次会话里跑过时显示「上次回测」）；按结束时间取最新 */
function srLatestVerdictReport(reports) {
  const done = (reports || []).filter((r) => r && r.verdict);
  if (!done.length) return null;
  const ts = (r) => String(r.finished_at || r.created_at || "");
  return done.reduce((best, r) => (ts(r) >= ts(best) ? r : best), done[0]);
}

export async function srLoadDeep(bookId, { force = false } = {}) {
  if (!bookId) return null;
  if (!force && SR_DEEP[bookId] && SR_DEEP_FRESH.has(bookId)) return SR_DEEP[bookId];
  if (SR_DEEP_FETCHING[bookId]) return SR_DEEP_FETCHING[bookId];
  SR_DEEP_FETCHING[bookId] = (async () => {
    const out = {
      book: null, runId: null, run: null,
      findingsByDim: {}, dimCounts: {},
      profileId: null, profile: null, bindings: [], reports: [], lastReport: null,
      loaded: true, error: null,
    };
    try {
      // 1. 书详情（stats_json：metrics / input_assessment / 段型分布 / 分类器校准）
      try {
        const r = await apiGet(`/api/v2/style-reference/books/${encodeURIComponent(bookId)}`);
        out.book = (r && r.book) || null;
      } catch (e) { /* 详情失败不致命 */ }
      // 2. 最新 run（优先 done，否则最新一条）
      try {
        const rr = await apiGet(`/api/v2/style-reference/books/${encodeURIComponent(bookId)}/runs`);
        out.run = srPickLatestRun((rr && rr.runs) || []);
        out.runId = out.run ? out.run.run_id : null;
      } catch (e) { /* 读不到 run 列表：矩阵显示「未抽取」空态 */ }
      // 3. 该 run 的 findings（含证据）→ 按 sub_dim 分组 + 计数
      if (out.runId) {
        try {
          const fr = await apiGet(`/api/v2/style-reference/runs/${out.runId}/findings?include=evidence`);
          for (const f of (fr && fr.findings) || []) {
            const dim = f.sub_dimension;
            if (!out.findingsByDim[dim]) out.findingsByDim[dim] = { observations: [], forbidden_patterns: [] };
            (f.finding_kind === "forbidden_pattern" ? out.findingsByDim[dim].forbidden_patterns : out.findingsByDim[dim].observations).push(f);
          }
          for (const [dim, g] of Object.entries(out.findingsByDim)) {
            const confs = g.observations.map((o) => o.confidence);
            const conf = confs.includes("high") ? "high" : confs.includes("medium") ? "medium" : "low";
            // 格子颜色取最高一档；各档条数给提示用（只看最高档的话几乎每格都是「高」）
            const mix = { high: 0, medium: 0, low: 0 };
            confs.forEach((c) => { if (mix[c] != null) mix[c] += 1; });
            const q = [...g.observations, ...g.forbidden_patterns].reduce((s, x) => s + ((x.evidence || []).length), 0);
            out.dimCounts[dim] = { obs: g.observations.length, fp: g.forbidden_patterns.length, q, conf, mix };
          }
        } catch (e) { /* 读不到 findings：矩阵显示空态 */ }
      }
      // 4. 画像 + 绑定
      try {
        const pr = await apiGet(`/api/v2/style-reference/profiles?book_id=${encodeURIComponent(bookId)}`);
        const chosen = srChooseProfile((pr && pr.profiles) || []);
        out.profileId = chosen ? chosen.profile_id : null;
        out.profile = chosen;
        if (chosen) {
          try {
            const b = await apiGet(`/api/v2/style-reference/profiles/${chosen.profile_id}/bindings`);
            out.bindings = (b && b.bindings) || [];
          } catch (e) { /* 绑定拉取失败不致命 */ }
          // 5. 回测报告（步骤条「回测校验」是否做过）：列表只留状态字段，外加最近一份有结论的整份报告
          //    （回测页据此显示「上次回测」，不和步骤条的「已完成」各说各的）
          try {
            const rr = await apiGet(`/api/v2/style-reference/profiles/${chosen.profile_id}/reports`);
            const list = (rr && rr.reports) || [];
            out.reports = list.map((r) => ({ report_id: r.report_id, verdict: r.verdict || null, status: r.status || null, finished_at: r.finished_at || null }));
            out.lastReport = srLatestVerdictReport(list);
          } catch (e) { /* 报告拉取失败不致命 */ }
        }
      } catch (e) { /* 读不到画像：画像 / 应用页显示空态 */ }
    } catch (e) {
      out.error = (e && e.message) || String(e);
    } finally {
      SR_DEEP[bookId] = out;
      SR_DEEP_FRESH.add(bookId);
      delete SR_DEEP_FETCHING[bookId];
      srEmit("deep");
    }
    return SR_DEEP[bookId];
  })();
  return SR_DEEP_FETCHING[bookId];
}

/* ==========================================================
   观察、画像与绑定
   ========================================================== */

/* 合成画像：POST synthesize（需模型，同步请求几分钟）。按幂等键登记一条 synthesize 活动条目，
   服务端用同一个键汇报阶段，POST 返回后强制重载。模型未启用时抛 ApiRequestError(409)；
   同书已有一份在合成时后端 409 STYLE_REFERENCE_SYNTHESIS_ALREADY_ACTIVE。 */
export async function srSynthesize(runId, bookId) {
  const key = srActivityKey("sr-synth");
  srActivitySet(key, { kind: "synthesize", title: srBookTitle(bookId), bookId, targetId: runId, status: "running", phase: "start", percent: 0, server: null, error: null, owned: true, local: true });
  srActivityStart();
  let r;
  try {
    r = await apiPost(`/api/v2/style-reference/runs/${runId}/synthesize`, {}, { idempotencyKey: key });
  } catch (e) {
    const timedOut = !!(e && e.code === "REQUEST_TIMEOUT");
    // 前端等待上限到了但服务端仍在合成：条目交还给活动清单继续跟；其余失败原样写进面板。
    srActivitySet(key, { status: timedOut ? "running" : "failed", error: srActivityErrorText(e), errorCode: srErrorCode(e), owned: !timedOut });
    throw e;
  }
  srActivitySet(key, { status: "succeeded", percent: 100 });
  await srLoadDeep(bookId, { force: true });
  srSyncMeta();
  return r;
}

/* 观察审核（approved / rejected / pending）后强制重载。 */
export async function srReviewFinding(findingId, decision, bookId) {
  await apiPost(`/api/v2/style-reference/findings/${findingId}/review`, { decision });
  await srLoadDeep(bookId, { force: true });
  return true;
}

/* 观察的「准 / 不准」反馈：聚合后按阈值调档 confidence，强制重载使深层数据体现。 */
export async function srFindingFeedback(findingId, vote, bookId) {
  await apiPost(`/api/v2/style-reference/findings/${findingId}/user-feedback`, { vote });
  await srLoadDeep(bookId, { force: true });
  return true;
}

/* 画像预览：按段型逐张生成示例 + 自跑回测（需模型）。三次串行请求各带 paragraph_types，
   每张回来就通过 onSample 交给页面渲染，本地活动条目记 n/3；返回值形状与旧的一次性接口相同。 */
const SR_PREVIEW_TYPES = ["dialogue", "description_env", "psychology"];
export async function srPreviewSamples(profileId, { onSample = null, types = SR_PREVIEW_TYPES, title = null } = {}) {
  const key = srActivityKey("sr-preview");
  const label = (t) => SR_PARA_LABEL[t] || t;
  const snapshot = (i, phaseLabel) => ({ phase_label: phaseLabel, steps: { done: i, total: types.length, label: "段" }, percent: Math.round(99 * i / types.length) });
  srActivitySet(key, { kind: "preview", title, bookId: null, status: "running", phase: "generate", owned: true, local: true, server: snapshot(0, `生成 ${label(types[0])}`) });
  const samples = [];
  try {
    for (let i = 0; i < types.length; i += 1) {
      const t = types[i];
      srActivitySet(key, { server: snapshot(i, `生成 ${label(t)}`) });
      const r = await apiPost(`/api/v2/style-reference/profiles/${profileId}/preview`, { paragraph_types: [t] });
      samples.push(...(((r && r.samples) || [])));
      if (onSample) onSample(samples.slice());
    }
    srActivitySet(key, { status: "succeeded", percent: 100, server: snapshot(types.length, "完成") });
    return { profile_id: profileId, samples };
  } catch (e) {
    srActivitySet(key, { status: "failed", error: srActivityErrorText(e), errorCode: srErrorCode(e) });
    throw e;
  }
}

/* dryrun 注入预览（不写盘）：返回真实 fragments + prefix + stats。失败抛 ApiRequestError。 */
export async function srInjectionPreview(profileId, body) {
  return apiPost(`/api/v2/style-reference/profiles/${profileId}/injection-preview`, body);
}

/* 解除应用：DELETE binding 后强制重载该书的深层数据。 */
export async function srUnbind(bindingId, bookId) {
  await apiDelete(`/api/v2/style-reference/bindings/${bindingId}`);
  await srLoadDeep(bookId, { force: true });
  srSyncMeta(); // 「当前作品在用哪本」可能变了
  return true;
}

/* 注入应用页的几份只读数据：当前作品的章 / 场与角色（选场景、角色作用域用），
   画像的禁用词，任务默认表，当前作品实际叠加的注入层。读失败返回 null，由页面决定怎么说。 */
export async function srLoadProjectTargets(projectId) {
  const [catalog, library] = await Promise.all([
    apiGet(`/api/v2/projects/${projectId}/catalog`).catch(() => null),
    apiGet(`/api/v2/projects/${projectId}/library`).catch(() => null),
  ]);
  return {
    chapters: (catalog && catalog.chapters) || [],
    characters: (library && library.characters) || [],
  };
}

export async function srLoadBannedTerms(profileId) {
  const r = await apiGet(`/api/v2/style-reference/profiles/${profileId}/banned-terms`);
  return (r && r.terms) || [];
}

export async function srAddBannedTerm(profileId, term, scope) {
  return apiPost(`/api/v2/style-reference/profiles/${profileId}/banned-terms`, { term, scope });
}

export async function srRemoveBannedTerm(termId) {
  return apiDelete(`/api/v2/style-reference/banned-terms/${termId}`);
}

export async function srLoadTaskDefaults() {
  const r = await apiGet("/api/v2/style-reference/injection/task-defaults");
  return (r && r.tasks) || null;
}

export async function srLoadInjectionLayers({ projectId, taskType, sceneId = null, characterIds = [] }) {
  const qs = new URLSearchParams({ project_id: projectId, task_type: taskType });
  if (sceneId) qs.set("scene_id", sceneId);
  if (characterIds.length) qs.set("character_ids", characterIds.join(","));
  return apiGet(`/api/v2/style-reference/injection/layers?${qs.toString()}`);
}

/* ==========================================================
   回测
   ========================================================== */

/* 发起回测：快速（sync_only）直接带回 sync_result；完整（async_full）返回 report_id，由页面轮询。 */
export async function srValidate(profileId, { text, mode }) {
  return apiPost(`/api/v2/style-reference/profiles/${profileId}/validate`, { generated_text: text, target_kind: "manual", mode });
}

export async function srLoadReport(reportId) {
  return ((await apiGet(`/api/v2/style-reference/reports/${reportId}`)) || {}).report || null;
}
