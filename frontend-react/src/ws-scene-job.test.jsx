// AI 起草台 · 任务控制条（SceneRunJobControl）、起草任务的轮询 / 取消 / 终态收尾、运行步骤的中文标签。
// （2026-09-29 从 ws-scene-run.test.jsx 原样拆出；装配在 ws-scene-run.test-harness.jsx。）
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
// 单条上限放到 15 s：冷启动的模块转换加上 5 s 的 waitFor，主机负载高时会顶到默认的 5 s（与写作台的装配测试一样）
vi.setConfig({ testTimeout: 15000 });

describe("scene run cancellation client", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // 幂等键在同一次取消的重试里沿用、AbortSignal 转给 fetch：那是 apiGet / apiPost 的契约（lib/client.test.js）。
  // 这里钉住两个请求走的路径与参数都交给了它们。
  it("latest 与 cancel 走权威路径（场景 id / 任务 id 编码），选项原样交给 apiGet / apiPost", async () => {
    const api = await vi.importActual("./ws-scene-job-api.js");
    const client = await import("./lib/client.js");
    client.apiGet.mockResolvedValue({ job_id: "job/latest" });
    client.apiPost.mockResolvedValue({ job_id: "job/retry", status: "cancel_requested" });
    const controller = new AbortController();

    await api.getLatestSceneRunJob("SC /一", { signal: controller.signal });
    await api.cancelRunJob("job/retry", { signal: controller.signal });

    expect(client.apiGet).toHaveBeenCalledWith(
      "/api/v1/scenes/SC%20%2F%E4%B8%80/run/jobs/latest", { signal: controller.signal },
    );
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/run-jobs/job%2Fretry/cancel", {}, { signal: controller.signal },
    );
  });
});

describe("SceneRunJobControl", () => {
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

  it("treats latest 404 as an ordinary no-job state with accessible status", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );

    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });

    await vi.waitFor(() => {
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("暂无运行任务");
    }, T);
    expect(view.host.querySelector('[role="alert"]')).toBeNull();
    expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeNull();
  });

  it.each(["queued", "running"])("cancels %s once despite repeated clicks", async (status) => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({ job_id: `job-${status}`, scene_id: "SC01", status });
    let resolveCancel;
    client.cancelRunJob.mockImplementation(() => new Promise((resolve) => { resolveCancel = resolve; }));

    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy(), T);
    const button = view.host.querySelector('[data-testid="scene-run-cancel-button"]');
    await act(async () => {
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      button.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });

    expect(button.disabled).toBe(true);
    expect(client.cancelRunJob).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveCancel({ job_id: `job-${status}`, scene_id: "SC01", status: "cancel_requested" });
    });
    expect(view.host.querySelector('[role="status"]').textContent).toContain("正在取消");
    expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')?.disabled).toBe(true);
  });

  it("polls cancel_requested until cancelled and exposes every state through aria-live", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob
      .mockResolvedValueOnce({ job_id: "job-1", scene_id: "SC01", status: "cancel_requested" })
      .mockResolvedValue({ job_id: "job-1", scene_id: "SC01", status: "cancelled" });

    const view = await renderRunJobControl(mod.SceneRunJobControl, {
      sceneId: "SC01",
      pollIntervalMs: 5,
    });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 15));
    });

    await vi.waitFor(() => {
      const status = view.host.querySelector('[role="status"]');
      expect(status?.getAttribute("aria-live")).toBe("polite");
      expect(status?.textContent).toContain("已取消");
    }, T);
    expect(client.getLatestSceneRunJob.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeNull();
  });

  it("keeps a committed POST job newer than an older latest request", async () => {
    const { mod, client } = await loadSceneRun();
    let rejectOldLatest;
    client.getLatestSceneRunJob.mockImplementation(() => new Promise((resolve, reject) => {
      rejectOldLatest = reject;
    }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });

    await view.rerender({
      sceneId: "SC01",
      observedJob: { job_id: "job-new", scene_id: "SC01", status: "running" },
    });
    await vi.waitFor(() => expect(view.host.querySelector('[role="status"]')?.textContent).toContain("运行中"), T);
    await act(async () => {
      rejectOldLatest(Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }));
    });

    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-new");
  });

  it("does not regress cancel_requested when an older running observation arrives late", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({ job_id: "job-monotonic", scene_id: "SC01", status: "running" });
    client.cancelRunJob.mockResolvedValue({ job_id: "job-monotonic", scene_id: "SC01", status: "cancel_requested" });
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));
    await vi.waitFor(() => expect(view.host.querySelector('[role="status"]')?.textContent).toContain("正在取消"), T);
    await view.rerender({
      sceneId: "SC01",
      observedJob: { job_id: "job-monotonic", scene_id: "SC01", status: "running" },
    });

    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe("cancel_requested");
    expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')?.disabled).toBe(true);
  });

  it("does not let a late cancel response for job A overwrite a newer latest job B", async () => {
    const { mod, client } = await loadSceneRun();
    // B 只在点了取消之后才报来：过去 B 立刻就绪，5 ms 一轮的轮询可能在断言看见 A 之前就把它换成 B
    const latestB = deferred();
    client.getLatestSceneRunJob
      .mockResolvedValueOnce({ job_id: "job-a", scene_id: "SC01", status: "running" })
      .mockImplementation(() => latestB.promise);
    let resolveCancelA;
    client.cancelRunJob.mockImplementation(() => new Promise(resolve => { resolveCancelA = resolve; }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, {
      sceneId: "SC01",
      pollIntervalMs: 5,
    });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-a"), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));
    // 取消 A 还在路上时，下一轮 latest 报来 B（等它到，不赌固定的 15 ms）
    await act(async () => { latestB.resolve({ job_id: "job-b", scene_id: "SC01", status: "completed" }); });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-b"), T);
    await act(async () => {
      resolveCancelA({ job_id: "job-a", scene_id: "SC01", status: "cancelled" });
    });

    await vi.waitFor(() => {
      const control = view.host.querySelector('[data-testid="scene-run-job-control"]');
      expect(control?.dataset.jobId).toBe("job-b");
      expect(control?.dataset.status).toBe("completed");
      expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeNull();
    }, T);
  });

  it("does not attach a late cancel failure for job A to a newer latest job B", async () => {
    const { mod, client } = await loadSceneRun();
    // B 只在点了取消之后才报来（同上一条）
    const latestB = deferred();
    client.getLatestSceneRunJob
      .mockResolvedValueOnce({ job_id: "job-a", scene_id: "SC01", status: "running" })
      .mockImplementation(() => latestB.promise);
    let rejectCancelA;
    client.cancelRunJob.mockImplementation(() => new Promise((resolve, reject) => {
      void resolve;
      rejectCancelA = reject;
    }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, {
      sceneId: "SC01",
      pollIntervalMs: 5,
    });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-a"), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));
    // 取消 A 还在路上时，下一轮 latest 报来 B（等它到，不赌固定的 15 ms）
    await act(async () => { latestB.resolve({ job_id: "job-b", scene_id: "SC01", status: "completed" }); });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-b"), T);
    await act(async () => {
      rejectCancelA(Object.assign(new Error("cancel A failed"), { code: "NETWORK_ERROR", retryable: true }));
    });

    await vi.waitFor(() => {
      const control = view.host.querySelector('[data-testid="scene-run-job-control"]');
      expect(control?.dataset.jobId).toBe("job-b");
      expect(control?.dataset.status).toBe("completed");
      expect(view.host.querySelector('[role="alert"]')).toBeNull();
    }, T);
  });

  it("refreshSignal refetches a terminal job so the banner converges to archived", async () => {
    // C2 状态一致性债务：归档后终态 job 不轮询，横幅停在旧暂停点
    // （awaiting_candidate_selection）；父组件归档成功后递增 refreshSignal
    // 强制重取 latest，后端视图层已把 current_step 收敛为 archived。
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob
      .mockResolvedValueOnce({
        job_id: "job-adopt",
        scene_id: "SC01",
        status: "completed",
        current_step: "awaiting_candidate_selection",
      })
      .mockResolvedValue({
        job_id: "job-adopt",
        scene_id: "SC01",
        status: "completed",
        current_step: "archived",
        scene_status: "archived",
      });

    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => {
      // 候选终选暂停点读作中文，不再原样回显英文 token
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("等你终选");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("awaiting_candidate_selection");
    // 终态 job 不轮询：没有 refreshSignal 时不会自己刷新
    expect(client.getLatestSceneRunJob).toHaveBeenCalledTimes(1);

    await view.rerender({ sceneId: "SC01", refreshSignal: 1 });

    await vi.waitFor(() => {
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("已归档");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("archived");
    expect(client.getLatestSceneRunJob).toHaveBeenCalledTimes(2);
    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe("completed");
  });

  it("still shows a cancel network failure when an intervening latest poll remains on job A", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-a",
      scene_id: "SC01",
      status: "running",
    });
    let rejectCancelA;
    client.cancelRunJob.mockImplementation(() => new Promise((resolve, reject) => {
      void resolve;
      rejectCancelA = reject;
    }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, {
      sceneId: "SC01",
      pollIntervalMs: 20,
    });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-a"), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));
    await vi.waitFor(() => expect(client.getLatestSceneRunJob.mock.calls.length).toBeGreaterThanOrEqual(2), T);
    await act(async () => {
      rejectCancelA(Object.assign(new Error("cancel A failed"), { code: "NETWORK_ERROR", retryable: true }));
    });

    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-a");
    expect(view.host.querySelector('[role="alert"]')?.textContent).toContain("cancel A failed");
    expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')?.disabled).toBe(false);
    await view.unmount();
  });

  it("publishes only the POST-created job and never feeds job-specific poll results into observedJob", async () => {
    const { mod, client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-created", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-created") {
        return Promise.resolve({ job_id: "job-created", scene_id: "s1", status: "completed" });
      }
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "ready" },
        });
      }
      return baseGet(url);
    });
    const onJobCreated = vi.fn();
    vi.useFakeTimers();
    try {
      const runPromise = mod.scnRun(
        { sid: "ch01s1", kind: "主动场景" },
        "",
        "",
        { onJobCreated },
      );
      await vi.runAllTimersAsync();
      await runPromise;
    } finally {
      vi.useRealTimers();
    }

    expect(onJobCreated).toHaveBeenCalledTimes(1);
    expect(onJobCreated).toHaveBeenCalledWith(
      expect.objectContaining({ job_id: "job-created", status: "running" }),
      "s1",
    );
    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs",
      { run_policy: "strict" },
    );
  });

  it("keeps a neutral draft non-archivable when the durable job is blocked by lifecycle budget", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-budget", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-budget") {
        return Promise.resolve({
          job_id: "job-budget",
          scene_id: "s1",
          status: "blocked",
          current_step: "blocked",
          error_code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
          error_text: "scene token budget exhausted before dispatch",
        });
      }
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: {
            scene_status: "bundle_built",
            lifecycle_budget: {
              scene_token_budget: 34200,
              scene_tokens_used: 13615,
              scene_tokens_reserved: 0,
              scene_tokens_remaining: 20585,
              baseline_tokens: 6840,
              recommended_topup_tokens: 6840,
              attempt_budget: 4,
              total_attempt_count: 2,
              provider_attempt_budget: 32,
              provider_attempts_used: 5,
            },
          },
          author_state: { author_state: "draft_ready", can_archive: true },
        });
      }
      return baseGet(url);
    });

    vi.useFakeTimers();
    let result;
    try {
      const pending = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "");
      await vi.runAllTimersAsync();
      result = await pending;
    } finally {
      vi.useRealTimers();
    }

    expect(result.budgetBlock).toMatchObject({
      code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
      topup: { extra_tokens: 6840 },
    });
    expect(result.gate.canArchive).toBe(false);
    expect(result.draft.length).toBeGreaterThan(0);
  });

  it("hydrates a no-content budget checkpoint with its durable author instruction", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const authorNote = "保留第一人称视角，并延续上一版的人物动机。";
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          scene_run_state: {
            scene_status: "bundle_built",
            lifecycle_budget: {
              baseline_tokens: 7200,
              recommended_topup_tokens: 7200,
            },
          },
        });
      }
      return baseGet(url, options);
    });

    const restored = await mod.scnHydrateFromBackend("ch01s1", {
      terminalJob: {
        job_id: "job-no-content-budget",
        scene_id: "s1",
        status: "blocked",
        current_step: "style_running",
        error_code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        error_text: "scene token budget exhausted before dispatch",
        author_note: authorNote,
      },
    });

    expect(restored).toMatchObject({
      state: "queued",
      draft: [],
      authorNote,
      recoveredWithoutDraft: true,
      budgetBlock: {
        code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        currentStep: "style_running",
        topup: { extra_tokens: 7200 },
      },
      gate: { canArchive: false, blockReason: "lifecycle_budget" },
    });
    expect(restored.error).toContain("尚未产出正文");
  });

  /* 契约对位：GET /scenes/{id}/workbench 以 hard_qc_summary / soft_qc_summary 透出质检
     （api/routes/scenes.py `_serialize_qc_summary`，rewrite_brief 为字符串列表）。
     起草台的运行记录必须从这两个真实键名取回改写指令，否则「按硬问题重写并复检」
     只能退到 issue_key 拼接，作者看不到质检给出的具体改法。 */
  const HARD_BLOCKED_WORKBENCH = {
    neutral_draft: { content: "潮水退去。\n她留下了证词。" },
    scene_run_state: { scene_status: "hard_qc_rewrite_required" },
    author_state: {
      author_state: "hard_blocked",
      blocking_findings: [{ issue_key: "missing_required_text", quality_level: "Q1", verified_by: "scene_card_required_text" }],
      quality_warnings: [],
      recommended_actions: ["review_pipeline_gate"],
      can_archive: false,
    },
    hard_qc_summary: {
      qc_report_id: "qc_hard_s1_001",
      qc_type: "hard_qc",
      pass_flag: false,
      resolution_code: "rewrite_partial_requested",
      issue_keys: ["missing_required_text"],
      next_action: "rewrite_partial",
      rewrite_brief: ["补齐推门动作", "明确主动销毁通行证"],
      created_at: "2026-09-03T10:00:00",
    },
    soft_qc_summary: null,
  };

  it("scnRun：workbench 的 hard_qc_summary.rewrite_brief 落到作者可见的运行记录", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-hard-brief", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-hard-brief") {
        // run-jobs 视图的 latest_qc 不带 rewrite_brief（scene_run_jobs._latest_qc_summary），
        // 改写指令只存在于 workbench 摘要里——这里故意给出无指令的 latest_qc 以证明取数来源。
        return Promise.resolve({
          job_id: "job-hard-brief",
          scene_id: "s1",
          status: "blocked",
          current_step: "hard_qc",
          latest_qc: { qc_type: "hard_qc", pass_flag: false, issue_keys: ["missing_required_text"] },
        });
      }
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve(HARD_BLOCKED_WORKBENCH);
      return baseGet(url);
    });

    vi.useFakeTimers();
    let result;
    try {
      const pending = mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "");
      await vi.runAllTimersAsync();
      result = await pending;
    } finally {
      vi.useRealTimers();
    }

    expect(result.state).toBe("ready");
    expect(result.gate.authorState).toBe("hard_blocked");
    expect(result.gate.canArchive).toBe(false);
    expect(result.rewriteBrief).toBe("补齐推门动作；明确主动销毁通行证");
  });

  it("scnHydrateFromBackend：换浏览器恢复时同样取回 hard_qc_summary 的改写指令", async () => {
    const { mod, client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve(HARD_BLOCKED_WORKBENCH);
      return baseGet(url, options);
    });

    const restored = await mod.scnHydrateFromBackend("ch01s1", {});

    expect(restored).toBeTruthy();
    expect(restored.state).toBe("ready");
    expect(restored.draft.length).toBeGreaterThan(0);
    expect(restored.gate.canArchive).toBe(false);
    expect(restored.rewriteBrief).toBe("补齐推门动作；明确主动销毁通行证");
  });

  it("topups only the exhausted lifecycle dimension through the audited author route", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockResolvedValue({ scene_token_budget: 41040 });

    await mod.scnTopupBudget("ch01s1", {
      code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
      topup: { extra_tokens: 6840 },
    });

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/budget/topup",
      {
        extra_tokens: 6840,
        reason: "作者在起草台确认追加生命周期预算并从持久化检查点继续",
      },
    );
  });

  it("asks the server to resume its own budget-blocked checkpoint instead of starting a fresh execution", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/run/jobs") {
        return Promise.resolve({ job_id: "job-resume-budget", scene_id: "s1", status: "completed" });
      }
      return Promise.resolve({});
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
        });
      }
      return baseGet(url);
    });

    await mod.scnRun(
      { sid: "ch01s1", kind: "主动场景" },
      "",
      "",
      { resumeBudget: true },
    );

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs",
      { run_policy: "strict", resume_budget: true },
    );
  });

  it("preserves the complete author instruction when resuming a budget-blocked checkpoint", async () => {
    const { mod, client } = await loadSceneRun();
    const authorNote = "保留此前的叙事视角，不要重置人物动机。".repeat(30);
    client.apiPost.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/run/jobs") {
        return Promise.resolve({ job_id: "job-resume-note", scene_id: "s1", status: "completed" });
      }
      return Promise.resolve({});
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
        });
      }
      return baseGet(url);
    });

    await mod.scnRun(
      { sid: "ch01s1", kind: "主动场景" },
      authorNote,
      "",
      { resumeBudget: true },
    );

    expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs",
      { run_policy: "strict", author_note: authorNote, resume_budget: true },
    );
  });

  it("rejects an overlong author instruction instead of silently truncating it", async () => {
    const { mod, client } = await loadSceneRun();

    await expect(mod.scnRun(
      { sid: "ch01s1", kind: "主动场景" },
      "改".repeat(2001),
      "",
    )).rejects.toMatchObject({ code: "AUTHOR_NOTE_TOO_LONG" });

    expect(client.apiPost.mock.calls.filter(([url]) => url === "/api/v1/scenes/s1/run/jobs")).toEqual([]);
  });

  it("projects a server-archived completed run as archived instead of a blocked ready state", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/run/jobs") {
        return Promise.resolve({ job_id: "job-archived", scene_id: "s1", status: "completed" });
      }
      return Promise.resolve({});
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          final_scene: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "archived" },
          author_state: { author_state: "archived", can_archive: false },
        });
      }
      return baseGet(url);
    });

    const result = await mod.scnRun({ sid: "ch01s1", kind: "主动场景" }, "", "");

    expect(result.state).toBe("archived");
    expect(result.gate).toMatchObject({ authorState: "archived", canArchive: false });
  });

  it("aborts an in-flight job-specific GET instead of merely ignoring its response", async () => {
    const { mod, client } = await loadSceneRun();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-pending-get", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    let getSignal = null;
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, { signal } = {}) => {
      if (url !== "/api/v1/run-jobs/job-pending-get") return baseGet(url);
      getSignal = signal;
      return new Promise((resolve, reject) => {
        void resolve;
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    const controller = new AbortController();
    vi.useFakeTimers();
    try {
      const pending = mod.scnRun(
        { sid: "ch01s1", kind: "主动场景" },
        "",
        "",
        { signal: controller.signal },
      );
      await vi.advanceTimersByTimeAsync(2000);
      await vi.waitFor(() => expect(getSignal).toBe(controller.signal), T);
      controller.abort();
      await expect(pending).rejects.toMatchObject({ code: "SCENE_RUN_UI_ABORTED" });
      expect(getSignal.aborted).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["cancelled", "completed", "failed", "blocked"])(
    "renders terminal %s without an executable cancel action",
    async (status) => {
      const { mod, client } = await loadSceneRun();
      client.getLatestSceneRunJob.mockResolvedValue({ job_id: `job-${status}`, scene_id: "SC01", status });
      const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });

      await vi.waitFor(() => {
        expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe(status);
      }, T);
      expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeNull();
    },
  );

  it("shows backend 409 reason/details and refreshes the authoritative terminal state", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob
      .mockResolvedValueOnce({ job_id: "job-race", scene_id: "SC01", status: "running" })
      .mockResolvedValue({ job_id: "job-race", scene_id: "SC01", status: "completed" });
    client.cancelRunJob.mockRejectedValue(Object.assign(new Error("terminal scene run job cannot be cancelled"), {
      status: 409,
      code: "RUN_JOB_CANCEL_CONFLICT",
      details: { job_id: "job-race", status: "completed" },
    }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));

    await vi.waitFor(() => {
      // 错误码只留在 data-code 里排查用；给作者的是一句中文：任务已经是什么状态
      const alert = view.host.querySelector('[role="alert"]');
      expect(alert?.dataset.code).toBe("RUN_JOB_CANCEL_CONFLICT");
      expect(alert?.textContent).toContain("「已完成」");
      expect(alert?.textContent).not.toMatch(/RUN_JOB_CANCEL_CONFLICT|completed|job-race/);
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe("completed");
    }, T);
  });

  it("re-enables retry after a network failure without issuing a duplicate in-flight cancel", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({ job_id: "job-network", scene_id: "SC01", status: "running" });
    client.cancelRunJob
      .mockRejectedValueOnce(Object.assign(new Error("network down"), { code: "NETWORK_ERROR", retryable: true }))
      .mockResolvedValueOnce({ job_id: "job-network", scene_id: "SC01", status: "cancel_requested" });
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));
    await vi.waitFor(() => {
      expect(view.host.querySelector('[role="alert"]')?.textContent).toContain("network down");
      expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')?.disabled).toBe(false);
    }, T);
    await click(view.host.querySelector('[data-testid="scene-run-cancel-button"]'));

    expect(client.cancelRunJob).toHaveBeenCalledTimes(2);
    await vi.waitFor(() => expect(view.host.querySelector('[role="status"]')?.textContent).toContain("正在取消"), T);
  });

  it("does not let a stale scene response overwrite the newly selected scene", async () => {
    const { mod, client } = await loadSceneRun();
    let resolveA;
    client.getLatestSceneRunJob.mockImplementation((sceneId) => {
      if (sceneId === "SC-A") return new Promise((resolve) => { resolveA = resolve; });
      return Promise.resolve({ job_id: "job-b", scene_id: "SC-B", status: "cancelled" });
    });
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC-A" });

    await view.rerender({ sceneId: "SC-B" });
    await vi.waitFor(() => expect(view.host.querySelector('[role="status"]')?.textContent).toContain("已取消"), T);
    await act(async () => resolveA({ job_id: "job-a", scene_id: "SC-A", status: "running" }));

    expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-b");
    expect(view.host.querySelector('[role="status"]')?.textContent).toContain("已取消");
  });

  it("cleans its polling timer on unmount and reloads latest after a fresh mount", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({ job_id: "job-live", scene_id: "SC01", status: "cancel_requested" });
    const first = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01", pollIntervalMs: 80 });
    await vi.waitFor(() => expect(client.getLatestSceneRunJob).toHaveBeenCalledTimes(1), T);
    await first.unmount();
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(client.getLatestSceneRunJob).toHaveBeenCalledTimes(1);

    await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01", pollIntervalMs: 80 });
    await vi.waitFor(() => expect(client.getLatestSceneRunJob).toHaveBeenCalledTimes(2), T);
  });

  it("aborts an in-flight latest request when the control unmounts", async () => {
    const { mod, client } = await loadSceneRun();
    let latestSignal = null;
    client.getLatestSceneRunJob.mockImplementation((sceneId, { signal }) => new Promise((resolve, reject) => {
      void sceneId;
      void resolve;
      latestSignal = signal;
      signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    }));
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01" });
    await vi.waitFor(() => expect(latestSignal).toBeTruthy(), T);

    await view.unmount();

    expect(latestSignal.aborted).toBe(true);
  });

  it("is mounted by the real scene page instead of shipping as a dead component", () => {
    // 页面只负责摆放：控制条挂在 ws-scene.jsx 的管线行里，它报来的任务由 useSceneRuns 记成权威任务
    const page = readFileSync("src/ws-scene.jsx", "utf8");
    expect(page).toMatch(/<SceneRunJobControl[\s\S]*?onJobChange=\{run\.onJobChange\}/);
    const state = readFileSync("src/ws-scene-board-state.js", "utf8");
    expect(state).toContain("authoritativeRunJob");
    expect(state).toMatch(/const onJobChange = \(job\) =>/);
  });

  it("restores latest running state and cancel control in the real scene page", async () => {
    const { client } = await loadSceneRun();
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-page-refresh",
      scene_id: "s1",
      status: "running",
      current_step: "neutral_running",
    });
    const page = await import("./ws-scene.jsx");

    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(client.getLatestSceneRunJob).toHaveBeenCalledWith(
        "s1",
        expect.objectContaining({ signal: expect.anything() }),
      );
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-page-refresh");
      expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy();
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("中性稿");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("neutral_running");
  });

  it("keeps authoritative queued distinct from running and suppresses a duplicate start", async () => {
    const { client } = await loadSceneRun();
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-page-queued",
      scene_id: "s1",
      status: "queued",
      current_step: "queued",
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(view.host.querySelector(".scn2-state-tag")?.textContent).toContain("排队");
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("排队中");
      expect(view.host.querySelector('[data-testid="scene-run-cancel-button"]')).toBeTruthy();
    }, T);
    const executableStart = Array.from(view.host.querySelectorAll("button"))
      .find((button) => button.textContent.includes("开始起草") && !button.disabled);
    expect(executableStart).toBeUndefined();
  });

  it("selects the first backend-restored scene from an empty local queue and aligns stage, row, and counts", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    routeRunStates(client, () => Promise.resolve({
      items: [{ scene_id: "s1", scene_status: "neutral_running" }],
    }));
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-empty-restore",
      scene_id: "s1",
      status: "running",
      current_step: "neutral_running",
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(view.host.textContent).not.toContain("运行队列还是空的");
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-empty-restore");
      expect(view.host.querySelector(".scn2-state-tag")?.textContent).toContain("运行");
      expect(view.host.querySelector(".scn2-qrow.is-active .scn2-chip")?.textContent).toContain("运行");
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("运行中 · 中性稿");
    }, T);
    // 书脊头：后端恢复的这一场按后端说的算——「1 场正在运行」，在办 1、待复核 0
    //（过去是四块统计：一场没提交过的在办场会被数成「排队」）
    expect(view.host.querySelector(".scn2-spine-live")?.textContent).toContain("1 场正在运行");
    expect(view.host.querySelector('[data-testid="scene-spine-filter-active"] .seg-count')?.textContent).toBe("1");
    expect(view.host.querySelector('[data-testid="scene-spine-filter-review"] .seg-count')?.textContent).toBe("0");
  });

  it.each([
    { terminal: "completed", expectedRow: "待复核", expectReview: true },
    { terminal: "cancelled", expectedRow: "待起草", expectReview: false },
  ])("reconciles stale local running after A → B → A when latest is $terminal", async ({ terminal, expectedRow, expectReview }) => {
    const { mod, client } = await loadSceneRun({
      projects: [NON_DEMO_PROJECT],
      catalog: [TWO_SCENE_CHAP],
    });
    mod.scnRunSave("ch01s1", {
      state: "running",
      progress: 0.4,
      attempt: 1,
      draft: [],
      metrics: [],
      alignment: [],
      cost: [],
      log: [],
    });
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    const notFound = Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" });
    let aReads = 0;
    client.getLatestSceneRunJob.mockImplementation((sceneId) => {
      if (sceneId === "s2") return Promise.reject(notFound);
      aReads += 1;
      return Promise.resolve({
        job_id: "job-scene-a",
        scene_id: "s1",
        status: aReads === 1 ? "running" : terminal,
    });
  });

    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "ready" },
        });
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[role="status"]')?.textContent).toContain("运行中"), T);

    const rowByTitle = (title) => Array.from(view.host.querySelectorAll(".scn2-qrow"))
      .find(row => row.textContent.includes(title));
    await click(rowByTitle("回潮"));
    await vi.waitFor(() => expect(client.getLatestSceneRunJob.mock.calls.some(([sceneId]) => sceneId === "s2")).toBe(true), T);
    await click(rowByTitle("交班"));

    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe(terminal);
    }, T);
    if (terminal === "completed") {
      await vi.waitFor(() => expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/scenes/s1/workbench")).toBe(true), T);
    }
    await act(async () => { await Promise.resolve(); });
    expect(view.host.querySelector(".scn2-qrow.is-active .scn2-chip")?.textContent).toContain(expectedRow);
    expect(view.host.querySelector(".scn2-run")).toBeNull();
    if (expectReview) {
      expect(view.host.querySelector(".scn2-review")).toBeTruthy();
    }
  });

  it("restores a fresh-browser no-content budget block and resumes with the original author instruction", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const authorNote = "保留此前完整的作者指令，不要重置叙事视角。".repeat(12);
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-fresh-budget",
      scene_id: "s1",
      status: "blocked",
      current_step: "style_running",
      error_code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
      error_text: "scene token budget exhausted before dispatch",
      author_note: authorNote,
    });
    let resumed = false;
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        if (resumed) {
          return Promise.resolve({
            style_draft: { content: "潮水退去。\n她留下了证词。" },
            scene_run_state: { scene_status: "near_final" },
          });
        }
        return Promise.resolve({
          scene_run_state: {
            scene_status: "bundle_built",
            lifecycle_budget: {
              baseline_tokens: 6800,
              recommended_topup_tokens: 6800,
            },
          },
        });
      }
      return baseGet(url, options);
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (url === "/api/v1/scenes/s1/budget/topup") return Promise.resolve({});
      if (url === "/api/v1/scenes/s1/run/jobs") {
        resumed = true;
        return Promise.resolve({
          job_id: "job-fresh-budget-resumed",
          scene_id: "s1",
          status: "completed",
        });
      }
      return basePost(url, body, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(view.host.textContent).toContain("尚未产出正文");
      expect(view.host.querySelector('[data-testid="scene-budget-topup"]')).toBeTruthy();
      expect(mod.scnRunLoad("ch01s1")).toMatchObject({
        state: "queued",
        authorNote,
        budgetBlock: { code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED" },
      });
    }, T);

    await click(view.host.querySelector('[data-testid="scene-budget-topup"]'));
    await vi.waitFor(() => {
      expect(client.apiPost).toHaveBeenCalledWith(
        "/api/v1/scenes/s1/run/jobs",
        { run_policy: "strict", author_note: authorNote, resume_budget: true },
        expect.objectContaining({ signal: expect.anything() }),
      );
    }, T);
  });

  it("clears stale running immediately while completed workbench recovery is still pending", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", {
      state: "running",
      progress: 0.6,
      attempt: 1,
      draft: [],
      metrics: [],
      alignment: [],
      cost: [],
      log: [],
    });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-completed-pending-workbench",
      scene_id: "s1",
      status: "completed",
    });
    let workbenchSignal = null;
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, { signal } = {}) => {
      if (url !== "/api/v1/scenes/s1/workbench" || !signal) return baseGet(url);
      workbenchSignal = signal;
      return new Promise((resolve, reject) => {
        void resolve;
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(workbenchSignal).toBeTruthy();
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe("completed");
      expect(view.host.querySelector(".scn2-qrow.is-active .scn2-chip")?.textContent).toContain("待起草");
      expect(view.host.querySelector(".scn2-run")).toBeNull();
      expect(view.host.textContent).toContain("正在恢复产出");
    }, T);

    await view.unmount();
    expect(workbenchSignal.aborted).toBe(true);
  });

  it.each([
    { cachedState: "ready", expectedLabel: "待复核", rejectWorkbench: true },
    // 书脊上写完归档的场和目录里写完的场同一个词（ws-labels 的 SCENE_STATE_META.done）
    { cachedState: "archived", expectedLabel: "已完成", rejectWorkbench: false },
  ])("preserves a usable cached $cachedState result when completed workbench recovery has no data", async ({ cachedState, expectedLabel, rejectWorkbench }) => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const cached = {
      ...mod.scnQC([{ id: "p1", text: "潮水退去，她留下了证词。" }]),
      state: cachedState,
      progress: 1,
      attempt: 1,
      attempts: [],
      cost: [],
      log: [],
    };
    mod.scnRunSave("ch01s1", cached);
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: `job-cached-${cachedState}`,
      scene_id: "s1",
      status: "completed",
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url !== "/api/v1/scenes/s1/workbench") return baseGet(url, options);
      return rejectWorkbench ? Promise.reject(new Error("temporary network")) : Promise.resolve({});
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => {
      expect(client.apiGet.mock.calls.some(([url]) => url === "/api/v1/scenes/s1/workbench")).toBe(true);
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.status).toBe("completed");
      expect(view.host.querySelector(".scn2-qrow.is-active .scn2-chip")?.textContent).toContain(expectedLabel);
      expect(view.host.querySelector(".scn2-run")).toBeNull();
    }, T);
    expect(mod.scnRunLoad("ch01s1")).toMatchObject({ state: cachedState });
    expect(mod.scnRunLoad("ch01s1").draft.length).toBeGreaterThan(0);
  });

  it("真实起草台发现作者正文时打开差异决策，默认焦点落在“保存为候选”且不归档", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const cached = {
      ...mod.scnQC([{ id: "p1", text: "AI 写下了另一种开场。" }]),
      state: "ready", progress: 1, attempt: 1, attempts: [], cost: [], log: [],
    };
    mod.scnRunSave("ch01s1", cached);
    await queueSceneIntent({ sid: "ch01s1" });
    window.localStorage.setItem(window.wsKey("wr-doc:ch01s1"), "<p>作者亲写的开场。</p>");
    client.getLatestSceneRunJob.mockRejectedValue(Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }));
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    await click(view.host.querySelector('[data-testid="scene-archive"]'));
    const dialog = document.body.querySelector(".scn2-adopt");
    expect(dialog).toBeTruthy();
    expect(dialog.getAttribute("role")).toBe("dialog");
    // 头部说的是哪一场（章 · 场 · 题名），不是后端 id
    expect(dialog.textContent).toContain("第 1 章 · 第 1 场「交班」");
    expect(dialog.textContent).not.toContain("ch01s1");
    expect(dialog.textContent).toContain("作者当前稿");
    expect(dialog.textContent).toContain("AI 候选稿");
    const safe = dialog.querySelector('[data-testid="scene-save-candidate"]');
    const overwrite = dialog.querySelector('[data-testid="scene-confirm-overwrite"]');
    await vi.waitFor(() => expect(document.activeElement).toBe(safe), T);
    expect(overwrite.disabled).toBe(true);

    await click(safe);
    await vi.waitFor(() => expect(document.body.querySelector(".scn2-adopt")).toBeNull(), T);
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
    expect(window.localStorage.getItem(window.wsKey("wr-doc:ch01s1"))).toBe("<p>作者亲写的开场。</p>");
    expect(window.WrRecovery.list()).toEqual([expect.objectContaining({ type: "candidate", source: "ai" })]);
    expect(view.host.textContent).toContain("作者正文没有被改动");
  });

  it("真实起草台收到内容安全 409 后逐项确认，并只携 exact code 重试归档", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const cached = {
      ...mod.scnQC([{ id: "p1", text: "角色只有16岁，段落明确描写两人的性行为。" }]),
      state: "ready", progress: 1, attempt: 1, attempts: [], cost: [], log: [],
    };
    mod.scnRunSave("ch01s1", cached);
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    const reviewError = Object.assign(new Error("review required"), {
      code: "CONTENT_SAFETY_REVIEW_REQUIRED",
      status: 409,
      details: {
        final_text_gate: {
          content_safety: {
            findings: [{
              code: "sexual_content_with_minor_indicators",
              review_required: true,
              acknowledged: false,
              severity: "high",
              confidence: "heuristic",
              message: "核对人物年龄与叙事目的。",
              evidence_terms: ["age:16", "性行为"],
            }],
            limitations: ["启发式不能判断完整语境。"],
          },
        },
      },
    });
    const basePost = client.apiPost.getMockImplementation();
    const adoptBodies = [];
    client.apiPost.mockImplementation((url, body, options) => {
      if (!/\/api\/v1\/scenes\/s1\/adopt-current$/.test(url)) {
        return basePost(url, body, options);
      }
      adoptBodies.push(body);
      return body.accepted_warning_codes.length
        ? Promise.resolve({
            scene_status: "archived",
            final_scene_row_id: "final_s1_v1",
            draft_id: body.exact_author_draft.draft_id,
            draft_revision_no: 2,
            author_draft: {
              ...body.exact_author_draft,
              revision_no: 2,
              last_promoted_revision_no: 2,
              last_promoted_final_scene_row_id: "final_s1_v1",
              canonical_dirty: false,
            },
          })
        : Promise.reject(reviewError);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    await click(view.host.querySelector('[data-testid="scene-archive"]'));
    await vi.waitFor(() => expect(document.body.querySelector(".wr-safety-dialog")).toBeTruthy(), T);
    const dialog = document.body.querySelector(".wr-safety-dialog");
    // 风险代码不再印给作者，挂在条目的 data-code 上（确认时原样回传）
    expect(dialog.querySelector('[data-code="sexual_content_with_minor_indicators"]')).toBeTruthy();
    const confirm = dialog.querySelector('[data-testid="content-safety-confirm"]');
    expect(confirm.disabled).toBe(true);

    await click(dialog.querySelector('input[type="checkbox"]'));
    expect(confirm.disabled).toBe(false);
    await click(confirm);

    await vi.waitFor(() => expect(document.body.querySelector(".wr-safety-dialog")).toBeNull(), T);
    expect(adoptBodies).toEqual([
      expect.objectContaining({
        accepted_warning_codes: [],
        exact_author_draft: expect.objectContaining({ draft_id: "author_draft_scene_s1", base_revision_no: 1 }),
      }),
      expect.objectContaining({
        accepted_warning_codes: ["sexual_content_with_minor_indicators"],
        exact_author_draft: expect.objectContaining({ draft_id: "author_draft_scene_s1", base_revision_no: 1 }),
      }),
    ]);
    expect(view.host.textContent).toContain("已归档并写入正文文档");
  });

  it("持久化运行记录保留预算续跑所需的原作者指令", async () => {
    const { mod } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const authorNote = "保留原视角与人物动机。";

    mod.scnRunSave("ch01s1", {
      state: "queued",
      draft: [],
      metrics: [],
      alignment: [],
      log: [],
      attempts: [],
      budgetBlock: { code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED" },
      authorNote,
    });

    expect(mod.scnRunLoad("ch01s1")).toMatchObject({ authorNote });
  });

  it("退回重写在界面上明确提示 2000 字上限，超限时不提交也不截断", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const cached = {
      ...mod.scnQC([{ id: "p1", text: "潮水退去，她留下了证词。" }]),
      state: "ready", progress: 1, attempt: 1, attempts: [], cost: [], log: [],
    };
    mod.scnRunSave("ch01s1", cached);
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);
    const rework = Array.from(view.host.querySelectorAll("button"))
      .find(button => button.textContent.includes("退回重写"));
    await click(rework);
    const textarea = view.host.querySelector(".scn2-rework-input");
    const overlong = "改".repeat(2001);
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(textarea, overlong);
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    });

    expect(textarea.value).toBe(overlong);
    expect(textarea.getAttribute("aria-invalid")).toBe("true");
    expect(view.host.querySelector('[role="alert"]')?.textContent).toContain("不会静默截断");
    const submit = Array.from(view.host.querySelectorAll("button"))
      .find(button => button.textContent.includes("确认退回重写"));
    expect(submit.disabled).toBe(true);
    expect(client.apiPost.mock.calls.filter(([url]) => /\/run\/jobs$/.test(url))).toEqual([]);
  });

  /* 2026-09-20 真实故障：预检拦下的任务，作者只看到一句「任务已阻断…请检查阻断原因后重试」——原因没说，
     也没有地方可查。终态任务恢复（effect）与 startRun 的 catch 现在说同一句：任务自己留下的原因。 */
  // 裁决条那一句（页面上还有一条任务控制条，同样是 .scn2-decide）
  const decideSummary = (host) => host.querySelector('[data-testid="scene-start"]')
    ?.closest(".scn2-decide")?.querySelector(".scn2-decide-sum")?.textContent || "";
  it("预检阻断且没有草稿：起草台说任务留下的原因，而不是一句笼统的「请检查阻断原因」", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-preflight-blocked",
      scene_id: "s1",
      status: "blocked",
      current_step: "preflight_blocked",
      error_code: "SCENE_EXECUTION_CONTRACT_BLOCKED",
      error_text: "Fill the missing execution contract fields before drafting: scene_turn",
      missing_fields: ["scene_turn"],
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve({ scene_run_state: { scene_status: "ready" } });
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(decideSummary(view.host)).toContain("执行契约还缺关键字段"), T);
    const summary = decideSummary(view.host);
    expect(summary).toContain("scene_turn");
    expect(summary).not.toContain("请检查阻断原因");
    expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy();
  });

  it("点了开始起草被预检拦下：catch 与终态恢复两条路落在同一句原因上", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    const blocked = {
      job_id: "job-click-blocked",
      scene_id: "s1",
      status: "blocked",
      current_step: "preflight_blocked",
      error_code: "SCENE_EXECUTION_CONTRACT_BLOCKED",
      error_text: "Fill the missing execution contract fields before drafting: scene_turn",
      missing_fields: ["scene_turn"],
    };
    let created = false;
    client.getLatestSceneRunJob.mockImplementation(() => (created
      ? Promise.resolve(blocked)
      : Promise.reject(Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }))));
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve({ scene_run_state: { scene_status: "ready" } });
      if (url === "/api/v1/run-jobs/job-click-blocked") return Promise.resolve(blocked);
      return baseGet(url, options);
    });
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options) => {
      if (url === "/api/v1/scenes/s1/run/jobs") { created = true; return Promise.resolve(blocked); }
      return basePost(url, body, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-start"]'));

    await vi.waitFor(() => expect(decideSummary(view.host)).toContain("执行契约还缺关键字段"), T);
    // 两条路都写完之后仍然是这一句（过去后写的那条会把它盖成「请检查阻断原因后重试」）
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 60)); });
    const summary = decideSummary(view.host);
    expect(summary).toContain("scene_turn");
    expect(summary).not.toContain("请检查阻断原因");
    expect(summary).not.toContain("正在恢复");
  });

  it("取消之前留下的「声线 / 关系卡」阻断任务：如实说检查已取消，出口是重新起草，不再有补卡按钮", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    expect(mod.scnCreateCards).toBeUndefined();
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-legacy-voice",
      scene_id: "s1",
      status: "blocked",
      current_step: "preflight_blocked",
      error_code: "VOICE_PROFILE_MISSING",
      error_text: "请先补齐当前 POV 角色的可用声线档案，再执行完整场景运行。",
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") return Promise.resolve({ scene_run_state: { scene_status: "ready" } });
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });

    await vi.waitFor(() => expect(decideSummary(view.host)).toContain("这项检查已经取消"), T);
    expect(view.host.querySelector('[data-testid="scene-create-cards"]')).toBeNull();
    expect(view.host.querySelector('[data-testid="scene-start"]')).toBeTruthy();
    expect(client.apiPost.mock.calls.some(([url]) => /preflight\/create-cards/.test(url))).toBe(false);
  });

  it("追加预算等待期间切换场景，续跑仍绑定原场景和原作者指令", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT], catalog: [TWO_SCENE_CHAP] });
    const authorNote = "保留第一场的限知视角";
    mod.scnRunSave("ch01s1", {
      state: "queued", progress: 0, draft: [], metrics: [], alignment: [], cost: [], log: [], attempts: [],
      authorNote,
      budgetBlock: {
        code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        label: "本场 token 生命周期预算已到派发边界",
        topup: { extra_tokens: 6400 },
      },
    });
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    const topup = deferred();
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options = {}) => {
      if (url === "/api/v1/scenes/s1/budget/topup") return topup.promise;
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return new Promise((resolve, reject) => {
          void resolve;
          options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }
      return basePost(url, body, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-budget-topup"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-budget-topup"]'));
    const rowB = Array.from(view.host.querySelectorAll(".scn2-qrow")).find(row => row.textContent.includes("回潮"));
    await click(rowB);
    await act(async () => { topup.resolve({}); await Promise.resolve(); });

    await vi.waitFor(() => expect(client.apiPost).toHaveBeenCalledWith(
      "/api/v1/scenes/s1/run/jobs",
      { run_policy: "strict", author_note: authorNote, resume_budget: true },
      expect.objectContaining({ signal: expect.anything() }),
    ), T);
    expect(client.apiPost.mock.calls.some(([url]) => url === "/api/v1/scenes/s2/run/jobs")).toBe(false);
  });

  it("归档预检同步防双击，切换场景后不弹出旧场景的作者稿决策", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT], catalog: [TWO_SCENE_CHAP] });
    const ready = (text) => ({
      ...mod.scnQC([{ id: "p1", text }]),
      state: "ready", progress: 1, attempt: 1, attempts: [], cost: [], log: [],
    });
    mod.scnRunSave("ch01s1", ready("第一场 AI 稿。"));
    mod.scnRunSave("ch01s2", ready("第二场 AI 稿。"));
    await queueSceneIntent({ sids: ["ch01s1", "ch01s2"] });
    window.localStorage.setItem(window.wsKey("wr-doc:ch01s1"), "<p>第一场作者稿。</p>");
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    const hydration = deferred();
    const hydrateSpy = vi.spyOn(window.WrDocs, "hydrate").mockImplementation((sid) => (
      sid === "ch01s1" ? hydration.promise : Promise.resolve(null)
    ));
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector('[data-testid="scene-archive"]')).toBeTruthy(), T);

    await click(view.host.querySelector('[data-testid="scene-archive"]'));
    await vi.waitFor(() => {
      const button = view.host.querySelector('[data-testid="scene-archive"]');
      expect(button.disabled).toBe(true);
      expect(button.textContent).toContain("正在核对作者稿");
    }, T);
    await click(view.host.querySelector('[data-testid="scene-archive"]'));
    expect(hydrateSpy.mock.calls.filter(([sid]) => sid === "ch01s1")).toHaveLength(1);

    const rowB = Array.from(view.host.querySelectorAll(".scn2-qrow")).find(row => row.textContent.includes("回潮"));
    await click(rowB);
    await act(async () => { hydration.resolve(null); await Promise.resolve(); });
    await act(async () => { await Promise.resolve(); });

    expect(document.body.querySelector(".scn2-adopt")).toBeNull();
    expect(view.host.querySelector(".scn2-qrow.is-active")?.textContent).toContain("回潮");
    expect(client.apiPost.mock.calls.filter(([url]) => /adopt-current/.test(url))).toEqual([]);
  });


  it("尝试历史只把复盘意见加入当前稿重写，不宣称恢复历史正文 base", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    mod.scnRunSave("ch01s1", {
      ...mod.scnQC([{ id: "p1", text: "当前复核稿。" }]),
      state: "ready", progress: 1, attempt: 2, cost: [], log: [],
      attempts: [
        { n: 2, time: "本次 · 待裁决", result: "待裁决", tone: "gold", note: "当前稿" },
        { n: 1, time: "昨日", result: "质检阻断", tone: "rose", note: "视角泄漏", cmp: { verdict: "第 1 次尝试存在视角泄漏。" } },
      ],
    });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    const basePost = client.apiPost.getMockImplementation();
    client.apiPost.mockImplementation((url, body, options = {}) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return new Promise((resolve, reject) => {
          void resolve;
          options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        });
      }
      return basePost(url, body, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(view.host.querySelector(".scn2-try-view")).toBeTruthy(), T);

    await click(view.host.querySelector(".scn2-try-view"));
    expect(view.host.querySelector(".scn2-cmp")?.textContent).toContain("不包含可恢复的历史正文快照");
    expect(view.host.querySelector('[data-testid="scene-attempt-rewrite"]')?.textContent).toContain("参考该版复盘意见重写");
    await click(view.host.querySelector('[data-testid="scene-attempt-rewrite"]'));

    await vi.waitFor(() => expect(client.apiPost.mock.calls.some(([url]) => url === "/api/v1/scenes/s1/run/jobs")).toBe(true), T);
    const [, body] = client.apiPost.mock.calls.find(([url]) => url === "/api/v1/scenes/s1/run/jobs");
    expect(body.author_note).toContain("参考第 1 次尝试的复盘意见重写");
    expect(body.author_note).toContain("不恢复该版正文，以当前稿为输入");
    expect(body.author_note).not.toContain("为基础重写");
    expect(body).not.toHaveProperty("previous_draft");
  });

  it("stops the real page progress timer and scnRun polling after unmount", async () => {
    const { client } = await loadSceneRun();
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-unmount", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => {
      const start = Array.from(view.host.querySelectorAll("button"))
        .find((button) => button.textContent.includes("开始起草"));
      expect(start).toBeTruthy();
    }, T);
    const start = Array.from(view.host.querySelectorAll("button"))
      .find((button) => button.textContent.includes("开始起草"));
    await click(start);
    await vi.waitFor(() => {
      expect(client.apiPost).toHaveBeenCalledWith(
        "/api/v1/scenes/s1/run/jobs",
        { run_policy: "strict" },
        expect.objectContaining({ signal: expect.anything() }),
      );
    }, T);

    await view.unmount();
    client.apiGet.mockClear();
    await new Promise((resolve) => setTimeout(resolve, 2100));

    const stalePolls = client.apiGet.mock.calls.filter(([url]) => url === "/api/v1/run-jobs/job-unmount");
    expect(stalePolls).toEqual([]);
  });

  it("aborts a pending create-job POST when the real page unmounts", async () => {
    const { client } = await loadSceneRun();
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404, code: "RUN_JOB_NOT_FOUND" }),
    );
    let postSignal = null;
    client.apiPost.mockImplementation((url, body, { signal } = {}) => {
      if (!/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) return Promise.resolve({});
      void body;
      postSignal = signal;
      return new Promise((resolve, reject) => {
        void resolve;
        signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
      });
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => expect(Array.from(view.host.querySelectorAll("button"))
      .some(button => button.textContent.includes("开始起草"))).toBe(true), T);
    const start = Array.from(view.host.querySelectorAll("button"))
      .find(button => button.textContent.includes("开始起草"));
    await click(start);
    await vi.waitFor(() => expect(postSignal).toBeTruthy(), T);

    await view.unmount();

    expect(postSignal.aborted).toBe(true);
  });
});

/* 2026-09-12 风格直起（Step 2，Track C）：运行任务横幅的 current_step 中文标签——
   neutral_running 步位在 draft_mode=style_first 下写的是作者手笔首稿；起草方式来自
   workbench generation_summary.draft_mode，随运行记录持久化并由场景页传给横幅。 */
describe("scene run step labels（风格直起）", () => {
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

  it("runJobStepLabel：管线词表映射中文；neutral_running 按起草方式切换；未知 token 原样回显", async () => {
    const { mod } = await loadSceneRun();
    expect(mod.runJobStepLabel("neutral_running", "style_first")).toBe("首稿（作者手笔）");
    expect(mod.runJobStepLabel("neutral_running", "neutral_first")).toBe("中性稿");
    expect(mod.runJobStepLabel("neutral_running", null)).toBe("中性稿");
    expect(mod.runJobStepLabel("planning_running")).toBe("规划蓝图");
    expect(mod.runJobStepLabel("bundle_built")).toBe("上下文已冻结");
    expect(mod.runJobStepLabel("hard_qc_running")).toBe("硬质检");
    expect(mod.runJobStepLabel("style_running", "style_first")).toBe("风格稿");
    expect(mod.runJobStepLabel("soft_qc_running")).toBe("软质检");
    expect(mod.runJobStepLabel("rewrite_running")).toBe("近终稿改写");
    expect(mod.runJobStepLabel("acceptance_review_running")).toBe("近终稿评审");
    expect(mod.runJobStepLabel("near_final")).toBe("近终稿");
    expect(mod.runJobStepLabel("archived")).toBe("已归档");
    expect(mod.runJobStepLabel("awaiting_candidate_selection")).toBe("等你终选");
    expect(mod.runJobStepLabel("some_future_step")).toBe("some_future_step");
    expect(mod.runJobStepLabel("")).toBe("");
    expect(mod.runJobStepLabel(null)).toBe("");
  });

  it("scnDraftModeFrom：只认 workbench generation_summary 里的两个取值", async () => {
    const { mod } = await loadSceneRun();
    expect(mod.scnDraftModeFrom({ generation_summary: { draft_mode: "style_first" } })).toBe("style_first");
    expect(mod.scnDraftModeFrom({ generation_summary: { draft_mode: "neutral_first" } })).toBe("neutral_first");
    expect(mod.scnDraftModeFrom({ generation_summary: { draft_mode: "whatever" } })).toBeNull();
    expect(mod.scnDraftModeFrom({ generation_summary: null })).toBeNull();
    expect(mod.scnDraftModeFrom(null)).toBeNull();
  });

  it("横幅按 draftMode 属性给 neutral_running 打标签：style_first → 首稿（作者手笔）", async () => {
    const { mod, client } = await loadSceneRun();
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-style-first",
      scene_id: "SC01",
      status: "running",
      current_step: "neutral_running",
    });
    const view = await renderRunJobControl(mod.SceneRunJobControl, { sceneId: "SC01", draftMode: "style_first" });
    await vi.waitFor(() => {
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("运行中 · 首稿（作者手笔）");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("neutral_running");

    await view.rerender({ sceneId: "SC01", draftMode: "neutral_first" });
    await vi.waitFor(() => {
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("运行中 · 中性稿");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("作者手笔");
  });

  it("scnHydrateFromBackend：把 generation_summary.draft_mode 记到运行记录，并随 scnRunSave 持久化", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "neutral_running" },
          generation_summary: { draft_mode: "style_first", attempts: 1 },
        });
      }
      return baseGet(url, options);
    });
    const restored = await mod.scnHydrateFromBackend("ch01s1", {});
    expect(restored).toBeTruthy();
    expect(restored.draftMode).toBe("style_first");
    mod.scnRunSave("ch01s1", restored);
    expect(mod.scnRunLoad("ch01s1").draftMode).toBe("style_first");
  });

  it("scnHydrateFromBackend：无正文的预算断点同样带起草方式", async () => {
    const { mod, client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          scene_run_state: { scene_status: "bundle_built", lifecycle_budget: { baseline_tokens: 7200, recommended_topup_tokens: 7200 } },
          generation_summary: { draft_mode: "neutral_first" },
        });
      }
      return baseGet(url, options);
    });
    const restored = await mod.scnHydrateFromBackend("ch01s1", {
      terminalJob: {
        job_id: "job-budget",
        scene_id: "s1",
        status: "blocked",
        current_step: "neutral_running",
        error_code: "LLM_SCENE_TOKEN_BUDGET_EXHAUSTED",
        error_text: "scene token budget exhausted before dispatch",
      },
    });
    expect(restored).toMatchObject({ recoveredWithoutDraft: true, draftMode: "neutral_first" });
  });

  it("scnRun：运行记录带 draftMode，运行日志写新的管线句与首稿方式", async () => {
    const { mod, client } = await loadSceneRun();
    const baseGet = client.apiGet.getMockImplementation();
    client.apiPost.mockImplementation((url) => {
      if (/\/api\/v1\/scenes\/s1\/run\/jobs$/.test(url)) {
        return Promise.resolve({ job_id: "job-style", scene_id: "s1", status: "running" });
      }
      return Promise.resolve({});
    });
    client.apiGet.mockImplementation((url) => {
      if (url === "/api/v1/run-jobs/job-style") {
        return Promise.resolve({ job_id: "job-style", scene_id: "s1", status: "completed", current_step: "near_final" });
      }
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          style_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "near_final" },
          generation_summary: { draft_mode: "style_first" },
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
    expect(result.draftMode).toBe("style_first");
    const texts = result.log.map((l) => l.text);
    expect(texts[0]).toBe("已提交后端起草任务（预检 → 蓝图 → 首稿 → 硬质检 → 风格稿 → 软质检 → 近终稿）");
    expect(texts.some((t) => t.includes("首稿 作者手笔"))).toBe(true);
  });

  it("真实场景页：从 workbench 恢复的起草方式传到横幅，running · neutral_running 读作首稿（作者手笔）", async () => {
    const { client } = await loadSceneRun({ projects: [NON_DEMO_PROJECT] });
    await queueSceneIntent({ sid: "ch01s1" });
    client.getLatestSceneRunJob.mockResolvedValue({
      job_id: "job-page-style-first",
      scene_id: "s1",
      status: "running",
      current_step: "neutral_running",
    });
    const baseGet = client.apiGet.getMockImplementation();
    client.apiGet.mockImplementation((url, options) => {
      if (url === "/api/v1/scenes/s1/workbench") {
        return Promise.resolve({
          neutral_draft: { content: "潮水退去。\n她留下了证词。" },
          scene_run_state: { scene_status: "neutral_running" },
          generation_summary: { draft_mode: "style_first" },
        });
      }
      return baseGet(url, options);
    });
    const page = await import("./ws-scene.jsx");
    const view = await renderRunJobControl(page.WsScene, { go: vi.fn(), t: {} });
    await vi.waitFor(() => {
      expect(view.host.querySelector('[data-testid="scene-run-job-control"]')?.dataset.jobId).toBe("job-page-style-first");
      expect(view.host.querySelector('[role="status"]')?.textContent).toContain("首稿（作者手笔）");
    }, T);
    expect(view.host.querySelector('[role="status"]')?.textContent).not.toContain("neutral_running");
  });
});
