import React from "react";
import { StatTile } from "./ws-ui.jsx";

/* ==========================================================
   ws-quality-ui — 质量域共享小部件
   成本看板（ws-cost）等视图共用的统计卡：ws-ui 的 StatTile 外加一张卡的底
   （样式 .q-stat 在 ws-review.css）。children 是跟在注释前面的小标签（如「估算价」）。
   纯展示组件，无模块级副作用。
   ========================================================== */

function StatCard({ label, value, hint, tone, children, className }) {
  const note = children || hint
    ? <>{children}{children && hint ? " " : null}{hint}</>
    : null;
  return <StatTile className={`q-stat${className ? ` ${className}` : ""}`} label={label} value={value} hint={note} tone={tone} />;
}

export { StatCard };
