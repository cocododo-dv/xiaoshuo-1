// 风格参考页 · 文风画像（气质、按层分组的 16 维、✓ / ✗、逐维「重点 / 正常 / 不学」、本书专名）与「当前作品像不像」卡片（终稿在范围内几场、近期常见偏差、走势，场名来自目录）。
// （2026-09-30 从 ws-styleref.test.jsx 按步拆出，共用夹具在 ws-styleref.test-helpers.jsx。）
import React, { act } from "react";
import { describe, expect, it, vi } from "vitest";

// 没装路由之前（模块加载时目录 store 就会读一次目录）也回一个空载荷，不让它报「拉取目录失败」
vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(() => Promise.resolve({})),
  apiPost: vi.fn(() => Promise.resolve({})),
  apiPatch: vi.fn(() => Promise.resolve({})),
  apiDelete: vi.fn(() => Promise.resolve({})),
  getOperatorRef: vi.fn(() => "operator"),
}));
const workHolder = vi.hoisted(() => ({ current: { id: "w1", title: "北岸手记" } }));
vi.mock("./ws-works.jsx", () => ({
  WsWorks: {
    active: () => workHolder.current,
    activeId: () => (workHolder.current ? workHolder.current.id : null),
  },
}));

import {
  $, $$, API, bookRow, byTestId, click, client, mountView, openStage, OWN_BINDING, PROFILE_SUMMARY, settle, setupStyleRefSuite, unmountAll, state, store,
} from "./ws-styleref.test-helpers.jsx";

setupStyleRefSuite(workHolder);

describe("文风画像", () => {
  async function openPortrait({ applied = false } = {}) {
    state.books = [bookRow({ profile: PROFILE_SUMMARY, applied_projects: applied ? [{ project_id: "w1", binding_id: "bd-1", config: OWN_BINDING.config }] : [] })];
    if (applied) state.projectBinding = { project_id: "w1", binding: OWN_BINDING, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    await mountView();
    await openStage("learn");
    await settle();
  }
  const dimRow = (dim) => $(`.sr-dim[data-dimension="${dim}"]`);

  it("气质在最前；16 维按层分组；展开看通用写法对这位作者、原话", async () => {
    await openPortrait();
    expect(byTestId("sr-temperament").textContent).toContain("冷眼旁观，不替人物下结论");
    expect($$(".sr-dim-group-title").map((h) => h.textContent)).toEqual(["语言层", "场景层"]);
    await click($(".sr-dim-toggle", dimRow("scene.dialogue")));
    const row = dimRow("scene.dialogue");
    expect(row.textContent).toContain("通用写法对白里交代来龙去脉");
    expect(row.textContent).toContain("这位作者");
    expect(row.textContent).toContain("作者不这么写");
    expect(row.textContent).toContain("必须体现");
    await click($(".sr-line-quotes-toggle", row));
    expect($(".sr-quote", row).textContent).toContain("第 4 段");
    expect($(".sr-quote-more", row).textContent).toBe("另有 1 条");
  });

  it("✓ 总带上：先标上再等服务端；服务端拒绝就回滚", async () => {
    await openPortrait();
    await click($(".sr-dim-toggle", dimRow("scene.dialogue")));
    const line = () => $('[data-line-id="L1"]');
    let release;
    client.apiPost.mockImplementation((url) => (url.includes("/card-lines/")
      ? new Promise((resolve, reject) => { release = { resolve, reject }; })
      : Promise.resolve({})));
    await click($('[data-testid="sr-line-pinned"]', line()));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/profiles/pf-a/card-lines/L1`, { state: "pinned" });
    expect(line().className).toContain("is-pinned");
    await act(async () => { release.reject(Object.assign(new Error("x"), { code: "STYLE_REFERENCE_CARD_LINE_NOT_FOUND" })); });
    await settle();
    expect(line().className).not.toContain("is-pinned");
    expect(window.alert).toHaveBeenCalledWith("这一句已经不在文风卡上了（可能刚重新学过）。");
  });

  it("没用于当前作品：逐维状态锁住并说明；用上之后改「重点」写在绑定上", async () => {
    await openPortrait();
    expect(byTestId("sr-states-locked").textContent).toContain("这本书还没用于《北岸手记》");
    expect(byTestId("sr-dim-state-scene.dialogue-emphasize")).toBeNull();
    await unmountAll();
    store.srResetForTests();
    await openPortrait({ applied: true });
    expect(byTestId("sr-states-hint").textContent).toContain("给《北岸手记》设的");
    client.apiPatch.mockResolvedValueOnce({ binding: { ...OWN_BINDING, config: { ...OWN_BINDING.config, dimension_states: { "scene.dialogue": "emphasize" } } }, changed: true });
    await click(byTestId("sr-dim-state-scene.dialogue-emphasize"));
    await settle();
    expect(client.apiPatch).toHaveBeenCalledWith(`${API}/bindings/bd-1`, { config: { dimension_states: { "scene.dialogue": "emphasize" } } });
    expect(byTestId("sr-dim-state-scene.dialogue-emphasize").getAttribute("aria-checked")).toBe("true");
  });

  it("作品沿用的是旧版全局应用：逐维状态只读，不往那条全局应用上写（复核 #3）", async () => {
    const GLOBAL = { ...OWN_BINDING, binding_id: "bd-g", scope: "global", scope_ref_id: null, config: { ...OWN_BINDING.config, dimension_states: { "scene.dialogue": "emphasize" } } };
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.projectBinding = { project_id: "w1", binding: GLOBAL, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    await mountView();
    await openStage("learn");
    await settle();
    expect(byTestId("sr-states-legacy").textContent).toContain("沿用的是一条旧版的「全部作品」应用");
    expect(byTestId("sr-states-hint")).toBeNull();
    expect(byTestId("sr-dim-state-scene.dialogue-emphasize")).toBeNull();
    // 全局应用上标的「重点」照样显示（它在起草时照样起作用）
    expect($('.sr-dim[data-dimension="scene.dialogue"] .sr-dim-head').textContent).toContain("重点");
    expect(client.apiPatch).not.toHaveBeenCalled();
  });

  it("本书专名可以逐个去掉：先说清楚去掉之后不再受保护（复核 #12）", async () => {
    await openPortrait();
    const banned = byTestId("sr-banned");
    expect(banned.textContent).toContain("本书专名 1 个");
    await click([...banned.querySelectorAll("button")].find((b) => b.textContent === "看看"));
    const chip = $('[data-term-id="t1"]');
    expect(chip.textContent).toContain("某地");
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false);
    await click($('[data-testid="sr-protected-remove"]', chip));
    await settle();
    expect(confirm.mock.calls[0][0]).toContain("不再保护「某地」？");
    expect(confirm.mock.calls[0][0]).toContain("以后重新学习文风也不会再把它加回来");
    expect(client.apiDelete).not.toHaveBeenCalled();
    confirm.mockReturnValueOnce(true);
    state.bannedTerms = [];
    await click($('[data-testid="sr-protected-remove"]', $('[data-term-id="t1"]')));
    await settle();
    expect(client.apiDelete).toHaveBeenCalledWith(`${API}/banned-terms/t1`);
    expect(byTestId("sr-banned").textContent).toContain("本书专名 0 个");
  });

  it("重新学习（同一份画像）学完：本书专名那一组重读（复核 #13）", async () => {
    await openPortrait();
    const termCalls = () => client.apiGet.mock.calls.filter(([url]) => url === `${API}/profiles/pf-a/banned-terms`).length;
    const before = termCalls();
    expect(byTestId("sr-banned").textContent).toContain("本书专名 1 个");
    state.bannedTerms = [
      { term_id: "t2", term: "某城", source: "protected_auto", scope: "generation" },
      { term_id: "t3", term: "某人", source: "protected_auto", scope: "generation" },
    ];
    // 这本书的学习作业跑完（活动表从在跑走到成功）
    await act(async () => {
      store.srActivityApply([{ key: "job:jl", job_id: "jl", kind: "learn", status: "running", book_id: "bk-a", profile_id: "pf-a" }]);
      store.srActivityApply([{ key: "job:jl", job_id: "jl", kind: "learn", status: "succeeded", book_id: "bk-a", profile_id: "pf-a", result: { profile_id: "pf-a" } }]);
    });
    await settle(20);
    expect(termCalls()).toBeGreaterThan(before);
    expect(byTestId("sr-banned").textContent).toContain("本书专名 2 个");
  });

  it("没有文风卡的画像（旧版画像已归档，界面上不再有「旧版画像」这一说）：只说先学习文风", async () => {
    state.profile = { profile_id: "pf-a", has_card: false, needs_relearn: false, relearn_reason: null, dimensions: [], temperament: [], voice: { habits: [] }, structure: { lines: [] } };
    await openPortrait();
    expect(byTestId("sr-portrait-no-card").textContent).toContain("还没有文风卡");
    expect(byTestId("sr-portrait-legacy")).toBeNull();
    expect(document.body.textContent).not.toContain("旧版画像");
  });
});

describe("文风画像 · 当前作品像不像", () => {
  const WORK_FIDELITY = {
    project_id: "w1", bound: true, profile_id: "pf-a", reading_count: 5, final_scene_count: 2,
    trend: [
      { reading_id: "t1", scene_id: "sc-1", stage: "first_draft", percentile: 93, within_range: false, reliable: true, max_percentile: 90, created_at: "2026-09-22T08:00:00" },
      { reading_id: "t2", scene_id: "sc-1", stage: "final", percentile: 55, within_range: true, reliable: true, max_percentile: 90, created_at: "2026-09-22T09:00:00" },
      { reading_id: "t3", scene_id: "sc-2", stage: "final", percentile: 96, within_range: false, reliable: true, max_percentile: 90, created_at: "2026-09-23T09:00:00" },
    ],
    recent_gaps: ["对白比作者少"],
    recent_gap_details: [{ feature: "dialogue_char_share", direction: "low", dimension: "scene.dialogue", dimension_label: "对话写法", phrase: "对白比作者少", hits: 3, window: 4 }],
    dimension_averages: {
      "scene.dialogue": { label: "对话写法", deterministic: 6.2, judge: 6.5, scenes: 2, judged: 2 },
      "language.sentence_structure": { label: "句式结构", deterministic: 8.1, judge: null, scenes: 2, judged: 0 },
    },
    scene_finals: {
      "sc-1": { reading_id: "t2", percentile: 55, within_range: true, reliable: true },
      "sc-2": { reading_id: "t3", percentile: 96, within_range: false, reliable: true },
    },
  };

  async function openPortrait({ applied = true } = {}) {
    state.books = [bookRow({ profile: PROFILE_SUMMARY, applied_projects: applied ? [{ project_id: "w1", binding_id: "bd-1", config: OWN_BINDING.config }] : [] })];
    if (applied) state.projectBinding = { project_id: "w1", binding: OWN_BINDING, profile: PROFILE_SUMMARY, book: { book_id: "bk-a", title: "甲书" } };
    state.projectFidelity = WORK_FIDELITY;
    await mountView();
    await openStage("learn");
    await settle();
    await settle();
  }
  const dimRow = (dim) => $(`.sr-dim[data-dimension="${dim}"]`);

  it("用于当前作品时：顶上一张卡——终稿几场在作者范围内、近期常见偏差、走势（可切表格，场名来自目录）", async () => {
    await openPortrait();
    const card = byTestId("sr-work-fidelity");
    expect(card.textContent).toContain("《北岸手记》像不像");
    expect(byTestId("sr-work-fid-finals").textContent).toContain("1 / 2");
    expect(byTestId("sr-work-fid-gaps").textContent).toContain("1");
    expect(card.textContent).toContain("对话写法对白比作者少（3 次）");
    const trend = byTestId("sr-work-trend");
    expect($(".fid-trend-plot > svg", trend).getAttribute("aria-label")).toBe("最近 3 次读数：终稿 2 次，其中 1 次在作者的正常范围内；最近一次终稿第 96 位");
    expect(trend.textContent).toContain("作者的正常范围（前 90 位）");
    await click(byTestId("sr-work-trend-table-toggle"));
    const rows = $$("tbody tr", byTestId("sr-work-trend-table"));
    expect(rows).toHaveLength(3);
    expect(rows[0].textContent).toContain("第 1 章 · 第 2 场「夜渡」");
    expect(rows[0].textContent).toContain("第 96 位");
    expect(rows[0].textContent).toContain("超出范围");
    expect(rows[2].textContent).toContain("首稿");
  });

  it("每一维右边是作品在这一维的平均分；近期常见偏差标出来，展开说是哪一句、几次", async () => {
    await openPortrait();
    const dialogue = dimRow("scene.dialogue");
    const chip = $('[data-testid="sr-dim-fid"]', dialogue);
    expect(chip.textContent).toContain("测得6.2");
    expect(chip.textContent).toContain("评审6.5");
    expect(chip.textContent).toContain("近期常见偏差");
    const sentence = $('[data-testid="sr-dim-fid"]', dimRow("language.sentence_structure"));
    expect(sentence.textContent).toContain("测得8.1");
    expect(sentence.textContent).not.toContain("评审");
    await click($(".sr-dim-toggle", dialogue));
    const body = $('[data-testid="sr-dim-fid-body"]', dialogue);
    expect(body.textContent).toContain("《北岸手记》在这一维");
    expect(body.textContent).toContain("测得平均 6.2 / 10（2 场终稿）");
    expect(body.textContent).toContain("近期常见偏差：对白比作者少（最近 4 次首稿里 3 次）");
  });

  it("这本书没用于当前作品：不挂卡、不挂分数（读数是对着作品现在用的那份画像量的）", async () => {
    await openPortrait({ applied: false });
    expect(byTestId("sr-work-fidelity")).toBeNull();
    expect($('[data-testid="sr-dim-fid"]')).toBeNull();
    expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/projects/w1/style-fidelity")).toBe(false);
  });
});
