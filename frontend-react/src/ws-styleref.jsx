import React from "react";
import { I } from "./icons.jsx";
import { WsDialog } from "./ws-dialog.jsx";
import { EmptyState, Spinner, Tag } from "./ws-ui.jsx";
import {
  SR_STAGES, SR_STAGE_STATE_LABEL, srActivityActive, srAppliedToWork, srErrorInfo, srLandingStage, srPickLandingBook,
  srReadUiPrefs, srRememberUi, srStageStates,
} from "./ws-styleref-model.js";
import {
  srActivityEntries, srActivityStart, srBookById, srBooks, srBooksState, srDeleteBooks, srLoadProjectBinding,
  srLoadRuntime, srRememberSession, srSessionUi, srSetViewMounted, srSubscribe, srSyncBooks,
} from "./ws-styleref-store.js";
import { SrMenu, srActiveWork, srNotify, srNotifyError, useSrStore } from "./ws-styleref-ui.jsx";
import { srRunningFor } from "./ws-styleref-activity.jsx";
import { SrImportDialog, SrLibrary, srConfirmDeleteBooks, srPipelineFor } from "./ws-styleref-library.jsx";
import { SrOverview } from "./ws-styleref-overview.jsx";
import { SrLearn } from "./ws-styleref-learn.jsx";
import { SrApply } from "./ws-styleref-apply.jsx";
import { SrCheck } from "./ws-styleref-check.jsx";

/* ==========================================================
   风格参考 —— 参考书 → 学习文风 → 用于作品 → 对照检查
   这个文件只是页面外壳：左栏书库（≤1280 收进页头「参考书库」对话框）、页头（书名、进度徽标、更多）、
   三步（外加随时可用的「对照检查」）的步骤条与切换、导入对话框。各步在 ws-styleref-*.jsx，请求与缓存在 ws-styleref-store.js，说法与判断在
   ws-styleref-model.js。纯 ESM，不写 window。
   ========================================================== */

export function WsStyleRef({ go }) {
  useSrStore("books", "activity", "detail");
  const [bookId, setBookId] = React.useState(null);
  /* stage 为 null：按这本书的进度自动落点（书库数据到了再定） */
  const [stage, setStage] = React.useState(null);
  const [importOpen, setImportOpen] = React.useState(false);
  const [switcherOpen, setSwitcherOpen] = React.useState(false);
  const bodyRef = React.useRef(null);
  const [workTick, setWorkTick] = React.useState(0);
  const work = srActiveWork();
  const workId = work ? work.id : null;
  const books = srBooks();
  const booksState = srBooksState();
  const book = srBookById(bookId);

  React.useEffect(() => {
    /* 每次挂载（包括从「去设置模型」回来）都重读运行时：接好模型之后学习卡、参考书页、对照检查按新配置解锁 */
    srSetViewMounted(true);
    srLoadRuntime();
    srSyncBooks();
    srActivityStart();
    const offImported = srSubscribe("imported")((event) => {
      const id = event && event.detail && event.detail.bookId;
      if (!id) return;
      setBookId(id);
      setStage("book");
      const w = srActiveWork();
      srRememberSession(w ? w.id : null, { bookId: id, stage: "book" });
    });
    const onWorkChanged = () => {
      setWorkTick((n) => n + 1);
      const w = srActiveWork();
      if (w) srLoadProjectBinding(w.id, { force: true });
      srSyncBooks();
    };
    window.addEventListener("ws:work-changed", onWorkChanged);
    return () => {
      srSetViewMounted(false);
      offImported();
      window.removeEventListener("ws:work-changed", onWorkChanged);
    };
  }, []);

  /* 落点：本次打开应用期间刚看过的那本 → 当前作品在用的 → 这部作品上次打开的 → 上次打开的 → 第一本 */
  React.useEffect(() => {
    if (book || booksState.phase !== "ready") return;
    if (!books.length) { if (bookId) setBookId(null); return; }
    const pick = srPickLandingBook(books, { prefs: srReadUiPrefs(), workId, session: srSessionUi(workId) });
    if (!pick) return;
    setBookId(pick.bookId);
    setStage(pick.stage || null);
  });

  const running = book ? srRunningFor(book.id) : {};
  const states = book ? srStageStates(book, { running, workId }) : null;
  /* 自动落点只定一次：书库数据到了就把步骤定下来，之后作者点哪步就是哪步 */
  React.useEffect(() => {
    if (!book || stage) return;
    setStage(srLandingStage(states, { applied: !!srAppliedToWork(book, workId) }));
  }, [book && book.id, stage, workTick]); // eslint-disable-line react-hooks/exhaustive-deps
  React.useEffect(() => { if (bodyRef.current) bodyRef.current.scrollTop = 0; }, [stage, bookId]);

  /* targetStage：从活动面板「打开」一次对照检查时直接落在「对照检查」；其余照旧按这本书的进度自动落点 */
  const selectBook = (id, targetStage = null) => {
    setSwitcherOpen(false);
    setImportOpen(false);
    if (id === bookId) { if (targetStage) setStage(targetStage); return; }
    setBookId(id);
    setStage(targetStage);
    srRememberUi(workId, { bookId: id, stage: targetStage });
    srRememberSession(workId, { bookId: id, stage: targetStage });
  };
  const selectStage = (id) => {
    setStage(id);
    if (!book) return;
    srRememberUi(workId, { bookId: book.id, stage: id });
    srRememberSession(workId, { bookId: book.id, stage: id });
  };

  const openSettings = () => { if (go) go("settings", { type: "ws:settings-tab", detail: "ai" }); };
  /* 出错时的下一步（SrErrorLine 的按钮）：去设置模型 / 打开书库里的那本 / 去学习文风 / 去用于作品 */
  const onAction = (action) => {
    if (!action) return;
    if (action.type === "settings") openSettings();
    else if (action.type === "open_book" && action.bookId) selectBook(action.bookId);
    else if (action.type === "learn") {
      if (action.bookId && action.bookId !== bookId) selectBook(action.bookId);
      selectStage("learn");
    } else if (action.type === "apply") selectStage("apply");
  };

  const onDeleteBook = async (target) => {
    if (!target) return;
    if (!(await srConfirmDeleteBooks([target]))) return;
    const idx = srBooks().findIndex((x) => x.id === target.id);
    try {
      const result = await srDeleteBooks([target.id]);
      const failed = (result.results || []).find((item) => !item.deleted && !(item.error && item.error.code === "STYLE_REFERENCE_BOOK_NOT_FOUND"));
      if (failed) { srNotify(`没有删掉：${srErrorInfo(failed.error, "请稍后重试。").message}`); return; }
      afterDeleted([target.id], idx);
      srNotify(`已删除参考书《${target.title}》`, "neutral");
    } catch (e) {
      srNotifyError(e, "删除没有完成，请稍后重试。");
    }
  };
  /* 删掉的是当前这本：切到原位置的邻居 */
  const afterDeleted = (ids, idx = -1) => {
    if (!bookId || !ids.includes(bookId)) return;
    const left = srBooks();
    const at = idx >= 0 ? idx : 0;
    const next = left.length ? left[Math.min(at, left.length - 1)] : null;
    setBookId(next ? next.id : null);
    setStage(null);
  };

  const library = (
    <SrLibrary
      bookId={bookId}
      onSelect={selectBook}
      onImport={() => { setSwitcherOpen(false); setImportOpen(true); }}
      onDeleted={(ids) => afterDeleted(ids)}
    />
  );
  const librarySwitch = <SrLibrarySwitch open={switcherOpen} onOpen={() => setSwitcherOpen(true)} count={books.length} />;

  return (
    <div className="sr-page" data-screen-label="styleref">
      <div className="sr-cols">
        <aside className="sr-books" aria-label="参考书库">{library}</aside>

        {book ? (
          <section className="sr-stage">
            <SrStageHeader book={book} librarySwitch={librarySwitch} onDelete={() => onDeleteBook(book)} />
            <SrStepper stage={stage} states={states} onSelect={selectStage} />
            <div className="sr-stage-body" ref={bodyRef}>
              {!stage && <div className="sr-stage-loading"><Spinner size={16} label="正在读取这本书的进度" /> <span>正在读取这本书的进度…</span></div>}
              {stage === "book" && <SrOverview book={book} onAction={onAction} />}
              {stage === "learn" && <SrLearn book={book} go={selectStage} onAction={onAction} />}
              {stage === "apply" && <SrApply key={`${book.id}|${workId}`} book={book} go={selectStage} onAction={onAction} />}
              {stage === "check" && <SrCheck key={`${book.id}|${workId}`} book={book} go={selectStage} onAction={onAction} />}
            </div>
          </section>
        ) : (
          <section className="sr-stage sr-stage-empty">
            <header className="sr-stage-head sr-stage-head-empty">{librarySwitch}</header>
            {booksState.phase === "loading" || books.length > 0 ? (
              <div className="sr-stage-loading"><Spinner size={16} label="正在读取参考书库" /> <span>正在读取参考书库…</span></div>
            ) : booksState.phase === "error" ? (
              <EmptyState icon="AlertTriangle" title="读不到参考书库" actions={<button type="button" className="btn btn-ghost" onClick={() => srSyncBooks()}><I.Refresh size={14} /> 重试</button>}>
                <span title={booksState.code ? `错误代码：${booksState.code}` : undefined}>后端没有响应：检查后端是否在运行后重试。</span>
              </EmptyState>
            ) : (
              <EmptyState
                icon="BookOpen"
                title="参考书库还是空的"
                actions={<button type="button" className="btn btn-accent" data-testid="sr-import-first" onClick={() => setImportOpen(true)}><I.FileInput size={14} /> 导入第一本参考书</button>}
              >
                导入一本你想学它文风的书（txt / md）：参考书 → 学习文风 → 用于作品。系统只学写法，不复刻原文、人物或桥段。
              </EmptyState>
            )}
          </section>
        )}
      </div>

      <WsDialog open={switcherOpen} onClose={() => setSwitcherOpen(false)} label="参考书库" size="sm" className="sr-switcher" scrimClassName="sr-switcher-scrim">
        <div className="sr-switcher-body">{library}</div>
      </WsDialog>

      <SrImportDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        onImported={() => setImportOpen(false)}
        onOpenBook={selectBook}
        onOpenSettings={() => { setImportOpen(false); openSettings(); }}
      />
    </div>
  );
}

/* ≤1280 的「参考书库」按钮（打开左侧抽屉）；进行中与没完成的活动条数直接写在按钮上 */
function SrLibrarySwitch({ open, onOpen, count }) {
  const entries = srActivityEntries();
  const activeCount = entries.filter(srActivityActive).length;
  const failedCount = entries.filter((e) => e.status === "failed").length;
  const facts = [
    activeCount > 0 ? `${activeCount} 项进行中` : null,
    failedCount > 0 ? `${failedCount} 项没完成` : null,
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
      {activeCount > 0 && <span className="sr-books-switch-flag" data-tone="warn"><Spinner size={11} /> <span className="tab-num">{activeCount}</span> 项进行中</span>}
      {failedCount > 0 && <span className="sr-books-switch-flag" data-tone="danger"><I.AlertTriangle size={11} /> <span className="tab-num">{failedCount}</span> 项没完成</span>}
    </button>
  );
}

/* 页头：书名、作者与字数、进度徽标；「更多」里是删除 */
function SrStageHeader({ book, librarySwitch, onDelete }) {
  const pipeline = srPipelineFor(book);
  return (
    <header className="sr-stage-head">
      <div className="sr-stage-id">
        {librarySwitch}
        <div className={`sr-stage-mark spine-${book.color}`} aria-hidden="true">{(book.title || "·").slice(0, 1)}</div>
        <div className="sr-stage-titles">
          <h1 className="sr-stage-title text-serif">{book.title}</h1>
          <div className="sr-stage-meta">
            <span>{book.author ? `${book.author} · ` : ""}{book.chars.toLocaleString()} 字</span>
            {pipeline && <Tag tone={pipeline.tone} dot testId="sr-stage-pipeline">{pipeline.label}</Tag>}
          </div>
        </div>
      </div>
      <div className="sr-stage-actions">
        <SrMenu label="这本书的更多操作" items={[{ id: "delete", label: "删除这本书…", icon: "Trash", danger: true, testId: "sr-header-delete", onSelect: onDelete }]} />
      </div>
    </header>
  );
}

/* 步骤条：参考书 → 学习文风 → 用于作品 → 对照检查；每步的状态来自真实数据 */
function SrStepper({ stage, states, onSelect }) {
  return (
    <nav className="sr-stepper" aria-label="参考书 → 学习文风 → 用于作品 → 对照检查">
      {SR_STAGES.map((s, i) => {
        const Ic = I[s.icon] || I.Dot;
        const active = stage === s.id;
        const state = (states && states[s.id]) || "todo";
        const stateLabel = SR_STAGE_STATE_LABEL[state] || "";
        const prevDone = i > 0 && states && states[SR_STAGES[i - 1].id] === "done";
        return (
          <React.Fragment key={s.id}>
            {i > 0 && <span className={`sr-step-line ${prevDone ? "is-done" : ""}`} aria-hidden="true" />}
            <button
              type="button"
              className={`sr-step is-${state} ${active ? "is-active" : ""}`}
              data-stage={s.id}
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
