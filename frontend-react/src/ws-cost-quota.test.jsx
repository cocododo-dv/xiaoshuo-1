// 成本看板「全局硬额度 / 全局用量」区块的渲染契约：
// 1) 闸门全关时报用量、不报上限，也不画进度条；
// 2) 缺 enforced / any_enforced 字段的载荷（旧后端、下钻时保留的旧 quota）必须按
//    「已武装」渲染 —— 安全展示只能朝「有上限」的方向失败，不能谎报无上限；
// 3) 今日金额行只在金额闸门启用时出现：它按 env 单价计价，与本页其余
//    config/pricing.yaml 口径不同，未启用时恒为 0，摆出来会自相矛盾。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

/* 成本看板用目录把后端章 / 场 id 换成「第 N 章 · 第 M 场」；这里不测目录，给空目录 */
vi.mock("./ws-catalog.jsx", () => ({
  WsCatalog: { get: () => [], subscribe: () => () => {} },
  useCatalogChapters: () => [],
}));

vi.mock("./ws-works.jsx", () => ({
  WsWorks: { activeId: () => "P1" },
  useActiveWorkIdentity: () => ({ id: "P1", title: "测试长篇" }),
}));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

let root;
let host;

async function renderQuota(quota) {
  const { QuotaSection } = await import("./ws-cost.jsx");
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(<QuotaSection quota={quota} />));
  return host;
}

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  if (host) host.remove();
  root = null;
  host = null;
});

// 后端在闸门关闭时的真实形状（limit 置 null，enforced false）
const DISARMED = {
  period_timezone: "UTC",
  any_enforced: false,
  daily_tokens: { used: 257203, limit: null, enforced: false },
  monthly_tokens: { used: 565854, limit: null, enforced: false },
  project_daily_tokens: { project_id: "P1", used: 243752, limit: null, enforced: false },
  daily_requests: { used: 73, limit: null, enforced: false },
  concurrent_requests: { used: 0, limit: null, enforced: false },
  daily_cost_usd: { used: 0, limit: null, enforced: false },
};

describe("成本看板 · 全局额度区块", () => {
  it("闸门全关：报用量、标未设限，且一条进度条都不画", async () => {
    const el = await renderQuota(DISARMED);

    expect(el.textContent).toContain("全局用量");
    expect(el.textContent).not.toContain("全局硬额度");
    expect(el.textContent).toContain("243,752 token · 未设限");
    expect(el.textContent).toContain("73 次 · 未设限");
    expect(el.textContent).toContain("当前未设任何硬额度");
    expect(el.querySelectorAll('[role="progressbar"]').length).toBe(0);
  });

  it("闸门全关：今日金额行不出现，也不再给出「需配置模型单价」的错误归因", async () => {
    const el = await renderQuota(DISARMED);

    expect(el.textContent).not.toContain("今日金额");
    expect(el.textContent).not.toContain("需配置模型单价");
  });

  it("闸门启用：画进度条并显示 已用/上限 与百分比", async () => {
    const el = await renderQuota({
      ...DISARMED,
      any_enforced: true,
      project_daily_tokens: { project_id: "P1", used: 243752, limit: 250000, enforced: true },
    });

    expect(el.textContent).toContain("全局硬额度");
    expect(el.textContent).toContain("243,752 / 250,000 token · 98%");
    expect(el.querySelectorAll('[role="progressbar"]').length).toBe(1);
  });

  it("fail-closed：载荷缺 enforced/any_enforced 但 limit 是真数字时，仍按已武装渲染", async () => {
    // 旧后端或下钻保留的旧 quota 就是这个形状。若拿 enforced 当判据，这里会渲染成
    // 「未设限」，在闸门已启用且用满 90% 时向作者谎报没有上限。
    const el = await renderQuota({
      period_timezone: "UTC",
      daily_tokens: { used: 900000, limit: 1000000 },
    });

    expect(el.textContent).toContain("全局硬额度");
    expect(el.textContent).toContain("900,000 / 1,000,000 token · 90%");
    expect(el.textContent).not.toContain("未设限");
    expect(el.textContent).not.toContain("当前未设任何硬额度");
    expect(el.querySelectorAll('[role="progressbar"]').length).toBe(1);
  });

  it("金额闸门单独启用：该行出现，并按 4 位小数显示", async () => {
    const el = await renderQuota({
      ...DISARMED,
      daily_cost_usd: { used: 1.5, limit: 10, enforced: true },
    });

    expect(el.textContent).toContain("今日金额");
    expect(el.textContent).toContain("1.5000 / 10.0000 USD");
    // 其余闸门仍关闭，所以金额未启用的补充说明不该出现
    expect(el.textContent).not.toContain("金额上限未启用");
  });

  it("其他闸门启用而金额闸门未启用：补充说明指向正确的环境变量", async () => {
    const el = await renderQuota({
      ...DISARMED,
      daily_tokens: { used: 10, limit: 100, enforced: true },
    });

    expect(el.textContent).toContain("金额上限未启用");
    expect(el.textContent).toContain("NOVEL_SYSTEM_LLM_DAILY_COST_LIMIT_USD");
  });
});
