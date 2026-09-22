import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsWorks } from "./ws-works.jsx";
import { WsChapterPlanPanel, keepFocusOnDialogBackdrop } from "./ws-snow-chapters.jsx";
import { useFocusTrap, isImeComposing } from "./ws-dialog.jsx";
import { CloseButton } from "./ws-ui.jsx";
import { SnowSync } from "./ws-snow-sync.jsx";
import { navigateWithViewIntent, setViewIntentTargetReady } from "./ws-view-intents.js";
import { useUndoToast, UndoToast } from "./ws-undo-toast.jsx";
import {
  S2_STEPS, S2_BE_STEPS, S2_BE_KEY, S2_STEP_DATA, S2_STATE_LABEL, TRACK_LABEL,
  s2BlankScaffolds, s2Content, s2DefaultChecks, s2DefaultDrafts, s2DefaultStates, s2FindStepKey,
  s2LandingStep, s2MergeScaffolds, s2SceneNo, s2StaleMap,
} from "./ws-snow-model.js";
import {
  activeWorkId, s2Key, s2Load, s2LoadUiPref, s2SaveUiPref, s2StepSummary, S2_PREF_KEYS,
  useSnowDocument, useSnowEvents, useSnowGeneration, useSnowMedia, useSnowSyncMirror, useStableCallback,
} from "./ws-snow-hooks.js";
import { S2StepEditor } from "./ws-snow-scaffolds.jsx";
import { S2SceneAiActions, useSnowTriage } from "./ws-snow-scenes.jsx";
import { S2AiBar, S2Coach, useSnowCoach } from "./ws-snow-coach.jsx";
import { S2Rail } from "./ws-snow-rail.jsx";
import { S2History, S2Ref, S2SnapDiff, S2UpstreamDiff } from "./ws-snow-history.jsx";
import {
  S2DeliveredBanner, S2Footer, S2ImportPlanDialog, S2ResetDialog, S2ResyncBanner, S2StaleBanner,
  S2StepList, S2Strip, S2SyncNotice, S2Tab,
} from "./ws-snow-chrome.jsx";

/* ==========================================================
   WsSnowflake — 构思 · 雪花十步法 (Snowflake Method workbench)

   Built faithfully on Randy Ingermanson's Snowflake Method:
   · the FRACTAL principle — start with one sentence, expand it
     level by level (1句→5句→1页→4页); revising early is cheap,
     so backtracking is encouraged.
   · the STORY SPINE — three escalating disasters + a Moral
     Premise that flips false→true at the midpoint.
   · two interwoven TRACKS — plot and character expand in turns.
   · STRUCTURED SCAFFOLDS for the steps where the method's shape
     matters: the 40-character logline meter, the 5-sentence / 3-
     disaster paragraph, the character summary sheet (goal /
     ambition / values / conflict / epiphany), and the scene plan
     (Proactive G-C-S vs Reactive R-D-D).

   Layout: left step list · center canvas with tabs (编辑/教练/历史/引用上下文) ·
   right context rail that folds into a drawer on narrow screens.

   这个文件只剩工作台的状态与接线：步骤目录与纯推导在 ws-snow-model.js，缓存 / 事件 / 生成钩子在
   ws-snow-hooks.js，编辑器在 ws-snow-scaffolds.jsx（01–08）、ws-snow-scene-list.jsx（09）与 ws-snow-scene-plan.jsx（10），
   09 / 10 的整表 AI 动作与分诊状态在 ws-snow-scenes.jsx，
   教练在 ws-snow-coach.jsx，右栏在 ws-snow-rail.jsx，历史与对照在 ws-snow-history.jsx，外框在 ws-snow-chrome.jsx。
   同步层 SnowSync 直接 import（以前它反过来 import 本文件，视图只好经 window 绕开循环）。
   ========================================================== */

const { useState: useSS, useEffect: useSE, useRef: useSR, useMemo: useSM } = React;

/* 右栏：09 / 10 是两张宽表，默认收起、把宽度让给表格；其余步骤默认展开。作者的选择按两组记住。
   窄屏（≤1180）右栏本来就折成抽屉，这个偏好不参与。 */
const s2RailGroup = (key) => (key === "scenes" || key === "planning" ? "table" : "form");
const S2_NARROW_QUERY = "(max-width: 1180px)";
const NO_BRIEF_USAGE = { hasBrief: false, stale: false };
/* 这一步还什么都没写吗：折出来的文本和空白脚手架的一样（空白脚手架里本来就有 sel=c1、role=主角 这类默认值）。
   决定这一页唯一的实心主按钮是 AI 工具条上的生成，还是页脚的「确认本步」。 */
const S2_BLANK_TEXT = (() => {
  const blank = s2BlankScaffolds();
  return Object.fromEntries(Object.keys(blank).map(k => [k, s2Content("", blank[k]).trim()]));
})();
const s2StepIsBlank = (key, draft, scaffold) => s2Content(draft, scaffold).trim() === (S2_BLANK_TEXT[key] || "");

function WsSnowflake({ initialStep }) {
  // 挂载时冻结存储键：作品切换时整张视图会按作品重挂，卸载时的落盘写回正确的作品
  const keyRef = useSR(null);
  if (keyRef.current == null) keyRef.current = s2Key();
  const myKey = keyRef.current;
  const snowWorkId = String(myKey || "").split("::")[1] || "";

  /* 十步内容（本机缓存写穿 SnowSync）与同步层镜像。先挂内容：水合时它先重读缓存，
     后面的落点 / 场景目标处理拿到的就是重读之后的状态。 */
  const { drafts, setDrafts, scaffolds, setScaffolds, checks, setChecks, states, setStates, history, setHistory, savedAt, latestRef } = useSnowDocument(myKey, snowWorkId);
  const { syncState, health: beHealth, resync: resyncInfo, briefTick } = useSnowSyncMirror(snowWorkId);

  /* ---- 落在哪一步 ----
     外部指定（命令面板 / 成稿中心 / 章节编排的跳转）优先；否则先是需复核的第一步，再是还没确认的第一步，
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

  const { toast, show: pushToast, clear: clearToast } = useUndoToast();
  /* 统一回执：UndoToast（ws-undo-toast.jsx）。保留 (label, tone) 签名 */
  const showToast = (label, tone) => pushToast({ text: label, tone: tone || "sage", timeout: 4200 });

  const active = S2_STEPS.find(s => s.key === activeKey) || S2_STEPS[2];
  const data = S2_STEP_DATA[activeKey] || {};
  const idx = S2_STEPS.findIndex(s => s.key === activeKey);

  /* 场景的显示号（S01…）。09 的行 id 是不可变的 row_<uuid>，只做身份锚，不给作者看——
     回执、历史、教练日志里一律换成它在场景表里的编号。 */
  const sceneLabel = (rowUid) => {
    const list = ((scaffolds.scenes || {}).list) || [];
    const at = list.findIndex(s => s.id === rowUid);
    return at >= 0 ? s2SceneNo(rowUid, at) : (/^S\d+$/i.test(String(rowUid || "")) ? String(rowUid) : "这一场");
  };
  /* 历史时间线：snap 是可回滚的内容快照（只给最近 20 条保留，控制体积）。key 显式传入——
     异步生成回来时作者可能已经换了步，记账记在发起时的那一步上。 */
  const pushHist = (action, note, who = "我", snap = null, key = activeKey) =>
    setHistory(prev => [{ t: Date.now(), who, action, note: note || "", key, snap }, ...prev]
      .slice(0, 80)
      .map((h, i) => (i < 20 ? h : (h.snap ? { ...h, snap: null } : h))));
  const snapNow = (key) => {
    const cur = latestRef.current;
    try { return JSON.parse(JSON.stringify({ draft: cur.drafts[key] || "", scaffold: cur.scaffolds[key] })); } catch (e) { return null; }
  };

  /* 教练、生成、分诊三块状态都读这个 env（调用时读最新值） */
  const env = useSR(null);
  const coach = useSnowCoach(env, tab);
  const gen = useSnowGeneration(env);
  const tri = useSnowTriage(env);
  env.current = { activeKey, active, data, drafts, scaffolds, setScaffolds, setDrafts, setTabFor, pushHist, snapNow, showToast, sceneLabel, setCoachHist: coach.setCoachHist };
  const structBusy = !!gen.structBusyMap[activeKey];
  const genTarget = gen.genTargetMap[activeKey] || null;
  const dirBusy = !!gen.dirBusyMap[activeKey];
  const genErr = gen.genErrMap[activeKey] || null;

  const draft = drafts[activeKey] || "";
  const setDraft = useStableCallback((v) => setDrafts(prev => ({ ...prev, [activeKey]: typeof v === "function" ? v(prev[activeKey]) : v })));
  const updateScaffold = useStableCallback((updater) => setScaffolds(prev => ({ ...prev, [activeKey]: updater(prev[activeKey]) })));
  const toggleCheck = useStableCallback((i) => setChecks(prev => ({ ...prev, [activeKey]: (prev[activeKey] || []).map((v, j) => j === i ? !v : v) })));
  const doneCount = S2_STEPS.filter(s => states[s.key] === "done").length;

  /* 阶段 E（E3 第二步）：需复核只来自后端 status=stale（未确认仍有效）；值是按 input_refs 算出的
     「已有新版本」的上游列表，作方向指引与 diff 依据。 */
  const staleMap = useSM(() => s2StaleMap(beHealth), [beHealth]);
  const staleCount = Object.keys(staleMap).length;
  const curStale = staleMap[activeKey];   // 漂移上游 key 列表（可能为空数组），或 undefined
  const curBeStale = curStale ? beHealth[activeKey] : null;
  const [upDiff, setUpDiff] = useSS(null); // 上游 diff 对话框 { key, loading, items, error, reason }
  const [snapDiff, setSnapDiff] = useSS(null);   // 待预览的历史快照条目

  /* ---- 跳到第 10 步的某一场（成稿中心的场景三问、分章面板里的一场）----
     按 scene_id 对到 09 的 row_uid；对照表来自工作台，还没水合时把目标挂在 window.__snowSceneTarget 上，
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
  const tryPendingScene = () => {
    const target = window.__snowSceneTarget;
    if (target && focusPlanScene(target)) window.__snowSceneTarget = null;
  };
  useSE(() => { tryPendingScene(); }, []);
  const relandOnServer = (event) => {
    if (landedOnServerRef.current) return;
    if (!event || event.detail !== snowWorkId) return;
    landedOnServerRef.current = true;
    if (movedRef.current) return;
    const s = s2Load(myKey);
    let health = {};
    try { health = SnowSync.health() || {}; } catch (e) {}
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
    "ws:snow-scene": (event) => { window.__snowSceneTarget = event && event.detail; tryPendingScene(); },
    "ws:snow-hydrated": (event) => { relandOnServer(event); tryPendingScene(); },
  });

  /* ---- 物化后回流：构思 9/10 步领先于目录场景卡的场 ---- */
  const [resyncBusy, setResyncBusy] = useSS(false);
  const doResync = async () => {
    if (resyncBusy) return;
    setResyncBusy(true);
    try {
      const r = await SnowSync.resync();
      // 有一部分没能回流时不能报干净的成功——作者会以为目录已经是最新的。
      if (r.notice && r.notice.message) showToast(r.notice.message, "crimson");
      else showToast(`已把 ${r.synced} 场的构思改动同步到目录场景卡`, "sage");
    } catch (e) {
      showToast("同步到目录失败：" + ((e && e.message) || "请稍后重试"), "crimson");
    } finally { setResyncBusy(false); }
  };

  /* ---- 「整理章节结构」= 分章预览面板 ----
     面板只有这一个宿主：顶部按钮、07 章表的门、09 的章头都调同一个回调。ws:snow-chapter-plan 是 SnowSync
     每次水合都会广播的「分章状态」，视图不听它（以前把它当「打开面板」的命令，面板会在落地、刷新、
     09/10 自动保存之后自己弹出来）。 */
  const [chapterPlanOpen, setChapterPlanOpen] = useSS(false);
  const openChapterPlan = useStableCallback(() => setChapterPlanOpen(true));
  const goToPlanScene = (sceneId) => {
    setChapterPlanOpen(false);
    movedRef.current = true;
    setActiveKey("planning");
    setTabFor("planning", "edit");
    window.__snowSceneTarget = sceneId;
    tryPendingScene();
  };
  const goToMaterializationStep = (beKey) => {
    const pair = S2_BE_STEPS.find(([, candidate]) => candidate === beKey);
    if (!pair) return;
    setChapterPlanOpen(false);
    selectStep(pair[0]);
    setTabFor(pair[0], "edit");
  };
  /* 阶段 X：确认写入之后的交付条（几章几场、顺手做了什么、下一步去哪） */
  const [delivered, setDelivered] = useSS(null);
  const onChapterPlanDone = (result) => {
    setChapterPlanOpen(false);
    const chapters = (result && result.created_chapter_count) || 0;
    const trashed = ((result && result.trashed_empty_chapters) || []).length;
    const restored = ((result && result.restored_chapter_ids) || []).length;
    const restoredScenes = ((result && result.restored_scene_ids) || []).length;
    const placeholders = ((result && result.trashed_placeholder_chapters) || []).length;
    const notes = [
      placeholders ? `${placeholders} 个没动过笔的空白占位章已移入回收站，这一版的章从第 1 章排起` : "",
      trashed ? `${trashed} 个变空的旧章已移入回收站` : "",
      restored ? `${restored} 章从回收站取回` : "",
      // 作者先删了旧章再回来重新分章：随旧章进回收站的场景卡跟着这一版回来
      restoredScenes ? `${restoredScenes} 场随旧章进了回收站的场景卡已取回` : "",
      (result && result.chapter_order_held) ? "目录里有已终审的章，按章表排会挪动它——新章暂时接在最后，到成稿中心重新打开后再整理一次" : "",
    ].filter(Boolean);
    showToast(
      (chapters ? `已整理并写入 ${chapters} 章` : "章节结构已按这一版更新") + (notes.length ? ` · ${notes.join(" · ")}` : ""),
      "sage",
    );
    setDelivered({ created: chapters, notes });
  };
  /* 跟着目录走：确认写入之后目录是整份重拉的，交付条上的「几章几场」要等它回来再报（不能报 0 章 0 场）。
     听 ws:catalog-changed 广播而不是多引一个 hook——这张视图的单测把 ws-catalog 整个 mock 掉了。 */
  const [, setCatalogTick] = useSS(0);
  const catalogChapters = (() => { try { return (WsCatalog && WsCatalog.get ? WsCatalog.get() : []) || []; } catch (e) { return []; } })();
  const deliveredTotals = delivered
    ? { chapters: catalogChapters.length, scenes: catalogChapters.reduce((n, c) => n + (c.scenes || []).length, 0) }
    : null;
  const goWriteFirst = () => {
    const focus = WsCatalog && WsCatalog.focusScene ? WsCatalog.focusScene() : null;
    if (focus) navigateWithViewIntent("writer", "ws:writer-scene", focus.scene.sid);
    else location.hash = "#writer";
  };
  /* 确认 09 / 10 之后场景卡自动跟上构思（SnowSync 广播 ws:snow-catalog-synced）：出一句回执。
     作者点「确认本步」时，确认流程随后还会出它自己的回执——两句并成一句（catalogSyncRef 留 4 秒），
     不让「同步了几场」被后一句盖掉；键入后自动补批准的那条路没有第二句，这里直接出。 */
  const catalogSyncRef = useSR(null);
  useSnowEvents({
    "ws:catalog-changed": () => setCatalogTick(t => t + 1),
    "ws:snow-catalog-synced": (event) => {
      const d = (event && event.detail) || {};
      if (d.workId && d.workId !== activeWorkId()) return;
      const held = d.held_count || 0;
      const text = [
        d.synced_count ? `${d.synced_count} 场的改动已同步到目录（写作台 / AI 起草台读到的是新卡）` : "",
        held ? `${held} 场留给你看差异——用上方的「同步到目录」` : "",
      ].filter(Boolean).join(" · ");
      if (!text) return;
      catalogSyncRef.current = { at: Date.now(), text };
      pushToast({ text, tone: held && !d.synced_count ? "gold" : "sage", timeout: 6000 });
    },
  });

  /* ---- 右栏：宽屏是可收起的第三栏，窄屏是抽屉（ⓘ 打开，焦点移进抽屉并困在里面，Esc / 遮罩 / × 关闭后回到按钮） ---- */
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
  const openBriefInCoach = useStableCallback(() => { setTabFor(activeKey, "coach"); setCtxOpen(false); });

  /* ---- 步骤流转：确认 / 复核 / 略过 / 回滚 ---- */
  const goStep = (i) => selectStep(S2_STEPS[Math.max(0, Math.min(S2_STEPS.length - 1, i))].key);
  const nextUnfinished = (from) => {
    for (let i = 1; i <= S2_STEPS.length; i++) {
      const s = S2_STEPS[(from + i) % S2_STEPS.length];
      if (states[s.key] !== "done" && s.key !== activeKey) return S2_STEPS.findIndex(x => x.key === s.key);
    }
    return Math.min(S2_STEPS.length - 1, from + 1);
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
        showToast("重新确认未能记入服务端：" + ((err && err.message) || "稍后重试").slice(0, 40), "crimson");
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
      showToast("复核未能记入服务端：" + ((err && err.message) || "稍后重试").slice(0, 40), "crimson");
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
      showToast("略过未能记入服务端：" + ((err && err.message) || "稍后重试").slice(0, 40), "crimson");
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
    if (h.snap.scaffold) setScaffolds(prev => ({ ...prev, [h.key]: JSON.parse(JSON.stringify(h.snap.scaffold)) }));
    setHistory(prev => [{ t: Date.now(), who: "我", action: "回滚快照", note: `${st.num} ${st.name} ← ${new Date(h.t).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`, key: h.key, snap: backup }, ...prev].slice(0, 80));
    selectStep(h.key); setTabFor(h.key, "edit"); setSnapDiff(null);
    showToast(`已回滚 · ${st.name}`, "gold");
  };

  /* ---- AI：整步生成 / 按方向生成 / 定向补全（通道本身在 useSnowGeneration） ---- */
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
  /* 传给 09/10 与 04/06/08 编辑器的 AI 工具面（memo：编辑器只在忙态 / 分诊变化时因它重渲染） */
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
  const sceneAI = useSM(() => ({
    structBusy, busyTarget: genTarget, triage: tri.triage, triageBusy: tri.triageBusy,
    onTriage, onApplyRepair, onVerdict, onGenerateAll, onFillAll, onFillScene,
  }), [structBusy, genTarget, tri.triage, tri.triageBusy]);
  const charAI = useSM(() => ({ structBusy, busyTarget: genTarget, onFillChar }), [structBusy, genTarget]);
  const isTableStep = !!(data.scaffold && (data.scaffold.type === "scenelist" || data.scaffold.type === "scene"));
  const isCharStep = !!(data.scaffold && (data.scaffold.type === "charsheet" || data.scaffold.type === "backstory" || data.scaffold.type === "profile"));

  /* 要点镜像在 SnowSync 里；教练回包 / 作者编辑 / 生成回包都会发事件（briefTick 随之变，这里重读） */
  let brief = null;
  let briefUsage = NO_BRIEF_USAGE;
  try { brief = SnowSync.directionBrief(null, activeKey) || null; } catch (e) {}
  try { briefUsage = SnowSync.briefUsage(null, activeKey) || NO_BRIEF_USAGE; } catch (e) {}
  void briefTick;

  /* ---- 「更多」菜单：导入 / 导出 / 危险区的清空 ---- */
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
    { sep: true },
    { label: "清空十步构思…", hint: "服务器会记一版空稿，清空前的内容留在「历史」里", icon: <I.Trash size={14} />, danger: true, testId: "snow-reset-open",
      onSelect: () => setResetOpen(true) },
  ], [exportOutline]);

  /* ---- 键盘：⌘↵ 确认本步；←/→ 翻步——只在焦点落在页面本身或左侧步骤列表上时才翻 ----
     以前焦点在页签 / 下拉 / 按钮上按方向键也会翻步（页签自己的 ←/→ 刚切完页，整页又跳到下一步）。
     这张视图自己开着模态框（分章面板、导入、清空、两个对照框）或窄屏抽屉时，全局快捷键一律不响：
     在遮罩上按一下（有未确认调整时遮罩不关）焦点会落到 body，以前这时 ←/→ 会在面板背后换步、
     ⌘↵ 会确认背后那一步。 */
  const modalOpen = chapterPlanOpen || importOpen || resetOpen || !!upDiff || !!snapDiff;
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

  /* ---- 页脚：同步状态与重试 ---- */
  const [syncRetryBusy, setSyncRetryBusy] = useSS(false);
  const retrySnowSync = async () => {
    if (syncRetryBusy) return;
    setSyncRetryBusy(true);
    try {
      await SnowSync.retry(snowWorkId);
    } catch (error) {
      // SnowSync.retry 会自行记录远端 PATCH / approve 错误。这里的兜底异常
      // 不能被误标成“本机保存失败”，否则会把可重试的服务端故障变成导出告警。
      showToast(`同步重试失败：${error && error.message ? error.message : "请稍后再试"}`, "crimson");
    } finally { setSyncRetryBusy(false); }
  };

  const stStatus = states[activeKey];
  /* 「已确认」区分本地态 vs 后端批准态：beStatus==="approved" 才是后端已批。前序闸门不满足时 approve 被
     同步层跳过，此时本地 done 但后端仍 pending_review——这里显式标注。beHealth 缺失（未同步）时不误判为「未批」。 */
  const curHealth = beHealth[activeKey];
  const beApproved = !!(curHealth && curHealth.beStatus === "approved");
  /* 服务器已确认、之后没改过、上游也没漂移：这一步已经落定，页脚的「确认本步」退成次要按钮 */
  const stepSettled = stStatus === "done" && beApproved && !curStale;
  /* 一页只有一个实心主按钮：空着的一步是 AI 生成；写了还没落定的是「确认本步」；落定之后，
     第 10 步也确认过了就是页头的「整理章节结构」（见 S2Strip），否则这一页没有非按不可的动作 */
  const stepBlank = !stepSettled && s2StepIsBlank(activeKey, draft, scaffolds[activeKey]);
  const ctxExpanded = narrow ? ctxOpen : railShown;
  /* 本会话还没读到服务器（水合失败）：页头的确认数先写「—」，画布上方的提示条说清楚、给重试 */
  const serverUnread = !!(syncState && syncState.phase === "error" && syncState.error && syncState.error.scope === "hydrate");
  const coachTurns = coach.coachHist.filter(t => t.step_key === S2_BE_KEY[activeKey]).length;

  return (
    /* onMouseDown：这张视图开的对话框（导入 / 清空 / 两个对照框走 portal，React 事件照样冒泡到这里）在不关的遮罩上
       按下鼠标时不让焦点掉到 body（见 keepFocusOnDialogBackdrop） */
    <div className="snow-page" data-screen-label="snowflake" onPointerDownCapture={markMoved} onKeyDownCapture={markMoved}
      onMouseDown={keepFocusOnDialogBackdrop}>

      <S2Strip states={states} staleMap={staleMap} activeKey={activeKey} activeSettled={stepSettled} onSelect={selectStep} serverUnread={serverUnread}
        onOpenChapterPlan={openChapterPlan} moreItems={moreItems} />

      {delivered && deliveredTotals && (
        <S2DeliveredBanner totals={deliveredTotals} notes={delivered.notes} onWrite={goWriteFirst} onClose={() => setDelivered(null)} />
      )}
      {resyncInfo.pendingCount > 0 && <S2ResyncBanner info={resyncInfo} busy={resyncBusy} onResync={doResync} />}

      <div className="snow-cols" data-ctx={ctxOpen ? "open" : "closed"} data-rail={railShown ? "on" : "off"}>
        <S2StepList states={states} staleMap={staleMap} health={beHealth} activeKey={activeKey} onSelect={selectStep} />

        {/* center — canvas */}
        <section className="snow-canvas" aria-labelledby="snow-canvas-title">
          <S2SyncNotice syncState={syncState} retryBusy={syncRetryBusy} onRetry={retrySnowSync} onExport={exportOutline} />
          <div className="sf-canvas-anim" key={activeKey}>
            <header className="snow-canvas-head">
              <div className="sf-head-main">
                <span className="snow-canvas-num" aria-hidden="true">{active.num}</span>
                <div className="sf-head-text">
                  <h2 className="snow-canvas-title" id="snow-canvas-title">{active.name}</h2>
                  <div className="sf-head-meta">
                    <span className={`sf-trk-tag trk-${active.track}`}>{TRACK_LABEL[active.track]}</span>
                    <span title={`建议用时 ${active.timebox}`}>{active.book} · {active.grow}</span>
                    {active.from && (
                      <button className="sf-lineage" onClick={() => selectStep(active.fromKey)} title="回到它展开自的那一步">
                        <I.ChevronLeft size={11} /> 展开自 {active.from}
                      </button>
                    )}
                  </div>
                </div>
              </div>
              <div className="sf-head-side">
                {stStatus === "done" ? (
                  beApproved ? (
                    /* 和计数「N/10 已确认」、按钮「确认本步」同一个词（以前这里叫「已批准」） */
                    <span className="pill pill-sage" data-testid="snow-confirmed-pill" title="服务器已记下：本步已确认"><span className="pill-dot" />已确认</span>
                  ) : (curHealth && curHealth.revisedAfterApproval) ? (
                    <span className="pill pill-gold" data-testid="snow-reconfirm-pill" title="确认之后又改过：点「确认本步」重新确认，下游步骤才会按新版本核对"><span className="pill-dot" />已改动 · 待重新确认</span>
                  ) : (
                    <span className="pill pill-gold" title={(curHealth && !curHealth.gateSatisfied) ? "本地已确认，服务器还没确认：前序步骤没确认完——补齐上游各步后会自动确认" : "本地已确认 · 正在同步到服务器…"}><span className="pill-dot" />本地已确认</span>
                  )
                ) : active.essential ? (
                  <span className="pill pill-crimson" title="整理章节结构之前必须确认的一步"><span className="pill-dot" />必填</span>
                ) : (
                  <span className="pill" title="可以留空或略过"><span className="pill-dot" />建议</span>
                )}
                {stStatus === "warn" && <span className="pill pill-gold"><span className="pill-dot" />需补</span>}
                {curStale && <span className="pill pill-gold"><span className="pill-dot" />需复核</span>}
                <button ref={ctxBtnRef} className={`btn btn-quiet btn-sm sf-ctx-open ${ctxExpanded ? "is-on" : ""}`} onClick={toggleContext}
                  aria-expanded={ctxExpanded} aria-controls="snow-ctx" aria-label="本步上下文：任务、检查与写作指引"
                  title={narrow ? "打开本步上下文" : (railShown ? "收起右栏，把宽度让给编辑区" : "展开右栏：本步任务、检查与写作指引")}>
                  <I.Info size={14} /><span className="sf-ctx-open-label">本步上下文</span>
                </button>
              </div>
            </header>

            {curStale && (
              <S2StaleBanner reason={curBeStale && curBeStale.staleReason} drift={curStale} busy={structBusy}
                onGoStep={selectStep} onShowDiff={showUpstreamDiff} onRegen={regenFromUpstream} onReviewed={reviewStep} />
            )}

            <div className="snow-tabs" role="tablist" aria-label={`${active.name}工作区`}>
              <S2Tab id="edit" cur={tab} on={setTab}>编辑</S2Tab>
              <S2Tab id="coach" cur={tab} on={setTab}>教练{coachTurns ? <span className="snow-tab-count">{coachTurns}</span> : null}</S2Tab>
              <S2Tab id="history" cur={tab} on={setTab}>历史</S2Tab>
              <S2Tab id="ref" cur={tab} on={setTab}>引用上下文</S2Tab>
            </div>

            {tab === "edit" && (
              <React.Fragment>
                <S2AiBar stepName={active.name} canGenerate={!isTableStep} emphasize={stepBlank}
                  primary={isTableStep ? <S2SceneAiActions step={activeKey} ai={sceneAI} emphasize={stepBlank} sceneRows={((scaffolds.scenes || {}).list) || []} plans={(scaffolds.planning || {}).plans} /> : null}
                  structBusy={structBusy} busyTarget={genTarget} dirBusy={dirBusy} onGenerate={generateStep} onDirections={() => gen.requestDirections("")}
                  brief={brief} usage={briefUsage} health={curHealth} onOpenCoach={() => setTab("coach")}
                  onRegenWithBrief={regenWithBrief} err={genErr} onClearErr={() => gen.clearGenErr(activeKey)} />
                <S2StepEditor step={active} data={data} draft={draft} setDraft={setDraft}
                  scaffold={scaffolds[activeKey]} onScaffold={updateScaffold} refs={scaffolds} go={selectStep}
                  ai={isTableStep ? sceneAI : isCharStep ? charAI : undefined} onOpenChapterPlan={openChapterPlan}
                  catalogHasChapters={catalogChapters.length > 0} />
              </React.Fragment>
            )}
            {tab === "coach" && (
              <S2Coach active={active} beKey={S2_BE_KEY[activeKey]} history={coach.coachHist} busy={coach.coachBusy} dirBusy={dirBusy}
                focusRow={activeKey === "planning" ? ((scaffolds.planning || {}).sel || "") : ""} focusLabel={aiFocus ? aiFocus.label : null} sceneLabel={sceneLabel} freeText={!data.scaffold}
                onSend={coach.sendCoach} onDirections={gen.requestDirections} onApplyPatch={coach.applyCoachPatch}
                onAdoptDirection={adoptDirection} onAdoptDirectionAsText={adoptDirectionAsText}
                brief={brief} briefBusy={coach.briefBusy} onSaveBrief={coach.saveBrief}
                briefUsage={briefUsage} onRegenWithBrief={regenWithBrief} structBusy={structBusy} busyTarget={genTarget}
                err={genErr} onClearErr={() => gen.clearGenErr(activeKey)} />
            )}
            {tab === "history" && <S2History history={history} go={selectStep} onRestore={restoreSnap} />}
            {tab === "ref" && <S2Ref active={active} drafts={drafts} scaffolds={scaffolds} />}
          </div>

          <S2Footer step={active} idx={idx} settled={stepSettled} blank={stepBlank} syncState={syncState} savedAt={savedAt} retryBusy={syncRetryBusy} onRetry={retrySnowSync}
            onExport={exportOutline} onSkip={skipStep} onConfirm={confirmStep} onPrev={() => goStep(idx - 1)} onNext={() => goStep(idx + 1)} />
        </section>

        {/* right — 本步上下文：宽屏是可收起的第三栏，窄屏折成抽屉 */}
        <aside className="snow-ctx" id="snow-ctx" ref={ctxRef} tabIndex={-1}
          aria-label="本步上下文"
          {...(narrow ? { role: "dialog", "aria-modal": "true" } : {})}
          onKeyDown={(e) => { if (narrow && ctxOpen && e.key === "Escape" && !isImeComposing(e)) { e.preventDefault(); e.stopPropagation(); setCtxOpen(false); } }}>
          <div className="sf-ctx-drawer-head">
            <span className="fw-600">本步上下文</span>
            <CloseButton className="wr-drawer-x" label="关闭本步上下文" title="关闭（Esc）" onClick={() => setCtxOpen(false)} />
          </div>
          <S2Rail step={active} guide={data.guide} health={curHealth}
            stepScaffold={isTableStep ? scaffolds[activeKey] : null}
            scenesScaffold={activeKey === "planning" ? scaffolds.scenes : null}
            para={active.track === "plot" && activeKey !== "paragraph" ? scaffolds.paragraph : null}
            checks={checks[activeKey]} onToggle={toggleCheck} brief={brief} onOpenBrief={openBriefInCoach}
            go={selectStep} guideDefaultOpen={guideFirstVisit} />
        </aside>
        <div className={`sf-ctx-scrim ${ctxOpen ? "show" : ""}`} aria-hidden="true" onClick={() => setCtxOpen(false)} />
      </div>

      {upDiff && <S2UpstreamDiff diff={upDiff} onClose={() => setUpDiff(null)} />}
      {snapDiff && (
        <S2SnapDiff h={snapDiff} current={{ draft: drafts[snapDiff.key] || "", scaffold: scaffolds[snapDiff.key] }}
          onApply={() => applySnap(snapDiff)} onClose={() => setSnapDiff(null)} />
      )}
      {importOpen && (
        <S2ImportPlanDialog value={importText} busy={importBusy} error={importError}
          onChange={setImportText} onImport={importCanonicalPlan}
          onClose={() => { setImportOpen(false); setImportError(""); }} />
      )}
      {resetOpen && (
        <S2ResetDialog workTitle={workTitle()} onExport={exportOutline} onConfirm={resetAll} onClose={() => setResetOpen(false)} />
      )}
      {chapterPlanOpen && (
        <WsChapterPlanPanel onClose={() => setChapterPlanOpen(false)} onDone={onChapterPlanDone}
          onGoToStep={goToMaterializationStep} onGoToScene={goToPlanScene} />
      )}

      <UndoToast toast={toast} onClose={clearToast} />
    </div>
  );
}

/* 构思路由的入口（ws-app 的 LazyWsConstruct）。只做两件事：接住挂载前留下的目标步骤
   （旧握手 window.__snowStepTarget），以及告诉视图意图队列「雪花页就绪」——之后的跳步、跳场由
   WsSnowflake 就地处理。以前这里按目标步骤给 WsSnowflake 换 key，每次外部跳步整张视图重挂。 */
function WsConstruct() {
  const [initialStep] = useSS(() => window.__snowStepTarget || null);
  useSE(() => {
    window.__snowStepTarget = null;
    setViewIntentTargetReady("snowflake");
    return () => setViewIntentTargetReady("snowflake", false);
  }, []);
  return <WsSnowflake initialStep={initialStep} />;
}

/* 主页速览（smoke-f3 读 window.s2StepSummary）。其余旧的 window 导出没有读者，已去掉。 */
window.s2StepSummary = s2StepSummary;

export { WsSnowflake, WsConstruct };
export {
  S2_STEPS, S2_BE_STEPS, s2PacingRuns, s2LineStats, s2NormalizeState, s2NextSceneRowId,
  s2PlanSlots, s2PlanState, s2PlanAuto, s2StaleMap, s2UpstreamDrift, s2ReorderScenes,
} from "./ws-snow-model.js";
export { s2StepSummary, s2ExportState } from "./ws-snow-hooks.js";
