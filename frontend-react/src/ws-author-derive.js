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

/* 全书层面：这是不是一部雪花整理出来的书、POV 怎么分布、还有哪些章没起名 / 哪些场没规划三拍、写了多少字 */
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
    words: list.reduce((n, c) => n + ((c.words && c.words.cur) || 0), 0),
    /* 全书字数目标只在每一章都设过目标时才有意义：只设了一章时「4,004 / 4,000 = 100%」是假的 */
    wordsTarget: list.length && list.every((c) => c.words && c.words.target > 0)
      ? list.reduce((n, c) => n + c.words.target, 0) : 0,
  };
}

/* 章的显示状态。已批准 / 审阅中是后端流程给的；其余（后端的「规划中 / 草稿 / 进行中」写作时从不推进）
   按各场读：有一场在写 / 写完、或者已经有字 = 写作中，否则 = 规划中。只用于显示，绝不回写。 */
export function arrChapterStatus(c) {
  const raw = c && c.state;
  if (raw === "approved" || raw === "review") return { key: raw, derived: false };
  const scenes = (c && c.scenes) || [];
  const started = ((c && c.words && c.words.cur) || 0) > 0
    || scenes.some((s) => s.state === "writing" || s.state === "done" || (s.words || 0) > 0);
  return { key: started ? "writing" : "planned", derived: true };
}

/* 章节体检（右栏 + 页头「体检」按钮上的待办数）：只报读得出来的事实。
   snow = { canPlan, pending }：这部作品有没有构思的分章、还有几场改动没同步到场景卡。 */
export function arrChapterChecks(ch, snow) {
  const scenes = (ch && ch.scenes) || [];
  const facts = arrChapterFacts(ch);
  const drama = (ch && ch.drama) || {};
  const dramaDone = DRAMA_KEYS.filter((k) => !isBlank(drama[k])).length;
  const ready = scenes.filter((s) => s.state === "done" || s.state === "writing").length;
  const words = (ch && ch.words) || { cur: 0, target: 0 };
  /* 没设字数目标（雪花整理出来的章都没有）就不谈预算：0 目标下任何字数都会被算成「超额」 */
  const pct = words.target > 0 ? words.cur / words.target : 0;
  const budget = !(words.target > 0) ? { val: "未设目标" }
    : words.cur === 0 ? { val: "未开始", warn: true }
    : pct < 0.85 ? { val: "进行", warn: true }
    : pct <= 1.12 ? { val: "在轨", ok: true }
    : { val: "超额", warn: true };
  const rows = [
    { key: "beats", label: "三拍已规划", val: `${facts.beats.planned}/${facts.beats.total}`,
      ok: facts.beats.total > 0 && facts.beats.planned === facts.beats.total, warn: facts.beats.planned < facts.beats.total || !facts.beats.total },
    { key: "started", label: "场景动笔", val: `${ready}/${scenes.length}`,
      ok: ready === scenes.length && scenes.length > 0, warn: ready < scenes.length },
    { key: "drama", label: "戏剧卡（可选）", val: `${dramaDone}/${DRAMA_KEYS.length}`, ok: dramaDone === DRAMA_KEYS.length },
    { key: "budget", label: "字数预算", val: budget.val, ok: !!budget.ok, warn: !!budget.warn },
  ];
  if (snow && snow.canPlan) {
    rows.push({ key: "sync", label: "与构思同步", val: snow.pending ? `${snow.pending} 场待同步` : "已同步", ok: !snow.pending, warn: !!snow.pending });
  }
  return rows;
}

/* 戏剧卡的六格（护栏「禁止包含 / 备注」不算在内） */
export const DRAMA_KEYS = ["promise", "problem", "spine", "arc", "aftertaste", "ending"];

/* 各卷在章序上占的列（节奏镜头的卷带）。卷在目录里是连续的；不连续时取首尾。 */
export function arrActSpans(chapters) {
  const list = chapters || [];
  return ARR_ACTS.map((a) => {
    const idxs = list.map((c, i) => (c.act === a.id ? i : -1)).filter((i) => i >= 0);
    if (!idxs.length) return null;
    return { a, from: Math.min(...idxs), to: Math.max(...idxs) };
  }).filter(Boolean);
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
