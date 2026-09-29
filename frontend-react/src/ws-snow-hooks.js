import React from "react";
import { WsWorks, wsKey } from "./ws-works.jsx";
import { SnowSync } from "./ws-snow-sync.jsx";
import {
  s2DefaultDrafts, s2DefaultStates, s2MergeChecks, s2MergeScaffolds, s2PovSettleEntry,
} from "./ws-snow-model.js";
import { emit, useWindowEvents as useSnowEvents } from "./lib/events.js";
import { readyWorkId } from "./lib/ready-work.js";

/* ==========================================================
   雪花工作台的环境与状态钩子
   ----------------------------------------------------------
   当前作品、本机缓存（ws_snow_state_v2::<作品>）、界面偏好、窗口事件订阅，
   以及工作台的两块状态：十步内容（含防抖落盘）、同步层镜像（同步状态 / 后端健康 / 待同步 / 要点）。
   AI 生成在 ws-snow-generation.js。
   ========================================================== */

const { useState, useEffect, useRef, useCallback } = React;

/* 能拿去发请求的当前作品 id：书架还在加载、新建作品还在等后端给正式 id 时是 null（规则在 WsWorks.readyId） */
export function activeWorkId() {
  return readyWorkId(WsWorks);
}

/* ---- 本机缓存（按作品分键） ----
   v2：重构前的旧代码未按作品门控种子，曾把种子原样持久化到其它作品的 v1 键下（污染）。
   v2 起换键；旧 v1 键保留不删。 */
const S2_CACHE_PREFIX = "ws_snow_state_v2";
export const s2Key = () => (wsKey ? wsKey(S2_CACHE_PREFIX) : S2_CACHE_PREFIX);
/* 某部作品的缓存键，和它的反推（视图挂载时冻结的键 → 作品 id） */
export const s2KeyFor = (workId) => `${S2_CACHE_PREFIX}::${workId}`;
export const s2WorkIdOfKey = (key) => String(key || "").split("::")[1] || "";
export function s2Load(key) { try { return JSON.parse(localStorage.getItem(key || s2Key())) || {}; } catch (e) { return {}; } }

/* ---- 界面偏好（只存本机，不同步，读写都容错：隐私窗口 / 存储被禁时照常工作） ---- */
export const S2_PREF_KEYS = {
  /* 右栏「本步上下文」在宽屏上可以收起：按「表格步 / 表单步」两组记住（09 / 10 默认收起） */
  rail: "ws_snow_rail_v1",
  /* 写作指引：每一步第一次打开时展开，之后默认收起 */
  guideSeen: "ws_snow_guide_seen_v1",
  /* 第 10 步「场景卡细节」收起与否 */
  planDetails: "ws_snow_plan_details_v1",
  /* 每部作品上次看的是哪一步（十步都确认过时回到这里） */
  lastStep: "ws_snow_last_step_v1",
};
export function s2LoadUiPref(key) { try { return JSON.parse(localStorage.getItem(key)) || {}; } catch (e) { return {}; } }
export function s2SaveUiPref(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) {} }

/* ---- 小钩子 ---- */

/* 窗口事件订阅：{ 事件名: 处理函数 }。实现是 lib/events.js 的 useWindowEvents（只按事件名挂一次，
   处理函数每次渲染换成最新的）；雪花各件沿用这个名字。 */
export { useSnowEvents };

/* SnowSync 的通知（SnowSync.subscribe：fn(kind, detail)，kind 是 health / resync / brief / sync-state / hydrated /
   catalog-synced）：{ kind: 处理函数(detail) }。只订阅一次，处理函数每次渲染换成最新的。
   以前视图听的是同一条消息的 ws:snow-* 窗口事件（同步层照发，给还在听的别的视图）。 */
export function useSnowNotices(handlers) {
  const ref = useRef(handlers);
  ref.current = handlers;
  useEffect(() => {
    let off = null;
    try {
      off = SnowSync.subscribe((kind, detail) => {
        const fn = ref.current[kind];
        if (fn) fn(detail);
      });
    } catch (e) { /* 同步层不可用（单测桩）：没有通知可听 */ }
    return () => { if (typeof off === "function") off(); };
  }, []);
}

/* 身份不变、行为总是最新的回调：传给 memo 过的子组件时，不因父组件重渲染而让子组件跟着重渲染 */
export function useStableCallback(fn) {
  const ref = useRef(fn);
  ref.current = fn;
  return useCallback((...args) => ref.current(...args), []);
}

export function useSnowMedia(query) {
  const read = () => {
    try {
      const mm = window.matchMedia; // 先取出来再判断类型：直接写相等比较会被 tooling-independence 的「写 window」规则误判为赋值
      return typeof mm === "function" && mm.call(window, query).matches;
    } catch (e) { return false; }
  };
  const [matches, setMatches] = useState(read);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const mq = window.matchMedia(query);
    const on = () => setMatches(mq.matches);
    on();
    if (mq.addEventListener) mq.addEventListener("change", on); else if (mq.addListener) mq.addListener(on);
    return () => { if (mq.removeEventListener) mq.removeEventListener("change", on); else if (mq.removeListener) mq.removeListener(on); };
  }, [query]);
  return matches;
}

/* ---- 十步内容：本机缓存是写穿 SnowSync 的那一层 ----
   myKey 在挂载时冻结（作品切换时整张视图会按作品重挂），卸载时的落盘写回正确的作品。
   落盘 450ms 防抖，写完广播 ws:snow-saved——SnowSync 据此比对、上行。持久化的形状
   {drafts, scaffolds, checks, states, history, _t} 由同步层读取，不能变。
   读缓存（挂载、水合之后重读）时脚手架经 s2MergeScaffolds 归一：第 10 步 plan 里旧存的视角收归 09；
   换了人的场在历史最前面记一条「视角统一到 09」（s2PovSettleEntry），不悄悄丢掉作者在旧版第 10 步挑过的视角。 */
function readSnowCache(key) {
  const s = s2Load(key);
  const entry = s2PovSettleEntry(s.scaffolds, s.drafts);
  return entry ? { ...s, history: [entry, ...(Array.isArray(s.history) ? s.history : [])] } : s;
}
export function useSnowDocument(myKey, workId) {
  const initialRef = useRef(null);
  if (initialRef.current == null) initialRef.current = readSnowCache(myKey);
  const saved = initialRef.current;
  const [drafts, setDrafts] = useState(() => ({ ...s2DefaultDrafts(), ...(saved.drafts || {}) }));
  const [scaffolds, setScaffolds] = useState(() => s2MergeScaffolds(saved.scaffolds));
  const [checks, setChecks] = useState(() => s2MergeChecks(saved.checks));
  const [states, setStates] = useState(() => ({ ...s2DefaultStates(), ...(saved.states || {}) }));
  const [history, setHistory] = useState(() => saved.history || []);
  const [savedAt, setSavedAt] = useState(saved._t || null);
  const latestRef = useRef(null);
  latestRef.current = { drafts, scaffolds, checks, states, history };

  const markLocalFailure = (error) => { try { SnowSync.markLocalFailure(error, workId); } catch (ignored) {} };
  const writeNow = () => {
    const now = Date.now();
    localStorage.setItem(myKey, JSON.stringify({ ...latestRef.current, _t: now }));
    emit("ws:snow-saved", myKey);
    return now;
  };

  /* persist to localStorage (debounced) */
  useEffect(() => {
    const id = setTimeout(() => {
      try { setSavedAt(writeNow()); } catch (e) { markLocalFailure(e); }
    }, 450);
    return () => clearTimeout(id);
  }, [drafts, scaffolds, checks, states, history]);

  /* flush latest state on unmount (leaving for another view) so readers see fresh truth */
  useEffect(() => () => {
    try { writeNow(); } catch (e) { markLocalFailure(e); }
  }, []);

  useSnowEvents({
    /* 下一跳握手：分章预览发来 flush 请求时，不等 450ms 防抖，立即把当前内存态落盘
       并触发 SnowSync 上行。这样“确认本步 → 立刻整理”不会读取到上一版后端闸门。 */
    "ws:snow-flush-local": (event) => {
      const requested = event && event.detail && event.detail.workId;
      if (requested && requested !== workId) return;
      try { setSavedAt(writeNow()); } catch (e) { markLocalFailure(e); }
    },
  });
  useSnowNotices({
    /* 后端水合（SnowSync）落盘后重读缓存，刷新本组件状态 */
    hydrated: (hydratedWorkId) => {
      if (!hydratedWorkId || myKey !== s2KeyFor(hydratedWorkId)) return;
      const s = readSnowCache(myKey);
      setDrafts({ ...s2DefaultDrafts(), ...(s.drafts || {}) });
      setScaffolds(s2MergeScaffolds(s.scaffolds));
      setChecks(s2MergeChecks(s.checks));
      setStates({ ...s2DefaultStates(), ...(s.states || {}) });
      setHistory(s.history || []); // 跨会话 journal（无 snap 条目天然只读）
    },
  });

  return { drafts, setDrafts, scaffolds, setScaffolds, checks, setChecks, states, setStates, history, setHistory, savedAt, latestRef };
}

/* ---- 同步层镜像：同步状态、后端 per-step 健康、物化后待同步的场、要点镜像的版本号 ----
   SnowSync 的健康表是原地改写的（同一个对象），这里每次事件都浅拷一份，
   让用它做 props 的子组件（步骤列表、右栏）能按引用判断「变了没有」。 */
const IDLE_SYNC = { phase: "idle", error: null };
const NO_RESYNC = { pendingCount: 0, pendingScenes: [] };
export function useSnowSyncMirror(workId) {
  const readSync = () => { try { return SnowSync.syncState(workId) || IDLE_SYNC; } catch (e) { return IDLE_SYNC; } };
  const readHealth = () => { try { return { ...(SnowSync.health(workId) || {}) }; } catch (e) { return {}; } };
  const readResync = () => { try { return SnowSync.resyncStatus(workId) || NO_RESYNC; } catch (e) { return NO_RESYNC; } };
  const [syncState, setSyncState] = useState(readSync);
  const [health, setHealth] = useState(readHealth);
  const [resync, setResync] = useState(readResync);
  const [briefTick, setBriefTick] = useState(0);
  const onSyncState = (detail) => {
    const d = detail || {};
    if (d.workId && d.workId !== workId) return;
    try { setSyncState(SnowSync.syncState(workId) || d.state || IDLE_SYNC); } catch (e) {}
  };
  useSnowEvents({
    "ws:work-changed": (event) => { onSyncState(event && event.detail); setHealth(readHealth()); setResync(readResync()); },
  });
  useSnowNotices({
    "sync-state": onSyncState,
    health: () => { setHealth(readHealth()); setBriefTick(t => t + 1); },
    hydrated: () => { setHealth(readHealth()); setResync(readResync()); },
    resync: () => setResync(readResync()),
    brief: () => setBriefTick(t => t + 1),
  });
  return { syncState, health, resync, briefTick };
}
