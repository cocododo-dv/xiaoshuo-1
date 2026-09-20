/* ==========================================================
   章节编排 · 派生层（阶段 Z，2026-09-20「一张章表、两扇门」）
   ----------------------------------------------------------
   章节编排过去有一套自己的章级模型——张力 / 章级 POV / 时间 / 地点 / 入口出口 / 线索——产品里没有任何
   地方能填，也没有任何东西喂它；于是真实作品上的「故事弧线」是一条 0.3 的平线，「视角 · 时空」三格全空，
   全书体检报「张力曲线健康」和一条「字数超额 1,803 / 0」。而作者在构思里真的做出来的东西——三幕与三个
   灾难、每一场的形态 / POV / 时间 / 地点 / 离场变化、每一章装着故事序上的第几到第几场——到了这里一样都看不见。

   这一层把后者读出来：全部是目录载荷（WsCatalog 的章 / 场）上的纯函数，不写 window、不碰网络。
   章级字段作者真的填过（旧数据、AI 编排写的）就用作者的；没填过才用场上读出来的，并标明「来自各场」。
   ========================================================== */

import { ARR_ACTS } from "./ws-author-data.jsx";

const BLANK_MARKERS = ["待规划", "待定", "待补", "待填"];
const isBlank = (value) => {
  const text = String(value == null ? "" : value).trim();
  return !text || text === "—" || BLANK_MARKERS.some((m) => text.includes(m));
};
const clean = (value) => (isBlank(value) ? "" : String(value).trim());

/* 这一章的结构归构思的分章（先后 / 幕 / 成员只在「整理章节结构」里改）吗 */
export const arrIsPlanChapter = (c) => !!(c && c.structure && c.structure.owner === "plan");
export const arrIsPlanScene = (s) => !!(s && s.design && s.design.owner === "plan");

/* 一场的三拍规划过没有（系统占位「（本场目标待规划）」按没填算） */
export function arrSceneBeatsPlanned(s) {
  return !!s && [s.goal, s.obstacle, s.turn].every((beat) => !isBlank(beat));
}

function countBy(list) {
  const counts = new Map();
  list.forEach((name) => { if (name) counts.set(name, (counts.get(name) || 0) + 1); });
  return [...counts.entries()].map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count);
}

const sceneOpening = (s) => clean(s && (s.summary || s.title));
const sceneClosing = (s) => clean(s && (s.exitChange || s.summary || s.title));

/* 一章的入口 / 出口：作者填过就用作者的；否则入口 = 第一场在做什么，出口 = 最后一场离场时变了什么 */
export function arrChapterEdge(ch, edge) {
  if (!ch) return { text: "", derived: false };
  const authored = clean(edge === "entry" ? ch.entry : ch.exit);
  if (authored) return { text: authored, derived: false };
  const scenes = ch.scenes || [];
  const text = edge === "entry" ? sceneOpening(scenes[0]) : sceneClosing(scenes[scenes.length - 1]);
  return { text, derived: !!text };
}

export function arrRangeLabel(structure) {
  const span = structure && structure.sceneRange;
  if (!span || !span.first) return "";
  return span.first === span.last ? `第 ${span.first} 场` : `第 ${span.first}–${span.last} 场`;
}

/* 章节详情要用的全部派生事实 */
export function arrChapterFacts(ch) {
  const scenes = (ch && ch.scenes) || [];
  const povs = countBy(scenes.map((s) => clean(s.povName)));
  const times = scenes.map((s) => clean(s.design && s.design.storyTime)).filter(Boolean);
  const places = countBy(scenes.map((s) => clean(s.design && s.design.location)));
  const authoredPov = clean(ch && ch.pov);
  const authoredTime = clean(ch && ch.time);
  const authoredPlace = clean(ch && ch.place);
  const timeLine = times.length ? (times[0] === times[times.length - 1] ? times[0] : `${times[0]} → ${times[times.length - 1]}`) : "";
  const planned = scenes.filter(arrSceneBeatsPlanned).length;
  return {
    povs,
    pov: { text: authoredPov || povs.map((p) => (povs.length > 1 ? `${p.name} ${p.count}` : p.name)).join(" · "), derived: !authoredPov && povs.length > 0 },
    time: { text: authoredTime || timeLine, derived: !authoredTime && !!timeLine },
    place: { text: authoredPlace || places.slice(0, 3).map((p) => p.name).join(" · "), derived: !authoredPlace && places.length > 0 },
    entry: arrChapterEdge(ch, "entry"),
    exit: arrChapterEdge(ch, "exit"),
    beats: { planned, total: scenes.length },
    rangeLabel: arrRangeLabel(ch && ch.structure),
  };
}

/* 镜头用的章：章级 POV / 时间没填时用场上读出来的（主 POV = 本章场次最多的那一位） */
export function arrLensChapters(chapters) {
  return (chapters || []).map((c) => {
    const facts = arrChapterFacts(c);
    const names = facts.povs.map((p) => p.name);
    const authored = clean(c.pov);
    return {
      ...c,
      pov: authored || names[0] || "未定",
      povs: authored ? [authored] : (names.length ? names : ["未定"]),
      time: clean(c.time) || (facts.time.text ? facts.time.text.split(" → ")[0] : ""),
    };
  });
}

/* 全书层面：这是不是一部雪花整理出来的书、POV 怎么分布、还有哪些章没起名 / 哪些场没规划三拍 */
export function arrBookFacts(chapters) {
  const list = chapters || [];
  const planChapters = list.filter(arrIsPlanChapter);
  const scenes = list.flatMap((c) => (c.scenes || []).map((s) => ({ chapter: c, scene: s })));
  return {
    planned: planChapters.length > 0,
    planChapterCount: planChapters.length,
    deskChapterCount: list.length - planChapters.length,
    sceneTotal: scenes.length,
    doneScenes: scenes.filter((x) => x.scene.state === "done").length,
    reactiveScenes: scenes.filter((x) => x.scene.kind === "反应").length,
    povScenes: countBy(scenes.map((x) => clean(x.scene.povName))),
    unnamed: planChapters.filter((c) => c.structure.titleAuto),
    unplanned: scenes.filter((x) => arrIsPlanScene(x.scene) && !arrSceneBeatsPlanned(x.scene)),
    // 这些章级数据产品里没有编辑入口：只有旧数据 / 夹具里才有，有了镜头才出现
    hasTension: list.some((c) => c.tensionSet),
    hasThreads: list.some((c) => (c.threads || []).length > 0),
  };
}

/* 结构镜头：卷 → 章 → 场（故事序）。章的灾难标记落在它的最后一场上（灾难场收束所在的章）。 */
export function arrBookSpine(chapters, numOf) {
  const list = chapters || [];
  return ARR_ACTS.map((act) => ({
    act,
    chapters: list.filter((c) => c.act === act.id).map((c) => {
      const scenes = c.scenes || [];
      return {
        id: c.id,
        num: (numOf && numOf[c.id]) || "",
        title: c.title,
        spine: c.spine || "",
        planOwned: arrIsPlanChapter(c),
        rangeLabel: arrRangeLabel(c.structure),
        current: !!c.current,
        state: c.state,
        scenes: scenes.map((s, index) => ({
          sid: s.sid,
          index,
          no: (s.design && s.design.storyIndex) || 0,
          kind: s.kind,
          state: s.state,
          pov: clean(s.povName),
          title: s.title,
          summary: s.summary || "",
          mark: c.spine && index === scenes.length - 1 ? c.spine : "",
          handMade: !arrIsPlanScene(s),
        })),
      };
    }),
  })).filter((group) => group.chapters.length);
}
