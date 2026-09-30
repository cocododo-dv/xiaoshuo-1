import React from "react";
import { I } from "./icons.jsx";
import { ARR_SCENE_STATE } from "./ws-author-data.js";
import { Tag } from "./ws-ui.jsx";
import { isImeComposing } from "./lib/keyboard.js";
import { chapterStage, chapterStageDerived, chapterStateMeta } from "./labels/catalog.js";

/* ==========================================================
   章节编排 · 小零件（全书清单、序列栏、章节详情共用）
   状态标签、进度点、字数条、抓手、行内输入框的回车。纯展示，不读 store。
   ========================================================== */

/* 行内输入框按回车 = 改完了（失焦写回）。输入法确认候选的那一下回车不算：拼音选词时按回车会把半截题名写进目录、把作者甩出输入框 */
export const blurOnEnter = (e) => { if (e.key === "Enter" && !isImeComposing(e)) e.target.blur(); };

/* 章的状态：与主页、成稿中心同一条规则（labels/catalog.js 的 chapterStage，只显示不回写）。
   已定稿 / 审阅中 / 草稿来自审阅与批准流程，其余按各场的进度读出来。 */
function ArrChapterStateTag({ ch }) {
  const m = chapterStateMeta(chapterStage(ch));
  return (
    <Tag tone={m.tone} dot className="arr-state-tag"
      title={chapterStageDerived(ch) ? "按各场的进度显示：有一场动了笔就是写作中。审阅与终稿批准由成稿中心推进。" : "由审阅与终稿批准流程推进"}>
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
