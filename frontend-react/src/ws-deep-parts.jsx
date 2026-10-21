import React from "react";
import { I } from "./icons.jsx";
import { cx } from "./ws-ui.jsx";

/* ==========================================================
   ws-deep-parts — 深改面板里两样共用的小件（2026-10-21 右栏重排）
   ----------------------------------------------------------
   · DxFold    折叠：一个小标题按钮 + 展开的内容。整体建议、参考书校准、为什么、已忽略、最近的决定都是它——
               不常看的默认收起，第一屏留给发现清单。给 open / onToggle 时由调用方管开合（「已忽略」要能被
               跳进来的那条发现撑开），不给就自己记。
   · DxSource  来源状态条里的一行：名字 + 跑一次的按钮，下面一行说新旧与分数；出错的提示、折叠的内容放 children。
               AI 深评（ws-deep.jsx）与风格对照（ws-deep-style.jsx）共用，两行长得一样、按钮一个样式。
   ESM 模块，不写 window。
   ========================================================== */

export function DxFold({ label, open: controlled, onToggle, defaultOpen = false, className, children }) {
  const [own, setOwn] = React.useState(defaultOpen);
  const bodyId = React.useId();
  const open = controlled == null ? own : controlled;
  const toggle = onToggle || (() => setOwn((value) => !value));
  return (
    <div className={cx("wr-dxd-fold", className)}>
      <button type="button" className="wr-dxd-fold-btn" aria-expanded={open} aria-controls={open ? bodyId : undefined} onClick={toggle}>
        <I.ChevronRight size={12} /><span>{label}</span>
      </button>
      {open ? <div className="wr-dxd-fold-body" id={bodyId}>{children}</div> : null}
    </div>
  );
}

export function DxSource({ icon, name, meta, stale = false, action, className, testId, metaTestId, children }) {
  const Icon = icon ? I[icon] : null;
  return (
    <section className={cx("wr-dxd-source", className)} aria-label={name} data-testid={testId}>
      <div className="wr-dxd-source-head">
        <span className="wr-dxd-source-name">{Icon ? <Icon size={13} /> : null}{name}</span>
        {action}
      </div>
      {meta ? <p className={cx("wr-dxd-source-meta", stale && "is-stale")} data-testid={metaTestId}>{meta}</p> : null}
      {children}
    </section>
  );
}
