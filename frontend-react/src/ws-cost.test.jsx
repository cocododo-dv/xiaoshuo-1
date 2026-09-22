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
const works = vi.hoisted(() => ({ id: "P1" }));
vi.mock("./ws-works.jsx", () => ({
  WsWorks: { activeId: () => works.id },
  useActiveWorkIdentity: () => ({ id: works.id, title: "测试长篇" }),
}));

async function loadStore() {
  const client = await import("./lib/client.js");
  return { client, mod: await import("./ws-cost.jsx") };
}

const DASH = {
  project_id: "P1",
  summary: {
    total_cost: 1.23, currency: "USD", call_count: 3, is_estimate: true,
    total_tokens: 900, cross_provider: false,
    phase_breakdown: { candidate_generation: { cost: 1, share: 0.8, call_count: 2 } },
    judge_independence: { correlated_judge: false },
  },
  trend: {
    days: 30, series: [{ date: "2026-07-16", cost: 0.5, tokens: 300, call_count: 1 }],
    window_cost: 0.5, window_tokens: 300, window_call_count: 1,
  },
  by_model: [{ provider: "openai_compatible", model: "gpt-5", cost: 1.2, tokens: 900, call_count: 3, is_estimate: true }],
  by_node: { top: [{ node_id: "style_draft", phase: "candidate_generation", cost: 1, tokens: 600, call_count: 2 }], remainder: null },
  by_chapter: [{ chapter_id: "C1", cost: 1.2, tokens: 900, call_count: 3, scene_count: 2 }],
  top_calls: [{ llm_call_id: "c1", cost: 0.6, total_tokens: 300, phase: "candidate_generation" }],
  quota: {
    period_timezone: "UTC",
    any_enforced: true,
    daily_tokens: { used: 100, limit: 1000, enforced: true },
  },
};

describe("WsCost store（成本看板）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
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
    expect(st.quota.daily_tokens.limit).toBe(1000);
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
    expect(mod.csSnapshot().quota.daily_tokens.limit).toBe(1000);
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
});

/* ---------- 视图：章 / 场用目录里的叫法，金额与状态是给作者看的 ---------- */
import React, { act } from "react";
import { createRoot } from "react-dom/client";

describe("WsCost 视图", () => {
  let root;
  let host;
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    fx.catalog = [{
      id: "ch01", backendId: "C1", n: "01", title: "盐场的早班",
      scenes: [{ sid: "sid-a", backendId: "S1", title: "交班" }, { sid: "sid-b", backendId: "S2", title: "夜渡" }],
    }];
    works.id = "P1";
  });
  afterEach(async () => {
    if (root) await act(async () => root.unmount());
    if (host) host.remove();
    root = null;
    host = null;
    vi.restoreAllMocks();
  });

  it("按章节用后端 id 找到目录章名；调用明细里场景、状态、金额都是作者读得懂的样子", async () => {
    const { client, mod } = await loadStore();
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
    const { client, mod } = await loadStore();
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
    const { client, mod } = await loadStore();
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
});
