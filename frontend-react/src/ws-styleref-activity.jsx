import React from "react";
import { I } from "./icons.jsx";
import { Spinner } from "./ws-ui.jsx";
import { srActivityActive, srActivityKindLabel, srActivityView } from "./ws-styleref-model.js";
import {
  srActivityClearFinished, srActivityDismiss, srActivityEntries, srActivityFor, srCancelClassification,
  srCancelLearn, srResumeClassification, srStartLearn, srSubscribe,
} from "./ws-styleref-store.js";
import { SrProgressBar, srNotifyError, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 参考书活动（左栏面板，窄屏在书库对话框里）
   只显示作业表条目：段落分类（导入 / 用模型重新分类）、学习文风、对照检查。每条一根进度条；在跑的可取消，
   失败 / 取消的分类与学习可以「继续」（从断点续上）；终态给「打开 / 关闭」。
   · srRunningFor：某本书正在跑的分类 / 学习 / 对照检查（书库徽标、步骤条用）
   活动表本身与 /activity 轮询在 ws-styleref-store.js。
   ========================================================== */

export function srRunningFor(bookId) {
  const view = (e) => (e ? { percentText: srActivityView(e).percentText, entry: e } : null);
  return {
    classify: view(srActivityFor(bookId, "classify")),
    learn: view(srActivityFor(bookId, "learn")),
    check: view(srActivityFor(bookId, "check")),
  };
}

function srFinishedAnnouncement(e) {
  const what = e.title ? `《${e.title}》` : "";
  const kind = srActivityKindLabel(e);
  if (e.status === "succeeded") return `${what}${kind}完成`;
  return e.status === "cancelled" ? `${what}${kind}已取消` : `${what}${kind}没有完成`;
}

export function SrActivityPanel({ onOpenBook }) {
  useSrStore("activity");
  const [, bump] = React.useState(0);
  const [busyKey, setBusyKey] = React.useState(null);
  const [collapsed, setCollapsed] = React.useState(false);
  const [showOld, setShowOld] = React.useState(false);
  const [announce, setAnnounce] = React.useState("");
  const listId = React.useId();
  React.useEffect(() => srSubscribe("finished")((event) => {
    if (event && event.detail) setAnnounce(srFinishedAnnouncement(event.detail));
  }), []);
  const entries = srActivityEntries();
  const anyActive = entries.some(srActivityActive);
  React.useEffect(() => {
    if (!anyActive) return undefined;
    const timer = setInterval(() => bump((x) => x + 1), 1000);
    return () => clearInterval(timer);
  }, [anyActive]);
  if (!entries.length) return null;

  const run = async (entry, action) => {
    setBusyKey(entry.key);
    try { await action(); }
    catch (err) { srNotifyError(err); }
    finally { setBusyKey(null); }
  };
  const cancel = (e) => run(e, () => (e.kind === "learn" ? srCancelLearn(e.book_id) : srCancelClassification(e.book_id)));
  const resume = (e) => run(e, async () => {
    if (e.kind === "learn") await srStartLearn(e.book_id, { resume: true });
    else await srResumeClassification(e.book_id);
    srActivityDismiss(e.key);
  });

  /* 打开页面前就已结束的条目收进「更早结束的」；没完成的（可以继续）总放在外面 */
  const isOlder = (e) => e.seenTerminal && !srActivityActive(e) && e.status !== "failed";
  const current = entries.filter((e) => !isOlder(e));
  const older = entries.filter(isOlder);
  const finishedCount = entries.filter((e) => !srActivityActive(e)).length;
  const activeCount = entries.length - finishedCount;

  const renderItem = (e) => {
    const v = srActivityView(e);
    const kindLabel = srActivityKindLabel(e);
    const title = e.title ? `《${e.title}》` : "";
    const canCancel = srActivityActive(e) && !!e.book_id && e.cancellable !== false && !e.cancel_requested && (e.kind === "learn" || e.kind === "classify");
    const canResume = !srActivityActive(e) && !!e.book_id && !!e.resumable && (e.kind === "learn" || e.kind === "classify");
    const status = srActivityActive(e) ? "running" : e.status;
    return (
      <li key={e.key} className={`sr-activity-item is-${status}`} data-activity-key={e.key} data-activity-status={e.status} data-activity-kind={e.kind}>
        <div className="sr-activity-row">
          <span className="sr-activity-title"><span className="sr-activity-kind">{kindLabel}</span><span className="text-serif">{title}</span></span>
          <span className="sr-activity-pct tab-num">{v.percentText}</span>
        </div>
        <SrProgressBar percent={v.percent} label={`${kindLabel}${title}`} />
        <div className="sr-activity-meta" title={e.error && e.error.code ? `错误代码：${e.error.code}` : undefined}>{v.detail}</div>
        {(!srActivityActive(e) || canCancel) && (
          <div className="sr-activity-actions">
            {canCancel && (
              <button type="button" className="btn btn-ghost btn-sm" data-testid="sr-activity-cancel" disabled={busyKey === e.key} onClick={() => cancel(e)}>取消</button>
            )}
            {canResume && (
              <button type="button" className="btn btn-accent btn-sm" data-testid="sr-activity-resume" disabled={busyKey === e.key} onClick={() => resume(e)}>
                {e.kind === "learn" ? "继续学习" : "继续分类"}
              </button>
            )}
            {e.status === "succeeded" && e.book_id && onOpenBook && (
              <button type="button" className="btn btn-quiet btn-sm" onClick={() => { onOpenBook(e.book_id, e.kind === "check" ? "check" : null); srActivityDismiss(e.key); }}>打开</button>
            )}
            {!srActivityActive(e) && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => srActivityDismiss(e.key)}>关闭</button>
            )}
          </div>
        )}
      </li>
    );
  };

  return (
    <section className="sr-activity" data-testid="sr-activity" aria-label="参考书活动">
      <div className="ws-sr-only" aria-live="polite" role="status">{announce}</div>
      <header className="sr-activity-head">
        <button type="button" className="sr-activity-toggle" aria-expanded={!collapsed} aria-controls={collapsed ? undefined : listId} onClick={() => setCollapsed((c) => !c)}>
          {collapsed ? <I.ChevronRight size={13} /> : <I.ChevronDown size={13} />}
          <span>参考书活动</span>
          <span className="sr-activity-count tab-num">{entries.length}</span>
          {activeCount > 0 && <Spinner size={11} />}
        </button>
        {finishedCount > 0 && (
          <button type="button" className="btn btn-quiet btn-xs" onClick={srActivityClearFinished}>清除已结束</button>
        )}
      </header>
      {!collapsed && (
        <ul className="sr-activity-list" id={listId}>
          {current.map(renderItem)}
          {older.length > 0 && (
            <li className="sr-activity-older">
              <button type="button" className="sr-activity-older-toggle" aria-expanded={showOld} onClick={() => setShowOld((x) => !x)}>
                {showOld ? <I.ChevronDown size={12} /> : <I.ChevronRight size={12} />} 更早结束的 {older.length} 项
              </button>
              {showOld && <ul className="sr-activity-list">{older.map(renderItem)}</ul>}
            </li>
          )}
        </ul>
      )}
    </section>
  );
}
