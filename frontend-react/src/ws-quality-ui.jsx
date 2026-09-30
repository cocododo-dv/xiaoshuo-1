import React from "react";
import { StatTile } from "./ws-ui.jsx";

/* ==========================================================
   ws-quality-ui — 成本看板（ws-cost.jsx / ws-cost-parts.jsx，只有它们用）的统计卡：ws-ui 的 StatTile
   外加一张卡的底（样式 .q-stat 在 ws-quality.css）。children 是跟在注释前面的小标签（如「估算价」）。
   它该搬到成本看板自己的文件里（F04-14）：那两个文件归视图包 B，已列为请求；搬完这个文件就删掉。
   纯展示组件，无模块级副作用。
   ========================================================== */

function StatCard({ label, value, hint, tone, children, className }) {
  const note = children || hint
    ? <>{children}{children && hint ? " " : null}{hint}</>
    : null;
  return <StatTile className={`q-stat${className ? ` ${className}` : ""}`} label={label} value={value} hint={note} tone={tone} />;
}

export { StatCard };
