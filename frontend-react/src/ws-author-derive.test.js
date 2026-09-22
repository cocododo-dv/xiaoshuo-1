import { describe, expect, it } from "vitest";
import {
  arrActSpans, arrBookFacts, arrBookSpine, arrChapterChecks, arrChapterEdge, arrChapterFacts, arrChapterStatus,
  arrIsPlanChapter, arrLensChapters, arrRangeLabel, arrSceneBeatsPlanned,
} from "./ws-author-derive.js";
import { arrDeriveIssues } from "./ws-author-doctor.jsx";

/* 阶段 Z：章节编排读的是作者在构思里真的做出来的东西（场上的 POV / 时间 / 地点 / 离场变化、章装着第几到第几场），
   而不是一套没人能填的章级字段。这里全是目录载荷上的纯函数。 */

const scene = (sid, extra = {}) => ({
  sid, title: sid, summary: `${sid} 的整句摘要`, kind: "主动", state: "todo",
  goal: "目标", obstacle: "阻碍", turn: "挫折", povName: "林昭", exitChange: `${sid} 离场变化`,
  design: { owner: "plan", storyIndex: 0, storyTime: "", location: "" },
  ...extra,
});
const chapter = (id, extra = {}) => ({
  id, title: `第 ${id.slice(2)} 章`, act: "act1", state: "planned", current: false, spine: "",
  tension: 0.3, tensionSet: false, pov: "", time: "", place: "", entry: "", exit: "",
  words: { cur: 0, target: 0 }, threads: [], drama: {},
  structure: { owner: "plan", rowUid: `row-${id}`, sceneRange: null, plannedSceneCount: 0, titleAuto: true },
  scenes: [],
  ...extra,
});

describe("章节编排 · 派生层", () => {
  it("一章的入口 / 出口、视角 · 时空从各场读出来；作者填过的章级字段优先", () => {
    const ch = chapter("ch02", {
      structure: { owner: "plan", rowUid: "r2", sceneRange: { first: 6, last: 8 }, plannedSceneCount: 3, titleAuto: true },
      scenes: [
        scene("s6", { povName: "林昭", design: { owner: "plan", storyIndex: 6, storyTime: "第二日·晨", location: "码头" } }),
        scene("s7", { povName: "顾行", design: { owner: "plan", storyIndex: 7, storyTime: "第二日·上午", location: "邮局" } }),
        scene("s8", { povName: "林昭", exitChange: "林昭找到了寄信人", design: { owner: "plan", storyIndex: 8, storyTime: "第二日·中午", location: "码头" } }),
      ],
    });
    const facts = arrChapterFacts(ch);
    expect(facts.rangeLabel).toBe("第 6–8 场");
    expect(facts.entry).toEqual({ text: "s6 的整句摘要", derived: true });
    expect(facts.exit).toEqual({ text: "林昭找到了寄信人", derived: true });
    expect(facts.pov).toEqual({ text: "林昭 2 · 顾行 1", derived: true });
    expect(facts.time).toEqual({ text: "第二日·晨 → 第二日·中午", derived: true });
    expect(facts.place).toEqual({ text: "码头 · 邮局", derived: true });
    expect(facts.beats).toEqual({ planned: 3, total: 3 });

    const authored = arrChapterFacts({ ...ch, pov: "老陈", entry: "雨停了", exit: "（待规划）" });
    expect(authored.pov).toEqual({ text: "老陈", derived: false });
    expect(authored.entry).toEqual({ text: "雨停了", derived: false });
    expect(authored.exit.derived).toBe(true); // 「（待规划）」是占位，不是作者填的
    expect(arrChapterEdge(null, "entry")).toEqual({ text: "", derived: false });
    expect(arrRangeLabel({ sceneRange: { first: 4, last: 4 } })).toBe("第 4 场");
    expect(arrRangeLabel({ sceneRange: null })).toBe("");
  });

  it("系统占位的三拍按没规划算", () => {
    expect(arrSceneBeatsPlanned(scene("a"))).toBe(true);
    expect(arrSceneBeatsPlanned(scene("b", { goal: "（本场目标待规划）" }))).toBe(false);
    expect(arrSceneBeatsPlanned(scene("c", { turn: "—" }))).toBe(false);
    expect(arrSceneBeatsPlanned(scene("d", { obstacle: "" }))).toBe(false);
  });

  it("镜头用的章：章级 POV 没填时取本章场次最多的那一位，泳道看得见本章出现过的全部视角", () => {
    const [lensed, authored, empty] = arrLensChapters([
      chapter("ch01", { scenes: [scene("a", { povName: "老陈" }), scene("b", { povName: "林昭" }), scene("c", { povName: "林昭" })] }),
      chapter("ch02", { pov: "顾行", scenes: [scene("d", { povName: "林昭" })] }),
      chapter("ch03"),
    ]);
    expect(lensed.pov).toBe("林昭");
    expect(lensed.povs).toEqual(["林昭", "老陈"]);
    expect(authored.povs).toEqual(["顾行"]);
    expect(empty.pov).toBe("未定");
  });

  it("全书事实与结构镜头：卷 → 章 → 场，灾难标记落在章的最后一场上，手加的场没有场次", () => {
    const chapters = [
      chapter("ch01", { spine: "灾一", scenes: [scene("s1", { design: { owner: "plan", storyIndex: 1 } }), scene("s2", { state: "done", design: { owner: "plan", storyIndex: 2 } })] }),
      chapter("ch02", {
        act: "act2", title: "旧案重开",
        structure: { owner: "plan", rowUid: "r2", sceneRange: { first: 3, last: 3 }, plannedSceneCount: 1, titleAuto: false },
        scenes: [scene("s3", { kind: "反应", goal: "（本场目标待规划）", design: { owner: "plan", storyIndex: 3 } }), scene("hand", { design: { owner: "desk", storyIndex: 0 } })],
      }),
      chapter("ch03", { act: "act3", structure: { owner: "desk", rowUid: "", sceneRange: null, plannedSceneCount: 0, titleAuto: false }, threads: [{ name: "旧信", role: "新引" }] }),
    ];
    const book = arrBookFacts(chapters);
    expect(book.planned).toBe(true);
    expect([book.planChapterCount, book.deskChapterCount, book.sceneTotal, book.doneScenes, book.reactiveScenes]).toEqual([2, 1, 4, 1, 1]);
    expect(book.unnamed.map((c) => c.id)).toEqual(["ch01"]);
    expect(book.unplanned.map((x) => x.scene.sid)).toEqual(["s3"]); // 手加的场不归构思管
    expect(book).not.toHaveProperty("hasTension");   // 章级张力 / 线索的镜头已经删了，不再有门控
    expect(book).not.toHaveProperty("hasThreads");
    expect(arrIsPlanChapter(chapters[2])).toBe(false);

    const spine = arrBookSpine(chapters, { ch01: "01", ch02: "02", ch03: "03" });
    expect(spine.map((g) => g.act.id)).toEqual(["act1", "act2", "act3"]);
    expect(spine[0].chapters[0].scenes.map((s) => [s.no, s.mark])).toEqual([[1, ""], [2, "灾一"]]);
    expect(spine[1].chapters[0].scenes.map((s) => [s.no, s.handMade])).toEqual([[3, false], [0, true]]);
    expect(spine[1].chapters[0].rangeLabel).toBe("第 3 场");
  });
});

describe("全书体检 · 只报读得出来的事实", () => {
  const numOf = { ch01: "01", ch02: "02" };
  const chapters = [
    chapter("ch01", { words: { cur: 1803, target: 0 }, scenes: [scene("s1"), scene("s2", { povName: "老陈" })] }),
    chapter("ch02", { scenes: [scene("s3", { goal: "" })] }),
  ];

  it("没设字数目标不报超额；视角分布不在体检里重复（结构镜头的摘要条已经画了）", () => {
    const issues = arrDeriveIssues(chapters, numOf);
    const keys = issues.map((x) => x.key);
    expect(keys).not.toContain("fat");       // 真实故障：「字数超额 · 01 第 1 章 · 1,803/0」
    expect(keys).not.toContain("budget");
    expect(keys).not.toContain("pov");
  });

  it("设过目标的章照旧体检字数；章级张力 / 线索即便旧数据里有也不再报（没有编辑入口的东西不当事实）", () => {
    const legacy = [
      chapter("ch01", { words: { cur: 6000, target: 4000 }, tension: 0.8, tensionSet: true, threads: [{ name: "旧信", role: "新引" }] }),
      chapter("ch02", { words: { cur: 100, target: 4000 }, tension: 0.4, tensionSet: true }),
    ];
    const keys = arrDeriveIssues(legacy, numOf).map((x) => x.key);
    expect(keys).toContain("fat");
    expect(keys).not.toContain("dip");
    expect(keys).not.toContain("arc");
    expect(keys).not.toContain("dangling");
    expect(keys).not.toContain("open");
  });

  it("雪花整理出来的书：待同步 / 没起名的章 / 没规划三拍的场各是一项待办，各带一扇门；都清了才说「与构思一致」", () => {
    const book = arrBookFacts(chapters);
    const issues = arrDeriveIssues(chapters, numOf, { book, snow: { pending: 2 } });
    const byKey = Object.fromEntries(issues.map((x) => [x.key, x]));
    expect(byKey["plan-sync"].count).toBe(2);
    expect(byKey["plan-sync"].action.run).toBe("sync");
    expect(byKey["plan-unnamed"].count).toBe(2);
    expect(byKey["plan-unnamed"].action.run).toBe("plan");
    // 章号一律「第 N 章」：没起名的章不再写成「01 第 1 章」（章号挨着它自己的占位名）
    expect(byKey["plan-unnamed"].chips.map((c) => c.text)).toEqual(["第 1 章", "第 2 章"]);
    expect(byKey["plan-unplanned"].chips.map((c) => c.go)).toEqual(["ch02"]);
    expect(byKey.plan).toBeUndefined();

    const tidy = [chapter("ch01", { structure: { owner: "plan", rowUid: "r1", sceneRange: null, plannedSceneCount: 1, titleAuto: false }, scenes: [scene("s1")] })];
    const clean = arrDeriveIssues(tidy, numOf, { book: arrBookFacts(tidy), snow: { pending: 0 } });
    expect(clean.find((x) => x.key === "plan").kind).toBe("ok");
    // 不是雪花的书：这一组一项都不出现
    const manual = [chapter("ch01", { structure: { owner: "desk", rowUid: "", sceneRange: null, plannedSceneCount: 0, titleAuto: false } })];
    expect(arrDeriveIssues(manual, numOf, { book: arrBookFacts(manual), snow: { pending: 0 } }).map((x) => x.key).filter((k) => k.startsWith("plan"))).toEqual([]);
  });
});

describe("章的显示状态、章节体检与卷带", () => {
  it("已批准 / 审阅中来自后端；其余按各场读：有一场动了笔或已经有字 = 写作中，否则规划中", () => {
    expect(arrChapterStatus(chapter("a", { state: "approved" }))).toEqual({ key: "approved", derived: false });
    expect(arrChapterStatus(chapter("b", { state: "review" }))).toEqual({ key: "review", derived: false });
    expect(arrChapterStatus(chapter("c", { state: "planned", scenes: [scene("s1", { state: "done" })] }))).toEqual({ key: "writing", derived: true });
    expect(arrChapterStatus(chapter("d", { state: "planned", words: { cur: 12, target: 0 } })).key).toBe("writing");
    expect(arrChapterStatus(chapter("e", { state: "writing", scenes: [scene("s2")] }))).toEqual({ key: "planned", derived: true });
  });

  it("章节体检：只报读得出来的事实，与构思同步只对有构思分章的书出现", () => {
    const ch = chapter("ch02", {
      drama: { promise: "承诺", spine: "（待补）" },
      words: { cur: 900, target: 0 },
      scenes: [scene("s1", { state: "writing" }), scene("s2", { goal: "" })],
    });
    const rows = arrChapterChecks(ch, { canPlan: true, pending: 2 });
    const byKey = Object.fromEntries(rows.map((row) => [row.key, row]));
    expect(byKey.beats).toMatchObject({ val: "1/2", warn: true, ok: false });
    expect(byKey.started).toMatchObject({ val: "1/2", warn: true });
    expect(byKey.drama.val).toBe("1/6");          // 「（待补）」是占位，不算写过
    expect(byKey.budget).toMatchObject({ val: "未设目标", ok: false, warn: false });
    expect(byKey.sync).toMatchObject({ val: "2 场待同步", warn: true });
    expect(arrChapterChecks(ch, { canPlan: false, pending: 0 }).map((row) => row.key)).not.toContain("sync");
    expect(arrChapterChecks(chapter("x", { words: { cur: 4100, target: 4000 } }), null).find((row) => row.key === "budget").val).toBe("在轨");
  });

  it("全书字数目标只在每一章都设过目标时才算；卷带按章序取首尾", () => {
    expect(arrBookFacts([chapter("a", { words: { cur: 4004, target: 4000 } }), chapter("b", { words: { cur: 0, target: 0 } })]).wordsTarget).toBe(0);
    expect(arrBookFacts([chapter("a", { words: { cur: 100, target: 3000 } }), chapter("b", { words: { cur: 50, target: 2000 } })])).toMatchObject({ words: 150, wordsTarget: 5000 });
    const spans = arrActSpans([chapter("a"), chapter("b"), chapter("c", { act: "act2" }), chapter("d", { act: "act3" })]);
    expect(spans.map((s) => [s.a.id, s.from, s.to])).toEqual([["act1", 0, 1], ["act2", 2, 2], ["act3", 3, 3]]);
  });
});
