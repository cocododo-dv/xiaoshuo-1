import React from "react";
import { WsCatalog } from "./ws-catalog.jsx";
import { setViewIntentTargetReady } from "./ws-view-intents.js";
import { sceneLabel } from "./ws-labels.js";
import {
  scnQueueDismissAdd, scnQueueDismissClear, scnQueueDismissLoad, scnQueueLoad, scnQueueSave, scnRunLoad, scnRunSave,
} from "./ws-scene-store.js";
import {
  scnBackendRunSids, scnHydrateAfterSelection, scnHydrateFromBackend, scnResumeAfterSelection, scnRun, scnTopupBudget,
} from "./ws-scene-api.js";
import { RUN_JOB_TERMINAL_STATUSES, SCN_RUN_UI_ABORTED, scnRunUiAbortError, scnTerminalJobMessage } from "./ws-scene-derive.js";

const { useEffect, useRef, useState } = React;

/* 任务控制条接手一个新任务的宽限：它每 2 秒问一次 latest，三轮还没报来这个任务就当它接不上 */
const JOB_HANDOFF_GRACE_MS = 6000;

/* ==========================================================
   AI 起草台 — 台面状态（两只 hook，页面只负责摆放）
   · useSceneQueue：台面上有哪些场（在办 / 只是看看）、每场的运行记录、选中哪一场、多选移出，
     以及从本机缓存和后端（/scene-run-states + workbench）恢复
   · useSceneRuns：选中那一场的后端任务——解析后端 scene id、跟任务控制条要终态、起草 / 退回重写、
     追加预算、终选续跑，以及终态任务留下的本地 running 收敛
   ========================================================== */

/* 目录场景卡 → 起草台的一条工作项。
   阶段 X：左栏不再是一份手工挑出来的队列，而是全书的书脊（章 → 场，与写作台大纲同一份目录）。
   工作项分两种：**在办**（作者交给 AI 的、跑过管线的——持久化，刷新还在）与 **transient**（只是在书脊上
   点开看看——不落盘，点别的场就收走）。只是翻一遍书，不该把十七场都塞进在办清单。 */
function scnFromCatalog(sid, options) {
  if (!sid) return null;
  const hit = WsCatalog.sceneById(sid);
  if (!hit) return null;
  const { chapter: c, scene: s, index } = hit;
  return {
    id: "cq-" + s.sid, sid: s.sid, transient: !!(options && options.transient),
    n: sceneLabel(c, index),
    title: s.title, kind: (s.kind || "主动") + "场景",
    state: "queued", attempt: 0,
    draft: [], cost: [], log: [],
  };
}

/* 首屏：持久化的在办清单（按作品）+ 每场已持久化的运行结果；清单空时落在「现在该写哪一场」上
   （与主页 / 写作台同一条规则），不再给一块「队列是空的」的空白。 */
function initialBoard() {
  const items = scnQueueLoad().slice().map(sid => scnFromCatalog(sid)).filter(Boolean);
  const runs0 = {};
  items.forEach(it => { const r = scnRunLoad(it.sid); if (r) runs0[it.id] = r; });
  scnQueueSave(items.map(i => i.sid));
  if (!items.length) {
    const focus = WsCatalog.focusScene();
    const item = focus ? scnFromCatalog(focus.scene.sid, { transient: true }) : null;
    if (item) {
      items.push(item);
      const r = scnRunLoad(item.sid);
      if (r) runs0[item.id] = r;
    }
  }
  return { items, runs0 };
}

/* 只有在办的场落盘；transient（只是点开看看）不进 localStorage */
const persistQueue = (list) => scnQueueSave(list.filter(i => !i.transient).map(i => i.sid));

function useSceneQueue({ showNotice }) {
  const initRef = useRef(null);
  if (!initRef.current) initRef.current = initialBoard();
  const [items, setItems] = useState(initRef.current.items);
  const [runs, setRuns] = useState(initRef.current.runs0);
  const [pickedId, setPicked] = useState(() => (initRef.current.items[0] ? initRef.current.items[0].id : null));
  /* 进过管线的场（/scene-run-states）：null = 还没读到；"unknown" = 读不到（那就每场都问 latest）。 */
  const [backendRunSids, setBackendRunSids] = useState(null);
  const [selectMode, setSelectMode] = useState(false);          // 多选移出（会话内，不持久化）
  const [selected, setSelected] = useState(() => new Set());
  const mountedRef = useRef(true);
  const pickedIdRef = useRef(pickedId);
  pickedIdRef.current = pickedId;
  const runsRef = useRef(runs);
  runsRef.current = runs;

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const saveRun = (id, sid, record) => {
    setRuns(m => (m[id] ? m : { ...m, [id]: record }));
    scnRunSave(sid, record);
  };

  /* 把一场放上台面。pin=true：交给 AI（入列，落盘）；pin=false：只是在书脊上点开看看（transient）。
     已经在台面上的 transient 项再被「交给 AI」时转成在办。 */
  const bringScene = (sid, { pin = true, pick = true } = {}) => {
    const it = scnFromCatalog(sid, { transient: !pin });
    if (!it) return;
    const id = it.id;
    if (pin) scnQueueDismissClear([it.sid]);   // 重新入列 = 撤销之前的移出
    setItems(x => {
      const existing = x.find(y => y.id === id);
      let nx;
      if (existing) {
        if (!pin || !existing.transient) return x;
        nx = x.map(y => (y.id === id ? { ...y, transient: false } : y));
      } else {
        /* 点开另一场时，把上一张「只是看看」且没有任何产出的 transient 收走 */
        const kept = pin ? x : x.filter(y => !y.transient || !!runsRef.current[y.id]);
        nx = pin ? [it, ...kept] : [...kept, it];
      }
      persistQueue(nx);
      return nx;
    });
    setSelected(prev => (prev.size ? new Set([...prev].filter(x => x !== id)) : prev));
    const r = scnRunLoad(it.sid);
    if (r) setRuns(m => (m[id] ? m : { ...m, [id]: r }));
    else {
      // 本地无记录：尝试从后端 workbench 恢复既有产出（不覆盖期间跑起来的运行）
      scnHydrateFromBackend(it.sid)
        .then(hr => { if (hr && mountedRef.current) saveRun(id, it.sid, hr); })
        .catch(() => {});
    }
    if (pick) setPicked(id);
  };
  const enqueueSid = (sid) => bringScene(sid, { pin: true });
  const viewSid = (sid) => bringScene(sid, { pin: false });
  /* 整章入列：本章没写完的场一次交给 AI（不自动起草，逐场点开始）；落点在其中第一场 */
  const enqueueChapter = (sids) => {
    const list = (sids || []).filter(Boolean);
    list.slice().reverse().forEach((sid, i) => bringScene(sid, { pin: true, pick: i === list.length - 1 }));
  };
  /* 开始起草 = 这一场进入在办（落盘；之前移出过的销名） */
  const pinItem = (item) => {
    if (!item || !item.transient) return;
    scnQueueDismissClear([item.sid]);
    setItems(x => { const nx = x.map(y => (y.id === item.id ? { ...y, transient: false } : y)); persistQueue(nx); return nx; });
  };

  /* 移出队列：只把这一场从「在办清单」里拿掉——场景卡、已生成的 AI 稿和后端运行记录
     一概不动（重新入列即原样回来，所以这里说「移出」而不是「删除」；要删场景卡请去章节编排）。
     正因为不破坏任何东西，这里不拦一道确认弹窗，而是移完给一条带「撤销」的回执：
     动作即时、后悔成本近乎为零，比「先弹窗问一遍」顺手得多。
     运行中的场仍然拦下：后端任务还在跑，悄悄丢掉跟踪会让作者以为已经停了。 */
  const removeFromQueue = (ids) => {
    const wanted = new Set(ids || []);
    const targets = items.filter(x => wanted.has(x.id));
    if (!targets.length) return;
    const stateOf = (item) => (runs[item.id] && runs[item.id].state) || item.state || "queued";
    const running = targets.filter(x => stateOf(x) === "running");
    if (running.length) {
      showNotice({ tone: "warn", text: `有 ${running.length} 场正在运行，不能移出在办——先取消运行或等它跑完。` });
      return;
    }
    const removeIds = new Set(targets.map(x => x.id));
    const removedSids = targets.map(x => x.sid);
    const snapshot = items;                       // 撤销 = 整体还原这份快照（runs 全程没动，稿还在）
    const prevPicked = pickedIdRef.current;
    scnQueueDismissAdd(removedSids);
    const nx = items.filter(x => !removeIds.has(x.id));
    setItems(nx);
    persistQueue(nx);
    if (removeIds.has(prevPicked)) setPicked(nx.length ? nx[0].id : null);
    setSelected(prev => (prev.size ? new Set([...prev].filter(id => !removeIds.has(id))) : prev));
    showNotice({
      text: targets.length === 1
        ? `已把「${targets[0].title}」移出队列，场景卡与 AI 稿都保留`
        : `已把 ${targets.length} 场移出队列，场景卡与 AI 稿都保留`,
      actionLabel: "撤销",
      onAction: () => {
        scnQueueDismissClear(removedSids);
        setItems(snapshot);
        persistQueue(snapshot);
        setPicked(prevPicked);
      },
    });
  };

  /* 本地没有 scn-run 记录的入列场，从后端 workbench 恢复运行态——
     换浏览器 / 后台完成的运行不再「消失」；已有本地记录或期间跑起来的不覆盖 */
  useEffect(() => {
    let alive = true;
    (async () => {
      for (const it of initRef.current.items) {
        if (initRef.current.runs0[it.id]) continue;
        let r = null;
        try { r = await scnHydrateFromBackend(it.sid); } catch (e) {}
        if (!alive) return;
        if (r) saveRun(it.id, it.sid, r);
      }
    })();
    return () => { alive = false; };
  }, []);

  /* 队列成员的后端恢复：进过管线的场（scene-run-states）并入队列——本地队列在前、后端恢复在后；
     localStorage 队列由此退化为管线真相的读缓存，换浏览器队列成员不再是空的 */
  useEffect(() => {
    let alive = true;
    (async () => {
      let all = null;
      try { all = await scnBackendRunSids(); } catch (e) {}
      if (!alive) return;
      setBackendRunSids(all ? new Set(all) : "unknown");
      /* 作者移出过的场不再从后端恢复：管线里仍有它的运行记录，但队列是「在办清单」，
         恢复它等于把删除撤销掉。重新入列（交给 AI）会销名。 */
      const dismissed = new Set(scnQueueDismissLoad());
      const sids = (all || []).filter(sid => !dismissed.has(sid));
      if (!sids.length) return;
      const restored = sids.map(sid => scnFromCatalog(sid)).filter(Boolean);
      setItems(prev => {
        const restoredSids = new Set(restored.map(item => item.sid));
        const have = new Set(prev.filter(i => !i.transient).map(i => i.sid));
        const add = restored.filter(item => !have.has(item.sid));
        /* 进过管线的场是在办的：台面上同一场的 transient 项就地转正，不重复加一条 */
        const upgraded = prev.map(i => (i.transient && restoredSids.has(i.sid) ? { ...i, transient: false } : i));
        const fresh = add.filter(item => !upgraded.some(i => i.sid === item.sid));
        if (!fresh.length && upgraded.every((item, index) => item === prev[index])) return prev;
        const nx = [...upgraded, ...fresh];
        persistQueue(nx);
        return nx;
      });
      /* 空本地队列从后端恢复时必须选中首场，才能挂载 latest 控件。 */
      if (restored.length) setPicked(current => current || restored[0].id);
      /* 新并入的场恢复运行态；已在初始队列里的由上面的水合 effect 负责 */
      const fresh = sids.filter(sid => !initRef.current.items.some(i => i.sid === sid));
      for (const sid of fresh) {
        const id = "cq-" + sid;
        const local = scnRunLoad(sid);
        if (local) { setRuns(m => (m[id] ? m : { ...m, [id]: local })); continue; }
        let hr = null;
        try { hr = await scnHydrateFromBackend(sid); } catch (e) {}
        if (!alive) return;
        if (hr) saveRun(id, sid, hr);
      }
    })();
    return () => { alive = false; };
  }, []);

  /* 其它视图（章节编排「交给 AI」、写作台）经跨页指令送来的入列请求 */
  useEffect(() => {
    const onEnq = (e) => {
      const detail = e.detail || {};
      if (Array.isArray(detail.sids)) detail.sids.slice().reverse().forEach(enqueueSid);
      if (detail.sid) enqueueSid(detail.sid);
    };
    window.addEventListener("ws:scene-enqueue", onEnq);
    setViewIntentTargetReady("scene");
    return () => {
      setViewIntentTargetReady("scene", false);
      window.removeEventListener("ws:scene-enqueue", onEnq);
    };
  }, []);

  const pinned = items.filter(q => !q.transient);
  const select = {
    mode: selectMode,
    has: (id) => selected.has(id),
    count: selected.size,
    total: pinned.length,
    onToggleMode: () => { setSelectMode(v => !v); setSelected(new Set()); },
    onToggle: (id) => setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    }),
    onSelectAll: () => setSelected(prev => (prev.size === pinned.length ? new Set() : new Set(pinned.map(q => q.id)))),
    onRemoveSelected: () => { removeFromQueue([...selected]); setSelectMode(false); },
  };

  return {
    items, runs, setRuns, pickedId, setPicked, pickedIdRef, mountedRef, backendRunSids,
    viewSid, enqueueChapter, pinItem, removeFromQueue, select,
  };
}

/* 选中那一场的后端任务。任务控制条（SceneRunJobControl）是通常的唯一轮询者：它报来的每个任务经 onJobChange
   记成 authoritativeJob；startRun 在等的任务到了终态由这里兑现（控制条接不上时见 waitForTerminal）。 */
function useSceneRuns({ items, runs, setRuns, pickedId, pinItem }) {
  const runSeq = useRef({});
  const runAbortControllers = useRef({});
  const jobWaiters = useRef(new Map());
  const [activeBackendScene, setActiveBackendScene] = useState(null);
  const [observedRunJob, setObservedRunJob] = useState(null);
  const [authoritativeRunJob, setAuthoritativeRunJob] = useState(null);
  /* 归档成功后递增：让终态运行任务横幅重取 latest（后端视图已收敛为 archived）。 */
  const [refreshTick, setRefreshTick] = useState(0);
  const selected = items.find(x => x.id === pickedId) || null;

  /* 场景切换或页面卸载只停止前端跟踪，不伪造后端取消；回到场景后由 latest 恢复。 */
  useEffect(() => () => {
    Object.keys(runAbortControllers.current).forEach((id) => {
      const controller = runAbortControllers.current[id];
      if (controller) controller.abort();
      runSeq.current[id] = (runSeq.current[id] || 0) + 1;
    });
    runAbortControllers.current = {};
  }, [pickedId]);

  /* 换场时重新解析后端 scene id；旧解析和旧 latest 响应都不得覆盖新场景。 */
  useEffect(() => {
    let alive = true;
    setActiveBackendScene(null);
    setObservedRunJob(null);
    setAuthoritativeRunJob(null);
    const sid = selected && selected.sid;
    if (!sid) return () => { alive = false; };
    Promise.resolve(WsCatalog.__backendSceneId(sid))
      .then(sceneId => { if (alive) setActiveBackendScene(sceneId ? { sid, sceneId } : null); })
      .catch(() => { if (alive) setActiveBackendScene(null); });
    return () => { alive = false; };
  }, [pickedId, selected && selected.sid]);
  const activeBackendSceneId = (activeBackendScene && selected && activeBackendScene.sid === selected.sid)
    ? activeBackendScene.sceneId : "";
  const activeBackendSceneIdRef = useRef(activeBackendSceneId);
  activeBackendSceneIdRef.current = activeBackendSceneId;
  const currentJob = (authoritativeRunJob && activeBackendSceneId && authoritativeRunJob.sceneId === activeBackendSceneId)
    ? authoritativeRunJob.job : null;
  const jobStatus = currentJob && currentJob.status;

  /* 任务控制条报来的每一个任务：记下来，终态时兑现 startRun 的等待。
     同一个 job_id → 带着终态兑现；同一场却是别的任务到了终态 → 兑现 null，startRun 退回自己盯住它的任务。 */
  const onJobChange = (job) => {
    setAuthoritativeRunJob({ sceneId: activeBackendSceneId, job });
    if (!job) return;
    const mine = jobWaiters.current.get(job.job_id);
    if (mine) mine.seen();
    if (!RUN_JOB_TERMINAL_STATUSES.has(job.status)) return;
    [...jobWaiters.current.entries()].forEach(([jobId, waiter]) => {
      if (jobId !== job.job_id && waiter.sceneId !== activeBackendSceneId) return;
      waiter.resolve(jobId === job.job_id ? job : null);
    });
  };
  /* 等控制条报来终态。控制条只盯它挂着的那一场：这个任务不归它管（页面解析出的后端 id 还没到，
     或解析成了别的），或者控制条迟迟没接上这个任务（没挂载、拒收了它），就不等了——兑现 null，
     scnRun 自己盯住这个任务。宁可多一个轮询者，也不让这一场卡在「AI 正在起草」。 */
  const waitForTerminal = (signal) => (job, sceneId) => new Promise((resolve, reject) => {
    if (!sceneId || sceneId !== activeBackendSceneIdRef.current) { resolve(null); return; }
    let watchdog = null;
    const settle = () => {
      if (watchdog) { clearTimeout(watchdog); watchdog = null; }
      if (signal) signal.removeEventListener("abort", stop);
      if (jobWaiters.current.get(job.job_id) === waiter) jobWaiters.current.delete(job.job_id);
    };
    const stop = () => {
      settle();
      reject(scnRunUiAbortError());
    };
    const waiter = {
      sceneId,
      seen: () => { if (watchdog) { clearTimeout(watchdog); watchdog = null; } },
      resolve: (settled) => { settle(); resolve(settled); },
    };
    if (signal && signal.aborted) { stop(); return; }
    if (signal) signal.addEventListener("abort", stop, { once: true });
    jobWaiters.current.set(job.job_id, waiter);
    watchdog = setTimeout(() => { watchdog = null; waiter.resolve(null); }, JOB_HANDOFF_GRACE_MS);
  });

  /* latest 终态负责把切场景时遗留的本地 running 收敛回可继续状态。
     这一场正由本页的 startRun 跟着（它在等同一个任务的终态）时不插手：结果由 startRun 写，
     否则两条路各取一次 workbench，后到的那份会把尝试历史冲掉。 */
  useEffect(() => {
    const terminal = RUN_JOB_TERMINAL_STATUSES.has(jobStatus);
    if (!terminal || !selected || !activeBackendSceneId || !currentJob) return undefined;
    const id = selected.id;
    if (runAbortControllers.current[id]) return undefined;
    const sid = selected.sid;
    const expectedSeq = runSeq.current[id] || 0;
    const controller = new AbortController();
    let alive = true;
    const hasUsableLocalResult = (record) => (
      ["ready", "archived"].includes(record && record.state)
      && Array.isArray(record && record.draft)
      && record.draft.length > 0
    );
    const commit = (produce) => {
      if (!alive || controller.signal.aborted || (runSeq.current[id] || 0) !== expectedSeq) return;
      setRuns(current => {
        if (!alive || controller.signal.aborted || (runSeq.current[id] || 0) !== expectedSeq) return current;
        const next = produce(current[id] || {});
        scnRunSave(sid, next);
        return { ...current, [id]: next };
      });
    };

    if (jobStatus === "completed" || jobStatus === "blocked") {
      commit(previous => hasUsableLocalResult(previous) ? previous : ({
        ...previous,
        state: "queued",
        error: jobStatus === "blocked" ? "任务已阻断，正在恢复可审阅产出…" : "任务已完成，正在恢复产出…",
      }));
      (async () => {
        let hydrated = null;
        try { hydrated = await scnHydrateFromBackend(sid, { signal: controller.signal, terminalJob: currentJob }); } catch (e) {
          if (e && e.code === SCN_RUN_UI_ABORTED) return;
        }
        if (hydrated) {
          // 没有正文的预算断点要在「追加预算」旁边说清楚为什么；普通取回的正文照旧清掉旧错误
          commit(previous => ({ ...previous, ...hydrated, error: hydrated.recoveredWithoutDraft ? hydrated.error : null }));
          return;
        }
        // 没有草稿可恢复：说任务自己留下的原因（与 startRun 的 catch 同一句，见 scnTerminalJobMessage）
        const message = jobStatus === "blocked"
          ? scnTerminalJobMessage(currentJob)
          : "任务已完成，但暂未取回草稿，请稍后重试";
        commit(previous => hasUsableLocalResult(previous) ? previous : ({ ...previous, state: "queued", error: message }));
      })();
    } else {
      const message = scnTerminalJobMessage(currentJob);
      commit(previous => ({ ...previous, state: "queued", error: message }));
    }
    return () => {
      alive = false;
      controller.abort();
    };
  }, [activeBackendSceneId, jobStatus, currentJob && currentJob.job_id, selected && selected.id, selected && selected.sid]);

  /* —— 起草 / 退回重写（同一条路，带指令） —— */
  const startRun = async (sc, note, options = {}) => {
    if (!sc) return;
    const id = sc.id;
    pinItem(sc);
    if (runAbortControllers.current[id]) runAbortControllers.current[id].abort();
    const controller = new AbortController();
    runAbortControllers.current[id] = controller;
    const token = (runSeq.current[id] || 0) + 1; runSeq.current[id] = token;
    const normalizedNote = note == null ? "" : String(note).trim();
    if (Array.from(normalizedNote).length > 2000) {
      setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), error: "作者改写指令不能超过 2000 个字符，请精简后重试；内容没有被静默截断。" } }));
      delete runAbortControllers.current[id];
      return;
    }
    const attempt = ((runs[id] && runs[id].attempt) || 0) + 1;
    const t0 = new Date().toTimeString().slice(0, 8);
    setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), state: "running", attempt, authorNote: normalizedNote, error: null, budgetBlock: null, styleNotices: [], styleWindows: null, pipeState: "", cost: [],
      log: [{ t: t0, who: "system", text: `已提交第 ${attempt} 次起草${normalizedNote ? "，附改写指令" : ""}` }] } }));
    try {
      const res = await scnRun(sc, normalizedNote, "", {
        signal: controller.signal,
        resumeBudget: options.resumeBudget === true,
        onJobCreated: (job, sceneId) => setObservedRunJob({ job, sceneId }),
        waitForTerminal: waitForTerminal(controller.signal),
      });
      if (runSeq.current[id] !== token) return;
      setRuns(m => {
        const stamp = new Date().toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
        const prevAtt = ((m[id] && m[id].attempts) || []).map(a => a.time && a.time.startsWith("本次") ? { ...a, time: stamp, result: "退回重写", tone: "slate" } : a);
        const attempts = [{ n: attempt, time: "本次 · 待裁决", result: "待裁决", tone: "gold", note: normalizedNote ? "按指令改写" : "初稿", cmp: normalizedNote ? { verdict: "作者改写指令：" + normalizedNote } : undefined }, ...prevAtt].slice(0, 8);
        const nr = { ...(m[id] || {}), ...res, authorNote: normalizedNote, state: res.state === "archived" ? "archived" : "ready", attempt, attempts, at: Date.now() };
        scnRunSave(sc.sid, nr);
        return { ...m, [id]: nr };
      });
    } catch (e) {
      if (runSeq.current[id] !== token) return;
      if (e && e.code === SCN_RUN_UI_ABORTED) return;
      setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), state: "queued", error: (e && e.message) || "起草失败，请重试", budgetBlock: (e && e.budgetBlock) || null } }));
    } finally {
      if (runAbortControllers.current[id] === controller) delete runAbortControllers.current[id];
    }
  };

  /* 终选续跑之后把这一场换成后端的最新产出（候选终选面板与「追加预算」的终选续跑共用） */
  const applyResumed = async (sc, resumed, { requireDraft = false } = {}) => {
    const fresh = await scnHydrateAfterSelection(sc.sid, resumed);
    if (!fresh) {
      if (requireDraft) throw new Error("终选续跑后未取得可审阅正文。");
      return;
    }
    setRuns(m => {
      const next = { ...(m[sc.id] || {}), ...fresh, ...(requireDraft ? { error: null } : {}) };
      scnRunSave(sc.sid, next);
      return { ...m, [sc.id]: next };
    });
  };

  const topupBudget = async () => {
    const current = selected ? runs[selected.id] : null;
    if (!selected || !current || !current.budgetBlock) return;
    const sc = selected;
    const id = sc.id;
    setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), state: "running", error: null } }));
    try {
      await scnTopupBudget(sc.sid, current.budgetBlock);
      if (current.budgetBlock.resumeMode === "selection") {
        await applyResumed(sc, await scnResumeAfterSelection(sc.sid), { requireDraft: true });
      } else {
        await startRun(sc, current.authorNote || "", { resumeBudget: true });
      }
    } catch (e) {
      setRuns(m => ({ ...m, [id]: { ...(m[id] || {}), state: current.draft && current.draft.length ? "ready" : "queued", error: (e && e.message) || "追加预算失败，请重试", budgetBlock: current.budgetBlock } }));
    }
  };

  return {
    activeBackendSceneId,
    currentJob,
    jobStatus,
    observedJob: observedRunJob && observedRunJob.sceneId === activeBackendSceneId ? observedRunJob.job : null,
    onJobChange,
    refreshTick,
    refreshJob: () => setRefreshTick(tick => tick + 1),
    startRun: (note, options = {}) => startRun(selected, note, options),
    topupBudget,
    onCandidateChosen: (resumed) => (selected ? applyResumed(selected, resumed) : undefined),
  };
}

export { useSceneQueue, useSceneRuns };
