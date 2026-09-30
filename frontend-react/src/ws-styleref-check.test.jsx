// 风格参考页 · 第四步「对照检查」：贴一段文字或选一场，发起作业、看进度与结果；按作品的设置还是按画像查；没有模型时锁住；换了作品不带上一部的选择；取消与再查一次。
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
  $, $$, API, bookRow, byTestId, click, client, fidStore, mountView, openStage, OWN_BINDING, PROFILE_SUMMARY, settle, setupStyleRefSuite, state,
} from "./ws-styleref.test-helpers.jsx";

setupStyleRefSuite(workHolder);

describe("第四步 · 对照检查", () => {
  const CHECK_READING = {
    reading_id: "sfr-1", scene_id: null, project_id: null, profile_id: "pf-a", source: "manual_check", stage: "manual",
    percentile: 41.6, within_range: true, max_percentile: 90, emphasized_dimensions: [], excluded_dimensions: ["theme.values"],
    reliable: true, unreliable_reason: null, char_count: 1800, window_count: 40,
    out_of_band: [{ feature: "fw_modal_per_1k", dimension: "language.vocabulary", dimension_label: "词汇选择", direction: "low", phrase: "语气词（吧、呢、啊、嘛……）比作者少，口气不如作者松", z: -2.6 }],
    dimension_scores: { "language.vocabulary": 6.4, "scene.dialogue": 8.1 },
    judge: { overall: 7.2, summary: "对白再松一点就更像", dimensions: { "scene.dialogue": { score: 6, note: "对白偏正式" } } },
    copy_check: { blocked: false, hits: 0, protected_hits: 0 },
  };
  const JOB = (status, extra = {}) => ({ key: "job:job-c", job_id: "job-c", kind: "check", status, book_id: "bk-a", phase: status === "running" ? "judge" : status, percent: status === "running" ? 33.3 : null, ...extra });

  async function openCheck({ applied = false } = {}) {
    state.books = [bookRow({ profile: PROFILE_SUMMARY, applied_projects: applied ? [{ project_id: "w1", binding_id: "bd-1", config: OWN_BINDING.config }] : [] })];
    await mountView();
    await openStage("check");
    await settle();
  }
  function routeCheck({ post, get }) {
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, opts) => (url === `${API}/checks` ? post(body) : basePost(url, body, opts)));
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === `${API}/checks/job-c` ? get() : baseGet(url)));
  }
  async function typeText(value) {
    const area = byTestId("sr-check-text");
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    await act(async () => {
      setter.call(area, value);
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }
  async function pickScene(value) {
    await click(byTestId("sr-check-mode-scene"));
    await settle();
    const select = byTestId("sr-check-scene");
    await act(async () => {
      select.value = value;
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  it("没学过：先去学习文风", async () => {
    await mountView();
    await openStage("check");
    expect(byTestId("sr-check-empty").textContent).toContain("先在「学习文风」里学一次");
  });

  it("贴一段文字：发起作业、看进度，出结果——位次、在不在范围、越界的地方、评审、照搬、按维；不出现 z 分与旧叫法", async () => {
    await openCheck();
    expect($('.sr-step[data-stage="check"]').getAttribute("aria-label")).toBe("对照检查（随时可查）");
    expect(byTestId("sr-check-start").disabled).toBe(true);
    await typeText("潮水退下去的时候，他在闸门前站了很久。".repeat(40));
    expect(byTestId("sr-check-start").disabled).toBe(false);
    let posted = null;
    routeCheck({
      post: (body) => { posted = body; return Promise.resolve({ job_id: "job-c", state: "queued", job: JOB("running"), reading: null }); },
      get: () => Promise.resolve({ job: JOB("succeeded", { finished_at: "2026-09-23T10:00:00" }), reading: CHECK_READING }),
    });
    await click(byTestId("sr-check-start"));
    await settle();
    expect(posted).toEqual({ text: "潮水退下去的时候，他在闸门前站了很久。".repeat(40), profile_id: "pf-a" });
    expect(byTestId("sr-check-running").textContent).toContain("模型对着原文样例评审");
    expect($('[data-activity-key="job:job-c"]')).toBeTruthy();
    expect($('.sr-step[data-stage="check"]').className).toContain("is-running");

    await settle(1600);
    const result = byTestId("sr-check-result");
    expect(result).toBeTruthy();
    expect(byTestId("sr-check-headline").textContent).toContain("第42位/ 100");
    expect(byTestId("sr-check-headline-verdict").textContent).toBe("在作者的正常范围内");
    expect(byTestId("sr-check-headline").textContent).toContain("前 90 位都算作者的正常范围");
    expect(byTestId("sr-check-gaps").textContent).toContain("词汇选择语气词（吧、呢、啊、嘛……）比作者少");
    expect(byTestId("sr-check-judge").textContent).toContain("7.2");
    expect(byTestId("sr-check-judge").textContent).toContain("对白再松一点就更像");
    expect(byTestId("sr-check-copy").textContent).toContain("没有与参考书原文连续相同的地方");
    const dialogue = $('[data-testid="sr-check-dims"] tr[data-dimension="scene.dialogue"]');
    expect(dialogue.textContent).toContain("8.1");
    expect(dialogue.textContent).toContain("对白偏正式");
    expect($('[data-testid="sr-check-dims"] tr[data-dimension="theme.values"]').textContent).toContain("不学");
    expect(result.textContent).not.toMatch(/-2\.6|z 分|注入|回测/);
  });

  it("选一场：作品用着这本书就按作品现在的设置查（不带画像）；没用就按这份画像查", async () => {
    let posted = null;
    await openCheck({ applied: true });
    routeCheck({ post: (body) => { posted = body; return Promise.resolve({ job_id: "job-c", job: JOB("running") }); }, get: () => Promise.resolve({ job: JOB("running") }) });
    await pickScene("sc-2");
    expect(byTestId("sr-check-form").textContent).toContain("按《北岸手记》现在用这本书的设置");
    await click(byTestId("sr-check-start"));
    await settle();
    expect(posted).toEqual({ scene_id: "sc-2", project_id: "w1" });
    expect(byTestId("sr-check-running").textContent).toContain("第 1 章 · 第 2 场「夜渡」");
  });

  it("选一场而作品用的是这本书的旧版全局应用：同样按作品现在的设置查（不带画像）", async () => {
    let posted = null;
    state.projectBinding = {
      project_id: "w1",
      binding: { ...OWN_BINDING, binding_id: "bd-g", scope: "global", scope_ref_id: null },
      profile: PROFILE_SUMMARY,
      book: { book_id: "bk-a", title: "甲书" },
    };
    await openCheck();
    routeCheck({ post: (body) => { posted = body; return Promise.resolve({ job_id: "job-c", job: JOB("running") }); }, get: () => Promise.resolve({ job: JOB("running") }) });
    await pickScene("sc-2");
    expect(byTestId("sr-check-form").textContent).toContain("按《北岸手记》现在用这本书的设置");
    await click(byTestId("sr-check-start"));
    await settle();
    expect(posted).toEqual({ scene_id: "sc-2", project_id: "w1" });
  });
  it("选一场而作品没用这本书：带上这份画像", async () => {
    let posted = null;
    await openCheck();
    routeCheck({ post: (body) => { posted = body; return Promise.resolve({ job_id: "job-c", job: JOB("running") }); }, get: () => Promise.resolve({ job: JOB("running") }) });
    await pickScene("sc-1");
    expect(byTestId("sr-check-form").textContent).toContain("没有用这本书，这次按这份文风画像查");
    await click(byTestId("sr-check-start"));
    await settle();
    expect(posted).toEqual({ scene_id: "sc-1", profile_id: "pf-a", project_id: "w1" });
  });

  it("发起就被拒（没有模型）：说成中文并给「去设置模型」", async () => {
    const go = vi.fn();
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    await mountView(go);
    await openStage("check");
    await settle();
    await typeText("一段文字");
    routeCheck({
      post: () => Promise.reject(Object.assign(new Error("llm required"), { code: "STYLE_REFERENCE_LLM_REQUIRED", status: 409 })),
      get: () => Promise.resolve({}),
    });
    await click(byTestId("sr-check-start"));
    await settle();
    expect(byTestId("sr-check-error").textContent).toContain("对照检查要由模型对着原文样例评审");
    expect(byTestId("sr-check-error").textContent).not.toContain("llm required");
    await click(byTestId("sr-check-error-action"));
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
  });

  it("评审失败：说清楚并给「重新检查」，点了再发一次同样的检查", async () => {
    await openCheck();
    await typeText("一段文字".repeat(200));
    const bodies = [];
    routeCheck({
      post: (body) => { bodies.push(body); return Promise.resolve({ job_id: "job-c", job: JOB("failed", { error: { code: "STYLE_REFERENCE_CHECK_JUDGE_FAILED", message: "judge failed", retryable: true } }) }); },
      get: () => Promise.resolve({}),
    });
    await click(byTestId("sr-check-start"));
    await settle();
    expect(byTestId("sr-check-error").textContent).toContain("模型的参考评审没有完成");
    expect(byTestId("sr-check-error-action").textContent).toBe("重新检查");
    await click(byTestId("sr-check-error-action"));
    await settle();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
  });

  it("没有模型：「开始对照检查」锁住并说明，不白发请求", async () => {
    state.runtime = { llm_enabled: false, llm_is_local: false, default_cloud_policy: "local_only" };
    await openCheck();
    await typeText("一段文字");
    expect(byTestId("sr-check-no-llm").textContent).toContain("还没有接入模型");
    expect(byTestId("sr-check-start").disabled).toBe(true);
  });

  it("换了作品：上一部作品里选的那一场不带过来，按钮也不能点（复核 #7）", async () => {
    let posted = null;
    state.catalogs.w2 = { chapters: [{ chapter_id: "c9", slug: "ch01", no: "01", title: "南风", scenes: [{ scene_id: "sc-9", slug: "sc-9", title: "河口" }] }] };
    await openCheck();
    routeCheck({
      post: (body) => { posted = body; return Promise.resolve({ job_id: "job-c", job: JOB("succeeded", { finished_at: "2026-09-23T10:00:00" }), reading: { ...CHECK_READING, scene_id: "sc-2" } }); },
      get: () => Promise.resolve({}),
    });
    await pickScene("sc-2");
    await click(byTestId("sr-check-start"));
    await settle();
    expect(posted).toEqual({ scene_id: "sc-2", profile_id: "pf-a", project_id: "w1" });
    expect(byTestId("sr-check-result").textContent).toContain("第 1 章 · 第 2 场「夜渡」");
    // 换到另一部作品
    workHolder.current = { id: "w2", title: "南山" };
    await act(async () => { window.dispatchEvent(new CustomEvent("ws:work-changed")); });
    await settle();
    await settle();
    const select = byTestId("sr-check-scene");
    expect($$("option", select).map((o) => o.value)).toEqual(["", "sc-9"]);
    expect(select.value).toBe("");
    expect(byTestId("sr-check-start").disabled).toBe(true);
    // 上一次的结果还挂着，但说清楚那是另一部作品的一场，不冒充当前作品
    expect(byTestId("sr-check-result").textContent).toContain("另一部作品的一场");
    expect(byTestId("sr-check-result").textContent).not.toContain("当前作品的一场");
  });

  it("排队 / 进行中给「取消」：POST …/cancel，条目清掉，「开始对照检查」又能点（清理 C1）", async () => {
    await openCheck();
    await typeText("一段文字".repeat(100));
    routeCheck({
      post: () => Promise.resolve({ job_id: "job-c", job: JOB("running") }),
      get: () => Promise.resolve({ job: JOB("running") }),
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, opts) => (url === `${API}/checks/job-c/cancel` ? Promise.resolve({ job: JOB("cancelled") }) : basePost(url, body, opts)));
    await click(byTestId("sr-check-start"));
    await settle();
    expect(byTestId("sr-check-running")).toBeTruthy();
    expect(byTestId("sr-check-start").disabled).toBe(true);
    await click(byTestId("sr-check-cancel"));
    await settle();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/checks/job-c/cancel`, {});
    expect(byTestId("sr-check-running")).toBeNull();
    expect(byTestId("sr-check-error")).toBeNull();
    expect(byTestId("sr-check-start").disabled).toBe(false);
    expect(byTestId("sr-check-start").textContent).toContain("开始对照检查");
    expect(fidStore.fidCheck("book:bk-a")).toBeNull();
  });

  it("取消时作业已经结束（409）：提示一句，并把结果拿回来", async () => {
    await openCheck();
    await typeText("一段文字".repeat(100));
    routeCheck({
      post: () => Promise.resolve({ job_id: "job-c", job: JOB("running") }),
      get: () => Promise.resolve({ job: JOB("succeeded", { finished_at: "2026-09-23T10:00:00" }), reading: CHECK_READING }),
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, opts) => (url === `${API}/checks/job-c/cancel`
      ? Promise.reject(Object.assign(new Error("check not active"), { code: "STYLE_REFERENCE_CHECK_NOT_ACTIVE", status: 409 }))
      : basePost(url, body, opts)));
    await click(byTestId("sr-check-start"));
    await settle();
    await click(byTestId("sr-check-cancel"));
    await settle();
    await settle();
    expect(window.alert).toHaveBeenCalledWith(expect.stringContaining("已经结束"));
    expect(byTestId("sr-check-result")).toBeTruthy();
    expect(byTestId("sr-check-running")).toBeNull();
  });

  it("出了结果给「再查一次」：按同样的目标再发一次（sr-check-again）", async () => {
    await openCheck();
    await typeText("一段文字".repeat(100));
    const bodies = [];
    routeCheck({
      post: (body) => { bodies.push(body); return Promise.resolve({ job_id: "job-c", job: JOB("succeeded", { finished_at: "2026-09-23T10:00:00" }), reading: CHECK_READING }); },
      get: () => Promise.resolve({}),
    });
    await click(byTestId("sr-check-start"));
    await settle();
    expect(byTestId("sr-check-result")).toBeTruthy();
    await click(byTestId("sr-check-again"));
    await settle();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
  });
});
