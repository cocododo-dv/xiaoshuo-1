import React from "react";
import { I } from "./icons.jsx";
import { WsDialog } from "./ws-dialog.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { EmptyState, Spinner, Tag } from "./ws-ui.jsx";
import {
  SR_LAYERS, SR_STAGE_STATE_LABEL, buildDimOptions, computeIntensityReadout, computeResynthState, findShadowedBinding,
  srLandingStage, srMapStatus, srPickLandingBook, srPickLatestRun, srReadUiPrefs, srRememberUi, srRunLabel, srStageStates,
} from "./ws-styleref-model.js";
import {
  srActivityApply, srActivityDismiss, srActivityEntries, srActivityFor, srActivitySet, srActivityStart, srActivityStop,
  srBookAction, srBookApplied, srBookById, srBooks, srBooksState, srCancelClassification, srCancelRun, srDeepFor,
  srDeepFresh, srDeleteBook, srDropDeep, srFindingFeedback, srInjectionPreview, srLoadDeep, srMeta, srPollRun,
  srPreviewSamples, srRememberSession, srResumeClassification, srReviewFinding, srRunImport, srSessionUi,
  srSetViewMounted, srSubscribe, srSyncBooks, srSyncMeta, srSynthesize, srUnbind,
} from "./ws-styleref-store.js";
import { srNotify, srActiveWork, SrMenu, useSrStore } from "./ws-styleref-ui.jsx";
import { SrActivityPanel, srActivityView, srRunningFor } from "./ws-styleref-activity.jsx";
import {
  SR_CLOUD_POLICIES, SR_RIGHTS_TERMS, SrImportDialog, SrLibrary, srImportBook, srLossSummary, srPipelineFor, srRightsReady,
} from "./ws-styleref-library.jsx";
import { SrOverview, srRestClassifierLabel } from "./ws-styleref-overview.jsx";
import { SrMatrix, srMatrixEmptyHint } from "./ws-styleref-matrix.jsx";
import { SrProfile } from "./ws-styleref-profile.jsx";
import { SrValidation } from "./ws-styleref-val.jsx";
import { SR_TASKS, SrApply } from "./ws-styleref-apply.jsx";

/* ==========================================================
   风格参考 — 参考书 → 维度矩阵（抽取）→ 风格画像 → 回测校验 → 注入应用
   这个文件只是页面外壳：左栏书库、页头（书名、流水线徽标、抽取 / 更多）、步骤条、各 stage 的切换、
   窄屏书库对话框与导入单子。各 stage 在 ws-styleref-*.jsx，请求与缓存在 ws-styleref-store.js，
   共用的说法与纯判断在 ws-styleref-model.js。
   本文件是风格参考唯一写 window 的地方（冒烟脚本用 window.srSyncBooks），并原样再导出
   拆分前的全部名字（单测都从这里 import）。
   ========================================================== */

const SR_STAGES = [
  { id: "overview",   name: "概览",     icon: "Activity" },
  { id: "matrix",     name: "维度矩阵", icon: "Grid" },
  { id: "profile",    name: "风格画像", icon: "Sparkles" },
  { id: "validation", name: "回测校验", icon: "Flask" },
  { id: "apply",      name: "注入应用", icon: "Sliders" },
];

function WsStyleRef({ go }) {
  /* 书库 / 活动 / 深层数据都是 store 的模块级缓存 + 事件广播；页面订阅这三类事件重渲 */
  useSrStore("books", "activity", "deep");
  const [bookId, setBookId] = React.useState(null);
  /* stage 为 null 表示「按流水线自动落点」：深层数据到了再定落在哪一步 */
  const [stage, setStage] = React.useState(null);
  const [headerBusy, setHeaderBusy] = React.useState(null);
  const [delBusy, setDelBusy] = React.useState(null);
  const [importOpen, setImportOpen] = React.useState(false);
  const [switcherOpen, setSwitcherOpen] = React.useState(false);
  /* .sr-stage-body 是各步共用的滚动容器：换步 / 换书回到顶部，不带着上一步的滚动位置进来 */
  const bodyRef = React.useRef(null);
  const work = srActiveWork();
  const workId = work ? work.id : null;
  const books = srBooks();
  const booksState = srBooksState();
  const book = srBookById(bookId);

  /* 挂载：拉书库（不闪「书库还是空的」）、拉活动清单；导入成功自动切到新书；
     换作品时重算「当前作品在用哪本」。 */
  React.useEffect(() => {
    srSetViewMounted(true);
    srSyncBooks();
    srActivityStart();
    const offImported = srSubscribe("imported")((event) => {
      const id = event && event.detail && event.detail.bookId;
      if (!id) return;
      setBookId(id);
      setStage(null);
      const w = srActiveWork(); // 挂载时的闭包里没有最新的作品：现取
      srRememberSession(w ? w.id : null, { bookId: id, stage: null });
    });
    const onWorkChanged = () => { srSyncMeta(); };
    window.addEventListener("ws:work-changed", onWorkChanged);
    return () => {
      srSetViewMounted(false);
      offImported();
      window.removeEventListener("ws:work-changed", onWorkChanged);
    };
  }, []);

  /* 落点：本次打开应用期间刚看过的那本 → 当前作品在用的书 → 这部作品上次打开的书 → 上次打开的书 → 第一本
     （本机偏好在 ws_sr_ui_v1，本次会话的记录在 store 里，不跨刷新）。 */
  React.useEffect(() => {
    if (book || booksState.phase !== "ready") return;
    if (!books.length) { if (bookId) setBookId(null); return; }
    const meta = srMeta();
    const pick = srPickLandingBook(books, {
      prefs: srReadUiPrefs(),
      workId,
      session: srSessionUi(workId),
      appliedBookIds: meta.appliedBookIds,
      metaSettled: meta.phase === "ready" || meta.phase === "error",
    });
    if (!pick) return;
    setBookId(pick.bookId);
    setStage(pick.stage || null);
  });

  /* 页头徽标与步骤条都要这本书的深层数据（各 stage 自己也会懒加载，这里保证没进 stage 时也有） */
  React.useEffect(() => { if (bookId) srLoadDeep(bookId); }, [bookId]);

  const deep = book ? srDeepFor(book.id) : null;
  const stageStates = book ? srStageStates(book, deep, srRunningFor(book.id)) : null;
  /* 只按本次挂载里新读的深层数据落点：离开期间可能批准了绑定，按旧缓存会落错步 */
  const deepReady = !!(book && deep && deep.loaded && srDeepFresh(book.id));
  React.useEffect(() => { if (bodyRef.current) bodyRef.current.scrollTop = 0; }, [stage, bookId]);
  /* 自动落点只定一次：深层数据到了就把 stage 固定下来，之后作者点哪步就是哪步 */
  React.useEffect(() => {
    if (!book || stage || !deepReady) return;
    setStage(srLandingStage(stageStates, { applied: !!srBookApplied(book.id) }));
  }, [book && book.id, stage, deepReady]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectBook = (id) => {
    setSwitcherOpen(false);
    if (id === bookId) return;
    setBookId(id);
    setStage(null);
    srRememberUi(workId, { bookId: id, stage: null });
    srRememberSession(workId, { bookId: id, stage: null });
  };
  const selectStage = (id) => {
    setStage(id);
    if (!book) return;
    srRememberUi(workId, { bookId: book.id, stage: id });
    srRememberSession(workId, { bookId: book.id, stage: id });
  };

  const runHeaderAction = (id) => {
    if (headerBusy || !book) return;
    setHeaderBusy(id);
    srBookAction(id, book.id).finally(() => setHeaderBusy(null));
  };

  /* 重新分类会先清掉这本书的全部衍生数据（含应用绑定）——动手前说清要丢什么。 */
  const onReclassify = async () => {
    if (!book || headerLockReason(book, headerBusy, "reclassify")) return;
    const ok = await wsConfirm({
      title: `重新分类《${book.title}》？`,
      body: srLossSummary(book, "重新分类会先删除"),
      confirmLabel: "删除并重新分类",
      tone: "danger",
    });
    if (ok) runHeaderAction("reclassify");
  };

  /* 删除参考书：确认 → DELETE（级联清除全部衍生数据）→ 刷新书库；删的是当前书就切到原位置的邻居。 */
  const onDeleteBook = async (b) => {
    if (delBusy || !b) return;
    const ok = await wsConfirm({
      title: `删除参考书《${b.title}》？`,
      body: srLossSummary(b, "会一并删除") + "此操作无法恢复。",
      confirmLabel: "删除这本书",
      tone: "danger",
    });
    if (!ok) return;
    const wasActive = bookId === b.id;
    const idx = srBooks().findIndex((x) => x.id === b.id);
    setDelBusy(b.id);
    try {
      await srDeleteBook(b.id);
      if (wasActive) {
        const left = srBooks();
        const next = left.length ? left[Math.min(Math.max(idx, 0), left.length - 1)] : null;
        setBookId(next ? next.id : null);
        setStage(null);
      }
      srNotify(`已删除参考书《${b.title}》`, "neutral");
    } catch (e) {
      srNotify("删除失败：" + ((e && e.message) || e));
    } finally {
      setDelBusy(null);
    }
  };

  const library = (
    <SrLibrary
      bookId={bookId}
      onSelect={selectBook}
      onImport={() => { setSwitcherOpen(false); setImportOpen(true); }}
      onDelete={onDeleteBook}
      delBusy={delBusy}
    />
  );
  const librarySwitch = <SrLibrarySwitch open={switcherOpen} onOpen={() => setSwitcherOpen(true)} count={books.length} />;

  return (
    <div className="sr-page" data-screen-label="styleref">
      <div className="sr-cols">
        <aside className="sr-books" aria-label="参考书库">{library}</aside>

        {book ? (
          <section className="sr-stage">
            <SrStageHeader
              book={book}
              hasRun={!!(deep && deep.run)}
              librarySwitch={librarySwitch}
              headerBusy={headerBusy}
              delBusy={delBusy}
              onRerun={() => runHeaderAction("rerun")}
              onReclassify={onReclassify}
              onDelete={() => onDeleteBook(book)}
            />
            <SrStepper stage={stage} states={stageStates} onSelect={selectStage} />
            <div className="sr-stage-body" ref={bodyRef}>
              {!stage && <div className="sr-stage-loading"><Spinner size={16} label="正在读取这本书的进度" /> <span>正在读取这本书的进度…</span></div>}
              {stage === "overview"   && <SrOverview book={book} onReclassify={onReclassify} reclassifyLock={headerLockReason(book, headerBusy, "reclassify")} />}
              {stage === "matrix"     && <SrMatrix book={book} go={selectStage} />}
              {stage === "profile"    && <SrProfile book={book} go={selectStage} />}
              {stage === "validation" && <SrValidation book={book} go={selectStage} />}
              {stage === "apply"      && <SrApply book={book} go={selectStage} />}
            </div>
          </section>
        ) : (
          <section className="sr-stage sr-stage-empty">
            <header className="sr-stage-head sr-stage-head-empty">{librarySwitch}</header>
            {booksState.phase === "loading" || books.length > 0 ? (
              <div className="sr-stage-loading"><Spinner size={16} label="正在读取参考书库" /> <span>正在读取参考书库…</span></div>
            ) : booksState.phase === "error" ? (
              <EmptyState icon="AlertTriangle" title="读不到参考书库" actions={<button type="button" className="btn btn-ghost" onClick={() => srSyncBooks()}><I.Refresh size={14} /> 重试</button>}>
                {/* 错误代码只进悬停提示；原因那句本身已经说了该检查什么 */}
                <span title={booksState.code ? `错误代码：${booksState.code}` : undefined}>{booksState.error || "后端没有响应，检查后端是否在运行后重试。"}</span>
              </EmptyState>
            ) : (
              <EmptyState
                icon="BookOpen"
                title="参考书库还是空的"
                actions={<button type="button" className="btn btn-accent" onClick={() => setImportOpen(true)}><I.FileInput size={14} /> 导入第一本参考书</button>}
              >
                导入一本你想学习文风的书（txt / md）。系统只学抽象的写法，不复刻原文、人物或桥段。
              </EmptyState>
            )}
          </section>
        )}
      </div>

      {/* ≤1280 时左栏收起：书库在页头「参考书库」里打开 */}
      <WsDialog
        open={switcherOpen}
        onClose={() => setSwitcherOpen(false)}
        label="参考书库"
        size="sm"
        className="sr-switcher"
        scrimClassName="sr-switcher-scrim"
      >
        <div className="sr-switcher-body">{library}</div>
      </WsDialog>

      <SrImportDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onChoose={(policy, rights, picked) => {
          setImportOpen(false);
          try { srImportBook(policy, rights, picked); }
          catch (e) { srNotify((e && e.message) || String(e)); }
        }}
      />
    </div>
  );
}

/* 抽取 / 重新分类为什么不能点（有原因就锁住并把原因放进提示）；action 决定后半句怎么说 */
function headerLockReason(book, headerBusy, action = "extract") {
  const then = action === "reclassify" ? "重新分类" : "抽取";
  if (headerBusy) return "上一个操作还在进行";
  if (srActivityFor(book.id, "extract")) return `正在后台抽取，完成后可${action === "reclassify" ? "重新分类" : "再抽取"}`;
  if (srActivityFor(book.id, "reclassify") || srActivityFor(book.id, "import")) return `正在分类段落，完成后才能${then}`;
  if (book.rawStatus && book.rawStatus !== "ready") return "段落分类未完成：到概览里继续分类，或删除后重新导入";
  return null;
}

/* ≤1280 的「参考书库」按钮（打开左侧抽屉）。左栏收起后「参考书活动」只在抽屉里，
   所以进行中与失败的条数直接写在按钮上（不只放在 title 里），失败了一眼能看见。 */
function SrLibrarySwitch({ open, onOpen, count }) {
  const entries = srActivityEntries();
  const runningCount = entries.filter((e) => e.status === "running").length;
  const failedCount = entries.filter((e) => e.status === "failed").length;
  const facts = [
    runningCount > 0 ? `${runningCount} 项进行中` : null,
    failedCount > 0 ? `${failedCount} 项失败` : null,
  ].filter(Boolean);
  return (
    <button
      type="button"
      className="btn btn-ghost btn-sm sr-books-switch"
      data-testid="sr-books-switch"
      aria-haspopup="dialog"
      aria-expanded={open}
      title={facts.length ? `参考书库：${count} 本；参考书活动 ${facts.join("、")}` : "参考书库与参考书活动"}
      onClick={onOpen}
    >
      <I.BookOpen size={14} /> 参考书库
      <span className="sr-books-switch-count tab-num">{count}</span>
      {runningCount > 0 && (
        <span className="sr-books-switch-flag" data-tone="warn"><Spinner size={11} /> <span className="tab-num">{runningCount}</span> 项进行中</span>
      )}
      {failedCount > 0 && (
        <span className="sr-books-switch-flag" data-tone="danger" data-testid="sr-books-switch-failed"><I.AlertTriangle size={11} /> <span className="tab-num">{failedCount}</span> 项失败</span>
      )}
    </button>
  );
}

/* 页头：书名、作者与字数、流水线徽标；「开始 / 重跑抽取」与「更多」（重新分类、删除） */
function SrStageHeader({ book, hasRun, librarySwitch, headerBusy, delBusy, onRerun, onReclassify, onDelete }) {
  const pipeline = srPipelineFor(book);
  const extracting = srActivityFor(book.id, "extract");
  const reclassifying = srActivityFor(book.id, "reclassify") || srActivityFor(book.id, "import");
  const notReady = !!(book.rawStatus && book.rawStatus !== "ready");
  const lockReason = headerLockReason(book, headerBusy);
  const reclassifyLock = headerLockReason(book, headerBusy, "reclassify");
  return (
    <header className="sr-stage-head">
      <div className="sr-stage-id">
        {librarySwitch}
        <div className={`sr-stage-mark spine-${book.color}`} aria-hidden="true">{(book.title || "·").slice(0, 1)}</div>
        <div className="sr-stage-titles">
          <h1 className="sr-stage-title text-serif">{book.title}</h1>
          <div className="sr-stage-meta">
            <span>{book.author ? `${book.author} · ` : ""}{book.chars.toLocaleString()} 字</span>
            {pipeline && <Tag tone={pipeline.tone} dot>{pipeline.label}</Tag>}
          </div>
        </div>
      </div>
      <div className="sr-stage-actions">
        <button className="btn btn-ghost btn-sm" data-testid="sr-header-rerun" disabled={!!lockReason} title={lockReason || undefined} onClick={onRerun}>
          {extracting
            ? <><Spinner size={12} /> 抽取中 {srActivityView(extracting).percentText}</>
            : reclassifying ? <><Spinner size={12} /> 分类中 {srActivityView(reclassifying).percentText}</>
            : notReady ? "分类未完成"
            : headerBusy === "rerun" ? <><Spinner size={12} /> 启动抽取…</>
            : <><I.Refresh size={13} /> {hasRun ? "重跑抽取" : "开始抽取"}</>}
        </button>
        <SrMenu
          label="这本书的更多操作"
          items={[
            { id: "reclassify", label: headerBusy === "reclassify" ? "重新分类中…" : "重新分类…", icon: "Refresh", danger: true, testId: "sr-header-reclassify", disabled: !!reclassifyLock, title: reclassifyLock || undefined, onSelect: onReclassify },
            { id: "delete", label: "删除这本书…", icon: "Trash", danger: true, disabled: !!delBusy, onSelect: onDelete },
          ]}
        />
      </div>
    </header>
  );
}

/* 步骤条：每步的状态（已完成 / 进行中 / 需处理 / 未开始 / 等前一步）来自真实数据 */
function SrStepper({ stage, states, onSelect }) {
  return (
    <nav className="sr-stepper" aria-label="风格参考流水线">
      {SR_STAGES.map((s, i) => {
        const Ic = I[s.icon] || I.Dot;
        const active = stage === s.id;
        const state = (states && states[s.id]) || "unknown";
        const stateLabel = SR_STAGE_STATE_LABEL[state] || "";
        const prevDone = i > 0 && states && states[SR_STAGES[i - 1].id] === "done";
        return (
          <React.Fragment key={s.id}>
            {i > 0 && <span className={`sr-step-line ${prevDone ? "is-done" : ""}`} aria-hidden="true" />}
            <button
              type="button"
              className={`sr-step is-${state} ${active ? "is-active" : ""}`}
              onClick={() => onSelect(s.id)}
              aria-current={active ? "step" : undefined}
              aria-label={stateLabel ? `${s.name}（${stateLabel}）` : s.name}
            >
              <span className="sr-step-mark" aria-hidden="true">
                {state === "done" ? <I.Check size={14} />
                  : state === "running" ? <Spinner size={13} />
                  : state === "attention" ? <I.AlertTriangle size={14} />
                  : <Ic size={15} />}
              </span>
              <span className="sr-step-text" aria-hidden="true">
                <span className="sr-step-name">{s.name}</span>
                {stateLabel && <span className="sr-step-state">{stateLabel}</span>}
              </span>
            </button>
          </React.Fragment>
        );
      })}
    </nav>
  );
}

/* HMR 守卫：旧版本模块挂过启动定时器与 hashchange 监听，热替换时先摘掉；
   活动轮询在 store 里，热替换后旧模块的轮询也在这里停掉，免得两份轮询并行。 */
if (window.__srStyleGlobalHandlers) {
  const prev = window.__srStyleGlobalHandlers;
  clearTimeout(prev.hydrateTimer);
  if (prev.hashchange) window.removeEventListener("hashchange", prev.hashchange);
  if (typeof prev.stopActivity === "function") prev.stopActivity();
}
window.__srStyleGlobalHandlers = { hydrateTimer: null, hashchange: null, stopActivity: srActivityStop };

Object.assign(window, {
  WsStyleRef, srSyncBooks, srImportBook, srRunImport, srBookAction, srDeleteBook,
  srLoadDeep, srDeepFor, srInjectionPreview, srUnbind,
  srSynthesize, srReviewFinding, srFindingFeedback, srPreviewSamples,
  srCancelRun, srActivityStart, srActivityFor, srCancelClassification, srResumeClassification,
});

/* ESM 导出：拆分前 ws-styleref.jsx 的全部名字原样保留（单测与 ws-app 的懒加载都从这里取）；
   srImport* / SrImportProgressPanel 是「导入进度」扩成「参考书活动」之前的旧名。 */
export {
  WsStyleRef, SrImportDialog, SR_CLOUD_POLICIES, SR_RIGHTS_TERMS, srImportBook, srRightsReady,
  SrMatrix, SrProfile, SrApply, SrValidation, SR_TASKS, SR_LAYERS,
  buildDimOptions, computeIntensityReadout, computeResynthState, findShadowedBinding, srPickLatestRun,
  srLoadDeep, srDeepFor, srDropDeep, srPollRun, srBookAction, srDeleteBook, srSynthesize,
  srRunImport, srActivityView as srImportProgressView, srActivityEntries as srImportEntries,
  srActivityDismiss as srImportDismiss, SrActivityPanel as SrImportProgressPanel,
  SrActivityPanel, srActivityView, srActivityEntries, srActivitySet, srActivityDismiss, srActivityFor,
  srActivityApply, srActivityStart, srActivityStop, srCancelRun, srPreviewSamples, SrOverview,
  srCancelClassification, srResumeClassification, srMapStatus, srRunLabel, srMatrixEmptyHint, srRestClassifierLabel,
};
