import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";
import { UndoToast, useUndoToast } from "./ws-undo-toast.jsx";
import { WsChapterPlanPanel } from "./ws-snow-chapters.jsx";
import { planIntentsForScene } from "./ws-scene-design.jsx";
import { arrChapterChecks, arrIsPlanChapter, arrIsPlanScene } from "./ws-author-derive.js";
import {
  arrGoView, arrStampIds, useArrChapterList, useArrPref, useAuthorSnow, useChapterDnd, useCtxDrawer, useSceneDnd, useSelection,
} from "./ws-author-hooks.js";
import { ArrOverview, ArrOverviewHead } from "./ws-author-overview.jsx";
import { ArrEditor } from "./ws-author-detail.jsx";
import { ArrChapterContext, ArrRail } from "./ws-author-side.jsx";
import { EmptyState, Spinner } from "./ws-ui.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { chapterLabel, chapterOwnTitle } from "./ws-labels.js";

/* ==========================================================
   章节编排 — Chapter Arrangement（外壳）
   两种模式：
   · 全书编排（overview，ws-author-overview.jsx）—— 结构 / 节奏两个镜头 + 全书体检 + 按卷分组的章节清单
   · 章节详情（detail）—— 序列栏（ws-author-side.jsx）· 编辑器（ws-author-detail.jsx：构思条 / 场景看板 / 交接 /
     戏剧卡 / AI 编排）· 章节体检（ws-author-side.jsx）
   外壳只管：接住目录、现在看哪一章 / 哪种模式、章与场的改动怎么写回、「整理章节结构」面板开在哪。
   管线在 ws-author-hooks.js，小零件在 ws-author-ui.jsx，AI 编排在 ws-author-ai.jsx，派生事实在 ws-author-derive.js。

   阶段 Z「一张章表、两扇门」：雪花整理出来的章（structure.owner === "plan"）是构思分章的延续，不是另一份。
   · 章的结构（哪几场归哪一章、先后、幕）只有一个编辑器——「整理章节结构」面板，这里直接开得出来；
     这样的章在清单上不能拖（后端同样 409），手建的章照常拖、也能用方向键挪。
   · 章名两边改的是同一个名字（后端写穿到章计划，07 章节表 / 09 章头 / 分章面板跟着变）。
   · 章级的入口出口 / 视角 · 时空没有编辑入口：作者没填过就从各场读出来（ws-author-derive.js），不再留空壳。
   章级张力 / 线索（旧原型的「故事弧线」「线索织布机」）没有编辑入口、也没有东西喂它，连同 UI 一起删了；
   目录 store 里的 tension / threads 字段是后端契约，原样留着。
   ========================================================== */

const { useState, useEffect, useMemo, useCallback, useRef } = React;

/* 雪花的场（design.owner === "plan"）：设计字段不在这里改——行上已经是只读的，写回时再兜一层，
   免得别的入口（快捷键、以后新加的按钮）把一个注定被后端 409 的改动乐观写进本机目录。 */
const ARR_DESIGN_KEYS = ["goal", "obstacle", "turn", "povName", "kind"];

/* 构思分出来的章：删掉的只是目录里的章和场景卡，构思里的分章还在——下一次「确认写入」会把这些场取回。
   作者要的多半是并章 / 拆章（用「整理章节结构」），或者真不要这几场（在构思第 9 步删行）。删之前说清楚。 */
const planDeleteNote = (targets) => (targets.some(arrIsPlanChapter)
  ? "\n\n其中有构思里分出来的章：这里删掉的只是目录里的章和场景卡，构思的分章还在——下一次「整理章节结构 → 确认写入」会把这些场取回。\n想并章 / 拆章，用「整理章节结构」；真不要这几场，到构思第 9 步删行。"
  : "");

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

  /* —— 整理章节结构（阶段 Z）：和构思页头的同名按钮是同一张面板、同一条落库路径（SnowSync.materialize）。
     章的结构只有这一个编辑器；章节编排只是它的第二扇门。确认写入之后目录整份重拉，这里给一句回执。 */
  const [planOpen, setPlanOpen] = useState(false);
  const openPlan = () => {
    if (!window.SnowSync || !window.SnowSync.chapterPreview) { notifyError("分章能力还没准备好，请刷新页面后再试。"); return; }
    setPlanOpen(true);
  };
  const onPlanDone = (result) => {
    setPlanOpen(false);
    const r = result || {};
    const notes = [
      (r.trashed_placeholder_chapters || []).length ? `${r.trashed_placeholder_chapters.length} 个没动过笔的空白占位章已移入回收站` : "",
      (r.trashed_empty_chapters || []).length ? `${r.trashed_empty_chapters.length} 个变空的旧章已移入回收站` : "",
      (r.restored_chapter_ids || []).length ? `${r.restored_chapter_ids.length} 章从回收站取回` : "",
      (r.restored_scene_ids || []).length ? `${r.restored_scene_ids.length} 场随旧章进了回收站的场景卡已取回` : "",
      r.chapter_order_held ? "目录里有已终审的章，按章表排会挪动它——新章暂时接在最后" : "",
    ].filter(Boolean);
    showNotice({ text: "章节结构已按这一版写入目录" + (notes.length ? ` · ${notes.join(" · ")}` : ""), tone: "ok", timeout: 9000 });
    refreshResync();
  };
  const goToSnowStep = (beKey) => {
    const feKey = (window.SnowSync && window.SnowSync.feStepKey && window.SnowSync.feStepKey(beKey)) || "";
    setPlanOpen(false);
    goView("snowflake", feKey ? { type: "ws:snow-step", detail: feKey } : null);
  };
  const goToSnowScene = (sceneId) => {
    setPlanOpen(false);
    goView("snowflake", planIntentsForScene(sceneId));
  };
  const planPanel = planOpen
    ? <WsChapterPlanPanel onClose={() => setPlanOpen(false)} onDone={onPlanDone} onGoToStep={goToSnowStep} onGoToScene={goToSnowScene} />
    : null;

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
    const catalogError = WsCatalog.loadError && WsCatalog.loadError();
    return (
      <div className="page arr-state" data-screen-label="author · loading">
        {catalogError ? (
          <EmptyState icon="AlertTriangle" title="章节目录加载失败"
            actions={<button type="button" className="btn btn-ghost" onClick={() => WsCatalog.__refresh && WsCatalog.__refresh()}><I.Refresh size={13} /> 重试加载</button>}>
            系统不会把请求失败当成空作品，也不会用空目录覆盖服务端。
          </EmptyState>
        ) : (
          <div className="arr-loading" role="status"><Spinner size={16} /> 正在从服务端加载章节目录…</div>
        )}
        {/* 分章面板确认写入时目录会整份重拉（WsCatalog.reset）：面板留在原地把这一步走完 */}
        {planPanel}
      </div>
    );
  }

  /* 空白作品：服务端已确认没有任何章节，先引导建立结构 */
  if (!ch) {
    const createFirst = () => addChapter();
    return (
      <div className="page arr-state" data-screen-label="author · empty">
        <EmptyState icon="Layers" title="这部作品还没有章节结构"
          actions={snow.ready ? (
            <>
              <button type="button" className="btn btn-accent" data-testid="author-empty-open-plan" onClick={openPlan}><I.Layout size={15} /> 整理章节结构</button>
              <button type="button" className="btn btn-ghost" onClick={createFirst}><I.Plus size={15} /> 新建第一章</button>
            </>
          ) : (
            <>
              <button type="button" className="btn btn-accent" onClick={createFirst}><I.Plus size={15} /> 新建第一章</button>
              <button type="button" className="btn btn-ghost" onClick={() => goView("snowflake")}>去构思</button>
            </>
          )}>
          {snow.ready
            ? "构思里的场景已经列好了——把它们整理成章节，章和场就长到这里来；也可以自己从第一章建起。"
            : "章节编排从第一章开始；也可以先去雪花构思，把大纲长出来再回来编排。"}
        </EmptyState>
        {planPanel}
      </div>
    );
  }

  const idx = chapters.findIndex((c) => c.id === ch.id);
  const prev = idx > 0 ? chapters[idx - 1] : null;
  const next = idx >= 0 && idx < chapters.length - 1 ? chapters[idx + 1] : null;
  const locked = ch.state === "approved";
  const checks = arrChapterChecks(ch, snow);
  const warnCount = checks.filter((row) => row.warn).length;

  const openChapterScene = (id, sceneIndex) => {
    openChapter(id);
    const target = byId[id];
    const scene = target && (target.scenes || [])[sceneIndex || 0];
    setHighlightSid(scene ? scene.sid : null);
  };

  /* —— 当前章的改动：一律经 commitChapters 交给目录（乐观写 + 服务端收敛） —— */
  const updateCurrent = (fn) => commitChapters((cs) => cs.map((c) => (c.id === ch.id ? fn(c) : c)));
  const addScene = () => {
    if (locked) return;
    updateCurrent((c) => ({ ...c, scenes: [...c.scenes, { sid: "s_" + Math.random().toString(36).slice(2, 8), title: "未命名场景", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" }] }));
  };
  const patchTitle = (val) => { if (!locked) updateCurrent((c) => ({ ...c, title: val })); };
  const patchDrama = (key, val) => {
    if (locked) return;
    updateCurrent((c) => {
      const nextCh = { ...c, drama: { ...c.drama, [key]: val } };
      if (key === "promise") nextCh.promise = val;
      return nextCh;
    });
  };
  const editScene = (i, patch) => {
    if (locked) return;
    const allowed = arrIsPlanScene(ch.scenes[i])
      ? Object.fromEntries(Object.entries(patch || {}).filter(([key]) => !ARR_DESIGN_KEYS.includes(key)))
      : patch;
    if (!allowed || !Object.keys(allowed).length) return;
    updateCurrent((c) => ({ ...c, scenes: c.scenes.map((sc, k) => (k === i ? { ...sc, ...allowed } : sc)) }));
  };
  const cycleKind = (i) => {
    if (locked || arrIsPlanScene(ch.scenes[i])) return;
    updateCurrent((c) => ({ ...c, scenes: c.scenes.map((sc, k) => (k === i ? { ...sc, kind: sc.kind === "主动" ? "反应" : "主动" } : sc)) }));
  };

  /* 删完给一条回执（删了几条 / 去了哪 / 怎么找回）——不然作者只能看着东西消失 */
  const openTrash = () => goView("trash");
  const noticeTrashed = (text) => showNotice({ text, actionLabel: "打开回收站", onAction: openTrash });
  const deleteScene = (i) => {
    if (locked) return;
    const victim = ch.scenes[i];
    updateCurrent((c) => ({ ...c, scenes: c.scenes.filter((_, k) => k !== i) }));
    noticeTrashed(`已把场景「${(victim && victim.title) || "未命名场景"}」移入回收站`);
  };
  const deleteChapter = async () => {
    if (locked) return;
    const target = ch;
    const scenes = (target.scenes || []).length;
    const ok = await wsConfirm({
      title: `把${chName(target)}移入回收站？`,
      body: `${scenes ? `连同章下 ${scenes} 个场景。` : "这一章还没有场景。"}可以在回收站里恢复。${planDeleteNote([target])}`,
      confirmLabel: "移入回收站",
      tone: "danger",
    });
    if (!ok) return;
    const cs = chaptersRef.current;
    const i = cs.findIndex((c) => c.id === target.id);
    const neighbor = cs[i + 1] || cs[i - 1];
    commitChapters((all) => all.filter((c) => c.id !== target.id));
    if (neighbor) { setPickedId(neighbor.id); setHighlightSid(null); }
    noticeTrashed(`已把${chName(target, false)}移入回收站${scenes ? `（含 ${scenes} 场）` : ""}`);
  };

  /* —— 批量删除 ——
     已批准终稿始终排除在选择之外：后端也会 blocked，这里先在 UI 上说清楚，
     免得作者勾了一批、只删掉一部分还得自己对账。 */
  const selectableChapters = chapters.filter((c) => c.state !== "approved");
  const selectedChapters = selectableChapters.filter((c) => chSel.has(c.id));
  const chapterBatch = {
    mode: chSel.mode,
    has: chSel.has,
    onToggle: chSel.toggle,
    onEnter: chSel.enter,
    onExit: chSel.exit,
    selectedCount: selectedChapters.length,
    selectableCount: selectableChapters.length,
    allSelected: !!selectableChapters.length && selectedChapters.length === selectableChapters.length,
    onSelectAll: () => chSel.toggleAll(selectableChapters.map((c) => c.id)),
    onDelete: async () => {
      const targets = selectedChapters;
      if (!targets.length) return;
      const scenes = targets.reduce((n, c) => n + (c.scenes || []).length, 0);
      const what = targets.length === 1
        ? chName(targets[0])
        : `所选 ${targets.length} 章`;
      const ok = await wsConfirm({
        title: `把${what}移入回收站？`,
        body: `${scenes ? `连同章下 ${scenes} 个场景。` : ""}可以在回收站里恢复。${planDeleteNote(targets)}`,
        confirmLabel: "移入回收站",
        tone: "danger",
      });
      if (!ok) return;
      const drop = new Set(targets.map((c) => c.id));
      const survivor = chaptersRef.current.find((c) => !drop.has(c.id));
      commitChapters((cs) => cs.filter((c) => !drop.has(c.id)));
      chSel.exit();
      if (drop.has(pickedId)) { setPickedId(survivor ? survivor.id : null); setHighlightSid(null); }
      noticeTrashed(`已把 ${targets.length} 章移入回收站${scenes ? `（含 ${scenes} 场）` : ""}`);
    },
  };
  const sceneBatch = {
    mode: scSel.mode,
    has: scSel.has,
    onToggle: scSel.toggle,
    onToggleMode: scSel.toggleMode,
    onSelectAll: () => scSel.toggleAll(ch.scenes.map((s) => s.sid)),
    onDelete: async () => {
      if (locked) return;
      const targets = ch.scenes.filter((s) => scSel.has(s.sid));
      if (!targets.length) return;
      const what = targets.length === 1 ? `场景「${targets[0].title}」` : `本章 ${targets.length} 个场景`;
      const ok = await wsConfirm({ title: `把${what}移入回收站？`, body: "可以在回收站里恢复。", confirmLabel: "移入回收站", tone: "danger" });
      if (!ok) return;
      const drop = new Set(targets.map((s) => s.sid));
      updateCurrent((c) => ({ ...c, scenes: c.scenes.filter((s) => !drop.has(s.sid)) }));
      scSel.exit();
      setHighlightSid(null);
      noticeTrashed(`已把 ${targets.length} 个场景移入回收站`);
    },
  };

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
            <ArrEditor ch={ch} num={numOf[ch.id]} prev={prev} next={next} numOf={numOf}
              sceneDnd={scDnd} onAddScene={addScene} onCycleKind={cycleKind} onDeleteScene={deleteScene} onEditScene={editScene}
              onPatchTitle={patchTitle} onPatchDrama={patchDrama} onDeleteChapter={deleteChapter} onOpenTrash={openTrash}
              highlightSid={highlightSid} onClearHighlight={() => setHighlightSid(null)}
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
