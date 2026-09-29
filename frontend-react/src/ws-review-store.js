import { apiGet, apiPost } from "./lib/client.js";
import { agoLabel } from "./lib/format.js";
import { createSubscribers, storeAlert, useStoreTick } from "./lib/store-utils.js";
import { createKeyedLoader } from "./lib/store-kit.js";
import { adoptModuleListeners, emit, retireModuleListeners } from "./lib/events.js";
import { isRealWorkId } from "./lib/work-id.js";
import { readyWorkId } from "./lib/ready-work.js";
import { WsWorks } from "./ws-works.jsx";
import { preferenceHintLabel, reviewSourceLabel } from "./labels/review.js";

/* ==========================================================
   待办收件箱的 store（2026-09-29 从 ws-review.jsx 拆出；视图仍在 ws-review.jsx，它转出这里的名字）。
   没有视图、不 import 重模块：侧栏徽标、主页与收件箱视图读的是同一份缓存，
   徽标不再自己另拉一遍 open 列表。
   ========================================================== */

/* 待办的五种类型（主页、收件箱同读） */
const RV_KINDS = {
  decision: { label: "决策", tone: "crimson", icon: "GitBranch",     hint: "需要你拍板" },
  risk:     { label: "风险", tone: "rose",    icon: "AlertTriangle", hint: "可能出错" },
  qc:       { label: "质检", tone: "slate",   icon: "Microscope",    hint: "质量建议" },
  idea:     { label: "构思", tone: "gold",    icon: "Snowflake",     hint: "待补内容" },
  note:     { label: "批注", tone: "sage",    icon: "FileText",      hint: "你的批注" },
};


/* ==========================================================
   store —— 接后端统一收件箱。
   GET /api/v1/review-items?state=open|snoozed&project_id=…
   = 持久卡 ∪ 实时派生卡（派生由后端从工作台真相现算：
   不可划掉 / 修好自动消失 / 指纹变化复浮现）。
   resolve 的 effect 在后端同一事务执行；视图通过 rvResolveAction
   告知本次点击的动作，store 把 action_index 带给 resolve 端点。
   「今日已处理 N 件」是 UI 偏好级计数，留 localStorage。
   ========================================================== */
const RV_DONE_LS = "ws_review_done_v1";
const RV_MIGRATED_LS = "ws_review_migrated_v1";
const RV_LEGACY_LS = "ws_review_v1";

/* 目录与雪花的每次保存（写作时自动保存的字数回写也算）都可能改变派生卡，但频率太高：
   按 20 秒节流，窗口内的信号合并成窗口末尾的一次补拉。 */
const RV_NOISY_MIN_INTERVAL_MS = 20_000;

const rvActiveId = () => readyWorkId(WsWorks);

/* 旧表的「写作偏好」行（item_type author_preference_profile）标题是后端直接 dump 的 JSON：
   从里面取出可读的倾向拼一个标题，不把原始 JSON 摊给作者。 */
function rvPreferenceSummary(raw) {
  const text = String(raw || "").trim();
  if (!/^[{[]/.test(text)) return null;
  let parsed;
  try { parsed = JSON.parse(text); } catch (e) { return null; }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const hints = Array.isArray(parsed.safe_preference_hints) ? parsed.safe_preference_hints : [];
  const labels = [...new Set(hints.map(preferenceHintLabel).filter(Boolean))];
  const edits = Number(parsed.manual_edit_count) || 0;
  const rejected = Number(parsed.rejected_proposal_count) || 0;
  return {
    title: labels.length ? `写作偏好：${labels.slice(0, 3).join("、")}${labels.length > 3 ? " 等" : ""}` : "写作偏好有新记录",
    detail: [
      edits ? `从你最近 ${edits} 次手改里记下的倾向。` : rejected ? `从你驳回的 ${rejected} 条修改提案里记下的倾向。` : "系统记下的一条写作倾向。",
      "这条只是记录，不会改变之后的生成；点「知道了」把它移出收件箱。",
    ].join(""),
  };
}

/* 后端卡片 → 视图条目（形状=契约附录卡片形状） */
function rvAdapt(card) {
  const occurred = card.occurred_at ? Date.parse(card.occurred_at) : NaN;
  const pref = rvPreferenceSummary(card.title);
  return {
    id: card.id,
    kind: card.kind || "note",
    priority: card.priority || 2,
    /* 后端卡带质量分级时透传——Q0/Q1=阻断、Q2/Q3=建议，
       视图据此把「无法继续」与「有稿建议修改」标开；无分级不猜 */
    qualityLevel: card.quality_level || undefined,
    title: pref ? pref.title : card.title,
    where: card.where || "",
    source: reviewSourceLabel(card.source),
    time: card.live ? "实时" : (Number.isNaN(occurred) ? "" : agoLabel(occurred)),
    detail: card.detail || (pref ? pref.detail : ""),
    preview: card.preview || undefined,
    checklist: card.checklist || undefined,
    options: card.options || undefined,
    live: !!card.live,
    actions: (card.actions || []).map(a => ({
      label: a.label,
      intent: a.intent || "ghost",
      // 旧的 bridge 与 resolve 同义
      op: a.op === "nav" ? "nav" : a.op === "snooze" ? "snooze" : "resolve",
      to: a.nav_to || a.to,
      step: a.nav_step || a.step,
      scene: a.nav_scene || a.scene,
      posture: a.nav_posture || a.posture,
      canonId: a.canon_id || a.canonId,
      /* 后端 effect 不在前端执行；保留占位对象让视图的
         needsChoice 守卫（带效果的卡不允许批量划掉）继续生效 */
      effect: a.effect ? { type: "__backend__" } : undefined,
    })),
  };
}

/* 视图/调用方条目 → 后端卡片载荷（rvPush 用） */
function rvToPayload(item) {
  return {
    project_id: rvActiveId(),
    kind: item.kind || "note",
    priority: item.priority || 2,
    title: item.title,
    source: item.source || "",
    where: item.where || "",
    detail: item.detail || "",
    preview: item.preview,
    checklist: item.checklist,
    options: item.options,
    dedupe_key: item.dedupeKey || item.dedupe_key,
    actions: (item.actions || [{ label: "知道了", intent: "quiet", op: "resolve" }]).map(a => ({
      label: a.label,
      intent: a.intent,
      op: a.op,
      nav_to: a.to,
      nav_step: a.step,
      nav_scene: a.scene,
      nav_posture: a.posture,
      effect: a.effect && a.effect.type !== "__backend__" ? a.effect : undefined,
    })),
  };
}

let rvCache = { open: [], snoozed: [] };
let rvLoadedFor = null;                    // 最近一次成功拉取属于哪部作品（rvReady 用）
let rvLoadError = null;                    // { pid, message }：最近一次拉取失败（成功即清掉）
/* 订阅者（收件箱视图、主页、侧栏徽标）：列表、作品、拉取结果任何一样变了都通知。
   拉取失败只通知订阅者，不广播 ws:review-changed：主页把收到这个事件当成「装载过了」，
   失败时广播会让它把「还没读到」说成「没有待办」。 */
const rvSubs = createSubscribers();
const rvPendingAction = {};               // id → 本次点击的 action_index（resolve 携带）

const rvUrgentCount = () => rvCache.open.filter(i => i.priority === 1).length;

/* 广播时带上紧急条数：侧栏徽标拿它直接更新，不必为 store 自己的变化再拉一遍列表。 */
function rvEmit() {
  rvSubs.notify();
  emit("ws:review-changed", { urgent: rvUrgentCount(), projectId: rvActiveId() });
}

/* 读取器（lib/store-kit）：键 = 作品 id，只收「当前作品」的结果。拉取途中换了作品，新作品另发请求——
   过去并进上一部还在飞的那次，那份结果又因作品不符被丢掉，新作品的收件箱就一直停在「还没读到」（审计 F01-05）。
   处理 / 稍后 / 投递这些写入经 rvLoader.write：写入之前发出的读取回来时不把刚划掉的卡放回来，写完以服务端为准重读。 */
let rvLastFetchAt = 0;
const rvLoader = createKeyedLoader({
  async fetch(pid) {
    rvLastFetchAt = Date.now();
    await rvMigrateLegacy(pid);
    const [open, snoozed] = await Promise.all([
      apiGet(`/api/v1/review-items?state=open&project_id=${encodeURIComponent(pid)}`),
      apiGet(`/api/v1/review-items?state=snoozed&project_id=${encodeURIComponent(pid)}`),
    ]);
    return { open, snoozed };
  },
  isCurrent: (pid) => rvActiveId() === pid,
  apply(pid, { open, snoozed }) {
    rvCache = {
      open: ((open && open.items) || []).map(rvAdapt),
      snoozed: ((snoozed && snoozed.items) || []).map(rvAdapt),
    };
    rvLoadedFor = pid;
    rvLoadError = null;
    rvEmit();
  },
  onError(pid, e) {
    console.warn("[WsReview] 拉取收件箱失败:", e);
    // 记下来给视图说清楚、给重试；否则还没拉到过的收件箱会永远停在「正在读取」
    rvLoadError = { pid, message: (e && e.message) || "" };
    rvSubs.notify();
  },
});

/* 以服务端为准重读当前作品的收件箱（在飞的那一次作废，结束后恰好再读一次） */
function rvFetch() {
  const pid = rvActiveId();
  if (!isRealWorkId(pid)) return Promise.resolve(false);
  return rvLoader.invalidate(pid);
}

/* 本机对当前作品收件箱的一次写入：run 返回 Promise；写完（成功或失败）重读 */
function rvWrite(pid, run) {
  if (!isRealWorkId(pid)) return Promise.resolve().then(run);
  return rvLoader.write(pid, run);
}

/* 作者动作带来的变化（换作品、回收站、别处投递）：去抖后重读 */
let rvFetchTimer = null;
function rvFetchDebounced(ms = 600) {
  clearTimeout(rvFetchTimer);
  rvFetchTimer = setTimeout(() => { rvFetch(); }, ms);
}

let rvNoisyTimer = null;
function rvFetchThrottled() {
  const wait = rvLastFetchAt + RV_NOISY_MIN_INTERVAL_MS - Date.now();
  if (wait <= 0) { rvFetchDebounced(); return; }
  if (rvNoisyTimer) return;
  rvNoisyTimer = setTimeout(() => { rvNoisyTimer = null; rvFetch(); }, wait);
}

/* 一次性迁移：旧 localStorage 的 custom 项上行。
   只有全部写入成功才落完成标记；每项使用稳定 dedupe_key，因此中途失败后的整批重试
   不会复制已成功写入的卡片。resolved/snoozed 状态无法可靠映射，保留为 open。 */
const rvLegacyMigrations = new Map();
async function rvMigrateLegacy(pid) {
  if (!isRealWorkId(pid)) return false;
  const flagKey = RV_MIGRATED_LS + "::" + pid;
  try {
    if (localStorage.getItem(flagKey)) return true;
  } catch (e) {
    console.warn("[WsReview] 无法读取旧待办迁移状态:", e);
    return false;
  }
  if (rvLegacyMigrations.has(pid)) return rvLegacyMigrations.get(pid);

  const migration = (async () => {
    try {
      // 使用调用时已锁定的作品 id；不能再读取可能已切换的全局 activeId。
      const raw = localStorage.getItem(`${RV_LEGACY_LS}::${pid}`);
      const st = raw ? JSON.parse(raw) : null;
      const custom = Array.isArray(st && st.custom) ? st.custom : [];
      for (let index = 0; index < custom.length; index += 1) {
        const it = custom[index];
        if (!it || !it.title) continue;
        const legacyId = String(it.id || "item").replace(/[^a-zA-Z0-9_.:-]/g, "_").slice(0, 80);
        await apiPost("/api/v1/review-items", rvToPayload({
          ...it,
          dedupeKey: it.dedupeKey || it.dedupe_key || `legacy-review:${index}:${legacyId}`,
        }));
      }
      localStorage.setItem(flagKey, new Date().toISOString());
      return true;
    } catch (e) {
      // 不写完成标记，也不删除旧数据；下一次刷新会用同一 dedupe_key 安全重试。
      console.warn("[WsReview] 旧待办迁移失败（保留旧数据，下次重试）:", e);
      return false;
    } finally {
      rvLegacyMigrations.delete(pid);
    }
  })();
  rvLegacyMigrations.set(pid, migration);
  return migration;
}

function rvPush(item) {
  const payload = rvToPayload(item || {});
  rvWrite(payload.project_id, () => apiPost("/api/v1/review-items", payload)).catch((e) => {
    console.warn("[WsReview] 投递待办失败:", e);
  });
  return "pending"; // 旧签名返回 id；真实 id 由刷新后的列表供给
}

function rvOpenItems() {
  return rvCache.open.slice().sort((a, b) => (a.priority === 1 ? 0 : 1) - (b.priority === 1 ? 0 : 1));
}
function rvSnoozedList() { return rvCache.snoozed; }

/* 当前作品的收件箱是否已经从后端拉回来过——空列表是「真的没有」还是「还没拉」由它区分。 */
function rvReady() {
  const pid = rvActiveId();
  return !!pid && rvLoadedFor === pid;
}

/* 当前作品最近一次拉取失败的原因（没有失败就是 null）。 */
function rvLoadErrorOf() {
  const pid = rvActiveId();
  return rvLoadError && rvLoadError.pid === pid ? rvLoadError : null;
}

function rvSubscribe(fn) { return rvSubs.subscribe(fn); }

/* 订阅式读取：{ items, snoozed, ready, error }，收件箱、作品或拉取结果变化时重渲。
   error 只在还没成功拉到过时有意义：拉到过之后的失败保留旧列表，不打扰。 */
function useReviewOpenItems() {
  useStoreTick(rvSubscribe);
  return { items: rvOpenItems(), snoozed: rvSnoozedList(), ready: rvReady(), error: rvLoadErrorOf() };
}

/* 侧栏徽标：当前作品紧急（priority 1）待办的条数；还没读到 / 没有紧急的 → null。
   过去徽标自己另拉一遍 open 列表（作者写作时与本 store 各拉一次，审计 F01-08）；现在读这里的缓存。 */
function useReviewUrgent() {
  useStoreTick(rvSubscribe);
  const urgent = rvReady() ? rvUrgentCount() : 0;
  return urgent > 0 ? urgent : null;
}

const rvToday = () => new Date().toISOString().slice(0, 10);
function rvDoneState() {
  try { return JSON.parse(localStorage.getItem(RV_DONE_LS)) || {}; } catch (e) { return {}; }
}
function rvDoneToday() { const st = rvDoneState(); return st.d === rvToday() ? (st.n || 0) : 0; }
function rvBumpDone(delta) {
  try { localStorage.setItem(RV_DONE_LS, JSON.stringify({ d: rvToday(), n: Math.max(0, rvDoneToday() + delta) })); } catch (e) {}
}

/* 执行动作前告知 store「点了哪个动作」：resolve 端点据此执行卡片上的后端 effect */
function rvResolveAction(item, action) {
  if (!item || !action) return;
  const index = (item.actions || []).indexOf(action);
  if (index >= 0) rvPendingAction[item.id] = index;
}

function rvMarkResolved(ids) {
  const pid = rvActiveId();
  rvCache = { ...rvCache, open: rvCache.open.filter(i => !ids.includes(i.id)) };
  rvBumpDone((ids || []).length);
  rvEmit();
  rvWrite(pid, async () => {
    for (const id of ids || []) {
      const body = { project_id: pid };
      if (rvPendingAction[id] != null) { body.action_index = rvPendingAction[id]; delete rvPendingAction[id]; }
      try { await apiPost(`/api/v1/review-items/${encodeURIComponent(id)}/resolve`, body); }
      catch (e) {
        storeAlert(e, "处理失败。");
      }
    }
  });
}

/* 撤销：items 是视图刚放回列表的卡。缓存里也放回去（不广播，视图已经按原位置插好），
   免得撤销后、服务端确认前的任何一次广播又把它们从视图里同步掉。 */
function rvUnresolve(ids, items) {
  const back = (items || []).filter(it => it && !rvCache.open.some(x => x.id === it.id));
  if (back.length) rvCache = { ...rvCache, open: [...rvCache.open, ...back] };
  rvBumpDone(-(ids || []).length);
  rvWrite(rvActiveId(), async () => {
    for (const id of ids || []) {
      try { await apiPost(`/api/v1/review-items/${encodeURIComponent(id)}/unresolve`, {}); } catch (e) {}
    }
  });
}

/* 稍后 / 恢复都在 store 里乐观移动再广播：视图只听广播，不自己在两份列表之间搬
   （以前在一个 setState 的更新函数里调另一个 setState，开发模式把更新函数跑两遍，稍后一张卡数成 3）。
   fallback 是视图手上的那张卡：撤销刚恢复、缓存里还没有它时也能移过去。 */
function rvMarkSnoozed(id, fallback) {
  const pid = rvActiveId();
  const it = rvCache.open.find(x => x.id === id) || fallback;
  rvCache = {
    open: rvCache.open.filter(x => x.id !== id),
    snoozed: it ? [it, ...rvCache.snoozed.filter(x => x.id !== id)] : rvCache.snoozed,
  };
  rvEmit();
  rvWrite(pid, () => apiPost(`/api/v1/review-items/${encodeURIComponent(id)}/snooze`, { project_id: pid }).catch(() => {}));
}

function rvUnsnooze(id) {
  const pid = rvActiveId();
  const it = rvCache.snoozed.find(x => x.id === id);
  rvCache = {
    open: it ? [...rvCache.open.filter(x => x.id !== id), it] : rvCache.open,
    snoozed: rvCache.snoozed.filter(x => x.id !== id),
  };
  rvEmit();
  rvWrite(pid, () => apiPost(`/api/v1/review-items/${encodeURIComponent(id)}/unsnooze`, { project_id: pid }).catch(() => {}));
}

/* 启动装载 + 真相变动时刷新（派生卡在后端现算，目录/作品切换都可能改变它们）。
   模块在 HMR/测试 resetModules 后可能重新执行，先撤销旧实例的全局订阅。 */
retireModuleListeners("ws-review");
const rvOnWorkChanged = () => {
  try {
    // 换了作品：上一部的列表不能冒充这一部的（rvReady 回到 false，徽标清零）
    if (rvLoadedFor !== rvActiveId()) { rvCache = { open: [], snoozed: [] }; rvLoadedFor = null; }
    rvSubs.notify();
    rvFetchDebounced();
  } catch (e) {}
};
const rvOnTrashChanged = () => { try { rvFetchDebounced(); } catch (e) {} };
/* 别处投递的广播（不带本 store 算好的 urgent）：很快补拉一次；本 store 自己的广播不理 */
const rvOnReviewChanged = (event) => {
  const detail = event && event.detail;
  if (detail && typeof detail.urgent === "number") return;
  try { rvFetchDebounced(180); } catch (e) {}
};
/* 目录与雪花的保存在写作时每次自动保存都会广播（字数回写也算）：按 20 秒节流 */
const rvOnNoisy = () => { try { rvFetchThrottled(); } catch (e) {} };
const rvOnHashChanged = () => {
  try { if ((location.hash || "").includes("review")) rvFetchDebounced(); } catch (e) {}
};
const RV_LISTENERS = {
  "ws:work-changed": rvOnWorkChanged,
  "ws:trash-changed": rvOnTrashChanged,
  "ws:review-changed": rvOnReviewChanged,
  "ws:catalog-changed": rvOnNoisy,
  "ws:snow-saved": rvOnNoisy,
  hashchange: rvOnHashChanged,
};
Object.entries(RV_LISTENERS).forEach(([name, fn]) => window.addEventListener(name, fn));
adoptModuleListeners("ws-review", () => {
  Object.entries(RV_LISTENERS).forEach(([name, fn]) => window.removeEventListener(name, fn));
  clearTimeout(rvFetchTimer); clearTimeout(rvNoisyTimer); rvNoisyTimer = null;
});
try { rvFetch(); } catch (e) {}

export {
  RV_KINDS, rvOpenItems, rvSnoozedList, rvReady, rvLoadErrorOf, rvUrgentCount, rvDoneToday,
  rvFetch, rvPush, rvMarkResolved, rvUnresolve, rvMarkSnoozed, rvUnsnooze, rvResolveAction, rvMigrateLegacy,
  rvSubscribe, useReviewOpenItems, useReviewUrgent,
};
