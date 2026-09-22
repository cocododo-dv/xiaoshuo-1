// 主页派生纯函数单测（2026-09-16「流程」并入主页）：
// 进度脊必须只从目录真相派生——每章一段、状态计数、前线唯一且与焦点卡同源、
// 场景计数不再按旧占位符过滤、深链 sid 指向在写的场景。
import { describe, it, expect } from "vitest";
import {
  hmBookProgress, hmChapterWindow, hmCurrentChapter, hmDeriveSpine, hmFocusModel, hmResumeModel, hmSnowLoadState, hmSnowSummary,
} from "./ws-home-derive.js";

const CH = (n, state, extra = {}) => ({ id: `ch${n}`, n: String(n).padStart(2, "0"), title: `第${n}章`, state, scenes: [], ...extra });

describe("hmCurrentChapter", () => {
  it("空目录返回 null", () => {
    expect(hmCurrentChapter([])).toBeNull();
    expect(hmCurrentChapter(null)).toBeNull();
  });

  it("current 标记优先于「在写」，其次「在写」，最后回落到末章", () => {
    const writing = CH(2, "writing");
    const current = CH(3, "draft", { current: true });
    expect(hmCurrentChapter([CH(1, "approved"), writing, current])).toBe(current);
    expect(hmCurrentChapter([CH(1, "approved"), writing, CH(3, "planned")])).toBe(writing);
    const last = CH(3, "planned");
    expect(hmCurrentChapter([CH(1, "approved"), CH(2, "review"), last])).toBe(last);
  });
});

describe("hmDeriveSpine", () => {
  it("图例覆盖后端全部章节状态、顺序固定、0 章的阶段也在，叫法与成稿中心 / 章节编排同一个词（不再有短称）", () => {
    const legend = hmDeriveSpine([CH(1, "review")]).legend;
    expect(legend.map(l => l.state)).toEqual(["approved", "review", "draft", "writing", "planned", "todo"]);
    expect(legend.map(l => l.label)).toEqual(["已定稿", "审阅中", "草稿", "写作中", "规划中", "待写"]);
    expect(legend.map(l => l.n)).toEqual([0, 1, 0, 0, 0, 0]);
  });

  it("空目录：0 章、无分段、计数全零——绝不用目标章数或虚构章填充", () => {
    const spine = hmDeriveSpine([]);
    expect(spine.total).toBe(0);
    expect(spine.segments).toEqual([]);
    expect(Object.values(spine.counts).every(v => v === 0)).toBe(true);
    expect(spine.scenes).toEqual({ total: 0, done: 0, writing: 0, todo: 0, chapters: 0 });
    expect(spine.front).toBeNull();
  });

  it("每章一段：状态计数、未知状态归入 planned、前线只标当前章", () => {
    const chapters = [
      CH(1, "approved"),
      CH(2, "review"),
      CH(3, "writing", { current: true }),
      CH(4, "writing"),
      CH(5, "mystery"),
      CH(6, "todo"),
    ];
    const spine = hmDeriveSpine(chapters);
    expect(spine.total).toBe(6);
    expect(spine.segments.map(s => s.state)).toEqual(["approved", "review", "writing", "writing", "planned", "todo"]);
    expect(spine.counts).toEqual({ approved: 1, review: 1, draft: 0, writing: 2, planned: 1, todo: 1 });
    expect(spine.segments.filter(s => s.front).map(s => s.n)).toEqual(["03"]);
    expect(spine.front).toBe("03");
  });

  it("场景计数不按旧占位符过滤，未知场景状态归入待写；sid 指向在写的场景，否则第一场，没有场景则为空", () => {
    const chapters = [
      CH(1, "approved", { scenes: [{ sid: "ch01s1", state: "done" }, { sid: "ch01s2", state: "done" }] }),
      CH(2, "writing", { current: true, scenes: [{ sid: "ch02s1", state: "done" }, { sid: "ch02s2", state: "writing" }, { sid: "ch02s3", state: "todo" }] }),
      CH(3, "planned", { scenes: [{ sid: "ch03s1", state: "todo", title: "待规划场景", goal: "—" }, { sid: "ch03s2", state: "weird" }] }),
      CH(4, "planned"),
    ];
    const spine = hmDeriveSpine(chapters);
    expect(spine.scenes).toEqual({ total: 7, done: 3, writing: 1, todo: 3, chapters: 3 });
    expect(spine.segments.map(s => s.sid)).toEqual(["ch01s1", "ch02s2", "ch03s1", ""]);
  });
});

describe("hmDeriveSpine / hmChapterWindow · 章的阶段与成稿中心同一套（ws-labels.manuscriptStage）", () => {
  // 目录上还挂着「规划 / 待写」、但已经有字或有写完的场的章，成稿中心读作「写作中」；主页以前照抄目录标签说「规划」
  const book = () => [
    CH(1, "approved", { words: { cur: 0, target: 3000 } }),
    CH(2, "planned", { title: "码头", words: { cur: 1200, target: 3000 } }),
    CH(3, "todo", { scenes: [{ sid: "c3a", state: "done" }, { sid: "c3b", state: "todo" }] }),
    CH(4, "planned", { scenes: [{ sid: "c4a", state: "archived" }] }),
    CH(5, "planned", { current: true, words: { cur: 0, target: 3000 }, scenes: [{ sid: "c5a", state: "todo" }] }),
    CH(6, "todo"),
  ];

  it("已经有字或有写完的场的规划 / 待写章算「在写」；定稿章没字也还是定稿；没动笔的保持原阶段", () => {
    const spine = hmDeriveSpine(book());
    expect(spine.segments.map(s => s.state)).toEqual(["approved", "writing", "writing", "writing", "planned", "todo"]);
    expect(spine.counts).toEqual({ approved: 1, review: 0, draft: 0, writing: 3, planned: 1, todo: 1 });
    expect(spine.legend.find(l => l.state === "writing").n).toBe(3);
  });

  it("章卡的状态标签用同一个阶段：同一个词 + ws-ui 语气", () => {
    const cards = hmChapterWindow(book(), hmDeriveSpine(book())).cards;
    expect(cards.map(c => [Number(c.n), c.state, c.stateLabel, c.tone])).toEqual([
      [2, "writing", "写作中", "accent"],
      [3, "writing", "写作中", "accent"],
      [4, "writing", "写作中", "accent"],
      [5, "planned", "规划中", "neutral"],
      [6, "todo", "待写", "neutral"],
    ]);
  });

  it("分段的悬停说明与成稿中心进度格同一句：第 N 章 · 章名：阶段全称；占位章名不重复，前线另说", () => {
    const segs = hmDeriveSpine(book()).segments;
    expect(segs[1].hint).toBe("第 2 章 · 码头：写作中");
    expect(segs[0].hint).toBe("第 1 章：已定稿");
    expect(segs[4].hint).toBe("第 5 章：规划中，前线");
  });
});

describe("hmDeriveSpine · 前线跟焦点场景所在章", () => {
  it("传入 frontChapter 时前线就是它（按 id 认，不要求同一个对象）", () => {
    const chapters = [CH(1, "approved"), CH(2, "writing", { current: true }), CH(3, "planned")];
    const spine = hmDeriveSpine(chapters, { id: "ch3" });
    expect(spine.segments.filter(s => s.front).map(s => s.n)).toEqual(["03"]);
    expect(spine.front).toBe("03");
    expect(hmDeriveSpine(chapters, { id: "ghost" }).front).toBeNull();
  });
});

describe("hmFocusModel", () => {
  it("slug 只放第几章第几场，形态另起标签；反应场景用 kindFields 当三拍标签", () => {
    const chapter = CH(4, "writing");
    const scene = { sid: "x2", title: "决定回去", kind: "反应", kindFields: ["反应", "两难", "决定"], goal: "躲起来", obstacle: "", turn: "回去" };
    const m = hmFocusModel({ chapter, scene, index: 1 }, {});
    expect(m.slug).toBe("第 4 章 · 第 2 场");
    expect(m.kind).toBe("反应场景");
    expect(m.sid).toBe("x2");
    expect(m.beats.map(b => [b.k, b.v])).toEqual([["反应", "躲起来"], ["两难", "（两难待规划）"], ["决定", "回去"]]);
  });

  it("没有焦点场景时退回 dashboard 缓存，slug 截到两项", () => {
    const m = hmFocusModel(null, { slug: "第 2 章 · 第 3 场 · 主动场景", scene: "旧场景", gos: [{ k: "目标", tone: "sage", v: "g" }] });
    expect(m).toMatchObject({ slug: "第 2 章 · 第 3 场", title: "旧场景", sid: "", scene: null });
    expect(m.beats).toHaveLength(1);
  });
});

describe("hmChapterWindow", () => {
  const book = (n, front) => Array.from({ length: n }, (_, i) => CH(i + 1, "planned", i + 1 === front ? { current: true } : {}));

  it("前一章到后三章；靠近书尾往前补足五章；不足五章全列", () => {
    const at = (n, front) => hmChapterWindow(book(n, front), hmDeriveSpine(book(n, front))).cards.map(c => Number(c.n));
    expect(at(20, 8)).toEqual([7, 8, 9, 10, 11]);
    expect(at(20, 1)).toEqual([1, 2, 3, 4, 5]);
    expect(at(20, 20)).toEqual([16, 17, 18, 19, 20]);
    expect(at(3, 2)).toEqual([1, 2, 3]);
    expect(hmChapterWindow(book(3, 2), hmDeriveSpine(book(3, 2))).partial).toBe(false);
    expect(hmChapterWindow(book(9, 2), hmDeriveSpine(book(9, 2))).partial).toBe(true);
  });

  it("章卡带和进度脊一样的深链 sid 与前线标记", () => {
    const chapters = [CH(1, "approved", { scenes: [{ sid: "a", state: "done" }] }), CH(2, "writing", { current: true, scenes: [{ sid: "b1", state: "done" }, { sid: "b2", state: "todo" }] })];
    const cards = hmChapterWindow(chapters, hmDeriveSpine(chapters)).cards;
    expect(cards.map(c => [c.sid, c.front])).toEqual([["a", false], ["b2", true]]);
  });

  it("章卡的章号是「第 N 章」；占位章名不作为章名（卡上不会出现「第 1 章」上面再压一个「第 1 章」）", () => {
    const chapters = [CH(1, "approved"), CH(2, "writing", { title: "码头", current: true }), CH(3, "planned", { title: "未命名章节" })];
    const cards = hmChapterWindow(chapters, hmDeriveSpine(chapters)).cards;
    expect(cards.map(c => [c.num, c.title])).toEqual([["第 1 章", ""], ["第 2 章", "码头"], ["第 3 章", ""]]);
  });
});

describe("hmSnowSummary", () => {
  const KEYS = ["book_brief", "one_sentence_summary", "one_paragraph_summary", "character_sheets", "short_synopsis",
    "character_synopses", "long_synopsis", "character_bibles", "scene_list", "scene_details"];

  it("读 dashboard 的十步：当前步给出名字与序号，下一步回到那一步（前端步骤键）", () => {
    const snow = KEYS.map((key, i) => ({ key, name: `短名${i + 1}`, s: i < 8 ? "done" : (i === 8 ? "active" : "todo") }));
    const sum = hmSnowSummary(snow);
    expect(sum).toMatchObject({ total: 10, done: 8, allDone: false, now: "短名9 · 第 9 步" });
    expect(sum.next).toEqual({ action: "step", key: "scenes", label: "继续构思" });
  });

  it("没有 active 时取第一步没确认的；十步都确认了下一步是去写", () => {
    const partial = KEYS.map((key, i) => ({ key, name: `n${i}`, s: i === 2 ? "warn" : "done" }));
    expect(hmSnowSummary(partial).next.key).toBe("paragraph");
    const all = KEYS.map((key) => ({ key, name: key, s: "done" }));
    expect(hmSnowSummary(all)).toMatchObject({ allDone: true, now: "十步已全部确认", next: { action: "write" } });
  });

  it("还没读到 dashboard 时是空的，不编造进度", () => {
    expect(hmSnowSummary(undefined)).toMatchObject({ total: 0, done: 0, allDone: false, now: "", next: null });
  });
});

describe("hmSnowLoadState", () => {
  const st = (dashboard, projects = "ready") => ({ projects: { phase: projects, error: null }, dashboard });

  it("在读才说在读；读失败说读不到；读到了但没有步骤说没有", () => {
    expect(hmSnowLoadState(st({ phase: "loading", error: null }))).toBe("loading");
    expect(hmSnowLoadState(st({ phase: "error", error: { message: "断了" } }))).toBe("error");
    expect(hmSnowLoadState(st({ phase: "ready", error: null }))).toBe("empty");
    // 重试中：旧错误还挂着，但此刻是在读
    expect(hmSnowLoadState(st({ phase: "loading", error: { message: "断了" } }))).toBe("loading");
  });

  it("还没开始拉（idle）：列表在读时算在读，列表读失败就算读不到（不会再有人去拉）", () => {
    expect(hmSnowLoadState(st({ phase: "idle", error: null }, "loading"))).toBe("loading");
    expect(hmSnowLoadState(st({ phase: "idle", error: null }, "error"))).toBe("error");
    expect(hmSnowLoadState(undefined)).toBe("loading");
  });
});

describe("hmBookProgress", () => {
  it("动笔章数 / 目录章数与字数百分比", () => {
    expect(hmBookProgress({ words: 25000, written: 3, planned: 12 }, { wordsTarget: 100000 }))
      .toEqual({ written: 3, planned: 12, pct: 25, wordsWan: "2.5", targetWan: "10" });
  });
});

describe("hmResumeModel（「上次写到这里」）", () => {
  const P = "在这里开始写这一场……";

  it("本机缓存有字就用本机的末两段，不配服务端草稿的「暂停于」", () => {
    expect(hmResumeModel({ lines: ["一。", "二。", "三。"], placeholderOnly: false }, { lines: ["旧。"], pausedAgo: "昨天", sceneWords: 2 }, 6))
      .toEqual({ lines: ["二。", "三。"], pausedAgo: "", words: 6 });
  });

  it("本机缓存没字就看服务端的；服务端有句子才写「暂停于」", () => {
    expect(hmResumeModel(null, { lines: ["门开了。"], pausedAgo: "昨天", sceneWords: 4 }, undefined))
      .toEqual({ lines: ["门开了。"], pausedAgo: "昨天", words: 4 });
    expect(hmResumeModel({ lines: [], placeholderOnly: false }, { lines: [], pausedAgo: "今天", sceneWords: 0 }, 0))
      .toEqual({ lines: [], pausedAgo: "", words: 0 });
  });

  it("服务端只给一行时那一行就是整份草稿：开头的旧占位去掉，去完没字就是没有正文", () => {
    expect(hmResumeModel(null, { lines: [P], pausedAgo: "昨天", sceneWords: 11 }, 11))
      .toEqual({ lines: [], pausedAgo: "", words: 0 });
    expect(hmResumeModel(null, { lines: [`${P}潮水涨上来了。`], pausedAgo: "昨天", sceneWords: 18 }, 18))
      .toEqual({ lines: ["潮水涨上来了。"], pausedAgo: "昨天", words: 18 });
    // 正文里恰好写到这句话（不在开头）：是作者的字
    expect(hmResumeModel(null, { lines: [`纸条上写着：${P}`], pausedAgo: "昨天", sceneWords: 17 }, 17).lines)
      .toEqual([`纸条上写着：${P}`]);
  });

  it("两行时只有字数对得上（这两行就是整份草稿）才认第一行是开头", () => {
    expect(hmResumeModel(null, { lines: [P, "她推开门。"], pausedAgo: "昨天", sceneWords: 16 }, 16).lines).toEqual(["她推开门。"]);
    expect(hmResumeModel(null, { lines: [P, "她推开门。"], pausedAgo: "昨天", sceneWords: 900 }, 900).lines).toEqual([P, "她推开门。"]);
  });

  it("本机缓存只剩旧占位、服务端也没有别的字：字数正好是占位自己的字数时显示 0，别的数字不动", () => {
    expect(hmResumeModel({ lines: [], placeholderOnly: true }, {}, 11)).toEqual({ lines: [], pausedAgo: "", words: 0 });
    // 字数对不上（目录的数字来自更新的一次保存）：不猜
    expect(hmResumeModel({ lines: [], placeholderOnly: true }, {}, 40).words).toBe(40);
    // 不是只有占位的空稿：字数照实
    expect(hmResumeModel({ lines: [], placeholderOnly: false }, {}, 11).words).toBe(11);
  });

  it("本机缓存只剩旧占位，但服务端有别的设备写下的字：显示服务端的", () => {
    expect(hmResumeModel({ lines: [], placeholderOnly: true }, { lines: ["潮水退了。"], pausedAgo: "昨天", sceneWords: 5 }, 5))
      .toEqual({ lines: ["潮水退了。"], pausedAgo: "昨天", words: 5 });
  });
});
