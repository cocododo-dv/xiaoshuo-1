import { describe, expect, it } from "vitest";
import {
  arrActSpans, arrBookFacts, arrBookSpine, arrChapterChecks, arrChapterEdge, arrChapterFacts,
  arrIsPlanChapter, arrLensChapters, arrPovCandidates, arrRailTally, arrRangeLabel, arrSceneBeatsPlanned,
} from "./ws-author-derive.js";
import { arrDeriveIssues } from "./ws-author-doctor.jsx";
import { chapterStage, chapterStageDerived, chapterStarted } from "./labels/catalog.js";

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
  it("一章的入口 / 出口、视角 · 时空只从各场读；旧数据里存着的章级值不再盖过场上的事实", () => {
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
    expect(facts.entry).toBe("s6 的整句摘要");
    expect(facts.exit).toBe("林昭找到了寄信人");
    expect(facts.pov).toBe("林昭 2 · 顾行 1");
    expect(facts.time).toBe("第二日·晨 → 第二日·中午");
    expect(facts.place).toBe("码头 · 邮局");
    expect(facts.beats).toEqual({ planned: 3, total: 3 });

    const legacy = arrChapterFacts({ ...ch, pov: "老陈", time: "去年冬天", place: "别处", entry: "雨停了", exit: "天亮了" });
    expect([legacy.pov, legacy.time, legacy.place, legacy.entry, legacy.exit])
      .toEqual([facts.pov, facts.time, facts.place, facts.entry, facts.exit]);
    expect(arrChapterEdge(null, "entry")).toBe("");
    expect(arrChapterEdge(chapter("ch09"), "exit")).toBe("");
    expect(arrRangeLabel({ sceneRange: { first: 4, last: 4 } })).toBe("第 4 场");
    expect(arrRangeLabel({ sceneRange: null })).toBe("");
  });

  it("视角候选：资料库的人物在前，各场用过、资料库里还没建档的视角名跟在后面；去重、不收占位词", () => {
    const entries = [{ cat: "people", name: "林昭" }, { cat: "places", name: "雨城" }, { cat: "people", name: " 顾行 " }];
    const chapters = [chapter("ch01", { scenes: [scene("a", { povName: "顾行" }), scene("b", { povName: "老陈" })] }), chapter("ch02", { scenes: [scene("c", { povName: "待定" })] })];
    expect(arrPovCandidates(entries, chapters)).toEqual(["林昭", "顾行", "老陈"]);
    expect(arrPovCandidates(null, null)).toEqual([]);
  });

  it("系统占位的三拍按没规划算", () => {
    expect(arrSceneBeatsPlanned(scene("a"))).toBe(true);
    expect(arrSceneBeatsPlanned(scene("b", { goal: "（本场目标待规划）" }))).toBe(false);
    expect(arrSceneBeatsPlanned(scene("c", { turn: "—" }))).toBe(false);
    expect(arrSceneBeatsPlanned(scene("d", { obstacle: "" }))).toBe(false);
  });

  it("镜头用的章：主 POV 是本章场次最多的那一位，泳道看得见本章出现过的全部视角；旧的章级 POV / 时间不再算数", () => {
    const [lensed, legacy, empty] = arrLensChapters([
      chapter("ch01", { scenes: [scene("a", { povName: "老陈" }), scene("b", { povName: "林昭" }), scene("c", { povName: "林昭", design: { owner: "plan", storyTime: "第三日" } })] }),
      chapter("ch02", { pov: "顾行", time: "去年", scenes: [scene("d", { povName: "林昭" })] }),
      chapter("ch03"),
    ]);
    expect(lensed.pov).toBe("林昭");
    expect(lensed.povs).toEqual(["林昭", "老陈"]);
    expect(lensed.time).toBe("第三日");
    expect([legacy.pov, legacy.povs, legacy.time]).toEqual(["林昭", ["林昭"], ""]);
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
  it("章的阶段与主页、成稿中心同一条规则：审阅 / 定稿 / 退回小修的草稿照流程；目录说写作中、有字、有一场在写或写完 = 写作中", () => {
    expect(chapterStage(chapter("a", { state: "approved" }))).toBe("approved");
    expect(chapterStage(chapter("b", { state: "review" }))).toBe("review");
    // 成稿中心「退回小修」把章设成 draft：章节编排以前读成写作中 / 规划中
    expect(chapterStage(chapter("c", { state: "draft", words: { cur: 900, target: 0 }, scenes: [scene("s1", { state: "done" })] }))).toBe("draft");
    // 目录说写作中、还没有字：以前章节编排读成规划中，主页 / 成稿中心读成写作中
    expect(chapterStage(chapter("d", { state: "writing", scenes: [scene("s2")] }))).toBe("writing");
    expect(chapterStage(chapter("e", { state: "planned", scenes: [scene("s3", { state: "done" })] }))).toBe("writing");
    expect(chapterStage(chapter("f", { state: "planned", words: { cur: 12, target: 0 } }))).toBe("writing");
    // 有一场在写（还没存下字）也算动笔了：以前主页、成稿中心读成规划中
    expect(chapterStage(chapter("g", { state: "planned", scenes: [scene("s4", { state: "writing" })] }))).toBe("writing");
    expect(chapterStage(chapter("h", { state: "planned", scenes: [scene("s5")] }))).toBe("planned");
    expect(chapterStage(chapter("i", { state: "todo" }))).toBe("todo");
    expect(chapterStage(chapter("j", { state: "something-new" }))).toBe("planned");
    expect([chapter("k", { state: "draft" }), chapter("l", { state: "review" }), chapter("m", { state: "approved" })].map(chapterStageDerived)).toEqual([false, false, false]);
    expect([chapter("n", { state: "writing" }), chapter("o", { state: "planned" })].map(chapterStageDerived)).toEqual([true, true]);
    // 「动笔了没有」那一半单独转出（成稿中心左栏收不收规划中的章该用同一条）：有字、有一场在写 / 写完了 / 记着字数
    expect([
      chapter("p", { scenes: [scene("s6", { state: "writing" })] }),
      chapter("q", { scenes: [scene("s7", { words: 30 })] }),
      chapter("r", { words: { cur: 5, target: 0 } }),
      chapter("s", { scenes: [scene("s8", { state: "archived" })] }),
      chapter("t", { scenes: [scene("s9")] }),
      null,
    ].map(chapterStarted)).toEqual([true, true, true, true, false, false]);
  });

  it("序列栏的数按同一条阶段规则：审阅中 / 草稿有章时单独成一项，不再记在「写作中」名下", () => {
    const tally = arrRailTally([
      chapter("a", { state: "review" }), chapter("b", { state: "review" }), chapter("c", { state: "review" }),
      chapter("d", { state: "draft" }), chapter("e", { state: "planned" }),
    ]);
    expect(tally.map((t) => [t.label, t.n])).toEqual([["已定稿", 0], ["审阅中", 3], ["草稿", 1], ["写作中", 0], ["规划中", 1]]);
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
