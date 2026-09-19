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
  work: {
    id: "prj-main", title: "北岸手记", sub: "一句话简介", greet: "继续写作", mark: "北", accent: "slate", genre: "",
    wordsTotal: 12000, wordsTarget: 100000, chaptersWritten: 2, chaptersTotal: 4,
    wordsToday: 800, wordsTargetDay: 1000, streak: 3, home: {},
  },
}));

vi.mock("./ws-works.jsx", () => ({
  WsWorks: { retry: vi.fn() },
  useActiveWork: () => fx.work,
  useWorksStatus: () => ({ projects: { phase: "ready", error: null }, dashboard: { phase: "ready", error: null } }),
  wsKey: (k) => k,
}));
vi.mock("./ws-catalog.jsx", () => ({
  WsCatalog: { totals: () => ({ words: 12000, written: 2, planned: fx.chapters.length }) },
  useCatalogChapters: () => fx.chapters,
}));
vi.mock("./ws-review.jsx", () => ({
  RV_KINDS: {},
  rvOpenItems: () => [],
  rvMarkResolved: vi.fn(),
}));
vi.mock("./ws-ai-providers.jsx", () => ({
  WsAiProviders: { refresh: vi.fn(() => Promise.resolve()) },
  useAiProviders: () => ({
    loaded: true, loading: false, error: null,
    overview: { readiness: { ready: true }, api_snapshot: { enabled: true } },
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

async function mount(go) {
  const { WsHome } = await import("./ws-home.jsx");
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => { root.render(<WsHome go={go} />); });
}

beforeEach(() => {
  vi.resetModules();
  fx.chapters = catalogFixture();
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
    expect(container.querySelector(".hm-slug").textContent).toContain("CH 03");
    expect(container.querySelector(".hm-chaps-title").textContent).toBe("全书 4 章");
  });

  it("图例章数与场景计数来自目录真相", async () => {
    await mount(vi.fn());
    const leg = (k) => container.querySelector(`.hm-leg.st-${k} b`).textContent;
    expect([leg("approved"), leg("review"), leg("draft"), leg("writing"), leg("planned"), leg("todo")]).toEqual(["1", "1", "0", "1", "1", "0"]);
    expect(container.querySelector(".hm-spine-scenes").textContent.replace(/\s+/g, "")).toBe("已规划6场·完成4·在写1·待写1");
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
    expect(container.querySelector(".hm-slug").textContent).toContain("CH 02 · SC 02");
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

  it("主动场景仍是 目标 / 阻碍 / 挫折", async () => {
    fx.chapters = [CH(1, "writing", [SC("ch01s1", "writing", { goal: "拿到钥匙" })], { current: true })];
    await mount(vi.fn());
    const keys = [...container.querySelectorAll(".hm-gos-k")].map(el => el.textContent);
    expect(keys).toEqual(["目标", "阻碍", "挫折"]);
    expect(container.querySelector(".hm-gos-v").textContent).toBe("拿到钥匙");
  });
});
