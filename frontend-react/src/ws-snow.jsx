import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { WsChapterPlanPanel, keepFocusOnDialogBackdrop } from "./ws-snow-chapters.jsx";
import { isImeComposing } from "./ws-dialog.jsx";
import { CloseButton, Tag } from "./ws-ui.jsx";
import { SnowSync } from "./ws-snow-sync.jsx";
import { navigateWithViewIntent, setViewIntentTargetReady } from "./ws-view-intents.js";
import { useUndoToast, UndoToast } from "./ws-undo-toast.jsx";
import {
  S2_STEPS, S2_BE_STEPS, S2_BE_KEY, S2_STEP_DATA, TRACK_LABEL,
  s2BlankScaffolds, s2Content, s2PrependHistory, s2SceneNo, s2StaleMap,
} from "./ws-snow-model.js";
import {
  activeWorkId, s2Key, s2WorkIdOfKey, useSnowDocument, useSnowEvents, useSnowNotices, useSnowSyncMirror, useStableCallback,
} from "./ws-snow-hooks.js";
import { useSnowGeneration } from "./ws-snow-generation.js";
import {
  useSnowAiActions, useSnowContextRail, useSnowKeyboard, useSnowLanding, useSnowMoreMenu, useSnowStepFlow, useSnowWorkbenchApi,
} from "./ws-snow-workbench.jsx";
import { S2StepEditor } from "./ws-snow-scaffolds.jsx";
import { S2SceneAiActions, useSnowTriage } from "./ws-snow-scenes.jsx";
import { S2AiBar, S2Coach, useSnowCoach } from "./ws-snow-coach.jsx";
import { S2Rail } from "./ws-snow-rail.jsx";
import { S2History, S2Ref, S2ServerVersions, S2SnapDiff, S2UpstreamDiff, S2VersionDiff } from "./ws-snow-history.jsx";
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

   这个文件只剩工作台的状态接线与版面：步骤目录与纯推导在 ws-snow-model.js（门面），缓存 / 通知钩子在
   ws-snow-hooks.js，AI 生成通道在 ws-snow-generation.js，落点 / 右栏 / 步骤流转 / AI 入口 / 更多菜单 / 键盘这些
   行为钩子在 ws-snow-workbench.jsx，编辑器在 ws-snow-scaffolds.jsx（01–08）、ws-snow-scene-list.jsx（09）与 ws-snow-scene-plan.jsx（10），
   09 / 10 的整表 AI 动作与分诊状态在 ws-snow-scenes.jsx，
   教练在 ws-snow-coach.jsx，右栏在 ws-snow-rail.jsx，历史与对照在 ws-snow-history.jsx，外框在 ws-snow-chrome.jsx。
   同步层 SnowSync 直接 import（以前它反过来 import 本文件，视图只好经 window 绕开循环）。
   ========================================================== */

const { useState: useSS, useEffect: useSE, useRef: useSR, useMemo: useSM } = React;

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
  const snowWorkId = s2WorkIdOfKey(myKey);

  /* 十步内容（本机缓存写穿 SnowSync）与同步层镜像。先挂内容：水合时它先重读缓存，
     后面的落点 / 场景目标处理拿到的就是重读之后的状态。 */
  const {
    drafts, setDrafts, scaffolds, setScaffolds, checks, setChecks, states, setStates, history, setHistory, savedAt, latestRef, flushNow, replaceNow,
  } = useSnowDocument(myKey, snowWorkId);
  const { syncState, health: beHealth, resync: resyncInfo, briefTick } = useSnowSyncMirror(snowWorkId);

  /* 落在哪一步、按步骤记住的页签、外部跳步 / 跳场（钩子在 ws-snow-workbench.jsx）。挂在内容之后：
     水合时内容先重读缓存，落点 / 场景目标拿到的是重读之后的状态。 */
  const { activeKey, selectStep, markMoved, tab, setTab, setTabFor, jumpToPlanScene } = useSnowLanding({
    workId: snowWorkId, myKey, initialStep, states, health: beHealth, latestRef, setScaffolds,
  });

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
    setHistory(prev => s2PrependHistory(prev, { t: Date.now(), who, action, note: note || "", key, snap }));
  const snapNow = (key) => {
    const cur = latestRef.current;
    try { return JSON.parse(JSON.stringify({ draft: cur.drafts[key] || "", scaffold: cur.scaffolds[key] })); } catch (e) { return null; }
  };

  /* 教练、生成、分诊三条 AI 通道经工作台 API 调视图（ws-snow-workbench.jsx：挂载时建一次，调用时读最新值）；
     生成回来的教练历史直接交给教练的 setter */
  const api = useSnowWorkbenchApi({
    workId: snowWorkId, activeKey, active, data, drafts, scaffolds, setScaffolds, setDrafts, setTabFor, pushHist, snapNow, showToast, sceneLabel,
  });
  const coach = useSnowCoach(api, tab);
  const gen = useSnowGeneration(api, coach.setCoachHist);
  const tri = useSnowTriage(api);
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
     09/10 自动保存之后自己弹出来）。07 只读章表上某一章的「改名」也开这张面板，并带上那一章
     （{ rowUid, index }），面板拉回预览后把焦点放在它的章名框上（重评 R11）。 */
  const [chapterPlanOpen, setChapterPlanOpen] = useSS(false);
  const [chapterPlanFocus, setChapterPlanFocus] = useSS(null);
  const openChapterPlan = useStableCallback(() => { setChapterPlanFocus(null); setChapterPlanOpen(true); });
  const renameChapter = useStableCallback((target) => { setChapterPlanFocus(target || null); setChapterPlanOpen(true); });
  const goToPlanScene = (sceneId) => {
    setChapterPlanOpen(false);
    jumpToPlanScene(sceneId);
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
  /* 确认 09 / 10 之后场景卡自动跟上构思（SnowSync 的 catalog-synced 通知）：出一句回执。
     作者点「确认本步」时，确认流程随后还会出它自己的回执——两句并成一句（catalogSyncRef 留 4 秒），
     不让「同步了几场」被后一句盖掉；键入后自动补批准的那条路没有第二句，这里直接出。 */
  const catalogSyncRef = useSR(null);
  useSnowEvents({ "ws:catalog-changed": () => setCatalogTick(t => t + 1) });
  useSnowNotices({
    "catalog-synced": (detail) => {
      const d = detail || {};
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

  const { narrow, ctxOpen, setCtxOpen, railShown, toggleContext, ctxRef, ctxBtnRef, ctxExpanded, guideFirstVisit } = useSnowContextRail(activeKey);
  const openBriefInCoach = useStableCallback(() => { setTabFor(activeKey, "coach"); setCtxOpen(false); });

  const {
    goStep, confirmStep, reviewStep, showUpstreamDiff, skipStep, restoreSnap, applySnap, upDiff, setUpDiff, snapDiff, setSnapDiff,
    previewVersion, restoreVersion, versionDiff, setVersionDiff, versionsTick,
  } = useSnowStepFlow({
    activeKey, active, idx, states, setStates, staleMap, curBeStale, pushHist, snapNow, pushToast, showToast, catalogSyncRef,
    selectStep, setTabFor, setDrafts, setScaffolds, setHistory, flushDoc: flushNow, replaceDoc: replaceNow,
  });

  const { regenFromUpstream, aiFocus, adoptDirection, adoptDirectionAsText, generateStep, regenWithBrief, stepAI, isTableStep } = useSnowAiActions({
    gen, tri, activeKey, active, data, scaffolds, structBusy, genTarget, sceneLabel, pushHist, snapNow, setDraft, setTab, showToast, setStates,
  });

  /* 要点镜像在 SnowSync 里；教练回包 / 作者编辑 / 生成回包都会发事件（briefTick 随之变，这里重读） */
  let brief = null;
  let briefUsage = NO_BRIEF_USAGE;
  try { brief = SnowSync.directionBrief(snowWorkId, activeKey) || null; } catch (e) {}
  try { briefUsage = SnowSync.briefUsage(snowWorkId, activeKey) || NO_BRIEF_USAGE; } catch (e) {}
  void briefTick;

  const {
    moreItems, exportOutline, workTitle, resetOpen, setResetOpen, resetAll,
    importOpen, setImportOpen, importText, setImportText, importBusy, importError, setImportError, importCanonicalPlan,
  } = useSnowMoreMenu({
    activeKey, drafts, scaffolds, states, staleMap, staleCount, doneCount, snapNow, pushHist, showToast,
    setDrafts, setScaffolds, setChecks, setStates, setHistory,
  });

  /* 这张视图自己开着模态框（分章面板、导入、清空、两个对照框）时，全局快捷键一律不响 */
  const modalOpen = chapterPlanOpen || importOpen || resetOpen || !!upDiff || !!snapDiff || !!versionDiff;
  useSnowKeyboard({ modalOpen, narrow, ctxOpen, setCtxOpen, confirmStep, idx, goStep });


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
                    <Tag tone="ok" dot testId="snow-confirmed-pill" title="服务器已记下：本步已确认">已确认</Tag>
                  ) : (curHealth && curHealth.revisedAfterApproval) ? (
                    <Tag tone="warn" dot testId="snow-reconfirm-pill" title="确认之后又改过：点「确认本步」重新确认，下游步骤才会按新版本核对">已改动 · 待重新确认</Tag>
                  ) : (
                    <Tag tone="warn" dot title={(curHealth && !curHealth.gateSatisfied) ? "本地已确认，服务器还没确认：前序步骤没确认完——补齐上游各步后会自动确认" : "本地已确认 · 正在同步到服务器…"}>本地已确认</Tag>
                  )
                ) : active.essential ? (
                  <Tag tone="accent" dot title="整理章节结构之前必须确认的一步">必填</Tag>
                ) : (
                  <Tag dot title="可以留空或略过">建议</Tag>
                )}
                {curStale && <Tag tone="warn" dot>需复核</Tag>}
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
                  primary={isTableStep ? <S2SceneAiActions step={activeKey} ai={stepAI} emphasize={stepBlank} sceneRows={((scaffolds.scenes || {}).list) || []} plans={(scaffolds.planning || {}).plans} /> : null}
                  structBusy={structBusy} busyTarget={genTarget} dirBusy={dirBusy} onGenerate={generateStep} onDirections={() => gen.requestDirections("")}
                  brief={brief} usage={briefUsage} health={curHealth} onOpenCoach={() => setTab("coach")}
                  onRegenWithBrief={regenWithBrief} err={genErr} onClearErr={() => gen.clearGenErr(activeKey)} />
                <S2StepEditor step={active} data={data} draft={draft} setDraft={setDraft}
                  scaffold={scaffolds[activeKey]} onScaffold={updateScaffold} refs={scaffolds} go={selectStep}
                  ai={stepAI} onOpenChapterPlan={openChapterPlan} onRenameChapter={renameChapter}
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
            {tab === "history" && (
              <div className="sf-history">
                {/* 上面是这一步在服务器上的每一版（R15a：换了浏览器、整步被清空也找得回），下面是这台电脑上的操作记录 */}
                <S2ServerVersions workId={snowWorkId} step={active} refreshKey={versionsTick} onPreview={(item) => previewVersion(activeKey, item)} />
                <section className="sf-history-local" aria-labelledby="sf-history-local-title">
                  <h3 className="sf-history-title" id="sf-history-local-title">本机的操作记录</h3>
                  <S2History history={history} go={selectStep} onRestore={restoreSnap} />
                </section>
              </div>
            )}
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
        <S2SnapDiff h={snapDiff} current={{ draft: drafts[snapDiff.key] || "", scaffold: scaffolds[snapDiff.key] }} refs={scaffolds}
          onApply={() => applySnap(snapDiff)} onClose={() => setSnapDiff(null)} />
      )}
      {versionDiff && (
        <S2VersionDiff diff={versionDiff} current={{ draft: drafts[versionDiff.key] || "", scaffold: scaffolds[versionDiff.key] }} refs={scaffolds}
          onRestore={restoreVersion} onClose={() => { if (!versionDiff.restoring) setVersionDiff(null); }} />
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
          onGoToStep={goToMaterializationStep} onGoToScene={goToPlanScene} focusChapter={chapterPlanFocus} />
      )}

      <UndoToast toast={toast} onClose={clearToast} />
    </div>
  );
}

/* 构思路由的入口（ws-app 的 LazyWsConstruct）：告诉视图意图队列「雪花页就绪」——之后的跳步、跳场
   （ws:snow-step / ws:snow-scene）由 WsSnowflake 就地处理。以前这里按目标步骤给 WsSnowflake 换 key，
   每次外部跳步整张视图重挂；更早的 window.__snowStepTarget 握手已由视图意图队列取代。 */
function WsConstruct() {
  useSE(() => {
    setViewIntentTargetReady("snowflake");
    return () => setViewIntentTargetReady("snowflake", false);
  }, []);
  return <WsSnowflake />;
}

export { WsSnowflake, WsConstruct };
