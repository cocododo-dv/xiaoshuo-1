import React from "react";
import { I } from "./icons.jsx";
import { Spinner } from "./ws-ui.jsx";
import { srFormatDuration } from "./ws-styleref-model.js";
import {
  srActivityClearFinished, srActivityDismiss, srActivityEntries, srActivityFor,
  srCancelClassification, srCancelRun, srResumeClassification, srSubscribe,
} from "./ws-styleref-store.js";
import { SrProgressBar, srNotify, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   风格参考 · 参考书活动（左栏面板，窄屏在书库对话框里）
   · srActivityView：一条活动记录 → 百分比与文案（纯函数，导入的文案契约见 import 单测）
   · srRunningFor：某本书正在跑的分类 / 抽取 / 合成（书库徽标、步骤条用）
   · SrActivityPanel：每条操作一根进度条；抽取 / 分类可取消，分类可继续，终态给「打开 / 关闭」
   活动表本身与 /activity 轮询在 ws-styleref-store.js。
   ========================================================== */

export const SR_ACTIVITY_KIND_LABEL = {
  import: "导入", reclassify: "重新分类", extract: "抽取", synthesize: "合成画像",
  rag_index: "应用画像 · 建索引", validate: "回测", preview: "示例预览",
};
const SR_ACTIVITY_DONE_LABEL = {
  import: "已导入", reclassify: "已重新分类", extract: "抽取完成", synthesize: "画像已合成",
  rag_index: "索引已就绪", validate: "回测完成", preview: "示例已生成",
};
const SR_VERDICT_LABEL = { pass: "通过", partial: "部分通过", fail: "未通过", plagiarism: "疑似抄袭" };

/* 分类进度的「第几批」说法：导入与重新分类共用。
   2026-09-15 严格 LLM：产品路径只有 mode="llm"；「启发式」只会来自离线夹具，如实标出。 */
function srClassifyLabel(server) {
  const classify = server.classify || {};
  if (server.phase !== "classify") return null;
  if (classify.batches_total > 0) return `段落分类 ${classify.batches_done ?? 0}/${classify.batches_total} 批`;
  if (classify.mode && classify.mode !== "llm") return "段落分类（启发式）";
  return null;
}

/* 纯函数：一条活动记录（本地状态 + 最近一次服务端快照）→ 面板显示的百分比与文案。
   导入条目的文案与 2026-09-15 导入进度条的契约一致；其余 kind 走通用的「阶段 · 步骤 · 已用 · 预计」。 */
export function srActivityView(entry, now = Date.now()) {
  const kind = (entry && entry.kind) || "import";
  const server = (entry && entry.server) || null;
  const elapsed = Math.max(0, (now - ((entry && entry.startedAt) || now)) / 1000);
  let percent = 0;
  if (entry.status === "succeeded") percent = 100;
  else if (server) percent = Math.max(0, Math.min(99, Math.round(Number(server.percent) || 0)));
  else if (entry.percent != null) percent = Math.max(0, Math.min(99, Math.round(Number(entry.percent) || 0)));
  const percentText = `${percent}%`;
  const used = `已用 ${srFormatDuration(elapsed)}`;

  if (kind === "import") {
    if (entry.status === "succeeded") {
      const chars = entry.chars ? `${Number(entry.chars).toLocaleString()} 字` : null;
      const paras = entry.paragraphs != null ? `${Number(entry.paragraphs).toLocaleString()} 段` : null;
      return { percent, percentText, detail: ["已导入", chars, paras, `用时 ${srFormatDuration(elapsed)}`].filter(Boolean).join(" · ") };
    }
    if (entry.status === "failed") {
      return { percent, percentText, detail: `导入失败：${entry.error || "未知错误"}` };
    }
    if (!server) {
      return { percent, percentText, detail: `上传中 · ${used}` };
    }
    const parts = [srClassifyLabel(server) || server.phase_label || server.phase || "处理中", used];
    if (server.phase === "classify" && server.eta_seconds != null) parts.push(`预计还需 ${srFormatDuration(server.eta_seconds)}`);
    return { percent, percentText, detail: parts.join(" · ") };
  }

  const kindLabel = SR_ACTIVITY_KIND_LABEL[kind] || kind;
  if (entry.status === "succeeded") {
    const parts = [SR_ACTIVITY_DONE_LABEL[kind] || "完成"];
    const result = (server && server.result) || entry.result || null;
    if (kind === "validate" && result && result.verdict) parts.push(`结论 ${SR_VERDICT_LABEL[result.verdict] || result.verdict}`);
    if (kind === "extract" && server && server.llm_calls) parts.push(`模型调用 ${server.llm_calls} 次`);
    parts.push(`用时 ${srFormatDuration(elapsed)}`);
    return { percent, percentText, detail: parts.join(" · ") };
  }
  if (entry.status === "failed") {
    return { percent, percentText, detail: `${kindLabel}失败：${entry.error || "未知错误"}` };
  }
  if (entry.status === "cancelled") {
    return { percent, percentText, detail: `已取消 · 用时 ${srFormatDuration(elapsed)}` };
  }
  if (!server) {
    return { percent, percentText, detail: `${entry.phase === "start" ? "启动中" : "处理中"} · ${used}` };
  }
  const label = server.phase_label || server.phase || "处理中";
  const steps = server.steps && Number(server.steps.total) > 0 ? server.steps : null;
  const parts = [];
  if (kind === "extract") {
    parts.push(steps ? `${label} · ${steps.label ? `${steps.label} · ` : ""}第 ${Math.min(Number(steps.done) + 1, Number(steps.total))}/${steps.total} 维` : label);
  } else if (kind === "reclassify") {
    parts.push(srClassifyLabel(server) || label);
  } else if (steps) {
    parts.push(`${label} ${steps.done}/${steps.total}${steps.label ? ` ${steps.label}` : ""}`);
  } else {
    parts.push(label);
  }
  parts.push(used);
  if (server.eta_seconds != null) parts.push(`预计还需 ${srFormatDuration(server.eta_seconds)}`);
  if (kind === "extract" && server.llm_calls) {
    parts.push(`模型调用 ${server.llm_calls} 次${server.retries ? `（含补抽 ${server.retries} 次）` : ""}`);
  }
  return { percent, percentText, detail: parts.join(" · ") };
}

/* 某本书正在跑的三类操作，交给纯函数 srBookPipeline / srStageStates。 */
export function srRunningFor(bookId) {
  const view = (e) => (e ? { percentText: srActivityView(e).percentText } : null);
  return {
    classify: view(srActivityFor(bookId, "import") || srActivityFor(bookId, "reclassify")),
    extract: view(srActivityFor(bookId, "extract")),
    synthesize: view(srActivityFor(bookId, "synthesize")),
  };
}

/* 终态播报文案（读屏只播报状态变化，不每秒念一遍「已用 m:ss」） */
function srFinishedAnnouncement(e) {
  const kindLabel = SR_ACTIVITY_KIND_LABEL[e.kind] || e.kind;
  const what = e.title ? `《${e.title}》` : "";
  if (e.status === "succeeded") return `${what}${SR_ACTIVITY_DONE_LABEL[e.kind] || "完成"}`;
  return e.status === "cancelled" ? `${what}${kindLabel}已取消` : `${what}${kindLabel}失败`;
}

/* 「参考书活动」：有标题和条数，可折叠；刷新页面后才看到的已结束条目折叠在「更早结束的」里；
   在跑时每秒重绘（已用时间）。没有条目时整个面板不渲染（测试据此判断）。 */
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
  const anyRunning = entries.some((e) => e.status === "running");
  React.useEffect(() => {
    if (!anyRunning) return undefined;
    const timer = setInterval(() => bump((x) => x + 1), 1000);
    return () => clearInterval(timer);
  }, [anyRunning]);
  if (!entries.length) return null;

  const cancel = async (e) => {
    setBusyKey(e.key);
    try {
      if (e.kind === "extract") await srCancelRun(e.targetId);
      else await srCancelClassification(e.bookId);
    } catch (err) { srNotify("取消失败：" + ((err && err.message) || err)); }
    finally { setBusyKey(null); }
  };
  const resume = async (e) => {
    setBusyKey(e.key);
    try { await srResumeClassification(e.bookId); srActivityDismiss(e.key); }
    catch (err) { srNotify("继续分类失败：" + ((err && err.message) || err)); }
    finally { setBusyKey(null); }
  };
  const current = entries.filter((e) => !e.seenTerminal || e.status === "running");
  const older = entries.filter((e) => e.seenTerminal && e.status !== "running");
  const finishedCount = entries.filter((e) => e.status !== "running").length;
  const runningCount = entries.length - finishedCount;

  const renderItem = (e) => {
    const v = srActivityView(e);
    const kindLabel = SR_ACTIVITY_KIND_LABEL[e.kind] || e.kind;
    const title = e.title ? `《${e.title}》` : "";
    const classifying = e.kind === "import" || e.kind === "reclassify";
    const canCancel = e.status === "running" && (
      (e.kind === "extract" && !!e.targetId)
      || (classifying && !!e.bookId && !!(e.server && e.server.cancellable))
    );
    const canResume = e.status !== "running" && classifying && !!e.bookId && !!(e.server && e.server.resumable);
    return (
      <li key={e.key} className={`sr-import-item is-${e.status}`} data-import-key={e.key} data-import-status={e.status} data-activity-kind={e.kind}>
        <div className="sr-import-row">
          <span className="sr-import-title"><span className="sr-activity-kind">{kindLabel}</span><span className="text-serif">{title}</span></span>
          <span className="sr-import-pct tab-num">{v.percentText}</span>
        </div>
        <SrProgressBar percent={v.percent} label={`${kindLabel}${title}`} />
        <div className="sr-import-meta" title={e.status === "failed" && e.errorCode ? `错误代码：${e.errorCode}` : undefined}>{v.detail}</div>
        {(e.status !== "running" || canCancel) && (
          <div className="sr-import-actions">
            {canCancel && (
              <button type="button" className="btn btn-ghost btn-sm" data-testid="sr-activity-cancel" disabled={busyKey === e.key} onClick={() => cancel(e)}>取消</button>
            )}
            {canResume && (
              <button type="button" className="btn btn-accent btn-sm" data-testid="sr-activity-resume" disabled={busyKey === e.key} onClick={() => resume(e)}>继续分类</button>
            )}
            {e.status === "succeeded" && e.kind === "import" && e.bookId && onOpenBook && (
              <button type="button" className="btn btn-quiet btn-sm" onClick={() => { onOpenBook(e.bookId); srActivityDismiss(e.key); }}>打开</button>
            )}
            {e.status !== "running" && (
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => srActivityDismiss(e.key)}>关闭</button>
            )}
          </div>
        )}
      </li>
    );
  };

  return (
    <section className="sr-import-progress" data-testid="sr-import-progress" aria-label="参考书活动">
      <div className="ws-sr-only" aria-live="polite" role="status">{announce}</div>
      <header className="sr-activity-head">
        <button type="button" className="sr-activity-toggle" aria-expanded={!collapsed} aria-controls={listId} onClick={() => setCollapsed((c) => !c)}>
          {collapsed ? <I.ChevronRight size={13} /> : <I.ChevronDown size={13} />}
          <span>参考书活动</span>
          <span className="sr-activity-count tab-num">{entries.length}</span>
          {runningCount > 0 && <Spinner size={11} />}
        </button>
        {finishedCount > 0 && (
          <button type="button" className="btn btn-quiet btn-xs" onClick={srActivityClearFinished}>清除已完成</button>
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
