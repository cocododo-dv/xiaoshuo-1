// 主页视图渲染测（2026-09-16「流程」并入主页）：
// 断「可观测结果」——进度脊按目录真相逐章渲染、前线与焦点卡同源、图例与场景计数落到 DOM、
// 分段点击带 ws:writer-scene 深链进写作房间、反应场景的三拍标签跟着 kindFields 走。
// 四个 store 全部 mock：主页只读它们的同步缓存，不该有任何网络依赖。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

const fx = vi.hoisted(() => ({
  chapters: [],
  aiReady: true,
  catalogReady: true,
  status: null,
  work: {
    id: "prj-main", title: "北岸手记", sub: "一句话简介", mark: "北", accent: "slate", genre: "",
    wordsTotal: 12000, wordsTarget: 100000, chaptersWritten: 2, chaptersTotal: 4,
    wordsToday: 800, wordsTargetDay: 1000, streak: 3, home: {},
  },
}));

/* 诊断计数 store（写作台深改面板里还开着的发现数）：主页只读它的同步缓存 */
const diagFx = vi.hoisted(() => ({ byChapter: {}, totals: null }));
vi.mock("./ws-diagnosis-summary.jsx", () => ({
  useDiagnosisSummary: () => ({
    loaded: () => diagFx.totals != null,
    totals: () => diagFx.totals,
    chapterCounts: (id) => (diagFx.byChapter[id] ? { open: 0, blocking: 0, ...diagFx.byChapter[id] } : (diagFx.totals != null ? { open: 0, blocking: 0 } : null)),
    sceneCounts: () => null,
  }),
}));
vi.mock("./ws-works.jsx", () => ({
  WsWorks: { retry: vi.fn(() => Promise.resolve()) },
  useActiveWork: () => fx.work,
  useWorksStatus: () => fx.status,
  wsKey: (k) => k,
}));
// focusScene 用目录层真正的那一条规则（ws-catalog-focus.js，纯函数）：主页不自己判断「现在该写哪一场」，只认目录 store 给的答案
vi.mock("./ws-catalog.jsx", async () => {
  const { catalogFocusScene } = await import("./ws-catalog-focus.js");
  return {
    WsCatalog: {
      totals: () => ({ words: 12000, written: 2, planned: fx.chapters.length }),
      ready: () => fx.catalogReady,
      loadError: () => null,
      focusScene: () => catalogFocusScene(fx.chapters),
      reset: vi.fn(),
    },
    useCatalogChapters: () => fx.chapters,
  };
});
vi.mock("./ws-ai-providers.jsx", () => ({
  WsAiProviders: { refresh: vi.fn(() => Promise.resolve()) },
  useAiProviders: () => ({
    loaded: true, loading: false, error: null,
    overview: { readiness: { ready: fx.aiReady } },
  }),
}));

const SC = (sid, state, extra = {}) => ({ sid, title: sid, kind: "主动", state, goal: "", obstacle: "", turn: "", ...extra });
const CH = (n, state, scenes = [], extra = {}) => ({
  id: `ch${n}`, n: String(n).padStart(2, "0"), title: `第${n}章`, state, scenes,
  words: { cur: 0, target: 3000 }, ...extra,
});

function catalogFixture() {
  return [
    CH(1, "approved", [SC("ch01s1", "done"), SC("ch01s2", "done")], { words: { cur: 3000, target: 3000 } }),
    CH(2, "review", [SC("ch02s1", "done")]),
    CH(3, "writing", [
      SC("ch03s1", "done"),
      SC("ch03s2", "writing", { kind: "反应", kindFields: ["反应", "两难", "决定"], goal: "先躲起来", obstacle: "", turn: "决定回去" }),
      SC("ch03s3", "todo"),
    ], { current: true }),
    CH(4, "planned"),
  ];
}

let container;
let root;

async function mount(go, props = {}) {
  const { WsHome } = await import("./ws-home.jsx");
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => { root.render(<WsHome go={go} {...props} />); });
}

const click = (el) => act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); });

beforeEach(() => {
  vi.resetModules();
  fx.chapters = catalogFixture();
  fx.aiReady = true;
  fx.catalogReady = true;
  fx.status = { projects: { phase: "ready", error: null }, dashboard: { phase: "ready", error: null } };
  fx.work = { ...fx.work, home: {} };
});

afterEach(async () => {
  if (root) await act(async () => { root.unmount(); });
  if (container) container.remove();
  root = null;
  container = null;
});

describe("WsHome · 全书进度脊（并入的「流程」内容）", () => {
  it("逐章渲染分段、状态类名与唯一前线，前线与焦点卡指向同一章", async () => {
    await mount(vi.fn());
    const segs = container.querySelectorAll(".hm-spine-bar .hm-seg");
    expect(segs.length).toBe(4);
    expect([...segs].map(s => [...s.classList].find(c => c.startsWith("s-")))).toEqual(["s-approved", "s-review", "s-writing", "s-planned"]);
    const fronts = container.querySelectorAll(".hm-seg.is-front");
    expect(fronts.length).toBe(1);
    expect(fronts[0].getAttribute("data-testid")).toBe("home-spine-ch-03");
    expect(container.querySelector(".hm-seg-flag").textContent).toBe("前线");
    // 焦点卡同源：hero 的章号也是第 3 章
    expect(container.querySelector(".hm-slug").textContent).toContain("第 3 章");
    expect(container.querySelector(".hm-chaps-title").textContent).toBe("全书 4 章");
    // 页头只有书名与简介：按时段换的问候语（「晚上好 · …」）已删
    expect(container.querySelector(".hm-greet")).toBeNull();
    expect(container.querySelector(".hm-top").textContent).not.toContain("继续写作");
  });

  it("图例章数与场景计数来自目录真相", async () => {
    await mount(vi.fn());
    const leg = (k) => container.querySelector(`.hm-leg.st-${k} b`).textContent;
    expect([leg("approved"), leg("review"), leg("draft"), leg("writing"), leg("planned"), leg("todo")]).toEqual(["1", "1", "0", "1", "1", "0"]);
    // 各自带标签的几个数，不再用「·」串成一串
    expect([...container.querySelectorAll(".hm-spine-scenes > span")].map(n => n.textContent.replace(/\s+/g, "")))
      .toEqual(["已规划6场", "已完成4", "写作中1", "待写1"]);
  });

  it("章的阶段与成稿中心同一套：目录上还挂着「规划」但已经有字的章，图例、分段与章卡都读作写作中", async () => {
    // 以前主页照抄目录标签：第 4 章写了一千二百字，成稿中心说「写作中」，主页还说「规划」
    fx.chapters = [...catalogFixture().slice(0, 3), CH(4, "planned", [], { title: "灯塔", words: { cur: 1200, target: 3000 } })];
    await mount(vi.fn());
    const leg = (k) => container.querySelector(`.hm-leg.st-${k} b`).textContent;
    expect([leg("writing"), leg("planned")]).toEqual(["2", "0"]);
    const seg = container.querySelector('[data-testid="home-spine-ch-04"]');
    expect(seg.classList.contains("s-writing")).toBe(true);
    expect(seg.getAttribute("aria-label")).toBe("第 4 章 · 灯塔：写作中");
    const card = [...container.querySelectorAll(".hm-chap")].find(b => b.textContent.includes("灯塔"));
    expect(card.classList.contains("s-writing")).toBe(true);
    // 有真章名的章：章号作小字放在名字上方
    expect([card.querySelector(".hm-chap-n").textContent, card.querySelector(".hm-chap-t").textContent]).toEqual(["第 4 章", "灯塔"]);
    expect(card.querySelector(".hm-chap-top .ws-tag").textContent).toBe("写作中");
    expect(card.querySelector(".hm-chap-top .ws-tag").getAttribute("data-tone")).toBe("accent");
  });

  it("空场景目录不显示计数句，而是明确说还没有规划场景", async () => {
    fx.chapters = [CH(1, "planned"), CH(2, "planned")];
    await mount(vi.fn());
    expect(container.querySelector(".hm-spine-scenes").textContent).toBe("还没有规划场景");
    expect(container.querySelectorAll(".hm-seg").length).toBe(2);
  });

  it("点分段带 ws:writer-scene 深链进写作房间；没有场景的章只切视图", async () => {
    const go = vi.fn();
    await mount(go);
    await act(async () => {
      container.querySelector('[data-testid="home-spine-ch-03"]').dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(go).toHaveBeenLastCalledWith("writer", { type: "ws:writer-scene", detail: "ch03s2" });
    await act(async () => {
      container.querySelector('[data-testid="home-spine-ch-04"]').dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(go).toHaveBeenLastCalledWith("writer");
    expect(go.mock.calls[go.mock.calls.length - 1].length).toBe(1);
  });

  it("阶段 X：焦点卡与「进入写作房间」指着同一场——当前章里第一场没写完的（雪花刚整理完：没有任何一场标着在写）", async () => {
    // 真实故障的形状：雪花整理出来的当前章一场都没开始写；前面有一章手建的、里面一场标着「在写」
    fx.chapters = [
      CH(1, "writing", [SC("hand1", "writing")]),
      CH(2, "planned", [SC("snow1", "done"), SC("snow2", "todo", { title: "翻出案卷" }), SC("snow3", "todo")], { current: true }),
    ];
    const go = vi.fn();
    await mount(go);
    expect(container.querySelector(".hm-slug").textContent).toContain("第 2 章 · 第 2 场");
    expect(container.querySelector(".hm-scene").textContent).toBe("翻出案卷");
    await act(async () => {
      container.querySelector('[data-testid="home-enter-writer"]').dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(go).toHaveBeenLastCalledWith("writer", { type: "ws:writer-scene", detail: "snow2" });
  });

  it("反应场景的三拍标签跟着目录的 kindFields 走，缺项占位也用对应标签", async () => {
    await mount(vi.fn());
    const rows = [...container.querySelectorAll(".hm-gos-row")].map(r => [
      r.querySelector(".hm-gos-k").textContent, r.querySelector(".hm-gos-v").textContent,
    ]);
    expect(rows).toEqual([["反应", "先躲起来"], ["两难", "（两难待规划）"], ["决定", "决定回去"]]);
  });

  it("主动场景用全站同一套三拍名：目标 / 冲突 / 挫败", async () => {
    fx.chapters = [CH(1, "writing", [SC("ch01s1", "writing", { goal: "拿到钥匙" })], { current: true })];
    await mount(vi.fn());
    const keys = [...container.querySelectorAll(".hm-gos-k")].map(el => el.textContent);
    expect(keys).toEqual(["目标", "冲突", "挫败"]);
    expect(container.querySelector(".hm-gos-v").textContent).toBe("拿到钥匙");
  });
});

describe("WsHome · 等你处理（从目录现算，取代待办收件箱）", () => {
  const card = () => container.querySelector('[data-testid="home-attention"]');
  const rows = () => [...card().querySelectorAll(".home-todo")].map(row => row.textContent);

  it("列出写齐了等你批准的章与被管线停下的场（章在前）；点章去成稿中心那一章，点场去 AI 起草台那一场", async () => {
    fx.chapters = [
      ...catalogFixture().slice(0, 2),
      CH(3, "writing", [
        SC("ch03s1", "done"),
        SC("ch03s2", "writing", { title: "林昭翻案卷", work: { runStatus: "human_review_required" } }),
        SC("ch03s3", "todo", { title: "旧信", work: { runStatus: "awaiting_candidate_selection" } }),
      ], { current: true }),
    ];
    const go = vi.fn();
    await mount(go);
    expect(rows()).toEqual([
      "等你批准第 2 章",
      "等你审阅第 3 章 · 第 2 场「林昭翻案卷」",
      "等你终选第 3 章 · 第 3 场「旧信」",
    ]);
    expect(card().textContent).not.toContain("没有等你处理的事");
    // 没有「标记已处理」：源头处理完自己消失
    expect(card().querySelectorAll("button").length).toBe(3);

    const [chapterRow, sceneRow] = card().querySelectorAll(".home-todo-open");
    await click(chapterRow);
    expect(go).toHaveBeenLastCalledWith("manuscripts", { type: "ws:manu-chapter", detail: "ch2" });
    await click(sceneRow);
    expect(go).toHaveBeenLastCalledWith("scene", { type: "ws:scene-enqueue", detail: { sid: "ch03s2" } });
  });

  it("作者在写作台采纳了的场（目录标了完成）、管线还在跑的步位、已批准的章都不列；什么都没有时说清它管什么", async () => {
    fx.chapters = [
      CH(1, "approved", [SC("ch01s1", "done", { work: { runStatus: "archived", hasFinal: true } })]),
      CH(2, "writing", [
        SC("ch02s1", "done", { work: { runStatus: "human_review_required" } }),
        SC("ch02s2", "writing", { work: { runStatus: "near_final" } }),
        SC("ch02s3", "todo", { work: { runStatus: "ready" } }),
      ], { current: true }),
    ];
    await mount(vi.fn());
    expect(card().querySelectorAll(".home-todo").length).toBe(0);
    expect(card().textContent).toContain("现在没有等你处理的事");
    expect(card().textContent).toContain("等你批准");
  });

  it("各场都有终稿、还没批准的章读作等你批准（与成稿中心同一条规则，不看目录里存的状态）", async () => {
    fx.chapters = [
      CH(1, "draft", [
        SC("ch01s1", "done", { work: { runStatus: "archived", hasFinal: true } }),
        SC("ch01s2", "done", { work: { runStatus: "archived", hasFinal: true } }),
      ], { current: true }),
      CH(2, "planned", [SC("ch02s1", "todo")]),
    ];
    await mount(vi.fn());
    expect(rows()).toEqual(["等你批准第 1 章"]);
  });

  it("超过四件只列四行，脚注说剩下的在哪一页、有几件", async () => {
    const stopped = (n) => SC(`ch03s${n}`, "todo", { title: `第${n}场`, work: { runStatus: "hard_qc_full_rewrite_required" } });
    fx.chapters = [
      ...catalogFixture().slice(0, 2),
      CH(3, "writing", [1, 2, 3, 4, 5].map(stopped), { current: true }),
    ];
    await mount(vi.fn());
    expect(card().querySelectorAll(".home-todo").length).toBe(4);
    expect(card().querySelector(".home-todo-foot").textContent).toBe("还有 2 项没列出：AI 起草台 2 场。");
  });
});

describe("WsHome · 焦点卡的版面稳定", () => {
  it("场景还没有正文时不显示「暂停于」，也不再重复章号标签", async () => {
    fx.work = { ...fx.work, home: { resume: { lines: [], pausedAgo: "今天", sceneWords: 0 } } };
    await mount(vi.fn());
    expect(container.querySelector(".hm-resume").textContent).toContain("这一场还没有正文");
    expect(container.querySelector(".hm-resume-ago")).toBeNull();
    expect(container.querySelector(".hm-resume-tab")).toBeNull();
  });

  it("当前章写完、焦点挪到下一章时，不拿服务端那几行（上一场的句子）顶在新焦点场下面", async () => {
    fx.chapters = [
      CH(1, "review", [SC("ch01s1", "done", { backendId: "s1" }), SC("ch01s2", "done", { backendId: "s2" })], { current: true }),
      CH(2, "planned", [SC("ch02s1", "todo", { backendId: "s3" })]),
    ];
    fx.work = { ...fx.work, home: { resume: { sceneId: "s2", lines: ["上一场的结尾。"], pausedAgo: "昨天", sceneWords: 7 } } };
    await mount(vi.fn());
    expect(container.querySelector(".hm-resume").textContent).not.toContain("上一场的结尾");
    expect(container.querySelector(".hm-resume").textContent).toContain("这一场还没有正文");
  });

  it("有上次写下的句子时才显示「暂停于」", async () => {
    fx.work = { ...fx.work, home: { resume: { lines: ["她把门推开一条缝。"], pausedAgo: "昨天", sceneWords: 12 } } };
    await mount(vi.fn());
    expect(container.querySelector(".hm-resume-ago").textContent).toContain("昨天");
  });

  it("写作台缓存的正文按惰性文档解析，取末两段", async () => {
    localStorage.setItem("wr-doc:ch03s2", "<p>第一段。</p><p>第二段。</p><p>第三段<img src=x onerror=\"window.__hmXss=1\"></p>");
    await mount(vi.fn());
    const lines = [...container.querySelectorAll(".hm-resume-body p")].map(p => p.textContent);
    expect(lines).toEqual(["第二段。", "第三段"]);
    expect(window.__hmXss).toBeUndefined();
  });

  it("旧草稿开头的空白页占位不当成「上次写到这里」；只去开头那一句，后文里的同一句话照常显示", async () => {
    const resumeLines = () => [...container.querySelectorAll(".hm-resume-body p")].map(p => p.textContent);
    localStorage.setItem("wr-doc:ch03s2", "<p>在这里开始写这一场……</p>");
    await mount(vi.fn());
    expect(container.querySelector(".hm-resume").textContent).toContain("这一场还没有正文");
    await act(async () => { root.unmount(); });
    container.remove();

    // 旧实现只在「整份草稿就这一段」时才跳过，这里会把占位句当成倒数第二段显示出来
    localStorage.setItem("wr-doc:ch03s2", "<p>在这里开始写这一场……</p><p>她推开门。</p>");
    await mount(vi.fn());
    expect(resumeLines()).toEqual(["她推开门。"]);
    await act(async () => { root.unmount(); });
    container.remove();

    // 作者接着占位写在同一段里：旧实现把这一段整段当成占位，说「还没有正文」
    localStorage.setItem("wr-doc:ch03s2", "<p>在这里开始写这一场……潮水涨上来了。</p>");
    await mount(vi.fn());
    expect(resumeLines()).toEqual(["潮水涨上来了。"]);
    await act(async () => { root.unmount(); });
    container.remove();

    localStorage.setItem("wr-doc:ch03s2", "<p>门开了。</p><p>纸条上写着：在这里开始写这一场……</p>");
    await mount(vi.fn());
    expect(resumeLines()).toEqual(["门开了。", "纸条上写着：在这里开始写这一场……"]);
  });

  describe("旧占位也不能从服务端 dashboard 那条兜底路溜回来", () => {
    // 后端从草稿纯文本切 last_lines，不认识这句占位；以前本机缓存去完占位没字了，主页就改用 dashboard 的行，
    // 把同一句占位连同「暂停于」又印了出来
    const P = "在这里开始写这一场……";
    const resumeLines = () => [...container.querySelectorAll(".hm-resume-body p")].map(p => p.textContent);
    const words = () => container.querySelector(".hm-resume-words").textContent;

    it("本机缓存只有占位、dashboard 也只有这一行：说还没有正文，不写「暂停于」，字数不按占位算", async () => {
      localStorage.setItem("wr-doc:ch03s2", `<p>${P}</p>`);
      fx.chapters = catalogFixture();
      fx.chapters[2].scenes[1].words = 11;
      fx.work = { ...fx.work, home: { resume: { lines: [P], pausedAgo: "昨天", sceneWords: 11 } } };
      await mount(vi.fn());
      expect(resumeLines().join("")).toContain("这一场还没有正文");
      expect(resumeLines().join("")).not.toContain(P);
      expect(container.querySelector(".hm-resume-ago")).toBeNull();
      expect(words()).toBe("本场 0 字");
    });

    it("没有本机缓存、dashboard 只有占位这一行：同样说还没有正文", async () => {
      fx.work = { ...fx.work, home: { resume: { lines: [P], pausedAgo: "昨天", sceneWords: 11 } } };
      await mount(vi.fn());
      expect(resumeLines().join("")).toContain("这一场还没有正文");
      expect(container.querySelector(".hm-resume-ago")).toBeNull();
    });

    it("dashboard 的两行就是整份草稿（字数对得上）时，开头那行占位也去掉；对不上就看不出是不是开头，原样显示", async () => {
      fx.work = { ...fx.work, home: { resume: { lines: [P, "她推开门。"], pausedAgo: "昨天", sceneWords: 16 } } };
      await mount(vi.fn());
      expect(resumeLines()).toEqual(["她推开门。"]);
      expect(container.querySelector(".hm-resume-ago").textContent).toContain("昨天");
      await act(async () => { root.unmount(); });
      container.remove();

      // 草稿更长：这两行只是末尾，倒数第二行恰好是这句话——那是作者的字
      fx.work = { ...fx.work, home: { resume: { lines: [P, "她推开门。"], pausedAgo: "昨天", sceneWords: 400 } } };
      await mount(vi.fn());
      expect(resumeLines()).toEqual([P, "她推开门。"]);
    });

    it("本机缓存只剩占位但服务端有字（别的设备后来写的）：显示服务端的句子和「暂停于」", async () => {
      localStorage.setItem("wr-doc:ch03s2", `<p>${P}</p>`);
      fx.work = { ...fx.work, home: { resume: { lines: ["潮水退了。"], pausedAgo: "昨天", sceneWords: 5 } } };
      await mount(vi.fn());
      expect(resumeLines()).toEqual(["潮水退了。"]);
      expect(container.querySelector(".hm-resume-ago").textContent).toContain("昨天");
    });

    it("本机缓存有字时不写服务端草稿的「暂停于」", async () => {
      localStorage.setItem("wr-doc:ch03s2", "<p>她推开门。</p>");
      fx.work = { ...fx.work, home: { resume: { lines: ["旧的一句。"], pausedAgo: "昨天", sceneWords: 5 } } };
      await mount(vi.fn());
      expect(resumeLines()).toEqual(["她推开门。"]);
      expect(container.querySelector(".hm-resume-ago")).toBeNull();
    });
  });

  it("AI 未就绪的提示放进焦点卡的按钮行，不在页顶插一条把整页往下推", async () => {
    fx.aiReady = false;
    await mount(vi.fn());
    expect(container.querySelector(".hm-hero-actions .hm-ai-flag")).not.toBeNull();
    expect(container.querySelector(".hm-hero-actions .hm-ai-flag").textContent).toContain("AI 尚未就绪");
    expect(container.querySelector(".hm-data-notice")).toBeNull();
  });
});

describe("WsHome · 单一真相（阶段 2 重构）", () => {
  it("当前章没铺场时，焦点卡、前线与「进入写作房间」跟着目录的 focusScene 走到有场的那一章", async () => {
    // 过去主页自己按「当前章」判断：当前章没有场景时焦点卡只剩「—」，进入写作房间也不带场景
    fx.chapters = [
      CH(1, "planned"),
      CH(2, "planned", [], { current: true }),
      CH(3, "planned", [SC("c1", "todo", { title: "码头的雾" })]),
    ];
    const go = vi.fn();
    await mount(go);
    expect(container.querySelector(".hm-scene").textContent).toBe("码头的雾");
    expect(container.querySelector(".hm-slug").textContent).toBe("第 3 章 · 第 1 场");
    expect(container.querySelector(".hm-seg.is-front").getAttribute("data-testid")).toBe("home-spine-ch-03");
    await click(container.querySelector('[data-testid="home-enter-writer"]'));
    expect(go).toHaveBeenLastCalledWith("writer", { type: "ws:writer-scene", detail: "c1" });
  });

  it("章卡是前线章附近的一段（前一章到后三章），不是书尾五章；点章卡带深链", async () => {
    fx.chapters = Array.from({ length: 12 }, (_, i) => {
      const n = i + 1;
      return CH(n, n < 6 ? "approved" : "planned", [SC(`s${n}`, n < 6 ? "done" : "todo")], n === 6 ? { current: true } : {});
    });
    const go = vi.fn();
    await mount(go);
    // 占位章名（「第N章」）不和章号并排：卡上只写一遍「第 N 章」，没有 CH 缩写
    expect([...container.querySelectorAll(".hm-chap .hm-chap-t")].map(n => n.textContent)).toEqual(["第 5 章", "第 6 章", "第 7 章", "第 8 章", "第 9 章"]);
    expect(container.querySelectorAll(".hm-chap .hm-chap-n").length).toBe(0);
    expect(container.querySelector(".hm-chaps-sub").textContent).toBe("当前章附近");
    expect(container.querySelector('.hm-chap[aria-current="true"] .hm-chap-t').textContent).toBe("第 6 章");
    await click([...container.querySelectorAll(".hm-chap")].find(b => b.textContent.includes("第 8 章")));
    expect(go).toHaveBeenLastCalledWith("writer", { type: "ws:writer-scene", detail: "s8" });
  });

  it("「全部章节」在高级模式去章节编排，在作家模式去写作台", async () => {
    const go = vi.fn();
    await mount(go, { mode: "advanced" });
    const allBtn = () => container.querySelector(".hm-chaps-head .btn");
    expect(allBtn().textContent).toContain("章节编排");
    await click(allBtn());
    expect(go).toHaveBeenLastCalledWith("author");

    await act(async () => { root.unmount(); });
    container.remove();
    await mount(go, { mode: "writer" });
    expect(allBtn().textContent).toContain("全部章节");
    await click(allBtn());
    expect(go).toHaveBeenLastCalledWith("writer");
  });

  it("全书进度写明口径：动笔 N / M 章", async () => {
    await mount(vi.fn());
    expect(container.querySelector(".hm-book-val").textContent.replace(/\s+/g, "")).toBe("动笔2/4章");
  });

  it("雪花卡只读服务端 dashboard：有未确认的一步就回那一步，十步都确认了就去写焦点场景", async () => {
    const steps = (activeAt) => ["book_brief", "one_sentence_summary", "one_paragraph_summary", "character_sheets", "short_synopsis",
      "character_synopses", "long_synopsis", "character_bibles", "scene_list", "scene_details"]
      .map((key, i) => ({ key, name: `步骤${i + 1}`, s: activeAt < 0 || i < activeAt ? "done" : (i === activeAt ? "active" : "todo") }));
    fx.work = { ...fx.work, home: { snow: steps(3) } };
    const go = vi.fn();
    await mount(go);
    const card = () => [...container.querySelectorAll(".home-card")].find(el => el.textContent.includes("雪花构思"));
    expect(card().textContent).toContain("步骤4 · 第 4 步");
    expect(card().querySelector(".home-snow-count b").textContent).toBe("3");
    await click(card().querySelector(".home-snow-next"));
    expect(go).toHaveBeenLastCalledWith("snowflake", { type: "ws:snow-step", detail: "characters" });

    await act(async () => { root.unmount(); });
    container.remove();
    fx.work = { ...fx.work, home: { snow: steps(-1) } };
    await mount(go);
    expect(card().textContent).toContain("十步已全部确认");
    await click(card().querySelector(".home-snow-next"));
    expect(go).toHaveBeenLastCalledWith("writer", { type: "ws:writer-scene", detail: "ch03s2" });
  });

  it("雪花卡：dashboard 读失败且没有缓存的步骤时说读不到，不再一直说「正在读取」", async () => {
    fx.status = {
      projects: { phase: "ready", error: null },
      dashboard: { phase: "error", error: { message: "主页数据暂时无法连接" } },
    };
    await mount(vi.fn());
    const card = [...container.querySelectorAll(".home-card")].find(el => el.textContent.includes("雪花构思"));
    expect(card.textContent).not.toContain("正在读取构思进度");
    expect(card.textContent).toContain("构思进度暂时读不到");
    expect(card.querySelector('[role="status"]')).toBeNull();
    // 页顶的提示仍在，重新连接走 store 的 retry
    expect(container.querySelector(".hm-data-notice").textContent).toContain("服务端数据暂时没有更新");
  });

  it("雪花卡：dashboard 还在读时才说「正在读取」", async () => {
    fx.status = { projects: { phase: "ready", error: null }, dashboard: { phase: "loading", error: null } };
    await mount(vi.fn());
    const card = [...container.querySelectorAll(".home-card")].find(el => el.textContent.includes("雪花构思"));
    expect(card.querySelector('[role="status"]').textContent).toBe("正在读取构思进度…");
  });

  it("回到主页拉一次 dashboard；作品列表还在读时不拉（列表读完 store 会接着拉，免得启动时重复请求）", async () => {
    const { WsWorks } = await import("./ws-works.jsx");
    fx.status = { projects: { phase: "loading", error: null }, dashboard: { phase: "idle", error: null } };
    await mount(vi.fn());
    expect(WsWorks.retry).not.toHaveBeenCalled();

    await act(async () => { root.unmount(); });
    container.remove();
    fx.status = { projects: { phase: "ready", error: null }, dashboard: { phase: "ready", error: null } };
    await mount(vi.fn());
    expect(WsWorks.retry).toHaveBeenCalledTimes(1);
    expect(WsWorks.retry).toHaveBeenCalledWith("dashboard", "prj-main");
  });

  it("雪花卡不再读构思页的本机缓存（window.s2StepSummary）", async () => {
    window.s2StepSummary = () => ({ steps: [{ name: "本机", s: "done" }], now: "本机缓存的说法" });
    try {
      await mount(vi.fn());
      expect(container.textContent).not.toContain("本机缓存的说法");
    } finally {
      delete window.s2StepSummary;
    }
  });

  it("目录还没读到时不先说「白纸」，而是说正在读取", async () => {
    fx.chapters = [];
    fx.catalogReady = false;
    await mount(vi.fn());
    expect(container.textContent).toContain("正在读取章节目录");
    expect(container.textContent).not.toContain("这部作品还是一张白纸");
  });

  it("目录读到了且确实是空的，才是空白作品页", async () => {
    fx.chapters = [];
    await mount(vi.fn());
    expect(container.textContent).toContain("这部作品还是一张白纸");
  });
});


describe("WsHome · 诊断角标（写作台深改面板里还开着的发现数）", () => {
  it("章卡上标「诊断 N」、进度脊一句「诊断待改 N」；没有计数时什么也不画", async () => {
    const { mount, restore } = await (async () => {
      const mod = await import("./ws-home.jsx");
      const hosts = [];
      return {
        mount: async () => {
          const host = document.createElement("div");
          document.body.appendChild(host);
          const root = createRoot(host);
          hosts.push({ root, host });
          await act(async () => root.render(React.createElement(mod.WsHome, { go: vi.fn() })));
          return host;
        },
        restore: async () => { for (const { root, host } of hosts) { await act(async () => root.unmount()); host.remove(); } },
      };
    })();
    fx.chapters = [
      { id: "ch01", backendId: "c1", n: "01", title: "盐场的早班", state: "writing", current: true, words: { cur: 3600, target: 4000 }, scenes: [{ sid: "ch01s1", backendId: "s1", title: "交班", state: "writing", kind: "proactive", brief: {} }] },
      { id: "ch02", backendId: "c2", n: "02", title: "潮位", state: "planned", words: { cur: 0, target: 4000 }, scenes: [{ sid: "ch02s1", backendId: "s2", title: "涨潮", state: "todo", kind: "proactive", brief: {} }] },
    ];
    diagFx.byChapter = { c1: { open: 3, blocking: 1 } };
    diagFx.totals = { open: 3 };
    let host = await mount();
    expect(host.querySelector('[data-testid="home-chap-diag-01"]').textContent).toBe("诊断 3");
    expect(host.querySelector('[data-testid="home-chap-diag-02"]')).toBeNull();
    expect(host.textContent).toContain("诊断待改 3");
    await restore();

    diagFx.byChapter = {};
    diagFx.totals = null;
    host = await mount();
    expect(host.querySelector('[data-testid="home-chap-diag-01"]')).toBeNull();
    expect(host.textContent).not.toContain("诊断待改");
    await restore();
  });
});
