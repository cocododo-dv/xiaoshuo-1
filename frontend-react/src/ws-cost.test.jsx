// WsCost store 层单测（结果闭环治理 §5.8/§10）：
// 项目级读 cost-dashboard（趋势/构成/明细一读聚合）；chapter/scene 走 cost-summary 下钻；
// costBack 用缓存就地还原不重发请求；空/失败降级不抛。只读——不发写请求。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

/* 成本看板用目录把后端章 / 场 id 换成「第 N 章 · 第 M 场」；这里不测目录，给空目录 */
const fx = vi.hoisted(() => ({ catalog: [] }));
vi.mock("./ws-catalog.jsx", () => ({
  WsCatalog: { get: () => fx.catalog, subscribe: () => () => {} },
  useCatalogChapters: () => fx.catalog,
}));

/* 成本看板跟随当前作品（ws-works 的 useActiveWorkIdentity）；作品 id 由夹具给 */
const works = vi.hoisted(() => ({ id: "P1", pending: false }));
vi.mock("./ws-works.jsx", () => ({
  // readyId：能拿去发请求的当前作品（新建作品还没拿到正式 id 时是 null）
  WsWorks: { activeId: () => works.id, readyId: () => (works.pending || works.id === "__loading__" ? null : works.id) },
  useActiveWorkIdentity: () => ({ id: works.id, title: "测试长篇" }),
}));

async function loadStore() {
  const client = await import("./lib/client.js");
  return { client, mod: await import("./ws-cost-store.js") };
}

async function loadView() {
  const client = await import("./lib/client.js");
  const store = await import("./ws-cost-store.js");
  const view = await import("./ws-cost.jsx");
  return { client, mod: { ...store, WsCost: view.WsCost } };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

// 后端 cost-dashboard 的形状（2026-09-30 批准 #4 起以 token 为主）：这份夹具里所有模型都定了价
const DASH = {
  project_id: "P1",
  summary: {
    total_cost: 1.23, currency: "USD", call_count: 3, is_estimate: true,
    total_tokens: 900, cross_provider: false,
    pricing: { priced_call_count: 3, unpriced_call_count: 0, priced_tokens: 900, unpriced_tokens: 0, complete: true, unpriced_models: [] },
    phase_breakdown: { candidate_generation: { cost: 1, tokens: 720, share: 0.8, call_count: 2 } },
    archived_chapter_count: 0, chapter_count: 1, archived_scene_count: 0, scene_count: 2,
  },
  trend: {
    days: 30, series: [{ date: "2026-07-16", cost: 0.5, tokens: 300, call_count: 1 }],
    window_cost: 0.5, window_tokens: 300, window_call_count: 1,
  },
  by_model: [{ provider: "openai_compatible", model: "gpt-5", cost: 1.2, tokens: 900, call_count: 3, is_estimate: true, priced: true }],
  by_node: { top: [{ node_id: "style_draft", phase: "candidate_generation", cost: 1, tokens: 600, call_count: 2 }], remainder: null },
  by_chapter: [{ chapter_id: "C1", cost: 1.2, tokens: 900, call_count: 3, scene_count: 2 }],
  top_calls: [{ llm_call_id: "c1", cost: 0.6, total_tokens: 300, phase: "candidate_generation", priced: true, currency: "USD" }],
  quota: {
    period_timezone: "UTC",
    daily_tokens: { used: 100, limit: null, enforced: false },
  },
};

// 真实安装的样子：价书里没写单价，所有金额都是 null（「未定价」），只有 token
const UNPRICED_DASH = {
  project_id: "P1",
  summary: {
    total_cost: null, currency: null, call_count: 58, is_estimate: true,
    total_tokens: 1270116, cross_provider: false,
    pricing: {
      priced_call_count: 0, unpriced_call_count: 58, priced_tokens: 0, unpriced_tokens: 1270116, complete: false,
      unpriced_models: [
        { provider: "openai", model: "m-flash", tokens: 829284, call_count: 49 },
        { provider: "openai", model: "m-sonnet", tokens: 440832, call_count: 9 },
      ],
    },
    phase_breakdown: {
      candidate_generation: { tokens: 779425, call_count: 46, cost: null, share: 0.6137 },
      quality_check: { tokens: 490691, call_count: 12, cost: null, share: 0.3863 },
    },
    tokens_per_archived_scene: 635058, archived_scene_count: 2, scene_count: 19,
    cost_per_archived_chapter: null, archived_chapter_count: 1, chapter_count: 6,
  },
  trend: {
    days: 30,
    series: [
      { date: "2026-09-20", tokens: 0, call_count: 0, cost: null },
      { date: "2026-09-21", tokens: 300000, call_count: 10, cost: null },
      { date: "2026-09-22", tokens: 970116, call_count: 48, cost: null },
    ],
    window_cost: null, window_tokens: 1270116, window_call_count: 58,
  },
  by_model: [
    { provider: "openai", model: "m-flash", is_estimate: true, tokens: 829284, call_count: 49, cost: null, priced: false },
    { provider: "openai", model: "m-sonnet", is_estimate: false, tokens: 440832, call_count: 9, cost: null, priced: false },
  ],
  by_node: {
    top: [
      { node_id: "style_draft", phase: "candidate_generation", tokens: 314691, call_count: 6, cost: null },
      { node_id: "soft_qc", phase: "quality_check", tokens: 218451, call_count: 4, cost: null },
    ],
    remainder: { node_count: 3, tokens: 42217, call_count: 5, cost: null },
  },
  by_chapter: [{ chapter_id: "C1", tokens: 998899, call_count: 28, cost: null, scene_count: 4 }],
  top_calls: [{
    llm_call_id: "c1", created_at: "2026-09-22T07:19:37.557135+00:00", node_id: "style_patch", phase: "revision",
    provider: "openai", model: "m-sonnet", total_tokens: 68062, priced: false, cost: null, currency: null,
    is_estimate: false, latency_ms: 91836, error_code: null, accounting_status: "settled", scene_id: "S1",
  }],
  quota: {
    period_timezone: "UTC",
    daily_tokens: { used: 0, limit: null, enforced: false },
    monthly_tokens: { used: 8563789, limit: null, enforced: false },
    project_daily_tokens: { project_id: "P1", used: 0, limit: null, enforced: false },
    daily_requests: { used: 0, limit: null, enforced: false },
    concurrent_requests: { used: 0, limit: null, enforced: false },
  },
};

describe("WsCost store（成本看板）", () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => vi.restoreAllMocks());

  it("costLoad 项目级：拉 cost-dashboard（默认近 30 天）并入 store", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockResolvedValueOnce(DASH);
    await mod.costLoad("P1");
    expect(client.apiGet).toHaveBeenCalledWith("/api/v2/projects/P1/cost-dashboard?days=30");
    const st = mod.csSnapshot();
    expect(st.level).toBe("project");
    expect(st.dashboard.trend.window_cost).toBe(0.5);
    expect(st.summary.total_cost).toBe(1.23);
    expect(st.quota.daily_tokens.used).toBe(100);
  });

  it("costLoad 支持 days 窗口并记住选择", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockResolvedValue(DASH);
    await mod.costLoad("P1", { days: 7 });
    expect(client.apiGet).toHaveBeenLastCalledWith("/api/v2/projects/P1/cost-dashboard?days=7");
    expect(mod.csSnapshot().days).toBe(7);
    // 后续不带 days 沿用上次窗口
    await mod.costLoad("P1");
    expect(client.apiGet).toHaveBeenLastCalledWith("/api/v2/projects/P1/cost-dashboard?days=7");
  });

  it("costLoad 场景下钻：走 cost-summary?scene_id，保留 dashboard 缓存", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockResolvedValueOnce(DASH);
    await mod.costLoad("P1");
    client.apiGet.mockResolvedValueOnce({ level: "scene", summary: { scene_id: "S1", total_cost: 0.5 } });
    await mod.costLoad("P1", { sceneId: "S1" });
    expect(client.apiGet).toHaveBeenLastCalledWith(expect.stringContaining("cost-summary?scene_id=S1"));
    expect(mod.csSnapshot().level).toBe("scene");
    expect(mod.csSnapshot().summary.scene_id).toBe("S1");
    expect(mod.csSnapshot().dashboard.summary.total_cost).toBe(1.23);
  });

  it("costLoad 章节下钻：走 cost-summary?chapter_id；下钻响应无 quota 时保留旧额度", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockResolvedValueOnce(DASH);
    await mod.costLoad("P1");
    client.apiGet.mockResolvedValueOnce({ level: "chapter", summary: { chapter_id: "C1" } });
    await mod.costLoad("P1", { chapterId: "C1" });
    expect(client.apiGet).toHaveBeenLastCalledWith(expect.stringContaining("cost-summary?chapter_id=C1"));
    expect(mod.csSnapshot().level).toBe("chapter");
    expect(mod.csSnapshot().quota.daily_tokens.used).toBe(100);
  });

  it("costBack：有 dashboard 缓存时就地还原项目层，不重发请求", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockResolvedValueOnce(DASH);
    await mod.costLoad("P1");
    client.apiGet.mockResolvedValueOnce({ level: "chapter", summary: { chapter_id: "C1" } });
    await mod.costLoad("P1", { chapterId: "C1" });
    const callsBefore = client.apiGet.mock.calls.length;
    mod.costBack();
    expect(client.apiGet.mock.calls.length).toBe(callsBefore);
    expect(mod.csSnapshot().level).toBe("project");
    expect(mod.csSnapshot().summary.total_cost).toBe(1.23);
  });

  it("costBack：无缓存时回退为重新加载项目级", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockRejectedValueOnce(new Error("boom"));
    await mod.costLoad("P1"); // 失败，dashboard 仍为空
    client.apiGet.mockResolvedValueOnce(DASH);
    mod.costBack();
    await vi.waitFor(() => expect(mod.csSnapshot().dashboard).toBeTruthy());
    expect(client.apiGet).toHaveBeenLastCalledWith(expect.stringContaining("cost-dashboard"));
  });

  it("costLoad 失败：降级为 error，不抛；下次成功后清除 error", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockRejectedValueOnce(new Error("网络错误"));
    const r = await mod.costLoad("P1");
    expect(r).toBeNull();
    expect(mod.csSnapshot().error).toBeTruthy();
    expect(mod.csSnapshot().loading).toBe(false);
    client.apiGet.mockResolvedValueOnce(DASH);
    await mod.costLoad("P1");
    expect(mod.csSnapshot().error).toBeNull();
    expect(mod.csSnapshot().dashboard).toBeTruthy();
  });

  it("costLoad 只读：不发任何写请求", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockResolvedValue(DASH);
    await mod.costLoad("P1");
    await mod.costLoad("P1", { chapterId: "C1" });
    expect(client.apiPost).not.toHaveBeenCalled();
    expect(client.apiPatch).not.toHaveBeenCalled();
    expect(client.apiDelete).not.toHaveBeenCalled();
  });

  it("costLoad 无 projectId：直接返回 null 不发请求", async () => {
    const { client, mod } = await loadStore();
    const r = await mod.costLoad("");
    expect(r).toBeNull();
    expect(client.apiGet).not.toHaveBeenCalled();
  });

  /* 审计 F05-03：迟到的响应不能盖掉新的；换作品时上一部的看板不能留在屏上 */
  it("换作品：B 先回来、A 的响应迟到，看板仍是 B 的", async () => {
    const { client, mod } = await loadStore();
    const a = deferred();
    client.apiGet.mockImplementation((url) => {
      if (url.startsWith("/api/v2/projects/A/")) return a.promise;
      if (url.startsWith("/api/v2/projects/B/")) return Promise.resolve({ ...DASH, project_id: "B", summary: { ...DASH.summary, total_tokens: 222 } });
      return Promise.resolve(null);
    });
    const loadA = mod.costLoad("A");
    await mod.costLoad("B");
    a.resolve({ ...DASH, project_id: "A", summary: { ...DASH.summary, total_tokens: 111 } });
    await loadA;
    const st = mod.csSnapshot();
    expect(st.projectId).toBe("B");
    expect(st.dashboard.project_id).toBe("B");
    expect(st.summary.total_tokens).toBe(222);
    expect(st.loading).toBe(false);
  });

  it("换作品：新作品的请求在飞时不显示上一部作品的看板", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockResolvedValueOnce(DASH);
    await mod.costLoad("A");
    const b = deferred();
    client.apiGet.mockReturnValueOnce(b.promise);
    const loadB = mod.costLoad("B");
    const st = mod.csSnapshot();
    expect(st.projectId).toBe("B");
    expect(st.dashboard).toBeNull();
    expect(st.summary).toBeNull();
    expect(st.quota).toBeNull();
    expect(st.loading).toBe(true);
    b.resolve({ ...DASH, project_id: "B" });
    await loadB;
    expect(mod.csSnapshot().dashboard.project_id).toBe("B");
  });

  it("快速切换统计窗口：后发的那次说了算，先发的迟到不覆盖", async () => {
    const { client, mod } = await loadStore();
    const d7 = deferred();
    client.apiGet.mockImplementation((url) => (url.endsWith("days=7")
      ? d7.promise
      : Promise.resolve({ ...DASH, trend: { ...DASH.trend, days: 30, window_tokens: 3030 } })));
    const first = mod.costLoad("P1", { days: 7 });
    await mod.costLoad("P1", { days: 30 });
    d7.resolve({ ...DASH, trend: { ...DASH.trend, days: 7, window_tokens: 707 } });
    await first;
    expect(mod.csSnapshot().days).toBe(30);
    expect(mod.csSnapshot().dashboard.trend.window_tokens).toBe(3030);
  });

  it("返回全书之后，迟到的章节下钻响应不再把页面翻回下钻", async () => {
    const { client, mod } = await loadStore();
    client.apiGet.mockResolvedValueOnce(DASH);
    await mod.costLoad("P1");
    const drill = deferred();
    client.apiGet.mockReturnValueOnce(drill.promise);
    const loadDrill = mod.costLoad("P1", { chapterId: "C1" });
    mod.costBack();
    expect(mod.csSnapshot().level).toBe("project");
    expect(mod.csSnapshot().loading).toBe(false);
    drill.resolve({ level: "chapter", summary: { chapter_id: "C1" } });
    await loadDrill;
    expect(mod.csSnapshot().level).toBe("project");
    expect(mod.csSnapshot().summary.total_cost).toBe(1.23);
  });

  it("迟到的失败也不盖掉新结果：A 失败回来时看板仍是 B 的、不报错", async () => {
    const { client, mod } = await loadStore();
    const a = deferred();
    client.apiGet.mockImplementation((url) => (url.startsWith("/api/v2/projects/A/") ? a.promise : Promise.resolve({ ...DASH, project_id: "B" })));
    const loadA = mod.costLoad("A");
    await mod.costLoad("B");
    a.reject(new Error("A 超时"));
    await loadA;
    expect(mod.csSnapshot().error).toBeNull();
    expect(mod.csSnapshot().dashboard.project_id).toBe("B");
  });
});

/* ---------- 视图：章 / 场用目录里的叫法，金额与状态是给作者看的 ---------- */
import React, { act } from "react";
import { createRoot } from "react-dom/client";

async function mountCost(mod) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(<mod.WsCost />));
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
  return host;
}

let root;
let host;

describe("WsCost 视图", () => {
  beforeEach(() => {
    vi.resetModules();
    fx.catalog = [{
      id: "ch01", backendId: "C1", n: "01", title: "盐场的早班",
      scenes: [{ sid: "sid-a", backendId: "S1", title: "交班" }, { sid: "sid-b", backendId: "S2", title: "夜渡" }],
    }];
    works.id = "P1";
    works.pending = false;
  });
  afterEach(async () => {
    if (root) await act(async () => root.unmount());
    if (host) host.remove();
    root = null;
    host = null;
    vi.restoreAllMocks();
  });

  it("按章节用后端 id 找到目录章名；调用明细里场景、状态、金额都是作者读得懂的样子", async () => {
    const { client, mod } = await loadView();
    client.apiGet.mockResolvedValue({
      ...DASH,
      summary: { ...DASH.summary, total_cost: 402.7712 },
      top_calls: [
        { llm_call_id: "c1", cost: 26.84951, total_tokens: 300, phase: "candidate_generation", node_id: "style_draft", accounting_status: "settled", scene_id: "S2", latency_ms: 1520 },
        { llm_call_id: "c2", cost: 0.0042, total_tokens: 10, phase: "quality_check", node_id: "made_up_node", accounting_status: "reserved" },
      ],
    });
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root.render(<mod.WsCost />));
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(host.textContent).toContain("第 1 章 · 盐场的早班");
    expect([...host.querySelectorAll("td")].some((td) => td.textContent === "C1")).toBe(false);
    expect(host.textContent).toContain("第 1 章 · 第 2 场");
    expect(host.textContent).toContain("已结算");
    expect(host.textContent).not.toContain("settled");
    expect(host.textContent).toContain("风格稿");
    expect(host.textContent).toContain("402.77 USD");
    expect(host.textContent).toContain("26.85 USD");
    expect(host.textContent).toContain("0.0042 USD");
    expect(host.textContent).toContain("1.5 秒");
    // 开发者口径（配置文件路径）收在「口径说明」里，不在页头
    expect(host.querySelector(".ws-page-head").textContent).not.toContain("pricing.yaml");
  });

  it("场景预算解除武装时显示「不限」，而不是悄悄丢掉预算卡", async () => {
    const { client, mod } = await loadView();
    client.apiGet.mockResolvedValueOnce(DASH);
    await mod.costLoad("P1");
    client.apiGet.mockResolvedValueOnce({
      level: "scene",
      summary: { scene_id: "S1", total_cost: 0.5, currency: "USD", call_count: 1, total_tokens: 20, budget: { budget: null, used: 1234, disarmed: true } },
    });
    await mod.costLoad("P1", { sceneId: "S1" });
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root.render(<mod.WsCost />));
    expect(host.textContent).toContain("第 1 章 · 第 1 场「交班」");
    expect(host.textContent).toContain("不限");
    expect(host.textContent).toContain("已用 1,234 token");
  });

  it("作品列表还没到（__loading__）时不拉账本；当前作品落定后按它的 id 拉", async () => {
    works.id = "__loading__";
    const { client, mod } = await loadView();
    client.apiGet.mockResolvedValue(DASH);
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root.render(<mod.WsCost />));
    expect(client.apiGet).not.toHaveBeenCalled();
    expect(host.textContent).toContain("还没有选作品");

    works.id = "P2";
    await act(async () => root.render(<mod.WsCost />));
    await act(async () => { await Promise.resolve(); });
    expect(client.apiGet).toHaveBeenCalledWith("/api/v2/projects/P2/cost-dashboard?days=30");
  });

  it("新建的作品还没拿到正式 id：不拿临时 id 拉账本；拿到正式 id 后再拉", async () => {
    works.id = "tmp_new_work";
    works.pending = true;
    const { client, mod } = await loadView();
    client.apiGet.mockResolvedValue(DASH);
    await mountCost(mod);
    expect(client.apiGet).not.toHaveBeenCalled();

    works.id = "P9";
    works.pending = false;
    await act(async () => root.render(<mod.WsCost />));
    await act(async () => { await Promise.resolve(); });
    expect(client.apiGet).toHaveBeenCalledWith("/api/v2/projects/P9/cost-dashboard?days=30");
  });

  /* ---- 以 token 为主（批准 #4）：没定价的模型不编金额，图与条按 token 画 ---- */
  it("价书里没有单价：卡片、表格说「未定价」，不出现任何金额；趋势与节点条按 token 画", async () => {
    const { client, mod } = await loadView();
    client.apiGet.mockResolvedValue(UNPRICED_DASH);
    await mountCost(mod);

    const text = host.textContent;
    expect(text).toContain("全书累计 Token1,270,116");
    expect(text).toContain("全书累计费用未定价");
    expect(text).toContain("58 次调用的模型没有单价");
    expect(text).not.toContain("USD");
    expect(text).not.toContain("—：");
    // 逐日柱：高度按 token，最多的一天顶满（110 − 12），没调用的那天是 1 像素的底线
    const heights = [...host.querySelectorAll("rect.cs-bar")].map((r) => Number(r.getAttribute("height")));
    expect(heights[0]).toBe(1);
    expect(heights[2]).toBe(98);
    expect(heights[1]).toBeGreaterThan(20);
    expect(text).toContain("合计 1,270,116 token · 58 次调用");
    // 节点条：按 token 比，第一名满格，第二名按比例
    const nodeBars = [...host.querySelectorAll('[role="progressbar"]')].filter((el) => /的用量$/.test(el.getAttribute("aria-label") || ""));
    expect(nodeBars.map((el) => el.getAttribute("aria-valuenow"))).toEqual(["100", "69"]);
    expect(text).toContain("314,691 token · 6 次");
    expect(text).toContain("其余 3 个节点合计 42,217 token（5 次）");
    // 表格里的费用格是「未定价」标签
    const priceCells = [...host.querySelectorAll("td .ws-tag")].filter((el) => el.textContent === "未定价");
    expect(priceCells.length).toBe(4); // 两个模型 + 一章 + 一条调用
    // 口径说明点出哪些模型没有单价；价格不再是「占位估算」
    expect(host.querySelector('[data-testid="cs-unpriced-models"]').textContent).toContain("openai / m-flash（829,284 token，49 次）");
    expect(text).not.toContain("占位");
    const pills = [...host.querySelectorAll(".ws-tag")].map((el) => el.textContent);
    expect(pills).toContain("用量含估算");
    expect(pills).not.toContain("估算价");
    expect(text).toContain("全局用量");
  });

  it("只有一部分模型定了价：合计金额写成「已定价部分」，不当成全部的费用", async () => {
    const { client, mod } = await loadView();
    client.apiGet.mockResolvedValue({
      ...UNPRICED_DASH,
      summary: {
        ...UNPRICED_DASH.summary, total_cost: 0.5, currency: "USD",
        pricing: { ...UNPRICED_DASH.summary.pricing, priced_call_count: 9, unpriced_call_count: 49, complete: false },
      },
      by_model: [
        { ...UNPRICED_DASH.by_model[0] },
        { ...UNPRICED_DASH.by_model[1], cost: 0.5, priced: true },
      ],
    });
    await mountCost(mod);

    expect(host.textContent).toContain("全书累计费用已定价部分 0.50 USD");
    expect(host.textContent).toContain("49 次调用的模型没有单价");
    const modelRows = [...host.querySelectorAll("tbody tr")].filter((tr) => tr.textContent.includes("m-sonnet") && tr.textContent.includes("openai"));
    expect(modelRows[0].textContent).toContain("0.50 USD");
    expect(modelRows[0].textContent).not.toContain("已定价部分");
  });

  it("调用明细的时间按浏览器所在时区显示，不截取 UTC 串", async () => {
    const tz = process.env.TZ;
    process.env.TZ = "Asia/Shanghai";
    try {
      const { client, mod } = await loadView();
      client.apiGet.mockResolvedValue(UNPRICED_DASH);
      await mountCost(mod);
      const cell = [...host.querySelectorAll("td.is-nowrap")].find((td) => /^\d{2}-\d{2} \d{2}:\d{2}$/.test(td.textContent));
      expect(cell.textContent).toBe("09-22 15:19");
    } finally {
      if (tz === undefined) delete process.env.TZ; else process.env.TZ = tz;
    }
  });

  it("下钻的额外成本只说失败重试花掉的 token；没有失败就不说，也不再有重复质检 / 补候选", async () => {
    const { client, mod } = await loadView();
    client.apiGet.mockResolvedValueOnce(UNPRICED_DASH);
    await mod.costLoad("P1");
    client.apiGet.mockResolvedValueOnce({
      level: "scene",
      summary: {
        scene_id: "S1", total_cost: null, currency: null, call_count: 9, total_tokens: 440832,
        pricing: { complete: false, unpriced_call_count: 9, unpriced_models: [] },
        budget: { budget: null, used: 440832, disarmed: true },
        extra_cost: { failed_tokens: 22042, failed_attempt_count: 2, failed_cost: null, failed_share: 0.05 },
      },
    });
    await mod.costLoad("P1", { sceneId: "S1" });
    await mountCost(mod);

    const extra = host.querySelector('[data-testid="cs-extra-cost"]');
    expect(extra.textContent).toBe("失败重试花掉 22,042 token（占 5%，2 次失败的请求）。");
    expect(host.textContent).toContain("费用未定价");
    expect(host.textContent).not.toContain("重复质检");
    expect(host.textContent).not.toContain("补候选");

    await act(async () => root.unmount());
    client.apiGet.mockResolvedValueOnce({
      level: "scene",
      summary: {
        scene_id: "S1", total_cost: 0.25, currency: "USD", call_count: 9, total_tokens: 440832,
        pricing: { complete: true, unpriced_call_count: 0, unpriced_models: [] },
        extra_cost: { failed_tokens: 0, failed_attempt_count: 0, failed_cost: null, failed_share: 0 },
      },
    });
    await mod.costLoad("P1", { sceneId: "S1" });
    root = createRoot(host);
    await act(async () => root.render(<mod.WsCost />));
    expect(host.querySelector('[data-testid="cs-extra-cost"]')).toBeNull();
    expect(host.textContent).toContain("费用0.25 USD");
  });

  it("下钻还在读时说在读，不把全书的数挂在这一章名下", async () => {
    const { client, mod } = await loadView();
    client.apiGet.mockResolvedValueOnce(UNPRICED_DASH);
    await mod.costLoad("P1");
    const drill = deferred();
    client.apiGet.mockReturnValueOnce(drill.promise);
    const loading = mod.costLoad("P1", { chapterId: "C1" });
    await mountCost(mod);
    expect(host.textContent).toContain("正在读取账本");
    expect(host.textContent).not.toContain("1,270,116");
    drill.resolve({ level: "chapter", summary: { chapter_id: "C1", total_tokens: 998899, call_count: 28, total_cost: null, pricing: { complete: false } } });
    await act(async () => { await loading; });
    expect(host.textContent).not.toContain("正在读取账本");
    expect(host.textContent).toContain("盐场的早班");
    expect(host.textContent).toContain("998,899");
  });
});
