// 风格参考 W7（style-imitation-v2 §2.W7）的前端行为契约：
//   (a) 抽取 run 到达终态 / 重新分类 / 删书 → 深层缓存强制失效并重读；
//   (b) 画像 stale / 有新 run / 未激活 → 「重新合成」可点并真的打 synthesize 端点；
//   (c) 强度读数只来自预览端点返回的 stats，没有 stats 就显示「预览中…」，没有本地公式；
//   (d) 任务卡只剩 scene_generation（后端 task-defaults 返回多项也只展示一张）；
//   (e) 维度选择器按画像 sub_dimensions 动态生成；同作用域已有 active 绑定时提示遮蔽。
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

const TASK_DEFAULTS = [
  { task_type: "project_init", default_strategy: "A", refresh_every_chars: 0 },
  { task_type: "scene_generation", default_strategy: "mixed", refresh_every_chars: 0 },
  { task_type: "fine_tuning", default_strategy: "B", refresh_every_chars: 0 },
  { task_type: "key_chapter", default_strategy: "C", refresh_every_chars: 0 },
];

const PREVIEW_STATS = {
  positive_lines: 3, forbidden_lines: 1, metric_lines: 1, voice_lines: 3,
  few_shot_windows: 4, few_shot_chars: 2400, rag_snippets: 0,
  total_prefix_chars: 4200, intensity_effective_total_chars: 1800, few_shot_k: 4,
};

const SUB_DIMS_TWO = {
  "language.sentence_structure": { confidence: "high", observation_count: 3, forbidden_pattern_count: 1, quote_count: 5 },
  "scene.dialogue": { confidence: "medium", observation_count: 2, forbidden_pattern_count: 0, quote_count: 3 },
};

/* 后端形状的深层数据夹具；可控：画像状态 / stale / 画像所属 run / 最新 run / 绑定 / sub_dimensions */
function fixture({ profileStatus = "draft", stale = false, profileRunId = "run1", latestRunId = "run1", bindings = [], subDims = SUB_DIMS_TWO, withProfile = true } = {}) {
  const runs = [{ run_id: "run1", book_id: "bk1", status: "done", finished_at: "2026-09-01T00:00:00Z" }];
  if (latestRunId !== "run1") runs.push({ run_id: latestRunId, book_id: "bk1", status: "done", finished_at: "2026-09-05T00:00:00Z" });
  return {
    book: {
      book_id: "bk1", title: "参考书", author_label: "作者", total_chars: 120000, status: "ready",
      stats_json: { input_assessment: { language: "high", narrative: "medium", scene: "high", theme: "skip" }, metrics: {}, paragraph_type_distribution: {} },
    },
    runs,
    findings: [
      { finding_id: "f1", sub_dimension: "language.sentence_structure", finding_kind: "observation", confidence: "high", statement: "短句为骨", evidence: [{ quote_text: "a" }, { quote_text: "b" }] },
      { finding_id: "f2", sub_dimension: "scene.dialogue", finding_kind: "forbidden_pattern", confidence: "medium", statement: "不写解释性对白", evidence: [{ quote_text: "c" }, { quote_text: "d" }] },
    ],
    profiles: withProfile ? [{
      profile_id: "p1", book_id: "bk1", run_id: profileRunId, title: "作者风格画像", status: profileStatus,
      profile_json: { sub_dimensions: subDims, qualitative_summary: "短句为骨，白描见长。", style_features: ["短句"], voice_signature: { habits: ["连接词多用却、便"] }, narrative_guidance: ["关键信息段首一次给出"] },
      coverage_json: stale ? { stale: true, stale_reason: "source_finding_membership_changed" } : { findings_count: 2, quotes_count: 4, sub_dim_count: 2 },
    }] : [],
    bindings,
  };
}

/* 活动清单（2026-09-15）：抽取 run 的终态经 GET …/activity 回到前端（不再逐 run 轮询 + alert）。 */
function activityItemsFor(runStatus) {
  const status = runStatus === "done" ? "succeeded" : runStatus;
  return [{
    key: "run:run1", kind: "extract", kind_label: "抽取", status, book_id: "bk1", target_id: "run1",
    phase: status === "succeeded" ? "done" : "failed", phase_label: status === "succeeded" ? "完成" : "失败",
    percent: status === "succeeded" ? 100 : 40, steps: null, llm_calls: 7, retries: 1,
    started_at: "2026-09-05T00:00:00Z", updated_at: "2026-09-05T00:09:00Z", elapsed_seconds: 540, eta_seconds: null,
    error: status === "failed" ? { code: "STYLE_REFERENCE_EXTRACTION_FAILED", message: "extraction failed" } : null,
    result: null, cancellable: false, retryable: status === "failed",
  }];
}

function installStyleRefRouter(client, fx, { previewStats = PREVIEW_STATS, runStatus = "done" } = {}) {
  client.apiGet.mockImplementation((url) => {
    if (url === "/api/v2/style-reference/activity") return Promise.resolve({ items: activityItemsFor(runStatus) });
    if (url === "/api/v2/style-reference/books") return Promise.resolve({ books: [] });
    if (url === "/api/v2/style-reference/books/bk1") return Promise.resolve({ book: fx.book });
    if (url === "/api/v2/style-reference/books/bk1/runs") return Promise.resolve({ runs: fx.runs });
    if (/^\/api\/v2\/style-reference\/runs\/[^/]+\/findings/.test(url)) return Promise.resolve({ findings: fx.findings });
    if (/^\/api\/v2\/style-reference\/runs\/[^/]+$/.test(url)) return Promise.resolve({ run: { run_id: url.split("/").pop(), book_id: "bk1", status: runStatus } });
    if (url.startsWith("/api/v2/style-reference/profiles?book_id=")) return Promise.resolve({ profiles: fx.profiles });
    if (/\/profiles\/p1\/bindings$/.test(url)) return Promise.resolve({ bindings: fx.bindings });
    if (/\/profiles\/p1\/banned-terms$/.test(url)) return Promise.resolve({ terms: [] });
    if (url === "/api/v2/style-reference/injection/task-defaults") return Promise.resolve({ tasks: TASK_DEFAULTS });
    if (/\/catalog(\?|$)/.test(url)) return Promise.resolve({ chapters: [] });
    if (/\/library(\?|$)/.test(url)) return Promise.resolve({ characters: [] });
    return Promise.resolve({});
  });
  client.apiPost.mockImplementation((url) => {
    if (/\/injection-preview$/.test(url)) {
      const body = {
        fragments: { positive_block: "[正向风格特征]\n- 短句为骨\n- 少用关联词", forbidden_block: "[禁忌]\n- 不写解释性对白", anti_plagiarism_block: "严禁复制原文。" },
        prefix: "x".repeat(4200),
      };
      if (previewStats) body.stats = previewStats;
      return Promise.resolve(body);
    }
    if (/\/synthesize$/.test(url)) return Promise.resolve({ profile: fx.profiles[0] || null });
    return Promise.resolve({});
  });
  client.apiDelete.mockResolvedValue({});
}

async function load(fx, routerOpts) {
  const client = await import("./lib/client.js");
  installStyleRefRouter(client, fx, routerOpts);
  const mod = await import("./ws-styleref.jsx");
  return { mod, client };
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
const postUrls = (client) => client.apiPost.mock.calls.map((c) => c[0]);

beforeEach(() => {
  vi.resetModules();
  window.localStorage.clear();
  vi.spyOn(window, "alert").mockImplementation(() => {});
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(async () => {
  while (mounted.length) {
    const { root, host } = mounted.pop();
    await act(async () => root.unmount());
    host.remove();
  }
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/* ---------------- 纯函数 ---------------- */
describe("W7 纯函数", () => {
  it("computeIntensityReadout：只消费 stats；缺失返回 null，不虚构", async () => {
    const { mod } = await load(fixture());
    expect(mod.computeIntensityReadout(null)).toBeNull();
    expect(mod.computeIntensityReadout(undefined)).toBeNull();
    expect(mod.computeIntensityReadout("x")).toBeNull();
    const r = mod.computeIntensityReadout(PREVIEW_STATS);
    expect(r.ruleLines).toBe(5);
    expect(r.voiceLines).toBe(3);
    expect(r.sampleWindows).toBe(4);
    expect(r.totalChars).toBe(4200);
    expect(r.text).toBe("规则 5 行 · 声音特征 3 行 · 样例 4 段 · 共 4200 字");
    // rag 片段也算样例段；非数字字段按 0 处理
    expect(mod.computeIntensityReadout({ few_shot_windows: 2, rag_snippets: 3, total_prefix_chars: "abc" }).sampleWindows).toBe(5);
    expect(mod.computeIntensityReadout({ few_shot_windows: 2, rag_snippets: 3, total_prefix_chars: "abc" }).totalChars).toBe(0);
  });

  it("buildDimOptions：优先画像 sub_dimensions；缺失回退 input_assessment 的 layer 级 skip；都没有则全可选", async () => {
    const { mod } = await load(fixture());
    const fx = fixture();
    const byProfile = mod.buildDimOptions(fx.profiles[0], fx.book);
    expect(byProfile.source).toBe("profile");
    expect(byProfile.available).toEqual(["language.sentence_structure", "scene.dialogue"]);
    const sent = byProfile.layers[0].subs[0];
    expect(sent).toMatchObject({ path: "language.sentence_structure", available: true, conf: "high", obs: 3, fp: 1, q: 5 });
    expect(byProfile.layers[0].subs[1].available).toBe(false);
    // theme 层在 input_assessment 是 skip，但画像模式下不看 input_assessment（画像覆盖即可选）
    expect(byProfile.layers[3].skipped).toBe(false);

    const byInput = mod.buildDimOptions(null, fx.book);
    expect(byInput.source).toBe("input_assessment");
    expect(byInput.available).toHaveLength(12);
    expect(byInput.available.some((p) => p.startsWith("theme."))).toBe(false);
    expect(byInput.layers[3].skipped).toBe(true);
    expect(byInput.layers[3].subs.every((s) => !s.available)).toBe(true);

    // 画像存在但 sub_dimensions 为空对象 → 视为缺失，回退 input_assessment
    expect(mod.buildDimOptions({ profile_json: { sub_dimensions: {} } }, fx.book).source).toBe("input_assessment");

    const none = mod.buildDimOptions(null, null);
    expect(none.source).toBe("none");
    expect(none.available).toHaveLength(16);
  });

  it("computeResynthState：无画像 / 新 run / stale / 非 active 可再合成；active 且对应最新 run 不可", async () => {
    const { mod } = await load(fixture());
    expect(mod.computeResynthState(null)).toMatchObject({ hasProfile: false, canResynth: true, reason: "no_profile" });
    const active = { profile_id: "p1", run_id: "run1", status: "active", coverage_json: {} };
    expect(mod.computeResynthState({ runId: "run1", profile: active })).toMatchObject({ canResynth: false, reason: null });
    expect(mod.computeResynthState({ runId: "run2", profile: active })).toMatchObject({ canResynth: true, reason: "new_run" });
    expect(mod.computeResynthState({ runId: "run1", profile: { ...active, coverage_json: { stale: true } } })).toMatchObject({ canResynth: true, reason: "stale" });
    expect(mod.computeResynthState({ runId: "run1", profile: { ...active, status: "draft" } })).toMatchObject({ canResynth: true, reason: "inactive" });
  });

  it("findShadowedBinding：同 scope + scope_ref_id 的 active 绑定才算遮蔽", async () => {
    const { mod } = await load(fixture());
    const bindings = [
      { binding_id: "b1", scope: "project", scope_ref_id: "prj-main", status: "active" },
      { binding_id: "b2", scope: "scene", scope_ref_id: "s1", status: "revoked" },
      { binding_id: "b3", scope: "character", scope_ref_id: "c1" },
    ];
    expect(mod.findShadowedBinding(bindings, "project", "prj-main").binding_id).toBe("b1");
    expect(mod.findShadowedBinding(bindings, "project", "prj-other")).toBeNull();
    expect(mod.findShadowedBinding(bindings, "scene", "s1")).toBeNull();          // 非 active 不遮蔽
    expect(mod.findShadowedBinding(bindings, "character", "c1").binding_id).toBe("b3"); // 缺 status 视为 active
    expect(mod.findShadowedBinding(null, "project", "x")).toBeNull();
  });

  it("srPickLatestRun：按 finished_at 取最新 done，而不是列表首项", async () => {
    const { mod } = await load(fixture());
    const runs = [
      { run_id: "old", status: "done", finished_at: "2026-09-01T00:00:00Z" },
      { run_id: "new", status: "done", finished_at: "2026-09-05T00:00:00Z" },
      { run_id: "running", status: "running" },
    ];
    expect(mod.srPickLatestRun(runs).run_id).toBe("new");
    expect(mod.srPickLatestRun([{ run_id: "a", status: "done" }, { run_id: "b", status: "done" }]).run_id).toBe("b");
    expect(mod.srPickLatestRun([{ run_id: "r", status: "running" }]).run_id).toBe("r");
    expect(mod.srPickLatestRun([])).toBeNull();
  });

  it("任务卡常量只剩 scene_generation", async () => {
    const { mod } = await load(fixture());
    expect(mod.SR_TASKS.map((t) => t.id)).toEqual(["scene_generation"]);
    // SR_LAYERS 不再携带 conf / skip / input 硬编码
    for (const layer of mod.SR_LAYERS) {
      expect(layer).not.toHaveProperty("input");
      for (const sub of layer.subs) { expect(sub).not.toHaveProperty("conf"); expect(sub).not.toHaveProperty("obs"); }
    }
  });
});

/* ---------------- (a) 缓存失效 ---------------- */
describe("深层缓存失效", () => {
  it("run 到达 done 后 srLoadDeep 被 force 重读（非 force 命中缓存不发请求）；完成走活动面板，不弹窗", async () => {
    vi.useFakeTimers();
    const { mod, client } = await load(fixture());
    await mod.srLoadDeep("bk1");
    expect(mod.srDeepFor("bk1")).toBeTruthy();
    client.apiGet.mockClear();
    // 非 force：命中缓存，不再打 books/bk1
    await mod.srLoadDeep("bk1");
    expect(urls(client)).not.toContain("/api/v2/style-reference/books/bk1");

    mod.srPollRun("run1", "bk1");
    // 本地先登记一条在跑的 extract 条目（key = run:<run_id>）
    expect(mod.srActivityEntries()[0]).toMatchObject({ key: "run:run1", kind: "extract", status: "running", bookId: "bk1" });
    await vi.advanceTimersByTimeAsync(50);
    vi.useRealTimers();
    await vi.waitFor(() => expect(urls(client)).toContain("/api/v2/style-reference/activity"), T);
    await vi.waitFor(() => expect(urls(client)).toContain("/api/v2/style-reference/books/bk1"), T);
    await vi.waitFor(() => expect(mod.srActivityEntries()[0].status).toBe("succeeded"), T);
    expect(window.alert).not.toHaveBeenCalled();
    expect(mod.srActivityView(mod.srActivityEntries()[0]).detail).toContain("抽取完成");
    // 不再逐 run 轮询
    expect(urls(client)).not.toContain("/api/v2/style-reference/runs/run1");
    mod.srActivityStop();
  });

  it("run failed 同样强制重读（bookId 来自活动清单的 book_id），失败原因写进条目", async () => {
    vi.useFakeTimers();
    const { mod, client } = await load(fixture(), { runStatus: "failed" });
    await mod.srLoadDeep("bk1");
    client.apiGet.mockClear();
    mod.srPollRun("run1"); // 不传 bookId → 活动清单里的 book_id
    await vi.advanceTimersByTimeAsync(50);
    vi.useRealTimers();
    await vi.waitFor(() => expect(urls(client)).toContain("/api/v2/style-reference/books/bk1"), T);
    await vi.waitFor(() => expect(mod.srActivityEntries()[0].status).toBe("failed"), T);
    const entry = mod.srActivityEntries()[0];
    expect(entry.bookId).toBe("bk1");
    expect(entry.error).toContain("STYLE_REFERENCE_EXTRACTION_FAILED");
    expect(mod.srActivityView(entry).detail).toContain("抽取失败");
    expect(window.alert).not.toHaveBeenCalled();
    mod.srActivityStop();
  });

  it("重新分类：按幂等键登记活动条目，POST 返回后交给活动清单跟后台分类，任务完成时重读深层数据，不弹窗", async () => {
    const { mod, client } = await load(fixture());
    await mod.srLoadDeep("bk1");
    client.apiGet.mockClear();
    await mod.srBookAction("reclassify", "bk1");
    expect(postUrls(client)).toContain("/api/v2/style-reference/books/bk1/reclassify");
    const call = client.apiPost.mock.calls.find(([u]) => u.endsWith("/reclassify"));
    expect(call[2]).toEqual({ idempotencyKey: expect.stringMatching(/^sr-reclassify-/) });
    // 请求里已清派生数据 → 立即重读一次
    expect(urls(client)).toContain("/api/v2/style-reference/books/bk1");
    expect(window.alert).not.toHaveBeenCalled();
    let entry = mod.srActivityEntries().find((e) => e.kind === "reclassify");
    // 2026-09-15 严格 LLM：分类是后台任务，POST 返回时条目仍在跑、交由活动清单（owned=false）
    expect(entry).toMatchObject({ key: call[2].idempotencyKey, status: "running", owned: false, bookId: "bk1", phase: "classify" });
    client.apiGet.mockClear();
    mod.srActivityApply([{ key: entry.key, kind: "reclassify", status: "succeeded", book_id: "bk1", percent: 100, phase: "done", phase_label: "完成" }]);
    await vi.waitFor(() => expect(urls(client)).toContain("/api/v2/style-reference/books/bk1"), T);
    entry = mod.srActivityEntries().find((e) => e.kind === "reclassify");
    expect(entry.status).toBe("succeeded");
    mod.srActivityStop();
  });

  it("删书成功后本地深层缓存被清掉并广播", async () => {
    const { mod } = await load(fixture());
    await mod.srLoadDeep("bk1");
    expect(mod.srDeepFor("bk1")).toBeTruthy();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ ok: true, data: {} }) }));
    const changed = vi.fn();
    window.addEventListener("sr:deep-changed", changed);
    try {
      await mod.srDeleteBook("bk1");
      expect(mod.srDeepFor("bk1")).toBeNull();
      expect(changed).toHaveBeenCalled();
    } finally {
      window.removeEventListener("sr:deep-changed", changed);
    }
  });
});

/* ---------------- (b) 再合成 ---------------- */
describe("画像再合成", () => {
  it("画像 stale：画像页显示失效徽标与「重新合成」，点击真的打 synthesize 端点", async () => {
    const { mod, client } = await load(fixture({ profileStatus: "draft", stale: true }));
    const host = await render(<mod.SrProfile book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-profile-resynth-btn"]')).toBeTruthy(), T);
    expect(host.querySelector('[data-testid="sr-profile-stale"]').textContent).toContain("已失效");
    expect(host.querySelector('[data-testid="sr-profile-status"]').textContent).toContain("草稿");
    // v2 新键渲染
    expect(host.textContent).toContain("声音特征");
    expect(host.textContent).toContain("连接词多用却、便");

    const btn = host.querySelector('[data-testid="sr-profile-resynth-btn"]');
    expect(btn.disabled).toBe(false);
    await click(btn);
    await vi.waitFor(() => expect(postUrls(client)).toContain("/api/v2/style-reference/runs/run1/synthesize"), T);
  });

  it("有新 run：矩阵按钮变为「重新合成画像」并调用最新 run 的 synthesize", async () => {
    const { mod, client } = await load(fixture({ profileStatus: "active", profileRunId: "run1", latestRunId: "run2" }));
    const go = vi.fn();
    const host = await render(<mod.SrMatrix book={BOOK} go={go} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-matrix-synth"]').textContent).toContain("重新合成画像"), T);
    expect(host.querySelector('[data-testid="sr-matrix-resynth-reason"]').textContent).toContain("有新的抽取结果");
    await click(host.querySelector('[data-testid="sr-matrix-synth"]'));
    await vi.waitFor(() => expect(postUrls(client)).toContain("/api/v2/style-reference/runs/run2/synthesize"), T);
    await vi.waitFor(() => expect(go).toHaveBeenCalledWith("profile"), T);
  });

  it("active 画像且对应最新 run：矩阵按钮只导航，不再合成；画像页无重新合成按钮", async () => {
    const { mod, client } = await load(fixture({ profileStatus: "active" }));
    const go = vi.fn();
    const host = await render(<mod.SrMatrix book={BOOK} go={go} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-matrix-synth"]').textContent).toContain("查看风格画像"), T);
    await click(host.querySelector('[data-testid="sr-matrix-synth"]'));
    expect(go).toHaveBeenCalledWith("profile");
    expect(postUrls(client).some((u) => u.endsWith("/synthesize"))).toBe(false);

    const host2 = await render(<mod.SrProfile book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(host2.querySelector('[data-testid="sr-profile-status"]').textContent).toContain("已激活"), T);
    expect(host2.querySelector('[data-testid="sr-profile-resynth-btn"]')).toBeNull();
  });
});

/* ---------------- (c)(d)(e) 注入应用 ---------------- */
describe("注入应用页", () => {
  it("强度读数来自预览端点的 stats；刻度为 轻 / 中 / 强", async () => {
    const { mod, client } = await load(fixture({ profileStatus: "active" }));
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-intensity-readout"]')).toBeTruthy(), T);
    expect(host.querySelector(".sr-intensity-ticks").textContent).toBe("轻中强");
    // 预览未返回前是「预览中…」
    expect(host.querySelector('[data-testid="sr-intensity-readout"]').textContent).toContain("预览中…");
    await vi.waitFor(() => expect(postUrls(client).some((u) => u.endsWith("/profiles/p1/injection-preview"))).toBe(true), T);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-intensity-readout"]').textContent)
      .toContain("规则 5 行 · 声音特征 3 行 · 样例 4 段 · 共 4200 字"), T);
    // 旧文案（本地公式）不得再出现
    expect(host.textContent).not.toContain("当前将注入约");
    expect(host.textContent).not.toContain("轻微借鉴");
    // 预览请求体带 sub_dimensions = 画像覆盖的维度
    const previewCall = client.apiPost.mock.calls.find(([u]) => u.endsWith("/injection-preview"));
    expect(previewCall[1].sub_dimensions).toEqual(["language.sentence_structure", "scene.dialogue"]);
    expect(previewCall[1].task_type).toBe("scene_generation");
  });

  it("预览返回没有 stats 时显示「预览中…」，不回退到任何公式", async () => {
    const { mod, client } = await load(fixture({ profileStatus: "active" }), { previewStats: null });
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(postUrls(client).some((u) => u.endsWith("/injection-preview"))).toBe(true), T);
    // 预览已到（bundle 面板渲染出 fragments），但读数仍是「预览中…」
    await vi.waitFor(() => expect(host.textContent).toContain("banned_pattern_block"), T);
    expect(host.querySelector('[data-testid="sr-intensity-readout"]').textContent).toContain("预览中…");
    expect(host.textContent).not.toMatch(/规则 \d+ 行/);
  });

  it("任务卡只剩 scene_generation，即使 task-defaults 返回四项", async () => {
    const { mod, client } = await load(fixture({ profileStatus: "active" }));
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(urls(client)).toContain("/api/v2/style-reference/injection/task-defaults"), T);
    await vi.waitFor(() => expect(host.querySelectorAll(".sr-task").length).toBe(1), T);
    expect(host.querySelector(".sr-task .sr-task-name").textContent).toBe("场景生成");
    expect(host.textContent).not.toContain("项目初始化");
    expect(host.textContent).not.toContain("精修小改");
    expect(host.textContent).not.toContain("关键章节");
    expect(host.textContent).not.toContain("长文防漂移");
  });

  it("维度选择器按画像 sub_dimensions 动态生成：只有覆盖的维度可选，标题不再写死 / 12", async () => {
    const { mod } = await load(fixture({ profileStatus: "active" }));
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-dimselect"]')).toBeTruthy(), T);
    const cells = [...host.querySelectorAll(".sr-ds-cell")];
    expect(cells).toHaveLength(16);
    const enabled = cells.filter((c) => !c.disabled);
    expect(enabled.map((c) => c.dataset.dim)).toEqual(["language.sentence_structure", "scene.dialogue"]);
    expect(enabled.every((c) => c.classList.contains("is-on"))).toBe(true);
    expect(host.textContent).toContain("2 / 2 可用已选");
    expect(host.textContent).not.toContain("/ 12 已选");
    // 取消一个 → 1 / 2
    await click(enabled[0]);
    expect(host.textContent).toContain("1 / 2 可用已选");
  });

  it("所选作用域已有同 scope_ref_id 的 active 绑定 → 提示将遮蔽；画像非 active → 绑定列表标注注入不会生效", async () => {
    const bindings = [{ binding_id: "b1", profile_id: "p1", scope: "project", scope_ref_id: "prj-main", task_type: "scene_generation", strategy: "mixed", status: "active" }];
    const { mod } = await load(fixture({ profileStatus: "draft", stale: true, bindings }));
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-apply-shadow"]')).toBeTruthy(), T);
    expect(host.querySelector('[data-testid="sr-apply-shadow"]').textContent).toContain("将遮蔽该作用域已有绑定：作者风格画像");
    expect(host.querySelector('[data-testid="sr-bindings-inactive"]').textContent).toContain("注入不会生效");
    expect(host.querySelector('[data-testid="sr-apply-inactive"]').textContent).toContain("重新合成");

    // 切到「场景」作用域且未选目标 → 不提示遮蔽
    const sceneBtn = [...host.querySelectorAll(".sr-scope-btn")].find((b) => b.textContent === "场景");
    await click(sceneBtn);
    expect(host.querySelector('[data-testid="sr-apply-shadow"]')).toBeNull();
  });

  it("active 画像且无同域绑定：无遮蔽提示、无失效标注", async () => {
    const bindings = [{ binding_id: "b9", profile_id: "p1", scope: "scene", scope_ref_id: "s1", task_type: "scene_generation", strategy: "A", status: "active" }];
    const { mod } = await load(fixture({ profileStatus: "active", bindings }));
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector(".sr-bindings")).toBeTruthy(), T);
    await vi.waitFor(() => expect(host.textContent).toContain("s1 · A"), T);
    expect(host.querySelector('[data-testid="sr-apply-shadow"]')).toBeNull();
    expect(host.querySelector('[data-testid="sr-bindings-inactive"]')).toBeNull();
    expect(host.querySelector('[data-testid="sr-apply-inactive"]')).toBeNull();
  });
});
