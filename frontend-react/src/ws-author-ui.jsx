import React from "react";
import { I } from "./icons.jsx";
import { ARR_CH_STATE, ARR_SCENE_STATE } from "./ws-author-data.jsx";
import { arrChapterStatus } from "./ws-author-derive.js";
import { Tag } from "./ws-ui.jsx";

/* ==========================================================
   章节编排 · 小零件（全书清单、序列栏、章节详情共用）
   状态标签、进度点、字数条、抓手。纯展示，不读 store。
   ========================================================== */

/* 章的状态：已定稿 / 审阅中来自后端流程，其余按各场的进度读出来（arrChapterStatus，只显示不回写） */
function ArrChapterStateTag({ ch }) {
  const st = arrChapterStatus(ch);
  const m = ARR_CH_STATE[st.key] || ARR_CH_STATE.planned;
  return (
    <Tag tone={m.tone} dot className="arr-state-tag"
      title={st.derived ? "按各场的进度显示：有一场动了笔就是写作中。审阅与终稿批准由成稿中心推进。" : "由审阅与终稿批准流程推进"}>
      {m.label}
    </Tag>
  );
}

function ArrSceneStateTag({ s }) {
  const m = ARR_SCENE_STATE[s.state] || ARR_SCENE_STATE.todo;
  return <Tag tone={m.tone} dot title="场景完成状态由正文归档流程推进">{m.label}</Tag>;
}

/* 一章各场的进度点 */
function ArrMiniScenes({ scenes }) {
  return (
    <span className="arr-dots" aria-hidden="true">
      {scenes.map((s, i) => (
        <span key={i} className="arr-dot" style={{ background: (ARR_SCENE_STATE[s.state] || ARR_SCENE_STATE.todo).dot }} />
      ))}
    </span>
  );
}

/* 字数条：只在设过字数目标时画（0 目标下任何字数都会被算成「超额」） */
function ArrBudgetBar({ cur, target }) {
  if (!(target > 0)) return null;
  const pct = Math.min(100, Math.round((cur / target) * 100));
  const over = cur > target * 1.08;
  return (
    <span className="arr-budget" title={`${cur.toLocaleString()} / ${target.toLocaleString()} 字`}>
      <span className="arr-budget-track"><span className={`arr-budget-fill ${over ? "is-over" : ""}`} style={{ width: (cur === 0 ? 0 : Math.max(4, pct)) + "%" }} /></span>
    </span>
  );
}

/* 手建的章 / 场：抓手是一个按钮——可以拖，键盘上用上下方向键挪。构思分出来的、已批准终稿的只是个标记。
   moveKey 让键盘挪完之后能把焦点找回来（见 ws-author-hooks.js 的 useRefocusAfterMove）。 */
function ArrGrip({ movable, dnd, label, fixedTip, onMove, className, moveKey }) {
  if (!movable) {
    return <span className={`${className} is-fixed`} draggable={false} title={fixedTip} aria-hidden="true"><I.GripVertical size={14} /></span>;
  }
  return (
    <button type="button" className={className} {...dnd} aria-label={label} title={`${label}：拖动，或用上下方向键`}
      aria-keyshortcuts="ArrowUp ArrowDown" data-arr-move={moveKey}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
        e.preventDefault();
        e.stopPropagation();
        onMove(e.key === "ArrowUp" ? -1 : 1);
      }}>
      <I.GripVertical size={14} />
    </button>
  );
}

export { ArrBudgetBar, ArrChapterStateTag, ArrGrip, ArrMiniScenes, ArrSceneStateTag };
