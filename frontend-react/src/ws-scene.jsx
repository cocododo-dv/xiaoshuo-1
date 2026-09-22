import React from "react";
import { I } from "./icons.jsx";
import { useCatalogChapters, WsCatalog } from "./ws-catalog.jsx";
import { planIntentsForScene, sceneDesignModel } from "./ws-scene-design.jsx";
import { useDesignSync } from "./ws-design-sync.jsx";
import { UndoToast, useUndoToast } from "./ws-undo-toast.jsx";
import { WsWorks } from "./ws-works.jsx";
import { EmptyState } from "./ws-ui.jsx";
import { isImeComposing } from "./ws-dialog.jsx";
import { useSceneQueue, useSceneRuns } from "./ws-scene-board-state.js";
import { SceneSpine } from "./ws-scene-spine.jsx";
import { ArchivedStage, CandidatePicker, Pipeline, Preflight, ReviewStage, RunningStage, SceneHead, SceneStyleNoticeStrip } from "./ws-scene-stage.jsx";
import { SceneRunJobControl } from "./ws-scene-job.jsx";
import { DecisionBar } from "./ws-scene-decide.jsx";
import { AttemptCompare, Evidence } from "./ws-scene-evidence.jsx";
import { SceneAdoptionDialogs, SceneAdoptionNote, useSceneAdoption } from "./ws-scene-adopt.jsx";

const { useState, useEffect, useMemo, useRef } = React;

/* ==========================================================
   AI 起草台 — 一场一裁
   ┌──────────┬─────────────────────────────────┬──────────┐
   │ 全书书脊  │  场景头 · 管线进度（只认后端）    │ 证据      │
   │ 章 → 场   │  设计卡 / 起草中 / 正文 / 终选    │ 裁决 · 窗口│
   │          │  裁决条（开始 / 采纳 / 退回）     │ 尝试 · 记录│
   └──────────┴─────────────────────────────────┴──────────┘
   中间按这一场的状态换内容：待起草 → 设计卡；运行中 → 起草说明；待复核 → 正文与裁决；已归档 → 定稿。
   证据栏没东西可看时整栏收起；窄于 1120px 时变成从右侧拉出的抽屉。
   这个文件只负责摆放：状态在 ws-scene-board-state.js（台面 / 运行）与 ws-scene-adopt.jsx（采用），
   各栏在 ws-scene-spine / -stage / -decide / -evidence，与后端说话的在 ws-scene-api.js。
   ========================================================== */

/* 「参考该版复盘意见重写」的作者指令。复盘意见可能本身就是一条近 2000 字的旧指令：
   整句超过上限时把意见截短（带省略号），而不是让 startRun 以「指令太长」拒收、点了没反应。 */
const AUTHOR_NOTE_LIMIT = 2000;
function attemptRewriteNote(attempt) {
  const attemptNo = (attempt && (attempt.n || attempt.attempt)) || "所选";
  const verdict = attempt && attempt.cmp && attempt.cmp.verdict ? String(attempt.cmp.verdict).trim() : "";
  const compose = (v) => `参考第 ${attemptNo} 次尝试的复盘意见重写${v ? `（${v}）` : ""}；不恢复该版正文，以当前稿为输入修正当前质检问题。`;
  const full = compose(verdict);
  if (Array.from(full).length <= AUTHOR_NOTE_LIMIT) return full;
  const room = AUTHOR_NOTE_LIMIT - Array.from(compose("")).length - 3;   // 3 = 括号两个 + 省略号
  return room > 0 ? compose(`${Array.from(verdict).slice(0, room).join("")}…`) : compose("");
}

function WsSceneBoard({ go, t }) {
  const tw = t || {};
  const { toast, show: showNotice, clear: clearNotice } = useUndoToast();
  const board = useSceneQueue({ showNotice });
  const { items, runs, pickedId } = board;
  const run = useSceneRuns(board);
  const adoption = useSceneAdoption({ ...board, onArchived: run.refreshJob, go });
  const chapters = useCatalogChapters();
  const designSync = useDesignSync();
  const [spineFilter, setSpineFilter] = useState("all");   // all = 全书 · active = 只看在办 · review = 只看待复核
  const [logOpen, setLogOpen] = useState(tw.scnLog !== false);
  const [evidenceOpen, setEvidenceOpen] = useState(false);  // 窄屏（≤1120px）证据抽屉
  const [compare, setCompare] = useState(null);           // 正在看复盘的那次尝试
  useEffect(() => { setCompare(null); }, [pickedId]);

  /* 窄屏证据抽屉：Esc 收起（对话框开着时 WsDialog 在捕获阶段先吃掉 Esc）。
     输入法组字时的 Esc、在文本框里按的 Esc（例如退回重写的指令框）不算——那是在跟输入框说话。 */
  useEffect(() => {
    if (!evidenceOpen) return undefined;
    const onKey = (event) => {
      if (event.key !== "Escape" || event.defaultPrevented || isImeComposing(event)) return;
      const target = event.target;
      if (target && target.closest && target.closest("textarea, input, select, [contenteditable='true']")) return;
      setEvidenceOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [evidenceOpen]);
  /* 抽屉的焦点：拉开时落在抽屉的关闭按钮上；收起时焦点若还在抽屉里（或已掉到 body）就回到「证据」按钮。
     抽屉只在 ≤1120px 出现、没有遮罩，所以不锁焦点，只负责进出。宽屏时关闭按钮不显示，focus 什么也不做。 */
  const eviToggleRef = useRef(null);
  const eviWasOpenRef = useRef(false);
  useEffect(() => {
    const drawer = typeof document !== "undefined" ? document.getElementById("scn2-evi") : null;
    if (evidenceOpen) {
      eviWasOpenRef.current = true;
      const close = drawer && drawer.querySelector(".scn2-evi-drawerhead button");
      if (close) close.focus({ preventScroll: true });
      return;
    }
    if (!eviWasOpenRef.current) return;
    eviWasOpenRef.current = false;
    const active = document.activeElement;
    if ((!active || active === document.body || (drawer && drawer.contains(active))) && eviToggleRef.current) {
      eviToggleRef.current.focus({ preventScroll: true });
    }
  }, [evidenceOpen]);

  /* 台面上的状态：本地运行记录叠上后端任务（选中的这一场以任务控制条报来的为准） */
  const jobStatus = run.jobStatus;
  const jobState = jobStatus === "queued"
    ? "queued"
    : (["running", "cancel_requested"].includes(jobStatus) ? "running" : null);
  const jobQueued = jobStatus === "queued";
  /* hasRun：这一场有运行记录（本机的、从后端恢复的、或控制条报来的任务）。没有记录的在办场，
     书脊先按目录状态标（已完成 / 在写），不在恢复完成前一律写「待起草」。 */
  const queue = useMemo(() => items.map((x) => {
    const r = runs[x.id];
    const item = r ? { ...x, state: r.state || "queued", hasRun: true } : x;
    return item.id === pickedId && jobState ? { ...item, state: jobState, jobQueued, hasRun: true } : item;
  }), [items, runs, pickedId, jobState, jobQueued]);
  const renderState = jobState || (queue.find(q => q.id === pickedId) || {}).state;
  const scene = useMemo(() => {
    const base = items.find(x => x.id === pickedId);
    if (!base) return null;
    const r = runs[base.id];
    return r ? { ...base, ...r, attempt: r.attempt || 1 } : base;
  }, [pickedId, items, runs]);
  /* 统计只数在办的场；只是点开看看的 transient 不算 */
  const counts = useMemo(() => {
    const c = { running: 0, queued: 0, ready: 0, archived: 0 };
    queue.forEach(q => { if (!q.transient) c[q.state || "queued"]++; });
    return c;
  }, [queue]);

  const spine = (
    <SceneSpine
      chapters={chapters} queue={queue} pickedId={pickedId} counts={counts}
      filter={spineFilter} setFilter={setSpineFilter}
      pendingSync={(backendId) => !!designSync.pendingFor(backendId)}
      onPickItem={board.setPicked} onViewScene={board.viewSid} onEnqueueChapter={board.enqueueChapter}
      onRemove={(id) => board.removeFromQueue([id])}
      select={board.select}
    />
  );
  const pageStyle = { "--scn-font": (tw.scnFont || 16) + "px" };
  const density = tw.scnDensity || "cozy";

  /* 台面上没有场：目录里还没有场景卡（只有空章），或在办的场刚被全部移出 */
  if (!scene) {
    const hasScenes = chapters.some(c => (c.scenes || []).length > 0);
    return (
      <div className="scn2 no-evi" data-screen-label="scene" data-density={density} style={pageStyle}>
        {hasScenes && spine}
        <div className="scn2-empty" style={{ gridColumn: hasScenes ? "2 / -1" : "1 / -1" }}>
          <EmptyState
            icon="Play"
            title={hasScenes ? "从左边的书脊上点一场" : "目录里还没有场景"}
            actions={!hasScenes ? <button className="btn btn-accent" onClick={() => go && go("snowflake")}><I.Layout size={14} /> 去构思</button> : null}
          >
            {hasScenes
              ? "AI 按这一场的设计卡（坩埚、三拍、视角）和雪花构思起草，写完由你裁决。"
              : "先在构思里「整理章节结构」，或去章节编排给章加场、填好场景卡，再回来起草。"}
          </EmptyState>
        </div>
        <UndoToast toast={toast} onClose={clearNotice} />
      </div>
    );
  }

  const selectedItem = items.find(x => x.id === pickedId) || null;
  const designModel = sceneDesignModel(scene.sid ? WsCatalog.sceneById(scene.sid) : null);
  const designSyncProps = designModel && designModel.backendId && designSync.pendingFor(designModel.backendId)
    ? { pending: true, busy: designSync.isBusy(designModel.backendId), onSync: () => designSync.syncScenes([designModel.backendId]) }
    : null;
  /* 阶段 Y：雪花整理出来的场，设计只在构思里改（裁决条上唯一一个「在构思里改」） */
  const onEditPlan = designModel && designModel.planOwned && go ? () => go("snowflake", planIntentsForScene(designModel.backendId)) : null;
  const awaitingChoice = renderState === "ready" && !!(scene.gate && scene.gate.authorState === "awaiting_author_choice");
  /* 这一场从没进过管线（后端清单里没有、也没有本地记录）就不去问 latest：那一问只会得到 404 */
  const backendRunSids = board.backendRunSids;
  const latestWorthAsking = !!selectedItem && (
    !selectedItem.transient
    || !!runs[selectedItem.id]
    || backendRunSids === "unknown"
    || (backendRunSids instanceof Set && backendRunSids.has(selectedItem.sid))
  );
  const hasEvidence = renderState === "ready" || renderState === "archived"
    || !!(scene.styleWindows && Array.isArray(scene.styleWindows.windows) && scene.styleWindows.windows.length)
    || !!(scene.attempts && scene.attempts.length)
    || !!(scene.log && scene.log.length)
    || !!(scene.cost && scene.cost.length);

  return (
    <div className={`scn2${hasEvidence ? "" : " no-evi"}${evidenceOpen && hasEvidence ? " evi-open" : ""}`} data-screen-label="scene"
      data-density={density} style={pageStyle}>
      {spine}

      <section className="scn2-stage" key={pickedId}>
        <SceneHead
          scene={scene} state={renderState} jobQueued={jobQueued}
          evidence={hasEvidence ? { open: evidenceOpen, onToggle: () => setEvidenceOpen(o => !o), toggleRef: eviToggleRef } : null}
        />
        <div className="scn2-runbar">
          <Pipeline scene={scene} state={renderState} job={run.currentJob} />
          {run.activeBackendSceneId && (
            <SceneRunJobControl
              sceneId={run.activeBackendSceneId}
              observedJob={run.observedJob}
              onJobChange={run.onJobChange}
              draftMode={scene.draftMode || null}
              refreshSignal={run.refreshTick}
              fetchLatest={latestWorthAsking}
            />
          )}
        </div>
        <SceneStyleNoticeStrip notices={scene.styleNotices} />
        <div className="scn2-stage-body">
          {renderState === "queued" && <Preflight model={designModel} sync={designSyncProps} />}
          {renderState === "running" && <RunningStage model={designModel} job={run.currentJob} draftMode={scene.draftMode} />}
          {awaitingChoice && <CandidatePicker sid={scene.sid} onDone={run.onCandidateChosen} onRework={() => run.startRun("")} />}
          {renderState === "ready" && !awaitingChoice && <ReviewStage scene={scene} />}
          {renderState === "archived" && <ArchivedStage scene={scene} />}
        </div>
        <SceneAdoptionNote note={adoption.note} sid={scene.sid} onClose={adoption.clearNote} />
        <DecisionBar
          scene={scene} state={renderState} runJobStatus={jobStatus} go={go}
          onEditPlan={onEditPlan}
          onArchive={adoption.onArchive} onRun={run.startRun} onBudgetTopup={run.topupBudget}
          onCandidateToWriter={adoption.candidateToWriter}
          archiveBusy={adoption.archiveBusy}
        />
        {compare && (
          <AttemptCompare attempt={compare} onClose={() => setCompare(null)}
            onRewrite={() => {
              setCompare(null);
              run.startRun(attemptRewriteNote(compare));
            }} />
        )}
      </section>

      {hasEvidence && (
        <Evidence scene={scene} state={renderState} open={evidenceOpen} onClose={() => setEvidenceOpen(false)}
          logOpen={logOpen} setLogOpen={setLogOpen} onView={setCompare} />
      )}
      <UndoToast toast={toast} onClose={clearNotice} />
      <SceneAdoptionDialogs adoption={adoption} />
    </div>
  );
}

/* AI 起草台：左栏是全书书脊（与目录同源）——读雪花构思 + 场景设计卡起草，质检后写回正文。 */
function WsScene(props) {
  /* 订阅目录：雪花刚「整理章节结构」、目录晚到，这里都要跟着换到书脊，而不是停在空状态上 */
  const catalogChapters = useCatalogChapters();
  const work = WsWorks.active() || { title: "这部作品" };
  if (catalogChapters.length > 0) return <WsSceneBoard key={(work && work.id) || "work"} {...props} />;
  return (
    <div className="page scn2-page-empty" data-screen-label="scene · empty">
      <EmptyState
        icon="Play"
        title={`《${work.title}》还没有章节目录`}
        actions={(
          <>
            <button className="btn btn-accent" onClick={() => props.go && props.go("snowflake")}><I.Layout size={15} /> 去构思</button>
            <button className="btn btn-ghost" onClick={() => props.go && props.go("author")}>去章节编排</button>
          </>
        )}
      >
        AI 起草台按场景卡逐场起草，写完由你裁决再写回正文。先在构思里「整理章节结构」，或去章节编排建章、填好场景卡，再回来。
      </EmptyState>
    </div>
  );
}

/* 场景工作台只通过显式 ESM 导出。 */
export { WsScene };
