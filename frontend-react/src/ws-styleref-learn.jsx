import React from "react";
import { I } from "./icons.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { Notice, Spinner, Tag } from "./ws-ui.jsx";
import {
  SR_ACTIVITY_WHERE, srActivityView, srFormatWhen, srJobErrorText, srLearnEstimateText, srRelearnText,
} from "./ws-styleref-model.js";
import {
  srActivityFor, srCancelLearn, srLearnInfo, srLoadLearn, srLoadRuntime, srRuntime, srStartLearn,
} from "./ws-styleref-store.js";
import { SrErrorLine, SrProgressBar, srNotifyError, useSrStore } from "./ws-styleref-ui.jsx";
import { SrPortrait } from "./ws-styleref-portrait.jsx";

/* ==========================================================
   风格参考 · 第二步「学习文风」：一个按钮、一个作业（整理窗口 → 挑样本 → 分层读原文 → 写文风卡 →
   识别本书专名 → 给全书片段打标签 → 写入画像），下面就是学出来的文风画像。
   · 开始前给估算（调用次数、每层读多少原文）；在跑时显示进度、可取消；失败 / 取消后可「继续学习」（从断点续上）；
   · 已经学过：重新学习是就地更新同一份画像（用在作品上的设置与你的 ✓ / ✗ 都保留）；
   · 段落类型更新过 / 正文变过 / 旧版画像时，提示建议重新学习。
   ========================================================== */

export function SrLearn({ book, go, onAction }) {
  return (
    <div className="sr-learn">
      <SrLearnCard book={book} go={go} onAction={onAction} />
      <SrPortrait book={book} go={go} onAction={onAction} />
    </div>
  );
}

export function SrLearnCard({ book, go, onAction }) {
  useSrStore("detail", "activity", "books");
  const [busy, setBusy] = React.useState(null);
  const [error, setError] = React.useState(null);
  React.useEffect(() => { srLoadLearn(book.id); srLoadRuntime(); }, [book.id]);
  const info = srLearnInfo(book.id);
  const data = info && info.data;
  /* 先看得到的拦路：没有模型；「仅本机模型」的书而学习节点不在本机。服务端仍是最后一道闸（拒了照样说清楚） */
  const runtime = srRuntime();
  const noLlm = runtime.phase === "ready" && !!runtime.data && runtime.data.llm_enabled === false;
  const routes = (data && Array.isArray(data.routes)) ? data.routes : [];
  const cloudBlocked = !noLlm && book.cloudPolicy === "local_only" && routes.some((r) => r && r.local === false);
  const gate = noLlm
    ? { testId: "sr-learn-no-llm", text: "还没有接入模型：学习文风要由模型分层读原文。" }
    : cloudBlocked
      ? { testId: "sr-learn-cloud-blocked", text: "这本书设为「仅本机模型」，但学习用的模型不在本机：在设置里把学习节点换成本机模型，或用别的范围重新导入。" }
      : null;
  const lastJob = (data && data.learn) || book.learn || null;
  const running = srActivityFor(book.id, "learn");
  const view = running ? srActivityView(running) : null;
  const profile = book.profile;
  const ready = book.rawStatus === "ready";
  const classifying = !!srActivityFor(book.id, "classify");
  const resumable = !running && lastJob && (lastJob.state === "failed" || lastJob.state === "cancelled" || lastJob.stalled) && lastJob.resumable;
  const estimateText = data ? srLearnEstimateText(data.estimate, { bookChars: book.chars }) : null;

  const start = async (resume = false) => {
    if (busy) return;
    if (!resume && profile) {
      const ok = await wsConfirm({
        title: `重新学习《${book.title}》的文风？`,
        body: `${estimateText ? `${estimateText}。` : ""}会就地更新这份文风画像：用在作品上的设置和你对各句的 ✓ / ✗ 都保留。`,
        confirmLabel: "重新学习",
      });
      if (!ok) return;
    }
    setBusy(resume ? "resume" : "start"); setError(null);
    try { await srStartLearn(book.id, { resume }); }
    catch (e) { setError(e); }
    finally { setBusy(null); }
  };

  const cancel = async () => {
    if (busy) return;
    setBusy("cancel");
    try { await srCancelLearn(book.id); }
    catch (e) { srNotifyError(e); }
    finally { setBusy(null); }
  };

  const status = running ? { tone: "warn", label: `学习中 ${view.percentText}` }
    : profile && profile.needs_relearn ? { tone: "warn", label: profile.relearn_reason === "legacy_profile" ? "旧版画像" : "建议重新学习" }
    : profile ? { tone: "ok", label: "已学好" }
    : resumable ? { tone: "danger", label: "学习没有完成" }
    : { tone: "neutral", label: "还没学" };
  /* 上次失败的原因只说中文：作业边界记下的英文原话（「TypeError: …」）换成一句中文 */
  const lastError = !running && lastJob && lastJob.state === "failed" && lastJob.error
    ? { message: srJobErrorText(lastJob.error, { resumable: !!lastJob.resumable }) }
    : null;

  return (
    <div className="card sr-learn-card" data-testid="sr-learn-card">
      <div className="card-head">
        <div>
          <div className="card-title">学习文风</div>
          <div className="card-sub">模型分层读这本书的原文，写出「这位作者和通用写法哪里不一样」的文风卡，并给全书片段打上场面 / 情绪标签，起草时按这一场挑样例。</div>
        </div>
        <Tag tone={status.tone} dot testId="sr-learn-status">{status.label}</Tag>
      </div>

      {running ? (
        <div className="sr-ov-live" data-testid="sr-learn-running">
          <SrProgressBar percent={view.percent} label="学习文风进度" />
          <div className="sr-activity-meta">{view.detail}</div>
          <div className="sr-ov-foot">
            <span className="sr-ov-hint">{`进度也在${SR_ACTIVITY_WHERE}里；取消之后可以从断点接着学。`}</span>
            <button type="button" className="btn btn-ghost btn-sm" data-testid="sr-learn-cancel" disabled={!!busy || running.cancel_requested} onClick={cancel}>
              {busy === "cancel" || running.cancel_requested ? <><Spinner size={12} /> 正在取消…</> : "取消"}
            </button>
          </div>
        </div>
      ) : !ready ? (
        <Notice
          tone="info"
          testId="sr-learn-blocked"
          actions={go ? <button type="button" className="btn btn-ghost btn-sm" onClick={() => go("book")}>去看段落分类</button> : null}
        >
          {classifying ? "段落还在分类，分完才能学。" : "这本书的段落分类还没完成，分完才能学。"}
        </Notice>
      ) : (
        <>
          {gate && (
            <Notice
              tone="warn"
              testId={gate.testId}
              actions={onAction ? <button type="button" className="btn btn-ghost btn-sm" onClick={() => onAction({ type: "settings" })}>去设置模型</button> : null}
            >
              {gate.text}
            </Notice>
          )}
          {profile && profile.needs_relearn && (
            <Notice tone="warn" testId="sr-learn-relearn">{srRelearnText(profile.relearn_reason)}</Notice>
          )}
          {lastError && (
            <p className="sr-ov-text" data-testid="sr-learn-last-error">上次学习没有完成：{lastError.message}</p>
          )}
          {profile && !profile.needs_relearn && (
            <p className="sr-ov-text" data-testid="sr-learn-done">
              {profile.learned_at ? `学于 ${srFormatWhen(profile.learned_at)}` : "已学好"}
              {profile.version_tag ? ` · ${profile.version_tag}` : ""}
              {profile.card_lines ? ` · 文风卡 ${profile.card_lines} 句` : ""}
            </p>
          )}
          <div className="sr-learn-actions">
            {resumable && (
              <button type="button" className="btn btn-accent btn-sm" data-testid="sr-learn-resume" disabled={!!busy || classifying || !!gate} onClick={() => start(true)}>
                {busy === "resume" ? <><Spinner size={12} /> 启动中…</> : "继续学习"}
              </button>
            )}
            <button
              type="button"
              className={`btn ${resumable || (profile && !profile.needs_relearn) ? "btn-ghost" : "btn-accent"} btn-sm`}
              data-testid="sr-learn-start"
              disabled={!!busy || classifying || !!gate}
              title={classifying ? "正在重新分类段落，分完再学" : gate ? gate.text : undefined}
              onClick={() => start(false)}
            >
              {busy === "start" ? <><Spinner size={12} /> 启动中…</> : <><I.Sparkles size={13} /> {profile ? "重新学习" : "学习文风"}</>}
            </button>
            <span className="sr-learn-estimate" data-testid="sr-learn-estimate">
              {estimateText || (info && info.phase === "loading" ? "正在估算……" : "")}
            </span>
          </div>
        </>
      )}
      <SrErrorLine error={error} onAction={onAction} testId="sr-learn-error" />
    </div>
  );
}
