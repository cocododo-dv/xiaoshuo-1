import { apiGet, apiPost, apiPut } from "./lib/client.js";
import { createSubscribers } from "./lib/store-utils.js";
import { WsWorks } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { readyWorkId } from "./lib/ready-work.js";
import { dramaFieldLabel } from "./labels/catalog.js";

/* ==========================================================
   WsAuthorAi — 章节编排的「AI 编排」store（原 ws-chapter-plan.jsx 的 WsChapterPlan，2026-10 改名：
   「计划」在章节编排里有三种意思——这个 store、构思的分章面板 WsChapterPlanPanel、后端的 SnowflakeChapterPlan）
   （docs/chapter-arrangement-llm-design-2026-07-16.md §7）

   后端契约（/api/v2/projects/{pid}/catalog/chapters/{chid}/…）：
   · GET/PUT architecture + POST architecture/generate —— 章节蓝图一等公民；读回来的蓝图带 design_changed
     （作者写的蓝图留下来之后构思 / 参考书又改过时的 { reason, at, message }）
   · POST plan/candidates | plan/fill | plan/review —— 咨询通道。没有可用模型时回 409
     CHAPTER_PLAN_LLM_NOT_CONFIGURED + details.author_action（作者 2026-09-15「没有模型就不兜底」：
     不再有 200 + source:"fallback" 的规则结果冒充 AI）。这里把 author_action 挂进桶，界面给「去系统配置」；
   · GET plan/gaps —— 待补清单：按空槽列出的，不是 AI 的结果、不需要模型。一键补全碰上没有模型时读它；
   · POST plan/apply —— 咨询补丁经作者确认后的原子回写；成功后重拉目录收敛

   状态按「后端 chapter_id」分桶；所有键都是后端 id（视图层负责
   backendId 换算）。写失败不做本地猜测：错误原样挂进桶，目录以
   服务端为准（apply 失败不动 WsCatalog 缓存）。
   界面在 ws-author-ai.jsx（「AI 编排」卡与 AI 体检）；补丁摊行 / 收回的纯函数在本文件末尾。
   ========================================================== */

const cpState = {};          // backendChapterId → bucket
const cpSubs = createSubscribers();
let cpVersionCounter = 0;    // useSyncExternalStore 的快照信号（bucket 引用稳定，靠版本号触发重渲）

const CP_EMPTY = Object.freeze({
  arch: Object.freeze({ status: "idle", data: null, error: null, busy: false }),
  action: Object.freeze({ busy: false, kind: null, error: null }),
  candidates: null,          // {items, degraded, llmCallId} | null
  fill: null,                // {patch, notes, gaps, dropped, degraded, llmCallId} | null
  review: null,              // {findings, degraded} | null
  gaps: null,                // 待补清单 {source: "rules", gaps: [...]} | null（一键补全碰上没有模型时读）
  authorAction: null,        // 最近一次「没有可用模型」的 author_action
  applied: null,             // 最近一次 apply 的 {scenes, appended, skipped}
});

function cpNotify() { cpVersionCounter += 1; cpSubs.notify(); }

function cpBucket(chapterId) {
  if (!cpState[chapterId]) {
    cpState[chapterId] = {
      arch: { ...CP_EMPTY.arch },
      action: { ...CP_EMPTY.action },
      candidates: null,
      fill: null,
      review: null,
      gaps: null,
      authorAction: null,
      applied: null,
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

/* 跑一个动作：同一章同一时间只有一个。没有可用模型时不算失败——挂上 author_action、跑 onLlmMissing、返回 null
   （界面给「去系统配置」）；别的失败把错误挂进桶并抛出。 */
async function cpRun(chapterId, kind, fn, onLlmMissing) {
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
    if (onLlmMissing) await onLlmMissing();
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

  /* ---- 三通道 ---- */
  async requestCandidates(chapterId, directionHint) {
    const pid = cpProjectId();
    if (!pid || !chapterId) return null;
    return cpRun(chapterId, "candidates", async (bucket) => {
      const body = directionHint ? { direction_hint: directionHint } : {};
      const data = await apiPost(`${cpBase(pid, chapterId)}/plan/candidates`, body);
      bucket.authorAction = null;
      bucket.candidates = {
        items: (data && data.candidates) || [],
        degraded: (data && data.degraded_slots) || [],
        llmCallId: (data && data.llm_call_id) || null,
      };
      return bucket.candidates;
    });
  },

  async requestFill(chapterId, opts) {
    const pid = cpProjectId();
    if (!pid || !chapterId) return null;
    const mode = opts && opts.candidate ? "adopt" : "fill";
    /* 没有可用模型：不给 AI 的补丁，改列出还空着的格子（规则按空槽算的，界面上明说不是 AI） */
    return cpRun(chapterId, "fill", async (bucket) => {
      const body = mode === "adopt" ? { mode, candidate: opts.candidate } : { mode };
      const data = await apiPost(`${cpBase(pid, chapterId)}/plan/fill`, body);
      bucket.authorAction = null;
      bucket.gaps = null;
      bucket.fill = {
        patch: (data && data.patch) || { drama: {}, scenes: [], append_scenes: [] },
        notes: (data && data.notes) || [],
        gaps: (data && data.gaps) || [],
        dropped: (data && data.dropped) || [],
        degraded: (data && data.degraded_slots) || [],
        llmCallId: (data && data.llm_call_id) || null,
      };
      return bucket.fill;
    }, () => WsAuthorAi.loadGaps(chapterId));
  },

  /* 待补清单（GET plan/gaps，不是 AI）：读不到就不列 */
  async loadGaps(chapterId) {
    const pid = cpProjectId();
    if (!pid || !chapterId) return null;
    const bucket = cpBucket(chapterId);
    try {
      const data = await apiGet(`${cpBase(pid, chapterId)}/plan/gaps`);
      bucket.fill = null;
      bucket.gaps = { source: (data && data.source) || "rules", gaps: (data && data.gaps) || [] };
    } catch (e) {
      bucket.gaps = null;
    }
    cpNotify();
    return bucket.gaps;
  },

  async requestReview(chapterId) {
    const pid = cpProjectId();
    if (!pid || !chapterId) return null;
    return cpRun(chapterId, "review", async (bucket) => {
      const data = await apiPost(`${cpBase(pid, chapterId)}/plan/review`, {});
      bucket.authorAction = null;
      bucket.review = {
        findings: (data && data.findings) || [],
        degraded: (data && data.degraded_slots) || [],
      };
      return bucket.review;
    });
  },

  /* ---- 原子回写：成功后目录以服务端为准（重拉收敛） ---- */
  async applyPatch(chapterId, patch) {
    const pid = cpProjectId();
    if (!pid || !chapterId) return null;
    return cpRun(chapterId, "apply", async (bucket) => {
      const data = await apiPost(`${cpBase(pid, chapterId)}/plan/apply`, { patch });
      bucket.applied = {
        drama: (data && data.applied && data.applied.drama) || 0,
        scenes: (data && data.applied && data.applied.scenes) || 0,
        appended: (data && data.applied && data.applied.appended) || 0,
        skipped: (data && data.skipped) || [],
      };
      bucket.fill = null;        // 已消费的补丁不再展示
      bucket.candidates = null;
      try { await WsCatalog.refresh(); } catch (e) {}
      return bucket.applied;
    });
  },
};

/* ==========================================================
   补丁 ↔ 可勾选行（纯函数，章节编排的 AI 编排卡用）
   plan/fill 给的是一整份补丁；作者要逐条勾选，所以先摊成行（每行 = 一处填空或一张追加卡），
   写入前再按勾选收回补丁形状交给 applyPatch。
   ========================================================== */

/* 字段中文名：界面上不出现英文字段键。戏剧卡的格子（drama.promise……）叫法取 labels/catalog.js 的 DRAMA_FIELDS */
const CP_FIELD_LABELS = {
  goal: "目标", conflict: "冲突", setback: "挫败",
  reaction: "反应", dilemma: "两难", decision: "决定",
  pov_character_name: "视角", exit_change: "离场变化", hook: "钩子", title: "标题",
};
export const cpFieldLabel = (key) => {
  const text = String(key || "");
  return CP_FIELD_LABELS[text] || dramaFieldLabel(text.startsWith("drama.") ? text.slice(6) : text) || "其他字段";
};

export function cpPatchRows(patch, sceneNameOf) {
  const rows = [];
  Object.entries(patch.drama || {}).forEach(([field, value]) => {
    rows.push({
      key: `drama:${field}`,
      kind: "drama", field, value,
      label: `章节戏剧卡 · ${cpFieldLabel(`drama.${field}`)}`,
    });
  });
  (patch.scenes || []).forEach((item) => {
    Object.entries(item.set || {}).forEach(([field, value]) => {
      rows.push({
        key: `set:${item.scene_id}:${field}`,
        kind: "set", sceneId: item.scene_id, field, value,
        label: `${sceneNameOf(item.scene_id)} · ${cpFieldLabel(field)}`,
      });
    });
  });
  (patch.append_scenes || []).forEach((item, i) => {
    rows.push({
      key: `append:${i}`,
      kind: "append", append: item,
      label: `新场景 · ${item.title}（${item.kind === "reactive" ? "反应" : "主动"}）`,
      value: Object.entries(item.brief || {}).map(([k, v]) => `${cpFieldLabel(k)}：${v}`).join(" / ") || "（三拍待写）",
    });
  });
  return rows;
}

export function cpRowsToPatch(rows, checked) {
  const drama = {};
  const sceneMap = {};
  const appends = [];
  rows.forEach((row) => {
    if (!checked[row.key]) return;
    if (row.kind === "drama") {
      drama[row.field] = row.value;
    } else if (row.kind === "set") {
      sceneMap[row.sceneId] = sceneMap[row.sceneId] || { scene_id: row.sceneId, set: {} };
      sceneMap[row.sceneId].set[row.field] = row.value;
    } else {
      appends.push(row.append);
    }
  });
  return { drama, scenes: Object.values(sceneMap), append_scenes: appends };
}

