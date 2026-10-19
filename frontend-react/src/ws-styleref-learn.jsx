import React from "react";
import { I } from "./icons.jsx";
import { wsConfirm } from "./ws-notify.jsx";
import { Notice, ProgressBar, RadioCards, Spinner, Tag } from "./ws-ui.jsx";
import {
  SR_ACTIVITY_WHERE, SR_LEARN_MODE_FULL, SR_LEARN_MODE_QUICK, srActivityView, srFormatWhen, srFullReusable, srInputTooSmall,
  srJobErrorText, srLearnModeEstimateText, srLearnModeLabel, srModelGate, srRelearnText,
} from "./ws-styleref-model.js";
import {
  srActivityFor, srCancelLearn, srLearnInfo, srLoadLearn, srLoadRuntime, srRuntime, srStartLearn,
} from "./ws-styleref-store.js";
import { SrErrorLine, srNotifyError, useSrStore } from "./ws-styleref-ui.jsx";
import { SrPortrait } from "./ws-styleref-portrait.jsx";

/* ==========================================================
   风格参考 · 第二步「学习文风」：一个按钮、一个作业（整理窗口 → 挑样本 → 分层读原文 → 写文风卡 →
   识别本书专名 → 给全书片段打标签 → 写入画像），下面就是学出来的文风画像。
   · 开始前给估算（调用次数、每层读多少原文）；在跑时显示进度、可取消；失败 / 取消后可「继续学习」（从断点续上，
     按后端的 resumable——不可续跑的失败只给「重新学习」）；正文太短而失败（reason_code input_too_small，或建作业时
     409 STYLE_REFERENCE_INPUT_TOO_SMALL）另给「仍然学习」= force；
   · 已经学过：重新学习是就地更新同一份画像，并且在上一版文风卡上修订（没有新证据的句子原样留下；用在作品上的设置与
     你的 ✓ / ✗ 都保留）；
   · 两种学习方式（2026-10-19）：「通读全书」按整章分批读完全书、每维归并后写卡（读完才更新画像；上一次通读还能用时
     只重写文风卡，可勾「重新通读」）；「快速学习」固定读约 12 窗 / 4 万字，换模型或改提示词之后拿来对比。通读开始前
     一律确认（调用次数多）；
   · 段落类型更新过 / 正文变过时，提示建议重新学习。
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
  const [mode, setMode] = React.useState(SR_LEARN_MODE_FULL);
  const [reread, setReread] = React.useState(false);
  React.useEffect(() => { srLoadLearn(book.id); srLoadRuntime(); }, [book.id]);
  const info = srLearnInfo(book.id);
  const data = info && info.data;
  /* 先看得到的拦路：没有模型；「仅本机模型」的书而学习节点不在本机。服务端仍是最后一道闸（拒了照样说清楚）。
     学习节点在不在本机看学习信息里的路由；运行时还没读到时也照样按路由判 */
  const runtime = srRuntime();
  const routes = (data && Array.isArray(data.routes)) ? data.routes : [];
  const gate = srModelGate((runtime.phase === "ready" && runtime.data) || {}, { purpose: "learn", cloudPolicy: book.cloudPolicy, routes });
  const gateTestId = gate ? (gate.kind === "no_llm" ? "sr-learn-no-llm" : "sr-learn-cloud-blocked") : null;
  const lastJob = (data && data.learn) || book.learn || null;
  const running = srActivityFor(book.id, "learn");
  const view = running ? srActivityView(running) : null;
  const profile = book.profile;
  const ready = book.rawStatus === "ready";
  const classifying = !!srActivityFor(book.id, "classify");
  const unfinished = !running && !!lastJob && (lastJob.state === "failed" || lastJob.state === "cancelled" || !!lastJob.stalled);
  /* 能不能从断点续上，听后端的 resumable：不可续跑的失败（正文太少这类）只能重新学习 */
  const resumable = unfinished && !!lastJob.resumable;
  const tooSmall = unfinished && lastJob.state === "failed" && srInputTooSmall(lastJob.error);
  const full = mode === SR_LEARN_MODE_FULL;
  const reusable = full && data ? srFullReusable(data.estimate) : null;
  const estimateText = data ? srLearnModeEstimateText(data.estimate, mode, { bookChars: book.chars, reread }) : null;

  const start = async (resume = false, { force = false } = {}) => {
    if (busy) return;
    if (!resume && !force && (profile || full)) {
      const keep = profile ? "会就地更新这份文风画像、在上一版文风卡上修订：用在作品上的设置和你对各句的 ✓ / ✗ 都保留。" : "";
      const ok = await wsConfirm({
        title: full ? `通读《${book.title}》全书学习文风？` : `重新学习《${book.title}》的文风？`,
        body: [
          estimateText ? `${estimateText}。` : "",
          full && !(reusable && !reread) ? "读完全书才更新文风画像；中途可以取消，之后接着读。" : "",
          keep,
        ].join(""),
        confirmLabel: full ? "开始通读" : "重新学习",
      });
      if (!ok) return;
    }
    setBusy(resume ? "resume" : force ? "force" : "start"); setError(null);
    try { await srStartLearn(book.id, { resume, force, mode, reread: full && reread }); }
    catch (e) { setError(e); }
    finally { setBusy(null); }
  };
  /* 建作业时就被拒「正文太少」：出错行给「仍然学习」，在这里就地发 force，不用绕到外壳 */
  const act = (action) => {
    if (action && action.type === "force_learn") start(false, { force: true });
    else if (onAction) onAction(action);
  };

  const cancel = async () => {
    if (busy) return;
    setBusy("cancel");
    try { await srCancelLearn(book.id); }
    catch (e) { srNotifyError(e); }
    finally { setBusy(null); }
  };

  const status = running ? { tone: "warn", label: `学习中 ${view.percentText}` }
    : profile && profile.needs_relearn ? { tone: "warn", label: "建议重新学习" }
    : profile ? { tone: "ok", label: "已学好" }
    : unfinished ? { tone: "danger", label: "学习没有完成" }
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
          <div className="card-sub">模型分层读这本书的原文，写出「这位作者和通用写法哪里不一样」的文风卡，并给全书片段打上场面 / 情绪标签，起草时按这一场挑样例。可以通读全书，也可以只读一组固定的样本。</div>
        </div>
        <Tag tone={status.tone} dot testId="sr-learn-status">{status.label}</Tag>
      </div>

      {running ? (
        <div className="sr-ov-live" data-testid="sr-learn-running">
          <ProgressBar value={view.percent} label="学习文风进度" tone="warn" className="sr-progress" />
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
              testId={gateTestId}
              actions={onAction ? <button type="button" className="btn btn-ghost btn-sm" onClick={() => onAction(gate.action)}>{gate.action.label}</button> : null}
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
              {profile.learn_mode ? ` · ${srLearnModeLabel(profile.learn_mode)}` : ""}
              {profile.version_tag ? ` · ${profile.version_tag}` : ""}
              {profile.card_lines ? ` · 文风卡 ${profile.card_lines} 句` : ""}
            </p>
          )}
          <RadioCards
            label="学习方式"
            className="sr-learn-mode"
            testId="sr-learn-mode"
            value={mode}
            onChange={setMode}
            items={[
              {
                value: SR_LEARN_MODE_FULL,
                label: "通读全书",
                testId: "sr-learn-mode-full",
                detail: "按整章分批读完整本书，每种写法数一数在多少批里出现过，再写文风卡；读完才算一轮。",
              },
              {
                value: SR_LEARN_MODE_QUICK,
                label: "快速学习",
                testId: "sr-learn-mode-quick",
                detail: "只读固定的一组样本（约 12 窗 / 4 万字，每次同一组），几分钟学完；换模型、改提示词之后拿来对比。",
              },
            ]}
          />
          {full && reusable && (
            <label className="sr-learn-reread">
              <input type="checkbox" data-testid="sr-learn-reread" checked={reread} onChange={(event) => setReread(event.target.checked)} />
              <span>重新通读（不沿用上一次读出来的结果）</span>
            </label>
          )}
          <div className="sr-learn-actions">
            {resumable && (
              <button type="button" className="btn btn-accent btn-sm" data-testid="sr-learn-resume" disabled={!!busy || classifying || !!gate} onClick={() => start(true)}>
                {busy === "resume" ? <><Spinner size={12} /> 启动中…</> : "继续学习"}
              </button>
            )}
            {tooSmall && (
              <button type="button" className="btn btn-accent btn-sm" data-testid="sr-learn-force" disabled={!!busy || classifying || !!gate} onClick={() => start(false, { force: true })}>
                {busy === "force" ? <><Spinner size={12} /> 启动中…</> : "仍然学习（正文很短）"}
              </button>
            )}
            <button
              type="button"
              className={`btn ${resumable || tooSmall || (profile && !profile.needs_relearn) ? "btn-ghost" : "btn-accent"} btn-sm`}
              data-testid="sr-learn-start"
              disabled={!!busy || classifying || !!gate}
              title={classifying ? "正在重新分类段落，分完再学" : gate ? gate.text : undefined}
              onClick={() => start(false)}
            >
              {busy === "start" ? <><Spinner size={12} /> 启动中…</> : <><I.Sparkles size={13} /> {profile || unfinished ? "重新学习" : "学习文风"}</>}
            </button>
            <span className="sr-learn-estimate" data-testid="sr-learn-estimate">
              {estimateText || (info && info.phase === "loading" ? "正在估算……" : "")}
            </span>
          </div>
        </>
      )}
      <SrErrorLine error={error} onAction={act} testId="sr-learn-error" />
    </div>
  );
}
