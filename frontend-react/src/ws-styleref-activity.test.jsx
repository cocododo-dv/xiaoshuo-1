// 参考书活动面板（2026-09-15）：风格参考模块所有耗时操作共用一张活动表 + 一个 /activity 轮询。
//   (a) srActivityView：各 kind 的百分比与文案（导入契约在 ws-styleref-import.test.jsx 里钉住）；
//   (b) srActivityApply：服务端快照合并、owned 条目由发起方掌握终态、running→终态只触发一次刷新；
//   (c) 轮询：有在跑的持续拉 /activity，空了自动停；
//   (d) 面板：抽取可取消，取消打 cancel 端点；
//   (e) srSynthesize / srPreviewSamples：按幂等键登记条目、失败原因写进条目、预览逐张点亮；
//   (f) 总览的「进行中」块与回测四路的逐路点亮（纯函数）。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(), apiPost: vi.fn(), apiPatch: vi.fn(), apiDelete: vi.fn(),
  buildUrl: (path) => path,
  getOperatorRef: () => "operator",
  getRemoteAccessToken: () => null,
}));
vi.mock("./ws-works.jsx", () => ({
  WsWorks: { active: () => ({ id: "prj-main", title: "北岸手记" }), activeId: () => "prj-main" },
}));
vi.mock("./ws-review.jsx", () => ({ rvPush: vi.fn() }));

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const T = { timeout: 5000, interval: 25 };
const mounted = [];

const BOOK = { id: "bk1", real: true, title: "参考书", author: "作者", chars: 120000, color: "crimson", status: "ready", run: "已导入" };

function serverItem(overrides = {}) {
  return {
    key: "run:run9", kind: "extract", kind_label: "抽取", status: "running", title: "参考书", book_id: "bk1", target_id: "run9",
    phase: "scene", phase_label: "场景层", percent: 30, steps: { done: 5, total: 16, label: "对话" }, llm_calls: 7, retries: 1,
    started_at: "2026-09-15T08:00:00Z", updated_at: "2026-09-15T08:03:00Z", elapsed_seconds: 200, eta_seconds: 330,
    error: null, result: null, cancellable: true, retryable: false,
    ...overrides,
  };
}

async function load({ activity = [] } = {}) {
  const client = await import("./lib/client.js");
  const state = { activity };
  client.apiGet.mockImplementation((url) => {
    if (url === "/api/v2/style-reference/activity") return Promise.resolve({ items: state.activity });
    if (url === "/api/v2/style-reference/books") return Promise.resolve({ books: [] });
    if (url === "/api/v2/style-reference/books/bk1") return Promise.resolve({ book: { book_id: "bk1", title: "参考书", stats_json: {} } });
    if (url === "/api/v2/style-reference/books/bk1/runs") return Promise.resolve({ runs: [] });
    if (url.startsWith("/api/v2/style-reference/profiles?book_id=")) return Promise.resolve({ profiles: [] });
    return Promise.resolve({});
  });
  client.apiPost.mockResolvedValue({});
  const mod = await import("./ws-styleref.jsx");
  return { mod, client, state };
}

async function render(node) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(node));
  return host;
}
const click = (node) => act(async () => node.dispatchEvent(new MouseEvent("click", { bubbles: true })));
const urls = (client) => client.apiGet.mock.calls.map((c) => c[0]);

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  vi.spyOn(window, "alert").mockImplementation(() => {});
});

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/* ---------------- (a) 文案 ---------------- */
describe("srActivityView", () => {
  it("抽取：层 · 当前子维 · 第 n/N 维 · 已用 · 预计 · 模型调用（含补抽）", async () => {
    const { mod } = await load();
    const now = 100_000;
    const running = mod.srActivityView({ kind: "extract", status: "running", startedAt: now - 200_000, server: serverItem() }, now);
    expect(running.percent).toBe(30);
    expect(running.detail).toBe("场景层 · 对话 · 第 6/16 维 · 已用 3:20 · 预计还需 5:30 · 模型调用 7 次（含补抽 1 次）");
    const queued = mod.srActivityView({ kind: "extract", status: "running", startedAt: now, server: null, phase: "start" }, now);
    expect(queued.detail).toBe("启动中 · 已用 0:00");
    const done = mod.srActivityView({ kind: "extract", status: "succeeded", startedAt: now - 556_000, server: serverItem({ status: "succeeded", percent: 100, llm_calls: 17 }) }, now);
    expect(done).toEqual({ percent: 100, percentText: "100%", detail: "抽取完成 · 模型调用 17 次 · 用时 9:16" });
    expect(mod.srActivityView({ kind: "extract", status: "failed", startedAt: now, server: serverItem({ status: "failed" }), error: "心跳超时", errorCode: "STYLE_REFERENCE_RUN_INTERRUPTED" }, now).detail)
      .toBe("抽取失败：心跳超时");
    expect(mod.srActivityView({ kind: "extract", status: "cancelled", startedAt: now - 30_000, server: serverItem({ status: "cancelled", percent: 12 }) }, now))
      .toEqual({ percent: 12, percentText: "12%", detail: "已取消 · 用时 0:30" });
  });

  it("合成 / 重新分类 / 建索引 / 回测 / 预览：阶段 · 步骤 · 已用", async () => {
    const { mod } = await load();
    const now = 50_000;
    expect(mod.srActivityView({ kind: "synthesize", status: "running", startedAt: now - 65_000, server: { phase: "llm", phase_label: "模型合成 · 第 2 次", percent: 20, steps: { done: 0, total: null, label: null } } }, now).detail)
      .toBe("模型合成 · 第 2 次 · 已用 1:05");
    expect(mod.srActivityView({ kind: "synthesize", status: "running", startedAt: now, server: { phase: "index", phase_label: "建立 RAG 索引", percent: 89, steps: { done: 250, total: 1000, label: "段" } } }, now).detail)
      .toBe("建立 RAG 索引 250/1000 段 · 已用 0:00");
    expect(mod.srActivityView({ kind: "reclassify", status: "running", startedAt: now, server: { phase: "classify", phase_label: "段落分类", percent: 40, classify: { mode: "llm", batches_done: 2, batches_total: 4 }, eta_seconds: 30 } }, now).detail)
      .toBe("段落分类 2/4 批 · 已用 0:00 · 预计还需 0:30");
    expect(mod.srActivityView({ kind: "reclassify", status: "succeeded", startedAt: now - 10_000 }, now).detail).toBe("已重新分类 · 用时 0:10");
    expect(mod.srActivityView({ kind: "rag_index", status: "running", startedAt: now, server: { phase: "signatures", phase_label: "计算段落签名", percent: 40, steps: { done: 12000, total: 27000, label: "段" }, eta_seconds: 18 } }, now).detail)
      .toBe("计算段落签名 12000/27000 段 · 已用 0:00 · 预计还需 0:18");
    expect(mod.srActivityView({ kind: "validate", status: "succeeded", startedAt: now - 90_000, server: { status: "succeeded", result: { verdict: "partial" } } }, now).detail)
      .toBe("回测完成 · 结论 部分通过 · 用时 1:30");
    expect(mod.srActivityView({ kind: "preview", status: "running", startedAt: now, server: { phase_label: "生成 环境", steps: { done: 1, total: 3, label: "段" }, percent: 33 } }, now))
      .toEqual({ percent: 33, percentText: "33%", detail: "生成 环境 1/3 段 · 已用 0:00" });
    expect(mod.srActivityView({ kind: "synthesize", status: "failed", startedAt: now, error: "模型调用失败", errorCode: "STYLE_REFERENCE_SYNTHESIZE_FAILED" }, now).detail)
      .toBe("合成画像失败：模型调用失败");
  });
});

/* ---------------- (b) 合并 ---------------- */
describe("srActivityApply", () => {
  it("收下新条目、更新快照，running→终态只触发一次 sr:activity-finished；首次看见的终态不触发", async () => {
    const { mod } = await load();
    const finished = [];
    const onFinished = (e) => finished.push(e.detail.key);
    window.addEventListener("sr:activity-finished", onFinished);
    try {
      mod.srActivityApply([serverItem()]);
      expect(mod.srActivityEntries()[0]).toMatchObject({ key: "run:run9", kind: "extract", status: "running", bookId: "bk1", targetId: "run9", title: "参考书" });
      expect(mod.srActivityFor("bk1", "extract").key).toBe("run:run9");
      expect(mod.srActivityFor("bk1", "synthesize")).toBeNull();
      mod.srActivityApply([serverItem({ percent: 60, steps: { done: 10, total: 16, label: "价值观" } })]);
      expect(mod.srActivityEntries()[0].server.percent).toBe(60);
      expect(finished).toEqual([]);
      mod.srActivityApply([serverItem({ status: "succeeded", percent: 100 })]);
      await vi.waitFor(() => expect(finished).toEqual(["run:run9"]), T);
      mod.srActivityApply([serverItem({ status: "succeeded", percent: 100 })]);
      // 首次看见即终态的条目（刷新页面后才看到）：登记但不触发刷新
      mod.srActivityApply([serverItem({ key: "run:old", target_id: "old", status: "succeeded", percent: 100 })]);
      await new Promise((r) => setTimeout(r, 30));
      expect(finished).toEqual(["run:run9"]);
      expect(mod.srActivityFor("bk1", "extract")).toBeNull();
    } finally {
      window.removeEventListener("sr:activity-finished", onFinished);
    }
  });

  it("owned 条目（本页 await 着 POST 的合成）只补服务端阶段，状态由 POST 结果决定", async () => {
    const { mod } = await load();
    mod.srActivitySet("sr-synth-x", { kind: "synthesize", title: "参考书", bookId: "bk1", status: "running", owned: true, local: true, server: null });
    mod.srActivityApply([{ key: "sr-synth-x", kind: "synthesize", status: "running", phase: "llm", phase_label: "模型合成", percent: 20 }]);
    expect(mod.srActivityEntries()[0]).toMatchObject({ status: "running", owned: true, server: { phase_label: "模型合成" } });
    // 服务端快照先说成功也不改本地状态（POST 还没回来）
    mod.srActivityApply([{ key: "sr-synth-x", kind: "synthesize", status: "succeeded", percent: 100 }]);
    expect(mod.srActivityEntries()[0].status).toBe("running");
    mod.srActivitySet("sr-synth-x", { status: "succeeded", percent: 100 });
    mod.srActivityApply([{ key: "sr-synth-x", kind: "synthesize", status: "running", percent: 50 }]);
    expect(mod.srActivityEntries()[0].status).toBe("succeeded");
  });
});

/* ---------------- (c) 轮询 ---------------- */
describe("活动清单轮询", () => {
  it("有在跑的持续拉 /activity；条目到终态后自动停", async () => {
    vi.useFakeTimers();
    const { mod, client, state } = await load({ activity: [serverItem()] });
    mod.srActivityStart();
    await vi.advanceTimersByTimeAsync(10);
    expect(urls(client).filter((u) => u.endsWith("/activity"))).toHaveLength(1);
    expect(mod.srActivityEntries()[0].status).toBe("running");
    await vi.advanceTimersByTimeAsync(1600);
    expect(urls(client).filter((u) => u.endsWith("/activity"))).toHaveLength(2);
    state.activity = [serverItem({ status: "succeeded", percent: 100 })];
    await vi.advanceTimersByTimeAsync(1600);
    expect(mod.srActivityEntries()[0].status).toBe("succeeded");
    const settled = urls(client).filter((u) => u.endsWith("/activity")).length;
    await vi.advanceTimersByTimeAsync(5000);
    expect(urls(client).filter((u) => u.endsWith("/activity"))).toHaveLength(settled);
    mod.srActivityStop();
  });

  it("空清单：拉一次就停；srActivityStart 重复调用不叠加计时器", async () => {
    vi.useFakeTimers();
    const { mod, client } = await load({ activity: [] });
    mod.srActivityStart();
    mod.srActivityStart();
    await vi.advanceTimersByTimeAsync(10);
    await vi.advanceTimersByTimeAsync(5000);
    expect(urls(client).filter((u) => u.endsWith("/activity"))).toHaveLength(1);
  });
});

/* ---------------- (d) 面板 ---------------- */
describe("SrActivityPanel", () => {
  it("在跑的抽取带「取消」，点击打 cancel 端点；终态给「关闭」", async () => {
    const { mod, client } = await load();
    mod.srActivityApply([serverItem()]);
    const host = await render(<mod.SrActivityPanel onOpenBook={vi.fn()} />);
    const item = host.querySelector('[data-import-key="run:run9"]');
    expect(item.getAttribute("data-activity-kind")).toBe("extract");
    expect(item.textContent).toContain("抽取");
    expect(item.textContent).toContain("《参考书》");
    expect(host.querySelector('[role="progressbar"]').getAttribute("aria-valuenow")).toBe("30");
    const cancel = host.querySelector('[data-testid="sr-activity-cancel"]');
    expect(cancel).toBeTruthy();
    await click(cancel);
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v2/style-reference/runs/run9/cancel", {}), T);
    await act(async () => { mod.srActivityApply([serverItem({ status: "cancelled", percent: 30 })]); });
    await vi.waitFor(() => expect(host.querySelector('[data-import-status="cancelled"]')).toBeTruthy(), T);
    expect(host.querySelector('[data-testid="sr-activity-cancel"]')).toBeNull();
    expect(host.textContent).toContain("已取消");
    const close = Array.from(host.querySelectorAll("button")).find((b) => b.textContent === "关闭");
    await click(close);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-import-progress"]')).toBeNull(), T);
    mod.srActivityStop();
  });
});

describe("分类任务的面板控制（严格 LLM 的后台分类）", () => {
  it("在跑的分类可取消（打 classification/cancel），失败 / 取消的可「继续分类」（resume=true）", async () => {
    const { mod, client } = await load();
    const item = (overrides = {}) => ({
      key: "sr-import-x", kind: "import", kind_label: "导入", status: "running", title: "参考书", book_id: "bk1", target_id: "bk1",
      phase: "classify", phase_label: "段落分类", percent: 26, steps: { done: 3, total: 10, label: "批" },
      classify: { mode: "llm", batches_done: 3, batches_total: 10, llm_calls: 3 }, llm_calls: 3,
      started_at: "2026-09-15T08:00:00Z", elapsed_seconds: 60, eta_seconds: 140, error: null, result: null,
      cancellable: true, resumable: false, retryable: false, ...overrides,
    });
    mod.srActivityApply([item()]);
    const host = await render(<mod.SrActivityPanel onOpenBook={vi.fn()} />);
    expect(host.textContent).toContain("段落分类 3/10 批");
    const cancel = host.querySelector('[data-testid="sr-activity-cancel"]');
    expect(cancel).toBeTruthy();
    await click(cancel);
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith("/api/v2/style-reference/books/bk1/classification/cancel", {}), T);

    await act(async () => { mod.srActivityApply([item({ status: "cancelled", cancellable: false, resumable: true, retryable: true, phase: "cancelled", phase_label: "已取消", error: { code: "STYLE_REFERENCE_IMPORT_CANCELLED", message: "cancelled" } })]); });
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-activity-resume"]')).toBeTruthy(), T);
    expect(host.textContent).toContain("已取消");
    await click(host.querySelector('[data-testid="sr-activity-resume"]'));
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([u, body]) => u === "/api/v2/style-reference/books/bk1/reclassify" && body && body.resume === true)).toBe(true), T);
    const resumeCall = client.apiPost.mock.calls.find(([u, body]) => u.endsWith("/reclassify") && body && body.resume === true);
    expect(resumeCall[2]).toEqual({ idempotencyKey: expect.stringMatching(/^sr-reclassify-/) });
    const resumed = mod.srActivityEntries().find((e) => e.kind === "reclassify");
    expect(resumed).toMatchObject({ status: "running", owned: false, bookId: "bk1" });
    mod.srActivityStop();
  });

  it("书库状态映射：ingesting → 分类中，cancelling → 取消中，failed → 未完成；矩阵空态按运行中的操作说话", async () => {
    const { mod } = await load();
    expect(mod.srMapStatus("ingesting")).toBe("importing");
    expect(mod.srMapStatus("cancelling")).toBe("cancelling");
    expect(mod.srMapStatus("failed")).toBe("failed");
    expect(mod.srMapStatus("ready")).toBe("ready");
    expect(mod.srRunLabel("ingesting")).toBe("段落分类中");
    expect(mod.srRunLabel("failed")).toBe("分类未完成");
    const book = { id: "bk1", real: true, rawStatus: "ready" };
    expect(mod.srMatrixEmptyHint(book)).toContain("开始抽取");
    expect(mod.srMatrixEmptyHint({ ...book, rawStatus: "failed" })).toContain("继续分类");
    expect(mod.srMatrixEmptyHint({ ...book, rawStatus: "ingesting" })).toContain("段落分类还在进行");
    mod.srActivityApply([serverItem()]);
    expect(mod.srMatrixEmptyHint(book)).toContain("正在抽取");
    expect(mod.srMatrixEmptyHint(book)).toContain("场景层");
    expect(mod.srRestClassifierLabel({ rest_classifier: "fast_llm" })).toBe("快模型逐批");
    expect(mod.srRestClassifierLabel({ rest_classifier: null })).toContain("无余段");
    expect(mod.srRestClassifierLabel({ rest_classifier: "heuristic", heuristic_classified_paragraphs: 26416 })).toContain("旧版导入");
  });
});

/* ---------------- (e) 合成 / 预览 ---------------- */
describe("srSynthesize 与 srPreviewSamples", () => {
  it("合成：按 sr-synth-* 幂等键登记 synthesize 条目，成功后 succeeded 并重读深层数据", async () => {
    const { mod, client } = await load();
    client.apiPost.mockImplementation((url) => (url.endsWith("/synthesize") ? Promise.resolve({ profile: { profile_id: "p1" } }) : Promise.resolve({})));
    const r = await mod.srSynthesize("run1", "bk1");
    expect(r.profile.profile_id).toBe("p1");
    const call = client.apiPost.mock.calls.find(([u]) => u.endsWith("/synthesize"));
    expect(call[2]).toEqual({ idempotencyKey: expect.stringMatching(/^sr-synth-/) });
    const entry = mod.srActivityEntries().find((e) => e.kind === "synthesize");
    expect(entry).toMatchObject({ key: call[2].idempotencyKey, status: "succeeded", bookId: "bk1", targetId: "run1", owned: true });
    expect(urls(client)).toContain("/api/v2/style-reference/books/bk1");
    mod.srActivityStop();
  });

  it("合成失败：条目标 failed 并带 code；前端超时则交还给活动清单继续跟", async () => {
    const { mod, client } = await load();
    const err = Object.assign(new Error("模型调用失败"), { code: "STYLE_REFERENCE_SYNTHESIZE_FAILED" });
    client.apiPost.mockRejectedValueOnce(err);
    await expect(mod.srSynthesize("run1", "bk1")).rejects.toBe(err);
    let entry = mod.srActivityEntries().find((e) => e.kind === "synthesize");
    expect(entry.status).toBe("failed");
    // 作者读到的是那句话；错误代码另存（界面只放进悬停提示）
    expect(entry.error).toBe("模型调用失败");
    expect(entry.errorCode).toBe("STYLE_REFERENCE_SYNTHESIZE_FAILED");
    const timeout = Object.assign(new Error("请求超时，请稍后重试。"), { code: "REQUEST_TIMEOUT" });
    client.apiPost.mockRejectedValueOnce(timeout);
    await expect(mod.srSynthesize("run1", "bk1")).rejects.toBe(timeout);
    entry = mod.srActivityEntries().find((e) => e.kind === "synthesize" && e.status === "running");
    expect(entry).toBeTruthy();
    expect(entry.owned).toBe(false);
    mod.srActivityStop();
  });

  it("预览：按段型三次串行请求，每张回来就 onSample，条目记 n/3", async () => {
    const { mod, client } = await load();
    const seen = [];
    client.apiPost.mockImplementation((url, body) => Promise.resolve({ samples: [{ paragraph_type: body.paragraph_types[0], sample_text: "x", verdict: "pass" }] }));
    const r = await mod.srPreviewSamples("p1", { onSample: (list) => seen.push(list.map((s) => s.paragraph_type)) });
    expect(r.samples.map((s) => s.paragraph_type)).toEqual(["dialogue", "description_env", "psychology"]);
    expect(seen).toEqual([["dialogue"], ["dialogue", "description_env"], ["dialogue", "description_env", "psychology"]]);
    const bodies = client.apiPost.mock.calls.filter(([u]) => u.endsWith("/preview")).map(([, b]) => b);
    expect(bodies).toEqual([{ paragraph_types: ["dialogue"] }, { paragraph_types: ["description_env"] }, { paragraph_types: ["psychology"] }]);
    const entry = mod.srActivityEntries().find((e) => e.kind === "preview");
    expect(entry).toMatchObject({ status: "succeeded", server: { steps: { done: 3, total: 3 } } });
    mod.srActivityStop();
  });
});

/* ---------------- (f) 总览进行中块 + 回测逐路点亮 ---------------- */
describe("总览与回测", () => {
  it("总览：有在跑的抽取时显示进行中块，而不是缓存里上一条 run 的「抽取完成」", async () => {
    const { mod } = await load();
    mod.srActivityApply([serverItem()]);
    const host = await render(<mod.SrOverview book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-overview-live"]')).toBeTruthy(), T);
    expect(host.textContent).toContain("抽取中 30%");
    expect(host.textContent).toContain("场景层 · 对话 · 第 6/16 维");
    expect(host.textContent).not.toContain("抽取完成");
    await act(async () => { mod.srActivityApply([serverItem({ status: "succeeded", percent: 100 })]); });
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-overview-live"]')).toBeNull(), T);
    mod.srActivityStop();
  });

  it("srvRunningRows：本地三路先点亮，语义路等 semantic_json，禁忌语义等 verdict", async () => {
    const { srvRunningRows } = await import("./ws-styleref-val.jsx");
    const ids = (rows) => rows.filter((r) => r.done).map((r) => r.id);
    expect(ids(srvRunningRows(null, "async_full"))).toEqual([]);
    expect(srvRunningRows(null, "async_full").map((r) => r.id)).toEqual(["quant", "semantic", "plagiarism", "forbidden"]);
    expect(ids(srvRunningRows({ status: "running", plagiarism_json: { passed: true }, quantitative_json: [], semantic_json: [] }, "async_full"))).toEqual(["quant", "plagiarism"]);
    expect(ids(srvRunningRows({ status: "running", plagiarism_json: { passed: false }, semantic_json: [{ dimension: "rhythm", score: 7 }] }, "async_full"))).toEqual(["quant", "semantic", "plagiarism"]);
    expect(ids(srvRunningRows({ status: "done", verdict: "pass", plagiarism_json: { passed: true }, semantic_json: [] }, "async_full"))).toEqual(["quant", "semantic", "plagiarism", "forbidden"]);
    expect(srvRunningRows({ plagiarism_json: { passed: true } }, "sync_only").map((r) => r.id)).toEqual(["quant", "plagiarism", "forbidden"]);
    expect(ids(srvRunningRows({ plagiarism_json: { passed: true } }, "sync_only"))).toEqual(["quant", "plagiarism", "forbidden"]);
  });
});
