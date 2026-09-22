import React from "react";
import { I } from "./icons.jsx";
import { Segmented } from "./ws-ui.jsx";
import { WrCanonicalControl } from "./wr-canonical-control.jsx";
import { modCombo } from "./ws-writer-keys.js";
import { useWrCount } from "./ws-writer-hooks.js";

/* ==========================================================
   写作台顶栏（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   一行、定高：场景位置（点开大纲）· 起草 / 深改 · 保存与权威正文状态 · 本场字数。
   字数、篇幅进度环、顶部进度线和「还没有可提升的正文」都读字数小仓库（useWrCount），
   敲字时只有这几个叶子组件重渲染。ESM 模块，不写 window。
   ========================================================== */

/* 篇幅进度环：只在这一场的设计给了数字篇幅（如 1300–1600 字）时出现 */
function GoalRing({ pct }) {
  const r = 9;
  const c = 2 * Math.PI * r;
  const off = c * (1 - Math.min(100, pct) / 100);
  return (
    <svg className="wr-ring" width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
      <circle className="wr-ring-bg" cx="11" cy="11" r={r} strokeWidth="2.5" />
      <circle className={`wr-ring-fill ${pct >= 100 ? "is-full" : ""}`} cx="11" cy="11" r={r} strokeWidth="2.5"
        strokeDasharray={c} strokeDashoffset={off} />
    </svg>
  );
}

function goalPercent(count, lengthRange) {
  return lengthRange ? Math.min(100, Math.round((count / lengthRange.min) * 100)) : 0;
}

/* 顶部的细进度线：写到设计篇幅的下限就满 */
export function WrProgress({ counter, lengthRange }) {
  const count = useWrCount(counter);
  if (!lengthRange) return null;
  const pct = goalPercent(count, lengthRange);
  return <div className={`wr-progress ${pct >= 100 ? "is-full" : ""}`} style={{ width: pct + "%" }} />;
}

/* 本场字数；设计给了数字篇幅时对照「N / min–max 字」画进度环，短 / 中 / 长没有约定字数，只报字数 */
function WrWordCount({ counter, design }) {
  const count = useWrCount(counter);
  const lengthRange = design && design.lengthRange ? design.lengthRange : null;
  const title = lengthRange
    ? `本场字数 / 设计篇幅 ${lengthRange.min}–${lengthRange.max} 字`
    : (design && design.lengthLabel ? `本场字数（设计篇幅：${design.lengthLabel}）` : "本场字数");
  return (
    <span className="wr-count-wrap" title={title}>
      {lengthRange && <GoalRing pct={goalPercent(count, lengthRange)} />}
      <span className="wr-count">
        <b>{count}</b>
        {lengthRange ? <span className="wr-count-goal"> / {lengthRange.min}–{lengthRange.max}</span> : null}
        <span className="wr-count-unit"> 字</span>
      </span>
    </span>
  );
}

/* 「提升为权威正文」只对写了字的草稿有意义：一个字都没有时不显示待更新，也不给提升 */
function WrCanonicalStatus({ counter, saved, canonicalStatus, disabled, onPromote }) {
  const count = useWrCount(counter);
  const view = count === 0 && (canonicalStatus === "dirty" || canonicalStatus === "unknown") ? "none" : canonicalStatus;
  return <WrCanonicalControl saveStatus={saved} canonicalStatus={view} disabled={disabled} onPromote={onPromote} />;
}

export function WrHeader({
  meta, onOpenOutline, posture, onPosture, deepIssueCount, hasScene, approvedLocked,
  counter, saved, canonicalStatus, canonicalDisabled, onPromote,
}) {
  return (
    <div className="wr-top">
      <button type="button" className="wr-loc" onClick={onOpenOutline} title={`章节大纲（${modCombo("1")}）`}
        aria-label={`${meta.stamp || ""} ${meta.title || "当前场景"}，打开章节大纲`}>
        {meta.stamp && <span className="wr-loc-num">{meta.stamp}</span>}
        <span className="wr-loc-name">{meta.title}</span>
        <span className="wr-loc-chev" aria-hidden="true"><I.ChevronDown size={14} /></span>
      </button>
      <Segmented
        className="wr-posture"
        label="写作姿态"
        size="sm"
        value={posture}
        onChange={onPosture}
        options={[
          { value: "draft", label: "起草", icon: <I.Pen size={12} />, title: "起草：编辑正文" },
          {
            value: "deep", label: "深改", icon: <I.Microscope size={12} />,
            count: posture === "deep" && deepIssueCount > 0 ? deepIssueCount : null,
            disabled: !hasScene || approvedLocked,
            title: approvedLocked ? "终稿已锁定，请先重新打开章节" : "深改：逐句诊断，正文只读",
          },
        ]}
      />
      <div className="wr-top-spacer" />
      <div className="wr-stat">
        <WrCanonicalStatus counter={counter} saved={saved} canonicalStatus={canonicalStatus}
          disabled={canonicalDisabled} onPromote={onPromote} />
        <WrWordCount counter={counter} design={meta.design} />
      </div>
    </div>
  );
}
