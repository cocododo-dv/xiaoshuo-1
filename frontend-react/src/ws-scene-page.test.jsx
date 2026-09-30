// AI 起草台 · 整页：裁决 / 进度只认后端、一个轮询者；几条没有出口的路（对抗复查）。
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

/* ---- 2026-09-21 起草台重排：裁决只认后端、进度只认后端、一个轮询者 ----
   过去台面上有一套前端自己的「质检」（短句率 55%、划红线、「通过 · 有风险」）和永远是 0/4 的
   「戏剧卡对齐」；进度条按 700ms 计时器走到 92%，scnRun 与任务控制条各自每 2 秒问一次同一个任务；
   候选终选暂停被画成红色的「硬问题」；「送写作台深改」把作者送到一张空白页。 */
describe("AI 起草台 · 只认后端（2026-09-21）", () => {
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
    const { clearViewIntents } = await import("./ws-view-intents.js");
    clearViewIntents("scene");
    vi.restoreAllMocks();
  });

  const NOT_FOUND = () => Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" });
  const readyRecord = (mod, gate, text = "潮水退去，她留下了证词。") => ({
    ...mod.scnQC([{ id: "p1", text }]),
    state: "ready", attempt: 1, attempts: [], cost: [], log: [], gate,
  });

  it("scnQC 只切段、数字数：没有短句率、风险划线和戏剧卡对齐", async () => {
    const { mod } = await loadSceneRun();
    const qc = mod.scnQC([{ id: "p1", text: "她推门。" }, { id: "p2", text: "雨下了一整夜，街上没有一个人。" }]);
    expect(qc.words).toBe(19);
    expect(qc.draft.map(p => p.parts.map(x => x.text).join(""))).toEqual(["她推门。", "雨下了一整夜，街上没有一个人。"]);
    expect(qc.draft.every(p => p.parts.every(part => !part.risk))).toBe(true);
    expect(qc).not.toHaveProperty("metrics");
    expect(qc).not.toHaveProperty("alignment");
    expect(qc.verdict).toEqual({ words: 19 });
    expect(mod.scnSetQcThresholds).toBeUndefined();
    expect(mod.scnPickList).toBeUndefined();
  });

  it("scnRunRecordFromWorkbench：起草与恢复共用一份——终稿优先、段落与字数、预算断点一律不可归档", async () => {
    const { mod } = await loadSceneRun();
    const wb = {
      final_scene: { content: "终稿第一段。\n\n终稿第二段。" },
      style_draft: { content: "风格稿不该被读到。" },
      scene_run_state: { scene_status: "near_final", lifecycle_budget: { recommended_topup_tokens: 5000 } },
      author_state: { author_state: "draft_ready", can_archive: true },
      hard_qc_summary: { rewrite_brief: ["把结尾收得更短"] },
      generation_summary: { draft_mode: "style_first" },
    };
    const plain = mod.scnRunRecordFromWorkbench(wb, { authorNote: "保留对白" });
    expect(plain.draft.map(p => p.parts[0].text)).toEqual(["终稿第一段。", "终稿第二段。"]);
    expect(plain.words).toBe(12);
    expect(plain).toMatchObject({
      pipeState: "near_final", authorNote: "保留对白", rewriteBrief: "把结尾收得更短",
      draftMode: "style_first", budgetBlock: null, gate: { authorState: "draft_ready", canArchive: true },
    });
    // 调用方给的管线状态优先（scnRun 在 workbench 没有 scene_run_state 时退回任务状态）
    expect(mod.scnRunRecordFromWorkbench({}, { pipeState: "completed" }).pipeState).toBe("completed");
    const blocked = mod.scnRunRecordFromWorkbench(wb, { job: { error_code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED", current_step: "style_running" } });
    expect(blocked.budgetBlock).toMatchObject({ topup: { extra_tokens: 5000 }, currentStep: "style_running" });
    expect(blocked.gate).toMatchObject({ authorState: "draft_ready", canArchive: false, blockReason: "lifecycle_budget" });
    // 没有投影也照样不可归档
    expect(mod.scnRunRecordFromWorkbench({}, { job: { error_code: "LLM_BUSINESS_ATTEMPT_BUDGET_EXHAUSTED" } }).gate)
      .toMatchObject({ authorState: null, canArchive: false, blockReason: "lifecycle_budget" });
  });

  it("scnRunStageIndex：后端 token 映射到七步，硬质检的 rewrite_required 不会被当成近终稿", async () => {
    const { mod } = await loadSceneRun();
    expect(mod.RUN_STAGES.map(s => s.name)).toEqual(["预检", "首稿", "硬质检", "风格稿", "软质检", "近终稿", "归档"]);
    expect(mod.scnRunStageIndex("bundle_built")).toBe(0);
    expect(mod.scnRunStageIndex("neutral_running")).toBe(1);
    expect(mod.scnRunStageIndex("hard_qc_partial_rewrite_required")).toBe(2);
    expect(mod.scnRunStageIndex("style_running")).toBe(3);
    expect(mod.scnRunStageIndex("awaiting_candidate_selection")).toBe(3);
    expect(mod.scnRunStageIndex("soft_qc_patch_required")).toBe(4);
    expect(mod.scnRunStageIndex("rewrite_running")).toBe(5);
    expect(mod.scnRunStageIndex("near_final")).toBe(5);
    expect(mod.scnRunStageIndex("archived")).toBe(6);
    expect(mod.scnRunStageIndex("human_review_required")).toBe(-1);
    expect(mod.scnRunStageIndex("")).toBe(-1);
  });

  it("待复核稿的裁决来自后端 author_state：质量建议显示条数与说明，不显示本地读数与英文 issue_key", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", readyRecord(mod, mod.scnGateFrom({ author_state: {
      author_state: "quality_warning", can_archive: true,
      quality_warnings: [
        { issue_key: "dialogue_ratio_high", message: "对白比例偏高，可以删两句" },
        { issue_key: "soft_qc_payload_invalid", message: "soft QC payload validation failed: 5 validation errors for SoftQCOutput" },
      ],
    } })));
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    expect(view.host.querySelector(".scn2-verdict-badge")?.textContent).toBe("2 条建议");
    expect(view.host.querySelector('[data-testid="scene-archive"]').disabled).toBe(false);
    expect(view.host.querySelector(".scn2-gate-list")?.textContent).toBe("对白比例偏高，可以删两句");
    // 原样透出的英文异常不当作一句建议摆出来：折叠在「查看原文」里
    expect(view.host.querySelector(".scn2-gate-raw summary")?.textContent).toContain("1 条只有后端原始信息");
    expect(view.host.querySelector(".scn2-gate-raw pre")?.textContent).toContain("validation failed");
    // 退回重写的快捷短语只取中文改法
    await click([...view.host.querySelectorAll("button")].find(b => b.textContent.includes("退回重写")));
    expect([...view.host.querySelectorAll(".scn2-rework-chip")].map(c => c.textContent)).toEqual(["对白比例偏高，可以删两句"]);
    const text = view.host.textContent;
    expect(text).not.toContain("短句率");
    expect(text).not.toContain("戏剧卡");
    expect(text).not.toContain("dialogue_ratio_high");
    expect(text).not.toMatch(/Claude|Sonnet|Haiku/);
    expect(view.host.querySelector(".scn2-risk, .scn2-beat-tab, .scn2-meter")).toBeNull();
  });

  it("头部不再有一键「重跑」；退回重写不写指令时就是「直接重跑」，照常提交", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", readyRecord(mod, null));
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options = {}) => {
      if (url === "/api/v1/scenes/s1/run/jobs") {
        return new Promise((resolve, reject) => {
          void resolve;
          options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }
      return basePost(url, body, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    expect([...view.host.querySelectorAll("button")].some(b => b.textContent.trim() === "重跑")).toBe(false);

    await click([...view.host.querySelectorAll("button")].find(b => b.textContent.includes("退回重写")));
    const submit = [...view.host.querySelectorAll(".scn2-rework button")].find(b => b.textContent.includes("直接重跑"));
    expect(submit.disabled).toBe(false);
    await click(submit);
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs", { run_policy: "strict" }, expect.objectContaining({ signal: expect.anything() }),
    ), T);
  });

  it("已证实的硬问题：红色提示给出重写入口、归档禁用；没附说明的条目只报条数", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", readyRecord(mod, mod.scnGateFrom({ author_state: {
      author_state: "hard_blocked", can_archive: false,
      blocking_findings: [{ issue_key: "missing_required_text", quality_level: "Q1" }],
    } })));
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-hard-rewrite"]')).toBeTruthy(), T);
    expect(view.host.querySelector('[data-testid="scene-archive"]').disabled).toBe(true);
    expect(view.host.querySelector('[data-testid="scene-gate"]')?.textContent).toContain("另有 1 条没有附说明");
    expect(view.host.textContent).not.toContain("missing_required_text");
  });

  it("候选终选暂停不是硬问题：没有红色阻断、没有重写与归档按钮，台面是盲选页签", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "候选之一。" },
          scene_run_state: { scene_status: "awaiting_candidate_selection" },
          author_state: { author_state: "awaiting_author_choice", can_archive: false, blocking_findings: [], recommended_actions: ["select_candidate"] },
        });
      }
      if (url === "/api/v1/scenes/s1/style-candidates") {
        return Promise.resolve({ candidates: [
          { row_id: "cand-x", content: "第一份候选。\n它有两段。" },
          { row_id: "cand-y", content: "第二份候选。" },
          { row_id: "cand-z", content: "第三份。" },
        ] });
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-candidate-select"]')).toBeTruthy(), T);
    expect(view.host.querySelector('[data-testid="scene-awaiting-choice"]')?.textContent).toContain("等你终选一稿");
    expect(view.host.querySelector('[data-testid="scene-hard-rewrite"]')).toBeNull();
    expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeNull();
    expect(view.host.textContent).not.toContain("硬问题");
    const tabs = [...view.host.querySelectorAll('.scn2-pick-tabs [role="tab"]')];
    expect(tabs.map(tab => tab.textContent)).toEqual(["候选 A11 字", "候选 B6 字", "候选 C4 字"]);
    expect(view.host.querySelector('[data-testid="scene-candidate-select"]').dataset.candidateRowId).toBe("cand-x");
    await click(tabs[2]);
    expect(view.host.querySelector('[data-testid="scene-candidate-select"]').dataset.candidateRowId).toBe("cand-z");
    expect(view.host.querySelector('[data-testid="scene-candidate-tie"]')?.textContent).toContain("各稿无明显差异");
    // 终选锁定之后没有「重开」这条路了（重评 R2）：页脚不再许诺它，说清怎么换一稿
    const foot = view.host.querySelector(".scn2-draft-foot").textContent;
    expect(foot).toBe("终选提交后不能改选；想换一稿，请重新起草这一场。");
    expect(foot).not.toContain("重开");
  });

  it("「存为候选并去写作台」：稿进「同步与恢复」、不归档不覆盖，然后打开写作台这一场和那份候选", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", readyRecord(mod, mod.scnGateFrom({ author_state: { author_state: "draft_ready", can_archive: true } }), "交给写作台的那一稿。"));
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const opened = vi.fn();
    window.addEventListener("ws:recovery-open", opened);
    const go = vi.fn();
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go, t: {} });
    try {
      await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
      expect(view.host.textContent).not.toContain("送写作台深改");
      const button = [...view.host.querySelectorAll("button")].find(b => b.textContent.includes("存为候选并去写作台"));
      await click(button);

      await vi.waitFor(() => expect(go).toHaveBeenCalledWith("writer", [{ type: "ws:writer-scene", detail: "ch01s1" }]), T);
      const candidates = window.WrRecovery.list().filter(item => item.type === "candidate");
      expect(candidates).toHaveLength(1);
      expect(candidates[0].html).toContain("交给写作台的那一稿。");
      expect(opened).toHaveBeenCalledTimes(1);
      expect(opened.mock.calls[0][0].detail).toEqual({ id: candidates[0].id });
      expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
    } finally {
      window.removeEventListener("ws:recovery-open", opened);
    }
  });

  it("归档后的深改走 go 的意图握手，不再 setTimeout 派发", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", { ...readyRecord(mod, null), state: "archived" });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const go = vi.fn();
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go, t: {} });
    await vi.waitFor(() => expect([...view.host.querySelectorAll("button")].some(b => b.textContent.includes("在写作台深改"))).toBe(true), T);
    await click([...view.host.querySelectorAll("button")].find(b => b.textContent.includes("在写作台深改")));
    expect(go).toHaveBeenCalledWith("writer", [
      { type: "ws:writer-scene", detail: "ch01s1" },
      { type: "ws:writer-posture", detail: "deep" },
    ]);
    // 从后端恢复的归档场没有归档时刻：副标题不再是一句悬空的「已写回 ·」
    expect(view.host.querySelector(".scn2-head-sub")?.textContent).toBe("已写入写作台正文");
  });

  it("进度条只看后端 current_step：运行在硬质检时，前两步打勾、硬质检是当前步", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({ job_id: "job-hard", scene_id: "s1", status: "running", current_step: "hard_qc_running" });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('.scn2-pstep[aria-current="step"]')?.textContent).toContain("硬质检"), T);
    const steps = [...view.host.querySelectorAll(".scn2-pstep")];
    expect(steps.map(step => step.className.match(/s-(\w+)/)[1])).toEqual(["done", "done", "active", "todo", "todo", "todo", "todo"]);
    expect(view.host.querySelector(".scn2-run")).toBeTruthy();
    expect(view.host.textContent).not.toMatch(/\d+%/);
  });

  it("一个轮询者：开始起草后 scnRun 不再自己问 run-jobs/{id}，由任务控制条的终态兑现", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    let created = false;
    let polls = 0;
    client.getLatestSceneRunJob.mockImplementation(() => {
      if (!created) return Promise.reject(NOT_FOUND());
      polls += 1;
      return Promise.resolve({ job_id: "job-one", scene_id: "s1", status: polls >= 1 ? "completed" : "running", current_step: "near_final" });
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (url === "/api/v1/scenes/s1/run/jobs") { created = true; return Promise.resolve({ job_id: "job-one", scene_id: "s1", status: "running", current_step: "queued" }); }
      return basePost(url, body, options);
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve(created
          ? { style_draft: { content: "一个轮询者写出的稿。" }, scene_run_state: { scene_status: "near_final" }, author_state: { author_state: "draft_ready", can_archive: true } }
          : { scene_run_state: { scene_status: "ready" } });
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-start"]'));

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), { timeout: 6000, interval: 50 });
    expect(view.host.querySelector(".scn2-draft")?.textContent).toContain("一个轮询者写出的稿。");
    expect(client.apiGet.mock.calls.filter(([url]) => /\/api\/v1\/run-jobs\//.test(url))).toEqual([]);
    // 管线停在近终稿：前六步打勾，归档还等作者
    const steps = [...view.host.querySelectorAll(".scn2-pstep")].map(step => step.className.match(/s-(\w+)/)[1]);
    expect(steps).toEqual(["done", "done", "done", "done", "done", "done", "todo"]);
  }, 10000);

  it("控制条报来的是同一场的另一个任务（另一个标签页又起了一次）：startRun 退回自己盯住自己的任务，不卡在运行中", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    let created = false;
    client.getLatestSceneRunJob.mockImplementation(() => (created
      ? Promise.resolve({ job_id: "job-other-tab", scene_id: "s1", status: "completed", current_step: "near_final" })
      : Promise.reject(NOT_FOUND())));
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (url === "/api/v1/scenes/s1/run/jobs") { created = true; return Promise.resolve({ job_id: "job-mine", scene_id: "s1", status: "running" }); }
      return basePost(url, body, options);
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/run-jobs/job-mine") return Promise.resolve({ job_id: "job-mine", scene_id: "s1", status: "completed", current_step: "near_final" });
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve(created
          ? { style_draft: { content: "我自己的这一稿。" }, scene_run_state: { scene_status: "near_final" }, author_state: { author_state: "draft_ready", can_archive: true } }
          : { scene_run_state: { scene_status: "ready" } });
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-start"]'));

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), { timeout: 9000, interval: 50 });
    expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/run-jobs/job-mine")).toBe(true);
    expect(view.host.querySelector(".scn2-draft")?.textContent).toContain("我自己的这一稿。");
  }, 14000);

  it("scnRun 没传 waitForTerminal 时自己轮询到终态，且没有 5 分钟客户端时限", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => (
      /\/run\/jobs$/.test(url) ? Promise.resolve({ job_id: "job-long", scene_id: "s1", status: "running" }) : Promise.resolve({})
    ));
    let reads = 0;
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-long") {
        reads += 1;
        return Promise.resolve({ job_id: "job-long", scene_id: "s1", status: reads < 200 ? "running" : "completed" });
      }
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve({ style_draft: { content: "慢慢写完。" }, scene_run_state: { scene_status: "near_final" } });
      return baseGet(url);
    });
    vi.useFakeTimers();
    try {
      const pending = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "");
      await vi.advanceTimersByTimeAsync(200 * 2000 + 10);   // ≈6.7 分钟
      const result = await pending;
      expect(result.state).toBe("ready");
      expect(result.pipeState).toBe("near_final");
      expect(reads).toBe(200);
    } finally {
      vi.useRealTimers();
    }
  });
});

/* ---- 2026-09-21 对抗复查：几条没有出口的路 ----
   候选取不到时台面只剩一句话；控制条没挂上时 startRun 永远等；待复核时的失败不出声；
   在办场在后端恢复完成前一律写「待起草」。 */
describe("AI 起草台 · 出口与真话（对抗复查）", () => {
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
    const { clearViewIntents } = await import("./ws-view-intents.js");
    clearViewIntents("scene");
    vi.restoreAllMocks();
  });

  const NOT_FOUND = () => Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" });
  const hangingRunJobs = (client) => {
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options = {}) => {
      if (url === "/api/v1/scenes/s1/run/jobs") {
        return new Promise((resolve, reject) => {
          void resolve;
          options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }
      return basePost(url, body, options);
    });
  };

  it("候选终选取不到候选稿：台面给出「重新取候选」和「重新起草这一场」，不是一句话的死胡同", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    let candidateReads = 0;
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "候选之一。" },
          scene_run_state: { scene_status: "awaiting_candidate_selection" },
          author_state: { author_state: "awaiting_author_choice", can_archive: false, blocking_findings: [] },
        });
      }
      if (url === "/api/v1/scenes/s1/style-candidates") {
        candidateReads += 1;
        return candidateReads === 1 ? Promise.reject(new Error("network down")) : Promise.resolve({ candidates: [] });
      }
      return baseGet(url, options);
    });
    hangingRunJobs(client);
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-candidate-empty"]')).toBeTruthy(), T);
    const empty = () => view.host.querySelector('[data-testid="scene-candidate-empty"]');
    expect(empty().textContent).toContain("候选稿没取回来");
    expect(empty().textContent).not.toContain("network down");
    // 裁决条在等终选时仍然没有重写 / 归档按钮——出口在台面上
    expect(view.host.querySelector('[data-testid="scene-hard-rewrite"]')).toBeNull();
    expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeNull();

    await click(view.host.querySelector('[data-testid="scene-candidate-reload"]'));
    await vi.waitFor(() => expect(candidateReads).toBe(2), T);
    await vi.waitFor(() => expect(empty()?.textContent).toContain("后端没有给出可选的候选稿"), T);

    await click(view.host.querySelector('[data-testid="scene-candidate-rework"]'));
    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs", { run_policy: "strict" }, expect.objectContaining({ signal: expect.anything() }),
    ), T);
  });

  it("页面还没解析出后端 scene id 就开始起草：控制条没挂上，startRun 自己盯住任务，不卡在运行中", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const { WsCatalog } = await import("./ws-catalog.jsx");
    const realSceneId = WsCatalog.__backendSceneId;
    let synced = false;
    vi.spyOn(WsCatalog, "__backendSceneId").mockImplementation((sid) => (
      synced ? realSceneId.call(WsCatalog, sid) : Promise.resolve(null)
    ));
    let created = false;
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (url === "/api/v1/scenes/s1/run/jobs") { created = true; return Promise.resolve({ job_id: "job-late", scene_id: "s1", status: "running" }); }
      return basePost(url, body, options);
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/run-jobs/job-late") return Promise.resolve({ job_id: "job-late", scene_id: "s1", status: "completed", current_step: "near_final" });
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve(created
          ? { style_draft: { content: "晚到的 id 也写完了。" }, scene_run_state: { scene_status: "near_final" }, author_state: { author_state: "draft_ready", can_archive: true } }
          : {});
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy(), T);
    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')).toBeNull();

    synced = true;
    await click(view.host.querySelector('[data-testid="scene-start"]'));

    // 不等控制条的宽限：解析出的 id 对不上时立刻退回自己轮询（2 秒一问）
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), { timeout: 5000, interval: 50 });
    expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/run-jobs/job-late")).toBe(true);
    expect(view.host.querySelector(".scn2-draft")?.textContent).toContain("晚到的 id 也写完了。");
  }, 10000);

  it("待复核时的失败会说出来：追加预算没有可执行的量，台面上有一条红色说明而不是按钮闪一下", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", {
      ...mod.scnQC([{ id: "p1", text: "潮水退去，她留下了证词。" }]),
      state: "ready", attempt: 1, attempts: [], cost: [], log: [],
      gate: { authorState: "draft_ready", canArchive: false, blocking: [], warnings: [], blockReason: "lifecycle_budget" },
      budgetBlock: { code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED", label: "这一场的预算用完了", topup: {} },
    });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-budget-topup"]')).toBeTruthy(), T);
    expect(view.host.querySelector('[data-testid="scene-ready-error"]')).toBeNull();

    await click(view.host.querySelector('[data-testid="scene-budget-topup"]'));

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-ready-error"]')?.textContent).toContain("没有给出可以追加的预算量"), T);
    expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy();
    expect(client.apiPost.mock.calls.filter(([url]) => /budget\/topup/.test(url))).toEqual([]);
  });

  it("参考旧尝试的复盘意见重写：意见很长时截短，整条指令不超过 2000 字，照常提交", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const longVerdict = "作者改写指令：" + "把这一段收紧，".repeat(300);
    mod.scnRunSave("ch01s1", {
      ...mod.scnQC([{ id: "p1", text: "潮水退去，她留下了证词。" }]),
      state: "ready", attempt: 2, cost: [], log: [],
      gate: { authorState: "draft_ready", canArchive: true, blocking: [], warnings: [] },
      attempts: [
        { n: 2, time: "本次 · 待裁决", result: "待裁决", tone: "gold", note: "按指令改写" },
        { n: 1, time: "9/21 10:00", result: "退回重写", tone: "slate", note: "初稿", cmp: { verdict: longVerdict } },
      ],
    });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    hangingRunJobs(client);
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector(".scn2-try-view")).toBeTruthy(), T);

    await click(view.host.querySelector(".scn2-try-view"));
    await click(view.host.querySelector('[data-testid="scene-attempt-rewrite"]'));

    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => url === "/api/v1/scenes/s1/run/jobs")).toBe(true), T);
    const [, body] = client.apiPost.mock.calls.find(([url]) => url === "/api/v1/scenes/s1/run/jobs");
    expect(Array.from(body.author_note).length).toBeLessThanOrEqual(2000);
    expect(Array.from(body.author_note).length).toBeGreaterThan(1900);
    expect(body.author_note).toMatch(/^参考第 1 次尝试的复盘意见重写（作者改写指令：把这一段收紧/);
    expect(body.author_note).toContain("…）；不恢复该版正文");
    expect(view.host.querySelector('[data-testid="scene-ready-error"]')).toBeNull();
  });

  it("窄屏证据抽屉：拉开时焦点进抽屉，收起后回到「证据」按钮；文本框里的 Esc 不收抽屉", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", {
      ...mod.scnQC([{ id: "p1", text: "潮水退去，她留下了证词。" }]),
      state: "ready", attempt: 1, attempts: [], cost: [], log: [],
      gate: { authorState: "draft_ready", canArchive: true, blocking: [], warnings: [] },
    });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector(".scn2-evi-toggle")).toBeTruthy(), T);
    const toggle = view.host.querySelector(".scn2-evi-toggle");
    const drawer = () => view.host.querySelector("#scn2-evi");
    const esc = async (target) => {
      await act(async () => { target.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    };

    toggle.focus();
    await click(toggle);
    expect(drawer().classList.contains("is-open")).toBe(true);
    expect(drawer().contains(document.activeElement)).toBe(true);

    // 退回重写的指令框里按 Esc：抽屉不动
    await click([...view.host.querySelectorAll(".scn2-decide-acts button")].find(b => b.textContent.includes("退回重写")));
    const note = view.host.querySelector(".scn2-rework-input");
    note.focus();
    await esc(note);
    expect(drawer().classList.contains("is-open")).toBe(true);
    // 输入法组字时的 Esc 也不算
    await act(async () => { document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, isComposing: true })); });
    expect(drawer().classList.contains("is-open")).toBe(true);

    drawer().querySelector(".scn2-evi-drawerhead button").focus();
    await esc(document.activeElement);
    expect(drawer().classList.contains("is-open")).toBe(false);
    expect(document.activeElement).toBe(view.host.querySelector(".scn2-evi-toggle"));
  });

  it("在办场还没有运行记录时，书脊按目录状态标：已完成的场不写「待起草」", async () => {
    const doneChap = {
      ...TWO_SCENE_CHAP,
      scenes: [TWO_SCENE_CHAP.scenes[0], { ...TWO_SCENE_CHAP.scenes[1], state: "done" }],
    };
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT], catalog: [doneChap] });
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    client.getLatestSceneRunJob.mockRejectedValue(NOT_FOUND());
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    const row = (sid) => view.host.querySelector(`[data-testid="scene-queue-item"][data-scene-sid="${sid}"]`);
    await vi.waitFor(() => expect(row("ch01s2")).toBeTruthy(), T);
    expect(row("ch01s2").querySelector(".scn2-chip")?.textContent).toBe("已完成");
    expect(row("ch01s1").querySelector(".scn2-chip")?.textContent).toBe("待起草");
  });
});
