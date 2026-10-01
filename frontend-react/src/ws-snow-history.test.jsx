// 构思 → 历史 →「服务器上保存的版本」（R15a，批准 #24a）：列出这一步在服务器上的每一版（不带草稿），预览时按版本取草稿，
// 恢复走 POST …/steps/{key}/restore → SnowSync.applyServerStep → 本机内容整份换掉并立刻落盘。
// 这里用真的同步层（SnowSync / 上行 / 水合）、假的 client：要守的是「恢复之后的下一次自动保存不会把恢复前的旧文字推回去」，
// 只有把视图和上行接在一起才看得见。
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { installApiRouter } from "./test-helpers.js";

vi.mock("./lib/client.js", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  apiPatch: vi.fn(),
  apiPut: vi.fn(),
  apiDelete: vi.fn(),
}));

const T = { timeout: 5000, interval: 25 };
const WORK = "prj-main";
const CACHE_KEY = `ws_snow_state_v2::${WORK}`;
const NOW = "这是现在的一句话。";
const OLD = "这是旧的一句话。";

const step = (stepKey, extra = {}) => ({
  step_key: stepKey, status: "approved", gate_satisfied: true, version: 1, revised_after_approval: false,
  draft: {}, health: {}, completeness: {}, artifact: { step_run_id: `run_${stepKey}_1`, input_refs: {} }, ...extra,
});
const WORKSPACE = {
  ready_to_materialize: false,
  steps: [
    step("book_brief", { draft: { category: "文学悬疑", target_reader: "合成读者" } }),
    step("one_sentence_summary", { version: 3, draft: { summary: NOW }, artifact: { step_run_id: "run_log_3", input_refs: {} } }),
  ],
};
const VERSIONS = [
  { step_run_id: "run_log_3", version: 3, status: "approved", generation_source: "author", updated_at: "2026-09-30T08:00:00+00:00" },
  { step_run_id: "run_log_2", version: 2, status: "superseded", generation_source: "llm", updated_at: "2026-09-29T08:00:00+00:00" },
];
const DRAFTS = { run_log_2: { summary: OLD, fe_text: "（这一版写穿的旧缓存，不读）" }, run_log_3: { summary: NOW } };

const mounted = [];
let client = null;
let calls = null;
let seq = 0;   // 调用的先后（毫秒时钟分不清同一毫秒里的两次调用）

/* 假后端：工作台、版本列表（不带草稿）、按版本取草稿、恢复、自动保存的 PATCH。PATCH 的回包像真的一样记着每一步在服务器上
   的状态：内容没变的已确认步还是已确认；恢复出来的那一版是待确认、且「确认过又改了」（revised_after_approval）。 */
async function boot({ versions = VERSIONS, restore = null, workspace = WORKSPACE } = {}) {
  client = await import("./lib/client.js");
  installApiRouter(client, { snowflakeWorkspace: workspace });
  const route = client.apiGet.getMockImplementation();
  calls = { history: [], restore: [], approve: [], patch: [] };
  const server = Object.fromEntries(workspace.steps.map(st => [st.step_key, { status: st.status, revised: false }]));
  client.apiGet.mockImplementation((url) => {
    const m = /\/snowflake-workspace\/steps\/([^/?]+)\/history(\?.*)?$/.exec(url);
    if (!m) return route(url);
    calls.history.push(url);
    const query = new URLSearchParams((m[2] || "").slice(1));
    const runId = query.get("step_run_id");
    if (runId) {
      const hit = versions.find(v => v.step_run_id === runId);
      return Promise.resolve({ items: hit ? [{ ...hit, ...(query.get("include_draft") === "true" ? { draft: DRAFTS[runId] || {} } : {}) }] : [] });
    }
    return Promise.resolve({ project_id: WORK, step_key: m[1], items: versions });
  });
  client.apiPost.mockImplementation(async (url, body) => {
    if (/\/restore$/.test(url)) {
      calls.restore.push({ url, body, seq: ++seq });
      if (restore) return restore(url, body);
      server.one_sentence_summary = { status: "pending_review", revised: true };
      return {
        step: step("one_sentence_summary", { status: "pending_review", revised_after_approval: true, version: 4,
          draft: { summary: OLD }, artifact: { step_run_id: "run_log_4", input_refs: {} } }),
        workspace: { ...workspace, steps: workspace.steps },
        step_run: { step_run_id: "run_log_4", restored_from_step_run_id: body.step_run_id },
        restored_from: VERSIONS[1],
      };
    }
    if (/\/approve$/.test(url)) calls.approve.push({ url, seq: ++seq });
    return {};
  });
  client.apiPatch.mockImplementation(async (url, body) => {
    calls.patch.push({ url, body, seq: ++seq });
    const key = String(url).split("/steps/")[1].split("?")[0];
    const st = server[key] || { status: "pending_review", revised: false };
    return { step: step(key, { status: st.status, revised_after_approval: st.revised, draft: body.draft }) };
  });
  const sync = await import("./ws-snow-sync.jsx");
  await vi.waitFor(() => expect(window.WsWorks && window.WsWorks.activeId()).toBe(WORK), T);
  await sync.SnowSync.refetch(WORK);
  expect(sync.SnowSync.hydrated(WORK)).toBe(true);
  const { WsSnowflake } = await import("./ws-snow.jsx");
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  mounted.push({ root, host });
  await act(async () => root.render(<WsSnowflake initialStep="logline" />));
  return { host, sync };
}

const textarea = (host) => host.querySelector("textarea.edit-text");
const openHistory = async (host) => {
  const tab = [...host.querySelectorAll('[role="tab"]')].find(b => b.textContent.includes("历史"));
  await act(async () => tab.click());
  await vi.waitFor(() => expect(host.querySelectorAll('[data-testid="snow-version-row"]').length).toBeGreaterThan(0), T);
};
const previewV2 = async (host) => {
  const button = host.querySelector('[data-testid="snow-version-preview"]');
  await act(async () => button.click());
  await vi.waitFor(() => expect(document.querySelector('[data-testid="snow-version-old"]')).toBeTruthy(), T);
};
const openEdit = async (host) => {
  const tab = [...host.querySelectorAll('[role="tab"]')].find(b => b.textContent.trim() === "编辑");
  await act(async () => tab.click());
};
const sleep = (ms) => act(async () => { await new Promise(resolve => setTimeout(resolve, ms)); });

describe("构思 · 历史 · 服务器上保存的版本（R15a）", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });
  afterEach(async () => {
    while (mounted.length) {
      const { root, host } = mounted.pop();
      await act(async () => root.unmount());
      host.remove();
    }
    // 卸载时视图落盘、排下一次上行（700ms 防抖）：等这一用例的同步层实例把它跑完，别让它在下一个用例里读到
    // 新用例的本机缓存、往同一个假 client 上再推一次（resetModules 换的是模块实例，窗口与 client 是共用的）
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 900)); });
    vi.restoreAllMocks();
  });

  it("列出这一步的版本（列表不带草稿）；预览按版本取草稿，两栏是带栏名的分步文本、不是写穿缓存", async () => {
    const { host } = await boot();
    expect(textarea(host).value).toBe(NOW);
    await openHistory(host);
    expect(calls.history[0]).toBe(`/api/v2/projects/${WORK}/snowflake-workspace/steps/one_sentence_summary/history`);
    const rows = [...host.querySelectorAll('[data-testid="snow-version-row"]')].map(r => r.textContent);
    expect(rows[0]).toContain("第 3 版");
    expect(rows[0]).toContain("已确认");
    expect(rows[0]).toContain("现在的版本");
    expect(rows[1]).toContain("第 2 版");
    expect(rows[1]).toContain("已被新版取代");
    expect(rows[1]).toContain("AI 生成");
    // 本机的操作记录仍在下面
    expect(host.querySelector(".sf-history-local").textContent).toContain("本机的操作记录");

    await previewV2(host);
    expect(calls.history[1]).toBe(`/api/v2/projects/${WORK}/snowflake-workspace/steps/one_sentence_summary/history?step_run_id=run_log_2&include_draft=true`);
    const dialog = document.querySelector('[data-testid="snow-version-dialog"]');
    expect(dialog.getAttribute("role")).toBe("dialog");
    expect(document.querySelector('[data-testid="snow-version-old"]').textContent).toBe(OLD);
    expect(document.querySelector('[data-testid="snow-version-cur"]').textContent).toBe(NOW);
    // 02 不是会动场景 / 章表的步骤：没有额外的后果提示
    expect(document.querySelector('[data-testid="snow-version-warning"]')).toBeNull();
    expect(calls.restore).toHaveLength(0);
  });

  it("恢复：本机这一步换成那一版、显示「已改动 · 待重新确认」、操作记录留一份恢复前的快照；之后的自动保存不把旧文字推回去，也不自动补批", async () => {
    const { host } = await boot();
    await openHistory(host);
    await previewV2(host);
    await act(async () => document.querySelector('[data-testid="snow-version-restore"]').click());
    await vi.waitFor(() => expect(textarea(host).value).toBe(OLD), T);

    expect(calls.restore).toHaveLength(1);
    expect(calls.restore[0].url).toBe(`/api/v2/projects/${WORK}/snowflake-workspace/steps/one_sentence_summary/restore`);
    expect(calls.restore[0].body).toEqual({ step_run_id: "run_log_2" });
    expect(document.querySelector('[data-testid="snow-version-dialog"]')).toBeNull();
    expect(host.querySelector('[data-testid="snow-reconfirm-pill"]')).toBeTruthy();
    expect(host.querySelector('[data-testid="undo-toast"]').textContent).toContain("回到第 2 版");
    // 本机缓存当场就是恢复后的内容（不等 450ms 的防抖）
    expect(JSON.parse(window.localStorage.getItem(CACHE_KEY)).drafts.logline).toBe(OLD);

    // 自动保存（450ms 落盘 + 700ms 上行防抖）跑完：恢复（POST restore）之后的上行只带恢复后的文字；没有补批。
    // 恢复之前的那次上行是故意的——先把此刻的内容存上服务器，它成为上一版、恢复之后也找得回
    await sleep(1600);
    const restoredAt = calls.restore[0].seq;
    const after = calls.patch.filter(c => c.seq > restoredAt && c.url.includes("/steps/one_sentence_summary"));
    after.forEach(c => expect(c.body.draft.summary).toBe(OLD));
    expect(calls.approve.filter(c => c.seq > restoredAt && c.url.includes("/steps/one_sentence_summary/"))).toHaveLength(0);
    expect(textarea(host).value).toBe(OLD);

    // 本机的操作记录：一条「从服务器恢复」，带着恢复前的快照（可以回滚）
    const tab = [...host.querySelectorAll('[role="tab"]')].find(b => b.textContent.includes("历史"));
    await act(async () => tab.click());
    const row = [...host.querySelectorAll(".hist-row")].find(r => r.textContent.includes("从服务器恢复"));
    expect(row).toBeTruthy();
    expect(row.textContent).toContain("第 2 版");
    expect(row.querySelector(".hist-restore")).toBeTruthy();
  });

  it("恢复的回包慢：排着的那次上行读到的已经是恢复后的内容——不会拿恢复前的文字配上那一版的服务端字段推回去", async () => {
    // 01 的规范草稿里有前端没有输入框的字段（safety_rules）：上行时它取服务端镜像——恢复之后镜像是旧版的。
    // 本机缓存要是等 450ms 防抖才换，700ms 的上行定时器读到的还是恢复前的文字，合出一份「现在的文字 + 旧版的规矩」推上去。
    const NOW_BRIEF = { category: "文学悬疑", target_reader: "现在的读者画像", safety_rules: ["现在的规矩"] };
    const OLD_BRIEF = { category: "文学悬疑", target_reader: "旧的读者画像", safety_rules: ["旧的规矩"] };
    const workspace = { ...WORKSPACE, steps: [
      step("book_brief", { version: 2, draft: NOW_BRIEF, artifact: { step_run_id: "run_brief_2", input_refs: {} } }),
      WORKSPACE.steps[1],
    ] };
    DRAFTS.run_brief_1 = OLD_BRIEF;
    const versions = [
      { step_run_id: "run_brief_2", version: 2, status: "approved", generation_source: "author", updated_at: "2026-09-30T08:00:00+00:00" },
      { step_run_id: "run_brief_1", version: 1, status: "superseded", generation_source: "author", updated_at: "2026-09-29T08:00:00+00:00" },
    ];
    const slowRestore = async () => {
      await new Promise(resolve => setTimeout(resolve, 400));
      return { step: step("book_brief", { status: "pending_review", revised_after_approval: true, version: 3, draft: OLD_BRIEF,
        artifact: { step_run_id: "run_brief_3", input_refs: {} } }) };
    };
    const { host } = await boot({ versions, workspace, restore: slowRestore });
    await act(async () => host.querySelector('[data-testid="snow-step-audience"]').click());
    await openHistory(host);
    await previewV2(host);
    await act(async () => document.querySelector('[data-testid="snow-version-restore"]').click());
    await vi.waitFor(() => expect(JSON.parse(window.localStorage.getItem(CACHE_KEY)).scaffolds.audience.reader).toBe("旧的读者画像"), T);
    await sleep(1600);
    const restoredAt = calls.restore[0].seq;
    const after = calls.patch.filter(c => c.seq > restoredAt && c.url.includes("/steps/book_brief"));
    after.forEach(c => expect(c.body.draft.target_reader).toBe("旧的读者画像"));
    expect(after.some(c => c.body.draft.target_reader === "现在的读者画像")).toBe(false);
  });

  it("07 的版本预览：先说清只恢复五段展开的文字；章节表两边都是现在的分章（恢复不动章表）", async () => {
    const chapters = [{ row_uid: "cr1", chapter_seq: 1, act: 1, title: "合成一章", summary: "现在的章摘要", spine: "灾一", chapter_goal: "" }];
    const workspace = { ...WORKSPACE, steps: [
      ...WORKSPACE.steps,
      step("long_synopsis", { version: 2, draft: { paragraphs: ["现在的铺垫展开", "", "", "", ""], chapters }, artifact: { step_run_id: "run_out_2", input_refs: {} } }),
    ] };
    DRAFTS.run_out_1 = { paragraphs: ["旧的铺垫展开", "", "", "", ""], chapters: [{ row_uid: "old1", chapter_seq: 1, act: 1, title: "旧章表里的一章", summary: "", spine: "" }] };
    const versions = [
      { step_run_id: "run_out_2", version: 2, status: "approved", generation_source: "llm", updated_at: "2026-09-30T08:00:00+00:00" },
      { step_run_id: "run_out_1", version: 1, status: "superseded", generation_source: "llm", updated_at: "2026-09-29T08:00:00+00:00" },
    ];
    const { host } = await boot({ versions, workspace });
    await act(async () => host.querySelector('[data-testid="snow-step-outline"]').click());
    await openHistory(host);
    await previewV2(host);
    expect(document.querySelector('[data-testid="snow-version-warning"]').textContent).toContain("只恢复五段展开的文字");
    const old = document.querySelector('[data-testid="snow-version-old"]').textContent;
    expect(old).toContain("铺垫：旧的铺垫展开");
    expect(old).toContain(`第 ${1} 章 · 合成一章（灾一）：现在的章摘要`);
    expect(old).not.toContain("旧章表里的一章");
    expect(document.querySelector('[data-testid="snow-version-cur"]').textContent).toContain("铺垫：现在的铺垫展开");
  });

  it("恢复失败：本机这一步原样不动（文字、状态、缓存、操作记录），回执说清楚", async () => {
    const { host } = await boot({ restore: async () => { throw Object.assign(new Error("服务器开小差了"), { status: 500, code: "INTERNAL" }); } });
    const before = JSON.parse(window.localStorage.getItem(CACHE_KEY));
    await openHistory(host);
    await previewV2(host);
    await act(async () => document.querySelector('[data-testid="snow-version-restore"]').click());
    await vi.waitFor(() => expect(host.querySelector('[data-testid="undo-toast"]').textContent).toContain("恢复没有完成"), T);
    expect(host.querySelector('[data-testid="undo-toast"]').textContent).toContain("服务器开小差了");
    expect(document.querySelector('[data-testid="snow-version-dialog"]')).toBeNull();
    // 历史页签的本机记录里没有「从服务器恢复」；回编辑页，文字原样
    expect([...host.querySelectorAll(".hist-row")].some(r => r.textContent.includes("从服务器恢复"))).toBe(false);
    await openEdit(host);
    expect(textarea(host).value).toBe(NOW);
    expect(host.querySelector('[data-testid="snow-reconfirm-pill"]')).toBeNull();
    const cache = JSON.parse(window.localStorage.getItem(CACHE_KEY));
    expect(cache.drafts.logline).toBe(before.drafts.logline);
    expect(cache.states.logline).toBe(before.states.logline);
    expect((cache.history || []).some(h => h.action === "从服务器恢复")).toBe(false);
  });

  it("严格模式下「前面的步骤还没确认」（409）：回执点名是哪一步、给一扇去那一步的门，本机不动", async () => {
    const blocked = Object.assign(new Error("需要先确认前面的雪花步骤。"), {
      status: 409, code: "SNOWFLAKE_PREVIOUS_STEP_REQUIRED",
      details: { missing_previous_steps: [{ step_key: "book_brief", label: "读者定位" }] },
    });
    const { host } = await boot({ restore: async () => { throw blocked; } });
    await openHistory(host);
    await previewV2(host);
    await act(async () => document.querySelector('[data-testid="snow-version-restore"]').click());
    await vi.waitFor(() => expect(host.querySelector('[data-testid="undo-toast"]').textContent).toContain("先确认「01 读者定位」"), T);
    const door = [...host.querySelectorAll('[data-testid="undo-toast"] button')].find(b => b.textContent.includes("去 01 读者定位"));
    expect(door).toBeTruthy();
    expect(JSON.parse(window.localStorage.getItem(CACHE_KEY)).drafts.logline).toBe(NOW);
    await act(async () => door.click());
    expect(host.querySelector(".snow-canvas-title").textContent).toBe("读者定位");
  });

  it("这一步最近一次保存把内容整个清空了（抹空保护另起的一版）：上面一条提示，一键看清空前的那一版", async () => {
    const versions = [
      { step_run_id: "run_log_3", version: 3, status: "pending_review", generation_source: "author", wipe_guard_preserved_step_run_id: "run_log_2", updated_at: "2026-09-30T08:00:00+00:00" },
      { step_run_id: "run_log_2", version: 2, status: "pending_review", generation_source: "author", updated_at: "2026-09-29T08:00:00+00:00" },
    ];
    const { host } = await boot({ versions });
    await openHistory(host);
    const banner = host.querySelector('[data-testid="snow-version-wipe"]');
    expect(banner.textContent).toContain("清空前的第 2 版还在");
    expect(host.querySelector('[data-testid="snow-version-row"]').textContent).toContain("整步清空时另起的一版");
    await act(async () => host.querySelector('[data-testid="snow-version-wipe-open"]').click());
    await vi.waitFor(() => expect(document.querySelector('[data-testid="snow-version-old"]')).toBeTruthy(), T);
    expect(document.querySelector('[data-testid="snow-version-old"]').textContent).toBe(OLD);
  });
});
