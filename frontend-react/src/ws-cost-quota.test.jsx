// 成本看板「全局用量」区块：2026-09-30 重评 R3（批准 #3a）删了六道全局额度闸，这里只剩读数——
// 报今日 / 本月 / 本作品今日的 token、今日请求与并发，不画进度条、不说「未设限」「全局硬额度」，
// 不再指点作者去设已经退役的环境变量；「今日金额」也不再出现（它按另一套环境变量单价算）。
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

let root;
let host;

async function renderQuota(quota) {
  const { QuotaSection } = await import("./ws-cost-parts.jsx");
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root.render(<QuotaSection quota={quota} />));
  return host;
}

async function unmountQuota() {
  if (root) await act(async () => root.unmount());
  if (host) host.remove();
  root = null;
  host = null;
}

afterEach(unmountQuota);

// 后端 llm_usage_readings 的真实形状：每格 {used, limit: null, enforced: false}
const READINGS = {
  period_timezone: "UTC",
  daily_tokens: { used: 257203, limit: null, enforced: false },
  monthly_tokens: { used: 565854, limit: null, enforced: false },
  project_daily_tokens: { project_id: "P1", used: 243752, limit: null, enforced: false },
  daily_requests: { used: 73, limit: null, enforced: false },
  concurrent_requests: { used: 2, limit: null, enforced: false },
};

describe("成本看板 · 全局用量", () => {
  it("只报读数：五格用量都在，不画进度条，不说上限", async () => {
    const el = await renderQuota(READINGS);

    expect(el.querySelector("h3").textContent).toContain("全局用量");
    expect(el.textContent).toContain("今日总量257,203 token");
    expect(el.textContent).toContain("本月总量565,854 token");
    expect(el.textContent).toContain("本项目今日243,752 token");
    expect(el.textContent).toContain("今日请求73 次");
    expect(el.textContent).toContain("并发请求2 路");
    expect(el.textContent).toContain("按 UTC 计日");
    expect(el.textContent).toContain("不会拦下生成");
    expect(el.querySelectorAll('[role="progressbar"]').length).toBe(0);
    expect(el.textContent).not.toContain("未设限");
    expect(el.textContent).not.toContain("全局硬额度");
  });

  it("不再指点作者去设退役的环境变量，也没有「怎么设上限」", async () => {
    const el = await renderQuota(READINGS);

    expect(el.textContent).not.toContain("NOVEL_SYSTEM_");
    expect(el.textContent).not.toContain("怎么设上限");
    expect(el.querySelector("details")).toBeNull();
  });

  it("旧后端留下的额度字段（limit 是数、daily_cost_usd）一律不当上限、不显示金额", async () => {
    // 部署交替期或下钻时留下的旧载荷：闸已经删了，这些数不能再被画成「已用 / 上限」
    const el = await renderQuota({
      ...READINGS,
      any_enforced: true,
      daily_tokens: { used: 900000, limit: 1000000, enforced: true },
      daily_cost_usd: { used: 1.5, limit: 10, enforced: true },
    });

    expect(el.textContent).toContain("今日总量900,000 token");
    expect(el.textContent).not.toContain("1,000,000");
    expect(el.textContent).not.toContain("今日金额");
    expect(el.textContent).not.toContain("USD");
    expect(el.querySelectorAll('[role="progressbar"]').length).toBe(0);
  });

  it("结算时区跟着后端说；没有读数就整块不画", async () => {
    const el = await renderQuota({ ...READINGS, period_timezone: "Asia/Shanghai" });
    expect(el.textContent).toContain("按 Asia/Shanghai 计日");
    await unmountQuota();

    expect((await renderQuota(null)).textContent).toBe("");
    await unmountQuota();

    expect((await renderQuota({ period_timezone: "UTC" })).textContent).toBe("");
  });
});
