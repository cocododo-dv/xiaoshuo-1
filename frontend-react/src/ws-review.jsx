import React from "react";
import ReactDOM from "react-dom";
import { I } from "./icons.jsx";
import { agoLabel } from "./lib/ago.js";
import { apiGet, apiPost } from "./lib/client.js";
import { storeAlert } from "./lib/store-utils.js";
import { WsWorks } from "./ws-works.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { PageHeader, Segmented, Tag, EmptyState, Notice, Spinner } from "./ws-ui.jsx";
import { isImeComposing } from "./ws-dialog.jsx";
import { wsToast } from "./ws-notify.jsx";
import { UndoToast, useUndoToast } from "./ws-undo-toast.jsx";
import { preferenceHintLabel, reviewSourceLabel } from "./ws-labels.js";

/* ==========================================================
   WsReview — 待办收件箱
   把工作台各处需要作者拍板的事汇到一处——决策、风险、质检建议、构思缺口、
   批注——按紧急程度排好，每条带来源、位置和理由。处理完就回去写。
   ========================================================== */

const RV_KINDS = {
  decision: { label: "决策", tone: "crimson", icon: "GitBranch",     hint: "需要你拍板" },
  risk:     { label: "风险", tone: "rose",    icon: "AlertTriangle", hint: "可能出错" },
  qc:       { label: "质检", tone: "slate",   icon: "Microscope",    hint: "质量建议" },
  idea:     { label: "构思", tone: "gold",    icon: "Snowflake",     hint: "待补内容" },
  note:     { label: "批注", tone: "sage",    icon: "FileText",      hint: "你的批注" },
};

/* RV_KINDS 的色板名（旧契约，主页也读）→ ws-ui 的语气 */
const RV_TONE = { crimson: "accent", rose: "danger", slate: "info", gold: "warn", sage: "ok" };

/* priority: 1 = 优先处理 · 2/3 = 其余。提示只说轻重，不替卡片的类型下结论。 */
const RV_BAND = { 1: { label: "优先处理", hint: "尽快处理" }, 2: { label: "其余待办", hint: "不急，得空再看" } };

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

/* 目录的每次变化（写作时自动保存的字数回写也算）都可能改变派生卡，但频率太高：
   按 20 秒节流，窗口内的信号合并成窗口末尾的一次补拉（与侧栏徽标同一口径）。 */
const RV_NOISY_MIN_INTERVAL_MS = 20_000;

const rvActiveId = () => { try { return WsWorks.activeId(); } catch (e) { return null; } };

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
/* 拉取失败只通知本模块的订阅者，不广播 ws:review-changed：主页把收到这个事件当成「装载过了」，
   失败时广播会让它把「还没读到」说成「没有待办」。 */
const rvLoadListeners = new Set();
function rvNotifyLoad() { rvLoadListeners.forEach((fn) => { try { fn(); } catch (e) {} }); }
const rvResolvedSet = new Set();          // 本会话内已处理 id（rvIsResolved 用）
const rvPendingAction = {};               // id → 本次点击的 action_index（resolve 携带）

const rvUrgentCount = () => rvCache.open.filter(i => i.priority === 1).length;

/* 广播时带上紧急条数：侧栏徽标拿它直接更新，不必为 store 自己的变化再拉一遍列表。 */
function rvEmit() {
  try {
    window.dispatchEvent(new CustomEvent("ws:review-changed", {
      detail: { urgent: rvUrgentCount(), projectId: rvActiveId() },
    }));
  } catch (e) {}
}

let rvFetching = null;
let rvLastFetchAt = 0;
function rvFetch() {
  const pid = rvActiveId();
  if (!pid || pid === "__loading__") return Promise.resolve();
  if (rvFetching) return rvFetching;
  rvLastFetchAt = Date.now();
  rvFetching = (async () => {
    try {
      await rvMigrateLegacy(pid);
      const [open, snoozed] = await Promise.all([
        apiGet(`/api/v1/review-items?state=open&project_id=${encodeURIComponent(pid)}`),
        apiGet(`/api/v1/review-items?state=snoozed&project_id=${encodeURIComponent(pid)}`),
      ]);
      // 拉取途中换了作品：这份结果属于上一部，丢掉
      if (rvActiveId() !== pid) return;
      rvCache = {
        open: ((open && open.items) || []).map(rvAdapt),
        snoozed: ((snoozed && snoozed.items) || []).map(rvAdapt),
      };
      rvLoadedFor = pid;
      rvLoadError = null;
      rvEmit();
    } catch (e) {
      console.warn("[WsReview] 拉取收件箱失败:", e);
      // 记下来给视图说清楚、给重试；否则还没拉到过的收件箱会永远停在「正在读取」
      if (rvActiveId() === pid) {
        rvLoadError = { pid, message: (e && e.message) || "" };
        rvNotifyLoad();
      }
    } finally {
      rvFetching = null;
    }
  })();
  return rvFetching;
}

let rvFetchTimer = null;
function rvFetchDebounced() {
  clearTimeout(rvFetchTimer);
  rvFetchTimer = setTimeout(() => { rvFetch(); }, 600);
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
  if (!pid || pid === "__loading__") return false;
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

function rvCustomList() { return rvCache.open.filter(i => !i.live); }

function rvPush(item) {
  const payload = rvToPayload(item || {});
  apiPost("/api/v1/review-items", payload).then(() => rvFetch()).catch((e) => {
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

/* 订阅式读取：{ items, snoozed, ready, error }，收件箱、作品或拉取结果变化时重渲。
   error 只在还没成功拉到过时有意义：拉到过之后的失败保留旧列表，不打扰。 */
function useReviewOpenItems() {
  const [, force] = React.useState(0);
  React.useEffect(() => {
    const bump = () => force(n => n + 1);
    window.addEventListener("ws:review-changed", bump);
    window.addEventListener("ws:work-changed", bump);
    rvLoadListeners.add(bump);
    return () => {
      window.removeEventListener("ws:review-changed", bump);
      window.removeEventListener("ws:work-changed", bump);
      rvLoadListeners.delete(bump);
    };
  }, []);
  return { items: rvOpenItems(), snoozed: rvSnoozedList(), ready: rvReady(), error: rvLoadErrorOf() };
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
  (ids || []).forEach(id => rvResolvedSet.add(id));
  rvCache = { ...rvCache, open: rvCache.open.filter(i => !ids.includes(i.id)) };
  rvBumpDone((ids || []).length);
  rvEmit();
  (async () => {
    for (const id of ids || []) {
      const body = { project_id: pid };
      if (rvPendingAction[id] != null) { body.action_index = rvPendingAction[id]; delete rvPendingAction[id]; }
      try { await apiPost(`/api/v1/review-items/${encodeURIComponent(id)}/resolve`, body); }
      catch (e) {
        storeAlert(e, "处理失败。");
      }
    }
    rvFetch();
  })();
}

/* 撤销：items 是视图刚放回列表的卡。缓存里也放回去（不广播，视图已经按原位置插好），
   免得撤销后、服务端确认前的任何一次广播又把它们从视图里同步掉。 */
function rvUnresolve(ids, items) {
  (ids || []).forEach(id => rvResolvedSet.delete(id));
  const back = (items || []).filter(it => it && !rvCache.open.some(x => x.id === it.id));
  if (back.length) rvCache = { ...rvCache, open: [...rvCache.open, ...back] };
  rvBumpDone(-(ids || []).length);
  (async () => {
    for (const id of ids || []) {
      try { await apiPost(`/api/v1/review-items/${encodeURIComponent(id)}/unresolve`, {}); } catch (e) {}
    }
    rvFetch();
  })();
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
  apiPost(`/api/v1/review-items/${encodeURIComponent(id)}/snooze`, { project_id: pid })
    .then(() => rvFetch())
    .catch(() => rvFetch());
}

function rvUnsnooze(id) {
  const pid = rvActiveId();
  const it = rvCache.snoozed.find(x => x.id === id);
  rvCache = {
    open: it ? [...rvCache.open.filter(x => x.id !== id), it] : rvCache.open,
    snoozed: rvCache.snoozed.filter(x => x.id !== id),
  };
  rvEmit();
  apiPost(`/api/v1/review-items/${encodeURIComponent(id)}/unsnooze`, { project_id: pid })
    .then(() => rvFetch())
    .catch(() => rvFetch());
}

function rvIsResolved(id) { return rvResolvedSet.has(id); }

/* 启动装载 + 真相变动时刷新（派生卡在后端现算，目录/作品切换都可能改变它们）。
   模块在 HMR/测试 resetModules 后可能重新执行，先撤销旧实例的全局订阅。 */
if (window.__wsReviewGlobalHandlers) {
  const old = window.__wsReviewGlobalHandlers;
  window.removeEventListener("ws:work-changed", old.workChanged);
  window.removeEventListener("ws:trash-changed", old.trashChanged);
  window.removeEventListener("hashchange", old.hashChanged);
  old.catalogUnsubscribe?.();
  old.cancelPending?.();
}
const rvOnWorkChanged = () => {
  try {
    // 换了作品：上一部的列表不能冒充这一部的（rvReady 回到 false，徽标清零）
    if (rvLoadedFor !== rvActiveId()) { rvCache = { open: [], snoozed: [] }; rvLoadedFor = null; }
    rvFetchDebounced();
  } catch (e) {}
};
const rvOnTrashChanged = () => { try { rvFetchDebounced(); } catch (e) {} };
const rvOnHashChanged = () => {
  try { if ((location.hash || "").includes("review")) rvFetchDebounced(); } catch (e) {}
};
window.addEventListener("ws:work-changed", rvOnWorkChanged);
window.addEventListener("ws:trash-changed", rvOnTrashChanged);
window.addEventListener("hashchange", rvOnHashChanged);
let rvCatalogUnsubscribe = null;
try { rvCatalogUnsubscribe = WsCatalog.subscribe(() => rvFetchThrottled()); } catch (e) {}
window.__wsReviewGlobalHandlers = {
  workChanged: rvOnWorkChanged,
  trashChanged: rvOnTrashChanged,
  hashChanged: rvOnHashChanged,
  catalogUnsubscribe: rvCatalogUnsubscribe,
  cancelPending: () => { clearTimeout(rvFetchTimer); clearTimeout(rvNoisyTimer); rvNoisyTimer = null; },
};
try { rvFetch(); } catch (e) {}

/* 决策类待办（带真实效果或候选项）与实时派生项不允许被「无决策地划掉」：
   派生项只能去源头处理（修好自动消失）或稍后；快捷键 E / 全部处理完遇到它们改为展开 */
const rvNeedsChoice = (it) => !!(it && (it.live || (it.actions || []).some(a => a.effect) || it.options));

/* 没有动作的卡（旧表行）也要能用鼠标处理掉：补「知道了 / 稍后」。实时派生卡不能划掉，只给「稍后」。 */
function rvActionsOf(item) {
  if (item.actions && item.actions.length) return item.actions;
  const snooze = { label: "稍后", intent: "quiet", op: "snooze", fallback: true };
  return item.live ? [snooze] : [{ label: "知道了", intent: "ghost", op: "resolve", fallback: true }, snooze];
}

const RV_UNDO_MS = 6000;

function WsReview({ go }) {
  const [items, setItems] = React.useState(rvOpenItems);
  const [snoozed, setSnoozed] = React.useState(rvSnoozedList);
  const [ready, setReady] = React.useState(rvReady);
  const [loadError, setLoadError] = React.useState(rvLoadErrorOf);
  const [retrying, setRetrying] = React.useState(false);
  const [filter, setFilter] = React.useState("all");
  const [openId, setOpenId] = React.useState(() => { const l = rvOpenItems(); return l[0] ? l[0].id : null; });
  const [removing, setRemoving] = React.useState(null);
  const [doneToday, setDoneToday] = React.useState(rvDoneToday);
  const [showSnoozed, setShowSnoozed] = React.useState(false);
  const [selId, setSelId] = React.useState(null);
  const [kbd, setKbd] = React.useState(false); // 是否用过键盘（点亮快捷键提示）
  const rootRef = React.useRef(null);
  const undoRef = React.useRef(null);        // 最近一批可撤销的处理：{ entries, at }
  const { toast: localToast, show: showLocalToast, clear: clearLocalToast } = useUndoToast();

  /* store 异步装载：缓存更新（后端刷新/外部投递）同步进视图列表 */
  React.useEffect(() => {
    const sync = () => { setItems(rvOpenItems()); setSnoozed(rvSnoozedList()); setReady(rvReady()); setLoadError(rvLoadErrorOf()); };
    window.addEventListener("ws:review-changed", sync);
    window.addEventListener("ws:work-changed", sync);
    rvLoadListeners.add(sync);
    return () => {
      window.removeEventListener("ws:review-changed", sync);
      window.removeEventListener("ws:work-changed", sync);
      rvLoadListeners.delete(sync);
    };
  }, []);

  const retryLoad = async () => {
    setRetrying(true);
    try { await rvFetch(); } finally { setRetrying(false); }
  };

  const counts = React.useMemo(() => {
    const c = { all: items.length };
    Object.keys(RV_KINDS).forEach(k => { c[k] = items.filter(i => i.kind === k).length; });
    return c;
  }, [items]);

  /* 回执：外壳的提示层（wsToast）；单独渲染本视图（没有提示层）时用本地的同款回执。 */
  const notify = (message, action) => {
    if (wsToast({ message, action, timeout: RV_UNDO_MS })) return;
    showLocalToast({ text: message, actionLabel: action && action.label, onAction: action && action.onClick, timeout: RV_UNDO_MS });
  };

  const undo = (batch) => {
    const entries = (batch && batch.entries) || [];
    if (!entries.length) return;
    if (undoRef.current === batch) undoRef.current = null;
    setItems(prev => {
      const next = prev.filter(x => !entries.some(e => e.item.id === x.id));
      entries.slice().sort((a, b) => a.index - b.index).forEach(e => {
        next.splice(Math.min(e.index, next.length), 0, e.item);
      });
      return next;
    });
    rvUnresolve(entries.map(e => e.item.id), entries.map(e => e.item));
    setDoneToday(n => Math.max(0, n - entries.length));
    clearLocalToast();
  };

  const receipt = (entries) => {
    const batch = { entries, at: Date.now() };
    undoRef.current = batch;
    const text = entries.length === 1 ? `已处理「${entries[0].item.title}」` : `已处理 ${entries.length} 条待办`;
    notify(text, { label: "撤销", onClick: () => undo(batch) });
  };

  const resolve = (id) => {
    setRemoving(id);
    setTimeout(() => {
      const index = items.findIndex(x => x.id === id);
      const item = items[index];
      setItems(prev => prev.filter(x => x.id !== id));
      rvMarkResolved([id]);
      setDoneToday(n => n + 1);
      setRemoving(null);
      if (item) receipt([{ item, index }]);
    }, 300);
  };

  // 一键收尾：把当前筛选下的待办全部标记完成（整批可撤销）
  const resolveAll = () => {
    const pool = (filter === "all" ? items : items.filter(i => i.kind === filter));
    const ids = pool.filter(i => !rvNeedsChoice(i)).map(x => x.id);
    const skipped = pool.length - ids.length;
    if (!ids.length) {
      if (skipped) notify(`${skipped} 条需要你拍板或去源头处理，不能批量划掉`);
      return;
    }
    setRemoving("__all__");
    setTimeout(() => {
      const entries = ids.map(id => ({ item: items.find(x => x.id === id), index: items.findIndex(x => x.id === id) }))
        .filter(e => e.item).sort((a, b) => a.index - b.index);
      setItems(prev => prev.filter(x => !ids.includes(x.id)));
      rvMarkResolved(ids);
      setDoneToday(n => n + ids.length);
      setRemoving(null);
      if (entries.length) receipt(entries);
    }, 300);
  };

  /* 两份列表都由 store 的广播同步（sync）；这里只留 300ms 的移出动画 */
  const snooze = (id) => {
    setRemoving(id);
    const item = items.find(x => x.id === id);
    setTimeout(() => {
      rvMarkSnoozed(id, item);
      setRemoving(null);
    }, 300);
  };

  const unsnooze = (id) => rvUnsnooze(id);

  const act = (item, a) => {
    if (a.op === "nav" && a.to) {
      /* 带上下文深链：雪花步骤 / 写作器场景 / 深改姿态 / AI 起草台入列（与命令面板同一套事件） */
      const intents = [];
      if (a.step) intents.push({ type: "ws:snow-step", detail: a.step });
      if (a.scene && a.to === "scene") intents.push({ type: "ws:scene-enqueue", detail: { sid: a.scene } });
      else if (a.scene) intents.push({ type: "ws:writer-scene", detail: a.scene });
      if (a.posture) intents.push({ type: "ws:writer-posture", detail: a.posture });
      go(a.to, intents);
    }
    else if (a.op === "snooze") snooze(item.id);
    else {
      // 补出来的「知道了」不是卡上的动作，不带 action_index
      if (!a.fallback) rvResolveAction(item, a);
      resolve(item.id);
    }
  };

  const visible = filter === "all" ? items : items.filter(i => i.kind === filter);
  const decisionsLeft = items.filter(i => i.priority === 1).length;
  const allClear = items.length === 0;

  // 筛选或列表变化后，选中项不在可见列表里就清掉
  React.useEffect(() => {
    if (selId && !visible.some(x => x.id === selId)) setSelId(null);
  }, [filter, items]); // eslint-disable-line

  const focusRow = (id) => {
    const root = rootRef.current;
    if (!root || !id) return;
    const row = root.querySelector(`.rv-item[data-id="${String(id).replace(/["\\]/g, "\\$&")}"] .rv-row`);
    if (row) row.focus({ preventScroll: false });
  };

  /* 键盘：J / K（或在卡片标题上用 ↑ ↓）在卡片间移动焦点；焦点在某张卡的标题上时
     E 处理、S 稍后；U 撤销刚才那一批。只接管这些键——回车 / 空格留给按钮本身，
     焦点在输入框、别的按钮组、对话框里或收件箱之外时一概不管。 */
  const onKeyRef = React.useRef(null);
  onKeyRef.current = (e) => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || isImeComposing(e)) return;
    const root = rootRef.current;
    const t = e.target;
    if (!root || !t) return;
    const onPage = root.contains(t);
    if (!onPage && t !== document.body && t !== document.documentElement) return;
    if (t.closest && t.closest('[role="dialog"]')) return;
    const tag = (t.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || t.isContentEditable) return;
    const onRow = !!(t.classList && t.classList.contains("rv-row"));
    const currentId = onRow ? (t.closest(".rv-item") || {}).getAttribute?.("data-id") : null;
    const key = e.key;

    if (key === "u" || key === "U") {
      const batch = undoRef.current;
      if (batch && Date.now() - batch.at < RV_UNDO_MS) { e.preventDefault(); setKbd(true); undo(batch); }
      return;
    }
    const down = key === "j" || (onRow && key === "ArrowDown");
    const up = key === "k" || (onRow && key === "ArrowUp");
    if (down || up) {
      if (!visible.length) return;
      e.preventDefault();
      setKbd(true);
      const ids = visible.map(x => x.id);
      const from = currentId ? ids.indexOf(currentId) : ids.indexOf(selId);
      const next = from < 0 ? (down ? 0 : ids.length - 1) : (from + (down ? 1 : -1) + ids.length) % ids.length;
      setSelId(ids[next]);
      focusRow(ids[next]);
      return;
    }
    if (onRow && currentId && (key === "e" || key === "s")) {
      e.preventDefault();
      setKbd(true);
      const cur = visible.find(x => x.id === currentId);
      if (!cur) return;
      if (key === "e" && rvNeedsChoice(cur)) { setOpenId(cur.id); return; } // 决策项：展开让你选，不默认划掉
      const idx = visible.indexOf(cur);
      const next = visible[idx + 1] || visible[idx - 1];
      if (key === "e") resolve(cur.id); else snooze(cur.id);
      if (next) { setSelId(next.id); window.setTimeout(() => focusRow(next.id), 320); }
    }
  };
  React.useEffect(() => {
    const onKey = (e) => onKeyRef.current && onKeyRef.current(e);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // 按优先级分段；只有一段时不画段头（一个孤零零的「其余待办」前面什么都没有）
  const groups = [1, 2]
    .map(band => ({ band, items: visible.filter(it => (it.priority === 1 ? 1 : 2) === band) }))
    .filter(g => g.items.length);
  const showBands = filter === "all" && groups.length > 1;

  const filterOptions = [
    { value: "all", label: "全部", count: counts.all },
    ...Object.entries(RV_KINDS)
      .filter(([k]) => counts[k] > 0)
      .map(([k, m]) => ({ value: k, label: m.label, count: counts[k], icon: <span className="rv-chip-dot" data-tone={RV_TONE[m.tone]} aria-hidden="true" /> })),
  ];

  /* 还没成功拉到过、上一次又失败了：说清楚，给重试（以前只在控制台里警告一句，页面永远「正在读取」） */
  const failed = allClear && !ready && !!loadError;
  const description = allClear
    ? (ready ? "都处理完了，回写作房间继续吧。" : failed ? null : "正在读取待办…")
    : <>现在 <b>{items.length}</b> 条待处理{decisionsLeft ? <>，其中 <b className="rv-em">{decisionsLeft}</b> 条要尽快处理</> : ""}。处理完就回去写。</>;

  return (
    <div className="ws-page ws-view rv" ref={rootRef}>
      <PageHeader className="rv-head" title="待办收件箱" description={description} />

      {!allClear && (
        <div className="rv-toolbar">
          <Segmented label="按类型筛选" value={filter} onChange={setFilter} options={filterOptions} className="rv-chips" />
          <div className="rv-toolbar-right">
            <div className="rv-progress" title="今天在收件箱里处理掉的条数">
              <I.CheckCircle size={14} /> 今日已处理 <b>{doneToday}</b>
            </div>
            {visible.length > 1 && (
              <button type="button" className="btn btn-ghost btn-sm rv-clear-all" onClick={resolveAll}
                title={filter === "all" ? "把能直接划掉的待办都标记完成（需要拍板的会留下）" : "把这一类里能直接划掉的待办标记完成"}>
                <I.Check size={14} /> 全部处理完
              </button>
            )}
          </div>
        </div>
      )}

      {!allClear && (
        <div className={`rv-kbd-hint ${kbd ? "is-lit" : ""}`} aria-hidden="true">
          <kbd>J</kbd><kbd>K</kbd><span>上下切换</span>
          <kbd>↵</kbd><span>展开</span>
          <kbd>E</kbd><span>处理</span>
          <kbd>S</kbd><span>稍后</span>
          <kbd>U</kbd><span>撤销</span>
        </div>
      )}

      {allClear ? (
        ready ? <RvEmpty hasSnoozed={snoozed.length > 0} go={go} />
          : failed ? (
            <Notice tone="danger" className="rv-load-error" testId="review-load-error" title="待办暂时读不出来"
              actions={(
                <button type="button" className="btn btn-ghost btn-sm" data-testid="review-load-retry" disabled={retrying} onClick={retryLoad}>
                  {retrying ? <Spinner size={13} /> : <I.Refresh size={13} />} {retrying ? "重新读取中…" : "重试"}
                </button>
              )}>
              <span title={loadError.message || undefined}>后端可能没有连上。待办存在后端，不会因此丢失；连上后重试即可。</span>
            </Notice>
          )
          : <div className="rv-loading" role="status">正在读取待办…</div>
      ) : (
        <div className="rv-list">
          {groups.map(g => (
            <section key={g.band} className="rv-group" aria-label={showBands ? RV_BAND[g.band].label : "待办"}>
              {showBands && <RvBand band={g.band} />}
              <div className="rv-group-items" role="list">
                {g.items.map(it => (
                  <RvItem key={it.id} item={it} selected={selId === it.id && kbd}
                    open={openId === it.id} removing={removing === it.id || removing === "__all__"}
                    onToggle={() => { setSelId(it.id); setOpenId(o => o === it.id ? null : it.id); }}
                    onAct={(a) => act(it, a)} />
                ))}
              </div>
            </section>
          ))}
          {visible.length === 0 && (
            <EmptyState compact title="这一类暂时没有待办"
              actions={<button type="button" className="btn btn-ghost btn-sm" onClick={() => setFilter("all")}>看全部</button>} />
          )}
        </div>
      )}

      {snoozed.length > 0 && (
        <div className="rv-snoozed">
          <button type="button" className="rv-snoozed-head" onClick={() => setShowSnoozed(s => !s)} aria-expanded={showSnoozed}>
            <I.Clock size={14} />
            <span>稍后处理</span>
            <span className="rv-snoozed-n">{snoozed.length}</span>
            <span className="rv-snoozed-chev" data-open={showSnoozed}><I.ChevronDown size={15} /></span>
          </button>
          {showSnoozed && (
            <div className="rv-snoozed-list">
              {snoozed.map(it => {
                const m = RV_KINDS[it.kind] || RV_KINDS.note;
                return (
                  <div key={it.id} className="rv-snoozed-item">
                    <Tag tone={RV_TONE[m.tone]} dot>{m.label}</Tag>
                    <span className="rv-snoozed-title" title={it.title}>{it.title}</span>
                    <button type="button" className="btn btn-quiet btn-sm" onClick={() => unsnooze(it.id)}><I.Refresh size={13} /> 恢复</button>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {localToast && typeof document !== "undefined" && ReactDOM.createPortal(
        <UndoToast toast={localToast} onClose={clearLocalToast} />,
        document.body,
      )}
    </div>
  );
}

function RvBand({ band }) {
  const b = RV_BAND[band];
  return (
    <div className={`rv-band b-${band}`}>
      <span className="rv-band-label">{b.label}</span>
      <span className="rv-band-hint">{b.hint}</span>
      <span className="rv-band-rule" />
    </div>
  );
}

function RvItem({ item, open, removing, selected, onToggle, onAct }) {
  const m = RV_KINDS[item.kind] || RV_KINDS.note;
  const Ic = I[m.icon] || I.Dot;
  const blocking = item.qualityLevel === "Q0" || item.qualityLevel === "Q1";
  const actions = rvActionsOf(item);
  return (
    <article role="listitem" data-id={item.id} className={`rv-item t-${m.tone} ${open ? "is-open" : ""} ${removing ? "is-removing" : ""} ${selected ? "is-sel" : ""} ${item.priority === 1 ? "is-hot" : ""}`}>
      <span className="rv-spine" aria-hidden="true" />
      <div className="rv-body">
        <button type="button" className="rv-row" onClick={onToggle} aria-expanded={open}>
          <span className="rv-kind" aria-hidden="true"><Ic size={15} /></span>
          <div className="rv-row-main">
            <div className="rv-meta">
              <Tag tone={RV_TONE[m.tone]} dot>{m.label}</Tag>
              {item.qualityLevel && (
                <Tag tone={blocking ? "danger" : "warn"} title={blocking ? "阻断级：处理前不能归档（正文已保留）" : "建议级：不拦归档，按需修改"}>
                  {blocking ? "阻断" : "建议"}
                </Tag>
              )}
              {item.where && <span className="rv-where">{item.where}</span>}
            </div>
            <h3 className="rv-item-title" title={item.title}>{item.title}</h3>
          </div>
          <span className="rv-time">{item.time}</span>
          <span className="rv-chev" data-open={open} aria-hidden="true"><I.ChevronDown size={16} /></span>
        </button>

        <div className="rv-detail" data-open={open}>
          <div className="rv-detail-inner">
            {item.detail && <p className="rv-detail-text">{item.detail}</p>}

            {item.preview && (
              <div className="rv-preview">
                <div className="rv-preview-row"><span className="rv-preview-tag">原</span><span className="rv-preview-old">{item.preview.before}</span></div>
                <div className="rv-preview-row"><span className="rv-preview-tag is-new">改</span><span className="rv-preview-new">{item.preview.after}</span></div>
              </div>
            )}

            {item.checklist && (
              <ul className="rv-checklist">
                {item.checklist.map((c, i) => <li key={i}><I.Circle size={11} /> {c}</li>)}
              </ul>
            )}

            {item.options && (
              <div className="rv-options">
                {item.options.map((o, i) => <span key={i} className="rv-option">{o}</span>)}
              </div>
            )}

            {item.source && <div className="rv-src">来自{item.source}</div>}
          </div>
        </div>

        <div className="rv-actions">
          {actions.map((a, i) => {
            const cls = a.intent === "primary" ? "btn btn-accent btn-sm"
              : a.intent === "ghost" ? "btn btn-ghost btn-sm" : "btn btn-quiet btn-sm";
            return <button type="button" key={i} className={cls} onClick={() => onAct(a)}>{a.label}</button>;
          })}
        </div>
      </div>
    </article>
  );
}

function RvEmpty({ hasSnoozed, go }) {
  return (
    <EmptyState
      className="rv-empty"
      icon="CheckCircle"
      title="收件箱清空了"
      actions={<button type="button" className="btn btn-accent" onClick={() => go("writer")}><I.Pen size={15} /> 进入写作房间</button>}
    >
      {hasSnoozed ? "当前待办都处理完了，还有几条在「稍后处理」里等着。" : "需要你拍板的都处理完了，回到写作房间继续吧。"}
    </EmptyState>
  );
}

Object.assign(window, { WsReview, RV_KINDS, rvOpenItems, rvMarkResolved, rvPush, rvCustomList, rvIsResolved, rvResolveAction });

/* ESM 导出（window.* 赋值过渡期保留，旧调用方与冒烟脚本仍读 window.rvOpenItems 等） */
export {
  WsReview, RV_KINDS, rvOpenItems, rvMarkResolved, rvPush, rvCustomList, rvIsResolved, rvMigrateLegacy,
  rvResolveAction, rvReady, useReviewOpenItems,
};
