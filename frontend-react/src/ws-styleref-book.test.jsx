// 风格参考页 · 第一步「参考书」（段落分类：旧版启发式提醒、先报费用再确认的「用模型重新分类」、继续分类）与第二步「学习文风」（估算 / 进度 / 取消、没有模型与仅本机的拦路、续跑 / 仍然学习、出错行的下一步）。
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
  $, API, bookRow, byTestId, click, client, mountView, openStage, PROFILE_SUMMARY, settle, setupStyleRefSuite, unmountAll, state, store,
} from "./ws-styleref.test-helpers.jsx";

setupStyleRefSuite(workHolder);

describe("第一步 · 参考书", () => {
  it("旧版启发式标的类型：提醒并说出一致率；「用模型重新分类（保留画像）」先报费用再确认", async () => {
    state.books = [bookRow({ classification_provenance: { source: "legacy_heuristic", agreement: 0.42, heuristic_paragraphs: 700, llm_paragraphs: 200 } })];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/reclassify") ? { job_id: "job-r", mode: "retype" } : {}));
    await mountView();
    await openStage("book");
    const notice = byTestId("sr-overview-legacy-types");
    expect(notice.textContent).toContain("700 段是旧版导入时用启发式规则标的类型");
    expect(notice.textContent).toContain("只有 42% 一致");
    await click(byTestId("sr-overview-retype"));
    await settle();
    expect(client.apiGet).toHaveBeenCalledWith(`${API}/books/bk-a/classification/estimate`);
    expect(confirm.mock.calls[0][0]).toContain("约 120 次模型调用");
    expect(confirm.mock.calls[0][0]).toContain("文风画像和用在作品上的设置都保留");
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/reclassify`, { mode: "retype" });
  });

  it("确认框取消：不重新分类", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    await openStage("book");
    await click(byTestId("sr-overview-retype"));
    await settle();
    expect(client.apiPost.mock.calls.some(([url]) => url.endsWith("/reclassify"))).toBe(false);
  });

  it("没有模型：「用模型重新分类」锁住并说明", async () => {
    state.runtime = { llm_enabled: false, llm_is_local: false, default_cloud_policy: "local_only" };
    await mountView();
    await openStage("book");
    expect(byTestId("sr-overview-retype").disabled).toBe(true);
    expect(byTestId("sr-overview-model-gate").textContent).toContain("还没有接入模型");
  });

  it("分类没完成：给「继续分类」", async () => {
    state.books = [bookRow({ status: "failed", classification: { batches_done: 3, batches_total: 10, error: { code: "STYLE_REFERENCE_LLM_REQUIRED" } } })];
    await mountView();
    expect($(".sr-step.is-active").dataset.stage).toBe("book");
    expect(byTestId("sr-overview-classify").textContent).toContain("已分好 3/10 批");
    client.apiPost.mockResolvedValueOnce({ job_id: "job-c", mode: "reclassify" });
    await click(byTestId("sr-overview-resume"));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/reclassify`, { resume: true });
  });

  it("「用模型重新分类」失败了（书一直是 ready）：说清楚、给「继续分类」，不是只剩整本重付的重新分类（复核 #8）", async () => {
    state.books = [bookRow({
      profile: PROFILE_SUMMARY,
      classification: {
        job_id: "job-rt", mode: "retype", state: "failed", batches_done: 4, batches_total: 10, resumable: true,
        error: { code: "STYLE_REFERENCE_JOB_FAILED", message: "RuntimeError: relay closed the connection" },
      },
    })];
    await mountView();
    // 参考书一步「需处理」：落点就在这里
    expect($(".sr-step.is-active").dataset.stage).toBe("book");
    expect($('.sr-step[data-stage="book"]').getAttribute("aria-label")).toBe("参考书（需处理）");
    const card = byTestId("sr-overview-classify");
    expect(card.textContent).toContain("重新分类没完成");
    expect(card.textContent).not.toContain("模型已分好");
    expect(byTestId("sr-overview-retype-unfinished").textContent).toContain("已分好 4/10 批，继续分类只补剩下的");
    expect(byTestId("sr-overview-classify-reason").textContent).toBe("原因：后台作业出了意外停下了，可以从断点继续。");
    expect(card.textContent).not.toContain("RuntimeError");
    expect(byTestId("sr-overview-retype").textContent).toBe("从头重新分类");
    client.apiPost.mockResolvedValueOnce({ job_id: "job-rt", mode: "retype" });
    await click(byTestId("sr-overview-resume"));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/reclassify`, { resume: true });
    expect(client.apiPost.mock.calls.some(([url, body]) => url.endsWith("/reclassify") && body && body.mode === "retype")).toBe(false);
  });

  it("取消了的「用模型重新分类」同样能接着分", async () => {
    state.books = [bookRow({ classification: { job_id: "job-rt", mode: "retype", state: "cancelled", batches_done: 2, batches_total: 8, resumable: true, error: { code: "STYLE_REFERENCE_JOB_CANCELLED", message: "cancelled" } } })];
    await mountView();
    await openStage("book");
    expect(byTestId("sr-overview-classify").textContent).toContain("重新分类已取消");
    expect(byTestId("sr-overview-retype-unfinished").textContent).toContain("被取消了");
    expect(byTestId("sr-overview-classify-reason")).toBeNull();
    expect(byTestId("sr-overview-resume")).toBeTruthy();
  });

  it("「继续分类」被拒（正在学习文风）：出错行说中文原因、不给英文（sr-overview-error）", async () => {
    state.books = [bookRow({
      status: "failed",
      classification: { job_id: "jc", state: "failed", mode: "import", batches_done: 2, batches_total: 5, resumable: true, error: { code: "STYLE_REFERENCE_CLASSIFY_LLM_CALL_FAILED", message: "llm call failed" } },
    })];
    client.apiPost.mockImplementation((url) => (url.endsWith("/reclassify")
      ? Promise.reject(Object.assign(new Error("book learning"), { code: "STYLE_REFERENCE_BOOK_LEARNING", status: 409 }))
      : Promise.resolve({})));
    await mountView();
    expect(byTestId("sr-overview-classify-reason").textContent).toContain("分类时模型调用失败");
    await click(byTestId("sr-overview-resume"));
    await settle();
    expect(byTestId("sr-overview-error").textContent).toContain("这本书正在学习文风：等学完再重新分类。");
    expect(byTestId("sr-overview-error").textContent).not.toContain("book learning");
    expect(document.body.textContent).not.toContain("llm call failed");
  });
});

describe("第二步 · 学习文风", () => {
  it("没学过：按钮 + 估算；开始后显示进度，可取消", async () => {
    await mountView();
    expect(byTestId("sr-learn-status").textContent).toBe("还没学");
    expect(byTestId("sr-learn-estimate").textContent).toContain("约 12 次模型调用");
    expect(byTestId("sr-portrait-empty")).toBeTruthy();
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/learn") ? { job_id: "job-l" } : {}));
    state.activity = [{ key: "job:job-l", job_id: "job-l", kind: "learn", status: "running", book_id: "bk-a", percent: 30, phase_label: "学习文风 · 分层读原文", steps: { done: 1, total: 4 } }];
    await click(byTestId("sr-learn-start"));
    await settle(20);
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/learn`, {});
    expect(byTestId("sr-learn-running")).toBeTruthy();
    expect(byTestId("sr-learn-running").textContent).toContain("分层读原文 1/4");
    expect(byTestId("sr-learn-status").textContent).toBe("学习中 30%");
    await click(byTestId("sr-learn-cancel"));
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/learn/cancel`, {});
  });

  it("没有模型：错误说成中文，并给「去设置模型」", async () => {
    const go = vi.fn();
    client.apiPost.mockImplementation((url) => (url.endsWith("/learn")
      ? Promise.reject(Object.assign(new Error("llm required"), { code: "STYLE_REFERENCE_LLM_REQUIRED", details: { author_action: { view: "systemConfig" } } }))
      : Promise.resolve({})));
    await mountView(go);
    await click(byTestId("sr-learn-start"));
    await settle();
    expect(byTestId("sr-learn-error").textContent).toContain("这一步要用模型，但还没有接入可用的模型。");
    expect(byTestId("sr-learn-error").textContent).not.toContain("llm required");
    await click(byTestId("sr-learn-error-action"));
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
  });

  it("先看得到的拦路：没有模型时「学习文风」锁住并给「去设置模型」，不白发一次请求", async () => {
    const go = vi.fn();
    state.runtime = { llm_enabled: false, llm_is_local: false, default_cloud_policy: "local_only" };
    await mountView(go);
    expect(byTestId("sr-learn-no-llm").textContent).toContain("还没有接入模型");
    expect(byTestId("sr-learn-start").disabled).toBe(true);
    await click($('[data-testid="sr-learn-no-llm"] button'));
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
    expect(client.apiPost.mock.calls.some(([url]) => url.endsWith("/learn"))).toBe(false);
  });

  it("去设置里接好模型再回来（页面重新挂载）：重读运行时，学习文风不再锁着（复核 #2）", async () => {
    state.runtime = { llm_enabled: false, llm_is_local: false, default_cloud_policy: "local_only" };
    await mountView();
    expect(byTestId("sr-learn-no-llm")).toBeTruthy();
    expect(byTestId("sr-learn-start").disabled).toBe(true);
    // 去设置（页面卸载），接好模型，再回来——同一次打开应用，store 没有清空
    await unmountAll();
    state.runtime = { llm_enabled: true, llm_is_local: false, default_cloud_policy: "allow_full_cloud" };
    await mountView();
    await settle();
    expect(byTestId("sr-learn-no-llm")).toBeNull();
    expect(byTestId("sr-learn-start").disabled).toBe(false);
  });

  it("活动清单读不到、书库摘要说在学：照样显示「学习中」（复核 #10）", async () => {
    state.books = [bookRow({ learn: { job_id: "job-l7", state: "running", done: 3, total: 7, phase_label: "学习文风 · 给片段打标签" } })];
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === `${API}/activity`
      ? Promise.reject(Object.assign(new Error("down"), { code: "NETWORK_ERROR" }))
      : baseGet(url)));
    await mountView();
    expect(byTestId("sr-learn-status").textContent).toBe("学习中 43%");
    expect(byTestId("sr-learn-running").textContent).toContain("给片段打标签 3/7");
    expect($('[data-activity-key="job:job-l7"]')).toBeTruthy();
  });

  it("上次学习失败的原因只说中文：作业边界记下的英文原话不给作者看（复核 #14）", async () => {
    state.books = [bookRow({ learn: { job_id: "job-l8", state: "failed", resumable: true, error: { code: "STYLE_REFERENCE_JOB_FAILED", message: "KeyError: 'windows'" } } })];
    state.learn = { ...state.learn, learn: { job_id: "job-l8", state: "failed", resumable: true, error: { code: "", message: "KeyError: 'windows'" } } };
    await mountView();
    const line = byTestId("sr-learn-last-error").textContent;
    expect(line).toBe("上次学习没有完成：出了意外停下了，可以从断点继续。");
    expect(document.body.textContent).not.toContain("KeyError");
  });

  it("「仅本机模型」的书、学习节点在云端：说清楚并锁住", async () => {
    state.books = [bookRow({ cloud_policy: "local_only" })];
    state.learn = { ...state.learn, routes: [{ node_id: "style_ref_extract_language", local: false }, { node_id: "style_ref_tag_windows", local: true }] };
    await mountView();
    expect(byTestId("sr-learn-cloud-blocked").textContent).toContain("学习用的模型不在本机");
    expect(byTestId("sr-learn-start").disabled).toBe(true);
  });

  it("段落类型更新过：建议重新学习；重新学习先确认", async () => {
    state.books = [bookRow({ profile: { ...PROFILE_SUMMARY, needs_relearn: true, relearn_reason: "types_changed" } })];
    state.profile = { ...state.profile, needs_relearn: true, relearn_reason: "types_changed" };
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await mountView();
    expect($(".sr-step.is-active").dataset.stage).toBe("learn");
    expect(byTestId("sr-learn-relearn").textContent).toContain("段落类型已更新，建议重新学习");
    await click(byTestId("sr-learn-start"));
    await settle();
    expect(confirm.mock.calls[0][0]).toContain("重新学习《甲书》的文风？");
    expect(client.apiPost.mock.calls.some(([url]) => url.endsWith("/learn"))).toBe(false);
  });

  it("上次学习失败且不可续跑（后端 resumable=false）：只给「重新学习」，不给「继续学习」（清理 C2）", async () => {
    const failed = { job_id: "job-l9", state: "failed", resumable: false, error: { code: "STYLE_REFERENCE_LEARN_FAILED", message: "挑出的窗口里没有正文段。" } };
    state.books = [bookRow({ learn: failed })];
    state.learn = { ...state.learn, learn: failed };
    await mountView();
    expect(byTestId("sr-learn-status").textContent).toBe("学习没有完成");
    expect(byTestId("sr-learn-resume")).toBeNull();
    expect(byTestId("sr-learn-force")).toBeNull();
    expect(byTestId("sr-learn-start").textContent).toContain("重新学习");
    expect(byTestId("sr-learn-last-error").textContent).toBe("上次学习没有完成：挑出的窗口里没有正文段。");
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/learn") ? { job_id: "job-l10" } : {}));
    await click(byTestId("sr-learn-start"));
    await settle();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/learn`, {});
  });

  it("上次学习失败但能续（resumable=true）：「继续学习」照旧在", async () => {
    const failed = { job_id: "job-l9", state: "failed", resumable: true, error: { code: "STYLE_REFERENCE_LEARN_LLM_CALL_FAILED", message: "x" } };
    state.books = [bookRow({ learn: failed })];
    state.learn = { ...state.learn, learn: failed };
    await mountView();
    expect(byTestId("sr-learn-resume").textContent).toContain("继续学习");
    expect(byTestId("sr-learn-force")).toBeNull();
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/learn") ? { job_id: "job-l9" } : {}));
    await click(byTestId("sr-learn-resume"));
    await settle();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/learn`, { resume: true });
  });

  it("正文太短而失败（reason_code input_too_small）：另给「仍然学习（正文很短）」= force，不给「继续学习」", async () => {
    const failed = { job_id: "job-l9", state: "failed", resumable: false, error: { code: "STYLE_REFERENCE_LEARN_FAILED", message: "正文太少，学不出可靠的文风。", details: { reason_code: "input_too_small" } } };
    state.books = [bookRow({ learn: failed })];
    state.learn = { ...state.learn, learn: failed };
    await mountView();
    expect(byTestId("sr-learn-resume")).toBeNull();
    expect(byTestId("sr-learn-last-error").textContent).toContain("正文太少");
    const force = byTestId("sr-learn-force");
    expect(force.textContent).toContain("仍然学习（正文很短）");
    client.apiPost.mockImplementation((url) => Promise.resolve(url.endsWith("/learn") ? { job_id: "job-l11" } : {}));
    await click(force);
    await settle();
    expect(client.apiPost).toHaveBeenCalledWith(`${API}/books/bk-a/learn`, { force: true });
    expect(client.apiPost.mock.calls.filter(([url]) => url.endsWith("/learn"))).toHaveLength(1);
  });

  it("建作业时就被拒「正文太少」（409）：出错行说中文并给「仍然学习」，点了就地发 force", async () => {
    await mountView();
    const posts = [];
    client.apiPost.mockImplementation((url, body) => {
      if (!url.endsWith("/learn")) return Promise.resolve({});
      posts.push(body);
      return body && body.force
        ? Promise.resolve({ job_id: "job-l12" })
        : Promise.reject(Object.assign(new Error("input too small"), { code: "STYLE_REFERENCE_INPUT_TOO_SMALL", status: 409, details: { book_id: "bk-a" } }));
    });
    await click(byTestId("sr-learn-start"));
    await settle();
    expect(byTestId("sr-learn-error").textContent).toContain("正文太少");
    expect(byTestId("sr-learn-error").textContent).not.toContain("input too small");
    expect(byTestId("sr-learn-error-action").textContent).toBe("仍然学习");
    await click(byTestId("sr-learn-error-action"));
    await settle();
    expect(posts).toEqual([{}, { force: true }]);
  });

  it("出错行按后端的 author_action 给按钮：resume_learning → 「继续学习」直接续跑（清理 C5）", async () => {
    await mountView();
    const posts = [];
    client.apiPost.mockImplementation((url, body) => {
      if (!url.endsWith("/learn")) return Promise.resolve({});
      posts.push(body);
      return body && body.resume
        ? Promise.resolve({ job_id: "job-l20" })
        : Promise.reject(Object.assign(new Error("already active"), { code: "STYLE_REFERENCE_LEARN_ALREADY_ACTIVE", status: 409, details: { author_action: { action: "resume_learning", book_id: "bk-a" } } }));
    });
    await click(byTestId("sr-learn-start"));
    await settle();
    expect(byTestId("sr-learn-error").textContent).toContain("已经在学习文风了");
    expect(byTestId("sr-learn-error-action").textContent).toBe("继续学习");
    await click(byTestId("sr-learn-error-action"));
    await settle();
    expect(posts).toEqual([{}, { resume: true }]);
    expect($(".sr-step.is-active").dataset.stage).toBe("learn");
  });

  it("出错行按后端的 author_action 给按钮：review_book → 「查看这本书」落到那本书的总览（清理 C5）", async () => {
    state.books = [bookRow(), bookRow({ book_id: "bk-b", title: "乙书" })];
    await mountView();
    client.apiPost.mockImplementation((url) => (url.endsWith("/learn")
      ? Promise.reject(Object.assign(new Error("not ready"), { code: "STYLE_REFERENCE_BOOK_NOT_READY", status: 409, details: { author_action: { action: "review_book", book_id: "bk-b" } } }))
      : Promise.resolve({})));
    await click(byTestId("sr-learn-start"));
    await settle();
    expect(byTestId("sr-learn-error-action").textContent).toBe("查看这本书");
    await click(byTestId("sr-learn-error-action"));
    await settle();
    await settle();
    expect($(".sr-stage-title").textContent).toBe("乙书");
    expect($(".sr-step.is-active").dataset.stage).toBe("book");
    expect(byTestId("sr-overview-classify")).toBeTruthy();
  });

  it("分类还没完成：学习卡说明并给「去看段落分类」（sr-learn-blocked）", async () => {
    state.books = [bookRow({ status: "ingesting" })];
    await mountView();
    await openStage("learn");
    expect(byTestId("sr-learn-blocked").textContent).toContain("分完才能学");
    expect(byTestId("sr-learn-start")).toBeNull();
    await click($('[data-testid="sr-learn-blocked"] button'));
    await settle();
    expect($(".sr-step.is-active").dataset.stage).toBe("book");
  });
});
