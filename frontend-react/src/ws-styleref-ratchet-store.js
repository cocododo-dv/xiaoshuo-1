import { apiGet, apiPost, apiPut } from "./lib/client.js";
import { SR_API, srBookTitle, srEmit } from "./ws-styleref-store-core.js";
import { srActivityPoke, srActivityTrack } from "./ws-styleref-store-activity.js";

/* ==========================================================
   风格参考 ·「配置对比」的请求与缓存（频道 ratchet）
   ----------------------------------------------------------
   · 总览按画像缓存（能不能比、现在的配置、可换的模型、噪声记录、在跑的、历史），开跑 / 取消 / 一次比较结束后重读；
   · 一次比较的详情按作业 id 缓存（盲读三份文字、选择；揭晓之后加答案与数字）；
   · 盲读选择「先改界面、再存」：存失败回滚并把错误抛给调用方；揭晓（reveal）存完重读详情与总览；
   · 预估只读、不缓存（表单一变就重算）。
   依赖只朝一个方向（→ core / activity），不写 window。
   ========================================================== */

const OVERVIEW = new Map();
const RUNS = new Map();
const INFLIGHT = new Map();

function profilePath(profileId) {
  return `${SR_API}/profiles/${encodeURIComponent(profileId)}/ratchet`;
}

function runPath(jobId) {
  return `${SR_API}/ratchet-runs/${encodeURIComponent(jobId)}`;
}

async function load(cache, key, path, { force = false } = {}) {
  const current = cache.get(key);
  if (!force && current && current.phase === "ready") return current.data;
  const flightKey = `${path}`;
  if (INFLIGHT.has(flightKey)) return INFLIGHT.get(flightKey);
  if (!current || current.phase !== "ready") cache.set(key, { phase: "loading", data: current ? current.data : null, error: null });
  srEmit("ratchet");
  const promise = apiGet(path)
    .then((data) => {
      cache.set(key, { phase: "ready", data, error: null });
      return data;
    })
    .catch((error) => {
      const prev = cache.get(key);
      cache.set(key, { phase: "error", data: prev ? prev.data : null, error });
      return null;
    })
    .finally(() => {
      INFLIGHT.delete(flightKey);
      srEmit("ratchet");
    });
  INFLIGHT.set(flightKey, promise);
  return promise;
}

export function srRatchetOverview(profileId) {
  return (profileId && OVERVIEW.get(profileId)) || null;
}

export function srLoadRatchetOverview(profileId, options) {
  if (!profileId) return Promise.resolve(null);
  return load(OVERVIEW, profileId, profilePath(profileId), options);
}

export function srRatchetRun(jobId) {
  return (jobId && RUNS.get(jobId)) || null;
}

export function srLoadRatchetRun(jobId, options) {
  if (!jobId) return Promise.resolve(null);
  return load(RUNS, jobId, runPath(jobId), options);
}

export function srRatchetPreview(profileId, body) {
  return apiPost(`${profilePath(profileId)}/preview`, body);
}

export async function srStartRatchet(profileId, body, { bookId = null } = {}) {
  const data = await apiPost(profilePath(profileId), body);
  const book = bookId || (data && data.job && data.job.book_id) || null;
  if (data && data.job_id) srActivityTrack(data.job_id, { kind: "ratchet", book_id: book, title: book ? srBookTitle(book) : null });
  srLoadRatchetOverview(profileId, { force: true });
  return data;
}

export async function srCancelRatchet(profileId) {
  const data = await apiPost(`${profilePath(profileId)}/cancel`, {});
  srActivityPoke();
  srLoadRatchetOverview(profileId, { force: true });
  return data;
}

/* 存盲读选择（整份替换）：先改界面；失败回滚并抛出。reveal：存完重读详情（有了答案与数字）与这份画像的总览 */
export async function srSaveRatchetVotes(jobId, votes, { reveal = false, profileId = null } = {}) {
  const entry = RUNS.get(jobId);
  const before = entry && entry.data ? entry.data.votes : null;
  if (entry && entry.data) {
    RUNS.set(jobId, { ...entry, data: { ...entry.data, votes } });
    srEmit("ratchet");
  }
  try {
    const data = await apiPut(`${runPath(jobId)}/votes`, { votes, reveal });
    if (reveal || (data && data.revealed)) {
      await srLoadRatchetRun(jobId, { force: true });
      if (profileId) srLoadRatchetOverview(profileId, { force: true });
    }
    return data;
  } catch (error) {
    const now = RUNS.get(jobId);
    if (now && now.data) {
      RUNS.set(jobId, { ...now, data: { ...now.data, votes: before || {} } });
      srEmit("ratchet");
    }
    throw error;
  }
}

export function srResetRatchetForTests() {
  OVERVIEW.clear();
  RUNS.clear();
  INFLIGHT.clear();
}
