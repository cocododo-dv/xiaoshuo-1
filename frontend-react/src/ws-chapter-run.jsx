import React from "react";
import { I } from "./icons.jsx";
import { WsCatalog } from "./ws-catalog.jsx";
import { navigateWithViewIntent } from "./ws-view-intents.js";
import { ACTIVE_STATUSES, EMPTY_RUN, LAST_RUN_LABEL, STATUS_COPY, TERMINAL_STATUSES, buttonLabel, normalizeRun } from "./ws-chapter-run-model.js";
import { useChapterRun } from "./ws-chapter-run-state.js";

/* 章节编排的「运行本章」：按钮 + 状态卡。状态与请求在 ws-chapter-run-state.js（useChapterRun），
   纯数据（normalizeRun、文案）在 ws-chapter-run-model.js；这里只画。normalizeRun 照旧从这里转出（单测用）。 */

/* 受阻 / 失败时后端在 author_action 里点名的那扇门，按钮用它给的字：
   这一场在等你（例如关键场景等你终选）→ 去 AI 起草台打开那一场；有一条待办要处理 → 去待办；
   模型没配好（没配模型那一种已经有「请配置模型」）→ 去系统设置。过去卡片只念一句话，没有门。 */
function openSceneOnDesk(backendSceneId) {
  let sid = null;
  try { sid = WsCatalog.sidForBackendId(backendSceneId); } catch (e) { sid = null; }
  if (sid) navigateWithViewIntent("scene", "ws:scene-enqueue", { sid });
  else window.location.hash = "#scene";
}

function runDoor(run, onConfigureModel) {
  const action = run.action;
  if (!action || (run.status !== "blocked" && run.status !== "failed")) return null;
  if (action.view === "scene" && action.sceneId) {
    return { label: action.label || "去 AI 起草台", icon: I.Play, open: () => openSceneOnDesk(action.sceneId) };
  }
  if (action.view === "review") {
    return { label: action.label || "去待办", icon: I.Inbox, open: () => { window.location.hash = "#review"; } };
  }
  if (action.view === "config" && onConfigureModel && run.errorCode !== "LLM_DISABLED_FOR_CHAPTER_RUN") {
    return { label: action.label || "去系统设置", icon: I.Settings, open: onConfigureModel };
  }
  return null;
}

/**
 * 章节级真实运行入口（章节编排的「运行本章」）。
 *
 * 只提交空对象。状态卡只在作者亲眼看着的变化上弹出（提交 / 排队 / 运行 → 终态）；
 * 打开一章时水合到的终态只给一枚「上次运行：…」小标签——过去「本章已完成」的浮层每次打开
 * 那一章都压在编辑区上、还关不掉，并且每次都重拉一遍整份目录。
 */
function ArrChapterRunAction({
  chapter,
  onCatalogRefresh,
  onOpenReview,
  onConfigureModel,
  pollIntervalMs = 1200,
}) {
  const { run, hydration, cardOpen, setCardOpen, start, retryHydration } = useChapterRun({ chapter, onCatalogRefresh, pollIntervalMs });
  const chapterKey = (chapter && (chapter.backendId || chapter.id)) || "";
  const hydrationMatches = hydration.chapterKey === chapterKey;
  const hydrationStatus = hydrationMatches ? hydration.status : "loading";
  const shownRun = hydrationMatches ? run : EMPTY_RUN;
  const approved = !!(chapter && chapter.state === "approved");
  const nonCurrent = !chapter || chapter.current !== true;
  const active = ACTIVE_STATUSES.has(shownRun.status);
  const terminal = TERMINAL_STATUSES.has(shownRun.status);
  const copy = STATUS_COPY[shownRun.status];
  const showProgress = ["pending", "running", "blocked", "failed", "completed"].includes(shownRun.status)
    && (shownRun.sceneCount > 0 || shownRun.progressPct > 0);
  const startDisabled = hydrationStatus !== "ready" || nonCurrent || approved || active || shownRun.status === "completed";
  const disabledReason = hydrationStatus === "loading"
    ? "正在从服务端同步本章运行状态。"
    : hydrationStatus === "error"
      ? "请先重试同步服务端运行状态。"
      : approved
        ? "已批准终稿不能重新运行，请先在成稿中心重新打开。"
        : nonCurrent
          ? "只能运行作品当前章。"
          : shownRun.status === "completed" ? "本章已运行完成。" : "";
  /* 按钮为什么是灰的：直接写在按钮旁边（过去只在悬停提示里）。同步中、同步失败有自己的说法 */
  const quietHydrationError = hydrationStatus === "error"
    && (hydration.errorCode === "CHAPTER_NOT_SYNCED" || hydration.errorCode === "PROJECT_NOT_READY");
  /* 旁边只放一句短话（窄屏章头也放得下）；整句原因留在悬停提示里 */
  const inlineReason = quietHydrationError
    ? (hydration.errorCode === "PROJECT_NOT_READY" ? "作品还在加载" : "尚未同步到后端")
    : (hydrationStatus === "ready" && approved ? "终稿已批准，不能重跑"
      : (hydrationStatus === "ready" && nonCurrent ? "只能运行当前章" : ""));
  const reasonTitle = quietHydrationError ? (hydration.message || disabledReason) : disabledReason;
  const StateIcon = shownRun.status === "completed"
    ? I.CheckCircle
    : (shownRun.status === "failed" || shownRun.status === "blocked")
      ? I.AlertTriangle
      : shownRun.status === "submitting" || shownRun.status === "running"
        ? I.Refresh
        : I.Clock;
  const showCard = hydrationStatus === "ready" && cardOpen && shownRun.status !== "idle" && !!copy;
  const door = runDoor(shownRun, onConfigureModel);
  const showChip = hydrationStatus === "ready" && !cardOpen && terminal;

  return (
    <div className="arr-run-control" data-run-status={shownRun.status} data-hydration-status={hydrationStatus}>
      <button
        className="btn btn-ghost btn-sm"
        type="button"
        data-testid="chapter-run-start"
        disabled={startDisabled}
        title={reasonTitle || undefined}
        aria-busy={active || hydrationStatus === "loading" ? "true" : undefined}
        onClick={start}
      >
        {active || hydrationStatus === "loading" ? <I.Refresh className="arr-run-spin" size={13} /> : <I.Play size={13} />}
        {hydrationStatus === "loading" ? "同步状态中…" : hydrationStatus === "error" ? (quietHydrationError ? "运行本章" : "状态待重试") : buttonLabel(shownRun)}
      </button>
      {inlineReason ? <span className="arr-run-reason" title={reasonTitle || undefined}>{inlineReason}</span> : null}
      {showChip ? (
        <button type="button" className="arr-run-chip" data-tone={shownRun.status} aria-expanded="false"
          title="查看上次运行的详情" onClick={() => setCardOpen(true)}>
          上次运行：{LAST_RUN_LABEL[shownRun.status]}
        </button>
      ) : null}

      {hydrationStatus === "error" && !quietHydrationError ? (
        <section className="arr-run-card" data-tone="failed" role="alert" aria-live="polite" aria-label="章节运行状态同步失败">
          <div className="arr-run-card-head">
            <span className="arr-run-state-icon"><I.AlertTriangle size={14} /></span>
            <strong>无法核验运行状态</strong>
          </div>
          <p>{hydration.message || "暂时无法查询服务端运行状态。"}</p>
          <div className="arr-run-actions">
            <button className="btn btn-ghost btn-sm" type="button" data-testid="chapter-run-hydration-retry" onClick={retryHydration}>
              <I.Refresh size={13} /> 重试同步
            </button>
          </div>
        </section>
      ) : showCard ? (
        <section
          className="arr-run-card"
          data-tone={shownRun.status}
          role={shownRun.status === "failed" || shownRun.status === "blocked" ? "alert" : "status"}
          aria-live="polite"
          aria-label="章节运行状态"
        >
          <div className="arr-run-card-head">
            <span className="arr-run-state-icon"><StateIcon className={active ? "arr-run-spin" : undefined} size={14} /></span>
            <strong>{copy.label}</strong>
            {terminal ? (
              <button className="arr-run-close" type="button" aria-label="收起运行状态" onClick={() => setCardOpen(false)}>
                <I.X size={13} />
              </button>
            ) : null}
          </div>

          <p>{shownRun.message || copy.hint}</p>

          {showProgress ? (
            <div className="arr-run-progress" aria-label={`章节运行进度 ${shownRun.progressPct}%`}>
              <div className="arr-run-progress-meta">
                <span>{shownRun.completedCount} / {shownRun.sceneCount || "?"} 个场景</span>
                <strong>{shownRun.progressPct}%</strong>
              </div>
              <span className="arr-run-progress-track"><i style={{ width: `${shownRun.progressPct}%` }} /></span>
            </div>
          ) : null}

          {shownRun.refreshWarning ? <p className="arr-run-warning">{shownRun.refreshWarning}</p> : null}

          <div className="arr-run-actions">
            {shownRun.errorCode === "LLM_DISABLED_FOR_CHAPTER_RUN" ? (
              <button className="btn btn-accent btn-sm" type="button" onClick={onConfigureModel}>
                <I.Settings size={13} /> 请配置模型
              </button>
            ) : null}
            {door ? (
              <button className="btn btn-accent btn-sm" type="button" data-testid="chapter-run-action" onClick={door.open}>
                <door.icon size={13} /> {door.label}
              </button>
            ) : null}
            {shownRun.status === "completed" ? (
              <button className="btn btn-accent btn-sm" type="button" data-testid="chapter-run-review" onClick={onOpenReview}>
                <I.CheckCircle size={13} /> 去成稿中心审阅
              </button>
            ) : null}
          </div>
        </section>
      ) : null}
    </div>
  );
}

export { ArrChapterRunAction, normalizeRun };
