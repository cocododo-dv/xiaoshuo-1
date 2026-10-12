import { apiGet, apiPost, apiPut } from "./lib/client.js";
import { createSubscribers } from "./lib/store-utils.js";
import { WsWorks } from "./ws-works.jsx";
import { readyWorkId } from "./lib/ready-work.js";

/* ==========================================================
   WsAuthorAi — 章节编排「章节蓝图」卡的 store（原 ws-chapter-plan.jsx 的 WsChapterPlan，2026-10 改名：
   「计划」在章节编排里有三种意思——这个 store、构思的分章面板 WsChapterPlanPanel、后端的 SnowflakeChapterPlan）

   后端契约（/api/v2/projects/{pid}/catalog/chapters/{chid}/…）：
   · GET/PUT architecture + POST architecture/generate —— 章节蓝图（起草读的那一份）；读回来的蓝图带 design_changed
     （作者写的蓝图留下来之后构思 / 参考书又改过时的 { reason, at, message }）。没有可用模型时生成回 409
     CHAPTER_PLAN_LLM_NOT_CONFIGURED + details.author_action（作者 2026-09-15「没有模型就不兜底」）：这里把
     author_action 挂进桶，界面给「去系统配置」。

   2026-10（round 2 · B2-5）：方向（plan/candidates）、补全（plan/fill + plan/apply，连同待补清单 plan/gaps）与
   AI 体检（plan/review）一次都没用过，后端删了，这里的请求、补丁摊行 / 收回的纯函数一并删掉。

   状态按「后端 chapter_id」分桶；所有键都是后端 id（视图层负责 backendId 换算）。写失败不做本地猜测：错误原样挂进桶。
   界面在 ws-author-ai.jsx（「章节蓝图」卡）。
   ========================================================== */

const cpState = {};          // backendChapterId → bucket
const cpSubs = createSubscribers();
let cpVersionCounter = 0;    // useSyncExternalStore 的快照信号（bucket 引用稳定，靠版本号触发重渲）

const CP_EMPTY = Object.freeze({
  arch: Object.freeze({ status: "idle", data: null, error: null, busy: false }),
  action: Object.freeze({ busy: false, kind: null, error: null }),
  authorAction: null,        // 最近一次「没有可用模型」的 author_action
});

function cpNotify() { cpVersionCounter += 1; cpSubs.notify(); }

function cpBucket(chapterId) {
  if (!cpState[chapterId]) {
    cpState[chapterId] = {
      arch: { ...CP_EMPTY.arch },
      action: { ...CP_EMPTY.action },
      authorAction: null,
    };
  }
  return cpState[chapterId];
}

/* 能发请求的当前作品 id（书架还在读、新建作品还在等正式 id 时为 null：不发请求） */
const cpProjectId = () => readyWorkId(WsWorks);

const cpBase = (pid, chid) => `/api/v2/projects/${pid}/catalog/chapters/${encodeURIComponent(chid)}`;

function cpError(e) {
  return { code: (e && e.code) || "REQUEST_FAILED", message: (e && e.message) || "请求失败" };
}

/* 没有可用模型（后端 409 CHAPTER_PLAN_LLM_NOT_CONFIGURED）→ 给作者看的 author_action；别的错误（锁章 409 等）不算 */
const CP_LLM_NOT_CONFIGURED = "CHAPTER_PLAN_LLM_NOT_CONFIGURED";
function cpLlmSetupAction(e) {
  if (!e || e.code !== CP_LLM_NOT_CONFIGURED) return null;
  const action = e.details && e.details.author_action;
  if (action && typeof action === "object") return action;
  return { title: "需要先启用真实模型", message: e.message || "", primary_button_label: "去系统配置" };
}

/* 后端蓝图行 → 视图形状 */
function cpAdaptArch(row) {
  if (!row) return null;
  const p = row.payload || {};
  const changed = row.design_changed && typeof row.design_changed === "object" ? row.design_changed : null;
  return {
    rowId: row.row_id,
    createdBy: row.created_by || "",
    createdAt: row.created_at || "",
    fromLlm: !!row.llm_call_id,
    promise: p.chapter_promise || "",
    escalation: Array.isArray(p.escalation_path) ? p.escalation_path : [],
    reveals: Array.isArray(p.reveal_plan) ? p.reveal_plan : [],
    payoff: p.payoff_target || "",
    shift: p.character_shift || "",
    endingQuestion: p.ending_question || "",
    /* 这份蓝图写好之后构思或参考书又改过：{ reason, at, message } */
    designChanged: changed ? { reason: changed.reason || "", at: changed.at || "", message: changed.message || "" } : null,
  };
}

function cpArchBody(view) {
  return {
    chapter_promise: view.promise || "",
    escalation_path: (view.escalation || []).filter((s) => String(s || "").trim()),
    reveal_plan: (view.reveals || []).filter((s) => String(s || "").trim()),
    payoff_target: view.payoff || "",
    character_shift: view.shift || "",
    ending_question: view.endingQuestion || "",
  };
}

/* 跑一个动作：同一章同一时间只有一个。没有可用模型时不算失败——挂上 author_action、返回 null
   （界面给「去系统配置」）；别的失败把错误挂进桶并抛出。 */
async function cpRun(chapterId, kind, fn) {
  const bucket = cpBucket(chapterId);
  if (bucket.action.busy) return null;
  bucket.action = { busy: true, kind, error: null };
  cpNotify();
  try {
    const result = await fn(bucket);
    bucket.action = { busy: false, kind: null, error: null };
    cpNotify();
    return result;
  } catch (e) {
    const setup = cpLlmSetupAction(e);
    bucket.action = { busy: false, kind: setup ? null : kind, error: setup ? null : cpError(e) };
    if (setup) bucket.authorAction = setup;
    cpNotify();
    if (!setup) throw e;
    return null;
  }
}

export const WsAuthorAi = {
  subscribe(fn) { return cpSubs.subscribe(fn); },
  version() { return cpVersionCounter; },
  snapshot(chapterId) { return chapterId && cpState[chapterId] ? cpState[chapterId] : CP_EMPTY; },

  /* ---- 章节蓝图 ---- */
  async loadArchitecture(chapterId) {
    const pid = cpProjectId();
    if (!pid || !chapterId) return null;
    const bucket = cpBucket(chapterId);
    bucket.arch = { ...bucket.arch, status: "loading", error: null };
    cpNotify();
    try {
      const data = await apiGet(`${cpBase(pid, chapterId)}/architecture`);
      bucket.arch = { status: "ready", data: cpAdaptArch(data && data.architecture), error: null, busy: false };
      cpNotify();
      return bucket.arch.data;
    } catch (e) {
      bucket.arch = { status: "error", data: null, error: cpError(e), busy: false };
      cpNotify();
      throw e;
    }
  },

  async saveArchitecture(chapterId, view) {
    const pid = cpProjectId();
    if (!pid || !chapterId) return null;
    return cpRun(chapterId, "arch-save", async (bucket) => {
      const data = await apiPut(`${cpBase(pid, chapterId)}/architecture`, cpArchBody(view));
      bucket.arch = { status: "ready", data: cpAdaptArch(data && data.architecture), error: null, busy: false };
      return bucket.arch.data;
    });
  },

  async generateArchitecture(chapterId) {
    const pid = cpProjectId();
    if (!pid || !chapterId) return null;
    return cpRun(chapterId, "arch-generate", async (bucket) => {
      const data = await apiPost(`${cpBase(pid, chapterId)}/architecture/generate`, {});
      bucket.authorAction = null;
      bucket.arch = { status: "ready", data: cpAdaptArch(data && data.architecture), error: null, busy: false };
      return bucket.arch.data;
    });
  },
};
