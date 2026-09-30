import React from "react";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";
import { UndoToast, useUndoToast } from "./ws-undo-toast.jsx";
import { arrChapterChecks } from "./ws-author-derive.js";
import {
  arrGoView, arrStampIds, useArrChapterList, useArrPref, useAuthorSnow, useChapterDnd, useCtxDrawer, useSceneDnd, useSelection,
} from "./ws-author-hooks.js";
import { useArrPlanDoor } from "./ws-author-plan-door.jsx";
import { arrChapterEdits } from "./ws-author-edits.js";
import { arrBatches } from "./ws-author-batch.js";
import { ArrEmptyState, ArrLoadingState } from "./ws-author-states.jsx";
import { ArrOverview, ArrOverviewHead } from "./ws-author-overview.jsx";
import { ArrEditor } from "./ws-author-detail.jsx";
import { ArrChapterContext, ArrRail } from "./ws-author-side.jsx";
import { chapterLabel, chapterOwnTitle } from "./labels/catalog.js";

/* ==========================================================
   章节编排 — Chapter Arrangement（外壳）
   两种模式：
   · 全书编排（overview，ws-author-overview.jsx）—— 结构 / 节奏两个镜头 + 全书体检 + 按卷分组的章节清单
   · 章节详情（detail）—— 序列栏（ws-author-side.jsx）· 编辑器（ws-author-detail.jsx：构思条 / 场景看板 / 交接 /
     戏剧卡 / AI 编排）· 章节体检（ws-author-side.jsx）
   外壳只管：接住目录、现在看哪一章 / 哪种模式、焦点跟着模式走，再把各块拼起来。其余零件：
   管线（ws-author-hooks.js）、「整理章节结构」这扇门（ws-author-plan-door.jsx）、当前章的改动（ws-author-edits.js）、
   批量删除（ws-author-batch.js）、读取中 / 空作品的版面（ws-author-states.jsx）、小零件（ws-author-ui.jsx）、
   AI 编排（ws-author-ai.jsx）、派生事实（ws-author-derive.js）。

   阶段 Z「一张章表、两扇门」：雪花整理出来的章（structure.owner === "plan"）是构思分章的延续，不是另一份。
   · 章的结构（哪几场归哪一章、先后、幕）只有一个编辑器——「整理章节结构」面板，这里直接开得出来；
     这样的章在清单上不能拖（后端同样 409），手建的章照常拖、也能用方向键挪。
   · 章名两边改的是同一个名字（后端写穿到章计划，07 章节表 / 09 章头 / 分章面板跟着变）。
   · 章级的入口出口 / 视角 · 时空只从各场读出来（ws-author-derive.js）；章级张力 / 线索这一簇字段已经退役。
   ========================================================== */

const { useState, useEffect, useMemo, useCallback, useRef } = React;

function WsAuthor({ go }) {
  const catalogChapters = useCatalogChapters();
  const [mode, setMode] = useArrPref("arr.mode", "overview");
  const [lens, setLens] = useArrPref("arr.lens", "spine");
  const [pickedId, setPickedId] = useArrPref("arr.picked", null);
  /* 从结构镜头点进来的那一场：短暂标出来，几秒后或下一次点击就撤掉 */
  const [highlightSid, setHighlightSid] = useState(null);
  /* 体检抽屉：窗口宽过抽屉断点就自己收起（焦点陷阱不能留在一个看不见的抽屉上） */
  const [ctxOpen, setCtxOpen] = useCtxDrawer();
  const list = useArrChapterList(catalogChapters);
  const { chapters, setChapters, chaptersRef, commit: commitChapters } = list;
  const chSel = useSelection();
  const scSel = useSelection();
  const { toast, show: showNotice, clear: clearNotice } = useUndoToast();
  const notifyError = (text) => showNotice({ text, tone: "danger", timeout: 9000 });
  const goView = useCallback((view, intents) => arrGoView(go, view, intents), [go]);

  /* 接住目录：服务端目录一变，本页的章表整份换掉 */
  useEffect(() => {
    const prevList = chaptersRef.current || [];
    const next = arrStampIds(Array.isArray(catalogChapters) ? catalogChapters : []);
    setChapters(next);
    /* 章的 id 是位置式的（ch05 = 第 5 章）：前面删了、插了一章，同一个 id 就指向了别的章。
       先按后端 chapter_id 认回原来那一章；乐观新建、还没有后端 id 的章按它的位置找回。 */
    setPickedId((current) => {
      const prevAt = prevList.findIndex((c) => c.id === current);
      const prevCh = prevAt >= 0 ? prevList[prevAt] : null;
      if (prevCh && prevCh.backendId) {
        const same = next.find((c) => c.backendId === prevCh.backendId);
        if (same) return same.id;
      }
      if (next.some((item) => item.id === current)) return current;
      if (prevCh && !prevCh.backendId && next[prevAt]) return next[prevAt].id;
      const fallback = next.find((item) => item.current) || next[0];
      return fallback ? fallback.id : null;
    });
    chSel.prune(new Set(next.map((item) => item.id)));
    scSel.prune(new Set(next.flatMap((item) => (item.scenes || []).map((s) => s.sid))));
    // 只跟着目录走：选择集的 prune 用的是函数式更新，拿哪一次渲染的都一样
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalogChapters]);
  useEffect(() => {
    if (!highlightSid) return undefined;
    const timer = setTimeout(() => setHighlightSid(null), 4000);
    return () => clearTimeout(timer);
  }, [highlightSid]);

  const byId = useMemo(() => Object.fromEntries(chapters.map((c) => [c.id, c])), [chapters]);
  const numOf = useMemo(() => Object.fromEntries(chapters.map((c, i) => [c.id, String(i + 1).padStart(2, "0")])), [chapters]);
  // 「第 3 章「盐场」」；占位章名（第 N 章 / 未命名）不再跟在章号后面重复一遍
  const chName = (c, withTitle = true) => {
    const head = chapterLabel({ n: numOf[c.id] }, { withTitle: false });
    const own = withTitle ? chapterOwnTitle(c) : "";
    return own ? `${head}「${own}」` : head;
  };
  const ch = byId[pickedId] || chapters[0] || null;

  /* ⚠ 这些 hook 必须在下方「加载中 / 空作品」的 early return 之前调用（React hooks 顺序规则） */
  const { snow, refreshResync } = useAuthorSnow({ chapters, reload: list.reload, showNotice, notifyError, goView });
  const chDnd = useChapterDnd({ ...list, notifyError });
  const scDnd = useSceneDnd({ ...list, ch });
  const { openPlan, planPanel } = useArrPlanDoor({ goView, showNotice, refreshResync });
  const checks = useMemo(() => (ch ? arrChapterChecks(ch, snow) : []), [ch, snow]);

  /* 全书编排 ⇄ 章节详情整块换掉：焦点跟过去——进详情落在这一章上（section，tabIndex=-1），
     回全书编排落在刚才那一章的标题按钮上。过去两边都掉到 <body>，键盘用户得从页首重新 Tab。 */
  const shellRef = useRef(null);
  const focusAfterModeRef = useRef(null);
  useEffect(() => {
    const want = focusAfterModeRef.current;
    if (!want || want !== mode) return;
    focusAfterModeRef.current = null;
    const shell = shellRef.current;
    const target = shell && (want === "detail"
      ? shell.querySelector(".arr-ed")
      : shell.querySelector(".arr-card.is-picked .arr-card-title"));
    if (target && typeof target.focus === "function") target.focus({ preventScroll: true });
  }, [mode]);
  const backToOverview = () => { focusAfterModeRef.current = "overview"; setMode("overview"); };

  /* 新建一章：全应用只有 WsCatalog.addChapter 这一份配方；这里只决定它接在哪里 */
  const openChapter = (id) => {
    if (mode !== "detail") focusAfterModeRef.current = "detail";
    setPickedId(id); setHighlightSid(null); setMode("detail"); setCtxOpen(false);
    scSel.exit();   // 换章即清空场景勾选，避免跨章误删
  };
  const addChapter = (opts) => {
    const created = WsCatalog.addChapter(opts || {});
    if (created && created.id) openChapter(created.id);
  };

  if (!WsCatalog.ready()) {
    const refetch = () => WsCatalog.__refresh && WsCatalog.__refresh();
    return <ArrLoadingState error={WsCatalog.loadError && WsCatalog.loadError()} onRetry={refetch}>{planPanel}</ArrLoadingState>;
  }

  /* 空白作品：服务端已确认没有任何章节，先引导建立结构 */
  if (!ch) {
    return (
      <ArrEmptyState snowReady={snow.ready} onOpenPlan={openPlan} onCreateFirst={() => addChapter()} onGoSnow={() => goView("snowflake")}>
        {planPanel}
      </ArrEmptyState>
    );
  }

  const idx = chapters.findIndex((c) => c.id === ch.id);
  const prev = idx > 0 ? chapters[idx - 1] : null;
  const next = idx >= 0 && idx < chapters.length - 1 ? chapters[idx + 1] : null;
  const warnCount = checks.filter((row) => row.warn).length;

  const openChapterScene = (id, sceneIndex) => {
    openChapter(id);
    const target = byId[id];
    const scene = target && (target.scenes || [])[sceneIndex || 0];
    setHighlightSid(scene ? scene.sid : null);
  };

  /* 删完给一条回执（删了几条 / 去了哪 / 怎么找回）——不然作者只能看着东西消失 */
  const openTrash = () => goView("trash");
  const noticeTrashed = (text) => showNotice({ text, actionLabel: "打开回收站", onAction: openTrash });
  const pickChapter = (id) => { setPickedId(id); setHighlightSid(null); };
  const clearHighlight = () => setHighlightSid(null);
  const edits = arrChapterEdits({ ch, commitChapters, chaptersRef, chName, noticeTrashed, pickChapter });
  const { chapterBatch, sceneBatch } = arrBatches({
    chapters, ch, chSel, scSel, pickedId, commitChapters, chaptersRef, updateCurrent: edits.updateCurrent,
    chName, noticeTrashed, pickChapter, clearHighlight,
  });

  const refreshData = async () => {
    if (!WsCatalog.__refresh) return;
    await WsCatalog.__refresh();
    const failure = WsCatalog.loadError && WsCatalog.loadError();
    if (failure) {
      notifyError((failure && failure.message) || "章节目录刷新失败；当前视图仍保留上一次服务端版本。");
      return;
    }
    list.reload();
  };
  const onConfigureModel = () => goView("settings", { type: "ws:settings-tab", detail: "ai" });
  const chapterRun = {
    onCatalogRefresh: list.reload,
    onOpenReview: () => goView("manuscripts"),
    onConfigureModel,
  };
  const closeCtx = () => setCtxOpen(false);

  return (
    <div className="arr-shell" ref={shellRef} data-screen-label="author" data-mode={mode} data-dragging-ch={chDnd.dragId ? "true" : undefined}>
      <div className={`arr-main ${mode === "overview" ? "arr-main-ov" : "arr-main-detail"} ${ctxOpen ? "is-ctx-open" : ""}`}>
        {mode === "overview" ? (
          <>
            <ArrOverviewHead batch={chapterBatch} snow={snow} onOpenPlan={openPlan} onRefresh={refreshData} onMode={setMode}
              onNew={() => addChapter({ afterId: byId[pickedId] ? pickedId : undefined })}
              newTip={byId[pickedId] ? `接在第 ${Number(numOf[pickedId])} 章后面` : "接在全书最后"} />
            <ArrOverview chapters={chapters} numOf={numOf} pickedId={pickedId} onOpen={openChapter} onOpenScene={openChapterScene}
              rowDnd={chDnd.row} boardDnd={chDnd.board} onNew={(actId) => addChapter({ act: actId })} lens={lens} setLens={setLens}
              batch={chapterBatch} snow={snow} onOpenPlan={snow.canPlan ? openPlan : null}
              canMove={chDnd.canMove} onMoveChapter={chDnd.move} />
          </>
        ) : (
          <>
            <ArrRail chapters={chapters} numOf={numOf} pickedId={ch.id} onPick={openChapter} rowDnd={chDnd.row} boardDnd={chDnd.board}
              canMove={chDnd.canMove} onMove={chDnd.move}
              onBack={backToOverview} onNew={() => addChapter({ afterId: ch.id })} />
            <ArrEditor ch={ch} chapters={chapters} num={numOf[ch.id]} prev={prev} next={next} numOf={numOf}
              sceneDnd={scDnd} onAddScene={edits.addScene} onCycleKind={edits.cycleKind} onDeleteScene={edits.deleteScene} onEditScene={edits.editScene}
              onPatchTitle={edits.patchTitle} onPatchDrama={edits.patchDrama} onDeleteChapter={edits.deleteChapter} onOpenTrash={openTrash}
              highlightSid={highlightSid} onClearHighlight={clearHighlight}
              onJump={openChapter} onBack={backToOverview} snow={snow} chapterRun={chapterRun} sceneBatch={sceneBatch} onOpenPlan={openPlan}
              ctxOpen={ctxOpen} onToggleCtx={() => setCtxOpen((v) => !v)} warnCount={warnCount}
              goView={goView} onConfigureModel={onConfigureModel} />
            <div className="arr-ctx-scrim" aria-hidden="true" onClick={closeCtx} />
            <ArrChapterContext ch={ch} checks={checks} snow={snow} onOpenPlan={openPlan} open={ctxOpen} onClose={closeCtx}
              onConfigureModel={onConfigureModel} />
          </>
        )}
      </div>
      <UndoToast toast={toast} onClose={clearNotice} />
      {planPanel}
    </div>
  );
}

export { WsAuthor };
