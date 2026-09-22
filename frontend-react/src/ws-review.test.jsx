// WsReview store 层单测：投递 / 处理（乐观移除 + resolve 端点）/ 处理失败告警。
// 断言取向同 ws-catalog.test.jsx：只断可观测结果 + 非去重写动词；waitFor 给足超时耐负载。
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter, DEFAULT_REVIEW_CARD } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));

const T = { timeout: 5000, interval: 25 };

async function settleActive() {
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe("prj-main"), T);
}

async function loadReview(opts) {
  const client = await import("./lib/client.js");
  installApiRouter(client, opts);
  const mod = await import("./ws-review.jsx");
  await settleActive(); // 投递/处理 payload 需要真实 project_id
  return { mod, client };
}

describe("WsReview（收件箱乐观处理 + 失败告警）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    vi.spyOn(window, "alert").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("rvPush 投递待办到后端 review-items 端点（带当前作品 id）", async () => {
    const { mod, client } = await loadReview();
    client.apiPost.mockClear();

    mod.rvPush({ title: "新批注：第 5 章节奏", kind: "note" });

    await vi.waitFor(() =>
      expect(client.apiPost).toHaveBeenCalledWith(
        "/api/v1/review-items",
        expect.objectContaining({ title: "新批注：第 5 章节奏", project_id: "prj-main" })
      ), T);
  });

  it("rvMarkResolved 乐观移除并 POST resolve", async () => {
    const { mod, client } = await loadReview({ reviewOpen: [DEFAULT_REVIEW_CARD] });
    // 等收件箱从后端装载（work-changed 去抖后拉取）
    await vi.waitFor(() => expect(mod.rvOpenItems().length).toBeGreaterThan(0), T);
    client.apiPost.mockClear();

    mod.rvMarkResolved(["rv1"]);

    // 乐观：立即从 open 列表移除
    expect(mod.rvOpenItems().some((i) => i.id === "rv1")).toBe(false);

    // resolve 的 apiPost 在循环里逐条发，不参与 fetch 去重
    await vi.waitFor(() =>
      expect(client.apiPost).toHaveBeenCalledWith(
        "/api/v1/review-items/rv1/resolve",
        expect.objectContaining({ project_id: "prj-main" })
      ), T);
  });

  it("resolve 端点失败时告警", async () => {
    const { mod, client } = await loadReview({ reviewOpen: [DEFAULT_REVIEW_CARD] });
    await vi.waitFor(() => expect(mod.rvOpenItems().length).toBeGreaterThan(0), T);
    client.apiPost.mockRejectedValueOnce(new Error("resolve failed"));

    mod.rvMarkResolved(["rv1"]);

    await vi.waitFor(() => expect(window.alert).toHaveBeenCalled(), T);
  });

  it("旧待办迁移失败时不落完成标记，并以稳定去重键安全重试", async () => {
    const { mod, client } = await loadReview();
    const migrationProject = "prj-migration-test";
    const legacyKey = `ws_review_v1::${migrationProject}`;
    const migratedKey = `ws_review_migrated_v1::${migrationProject}`;
    window.localStorage.removeItem(migratedKey);
    window.localStorage.setItem(legacyKey, JSON.stringify({
      custom: [{ id: "legacy-1", title: "未迁移批注", kind: "note" }],
    }));
    client.apiPost.mockClear();
    client.apiPost.mockRejectedValueOnce(new Error("offline"));

    await expect(mod.rvMigrateLegacy(migrationProject)).resolves.toBe(false);
    expect(window.localStorage.getItem(migratedKey)).toBeNull();
    expect(window.localStorage.getItem(legacyKey)).not.toBeNull();

    await expect(mod.rvMigrateLegacy(migrationProject)).resolves.toBe(true);
    expect(window.localStorage.getItem(migratedKey)).not.toBeNull();
    const writes = client.apiPost.mock.calls.filter(([url]) => url === "/api/v1/review-items");
    expect(writes).toHaveLength(2);
    expect(writes[0][1].dedupe_key).toBe("legacy-review:0:legacy-1");
    expect(writes[1][1].dedupe_key).toBe(writes[0][1].dedupe_key);
  });
});

/* ---------- 视图：旧表行可读、没有动作的卡也能用鼠标处理、键盘快捷键不吞按钮 ---------- */
import React, { act } from "react";
import { createRoot } from "react-dom/client";

const PREF_CARD = {
  id: "rv-pref",
  kind: "decision",
  priority: 2,
  title: JSON.stringify({
    manual_edit_count: 2,
    manual_edit_observations: [{ object_id: "X_CH01_SC01", labels: ["prefer_expansion"] }],
    safe_preference_hints: ["prefer_expansion", "prefer_longer_paragraphs"],
  }),
  where: "",
  source: "author_preference_profile",
  occurred_at: "2026-06-08T00:00:00Z",
  live: false,
  actions: [],
  legacy: true,
};

describe("WsReview 视图", () => {
  let root;
  let host;
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  });
  afterEach(async () => {
    if (root) await act(async () => root.unmount());
    if (host) host.remove();
    root = null;
    host = null;
    vi.restoreAllMocks();
  });

  /* strict：包一层 React.StrictMode（npm run dev 的样子：更新函数会被跑两遍） */
  async function mount(cards, { strict = false } = {}) {
    const { mod, client } = await loadReview({ reviewOpen: cards });
    await vi.waitFor(() => expect(mod.rvOpenItems().length).toBe(cards.length), T);
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    const view = <mod.WsReview go={vi.fn()} />;
    await act(async () => root.render(strict ? <React.StrictMode>{view}</React.StrictMode> : view));
    return { mod, client };
  }

  it("旧表的写作偏好行：标题是可读的倾向，不是原始 JSON；来源是中文；给出「知道了 / 稍后」", async () => {
    const { mod, client } = await mount([PREF_CARD]);
    const card = host.querySelector(".rv-item");
    expect(card.textContent).toContain("写作偏好：偏好扩写、偏好更长的段落");
    expect(card.textContent).not.toContain("safe_preference_hints");
    expect(card.textContent).not.toContain("author_preference_profile");
    expect(card.textContent).toContain("来自写作偏好");
    const labels = [...card.querySelectorAll(".rv-actions button")].map((b) => b.textContent);
    expect(labels).toEqual(["知道了", "稍后"]);
    expect(mod.rvReady()).toBe(true);

    client.apiPost.mockClear();
    await act(async () => { card.querySelector(".rv-actions button").click(); });
    await act(async () => { await new Promise((r) => setTimeout(r, 350)); });
    // 补出来的「知道了」不是卡上的动作：resolve 不带 action_index
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/review-items/rv-pref/resolve", { project_id: "prj-main" }), T);
  });

  it("只有一个优先级段时不画段头", async () => {
    await mount([PREF_CARD]);
    expect(host.querySelector(".rv-band")).toBeNull();
  });

  it("焦点在按钮上时回车 / 空格不被收件箱吞掉；E 只处理焦点所在的那张卡", async () => {
    const { client } = await mount([DEFAULT_REVIEW_CARD, { ...PREF_CARD, id: "rv2", priority: 1 }]);
    const chip = [...host.querySelectorAll('[role="radio"]')].find((b) => b.textContent.includes("决策"));
    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    await act(async () => { chip.dispatchEvent(enter); });
    expect(enter.defaultPrevented).toBe(false);

    // 焦点不在任何卡上：E 不处理任何东西（以前默认处理第一张）
    client.apiPost.mockClear();
    const stray = new KeyboardEvent("keydown", { key: "e", bubbles: true, cancelable: true });
    await act(async () => { chip.dispatchEvent(stray); });
    await act(async () => { await new Promise((r) => setTimeout(r, 350)); });
    expect(client.apiPost).not.toHaveBeenCalled();

    // J 把焦点移到第一张卡的标题上
    await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "j", bubbles: true, cancelable: true })); });
    const rows = host.querySelectorAll(".rv-row");
    expect(document.activeElement).toBe(rows[0]);
    await act(async () => { rows[0].dispatchEvent(new KeyboardEvent("keydown", { key: "j", bubbles: true, cancelable: true })); });
    expect(document.activeElement).toBe(rows[1]);
  });

  it("目录高频变化（自动保存）不再每次重拉收件箱：20 秒内合并成一次", async () => {
    const { client } = await mount([DEFAULT_REVIEW_CARD]);
    const catalog = await import("./ws-catalog.jsx");
    const reviewGets = () => client.apiGet.mock.calls.filter(([url]) => String(url).startsWith("/api/v1/review-items?state=open")).length;
    vi.useFakeTimers();
    try {
      const before = reviewGets();
      for (let i = 0; i < 5; i += 1) catalog.WsCatalog.recordSceneWords("ch01s1", 10 + i);
      await act(async () => { vi.advanceTimersByTime(1000); });
      expect(reviewGets()).toBe(before);
      await act(async () => { vi.advanceTimersByTime(20_000); });
      expect(reviewGets()).toBe(before + 1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("开发模式（StrictMode）下稍后一张卡，「稍后处理」只数 1；恢复后列表里也只有一张", async () => {
    const { client } = await mount([PREF_CARD], { strict: true });
    // 稍后 / 恢复的请求一直挂着：断言的是乐观移动本身，不让刷新回来的列表把它盖掉
    client.apiPost.mockImplementation(() => new Promise(() => {}));
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});

    const later = [...host.querySelectorAll(".rv-actions button")].find((b) => b.textContent === "稍后");
    await act(async () => { later.click(); });
    await act(async () => { await new Promise((r) => setTimeout(r, 350)); });
    expect(host.querySelector(".rv-snoozed-n").textContent).toBe("1");
    expect(host.querySelectorAll(".rv-item")).toHaveLength(0);

    await act(async () => { host.querySelector(".rv-snoozed-head").click(); });
    const restore = [...host.querySelectorAll(".rv-snoozed-item button")].find((b) => b.textContent.includes("恢复"));
    await act(async () => { restore.click(); });
    expect(host.querySelectorAll(".rv-item")).toHaveLength(1);
    expect(host.querySelector(".rv-snoozed")).toBeNull();
    const dupes = errors.mock.calls.filter((args) => String(args[0]).includes("same key"));
    expect(dupes).toHaveLength(0);
  });

  it("收件箱读不出来时说清楚并给重试，不无限停在「正在读取」", async () => {
    const client = await import("./lib/client.js");
    installApiRouter(client, { reviewOpen: [DEFAULT_REVIEW_CARD] });
    const route = client.apiGet.getMockImplementation();
    // 收件箱请求先挂着，页面已经显示「正在读取」之后再失败：失败必须自己把页面叫醒
    let offline = true;
    const pending = [];
    client.apiGet.mockImplementation((url, ...rest) => (offline && String(url).includes("/review-items")
      ? new Promise((resolve, reject) => { pending.push(reject); })
      : route(url, ...rest)));
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const mod = await import("./ws-review.jsx");
    await settleActive();
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => root.render(<mod.WsReview go={vi.fn()} />));
    await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0), T);
    expect(host.textContent).toContain("正在读取待办");

    await act(async () => { pending.splice(0).forEach((reject) => reject(new Error("Failed to fetch"))); });
    await vi.waitFor(() => expect(host.querySelector('[data-testid="review-load-error"]')).not.toBeNull(), T);
    const notice = host.querySelector('[data-testid="review-load-error"]');
    expect(notice.getAttribute("role")).toBe("alert");
    expect(notice.textContent).toContain("待办暂时读不出来");
    expect(notice.textContent).not.toContain("Failed to fetch");
    expect(host.textContent).not.toContain("正在读取待办");
    expect(host.textContent).not.toContain("收件箱清空了");
    expect(mod.rvReady()).toBe(false);

    offline = false;
    await act(async () => { host.querySelector('[data-testid="review-load-retry"]').click(); });
    await vi.waitFor(() => expect(host.querySelector(".rv-item")).not.toBeNull(), T);
    expect(host.querySelector('[data-testid="review-load-error"]')).toBeNull();
    expect(mod.rvReady()).toBe(true);
  });
});
