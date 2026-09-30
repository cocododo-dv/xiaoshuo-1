// 风格参考页 · 「参考书活动」面板：只列作业表条目、各类作业的叫法、失败说中文、继续 / 仍然学习 / 重新检查 / 打开 / 取消的按钮规则。
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
  $, $$, API, bookRow, byTestId, click, client, mountView, openStage, PROFILE_SUMMARY, settle, setupStyleRefSuite, state,
} from "./ws-styleref.test-helpers.jsx";

setupStyleRefSuite(workHolder);

describe("参考书活动", () => {
  it("对照检查的条目：叫「对照检查」，做完「打开」落在这本书的「对照检查」", async () => {
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.activity = [{ key: "job:jc", job_id: "jc", kind: "check", status: "succeeded", book_id: "bk-a", title: "甲书", percent: 100 }];
    await mountView();
    await settle(20);
    // 打开页面之前就已结束的条目收在「更早结束的」里
    await click($(".sr-activity-older-toggle"));
    const item = $('[data-activity-key="job:jc"]');
    expect(item.textContent).toContain("对照检查");
    expect($("[data-testid=\"sr-activity-cancel\"]", item)).toBeNull();
    expect($("[data-testid=\"sr-activity-resume\"]", item)).toBeNull();
    const open = [...item.querySelectorAll("button")].find((b) => b.textContent === "打开");
    await click(open);
    await settle();
    expect($(".sr-step.is-active").dataset.stage).toBe("check");
    expect(byTestId("sr-check-form")).toBeTruthy();
  });

  it("只列作业表条目，叫法是「段落分类 / 学习文风」；失败的给「继续学习」", async () => {
    state.activity = [
      { key: "job:j1", job_id: "j1", kind: "learn", status: "failed", book_id: "bk-a", title: "甲书", resumable: true, error: { code: "STYLE_REFERENCE_LLM_REQUIRED" } },
      { key: "job:j2", job_id: "j2", kind: "classify", mode: "import", status: "running", book_id: "bk-a", title: "甲书", percent: 50, cancellable: true },
      { key: "legacy-op-key", kind: "import", status: "running", book_id: "bk-a", title: "甲书" },
    ];
    await mountView();
    await settle(20);
    const panel = byTestId("sr-activity");
    expect($$("[data-activity-key]", panel).map((li) => li.dataset.activityKey).sort()).toEqual(["job:j1", "job:j2"]);
    expect(panel.textContent).toContain("导入 · 段落分类");
    expect(panel.textContent).toContain("学习文风");
    expect(panel.textContent).toContain("没有完成：这一步要用模型");
    client.apiPost.mockResolvedValue({ job_id: "j3" });
    await click(byTestId("sr-activity-resume"));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/learn`, { resume: true });
  });

  it("失败原因是英文原话（作业边界记下的异常串）时说中文（复核 #14）", async () => {
    state.activity = [
      { key: "job:j1", job_id: "j1", kind: "classify", mode: "retype", status: "failed", book_id: "bk-a", title: "甲书", resumable: true, error: { code: "", message: "TypeError: 'NoneType' object is not iterable" } },
    ];
    await mountView();
    await settle(20);
    const item = $('[data-activity-key="job:j1"]');
    expect(item.textContent).toContain("没有完成：出了意外停下了，可以从断点继续。");
    expect(byTestId("sr-activity").textContent).not.toMatch(/TypeError|NoneType/);
  });

  it("「继续学习」沿用同一个作业：条目换成排队、不被关掉；很快跑完的结果照常显示（复核 #15）", async () => {
    state.activity = [
      { key: "job:j1", job_id: "j1", kind: "learn", status: "failed", book_id: "bk-a", title: "甲书", resumable: true, error: { code: "STYLE_REFERENCE_LEARN_LLM_CALL_FAILED", message: "x" } },
    ];
    await mountView();
    await settle(20);
    // 续跑的同一个作业在第一次轮询之前就跑完了
    state.activity = [{ key: "job:j1", job_id: "j1", kind: "learn", status: "succeeded", book_id: "bk-a", title: "甲书", percent: 100, result: { profile_id: "pf-a" } }];
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/learn") ? { job_id: "j1", state: "queued" } : {}));
    await click(byTestId("sr-activity-resume"));
    await settle(30);
    const item = $('[data-activity-key="job:j1"]');
    expect(item, "续跑的条目被当成「关掉过」丢掉了").toBeTruthy();
    expect(item.dataset.activityStatus).toBe("succeeded");
    expect(item.textContent).toContain("完成");
  });

  const READING_A = {
    reading_id: "sfr-9", scene_id: "sc-1", project_id: "w1", profile_id: "pf-a", source: "manual_check", stage: "manual",
    percentile: 41.6, within_range: true, max_percentile: 90, emphasized_dimensions: [], excluded_dimensions: [], reliable: true,
    char_count: 1800, window_count: 40, out_of_band: [], dimension_scores: {}, judge: null, copy_check: { blocked: false, hits: 0, protected_hits: 0 },
  };
  async function typeCheckText(value) {
    const area = byTestId("sr-check-text");
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    await act(async () => {
      setter.call(area, value);
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
  }

  it("进行中的对照检查可以取消（按载荷的 cancellable）：POST …/checks/{id}/cancel；标着不可取消的不给按钮（清理 C1）", async () => {
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.activity = [
      { key: "job:jc1", job_id: "jc1", kind: "check", status: "running", book_id: "bk-a", title: "甲书", percent: 40, cancellable: true },
      { key: "job:jc2", job_id: "jc2", kind: "check", status: "running", book_id: "bk-a", title: "甲书", percent: 40, cancellable: false },
    ];
    await mountView();
    await settle(20);
    // 进度条是 ws-ui 的 ProgressBar：在跑的用 warn 的语气，读屏报名字与百分比
    const bar = $('[data-activity-key="job:jc1"] [role="progressbar"]');
    expect(bar.dataset.tone).toBe("warn");
    expect(bar.getAttribute("aria-valuenow")).toBe("40");
    expect($('[data-activity-key="job:jc2"] [data-testid="sr-activity-cancel"]')).toBeNull();
    const cancel = $('[data-activity-key="job:jc1"] [data-testid="sr-activity-cancel"]');
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/cancel") ? { job: { job_id: "jc1", kind: "check", status: "cancelled" } } : {}));
    await click(cancel);
    await settle();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/checks/jc1/cancel`, {});
  });

  it("做完的对照检查「打开」：落在这本书的「对照检查」并按作业 id 取那次的结果（在起草台发起的也能看到）", async () => {
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    state.activity = [{ key: "job:jc", job_id: "jc", kind: "check", status: "succeeded", book_id: "bk-a", title: "甲书", percent: 100 }];
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === `${API}/checks/jc`
      ? Promise.resolve({ job: { key: "job:jc", job_id: "jc", kind: "check", status: "succeeded", finished_at: "2026-09-23T10:00:00" }, reading: READING_A })
      : baseGet(url)));
    await mountView();
    await settle(20);
    await click($(".sr-activity-older-toggle"));
    await click($('[data-activity-key="job:jc"] [data-testid="sr-activity-open"]'));
    await settle();
    await settle();
    expect($(".sr-step.is-active").dataset.stage).toBe("check");
    expect(client.apiGet.mock.calls.some(([url]) => url === `${API}/checks/jc`)).toBe(true);
    expect(byTestId("sr-check-result")).toBeTruthy();
    expect(byTestId("sr-check-headline").textContent).toContain("第42位/ 100");
    expect(byTestId("sr-check-result").textContent).toContain("从「参考书活动」打开的一次检查");
    // 本机没记着它的目标：没有「再查一次」（不知道要发什么），表单照常可以再发起
    expect(byTestId("sr-check-again")).toBeNull();
    expect(byTestId("sr-check-form")).toBeTruthy();
    // 认领过就清掉：再换到别的步再回来，不会又认领一遍
    await openStage("learn");
    client.apiGet.mockClear();
    await openStage("check");
    await settle();
    expect(client.apiGet.mock.calls.some(([url]) => url === `${API}/checks/jc`)).toBe(false);
    expect(byTestId("sr-check-result")).toBeTruthy();
  });

  it("没做成的对照检查：不记着目标的给「打开」——对照检查页说原因、不给会落空的「重新检查」；本机记着目标的给「重新检查」", async () => {
    state.books = [bookRow({ profile: PROFILE_SUMMARY })];
    const failedJob = (id) => ({ key: `job:${id}`, job_id: id, kind: "check", status: "failed", book_id: "bk-a", title: "甲书", error: { code: "STYLE_REFERENCE_CHECK_JUDGE_FAILED", message: "judge failed", retryable: true } });
    state.activity = [failedJob("jx")];
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === `${API}/checks/jx` ? Promise.resolve({ job: failedJob("jx"), reading: null }) : baseGet(url)));
    await mountView();
    await settle(20);
    const row = $('[data-activity-key="job:jx"]');
    expect(row.textContent).toContain("没有完成：模型的参考评审没有完成");
    expect($('[data-testid="sr-activity-recheck"]', row)).toBeNull();
    await click($('[data-testid="sr-activity-open"]', row));
    await settle();
    await settle();
    expect($(".sr-step.is-active").dataset.stage).toBe("check");
    expect(byTestId("sr-check-error").textContent).toContain("模型的参考评审没有完成");
    expect(byTestId("sr-check-error-action")).toBeNull();

    // 在这一页发起一次并失败：活动清单里这条记着目标，给「重新检查」，按同样的目标再发
    await typeCheckText("一段文字".repeat(100));
    const bodies = [];
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, opts) => {
      if (url !== `${API}/checks`) return basePost(url, body, opts);
      bodies.push(body);
      return Promise.resolve(bodies.length === 1
        ? { job_id: "jy", job: failedJob("jy") }
        : { job_id: "jz", job: { key: "job:jz", job_id: "jz", kind: "check", status: "running", book_id: "bk-a" } });
    });
    state.activity = [failedJob("jy")];
    await click(byTestId("sr-check-start"));
    await settle(20);
    await settle();
    const failedRow = $('[data-activity-key="job:jy"]');
    expect(failedRow, "发起的作业没有登记进活动清单").toBeTruthy();
    expect($('[data-testid="sr-activity-open"]', failedRow)).toBeNull();
    await click($('[data-testid="sr-activity-recheck"]', failedRow));
    await settle();
    expect(bodies).toHaveLength(2);
    expect(bodies[1]).toEqual(bodies[0]);
    expect($('[data-activity-key="job:jy"]')).toBeNull();
    expect($('[data-activity-key="job:jz"]')).toBeTruthy();
    expect(byTestId("sr-check-running")).toBeTruthy();
  });

  it("正文太短而失败的学习：给「仍然学习」（force），不给「继续学习」（后端 resumable=false）（清理 C2）", async () => {
    state.activity = [{ key: "job:j1", job_id: "j1", kind: "learn", status: "failed", book_id: "bk-a", title: "甲书", resumable: false, error: { code: "STYLE_REFERENCE_LEARN_FAILED", message: "正文太少。", details: { reason_code: "input_too_small" } } }];
    await mountView();
    await settle(20);
    const item = $('[data-activity-key="job:j1"]');
    expect($('[data-testid="sr-activity-resume"]', item)).toBeNull();
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/learn") ? { job_id: "j2" } : {}));
    await click($('[data-testid="sr-activity-force-learn"]', item));
    await settle();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/learn`, { force: true });
    expect($('[data-activity-key="job:j1"]')).toBeNull();
    expect($('[data-activity-key="job:j2"]')).toBeTruthy();
  });

  it("不可续跑的失败（resumable=false，不是正文太短）：既没有「继续学习」也没有「仍然学习」，只能关闭", async () => {
    state.activity = [{ key: "job:j1", job_id: "j1", kind: "learn", status: "failed", book_id: "bk-a", title: "甲书", resumable: false, error: { code: "STYLE_REFERENCE_LEARN_FAILED", message: "挑出的窗口里没有正文段。" } }];
    await mountView();
    await settle(20);
    const item = $('[data-activity-key="job:j1"]');
    expect($('[data-testid="sr-activity-resume"]', item)).toBeNull();
    expect($('[data-testid="sr-activity-force-learn"]', item)).toBeNull();
    expect([...item.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["关闭"]);
  });
});
