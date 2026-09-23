import { apiGet, apiPost } from "./lib/client.js";
import { createSubscribers, useStoreTick } from "./lib/store-utils.js";
import { fidJobActive } from "./ws-fidelity-model.js";

/* ==========================================================
   「像不像」store（2026-09-23 风格参考 v3 · P6b）
   ----------------------------------------------------------
   三样东西，都只读后端（读数入库只有后端一个入口，这里从不写读数）：
   · 一场的读数：GET /api/v1/scenes/{id}/style-fidelity（每个阶段最新的读数、风格步与补丁的决定、评审）
   · 一部作品的汇总：GET /api/v1/projects/{id}/style-fidelity（走势、近期常见偏差、按维平均、每场最新的终稿读数）
   · 对照检查作业：POST /api/v2/style-reference/checks → 轮询 GET /checks/{job_id} 到终态。作业按调用方给的键
     （风格参考页 `book:<id>`、起草台 `scene:<id>`）记在模块里：离开页面再回来，进度和结果还在；
     成功后把那一场 / 那部作品的缓存标旧并重读。
   写操作只有「发起检查」：失败把错误记进条目（界面按 ws-fidelity-model 的 fidErrorInfo 说中文），不抛给调用方。
   只依赖 lib/client.js 与纯派生；不写 window。
   ========================================================== */

const CHECKS_API = "/api/v2/style-reference/checks";
export const FID_POLL_MS = 1500;

const subs = createSubscribers();
const SCENES = new Map();
const PROJECTS = new Map();
const INFLIGHT = new Map();
const CHECKS = new Map();
const TIMERS = new Map();

function notify() { subs.notify(); }

export function fidSubscribe(fn) { return subs.subscribe(fn); }

/* 视图订阅：store 一变就重渲 */
export function useFidelityStore() {
  useStoreTick((bump) => subs.subscribe(bump));
}

/* ---------- 通用：键 → { phase, data, error } ----------
   同一个键同一时间只有一个请求；force 撞上在途的请求时，等它回来再重读一次（在途的那份可能是改动之前发出的）。
   读失败保留上一次的数据。 */
function load(map, prefix, key, fetcher, { force = false } = {}) {
  if (!key) return Promise.resolve(null);
  const flightKey = `${prefix}:${key}`;
  const inflight = INFLIGHT.get(flightKey);
  if (inflight) return force ? inflight.then(() => load(map, prefix, key, fetcher, { force: true })) : inflight;
  const current = map.get(key);
  if (!force && current && current.phase === "ready") return Promise.resolve(current.data);
  map.set(key, { phase: "loading", data: current ? current.data : null, error: null });
  notify();
  const promise = (async () => {
    try {
      const data = await fetcher();
      map.set(key, { phase: "ready", data: data || null, error: null });
      return data || null;
    } catch (error) {
      map.set(key, { phase: "error", data: current ? current.data : null, error });
      return null;
    } finally {
      INFLIGHT.delete(flightKey);
      notify();
    }
  })();
  INFLIGHT.set(flightKey, promise);
  return promise;
}

/* 一场：{ scene_id, bound, policy, readings: {first_draft|revision|patched|final|manual}, decisions[], judge } */
export function fidScene(sceneId) { return (sceneId && SCENES.get(sceneId)) || null; }
export function fidLoadScene(sceneId, opts) {
  return load(SCENES, "scene", sceneId, () => apiGet(`/api/v1/scenes/${encodeURIComponent(sceneId)}/style-fidelity`), opts);
}

/* 一部作品：{ project_id, bound, profile_id, trend[], recent_gaps[], recent_gap_details[], dimension_averages{},
   scene_finals{}, reading_count, final_scene_count } */
export function fidProject(projectId) { return (projectId && PROJECTS.get(projectId)) || null; }
export function fidLoadProject(projectId, opts) {
  return load(PROJECTS, "project", projectId, () => apiGet(`/api/v1/projects/${encodeURIComponent(projectId)}/style-fidelity`), opts);
}

/* 一场的终稿读数变了（归档 / 采纳 / 检查）：这一场重读，作品汇总读过的就重读 */
export function fidInvalidate({ sceneId = null, projectId = null } = {}) {
  if (sceneId && SCENES.has(sceneId)) fidLoadScene(sceneId, { force: true });
  if (projectId && PROJECTS.has(projectId)) fidLoadProject(projectId, { force: true });
  if (!projectId) {
    for (const key of PROJECTS.keys()) fidLoadProject(key, { force: true });
  }
}

/* ==========================================================
   对照检查
   条目：{ key, target, phase: "starting" | "running" | "done" | "failed", jobId, job, reading, error, startedAt }
   ========================================================== */

export function fidCheck(key) { return (key && CHECKS.get(key)) || null; }

export function fidCheckActive(key) {
  const entry = fidCheck(key);
  return !!entry && (entry.phase === "starting" || entry.phase === "running");
}

function setCheck(key, patch) {
  const next = { ...(CHECKS.get(key) || { key }), ...patch };
  CHECKS.set(key, next);
  notify();
  return next;
}

function stopTimer(key) {
  const timer = TIMERS.get(key);
  if (timer != null) clearTimeout(timer);
  TIMERS.delete(key);
}

function checkBody(target) {
  const body = {};
  if (target.sceneId) body.scene_id = target.sceneId;
  else body.text = String(target.text || "");
  if (target.profileId) body.profile_id = target.profileId;
  if (target.projectId) body.project_id = target.projectId;
  return body;
}

function terminalError(job) {
  const raw = (job && job.error) || null;
  if (job && job.status === "cancelled") return { code: "STYLE_REFERENCE_JOB_CANCELLED", message: "这次检查被取消了。" };
  if (!raw) return { code: "", message: "" };
  return { code: raw.code || "", message: raw.message || "", details: raw, retryable: raw.retryable === true };
}

function settle(key, payload) {
  const job = (payload && payload.job) || null;
  const reading = (payload && payload.reading) || null;
  if (fidJobActive(job)) {
    setCheck(key, { phase: "running", job });
    schedule(key);
    return;
  }
  stopTimer(key);
  const entry = CHECKS.get(key) || {};
  if (job && job.status === "succeeded") {
    setCheck(key, { phase: "done", job, reading, error: null, finishedAt: Date.now() });
    const target = entry.target || {};
    fidInvalidate({ sceneId: target.sceneId || (reading && reading.scene_id) || null, projectId: (reading && reading.project_id) || target.projectId || null });
    return;
  }
  setCheck(key, { phase: "failed", job, reading: null, error: terminalError(job), finishedAt: Date.now() });
}

function schedule(key, ms = FID_POLL_MS) {
  stopTimer(key);
  TIMERS.set(key, setTimeout(() => poll(key), ms));
}

async function poll(key) {
  TIMERS.delete(key);
  const entry = CHECKS.get(key);
  if (!entry || !entry.jobId || entry.phase !== "running") return;
  let payload;
  try {
    payload = await apiGet(`${CHECKS_API}/${encodeURIComponent(entry.jobId)}`);
  } catch (error) {
    const now = CHECKS.get(key);
    if (!now || now.jobId !== entry.jobId) return;
    if (error && error.code === "STYLE_REFERENCE_CHECK_NOT_FOUND") {
      setCheck(key, { phase: "failed", error, finishedAt: Date.now() });
      return;
    }
    schedule(key, FID_POLL_MS * 2); // 网络抖动：放慢一点接着问
    return;
  }
  const now = CHECKS.get(key);
  if (!now || now.jobId !== entry.jobId) return; // 期间又发起了一次新的检查
  settle(key, payload);
}

/* 发起一次对照检查。target：{ text } 或 { sceneId }，另给 profileId（对照这份画像）或 projectId（用作品现在的绑定）。
   返回条目；发起失败记进条目的 error（不抛）。 */
export async function fidStartCheck(key, target) {
  if (!key || !target) return null;
  stopTimer(key);
  setCheck(key, { key, target: { ...target }, phase: "starting", jobId: null, job: null, reading: null, error: null, startedAt: Date.now(), finishedAt: null });
  let data;
  try {
    data = await apiPost(CHECKS_API, checkBody(target));
  } catch (error) {
    return setCheck(key, { phase: "failed", error, finishedAt: Date.now() });
  }
  const jobId = (data && (data.job_id || (data.job && data.job.job_id))) || null;
  if (!jobId) return setCheck(key, { phase: "failed", error: { code: "", message: "" }, finishedAt: Date.now() });
  setCheck(key, { jobId, job: (data && data.job) || { job_id: jobId, status: data && data.state }, phase: "running" });
  settle(key, { job: (data && data.job) || { job_id: jobId, status: (data && data.state) || "queued" }, reading: data && data.reading });
  return CHECKS.get(key);
}

/* 页面重新挂载：还在跑的检查接着轮询（模块里的计时器没了时补上） */
export function fidResumeCheck(key) {
  const entry = fidCheck(key);
  if (entry && entry.phase === "running" && !TIMERS.has(key)) schedule(key, 0);
}

export function fidDismissCheck(key) {
  stopTimer(key);
  if (CHECKS.delete(key)) notify();
}

/* 单测用：清空模块级状态 */
export function fidResetForTests() {
  for (const key of Array.from(TIMERS.keys())) stopTimer(key);
  SCENES.clear();
  PROJECTS.clear();
  INFLIGHT.clear();
  CHECKS.clear();
}
