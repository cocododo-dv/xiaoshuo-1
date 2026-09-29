import React from "react";
import { I } from "./icons.jsx";
import { WsWorks } from "./ws-works.jsx";
import { SnowSync } from "./ws-snow-sync.jsx";
import { useFocusTrap, isImeComposing } from "./ws-dialog.jsx";
import {
  S2_STEPS, S2_STATE_LABEL,
  s2BlankScaffolds, s2BlockedStep, s2Content, s2DefaultChecks, s2DefaultDrafts, s2DefaultStates, s2FindStepKey,
  s2LandingStep, s2MergeScaffolds, s2SettlePlanning,
} from "./ws-snow-model.js";
import {
  activeWorkId, s2Load, s2LoadUiPref, s2SaveUiPref, S2_PREF_KEYS,
  useSnowEvents, useSnowMedia, useSnowNotices, useStableCallback,
} from "./ws-snow-hooks.js";
import { formatLocaleMonthDayTime } from "./lib/format.js";

/* ==========================================================
   雪花工作台的行为钩子（从 WsSnowflake 拆出，2026-09-29）
   ----------------------------------------------------------
   WsSnowflake 以前是一个约 770 行的函数：落点与跳转、右栏抽屉、步骤流转、十几个 AI 入口、导入 / 导出 /
   清空、键盘全挤在一起。这里一块一个钩子，视图只剩状态接线与版面：
   · useSnowWorkbenchApi —— AI 通道（生成 / 教练 / 分诊）调视图的那张显式接口；
   · useSnowLanding —— 落在哪一步、按步骤记住的页签、外部跳步 / 跳场（ws:snow-step / ws:snow-scene）；
   · useSnowContextRail —— 右栏：宽屏可收起的第三栏、窄屏抽屉（焦点陷阱与还焦点）、写作指引首访展开；
   · useSnowStepFlow —— 确认 / 复核 / 上游对照 / 略过 / 快照回滚；
   · useSnowAiActions —— 整步生成、按方向生成、定向补全、分诊，以及交给编辑器的 AI 工具面；
   · useSnowMoreMenu —— 「更多」菜单：导入结构、导出大纲、清空十步构思；
   · useSnowKeyboard —— ⌘↵ 确认本步、←/→ 翻步。
   每个钩子收一个对象（这一次渲染里视图的值与回调），异步回调读的是发起那一刻的值——与拆之前一样。
   ========================================================== */

const { useState: useSS, useEffect: useSE, useRef: useSR, useMemo: useSM } = React;

/* ---- 工作台 API：AI 通道调视图的一张显式接口 ----
   以前三个通道钩子（useSnowGeneration / useSnowCoach / useSnowTriage）共读一个每次渲染整份重写的 env ref：
   视图的状态、setter、回调全装在里面，教练的历史 setter 也是塞进去给生成通道用的——谁读了什么、能改什么，
   只能挨个翻。这里挂载时建一次、身份不变；方法在调用那一刻读视图的最新值（通道在 await 之后写回时仍用发起那一刻
   取到的步骤键，与以前一样），通道只能调这些方法，碰不到视图的其余状态。live 是这一次渲染视图交出来的值：
   { workId, activeKey, active, data, drafts, scaffolds, setScaffolds, setDrafts, setTabFor, pushHist, snapNow, showToast, sceneLabel }。 */
export function useSnowWorkbenchApi(live) {
  const liveRef = useSR(live);
  liveRef.current = live;
  return useSM(() => {
    const v = () => liveRef.current;
    return {
      /* 挂载时冻结的作品 id（本机缓存键里的那个） */
      workId: () => v().workId,
      /* 此刻所在的一步：key 前端步骤键，step 目录条目（num / name…），data 写作指引与编辑形态 */
      current: () => ({ key: v().activeKey, step: v().active, data: v().data }),
      /* 此刻的十步内容：draft_override、聚焦的场 / 角色都从这里取 */
      doc: () => ({ drafts: v().drafts, scaffolds: v().scaffolds }),
      setScaffolds: (updater) => v().setScaffolds(updater),
      setDrafts: (updater) => v().setDrafts(updater),
      /* 把某一步切到某个页签（生成完回编辑页，要方向时去教练页） */
      showTab: (key, tab) => v().setTabFor(key, tab),
      /* 历史记一条（snap 是可回滚的快照；who / key 缺省 = 我 / 此刻这一步）；给某一步此刻的内容拍一份快照 */
      journal: (action, note, who, snap, key) => v().pushHist(action, note, who, snap, key),
      snapshot: (key) => v().snapNow(key),
      toast: (text, tone) => v().showToast(text, tone),
      /* 场景的显示号（S01…），不把 row_<uuid> 摆给作者 */
      sceneLabel: (rowUid) => v().sceneLabel(rowUid),
    };
  }, []);
}

/* 右栏：09 / 10 是两张宽表，默认收起、把宽度让给表格；其余步骤默认展开。作者的选择按两组记住。
   窄屏（≤1180）右栏本来就折成抽屉，这个偏好不参与。 */
const s2RailGroup = (key) => (key === "scenes" || key === "planning" ? "table" : "form");
const S2_NARROW_QUERY = "(max-width: 1180px)";

/* ---- 落在哪一步 · 页签 · 外部跳转 ---- */
export function useSnowLanding({ workId: snowWorkId, myKey, initialStep, states, health: beHealth, latestRef, setScaffolds }) {
  /* 外部指定（命令面板 / 成稿中心 / 章节编排的跳转）优先；否则先是需复核的第一步，再是还没确认的第一步，
     都没有就回到上次看的那一步。本地缓存还没水合（新浏览器、清过缓存）时，第一次水合回来再按服务端真相
     定一次落点——作者已经在这页上点过、敲过任何东西，就不再挪。以前每次进来都落在 03，且外部每次跳步
     都会让整张视图换 key 重挂，交付条、打开的分章面板、教练历史与生成中的忙态都跟着丢。 */
  const lastVisitedRef = useSR(undefined);
  if (lastVisitedRef.current === undefined) lastVisitedRef.current = s2LoadUiPref(S2_PREF_KEYS.lastStep)[snowWorkId] || "";
  const [activeKey, setActiveKey] = useSS(() => s2FindStepKey(initialStep)
    || s2LandingStep({ states, health: beHealth, lastVisited: lastVisitedRef.current }));
  const movedRef = useSR(!!s2FindStepKey(initialStep));
  const landedOnServerRef = useSR(null);
  if (landedOnServerRef.current == null) { try { landedOnServerRef.current = !!SnowSync.hydrated(snowWorkId); } catch (e) { landedOnServerRef.current = true; } }
  const markMoved = () => { movedRef.current = true; };
  const selectStep = useStableCallback((key) => {
    const k = s2FindStepKey(key);
    if (!k) return;
    movedRef.current = true;
    setActiveKey(k);
  });
  useSE(() => {
    if (!snowWorkId) return;
    const all = s2LoadUiPref(S2_PREF_KEYS.lastStep);
    if (all[snowWorkId] !== activeKey) s2SaveUiPref(S2_PREF_KEYS.lastStep, { ...all, [snowWorkId]: activeKey });
  }, [activeKey, snowWorkId]);

  /* 页签按步骤记忆：在某步点开「教练」，不该让其它步骤也停在教练页 */
  const [tabByStep, setTabByStep] = useSS({});
  const tab = tabByStep[activeKey] || "edit";
  const setTabFor = useStableCallback((key, v) => setTabByStep(prev => ({ ...prev, [key]: v })));
  const setTab = (v) => setTabFor(activeKey, v);

  /* ---- 跳到第 10 步的某一场（成稿中心的场景三问、分章面板里的一场）----
     按 scene_id 对到 09 的 row_uid；对照表来自工作台，还没水合时把目标先挂着（pendingSceneRef），
     水合回来再试一次。 */
  const focusPlanScene = (sceneId) => {
    if (!sceneId) return false;
    let rowUid = "";
    try { rowUid = SnowSync.rowUidForSceneId(snowWorkId, sceneId) || ""; } catch (e) {}
    if (!rowUid) {
      const list = ((latestRef.current.scaffolds || {}).scenes || {}).list || [];
      if (list.some(s => s.id === sceneId)) rowUid = sceneId;
    }
    if (!rowUid) return false;
    movedRef.current = true;
    setActiveKey("planning");
    setScaffolds(prev => ({ ...prev, planning: { ...(prev.planning || {}), sel: rowUid } }));
    return true;
  };
  const pendingSceneRef = useSR(null);
  const tryPendingScene = () => {
    const target = pendingSceneRef.current;
    if (target && focusPlanScene(target)) pendingSceneRef.current = null;
  };
  const relandOnServer = (hydratedWorkId) => {
    if (landedOnServerRef.current) return;
    if (hydratedWorkId !== snowWorkId) return;
    landedOnServerRef.current = true;
    if (movedRef.current) return;
    const s = s2Load(myKey);
    let health = {};
    try { health = SnowSync.health(snowWorkId) || {}; } catch (e) {}
    setActiveKey(s2LandingStep({ states: { ...s2DefaultStates(), ...(s.states || {}) }, health, lastVisited: lastVisitedRef.current }));
  };
  useSnowEvents({
    /* 命令面板 / 主页 / 章节编排 / 成稿中心的跳步：就地换步（不再重挂整张视图） */
    "ws:snow-step": (event) => {
      const k = s2FindStepKey(event && event.detail);
      if (!k) return;
      movedRef.current = true;
      setActiveKey(k);
      setTabFor(k, "edit");
    },
    "ws:snow-scene": (event) => { pendingSceneRef.current = (event && event.detail) || null; tryPendingScene(); },
  });
  useSnowNotices({ hydrated: (hydratedWorkId) => { relandOnServer(hydratedWorkId); tryPendingScene(); } });

  /* 分章面板里「在构思里改这一场」：就地换到第 10 步的编辑页并选中那一场 */
  const jumpToPlanScene = (sceneId) => {
    movedRef.current = true;
    setActiveKey("planning");
    setTabFor("planning", "edit");
    pendingSceneRef.current = sceneId || null;
    tryPendingScene();
  };
  return { activeKey, selectStep, markMoved, tab, setTab, setTabFor, jumpToPlanScene };
}

/* ---- 右栏：宽屏是可收起的第三栏，窄屏是抽屉（ⓘ 打开，焦点移进抽屉并困在里面，Esc / 遮罩 / × 关闭后回到按钮） ---- */
export function useSnowContextRail(activeKey) {
  const narrow = useSnowMedia(S2_NARROW_QUERY);
  const [ctxOpen, setCtxOpen] = useSS(false);
  const [railPref, setRailPref] = useSS(() => s2LoadUiPref(S2_PREF_KEYS.rail));
  const railGroup = s2RailGroup(activeKey);
  const railShown = typeof railPref[railGroup] === "boolean" ? railPref[railGroup] : railGroup === "form";
  const toggleContext = () => {
    if (narrow) { setCtxOpen(o => !o); return; }
    setRailPref(prev => { const next = { ...prev, [railGroup]: !railShown }; s2SaveUiPref(S2_PREF_KEYS.rail, next); return next; });
  };
  useSE(() => { if (!narrow) setCtxOpen(false); }, [narrow]);
  const ctxRef = useSR(null);
  const ctxBtnRef = useSR(null);
  /* 抽屉一直挂在 DOM 里（关上只是隐藏），陷阱在 layout 清理里还焦点会被 React 随后的「恢复选区」
     改回抽屉里的按钮，抽屉一隐藏焦点就掉到 body。所以陷阱不还焦点，由下面的 passive effect
     （在恢复选区之后才跑）把焦点交回 ⓘ 按钮——Esc、×、遮罩、「去教练页」都走这里。 */
  useFocusTrap(ctxRef, ctxOpen && narrow, { restoreFocus: false });
  const ctxWasOpenRef = useSR(false);
  useSE(() => {
    const wasOpen = ctxWasOpenRef.current;
    ctxWasOpenRef.current = ctxOpen;
    if (!wasOpen || ctxOpen || !narrow) return;
    const focused = document.activeElement;
    const lost = !focused || focused === document.body || (ctxRef.current && ctxRef.current.contains(focused));
    if (lost && ctxBtnRef.current) ctxBtnRef.current.focus({ preventScroll: true });
  }, [ctxOpen]);
  /* 写作指引：每一步第一次打开时展开，之后默认收起（读过的指引不必每次占半屏） */
  const guideSeenRef = useSR(null);
  if (guideSeenRef.current == null) guideSeenRef.current = s2LoadUiPref(S2_PREF_KEYS.guideSeen);
  const guideFirstVisit = !guideSeenRef.current[activeKey];
  useSE(() => {
    if (guideSeenRef.current[activeKey]) return;
    guideSeenRef.current = { ...guideSeenRef.current, [activeKey]: 1 };
    s2SaveUiPref(S2_PREF_KEYS.guideSeen, guideSeenRef.current);
  }, [activeKey]);
  const ctxExpanded = narrow ? ctxOpen : railShown;
  return { narrow, ctxOpen, setCtxOpen, railShown, toggleContext, ctxRef, ctxBtnRef, ctxExpanded, guideFirstVisit };
}

/* ---- 步骤流转：确认 / 复核 / 上游对照 / 略过 / 回滚 ---- */
export function useSnowStepFlow({
  activeKey, active, idx, states, setStates, staleMap, curBeStale, pushHist, snapNow, pushToast, showToast, catalogSyncRef,
  selectStep, setTabFor, setDrafts, setScaffolds, setHistory,
}) {
  const [upDiff, setUpDiff] = useSS(null);     // 上游 diff 对话框 { key, loading, items, error, reason }
  const [snapDiff, setSnapDiff] = useSS(null); // 待预览的历史快照条目
  const goStep = (i) => selectStep(S2_STEPS[Math.max(0, Math.min(S2_STEPS.length - 1, i))].key);
  const nextUnfinished = (from) => {
    for (let i = 1; i <= S2_STEPS.length; i++) {
      const s = S2_STEPS[(from + i) % S2_STEPS.length];
      if (states[s.key] !== "done" && s.key !== activeKey) return S2_STEPS.findIndex(x => x.key === s.key);
    }
    return Math.min(S2_STEPS.length - 1, from + 1);
  };
  /* 服务端没记下（确认 / 复核 / 略过）时的回执。卡在「前面的步骤还没确认」时点名是哪一步、给一扇去那一步的门——
     以前只转述「需要先确认前面的雪花步骤。」，作者得一步步去找；其余错误照旧给服务端的原话。 */
  const failToast = (lead, err) => {
    const blocked = s2BlockedStep(err);
    if (!blocked) {
      showToast(`${lead}：` + ((err && err.message) || "稍后重试").slice(0, 40), "crimson");
      return;
    }
    pushToast({
      text: `${lead}：先确认「${blocked.label}」${blocked.more > 0 ? `（前面还有 ${blocked.more} 步没确认）` : ""}`,
      tone: "crimson", timeout: 7000,
      ...(blocked.key ? { actionLabel: `去 ${blocked.label}`, onAction: () => { selectStep(blocked.key); setTabFor(blocked.key, "edit"); } } : {}),
    });
  };
  const confirmStep = async () => {
    /* 阶段 G：确认过又改了的步骤（后端 pending_review + revised_after_approval）不再在键入后自动补批准，
       而是在这里由作者显式重新确认——下游失效级联在这一刻发生、回包整份工作台刷新健康。失败就诚实提示，不跳步。 */
    const workId = activeWorkId();
    let reconfirm = false;
    try { reconfirm = !!(workId && SnowSync.needsReconfirm(workId, activeKey)); } catch (e) {}
    if (reconfirm) {
      try {
        await SnowSync.approveStep(workId, activeKey);
      } catch (err) {
        failToast("重新确认未能记入服务端", err);
        return;
      }
    }
    setStates(prev => ({ ...prev, [activeKey]: "done" }));
    pushHist(reconfirm ? "重新确认" : "确认本步", `${active.num} ${active.name}`, "我", snapNow(activeKey));
    const synced = catalogSyncRef.current && Date.now() - catalogSyncRef.current.at < 4000 ? catalogSyncRef.current.text : "";
    catalogSyncRef.current = null;
    pushToast({
      text: (reconfirm ? `已重新确认 · ${active.name}，下游按新版本核对` : `已确认 · ${active.name}`) + (synced ? ` · ${synced}` : ""),
      tone: "sage", timeout: synced ? 7000 : 4200,
    });
    const ni = nextUnfinished(idx); if (ni >= 0) goStep(ni);
  };
  /* 「已复核」= 在服务端记下「仍然有效」（accept-stale，后端把消费的上游版本刷新到当前），回包刷新健康后
     横幅自然消失；失败就什么都不改——没有本地图可以偷偷对齐，两边永远一致。 */
  const reviewStep = async () => {
    if (!staleMap[activeKey]) return;
    try {
      const workId = activeWorkId();
      if (!workId) throw new Error("同步层未就绪");
      await SnowSync.acceptStale(workId, activeKey, "");
    } catch (err) {
      failToast("复核未能记入服务端", err);
      return;
    }
    setStates(prev => ({ ...prev, [activeKey]: "done" }));
    pushHist("复核对齐", `${active.num} ${active.name}`, "我", snapNow(activeKey));
    showToast(`已复核 · ${active.name} 仍然有效，已在服务端留痕`, "sage");
  };
  /* 阶段 E：看清上游改了什么——本步确认时消费的上游版本 vs 现在的版本（后端 input_refs + history） */
  const showUpstreamDiff = async () => {
    const key = activeKey;
    const reason = curBeStale ? (curBeStale.staleReason || "") : "";
    setUpDiff({ key, loading: true, items: [], error: null, reason });
    try {
      const workId = activeWorkId();
      const items = workId ? await SnowSync.upstreamChanges(workId, key) : [];
      setUpDiff({ key, loading: false, items: items || [], error: null, reason });
    } catch (err) {
      setUpDiff({ key, loading: false, items: [], error: (err && err.message) || "拉取上游历史失败", reason });
    }
  };
  /* 阶段 M：略过写回服务端。必填步（01/02/03/09/10）不能略过——按钮直接禁用；可略过的步要一个理由
     （后端必填，在页脚的小浮层里写），服务端记为 skipped 并让下游视之为已满足。返回 true = 已略过。 */
  const skipStep = async (reasonText) => {
    if (active.essential) return false;
    const reason = String(reasonText || "").trim();
    if (!reason) { showToast("略过需要写一句理由", "crimson"); return false; }
    try {
      const workId = activeWorkId();
      if (!workId) throw new Error("同步层未就绪");
      await SnowSync.skipStep(workId, activeKey, reason);
    } catch (err) {
      failToast("略过未能记入服务端", err);
      return false;
    }
    setStates(prev => ({ ...prev, [activeKey]: prev[activeKey] === "done" ? "done" : "skip" }));
    pushHist("略过此步", `${active.num} ${active.name} · ${reason}`);
    showToast(`已略过 · ${active.name}（已在服务端留痕）`, "slate"); goStep(idx + 1);
    return true;
  };
  const restoreSnap = (h) => { if (h && h.snap) setSnapDiff(h); };
  const applySnap = (h) => {
    if (!h || !h.snap) return;
    const st = S2_STEPS.find(s => s.key === h.key); if (!st) return;
    // 回滚前先给当前状态留底，回滚本身也可被撤销
    const backup = snapNow(h.key);
    setDrafts(prev => ({ ...prev, [h.key]: h.snap.draft || "" }));
    if (h.snap.scaffold) setScaffolds(prev => s2SettlePlanning({ ...prev, [h.key]: JSON.parse(JSON.stringify(h.snap.scaffold)) }));
    setHistory(prev => [{ t: Date.now(), who: "我", action: "回滚快照", note: `${st.num} ${st.name} ← ${formatLocaleMonthDayTime(h.t)}`, key: h.key, snap: backup }, ...prev].slice(0, 80));
    selectStep(h.key); setTabFor(h.key, "edit"); setSnapDiff(null);
    showToast(`已回滚 · ${st.name}`, "gold");
  };
  return { goStep, confirmStep, reviewStep, showUpstreamDiff, skipStep, restoreSnap, applySnap, upDiff, setUpDiff, snapDiff, setSnapDiff };
}

/* ---- AI：整步生成 / 按方向生成 / 定向补全 / 分诊（通道本身在 ws-snow-generation.js 的 useSnowGeneration） ---- */
export function useSnowAiActions({
  gen, tri, activeKey, active, data, scaffolds, structBusy, genTarget, sceneLabel, pushHist, snapNow, setDraft, setTab, showToast, setStates,
}) {
  /* 按新上游重新展开本步——用现在的上游材料重新生成（生成前留底，可回滚），生成后本步回到「进行中」，由作者再确认 */
  const regenFromUpstream = async () => {
    const key = activeKey;
    const ok = await gen.structuredGenerate({
      source: "fe_restale_regen", switchTab: true,
      histAction: "按新上游重新展开", histNote: "重展前留底",
      doneAction: "按新上游重新展开", doneNote: "上游已改，本步已按新上游重新生成，请核对后确认",
      toastOk: "已按新上游重新展开 · 请核对后确认本步", toastFail: "重新展开失败",
    });
    if (ok) setStates(prev => ({ ...prev, [key]: "active" }));
    return ok;
  };
  /* 多成员步骤（04/06/08 角色 · 10 场景规划）的定向生成：方向只落到当前选中的成员，其余保持不动 */
  const aiFocus = (() => {
    if (activeKey === "characters" || activeKey === "backstory" || activeKey === "profile") {
      const sc = scaffolds[activeKey] || {};
      const roster = activeKey === "characters" ? (sc.chars || {}) : (((scaffolds.characters || {}).chars) || {});
      const ids = Object.keys(roster);
      if (!ids.length) return null;
      const sel = (sc.sel && (roster[sc.sel] || (sc.chars || {})[sc.sel])) ? sc.sel : ids[0];
      const name = (((roster[sel] || (sc.chars || {})[sel]) || {}).name || "").trim();
      return { kind: "char", id: sel, label: (name || sel).slice(0, 8) };
    }
    if (activeKey === "planning") {
      const sel = ((scaffolds.planning || {}).sel || "").trim();
      if (!sel) return null;
      const row = (((scaffolds.scenes || {}).list) || []).find(s => s.id === sel);
      const title = ((row && (row.event || row.place)) || "").trim();
      const no = sceneLabel(sel);
      return { kind: "scene", id: sel, label: title ? `${no} ${title}`.slice(0, 12) : no };
    }
    return null;
  })();
  /* 「按此生成本步」（阶段 U）：教练日志里的一个方向（方向回合的第 index 条）或一段教练回复，作为这一次生成的蓝本。
     服务端按回合种类决定用法说明，回合记「已按此生成」，这一版 health.direction 记出处。
     focused = 只更新当前选中的成员（04/06/08 角色、10 场景）。 */
  const adoptDirection = (turn, index = null, { focused = false } = {}) => {
    const isCards = !!(turn && turn.turn_kind === "candidates");
    const item = isCards ? (((turn.candidates || [])[index]) || null) : null;
    const text = String(isCards ? ((item && item.text) || "") : ((turn && turn.reply) || "")).slice(0, 2000);
    if (!text) return Promise.resolve(false);
    const label = isCards ? ((item && item.label) || `方向 ${index + 1}`) : "教练回复";
    const useFocus = focused && aiFocus;
    const focusBody = useFocus
      ? (aiFocus.kind === "char" ? { focusChars: [aiFocus.id], focusChar: aiFocus.id } : { focus: [aiFocus.id], focusRow: aiFocus.id })
      : {};
    const verb = useFocus ? `按「${label}」只更新「${aiFocus.label}」` : `按「${label}」生成本步`;
    return gen.structuredGenerate({
      direction: text, directionKind: isCards ? "candidate" : "coach_reply",
      directionTurnId: turn.turn_id || null, directionIndex: isCards ? index : null,
      source: isCards ? "fe_candidate_adopt" : "fe_coach_adopt", switchTab: true, ...focusBody,
      target: { kind: "direction", turnId: turn.turn_id || null, index: isCards ? index : null, focused: !!useFocus },
      histAction: verb, histNote: "生成前留底",
      doneAction: verb,
      doneNote: useFocus ? "其余成员未动" : (isCards ? "按这个方向展开本步全部字段" : "按这段回复的判断与建议展开本步"),
      toastOk: useFocus ? `已按「${label}」更新「${aiFocus.label}」· 其余未动 · 可回滚` : `已按「${label}」生成「${active.name}」· 可回滚`,
      toastFail: "按方向生成失败",
    });
  };
  /* 02 一句话概括是自由文本：方向本身就是那一句，直接采用，不必再让模型转述一遍 */
  const adoptDirectionAsText = (turn, index) => {
    const item = ((turn && turn.candidates) || [])[index];
    if (!item || !item.text) return;
    pushHist(`采用方向「${item.label || index + 1}」`, `${active.num} ${active.name} · 采纳前留底`, "我", snapNow(activeKey));
    setDraft(item.text); setTab("edit");
    showToast(`已采用「${item.label || "方向"}」· 写入「${active.name}」`, "gold");
  };
  /* 「AI 生成本步」：按上游材料 + 本步要点整步生成（01–08；09/10 的整表动作在 AI 工具条的主位上） */
  const generateStep = () => gen.structuredGenerate({
    source: "fe_scaffold_ai", switchTab: true, target: { kind: "bar" },
    histAction: "AI 生成本步", doneAction: "AI 生成本步", doneNote: "依上游材料与本步要点整步生成",
    toastOk: `已生成「${active.name}」· 可回滚`, toastFail: "生成失败",
  });
  /* 要点改过而本稿没跟上 → 按最新要点重新展开本步（生成前留底，可回滚） */
  const regenWithBrief = () => gen.structuredGenerate({
    source: "fe_brief_regen", switchTab: true,
    histAction: "按最新要点重新生成", histNote: "生成前留底",
    doneAction: "按最新要点重新生成", doneNote: "本步按最新意图要点重新展开",
    toastOk: "已按最新要点重新生成 · 可回滚", toastFail: "重新生成失败",
  });
  /* AI 工具面（下面的 stepAI）上的各个入口：身份不变的回调 */
  const onGenerateAll = useStableCallback(() => gen.structuredGenerate({
    target: { kind: "scenes_all" },
    histAction: "AI 生成场景表", doneAction: "AI 生成场景表",
    doneNote: "依上游大纲与角色生成整表", toastOk: "场景表已生成 · 依上游材料 · 可回滚",
  }));
  const onFillAll = useStableCallback(() => gen.structuredGenerate({
    target: { kind: "fill_all" },
    histAction: "AI 补全所有场景", doneAction: "AI 补全所有场景",
    doneNote: "逐场补齐三拍与钩子", toastOk: "所有场景已补全 · 可回滚",
  }));
  const onFillScene = useStableCallback((rowUid) => gen.structuredGenerate({
    focus: [rowUid], focusRow: rowUid, target: { kind: "fill_scene", id: rowUid },
    histAction: `AI 补全 ${sceneLabel(rowUid)}`, doneAction: `AI 补全 ${sceneLabel(rowUid)}`,
    doneNote: "单场定向生成", toastOk: `${sceneLabel(rowUid)} 已补全 · 其余场景未动 · 可回滚`,
  }));
  /* 04/06/08：只补全当前选中的角色——后端 focus_character_refs 定向生成，其余角色（含名册顺序）保持不动 */
  const onFillChar = useStableCallback((charId, charName) => {
    const label = (charName || "").trim() || charId;
    return gen.structuredGenerate({
      focusChars: [charId], focusChar: charId, target: { kind: "fill_char", id: charId },
      histAction: `AI 补全角色「${label}」`, doneAction: `AI 补全角色「${label}」`,
      doneNote: "单角色定向生成", toastOk: `「${label}」已补全 · 其余角色未动 · 可回滚`,
    });
  });
  const onTriage = useStableCallback(() => tri.runTriage());
  const onApplyRepair = useStableCallback((rowUid, item) => tri.applyTriageRepair(rowUid, item));
  const onVerdict = useStableCallback((rowUid, status) => tri.setTriageVerdict(rowUid, status));
  /* 交给编辑器（与 09 / 10 的整表动作）的 AI 工具面：每一步都是同一个形状——本步的忙态与正在转圈的入口、
     分诊结果，以及各个入口（稳定回调）；编辑器取自己用得上的那几样。以前按步骤给三种东西（09 / 10 一份、
     04 / 06 / 08 另一份、其余不给），编辑器得先猜自己拿到的是哪一种。memo：只在忙态 / 分诊变化时换身份。 */
  const stepAI = useSM(() => ({
    structBusy, busyTarget: genTarget, triage: tri.triage, triageBusy: tri.triageBusy,
    onTriage, onApplyRepair, onVerdict, onGenerateAll, onFillAll, onFillScene, onFillChar,
  }), [structBusy, genTarget, tri.triage, tri.triageBusy]);
  const isTableStep = !!(data.scaffold && (data.scaffold.type === "scenelist" || data.scaffold.type === "scene"));
  return { regenFromUpstream, aiFocus, adoptDirection, adoptDirectionAsText, generateStep, regenWithBrief, stepAI, isTableStep };
}

/* ---- 「更多」菜单：导入 / 导出 / 危险区的清空 ---- */
export function useSnowMoreMenu({
  activeKey, drafts, scaffolds, states, staleMap, staleCount, doneCount, snapNow, pushHist, showToast,
  setDrafts, setScaffolds, setChecks, setStates, setHistory,
}) {
  const [importOpen, setImportOpen] = useSS(false);
  const [importText, setImportText] = useSS("");
  const [importBusy, setImportBusy] = useSS(false);
  const [importError, setImportError] = useSS("");
  const [resetOpen, setResetOpen] = useSS(false);
  /* 清空十步构思（原「重置」）。它不只是清本机：视图清空后 SnowSync 会把每一步的空稿上行到服务器
     （同步过的步骤都在账上，清空是作者的编辑）。所以它住在「更多」菜单的危险区，先开一个说清后果的对话框；
     清空前给每一步留一份快照进「历史」，可以逐步回滚。 */
  const resetAll = () => {
    const now = Date.now();
    // 只给真写过东西的步骤留底：空白脚手架里也有「c1 / 主角」这类默认值，不能算内容
    const blank = s2BlankScaffolds();
    const backups = S2_STEPS
      .map(st => ({ t: now, who: "我", action: "清空前留底", note: `${st.num} ${st.name}`, key: st.key, snap: snapNow(st.key) }))
      .filter(h => h.snap && s2Content(h.snap.draft, h.snap.scaffold).trim() !== s2Content("", blank[h.key]).trim());
    setDrafts(s2DefaultDrafts());
    setScaffolds(s2MergeScaffolds(null));
    setChecks(s2DefaultChecks());
    setStates(s2DefaultStates());
    setHistory(prev => [{ t: now, who: "我", action: "清空十步构思", note: `${backups.length} 步清空前留了快照`, key: activeKey, snap: null }, ...backups, ...prev]
      .slice(0, 80)
      .map((h, i) => (i < 20 ? h : (h.snap ? { ...h, snap: null } : h))));
    setResetOpen(false);
    showToast(backups.length ? `已清空十步构思 · 清空前的内容在「历史」里，可以逐步回滚` : "已清空十步构思", "slate");
  };
  const importCanonicalPlan = async () => {
    if (importBusy) return;
    setImportError("");
    let parsed;
    try { parsed = JSON.parse(importText); }
    catch (e) { setImportError("JSON 格式无效，请检查引号、逗号和括号。"); return; }
    setImportBusy(true);
    try {
      const result = await SnowSync.importCanonicalPlan(null, parsed);
      if (!result.readyToMaterialize) throw new Error("十步已导入，但后端物化闸门仍未通过；请检查标为重写的步骤或场景。");
      setImportOpen(false);
      setImportText("");
      showToast("结构化计划已导入 · 后端 10/10 批准", "sage");
    } catch (e) {
      setImportError((e && e.message) || "导入失败，请检查计划内容。");
    } finally { setImportBusy(false); }
  };
  const workTitle = () => { try { return (WsWorks && WsWorks.active && WsWorks.active().title) || ""; } catch (e) { return ""; } };
  /* export the whole snowflake as a Markdown outline (real download) */
  const exportOutline = useStableCallback(() => {
    const lines = [`# 雪花大纲 · ${workTitle() || "未命名作品"}`, "", `> 导出于 ${new Date().toLocaleString("zh-CN")} · 已确认 ${doneCount}/10${staleCount ? ` · ${staleCount} 需复核` : ""}`, ""];
    S2_STEPS.forEach(s => {
      const text = s2Content(drafts[s.key], scaffolds[s.key]).trim();
      const st = states[s.key];
      const tag = staleMap[s.key] ? "需复核" : (S2_STATE_LABEL[st] || st);
      lines.push(`## ${s.num} ${s.name}　[${tag}]`);
      lines.push(text || "（本步尚未填写）");
      lines.push("");
    });
    try {
      const blob = new Blob([lines.join("\n")], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href = url; a.download = "雪花大纲.md"; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1500);
      pushHist("导出大纲", "全书 10 步 · Markdown");
      showToast("已导出大纲 · 雪花大纲.md", "sage");
    } catch (e) { showToast("导出失败，请重试", "crimson"); }
  });
  const moreItems = useSM(() => [
    { label: "导入结构", hint: "粘贴十步规范 JSON，逐步保存并批准", icon: <I.Download size={14} />, testId: "snow-import-open",
      onSelect: () => { setImportError(""); setImportOpen(true); } },
    { label: "导出大纲", hint: "全书十步导出为 Markdown", icon: <I.UploadCloud size={14} />, onSelect: exportOutline },
    { separator: true },
    { label: "清空十步构思…", hint: "服务器会记一版空稿，清空前的内容留在「历史」里", icon: <I.Trash size={14} />, danger: true, testId: "snow-reset-open",
      onSelect: () => setResetOpen(true) },
  ], [exportOutline]);
  return {
    moreItems, exportOutline, workTitle, resetOpen, setResetOpen, resetAll,
    importOpen, setImportOpen, importText, setImportText, importBusy, importError, setImportError, importCanonicalPlan,
  };
}

/* ---- 键盘：⌘↵ 确认本步；←/→ 翻步——只在焦点落在页面本身或左侧步骤列表上时才翻 ----
     以前焦点在页签 / 下拉 / 按钮上按方向键也会翻步（页签自己的 ←/→ 刚切完页，整页又跳到下一步）。
     这张视图自己开着模态框（分章面板、导入、清空、两个对照框）或窄屏抽屉时，全局快捷键一律不响：
     在遮罩上按一下（有未确认调整时遮罩不关）焦点会落到 body，以前这时 ←/→ 会在面板背后换步、
     ⌘↵ 会确认背后那一步。 */
export function useSnowKeyboard({ modalOpen, narrow, ctxOpen, setCtxOpen, confirmStep, idx, goStep }) {
  useSnowEvents({
    keydown: (e) => {
      if (e.defaultPrevented || isImeComposing(e)) return;
      if (modalOpen) return;
      if (narrow && ctxOpen) {
        if (e.key === "Escape") setCtxOpen(false);
        return;
      }
      const t = e.target;
      const within = (sel) => !!(t && t.closest && t.closest(sel));
      // 阶段 E：焦点在教练输入框时，⌘↵ 是「发送」（它自己处理并 stopPropagation），窗口级不再抢去确认本步
      if (within(".sf-coach-input")) return;
      // 其它对话框 / 浮层（略过浮层、更多菜单、别的视图叠上来的框）里的按键归它们自己
      if (within('[role="dialog"]') || within('[role="menu"]')) return;
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); confirmStep(); return; }
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return;
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      const onPage = !t || t === document.body || t === document.documentElement;
      const inList = within(".snow-steps");
      if (!onPage && !inList) return;
      e.preventDefault();
      const next = Math.max(0, Math.min(S2_STEPS.length - 1, idx + (e.key === "ArrowLeft" ? -1 : 1)));
      goStep(next);
      // 在步骤列表里翻步时焦点跟着走，读屏与键盘用户都知道自己到了哪一步
      if (inList) setTimeout(() => { try { const el = document.querySelector(`[data-testid="snow-step-${S2_STEPS[next].key}"]`); if (el) el.focus(); } catch (err) {} }, 0);
    },
  });
}
