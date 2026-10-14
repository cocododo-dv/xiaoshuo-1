import React from "react";
import { I } from "./icons.jsx";
import { Notice, ProgressBar, Tag } from "./ws-ui.jsx";
import { srActivityActive, srActivityView, srCalibrationView } from "./ws-styleref-model.js";
import { srActivityFor, srCalibrateJudge, srCancelCalibration } from "./ws-styleref-store.js";
import { srNotifyError, useSrStore } from "./ws-styleref-ui.jsx";

/* ==========================================================
   文风画像页的「评审校准」卡（2026-10-14 评审统一）
   评审给「像不像」打的分本身没有零点：同一个评审给这本书几段原文和它们的普通改写各打一次分，之后的分按这两点
   折算（0 = 普通文字，10 = 作者原文）；两类拉不开 = 这个评审模型分不清这位作者，它的分不作数。
   学习文风成功后后端自动排一次；这张卡说此刻的口径、给「校准评审 / 重新校准」与在跑时的进度和取消。
   在跑的进度读活动清单（/activity 轮询）里这本书的 calibrate 作业；画像详情里的 job 只在清单里还没有时兜底。
   ========================================================== */

export function SrJudgeCalibrationCard({ profileId, bookId, calibration }) {
  useSrStore("activity");
  const [busy, setBusy] = React.useState(false);
  const view = srCalibrationView(calibration);
  if (!view) return null;
  const live = bookId ? srActivityFor(bookId, "calibrate") : null;
  const job = live || (calibration && calibration.job) || null;
  const running = !!job && srActivityActive(job);
  const jobView = job ? srActivityView(job) : null;
  const failed = !running && job && job.status === "failed" && calibration.state !== "calibrated";
  const run = async (action) => {
    setBusy(true);
    try { await action(); } catch (err) { srNotifyError(err); } finally { setBusy(false); }
  };
  const actions = running ? (
    <button
      type="button"
      className="btn btn-ghost btn-sm"
      data-testid="sr-calibration-cancel"
      disabled={busy || !!job.cancel_requested}
      onClick={() => run(() => srCancelCalibration(profileId))}
    >
      <I.X size={13} /> 取消校准
    </button>
  ) : (
    <button
      type="button"
      className="btn btn-accent btn-sm"
      data-testid="sr-calibration-start"
      disabled={busy}
      onClick={() => run(() => srCalibrateJudge(profileId, { bookId }))}
    >
      <I.Target size={13} /> {view.buttonLabel}
    </button>
  );
  return (
    <div className="card" data-testid="sr-calibration">
      <div className="card-head">
        <div>
          <div className="card-title">评审校准</div>
          <div className="card-sub">{view.title}</div>
        </div>
        <Tag tone={view.tone} dot testId="sr-calibration-state">{view.label}</Tag>
      </div>
      <Notice tone={view.tone === "ok" ? "info" : view.tone} testId="sr-calibration-text" actions={actions}>
        {view.text}
        {view.undistinguished.length > 0 && (
          <> 这几维评审分不清，分数不作数：{view.undistinguished.join("、")}。</>
        )}
      </Notice>
      {running && (
        <ProgressBar
          value={jobView.percent}
          label="评审校准的进度"
          valueText={jobView.detail}
          testId="sr-calibration-progress"
        />
      )}
      {running && jobView.detail && <p className="sr-ov-hint" role="status">{jobView.detail}</p>}
      {failed && (
        <Notice tone="warn" testId="sr-calibration-failed">上次校准没有完成：{jobView.detail}</Notice>
      )}
    </div>
  );
}
