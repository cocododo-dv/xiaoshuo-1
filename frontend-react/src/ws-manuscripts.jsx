import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog, useCatalogChapters } from "./ws-catalog.jsx";
import { WsWorks } from "./ws-works.jsx";
import { manuscriptChapterEligible } from "./ws-manuscripts-store.jsx";
import { EmptyState, Notice, Segmented, Spinner } from "./ws-ui.jsx";
import { SCENE_STATE_META, chapterHeading, chapterLabel, chapterOwnTitle } from "./ws-labels.js";
import {
  manuBuildBody, manuCanonicalBlockReason, manuCanonicalComplete, manuChapterRows, manuDefaultPick,
  manuFirstMissingScene, manuListGroups, manuProgressCells, manuScenesArchived,
} from "./ws-manuscripts-compile.js";
import { useManuCanonical, useManuWorkflow } from "./ws-manuscripts-workflow.js";
import { ManuHero } from "./ws-manuscripts-hero.jsx";
import { ManuFootNote, ManuRead, ManuState, ManuStructure } from "./ws-manuscripts-reader.jsx";
import { ManuCanon } from "./ws-manuscripts-canon.jsx";
import { ManuDiagnosis } from "./ws-manuscripts-diagnosis.jsx";
import { useManuFidelity } from "./ws-manuscripts-fidelity.jsx";
import { useDiagnosisSummary } from "./ws-diagnosis-summary.jsx";
import { ManuDiff } from "./ws-manuscripts-diff.jsx";
import { ManuApprovalDialog, ManuReopenDialog, ManuReturnDialog } from "./ws-manuscripts-dialogs.jsx";

/* ==========================================================
   成稿中心 — 一本书在这里一章章成形：页头是整书进度与统一导出，
   左栏按阶段分组的章，右侧是本章的阅读器（正文 / 结构 / 正史 / 对比）
   和页脚的下一步。
   正文的唯一来源是后端章节聚合（WsManuStore ← GET /chapter-manuscripts/{id}，
   服务端以 FinalScene 归档行为源）；写作器的 wr-doc:* 缓存不是成稿来源——
   清缓存不丢稿的前提是稿在后端。
   各部分分在同前缀的模块里：-compile（纯函数）、-workflow（快照与流转）、
   -hero、-reader、-canon、-diff、-dialogs。
   ========================================================== */

const { useEffect, useState } = React;

function WsManuscripts({ go }) {
  /* 章节列表派生自 WsCatalog（与主页 / 写作器 / 编排台同源）；批准 / 退回写穿目录后这里自动刷新 */
  const catChs = useCatalogChapters() || [];
  const chs = manuChapterRows(catChs, manuscriptChapterEligible);
  const work = WsWorks.active();
  const book = {
    title: work ? work.title : "—",
    kind: work ? work.genre : "",
    goalWords: work ? work.wordsTarget : 0,
    planChapters: Math.max(work ? (work.chaptersTotal || 0) : 0, catChs.length),
  };

  /* 作者点过的章优先；没点过（或点的章已不在列表里）就落在「等你批准 → 写作中 → 第一章」。
     冷启动深链时目录还没到，等它到了默认章才落定。 */
  const [pickedId, setPicked] = useState(null);
  const defaultPick = manuDefaultPick(chs);
  const picked = chs.find((c) => c.id === pickedId) || defaultPick;
  const activeId = picked ? picked.id : null;
  /* 默认章一旦落定就钉住：批准 / 送审改变了各章阶段时，页面不该自己跳到另一章去 */
  useEffect(() => { if (!pickedId && defaultPick) setPicked(defaultPick.id); }, [pickedId, defaultPick && defaultPick.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const catPicked = catChs.find((c) => c.id === activeId) || null;

  const [chosenView, setView] = useState("read");
  /* 每场 / 每章开着的诊断发现数（与写作台深改面板同一份，忽略过的不算）：左栏、场景拼接、诊断页签都读它 */
  const diag = useDiagnosisSummary();
  /* 每一场最新的终稿像不像这位参考作者（作品用着参考书的文风时才有）：结构页签的场景行、正文的场头、页头一句 */
  const fidelity = useManuFidelity();
  const { snapshot: canonical, bump } = useManuCanonical(catPicked);
  const flow = useManuWorkflow({ picked, chapter: catPicked, canonical, bump, book, chapters: catChs, go });

  /* 「对比」只对审阅中 / 已定稿、有场的章存在。不只是换章：停在对比页上退回小修、重新打开，
     本章也会失去对比——显示层当场落回「正文」（不留一个没有选中项的分段），状态随后归位。 */
  const canDiff = !!picked && (picked.stage === "approved" || picked.stage === "review") && !!catPicked && (catPicked.scenes || []).length > 0;
  const view = chosenView === "diff" && !canDiff ? "read" : chosenView;
  useEffect(() => {
    if (chosenView === "diff" && !canDiff) setView("read");
  }, [chosenView, canDiff]);

  if (!picked) return <ManuEmptyPage go={go} />;

  const approved = chs.filter((c) => c.stage === "approved");
  const stats = {
    approvedWords: approved.reduce((sum, c) => sum + c.words, 0),
    approvedCount: approved.length,
    reviewCount: chs.filter((c) => c.stage === "review").length,
  };
  const canonicalComplete = manuCanonicalComplete(canonical);
  const canonicalBlockReason = manuCanonicalBlockReason(canonical);
  /* 有服务端章节时，正文只能来自权威聚合；加载失败也不退回任何示例稿 */
  const body = manuBuildBody(catPicked, canonical);
  const synced = !!(catPicked && catPicked.backendId);
  const chapterDiag = catPicked && catPicked.backendId ? diag.chapterCounts(catPicked.backendId) : null;
  const tabOptions = [
    { value: "read", label: "正文" },
    { value: "structure", label: "结构" },
    { value: "canon", label: "正史", testId: "manuscript-canon-tab" },
    { value: "diagnosis", label: chapterDiag && chapterDiag.open ? `诊断 ${chapterDiag.open}` : "诊断", testId: "manuscript-diagnosis-tab" },
    ...(canDiff ? [{ value: "diff", label: "对比" }] : []),
  ];
  const message = flow.status.message;
  const busy = flow.status.busy;
  const head = chapterLabel(picked, { withTitle: false });

  return (
    <div className="ms-page" data-screen-label="manuscripts">
      <ManuHero
        book={book} cells={manuProgressCells(catChs, book.planChapters)} activeId={activeId} stats={stats}
        exportCtx={{ catChs, book, chs, pickedId: activeId }} fidelity={fidelity.summary}
      />

      <div className="ms-cols">
        <ManuChapterList groups={manuListGroups(chs)} activeId={activeId} onPick={setPicked} diag={diag} />

        <section className="ms-reader" aria-label={chapterLabel(picked, { maxTitle: Infinity })}>
          <header className="ms-reader-head">
            <div className="ms-reader-id">
              {/* 章号只在章名不是「第 N 章」「未命名」这种占位时单独写一行，免得上下两行一模一样 */}
              {chapterOwnTitle(picked) && <div className="ms-reader-eyebrow">{head}</div>}
              <h2 className="ms-reader-title text-serif">{chapterOwnTitle(picked) || head}</h2>
              <div className="ms-reader-sub">
                <ManuState stage={picked.stage} />
                {/* 场的状态词与左栏列表、主页、章节编排同一个（ws-labels）：以前页头说「已归档」、左栏说「写完」 */}
                <span>{body ? `${SCENE_STATE_META.done.label} ${body.scenes.filter((s) => s.live).length}/${body.scenes.length} 场` : `${picked.scenes} 场`}</span>
                <span>{picked.words ? `${picked.words.toLocaleString()} 字` : "还没有字"}</span>
              </div>
            </div>
            <div className="ms-reader-tools">
              {synced && (
                <button type="button" className="btn btn-quiet btn-sm" data-testid="chapter-aggregate" onClick={flow.aggregate} disabled={busy.aggregate}
                  title="用各场已完成的终稿重新拼出本章汇总">
                  {busy.aggregate ? <Spinner size={13} /> : <I.Layers size={13} />}
                  {busy.aggregate ? "汇总中…" : "刷新章节汇总"}
                </button>
              )}
              <Segmented label="阅读方式" value={view} onChange={setView} options={tabOptions} />
            </div>
          </header>
          {message && <Notice tone={message.tone} className="ms-status">{message.text}</Notice>}
          {synced && view !== "read" && canonical.status === "error" && (
            <Notice tone="danger" className="ms-status"
              actions={<button className="btn btn-ghost btn-sm" type="button" onClick={flow.retryCanonical}><I.Refresh size={13} /> 重试加载</button>}>
              {(canonical.error && canonical.error.message) || "服务端正文加载失败。"}
            </Notice>
          )}
          {synced && view !== "read" && (canonical.status === "idle" || canonical.status === "loading") && (
            <div className="ms-canonical-loading" role="status"><Spinner size={13} /> 正在从服务端核验本章正文…</div>
          )}

          {view === "canon" && (
            <ManuCanon projectId={WsWorks.activeId()} chapterId={catPicked && catPicked.backendId} canonical={canonical} onChanged={bump} />
          )}
          {view === "read" && <ManuRead picked={picked} body={body} loadState={canonical} onRetry={flow.retryCanonical} fidelity={fidelity.finals} />}
          {view === "structure" && <ManuStructure body={body} chapter={catPicked} canonical={canonical} go={go} diag={diag} fidelity={fidelity.finals} />}
          {view === "diagnosis" && <ManuDiagnosis chapter={catPicked} go={go} />}
          {view === "diff" && <ManuDiff picked={picked} chapter={catPicked} />}

          <footer className="ms-reader-foot">
            <ManuFootNote picked={picked} canonical={canonical} canonicalComplete={canonicalComplete}
              blockReason={canonicalBlockReason} scenesArchived={manuScenesArchived(canonical)} />
            <div className="ms-foot-actions">
              <ManuNextStep picked={picked} chapter={catPicked} canonical={canonical} canonicalComplete={canonicalComplete}
                blockReason={canonicalBlockReason} flow={flow} go={go} onShowCanon={() => setView("canon")} />
            </div>
          </footer>
        </section>
      </div>

      {/* 三个对话框常驻、只切 open（见 ws-manuscripts-dialogs.jsx 开头：开发模式下焦点才进得去） */}
      <ManuReturnDialog open={flow.dialog === "return"} picked={picked} chapter={catPicked} busy={busy.workflow} error={flow.workflowError} onClose={flow.closeDialog} onConfirm={flow.returnToDraft} />
      <ManuApprovalDialog open={flow.dialog === "approve"} picked={picked} busy={busy.workflow} error={flow.workflowError} onClose={flow.closeDialog} onConfirm={flow.approveFinal} />
      <ManuReopenDialog open={flow.dialog === "reopen"} picked={picked} busy={busy.workflow} error={flow.workflowError} onClose={flow.closeDialog} onConfirm={flow.reopenFinal} />
    </div>
  );
}

/* 目录还没到 / 拉取失败时，不能先下结论说「成稿中心还是空的」 */
function ManuEmptyPage({ go }) {
  const catalogError = WsCatalog.loadError();
  if (catalogError) {
    return (
      <div className="ms-page" data-screen-label="manuscripts · empty">
        <EmptyState className="ms-page-empty" icon="AlertTriangle" title="目录没有加载出来"
          actions={<button type="button" className="btn btn-accent" onClick={() => WsCatalog.reset()}><I.Refresh size={14} /> 重新加载</button>}>
          {catalogError.message || "章节目录暂时读不到，成稿中心需要它来列出章节。"}
        </EmptyState>
      </div>
    );
  }
  if (!WsCatalog.ready()) {
    return (
      <div className="ms-page" data-screen-label="manuscripts · empty">
        <div className="ms-page-empty ms-page-loading" role="status"><Spinner size={16} /> 正在读取章节目录…</div>
      </div>
    );
  }
  return (
    <div className="ms-page" data-screen-label="manuscripts · empty">
      <EmptyState className="ms-page-empty" icon="BookOpen" title="成稿中心还是空的"
        actions={<button type="button" className="btn btn-accent" onClick={() => go && go("writer")}><I.Pen size={15} /> 去写作</button>}>
        写出第一章后，章节会在这里一章章成形、送审、定稿。
      </EmptyState>
    </div>
  );
}

/* 左栏：按阶段分组的章（先放要你拍板的，定稿放最后），也是唯一的选章入口 */
function ManuChapterList({ groups, activeId, onPick, diag }) {
  return (
    <nav className="ms-list" aria-label="章节">
      {groups.map((g) => (
        <div key={g.key} className="ms-list-group">
          <div className="ms-list-grouphd">
            <span>{g.label}</span>
            <span className="ms-list-groupn">{g.items.length}</span>
          </div>
          <ul className="ms-list-items">
            {g.items.map((c) => {
              // 「第 N 章」只在有真章名时作小字放在名字前；占位名（第 N 章 / 未命名）只写一遍
              const head = chapterHeading(c);
              const counts = diag && c.backendId ? diag.chapterCounts(c.backendId) : null;
              return (
                <li key={c.id}>
                  <button type="button" className={`ms-list-row ${activeId === c.id ? "is-active" : ""}`} aria-current={activeId === c.id ? "true" : undefined}
                    data-testid="manuscript-chapter-item" data-chapter-id={c.backendId || ""} onClick={() => onPick(c.id)}>
                    <span className="ms-list-body">
                      <span className="ms-list-line">
                        {head.title && <span className="ms-list-num">{head.num}</span>}
                        <span className="ms-list-title text-serif">{head.title || head.num}</span>
                      </span>
                      <span className="ms-list-meta">
                        {c.words ? `${c.words.toLocaleString()} 字` : "还没有字"}
                        {c.stage === "approved" ? ` · ${c.scenes} 场` : ` · ${SCENE_STATE_META.done.label} ${c.sceneDone}/${c.scenes} 场`}
                        {counts && counts.open > 0 && <span className="ms-list-diag" title="写作台深改面板里还开着的诊断发现（忽略过的不算）"> · 诊断 {counts.open}</span>}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

/* 页脚右边：每个阶段只有一个主动作。
   审阅中 → 退回小修 / 批准为终稿；已定稿 → 导出本章 / 重新打开；
   其余 → 齐了就送入审阅，各场归档了但正史没核 → 去核对正史，缺场 → 去写作台写缺的那一场。 */
function ManuNextStep({ picked, chapter, canonical, canonicalComplete, blockReason, flow, go, onShowCanon }) {
  const busy = flow.status.busy;
  const blockedTitle = !canonicalComplete ? blockReason : undefined;
  if (picked.stage === "review") {
    return (
      <>
        <button type="button" className="btn btn-ghost" disabled={busy.workflow} onClick={() => flow.openDialog("return")}>退回小修</button>
        <button type="button" className="btn btn-accent" data-testid="approve-final-open" disabled={busy.workflow || !canonicalComplete} title={blockedTitle} onClick={() => flow.openDialog("approve")}><I.Check size={14} /> 批准为终稿</button>
      </>
    );
  }
  if (picked.stage === "approved") {
    return (
      <>
        <button type="button" className="btn btn-quiet btn-sm" data-testid="chapter-export" disabled={busy.export || !canonicalComplete} title={blockedTitle} onClick={flow.exportChapter}>
          {busy.export ? <Spinner size={13} /> : <I.Download size={13} />} {busy.export ? "导出中…" : "导出本章"}
        </button>
        <button type="button" className="btn btn-ghost" data-testid="reopen-final-open" disabled={busy.workflow || !chapter || !chapter.backendId} onClick={() => flow.openDialog("reopen")}><I.Refresh size={13} /> 重新打开</button>
      </>
    );
  }
  if (canonicalComplete) {
    return <button type="button" className="btn btn-accent" disabled={busy.workflow} onClick={flow.submitToReview}><I.Check size={14} /> 送入审阅</button>;
  }
  if (manuScenesArchived(canonical)) {
    return <button type="button" className="btn btn-accent" onClick={onShowCanon}><I.ShieldCheck size={14} /> 去核对正史</button>;
  }
  if (canonical.status !== "ready") return null;
  const firstMissing = manuFirstMissingScene(chapter, canonical);
  const goWrite = () => {
    if (!go) return;
    go("writer", firstMissing && firstMissing.sid ? [{ type: "ws:writer-scene", detail: firstMissing.sid }] : []);
  };
  return <button type="button" className="btn btn-accent" onClick={goWrite}><I.Pen size={14} /> {picked.words ? "去写作台续写" : "去写作台动笔"}</button>;
}

export { WsManuscripts };
