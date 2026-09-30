import React from "react";
import { SnowSync } from "./ws-snow-sync.jsx";
import { WsChapterPlanPanel } from "./ws-snow-chapters.jsx";
import { planIntentsForScene } from "./ws-scene-design.jsx";

/* ==========================================================
   章节编排 ·「整理章节结构」这扇门（从 ws-author.jsx 拆出，2026-10）
   阶段 Z：和构思页头的同名按钮是同一张面板、同一条落库路径（SnowSync.materialize）。章的结构只有这一个编辑器；
   章节编排只是它的第二扇门。确认写入之后目录整份重拉，这里给一句回执（arrPlanReceipt）。
   ========================================================== */

const { useState } = React;

/* 确认写入的回执：顺手做了什么都说一句（回收站里的占位章 / 变空的旧章、取回的章与场、被已终审的章挡住的排序） */
export function arrPlanReceipt(result) {
  const r = result || {};
  const notes = [
    (r.trashed_placeholder_chapters || []).length ? `${r.trashed_placeholder_chapters.length} 个没动过笔的空白占位章已移入回收站` : "",
    (r.trashed_empty_chapters || []).length ? `${r.trashed_empty_chapters.length} 个变空的旧章已移入回收站` : "",
    (r.restored_chapter_ids || []).length ? `${r.restored_chapter_ids.length} 章从回收站取回` : "",
    (r.restored_scene_ids || []).length ? `${r.restored_scene_ids.length} 场随旧章进了回收站的场景卡已取回` : "",
    r.chapter_order_held ? "目录里有已终审的章，按章表排会挪动它——新章暂时接在最后" : "",
  ].filter(Boolean);
  return "章节结构已按这一版写入目录" + (notes.length ? ` · ${notes.join(" · ")}` : "");
}

/* 返回 { openPlan, planPanel }：planPanel 是面板本身（没开时为 null），调用方把它放在页面最后 */
export function useArrPlanDoor({ goView, showNotice, refreshResync }) {
  const [planOpen, setPlanOpen] = useState(false);
  const openPlan = () => setPlanOpen(true);
  const onPlanDone = (result) => {
    setPlanOpen(false);
    showNotice({ text: arrPlanReceipt(result), tone: "ok", timeout: 9000 });
    refreshResync();
  };
  const goToSnowStep = (beKey) => {
    const feKey = SnowSync.feStepKey(beKey) || "";
    setPlanOpen(false);
    goView("snowflake", feKey ? { type: "ws:snow-step", detail: feKey } : null);
  };
  const goToSnowScene = (sceneId) => {
    setPlanOpen(false);
    goView("snowflake", planIntentsForScene(sceneId));
  };
  const planPanel = planOpen
    ? <WsChapterPlanPanel onClose={() => setPlanOpen(false)} onDone={onPlanDone} onGoToStep={goToSnowStep} onGoToScene={goToSnowScene} />
    : null;
  return { openPlan, planPanel };
}
