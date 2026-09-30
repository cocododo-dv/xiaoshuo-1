import { apiGet } from "./lib/client.js";
import { createPoller } from "./lib/poll.js";
import { srActivityActive } from "./ws-styleref-model.js";
import {
  SR_API, srBookDeleted, srBooksState, srEmit, srLoadBookDetail, srLoadLearn, srLoadProfile, srOnBooksSynced,
  srSyncBooks, srViewMounted,
} from "./ws-styleref-store-core.js";

/* ==========================================================
   风格参考 · 参考书活动：作业表条目（段落分类 / 学习文风 / 对照检查）+ /activity 轮询
   （2026-09-30 从 ws-styleref-store.js 拆出，审计 F05-15）
   · 只收作业表条目（key 以 job: 开头），别的不收；
   · 条目到终态时按 kind 刷新对应缓存，再通知 finished 频道；
   · 作者关掉的终态条目记下来，别让服务端清单（10 分钟内结束的也会回来）每轮把它们复活；
   · 一次成功的 /activity 就是服务端的全量清单：本地还「进行中」、登记已超过几秒、清单里却没有了的条目
     （书删了，作业行随书删了；或别处清掉了）收尾拿掉——不然它永远「进行中」，轮询也永远停不下来；
   · 书库摘要里说在排队 / 在跑的分类、学习作业，活动表里没有就补登（第一次 /activity 失败、刷新了页面……），
     并开始轮询；/activity 读失败时退避重试（页面挂着、或还有在跑的条目时）。
   轮询走 lib/poll.js 的 createPoller：页面隐藏时放慢到 30 秒一次，回到前台立刻问一次。
   只 import lib/client.js、lib/poll.js、纯派生的 model 与 store 底座；不写 window。
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

/* 书库摘要里说在排队 / 在跑、活动表里却没有的分类 / 学习作业：补登一条，开始轮询（读完书库时由底座调用） */
function srActivitySeedFromBooks(books) {
  let seeded = false;
  const now = Date.now();
  for (const book of books) {
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
srOnBooksSynced(srActivitySeedFromBooks);

async function srActivityFinished(entry) {
  const bookId = entry.book_id;
  try {
    if (entry.kind === "classify" || entry.kind === "learn") await srSyncBooks();
    if (bookId) {
      srLoadBookDetail(bookId, { force: true });
      srLoadLearn(bookId, { force: true });
    }
    if (entry.kind === "learn") {
      const profileId = entry.profile_id || (entry.result && entry.result.profile_id);
      if (profileId) srLoadProfile(profileId, { force: true });
    }
  } catch (e) { /* 刷新失败不影响面板 */ }
  srEmit("finished", entry);
}

/* 在全量清单里消失的条目收尾：书还在就把它的书库摘要与详情重读（结果以那里为准）；不通知 finished（不知道
   它是怎么结束的，不能说「完成」） */
function srActivityVanished(entry) {
  const bookId = entry.book_id;
  if (!bookId || srBookDeleted(bookId)) return;
  srSyncBooks();
  srLoadBookDetail(bookId, { force: true });
  srLoadLearn(bookId, { force: true });
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
    if (item.book_id && srBookDeleted(item.book_id)) continue;
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

/* 删掉了一本书：它的活动条目全部拿掉——在跑的也是：作业行随书一起删了，服务端不会再列出它们，留着就是一条永远
   「进行中」的幽灵条目，轮询也因此永远停不下来（由删书的写操作调用，频道由它统一通知） */
export function srActivityForgetBook(bookId) {
  for (const e of Array.from(SR_ACTIVITY.values())) if (e.book_id === bookId) SR_ACTIVITY.delete(e.key);
}

function srActivityAnyActive() {
  for (const e of SR_ACTIVITY.values()) if (srActivityActive(e)) return true;
  return false;
}

/* ---------- 轮询 ----------
   有在跑的每 1.5 秒问一次，都结束了就停；读失败按 1.5 秒 × 2^n（封顶 15 秒）退避——只在还有在跑的条目、或页面
   挂着（第一次就没读到，说不定有在跑的作业）时接着问；后端回来了顺手补读进页面时没读到的书库。 */
let srActivityFailures = 0;

async function srActivityTick() {
  let ok = false;
  try {
    const sentAt = Date.now();
    const data = await apiGet(`${SR_API}/activity`);
    ok = true;
    const items = data && Array.isArray(data.items) ? data.items : null;
    srActivityApply(items || [], { complete: !!items, sentAt });
  } catch (e) { /* 网络抖动 / 后端在重启：退避后再试 */ }
  if (ok) {
    const recovered = srActivityFailures > 0;
    srActivityFailures = 0;
    // 后端重启回来了：进页面时没读到的书库顺手补读（书库摘要里在跑的作业会补登进活动表）
    if (recovered && srBooksState().phase === "error") srSyncBooks();
    return srActivityAnyActive();
  }
  srActivityFailures += 1;
  return srActivityAnyActive() || srViewMounted();
}

const srActivityPoller = createPoller({
  run: srActivityTick,
  interval: () => (srActivityFailures > 0
    ? Math.min(SR_ACTIVITY_BACKOFF_MAX_MS, SR_ACTIVITY_POLL_MS * 2 ** Math.min(srActivityFailures, 4))
    : SR_ACTIVITY_POLL_MS),
});

/* 页面挂载 / 发起作业时调用：立刻拉一次，有在跑的就持续轮询，空了自动停 */
export function srActivityStart() {
  if (srActivityPoller.active()) return;
  srActivityPoller.start(0);
}

/* 发起 / 取消了一项作业：马上拉一次活动清单（界面在别的 store 里取消了对照检查时也用） */
export function srActivityPoke() {
  srActivityPoller.start(0);
}

/* 单测用：清空活动表并停掉轮询（门面的 srResetForTests 调它） */
export function srResetActivityForTests() {
  SR_ACTIVITY.clear();
  SR_ACTIVITY_DISMISSED.clear();
  SR_ACTIVITY_VANISHED.clear();
  srActivityFailures = 0;
  srActivityPoller.stop();
}
