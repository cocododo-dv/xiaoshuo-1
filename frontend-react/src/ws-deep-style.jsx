import React from "react";
import { I } from "./icons.jsx";
import { Notice, Spinner } from "./ws-ui.jsx";
import { fidErrorInfo, fidJobView, fidPassText, fidScoreText } from "./ws-fidelity-model.js";
import { fidCheck, fidResumeCheck, fidSceneCheckKey, fidStartCheck, useFidelityStore } from "./ws-fidelity-store.js";
import { formatLocaleMonthDayTime } from "./lib/format.js";

/* ==========================================================
   ws-deep-style — 写作台深改面板里「风格对照」那一块（2026-10-20）
   ----------------------------------------------------------
   风格参考的「对照检查」做完以前是死胡同：评审给的改法只存在读数里、哪儿都不显示，写作台的诊断也不认它。
   现在服务端把这一场最近一次对照检查（评审问三次、每维取中位）里低于及格线、给了改法的维并进同一份诊断
   （source = "style"），面板照常列出、可按改法改写、可忽略；这一块只说这次检查的口径与新旧，并能就地再查一次：
   · style：诊断载荷的 style 块 { bound, status: not_run | current | stale, overall, pass_score, judge_state, samples,
     created_at, summary }；没绑定参考书的场不画这一块。
   · 检查作业与起草台共用一个键（fidSceneCheckKey）：在哪边发起，两边看到的是同一份进度；跑完（读数 id 与诊断里的
     不一样了）调 onChecked 重新取诊断，新发现进清单。
   ========================================================== */

const STATE_WORDS = { calibrated: "已校准", missing: "模型原始分", stale: "校准已过期", unusable: "评审不可用" };

export function DxStyleBlock({ style, sceneId, onChecked }) {
  useFidelityStore();
  const key = sceneId ? fidSceneCheckKey(sceneId) : null;
  const entry = key ? fidCheck(key) : null;
  const busy = !!entry && (entry.phase === "starting" || entry.phase === "running");
  const doneReading = entry && entry.phase === "done" && entry.reading ? entry.reading.reading_id : null;
  const seen = React.useRef(null);
  React.useEffect(() => { if (key) fidResumeCheck(key); }, [key]);
  /* 检查跑完、诊断里还是旧的那次：重新取一次诊断（每个读数只取一次） */
  React.useEffect(() => {
    if (!doneReading || doneReading === (style && style.reading_id) || seen.current === doneReading) return;
    seen.current = doneReading;
    if (onChecked) onChecked();
  }, [doneReading, style, onChecked]);
  if (!style || !style.bound || !sceneId) return null;

  const status = style.status || "not_run";
  const when = formatLocaleMonthDayTime(style.created_at);
  const line = fidPassText(style.pass_score);
  const samples = Number(style.samples || 1);
  const start = () => { if (!busy) fidStartCheck(key, { sceneId }); };
  const view = busy ? fidJobView(entry.job) : null;
  const error = !busy && entry && entry.phase === "failed" ? fidErrorInfo(entry.error) : null;
  return (
    <section className="wr-dxd-ai" aria-label="风格对照" data-testid="dx-style">
      <div className="wr-dxd-ai-head">
        <span className="wr-dxd-sub"><I.Target size={13} /> 风格对照</span>
        <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={start} data-testid="dx-style-check">
          {busy ? <Spinner size={13} /> : <I.Target size={13} />} {busy ? "检查中…" : (status === "not_run" ? "对照检查" : "重新检查")}
        </button>
      </div>
      <p className="wr-dxd-ai-meta" data-testid="dx-style-meta">
        {busy && `${(view && view.label) || "排队中"}：请模型对着参考书的原文样例评审这一场（问三次、每维取中位，一两分钟）。`}
        {!busy && status === "not_run" && "还没对照检查过：请模型对着参考书的原文样例评审这一场，低于及格线的维会带着改法并进下面的清单（来源「风格对照」）。"}
        {!busy && status === "current" && (
          `${when ? `${when} ` : ""}对照检查：评审 ${fidScoreText(style.overall)}（${line} 分以上算像，${STATE_WORDS[style.judge_state] || "模型原始分"}`
          + `${samples > 1 ? `，${samples} 次取中位` : "，只问了一次"}），对着现在这份正文。`
        )}
        {!busy && status === "stale" && `${when ? `${when} 的` : "这次"}对照检查是改前的——正文已经改过，改法引的那句找不到的会标出来；要按现在的稿子判断就重新检查。`}
      </p>
      {error && <Notice tone="warn" className="wr-dxd-notice">{error.message}</Notice>}
    </section>
  );
}
