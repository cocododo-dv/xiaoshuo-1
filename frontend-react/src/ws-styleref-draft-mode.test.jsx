// 风格直起（2026-09-12 Step 2，docs/style-first-draft-plan-2026-09-12.md Track C）的前端契约：
//   (a) 注入应用页有「起草方式」单选，默认 style_first（作者手笔直起），强度滑块默认 100；
//   (b) 「应用 · 进审核」的决策卡 effect（后端 bind_style_profile 执行 apply）携带 draft_mode；
//   (c) 绑定列表按 config_json.draft_mode 显示起草方式小标签，缺省显示「作者手笔直起」；
//   (d) 注入预览（dryrun）请求体不带 draft_mode——预览模型 extra=forbid，且起草方式不影响前缀渲染。
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

const PREVIEW_STATS = {
  positive_lines: 3, forbidden_lines: 1, metric_lines: 1, voice_lines: 3,
  few_shot_windows: 4, few_shot_chars: 2400, rag_snippets: 0,
  total_prefix_chars: 4200, intensity_effective_total_chars: 1800, few_shot_k: 4,
};

/* 后端形状的深层数据夹具：active 画像 + 可控绑定表（绑定行由 _serialize_binding 带 config_json） */
function fixture({ bindings = [] } = {}) {
  return {
    book: {
      book_id: "bk1", title: "参考书", author_label: "作者", total_chars: 120000, status: "ready",
      stats_json: { input_assessment: { language: "high", narrative: "medium", scene: "high", theme: "skip" }, metrics: {}, paragraph_type_distribution: {} },
    },
    runs: [{ run_id: "run1", book_id: "bk1", status: "done", finished_at: "2026-09-01T00:00:00Z" }],
    findings: [
      { finding_id: "f1", sub_dimension: "language.sentence_structure", finding_kind: "observation", confidence: "high", statement: "短句为骨", evidence: [{ quote_text: "a" }, { quote_text: "b" }] },
    ],
    profiles: [{
      profile_id: "p1", book_id: "bk1", run_id: "run1", title: "作者风格画像", status: "active",
      profile_json: {
        sub_dimensions: { "language.sentence_structure": { confidence: "high", observation_count: 3, forbidden_pattern_count: 1, quote_count: 5 } },
        qualitative_summary: "短句为骨，白描见长。", style_features: ["短句"],
      },
      coverage_json: { findings_count: 1, quotes_count: 2, sub_dim_count: 1 },
    }],
    bindings,
  };
}

function installStyleRefRouter(client, fx) {
  client.apiGet.mockImplementation((url) => {
    if (url === "/api/v2/style-reference/books") return Promise.resolve({ books: [] });
    if (url === "/api/v2/style-reference/books/bk1") return Promise.resolve({ book: fx.book });
    if (url === "/api/v2/style-reference/books/bk1/runs") return Promise.resolve({ runs: fx.runs });
    if (/^\/api\/v2\/style-reference\/runs\/[^/]+\/findings/.test(url)) return Promise.resolve({ findings: fx.findings });
    if (/^\/api\/v2\/style-reference\/runs\/[^/]+$/.test(url)) return Promise.resolve({ run: { run_id: url.split("/").pop(), book_id: "bk1", status: "done" } });
    if (url.startsWith("/api/v2/style-reference/profiles?book_id=")) return Promise.resolve({ profiles: fx.profiles });
    if (/\/profiles\/p1\/bindings$/.test(url)) return Promise.resolve({ bindings: fx.bindings });
    if (/\/profiles\/p1\/banned-terms$/.test(url)) return Promise.resolve({ terms: [] });
    if (url === "/api/v2/style-reference/injection/task-defaults") {
      return Promise.resolve({ tasks: [{ task_type: "scene_generation", default_strategy: "mixed", refresh_every_chars: 0 }] });
    }
    if (/\/catalog(\?|$)/.test(url)) return Promise.resolve({ chapters: [] });
    if (/\/library(\?|$)/.test(url)) return Promise.resolve({ characters: [] });
    return Promise.resolve({});
  });
  client.apiPost.mockImplementation((url) => {
    if (/\/injection-preview$/.test(url)) {
      return Promise.resolve({
        fragments: { positive_block: "[正向风格特征]\n- 短句为骨", forbidden_block: "[禁忌]\n- 不写解释性对白", anti_plagiarism_block: "严禁复制原文。" },
        prefix: "x".repeat(4200),
        stats: PREVIEW_STATS,
      });
    }
    return Promise.resolve({});
  });
  client.apiDelete.mockResolvedValue({});
}

async function load(fx) {
  const client = await import("./lib/client.js");
  installStyleRefRouter(client, fx);
  const mod = await import("./ws-styleref.jsx");
  const review = await import("./ws-review.jsx");
  return { mod, client, rvPush: review.rvPush };
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
const applyButton = (host) => [...host.querySelectorAll("button")].find((b) => b.textContent.includes("应用到项目 · 进审核"));
const radio = (host, value) => host.querySelector(`input[name="sr-draft-mode"][value="${value}"]`);
const lastEffect = (rvPush) => {
  const card = rvPush.mock.calls.at(-1)[0];
  return card.actions.find((a) => a.effect && a.effect.type === "bind_style_profile").effect;
};

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
  vi.restoreAllMocks();
});

describe("注入应用 · 起草方式（风格直起）", () => {
  it("默认作者手笔直起、强度 100；应用决策卡的 effect 携带 draft_mode=style_first 与 intensity=100", async () => {
    const { mod, rvPush } = await load(fixture());
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelector('[data-testid="sr-draft-mode"]')).toBeTruthy(), T);

    // 单选默认 style_first，带「默认」角标与两条帮助文案
    expect(radio(host, "style_first").checked).toBe(true);
    expect(radio(host, "neutral_first").checked).toBe(false);
    const group = host.querySelector('[data-testid="sr-draft-mode"]');
    expect(group.textContent).toContain("作者手笔直起");
    expect(group.textContent).toContain("首稿直接以参考作者手笔写；系统的房风质量门让位");
    expect(group.textContent).toContain("中性稿再上风格");
    expect(group.textContent).toContain("现状流程，用于阅读对照");
    expect(host.querySelector('[data-testid="sr-draft-mode-current"]').textContent).toContain("作者手笔直起");
    // 强度滑块默认 100（2026-09-12：新绑定默认强度拉满）
    expect(host.querySelector('input[aria-label="风格强度"]').value).toBe("100");
    expect(host.querySelector(".sr-intensity-val").textContent).toBe("100%");

    await vi.waitFor(() => expect(applyButton(host)).toBeTruthy(), T);
    expect(applyButton(host).disabled).toBe(false);
    await click(applyButton(host));

    expect(rvPush).toHaveBeenCalledTimes(1);
    const effect = lastEffect(rvPush);
    expect(effect).toMatchObject({
      type: "bind_style_profile",
      profile_id: "p1",
      scope: "project",
      scope_ref_id: "prj-main",
      task_type: "scene_generation",
      intensity: 100,
      draft_mode: "style_first",
      include_positive: true,
      include_forbidden: true,
    });
    expect(rvPush.mock.calls[0][0].detail).toContain("起草 作者手笔直起");
  });

  it("切到「中性稿再上风格」→ effect.draft_mode=neutral_first，标签同步", async () => {
    const { mod, rvPush } = await load(fixture());
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(radio(host, "neutral_first")).toBeTruthy(), T);

    await click(radio(host, "neutral_first"));
    expect(radio(host, "neutral_first").checked).toBe(true);
    expect(radio(host, "style_first").checked).toBe(false);
    expect(host.querySelector('[data-testid="sr-draft-mode-current"]').textContent).toContain("中性稿再上风格");

    await vi.waitFor(() => expect(applyButton(host)).toBeTruthy(), T);
    await click(applyButton(host));
    expect(lastEffect(rvPush).draft_mode).toBe("neutral_first");
    expect(rvPush.mock.calls[0][0].detail).toContain("起草 中性稿再上风格");
  });

  it("绑定列表：config_json.draft_mode 决定小标签，缺省显示「作者手笔直起」", async () => {
    const bindings = [
      { binding_id: "b-default", profile_id: "p1", scope: "project", scope_ref_id: "prj-main", task_type: "scene_generation", strategy: "mixed", status: "active" },
      { binding_id: "b-neutral", profile_id: "p1", scope: "scene", scope_ref_id: "s1", task_type: "scene_generation", strategy: "A", status: "active", config_json: { intensity: 60, draft_mode: "neutral_first" } },
      { binding_id: "b-style", profile_id: "p1", scope: "character", scope_ref_id: "c1", task_type: "scene_generation", strategy: "B", status: "active", config_json: { draft_mode: "style_first" } },
    ];
    const { mod } = await load(fixture({ bindings }));
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(host.querySelectorAll('[data-testid="sr-binding-draft-mode"]').length).toBe(3), T);
    const rows = [...host.querySelectorAll(".sr-bindings li")].filter((li) => li.querySelector('[data-testid="sr-binding-draft-mode"]'));
    const chipOf = (refId) => rows.find((li) => li.textContent.includes(refId)).querySelector('[data-testid="sr-binding-draft-mode"]').textContent;
    // 项目级绑定显示作品名（不再印 project id）
    expect(chipOf("北岸手记")).toBe("作者手笔直起");
    expect(chipOf("s1")).toBe("中性稿再上风格");
    expect(chipOf("c1")).toBe("作者手笔直起");
  });

  it("叠加层：按 binding_id 对位本画像绑定表显示起草方式；对不上的层不猜", async () => {
    const bindings = [
      { binding_id: "b-neutral", profile_id: "p1", scope: "project", scope_ref_id: "prj-main", task_type: "scene_generation", strategy: "mixed", status: "active", config_json: { draft_mode: "neutral_first" } },
    ];
    const { mod, client } = await load(fixture({ bindings }));
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url.startsWith("/api/v2/style-reference/injection/layers?")) {
        return Promise.resolve({
          budget_total: 1800,
          merged: { layer_count: 2, strategy: "mixed", prefix_chars: 3000 },
          layers: [
            { rank: 1, scope: "global", scope_ref_id: null, binding_id: "b-other-profile", profile_id: "p9", profile_title: "别家画像", strategy: "A", intensity: 50, weight: 1, budget_chars: 600, block_chars: {}, fragment_count: 2 },
            { rank: 3, scope: "project", scope_ref_id: "prj-main", binding_id: "b-neutral", profile_id: "p1", profile_title: "作者风格画像", strategy: "mixed", intensity: 100, weight: 2, budget_chars: 1200, block_chars: {}, fragment_count: 3 },
          ],
          deduplicated: [],
        });
      }
      return baseGet(url);
    });
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    const layersTab = () => [...host.querySelectorAll('[role="tab"]')].find((b) => b.textContent.includes("叠加层"));
    await vi.waitFor(() => expect(layersTab()).toBeTruthy(), T);
    await click(layersTab());
    await vi.waitFor(() => expect(host.querySelectorAll(".sr-stack-layer").length).toBe(2), T);
    const chips = [...host.querySelectorAll('[data-testid="sr-layer-draft-mode"]')];
    expect(chips).toHaveLength(1);
    expect(chips[0].textContent).toBe("中性稿再上风格");
    expect(chips[0].closest(".sr-stack-layer").textContent).toContain("作者风格画像");
  });

  it("注入预览（dryrun）请求体不带 draft_mode", async () => {
    const { mod, client } = await load(fixture());
    const host = await render(<mod.SrApply book={BOOK} go={vi.fn()} />);
    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([u]) => u.endsWith("/profiles/p1/injection-preview"))).toBe(true), T);
    const previewCall = client.apiPost.mock.calls.find(([u]) => u.endsWith("/injection-preview"));
    expect(previewCall[1]).not.toHaveProperty("draft_mode");
    expect(previewCall[1].intensity).toBe(100);
    expect(host.querySelector('[data-testid="sr-draft-mode"]')).toBeTruthy();
  });
});
