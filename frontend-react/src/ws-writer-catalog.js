import React from "react";
import { WsCatalog } from "./ws-catalog.jsx";
import { sceneDesignModel } from "./ws-scene-design.jsx";
import { manuscriptStage, sceneLabel } from "./ws-labels.js";

/* ==========================================================
   写作台读目录（2026-09-21 从 ws-writer.jsx 拆出）
   ----------------------------------------------------------
   WsCatalog 是章节 / 场景的单一真相源；写作台只把它映射成大纲要的形状，
   并算出当前这一场的页头（章场编号、题名、设计卡）。
   目录每次通知（包括每次自动保存后的字数回写）都会给一份新数组；大纲的形状没变时
   沿用上一份，React.memo 的大纲栏就不必重渲染。ESM 模块，不写 window。
   ========================================================== */

const { useEffect, useMemo, useState } = React;

/* 目录 → 大纲形状（写作器本地渲染用） */
export function wrFromCatalog() {
  const cat = WsCatalog ? WsCatalog.get() : null;
  if (!cat) return [];
  return cat.map((c) => ({
    id: c.id, n: c.n, title: c.title, state: c.state,
    // 章徽标显示的阶段：与成稿中心、主页、章节编排同一条规则（目录上挂着「规划」但已经有字 = 写作中）
    stage: manuscriptStage(c),
    // 阶段 X：章从哪来 / 构思里的章摘要 / 脊柱标记——大纲上看得见这一章在书里的位置
    origin: c.origin || "manual", summary: c.summary || "", spine: c.spine || "",
    expanded: !!(c.current || c.state === "writing"),
    scenes: (c.scenes || []).map((s) => ({
      id: s.sid, title: s.title, summary: s.summary || "",
      state: s.state === "writing" ? "active" : (s.state || "todo"),
      // 阶段 Y：雪花整理出来的场，先后 = 构思第 9 步的行序——大纲里不给拖（手加的场照常）
      planOwned: !!(s.design && s.design.owner === "plan"),
    })),
  }));
}

function sameOutline(a, b) {
  if (a === b) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch (e) { return false; }
}

export function wrInitialScene() {
  if (!WsCatalog) return null;
  const hit = WsCatalog.writingScene();
  return hit ? hit.scene.sid : null;
}

/* 保存路径读目录此刻的章状态（不是渲染时的快照）：章节刚被批准，下一次自动保存就停 */
export function wrSceneIsApproved(sceneId) {
  if (!sceneId || !WsCatalog) return false;
  try {
    const hit = WsCatalog.sceneById(sceneId);
    return !!(hit && hit.chapter && hit.chapter.state === "approved");
  } catch (e) {
    return false;
  }
}

/* 当前这一场的页头：章场编号跟着大纲里的实际先后走（重排后立即更新）；
   设计卡与 AI 起草台是同一张（阶段 X）。 */
export function wrSceneMeta(chapters, sceneId) {
  if (!sceneId) return { stamp: "", title: "", goal: "", design: null };
  let hit = null;
  try { hit = WsCatalog ? WsCatalog.sceneById(sceneId) : null; } catch (e) { hit = null; }
  for (const chapter of chapters) {
    const index = chapter.scenes.findIndex((scene) => scene.id === sceneId);
    if (index >= 0) {
      return {
        stamp: sceneLabel(chapter, index),
        title: chapter.scenes[index].title,
        goal: (hit && hit.scene.goal) || "（本场目标待规划）",
        design: hit ? sceneDesignModel(hit) : null,
      };
    }
  }
  return { stamp: "", title: "", goal: "", design: null };
}

/* 全书的场按顺序排开：上一场 / 下一场 */
export function wrNeighbours(chapters, sceneId) {
  const flat = chapters.flatMap((chapter) => chapter.scenes);
  const index = flat.findIndex((scene) => scene.id === sceneId);
  const at = (offset) => (index >= 0 && flat[index + offset] ? flat[index + offset] : null);
  const next = at(1);
  const prev = at(-1);
  return { prevId: prev ? prev.id : null, nextId: next ? next.id : null, nextTitle: next ? next.title || "" : "" };
}

/* 订阅目录：大纲形状 + 一个版本号（设计卡等随目录内容变化的派生值按它重算）。
   onChange(prevScene => nextScene) 让调用方在目录重载 / 换作品时校正当前场。 */
export function useWrCatalog(onChange) {
  const [chapters, setChapters] = useState(wrFromCatalog);
  const [rev, setRev] = useState(0);
  useEffect(() => {
    if (!WsCatalog) return undefined;
    const sync = () => {
      const next = wrFromCatalog();
      setChapters((prev) => (sameOutline(prev, next) ? prev : next));
      setRev((value) => value + 1);
      if (onChange) onChange();
    };
    const un = WsCatalog.subscribe(sync);
    sync();
    return un;
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const refresh = () => {
    const next = wrFromCatalog();
    setChapters((prev) => (sameOutline(prev, next) ? prev : next));
    return next;
  };
  return { chapters, setChapters, rev, refresh };
}

export function useWrSceneMeta(chapters, sceneId, rev) {
  // rev：目录内容变了（设计卡、字数……）但大纲形状没变时，页头的设计卡也要重算
  return useMemo(() => wrSceneMeta(chapters, sceneId), [chapters, sceneId, rev]); // eslint-disable-line react-hooks/exhaustive-deps
}
