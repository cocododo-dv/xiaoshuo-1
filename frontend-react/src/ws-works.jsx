import React from "react";
import { apiDelete, apiGet, apiPatch, apiPost } from "./lib/client.js";
import { createSubscribers, storeAlert, useStoreTick } from "./lib/store-utils.js";

/* global window */
/* ==========================================================
   WsWorks — 多作品管理（FE-ALIGN Phase 2：后端为唯一真相源）
   · 列表/创建/档案更新 走 /api/v2/projects（信封契约见 lib/client.js）
   · 当前作品 id 仍存 localStorage（UI 状态）
   · get/list 保持同步语义：启动用本地缓存影子即时渲染，API 返回后失效更新
   · 字数/进度字段（wordsTotal/wordsToday/streak/chaptersWritten）只读派生：
     由 writing-stats / dashboard 填充，update() 不再回写（原 catPushTotals 回写路径删除）
   · 公开方法签名/订阅语义/ws:work-changed 事件 与原型完全一致（契约附录）
   ========================================================== */

const WS_WORKS_LS = "ws_works_created_v1";   // 旧 localStorage 时代的本地作品（一次性上行迁移源）
const WS_ACTIVE_LS = "ws_active_work_v1";    // 当前作品 id（UI 状态，长期保留 localStorage）
const WS_CACHE_LS = "ws_works_cache_v1";     // 列表启动缓存（API 真相的本地影子，仅为同步 list()）
const WS_MIGRATED_LS = "ws_migrated_v1";     // 一次性迁移标记
const WS_RETIRED_DEMO_IDS = new Set(["tide", "salt"]);

function wsIsRetiredDemo(work) {
  return !!(
    work
    && (WS_RETIRED_DEMO_IDS.has(String(work.id || work.project_id || "")) || work.isDemo === true || work.is_demo === true)
  );
}

/* —— 问候语：前端按时段生成（契约附录：不必入库）—— */
function wsGreetNow() {
  const h = new Date().getHours();
  if (h < 5) return "夜深了 · 写完这一段就休息";
  if (h < 11) return "早上好 · 新的一页刚刚展开";
  if (h < 14) return "中午好 · 喝口水再继续";
  if (h < 18) return "下午好 · 光线正适合写作";
  return "晚上好 · 夜里适合沉下心来";
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

/* 雪花步骤短名（原型主页 chips 的文案口径） */
const WS_SNOW_SHORT = {
  book_brief: "读者定位",
  one_sentence_summary: "一句话",
  one_paragraph_summary: "一段话",
  character_sheets: "角色摘要",
  short_synopsis: "一页梗概",
  character_synopses: "角色背景",
  long_synopsis: "长篇大纲",
  character_bibles: "角色全档案",
  scene_list: "场景列表",
  scene_details: "场景规划",
};

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
    greet: wsGreetNow(),
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
  if (!d || (!d.resume && !(d.chapters_recent || []).length)) return { blank: true };
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
        { k: "阻碍", tone: "gold", v: brief.conflict || "" },
        { k: "挫折", tone: "crimson", v: brief.setback || "" },
      ];
  const resume = d.resume || {};
  /* 章内第几场：后端单独给（阶段 X 起 scene_slug 是稳定的 scene_id，不再含位置）；
     旧后端没有 scene_no 时退回从位置式 slug（ch08s3）里读。 */
  const legacyNo = /^ch\d+s(\d+)$/.exec(resume.scene_slug || "");
  const sceneNo = String(resume.scene_no || (legacyNo ? legacyNo[1] : "") || "");
  const snow = (d.snowflake || []).map(s => ({ name: WS_SNOW_SHORT[s.step_key] || s.label, s: s.status }));
  const act = (d.snowflake || []).find(s => s.status === "active");
  return {
    slug: resume.chapter_no
      ? `CH ${resume.chapter_no} · SC ${sceneNo.padStart(2, "0")} · ${reactive ? "反应" : "主动"}场景`
      : "",
    scene: resume.scene_title || "",
    snowNow: act ? (WS_SNOW_SHORT[act.step_key] || act.label) : "",
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
      id: "__loading__",
      title: "正在打开书架…",
      genre: "", mark: "汐", accent: "slate", sub: "",
      greet: wsGreetNow(),
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
  greet: "欢迎回来",
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

function wsErrorShape(error, fallback) {
  return {
    code: (error && error.code) || (error && error.status) || "NETWORK_ERROR",
    message: (error && error.message) || fallback,
    offline: typeof navigator !== "undefined" && navigator.onLine === false,
  };
}

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

function wsNotify() {
  wsSubs.notify();
  try { window.dispatchEvent(new CustomEvent("ws:work-changed", { detail: WS_ACTIVE_ID })); } catch (e) {}
}

function wsToastError(error, fallback) {
  storeAlert(error, fallback);
}

/* —— 一次性上行迁移：旧 localStorage 作品 → POST 后端 —— */
async function wsMigrateLegacy() {
  try {
    if (localStorage.getItem(WS_MIGRATED_LS)) return false;
    const raw = localStorage.getItem(WS_WORKS_LS);
    const legacy = raw ? JSON.parse(raw) : null;
    let migrated = false;
    let failed = 0;
    if (Array.isArray(legacy)) {
      for (const w of legacy) {
        if (!w || !w.title || wsIsRetiredDemo(w)) continue;
        try {
          await apiPost("/api/v2/projects", {
            title: w.title,
            genre: w.genre || null,
            mark: w.mark || null,
            accent: w.accent || null,
            synopsis_line: w.sub || null,
            target_word_count: Number(w.wordsTarget) || null,
            words_target_daily: Number(w.wordsTargetDay) || null,
            outline_text: ((w.sub || w.title || "").trim() || "（迁移自本地草稿）"),
          });
          migrated = true;
        } catch (e) {
          // 单部失败不阻塞其余；旧键保留（Phase 8 清理）
          failed += 1;
          console.warn("[WsWorks] 迁移本地作品失败:", w.title, e);
        }
      }
    }
    // 审计 P-19：有失败就不落"已迁移"标记，下次启动重试失败的作品；
    // 全部成功（或无可迁移项）才封口。
    if (failed === 0) {
      localStorage.setItem(WS_MIGRATED_LS, new Date().toISOString());
    } else {
      console.warn(`[WsWorks] ${failed} 部作品迁移失败，保留旧键待下次启动重试。`);
    }
    return migrated;
  } catch (e) {
    return false;
  }
}

/* —— 拉取列表（启动 / 写后失效重拉）—— */
let wsRefreshing = null;
async function wsRefresh() {
  if (wsRefreshing) return wsRefreshing;
  wsRefreshing = (async () => {
    wsSetProjectsStatus("loading");
    try {
      const migrated = await wsMigrateLegacy();
      let data = await apiGet("/api/v2/projects");
      if (migrated) data = await apiGet("/api/v2/projects");
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
      wsSetProjectsStatus("error", wsErrorShape(e, "作品列表暂时无法连接，当前显示本地缓存"));
    } finally {
      wsRefreshing = null;
    }
  })();
  return wsRefreshing;
}

/* —— 当前作品的 dashboard → home + 派生字段 —— */
async function wsLoadHome(id) {
  if (!id || id === "__loading__") return;
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
    wsSetDashboardStatus(id, "error", wsErrorShape(e, "主页数据暂时无法连接，当前显示最近一次缓存"));
  }
}

const WsWorks = {
  list: () => WS_WORKS,
  active: () => WS_WORKS.find(w => w.id === WS_ACTIVE_ID) || WS_WORKS[0] || WS_EMPTY_WORK,
  activeId: () => WS_ACTIVE_ID,
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
    WS_WORKS = [...WS_WORKS.filter(w => w.id !== "__loading__"), temp];
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
       乐观下架 + 失败回滚；回收站条目由后端自动产生。 */
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
      try { window.dispatchEvent(new CustomEvent("ws:trash-changed")); } catch (e) {}
    }).catch((error) => {
      WS_WORKS = prevList;
      WS_ACTIVE_ID = prevActive;
      wsSaveCache();
      wsNotify();
      wsToastError(error, "删除作品失败。");
    });
  },
  update(id, patch) {
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
  /* FE-ALIGN P4：摘除「种子不可删」前端限制。视图以 isSeed 作为删除门闩，故恒为 false。 */
  isSeed: () => false,
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
  /* —— FE-ALIGN 内部接缝（非契约面）：统计派生字段的只读注入 + 手动刷新 —— */
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
  __refresh: wsRefresh,
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

/* 启动即拉一次后端列表（缓存影子先行渲染） */
wsRefresh();

Object.assign(window, { WsWorks, useActiveWork, useWorks, useWorksStatus, wsKey });

/* ESM 导出（window.* 赋值过渡期保留） */
export { WsWorks, useActiveWork, useWorks, useWorksStatus, wsKey };
