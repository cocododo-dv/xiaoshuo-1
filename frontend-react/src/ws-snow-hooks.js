import React from "react";
import { WsWorks, wsKey } from "./ws-works.jsx";
import { SnowSync } from "./ws-snow-sync.jsx";
import { apiPost } from "./lib/client.js";
import {
  S2_STEPS, S2_BE_KEY, s2DefaultDrafts, s2DefaultStates, s2MergeChecks, s2MergeScaffolds, s2NormalizeState,
} from "./ws-snow-model.js";

/* ==========================================================
   雪花工作台的环境与状态钩子
   ----------------------------------------------------------
   当前作品、本机缓存（ws_snow_state_v2::<作品>）、界面偏好、窗口事件订阅，
   以及工作台的三块状态：十步内容（含防抖落盘）、同步层镜像（同步状态 / 后端健康 / 待同步 / 要点）、
   AI 生成（忙态、入口、错误按步骤隔离）。
   ========================================================== */

const { useState, useEffect, useRef, useCallback } = React;

/* 当前作品 id；作品层还没就绪时是 null（以前这段 try/catch 在视图里抄了十几份） */
export function activeWorkId() {
  try { return (WsWorks && WsWorks.activeId && WsWorks.activeId()) || null; } catch (e) { return null; }
}

/* ---- 本机缓存（按作品分键） ----
   v2：重构前的旧代码未按作品门控种子，曾把种子原样持久化到其它作品的 v1 键下（污染）。
   v2 起换键；旧 v1 键保留不删。 */
export const s2Key = () => (wsKey ? wsKey("ws_snow_state_v2") : "ws_snow_state_v2");
export function s2Load(key) { try { return JSON.parse(localStorage.getItem(key || s2Key())) || {}; } catch (e) { return {}; } }

/* 主页速览：读同一份持久化真相，而非静态拷贝（window.s2StepSummary，smoke-f3 读它） */
export function s2StepSummary() {
  try {
    const saved = s2Load();
    const states = { ...s2DefaultStates(), ...(saved.states || {}) };
    const steps = S2_STEPS.map(s => {
      let v = states[s.key] || "todo";
      if (v === "skip") v = "warn";
      return { name: s.name, s: v };
    });
    const cur = S2_STEPS.find(s => { const v = states[s.key] || "todo"; return v !== "done" && v !== "skip"; });
    return { steps, now: cur ? `${cur.name} · 第 ${cur.num} 步` : "十步已全部确认" };
  } catch (e) { return null; }
}

/* 场景运行提示词等只读消费者使用：把当前作品的雪花状态物化为独立快照。
   该快照不是项目备份，也不承担跨作品恢复。 */
export function s2ExportState() {
  try { return s2NormalizeState(s2Load()); } catch (e) { return null; }
}

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

/* 窗口事件订阅：{ 事件名: 处理函数 }。只按事件名挂一次，处理函数每次渲染换成最新的（不再随依赖反复拆装）。 */
export function useSnowEvents(handlers) {
  const ref = useRef(handlers);
  ref.current = handlers;
  const names = Object.keys(handlers).sort().join("|");
  useEffect(() => {
    const bound = (names ? names.split("|") : []).map(name => [name, (event) => {
      const handler = ref.current && ref.current[name];
      if (handler) handler(event);
    }]);
    bound.forEach(([name, fn]) => window.addEventListener(name, fn));
    return () => bound.forEach(([name, fn]) => window.removeEventListener(name, fn));
  }, [names]);
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
   {drafts, scaffolds, checks, states, history, _t} 由同步层读取，不能变。 */
export function useSnowDocument(myKey, workId) {
  const initialRef = useRef(null);
  if (initialRef.current == null) initialRef.current = s2Load(myKey);
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
    window.dispatchEvent(new CustomEvent("ws:snow-saved", { detail: myKey }));
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
    /* 后端水合（SnowSync）落盘后重读缓存，刷新本组件状态 */
    "ws:snow-hydrated": (event) => {
      if (!event || !event.detail || myKey !== "ws_snow_state_v2::" + event.detail) return;
      const s = s2Load(myKey);
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
  const readHealth = () => { try { return { ...(SnowSync.health() || {}) }; } catch (e) { return {}; } };
  const readResync = () => { try { return SnowSync.resyncStatus() || NO_RESYNC; } catch (e) { return NO_RESYNC; } };
  const [syncState, setSyncState] = useState(readSync);
  const [health, setHealth] = useState(readHealth);
  const [resync, setResync] = useState(readResync);
  const [briefTick, setBriefTick] = useState(0);
  const onSyncState = (event) => {
    const detail = (event && event.detail) || {};
    if (detail.workId && detail.workId !== workId) return;
    try { setSyncState(SnowSync.syncState(workId) || detail.state || IDLE_SYNC); } catch (e) {}
  };
  useSnowEvents({
    "ws:snow-sync-state": onSyncState,
    "ws:work-changed": (event) => { onSyncState(event); setHealth(readHealth()); setResync(readResync()); },
    "ws:snow-health": () => { setHealth(readHealth()); setBriefTick(t => t + 1); },
    "ws:snow-hydrated": () => { setHealth(readHealth()); setResync(readResync()); },
    "ws:snow-resync": () => setResync(readResync()),
    "ws:snow-brief": () => setBriefTick(t => t + 1),
  });
  return { syncState, health, resync, briefTick };
}

/* ---- AI 生成：忙态 / 入口 / 错误按步骤隔离 ----
   全局布尔会让「生成中…」在所有步骤的按钮上亮起，并挡住其它步骤发起自己的生成；
   「生成中…」也只该亮在被点的那个入口上（{kind, turnId, index, focused, id}），其余按钮只禁用、不改文案。
   env 是视图每次渲染更新的 ref：{ activeKey, active, data, drafts, scaffolds, setScaffolds, setDrafts,
   setTabFor, pushHist, snapNow, showToast, setCoachHist, sceneLabel }——调用时读最新值，
   异步回来后的写回一律用调用那一刻的步骤键。 */
export function useSnowGeneration(env) {
  const [structBusyMap, setStructBusyMap] = useState({});
  const [genTargetMap, setGenTargetMap] = useState({});
  const [genErrMap, setGenErrMap] = useState({});   // 本步最近一次 AI 动作的错误（编辑页 AI 工具条显示）
  const [dirBusyMap, setDirBusyMap] = useState({});  // 「先看 3 个方向」进行中

  /* 结构化生成通用通道：按方向生成 / AI 生成本步 / 整表生成 / 全部补全 / 单场补全共用——
     后端 generate（每步专用模板 + 权威上游材料 + 压力诊断 + 本步要点 + 空字段定向重试；require_llm 保证
     LLM 不可用时诚实报错，绝不落一版启发式草稿冒充）→ 整步规范草稿经 applyServerStep 反推回脚手架。
     focusRow 时只回写焦点场的规划（其余场保留本地态，防止未上行编辑被服务端旧值盖掉）。
     本步要点默认带入（服务端 use_direction_brief 缺省 true）：不想让某条要点约束生成，撤下那条即可。 */
  const structuredGenerate = async ({ direction = null, directionKind = null, directionTurnId = null, directionIndex = null, focus = null, focusRow = null, focusChars = null, focusChar = null, source = null, target = null, histAction, histNote, doneAction, doneNote, toastOk, toastFail, switchTab = false, fallbackText = null }) => {
    const e = env.current;
    const key = e.activeKey, step = e.active;
    if (structBusyMap[key]) return false;
    setGenErrMap(prev => ({ ...prev, [key]: null }));
    setStructBusyMap(prev => ({ ...prev, [key]: true }));
    setGenTargetMap(prev => ({ ...prev, [key]: target || { kind: source || "generate" } }));
    e.pushHist(histAction, `${step.num} ${step.name}${histNote ? " · " + histNote : ""} · 生成前留底`, "我", e.snapNow(key), key);
    try {
      const workId = activeWorkId();
      const beKey = S2_BE_KEY[key];
      if (!workId || !beKey) throw new Error("作品尚未就绪，稍后重试");
      const body = { require_llm: true, source: source || (focus ? "fe_scene_focus_ai" : focusChars ? "fe_char_focus_ai" : (direction ? "fe_candidate_adopt" : "fe_scaffold_ai")) };
      if (direction) body.direction_text = direction;
      if (direction && directionKind) body.direction_kind = directionKind; // 教练回复 vs 方向正文：用法说明不同
      // 阶段 U：方向来自教练日志里的哪一回合——服务端在回合上记「已按此生成」、在 health.direction 记出处
      if (direction && directionTurnId) {
        body.direction_turn_id = directionTurnId;
        if (directionIndex != null) body.direction_index = directionIndex;
      }
      if (focus) body.focus_scene_refs = focus;
      if (focusChars) body.focus_character_refs = focusChars;
      /* 本地最新规范草稿随请求带入（与上行 PATCH 同源）：消除「刚加的角色/场
         还没自动保存上行，模型看不到、合并后被丢掉」的竞态 */
      try {
        const dOv = SnowSync.pushCanon(key, { drafts: e.drafts, scaffolds: e.scaffolds }, workId);
        if (dOv && Object.keys(dOv).length) body.draft_override = dOv;
      } catch (ignored) {}
      const res = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}/generate`, body);
      if (!res || !res.step) throw new Error("生成回包缺少 step");
      try { if (res.workspace) SnowSync.captureBriefs(workId, res.workspace); } catch (ignored) {}
      // 回包的教练历史带「已按此生成」标记（adoption）——方向卡 / 回复上的徽章据此更新
      if (res.workspace && Array.isArray(res.workspace.assistant_history)) env.current.setCoachHist(res.workspace.assistant_history);
      let fe = null;
      try { fe = SnowSync.applyServerStep(workId, key, res.step); } catch (ignored) {}
      const { setScaffolds, setDrafts } = env.current;
      if (fe && fe.scaffold) {
        if (focusRow && key === "planning") {
          const fePlans = (fe.scaffold || {}).plans || {};
          setScaffolds(prev => {
            const cur = prev[key] || {};
            return { ...prev, [key]: { ...cur, sel: focusRow, plans: { ...(cur.plans || {}), [focusRow]: fePlans[focusRow] || (cur.plans || {})[focusRow] || {} } } };
          });
        } else if (focusChar && (key === "characters" || key === "backstory" || key === "profile")) {
          /* 单角色定向：只把焦点角色的生成结果并回本地，其余角色保持本地态
             （与 planning 的 focusRow 同一防线：未上行编辑不被服务端旧值盖掉） */
          const feChars = (fe.scaffold || {}).chars || {};
          setScaffolds(prev => {
            const cur = prev[key] || {};
            return { ...prev, [key]: { ...cur, sel: focusChar, chars: { ...(cur.chars || {}), [focusChar]: feChars[focusChar] || (cur.chars || {})[focusChar] || {} } } };
          });
        } else {
          setScaffolds(prev => ({ ...prev, [key]: fe.scaffold }));
        }
        setDrafts(prev => ({ ...prev, [key]: "" })); // 脚手架即唯一内容源，避免旧自由草稿盖住它
      } else if (fe && fe.text != null) {
        setDrafts(prev => ({ ...prev, [key]: fe.text }));
      } else if (fallbackText != null) {
        setDrafts(prev => ({ ...prev, [key]: fallbackText })); // 兜底：至少落自由草稿
      }
      if (switchTab) env.current.setTabFor(key, "edit");
      /* 分批深化中途失败等半成品：后端把事实放在 health.generation_notice，
         这里必须把绿色的「已生成」降级成警告——否则作者以为整表都做完了 */
      const notice = ((res.step || {}).health || {}).generation_notice;
      const noticeMsg = notice && String(notice.message || "").trim();
      env.current.pushHist(doneAction || histAction,
        `${step.num} ${step.name}${doneNote ? " · " + doneNote : ""}${noticeMsg ? " · " + noticeMsg : ""}`, "AI", null, key);
      if (noticeMsg) {
        setGenErrMap(prev => ({ ...prev, [key]: noticeMsg }));
        env.current.showToast(noticeMsg.slice(0, 60), "crimson");
      } else {
        env.current.showToast(toastOk || "已生成 · 可回滚", "gold");
      }
      return true;
    } catch (err) {
      setGenErrMap(prev => ({ ...prev, [key]: (err && err.message) || "生成失败，请稍后重试" }));
      env.current.showToast(toastFail || ("生成失败：" + ((err && err.message) || "稍后重试").slice(0, 40)), "crimson");
      return false;
    } finally {
      setStructBusyMap(prev => ({ ...prev, [key]: false }));
      setGenTargetMap(prev => ({ ...prev, [key]: null }));
    }
  };

  /* 「先看 3 个方向」（阶段 U）：走后端节点 snowflake_step_candidates，结果是教练日志里的一种回合
     （turn_kind=candidates），回包带整条教练历史；底稿与整步生成同源（draft_override），作者在输入框里
     写的要求作为 ask 一并带上；第 10 步只针对选中的那一场。进行中就切到教练页（方向卡出现在那里）；
     fail-closed：LLM 不可用 → 后端 409，按步记错误并提示。 */
  const requestDirections = async (ask = "") => {
    const e = env.current;
    const key = e.activeKey, step = e.active, data = e.data;
    if (dirBusyMap[key]) return false;
    setGenErrMap(prev => ({ ...prev, [key]: null }));
    setDirBusyMap(prev => ({ ...prev, [key]: true }));
    e.setTabFor(key, "coach");
    try {
      const focusRow = key === "planning" ? ((e.scaffolds.planning || {}).sel || "") : "";
      const workId = activeWorkId();
      const beKey = S2_BE_KEY[key];
      if (!workId || !beKey) throw new Error("作品尚未就绪，稍后重试");
      const body = { target_chars: data.target || 120 };
      const askText = String(ask || "").trim();
      if (askText) body.ask = askText;
      if (key === "planning" && focusRow) body.focus_scene_id = focusRow;
      try {
        const dOv = SnowSync.pushCanon(key, { drafts: e.drafts, scaffolds: e.scaffolds }, workId);
        if (dOv && Object.keys(dOv).length) body.draft_override = dOv;
      } catch (ignored) {}
      const res = await apiPost(`/api/v2/projects/${workId}/snowflake-workspace/steps/${beKey}/fe-candidates`, body);
      if (!res || !res.turn_id) throw new Error("方向回包缺少回合");
      if (Array.isArray(res.assistant_history)) env.current.setCoachHist(res.assistant_history);
      const n = (res.candidates || []).length;
      env.current.pushHist(`教练给了 ${n} 个方向`, `${step.num} ${step.name}${focusRow ? " · 聚焦 " + env.current.sceneLabel(focusRow) : ""}`, "AI", null, key);
      env.current.showToast(`教练给了 ${n} 个方向 · 选一个「按此生成本步」`, "gold");
      return true;
    } catch (err) {
      const msg = (err && err.message) || "方向生成失败，请稍后重试";
      setGenErrMap(prev => ({ ...prev, [key]: msg }));
      env.current.showToast(msg.slice(0, 60), "crimson");
      return false;
    } finally {
      setDirBusyMap(prev => ({ ...prev, [key]: false }));
    }
  };

  const clearGenErr = (key) => setGenErrMap(prev => ({ ...prev, [key]: null }));
  return { structBusyMap, genTargetMap, genErrMap, dirBusyMap, clearGenErr, structuredGenerate, requestDirections };
}
