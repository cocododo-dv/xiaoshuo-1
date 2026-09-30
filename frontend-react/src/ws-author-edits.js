import { wsConfirm } from "./ws-notify.jsx";
import { arrIsPlanChapter, arrIsPlanScene } from "./ws-author-derive.js";
import { randomSuffix } from "./lib/ids.js";

/* ==========================================================
   章节编排 · 当前章的改动（从 ws-author.jsx 拆出，2026-10）
   加场、改章名、改戏剧卡、改场、切形态、删场、删章——一律经 commitChapters 交给目录（乐观写 + 服务端收敛）。
   已批准终稿的章什么都不改。不是 hook：每次渲染按当前章现拼一份（闭包里读的都是这一次渲染的值）。
   ========================================================== */

/* 雪花的场（design.owner === "plan"）：设计字段不在这里改——行上已经是只读的，写回时再兜一层，
   免得别的入口（快捷键、以后新加的按钮）把一个注定被后端 409 的改动乐观写进本机目录。 */
const ARR_DESIGN_KEYS = ["goal", "obstacle", "turn", "povName", "kind"];

/* 构思分出来的章：删掉的只是目录里的章和场景卡，构思里的分章还在——下一次「确认写入」会把这些场取回。
   作者要的多半是并章 / 拆章（用「整理章节结构」），或者真不要这几场（在构思第 9 步删行）。删之前说清楚。 */
export const planDeleteNote = (targets) => (targets.some(arrIsPlanChapter)
  ? "\n\n其中有构思里分出来的章：这里删掉的只是目录里的章和场景卡，构思的分章还在——下一次「整理章节结构 → 确认写入」会把这些场取回。\n想并章 / 拆章，用「整理章节结构」；真不要这几场，到构思第 9 步删行。"
  : "");

/* ctx：{ ch, commitChapters, chaptersRef, chName, noticeTrashed, pickChapter }
   pickChapter(id) —— 删章之后落到哪一章（邻章），并撤掉场的高亮 */
export function arrChapterEdits({ ch, commitChapters, chaptersRef, chName, noticeTrashed, pickChapter }) {
  const locked = ch.state === "approved";
  const updateCurrent = (fn) => commitChapters((cs) => cs.map((c) => (c.id === ch.id ? fn(c) : c)));
  return {
    locked,
    updateCurrent,
    addScene: () => {
      if (locked) return;
      updateCurrent((c) => ({ ...c, scenes: [...c.scenes, { sid: "s_" + randomSuffix(6), title: "未命名场景", kind: "主动", state: "todo", goal: "", obstacle: "", turn: "" }] }));
    },
    patchTitle: (val) => { if (!locked) updateCurrent((c) => ({ ...c, title: val })); },
    patchDrama: (key, val) => {
      if (locked) return;
      updateCurrent((c) => {
        const nextCh = { ...c, drama: { ...c.drama, [key]: val } };
        if (key === "promise") nextCh.promise = val;
        return nextCh;
      });
    },
    editScene: (i, patch) => {
      if (locked) return;
      const allowed = arrIsPlanScene(ch.scenes[i])
        ? Object.fromEntries(Object.entries(patch || {}).filter(([key]) => !ARR_DESIGN_KEYS.includes(key)))
        : patch;
      if (!allowed || !Object.keys(allowed).length) return;
      updateCurrent((c) => ({ ...c, scenes: c.scenes.map((sc, k) => (k === i ? { ...sc, ...allowed } : sc)) }));
    },
    cycleKind: (i) => {
      if (locked || arrIsPlanScene(ch.scenes[i])) return;
      updateCurrent((c) => ({ ...c, scenes: c.scenes.map((sc, k) => (k === i ? { ...sc, kind: sc.kind === "主动" ? "反应" : "主动" } : sc)) }));
    },
    /* 删完给一条回执（删了几条 / 去了哪 / 怎么找回）——不然作者只能看着东西消失 */
    deleteScene: (i) => {
      if (locked) return;
      const victim = ch.scenes[i];
      updateCurrent((c) => ({ ...c, scenes: c.scenes.filter((_, k) => k !== i) }));
      noticeTrashed(`已把场景「${(victim && victim.title) || "未命名场景"}」移入回收站`);
    },
    deleteChapter: async () => {
      if (locked) return;
      const target = ch;
      const scenes = (target.scenes || []).length;
      const ok = await wsConfirm({
        title: `把${chName(target)}移入回收站？`,
        body: `${scenes ? `连同章下 ${scenes} 个场景。` : "这一章还没有场景。"}可以在回收站里恢复。${planDeleteNote([target])}`,
        confirmLabel: "移入回收站",
        tone: "danger",
      });
      if (!ok) return;
      const cs = chaptersRef.current;
      const i = cs.findIndex((c) => c.id === target.id);
      const neighbor = cs[i + 1] || cs[i - 1];
      commitChapters((all) => all.filter((c) => c.id !== target.id));
      if (neighbor) pickChapter(neighbor.id);
      noticeTrashed(`已把${chName(target, false)}移入回收站${scenes ? `（含 ${scenes} 场）` : ""}`);
    },
  };
}
