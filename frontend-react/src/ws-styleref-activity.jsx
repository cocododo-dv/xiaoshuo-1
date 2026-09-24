import React from "react";
import { I } from "./icons.jsx";
import { Spinner } from "./ws-ui.jsx";
import { srActivityActive, srActivityKindLabel, srActivityView, srInputTooSmall } from "./ws-styleref-model.js";
import {
  srActivityClearFinished, srActivityDismiss, srActivityEntries, srActivityFor, srActivityPoke, srActivityTrack,
  srCancelClassification, srCancelLearn, srResumeClassification, srStartLearn, srSubscribe,
} from "./ws-styleref-store.js";
import { fidCancelCheckJob, fidCheckByJob, fidStartCheck, useFidelityStore } from "./ws-fidelity-store.js";
import { SrProgressBar, srNotifyError, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 参考书活动（左栏面板，窄屏在书库对话框里）
   只显示作业表条目：段落分类（导入 / 用模型重新分类）、学习文风、对照检查。每条一根进度条；在跑的可取消（对照检查
   也可以，按载荷的 cancellable），失败 / 取消的分类与学习可以「继续」（从断点续上，按载荷的 resumable——不可续跑的失败
   不给「继续」）；正文太短而失败的学习给「仍然学习」；做完 / 没做成的对照检查「打开」到这本书的「对照检查」并按作业 id
   认领（在起草台发起的也能看到结果），本机记着目标的失败检查给「重新检查」；终态给「关闭」。
   · srRunningFor：某本书正在跑的分类 / 学习 / 对照检查（书库徽标、步骤条用）
   活动表本身与 /activity 轮询在 ws-styleref-store.js；对照检查的取消 / 发起走 ws-fidelity-store.js。
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
  useFidelityStore();
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
  const cancel = (e) => run(e, async () => {
    if (e.kind === "check") {
      await fidCancelCheckJob(e.job_id);
      srActivityPoke();
      return;
    }
    await (e.kind === "learn" ? srCancelLearn(e.book_id) : srCancelClassification(e.book_id));
  });
  /* 正文太短而失败的学习：仍然学（force）。新作业是另一个 id 时把旧的这条收起 */
  const forceLearn = (e) => run(e, async () => {
    const data = await srStartLearn(e.book_id, { force: true });
    const jobId = data && data.job_id;
    if (jobId && `job:${jobId}` !== e.key) srActivityDismiss(e.key);
  });
  /* 本机记着目标的失败检查：按同样的目标再发一次（记在发起它的那个键下：风格参考页或起草台） */
  const recheck = (e, local) => run(e, async () => {
    const next = await fidStartCheck(local.key, local.target);
    if (next && next.phase === "failed" && next.error) throw next.error;
    const jobId = next && next.jobId;
    if (!jobId) return;
    srActivityTrack(jobId, { kind: "check", book_id: e.book_id, title: e.title });
    if (`job:${jobId}` !== e.key) srActivityDismiss(e.key);
  });
  /* 续跑通常沿用同一个作业 id：store 已经把这条换成了「排队中」，不能再把它关掉（关掉了就看不到在跑，跑完的
     结果还会被当成「关掉过」丢掉）；只有续出来的是另一个作业时，才把旧的这条收起 */
  const resume = (e) => run(e, async () => {
    const data = e.kind === "learn" ? await srStartLearn(e.book_id, { resume: true }) : await srResumeClassification(e.book_id);
    const jobId = data && data.job_id;
    if (jobId && `job:${jobId}` !== e.key) srActivityDismiss(e.key);
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
    const active = srActivityActive(e);
    const isBookJob = e.kind === "learn" || e.kind === "classify";
    const canCancel = active && e.cancellable !== false && !e.cancel_requested
      && (e.kind === "check" ? !!e.job_id : (isBookJob && !!e.book_id));
    const canResume = !active && !!e.book_id && !!e.resumable && isBookJob;
    const canForceLearn = !active && e.kind === "learn" && e.status === "failed" && !!e.book_id && srInputTooSmall(e.error);
    const localCheck = e.kind === "check" && !active ? fidCheckByJob(e.job_id) : null;
    const canRecheck = e.kind === "check" && e.status === "failed" && !!(localCheck && localCheck.target);
    const canOpen = !active && !!e.book_id && !!onOpenBook
      && (e.status === "succeeded" || (e.kind === "check" && e.status === "failed" && !canRecheck));
    const status = active ? "running" : e.status;
    return (
      <li key={e.key} className={`sr-activity-item is-${status}`} data-activity-key={e.key} data-activity-status={e.status} data-activity-kind={e.kind}>
        <div className="sr-activity-row">
          <span className="sr-activity-title"><span className="sr-activity-kind">{kindLabel}</span><span className="text-serif">{title}</span></span>
          <span className="sr-activity-pct tab-num">{v.percentText}</span>
        </div>
        <SrProgressBar percent={v.percent} label={`${kindLabel}${title}`} />
        <div className="sr-activity-meta" title={e.error && e.error.code ? `错误代码：${e.error.code}` : undefined}>{v.detail}</div>
        {(!active || canCancel) && (
          <div className="sr-activity-actions">
            {canCancel && (
              <button type="button" className="btn btn-ghost btn-sm" data-testid="sr-activity-cancel" disabled={busyKey === e.key} onClick={() => cancel(e)}>取消</button>
            )}
            {canResume && (
              <button type="button" className="btn btn-accent btn-sm" data-testid="sr-activity-resume" disabled={busyKey === e.key} onClick={() => resume(e)}>
                {e.kind === "learn" ? "继续学习" : "继续分类"}
              </button>
            )}
            {canForceLearn && (
              <button type="button" className="btn btn-accent btn-sm" data-testid="sr-activity-force-learn" disabled={busyKey === e.key} onClick={() => forceLearn(e)}>仍然学习</button>
            )}
            {canRecheck && (
              <button type="button" className="btn btn-accent btn-sm" data-testid="sr-activity-recheck" disabled={busyKey === e.key} onClick={() => recheck(e, localCheck)}>重新检查</button>
            )}
            {canOpen && (
              <button
                type="button"
                className="btn btn-quiet btn-sm"
                data-testid="sr-activity-open"
                onClick={() => { onOpenBook(e.book_id, e.kind === "check" ? "check" : null, e.kind === "check" ? e.job_id : null); srActivityDismiss(e.key); }}
              >打开</button>
            )}
            {!active && (
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
