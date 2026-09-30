// AI 起草台 · 风格链路提示条、本场参考窗口与「像不像」。
// （2026-09-29 从 ws-scene-run.test.jsx 原样拆出。）
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { readFileSync } from "node:fs";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { installApiRouter, DEFAULT_CHAP, DEFAULT_PROJECT } from "./test-helpers.js";
import {
  T, RUN_STATES_URL, NON_DEMO_PROJECT, TWO_SCENE_CHAP, settleActive, routeRunStates, loadSceneRun,
  mountedRoots, renderRunJobControl, click, deferred, queueSceneIntent,
} from "./ws-scene-run.test-harness.jsx";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiDelete: vi.fn(),
}));
// 任务控制条的两个请求（ws-scene-job-api.js）
vi.mock("./ws-scene-job-api.js", () => ({ cancelRunJob: vi.fn(), getLatestSceneRunJob: vi.fn() }));

/* ---- 2026-09-14 风格保真修补 WP4「作者看得见」：风格链路提示条 + 本场参考窗口 ----
   后端早已算出 STYLE_* notices 与本场实际进入提示的参考书样例窗口，此前没有视图渲染。
   这里验证：规整（只认合法条目）、提示条按严重度着色与中文标签、窗口面板默认收起、
   展开时按区间取原文并缓存、随运行记录持久化、真实场景页能看见。 */
describe("风格链路提示与本场参考窗口（WP4）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(async () => {
    while (mountedRoots.length) {
      const { root, host } = mountedRoots.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
  });

  const NOTICES = [
    { code: "STYLE_FIRST_DRAFT", severity: "info", message: "首稿已按参考作者手笔直接起草。" },
    { code: "STYLE_INJECTION_DEGRADED", severity: "warning", message: "输入预算不足，风格参考前缀被整体裁掉。" },
    { code: "STYLE_PLAGIARISM_HIT", severity: "blocking", message: "风格稿与参考作品原文存在确定性 n-gram 重叠。", hit_count: 2, stage: "style_draft" },
    { code: "STYLE_SOMETHING_NEW", severity: "error", message: "后端新增的提示" },
    { not: "a notice" },
    "garbage",
  ];
  const STYLE_WINDOWS = {
    book_id: "book-1",
    profile_id: "profile-1",
    step: "style_draft",
    windows: [
      { start: 0, end: 59, chapter: 1, position: "opening", paragraph_type: "narration", paragraphs: 60, chars: 3800 },
      { start: 640, end: 662, chapter: 9, position: "closing", paragraph_type: "dialogue", paragraphs: 23, chars: 1510 },
      { start: 5, end: 2 },
      { end: 9 },
    ],
  };
  const PARAGRAPHS_URL = /^\/api\/v2\/style-reference\/books\/([^/]+)\/paragraphs\?start=(\d+)&end=(\d+)$/;

  function routeParagraphs(client, responder) {
    const base = client.apiGet.getMockImplementation();
    const calls = [];
    client.apiGet.mockImplementation((url, options) => {
      const m = PARAGRAPHS_URL.exec(url);
      if (m) {
        calls.push(url);
        return responder({ bookId: m[1], start: Number(m[2]), end: Number(m[3]) }, calls.length);
      }
      return base(url, options);
    });
    return calls;
  }

  it("scnStyleNoticesFrom / scnStyleWindowsFrom：只认 generation_summary 里的合法条目并规整字段名", async () => {
    const { mod } = await loadSceneRun();
    const notices = mod.scnStyleNoticesFrom({ generation_summary: { notices: NOTICES } });
    expect(notices.map((n) => n.code)).toEqual(["STYLE_FIRST_DRAFT", "STYLE_INJECTION_DEGRADED", "STYLE_PLAGIARISM_HIT", "STYLE_SOMETHING_NEW"]);
    expect(notices.map((n) => n.severity)).toEqual(["info", "warning", "error", "error"]);
    expect(notices[2]).toMatchObject({ blocking: true, hitCount: 2, stage: "style_draft" });
    expect(notices[0].blocking).toBe(false);
    expect(mod.scnStyleNoticesFrom({ generation_summary: { notices: "nope" } })).toEqual([]);
    expect(mod.scnStyleNoticesFrom({ generation_summary: null })).toEqual([]);
    expect(mod.scnStyleNoticesFrom(null)).toEqual([]);

    const windows = mod.scnStyleWindowsFrom({ generation_summary: { style_windows: STYLE_WINDOWS } });
    expect(windows).toEqual({
      bookId: "book-1",
      profileId: "profile-1",
      step: "style_draft",
      windows: [
        { start: 0, end: 59, chapter: 1, position: "opening", paragraphType: "narration", paragraphs: 60, chars: 3800 },
        { start: 640, end: 662, chapter: 9, position: "closing", paragraphType: "dialogue", paragraphs: 23, chars: 1510 },
      ],
    });
    expect(mod.scnStyleWindowsFrom({ generation_summary: { style_windows: null } })).toBeNull();
    expect(mod.scnStyleWindowsFrom({ generation_summary: { style_windows: { book_id: "b", windows: [] } } })).toBeNull();
    expect(mod.scnStyleWindowsFrom({ generation_summary: { style_windows: { windows: [{ start: 3, end: 1 }] } } })).toBeNull();
    expect(mod.scnStyleWindowLabel(windows.windows[0])).toBe("第 1 章 · 章首 · 第 1–60 段（3,800 字）");
    expect(mod.scnStyleWindowLabel({ start: 4, end: 4, chapter: 0, position: "", paragraphType: "x_new", paragraphs: 1, chars: 12 })).toBe("第 5–5 段（12 字）");
    // 段落类型、配额、标签用风格参考同一张词表；词表外的段落类型不念给作者
    expect(mod.scnStyleWindowTags(windows.windows[1])).toEqual({ slot: "", tags: [], dimensions: [], paragraphType: "对话为主" });
    expect(mod.scnStyleWindowTags({ paragraphType: "x_new" }).paragraphType).toBe("");
  });

  it("提示条：每条一行、中文标签、按严重度着色；未知 code 回退为「code: message」", async () => {
    const { mod } = await loadSceneRun();
    const view = await renderRunJobControl(mod.SceneStyleNoticeStrip, {
      notices: mod.scnStyleNoticesFrom({ generation_summary: { notices: NOTICES } }),
    });
    const strip = view.host.querySelector('[data-testid="scene-style-notices"]');
    expect(strip).not.toBeNull();
    const rows = Array.from(strip.querySelectorAll("li"));
    expect(rows.length).toBe(4);
    expect(rows[0].className).toContain("sev-info");
    expect(rows[0].dataset.code).toBe("STYLE_FIRST_DRAFT");
    expect(rows[0].textContent).toContain(mod.STYLE_NOTICE_LABELS.STYLE_FIRST_DRAFT);
    expect(rows[0].textContent).toContain("写完先量像不像");
    expect(rows[1].className).toContain("sev-warning");
    expect(rows[1].textContent).toContain(mod.STYLE_NOTICE_LABELS.STYLE_INJECTION_DEGRADED);
    expect(rows[1].textContent).toContain("提示太长，文风被整块挤掉了");
    expect(rows[2].className).toContain("sev-error");
    expect(rows[2].className).toContain("is-blocking");
    expect(rows[2].textContent).toContain("风格稿里有与参考书原文连续相同的地方");
    expect(rows[2].textContent).toContain("共 2 处");
    // 后端原话里的术语不给作者看；「注入」一律说「带入起草」
    expect(strip.textContent).not.toMatch(/n-gram|soft_qc|注入|手笔直接起草。/);
    expect(rows[3].className).toContain("sev-error");
    expect(rows[3].textContent).toContain("STYLE_SOMETHING_NEW: 后端新增的提示");
    expect(rows.every((row) => row.className.includes("scn2-style-notice"))).toBe(true);
  });

  it("提示条：无 notices 时不渲染", async () => {
    const { mod } = await loadSceneRun();
    const empty = await renderRunJobControl(mod.SceneStyleNoticeStrip, { notices: [] });
    expect(empty.host.querySelector('[data-testid="scene-style-notices"]')).toBeNull();
    expect(empty.host.innerHTML).toBe("");
    const missing = await renderRunJobControl(mod.SceneStyleNoticeStrip, {});
    expect(missing.host.querySelector('[data-testid="scene-style-notices"]')).toBeNull();
  });

  it("参考窗口面板：一窗一行、默认收起；展开时按窗口区间取原文并只读展示，收起再展开不重复请求", async () => {
    const { mod, client } = await loadSceneRun();
    const calls = routeParagraphs(client, ({ bookId, start, end }) => Promise.resolve({
      book_id: bookId,
      start,
      end,
      capped: false,
      paragraphs: [
        { paragraph_index: start, paragraph_type: "narration", text: "潮水退去的时候，滩涂上只剩一只鞋。" },
        { paragraph_index: start + 1, paragraph_type: "dialogue", text: "“你来晚了。”" },
      ],
    }));
    const view = await renderRunJobControl(mod.SceneStyleWindowsPanel, {
      styleWindows: mod.scnStyleWindowsFrom({ generation_summary: { style_windows: STYLE_WINDOWS } }),
    });
    const panel = view.host.querySelector('[data-testid="scene-style-windows"]');
    expect(panel).not.toBeNull();
    expect(panel.textContent).toContain("本场参考窗口 · 2");
    expect(panel.textContent).toContain("风格稿");
    const rows = Array.from(panel.querySelectorAll('[data-testid="scene-style-window-row"]'));
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain("第 1 章 · 章首 · 第 1–60 段（3,800 字）");
    expect(rows[0].textContent).toContain("叙述为主");
    expect(rows[1].textContent).toContain("第 9 章 · 章末 · 第 641–663 段（1,510 字）");
    expect(view.host.querySelector('[data-testid="scene-style-window-text"]')).toBeNull();
    expect(calls).toEqual([]);

    const button = rows[0].querySelector("button");
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(button.disabled).toBe(false);
    await click(button);
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-style-window-text"]')?.textContent).toContain("潮水退去的时候，滩涂上只剩一只鞋。");
    }, T);
    expect(calls).toEqual(["/api/v2/style-reference/books/book-1/paragraphs?start=0&end=59"]);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    const text = view.host.querySelector('[data-testid="scene-style-window-text"]');
    expect(text.textContent).toContain("“你来晚了。”");
    expect(text.querySelectorAll("p").length).toBe(2);
    expect(text.querySelector("textarea, input, [contenteditable]")).toBeNull();

    await click(button);
    expect(view.host.querySelector('[data-testid="scene-style-window-text"]')).toBeNull();
    expect(button.getAttribute("aria-expanded")).toBe("false");
    await click(button);
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-style-window-text"]')?.textContent).toContain("潮水退去的时候");
    }, T);
    expect(calls.length).toBe(1);

    // 第二窗独立取回，区间是它自己的
    await click(rows[1].querySelector("button"));
    await vi.waitFor(() => expect(calls.length).toBe(2), T);
    expect(calls[1]).toBe("/api/v2/style-reference/books/book-1/paragraphs?start=640&end=662");
  });

  it("参考窗口面板：无窗口不渲染；参考书不可用时不能展开；取回失败显示错误并可重试；截断有提示", async () => {
    const { mod, client } = await loadSceneRun();
    const none = await renderRunJobControl(mod.SceneStyleWindowsPanel, { styleWindows: null });
    expect(none.host.querySelector('[data-testid="scene-style-windows"]')).toBeNull();
    const emptyList = await renderRunJobControl(mod.SceneStyleWindowsPanel, { styleWindows: { bookId: "b", windows: [] } });
    expect(emptyList.host.querySelector('[data-testid="scene-style-windows"]')).toBeNull();

    const noBook = await renderRunJobControl(mod.SceneStyleWindowsPanel, {
      styleWindows: mod.scnStyleWindowsFrom({ generation_summary: { style_windows: { ...STYLE_WINDOWS, book_id: null } } }),
    });
    const noBookButton = noBook.host.querySelector('[data-testid="scene-style-window-row"] button');
    expect(noBookButton.disabled).toBe(true);
    expect(noBook.host.textContent).toContain("参考书已不可用");

    const calls = routeParagraphs(client, ({ bookId, start, end }, n) => (n === 1
      ? Promise.reject(new Error("后端不可达"))
      : Promise.resolve({ book_id: bookId, start, end: start + 79, capped: true, paragraphs: [{ paragraph_index: start, paragraph_type: "narration", text: "第二次取回成功。" }] })));
    const view = await renderRunJobControl(mod.SceneStyleWindowsPanel, {
      styleWindows: mod.scnStyleWindowsFrom({ generation_summary: { style_windows: STYLE_WINDOWS } }),
    });
    const button = view.host.querySelector('[data-testid="scene-style-window-row"] button');
    await click(button);
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-style-window-text"] [role="alert"]')?.textContent).toContain("后端不可达");
    }, T);
    expect(calls.length).toBe(1);
    await click(button);
    await click(button);
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-style-window-text"]')?.textContent).toContain("第二次取回成功。");
    }, T);
    expect(calls.length).toBe(2);
    expect(view.host.querySelector('[data-testid="scene-style-window-text"]').textContent).toContain("只显示了这一窗的前 80 段");
    expect(view.host.querySelector('[role="alert"]')).toBeNull();
  });

  it("scnHydrateFromBackend：把 notices 与 style_windows 记到运行记录，并随 scnRunSave 持久化", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
          generation_summary: { draft_mode: "style_first", notices: NOTICES, style_windows: STYLE_WINDOWS },
        });
      }
      return baseGet(url, options);
    });
    const restored = await mod.scnHydrateFromBackend("ch01s1", {});
    expect(restored).toBeTruthy();
    expect(restored.styleNotices.map((n) => n.code)).toEqual(["STYLE_FIRST_DRAFT", "STYLE_INJECTION_DEGRADED", "STYLE_PLAGIARISM_HIT", "STYLE_SOMETHING_NEW"]);
    expect(restored.styleWindows.bookId).toBe("book-1");
    expect(restored.styleWindows.windows.length).toBe(2);
    mod.scnRunSave("ch01s1", restored);
    const reloaded = mod.scnRunLoad("ch01s1");
    expect(reloaded.styleNotices.map((n) => n.code)).toEqual(restored.styleNotices.map((n) => n.code));
    expect(reloaded.styleWindows).toEqual(restored.styleWindows);

    // 没有 notices / 窗口的运行：空列表与 null，不是 undefined（场景页据此清掉上一轮的提示）
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。" },
          scene_run_state: { scene_status: "neutral_running" },
          generation_summary: { draft_mode: "neutral_first", notices: [], style_windows: null },
        });
      }
      return baseGet(url, options);
    });
    const plain = await mod.scnHydrateFromBackend("ch01s1", {});
    expect(plain.styleNotices).toEqual([]);
    expect(plain.styleWindows).toBeNull();
  });

  it("scnRun：运行记录带 styleNotices / styleWindows，运行日志写一行提示摘要", async () => {
    const { mod, client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-wp4", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-wp4") {
        return Promise.resolve({ job_id: "job-wp4", scene_id: "s1", status: "completed", current_step: "near_final" });
      }
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
          generation_summary: { draft_mode: "style_first", notices: NOTICES.slice(0, 2), style_windows: STYLE_WINDOWS },
        });
      }
      return baseGet(url);
    });
    vi.useFakeTimers();
    let result;
    try {
      const runPromise = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "", {});
      await vi.runAllTimersAsync();
      result = await runPromise;
    } finally {
      vi.useRealTimers();
    }
    expect(result.styleNotices.map((n) => n.code)).toEqual(["STYLE_FIRST_DRAFT", "STYLE_INJECTION_DEGRADED"]);
    expect(result.styleWindows.windows.length).toBe(2);
    const texts = result.log.map((l) => l.text);
    expect(texts.some((t) => t.includes("风格提示 2 条") && t.includes(mod.STYLE_NOTICE_LABELS.STYLE_INJECTION_DEGRADED))).toBe(true);
    expect(texts.some((t) => t.includes("参考书原文窗口 2 个") && t.includes("5310 字"))).toBe(true);
  });

  it("真实场景页：从 workbench 恢复的风格提示渲染成提示条，本场参考窗口进证据栏并可展开原文", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-page-wp4",
      scene_id: "s1",
      status: "running",
      current_step: "style_running",
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "style_running" },
          generation_summary: { draft_mode: "style_first", notices: NOTICES.slice(0, 3), style_windows: STYLE_WINDOWS },
        });
      }
      return baseGet(url, options);
    });
    const calls = routeParagraphs(client, ({ bookId, start, end }) => Promise.resolve({
      book_id: bookId, start, end, capped: false,
      paragraphs: [{ paragraph_index: start, paragraph_type: "narration", text: "页面里展开的参考原文。" }],
    }));
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-page-wp4");
      expect(view.host.querySelectorAll('[data-testid="scene-style-notices"] li').length).toBe(3);
      expect(view.host.querySelectorAll('[data-testid="scene-style-window-row"]').length).toBe(2);
    }, T);
    const strip = view.host.querySelector('[data-testid="scene-style-notices"]');
    expect(strip.textContent).toContain(mod.STYLE_NOTICE_LABELS.STYLE_PLAGIARISM_HIT);
    expect(strip.querySelector("li.sev-error")).not.toBeNull();
    await click(view.host.querySelector('[data-testid="scene-style-window-row"] button'));
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-style-window-text"]')?.textContent).toContain("页面里展开的参考原文。");
    }, T);
    expect(calls).toEqual(["/api/v2/style-reference/books/book-1/paragraphs?start=0&end=59"]);
  });
});

/* ---- 2026-09-23 风格参考 v3 · P6b：像不像 ----
   起草台证据栏的「像不像」：这次运行一步步量出来的位次与决定（首稿 → 风格步 → 修改稿 → 补丁 → 终稿）、评审最弱的几维、
   和作者不一样的地方、对照检查；风格步 / 补丁的新提示码按 reason 说白话；v3 的参考窗口带梗概与标签。合成数据。 */
describe("像不像（风格参考 v3）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(async () => {
    while (mountedRoots.length) {
      const { root, host } = mountedRoots.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    vi.restoreAllMocks();
  });

  const READ = (over = {}) => ({
    reading_id: "r-first", percentile: 91.2, within_range: false, reliable: true, max_percentile: 90, char_count: 1800,
    emphasized_dimensions: [], excluded_dimensions: [],
    out_of_band: [{ feature: "fw_modal_per_1k", dimension: "language.vocabulary", dimension_label: "词汇选择", direction: "low", phrase: "语气词比作者少", z: -2.4 }],
    dimension_scores: { "language.vocabulary": 5.9 },
    ...over,
  });
  const RUN_FIDELITY = {
    first_draft: READ(),
    style_step: {
      kind: "style_step", decision: "revision_kept", reason: "closer_to_author", dimensions: ["language.vocabulary"],
      dimension_labels: ["词汇选择"], first_percentile: 91.2, revision_percentile: 60.4, bundle_id: "b1",
    },
    revision: READ({ reading_id: "r-rev", percentile: 60.4, within_range: true, out_of_band: [] }),
    patch: { kind: "patch", decision: "reverted", reason: "judge_worse", bundle_id: "b1" },
    final: READ({ reading_id: "r-final", percentile: 58, within_range: true, out_of_band: [{ feature: "punct_dash_per_1k", dimension: "language.punctuation", dimension_label: "标点节奏", direction: "high", phrase: "破折号比作者多", z: 2.2 }] }),
    judge: { overall: 6.8, dimensions: { "scene.dialogue": { score: 5.5, note: "对白偏正式" }, "language.rhetoric": { score: 8, note: "" } } },
  };

  it("新的提示码：风格步 / 补丁的决定按 reason 说白话；参考书的状态四条都认", async () => {
    const { mod } = await loadSceneRun();
    const notices = mod.scnStyleNoticesFrom({
      generation_summary: {
        notices: [
          { code: "STYLE_FIRST_DRAFT_ACCEPTED", severity: "info", reason: "within_author_range", message: "首稿读数在参考作者的正常范围内，风格步没有再调模型" },
          { code: "STYLE_FIRST_DRAFT_ACCEPTED", severity: "info", reason: "reading_unreliable", message: "…" },
          { code: "STYLE_REVISION_REJECTED", severity: "info", reason: "not_closer", message: "定向修改没有让稿子更像参考作者（读数没有变近）" },
          { code: "STYLE_REVISION_REJECTED", severity: "warning", reason: "revision_template_missing", message: "…sync_prompt_templates…" },
          { code: "STYLE_PATCH_REVERTED", severity: "info", reason: "judge_worse", message: "软 QC 的补丁让稿子离参考作者更远" },
          { code: "STYLE_REFERENCE_SAMPLES_BLOCKED", severity: "warning", message: "云端策略不允许" },
          { code: "STYLE_REFERENCE_BOOK_CHANGED", severity: "warning", message: "…" },
          { code: "STYLE_REFERENCE_BOOK_MISSING", severity: "warning", message: "…" },
          { code: "STYLE_REFERENCE_NO_WINDOWS", severity: "warning", message: "…" },
          { code: "STYLE_DRAFT_FALLBACK_NEUTRAL", severity: "warning", draft_mode: "style_first", message: "…" },
        ],
      },
    });
    expect(notices[0].reason).toBe("within_author_range");
    expect(notices[9].draftMode).toBe("style_first");
    const view = await renderRunJobControl(mod.SceneStyleNoticeStrip, { notices });
    const rows = Array.from(view.host.querySelectorAll('[data-testid="scene-style-notices"] li'));
    const text = (i) => rows[i].textContent;
    expect(text(0)).toContain("首稿已在作者范围内，没有再改");
    expect(text(0)).toContain("量下来首稿已经像这位作者");
    expect(text(1)).toContain("首稿量不准，没有再改");
    expect(text(2)).toContain("改了一版没有更像，保留首稿");
    expect(text(3)).toContain("缺「定向修改」模板");
    expect(rows[3].className).toContain("sev-warning");
    expect(text(4)).toContain("补丁没有更像，已退回");
    expect(text(4)).toContain("参考评审分降了");
    expect(text(5)).toContain("这本书的原文不能发给当前模型");
    expect(text(6)).toContain("参考书学完后改过");
    expect(text(7)).toContain("参考书已不在书库里");
    expect(text(8)).toContain("参考书还没有样例片段");
    expect(text(9)).toContain("风格稿没过检查，用回了首稿");
    const all = view.host.textContent;
    expect(all).not.toMatch(/风格步|读数尺子|软 QC|sync_prompt_templates|STYLE_/);
  });

  it("本场参考窗口：v3 的窗带一句话梗概与标签（配额、场面 / 情绪、示范的维度、以哪种段落为主）", async () => {
    const { mod } = await loadSceneRun();
    const styleWindows = mod.scnStyleWindowsFrom({
      generation_summary: {
        style_windows: {
          book_id: "book-1", profile_id: "profile-1", step: "neutral_draft",
          windows: [{
            start: 120, end: 179, chapter: 3, position: "opening", paragraph_type: "narration", paragraphs: 60, chars: 3820,
            window_no: 7, slot: "dimension", situations: ["开章引入"], moods: ["平静"], dimensions: ["language.rhetoric"], gist: "某人在渡口等船",
            devices: ["留白"],
          }],
        },
      },
    });
    // 2026-09-24 O1：窗口标签记的是维度键，不再有 devices（旧字段来了也不收）
    expect(styleWindows.windows[0]).toMatchObject({ windowNo: 7, slot: "dimension", gist: "某人在渡口等船", situations: ["开章引入"], moods: ["平静"], dimensions: ["language.rhetoric"] });
    expect(styleWindows.windows[0].devices).toBeUndefined();
    expect(mod.scnStyleWindowTags(styleWindows.windows[0])).toMatchObject({ slot: "维度示范", dimensions: ["修辞手法"] });
    const view = await renderRunJobControl(mod.SceneStyleWindowsPanel, { styleWindows });
    const row = view.host.querySelector('[data-testid="scene-style-window-row"]');
    expect(row.textContent).toContain("第 3 章 · 章首 · 第 121–180 段（3,820 字）");
    expect(row.querySelector('[data-testid="scene-style-window-gist"]').textContent).toBe("某人在渡口等船");
    for (const word of ["维度示范", "开章引入", "平静", "修辞手法", "叙述为主"]) expect(row.textContent).toContain(word);
    expect(row.textContent).not.toContain("留白");
    expect(view.host.textContent).toContain("（首稿）");
  });

  it("运行记录带这次运行的像不像，随 scnRunSave 持久化；运行日志写一行", async () => {
    const { mod, client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiPost.mockImplementation((url) => (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)
      ? Promise.resolve({ job_id: "job-fid", scene_id: "s1", status: "running" })
      : Promise.resolve({})));
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-fid") return Promise.resolve({ job_id: "job-fid", scene_id: "s1", status: "completed", current_step: "near_final" });
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
          generation_summary: { draft_mode: "style_first", notices: [], style_windows: null, style_fidelity: RUN_FIDELITY },
        });
      }
      return baseGet(url);
    });
    vi.useFakeTimers();
    let result;
    try {
      const runPromise = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "", {});
      await vi.runAllTimersAsync();
      result = await runPromise;
    } finally {
      vi.useRealTimers();
    }
    expect(result.styleFidelity.style_step.decision).toBe("revision_kept");
    const line = result.log.map((l) => l.text).find((t) => t.startsWith("像不像："));
    expect(line).toContain("首稿第 91 位（超出作者的正常范围）");
    expect(line).toContain("按 1 个维度（词汇选择）定向修改并采用：第 91 位 → 第 60 位");
    expect(line).toContain("补丁没有更像");
    expect(line).toContain("终稿第 58 位（在作者的正常范围内）");
    mod.scnRunSave("ch01s1", result);
    expect(mod.scnRunLoad("ch01s1").styleFidelity.final.reading_id).toBe("r-final");
  });

  it("证据栏「像不像」：这次运行的一串、评审最弱的几维、和作者不一样的地方；对照检查跑一次并显示结果", async () => {
    const { client } = await loadSceneRun();
    const scenePayload = { scene_id: "s1", bound: true, readings: { manual: null }, decisions: [], judge: null };
    const baseGet = client.apiGet.getMockImplementation();
    let checkStatus = "running";
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/style-fidelity") return Promise.resolve(scenePayload);
      if (url === "/api/v2/style-reference/checks/job-chk") {
        return Promise.resolve(checkStatus === "running"
          ? { job: { job_id: "job-chk", kind: "check", status: "running", phase: "judge" }, reading: null }
          : { job: { job_id: "job-chk", kind: "check", status: "succeeded" }, reading: { ...READ({ reading_id: "r-man", percentile: 44, within_range: true, stage: "manual" }), judge: { overall: 7.4, summary: "对白再松一点就更像", dimensions: {} } } });
      }
      return baseGet(url);
    });
    const basePost = client.apiPost.getMockImplementation();
    const posted = [];
    client.apiPost.mockImplementation((url, body, opts) => {
      if (url === "/api/v2/style-reference/checks") {
        posted.push(body);
        return Promise.resolve({ job_id: "job-chk", state: "queued", job: { job_id: "job-chk", kind: "check", status: "queued" } });
      }
      return basePost(url, body, opts);
    });
    const { SceneFidelityPanel } = await import("./ws-scene-fidelity.jsx");
    const view = await renderRunJobControl(SceneFidelityPanel, { sceneId: "s1", runFidelity: RUN_FIDELITY, go: vi.fn() });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-fidelity"]')).not.toBeNull(), T);
    const panel = view.host.querySelector('[data-testid="scene-fidelity"]');
    expect(panel.querySelector('[data-testid="scene-fidelity-first"]').textContent).toContain("第 91 位");
    expect(panel.querySelector('[data-testid="scene-fidelity-first"]').textContent).toContain("超出作者的正常范围");
    expect(panel.querySelector('[data-testid="scene-fidelity-step"]').textContent).toContain("按 1 个维度（词汇选择）定向修改并采用：第 91 位 → 第 60 位");
    expect(panel.querySelector('[data-testid="scene-fidelity-revision"]').textContent).toContain("第 60 位");
    expect(panel.querySelector('[data-testid="scene-fidelity-patch"]').textContent).toContain("补丁没有更像（参考评审分降了），已退回补丁前的稿子");
    expect(panel.querySelector('[data-testid="scene-fidelity-final"]').textContent).toContain("在作者的正常范围内");
    const judge = panel.querySelector('[data-testid="scene-fidelity-judge"]');
    expect(judge.textContent).toContain("6.8");
    expect(judge.textContent).toContain("对话写法");
    expect(judge.textContent).toContain("对白偏正式");
    expect(panel.querySelector('[data-testid="scene-fidelity-gaps"]').textContent).toContain("和作者不一样的地方（终稿）");
    expect(panel.querySelector('[data-testid="scene-fidelity-gaps"]').textContent).toContain("破折号比作者多");
    expect(panel.textContent).not.toMatch(/2\.2|-2\.4/);
    expect(panel.querySelector('[data-testid="scene-fidelity-check"]').textContent).toContain("还没对这一场做过对照检查");

    await click(panel.querySelector('[data-testid="scene-fidelity-check-run"]'));
    expect(posted).toEqual([{ scene_id: "s1" }]);
    expect(panel.querySelector('[data-testid="scene-fidelity-check-run"]').disabled).toBe(true);
    checkStatus = "succeeded";
    scenePayload.readings = { manual: { ...READ({ reading_id: "r-man", percentile: 44, within_range: true, stage: "manual" }), judge: { overall: 7.4, summary: "对白再松一点就更像", dimensions: {} } } };
    await vi.waitFor(() => {
      const manual = view.host.querySelector('[data-testid="scene-fidelity-manual"]');
      expect(manual).not.toBeNull();
      expect(manual.textContent).toContain("第 44 位");
      expect(manual.textContent).toContain("7.4");
    }, { timeout: 6000, interval: 50 });
  });

  it("没绑定、也没有这次运行的读数：整块不出现", async () => {
    const { client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === "/api/v1/scenes/s1/style-fidelity"
      ? Promise.resolve({ scene_id: "s1", bound: false, readings: {}, decisions: [], judge: null })
      : baseGet(url)));
    const { SceneFidelityPanel } = await import("./ws-scene-fidelity.jsx");
    const view = await renderRunJobControl(SceneFidelityPanel, { sceneId: "s1", runFidelity: null });
    await vi.waitFor(() => expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/scenes/s1/style-fidelity")).toBe(true), T);
    await act(async () => { await Promise.resolve(); });
    expect(view.host.querySelector('[data-testid="scene-fidelity"]')).toBeNull();
  });

  it("没绑定的场景：有终稿读数也不画「参考评审总分」（润色口径的软 QC 顺手给的分不是像不像）", async () => {
    const { client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === "/api/v1/scenes/s1/style-fidelity"
      ? Promise.resolve({
        scene_id: "s1", bound: false, readings: { final: READ({ reading_id: "r-old-final", percentile: 40, within_range: true }) },
        decisions: [], judge: { overall: 8.2, dimensions: { "scene.dialogue": { score: 8.2, note: "" } } },
      })
      : baseGet(url)));
    const { SceneFidelityPanel } = await import("./ws-scene-fidelity.jsx");
    const view = await renderRunJobControl(SceneFidelityPanel, { sceneId: "s1", runFidelity: null });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-fidelity-final"]')).not.toBeNull(), T);
    expect(view.host.querySelector('[data-testid="scene-fidelity-judge"]')).toBeNull();
    expect(view.host.textContent).not.toContain("参考评审总分");
    expect(view.host.textContent).not.toContain("8.2");
  });

  it("对照检查被拒（没有模型）：说成中文并给「去设置模型」", async () => {
    const { client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => (url === "/api/v1/scenes/s1/style-fidelity"
      ? Promise.resolve({ scene_id: "s1", bound: true, readings: {}, decisions: [], judge: null })
      : baseGet(url)));
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, opts) => (url === "/api/v2/style-reference/checks"
      ? Promise.reject(Object.assign(new Error("llm required"), { code: "STYLE_REFERENCE_LLM_REQUIRED", status: 409 }))
      : basePost(url, body, opts)));
    const go = vi.fn();
    const { SceneFidelityPanel } = await import("./ws-scene-fidelity.jsx");
    const view = await renderRunJobControl(SceneFidelityPanel, { sceneId: "s1", runFidelity: null, go });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-fidelity-check-run"]')).not.toBeNull(), T);
    expect(view.host.textContent).toContain("这一场还没有读数");
    await click(view.host.querySelector('[data-testid="scene-fidelity-check-run"]'));
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-fidelity-check-error"]')).not.toBeNull(), T);
    expect(view.host.querySelector('[data-testid="scene-fidelity-check-error"]').textContent).toContain("对照检查要由模型对着原文样例评审");
    await click(view.host.querySelector('[data-testid="scene-fidelity-check-error-action"]'));
    expect(go).toHaveBeenCalledWith("settings", { type: "ws:settings-tab", detail: "ai" });
  });

  it("采用时被抄袭门拦下：说有几处与参考书原文相同、稿子已保留，不给英文原话", async () => {
    const { copyGateAdoptMessage, isCopyGateError } = await import("./ws-copy-gate.js");
    const error = Object.assign(new Error("reference copy gate blocked adoption — draft is kept and can be revised"), {
      code: "SOURCE_SAFETY_BLOCKED",
      details: { reference_copy: { blocked: true, hit_count: 2, hits: [{ start: 10, end: 24 }, { start: 90, end: 104 }], protected_hit_count: 1, protected_hits: [{ start: 3, end: 5 }] } },
    });
    expect(isCopyGateError(error)).toBe(true);
    const message = copyGateAdoptMessage(error);
    // 只有原文重合会拦；同一稿里的专名只顺带一句「不拦」
    expect(message).toBe("这一稿里有 2 处与参考书原文连续相同，不能采用：改写这些地方（或退回重写）之后再采用。稿子已保留，写作台的正文没有改动。另有 1 处用了参考书里的专名——这一项不拦，定稿前可以换成自己的。");
    expect(isCopyGateError(Object.assign(new Error("x"), { code: "SOURCE_SAFETY_BLOCKED", details: {} }))).toBe(false);
  });
});
