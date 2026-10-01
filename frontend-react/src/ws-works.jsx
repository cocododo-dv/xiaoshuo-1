import React from "react";
import { apiDelete, apiGet, apiPatch, apiPost } from "./lib/client.js";
import { createSubscribers, storeAlert, useStoreTick } from "./lib/store-utils.js";
import { toStoreError } from "./lib/store-kit.js";
import { emit } from "./lib/events.js";
import { LOADING_WORK_ID, isRealWorkId } from "./lib/work-id.js";
import { snowStepByBackendKey } from "./ws-nav.js";
import { sceneLabel } from "./labels/catalog.js";

/* ==========================================================
   WsWorks — 多作品管理（FE-ALIGN Phase 2：后端为唯一真相源）
   · 列表/创建/档案更新 走 /api/v2/projects（信封契约见 lib/client.js）
   · 当前作品 id 仍存 localStorage（UI 状态）
   · get/list 保持同步语义：启动用本地缓存影子即时渲染，API 返回后失效更新
   · 字数/进度字段（wordsTotal/wordsToday/streak/chaptersWritten）只读派生：
     由 writing-stats / dashboard 填充，update() 不再回写（原 catPushTotals 回写路径删除）
   · 公开方法签名/订阅语义与原型一致（契约附录）；ws:work-changed 只在切换作品 / 书架成员变化时广播，
     派生统计与档案字段的变化只通知 WsWorks.subscribe 的订阅者（见 wsNotify）
   ========================================================== */

/* 6 月原型时期本机作品（ws_works_created_v1）的一次性上行已删除（批准 #25，重评 R16）：旧键原样留着、不再读。 */
const WS_ACTIVE_LS = "ws_active_work_v1";    // 当前作品 id（UI 状态，长期保留 localStorage）
const WS_CACHE_LS = "ws_works_cache_v1";     // 列表启动缓存（API 真相的本地影子，仅为同步 list()）
const WS_RETIRED_DEMO_IDS = new Set(["tide", "salt"]);

function wsIsRetiredDemo(work) {
  return !!(
    work
    && (WS_RETIRED_DEMO_IDS.has(String(work.id || work.project_id || "")) || work.isDemo === true || work.is_demo === true)
  );
}

function wsAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "";
  const days = Math.floor((Date.now() - then.getTime()) / 864e5);
  if (days <= 0) return "今天";
  if (days === 1) return "昨天";
  return `${days} 天前`;
}

/* 雪花步骤短名（主页雪花卡的文案口径，来自 ws-nav.js 的十步表） */
function wsSnowShort(stepKey, fallback) {
  const step = snowStepByBackendKey(stepKey);
  return (step && step.short) || fallback || "";
}

/* dashboard 的雪花十步 → 主页雪花卡的原料（带后端 step_key，主页据此回到对应那一步） */
function wsAdaptSnow(d) {
  return ((d && d.snowflake) || []).map(s => ({ key: s.step_key, name: wsSnowShort(s.step_key, s.label), s: s.status }));
}

/* —— 响应适配：后端 project payload → 视图作品对象（契约附录形状）—— */
function wsAdaptProject(item, prevHome) {
  const stats = item.stats || {};
  const title = item.title || "未命名作品";
  return {
    id: item.project_id,
    title,
    genre: item.genre || "未定题材",
    mark: item.mark || Array.from(title)[0] || "新",
    accent: item.accent || "slate",
    sub: item.synopsis_line || "",
    wordsTotal: stats.words_total || 0,
    wordsTarget: item.target_word_count || 100000,
    chaptersWritten: item.chapters_written || 0,
    chaptersTotal: item.target_chapter_count || 0,
    wordsToday: stats.words_today || 0,
    wordsTargetDay: item.words_target_daily || 1000,
    streak: stats.streak_days || 0,
    home: prevHome || { blank: true },
  };
}

/* dashboard 载荷 → 原型 home 形状（主页视图的兜底数据源） */
function wsAdaptHome(d) {
  // 还没有正文的作品也照样带上雪花十步：主页雪花卡只读这一份
  if (!d || (!d.resume && !(d.chapters_recent || []).length)) return { blank: true, snow: wsAdaptSnow(d) };
  const brief = d.brief || {};
  const reactive = brief.kind === "reactive";
  const gos = reactive
    ? [
        { k: "反应", tone: "sage", v: brief.reaction || "" },
        { k: "两难", tone: "gold", v: brief.dilemma || "" },
        { k: "决定", tone: "crimson", v: brief.decision || "" },
      ]
    : [
        { k: "目标", tone: "sage", v: brief.goal || "" },
        { k: "冲突", tone: "gold", v: brief.conflict || "" },
        { k: "挫败", tone: "crimson", v: brief.setback || "" },
      ];
  const resume = d.resume || {};
  /* 章内第几场：后端单独给（阶段 X 起 scene_slug 是稳定的 scene_id，不再含位置） */
  const sceneNo = String(resume.scene_no || "");
  const snow = wsAdaptSnow(d);
  const act = (d.snowflake || []).find(s => s.status === "active");
  return {
    slug: resume.chapter_no
      ? `${sceneLabel({ n: resume.chapter_no }, sceneNo ? Number(sceneNo) - 1 : -1)} · ${reactive ? "反应" : "主动"}场景`
      : "",
    scene: resume.scene_title || "",
    snowNow: act ? wsSnowShort(act.step_key, act.label) : "",
    gos,
    resume: {
      ch: resume.chapter_no || "01",
      lines: resume.last_lines || [],
      sceneWords: resume.scene_words || 0,
      pausedAgo: wsAgo(resume.paused_at),
    },
    snow,
    chaps: (d.chapters_recent || []).map(c => ({ n: c.no, t: c.title, s: c.state, pct: c.pct, active: !!c.active })),
  };
}

/* ---- store 内部状态 ---- */
function wsLoadCache() {
  try {
    const cached = JSON.parse(localStorage.getItem(WS_CACHE_LS));
    if (Array.isArray(cached)) {
      const current = cached.filter((work) => !wsIsRetiredDemo(work));
      if (current.length) return current;
    }
  } catch (e) {}
  return [
    {
      id: LOADING_WORK_ID,
      title: "正在打开书架…",
      genre: "", mark: "汐", accent: "slate", sub: "",
      wordsTotal: 0, wordsTarget: 100000, chaptersWritten: 0, chaptersTotal: 0,
      wordsToday: 0, wordsTargetDay: 1000, streak: 0,
      home: { blank: true },
    },
  ];
}

/* 后端已经确认书架为空时使用的只读展示对象。
   它不进入 list/cache，id 为空可让各业务模块沿用现有的“未选择作品”守卫，
   同时避免把 __loading__（请求尚未完成）误当成真实空状态。 */
const WS_EMPTY_WORK = Object.freeze({
  id: "",
  title: "还没有作品",
  genre: "从这里开始",
  mark: "新",
  accent: "slate",
  sub: "",
  wordsTotal: 0,
  wordsTarget: 100000,
  chaptersWritten: 0,
  chaptersTotal: 0,
  wordsToday: 0,
  wordsTargetDay: 1000,
  streak: 0,
  home: { blank: true },
});

let WS_WORKS = wsLoadCache();
let WS_ACTIVE_ID = (() => {
  try {
    const id = localStorage.getItem(WS_ACTIVE_LS);
    // The projects cache is only a disposable rendering shadow. Preserve the
    // selected id even when that cache was cleared; wsRefresh validates it
    // against the authoritative backend list and falls back only if needed.
    if (id && !WS_RETIRED_DEMO_IDS.has(id)) return id;
  } catch (e) {}
  return WS_WORKS[0].id;
})();

const wsSubs = createSubscribers();
const wsStatusSubs = createSubscribers();
const wsRemoteState = {
  projects: { phase: "loading", error: null, updatedAt: null },
  dashboards: {},
};

function wsStatusNotify() {
  wsStatusSubs.notify();
}

function wsSetProjectsStatus(phase, error = null) {
  const visibleError = phase === "loading" && error == null ? wsRemoteState.projects.error : error;
  wsRemoteState.projects = { phase, error: visibleError, updatedAt: phase === "ready" ? Date.now() : wsRemoteState.projects.updatedAt };
  wsStatusNotify();
}

function wsSetDashboardStatus(id, phase, error = null) {
  if (!id) return;
  const previous = wsRemoteState.dashboards[id] || {};
  const visibleError = phase === "loading" && error == null ? previous.error || null : error;
  wsRemoteState.dashboards[id] = { phase, error: visibleError, updatedAt: phase === "ready" ? Date.now() : previous.updatedAt || null };
  wsStatusNotify();
}

function wsSaveCache() {
  try {
    localStorage.setItem(WS_CACHE_LS, JSON.stringify(WS_WORKS));
    if (WS_ACTIVE_ID) localStorage.setItem(WS_ACTIVE_LS, WS_ACTIVE_ID);
    else localStorage.removeItem(WS_ACTIVE_LS);
  } catch (e) {}
}

/* ws:work-changed 的语义是「当前作品换了 / 书架成员变了」：目录、回收站、待办、资料、雪花都拿它
   当「重拉本作品的一切」的信号。字数 / 今日 / 连续天数这类派生统计每次自动保存都会回写，过去同样
   广播它，一次字数汇总就引发约 10 个 GET 和整个应用重渲。现在统计与档案字段（书名 / 主色）的变化
   只通知 WsWorks.subscribe 的订阅者（React 侧的 hook 都走它），不上窗口事件。
   作品 id 第一次从 __loading__ 落定时 id 变了，照旧广播 ws:work-changed。 */
function wsMembership() { return WS_WORKS.filter(w => !w.pending).map(w => w.id).join("\u0001"); }
/* 这个 id 是不是一部还在等后端正式 id 的新建作品 */
function wsIsPending(id) { return !!id && WS_WORKS.some(w => w.id === id && w.pending); }
let wsBroadcast = { id: WS_ACTIVE_ID, members: wsMembership() };

function wsNotify() {
  wsSubs.notify();
  /* 新建作品还在等后端给正式 id（临时作品）：切换器 / 主页已经显示它，但不广播——各 store 听到
     ws:work-changed 就会拿临时 id 去拉目录、回收站、待办、资料、雪花，全是打不中的 GET（审计 F01-06）。
     正式 id 回来（或新建失败回滚）时再按那一刻的状态广播。 */
  if (wsIsPending(WS_ACTIVE_ID)) return;
  const members = wsMembership();
  const switched = WS_ACTIVE_ID !== wsBroadcast.id || members !== wsBroadcast.members;
  wsBroadcast = { id: WS_ACTIVE_ID, members };
  if (switched) emit("ws:work-changed", WS_ACTIVE_ID);
}

function wsToastError(error, fallback) {
  storeAlert(error, fallback);
}

/* —— 拉取列表（启动 / 写后失效重拉）—— */
let wsRefreshing = null;
async function wsRefresh() {
  if (wsRefreshing) return wsRefreshing;
  wsRefreshing = (async () => {
    wsSetProjectsStatus("loading");
    try {
      const data = await apiGet("/api/v2/projects");
      const items = data && Array.isArray(data.items) ? data.items : [];
      const prevHomes = Object.fromEntries(WS_WORKS.map(w => [w.id, w.home]));
      WS_WORKS = items.map(item => wsAdaptProject(item, prevHomes[item.project_id]));
      if (WS_WORKS.length) {
        if (!WS_WORKS.some(w => w.id === WS_ACTIVE_ID)) WS_ACTIVE_ID = WS_WORKS[0].id;
      } else {
        WS_ACTIVE_ID = "";
      }
      wsSaveCache();
      wsNotify();
      if (WS_ACTIVE_ID) wsLoadHome(WS_ACTIVE_ID);
      wsSetProjectsStatus("ready");
    } catch (e) {
      console.warn("[WsWorks] 拉取作品列表失败（保留本地缓存影子）:", e);
      wsSetProjectsStatus("error", toStoreError(e, "作品列表暂时无法连接，当前显示本地缓存"));
    } finally {
      wsRefreshing = null;
    }
  })();
  return wsRefreshing;
}

/* —— 当前作品的 dashboard → home + 派生字段 ——
   同一部作品的 dashboard 在途时，再来的请求共用这一次（和 wsRefreshing 一样）：启动时列表装载完会拉一次，
   主页挂载又会拉一次，开发模式的 StrictMode 还会再挂一次——过去启动就是 2～3 个一模一样的 GET。 */
const wsHomeInflight = new Map();
function wsLoadHome(id) {
  if (!isRealWorkId(id) || wsIsPending(id)) return Promise.resolve();
  const pending = wsHomeInflight.get(id);
  if (pending) return pending;
  const run = wsFetchHome(id).finally(() => { wsHomeInflight.delete(id); });
  wsHomeInflight.set(id, run);
  return run;
}

async function wsFetchHome(id) {
  wsSetDashboardStatus(id, "loading");
  try {
    const d = await apiGet(`/api/v2/projects/${id}/dashboard`);
    const stats = (d && d.stats) || {};
    WS_WORKS = WS_WORKS.map(w => w.id !== id ? w : {
      ...w,
      home: wsAdaptHome(d),
      wordsTotal: stats.words_total ?? w.wordsTotal,
      wordsToday: stats.words_today ?? w.wordsToday,
      streak: stats.streak_days ?? w.streak,
    });
    wsSaveCache();
    wsNotify();
    wsSetDashboardStatus(id, "ready");
  } catch (e) {
    console.warn("[WsWorks] 拉取 dashboard 失败:", e);
    wsSetDashboardStatus(id, "error", toStoreError(e, "主页数据暂时无法连接，当前显示最近一次缓存"));
  }
}

const WsWorks = {
  list: () => WS_WORKS,
  active: () => WS_WORKS.find(w => w.id === WS_ACTIVE_ID) || WS_WORKS[0] || WS_EMPTY_WORK,
  activeId: () => WS_ACTIVE_ID,
  /* 当前作品在后端已经存在时给它的 id；书架还在加载（占位作品）、后端确认书架为空、或新建作品还在等
     正式 id 时给 null。store 拿它决定能不能发请求——临时 id 在后端不存在，发出去全是失败的 GET。 */
  readyId: () => (isRealWorkId(WS_ACTIVE_ID) && !wsIsPending(WS_ACTIVE_ID) ? WS_ACTIVE_ID : null),
  setActive(id) {
    if (id !== WS_ACTIVE_ID && WS_WORKS.some(w => w.id === id)) {
      WS_ACTIVE_ID = id;
      wsSaveCache();
      wsNotify();
      wsLoadHome(id);
    }
  },
  create(data) {
    const body = data || {};
    const title = (body.title || "").trim() || "未命名作品";
    /* 乐观更新：先以临时 id 入列并激活，POST 成功后换正式 project_id，失败回滚 */
    const tempId = "w" + Date.now().toString(36) + Math.floor(Math.random() * 1e3).toString(36);
    const prevActive = WS_ACTIVE_ID;
    const temp = wsAdaptProject({
      project_id: tempId,
      title,
      genre: (body.genre || "").trim() || "未定题材",
      mark: body.mark || Array.from(title)[0] || "新",
      accent: body.accent || "slate",
      synopsis_line: (body.sub || "").trim(),
      target_word_count: Number(body.wordsTarget) || 100000,
      words_target_daily: 1000,
    });
    temp.pending = true;
    WS_WORKS = [...WS_WORKS.filter(w => w.id !== LOADING_WORK_ID), temp];
    WS_ACTIVE_ID = tempId;
    wsSaveCache();
    wsNotify();
    apiPost("/api/v2/projects", {
      title,
      genre: (body.genre || "").trim() || null,
      mark: body.mark || Array.from(title)[0] || null,
      accent: body.accent || "slate",
      synopsis_line: (body.sub || "").trim() || null,
      target_word_count: Number(body.wordsTarget) || 100000,
      words_target_daily: 1000,
      outline_text: ((body.sub || "").trim() || title),
    }).then((result) => {
      const project = result && result.project;
      if (!project) return;
      WS_WORKS = WS_WORKS.map(w => (w.id === tempId ? wsAdaptProject(project, w.home) : w));
      if (WS_ACTIVE_ID === tempId) WS_ACTIVE_ID = project.project_id;
      wsSaveCache();
      wsNotify();
    }).catch((error) => {
      WS_WORKS = WS_WORKS.filter(w => w.id !== tempId);
      if (WS_ACTIVE_ID === tempId) {
        WS_ACTIVE_ID = WS_WORKS.some(w => w.id === prevActive) ? prevActive : ((WS_WORKS[0] && WS_WORKS[0].id) || "");
      }
      wsSaveCache();
      wsNotify();
      wsToastError(error, "创建作品失败，请检查后端服务。");
    });
    return temp;
  },
  remove(id) {
    /* FE-ALIGN P4：整部软删（DELETE /api/v2/projects/{id}）。
       乐观下架 + 失败回滚；回收站条目由后端自动产生。
       还不能发请求的 id（书架还在读的 __loading__ 占位、还在等后端正式 id 的新建作品）不动：
       发出去只会是 /projects/<临时 id> 的 404（与 wsLoadHome 同一条规矩；设置页的调用方早已守住，这里是底线） */
    if (!isRealWorkId(id) || wsIsPending(id)) return;
    if (WS_WORKS.length <= 1) {
      // 审计 P-19：静默 return 让用户不知道为何删不掉——给出明确提示
      storeAlert(null, "至少需要保留一部作品，无法删除最后一部。");
      return;
    }
    const victim = WS_WORKS.find(w => w.id === id);
    if (!victim) return;
    const prevList = WS_WORKS;
    const prevActive = WS_ACTIVE_ID;
    WS_WORKS = WS_WORKS.filter(w => w.id !== id);
    if (WS_ACTIVE_ID === id) WS_ACTIVE_ID = WS_WORKS[0].id;
    wsSaveCache();
    wsNotify();
    apiDelete(`/api/v2/projects/${id}`).then(() => {
      emit("ws:trash-changed");
    }).catch((error) => {
      WS_WORKS = prevList;
      WS_ACTIVE_ID = prevActive;
      wsSaveCache();
      wsNotify();
      wsToastError(error, "删除作品失败。");
    });
  },
  update(id, patch) {
    if (!isRealWorkId(id) || wsIsPending(id)) return; // 同 remove：没有正式 id 的作品不发请求
    const body = patch || {};
    /* 字数/进度类字段改为只读派生（writing-stats / dashboard），不再接受回写 */
    const profile = {};
    if ("title" in body) profile.title = body.title;
    if ("genre" in body) profile.genre = body.genre;
    if ("sub" in body) profile.synopsis_line = body.sub;
    if ("mark" in body) profile.mark = body.mark;
    if ("accent" in body) profile.accent = body.accent;
    if ("wordsTarget" in body) profile.target_word_count = Number(body.wordsTarget) || null;
    if ("wordsTargetDay" in body) profile.words_target_daily = Number(body.wordsTargetDay) || null;
    if ("chaptersTotal" in body) profile.target_chapter_count = Number(body.chaptersTotal) || null;
    if (!Object.keys(profile).length) return;
    const before = WS_WORKS.find(w => w.id === id);
    if (!before) return;
    /* 乐观更新（仅档案字段）→ PATCH → 失败回滚 */
    const optimistic = { ...before };
    if ("title" in body) optimistic.title = body.title;
    if ("genre" in body) optimistic.genre = body.genre;
    if ("sub" in body) optimistic.sub = body.sub;
    if ("mark" in body) optimistic.mark = body.mark;
    if ("accent" in body) optimistic.accent = body.accent;
    if ("wordsTarget" in body) optimistic.wordsTarget = Number(body.wordsTarget) || optimistic.wordsTarget;
    if ("wordsTargetDay" in body) optimistic.wordsTargetDay = Number(body.wordsTargetDay) || optimistic.wordsTargetDay;
    if ("chaptersTotal" in body) optimistic.chaptersTotal = Number(body.chaptersTotal) || optimistic.chaptersTotal;
    WS_WORKS = WS_WORKS.map(w => (w.id === id ? optimistic : w));
    wsSaveCache();
    wsNotify();
    apiPatch(`/api/v2/projects/${id}/profile`, profile).catch((error) => {
      WS_WORKS = WS_WORKS.map(w => (w.id === id ? before : w));
      wsSaveCache();
      wsNotify();
      wsToastError(error, "保存作品档案失败。");
    });
  },
  subscribe(fn) { return wsSubs.subscribe(fn); },
  subscribeStatus(fn) { return wsStatusSubs.subscribe(fn); },
  status(id) {
    const workId = id || WS_ACTIVE_ID;
    return {
      projects: { ...wsRemoteState.projects },
      dashboard: { ...(wsRemoteState.dashboards[workId] || { phase: "idle", error: null, updatedAt: null }) },
    };
  },
  retry(scope = "dashboard", id) {
    if (scope === "projects") return wsRefresh();
    return wsLoadHome(id || WS_ACTIVE_ID);
  },
  /* —— FE-ALIGN 内部接缝（非契约面）：统计派生字段的只读注入（重读书架用公开的 retry("projects")） —— */
  __applyDerived(id, fields) {
    const allowed = {};
    if ("wordsTotal" in (fields || {})) allowed.wordsTotal = fields.wordsTotal;
    if ("wordsToday" in (fields || {})) allowed.wordsToday = fields.wordsToday;
    if ("streak" in (fields || {})) allowed.streak = fields.streak;
    if ("chaptersWritten" in (fields || {})) allowed.chaptersWritten = fields.chaptersWritten;
    if (!Object.keys(allowed).length) return;
    WS_WORKS = WS_WORKS.map(w => (w.id === id ? { ...w, ...allowed } : w));
    wsSaveCache();
    wsNotify();
  },
};

/* ---- per-work storage namespace ----
   Functional isolation: every module persists under a key suffixed with
   the active work id, so edits in one work never leak into another.
   接 API 后业务数据不再经过它，仅剩 UI 偏好键在用（Phase 8 收口）。
   风格参考 stays global and does NOT call this — intentionally shared. */
function wsKey(base) { return base + "::" + WS_ACTIVE_ID; }

/* ---- React hooks ---- */
function useActiveWork() {
  useStoreTick((fn) => WsWorks.subscribe(fn));
  return WsWorks.active();
}
function useWorks() {
  useStoreTick((fn) => WsWorks.subscribe(fn));
  return WsWorks.list();
}
function useWorksStatus(id) {
  useStoreTick((fn) => WsWorks.subscribeStatus(fn));
  return WsWorks.status(id);
}

/* 外壳只关心当前作品「是谁」：id / 书名 / 题材 / 印记 / 主色。字数统计的回写不该让整棵视图树重渲，
   所以快照只在这几个字段变化时换引用（useSyncExternalStore 以引用相等判断是否重渲）。 */
let wsIdentity = null;
function wsIdentitySnapshot() {
  const w = WsWorks.active();
  const next = { id: w.id, title: w.title, genre: w.genre, mark: w.mark, accent: w.accent };
  if (!wsIdentity || Object.keys(next).some(k => wsIdentity[k] !== next[k])) wsIdentity = next;
  return wsIdentity;
}
function wsSubscribeWorks(fn) { return WsWorks.subscribe(fn); }
function useActiveWorkIdentity() {
  return React.useSyncExternalStore(wsSubscribeWorks, wsIdentitySnapshot, wsIdentitySnapshot);
}

/* 启动即拉一次后端列表（缓存影子先行渲染） */
wsRefresh();

Object.assign(window, { WsWorks, useActiveWork, useWorks, useWorksStatus, wsKey });

/* ESM 导出（window.* 赋值过渡期保留；useActiveWorkIdentity 是新接口，只走 ESM） */
export { WsWorks, useActiveWork, useActiveWorkIdentity, useWorks, useWorksStatus, wsKey };
