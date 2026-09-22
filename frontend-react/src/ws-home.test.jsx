// 主页视图渲染测（2026-09-16「流程」并入主页）：
// 断「可观测结果」——进度脊按目录真相逐章渲染、前线与焦点卡同源、图例与场景计数落到 DOM、
// 分段点击带 ws:writer-scene 深链进写作房间、反应场景的三拍标签跟着 kindFields 走。
// 四个 store 全部 mock：主页只读它们的同步缓存，不该有任何网络依赖。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const fx = vi.hoisted(() => ({
  chapters: [],
  reviewItems: [],
  reviewReady: false,
  reviewError: null,
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
// focusScene 逐行照抄 ws-catalog.jsx 的 WsCatalog.currentChapter() + focusScene()（那边改了，这里跟着改）：
// 当前章里在写的 → 第一场没写完的 → 末场；当前章没铺场时从当前章往后找第一场没写完的（到书尾绕回书头），
// 全书都写完了就停在当前章之前最近的那一场上。主页不再自己判断「现在该写哪一场」，只认目录 store 给的答案。
function focusSceneOf(chapters) {
  const pick = (c) => {
    const scenes = (c && c.scenes) || [];
    if (!scenes.length) return null;
    const s = scenes.find(x => x.state === "writing") || scenes.find(x => x.state !== "done") || scenes[scenes.length - 1];
    return { chapter: c, scene: s, index: scenes.indexOf(s) };
  };
  const current = chapters.find(c => c.current) || chapters.find(c => c.state === "writing") || chapters[chapters.length - 1] || null;
  const hit = pick(current);
  if (hit) return hit;
  const at = Math.max(0, chapters.indexOf(current));
  const forward = [...chapters.slice(at + 1), ...chapters.slice(0, at)];
  for (const c of forward) {
    const next = pick(c);
    if (next && next.scene.state !== "done") return next;
  }
  const backward = [...chapters.slice(0, at).reverse(), ...chapters.slice(at + 1)];
  for (const c of backward) { const next = pick(c); if (next) return next; }
  return null;
}
vi.mock("./ws-catalog.jsx", () => ({
  WsCatalog: {
    totals: () => ({ words: 12000, written: 2, planned: fx.chapters.length }),
    ready: () => fx.catalogReady,
    loadError: () => null,
    focusScene: () => focusSceneOf(fx.chapters),
    reset: vi.fn(),
  },
  useCatalogChapters: () => fx.chapters,
}));
// useReviewOpenItems 照 ws-review.jsx 的订阅式读取来：ws:review-changed 与 store 私有的装载监听都会重渲。
// 装载失败不广播 ws:review-changed，这里用测试专用事件 hm-test:review-load 代替 store 的 rvLoadListeners。
vi.mock("./ws-review.jsx", async () => {
  const React = await import("react");
  return {
    RV_KINDS: { decision: { tone: "crimson", label: "决策" } },
    rvMarkResolved: vi.fn(),
    useReviewOpenItems: () => {
      const [, force] = React.useState(0);
      React.useEffect(() => {
        const bump = () => force(n => n + 1);
        window.addEventListener("ws:review-changed", bump);
        window.addEventListener("hm-test:review-load", bump);
        return () => {
          window.removeEventListener("ws:review-changed", bump);
          window.removeEventListener("hm-test:review-load", bump);
        };
      }, []);
      return { items: fx.reviewItems, snoozed: [], ready: fx.reviewReady, error: fx.reviewError };
    },
  };
});
vi.mock("./ws-ai-providers.jsx", () => ({
  WsAiProviders: { refresh: vi.fn(() => Promise.resolve()) },
  useAiProviders: () => ({
    loaded: true, loading: false, error: null,
    overview: { readiness: { ready: fx.aiReady }, api_snapshot: { enabled: true } },
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
  fx.reviewItems = [];
  fx.reviewReady = false;
  fx.reviewError = null;
  fx.aiReady = true;
  fx.catalogReady = true;
  fx.status = { projects: { phase: "ready", error: null }, dashboard: { phase: "ready", error: null } };
  fx.work = { ...fx.work, home: {} };
  localStorage.clear();
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

describe("WsHome · 待办速览跟着收件箱 store 走", () => {
  const todoCard = () => [...container.querySelectorAll(".home-card")].find(el => el.textContent.includes("待办收件箱"));

  it("收件箱还没拉回来时说「正在读取」，拉回来后显示真实待办（不再先报「都处理完了」）", async () => {
    await mount(vi.fn());
    expect(todoCard().querySelector('[role="status"]').textContent).toBe("正在读取待办…");
    expect(todoCard().textContent).not.toContain("待办都处理完了");

    fx.reviewItems = [{ id: "rv1", kind: "decision", title: "需要你拍板：第 3 章结尾", priority: 1 }];
    fx.reviewReady = true;
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:review-changed")); });
    expect(todoCard().textContent).toContain("需要你拍板：第 3 章结尾");
    expect(todoCard().textContent).not.toContain("正在读取待办");
  });

  it("「装载过没有」只认 store 的 ready：收到 ws:review-changed 但 store 还没装载好，仍说正在读取", async () => {
    // 旧实现把「收到过一次 ws:review-changed」当成装载过——这一条会让它先报「都处理完了」
    await mount(vi.fn());
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:review-changed")); });
    expect(todoCard().textContent).toContain("正在读取待办");
    expect(todoCard().textContent).not.toContain("待办都处理完了");
  });

  it("没有兜底计时器：迟迟没拉回来就一直说正在读取，不会过几秒自己改口成「都处理完了」", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    try {
      await mount(vi.fn());
      await act(async () => { vi.advanceTimersByTime(10_000); });
      expect(todoCard().textContent).toContain("正在读取待办");
      expect(todoCard().textContent).not.toContain("待办都处理完了");
    } finally {
      vi.useRealTimers();
    }
  });

  it("拉回来确实是空的，才说都处理完了", async () => {
    fx.reviewReady = true;
    await mount(vi.fn());
    expect(todoCard().textContent).toContain("待办都处理完了");
    expect(todoCard().textContent).not.toContain("正在读取待办");
  });

  it("还没拉到过就失败：说读不到并给去待办重试的出口，不再一直挂着「正在读取」", async () => {
    const go = vi.fn();
    await mount(go);
    expect(todoCard().textContent).toContain("正在读取待办");

    // 失败不广播 ws:review-changed，只通知订阅者
    fx.reviewError = { pid: "prj-main", message: "network down" };
    await act(async () => { window.dispatchEvent(new CustomEvent("hm-test:review-load")); });
    const notice = container.querySelector('[data-testid="home-todo-error"]');
    expect(notice).not.toBeNull();
    expect(notice.textContent).toContain("待办暂时读不出来");
    expect(todoCard().textContent).not.toContain("正在读取待办");
    expect(todoCard().textContent).not.toContain("待办都处理完了");
    await click(container.querySelector('[data-testid="home-todo-open-review"]'));
    expect(go).toHaveBeenLastCalledWith("review");
  });

  it("拉到过之后的失败保留原来的说法，不打扰", async () => {
    fx.reviewReady = true;
    fx.reviewError = { pid: "prj-main", message: "network down" };
    await mount(vi.fn());
    expect(container.querySelector('[data-testid="home-todo-error"]')).toBeNull();
    expect(todoCard().textContent).toContain("待办都处理完了");
  });

  it("标题原样显示（旧的 JSON 标题由收件箱 store 转成人话）；空标题有个兜底", async () => {
    fx.reviewReady = true;
    fx.reviewItems = [
      { id: "rv1", kind: "note", title: "写作偏好：少用比喻", priority: 2 },
      { id: "rv2", kind: "note", title: "  ", priority: 3 },
    ];
    await mount(vi.fn());
    expect([...container.querySelectorAll(".home-todo-text")].map(n => n.textContent)).toEqual(["写作偏好：少用比喻", "（未命名待办）"]);
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

  it("不能直接划掉的卡（实时派生项、带真实效果或候选项的卡）只给「去收件箱」，不给「标记已处理」", async () => {
    // 后端拒绝直接划掉实时派生卡（derived:…）；以前主页按 kind 判，给它们也挂了 ✓：
    // 点下去先乐观移除、记一笔「今日已处理」，接着报错、刷新后又回来
    fx.reviewReady = true;
    fx.reviewItems = [
      { id: "derived:qc:sc-1", live: true, kind: "qc", title: "第 2 场质检没过", priority: 1 },
      { id: "rv-effect", kind: "note", title: "绑定参考画像", priority: 2, actions: [{ label: "绑定", effect: { kind: "bind" } }] },
      { id: "rv-options", kind: "note", title: "选一个结尾", priority: 3, options: ["甲", "乙"] },
    ];
    const { rvMarkResolved } = await import("./ws-review.jsx");
    const go = vi.fn();
    await mount(go);
    const rows = [...container.querySelectorAll(".home-todo")];
    expect(rows.length).toBe(3);
    expect(container.querySelector("button.home-todo-go")).toBeNull();
    expect(rows[0].querySelector(".home-todo-open").getAttribute("title")).toContain("源头改好后会自己消失");
    await click(rows[0].querySelector(".home-todo-open"));
    expect(go).toHaveBeenLastCalledWith("review");
    expect(rvMarkResolved).not.toHaveBeenCalled();
  });

  it("「标记已处理」后焦点交给顶上来的下一行；划掉最后一行交给上一行；列表空了交给「全部」", async () => {
    fx.reviewReady = true;
    fx.reviewItems = [
      { id: "rv1", kind: "note", title: "一条提醒", priority: 1 },
      { id: "rv2", kind: "note", title: "第二条提醒", priority: 2 },
      { id: "rv3", kind: "note", title: "第三条提醒", priority: 3 },
    ];
    const { rvMarkResolved } = await import("./ws-review.jsx");
    // 照 store 的行为：同步乐观移除并广播 ws:review-changed
    rvMarkResolved.mockImplementation((ids) => {
      fx.reviewItems = fx.reviewItems.filter(it => !ids.includes(it.id));
      window.dispatchEvent(new CustomEvent("ws:review-changed"));
    });
    await mount(vi.fn());
    const checkOf = (title) => container.querySelector(`button.home-todo-go[aria-label="标记已处理：${title}"]`);
    const focusedRow = () => document.activeElement.closest(".home-todo");

    // 划掉最后一行：交给上一行
    checkOf("第三条提醒").focus();
    await click(checkOf("第三条提醒"));
    expect(document.activeElement.classList.contains("home-todo-open")).toBe(true);
    expect(focusedRow().textContent).toContain("第二条提醒");

    // 划掉第一行：交给顶上来的下一行
    checkOf("一条提醒").focus();
    await click(checkOf("一条提醒"));
    expect(document.activeElement.classList.contains("home-todo-open")).toBe(true);
    expect(focusedRow().textContent).toContain("第二条提醒");

    checkOf("第二条提醒").focus();
    await click(checkOf("第二条提醒"));
    expect(container.querySelectorAll(".home-todo").length).toBe(0);
    const todoAll = [...container.querySelectorAll(".home-card")]
      .find(el => el.textContent.includes("待办收件箱")).querySelector(".home-card-go");
    expect(todoAll.textContent).toContain("全部");
    expect(document.activeElement).toBe(todoAll);
  });

  it("「标记已处理」后若作者已经把焦点放到别处，不抢焦点", async () => {
    fx.reviewReady = true;
    fx.reviewItems = [
      { id: "rv1", kind: "note", title: "一条提醒", priority: 1 },
      { id: "rv2", kind: "note", title: "第二条提醒", priority: 2 },
    ];
    const { rvMarkResolved } = await import("./ws-review.jsx");
    let emit;
    rvMarkResolved.mockImplementation((ids) => {
      // 移除晚到：作者先点了别的按钮
      emit = () => {
        fx.reviewItems = fx.reviewItems.filter(it => !ids.includes(it.id));
        window.dispatchEvent(new CustomEvent("ws:review-changed"));
      };
    });
    await mount(vi.fn());
    const enter = container.querySelector('[data-testid="home-enter-writer"]');
    await click(container.querySelector("button.home-todo-go"));
    enter.focus();
    await act(async () => { emit(); });
    expect(document.activeElement).toBe(enter);
  });

  it("普通待办行是两个并排的按钮：打开收件箱 / 标记已处理；决策项只能去收件箱拍板（规则同收件箱的 rvNeedsChoice，见上一条）", async () => {
    fx.reviewItems = [
      { id: "rv1", kind: "decision", title: "需要你拍板：结尾", priority: 1 },
      { id: "rv2", kind: "note", title: "一条提醒", priority: 2 },
    ];
    const { rvMarkResolved } = await import("./ws-review.jsx");
    const go = vi.fn();
    await mount(go);
    const rows = [...container.querySelectorAll(".home-todo")];
    expect(rows.length).toBe(2);
    rows.forEach(row => expect(row.tagName).not.toBe("BUTTON"));
    expect(rows[0].querySelector(".home-todo-go")).toBeNull();
    const check = rows[1].querySelector("button.home-todo-go");
    expect(check.getAttribute("aria-label")).toBe("标记已处理：一条提醒");
    await click(check);
    expect(rvMarkResolved).toHaveBeenCalledWith(["rv2"]);
    expect(go).not.toHaveBeenCalled();
    await click(rows[0].querySelector("button.home-todo-open"));
    expect(go).toHaveBeenLastCalledWith("review");
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
